-- Ledger of every PDF the folder-scanner has looked at, so auto-processing is
-- idempotent: a file is extracted at most once, and unchanged files are skipped
-- without touching the GPU. `result` is the outcome, not just "seen".
CREATE TABLE IF NOT EXISTS ingested_files (
    path         TEXT PRIMARY KEY,          -- absolute path inside the container
    mtime        DOUBLE PRECISION,          -- st_mtime at processing time
    sha256       TEXT,                      -- content hash (dedups identical files)
    invoice_id   INTEGER,                   -- FK-ish: the row it produced (if any)
    result       TEXT NOT NULL,             -- ingested | duplicate | error
    message      TEXT,
    processed_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_ingested_sha ON ingested_files (sha256);
