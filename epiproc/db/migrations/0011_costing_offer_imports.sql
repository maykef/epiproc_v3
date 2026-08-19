-- Batch record for offer imports: one row per confirmed upload, so every file
-- upload is a saved version of the product/costing set (the uploaded workbook
-- is archived alongside as data_dir/imports/offer_<id>_<filename>, and each
-- product's costing keeps its own per-product version bump). Inserted inside
-- the same transaction as the import — all-or-nothing.

CREATE TABLE IF NOT EXISTS offer_imports (
    id               BIGSERIAL PRIMARY KEY,
    filename         TEXT NOT NULL,
    uploaded_by      TEXT,
    uploaded_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    row_count        INTEGER NOT NULL DEFAULT 0,
    products_created INTEGER NOT NULL DEFAULT 0,
    products_updated INTEGER NOT NULL DEFAULT 0,
    costings_created INTEGER NOT NULL DEFAULT 0,
    skipped          INTEGER NOT NULL DEFAULT 0,
    finalised        BOOLEAN NOT NULL DEFAULT FALSE  -- bulk-finalise checkbox
);
