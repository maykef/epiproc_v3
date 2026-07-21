"""The pre-auth rate-limit key must be the real client IP, not the proxy socket peer.

Regression for the reviewer-flagged lockout: keying anonymous requests on the raw
socket peer means that behind a reverse proxy every client shares the proxy's IP and
one client tripping the login limit locks everyone out. _key_func must instead honour
EPIPROC_TRUST_XFF via _request_ip.
"""
from __future__ import annotations

import pytest


class _FakeReq:
    def __init__(self, peer: str, xff: str = ""):
        self.client = type("C", (), {"host": peer})()
        self.headers = {"x-forwarded-for": xff} if xff else {}
        self.cookies = {}
        self.state = type("S", (), {})()


def test_authenticated_key_is_username(monkeypatch):
    pytest.importorskip("fastapi")
    from epiproc.web import security

    monkeypatch.setattr(security, "_request_username", lambda r: "alice")
    assert security._key_func(_FakeReq("10.0.0.9")) == "alice"


def test_anon_key_is_socket_peer_without_trust_xff(monkeypatch):
    pytest.importorskip("fastapi")
    from epiproc.web import security

    monkeypatch.setattr(security, "_request_username", lambda r: "anonymous")
    monkeypatch.setattr(security, "_TRUST_XFF", False)
    req = _FakeReq("172.18.0.5", xff="203.0.113.7")
    # Not proxied: the forwarded header is untrusted, so key on the socket peer.
    assert security._key_func(req) == "172.18.0.5"


def test_anon_key_is_real_client_ip_behind_trusted_proxy(monkeypatch):
    pytest.importorskip("fastapi")
    from epiproc.web import security

    monkeypatch.setattr(security, "_request_username", lambda r: "anonymous")
    monkeypatch.setattr(security, "_TRUST_XFF", True)
    a = _FakeReq("172.18.0.5", xff="203.0.113.7")
    b = _FakeReq("172.18.0.5", xff="203.0.113.8")
    # Same proxy peer, different real clients -> must land in different buckets.
    assert security._key_func(a) == "203.0.113.7"
    assert security._key_func(b) == "203.0.113.8"
    assert security._key_func(a) != security._key_func(b)
