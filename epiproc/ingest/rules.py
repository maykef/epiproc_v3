"""Declarative corrections engine — replaces v1's ~28 hardcoded correct_* branches.

An op is a small pure function: record -> (record, note|None). Ops are chosen by
name; a supplier's YAML lists which to run (under `rules:`), plus a universal
default set. No SQL surgery, no `if supplier == ...`, fully testable.

This is the seed set. Each v1 correct_* becomes one op here as extraction is
validated supplier by supplier.
"""
from __future__ import annotations

import re
from typing import Callable

_OPS: dict[str, Callable[[dict], tuple[dict, str | None]]] = {}
_HS_CODE = re.compile(r"^\s*\d{8}\s*$")


def op(name: str):
    def reg(fn):
        _OPS[name] = fn
        return fn
    return reg


def _is_credit(rec: dict) -> bool:
    return "credit" in (rec.get("document_type") or "").lower()


@op("drop_hs_summary")
def _drop_hs_summary(rec: dict) -> tuple[dict, str | None]:
    """Drop customs/commodity-summary lines (description is a bare 8-digit HS
    code, e.g. 84713000). Some suppliers repeat the
    whole invoice as an HS-code summary on a later page; counting both double-
    counts spend. A genuine product description is never just an 8-digit number.
    """
    items = rec.get("line_items") or []
    kept = [it for it in items
            if not (isinstance(it, dict) and _HS_CODE.match(str(it.get("description") or "")))]
    dropped = len(items) - len(kept)
    if dropped:
        rec["line_items"] = kept
        return rec, f"drop_hs_summary: removed {dropped} customs-code summary line(s)"
    return rec, None


@op("credit_note_sign")
def _credit_note_sign(rec: dict) -> tuple[dict, str | None]:
    """Credit notes carry negative value. Ensure totals + line prices are negative."""
    if not _is_credit(rec):
        return rec, None
    changed = False
    tot = rec.get("totals") or {}
    for k in ("subtotal", "total", "discount_amount", "vat_amount"):
        v = tot.get(k)
        if isinstance(v, (int, float)) and v > 0:
            tot[k] = -abs(v)
            changed = True
    for it in rec.get("line_items") or []:
        for k in ("unit_price", "total_price"):
            v = it.get(k)
            if isinstance(v, (int, float)) and v > 0:
                it[k] = -abs(v)
                changed = True
    return rec, ("credit_note_sign: flipped to negative" if changed else None)


@op("derive_total_from_subtotal")
def _derive_total(rec: dict) -> tuple[dict, str | None]:
    """Safety net (v1 S2): total null but subtotal present -> use subtotal."""
    tot = rec.get("totals") or {}
    if tot.get("total") is None and isinstance(tot.get("subtotal"), (int, float)):
        tot["total"] = tot["subtotal"]
        return rec, f"derive_total: total={tot['subtotal']} from subtotal"
    return rec, None


@op("reconcile_totals")
def _reconcile_totals(rec: dict) -> tuple[dict, str | None]:
    """Cross-check the two spend levels: sum(line_items) vs subtotal vs total.

    The dashboard aggregates categories from line items but suppliers/months from
    the invoice total. When line items don't sum to the subtotal (e.g. deposits or
    pallets are in the total but not itemised), those views disagree. We can't know
    which figure is 'right', so we don't silently adjust — we flag the invoice via
    validation_warning so the mismatch is visible instead of a silent Δ on screen.
    """
    items = rec.get("line_items") or []
    line_sum = sum(it["total_price"] for it in items
                   if isinstance(it, dict) and isinstance(it.get("total_price"), (int, float)))
    tot = rec.get("totals") or {}
    subtotal, total = tot.get("subtotal"), tot.get("total")
    warns: list[str] = []

    def _off(a: float, b: float) -> bool:
        # Exact accounting figures — a cents-level band (rounded to avoid float noise),
        # not a percentage. A 2-cent tolerance absorbs legitimate rounding only.
        return round(abs(a - b), 2) > 0.02

    def _n(k: str) -> float:
        v = tot.get(k)
        return v if isinstance(v, (int, float)) else 0.0

    # If the line items already reconcile to the invoice total, every dashboard
    # view agrees — categories are summed from line items, supplier/month spend
    # from the invoice total — so the invoice is consistent. A mis-read *subtotal*
    # (e.g. the VLM lifting a VAT-base figure instead of the true subtotal, when
    # deposits/charges are themselves itemised) is then immaterial and must not
    # raise a reconciliation warning. This is the common false positive.
    if items and isinstance(total, (int, float)) and not _off(line_sum, total):
        return rec, None

    # 1. Line items should reconcile to the subtotal (net of charges).
    if items and isinstance(subtotal, (int, float)) and _off(line_sum, subtotal):
        warns.append(f"line items sum {line_sum:.2f} ≠ subtotal {subtotal:.2f} "
                     f"(Δ{line_sum - subtotal:+.2f})")
    elif items and isinstance(total, (int, float)) and not isinstance(subtotal, (int, float)) \
            and _off(line_sum, total):
        warns.append(f"line items sum {line_sum:.2f} ≠ total {total:.2f} "
                     f"(Δ{line_sum - total:+.2f})")
    # 2. subtotal + charges − discounts should reconcile to the total. This is the gap
    #    where UNITEMISED charges (deposits, pallets) hide — invisible to any line-item
    #    check. Skipped for credit notes, whose signs are already flipped upstream.
    if not _is_credit(rec) and isinstance(subtotal, (int, float)) and isinstance(total, (int, float)):
        explained = (subtotal + _n("freight") + _n("handling_charges") + _n("vat_amount")
                     - _n("discount_amount") - _n("discount_2"))
        if _off(explained, total):
            warns.append(f"subtotal+charges−discounts {explained:.2f} ≠ total {total:.2f} "
                         f"(Δ{total - explained:+.2f}) — unitemised charge?")
    if not warns:
        return rec, None
    prior = rec.get("validation_warning")
    rec["validation_warning"] = "; ".join(([prior] if prior else []) + warns)
    return rec, "reconcile_totals: " + "; ".join(warns)


DEFAULT_RULES = ["drop_hs_summary", "credit_note_sign",
                 "derive_total_from_subtotal", "reconcile_totals"]


def apply_rules(record: dict, cfg) -> tuple[dict, list[str]]:  # noqa: ANN001
    """Run the default universal ops + any op names listed in the supplier YAML."""
    names = DEFAULT_RULES + [r for r in getattr(cfg, "rules", []) if isinstance(r, str)]
    notes: list[str] = []
    for name in names:
        fn = _OPS.get(name)
        if fn is None:
            continue
        record, note = fn(record)
        if note:
            notes.append(note)
    return record, notes
