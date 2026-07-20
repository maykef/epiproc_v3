"""Security middleware: audit logging, rate limiting, security headers, CSRF.

Ported from v1 dashboard_app/api/security.py. Single-DB: the audit_log INSERT
drops the tenant column (there is only one tenant now), and DB access goes
through the shared epiproc.db.pool connection pool.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import secrets
from urllib.parse import parse_qs

from fastapi import Request
from slowapi import Limiter
from slowapi.util import get_remote_address
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.responses import Response as StarletteResponse

from epiproc.web.session import COOKIE_NAME, COOKIE_SECURE, session_key

# ─────────────────────────────────────────────────────────────────────────────
# CSRF — synchronizer token (double-submit cookie pattern)
# ─────────────────────────────────────────────────────────────────────────────

CSRF_COOKIE = "ds_csrf"

_CSRF_SAFE = frozenset({"GET", "HEAD", "OPTIONS", "TRACE"})
# Routes exempt from CSRF validation (pre-session or safe by other means)
_CSRF_EXEMPT = frozenset({"/login", "/health"})


def _make_csrf_token(request: Request) -> str:
    """Derive CSRF token from session cookie; random token if no session."""
    session_cookie = request.cookies.get(COOKIE_NAME, "")
    if session_cookie:
        return hmac.new(session_key(), session_cookie.encode(), hashlib.sha256).hexdigest()
    existing = request.cookies.get(CSRF_COOKIE, "")
    if existing and len(existing) == 64:
        return existing
    return secrets.token_hex(32)


class CSRFMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        token = _make_csrf_token(request)
        request.state.csrf_token = token

        if request.method not in _CSRF_SAFE and request.url.path not in _CSRF_EXEMPT:
            cookie_token = request.cookies.get(CSRF_COOKIE, "")
            if not cookie_token:
                return StarletteResponse("CSRF cookie missing — reload the page.", status_code=403)

            submitted = request.headers.get("x-csrf-token", "")
            if not submitted:
                body = await request.body()  # cached in request._body; form() can still read it
                ct = request.headers.get("content-type", "")
                if "application/x-www-form-urlencoded" in ct:
                    form_data = parse_qs(body.decode("utf-8", errors="ignore"))
                    submitted = form_data.get("_csrf", [""])[0]

            if not submitted or not hmac.compare_digest(submitted, cookie_token):
                return StarletteResponse("CSRF validation failed.", status_code=403)

        response = await call_next(request)

        response.set_cookie(
            CSRF_COOKIE,
            token,
            httponly=False,  # JS must read it as fallback
            samesite="lax",
            secure=COOKIE_SECURE,
            max_age=60 * 60 * 24 * 14,
        )
        return response


# ─────────────────────────────────────────────────────────────────────────────
# Audit log
# ─────────────────────────────────────────────────────────────────────────────

_INSERT_AUDIT = """
    INSERT INTO public.audit_log (username, ip, action, resource, detail, user_agent)
    VALUES (%s, %s, %s, %s, %s, %s)
"""


def audit_log(
    action: str,
    username: str = "anonymous",
    ip: str | None = None,
    resource: str | None = None,
    detail: dict | None = None,
    user_agent: str | None = None,
) -> None:
    try:
        from epiproc.db.pool import pool
        with pool().connection() as conn:
            conn.execute(
                _INSERT_AUDIT,
                (
                    username,
                    ip,
                    action,
                    resource,
                    json.dumps(detail) if detail else None,
                    user_agent,
                ),
            )
    except Exception:
        pass


def _request_username(request: Request) -> str:
    # get_session_user() sets this on every authenticated request — no extra DB hit.
    username = getattr(request.state, "username", None)
    if username:
        return username
    return "anonymous"


def _request_ip(request: Request) -> str:
    return request.headers.get("x-forwarded-for", request.client.host if request.client else "unknown")


# ─────────────────────────────────────────────────────────────────────────────
# Rate limiting (slowapi)
# ─────────────────────────────────────────────────────────────────────────────

def _key_func(request: Request) -> str:
    username = _request_username(request)
    if username != "anonymous":
        return username
    return get_remote_address(request)


limiter = Limiter(key_func=_key_func)

RATE_REPORT_GENERATE = "5/hour"
RATE_LOGIN = "10/minute"
RATE_MFA = "10/minute"      # brute-force guard on the 6-digit TOTP second step
RATE_DASHBOARD = "60/minute"
RATE_SEARCH = "30/minute"
RATE_DEFAULT = "120/minute"


# ─────────────────────────────────────────────────────────────────────────────
# Security headers middleware
# ─────────────────────────────────────────────────────────────────────────────

_SECURITY_HEADERS = {
    "Strict-Transport-Security": "max-age=31536000; includeSubDomains",
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "SAMEORIGIN",
    "Content-Security-Policy": (
        "default-src 'self'; "
        "script-src 'self' cdn.jsdelivr.net 'unsafe-inline'; "
        "style-src 'self' 'unsafe-inline'; "
        "img-src 'self' data:; "
        "font-src 'self' cdn.jsdelivr.net; "
        "frame-ancestors 'self'"
    ),
    "Referrer-Policy": "strict-origin-when-cross-origin",
}


class SecurityHeadersMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        response = await call_next(request)
        for header, value in _SECURITY_HEADERS.items():
            response.headers[header] = value
        return response


# ─────────────────────────────────────────────────────────────────────────────
# Audit middleware — logs actions based on route patterns
# ─────────────────────────────────────────────────────────────────────────────

# Audit log records security/compliance events only. Routine page views and
# search queries belong in usage_events (product analytics), not here.
# login, logout, and login_failed are written directly from login.py.


def _classify_action(method: str, path: str, status: int) -> str | None:
    if method == "POST" and path.startswith("/reports/generate"):
        return "report_generate"
    if method == "DELETE" and path.startswith("/reports/"):
        return "report_delete"
    if method == "GET" and path.startswith("/reports/") and path.endswith("/pdf"):
        return "report_download"
    return None


class AuditMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        response = await call_next(request)
        action = _classify_action(request.method, request.url.path, response.status_code)
        if action:
            username = _request_username(request)
            ip = _request_ip(request)
            ua = request.headers.get("user-agent", "")
            resource = request.url.path
            audit_log(
                action=action,
                username=username,
                ip=ip,
                resource=resource,
                user_agent=ua,
            )
        return response
