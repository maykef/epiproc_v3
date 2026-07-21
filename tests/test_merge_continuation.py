"""_merge_continuation — folding a multi-page invoice's continuation pages into
the first page's record (ported from v1). The matching logic is subtle: a
continuation line is merged onto an existing line only when it shares a position
AND the same article or description; otherwise it is a genuinely new line. Prices
from the continuation always win; other fields only fill gaps. A regression here
either double-counts spend (wrongly appending) or drops it (wrongly merging).
"""
from __future__ import annotations

from epiproc.ingest.pdf_vlm import _merge_continuation


def test_new_line_is_appended_and_matched_line_is_updated_in_place():
    data = {
        "line_items": [
            {"position": 1, "article": "A1", "description": "Roses",
             "unit_price": 1.0, "total_price": 10.0, "quantity": 10},
        ],
        "totals": {"subtotal": 10.0},
    }
    cont = {
        "line_items": [
            # Same position + same article -> merge onto the existing line.
            {"position": 1, "article": "A1", "quantity": None,
             "unit_price": 1.5, "total_price": 15.0, "unit": "units"},
            # New position -> appended as a distinct line.
            {"position": 2, "article": "A2", "description": "Gadgets",
             "unit_price": 2.0, "total_price": 20.0},
        ],
        "totals": {"vat_amount": 5.0, "total": 40.0},
    }

    _merge_continuation(data, cont)

    items = data["line_items"]
    assert len(items) == 2

    p1 = items[0]
    assert p1["position"] == 1
    assert p1["unit_price"] == 1.5      # price from continuation always wins
    assert p1["total_price"] == 15.0
    assert p1["quantity"] == 10         # None in continuation -> existing kept
    assert p1["description"] == "Roses"  # not present in continuation -> kept
    assert p1["unit"] == "units"        # gap on existing -> filled from continuation

    assert items[1]["position"] == 2    # the new line

    # Totals merge: continuation fields added, existing subtotal preserved.
    assert data["totals"] == {"subtotal": 10.0, "vat_amount": 5.0, "total": 40.0}


def test_same_position_but_different_content_is_a_new_line_not_a_merge():
    data = {"line_items": [
        {"position": 1, "article": "A1", "description": "Roses", "total_price": 10.0}]}
    cont = {"line_items": [
        {"position": 1, "article": "ZZ", "description": "Different", "total_price": 99.0}]}

    _merge_continuation(data, cont)

    # Neither article nor description matches, so it must NOT overwrite line 1.
    assert len(data["line_items"]) == 2
    totals = {it["total_price"] for it in data["line_items"]}
    assert totals == {10.0, 99.0}


def test_header_fields_and_totals_only_overwrite_when_present():
    data = {"line_items": [], "payment_terms": "Net 30", "notes": "orig"}
    cont = {"line_items": [], "payment_terms": None, "notes": "page 2 note"}

    _merge_continuation(data, cont)

    assert data["payment_terms"] == "Net 30"     # None in continuation -> kept
    assert data["notes"] == "page 2 note"        # present -> overwritten
