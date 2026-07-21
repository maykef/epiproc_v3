"""`cli.py new` writes secrets into .env — it must be created owner-only (0600).

The .env holds POSTGRES_PASSWORD and EPIPROC_SESSION_KEY; it should match the care
taken with the on-disk session-key file (see test_session_key_perms).
"""
from __future__ import annotations

import argparse
import importlib.util
import pathlib
import stat

_CLI = pathlib.Path(__file__).resolve().parent.parent / "cli.py"


def _load_cli():
    spec = importlib.util.spec_from_file_location("epiproc_cli", _CLI)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_new_env_is_owner_only(monkeypatch, tmp_path):
    cli = _load_cli()
    monkeypatch.setattr(cli, "BASE", tmp_path)
    cli.cmd_new(argparse.Namespace(name="acme", port=5011, institution="ACME Ltd"))

    env = tmp_path / "acme" / ".env"
    assert env.exists()
    mode = stat.S_IMODE(env.stat().st_mode)
    assert mode == 0o600, f"expected 0600, got {oct(mode)}"

    body = env.read_text()
    assert "POSTGRES_PASSWORD=" in body
    assert "EPIPROC_SESSION_KEY=" in body
