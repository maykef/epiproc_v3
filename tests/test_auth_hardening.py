"""Auth hardening: username-enumeration timing + no premature last-login.

- authenticate() must run the same argon2 work when the username is unknown as
  when the password is wrong, and must not blow up.
- authenticate() must NOT record last-login itself — the login routes do that
  after full auth (incl. MFA), so doing it here double-counts.
"""
from __future__ import annotations

import pytest


def _auth():
    pytest.importorskip("argon2")
    pytest.importorskip("psycopg")  # epiproc.db.users imports the pool module
    from epiproc.db import users
    from epiproc.web import auth
    return auth, users


def test_unknown_user_returns_none_without_crashing(monkeypatch):
    auth, users = _auth()
    monkeypatch.setattr(users, "get_user_by_username", lambda u: None)
    # Exercises the dummy-verify timing-equalisation branch.
    assert auth.authenticate("no-such-user", "whatever") is None


def test_authenticate_does_not_record_last_login(monkeypatch):
    auth, users = _auth()
    pw = "correct horse battery staple"
    user = {"id": 1, "username": "alice", "password_hash": auth.hash_password(pw)}
    monkeypatch.setattr(users, "get_user_by_username", lambda u: user)
    called = []
    monkeypatch.setattr(users, "record_last_login", lambda uid: called.append(uid))

    result = auth.authenticate("alice", pw)

    assert result is user
    assert called == [], "authenticate must not record last-login (the routes do)"
