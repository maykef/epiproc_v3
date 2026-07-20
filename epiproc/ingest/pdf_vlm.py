"""PDF -> structured invoice record via per-page vision extraction + guided JSON.

Proven shape ported verbatim from v1 (invoice_extraction_v9.py):
  unwrap embedded PDF -> render each page -> page 1 (extraction_prompt) +
  pages 2+ (continuation_prompt) -> merge by position+article/description.

The one change vs v1: output is constrained by the model via
`response_format: json_schema` (schema in schema.py), so it is valid JSON by
construction. v1's find-'{' + 3-pass regex repair is gone; a page can no longer
produce malformed JSON. `enable_thinking=false` is required or Qwen3 returns
empty content (the reasoning parser strips it).
"""
from __future__ import annotations

import base64
import tempfile
import time
from dataclasses import dataclass, field
from io import BytesIO
from pathlib import Path

import fitz  # pymupdf
from openai import APIConnectionError, OpenAI
from PIL import Image

from epiproc.ingest.schema import response_format
from epiproc.suppliers import SupplierConfig

_EMBED_SENTINEL = "No valid PDF pages found"


@dataclass
class ExtractResult:
    data: dict | None
    error: str | None
    n_pages: int
    raw_page1: str
    log: list[str] = field(default_factory=list)


# ── PDF -> images (ported from v1) ───────────────────────────────────────────
def _unwrap_embedded_pdf(pdf_path: Path) -> Path:
    """SoftCo wrapper: if page 1 is the sentinel, extract the embedded .pdf."""
    doc = fitz.open(str(pdf_path))
    try:
        page_text = doc[0].get_text() if len(doc) else ""
        if _EMBED_SENTINEL not in page_text:
            return pdf_path
        for i in range(doc.embfile_count()):
            info = doc.embfile_info(i)
            if info.get("filename", "").lower().endswith(".pdf"):
                tmp = tempfile.NamedTemporaryFile(
                    suffix=".pdf", prefix=f"unwrap_{pdf_path.stem}_",
                    dir=pdf_path.parent, delete=False)
                tmp.write(doc.embfile_get(i))
                tmp.close()
                return Path(tmp.name)
    finally:
        doc.close()
    return pdf_path


def _pdf_to_images(pdf_path: Path, dpi: int) -> list[Image.Image]:
    doc = fitz.open(str(pdf_path))
    try:
        out = []
        for page in doc:
            pix = page.get_pixmap(dpi=dpi)
            out.append(Image.frombytes("RGB", [pix.width, pix.height], pix.samples))
        return out
    finally:
        doc.close()


def _image_to_b64(img: Image.Image) -> str:
    buf = BytesIO()
    img.save(buf, format="JPEG", quality=85)
    return base64.b64encode(buf.getvalue()).decode()


# ── VLM call (guided JSON) ───────────────────────────────────────────────────
def _run_page(client: OpenAI, model: str, image: Image.Image, prompt: str, max_tokens: int) -> dict:
    import json
    b64 = _image_to_b64(image)
    last = None
    mt = max_tokens
    for _ in range(4):
        try:
            r = client.chat.completions.create(
                model=model,
                messages=[{"role": "user", "content": [
                    {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{b64}"}},
                    {"type": "text", "text": prompt},
                ]}],
                max_tokens=mt, temperature=0.0,
                response_format=response_format(),
                extra_body={"top_k": 1, "chat_template_kwargs": {"enable_thinking": False}},
            )
            choice = r.choices[0]
            # A length-truncated response is NOT valid JSON however "guided" it was;
            # parsing it silently drops line items. Grow the budget and retry.
            if choice.finish_reason == "length":
                last = RuntimeError(f"response truncated at {mt} tokens")
                mt = min(mt * 2, 16384)
                continue
            return json.loads(choice.message.content)
        except APIConnectionError as e:      # vLLM unreachable — transient, back off
            last = e
            time.sleep(2)
        except Exception as e:               # 500 / timeout / malformed JSON — retry a few times
            last = e
            time.sleep(1)
    raise RuntimeError(f"page extraction failed after retries: {last}")


# ── merge continuation pages (ported from v1 — the good matching logic) ───────
def _merge_continuation(data: dict, cont: dict) -> None:
    extra = cont.get("line_items") or []
    if extra:
        existing = data.get("line_items") or []
        by_pos = {str(it["position"]): it for it in existing
                  if isinstance(it, dict) and it.get("position") is not None}
        new = []
        for ci in extra:
            if not isinstance(ci, dict):
                continue
            pos = ci.get("position")
            matched = False
            if pos is not None and str(pos) in by_pos:
                ei = by_pos[str(pos)]
                if (ci.get("article") and ci["article"] == ei.get("article")) or \
                   (ci.get("description") and ci["description"] == ei.get("description")):
                    for k, v in ci.items():
                        if v is not None and (k in ("unit_price", "total_price") or ei.get(k) is None):
                            ei[k] = v
                    matched = True
            if not matched:
                new.append(ci)
        data["line_items"] = existing + new

    for section in ("totals",):
        cs = cont.get(section) or {}
        if isinstance(cs, dict):
            data.setdefault(section, {})
            for k, v in cs.items():
                if v is not None:
                    data[section][k] = v
    for key in ("payment_terms", "notes"):
        if cont.get(key) is not None:
            data[key] = cont[key]


# ── orchestration ────────────────────────────────────────────────────────────
def extract_invoice(pdf_path: Path, cfg: SupplierConfig, client: OpenAI, model: str) -> ExtractResult:
    log: list[str] = []
    tmp: Path | None = None
    try:
        actual = _unwrap_embedded_pdf(pdf_path)
        if actual != pdf_path:
            tmp = actual
            log.append(f"unwrapped embedded PDF -> {actual.name}")
        images = _pdf_to_images(actual, cfg.pdf_dpi)
        log.append(f"{len(images)} page(s) @ {cfg.pdf_dpi}dpi")

        data = _run_page(client, model, images[0], cfg.extraction_prompt, cfg.max_tokens)
        raw1 = str(data)
        log.append(f"page 1: {len(data.get('line_items') or [])} item(s)")

        for n, img in enumerate(images[1:], start=2):
            cont = _run_page(client, model, img, cfg.continuation_prompt, cfg.max_tokens)
            _merge_continuation(data, cont)
            log.append(f"page {n}: +{len(cont.get('line_items') or [])} item(s)")

        for img in images:
            img.close()
        return ExtractResult(data=data, error=None, n_pages=len(images), raw_page1=raw1, log=log)
    except Exception as e:  # noqa: BLE001
        return ExtractResult(data=None, error=str(e), n_pages=0, raw_page1="", log=log)
    finally:
        if tmp and tmp.exists():
            tmp.unlink(missing_ok=True)
