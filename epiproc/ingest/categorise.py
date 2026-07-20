"""Line-item categorisation via the local model — fully domain-agnostic.

Text-only (no vision): it classifies already-extracted line-item text, so it is
fast and cheap. The category vocabulary is DISCOVERED from the customer's own data
by the local model on first run (ingest/discover.py) and stored per instance — the
engine hardcodes no domain terms. That vocabulary is applied here as a strict
JSON-schema `enum`, so guided decoding constrains `category` to exactly those
values: the same product always lands under the same name (no "Rose"/"Roses"
drift). `variety` (the specific product/model/cultivar) is open free text.
"""
from __future__ import annotations

import json

from openai import OpenAI

from epiproc.db.pool import pool
from epiproc.db.settings import (
    OTHER_CATEGORY,
    get_categories,
    get_categorisation_scheme,
)
from epiproc.ingest.discover import ensure_categories
from epiproc.settings import settings


_BATCH = 25          # items per model call — keeps well inside the token budget
_TOK_PER_ITEM = 90   # generous per-item allowance for the JSON response


def _schema(categories: list[str]) -> dict:
    """Each object echoes the 1-based item 'index' so results map by index, never
    by position; category is enum-constrained; variety is free text."""
    cat_schema = {"type": "string", "enum": categories} if categories else {"type": "string"}
    return {"type": "json_schema", "json_schema": {
        "name": "classification", "strict": True,
        "schema": {
            "type": "object",
            "properties": {"items": {"type": "array", "items": {
                "type": "object",
                "properties": {"index": {"type": "integer"},
                               "category": cat_schema, "variety": {"type": "string"}},
                "required": ["index", "category", "variety"], "additionalProperties": False,
            }}},
            "required": ["items"], "additionalProperties": False,
        },
    }}


def _classify_batch(client: OpenAI, model: str, descriptions: list[str],
                    categories: list[str], scheme: str) -> list[tuple[str, str]]:
    """Classify one bounded batch. Maps by echoed index and VALIDATES coverage —
    raises on truncation or missing indices rather than silently misaligning."""
    n = len(descriptions)
    lines = "\n".join(f"{i + 1}. {d or '(no description)'}" for i, d in enumerate(descriptions))
    cat_list = ", ".join(categories) if categories else "(derive a short category name)"
    guidance = f"\nCustomer guidance: {scheme}\n" if scheme.strip() else ""
    prompt = (
        f"Classify each invoice line item below.{guidance}\n"
        f"For each of the {n} items return an object with:\n"
        f"- 'index': the item number shown (1 to {n}).\n"
        f"- 'category': assign EXACTLY ONE from this list: {cat_list}. Pick the single "
        f"most specific one that fits; if none fits, use '{OTHER_CATEGORY}'.\n"
        f"- 'variety': the specific product/model/grade/cultivar (free text); if none, "
        f"repeat the category.\n"
        f"Return an 'items' array of exactly {n} objects, one per index, none omitted."
        f"\n\nItems:\n{lines}"
    )
    r = client.chat.completions.create(
        model=model,
        messages=[{"role": "user", "content": prompt}],
        temperature=0.0, max_tokens=max(512, n * _TOK_PER_ITEM),
        response_format=_schema(categories),
        extra_body={"chat_template_kwargs": {"enable_thinking": False}},
    )
    choice = r.choices[0]
    if choice.finish_reason == "length":
        raise RuntimeError(f"categorisation truncated for a batch of {n} items")
    got = json.loads(choice.message.content).get("items", [])
    by_idx: dict[int, dict] = {}
    for g in got:
        idx = g.get("index")
        if isinstance(idx, int) and 1 <= idx <= n:
            by_idx[idx] = g
    if len(by_idx) != n:                      # missing/duplicate/extra -> do NOT guess alignment
        raise RuntimeError(f"categorisation returned {len(by_idx)}/{n} valid indices")
    out = []
    for i in range(1, n + 1):
        g = by_idx[i]
        cat = (g.get("category") or OTHER_CATEGORY).strip() or OTHER_CATEGORY
        var = (g.get("variety") or cat).strip() or cat
        out.append((cat, var))
    return out


def _categorise_descriptions(client: OpenAI, model: str, descriptions: list[str],
                             categories: list[str], scheme: str = "") -> list[tuple[str, str]]:
    """Classify all descriptions in bounded, index-validated batches."""
    out: list[tuple[str, str]] = []
    for start in range(0, len(descriptions), _BATCH):
        batch = descriptions[start:start + _BATCH]
        out.extend(_classify_batch(client, model, batch, categories, scheme))
    return out


def categorise_all(only_uncategorised: bool = True, progress=None) -> int:
    """Categorise every invoice's line items (per-invoice batches). Returns count.

    Ensures the category vocabulary has been discovered from the data first, so the
    classifier always runs against a concrete enum.
    """
    client = OpenAI(base_url=settings.vllm_url, api_key="none")
    categories = ensure_categories(progress=progress)      # discover on first use; no-op after
    scheme = get_categorisation_scheme()
    if not categories:                                     # e.g. no data sampled yet
        categories = get_categories()
    done = 0
    with pool().connection() as conn:
        inv_ids = [r["id"] for r in conn.execute("SELECT id FROM invoices ORDER BY id").fetchall()]
        for n, inv_id in enumerate(inv_ids, 1):
            q = "SELECT id, description, article FROM invoice_items WHERE invoice_id=%s"
            if only_uncategorised:
                q += " AND category IS NULL"
            items = conn.execute(q + " ORDER BY id", (inv_id,)).fetchall()
            if not items:
                continue
            # Feed BOTH article and description: on some suppliers the product name
            # is in `article` while `description` holds a code, and vice-versa.
            descs = [" — ".join(p for p in (it["article"], it["description"]) if p) or "(no description)"
                     for it in items]
            try:
                cats = _categorise_descriptions(client, settings.vllm_model, descs, categories, scheme)
            except Exception as e:  # noqa: BLE001
                if progress:
                    progress(f"invoice {inv_id}: error {e}")
                continue
            for it, (cat, var) in zip(items, cats):
                conn.execute("UPDATE invoice_items SET category=%s, variety=%s WHERE id=%s",
                             (cat, var, it["id"]))
            conn.commit()
            done += len(items)
            if progress:
                progress(f"invoice {n}/{len(inv_ids)}: {len(items)} items categorised")
    return done
