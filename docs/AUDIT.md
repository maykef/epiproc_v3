# EpiProc audit — what v3 keeps, rewrites, drops

Grounded in a full read of epiproc_v1 (code traced, not summarised) + live-state
checks, 2026-07-19. Verdicts drive what this repo copies vs rebuilds.

## KEEP — works well, copy-and-own
| Subsystem | Why |
|---|---|
| DB pool + job queue (`FOR UPDATE SKIP LOCKED`, stuck-job requeue, GPU=1/API=2 semaphores) | Correct concurrency, survives restarts. The orchestration core. |
| Auth/security plane (argon2id, server-side signed-UUID sessions, CSRF, TOTP MFA, rate limits, security headers) | Complete, tested, Cyber-Essentials-aligned. |
| Deterministic checks `checks.py` C0–C5 | The reconciliation "brain"; encodes credit-note/KMAT/tolerance knowledge. |
| Department normalisation | Single source of truth. |
| Dedup (two-pass: MIME strip → page-1 VLM key probe) | Clever, works. |
| Report agentic loop (`generate_v2` tool-use + Qwen hardening) | Hard-won; pruning/budget/`/no_think` fixes. |
| Config library (18 supplier YAMLs) | Crown-jewel asset; pure data. |

## REWRITE — right idea, wrong implementation
| Subsystem | Problem → v3 |
|---|---|
| Extraction (`invoice_extraction_v9.py`, 2,610 lines) | ~28 hardcoded `if supplier==...` correction branches; JSON scraped from free text with 3-pass regex repair (malformed page → error row). → per-page vision + **guided JSON** (response_format json_schema) so malformed JSON can't happen; corrections as **declarative rules**. |
| Corrections | Imperative Python doing SQL UPDATEs on inserted rows, re-querying SUM() between steps; generics run on every supplier and can mis-fire. → `rules.py` ops as data, opt-in per supplier. |
| Three data stores (PG dashboard / SQLite reports / in-memory FTS) | No source of truth; reports/dashboard can disagree; search goes stale. → one Postgres per container; PG FTS. |
| Duplicate paths | 2 report backends + ~7 dead generators; 3–4 vLLM launchers; ~15 tangled EPIPROC_* vars. → one of each; one settings object. |

## DROP — dead/broken/redundant
- Prefect pipeline (vestigial; docker-in-docker; its dashboard leg shells a
  script absent from the repo → already broken).
- Streamlit frontend + `users.yml` auth; unauthenticated JSON data routers.
- `inherits:` config system (used by 0 suppliers); `--from-list` dry-run flag
  (extractor doesn't accept it → onboarding dry-run gate broken).
- vLLM PID manager vs systemd drift; schema-per-tenant machinery.

## Must reconstruct (not in v1 or v2)
- **Categorisation engine** — only in retired `prefect_invoice_extraction/
  scripts/generate_dashboard_v5.py` (Phase 1). Port into `ingest/categorise.py`.

## Live-state notes (2026-07-19)
- vLLM 0.23.0 serving `qwen3.5-122b` (A10B, max_model_len 131072). Probe: legacy
  `guided_json` extra field **silently ignored** — use `response_format:
  {type: json_schema, strict: true}` and verify before extraction leaves stub.
- Qwen3 emits empty `content` unless thinking is disabled — set
  `chat_template_kwargs={"enable_thinking": false}` for extraction calls.
