"""Reconciliation rule: line-items vs subtotal vs total.

Regression for the false positive on a real invoice (W. Tuning 2603947): the
line items (incl. itemised deposits) summed exactly to the total, but the VLM
mis-read the *subtotal* (it lifted a VAT-base figure). The invoice reconciles at
the item->total level, so it must NOT be flagged — while genuine mismatches and
unitemised charges must still warn.
"""
from __future__ import annotations

from epiproc.ingest.rules import _reconcile_totals


def _warn(rec):
    out, _note = _reconcile_totals(rec)
    return out.get("validation_warning")


def test_items_reconcile_total_with_wrong_subtotal_is_not_flagged():
    # Mirrors invoice 2603947: items sum == total == 4456.86; subtotal mis-read as 3082.
    rec = {
        "line_items": [{"total_price": 2681.86}, {"total_price": 400.00}, {"total_price": 1375.00}],
        "totals": {"subtotal": 3082.0, "total": 4456.86, "vat_amount": 0.0},
    }
    assert _warn(rec) is None


def test_genuine_item_subtotal_mismatch_still_warns():
    # Items don't reconcile to total or subtotal -> real problem.
    rec = {
        "line_items": [{"total_price": 90.0}],
        "totals": {"subtotal": 100.0, "total": 100.0},
    }
    assert _warn(rec) and "≠ subtotal" in _warn(rec)


def test_unitemised_charge_still_warns():
    # Items == subtotal, but total is higher with no charge fields -> deposit/pallet
    # hidden in the total. line_sum != total, so it is (correctly) still flagged.
    rec = {
        "line_items": [{"total_price": 100.0}],
        "totals": {"subtotal": 100.0, "total": 200.0},
    }
    assert _warn(rec) and "unitemised charge" in _warn(rec)


def test_clean_invoice_with_vat_is_not_flagged():
    # Items == subtotal, subtotal + VAT == total.
    rec = {
        "line_items": [{"total_price": 100.0}],
        "totals": {"subtotal": 100.0, "vat_amount": 21.0, "total": 121.0},
    }
    assert _warn(rec) is None
