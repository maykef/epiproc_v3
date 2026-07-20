# legacy/ — v1 originals, not part of the running engine

These files are the EpiProc **v1** implementations, kept for reference. They do
**not** import or run in v3 and are excluded from the engine package (they live
outside `epiproc/`):

| File | What it is | Why it can't run here |
|------|------------|-----------------------|
| `dedup_v1_sqlite.py` | v1 duplicate detection | Uses `sqlite3`; v3 is Postgres |
| `verify_v1_sqlite.py` | v1 arithmetic checks (C0–C5) | Uses `sqlite3` and `from pipeline.config import SupplierConfig`, a module that doesn't exist in v3 |

### What v3 does instead
- **Dedup** — `epiproc/ingest/pipeline.py` skips an invoice whose `invoice_number`
  is already stored, and `epiproc/ingest/scan.py` skips byte-identical files (content
  hash) and anything already recorded in the `ingested_files` ledger.
- **Verify** — `pipeline._verify()` does a light, non-blocking sanity check and sets
  the row `status`. Porting the full C0–C5 checks from `verify_v1_sqlite.py` to
  Postgres is the outstanding follow-up.
