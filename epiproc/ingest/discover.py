"""Model-driven onboarding: profile the data and DISCOVER the category set.

Nothing here knows about any domain or vocabulary in advance. On a new container's first
run the local model reads a representative sample of the customer's own line items
and proposes the category vocabulary; that vocabulary then constrains classification
(ingest/categorise.py) so category names stay consistent. Entirely data-driven.
"""
from __future__ import annotations

import json

from openai import OpenAI

from epiproc.db.pool import pool
from epiproc.db.settings import set_categories
from epiproc.settings import settings

_SAMPLE_CAP = 400          # max line items shown to the model for discovery
_PER_SUPPLIER = 60         # cap per supplier so one big supplier can't dominate


def profile(conn) -> dict:  # noqa: ANN001
    """Cheap onboarding profile: invoices, suppliers, doc-types per supplier."""
    n_inv = conn.execute("SELECT count(*) AS c FROM invoices").fetchone()["c"]
    n_items = conn.execute("SELECT count(*) AS c FROM invoice_items").fetchone()["c"]
    sup_rows = conn.execute(
        "SELECT supplier, count(*) AS invoices, "
        "count(DISTINCT coalesce(document_type,'')) AS doc_types "
        "FROM invoices GROUP BY supplier ORDER BY invoices DESC"
    ).fetchall()
    return {
        "invoices": n_inv,
        "items": n_items,
        "suppliers": len(sup_rows),
        "by_supplier": [dict(r) for r in sup_rows],
    }


def _sample_descriptions(conn) -> list[str]:  # noqa: ANN001
    """A representative, cross-supplier sample of item texts (article + description)."""
    suppliers = [r["supplier"] for r in
                 conn.execute("SELECT DISTINCT supplier FROM invoices").fetchall()]
    out: list[str] = []
    for sup in suppliers:
        # Order by frequency so the taxonomy is derived from what actually dominates
        # the data, not from whatever rows Postgres happened to return first.
        rows = conn.execute(
            """SELECT ii.article, ii.description, count(*) AS n
               FROM invoice_items ii JOIN invoices i ON ii.invoice_id = i.id
               WHERE i.supplier = %s
                 AND coalesce(ii.description, ii.article, '') <> ''
               GROUP BY ii.article, ii.description
               ORDER BY n DESC
               LIMIT %s""",
            (sup, _PER_SUPPLIER),
        ).fetchall()
        for r in rows:
            txt = " — ".join(p for p in (r["article"], r["description"]) if p)
            if txt:
                out.append(txt)
    return out[:_SAMPLE_CAP]


def _discovery_schema() -> dict:
    return {"type": "json_schema", "json_schema": {
        "name": "taxonomy", "strict": True,
        "schema": {
            "type": "object",
            "properties": {"categories": {"type": "array", "items": {"type": "string"}}},
            "required": ["categories"], "additionalProperties": False,
        },
    }}


def discover_categories(client: OpenAI, model: str, conn, hint: str = "") -> list[str]:  # noqa: ANN001
    """Ask the local model to propose the category set from the actual line items."""
    samples = _sample_descriptions(conn)
    if not samples:
        return []
    listing = "\n".join(f"- {s}" for s in samples)
    prompt = (
        "Below are line items taken from one organisation's purchase invoices "
        "(article code and/or description). Infer the natural set of product "
        "categories that best organises THIS data.\n\n"
        "Return a JSON object {\"categories\": [...]} where categories is a list of "
        "8-25 short, SPECIFIC category names, each the most specific class that still "
        "groups similar items together. Rules:\n"
        "- Derive the categories only from what the items actually are — do not assume "
        "any industry or domain.\n"
        "- Prefer specific classes over umbrella terms; avoid near-duplicates "
        "(never both a singular and plural of the same word).\n"
        "- Charges are real spend, not miscellany: give freight, deposits, packaging, "
        "pallets, handling and fees their OWN specific categories (e.g. Freight, Deposit, "
        "Packaging, Pallet) when they occur.\n"
        "- Include a single 'Other' category ONLY as a fallback for items that genuinely "
        "fit nothing above — never use it as a charges bucket.\n"
        + (f"- Customer guidance to honour if compatible: {hint}\n" if hint.strip() else "")
        + f"\nLine items:\n{listing}"
    )
    r = client.chat.completions.create(
        model=model,
        messages=[{"role": "user", "content": prompt}],
        temperature=0.0, max_tokens=800,
        response_format=_discovery_schema(),
        extra_body={"chat_template_kwargs": {"enable_thinking": False}},
    )
    cats = json.loads(r.choices[0].message.content).get("categories", [])
    return [c for c in (str(x).strip() for x in cats) if c]


def ensure_categories(progress=None, hint: str = "", force: bool = False) -> list[str]:  # noqa: ANN001
    """Populate the category vocabulary from data if unset (or force a re-derive).

    Returns the resulting category list. Safe to call every scan: it only hits the
    model when the vocabulary is missing or a re-derive is explicitly requested.
    """
    from epiproc.db.settings import get_categories, get_categorisation_scheme
    if not force and get_categories():
        return get_categories()
    client = OpenAI(base_url=settings.vllm_url, api_key="none")
    with pool().connection() as conn:
        prof = profile(conn)
        if progress:
            progress(f"onboarding profile: {prof['invoices']} invoice(s), "
                     f"{prof['suppliers']} supplier(s), {prof['items']} item(s)")
        # A customer scheme, if one was entered, is passed only as optional guidance.
        scheme = get_categorisation_scheme()
        cats = discover_categories(client, settings.vllm_model, conn, hint=hint or scheme)
    set_categories(cats)
    result = get_categories()
    if progress:
        progress(f"discovered {len(result)} categor(y/ies): {', '.join(result)}")
    return result
