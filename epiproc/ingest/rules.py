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
    code, e.g. 06031100). Some suppliers (e.g. Dutch flower exporters) repeat the
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
            tot[k] = -abs(v); changed = True
    for it in rec.get("line_items") or []:
        for k in ("unit_price", "total_price"):
            v = it.get(k)
            if isinstance(v, (int, float)) and v > 0:
                it[k] = -abs(v); changed = True
    return rec, ("credit_note_sign: flipped to negative" if changed else None)


@op("derive_total_from_subtotal")
def _derive_total(rec: dict) -> tuple[dict, str | None]:
    """Safety net (v1 S2): total null but subtotal present -> use subtotal."""
    tot = rec.get("totals") or {}
    if tot.get("total") is None and isinstance(tot.get("subtotal"), (int, float)):
        tot["total"] = tot["subtotal"]
        return rec, f"derive_total: total={tot['subtotal']} from subtotal"
    return rec, None


DEFAULT_RULES = ["drop_hs_summary", "credit_note_sign", "derive_total_from_subtotal"]


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
