-- Extend the per-customer costing_defaults with the engine constants the offer
-- import needs (the roses-sheet values): inbound pallet rate / boxes per CC /
-- qty per box, and outbound price per pallet / fill rate / boxes on order /
-- fuel surcharge. These are shared across all imported products — the offer
-- sheet supplies per-product data (material cost, UPT, box height) only.
--
-- 0009 seeded the costing_defaults row on a fresh DB; this merge adds the new
-- keys to BOTH fresh and already-migrated installs. JSONB || is idempotent
-- (existing keys keep their stored value, missing keys are added), matching the
-- house ON CONFLICT DO NOTHING seed style.

UPDATE settings
   SET value = value || '{
        "pallet_rate": 125,
        "boxes_per_cc": 252,
        "qty_per_box": 1,
        "price_per_pallet": 50,
        "fill_rate": 0.8,
        "boxes_on_order": 24,
        "fuel_surcharge_pct": 0
    }'::jsonb,
       updated_at = now()
 WHERE key = 'costing_defaults';
