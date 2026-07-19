# EpiProc v3 — the engine

A **self-sufficient**, **data-free** procurement engine. One image; each customer
is a container that mounts its own data and runs its own Postgres.

- **Engine (this repo)** = pure code: onboarding, dedup, scanning/extraction,
  rules, categorisation, verification, dashboard + admin/auth. No customer data.
- **A customer = a container** carrying `invoices/`, `pgdata/`, `reports/`,
  `configs/`, filled dashboards. Same engine image, many data containers.
- **Runtime deps:** its own Postgres (sidecar) + the shared vLLM GPU server by
  `EPIPROC_VLLM_URL`. Nothing from epiproc_v1 or epiproc_v2.

## Deploy a customer
```bash
python cli.py new acme 5011 --institution "ACME University"
cp docker/docker-compose.template.yml /mnt/nvme8tb/customers/acme/compose.yml
cd /mnt/nvme8tb/customers/acme && docker compose up -d
# -> migrations run, empty dashboard at :5011. Drop PDFs, run process.
```

## Status (2026-07-19)

Working end-to-end and live on a first real customer instance (`floral_portal`).

| Area | State |
|------|-------|
| Engine image `epiproc:3` + per-customer container (app + Postgres sidecar) | ✅ done |
| **Extraction** — `ingest/pdf_vlm.py` per-page vision + guided JSON (`response_format` json_schema) | ✅ done |
| **Rules** — `ingest/rules.py` declarative ops (credit-note sign, derive-total, drop HS-code summary) | ✅ done |
| **Categorisation** — `ingest/categorise.py` two-level **category + variety**, per-customer scheme, worker job | ✅ done |
| Web / auth / admin / dashboard plane (single-DB port) | ✅ done |
| Per-customer settings (tabs, currency, price-tracker mode, scheme) + **Admin → Dashboard** editor | ✅ done |
| Dashboard: tab/KPI/column toggles, supplier colours, chart drill-downs, supplier treemap | ✅ done |
| `ingest/dedup.py`, `ingest/verify.py` | copied from v1, not yet wired into the job pipeline |
| Process invoices **as a queue job** (worker handles `categorise`, not yet `extract`/`onboard`) | pending |
| **Reports** engine (`epiproc/reports/`, `/reports` router, report jobs) | **not built (P4)** |
| Extraction truncation-retry; prod hardening (secure cookies, self-service pw); tests/CI | pending |

## Per-customer customisation (one image, no forks)
Everything customer-specific lives in that container's own Postgres, editable from
**Admin → Dashboard** (admin-only): visible **tabs**, **currency**, **Price Tracker
grouping** (article / category / variety), and the **categorisation scheme** (e.g. a
flower buyer categorises by flower type). Changing these needs no rebuild.

See `docs/AUDIT.md` for the keep/rewrite/drop verdicts this repo is built from.
