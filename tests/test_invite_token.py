"""Defect #6 — invite tokens must be stored hashed, never in cleartext.

A leaked DB dump/backup must not hand an attacker a usable password-set link.
create_invite_token returns the raw token (for the email link) but must persist
only its sha256 hash.
"""
from __future__ import annotations

import hashlib

from epiproc.db import users


class _Cursor:
    def fetchone(self):
        return None


class _Conn:
    def __init__(self):
        self.inserts = []

    def execute(self, sql, params=()):
        if "INSERT INTO invite_tokens" in sql:
            self.inserts.append(params)
        return _Cursor()


class _PoolCtx:
    def __init__(self, conn):
        self._conn = conn

    def connection(self):
        conn = self._conn

        class _CM:
            def __enter__(self_):
                return conn

            def __exit__(self_, *a):
                return False

        return _CM()


def test_hash_helper_is_sha256():
    assert users._hash_token("abc") == hashlib.sha256(b"abc").hexdigest()


def test_created_token_is_stored_hashed_not_cleartext(monkeypatch):
    conn = _Conn()
    monkeypatch.setattr(users, "pool", lambda: _PoolCtx(conn))

    raw = users.create_invite_token(user_id=42)

    assert conn.inserts, "no invite_tokens row inserted"
    stored = conn.inserts[0][0]          # first column = token_hash
    # What's persisted is the hash, and the raw bearer token is NOT in the row.
    assert stored == hashlib.sha256(raw.encode()).hexdigest()
    assert len(stored) == 64
    assert raw not in conn.inserts[0]
    assert stored != raw
