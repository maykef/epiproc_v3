-- invoice_date was TEXT holding an ISO string. Store it as a real DATE so range
-- queries and date maths are correct and malformed values can't sneak in.
-- Guarded cast: only well-formed YYYY-MM-DD values convert; anything else (never
-- a usable date anyway) becomes NULL rather than failing the migration.
ALTER TABLE invoices
    ALTER COLUMN invoice_date TYPE date
    USING (
        CASE
            WHEN invoice_date ~ '^\d{4}-\d{2}-\d{2}$'
            THEN invoice_date::date
            ELSE NULL
        END
    );
