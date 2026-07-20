"""The on-disk session-signing key must be created owner-only (0600)."""
from __future__ import annotations

import stat

import pytest


def test_key_file_is_created_owner_only(monkeypatch, tmp_path):
    pytest.importorskip("fastapi")
    from epiproc.settings import settings
    from epiproc.web import session

    key_file = tmp_path / "sub" / ".secret_key"
    monkeypatch.setattr(session, "_KEY_FILE", key_file)
    monkeypatch.setattr(settings, "session_key", "", raising=False)  # force file path

    data = session._key()

    assert key_file.exists()
    assert len(data) == 64  # token_hex(32)
    mode = stat.S_IMODE(key_file.stat().st_mode)
    assert mode == 0o600, f"expected 0600, got {oct(mode)}"
