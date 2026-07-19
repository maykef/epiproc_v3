-- Two-level classification: category (broad type) + variety (specific product).
ALTER TABLE invoice_items ADD COLUMN IF NOT EXISTS variety TEXT;
CREATE INDEX IF NOT EXISTS idx_item_variety ON invoice_items (variety);
