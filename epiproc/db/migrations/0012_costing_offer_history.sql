-- Price history: tie every costing version to the offer import that produced
-- it, so a product's prices form a dated series (one point per uploaded offer)
-- that the UI can list newest-first and compare for sudden increases.
--
-- NULL offer_import_id = a costing saved by hand in the admin calculator, not
-- from an offer. ON DELETE SET NULL keeps hand-saved and imported history alive
-- if a batch row is ever removed.
ALTER TABLE costings
    ADD COLUMN IF NOT EXISTS offer_import_id BIGINT
        REFERENCES offer_imports(id) ON DELETE SET NULL;

CREATE INDEX IF NOT EXISTS costings_offer_import_idx
    ON costings (offer_import_id);

-- Price provenance: an auto price computed by an import must be recomputed by
-- the NEXT offer (prices move with the supplier's offers), while a price an
-- admin set by hand stays put. No backfill: existing rows are admin-managed by
-- definition, since no offer import has run before this migration.
ALTER TABLE products
    ADD COLUMN IF NOT EXISTS price_origin TEXT NOT NULL DEFAULT 'human'
        CHECK (price_origin IN ('human', 'auto'));
