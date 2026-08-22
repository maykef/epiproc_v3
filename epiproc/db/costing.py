"""Costing persistence: products, box types, cost-menu items, and saved costings.

Follows the house DB idiom (``pool().connection()``, ``dict_row`` rows, raw SQL,
percentages stored as fractions, money as NUMERIC). Reference tables
(``box_types``, ``cost_menu_items``) are editable master data; ``costings`` rows
are immutable versioned snapshots — see :func:`save_costing`.

The per-customer defaults (rates, percentages, target margins) live in the shared
``settings`` key/value table under ``costing_defaults`` and are read/written
through the existing ``epiproc.db.settings`` helpers, so they behave exactly like
the other per-instance settings.
"""
from __future__ import annotations

import json
from decimal import Decimal
from typing import Any

from epiproc.db.pool import pool
from epiproc.db.settings import get_setting, set_setting

# Fallback used when the settings row is absent (mirrors the 0009/0010 seeds).
# The migrations seed this row on a fresh DB; the fallback keeps callers working
# on a DB migrated before the row existed (and supplies the engine constants the
# offer import shares across products — see offer_import.build_inputs).
DEFAULT_COSTING_DEFAULTS: dict[str, float] = {
    "vat_rate": 0.20,
    "eur_rate": 1.0,
    "usd_rate": 1.14,
    "waste_pct": 0.01,
    "intake_labour_pct": 0.10,
    "additional_pct": 0.10,
    "customer_target_margin": 0.35,
    "our_target_margin": 0.10,
    # Engine constants (roses-sheet values), shared by every imported product.
    "pallet_rate": 125,
    "boxes_per_cc": 252,
    "qty_per_box": 1,
    "price_per_pallet": 50,
    "fill_rate": 0.8,
    "boxes_on_order": 24,
    "fuel_surcharge_pct": 0,
}

_SETTINGS_KEY = "costing_defaults"


def _clean(rows) -> list[dict]:  # noqa: ANN001
    return [dict(r) for r in rows]


def _floatify(value: Any) -> Any:
    """Recursively turn Decimals (psycopg returns NUMERIC as Decimal) into floats
    so a row is JSON-serialisable and template-friendly."""
    if isinstance(value, Decimal):
        return float(value)
    if isinstance(value, dict):
        return {k: _floatify(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_floatify(v) for v in value]
    return value


def _row(row) -> dict | None:  # noqa: ANN001
    return {k: _floatify(v) for k, v in dict(row).items()} if row else None


# ── Costing defaults (per-customer settings) ─────────────────────────────────


def get_costing_defaults() -> dict:
    v = get_setting(_SETTINGS_KEY)
    if isinstance(v, dict):
        # Merge over the fallback so a partially-populated row still has every key.
        return {**DEFAULT_COSTING_DEFAULTS, **v}
    return dict(DEFAULT_COSTING_DEFAULTS)


def set_costing_defaults(values: dict) -> None:
    merged = {**DEFAULT_COSTING_DEFAULTS, **{k: v for k, v in (values or {}).items()
                                             if k in DEFAULT_COSTING_DEFAULTS}}
    set_setting(_SETTINGS_KEY, merged)


# ── Products ─────────────────────────────────────────────────────────────────


def list_products(active_only: bool = False) -> list[dict]:
    q = "SELECT * FROM products"
    if active_only:
        q += " WHERE active = TRUE"
    q += " ORDER BY name"
    with pool().connection() as conn:
        rows = conn.execute(q).fetchall()
    return [_row(r) for r in rows]


def get_product(product_id: int) -> dict | None:
    with pool().connection() as conn:
        row = conn.execute("SELECT * FROM products WHERE id = %s", (product_id,)).fetchone()
    return _row(row)


def get_product_by_ean(ean: str, conn=None) -> dict | None:  # noqa: ANN001
    if conn is not None:
        row = conn.execute("SELECT * FROM products WHERE ean = %s", (ean,)).fetchone()
        return _row(row)
    with pool().connection() as c:
        row = c.execute("SELECT * FROM products WHERE ean = %s", (ean,)).fetchone()
        return _row(row)


def get_product_by_name(name: str, conn=None) -> dict | None:  # noqa: ANN001
    """First product with this exact name (products.name is not unique)."""
    if conn is not None:
        row = conn.execute(
            "SELECT * FROM products WHERE name = %s ORDER BY id LIMIT 1", (name,),
        ).fetchone()
        return _row(row)
    with pool().connection() as c:
        row = c.execute(
            "SELECT * FROM products WHERE name = %s ORDER BY id LIMIT 1", (name,),
        ).fetchone()
        return _row(row)


def upsert_product(
    name: str,
    ean: str | None = None,
    units_per_tray: int = 1,
    retail_price: float | None = None,
    selling_price: float | None = None,
    active: bool = True,
    product_id: int | None = None,
) -> int:
    """Insert a product, or update an existing one identified by ``product_id``
    (explicit) or by ``ean`` (natural key). Returns the product id."""
    fields = dict(
        name=name.strip(),
        ean=(ean or None) or None,
        units_per_tray=int(units_per_tray),
        retail_price=retail_price,
        selling_price=selling_price,
        active=active,
    )
    # Locate an existing row to update.
    target = product_id
    if target is None and fields["ean"]:
        existing = get_product_by_ean(fields["ean"])
        target = existing["id"] if existing else None

    with pool().connection() as conn:
        if target is not None:
            conn.execute(
                """UPDATE products
                   SET name=%s, ean=%s, units_per_tray=%s, retail_price=%s,
                       selling_price=%s, active=%s, updated_at=now(),
                       price_origin='human'
                   WHERE id=%s""",
                (fields["name"], fields["ean"], fields["units_per_tray"],
                 fields["retail_price"], fields["selling_price"], fields["active"], target),
            )
            return target
        row = conn.execute(
            """INSERT INTO products
               (name, ean, units_per_tray, retail_price, selling_price, active,
                price_origin)
               VALUES (%s, %s, %s, %s, %s, %s, 'human') RETURNING id""",
            (fields["name"], fields["ean"], fields["units_per_tray"],
             fields["retail_price"], fields["selling_price"], fields["active"]),
        ).fetchone()
    return row["id"]


def find_product_by_key(name: str, ean: str | None, conn=None) -> dict | None:  # noqa: ANN001
    """The offer import's product key: a valid (8/13-digit) EAN when the row has
    one, else the exact name. Suspect-length EANs are never persisted at all
    (products.ean is UNIQUE and junk must never merge distinct products) — the
    driver surfaces them as warnings only."""
    key_ean = ean if (ean and len(ean) in (8, 13)) else None
    existing = get_product_by_ean(key_ean, conn=conn) if key_ean else None
    if existing is None:
        existing = get_product_by_name(name.strip(), conn=conn)
    return existing


def upsert_product_by_key(
    name: str,
    ean: str | None,
    units_per_tray: int | None,
    conn,  # noqa: ANN001 — caller's transaction (offer import batches in one)
    selling_price: float | None = None,
    retail_price: float | None = None,
) -> tuple[int, bool]:
    """Upsert for the offer import (keying per :func:`find_product_by_key`).
    Price provenance decides what an import may touch (``products.price_origin``,
    migration 0012): a ``human`` price — set by an admin on the admin site — is
    never written by an import, while an ``auto`` price is replaced outright by
    the new offer's. Price movements arrive in the supplier's offers, so an auto
    price must follow the latest one rather than freeze at the first import.
    ``ean`` is only written on create or when the offer row has one (a blank
    offer EAN never wipes a stored one). Returns (product_id, created)."""
    name = name.strip()
    existing = find_product_by_key(name, ean, conn=conn)

    if existing is not None:
        if existing.get("price_origin") == "human":
            # Admin-managed prices: touch identity/UPT only, never the money.
            conn.execute(
                """UPDATE products
                   SET name=%s, units_per_tray=COALESCE(%s, units_per_tray),
                       updated_at=now(), ean=COALESCE(%s, ean)
                   WHERE id=%s""",
                (name, units_per_tray, ean or None, existing["id"]),
            )
        else:
            # Import-derived prices: assign outright (no COALESCE — that is what
            # froze them at the first offer), so this offer's prices take effect.
            conn.execute(
                """UPDATE products
                   SET name=%s, units_per_tray=COALESCE(%s, units_per_tray),
                       updated_at=now(), ean=COALESCE(%s, ean),
                       selling_price=%s, retail_price=%s, price_origin='auto'
                   WHERE id=%s""",
                (name, units_per_tray, ean or None, selling_price, retail_price,
                 existing["id"]),
            )
        return existing["id"], False

    row = conn.execute(
        """INSERT INTO products
           (name, ean, units_per_tray, selling_price, retail_price, active,
            price_origin)
           VALUES (%s, %s, %s, %s, %s, TRUE, 'auto') RETURNING id""",
        (name, ean or None, units_per_tray or 1, selling_price, retail_price),
    ).fetchone()
    return row["id"], True


def record_offer_import(
    filename: str,
    uploaded_by: str,
    row_count: int,
    products_created: int,
    products_updated: int,
    costings_created: int,
    skipped: int,
    conn,  # noqa: ANN001 — caller's transaction (one import = one batch row)
    finalised: bool = False,
) -> int:
    """One row per confirmed offer upload — each upload is a saved version of
    the product/costing set (migration 0011). ``finalised`` records whether the
    wizard's bulk-finalise checkbox was ticked. Returns the batch id."""
    row = conn.execute(
        """INSERT INTO offer_imports
           (filename, uploaded_by, row_count, products_created, products_updated,
            costings_created, skipped, finalised)
           VALUES (%s, %s, %s, %s, %s, %s, %s, %s) RETURNING id""",
        (filename, uploaded_by, row_count, products_created, products_updated,
         costings_created, skipped, finalised),
    ).fetchone()
    return row["id"]


def create_offer_import(
    filename: str,
    uploaded_by: str,
    conn,  # noqa: ANN001 — caller's transaction (one import = one batch row)
    finalised: bool = False,
) -> int:
    """Open the batch row for an offer upload and return its id. Created BEFORE
    the rows are costed so every costing can carry ``offer_import_id`` — the link
    that makes a product's prices a dated series, one point per offer. The counts
    are filled in by :func:`update_offer_import_counts` at the end of the same
    transaction, so a failed import leaves no batch behind."""
    row = conn.execute(
        """INSERT INTO offer_imports (filename, uploaded_by, finalised)
           VALUES (%s, %s, %s) RETURNING id""",
        (filename, uploaded_by, finalised),
    ).fetchone()
    return row["id"]


def update_offer_import_counts(
    offer_import_id: int,
    row_count: int,
    products_created: int,
    products_updated: int,
    costings_created: int,
    skipped: int,
    conn,  # noqa: ANN001 — the import's transaction
) -> None:
    """Write the tallies onto a batch row opened by :func:`create_offer_import`."""
    conn.execute(
        """UPDATE offer_imports
           SET row_count=%s, products_created=%s, products_updated=%s,
               costings_created=%s, skipped=%s
           WHERE id=%s""",
        (row_count, products_created, products_updated, costings_created,
         skipped, offer_import_id),
    )


def get_offer_history() -> list[dict]:
    """Every offer upload, newest first, with how many costings it produced —
    the collapsed rows of the price history list."""
    with pool().connection() as conn:
        rows = conn.execute(
            """SELECT oi.id, oi.filename, oi.uploaded_at, oi.uploaded_by,
                      oi.finalised, oi.row_count, oi.products_created,
                      oi.products_updated, COUNT(c.id) AS costing_count
               FROM offer_imports oi
               LEFT JOIN costings c ON c.offer_import_id = oi.id
               GROUP BY oi.id
               ORDER BY oi.uploaded_at DESC, oi.id DESC"""
        ).fetchall()
    return [_row(r) for r in rows]


def get_offer_detail(offer_import_id: int) -> list[dict]:
    """The per-product prices recorded by one offer — what an expanded row shows.
    Costs/prices come from the costing snapshot, not the product, so the figures
    are the ones this offer actually produced even if the product moved since."""
    with pool().connection() as conn:
        rows = conn.execute(
            """SELECT p.name, p.ean, c.version, c.status,
                      (c.results->>'total_direct_cost')::numeric AS total_cost,
                      (c.inputs->>'selling_price')::numeric      AS selling_price,
                      (c.inputs->>'retail_price')::numeric       AS retail_price
               FROM costings c
               JOIN products p ON p.id = c.product_id
               WHERE c.offer_import_id = %s
               ORDER BY p.name""",
            (offer_import_id,),
        ).fetchall()
    return [_row(r) for r in rows]


def get_offer_history_data() -> dict:
    """Offer history for the customer-facing Costing tab: every upload, newest
    first, each carrying the per-product prices it recorded. Two queries (not one
    per offer), stitched in Python, so the tab stays a single inlined payload."""
    offers = get_offer_history()
    if not offers:
        return {"offers": []}
    with pool().connection() as conn:
        rows = conn.execute(
            """SELECT c.offer_import_id, p.name, p.ean, c.version, c.status,
                      (c.results->>'total_direct_cost')::numeric AS total_cost,
                      (c.inputs->>'selling_price')::numeric      AS selling_price,
                      (c.inputs->>'retail_price')::numeric       AS retail_price
               FROM costings c
               JOIN products p ON p.id = c.product_id
               WHERE c.offer_import_id = ANY(%s)
               ORDER BY p.name""",
            ([o["id"] for o in offers],),
        ).fetchall()
    by_offer: dict[int, list[dict]] = {}
    for r in rows:
        item = _row(r)
        by_offer.setdefault(item.pop("offer_import_id"), []).append(item)
    for o in offers:
        o["items"] = by_offer.get(o["id"], [])
        o["uploaded_at"] = o["uploaded_at"].isoformat() if o.get("uploaded_at") else None
    return {"offers": offers}


# ── Box types ────────────────────────────────────────────────────────────────


def list_box_types(active_only: bool = False) -> list[dict]:
    q = "SELECT * FROM box_types"
    if active_only:
        q += " WHERE active = TRUE"
    q += " ORDER BY code"
    with pool().connection() as conn:
        rows = conn.execute(q).fetchall()
    return [_row(r) for r in rows]


def get_box_type(box_id: int) -> dict | None:
    with pool().connection() as conn:
        row = conn.execute("SELECT * FROM box_types WHERE id = %s", (box_id,)).fetchone()
    return _row(row)


def upsert_box_type(
    code: str,
    price: float,
    model: str | None = None,
    dimensions: str | None = None,
    active: bool = True,
    box_id: int | None = None,
) -> int:
    with pool().connection() as conn:
        if box_id is not None:
            conn.execute(
                """UPDATE box_types SET code=%s, model=%s, dimensions=%s, price=%s, active=%s
                   WHERE id=%s""",
                (code.strip(), model, dimensions, price, active, box_id),
            )
            return box_id
        row = conn.execute(
            """INSERT INTO box_types (code, model, dimensions, price, active)
               VALUES (%s, %s, %s, %s, %s)
               ON CONFLICT (code) DO UPDATE
                   SET model=EXCLUDED.model, dimensions=EXCLUDED.dimensions,
                       price=EXCLUDED.price, active=EXCLUDED.active
               RETURNING id""",
            (code.strip(), model, dimensions, price, active),
        ).fetchone()
    return row["id"]


# ── Cost-menu items ──────────────────────────────────────────────────────────

MENU_KINDS = ("packaging_per_unit", "packaging_per_case", "equipment", "labour")


def list_menu_items(kind: str | None = None, active_only: bool = False) -> list[dict]:
    clauses, params = [], []
    if kind:
        clauses.append("kind = %s")
        params.append(kind)
    if active_only:
        clauses.append("active = TRUE")
    where = ("WHERE " + " AND ".join(clauses)) if clauses else ""
    q = f"SELECT * FROM cost_menu_items {where} ORDER BY kind, sort_order, name"
    with pool().connection() as conn:
        rows = conn.execute(q, params).fetchall()
    return [_row(r) for r in rows]


def get_menu_item(item_id: int) -> dict | None:
    with pool().connection() as conn:
        row = conn.execute("SELECT * FROM cost_menu_items WHERE id = %s", (item_id,)).fetchone()
    return _row(row)


def upsert_menu_item(
    kind: str,
    name: str,
    unit_cost: float,
    active: bool = True,
    sort_order: int = 0,
    item_id: int | None = None,
) -> int:
    if kind not in MENU_KINDS:
        raise ValueError(f"invalid menu kind: {kind!r}")
    with pool().connection() as conn:
        if item_id is not None:
            conn.execute(
                """UPDATE cost_menu_items
                   SET kind=%s, name=%s, unit_cost=%s, active=%s, sort_order=%s
                   WHERE id=%s""",
                (kind, name.strip(), unit_cost, active, int(sort_order), item_id),
            )
            return item_id
        row = conn.execute(
            """INSERT INTO cost_menu_items (kind, name, unit_cost, active, sort_order)
               VALUES (%s, %s, %s, %s, %s)
               ON CONFLICT (kind, name) DO UPDATE
                   SET unit_cost=EXCLUDED.unit_cost, active=EXCLUDED.active,
                       sort_order=EXCLUDED.sort_order
               RETURNING id""",
            (kind, name.strip(), unit_cost, active, int(sort_order)),
        ).fetchone()
    return row["id"]


# ── Costings (versioned snapshots) ───────────────────────────────────────────


def save_costing(
    product_id: int,
    inputs: dict,
    results: dict,
    created_by: str | None = None,
    status: str = "draft",
    conn=None,  # noqa: ANN001 — caller's transaction (offer import batches in one)
    offer_import_id: int | None = None,
) -> dict:
    """Persist a costing as the next version for a product. ``inputs``/``results``
    are the fully-resolved snapshots (see calc / router). Version assignment is a
    single INSERT with a MAX(version)+1 subquery, guarded by UNIQUE(product_id,
    version) so a concurrent save fails loudly rather than duplicating a version.
    ``offer_import_id`` ties the version to the dated offer that produced it (NULL
    for a costing saved by hand in the admin calculator), so a product's prices
    form one point per uploaded offer. Returns {id, version}."""
    if status not in ("draft", "final"):
        raise ValueError(f"invalid costing status: {status!r}")
    if conn is not None:
        row = conn.execute(
            """INSERT INTO costings (product_id, version, status, inputs, results,
                                     created_by, offer_import_id)
               VALUES (
                   %s,
                   (SELECT COALESCE(MAX(version), 0) + 1 FROM costings WHERE product_id = %s),
                   %s, %s::jsonb, %s::jsonb, %s, %s)
               RETURNING id, version""",
            (product_id, product_id, status,
             json.dumps(inputs), json.dumps(results), created_by, offer_import_id),
        ).fetchone()
        return {"id": row["id"], "version": row["version"]}
    with pool().connection() as c:
        row = c.execute(
            """INSERT INTO costings (product_id, version, status, inputs, results,
                                     created_by, offer_import_id)
               VALUES (
                   %s,
                   (SELECT COALESCE(MAX(version), 0) + 1 FROM costings WHERE product_id = %s),
                   %s, %s::jsonb, %s::jsonb, %s, %s)
               RETURNING id, version""",
            (product_id, product_id, status,
             json.dumps(inputs), json.dumps(results), created_by, offer_import_id),
        ).fetchone()
    return {"id": row["id"], "version": row["version"]}


def list_costings(product_id: int) -> list[dict]:
    with pool().connection() as conn:
        rows = conn.execute(
            "SELECT id, product_id, version, status, created_by, created_at "
            "FROM costings WHERE product_id = %s ORDER BY version DESC",
            (product_id,),
        ).fetchall()
    return _clean(rows)


def get_costing(costing_id: int) -> dict | None:
    with pool().connection() as conn:
        row = conn.execute("SELECT * FROM costings WHERE id = %s", (costing_id,)).fetchone()
    return dict(row) if row else None


def get_latest_costing(product_id: int, status: str | None = None) -> dict | None:
    clauses = ["product_id = %s"]
    params: list = [product_id]
    if status:
        clauses.append("status = %s")
        params.append(status)
    where = " AND ".join(clauses)
    with pool().connection() as conn:
        row = conn.execute(
            f"SELECT * FROM costings WHERE {where} ORDER BY version DESC LIMIT 1",
            params,
        ).fetchone()
    return dict(row) if row else None


def get_costing_dashboard_data() -> dict:
    """Customer-facing read model for the dashboard 'costing' tab: every active
    product with its latest FINAL costing's headline figures + full breakdown.
    Products without a final costing are included with nulls so the tab shows the
    full catalogue."""
    products = list_products(active_only=True)
    rows = []
    for p in products:
        latest = get_latest_costing(p["id"], status="final")
        results = latest["results"] if latest else None
        total_cost = (results or {}).get("total_direct_cost")
        rows.append({
            "id": p["id"],
            "name": p["name"],
            "ean": p.get("ean"),
            "selling_price": p.get("selling_price"),
            "retail_price": p.get("retail_price"),
            "version": latest["version"] if latest else None,
            "total_direct_cost": total_cost,
            # Our price = total direct cost + 10% (the operator's pricing rule).
            # Derived at read time from the stored result, like the other
            # headline figures — no new stored field, calc untouched.
            "our_price": round(total_cost * 1.10, 4) if total_cost is not None else None,
            "our_gp": (results or {}).get("our_gp"),
            "our_gp_pct": (results or {}).get("our_gp_pct"),
            "customer_gp_pct": (results or {}).get("customer_gp_pct"),
            "results": results,
        })
    return {"products": rows}
