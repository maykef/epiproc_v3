"""Ingest engine: scan -> extract -> rules -> dedup -> store -> categorise.

Stages:
  pdf_vlm.py    guided-JSON per-page vision extraction
  rules.py      declarative corrections engine (replaces v1's correct_* branches)
  pipeline.py   orchestrator for ONE pdf: extract -> rules -> dedup -> verify -> store
  scan.py       folder scanner: walk invoices/, run new PDFs through the pipeline,
                then categorise new rows (the turn-key entry point)
  categorise.py two-level (category + variety) line-item classification

The original v1 dedup.py / verify.py (SQLite; import `pipeline.config`) live under
../../legacy/ — unrunnable as-is, kept for reference until the checks are ported.
Practical dedup now happens in pipeline.py (by invoice_number) and scan.py (by
content hash + the ingested_files ledger).
"""
