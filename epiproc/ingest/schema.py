"""Canonical invoice JSON schema for guided decoding (response_format json_schema).

Derived from v1's configs/base_extraction_v1.yaml json_schema_block, expressed as
a strict JSON Schema so the vLLM constrains the model to emit exactly this shape.
Every field is nullable + required: the model MUST emit each key (null when the
value is absent), which makes downstream parsing deterministic. `strict: true`
+ additionalProperties:false is honoured by this vLLM build (verified 2026-07-19).
"""
from __future__ import annotations


def _s():   # nullable string
    return {"type": ["string", "null"]}


def _n():   # nullable number
    return {"type": ["number", "null"]}


def _i():   # nullable integer
    return {"type": ["integer", "null"]}


def _obj(props: dict) -> dict:
    return {"type": "object", "properties": props,
            "required": list(props), "additionalProperties": False}


INVOICE_SCHEMA = _obj({
    "document_type":  _s(),
    "invoice_number": _s(),
    "invoice_date":   _s(),
    "currency":       _s(),
    "seller": _obj({"name": _s(), "address": _s(), "vat_number": _s(), "registration_number": _s()}),
    "buyer":  _obj({"name": _s(), "department": _s(), "address": _s(), "customer_number": _s()}),
    "ship_to": _obj({"name": _s(), "department": _s(), "address": _s()}),
    "sold_to": _obj({"name": _s(), "department": _s(), "address": _s()}),
    "references": _obj({"your_reference": _s(), "order_reference": _s(),
                        "order_date": _s(), "sales_person": _s()}),
    "line_items": {
        "type": "array",
        "items": _obj({
            "position":     _i(),
            "article":      _s(),
            "quantity":     _n(),
            "unit":         _s(),
            "description":  _s(),
            "unit_price":   _n(),
            "total_price":  _n(),
            "line_discount_amount": _n(),
        }),
    },
    "totals": _obj({
        "subtotal": _n(), "discount_rate_percent": _n(), "discount_amount": _n(),
        "discount_2": _n(), "handling_charges": _n(), "freight": _n(),
        "vat_rate_percent": _n(), "vat_amount": _n(), "total": _n(),
    }),
    "payment_terms": _s(),
    "notes": _s(),
})


def response_format(name: str = "invoice") -> dict:
    return {"type": "json_schema",
            "json_schema": {"name": name, "strict": True, "schema": INVOICE_SCHEMA}}
