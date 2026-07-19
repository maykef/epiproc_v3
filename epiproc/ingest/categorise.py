"""Line-item spend categorisation.

PORT TARGET: /mnt/nvme8tb/prefect_invoice_extraction/scripts/generate_dashboard_v5.py
(Phase 1). This logic lives ONLY in the retired repo — not in v1 or v2 — so to
keep v3 self-sufficient it must be copied in and owned here.

Categories are defined in configs/categorisation.yml (12 categories: Capital
Equipment, Consumables, Spare Parts, Accessories & IT, Service Contracts,
Preventive Maintenance, Installation, Labour & Repairs, Software, Training,
Freight & Handling, Discounts & Adjustments). "Unsure -> Consumables";
"Delivery Notes -> empty".
"""
from __future__ import annotations


def categorise_invoice(invoice_id: int, cfg, client) -> int:  # noqa: ANN001
    """Classify every line item of one invoice; write invoice_items.category.

    STUB. Build order:
      1. Read generate_dashboard_v5.py Phase 1 from the retired repo.
      2. Port the categorisation prompt call + row-number->category mapping.
      3. UPDATE invoice_items.category; return count categorised.
    """
    raise NotImplementedError("categorise_invoice — P3 build (port from retired repo)")
