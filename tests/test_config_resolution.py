"""Per-file config resolution — the fix for the empty-mounted-configs bug.

compose always mounts <data_dir>/configs, so the folder existing tells us nothing
about which files are inside it. An earlier all-or-nothing directory switch made a
customer with an empty mounted folder extract every PDF with a BLANK
extraction_prompt (the baked _base.yml was never consulted). Resolution is now
per-file: mounted copy if present, else the repo-baked default. These tests pin
that so it can't silently regress.
"""
from __future__ import annotations

import pathlib

from epiproc import suppliers
from epiproc.settings import settings

REPO_BASE = pathlib.Path(suppliers.__file__).resolve().parent.parent / "configs" / "_base.yml"


def _point_at(tmp_path, monkeypatch) -> pathlib.Path:
    """Point settings.data_dir at a fresh dir; return its configs/ path."""
    monkeypatch.setattr(settings, "data_dir", str(tmp_path))
    return tmp_path / "configs"


def test_repo_base_yaml_exists():
    # The whole fix depends on _base.yml being baked into the image.
    assert REPO_BASE.is_file()


def test_empty_mounted_dir_falls_back_to_baked_base(tmp_path, monkeypatch):
    cfg = _point_at(tmp_path, monkeypatch)
    cfg.mkdir()  # present but EMPTY, as compose leaves it for a fresh customer
    c = suppliers.load_config("_generic")
    assert len(c.extraction_prompt) > 100, "empty mounted configs must fall back to baked _base.yml"


def test_missing_mounted_dir_falls_back_to_baked_base(tmp_path, monkeypatch):
    _point_at(tmp_path, monkeypatch)  # no configs/ dir at all
    c = suppliers.load_config("_generic")
    assert len(c.extraction_prompt) > 100


def test_mounted_base_overrides_baked(tmp_path, monkeypatch):
    cfg = _point_at(tmp_path, monkeypatch)
    cfg.mkdir()
    (cfg / "_base.yml").write_text("extraction_prompt: CUSTOM_PROMPT\n")
    c = suppliers.load_config("_generic")
    assert c.extraction_prompt == "CUSTOM_PROMPT"


def test_mounted_supplier_yaml_resolves_and_inherits_baked_base(tmp_path, monkeypatch):
    # A customer supplies only a per-supplier YAML; it must resolve AND still
    # inherit the baked _base.yml's prompt for the fields it omits.
    cfg = _point_at(tmp_path, monkeypatch)
    cfg.mkdir()
    (cfg / "acme.yml").write_text('dashboard:\n  display_name: ACME\n  color: "#123456"\n')
    c = suppliers.load_config("acme")
    assert c.dashboard.get("display_name") == "ACME"
    assert len(c.extraction_prompt) > 100  # inherited from baked _base.yml
    assert "acme" in suppliers.list_suppliers()


def test_resolve_config_returns_none_when_absent_everywhere(tmp_path, monkeypatch):
    _point_at(tmp_path, monkeypatch)
    assert suppliers.resolve_config("departments.yml") is None  # not baked, not mounted
    assert suppliers.resolve_config("_base.yml") == REPO_BASE   # baked fallback
