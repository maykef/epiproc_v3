"""Orchestrator: chain the ingest stages for one PDF.

The single place the pipeline order lives (v1 spread it across the Prefect flow +
the extractor's main()). No Prefect, no docker-in-docker — this runs in-process in
the worker:

    extract -> rules -> dedup (by supplier + invoice_number) -> verify -> insert

Categorisation runs as a separate batch after a scan (see ingest/scan.py) so a
whole folder is classified in one pass rather than per file.
"""
from __future__ import annotations

import pathlib
import re

from epiproc.db.invoices import insert_record
from epiproc.ingest import pdf_vlm, rules
from epiproc.settings import settings

# Legal-form suffixes stripped so "Acme Trading B.V." and "Acme Trading" collapse
# to one supplier key instead of two. "b_v" is what "B.V." slugifies to.
_SUFFIX_RE = re.compile(
    r"_(b_v|bv|ltd|limited|plc|llc|inc|co|gmbh|ag|sa|nv|srl|oy|as|kg|spa)$")


def slug_supplier(name: str | None) -> str | None:
    """"W. Tuning Bloemenexport" -> "w_tuning_bloemenexport" (matches existing data)."""
    if not name:
        return None
    s = re.sub(r"[^a-z0-9]+", "_", name.lower()).strip("_")
    prev = None
    while s and s != prev:                 # strip stacked suffixes, e.g. "..._co_ltd"
        prev = s
        s = _SUFFIX_RE.sub("", s)
    return s or None


def _verify(record: dict) -> str:
    """Light sanity check -> row status. Full C0-C5 checks are a later port.

    'review' flags a row a human should look at (nothing extracted); everything
    else is 'extracted'. Non-blocking: the row is always stored.
    """
    items = record.get("line_items") or []
    total = (record.get("totals") or {}).get("total")
    if not items and total is None:
        return "review"
    return "extracted"


def process_pdf(pdf_path: pathlib.Path, cfg, client, conn,  # noqa: ANN001
                model: str | None = None, supplier_hint: str | None = None) -> dict:
    """Extract one PDF, correct it, dedup it, and store it.

    Returns a dict with status ∈ {ingested, duplicate, error}. `duplicate` means
    an invoice with the same invoice_number is already stored (a re-sent PDF, or
    the same document under a different filename) — nothing is inserted.
    """
    model = model or settings.vllm_model
    res = pdf_vlm.extract_invoice(pdf_path, cfg, client, model)
    if res.error or res.data is None:
        return {"filename": pdf_path.name, "status": "error", "error": res.error}

    record, notes = rules.apply_rules(res.data, cfg)
    seller = (record.get("seller") or {}).get("name")
    supplier = supplier_hint or slug_supplier(seller) or cfg.supplier or "unknown"
    invoice_number = record.get("invoice_number")

    # Dedup by invoice_number WITHIN a supplier: the same document can arrive as
    # "IN022490.pdf" and "IN022490 (1).pdf" — both carry invoice_number IN022490.
    # Scoping by supplier is essential: two different suppliers can legitimately
    # both number an invoice "INV-001", and a global match would silently drop the
    # second supplier's real spend.
    if invoice_number:
        dup = conn.execute(
            "SELECT supplier, filename FROM invoices "
            "WHERE invoice_number = %s AND invoice_number IS NOT NULL "
            "AND supplier = %s LIMIT 1",
            (invoice_number, supplier),
        ).fetchone()
        if dup and dup["filename"] != pdf_path.name:
            return {"filename": pdf_path.name, "status": "duplicate",
                    "invoice_number": invoice_number, "supplier": supplier,
                    "of": dup["filename"]}

    status = _verify(record)
    inv_id = insert_record(conn, supplier, pdf_path.name, record, notes, status=status,
                           path=str(pdf_path))
    return {"filename": pdf_path.name, "status": "ingested", "invoice_id": inv_id,
            "supplier": supplier, "invoice_number": invoice_number, "corrections": notes}
