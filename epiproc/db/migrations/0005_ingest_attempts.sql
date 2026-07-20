-- Track how many times the scanner has failed on a file, so a non-transient
-- failure is retried a few times (not skipped forever on a matching mtime) and
-- then surfaced rather than silently lost.
ALTER TABLE ingested_files ADD COLUMN IF NOT EXISTS attempts INTEGER NOT NULL DEFAULT 0;
