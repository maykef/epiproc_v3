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

- **`db/dashboard.py` size (~930 lines)** — cohesive (all dashboard data access)
  and already sectioned, so a split is pure churn with no behaviour change.
  Deliberately deferred: do it only when the file is next touched for real work
  (e.g. Reports or the pagination endpoints), not for its own sake.
- **Reports** — parameterised report generation is in progress (see README).

## Recently closed

- **Offer-sheet batch import built and mock-verified** (2026-08-19, branch
  `feature/costing-offer-import`, not yet deployed) — Admin → Costing → Import
  offer (.xlsx): upload → dry-run preview → confirm, one transaction per import,
  idempotent re-imports keyed by valid EAN (else exact name), one `offer_imports`
  row + archived workbook per upload, per-product versioned costings — always
  **draft** unless the admin ticks the preview's **bulk-finalise checkbox**.
  Column mapping per the operator: B name, C EAN, J UPT, L box height (first
  integer → smallest real box model it fits, 34/40/48/80), **N** material cost
  (column M deliberately ignored), K display only; shared constants from
  `costing_defaults` (migration 0010). Rows without stored prices get auto
  selling = direct ÷ 0.9 (Target FP) and auto retail = selling ÷ 0.65 × 1.2
  (Target Retail) — filling customer GP% with zero manual steps; stored prices
  always win. Dashboard Costing tab columns finalised per the operator:
  Total cost, then **Our Price** (cost + 10%, derived), Our GP%, Customer GP%,
  **Customer price** (retail inc VAT) last — no "Selling" column on the tab —
  and every costing money cell renders the instance's `settings.currency_symbol`
  (€ on the demo, Jinja `currency()` global). Tests: offer parser + box
  resolution units, costing integration additions (84 tests with the PG DSN).
  Full E2E green twice on the costingdemo mock (draft run + bulk-finalise run).
  Known data-quality flag: the real offer file's rows 8/10 share barcode
  8713626094170 (two different Chrysanthemums) — flagged `duplicate_ean`, the
  operator should fix one. Awaiting operator approval of the mock before baking
  into `epiproc:3`.
- **Per-unit product costing shipped to the live floral_portal instance**
  (2026-08-14) — the costing module (migration 0009, admin calculator, read-only
  customer Costing dashboard tab) was clone-tested against real data, merged to
  master and deployed as `epiproc:3.0.0-50252f9`, with the Costing tab enabled on
  the instance. The workbook's worked example was seeded (product "Roses", final
  costing v1); all 18 pinned spreadsheet values — every intermediate and output —
  match the stored costing to 1e-9, and the stored snapshot recomputes identically
  from its inputs. See `docs/costing.md`. Remaining: the interactive "what-if"
  playground (deferred pending the client) and invoice-derived input seeding
  (longer term).
- **Domain purge — engine made fully generic** — removed all lab/microscopy/
  research-institute code a reviewer could use to infer origins: the CUFS
  (Cambridge University Financial System) subsystem + hardcoded institutes in
  `normalisation.py` (now config-driven via `departments.yml`), the **Reagents
  Intel** and **Service Intel** tabs/pages/JS + the `_dash_svc` builder +
  `get_service_intel` + `svc` plumbing, microscopy-flavoured Reports presets
  (TCO/capital-equipment/instrument/service-contract/serial-number), and stale
  comments/test fixtures. Deleted `docs/AUDIT.md` (named the "18 supplier YAMLs
  crown jewel"). −1546 lines; clone-verified (renders, `node --check` clean, 55
  tests). Kept generic "By Department" and "Reports" (genericised) by design.
- **Customer DB now actually backed up** — the daily `nvme8tb`→`tank` rsync
  silently skipped `pgdata` (postgres-owned 0700, unreadable by the backup user),
  so the DB wasn't in the backup. Added `~/bin/dump_customer_dbs.sh` (cron 01:15)
  that `pg_dump`s each running customer DB into `<customer>/snapshots/nightly-db.sql.gz`
  before the 02:00 mirror. Not a repo file — see memory `backups-and-db-dump`.

- **Postgres integration test in CI** — `ci.yml` now runs a health-checked
  `postgres:16` service and sets `EPIPROC_PG_TEST_DSN`, so `run_migrations()`
  (`db/pool.py:42`) and real SQL are exercised in CI instead of only fakes. New
  `tests/test_integration_postgres.py` (marker `integration`, skipped when no DSN so
  a dev box with no DB still passes): asserts the full 0001→0008 chain applies from
  an empty schema, that re-running is idempotent, and round-trips `insert_record()`
  → `get_suppliers()`/`get_data_quality()` — plus a case pinning the
  `(supplier, filename)` upsert so a re-processed file overwrites rather than
  duplicates. A follow-up can drive login→MFA→session through the ASGI stack with
  `httpx`. Verified locally against a throwaway `postgres:16` (4 integration tests
  pass; suite = 59 with DSN, 55 + 4 skipped without).
- **Front-end libs vendored; CDN dropped from the CSP** — chart.js 4.4.4, d3 7.9.0
  and d3-sankey 0.12.3 are now committed under `web/static/vendor` and served
  same-origin via a `/static` `StaticFiles` mount (with SRI `integrity`), so the
  dashboard needs no outbound internet and `_csp()` no longer whitelists
  `cdn.jsdelivr.net` in `script-src`/`font-src` — a compromised CDN can no longer
  inject into the nonce-protected page. Regression test: `tests/test_static_vendor.py`.
- **Rate-limit lockout behind a proxy** — the pre-auth limiter keyed on the raw
  socket peer (slowapi `get_remote_address`), so behind a reverse proxy every client
  shared the proxy's IP and one client tripping the login limit locked everyone out.
  `_key_func` now uses `_request_ip`, honouring `EPIPROC_TRUST_XFF` to recover the
  real client IP. Regression test: `tests/test_rate_limit_key.py`.
- **`cli.py new` `.env` perms** — the scaffolded `.env` (holds `POSTGRES_PASSWORD`
  + `EPIPROC_SESSION_KEY`) is now created owner-only (0600) via `os.open(..., O_EXCL,
  0o600)`, matching the session-key file. Regression test: `tests/test_cli_env_perms.py`.
- **Login-CSRF comment accuracy** — the double-submit comment now documents that the
  pre-session token is unbound (only as strong as `ds_csrf` cookie integrity) rather
  than overclaiming; no behaviour change, honest scope.
- **Dashboard XSS / CSP** — VLM-extracted strings are now HTML-escaped on every
  DOM insertion, and the CSP dropped `script-src 'unsafe-inline'`: inline scripts
  carry a per-request nonce and inline event handlers were replaced by delegated
  listeners (`data-click`/`data-change`/`data-input`). `object-src`, `base-uri`
  and `form-action` are locked down. Verified against a clone of real data.
