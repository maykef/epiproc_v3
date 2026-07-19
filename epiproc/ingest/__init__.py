"""Ingest engine: dedup -> extract -> rules -> verify -> categorise.

Stage status (skeleton):
  dedup.py      COPIED from v1 (works; needs import rewiring to epiproc.*)
  pdf_vlm.py    STUB  — the one true rewrite: guided-JSON extraction
  rules.py      STUB  — declarative corrections engine (replaces correct_* branches)
  verify.py     COPIED from v1 checks.py (C0-C5; needs import rewiring)
  categorise.py STUB  — port from retired generate_dashboard_v5.py Phase 1
  pipeline.py   the orchestrator that chains the stages for one PDF / one batch
"""
