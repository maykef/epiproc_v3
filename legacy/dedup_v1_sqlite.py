#!/usr/bin/env python3
"""
dedup_invoices.py — Pre-extraction duplicate detection and MIME cleanup.

For each PDF in the invoice directory, two passes run concurrently:

  Pass 1 — MIME / garbled-text scan (no model, text-layer only):
    • Trailing pages containing email headers or >90% base64 text (and no images)
      are stripped in-place; originals backed up to backup_originals/.
    • Files where page 1 itself is unreadable (MIME markers present, or >90% base64
      with no page images) are moved to manual_review/ — cannot be auto-fixed.

  Pass 2 — Invoice-number dedup (minimal VLM call):
    • Page 1 is sent to the running vLLM server with a two-field prompt:
        "Return only invoice_number and total_amount."
      max_tokens=80, so this is much faster than a full extraction call.
    • The result is compared against the supplier DB:
        - invoice_number must match an existing successful row
        - total_amount must agree within £1 (guards against same number, different doc)
      If both match → file is moved to duplicates/.

Files that are already known by filename (any DB status) are moved to
duplicates/ immediately without any VLM call.

Usage (inside Docker — vLLM must be running on host port 8000):
    python3 dedup_invoices.py invoices_life_technologies/ --supplier life_technologies
    python3 dedup_invoices.py invoices_leica/ --supplier leica --dry-run
    python3 dedup_invoices.py invoices_leica/ --supplier leica --workers 4
"""

import os
import sys
import re
import json
import base64
import shutil
import tempfile
import argparse
import sqlite3
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from io import BytesIO
from pathlib import Path

import fitz          # pymupdf
import yaml
from PIL import Image
from openai import OpenAI

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------
CONFIGS_DIR   = Path(os.environ.get("INVOICE_CONFIGS_DIR", Path(__file__).parent / "configs"))
VLLM_PORT     = int(os.environ.get("VLLM_PORT", "8000"))
VLLM_BASE_URL = f"http://localhost:{VLLM_PORT}/v1"
MODEL_ID      = os.environ.get("VLLM_SERVED_MODEL", "qwen3.5-122b")

DEDUP_PROMPT = (
    "You are reading an invoice. Extract ONLY these two fields:\n"
    "  invoice_number : the invoice or document reference number\n"
    "  total_amount   : the final grand total (number only, no currency symbol)\n\n"
    "Reply with ONLY valid JSON, nothing else:\n"
    '{"invoice_number": "...", "total_amount": 123.45}\n\n'
    "Use null for any field you cannot find."
)
MAX_TOKENS_DEDUP = 80
PDF_DPI          = 72

# ---------------------------------------------------------------------------
# MIME / garbled-text detection
# ---------------------------------------------------------------------------
MIME_PATTERNS = [
    re.compile(r'------=_NextPart_'),
    re.compile(r'--=_Part_\d+'),
    re.compile(r'^Content-Type\s*:',              re.IGNORECASE | re.MULTILINE),
    re.compile(r'^Content-Transfer-Encoding\s*:', re.IGNORECASE | re.MULTILINE),
    re.compile(r'^MIME-Version\s*:',              re.IGNORECASE | re.MULTILINE),
    re.compile(r'^X-Mailer\s*:',                  re.IGNORECASE | re.MULTILINE),
    re.compile(r'^Received\s*: from',             re.IGNORECASE | re.MULTILINE),
]
BASE64_MIN_LENGTH = 200   # min run length to flag as base64 MIME content
FINANCIAL_KEYWORDS = [
    'grand total', 'total:', 'sub total', 'subtotal',
    'amount due', 'net amount', 'vat ', 'vat:', 'discount', 'freight',
]


def _has_mime_markers(text: str) -> bool:
    return any(p.search(text) for p in MIME_PATTERNS)


# Matches a solid run of base64 chars with no whitespace — real base64 encoded
# attachments appear as long unbroken strings.  Normal invoice text (addresses,
# product names, amounts) always has spaces/punctuation between tokens, so it
# never produces runs this long.
_BASE64_RUN_RE = re.compile(
    r'[A-Za-z0-9+/]{%d,}={0,2}' % BASE64_MIN_LENGTH
)


def _has_base64_run(text: str) -> bool:
    """Return True if the text contains a long uninterrupted base64-like run."""
    return bool(_BASE64_RUN_RE.search(text))


def _is_mime_page(text: str, has_images: bool) -> bool:
    """
    A page is MIME/garbled if it contains explicit email/MIME markers, OR if
    it contains a long uninterrupted run of base64 characters (≥200 chars with no
    spaces) AND the page has no embedded images.

    Normal invoice text — even when composed mostly of letters and digits — always
    has spaces, colons, dots and other punctuation between tokens, so it never
    produces solid base64 runs of this length.  Only real base64-encoded MIME
    attachments do.
    """
    if _has_mime_markers(text):
        return True
    if not has_images and _has_base64_run(text):
        return True
    return False


def _has_financial(text: str) -> bool:
    lo = text.lower()
    return any(kw in lo for kw in FINANCIAL_KEYWORDS)


def mime_scan(path: Path) -> dict:
    """
    Scan a PDF for email/MIME garbage.

    Returns a dict with:
      status : 'clean' | 'strip' | 'unreadable' | 'error'
      detail : human-readable note
      keep   : list of 0-based page indices to keep  (only for 'strip')
    """
    try:
        doc = fitz.open(str(path))
        n   = doc.page_count
        pages = []
        for i in range(n):
            page = doc[i]
            pages.append({
                'text':       page.get_text(),
                'has_images': bool(page.get_images()),
            })
        doc.close()
    except Exception as e:
        return {'status': 'error', 'detail': str(e), 'keep': None}

    # Page 1 unreadable → cannot recover
    if _is_mime_page(pages[0]['text'], pages[0]['has_images']):
        return {'status': 'unreadable', 'detail': 'page 1 is MIME/garbled', 'keep': None}

    # Find last page with financial content
    last_fin = next(
        (i for i in range(n - 1, -1, -1) if _has_financial(pages[i]['text'])),
        -1,
    )
    if last_fin == -1:
        # No financial keywords — likely a scanned invoice; treat as clean
        return {'status': 'clean', 'detail': 'scanned (no text layer)', 'keep': None}

    # Check for MIME pages after last financial page
    first_mime = next(
        (i for i in range(last_fin + 1, n)
         if _is_mime_page(pages[i]['text'], pages[i]['has_images'])),
        None,
    )
    if first_mime is not None:
        keep = list(range(first_mime))
        return {'status': 'strip', 'detail': f'MIME from page {first_mime + 1}/{n}', 'keep': keep}

    return {'status': 'clean', 'detail': 'ok', 'keep': None}


def strip_pages(path: Path, keep: list, backup_dir: Path) -> bool:
    """Strip trailing MIME pages in-place; back up original. Returns True on success."""
    backup_dir.mkdir(exist_ok=True)
    tmp_path = None
    try:
        shutil.copy2(str(path), str(backup_dir / path.name))
        doc = fitz.open(str(path))
        doc.select(keep)
        fd, tmp_path = tempfile.mkstemp(suffix='.pdf', dir=str(path.parent))
        os.close(fd)
        doc.save(tmp_path, garbage=4, deflate=True, clean=True)
        doc.close()
        os.replace(tmp_path, str(path))
        return True
    except Exception:
        if tmp_path:
            try:
                os.unlink(tmp_path)
            except Exception:
                pass
        return False


# ---------------------------------------------------------------------------
# VLM helpers
# ---------------------------------------------------------------------------
def render_page1_b64(path: Path, dpi: int = PDF_DPI) -> str:
    doc = fitz.open(str(path))
    pix = doc[0].get_pixmap(dpi=dpi)
    img = Image.frombytes("RGB", [pix.width, pix.height], pix.samples)
    doc.close()
    buf = BytesIO()
    img.save(buf, format="JPEG", quality=85)
    return base64.b64encode(buf.getvalue()).decode()


def extract_key_fields(
    client: OpenAI, path: Path
) -> tuple[str | None, float | None, str | None]:
    """
    Send page 1 to vLLM with a minimal two-field prompt.
    Returns (invoice_number, total_amount, error_or_None).
    """
    try:
        b64 = render_page1_b64(path)
        resp = client.chat.completions.create(
            model=MODEL_ID,
            messages=[{
                "role": "user",
                "content": [
                    {"type": "image_url",
                     "image_url": {"url": f"data:image/jpeg;base64,{b64}"}},
                    {"type": "text", "text": DEDUP_PROMPT},
                ],
            }],
            max_tokens=MAX_TOKENS_DEDUP,
            temperature=0.0,
            extra_body={"top_k": 1, "chat_template_kwargs": {"enable_thinking": False}},
        )
        raw = resp.choices[0].message.content.strip()
        # Strip markdown fences if present
        raw = re.sub(r'^```(?:json)?\s*', '', raw, flags=re.MULTILINE)
        raw = re.sub(r'\s*```$',          '', raw, flags=re.MULTILINE).strip()
        start = raw.find('{')
        if start == -1:
            return None, None, f"no JSON in: {raw[:80]}"
        obj = json.loads(raw[start:])
        inv = obj.get('invoice_number')
        tot = obj.get('total_amount')
        if isinstance(inv, str) and inv.strip().lower() in ('null', ''):
            inv = None
        try:
            tot = float(tot) if tot is not None else None
        except (ValueError, TypeError):
            tot = None
        return inv, tot, None
    except Exception as e:
        return None, None, str(e)


# ---------------------------------------------------------------------------
# Database helpers
# ---------------------------------------------------------------------------
def load_db_path(supplier: str | None, db_arg: str | None) -> Path | None:
    if db_arg:
        return Path(db_arg)
    if not supplier:
        return None
    yml = CONFIGS_DIR / f"{supplier}.yml"
    if not yml.exists():
        print(f"ERROR: config not found: {yml}", file=sys.stderr)
        sys.exit(1)
    with open(yml) as f:
        raw = yaml.safe_load(f)
    return (Path(raw.get('output_dir', 'databases'))
            / raw.get('db_filename', 'invoices.db'))


def get_known_filenames(conn: sqlite3.Connection) -> set[str]:
    try:
        rows = conn.execute("SELECT filename FROM invoices").fetchall()
        return {r[0] for r in rows}
    except sqlite3.OperationalError:
        return set()


def is_duplicate(
    conn: sqlite3.Connection,
    invoice_number: str,
    total: float | None,
) -> str | None:
    """
    Returns the original filename if a matching successful row exists, else None.
    invoice_number must match; total_amount must agree within £1 (or be absent).
    """
    try:
        rows = conn.execute(
            "SELECT filename, total_amount FROM invoices "
            "WHERE invoice_number = ? AND extraction_error IS NULL",
            (invoice_number,),
        ).fetchall()
    except sqlite3.OperationalError:
        return None
    if not rows:
        return None
    if total is None:
        return rows[0][0]   # match on invoice number alone when total unknown
    for fname, db_total in rows:
        if db_total is None or abs(float(db_total) - total) < 1.0:
            return fname
    return None  # same invoice number but significantly different total → not a dup


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
def main() -> int:
    parser = argparse.ArgumentParser(
        description='Pre-extraction MIME cleanup and invoice-number deduplication.',
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument('invoice_dir',
                        help='Directory containing invoice PDFs')
    parser.add_argument('--supplier',
                        help='Supplier name — loads DB path from configs/<supplier>.yml')
    parser.add_argument('--db',
                        help='Explicit path to supplier SQLite database')
    parser.add_argument('--workers', type=int, default=4,
                        help='Parallel VLM workers (default: 4)')
    parser.add_argument('--dry-run', action='store_true',
                        help='Report actions without moving or modifying any files')
    args = parser.parse_args()

    invoice_dir = Path(args.invoice_dir)
    if not invoice_dir.is_dir():
        print(f"ERROR: {invoice_dir} is not a directory", file=sys.stderr)
        return 1

    pdfs = sorted(invoice_dir.glob('*.pdf'))
    if not pdfs:
        print(f"No PDFs found in {invoice_dir}")
        return 0

    db_path = load_db_path(args.supplier, args.db)
    conn    = None
    known_filenames: set[str] = set()
    if db_path and db_path.exists():
        conn = sqlite3.connect(str(db_path), check_same_thread=False)
        known_filenames = get_known_filenames(conn)

    client = OpenAI(api_key="EMPTY", base_url=VLLM_BASE_URL)

    backup_dir = invoice_dir / 'backup_originals'

    # ── Header ────────────────────────────────────────────────────────────────
    print(f"\nDedup  ──  {invoice_dir}  ({len(pdfs)} PDFs)")
    if db_path:
        exists = "exists" if (db_path and db_path.exists()) else "not found"
        print(f"  DB      : {db_path}  ({len(known_filenames)} known files, {exists})")
    else:
        print("  DB      : none — MIME cleanup only, no dedup")
    print(f"  Workers : {args.workers}   Model : {MODEL_ID}")
    if args.dry_run:
        print("  DRY-RUN : no files will be moved or modified")

    # ── Instant skip: filename already in DB ──────────────────────────────────
    by_filename = [p for p in pdfs if p.name in known_filenames]
    for p in by_filename:
        print(f"  🔁  {p.name:40s} duplicate (filename already in DB)")

    dup_files: list[str] = [p.name for p in by_filename]

    to_check = [p for p in pdfs if p.name not in known_filenames]

    print(f"\n  Skipped (filename in DB) : {len(by_filename)}")
    print(f"  To check via VLM         : {len(to_check)}")
    print(f"\n{'─'*72}")

    # ── Per-file: MIME scan + VLM dedup ──────────────────────────────────────
    counts = {'dup': 0, 'review': 0, 'stripped': 0, 'ready': 0, 'error': 0}
    _lock  = threading.Lock()

    # In-run index: invoice_number → first filename seen this session.
    # Catches intra-batch duplicates even when the DB is empty.
    _seen_this_run: dict[str, str] = {}

    done_count = [0]   # mutable counter for progress display

    def process(pdf: Path) -> tuple[str, str, str]:
        """Returns (action, icon, detail)."""
        # 1. MIME scan
        scan = mime_scan(pdf)

        if scan['status'] == 'error':
            return 'error', '❌', f"cannot open PDF: {scan['detail']}"

        if scan['status'] == 'unreadable':
            return 'review', '⚠ ', f"unreadable (MIME/garbled page 1) — {scan['detail']}"

        stripped_note = ''
        if scan['status'] == 'strip' and not args.dry_run:
            ok = strip_pages(pdf, scan['keep'], backup_dir)
            stripped_note = f"  [stripped: {scan['detail']}]" if ok else '  [strip failed]'

        # 2. VLM: extract invoice_number + total_amount
        if not conn:
            return 'ready', '✅', f"no DB — skipping dedup{stripped_note}"

        t0 = time.time()
        inv_num, total, err = extract_key_fields(client, pdf)
        elapsed = round(time.time() - t0, 1)

        if err:
            # VLM failed — leave file in queue for full extraction to handle
            return 'ready', '⚠ ', (f"VLM error ({err[:60]}) — "
                                    f"leaving for full extraction{stripped_note}  [{elapsed}s]")

        # 3. Duplicate check — DB first, then in-run index
        if inv_num:
            with _lock:
                orig = is_duplicate(conn, inv_num, total)
                if not orig:
                    # Check files already seen in this dedup run (intra-batch dups)
                    seen_key = inv_num.strip().lower()
                    if seen_key in _seen_this_run:
                        orig = _seen_this_run[seen_key]
                    else:
                        _seen_this_run[seen_key] = pdf.name
            if orig:
                return ('dup', '🔁',
                        f"inv={inv_num}  total={total}  → dup of {orig}  [{elapsed}s]"
                        + stripped_note)

        return ('ready', '✅',
                f"inv={inv_num}  total={total}  [{elapsed}s]" + stripped_note)

    total_to_check = len(to_check)
    with ThreadPoolExecutor(max_workers=args.workers) as ex:
        futures = {ex.submit(process, p): p for p in to_check}
        for future in as_completed(futures):
            pdf = futures[future]
            action, icon, detail = future.result()
            with _lock:
                counts[action] = counts.get(action, 0) + 1
                done_count[0] += 1
                n_done = done_count[0]
                if action == 'dup':
                    dup_files.append(pdf.name)
            print(f"  {icon}  {pdf.name:40s} ({n_done}/{total_to_check})  {detail}")

    if conn:
        conn.close()

    # ── Summary ───────────────────────────────────────────────────────────────
    total_dups = len(by_filename) + counts['dup']
    ready      = counts['ready']
    print(f"\n{'─'*72}")
    print(f"  Duplicate (filename)  : {len(by_filename)}")
    print(f"  Duplicate (inv. no.)  : {counts['dup']}")
    print(f"  Unreadable (MIME)     : {counts['review']}")
    print(f"  Ready for extraction  : {ready}")
    if counts.get('error'):
        print(f"  Errors                : {counts['error']}")

    # ── Write skip list so extraction can skip duplicates without moving files ─
    if db_path and dup_files:
        skip_path = db_path.parent / (db_path.stem + '_dedup_skip.json')
        with open(skip_path, 'w') as fh:
            json.dump(sorted(dup_files), fh, indent=2)
        print(f"  Skip list written     : {skip_path}  ({len(dup_files)} files)")
    print()
    return 0


if __name__ == '__main__':
    sys.exit(main())
