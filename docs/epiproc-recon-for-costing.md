# EpiProc Atlas — Codebase Recon for the Per-Unit Costing Module

*Recon for the per-product costing calculator that replaces the floral_portal
Excel workbook. Paths are relative to the repo root. This report predates the
implementation; see `docs/costing.md` for the delivered module.*

## 0. Executive summary

- **Engine-in-a-box.** One data-free Docker image (`epiproc:3`); each customer is a
  container with its own Postgres sidecar and mounted data folders. "Floral Portal"
  is a *deployed instance* (`floral_portal`), not anything in this repo — the repo is
  code only.
- **Stack:** Python 3.11+/3.12, FastAPI, psycopg3 (raw SQL, **no ORM**), Postgres 16,
  Jinja2 + a hand-built HTML/vanilla-JS dashboard. LLM work goes to a shared local
  vLLM server (Qwen-family vision model) via the OpenAI SDK.
- **Reusable for costing:** invoices + line items (unit price, qty, discounts),
  currency handling, VAT-amount capture, a per-customer key/value `settings` table
  (the reference-data pattern), a job queue + worker, an auth/role model, and a
  SQL-first aggregation layer.
- **Must be built new:** product master data, suppliers/customers as entities, cost
  build-ups, margins/GP, target-price solvers, packaging/labour menus, box-type
  lookups, transport params, waste/overhead %, exchange rates, selling/retail price.
  Costing is a new bounded context on the invoice engine, not an extension of
  existing tables.

## 1. Stack & architecture

- **Deps** (`pyproject.toml`, single source of truth): FastAPI, uvicorn, psycopg[binary]+pool,
  pydantic v2, pydantic-settings, jinja2, pyyaml, argon2-cffi, pyotp, openai, slowapi,
  pymupdf, pillow, prometheus-client. No ORM. `requires-python = ">=3.11"`; image is `python:3.12-slim`.
- **DB:** Postgres 16 (`docker/docker-compose.template.yml`).
- **Frontend:** server-rendered; admin via Jinja templates (`epiproc/web/templates/admin/*.html`),
  the customer dashboard is one 2200-line HTML file (`dashboard_template.html`) with vanilla JS +
  vendored chart.js/d3 served locally. No React, no build step, no form library.
- **Layout:** monolith package `epiproc/` — `db/` (raw SQL + `migrations/*.sql`), `ingest/`
  (extraction pipeline), `web/` (app, routers, auth, templates). Entry points:
  `epiproc.web.app:app` and `python -m epiproc.worker`, launched by `docker/start.sh`
  (migrations run first).
- **Model integration:** OpenAI SDK pointed at vLLM (`vllm_model = "qwen3.5-122b"`), used for
  (1) vision extraction (`ingest/pdf_vlm.py`, strict-JSON `response_format`) and (2) text
  categorisation (`ingest/categorise.py`). **The costing module makes no LLM calls.**

## 2. Data model (all in `epiproc/db/migrations/*.sql`, forward-only; head 0008 at recon)

- `invoices` (header) ⟵ `invoice_items` (lines) via `invoice_id … ON DELETE CASCADE`.
  Line items carry `unit_price`, `quantity`, `total_price`, `line_discount_amount`,
  `category`, `variety`.
- `settings` (0002): per-customer `key`/`value` JSONB — the reference-data / config pattern.
- `jobs`, `users` (role + `suppliers TEXT[]`), `sessions`, `audit_log`, `usage_events`,
  `invite_tokens`, `ingested_files`, `schema_migrations`.
- **Not entities:** supplier is a `TEXT` slug on `invoices`; customer/client is not modelled
  (one customer per container); product is only ever an `invoice_items` row (no EAN, no master).
- **Costs/margins:** none. Purchase price only (`unit_price`). `get_costs()` is a stubbed
  LLM-spend log, unrelated. **VAT** captured descriptively (`vat_amount`), never computed; no
  rate table. **Currency** per-invoice + display symbol only. **Exchange rates:** none.

## 3. Extension patterns

- A feature is a vertical slice: `migrations/000N_*.sql` (auto-applied at boot, lexical order,
  each file one transaction, advisory-locked) → `db/*.py` raw-SQL functions → `routers/*.py`
  `APIRouter` registered in `web/app.py` → Jinja admin page and/or a dashboard tab.
- **Auth:** not DB-multi-tenant (one container = one customer = one Postgres). Roles: `admin`
  checked via a `_admin(request)` guard (`routers/admin.py`); routes depend on `get_session_user`.
  Per-supplier ACL via `users.suppliers TEXT[]`.
- **Forms:** raw HTML `<form method=post>` + `Form(...)`, server-side validation re-rendering at 422
  (see `routers/reset.py`, `routers/admin.py`). CSRF via middleware (`_csrf` field auto-injected).
  Nonce CSP, no `unsafe-inline`, no inline `on*` handlers — interactions use `data-*` + delegation.
- **Reference data:** the `settings` JSONB table (tabs, categories, currency, price-tracker key)
  with `get_setting`/`set_setting`; and baked YAML configs for extraction only.

## 4. Integration points for costing

- Almost nothing costing needs exists: no product master/EAN, no suppliers/customers as entities,
  no cost build-up, margins, VAT rate table, FX, selling/retail price, or solvers.
- **Product master** → a new first-class `products` table (durable identity). **Per-costing inputs**
  → a `costings` header snapshotting resolved params + line items; keep it immutable/versioned like
  `invoices.raw_json`. **Reusable menus & lookups** → reference tables (box types, menu items) with
  editable prices, following the `categories`/`settings` idiom.
- **Calculation layer:** none to extend — the nearest analogue is `db/dashboard.py` (SQL
  aggregations). Costing math should be a **new pure-Python module** (deterministic, unit-tested),
  with DB helpers in `db/costing.py` and HTTP in `routers/costing.py`. Do the math in Python, not
  the LLM.

## 5. Schema summary

```
invoices(id, supplier TEXT, invoice_number, invoice_date DATE, currency,
         subtotal, vat_amount, total_amount, status, raw_json, path)
invoice_items(id, invoice_id FK, article, quantity, unit_price, total_price,
              line_discount_amount, category, variety, fts)
settings(key PK, value JSONB)            -- per-customer config / reference data
users(id, username, role, suppliers TEXT[], …)   +  sessions/audit_log/jobs/…
```
Migrations forward-only, applied lexically at boot. Next file for costing: `0009_costing.sql`.

## 6. Open questions & risks

1. **No product master / EAN.** Product identity is greenfield; decide the product↔invoice link.
2. **No customer/client entity.** "GP for the customer" needs a clarified meaning (downstream
   client vs instance owner); tenancy is container-per-customer.
3. **No suppliers table** — supplier is a VLM-derived text slug.
4. **Single-currency, no FX.** Exchange rates are net-new and cross-cut every money field.
5. **VAT is descriptive, not computed** — costing must add a rate source and math.
6. `get_costs()` is a zero-returning LLM-spend stub; don't confuse it with product costing.
7. **Snapshot vs live reference** — snapshot resolved prices into a saved costing or its GP drifts.
8. **Forward-only migrations, no down-migrations**, run against the live floral_portal DB on the
   next deploy; test via the Postgres integration harness (`EPIPROC_PG_TEST_DSN`), clone-test first.
9. **No ORM/validation net** — costing correctness must live in unit tests on the pure calc.
10. **Nonce CSP** — a costing UI follows `_stamp_nonce`/`_inline_data`, escapes all inserted text,
    and adds no unauthenticated JSON route.
