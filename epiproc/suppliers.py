"""Supplier config loader. One loader, flat YAMLs (no `inherits:` — dropped).

Reads configs/<supplier>.yml and exposes what extraction needs. The heavy
`extraction_prompt` / `continuation_prompt` strings come straight from the
weeks-of-tuning YAMLs (copied into v3/configs and owned here).
"""
from __future__ import annotations

import pathlib
from dataclasses import dataclass, field

import yaml

from epiproc.settings import settings

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
    """The active config directory.

    Prefer the container's mounted ``<data_dir>/configs`` when it exists, else the
    repo-baked defaults. A customer drops per-supplier YAMLs (and ``departments.yml``)
    into their mounted configs folder to customise extraction/normalisation without a
    rebuild — so BOTH ingest and the dashboard must read from here, not the baked copy.
    """
    mounted = pathlib.Path(settings.data_dir) / "configs"
    if mounted.is_dir():
        return mounted
    return _REPO_CONFIGS


def _base_config(configs: pathlib.Path) -> dict:
    """Generic fallback fields (prompts, dpi, …) from configs/_base.yml."""
    path = configs / "_base.yml"
    if not path.exists():
        return {}
    return yaml.safe_load(path.read_text()) or {}


def load_config(supplier: str, configs: pathlib.Path | None = None) -> SupplierConfig:
    d = configs or configs_dir()
    base = _base_config(d)
    path = d / f"{supplier}.yml"
    if not path.exists():
        # Blank-slate default: suppliers discovered from data need no hand-written
        # config to appear on the dashboard. Extraction uses the generic _base
        # prompts; display name is derived; credit-note totals are signed by rules.
        return SupplierConfig(
            supplier=supplier,
            extraction_prompt=base.get("extraction_prompt", ""),
            continuation_prompt=base.get("continuation_prompt",
                                         base.get("extraction_prompt", "")),
            pdf_dpi=int(base.get("pdf_dpi", 100)),
            max_tokens=int(base.get("max_tokens", 4096)),
            dedup_key=base.get("dedup_key", "invoice_number"),
            dashboard={"display_name": supplier.replace("_", " ").title()},
        )
    data = yaml.safe_load(path.read_text())
    # A supplier YAML may omit any field; fall back to _base for what it leaves out.
    return SupplierConfig(
        supplier=data.get("supplier", supplier),
        extraction_prompt=data.get("extraction_prompt", base.get("extraction_prompt", "")),
        continuation_prompt=data.get(
            "continuation_prompt",
            data.get("extraction_prompt",
                     base.get("continuation_prompt", base.get("extraction_prompt", "")))),
        pdf_dpi=int(data.get("pdf_dpi", base.get("pdf_dpi", 100))),
        max_tokens=int(data.get("max_tokens", base.get("max_tokens", 4096))),
        dedup_key=data.get("dedup_key", base.get("dedup_key", "invoice_number")),
        dashboard=data.get("dashboard", {}) or {},
        rules=data.get("rules", []) or [],
        raw=data,
    )


def list_suppliers(configs: pathlib.Path | None = None) -> list[str]:
    d = configs or configs_dir()
    skip = {"_base", "base_extraction_v1", "categorisation", "departments", "tier_overrides"}
    return sorted(p.stem for p in d.glob("*.yml") if p.stem not in skip)
