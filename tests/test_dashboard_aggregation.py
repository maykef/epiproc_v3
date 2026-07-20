"""Dashboard aggregation layer — the pure `_dash_*` reducers in db.dashboard.

These turn the row lists into the numbers the charts render. They take plain
lists (no DB), so they are cheap to pin — and a silent regression here shows up
as wrong money on screen. We assert the money maths, the null handling, the
Uncategorised/Unknown fallbacks, and the sort order the client relies on.
"""
from __future__ import annotations

from epiproc.db import dashboard as d


def test_cat_totals_sums_by_category_and_sorts_desc():
    items = [
        {"category": "Roses", "total_price": 100.0},
        {"category": "Roses", "total_price": 50.0},
        {"category": "Tulips", "total_price": 200.0},
        {"category": None, "total_price": 5.0},        # -> Uncategorised
        {"category": "Roses", "total_price": None},    # ignored (no price)
    ]
    out = d._dash_cat_totals(items)
    # Sorted by total descending.
    assert [r["cat"] for r in out] == ["Tulips", "Roses", "Uncategorised"]
    roses = next(r for r in out if r["cat"] == "Roses")
    assert roses["total"] == 150.0
    assert roses["count"] == 2                          # the None-price row not counted
    assert next(r for r in out if r["cat"] == "Uncategorised")["total"] == 5.0


def test_dept_totals_uses_invoice_total_and_unknown_fallback():
    invoices = [
        {"department": "Pathology", "total_amount": 300.0},
        {"department": "Pathology", "total_amount": 100.0},
        {"department": None, "total_amount": 40.0},     # -> Unknown
        {"department": "Genetics", "total_amount": None},  # ignored
    ]
    out = d._dash_dept_totals(invoices)
    assert [r["dept"] for r in out] == ["Pathology", "Unknown"]
    assert next(r for r in out if r["dept"] == "Pathology")["total"] == 400.0
    # A department whose only invoice has no total contributes nothing.
    assert all(r["dept"] != "Genetics" for r in out)


def test_monthly_cat_buckets_by_year_month_and_sorts():
    items = [
        {"invoice_date": "2026-03-15", "category": "Roses", "total_price": 10.0},
        {"invoice_date": "2026-03-20", "category": "Roses", "total_price": 5.0},
        {"invoice_date": "2026-02-01", "category": "Tulips", "total_price": 7.0},
        {"invoice_date": "n/a", "category": "Roses", "total_price": 99.0},   # too short -> dropped
        {"invoice_date": None, "category": "Roses", "total_price": 1.0},     # None -> dropped
    ]
    out = d._dash_monthly_cat(items)
    assert out == [
        {"month": "2026-02", "cat": "Tulips", "total": 7.0},
        {"month": "2026-03", "cat": "Roses", "total": 15.0},
    ]


def test_rounding_helper_keeps_four_places_and_passes_none():
    assert d._r(1.234567) == 1.2346
    assert d._r(None) is None
