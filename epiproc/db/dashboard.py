"""Dashboard data-shaping + builder queries.

Ported from v1 dashboard_app/api/db.py. Single-DB changes:
  * No `SET search_path TO {tenant}` — every query runs against the default
    (public) schema of this container's own Postgres.
  * Supplier discovery is DB-first: `SELECT DISTINCT supplier FROM invoices`,
    falling back to config files when the table is empty.
  * Department normalisation imports from epiproc.normalisation.
  * CUFS lookup (v1 read an .xls next to a SQLite db) is stubbed to {} — v3 has
    no SQLite side-car.
  * File-backed helpers (claude_costs.jsonl, state/*.json) are stubbed empty.
"""
from __future__ import annotations

from collections import defaultdict
from datetime import date as _date
from typing import Any
from urllib.parse import quote

from epiproc.db.pool import pool
from epiproc.normalisation import norm_dept
from epiproc.suppliers import list_suppliers, load_config


def get_data_quality() -> dict:
    """Instance-wide ingest health for the Overview banner: PDFs that failed to
    process, invoices whose line items don't reconcile, line items that failed
    classification, and the 'Other' share (a signal the discovered taxonomy has
    gone stale and should be re-derived)."""
    from epiproc.db import ingested
    from epiproc.db.settings import OTHER_CATEGORY
    with pool().connection() as conn:
        try:
            fails = ingested.failure_count(conn)
            fail_files = [r["path"].rsplit("/", 1)[-1] for r in conn.execute(
                "SELECT path FROM ingested_files WHERE result='error' "
                "ORDER BY processed_at DESC LIMIT 8").fetchall()]
        except Exception:  # noqa: BLE001 — ledger table may not exist on an old DB
            fails, fail_files = 0, []
        warns = conn.execute(
            "SELECT count(*) AS c FROM invoices "
            "WHERE validation_warning IS NOT NULL AND validation_warning <> ''"
        ).fetchone()["c"]
        uncategorised = conn.execute(
            "SELECT count(*) AS c FROM invoice_items WHERE category IS NULL"
        ).fetchone()["c"]
        row = conn.execute(
            "SELECT coalesce(sum(total_price), 0) AS tot, "
            "coalesce(sum(total_price) FILTER (WHERE category = %s), 0) AS other "
            "FROM invoice_items WHERE total_price IS NOT NULL AND category IS NOT NULL",
            (OTHER_CATEGORY,),
        ).fetchone()
        other_share = (row["other"] / row["tot"]) if row["tot"] else 0.0
    return {"ingest_failures": fails, "reconciliation_warnings": warns,
            "uncategorised_items": uncategorised, "other_share": other_share,
            "fail_files": fail_files}



_FALLBACK_COLOR = "#6c7cff"


def _readable_color(hex_color: str) -> str:
    try:
        h = hex_color.lstrip("#")
        r, g, b = int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16)
        luminance = 0.2126 * r + 0.7152 * g + 0.0722 * b
        return hex_color if luminance > 40 else _FALLBACK_COLOR
    except Exception:
        return _FALLBACK_COLOR


def get_suppliers() -> list[str]:
    """Suppliers with data in this container's DB; fall back to config files."""
    try:
        with pool().connection() as conn:
            rows = conn.execute(
                "SELECT DISTINCT supplier FROM invoices WHERE supplier IS NOT NULL ORDER BY supplier"
            ).fetchall()
        found = [r["supplier"] for r in rows if r["supplier"]]
        if found:
            return found
    except Exception:
        pass
    try:
        return list_suppliers()
    except Exception:
        return []


# File-backed helpers from v1 are stubbed — v3 has no state/*.json or cost logs.

def get_costs() -> dict:
    return {"total_usd": 0.0, "by_supplier": {}, "by_call_type": {}, "entries": []}


def get_audit_state(supplier: str) -> dict:
    return {
        "supplier": supplier,
        "audit_status": "pending",
        "last_extraction_time": None,
        "errors": [],
        "updated_at": None,
    }


# ─────────────────────────────────────────────────────────────────────────────
# Per-tab aggregate queries (kept from v1; used by JSON helpers/reports)
# ─────────────────────────────────────────────────────────────────────────────

def get_categories(supplier: str) -> list[dict]:
    with pool().connection() as conn:
        rows = conn.execute("""
            SELECT COALESCE(ii.category, 'Uncategorised') AS category,
                   SUM(ii.total_price) AS spend,
                   COUNT(*) AS item_count
            FROM invoice_items ii
            JOIN invoices i ON ii.invoice_id = i.id
            WHERE i.supplier = %s
              AND (i.extraction_error IS NULL OR i.extraction_error = '')
              AND ii.total_price IS NOT NULL
            GROUP BY category
            ORDER BY spend DESC
        """, (supplier,)).fetchall()
    return [dict(r) for r in rows]


def get_departments(supplier: str) -> list[dict]:
    with pool().connection() as conn:
        rows = [dict(r) for r in conn.execute("""
            SELECT id, buyer_name, buyer_department, buyer_address, notes,
                   your_reference, order_reference,
                   ship_to_name, ship_to_department, ship_to_address,
                   sold_to_name, sold_to_department, sold_to_address,
                   buyer_customer_number, total_amount
            FROM invoices
            WHERE supplier = %s
              AND (extraction_error IS NULL OR extraction_error = '')
              AND total_amount IS NOT NULL
        """, (supplier,)).fetchall()]
    _apply_dept_normalisation(supplier, rows)
    agg: dict[str, dict] = defaultdict(lambda: {"spend": 0.0, "invoice_count": 0})
    for r in rows:
        dept = r["department"] or "Unknown"
        agg[dept]["spend"] += r["total_amount"] or 0
        agg[dept]["invoice_count"] += 1
    return sorted(
        [{"department": k, "spend": v["spend"], "invoice_count": v["invoice_count"]}
         for k, v in agg.items()],
        key=lambda x: -(x["spend"] or 0),
    )


def get_monthly_spend(supplier: str) -> list[dict]:
    with pool().connection() as conn:
        rows = conn.execute("""
            SELECT to_char(i.invoice_date, 'YYYY-MM') AS month,
                   COALESCE(ii.category, 'Uncategorised') AS category,
                   SUM(ii.total_price) AS spend
            FROM invoice_items ii
            JOIN invoices i ON ii.invoice_id = i.id
            WHERE i.supplier = %s
              AND i.invoice_date IS NOT NULL
              AND (i.extraction_error IS NULL OR i.extraction_error = '')
              AND ii.total_price IS NOT NULL
            GROUP BY month, category
            ORDER BY month, category
        """, (supplier,)).fetchall()
    return [dict(r) for r in rows]


def get_spend_matrix(supplier: str) -> list[dict]:
    with pool().connection() as conn:
        invoices = [dict(r) for r in conn.execute("""
            SELECT i.id, i.buyer_name, i.buyer_department, i.buyer_address, i.notes,
                   i.your_reference, i.order_reference,
                   i.ship_to_name, i.ship_to_department, i.ship_to_address,
                   i.sold_to_name, i.sold_to_department, i.sold_to_address,
                   i.buyer_customer_number
            FROM invoices i
            WHERE i.supplier = %s
              AND (i.extraction_error IS NULL OR i.extraction_error = '')
        """, (supplier,)).fetchall()]
        items = [dict(r) for r in conn.execute("""
            SELECT ii.invoice_id, ii.category, ii.total_price
            FROM invoice_items ii
            JOIN invoices i ON ii.invoice_id = i.id
            WHERE i.supplier = %s
              AND (i.extraction_error IS NULL OR i.extraction_error = '')
              AND ii.total_price IS NOT NULL
        """, (supplier,)).fetchall()]
    _apply_dept_normalisation(supplier, invoices, items)
    agg: dict[tuple, float] = defaultdict(float)
    for it in items:
        agg[(it.get("department") or "Unknown",
             it.get("category") or "Uncategorised")] += it.get("total_price") or 0
    return sorted(
        [{"department": k[0], "category": k[1], "spend": v} for k, v in agg.items()],
        key=lambda x: -(x["spend"] or 0),
    )


def get_service_intel(supplier: str) -> list[dict]:
    with pool().connection() as conn:
        rows = conn.execute("""
            SELECT ii.id, ii.invoice_id, i.invoice_number,
                   i.invoice_date::text AS invoice_date,
                   ii.article, ii.description, ii.quantity, ii.unit_price,
                   ii.total_price, i.subscription_start, i.subscription_end,
                   i.service_tier, i.seller_name
            FROM invoice_items ii
            JOIN invoices i ON ii.invoice_id = i.id
            WHERE i.supplier = %s
              AND ii.category = 'Service Contracts'
              AND (i.extraction_error IS NULL OR i.extraction_error = '')
              AND NOT (ii.total_price IS NULL AND ii.unit_price IS NULL
                       AND ii.article IS NULL)
            ORDER BY i.invoice_date DESC
        """, (supplier,)).fetchall()
    return [dict(r) for r in rows]


# ─────────────────────────────────────────────────────────────────────────────
# Dashboard data builder — supplies all JS variables for the HTML template
# ─────────────────────────────────────────────────────────────────────────────

_INVOICES_SQL = """
    SELECT
        i.id, i.filename, i.document_type, i.invoice_number,
        i.invoice_date::text AS invoice_date,
        i.currency, i.seller_name, i.buyer_name, i.buyer_department, i.buyer_address,
        i.notes, i.subtotal, i.discount_amount, i.discount_rate_percent,
        i.vat_amount, i.total_amount, i.payment_terms,
        i.subscription_start, i.subscription_end, i.service_tier,
        i.validation_warning, i.extraction_error,
        i.your_reference, i.order_reference,
        i.ship_to_name, i.ship_to_department, i.ship_to_address,
        i.sold_to_name, i.sold_to_department, i.sold_to_address,
        i.buyer_customer_number
    FROM invoices i
    WHERE i.supplier = %s
      AND (i.extraction_error IS NULL OR i.extraction_error = '')
      AND (i.invoice_number NOT LIKE 'DUPLICATE%%' OR i.invoice_number IS NULL)
    ORDER BY i.invoice_date DESC
"""

_ITEMS_SQL_BASE = """
    SELECT
        ii.id, ii.invoice_id, ii.description, ii.article,
        ii.quantity, ii.unit, ii.unit_price, ii.total_price, ii.category, ii.variety,
        i.invoice_number, i.invoice_date::text AS invoice_date, i.currency, i.buyer_name,
        i.buyer_department, i.buyer_address, i.notes, i.document_type,
        i.filename, i.total_amount, i.seller_name, i.subscription_start,
        i.subscription_end, i.validation_warning, i.extraction_error,
        i.your_reference, i.order_reference, i.service_tier,
        i.ship_to_name, i.ship_to_department, i.ship_to_address,
        i.sold_to_name, i.sold_to_department, i.sold_to_address,
        i.discount_rate_percent, i.buyer_customer_number
    FROM invoice_items ii
    JOIN invoices i ON ii.invoice_id = i.id
    WHERE i.supplier = %s
      AND (i.extraction_error IS NULL OR i.extraction_error = '')
"""

_ITEMS_SQL = _ITEMS_SQL_BASE + "    ORDER BY i.invoice_date DESC\n"


def _items_sql(neg_types: list[str]) -> tuple[str, list]:
    if not neg_types:
        return _ITEMS_SQL, []
    placeholders = ",".join(["%s"] * len(neg_types))
    sql = (
        _ITEMS_SQL_BASE
        + f"      AND LOWER(COALESCE(i.document_type,'')) NOT IN ({placeholders})\n"
        + "    ORDER BY i.invoice_date DESC\n"
    )
    return sql, neg_types


# ─────────────────────────────────────────────────────────────────────────────
# Department normalisation
# ─────────────────────────────────────────────────────────────────────────────

_CUFS_CACHE: dict[str, dict[str, str]] = {}


def _supplier_cufs(supplier: str) -> dict[str, str]:
    # v1 parsed a CUFS .xls that lived next to a per-supplier SQLite db. v3 has
    # no SQLite side-car, so there is no CUFS table to load.
    return {}


def _supplier_payer_keywords(supplier: str) -> list[str]:
    try:
        return load_config(supplier).dashboard.get("payer_fallback_keywords") or []
    except Exception:
        return []


def _norm_row_dept(row: dict, payer_keywords: list[str], cufs: dict[str, str]) -> str:
    return norm_dept(
        row.get("buyer_name"),
        row.get("buyer_department"),
        row.get("buyer_address"),
        row.get("notes"),
        row.get("your_reference"),
        cufs,
        row.get("order_reference"),
        ship_to_name=row.get("ship_to_name"),
        ship_to_department=row.get("ship_to_department"),
        ship_to_address=row.get("ship_to_address"),
        payer_fallback_keywords=payer_keywords,
        sold_to_name=row.get("sold_to_name"),
        sold_to_department=row.get("sold_to_department"),
        sold_to_address=row.get("sold_to_address"),
    )


def _apply_dept_normalisation(
    supplier: str, invoices: list[dict], items: list[dict] | None = None
) -> None:
    payer_keywords = _supplier_payer_keywords(supplier)
    cufs = _supplier_cufs(supplier)

    invoice_dept_by_id: dict[Any, str] = {}
    for inv in invoices:
        dept = _norm_row_dept(inv, payer_keywords, cufs)
        inv["department"] = dept
        if inv.get("id") is not None:
            invoice_dept_by_id[inv["id"]] = dept

    customer_dept_map: dict[Any, str] = {}
    for inv in invoices:
        cnum = inv.get("buyer_customer_number")
        if cnum and inv["department"] != "Other":
            customer_dept_map.setdefault(cnum, inv["department"])
    if customer_dept_map:
        for inv in invoices:
            if inv["department"] == "Other":
                cnum = inv.get("buyer_customer_number")
                if cnum and cnum in customer_dept_map:
                    inv["department"] = customer_dept_map[cnum]
                    if inv.get("id") is not None:
                        invoice_dept_by_id[inv["id"]] = inv["department"]

    if items is None:
        return

    for it in items:
        inv_id = it.get("invoice_id")
        dept = invoice_dept_by_id.get(inv_id) if inv_id is not None else None
        if dept is None:
            dept = _norm_row_dept(it, payer_keywords, cufs)
            if dept == "Other":
                cnum = it.get("buyer_customer_number")
                if cnum and cnum in customer_dept_map:
                    dept = customer_dept_map[cnum]
        it["department"] = dept


def _r(v: float | None) -> float | None:
    return round(v, 4) if v is not None else None


def _dash_sup_totals(
    invoices: list[dict], items: list[dict], display_name: str, color: str
) -> list[dict]:
    total = sum(i["total_amount"] or 0 for i in invoices if i.get("total_amount") is not None)
    return [{"display": display_name, "color": color,
             "count": len(invoices), "total": _r(total), "items": len(items)}]


def _dash_cat_totals(items: list[dict]) -> list[dict]:
    agg: dict[str, dict[str, Any]] = defaultdict(lambda: {"count": 0, "total": 0.0})
    for it in items:
        cat = it.get("category") or "Uncategorised"
        if it.get("total_price") is not None:
            agg[cat]["count"] += 1
            agg[cat]["total"] += it["total_price"]
    return sorted(
        [{"cat": k, "count": v["count"], "total": _r(v["total"])} for k, v in agg.items()],
        key=lambda x: -(x["total"] or 0),
    )


def _dash_dept_totals(invoices: list[dict]) -> list[dict]:
    agg: dict[str, dict[str, Any]] = defaultdict(lambda: {"count": 0, "total": 0.0})
    for inv in invoices:
        dept = inv.get("department") or "Unknown"
        if inv.get("total_amount") is not None:
            agg[dept]["count"] += 1
            agg[dept]["total"] += inv["total_amount"]
    return sorted(
        [{"dept": k, "count": v["count"], "total": _r(v["total"])} for k, v in agg.items()],
        key=lambda x: -(x["total"] or 0),
    )


def _dash_cat_dept(items: list[dict]) -> list[dict]:
    agg: dict[tuple, float] = defaultdict(float)
    for it in items:
        if it.get("total_price") is not None:
            agg[(it.get("category") or "Uncategorised", it.get("department") or "Unknown")] += it["total_price"]
    return [{"cat": k[0], "dept": k[1], "total": _r(v)} for k, v in agg.items()]


def _dash_monthly_cat(items: list[dict]) -> list[dict]:
    agg: dict[tuple, float] = defaultdict(float)
    for it in items:
        date = it.get("invoice_date") or ""
        month = date[:7] if len(date) >= 7 else None
        if month and it.get("total_price") is not None:
            agg[(month, it.get("category") or "Uncategorised")] += it["total_price"]
    return sorted(
        [{"month": k[0], "cat": k[1], "total": _r(v)} for k, v in agg.items()],
        key=lambda x: (x["month"], x["cat"]),
    )


def _dash_monthly_sup(invoices: list[dict], display_name: str) -> list[dict]:
    agg: dict[str, float] = defaultdict(float)
    for inv in invoices:
        date = inv.get("invoice_date") or ""
        month = date[:7] if len(date) >= 7 else None
        if month and inv.get("total_amount") is not None:
            agg[month] += inv["total_amount"]
    return sorted(
        [{"month": k, "sup": display_name, "total": _r(v)} for k, v in agg.items()],
        key=lambda x: x["month"],
    )


def _dash_cat_supplier(items: list[dict], display_name: str) -> list[dict]:
    agg: dict[str, float] = defaultdict(float)
    for it in items:
        if it.get("total_price") is not None:
            agg[it.get("category") or "Uncategorised"] += it["total_price"]
    return [{"cat": k, "sup": display_name, "total": _r(v)} for k, v in agg.items()]


def _dash_svc(items: list[dict]) -> dict:
    today = _date.today().isoformat()
    svc_items_raw = [it for it in items if (it.get("category") or "") == "Service Contracts"]

    items_list = []
    for it in svc_items_raw:
        disc_pct = None
        drp = it.get("discount_rate_percent")
        if drp and drp > 0:
            disc_pct = round(float(drp), 2)
        # Tier comes from the invoice's own service_tier field (per-tenant data);
        # unset -> "Unknown" in the aggregation below. No hardcoded supplier ladder.
        tier = (it.get("service_tier") or "").strip()
        items_list.append({
            "article": it.get("article") or "",
            "desc": it.get("description") or "",
            "tier": tier,
            "unit_price": it.get("unit_price"),
            "qty": it.get("quantity"),
            "unit": it.get("unit") or "",
            "total": it.get("total_price"),
            "invoice": it.get("invoice_number") or "",
            "date": it.get("invoice_date") or "",
            "dept": it.get("department") or "Unknown",
            "disc_pct": disc_pct,
            "sub_start": it.get("subscription_start"),
            "sub_end": it.get("subscription_end"),
            "doc_type": (it.get("document_type") or "").lower(),
        })

    tier_agg: dict[str, dict] = defaultdict(lambda: {"count": 0, "total": 0.0})
    for it in items_list:
        tier = it["tier"] or "Unknown"
        tier_agg[tier]["count"] += 1
        if it["total"] is not None:
            tier_agg[tier]["total"] += it["total"]
    tier_totals = sorted(
        [{"tier": k, "count": v["count"], "total": _r(v["total"])} for k, v in tier_agg.items()],
        key=lambda x: -(x["total"] or 0),
    )

    prod_agg: dict[str, dict] = {}
    for it in items_list:
        key = it["article"] or f"__desc_{it['desc'][:30]}"
        if key not in prod_agg:
            prod_agg[key] = {
                "article": it["article"],
                "desc": it["desc"],
                "tier": it["tier"],
                "prices": [],
                "net_prices": [],
                "dept_nets": {},
                "depts": [],
                "total": 0.0,
            }
        p = prod_agg[key]
        if it["unit_price"] is not None:
            p["prices"].append(it["unit_price"])
        qty = it["qty"] or 0
        net_p = (it["total"] / qty) if (it["total"] is not None and qty != 0) else it["unit_price"]
        if net_p is not None:
            p["net_prices"].append(net_p)
        dept = it["dept"]
        if dept not in p["depts"]:
            p["depts"].append(dept)
        if dept not in p["dept_nets"]:
            p["dept_nets"][dept] = {
                "gross": it["unit_price"],
                "net": net_p,
                "disc": it["disc_pct"] or 0,
            }
        if it["total"] is not None:
            p["total"] += it["total"]

    products = []
    for p in prod_agg.values():
        p["has_variance"] = len(set(p["prices"])) > 1
        p["has_net_variance"] = len(set(p["net_prices"])) > 1
        p["total"] = _r(p["total"])
        products.append(p)
    products.sort(key=lambda x: -(x["total"] or 0))

    dept_agg: dict[str, dict] = {}
    for it in items_list:
        dept = it["dept"]
        if dept not in dept_agg:
            dept_agg[dept] = {"total": 0.0, "tiers": set(), "disc_rates": []}
        if it["total"] is not None:
            dept_agg[dept]["total"] += it["total"]
        if it["tier"]:
            dept_agg[dept]["tiers"].add(it["tier"])
        if it["disc_pct"] is not None and it["disc_pct"] > 0:
            dept_agg[dept]["disc_rates"].append(it["disc_pct"])
    dept_disc = sorted(
        [{"dept": d, "total": _r(v["total"]), "tiers": sorted(v["tiers"]),
          "disc_pct": round(sum(v["disc_rates"]) / len(v["disc_rates"]), 1) if v["disc_rates"] else 0}
         for d, v in dept_agg.items()],
        key=lambda x: -(x["total"] or 0),
    )

    td_agg: dict[tuple, float] = defaultdict(float)
    for it in items_list:
        if it["total"] is not None:
            td_agg[(it["tier"] or "Unknown", it["dept"])] += it["total"]
    tier_dept = [{"tier": k[0], "dept": k[1], "total": _r(v)} for k, v in td_agg.items()]

    has_discounts = any(it["disc_pct"] for it in items_list if it["disc_pct"])

    # ── Timeline ─────────────────────────────────────────────────────────────
    import re as _re
    from datetime import date as _dt

    _NEGATIVE_DOC_TYPES = {"credit note", "credit memo", "cancellation invoice"}
    _SERIAL_RE = _re.compile(
        r'(?:Serial\s+(?:no|number)\.?\s*[:#]?\s*|SN\s*#?\s+)(\S+)',
        _re.IGNORECASE,
    )

    inv_serials: dict[str, set] = {}
    for it in items_list:
        for m in _SERIAL_RE.finditer(it.get("desc") or ""):
            sn = m.group(1).strip().rstrip(".,;")
            if len(sn) >= 3:
                inv_serials.setdefault(it["invoice"], set()).add(sn)

    def _infer_months(item: dict) -> int:
        unit = (item.get("unit") or "").upper().strip()
        qty = item.get("qty") or 0
        if "MON" in unit and qty and qty > 0:
            return int(qty)
        desc = (item.get("desc") or "").lower()
        for pat, months in [
            (r'(\d+)\s*year', None), (r'(\d+)\s*yr', None),
            (r'(\d+)\s*mo(?:nth|\.|\b)', None),
        ]:
            m = _re.search(pat, desc)
            if m:
                n = int(m.group(1))
                return n * 12 if 'year' in pat or 'yr' in pat else n
        return 12

    tl_map: dict = {}
    for it in items_list:
        if it.get("doc_type") in _NEGATIVE_DOC_TYPES:
            continue
        start = it.get("sub_start")
        end = it.get("sub_end")
        inferred = False
        if not start or not end:
            inv_date = it.get("date")
            if not inv_date:
                continue
            try:
                inv_dt = _dt.fromisoformat(inv_date)
            except ValueError:
                continue
            months = _infer_months(it)
            start = start or inv_date
            if not end:
                _total = inv_dt.month - 1 + months
                _end_year = inv_dt.year + _total // 12
                _end_month = _total % 12 + 1
                try:
                    end_dt = inv_dt.replace(year=_end_year, month=_end_month)
                except ValueError:
                    end_dt = inv_dt.replace(year=_end_year, month=_end_month, day=28)
                end = end_dt.isoformat()
            inferred = True
        try:
            dur_days = (_dt.fromisoformat(end) - _dt.fromisoformat(start)).days
        except ValueError:
            dur_days = 999
        if dur_days < 28:
            continue
        inv_key = (it["invoice"], start, end)
        if inv_key not in tl_map:
            serials = inv_serials.get(it["invoice"], set())
            tl_map[inv_key] = {
                "db_id": None,
                "invoice": it["invoice"],
                "invoice_date": it["date"],
                "dept": it["dept"],
                "instrument": "—",
                "serial": sorted(serials)[0] if serials else "—",
                "tier": it["tier"],
                "start": start,
                "end": end,
                "total": it["total"],
                "supplier": it.get("supplier") or "",
                "chain_id": None,
                "chain_pos": None,
                "chain_len": None,
                "gap_days": None,
                "status": "",
                "inferred": inferred,
            }
        else:
            existing = tl_map[inv_key]
            if it["total"] is not None:
                existing["total"] = (existing["total"] or 0) + it["total"]

    timeline = sorted(tl_map.values(), key=lambda x: x["start"] or "")

    serial_groups: dict[str, list] = {}
    for entry in timeline:
        sn = entry["serial"]
        if sn != "—":
            serial_groups.setdefault(sn, []).append(entry)

    chain_id_ctr = 0
    for sn, grp in serial_groups.items():
        grp.sort(key=lambda x: x["start"] or "")
        chain_id_ctr += 1
        for i, e in enumerate(grp):
            e["chain_id"] = chain_id_ctr
            e["chain_pos"] = i + 1
            e["chain_len"] = len(grp)
            if i > 0:
                prev_end = grp[i - 1]["end"]
                try:
                    gap = (_dt.fromisoformat(e["start"]) - _dt.fromisoformat(prev_end)).days
                    e["gap_days"] = gap
                except (ValueError, TypeError):
                    pass

    serials_covered = {
        sn for sn, grp in serial_groups.items()
        if any((e["end"] or "") >= today for e in grp)
    }

    for e in timeline:
        start, end = e["start"] or "", e["end"] or ""
        sn = e["serial"]
        if start > today:
            e["status"] = "future"
        elif end >= today:
            e["status"] = "active"
        elif sn != "—" and sn in serials_covered:
            e["status"] = "expired_renewed"
        else:
            e["status"] = "expired_lapsed"

    tl_dates = [t["start"] for t in timeline if t["start"]]
    tl_date_min = min(tl_dates) if tl_dates else None
    tl_dates_end = [t["end"] for t in timeline if t["end"]]
    tl_date_max = max(tl_dates_end) if tl_dates_end else None

    return {
        "items": items_list,
        "tier_totals": tier_totals,
        "products": products,
        "dept_disc": dept_disc,
        "tier_dept": tier_dept,
        "has_discounts": has_discounts,
        "timeline": timeline,
        "tl_date_min": tl_date_min,
        "tl_date_max": tl_date_max,
    }


# Fields the dashboard's client JS actually reads from each inlined ITEMS /
# INVOICES element (per the field-usage audit). The row queries deliberately
# fetch more than this — the extra columns drive server-side derivation
# (department normalisation, aggregates, service intel) — but only these are
# inlined into the page, so the payload doesn't carry ~20 unused columns on every
# row. The item array is the one that grows with the data, so this is where the
# "whole DB in the page" growth is contained.
_ITEM_KEEP = frozenset({
    "invoice_id", "description", "article", "quantity", "unit_price", "total_price",
    "category", "variety", "invoice_number", "invoice_date", "filename",
    "department", "supplier", "supplier_name", "supplier_color", "comment",
})
_INVOICE_KEEP = frozenset({
    "id", "filename", "document_type", "invoice_number", "invoice_date", "currency",
    "buyer_department", "subtotal", "discount_amount", "total_amount",
    "validation_warning", "extraction_error", "your_reference",
    "department", "supplier", "supplier_name", "supplier_color", "pdf_url", "thumb",
})


def _slim(d: dict, keep: frozenset) -> dict:
    return {k: v for k, v in d.items() if k in keep}


def get_dashboard_data(supplier: str) -> dict:
    cfg = load_config(supplier)
    display_name = cfg.dashboard.get("display_name", supplier.title())
    color = _readable_color(cfg.dashboard.get("color", "#6c7cff"))

    _empty_svc = {
        "items": [], "tier_totals": [], "products": [], "dept_disc": [],
        "tier_dept": [], "has_discounts": False, "timeline": [],
        "tl_date_min": None, "tl_date_max": None,
    }

    _neg_types = [t.lower() for t in (cfg.dashboard.get("negative_document_types") or [])]
    _isql, _iparams = _items_sql(_neg_types)

    with pool().connection() as conn:
        inv_rows = conn.execute(_INVOICES_SQL, (supplier,)).fetchall()
        item_rows = conn.execute(_isql, (supplier, *_iparams)).fetchall()

    if not inv_rows:
        return {
            "supplier": supplier, "display_name": display_name, "color": color,
            "n_inv": 0, "n_items": 0, "invoices": [], "items": [],
            "sup_totals": [], "cat_totals": [], "dept_totals": [], "cat_dept": [],
            "monthly_cat": [], "monthly_sup": [], "cat_supplier": [],
            "sup_names": [display_name], "sup_colors": [color], "svc": _empty_svc,
            "grand_total": 0,
        }

    invoices = []
    for r in inv_rows:
        d = dict(r)
        d["supplier"] = supplier
        d["supplier_name"] = display_name
        d["supplier_color"] = color
        d["thumb"] = None
        filename = d.get("filename") or d.get("pdf_file") or ""
        d["pdf_url"] = f"/pdf/{supplier}/{quote(filename)}" if filename else ""
        invoices.append(d)

    items = []
    items_with_drp = []
    for r in item_rows:
        base = dict(r)
        base["comment"] = ""
        base["supplier"] = supplier
        base["supplier_name"] = display_name
        base["supplier_color"] = color
        items_with_drp.append(base)
        slim = dict(base)
        slim.pop("discount_rate_percent", None)
        items.append(slim)

    _apply_dept_normalisation(supplier, invoices, items)
    inv_dept_by_id = {inv["id"]: inv.get("department")
                      for inv in invoices if inv.get("id") is not None}
    for it in items_with_drp:
        dept = inv_dept_by_id.get(it.get("invoice_id"))
        if dept is not None:
            it["department"] = dept

    return {
        "supplier": supplier,
        "display_name": display_name,
        "color": color,
        "n_inv": len(invoices),
        "n_items": len(items),
        # Inline only the client-read fields; aggregates below still use the full
        # rows. This is what keeps the page payload from growing with every column.
        "invoices": [_slim(inv, _INVOICE_KEEP) for inv in invoices],
        "items": [_slim(it, _ITEM_KEEP) for it in items],
        "sup_totals": _dash_sup_totals(invoices, items, display_name, color),
        "cat_totals": _dash_cat_totals(items),
        "dept_totals": _dash_dept_totals(invoices),
        "cat_dept": _dash_cat_dept(items),
        "monthly_cat": _dash_monthly_cat(items),
        "monthly_sup": _dash_monthly_sup(invoices, display_name),
        "cat_supplier": _dash_cat_supplier(items, display_name),
        "sup_names": [display_name],
        "sup_colors": [color],
        "svc": _dash_svc(items_with_drp),
        "grand_total": sum(i["total_amount"] or 0 for i in invoices if i.get("total_amount") is not None),
    }


_SUP_PALETTE = ["#6c7cff", "#56cf8e", "#ff6b6b", "#4ecdc4", "#c77dff",
                "#ff9f43", "#f4a261", "#38bdf8", "#fb7185", "#34d399",
                "#facc15", "#a78bfa", "#2dd4bf", "#f472b6"]


def _supplier_colours(suppliers: list[str]) -> dict[str, str]:
    """Config colour if set, else a distinct palette colour per supplier so the
    By-Supplier views don't render every supplier in the same default blue."""
    out: dict[str, str] = {}
    i = 0
    for s in suppliers:
        c = load_config(s).dashboard.get("color")
        if not c:
            c = _SUP_PALETTE[i % len(_SUP_PALETTE)]
            i += 1
        out[s] = c
    return out


def get_multi_dashboard_data(suppliers: list[str]) -> dict:
    all_invoices: list[dict] = []
    all_items: list[dict] = []
    all_items_with_drp: list[dict] = []
    meta: list[dict] = []

    svc_by_sup: dict = {}
    colours = _supplier_colours(suppliers)
    for supplier in suppliers:
        d = get_dashboard_data(supplier)
        col = colours[supplier]
        d["color"] = col
        for _it in d["items"]:
            _it["supplier_color"] = col
        for _inv in d["invoices"]:
            _inv["supplier_color"] = col
        all_invoices.extend(d["invoices"])
        all_items.extend(d["items"])
        meta.append({"display_name": d["display_name"], "color": col})
        svc_by_sup[d["display_name"]] = d["svc"]

        cfg = load_config(supplier)
        inv_dept_by_id = {inv["id"]: inv.get("department")
                          for inv in d["invoices"] if inv.get("id") is not None}
        _neg = [t.lower() for t in (cfg.dashboard.get("negative_document_types") or [])]
        _msql, _mparams = _items_sql(_neg)

        with pool().connection() as conn:
            for r in conn.execute(_msql, (supplier, *_mparams)).fetchall():
                row = dict(r)
                row["comment"] = ""
                row["supplier"] = supplier
                row["supplier_name"] = d["display_name"]
                row["supplier_color"] = col
                row["department"] = inv_dept_by_id.get(row.get("invoice_id")) or "Unknown"
                all_items_with_drp.append(row)

    sup_totals = []
    for m in meta:
        name, color = m["display_name"], m["color"]
        s_invs  = [i for i in all_invoices if i.get("supplier_name") == name]
        s_items = [i for i in all_items    if i.get("supplier_name") == name]
        total = sum(i["total_amount"] or 0 for i in s_invs if i.get("total_amount") is not None)
        sup_totals.append({"display": name, "color": color,
                           "count": len(s_invs), "total": _r(total), "items": len(s_items)})

    ms_agg: dict[tuple, float] = defaultdict(float)
    for inv in all_invoices:
        month = (inv.get("invoice_date") or "")[:7] or None
        if month and inv.get("total_amount") is not None:
            ms_agg[(month, inv.get("supplier_name", "Unknown"))] += inv["total_amount"]
    monthly_sup = sorted(
        [{"month": k[0], "sup": k[1], "total": _r(v)} for k, v in ms_agg.items()],
        key=lambda x: (x["month"], x["sup"]),
    )

    cs_agg: dict[tuple, float] = defaultdict(float)
    for it in all_items:
        cat = it.get("category") or "Uncategorised"
        if it.get("total_price") is not None:
            cs_agg[(cat, it.get("supplier_name", "Unknown"))] += it["total_price"]
    cat_supplier = [{"cat": k[0], "sup": k[1], "total": _r(v)} for k, v in cs_agg.items()]

    grand_total = sum(s["total"] or 0 for s in sup_totals)

    return {
        "display_name": "All Suppliers",
        "n_inv":    len(all_invoices),
        "n_items":  len(all_items),
        "invoices": all_invoices,
        "items":    all_items,
        "sup_totals":   sup_totals,
        "cat_totals":   _dash_cat_totals(all_items),
        "dept_totals":  _dash_dept_totals(all_invoices),
        "cat_dept":     _dash_cat_dept(all_items),
        "monthly_cat":  _dash_monthly_cat(all_items),
        "monthly_sup":  monthly_sup,
        "cat_supplier": cat_supplier,
        "sup_names":  [m["display_name"] for m in meta],
        "sup_colors": [m["color"]        for m in meta],
        "sup_keys":   suppliers,
        "svc":        _dash_svc(all_items_with_drp),
        "svc_by_sup": svc_by_sup,
        "grand_total": grand_total,
        "n_suppliers": len(suppliers),
    }
