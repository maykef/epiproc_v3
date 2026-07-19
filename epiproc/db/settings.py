"""Per-container settings (key/value in this instance's own DB).

Currently drives which dashboard tabs are visible for THIS customer. The engine
image is shared; the visible-tabs choice is data in the customer's own Postgres,
toggled from the admin panel. No forked templates, no per-customer image.
"""
from __future__ import annotations

import json

from epiproc.db.pool import pool

# Canonical tab list: (key used by showPage('key') + panel id "page-key", label).
DASHBOARD_TABS = [
    ("overview", "Overview"),
    ("suppliers", "By Supplier"),
    ("categories", "By Category"),
    ("departments", "By Department"),
    ("invoices", "Invoices"),
    ("pricetracker", "Price Tracker"),
    ("svccontracts", "Service Intel"),
    ("reagents", "Reagents Intel"),
    ("reports", "Reports"),
]
_ALL = [k for k, _ in DASHBOARD_TABS]


def get_setting(key: str, default=None):
    with pool().connection() as conn:
        r = conn.execute("SELECT value FROM settings WHERE key=%s", (key,)).fetchone()
    return r["value"] if r else default


def set_setting(key: str, value) -> None:
    with pool().connection() as conn:
        conn.execute(
            "INSERT INTO settings(key, value) VALUES (%s, %s::jsonb) "
            "ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value, updated_at = now()",
            (key, json.dumps(value)),
        )
        conn.commit()


def get_enabled_tabs() -> list[str]:
    v = get_setting("dashboard_tabs")
    if isinstance(v, list):
        return [t for t in _ALL if t in v]     # preserve canonical order, drop unknowns
    return list(_ALL)                          # default: everything on


def set_enabled_tabs(tabs) -> None:
    keep = [t for t in _ALL if t in set(tabs)]
    if not keep:                               # never leave a customer with no tabs
        keep = list(_ALL)
    set_setting("dashboard_tabs", keep)


# ── Categorisation scheme (per customer) ─────────────────────────────────────
# The category vocabulary is customer-specific: a flower wholesaler categorises
# by flower type, a lab by equipment/consumables. The scheme is an instruction to
# the local model; it lives in this instance's DB, editable per customer.
DEFAULT_CATEGORISATION = (
    "Categorise each line item by its product category using a short category "
    "name. For freight, delivery, packaging or handling lines use 'Other'."
)


def get_categorisation_scheme() -> str:
    return get_setting("categorisation_scheme") or DEFAULT_CATEGORISATION


def set_categorisation_scheme(text: str) -> None:
    set_setting("categorisation_scheme", (text or "").strip() or DEFAULT_CATEGORISATION)


# ── Currency symbol (per customer) ───────────────────────────────────────────
_CUR_SYMBOLS = {"EUR": "€", "GBP": "£", "USD": "$", "JPY": "¥", "CHF": "CHF ", "SEK": "kr "}


def get_currency_symbol() -> str:
    """Explicit setting wins; otherwise auto-detect from the data's currency."""
    v = get_setting("currency_symbol")
    if v:
        return v
    with pool().connection() as conn:
        r = conn.execute(
            "SELECT currency FROM invoices WHERE currency IS NOT NULL "
            "GROUP BY currency ORDER BY count(*) DESC LIMIT 1"
        ).fetchone()
    if r and r["currency"]:
        return _CUR_SYMBOLS.get(r["currency"].upper(), "£")
    return "£"


def set_currency_symbol(sym: str) -> None:
    set_setting("currency_symbol", sym)
