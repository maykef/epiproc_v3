-- Per-unit product costing (replaces the floral_portal Excel workbook).
--
-- Additive and idempotent, matching the house style (CREATE TABLE IF NOT EXISTS,
-- ON CONFLICT DO NOTHING seeds). Four tables:
--   products         master data (EAN, name, units-per-tray, selling/retail price)
--   box_types        priced lookup of outbound box models
--   cost_menu_items  reusable packaging / equipment / labour line items, toggled
--                    on/off per costing by giving them a quantity
--   costings         a saved calculation: a per-product, monotonically-versioned
--                    snapshot of RESOLVED inputs + computed results (JSONB).
--
-- Snapshot philosophy (mirrors invoices.raw_json): `costings.inputs` embeds the
-- resolved prices used at save time — each material line, each selected menu
-- item's unit_cost, and the box price — so later edits to box_types /
-- cost_menu_items never silently change a saved costing. `results` is the full
-- computed snapshot so a historical GP is reproducible without re-deriving it.
--
-- Money is NUMERIC in the DB; percentages are stored as fractions (0.15 = 15%).

CREATE TABLE IF NOT EXISTS products (
    id             BIGSERIAL PRIMARY KEY,
    ean            TEXT UNIQUE,                      -- barcode; nullable, unique when present
    name           TEXT NOT NULL,
    units_per_tray INTEGER NOT NULL DEFAULT 1,       -- "UPT"
    retail_price   NUMERIC(12,4),                    -- inc. VAT, customer shelf price
    selling_price  NUMERIC(12,4),                    -- our price to the client, ex VAT
    active         BOOLEAN NOT NULL DEFAULT TRUE,
    created_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at     TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_products_active ON products (active);

CREATE TABLE IF NOT EXISTS box_types (
    id         BIGSERIAL PRIMARY KEY,
    code       TEXT UNIQUE NOT NULL,                 -- e.g. 'Бокс 34см'
    model      TEXT,
    dimensions TEXT,
    price      NUMERIC(12,4) NOT NULL,
    active     BOOLEAN NOT NULL DEFAULT TRUE
);

CREATE TABLE IF NOT EXISTS cost_menu_items (
    id         BIGSERIAL PRIMARY KEY,
    kind       TEXT NOT NULL CHECK (kind IN
                   ('packaging_per_unit','packaging_per_case','equipment','labour')),
    name       TEXT NOT NULL,
    unit_cost  NUMERIC(12,4) NOT NULL,
    active     BOOLEAN NOT NULL DEFAULT TRUE,
    sort_order INTEGER NOT NULL DEFAULT 0,
    UNIQUE (kind, name)
);

CREATE TABLE IF NOT EXISTS costings (
    id         BIGSERIAL PRIMARY KEY,
    product_id BIGINT NOT NULL REFERENCES products(id),
    version    INTEGER NOT NULL,                     -- per-product; next = max+1
    status     TEXT NOT NULL DEFAULT 'draft'
                   CHECK (status IN ('draft','final')),
    inputs     JSONB NOT NULL,                       -- full resolved input snapshot
    results    JSONB NOT NULL,                       -- full computed snapshot
    created_by TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (product_id, version)
);
CREATE INDEX IF NOT EXISTS idx_costings_product ON costings (product_id, version DESC);

-- ── Seeds (from the workbook's lookups tab) ──────────────────────────────────
-- Box types: only the four rows that carry a code; the three codeless models are
-- skipped intentionally.
INSERT INTO box_types (code, model, dimensions, price) VALUES
    ('Бокс 34см', 'Модел 10', '60х40х34', 0.648),
    ('Бокс 40см', 'Модел 11', '60х40х40', 0.686),
    ('Бокс 48см', 'Модел 12', '60х40х48', 0.742),
    ('Бокс 80см', 'Модел 13', '60х40х80', 0.979)
ON CONFLICT (code) DO NOTHING;

INSERT INTO cost_menu_items (kind, name, unit_cost, sort_order) VALUES
    ('packaging_per_unit', 'Small Sleeve (40cm)',   0.14,  0),
    ('packaging_per_unit', 'Price Label',           0.02,  1),
    ('packaging_per_case', 'Consumables',           0.03,  0),
    ('packaging_per_case', 'Box end',               0.01,  1),
    ('packaging_per_case', 'Additional label(s)',   0.005, 2),
    ('packaging_per_case', 'Spare 1 (per case)',    0,     3),
    ('equipment',          'Chep Pallets',          10,    0),
    ('equipment',          'Add ons',               0,     1),
    ('labour',             'Pack on line',          0.10,  0),
    ('labour',             'Labelling',             0.01,  1)
ON CONFLICT (kind, name) DO NOTHING;

-- Per-customer costing defaults (rates, percentages, target margins). Stored in
-- the existing settings key/value table so an admin can edit them per instance;
-- percentages are fractions.
INSERT INTO settings (key, value) VALUES (
    'costing_defaults',
    '{"vat_rate": 0.20, "eur_rate": 1.0, "usd_rate": 1.14,
      "waste_pct": 0.01, "intake_labour_pct": 0.10, "additional_pct": 0.10,
      "customer_target_margin": 0.35, "our_target_margin": 0.10}'::jsonb
) ON CONFLICT (key) DO NOTHING;
