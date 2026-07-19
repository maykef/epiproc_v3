"""Declarative corrections engine — replaces v1's ~28 hardcoded correct_* branches.

In v1 a supplier quirk was imperative Python doing SQL UPDATEs on inserted rows
(re-querying SUM(total_price) between steps). Here a quirk is DATA in the
supplier YAML, e.g.:

    rules:
      - op: flip_sign_if
        when: document_type == "Credit Note"
        fields: [total_amount, subtotal]
      - op: scale
        when: multi_year
        factor: "1/years"
        fields: [unit_price, total_price, line_discount_amount]
      - op: derive_total_from_subtotal      # Sartorius S2 safety net
        when: total_amount is null and subtotal is not null
      - op: suppress_check
        check: C4

Each op is a small pure function (record in -> record out + an applied-note
string). No SQL surgery, no per-supplier if-branches, fully portable and testable.
The generic ops that v1 ran on EVERY supplier become opt-in per config, killing
the "generic correction mis-fires on another supplier" risk.
"""
from __future__ import annotations


def apply_rules(record: dict, cfg) -> tuple[dict, list[str]]:  # noqa: ANN001
    """Apply the supplier's declarative rule list to one extracted record.

    STUB. Build order:
      1. Define the op vocabulary (flip_sign_if, scale, derive_*, drop_item_if,
         merge_bundle, suppress_check, ...) as a registry of pure functions.
      2. Translate each v1 correct_* into one or more ops; attach to the right
         supplier YAML under `rules:`.
      3. Return (corrected_record, applied_notes) — notes go to corrections_applied.
    """
    return record, []
