# EpiProc Atlas

**Procurement intelligence derived from an organisation's existing supplier invoices.**

EpiProc Atlas is an AI-powered procurement intelligence platform. It ingests the
supplier invoices an organisation already receives — in any format and from any
supplier — and automatically extracts, structures, and categorises each line item,
tracks price movements over time, and reports where expenditure is concentrated. It
removes the need for manual data entry, spreadsheet consolidation, and per-supplier
templates.

The platform processes data entirely within the organisation's own infrastructure.
Invoices are never transmitted to a third party: extraction is performed by a locally
hosted vision-language model, and each customer's data resides in a dedicated private
database.

---

## Capabilities

### 📁 Invoice repository
A single, searchable record of every invoice the organisation has received. Atlas
ingests PDFs, extracts the header (supplier, date, invoice number, totals, currency)
and each line item (description, article code, quantity, unit price, department),
removes duplicate submissions, and stores the result as structured data. Invoices can
be browsed and filtered by supplier, category, department, or date, converting the
document archive into a queryable dataset.

### 🏷 Categorisation
Each line item is automatically classified using a **customer-specific scheme**, since
category definitions differ between buyers. A flower wholesaler categorises by flower
type (Roses, Peonies, Chrysanthemums); a laboratory by equipment, consumables, and
reagents. Classification is **two-level (category and variety)** and runs on the local
model against the extracted text, which keeps it fast and low-cost. The vocabulary is
editable per customer without code changes.

### 📈 Price tracking
Atlas monitors each recurring product and reports how its price changes over time. For
every tracked item it records the lowest and highest list and net prices, the net
change per month, and the discount applied by each department. Tracking granularity is
configurable per customer — by **article**, **category**, or **variety** — so a floral
buyer can track by flower type while a laboratory tracks by catalogue article.

### 📊 Spend intelligence
The Overview presents invoice data as decision-ready analytics:
- **Monthly spend by category and by supplier** — a rolling latest-12-months view.
- **Spend-flow Sankey** (Supplier → Category → Department) with click-through drill-down.
- **Supplier treemap** — spend by supplier, drilling into that supplier's categories.
- **Category × Department matrix**, category totals, spend distribution, and headline
  KPIs (total invoices, top supplier, top category).

### 🔍 Insights
Domain-specific analyses layered on the same data:
- **Service Intel** — service-contract spend by tier, tier × department, renewal
  tracking, and **price-variance alerts** (same article, different price by department).
- **Reagents Intel** — consumable spend by supplier, with reagent catalogue and
  article-level drill-down.
- **Reports** — parameterised, filterable report generation *(in progress — see roadmap)*.

Every tab, KPI, and column can be enabled or disabled per customer, so each instance
presents only the analyses relevant to that buyer.

---

## How it works

```
PDF ─► extract (local vision model, guided JSON) ─► rules ─► store ─► verify ─► categorise ─► dashboard
```

- **Extract** — `ingest/pdf_vlm.py` reads each page with a vision-language model and a
  strict JSON schema, producing structured output rather than free text.
- **Rules** — `ingest/rules.py` applies declarative corrections (credit-note sign,
  derive-total, drop HS-code summary rows).
- **Categorise** — `ingest/categorise.py` classifies line items against the customer's
  scheme.
- **Serve** — a FastAPI application providing authentication, an administration plane,
  and the interactive dashboard.

---

## Architecture — one image, many customers

Atlas is a **data-free engine**: this repository contains code only. Each customer runs
as a **container** that holds its own data and its own database sidecar.

- **Engine (this repository):** onboarding, extraction, rules, categorisation,
  verification, dashboard, and administration/authentication. It ships as a single
  image, `epiproc:3`, and contains no customer data.
- **A customer is a container** mounting its own `invoices/`, `pgdata/`, `reports/`,
  and `configs/` directories and holding its own populated dashboard. One image serves
  many data containers.
- **Runtime dependencies:** a per-customer Postgres sidecar and a shared, locally hosted
  vision-model server reached via `EPIPROC_VLLM_URL`. No data leaves the host.

### Per-customer customisation (no forks, no rebuilds)
All customer-specific configuration resides in that container's own Postgres database
and is editable from **Admin → Dashboard**: the visible **tabs**, **currency**,
**price-tracker grouping** (article, category, or variety), and the **categorisation
scheme**. A single image serves every customer; changing any of these settings requires
no rebuild.

---

## Deployment model

EpiProc Atlas is not a cloud service. It is distributed as a self-contained Docker
image, which gives each customer full control over where it runs. The same image can be
deployed **on-premises** on the customer's own hardware, hosted on **EpiProc's managed
servers**, or run on the customer's **preferred cloud provider**. In every case the
customer's invoices and database remain within the environment they select, and no data
is shared with any external party.

### Deploy a customer

```bash
python cli.py new acme 5011 --institution "ACME University"
cp docker/docker-compose.template.yml /mnt/nvme8tb/customers/acme/compose.yml
cd /mnt/nvme8tb/customers/acme && docker compose up -d
# -> migrations run, empty dashboard at :5011. Add PDFs to invoices/, then process.
```

---

## Status (2026-07-20)

Operational end-to-end and running on a first production customer instance
(`floral_portal`).

| Area | State |
|------|-------|
| Engine image `epiproc:3` + per-customer container (app + Postgres sidecar) | ✅ done |
| **Extraction** — per-page vision + guided JSON (`response_format` json_schema) | ✅ done |
| **Rules** — declarative ops (credit-note sign, derive-total, drop HS-code summary) | ✅ done |
| **Categorisation** — two-level category + variety, per-customer scheme, worker job | ✅ done |
| Web / authentication / administration / dashboard plane | ✅ done |
| Per-customer settings (tabs, currency, price-tracker mode, scheme) + **Admin → Dashboard** editor | ✅ done |
| Dashboard: spend intel, price tracker, Service/Reagents Intel, drill-downs, treemap | ✅ done |
| **Auto-processing** — worker scans `invoices/` on a timer, runs extract → rules → dedup → store → categorise; also an `extract` job + Admin button | ✅ done |
| **Dedup** — by `invoice_number` (pipeline) + content hash & `ingested_files` ledger (scan); idempotent, GPU-safe re-scans | ✅ done |
| Verify — light non-blocking status check wired; full v1 C0–C5 checks pending port (`legacy/verify_v1_sqlite.py`) | partial |
| **Reports** engine (`epiproc/reports/`, `/reports` router, report jobs) | not built (P4) |
| Extraction truncation-retry; production hardening (secure cookies, self-service password reset); tests/CI | pending |

See `docs/AUDIT.md` for the keep/rewrite/drop assessment this repository is built from.
