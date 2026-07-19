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
        "name": "classification", "strict": True,
        "schema": {
            "type": "object",
            "properties": {"items": {"type": "array", "items": {
                "type": "object",
                "properties": {"category": {"type": "string"}, "variety": {"type": "string"}},
                "required": ["category", "variety"], "additionalProperties": False,
            }}},
            "required": ["items"], "additionalProperties": False,
        },
    }}


def _categorise_descriptions(client: OpenAI, model: str, descriptions: list[str], scheme: str) -> list[tuple[str, str]]:
    lines = "\n".join(f"{i + 1}. {d or '(no description)'}" for i, d in enumerate(descriptions))
    prompt = (
        f"{scheme}\n\n"
        f"For each of the following {len(descriptions)} line items return an object with:\n"
        f"- 'category': the SPECIFIC flower type / product class from the scheme above "
        f"(e.g. Roses, Peonies, Chrysanthemums, Orchids, Lisianthus, Anthuriums, Plants, "
        f"Deposit, Pallet, Packaging, Freight). NEVER use an umbrella term like 'Cut flowers'.\n"
        f"- 'variety': the specific cultivar/product within that type (e.g. 'Avalanche', "
        f"'Kenyan', 'Star Roses Mixed', 'Rosita White', 'Phalaenopsis'); if there is no "
        f"distinct variety, repeat the category.\n"
        f"Return JSON with an 'items' array of exactly {len(descriptions)} objects, in order."
        f"\n\nItems:\n{lines}"
    )
    r = client.chat.completions.create(
        model=model,
        messages=[{"role": "user", "content": prompt}],
        temperature=0.0, max_tokens=2500,
        response_format=_schema(),
        extra_body={"chat_template_kwargs": {"enable_thinking": False}},
    )
    got = json.loads(r.choices[0].message.content).get("items", [])
    got = (got + [{}] * len(descriptions))[:len(descriptions)]
    out = []
    for g in got:
        cat = (g.get("category") or "Other").strip() or "Other"
        var = (g.get("variety") or cat).strip() or cat
        out.append((cat, var))
    return out


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
            # Feed BOTH article and description: on some suppliers the product
            # name is in `article` while `description` holds a customs code, and
            # vice-versa. Give the model everything so it never sees only a code.
            descs = [" — ".join(p for p in (it["article"], it["description"]) if p) or "(no description)"
                     for it in items]
            try:
                cats = _categorise_descriptions(client, settings.vllm_model, descs, scheme)
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
