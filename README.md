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

## What's built vs stubbed (skeleton state)

| Area | State |
|------|-------|
| settings / db pool / migrations / job queue / worker loop | authored |
| dashboard template + admin templates | copied from v1 (empty until data) |
| `ingest/dedup.py`, `ingest/verify.py`, `normalisation.py` | copied from v1 (needs import rewiring) |
| `ingest/pdf_vlm.py` (guided-JSON extraction) | **STUB — the one true rewrite** |
| `ingest/rules.py` (declarative corrections) | **STUB** |
| `ingest/categorise.py` | **STUB — port from retired generate_dashboard_v5.py** |
| web routers / auth / admin (full port) | pending |

## Build order
1. Skeleton (this) — empty dashboard + plumbing + stubs.
2. **P2 extraction** — `pdf_vlm` (response_format json_schema) + `rules`; wire
   dedup/verify. Milestone: drop PDFs → rows appear.
3. **P3 categorise + search + dashboard data** — port categoriser; PG FTS.
4. **P4 reports** — port the agentic report loop.
5. Full web/auth/admin port; tests; CI.

See `docs/AUDIT.md` for the keep/rewrite/drop verdicts this repo is built from.
