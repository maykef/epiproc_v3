"""Supplier config loader. One loader, flat YAMLs (no `inherits:` — dropped).

Reads configs/<supplier>.yml and exposes what extraction needs. The heavy
`extraction_prompt` / `continuation_prompt` strings come straight from the
weeks-of-tuning YAMLs (copied into v3/configs and owned here).
"""
from __future__ import annotations

import pathlib
from dataclasses import dataclass, field

import yaml

# repo-local configs by default; a container overrides via settings.data_dir/configs
_REPO_CONFIGS = pathlib.Path(__file__).resolve().parent.parent / "configs"


@dataclass
class SupplierConfig:
    supplier: str
    extraction_prompt: str
    continuation_prompt: str
    pdf_dpi: int = 100
    max_tokens: int = 4096
    dedup_key: str = "invoice_number"
    dashboard: dict = field(default_factory=dict)
    rules: list = field(default_factory=list)   # declarative corrections (P2 rules.py)
    raw: dict = field(default_factory=dict)


def configs_dir() -> pathlib.Path:
    return _REPO_CONFIGS


def load_config(supplier: str, configs: pathlib.Path | None = None) -> SupplierConfig:
    d = configs or _REPO_CONFIGS
    data = yaml.safe_load((d / f"{supplier}.yml").read_text())
    return SupplierConfig(
        supplier=data.get("supplier", supplier),
        extraction_prompt=data.get("extraction_prompt", ""),
        continuation_prompt=data.get("continuation_prompt", data.get("extraction_prompt", "")),
        pdf_dpi=int(data.get("pdf_dpi", 100)),
        max_tokens=int(data.get("max_tokens", 4096)),
        dedup_key=data.get("dedup_key", "invoice_number"),
        dashboard=data.get("dashboard", {}) or {},
        rules=data.get("rules", []) or [],
        raw=data,
    )


def list_suppliers(configs: pathlib.Path | None = None) -> list[str]:
    d = configs or _REPO_CONFIGS
    skip = {"base_extraction_v1", "categorisation", "departments", "tier_overrides"}
    return sorted(p.stem for p in d.glob("*.yml") if p.stem not in skip)
