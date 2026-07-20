# Roadmap & known limitations

Tracked work, with the intended approach and the risks behind each.

## Dashboard payload — full pagination (parked; not currently a problem)

The dashboard inlines the whole dataset (`ITEMS`/`INVOICES`) into the page on
every load, so page weight grows with the data. **Parked** — in practice this has
held up fine at ~3000 invoices / ~15k line items with no user-visible problem, so
it is not a current ceiling. Revisit only if a much larger instance shows real
slowness.

**Already done (kept the growth rate down):** the inlined arrays carry only the
fields the client JS actually reads — the row queries still fetch the rest for
server-side derivation (department normalisation, aggregates, service intel), but
~20 unused columns per row are stripped before inlining (`dashboard._slim`). The
item array (the one that grows with the data) dropped from ~35 to ~16 fields/row.

**If/when revisited:** serve the invoice/line-item tables from a paged endpoint
(`/api/items?…`) with server-side filter/sort, and have the client-side supplier
filter round-trip instead of re-aggregating the full arrays. It is a redesign, not
a patch — it touches nearly every chart plus the search/table views — so it wants
its own design + testing pass.

## Known minor items

- **Residual DOM-XSS in dashboard template JS** — the stored-XSS fix escapes the
  `<script>` context; VLM strings are still inserted via `innerHTML` in the
  template's client JS (e.g. `populateSearchSuppliers`, table renderers). Sanitise
  on insert (`textContent`/escape) to close it fully.
- **`db/dashboard.py` size (~930 lines)** — cohesive (all dashboard data access)
  and already sectioned, so a split is pure churn with no behaviour change.
  Deliberately deferred: do it only when the file is next touched for real work
  (e.g. Reports or the pagination endpoints), not for its own sake.
- **Reports** — parameterised report generation is in progress (see README).
