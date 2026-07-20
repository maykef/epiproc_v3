# Roadmap & known limitations

Tracked work, with the intended approach and the risks behind each.

## Dashboard payload — full pagination (partially addressed)

**Done:** the inlined `ITEMS`/`INVOICES` arrays now carry only the fields the
client JS actually reads — the row queries still fetch the rest for server-side
derivation (department normalisation, aggregates, service intel), but ~20 unused
columns per row are stripped before inlining (`dashboard._slim`). The item array
(the one that grows with the data) dropped from ~35 to ~16 fields per row.

**Still open:** the page still inlines *every* row. For very large instances the
next step is server-side pagination — serve the invoice/line-item tables from a
paged endpoint (`/api/items?…`) with server-side filter/sort, and have the
client-side supplier filter round-trip instead of re-aggregating the full arrays.
Deferred because it touches nearly every chart plus the search/table views and is
a redesign, not a patch.

## Known minor items

- **Residual DOM-XSS in dashboard template JS** — the stored-XSS fix escapes the
  `<script>` context; VLM strings are still inserted via `innerHTML` in the
  template's client JS (e.g. `populateSearchSuppliers`, table renderers). Sanitise
  on insert (`textContent`/escape) to close it fully.
- **Reports** — parameterised report generation is in progress (see README).
