"""Folder scanner — the turn-key entry point.

Walks <data_dir>/invoices/ and, for every PDF it has not already handled, runs the
ingest pipeline (extract -> rules -> dedup -> store) and then categorises the newly
stored rows in one batch. Idempotent via the `ingested_files` ledger: unchanged
files are skipped without touching the GPU, and byte-identical copies are dropped
by content hash. This is what makes "drop PDFs in, dashboard fills itself" work.

Supplier is taken from the sub-folder name when a PDF lives in a per-supplier
folder; for a generic drop-box (invoices/inbox/…) it is derived from the extracted
seller name, which reproduces the same slug the existing data uses.
"""
from __future__ import annotations

import hashlib
import pathlib

from openai import OpenAI

from epiproc.db import ingested
from epiproc.db.pool import pool
from epiproc.ingest import pipeline
from epiproc.ingest.categorise import categorise_all
from epiproc.settings import settings
from epiproc.suppliers import load_config

# Sub-folders that are a generic inbox, not a supplier name.
_DROPBOX = {"inbox", "imports", "incoming", "unsorted", "invoices", ""}
_MAX_ATTEMPTS = 3      # retry a failing file this many times before giving up (still surfaced)


def _iter_pdfs(root: pathlib.Path):
    for p in sorted(root.rglob("*.pdf")):
        if not p.name.startswith("."):
            yield p


def _sha256(path: pathlib.Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _supplier_from_path(pdf: pathlib.Path, root: pathlib.Path) -> str | None:
    if pdf.parent == root:
        return None
    parent = pdf.parent.name
    return None if parent.lower() in _DROPBOX else parent


def scan_and_process(progress=None) -> dict:  # noqa: ANN001
    """Process new PDFs under invoices/ and categorise new rows. Returns counts."""
    root = pathlib.Path(settings.data_dir) / "invoices"
    counts = {"scanned": 0, "ingested": 0, "duplicate": 0, "error": 0, "skipped": 0}
    if not root.exists():
        return counts

    client = OpenAI(base_url=settings.vllm_url, api_key="none")
    new_rows = 0

    def _say(msg: str) -> None:
        if progress:
            progress(msg)

    with pool().connection() as conn:
        for pdf in _iter_pdfs(root):
            counts["scanned"] += 1
            path = str(pdf)
            st = pdf.stat()

            led = ingested.get(conn, path)
            # Skip a file we've already handled, UNLESS it errored and still has
            # retries left — a failed invoice must not be lost on a matching mtime.
            if led and led["mtime"] == st.st_mtime and not (
                    led["result"] == "error" and (led.get("attempts") or 0) < _MAX_ATTEMPTS):
                counts["skipped"] += 1
                continue

            sha = _sha256(pdf)
            # Same content already ingested under another name. This stays GLOBAL:
            # identical bytes really are the same document, whoever sent it.
            if ingested.sha_ingested(conn, sha, exclude_path=path):
                ingested.record(conn, path, st.st_mtime, sha, None,
                                "duplicate", "same content as an already-ingested file")
                counts["duplicate"] += 1
                continue

            hint = _supplier_from_path(pdf, root)
            # Exact (supplier, filename) already an invoice (e.g. pre-existing
            # data). Scoped by supplier — two suppliers can each attach
            # "invoice.pdf", and dropping the second on filename alone silently
            # loses real spend. Matches the UNIQUE (supplier, filename) constraint.
            # When the supplier isn't known before extraction (generic inbox, hint
            # is None) we skip this shortcut and let the pipeline's per-supplier
            # invoice_number check and the (supplier, filename) upsert dedup.
            if hint and conn.execute(
                    "SELECT 1 FROM invoices WHERE filename = %s AND supplier = %s",
                    (pdf.name, hint)).fetchone():
                ingested.record(conn, path, st.st_mtime, sha, None,
                                "duplicate", "filename already present in invoices")
                counts["duplicate"] += 1
                continue

            cfg = load_config(hint or "_generic")
            res = pipeline.process_pdf(pdf, cfg, client, conn, supplier_hint=hint)
            status = res.get("status")

            if status == "ingested":
                ingested.record(conn, path, st.st_mtime, sha,
                                res.get("invoice_id"), "ingested", None)
                counts["ingested"] += 1
                new_rows += 1
            elif status == "duplicate":
                ingested.record(conn, path, st.st_mtime, sha, None, "duplicate",
                                f"invoice_number {res.get('invoice_number')} "
                                f"already present (of {res.get('of')})")
                counts["duplicate"] += 1
            else:  # error
                err = (res.get("error") or "").strip()
                # Connectivity errors are transient — leave them unledgered so a
                # later scan retries indefinitely once the GPU server is back.
                if any(w in err.lower() for w in ("unreachable", "connection", "timeout", "timed out")):
                    counts["error"] += 1
                    _say(f"{pdf.name}: transient extract error, will retry")
                    continue
                # Non-transient: ledger with an incremented attempt count. It keeps
                # retrying (up to _MAX_ATTEMPTS) and is surfaced as an ingest failure.
                prev = led["attempts"] if (led and led["mtime"] == st.st_mtime) else 0
                ingested.record(conn, path, st.st_mtime, sha, None, "error", err[:500],
                                attempts=prev + 1)
                counts["error"] += 1
            _say(f"{pdf.name}: {status}")

    if new_rows:
        _say(f"categorising {new_rows} new invoice(s)…")
        categorise_all(only_uncategorised=True, progress=progress)
    return counts
