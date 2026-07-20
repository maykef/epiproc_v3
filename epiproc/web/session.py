"""Server-side session store — cookie carries a signed UUID, DB holds the state.

Ported from v1 dashboard_app/api/session.py. Single-DB: no tenant machinery.
Keys / flags come from epiproc.settings; DB access via epiproc.db.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import secrets
from pathlib import Path

from fastapi import HTTPException, Request

from epiproc.settings import settings

_KEY_FILE = Path(settings.data_dir) / ".secret_key"
COOKIE_NAME = "ds_session"
MFA_PENDING_COOKIE = "ds_mfa_pending"

# Cookies are marked Secure by default (production runs behind HTTPS). Browsers
# discard Secure cookies on plain http:// (except localhost), so set
# EPIPROC_COOKIE_SECURE=false for HTTP LAN access in development.
COOKIE_SECURE = settings.cookie_secure


def _key() -> bytes:
    # Prefer configured key (per-container); fall back to on-disk file.
    if settings.session_key:
        return settings.session_key.encode()
    if not _KEY_FILE.exists():
        _KEY_FILE.parent.mkdir(parents=True, exist_ok=True)
        _KEY_FILE.write_text(secrets.token_hex(32))
    return _KEY_FILE.read_text().strip().encode()


def session_key() -> bytes:
    return _key()


def make_cookie(session_id: str) -> str:
    """Sign a session UUID. Cookie value = '{session_id}.{hmac}'."""
    sig = hmac.new(_key(), session_id.encode(), hashlib.sha256).hexdigest()
    return f"{session_id}.{sig}"


def verify_cookie(value: str) -> str | None:
    """Return the session_id UUID if the HMAC is valid, else None.

    The DB lookup (revoked / expired check) is the caller's responsibility.
    """
    try:
        session_id, sig = value.rsplit(".", 1)
        expected = hmac.new(_key(), session_id.encode(), hashlib.sha256).hexdigest()
        if not hmac.compare_digest(sig, expected):
            return None
        return session_id
    except Exception:
        return None


def make_mfa_cookie(username: str) -> str:
    """Signed short-lived cookie marking a pending MFA check after password verification."""
    import time
    payload = base64.urlsafe_b64encode(
        json.dumps({"u": username, "t": int(time.time())}).encode()
    ).decode()
    sig = hmac.new(_key(), payload.encode(), hashlib.sha256).hexdigest()
    return f"{payload}.{sig}"


def verify_mfa_cookie(value: str, max_age: int = 300) -> str | None:
    """Return username if the MFA pending cookie is valid and not expired, else None."""
    import time
    try:
        payload, sig = value.rsplit(".", 1)
        expected = hmac.new(_key(), payload.encode(), hashlib.sha256).hexdigest()
        if not hmac.compare_digest(sig, expected):
            return None
        data = json.loads(base64.urlsafe_b64decode(payload.encode()))
        if time.time() - data["t"] > max_age:
            return None
        return data.get("u")
    except Exception:
        return None


def get_session_user(request: Request) -> dict:
    """Return user dict from DB-backed session, or raise 307 redirect to /login."""
    from epiproc.db.sessions import get_session_with_user, touch_session
    from epiproc.web.auth import is_account_expired
    cookie = request.cookies.get(COOKIE_NAME)
    if not cookie:
        raise HTTPException(status_code=307, headers={"Location": "/login"})
    session_id = verify_cookie(cookie)
    if not session_id:
        raise HTTPException(status_code=307, headers={"Location": "/login"})
    user = get_session_with_user(session_id)
    if not user:
        raise HTTPException(status_code=307, headers={"Location": "/login"})
    if is_account_expired(user):
        raise HTTPException(status_code=307, headers={"Location": "/login?reason=expired"})
    request.state.username = user["username"]
    request.state.session_id = session_id
    try:
        touch_session(session_id)
    except Exception:
        pass
    return user
