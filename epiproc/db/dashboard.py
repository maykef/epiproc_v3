"""Dashboard data-shaping + builder queries.

Ported from v1 dashboard_app/api/db.py. Single-DB changes:
  * No `SET search_path TO {tenant}` — every query runs against the default
    (public) schema of this container's own Postgres.
  * Supplier discovery is DB-first: `SELECT DISTINCT supplier FROM invoices`,
    falling back to config files when the table is empty.
  * Department normalisation imports from epiproc.normalisation (data-driven from
    the customer's own departments.yml — no organisation is hardcoded).
  * File-backed helpers (claude_costs.jsonl, state/*.json) are stubbed empty.
"""
from __future__ import annotations

from collections import defaultdict
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
        i.filename, i.total_amount, i.seller_name,
        i.validation_warning, i.extraction_error,
        i.your_reference, i.order_reference,
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

def _supplier_payer_keywords(supplier: str) -> list[str]:
    try:
        return load_config(supplier).dashboard.get("payer_fallback_keywords") or []
    except Exception:
        return []


def _norm_row_dept(row: dict, payer_keywords: list[str]) -> str:
    return norm_dept(
        row.get("buyer_name"),
        row.get("buyer_department"),
        row.get("buyer_address"),
        row.get("notes"),
        payer_fallback_keywords=payer_keywords,
        ship_to_name=row.get("ship_to_name"),
        ship_to_department=row.get("ship_to_department"),
        ship_to_address=row.get("ship_to_address"),
        sold_to_name=row.get("sold_to_name"),
        sold_to_department=row.get("sold_to_department"),
        sold_to_address=row.get("sold_to_address"),
    )


def _apply_dept_normalisation(
    supplier: str, invoices: list[dict], items: list[dict] | None = None
) -> None:
    payer_keywords = _supplier_payer_keywords(supplier)

    invoice_dept_by_id: dict[Any, str] = {}
    for inv in invoices:
        dept = _norm_row_dept(inv, payer_keywords)
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
            dept = _norm_row_dept(it, payer_keywords)
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


# Fields the dashboard's client JS actually reads from each inlined ITEMS /
# INVOICES element. The row queries deliberately fetch more than this — the extra
# columns drive server-side derivation (department normalisation, aggregates) —
# but only these are inlined into the page, so the payload doesn't carry ~20 unused
# columns on every row. The item array is the one that grows with the data.
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
            "sup_names": [display_name], "sup_colors": [color],
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
    for r in item_rows:
        base = dict(r)
        base["comment"] = ""
        base["supplier"] = supplier
        base["supplier_name"] = display_name
        base["supplier_color"] = color
        base.pop("discount_rate_percent", None)
        items.append(base)

    _apply_dept_normalisation(supplier, invoices, items)

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
    meta: list[dict] = []

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
        "grand_total": grand_total,
        "n_suppliers": len(suppliers),
    }
