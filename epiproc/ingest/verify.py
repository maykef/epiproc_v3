"""Deterministic checks on extracted invoice data.

Run order in audit_db():
  1. Schema validation  (required fields, data types)
  2. Date checks        (format, plausible range, future-date flags)
  3. Totals vs items    (reconciliation, item arithmetic)
  4. Anomaly detection  (outliers, duplicate invoice numbers, error rate)

LLM is NOT called here.  These checks produce a structured AuditReport that
audit.py passes to Claude when interpretation or config fixes are needed.
"""

from __future__ import annotations

import re
import sqlite3
import statistics
from dataclasses import dataclass, field
from datetime import date, datetime
from pathlib import Path
from typing import Literal

from pipeline.config import SupplierConfig

APP_DIR = Path(__file__).parent.parent.parent

Severity = Literal["error", "warning", "info"]

# ── Thresholds ────────────────────────────────────────────────────────────────

TOTALS_ERROR_THRESHOLD   = 0.20  # items_net vs subtotal >20% → error
TOTALS_WARN_THRESHOLD    = 0.05  # 5–20% → warning
ITEM_ARITH_THRESHOLD     = 0.02  # qty × unit_price vs total_price >2% → warning
OUTLIER_ZSCORE_THRESHOLD = 3.0   # log Z-score for invoice total outlier detection
FUTURE_DATE_WARN_DAYS    = 90    # days ahead before flagging as possible swap
ERROR_RATE_WARN          = 0.05  # extraction error fraction threshold

DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


# ── Result types ──────────────────────────────────────────────────────────────

@dataclass
class Finding:
    check_id: str
    severity: Severity
    message: str
    invoice_id: int | None = None   # None → dataset-level finding


@dataclass
class AuditReport:
    supplier: str
    run_at: datetime
    total_invoices: int
    extraction_errors: int
    findings: list[Finding] = field(default_factory=list)

    @property
    def passed(self) -> bool:
        return not any(f.severity == "error" for f in self.findings)

    @property
    def error_count(self) -> int:
        return sum(1 for f in self.findings if f.severity == "error")

    @property
    def warning_count(self) -> int:
        return sum(1 for f in self.findings if f.severity == "warning")

    @property
    def flagged_invoice_ids(self) -> set[int]:
        return {f.invoice_id for f in self.findings if f.invoice_id is not None}

    def summary(self) -> str:
        lines = [
            f"Supplier : {self.supplier}",
            f"Invoices : {self.total_invoices} extracted, {self.extraction_errors} extraction errors",
            f"Findings : {self.error_count} error(s), {self.warning_count} warning(s)",
            "",
        ]
        if not self.findings:
            lines.append("  No issues found.")
        for f in self.findings:
            loc = f"[invoice {f.invoice_id}]" if f.invoice_id else "[dataset]  "
            lines.append(f"  {f.severity.upper():7s}  {f.check_id:<4s}  {loc}  {f.message}")
        return "\n".join(lines)


# ── Individual checks ─────────────────────────────────────────────────────────

def _check_schema(rows: list[dict], findings: list[Finding], *,
                  zero_value_allowed: bool = False) -> None:
    """C1: Required fields present and non-empty; total_amount positive."""
    required = ("invoice_number", "invoice_date", "total_amount", "currency")
    for r in rows:
        iid = r["id"]
        doc_type_lower = (r.get("document_type") or "").lower()
        # Delivery notes have no financial fields by design — skip all C1 checks
        if doc_type_lower in ("delivery_note", "delivery note", "packing list"):
            continue
        # Credit/cancellation documents legitimately carry negative totals
        is_credit_or_cancel = "credit" in doc_type_lower or "cancellation" in doc_type_lower
        for col in required:
            val = r[col]
            # When zero is a valid total, treat 0.0 as present (not missing)
            if zero_value_allowed and col == "total_amount":
                missing = val is None
            else:
                missing = not val
            if missing:
                findings.append(Finding(
                    "C1", "error", f"Missing required field: {col}", iid,
                ))
        total = r["total_amount"]
        if total is not None and total < 0 and not is_credit_or_cancel:
            findings.append(Finding(
                "C1", "error", f"total_amount is negative: {total}", iid,
            ))
        elif total is not None and total == 0 and not zero_value_allowed:
            findings.append(Finding(
                "C1", "error", f"total_amount is non-positive: {total}", iid,
            ))


def _check_dates(rows: list[dict], findings: list[Finding]) -> None:
    """C2: Date format, plausible range, future-date / day-month-swap flags."""
    today = date.today()
    warn_cutoff = today.toordinal() + FUTURE_DATE_WARN_DAYS

    for r in rows:
        iid = r["id"]
        raw = r["invoice_date"]
        if not raw:
            continue  # already caught by C1

        if not DATE_RE.match(raw):
            findings.append(Finding(
                "C2", "error", f"invoice_date not YYYY-MM-DD: {raw!r}", iid,
            ))
            continue

        try:
            d = date.fromisoformat(raw)
        except ValueError:
            findings.append(Finding(
                "C2", "error", f"invoice_date does not parse: {raw!r}", iid,
            ))
            continue

        if d.year < 2020:
            findings.append(Finding(
                "C2", "warning", f"invoice_date unusually old: {raw}", iid,
            ))
        elif d.toordinal() > warn_cutoff:
            # Check if swapping day/month gives a plausible past date
            try:
                swapped = date(d.year, d.day, d.month)
                swap_note = f" (swapped would be {swapped.isoformat()})" if swapped <= today else ""
            except ValueError:
                swap_note = ""
            findings.append(Finding(
                "C2", "warning",
                f"invoice_date is >90 days in the future: {raw}{swap_note} — possible day/month swap",
                iid,
            ))


def _check_totals(rows: list[dict], findings: list[Finding], con: sqlite3.Connection) -> None:
    """C3: Line-item sum vs subtotal; subtotal + adjustments vs total_amount."""
    for r in rows:
        iid = r["id"]
        total = r["total_amount"]
        subtotal = r["subtotal"]
        if total is None:
            continue

        # Credit notes and cancellation invoices: items may carry mixed signs
        doc_type = (r.get("document_type") or "").lower()
        inv_num  = r.get("invoice_number") or ""
        is_credit_note = "credit" in doc_type or inv_num.upper().startswith("Z")
        is_cancellation = "cancellation" in doc_type

        # G3/KMAT-corrected invoices: subtotal was rewritten to match billed amount;
        # any residual gap is structural, not an extraction error.
        corrections = r.get("corrections_applied") or ""
        is_g3 = "G3:" in corrections
        is_kmat = "KMAT" in corrections
        is_g1 = "G1:" in corrections

        # G9: freight is embedded in subtotal; C3 anchor must be reduced so items_net
        # (which naturally excludes freight) can reconcile against subtotal.
        _g9_match = re.search(r"G9: nulled redundant freight ([0-9.]+)", corrections)
        g9_embedded_freight: float = float(_g9_match.group(1)) if _g9_match else 0.0

        # items_net excludes "Brought forward" carry rows (qty IS NULL AND unit_price IS NULL)
        items_net: float = con.execute(
            """
            SELECT COALESCE(SUM(total_price), 0)
            FROM invoice_items
            WHERE invoice_id = ?
              AND NOT (
                quantity IS NULL AND unit_price IS NULL
                AND LOWER(description) LIKE '%brought forward%'
              )
            """,
            (iid,),
        ).fetchone()[0] or 0.0

        item_cols = {row[1] for row in con.execute("PRAGMA table_info(invoice_items)").fetchall()}
        lda_sum: float = 0.0
        if "line_discount_amount" in item_cols:
            lda_sum = con.execute(
                "SELECT COALESCE(SUM(line_discount_amount), 0) FROM invoice_items WHERE invoice_id=?",
                (iid,),
            ).fetchone()[0] or 0.0

        # No item prices extracted — nothing to reconcile; skip silently
        if items_net == 0:
            continue

        # G4-corrected: subtotal already has discount subtracted; items_net is pre-discount
        is_g4 = "G4:" in corrections

        # For credit/cancellation use absolute values — sign is correct, direction isn't the check
        # For cancellation invoices, also use sum of absolute values per item (mixed signs expected)
        if is_cancellation:
            abs_items_net: float = con.execute(
                "SELECT COALESCE(SUM(ABS(total_price)), 0) FROM invoice_items WHERE invoice_id=?",
                (iid,),
            ).fetchone()[0] or 0.0
            effective_items_net = abs_items_net
        elif is_credit_note:
            effective_items_net = abs(items_net)
        elif is_g4:
            # G4 sets subtotal = item_price_sum, so items_net == subtotal by construction.
            # Don't subtract discount — items already carry NET prices after the new extraction.
            effective_items_net = items_net
        else:
            effective_items_net = items_net

        is_g2 = "G2:" in corrections
        anchor = subtotal if subtotal is not None else None

        # Garbled extraction: discount > subtotal is arithmetically impossible for a
        # normal invoice — indicates garbled credit-memo or extraction failure.  Skip C3.
        disc_raw = r["discount_amount"] or 0
        if anchor is not None and anchor > 0 and disc_raw > anchor * 1.5:
            findings.append(Finding(
                "C3", "info",
                f"items_net={items_net:.2f} vs subtotal={subtotal:.2f} — skipped (discount {disc_raw:.2f} > subtotal; garbled extraction)",
                iid,
            ))
            continue

        # G3-corrected: subtotal was set to total_excl_vat at extraction time; residual
        # gaps are due to discount fields, not extraction errors — use wider tolerance.
        if is_g3 and anchor is not None and anchor > 0:
            frac = abs(effective_items_net - anchor) / anchor
            if frac > 0.10:   # 10% tolerance for G3 (discount not in items_net)
                findings.append(Finding(
                    "C3", "warning",
                    f"items_net={items_net:.2f} vs subtotal={anchor:.2f} ({frac:.0%} off) [G3-scaled]",
                    iid,
                ))
            continue

        # KMAT bundles: items_net == total_amount by design (residual assigned to total)
        if is_kmat:
            continue

        # G1-corrected: only a partial residual was assigned (some items still have null price)
        # The items_net only covers the G1-assigned subset — use a very wide tolerance.
        if is_g1 and anchor is not None and anchor > 0:
            frac = abs(effective_items_net - anchor) / anchor
            if frac > 0.50:   # >50% off after G1 partial fix → warn, not error
                findings.append(Finding(
                    "C3", "warning",
                    f"items_net={items_net:.2f} vs subtotal={anchor:.2f} ({frac:.0%} off) [G1-partial]",
                    iid,
                ))
            continue

        if anchor is not None and anchor > 0:
            frac = abs(effective_items_net - anchor) / anchor
            # Invoice-level discount: some suppliers store gross unit prices;
            # (items_net - discount) should equal subtotal in those cases.
            disc = r["discount_amount"] or 0
            disc2 = r["discount_2"] or 0
            if disc + disc2 > 0:
                frac_disc = abs(effective_items_net - disc - disc2 - anchor) / anchor
                frac = min(frac, frac_disc)
            # Line-level discount: total_price extracted as gross but line_discount_amount set;
            # (items_net - lda_sum) should equal subtotal in those cases.
            if lda_sum > 0:
                frac_lda = abs(effective_items_net - lda_sum - anchor) / anchor
                frac = min(frac, frac_lda)
            # G9: freight embedded in subtotal but not in items_net — try subtracting it.
            # Only helps when items_net excludes freight (not when P&P is a line item).
            if g9_embedded_freight > 0 and not is_g4 and not is_g2:
                adj_anchor = anchor - g9_embedded_freight
                if adj_anchor > 0:
                    frac_g9 = abs(effective_items_net - adj_anchor) / adj_anchor
                    frac = min(frac, frac_g9)
            if frac > TOTALS_ERROR_THRESHOLD:
                findings.append(Finding(
                    "C3", "error",
                    f"items_net={items_net:.2f} vs subtotal={anchor:.2f} ({frac:.0%} off)",
                    iid,
                ))
            elif frac > TOTALS_WARN_THRESHOLD:
                findings.append(Finding(
                    "C3", "warning",
                    f"items_net={items_net:.2f} vs subtotal={anchor:.2f} ({frac:.0%} off)",
                    iid,
                ))
        elif items_net > 0:
            # No subtotal: for zero-VAT, zero-discount invoices items_net should ≈ total
            vat = r["vat_amount"] or 0
            disc = r["discount_amount"] or 0
            disc2 = r["discount_2"] or 0
            freight = r["freight"] or 0
            handling = r["handling_charges"] or 0
            # Only check when there are no adjustments that could explain the gap
            if vat == 0 and disc == 0 and disc2 == 0 and freight == 0 and handling == 0:
                frac = abs(items_net - total) / max(total, 1.0)
                if frac > TOTALS_ERROR_THRESHOLD:
                    findings.append(Finding(
                        "C3", "error",
                        f"items_net={items_net:.2f} vs total={total:.2f} ({frac:.0%} off) — no adjustments to explain gap",
                        iid,
                    ))
                elif frac > TOTALS_WARN_THRESHOLD:
                    findings.append(Finding(
                        "C3", "warning",
                        f"items_net={items_net:.2f} vs total={total:.2f} ({frac:.0%} off)",
                        iid,
                    ))


def _check_item_arithmetic(con: sqlite3.Connection, findings: list[Finding], *,
                           suppress: bool = False,
                           threshold: float = ITEM_ARITH_THRESHOLD) -> None:
    """C4: qty × unit_price ≈ total_price for every line item."""
    if suppress:
        return
    item_cols = {r[1] for r in con.execute("PRAGMA table_info(invoice_items)").fetchall()}
    lda_col = "ii.line_discount_amount" if "line_discount_amount" in item_cols else "NULL"
    rows = con.execute(
        f"""
        SELECT ii.id, ii.invoice_id, ii.quantity, ii.unit_price, ii.total_price,
               ii.description, {lda_col}, COALESCE(i.discount_amount, 0)
        FROM invoice_items ii
        JOIN invoices i ON i.id = ii.invoice_id
        WHERE ii.quantity IS NOT NULL
          AND ii.unit_price IS NOT NULL
          AND ii.total_price IS NOT NULL
          AND ii.total_price > 0
          AND i.extraction_error IS NULL
        """
    ).fetchall()

    # Zeiss "Protect" service contracts bill at a monthly per-instrument rate
    # but total_price = annual × n_instruments; qty×unit_price ≠ total_price by design.
    _SERVICE_PREFIXES = ("protect ", "planned maintenance", "preventive maintenance")

    for item_id, inv_id, qty, unit_price, total_price, desc, line_disc, inv_discount in rows:
        desc_lower = (desc or "").lower()
        if any(desc_lower.startswith(p) for p in _SERVICE_PREFIXES):
            continue
        # Invoice carries a global discount: unit_price is list price, total_price reflects
        # contract/framework pricing. C4 mismatch is expected and correct.
        if inv_discount > 0:
            continue
        expected = qty * unit_price
        frac = abs(expected - total_price) / total_price
        # If a line discount is explicitly captured, check gross - discount ≈ total_price
        if frac > threshold and line_disc is not None and line_disc > 0:
            frac_disc = abs(expected - line_disc - total_price) / total_price
            frac = min(frac, frac_disc)
        if frac > threshold:
            short_desc = (desc or "")[:50]
            findings.append(Finding(
                "C4", "warning",
                f"item arithmetic: {qty}×{unit_price:.4f}={expected:.2f} but total_price={total_price:.2f} "
                f"({frac:.1%} off) — {short_desc!r}",
                inv_id,
            ))


def _check_anomalies(rows: list[dict], findings: list[Finding], extraction_errors: int) -> None:
    """C5: Outlier totals (log Z-score), duplicate invoice numbers, high error rate.

    Invoice amounts follow a log-normal distribution — large legitimate purchases
    sit far above the median but are not extraction errors.  We use Z-score on
    log(total) so the threshold scales with the distribution rather than using
    an IQR fence that fires on every large invoice.
    """
    import math

    totals = [r["total_amount"] for r in rows if r["total_amount"] is not None]
    ids: dict[str, list[int]] = {}
    for r in rows:
        num = r["invoice_number"]
        if num:
            ids.setdefault(num, []).append(r["id"])

    # Duplicate invoice numbers
    for num, inv_ids in ids.items():
        if len(inv_ids) > 1:
            findings.append(Finding(
                "C5", "warning",
                f"duplicate invoice_number {num!r} appears {len(inv_ids)} times (ids: {inv_ids})",
            ))

    # Extraction error rate
    total_attempted = len(rows) + extraction_errors
    if total_attempted > 0 and extraction_errors / total_attempted > ERROR_RATE_WARN:
        rate = extraction_errors / total_attempted
        findings.append(Finding(
            "C5", "warning",
            f"extraction error rate {rate:.1%} ({extraction_errors}/{total_attempted})",
        ))

    # Outlier totals — log Z-score (requires ≥4 positive values)
    pos = [math.log(t) for t in totals if t > 0]
    if len(pos) >= 4:
        mean_log = statistics.mean(pos)
        stdev_log = statistics.stdev(pos)
        if stdev_log > 0:
            for r in rows:
                t = r["total_amount"]
                if t is None or t <= 0:
                    continue
                z = abs(math.log(t) - mean_log) / stdev_log
                if z > OUTLIER_ZSCORE_THRESHOLD:
                    findings.append(Finding(
                        "C5", "info",
                        f"total_amount={t:.2f} is a statistical outlier "
                        f"(log Z={z:.1f}, threshold={OUTLIER_ZSCORE_THRESHOLD}) — verify manually",
                        r["id"],
                    ))


# ── Public entry point ────────────────────────────────────────────────────────

def run_checks(cfg: SupplierConfig) -> AuditReport:
    """Run all deterministic checks against a supplier's SQLite database.

    Returns an AuditReport.  Does not call any LLM or Docker process.
    """
    db_path = APP_DIR / cfg.db_path
    report = AuditReport(
        supplier=cfg.supplier,
        run_at=datetime.now(),
        total_invoices=0,
        extraction_errors=0,
    )

    if not db_path.exists():
        report.findings.append(Finding("C0", "error", f"Database not found: {db_path}"))
        return report

    con = sqlite3.connect(str(db_path))
    con.row_factory = sqlite3.Row

    _NUMERIC = {"total_amount", "subtotal", "vat_amount", "vat_rate_percent",
                "discount_amount", "discount_2", "freight", "handling_charges"}

    def _to_float(v: object) -> float | None:
        if v is None:
            return None
        try:
            return float(v)
        except (TypeError, ValueError):
            return None

    raw_rows = con.execute(
        """
        SELECT id, invoice_number, invoice_date, total_amount, currency, subtotal,
               vat_amount, vat_rate_percent, discount_amount, discount_2,
               freight, handling_charges, corrections_applied, document_type
        FROM invoices
        WHERE extraction_error IS NULL
        """
    ).fetchall()
    rows = []
    for r in raw_rows:
        d = dict(r)
        for col in _NUMERIC:
            d[col] = _to_float(d[col])
        rows.append(d)

    extraction_errors = con.execute(
        "SELECT COUNT(*) FROM invoices "
        "WHERE extraction_error IS NOT NULL AND extraction_error NOT LIKE 'DUPLICATE%'"
    ).fetchone()[0]

    report.total_invoices = len(rows)
    report.extraction_errors = extraction_errors

    zero_value_allowed = bool(cfg.dashboard.get("zero_value_allowed", False))
    suppress_c4 = bool(cfg.dashboard.get("suppress_c4", False))
    c4_threshold = float(cfg.dashboard.get("c4_item_tolerance", ITEM_ARITH_THRESHOLD))
    _check_schema(rows, report.findings, zero_value_allowed=zero_value_allowed)
    _check_dates(rows, report.findings)
    _check_totals(rows, report.findings, con)
    _check_item_arithmetic(con, report.findings, suppress=suppress_c4, threshold=c4_threshold)
    _check_anomalies(rows, report.findings, extraction_errors)

    con.close()
    return report
