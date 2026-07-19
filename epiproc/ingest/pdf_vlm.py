"""PDF -> structured invoice record, via per-page vision extraction.

THE ONE TRUE REWRITE (vs v1's invoice_extraction_v9.py):
  - Same proven shape: unwrap embedded PDF -> render each page to an image ->
    page 1 with extraction_prompt, pages 2+ with continuation_prompt -> merge.
  - DIFFERENCE: output is constrained by the model, not scraped from free text.
    v1 did find-first-'{' + 3-pass regex repair; a malformed page became an
    error row. Here the request carries the JSON schema so the model can only
    emit conformant JSON. Malformed JSON stops being a failure mode.

  Probe finding (2026-07-19): on the live vLLM 0.23.0 build, the legacy
  `guided_json` extra field was SILENTLY IGNORED (model returned off-schema keys
  + code fences). The `response_format: {type: json_schema, strict: true}` path
  is the one to wire and verify here before this leaves stub state.
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass
class ExtractResult:
    data: dict | None
    error: str | None
    n_pages: int
    raw_page1: str


def extract_invoice(pdf_path, cfg, client) -> ExtractResult:  # noqa: ANN001
    """Render pages, extract page 1 (full) + pages 2+ (continuation), merge.

    STUB. Build order:
      1. Port _unwrap_embedded_pdf + _pdf_to_images (verbatim, they work).
      2. Port _merge_continuation (position+article/description match — it's good).
      3. Replace parse_json_response with response_format=json_schema requests.
      4. Return the merged dict; corrections happen in rules.py, not here.
    """
    raise NotImplementedError("pdf_vlm.extract_invoice — P2 build")
