"""Defect #2 — dedup must be per-supplier, not global.

Two failure modes, both of which silently drop real spend:
  a) scan.py: filename-only match — two suppliers each attach "invoice.pdf".
  b) pipeline.py: invoice_number-only match — two suppliers each use "INV-001".

The behavioural test drives the real ``pipeline.process_pdf`` against a fake
connection (skipped where the extractor's native ``fitz`` dependency is absent,
e.g. a bare dev box; it runs in the container image). The source guards always
run and pin the queries to their supplier-scoped form so a future edit can't
quietly revert to a global match.
"""
from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

REPO = Path(__file__).resolve().parent.parent


# ── Source guards (no imports, always runnable) ──────────────────────────────

def test_scan_filename_dedup_is_scoped_by_supplier():
    src = (REPO / "epiproc" / "ingest" / "scan.py").read_text(encoding="utf-8")
    assert "WHERE filename = %s AND supplier = %s" in src
    # The old global form must be gone.
    assert 'SELECT 1 FROM invoices WHERE filename = %s"' not in src


def test_pipeline_invoice_number_dedup_is_scoped_by_supplier():
    src = (REPO / "epiproc" / "ingest" / "pipeline.py").read_text(encoding="utf-8")
    assert "AND supplier = %s" in src
    # supplier must be passed as a query parameter, not just mentioned in a comment.
    assert "(invoice_number, supplier)" in src


# ── Behavioural test of the real pipeline dedup query ────────────────────────

class _Cursor:
    def __init__(self, row):
        self._row = row

    def fetchone(self):
        return self._row


class _FakeConn:
    """Answers only the invoice_number dedup SELECT from process_pdf.

    Unpacking ``params`` as ``(invoice_number, supplier)`` is deliberate: if the
    query ever regresses to a global, single-parameter form, this raises and the
    test fails loudly instead of silently passing.
    """

    def __init__(self, rows):
        self.rows = rows  # list of {"supplier","filename","invoice_number"}

    def execute(self, sql, params=()):
        norm = " ".join(sql.split())
        if norm.startswith("SELECT supplier, filename FROM invoices"):
            invoice_number, supplier = params
            for r in self.rows:
                if r["invoice_number"] == invoice_number and r["supplier"] == supplier:
                    return _Cursor(r)
            return _Cursor(None)
        return _Cursor(None)

    def commit(self):
        pass


def _load_pipeline():
    pytest.importorskip("fitz", reason="pdf_vlm needs pymupdf; runs in the image")
    from epiproc.ingest import pipeline
    return pipeline


def _patch(monkeypatch, pipeline, invoice_number):
    monkeypatch.setattr(
        pipeline.pdf_vlm, "extract_invoice",
        lambda *a, **k: SimpleNamespace(error=None, data={"raw": True}),
    )
    monkeypatch.setattr(
        pipeline.rules, "apply_rules",
        lambda data, cfg: (
            {"invoice_number": invoice_number,
             "seller": {"name": "Acme"},
             "line_items": [{"description": "x", "total_price": 1.0}],
             "totals": {"total": 1.0}},
            [],
        ),
    )
    monkeypatch.setattr(pipeline, "insert_record", lambda *a, **k: 42)


def test_same_invoice_number_different_supplier_is_not_duplicate(monkeypatch):
    pipeline = _load_pipeline()
    _patch(monkeypatch, pipeline, "INV-001")
    conn = _FakeConn([{"supplier": "supplier_a", "filename": "a.pdf",
                       "invoice_number": "INV-001"}])

    res = pipeline.process_pdf(Path("b.pdf"), cfg=None, client=None, conn=conn,
                               model="test", supplier_hint="supplier_b")

    assert res["status"] == "ingested"
    assert res["supplier"] == "supplier_b"


def test_same_invoice_number_same_supplier_is_duplicate(monkeypatch):
    pipeline = _load_pipeline()
    _patch(monkeypatch, pipeline, "INV-001")
    conn = _FakeConn([{"supplier": "supplier_a", "filename": "a.pdf",
                       "invoice_number": "INV-001"}])

    res = pipeline.process_pdf(Path("a_copy.pdf"), cfg=None, client=None, conn=conn,
                               model="test", supplier_hint="supplier_a")

    assert res["status"] == "duplicate"
    assert res["of"] == "a.pdf"
