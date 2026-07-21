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
category definitions differ between buyers — they depend entirely on what the
organisation buys. The vocabulary is **discovered from the customer's own invoice data**
by the local model on first run, not hand-written or hardcoded. Classification is
**two-level (category and variety)** and runs on the local model against the extracted
text, which keeps it fast and low-cost. The vocabulary is editable per customer without
code changes.

### 📈 Price tracking
Atlas monitors each recurring product and reports how its price changes over time. For
every tracked item it records the lowest and highest list and net prices, the net
change per month, and the discount applied by each department. Tracking granularity is
configurable per customer — by **article**, **category**, or **variety** — so each buyer
tracks price movement at the level that matches how they purchase.

### 📊 Spend intelligence
The Overview presents invoice data as decision-ready analytics:
- **Monthly spend by category and by supplier** — a rolling latest-12-months view.
- **Spend-flow Sankey** (Supplier → Category → Department) with click-through drill-down.
- **Supplier treemap** — spend by supplier, drilling into that supplier's categories.
- **Category × Department matrix**, category totals, spend distribution, and headline
  KPIs (total invoices, top supplier, top category).

### 🔍 Insights
Further analyses layered on the same data:
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

## Create a new customer — manual runbook

Run from the host that holds the customer instances (paths below assume
`/mnt/nvme8tb`). This is the full, unassisted procedure.

### Prerequisites (once)

1. **Build the engine image locally** and reuse it for every customer:
   ```bash
   cd /mnt/nvme8tb/epiproc_v3
   docker build -t epiproc:3 -f docker/Dockerfile .
   ```
   Rebuild only when the engine code changes.
2. **The shared vLLM GPU server is running** on `:8000`, serving the model named in
   `EPIPROC_VLLM_URL` / `vllm_model` (currently `qwen3.5-122b`). Every customer talks to
   this one server for extraction and categorisation.

### 1. Scaffold the instance

```bash
cd /mnt/nvme8tb/epiproc_v3
python cli.py new acme 5011 --institution "ACME Ltd"
```
`acme` = instance name, `5011` = the **host** port (must be free and unique per
customer). This creates `/mnt/nvme8tb/customers/acme/` with the data folders
(`invoices/ pgdata/ configs/ reports/ …`) and a `.env` holding a generated DB password
(written to both `POSTGRES_PASSWORD` and the DSN so they match), a session key, and the
vLLM URL.

> `cli.py up`/`down`/`process` are stubs — use `docker compose` directly (below).

### 2. Add the compose file

```bash
cp docker/docker-compose.template.yml /mnt/nvme8tb/customers/acme/compose.yml
```

### 3. Drop in the invoices

```bash
cp /path/to/their/*.pdf /mnt/nvme8tb/customers/acme/invoices/inbox/
```
`invoices/inbox/` is the generic drop-box (supplier is derived from the extracted seller
name). Alternatively use `invoices/<supplier_name>/` to force the supplier by folder.

### 4. Start it

```bash
cd /mnt/nvme8tb/customers/acme
docker compose up -d
```
Runs DB migrations, then launches the web app (container port 5001, published on your
`5011`) and the worker.

### 5. Create the first admin ⚠️

A fresh instance has **no users** — there is no automatic admin bootstrap yet, so create
the first admin once:
```bash
docker exec acme-app-1 python -c "
from epiproc.db.pool import init_pool; init_pool()
from epiproc.db.users import create_user
from epiproc.web.auth import hash_password
create_user('admin', hash_password('CHANGE-ME-NOW'), role='admin', display_name='Admin')
print('admin created')
"
```
Container names are `<folder>-app-1` / `<folder>-db-1`. Add more users later from
**Admin → Users**.

### 6. Log in and verify

```bash
curl -s http://localhost:5011/health   # -> {"status":"ok", ... "institution":"ACME Ltd"}
```
Open `http://<host>:5011` and log in as `admin`.

### What happens automatically (no action needed)

With PDFs in `invoices/`, the worker scans on boot and every ~60s (idempotent) and, for
each new PDF: **extracts** (vision + guided JSON) → applies **rules** → **de-dups**
(per supplier, by invoice number or content hash) → **stores** → **discovers the
categories from the data** with
the local model on first run → **categorises** every line item against that vocabulary →
**reconciles** line-item sums against invoice totals (flagging mismatches). You can also
force a pass from **Admin → Dashboard → "Scan & process invoices now"** and re-derive the
taxonomy with **"Re-derive from data"**.

### Optional tuning (Admin → Dashboard, no rebuild)

Visible **tabs**, **currency**, **price-tracker grouping**, the **discovered categories**
(editable), and optional free-text **categorisation guidance** — all per customer.

### Day-2 operations

```bash
cd /mnt/nvme8tb/customers/acme
docker compose logs -f app     # watch processing
docker compose stop | start    # pause / resume
docker compose down            # remove containers (data in ./pgdata survives)
docker compose up -d           # after an image rebuild, recreates on the new epiproc:3
```
Everything customer-specific lives in `/mnt/nvme8tb/customers/acme/` — back up that
folder (especially `pgdata/` and `invoices/`) and the customer is backed up.

---

## Backup & restore

Two independent things to protect — they are backed up in different places:

| What | Where it's backed up |
|------|----------------------|
| **Engine code** | GitHub (`origin/master`) — full history is the backup; `docker build` recreates a working image anytime |
| **Engine image** (exact, tested) | Local tarball under `/mnt/nvme8tb/backups/` (kept off any registry) |


### Restore the engine image (offline, no rebuild)

The exact tested image is saved as a gzipped `docker save` tarball, named by version +
the git commit it was built from:

```
/mnt/nvme8tb/backups/epiproc-<version>-<commit>.tar.gz      # e.g. epiproc-3.0.0-9c5eabc.tar.gz
/mnt/nvme8tb/backups/epiproc-<version>-<commit>.README.txt  # provenance + these steps
```

To restore it:

```bash
docker load < /mnt/nvme8tb/backups/epiproc-3.0.0-9c5eabc.tar.gz
docker tag epiproc:3.0.0-9c5eabc epiproc:3    # restore the plain :3 tag the compose files use
```

Then bring any customer back up with `docker compose up -d` as usual.

> Rebuilding from source instead (`docker build -t epiproc:3 -f docker/Dockerfile .`) is
> functional but **not byte-identical** — `pyproject.toml` uses version ranges and the base
> image floats — so the saved tarball is the way to restore the exact image.

### Create a new image backup (after a rebuild worth keeping)

```bash
cd /mnt/nvme8tb/epiproc_v3
COMMIT=$(git rev-parse --short HEAD)
docker tag epiproc:3 "epiproc:3.0.0-${COMMIT}"
docker save "epiproc:3.0.0-${COMMIT}" | gzip > "/mnt/nvme8tb/backups/epiproc-3.0.0-${COMMIT}.tar.gz"
```

---

## Tests

A focused pytest suite lives in `tests/` (the extractor's `pymupdf` dependency is
only needed for the behavioural ingest cases; they self-skip where it is absent):

```bash
pip install -e ".[test]"
pytest tests/
ruff check .
```

Coverage is deliberately narrow — the script-context escaping of extracted text
(`_js_json`) and the per-supplier de-duplication (a shared invoice number or
filename across two suppliers must not drop the second supplier's spend).

---

