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

# repo-baked configs (packaged into the image); a container overrides per-file via
# its mounted <data_dir>/configs.
_REPO_CONFIGS = pathlib.Path(__file__).resolve().parent.parent / "configs"


def _mounted_configs() -> pathlib.Path:
    return pathlib.Path(settings.data_dir) / "configs"


def resolve_config(filename: str, configs: pathlib.Path | None = None) -> pathlib.Path | None:
    """Resolve ONE config file, per-file: the customer's mounted copy if it exists,
    else the repo-baked default, else None.

    Per-FILE, not per-directory, on purpose. compose always mounts
    ``<data_dir>/configs``, so the folder existing says nothing about which files
    are in it — a customer who supplies only a supplier YAML (or nothing at all)
    must still get the baked ``_base.yml``. An earlier all-or-nothing directory
    switch broke exactly this: an empty mounted folder yielded a blank
    ``extraction_prompt``. When ``configs`` is given (tests), only that directory
    is consulted.
    """
    if configs is not None:
        p = configs / filename
        return p if p.is_file() else None
    mounted = _mounted_configs() / filename
    if mounted.is_file():
        return mounted
    repo = _REPO_CONFIGS / filename
    return repo if repo.is_file() else None


def _load_yaml(path: pathlib.Path | None) -> dict:
    return (yaml.safe_load(path.read_text()) or {}) if path else {}


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
    """The repo-baked config directory (packaged defaults). Kept for callers that
    need a directory handle; per-FILE lookups must go through ``resolve_config()``,
    which falls back to this directory file-by-file rather than all-or-nothing."""
    return _REPO_CONFIGS


def _base_config(configs: pathlib.Path | None = None) -> dict:
    """Generic fallback fields (prompts, dpi, …) from _base.yml (mounted or baked)."""
    return _load_yaml(resolve_config("_base.yml", configs))


def load_config(supplier: str, configs: pathlib.Path | None = None) -> SupplierConfig:
    base = _base_config(configs)
    path = resolve_config(f"{supplier}.yml", configs)
    if path is None:
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
    data = yaml.safe_load(path.read_text()) or {}
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
    """Supplier stems from any *.yml, unioned across the customer's mounted configs
    AND the repo-baked defaults (or just `configs` when given), so a supplier YAML
    in either location is discovered."""
    skip = {"_base", "base_extraction_v1", "categorisation", "departments", "tier_overrides"}
    dirs = [configs] if configs is not None else [_mounted_configs(), _REPO_CONFIGS]
    stems: set[str] = set()
    for d in dirs:
        if d.is_dir():
            stems |= {p.stem for p in d.glob("*.yml") if p.stem not in skip}
    return sorted(stems)
