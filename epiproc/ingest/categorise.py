"""Line-item categorisation via the local model.

Text-only (no vision) — it classifies the already-extracted line-item
descriptions, so it's fast and cheap. The category VOCABULARY is customer-
specific and lives in the instance DB (settings.categorisation_scheme): a flower
wholesaler categorises by flower type (Roses, Peonies, Chrysanthemums…), a lab by
equipment/consumables. Guided JSON constrains the output to one category per item.
"""
from __future__ import annotations

import json

from openai import OpenAI

from epiproc.db.pool import pool
from epiproc.db.settings import get_categorisation_scheme
from epiproc.settings import settings


def _schema() -> dict:
    return {"type": "json_schema", "json_schema": {
        "name": "categories", "strict": True,
        "schema": {
            "type": "object",
            "properties": {"categories": {"type": "array", "items": {"type": "string"}}},
            "required": ["categories"], "additionalProperties": False,
        },
    }}


def _categorise_descriptions(client: OpenAI, model: str, descriptions: list[str], scheme: str) -> list[str]:
    lines = "\n".join(f"{i + 1}. {d or '(no description)'}" for i, d in enumerate(descriptions))
    prompt = (
        f"{scheme}\n\n"
        f"Classify each of the following {len(descriptions)} line items. Return JSON "
        f"with a 'categories' array of exactly {len(descriptions)} short strings, one "
        f"per item, in the same order.\n\nItems:\n{lines}"
    )
    r = client.chat.completions.create(
        model=model,
        messages=[{"role": "user", "content": prompt}],
        temperature=0.0, max_tokens=1500,
        response_format=_schema(),
        extra_body={"chat_template_kwargs": {"enable_thinking": False}},
    )
    cats = json.loads(r.choices[0].message.content).get("categories", [])
    # pad/truncate to match item count so zip is safe
    cats = (cats + ["Other"] * len(descriptions))[:len(descriptions)]
    return [c.strip() or "Other" for c in cats]


def categorise_all(only_uncategorised: bool = True, progress=None) -> int:
    """Categorise every invoice's line items (per-invoice batches). Returns count."""
    client = OpenAI(base_url=settings.vllm_url, api_key="none")
    scheme = get_categorisation_scheme()
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
            descs = [(it["description"] or it["article"] or "") for it in items]
            try:
                cats = _categorise_descriptions(client, settings.vllm_model, descs, scheme)
            except Exception as e:  # noqa: BLE001
                if progress:
                    progress(f"invoice {inv_id}: error {e}")
                continue
            for it, c in zip(items, cats):
                conn.execute("UPDATE invoice_items SET category=%s WHERE id=%s", (c, it["id"]))
            conn.commit()
            done += len(items)
            if progress:
                progress(f"invoice {n}/{len(inv_ids)}: {len(items)} items categorised")
    return done
