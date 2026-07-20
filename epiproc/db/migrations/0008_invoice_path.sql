-- Store each invoice's PDF path on the row so the download route can open the
-- file directly instead of walking the whole invoices/ tree on a path miss.
-- Populated at ingest and backfilled at boot (backfill_paths); nullable so old
-- rows are valid until backfilled.
ALTER TABLE invoices ADD COLUMN IF NOT EXISTS path TEXT;
