"""Unit tests for the #7/#8 helper functions.

- _date: invoice_date normalisation for the new DATE column (#8c).
- csrf_inject_html: the single-sourced CSRF injector shared by the dashboard and
  the admin templates (#8d).
- _slim: the payload trimmer that inlines only client-read fields (#7).
"""
from __future__ import annotations

import datetime

from epiproc.db.invoices import _date
from epiproc.web.csrf import csrf_inject_html


def test_date_parses_iso_and_rejects_junk():
    assert _date("2026-06-11") == datetime.date(2026, 6, 11)
    assert _date("2026-06-11T09:00:00") == datetime.date(2026, 6, 11)  # trailing time ok
    assert _date("not-a-date") is None
    assert _date("") is None
    assert _date(None) is None
    assert _date(datetime.date(2026, 1, 2)) == datetime.date(2026, 1, 2)


def test_csrf_inject_html():
    out = csrf_inject_html("deadbeef")
    assert 'name="csrf-token"' in out
    assert "X-CSRF-Token" in out
    assert "deadbeef" in out
    assert csrf_inject_html("") == ""  # no token -> nothing injected


def test_slim_keeps_only_allowed_keys():
    import pytest
    pytest.importorskip("psycopg")  # epiproc.db.dashboard imports the pool module
    from epiproc.db.dashboard import _ITEM_KEEP, _slim
    full = {
        "invoice_id": 1, "description": "Rosa", "total_price": 9.0, "category": "Roses",
        # fields the client never reads and must be dropped from the payload:
        "ship_to_address": "x", "buyer_customer_number": "y", "seller_name": "z",
        "notes": "n", "sold_to_name": "s",
    }
    slim = _slim(full, _ITEM_KEEP)
    assert slim == {"invoice_id": 1, "description": "Rosa", "total_price": 9.0, "category": "Roses"}
    for dropped in ("ship_to_address", "buyer_customer_number", "seller_name", "notes", "sold_to_name"):
        assert dropped not in slim
