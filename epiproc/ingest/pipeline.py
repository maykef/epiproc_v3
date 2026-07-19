"""Orchestrator: chain the ingest stages for one PDF and one batch.

This is the single place the pipeline order lives (v1 spread it across the
Prefect flow + the extractor's main()). No Prefect, no docker-in-docker — this
runs in-process in the worker.
"""
from __future__ import annotations

import pathlib

from epiproc.ingest import categorise, pdf_vlm, rules


def process_pdf(pdf_path: pathlib.Path, cfg, client, conn) -> dict:  # noqa: ANN001
    """dedup (caller) -> extract -> rules -> insert -> verify -> categorise.

    STUB wiring: shows the intended call graph so the worker has a contract to
    call. Each referenced stage is built in P2/P3.
    """
    res = pdf_vlm.extract_invoice(pdf_path, cfg, client)
    if res.error or res.data is None:
        return {"filename": pdf_path.name, "status": "error", "error": res.error}
    record, notes = rules.apply_rules(res.data, cfg)
    # insert record + items -> invoices/invoice_items  (P2)
    # verify (checks.py C0-C5)                          (port)
    # categorise.categorise_invoice(...)                (P3)
    return {"filename": pdf_path.name, "status": "extracted", "corrections": notes}


def process_folder(supplier: str, cfg, client, conn, progress_cb=None) -> dict:  # noqa: ANN001
    """Scan <data_dir>/invoices/<supplier>/*.pdf and process each. STUB."""
    raise NotImplementedError("process_folder — P2 build")
