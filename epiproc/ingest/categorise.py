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


def _schema(categories: list[str]) -> dict:
    """category is enum-constrained to the discovered vocabulary; variety is free."""
    cat_schema = {"type": "string", "enum": categories} if categories else {"type": "string"}
    return {"type": "json_schema", "json_schema": {
        "name": "classification", "strict": True,
        "schema": {
            "type": "object",
            "properties": {"items": {"type": "array", "items": {
                "type": "object",
                "properties": {"category": cat_schema, "variety": {"type": "string"}},
                "required": ["category", "variety"], "additionalProperties": False,
            }}},
            "required": ["items"], "additionalProperties": False,
        },
    }}


def _categorise_descriptions(client: OpenAI, model: str, descriptions: list[str],
                             categories: list[str], scheme: str = "") -> list[tuple[str, str]]:
    lines = "\n".join(f"{i + 1}. {d or '(no description)'}" for i, d in enumerate(descriptions))
    cat_list = ", ".join(categories) if categories else "(derive a short category name)"
    guidance = f"\nCustomer guidance: {scheme}\n" if scheme.strip() else ""
    prompt = (
        f"Classify each invoice line item below.{guidance}\n"
        f"For each of the {len(descriptions)} items return an object with:\n"
        f"- 'category': assign EXACTLY ONE category from this list: {cat_list}. "
        f"Pick the single most specific one that fits; if none fits, use "
        f"'{OTHER_CATEGORY}'.\n"
        f"- 'variety': the specific product, model, grade or cultivar within that "
        f"category (free text); if there is no distinct variety, repeat the category.\n"
        f"Return JSON with an 'items' array of exactly {len(descriptions)} objects, in order."
        f"\n\nItems:\n{lines}"
    )
    r = client.chat.completions.create(
        model=model,
        messages=[{"role": "user", "content": prompt}],
        temperature=0.0, max_tokens=2500,
        response_format=_schema(categories),
        extra_body={"chat_template_kwargs": {"enable_thinking": False}},
    )
    got = json.loads(r.choices[0].message.content).get("items", [])
    got = (got + [{}] * len(descriptions))[:len(descriptions)]
    out = []
    for g in got:
        cat = (g.get("category") or OTHER_CATEGORY).strip() or OTHER_CATEGORY
        var = (g.get("variety") or cat).strip() or cat
        out.append((cat, var))
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
