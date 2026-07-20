# Roadmap & known limitations

Tracked work that is intentionally deferred, with the intended approach and the
risks that make each one more than a quick patch.

## Dashboard payload — paginate instead of shipping the whole dataset

**Today:** `build_(multi_)dashboard_html` inlines the full `INVOICES` and `ITEMS`
arrays into the page (`dashboard_html._inline_data`), and every chart reads from
those globals. This is fine at the current scale (tens of invoices) but grows
linearly — a large customer would ship megabytes on every load.

**Planned approach:**
- Move aggregates (category/supplier/department totals, monthly series) to
  server-side SQL endpoints that return only the rolled-up numbers the charts need.
- Serve the invoice/line-item tables from a paged endpoint (`/api/items?…`) with
  server-side filtering and sorting; the client fetches pages on demand.
- Keep the current inline path as a fast route for small instances.

**Risk / why deferred:** the dashboard's client JS assumes the full `INVOICES`
/`ITEMS` arrays are present; this touches nearly every chart and the search/table
views. It is a redesign, not a patch, and needs its own testing pass.

## Invoice dates — store as `DATE`, not `TEXT`

**Today:** `invoices.invoice_date` is `TEXT` holding an ISO string
(`0001_core.sql`). Ordering works because ISO strings sort lexically, but range
queries and date maths are string-based and non-ISO values are not caught.

**Planned approach:**
- Audit existing values for anything not `YYYY-MM-DD` parseable.
- Migration: add `invoice_date_d DATE`, backfill with a safe cast
  (`NULLIF` / regex-guarded), verify counts, then swap columns and update the
  extractor/insert path and queries.

**Risk / why deferred:** a naive `ALTER … TYPE date USING invoice_date::date`
fails on any unparseable value in live data and touches the ingest write path and
several queries. It needs a data-validation step first and careful, reversible
migration.

## Known minor items

- **Residual DOM-XSS in dashboard template JS** — the stored-XSS fix escapes the
  `<script>` context; VLM strings are still inserted via `innerHTML` in the
  template's client JS (e.g. `populateSearchSuppliers`, table renderers). Sanitise
  on insert (`textContent`/escape) to close it fully.
- **Duplicated inline scripts** — the CSRF injector is mirrored between
  `dashboard_html._csrf_inject` and `admin/base.html`; candidate for a shared include.
- **Reports** — parameterised report generation is in progress (see README).
