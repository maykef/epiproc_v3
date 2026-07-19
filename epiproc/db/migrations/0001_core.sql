-- EpiProc v3 core schema. ONE database per customer container.
-- The invoices/invoice_items shape is grounded in v1's actual extractor writes
-- (the INSERT/UPDATE columns in invoice_extraction_v9.py). A `status` column
-- gives per-invoice lineage: extracted -> verified -> categorised -> published.
-- NOTE: seed schema — reconcile against v1's full 53-column list during the
-- extraction build before loading real data.

CREATE TABLE IF NOT EXISTS invoices (
    id                  SERIAL PRIMARY KEY,
    supplier            TEXT NOT NULL,
    filename            TEXT NOT NULL,
    document_type       TEXT,
    invoice_number      TEXT,
    invoice_date        TEXT,                 -- ISO string, as v1
    currency            TEXT,
    seller_name         TEXT,
    buyer_name          TEXT,
    buyer_dept          TEXT,
    ship_to_name        TEXT,
    sold_to_name        TEXT,
    subtotal            DOUBLE PRECISION,
    discount_amount     DOUBLE PRECISION,
    discount_2          DOUBLE PRECISION,
    freight             DOUBLE PRECISION,
    handling_charges    DOUBLE PRECISION,
    vat_amount          DOUBLE PRECISION,
    total_amount        DOUBLE PRECISION,
    service_tier        TEXT,
    notes               TEXT,
    validation_warning  TEXT,
    corrections_applied TEXT,                 -- audit trail of rule ops applied
    raw_json            TEXT,
    extraction_error    TEXT,
    processing_time_s   DOUBLE PRECISION,
    status              TEXT NOT NULL DEFAULT 'extracted',
    created_at          TIMESTAMPTZ DEFAULT now(),
    UNIQUE (supplier, filename)
);
CREATE INDEX IF NOT EXISTS idx_inv_supplier ON invoices (supplier);
CREATE INDEX IF NOT EXISTS idx_inv_date     ON invoices (invoice_date);
CREATE INDEX IF NOT EXISTS idx_inv_status   ON invoices (status);

CREATE TABLE IF NOT EXISTS invoice_items (
    id                   SERIAL PRIMARY KEY,
    invoice_id           INTEGER NOT NULL REFERENCES invoices(id) ON DELETE CASCADE,
    position             INTEGER,
    article              TEXT,
    quantity             DOUBLE PRECISION,
    unit                 TEXT,
    description          TEXT,
    unit_price           DOUBLE PRECISION,
    total_price          DOUBLE PRECISION,
    line_discount_amount DOUBLE PRECISION,
    category             TEXT                  -- filled by categorise stage
);
CREATE INDEX IF NOT EXISTS idx_item_invoice  ON invoice_items (invoice_id);
CREATE INDEX IF NOT EXISTS idx_item_category ON invoice_items (category);

-- Full-text search on line items (always current — no separate index process).
ALTER TABLE invoice_items ADD COLUMN IF NOT EXISTS fts tsvector
    GENERATED ALWAYS AS (to_tsvector('english', coalesce(description,'') || ' ' || coalesce(article,''))) STORED;
CREATE INDEX IF NOT EXISTS idx_item_fts ON invoice_items USING GIN (fts);

-- Job queue (the only orchestrator).
CREATE TABLE IF NOT EXISTS jobs (
    id          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    job_type    TEXT NOT NULL,                -- onboard | extract | categorise | report
    params      JSONB NOT NULL DEFAULT '{}',
    status      TEXT NOT NULL DEFAULT 'queued',
    status_text TEXT,
    result      JSONB,
    error       TEXT,
    worker_pid  INTEGER,
    created_at  TIMESTAMPTZ DEFAULT now(),
    started_at  TIMESTAMPTZ,
    finished_at TIMESTAMPTZ
);
CREATE INDEX IF NOT EXISTS idx_jobs_status ON jobs (status, created_at);

-- Auth / audit / analytics plane (ported from v1; single-tenant).
CREATE TABLE IF NOT EXISTS users (
    id            SERIAL PRIMARY KEY,
    username      TEXT UNIQUE NOT NULL,
    display_name  TEXT,
    email         TEXT,
    password_hash TEXT NOT NULL,
    mfa_secret    TEXT,
    role          TEXT NOT NULL DEFAULT 'viewer',
    suppliers     TEXT[],
    active        BOOLEAN NOT NULL DEFAULT TRUE,
    expires_at    DATE,
    last_login    TIMESTAMPTZ,
    created_at    TIMESTAMPTZ DEFAULT now()
);

CREATE TABLE IF NOT EXISTS sessions (
    id         UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id    INTEGER REFERENCES users(id) ON DELETE CASCADE,
    created_at TIMESTAMPTZ DEFAULT now(),
    expires_at TIMESTAMPTZ NOT NULL,
    last_seen  TIMESTAMPTZ,
    ip         INET,
    user_agent TEXT,
    revoked    BOOLEAN NOT NULL DEFAULT FALSE
);
CREATE INDEX IF NOT EXISTS idx_sessions_user ON sessions (user_id, revoked);

CREATE TABLE IF NOT EXISTS audit_log (
    id         BIGSERIAL PRIMARY KEY,
    ts         TIMESTAMPTZ NOT NULL DEFAULT now(),
    username   TEXT NOT NULL,
    ip         INET,
    action     TEXT NOT NULL,
    resource   TEXT,
    detail     JSONB,
    user_agent TEXT
);
CREATE INDEX IF NOT EXISTS idx_audit_ts ON audit_log (ts);

CREATE TABLE IF NOT EXISTS usage_events (
    id         BIGSERIAL PRIMARY KEY,
    ts         TIMESTAMPTZ NOT NULL DEFAULT now(),
    username   TEXT,
    session_id TEXT,
    event      TEXT,
    supplier   TEXT,
    detail     JSONB
);

CREATE TABLE IF NOT EXISTS invite_tokens (
    token      TEXT PRIMARY KEY,
    user_id    INTEGER REFERENCES users(id) ON DELETE CASCADE,
    expires_at TIMESTAMPTZ NOT NULL,
    used       BOOLEAN NOT NULL DEFAULT FALSE
);
