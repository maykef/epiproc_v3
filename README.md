# EpiProc Atlas

**Turn a pile of supplier invoice PDFs into a living map of your spend.**

EpiProc Atlas is an AI-powered procurement intelligence platform. You drop in the
invoices you already receive — messy, multi-format, multi-supplier PDFs — and Atlas
reads them, structures them, categorises every line item, tracks how prices move over
time, and surfaces where the money actually goes. No manual data entry, no spreadsheet
wrangling, no per-supplier templates.

It runs entirely on your own infrastructure. Invoices never leave the box: extraction
is done by a local vision-language model, and each customer's data lives in its own
private database.

---

## What it does

### 📁 Invoice repository
A single, searchable home for every invoice you've ever received. Atlas ingests PDFs,
extracts the header (supplier, date, invoice number, totals, currency) and every line
item (description, article code, quantity, unit price, department), de-duplicates
re-sends, and stores it all as clean structured data. Browse and filter by supplier,
category, department, or date — the paper trail becomes a queryable dataset.

### 🏷 Categorisation
Every line item is automatically classified using a **customer-specific scheme** —
because "categories" mean different things to different buyers. A flower wholesaler
categorises by flower type (Roses, Peonies, Chrysanthemums…); a lab by
equipment/consumables/reagents. Classification is **two-level (category + variety)**,
runs on the local model against the extracted text (fast and cheap, no vision needed),
and the vocabulary is editable per customer with no code change.

### 📈 Price tracking
Atlas watches the same product bought over time and flags how its price moves. For
each tracked item it shows lowest/highest list and net price, the net change per month,
and which departments paid what discount. Tracking granularity is configurable per
customer — by **article**, **category**, or **variety** — so a floral buyer tracks by
flower type while a lab tracks by catalogue article.

### 📊 Spend intelligence
The Overview turns raw invoices into decisions:
- **Monthly spend by category and by supplier** — a rolling latest-12-months view.
- **Spend-flow Sankey** (Supplier → Category → Department) with click-through drill-down.
- **Supplier treemap** — spend by supplier, drilling into that supplier's categories.
- **Category × Department matrix**, category totals, spend distribution, and headline
  KPIs (total invoices, top supplier, top category…).

### 🔍 Insights
Domain lenses layered on top of the same data:
- **Service Intel** — service-contract spend by tier, tier × department, renewal
  tracking, and **price-variance alerts** (same article, different price by department).
- **Reagents Intel** — consumable spend by supplier, reagent catalogue and article-level
  drill-down.
- **Reports** — parameterised, filterable report generation *(in progress — see roadmap)*.

Every tab, KPI, and column can be switched on or off per customer, so each instance
shows only the intelligence that matters to that buyer.

---

## How it works

```
PDF ─► extract (local vision model, guided JSON) ─► rules ─► store ─► verify ─► categorise ─► dashboard
```

- **Extract** — `ingest/pdf_vlm.py` reads each page with a vision-language model and a
  strict JSON schema, so the output is structured, not free text.
- **Rules** — `ingest/rules.py` applies declarative corrections (credit-note sign,
  derive-total, drop HS-code summary rows, …).
- **Categorise** — `ingest/categorise.py` classifies line items against the customer's
  scheme.
- **Serve** — a FastAPI app with auth, an admin plane, and the interactive dashboard.

---

## Architecture — one image, many customers

Atlas is a **data-free engine**: this repository is pure code. Each customer is a
**container** that carries its own data and runs its own database sidecar.

- **Engine (this repo):** onboarding, extraction, rules, categorisation, verification,
  dashboard, admin/auth. Ships as one image, `epiproc:3`. No customer data.
- **A customer = a container** mounting its own `invoices/`, `pgdata/`, `reports/`,
  `configs/`, and holding its own filled dashboard. Same image, many data containers.
- **Runtime deps:** its own Postgres (sidecar) + a shared local vision-model server
  reached via `EPIPROC_VLLM_URL`. Nothing leaves the host.

### Per-customer customisation (no forks, no rebuilds)
Everything customer-specific lives in that container's own Postgres and is editable from
**Admin → Dashboard**: visible **tabs**, **currency**, **price-tracker grouping**
(article / category / variety), and the **categorisation scheme**. One image serves
every customer; changing any of these needs no rebuild.

---

## Deploy a customer

```bash
python cli.py new acme 5011 --institution "ACME University"
cp docker/docker-compose.template.yml /mnt/nvme8tb/customers/acme/compose.yml
cd /mnt/nvme8tb/customers/acme && docker compose up -d
# -> migrations run, empty dashboard at :5011. Drop PDFs in invoices/, then process.
```

---

## Status (2026-07-20)

Working end-to-end and live on a first real customer instance (`floral_portal`).

| Area | State |
|------|-------|
| Engine image `epiproc:3` + per-customer container (app + Postgres sidecar) | ✅ done |
| **Extraction** — per-page vision + guided JSON (`response_format` json_schema) | ✅ done |
| **Rules** — declarative ops (credit-note sign, derive-total, drop HS-code summary) | ✅ done |
| **Categorisation** — two-level category + variety, per-customer scheme, worker job | ✅ done |
| Web / auth / admin / dashboard plane | ✅ done |
| Per-customer settings (tabs, currency, price-tracker mode, scheme) + **Admin → Dashboard** editor | ✅ done |
| Dashboard: spend intel, price tracker, Service/Reagents Intel, drill-downs, treemap | ✅ done |
| `ingest/dedup.py`, `ingest/verify.py` | copied from v1, not yet wired into the job pipeline |
| Process invoices **as a queue job** (worker handles `categorise`, not yet `extract`/`onboard`) | pending |
| **Reports** engine (`epiproc/reports/`, `/reports` router, report jobs) | not built (P4) |
| Extraction truncation-retry; prod hardening (secure cookies, self-service pw); tests/CI | pending |

See `docs/AUDIT.md` for the keep/rewrite/drop verdicts this repo is built from.
