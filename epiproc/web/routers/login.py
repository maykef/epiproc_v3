"""Login / logout routes and the landing page.

Ported from v1 dashboard_app/api/routers/login.py. Single-DB: the standalone
pre-built dashboard path is dropped — every user lands on /dashboard.
"""
from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Form, Request
from fastapi.responses import RedirectResponse

from epiproc.web.auth import authenticate, verify_totp
from epiproc.web.security import RATE_LOGIN, RATE_MFA, _request_ip, audit_log, limiter
from epiproc.web.session import (
    COOKIE_NAME,
    COOKIE_SECURE,
    MFA_PENDING_COOKIE,
    make_cookie,
    make_mfa_cookie,
    verify_cookie,
    verify_mfa_cookie,
)
from epiproc.web.templates import templates


def _landing_url() -> str:
    return "/dashboard"


router = APIRouter(include_in_schema=False)


@router.get("/")
def root():
    return RedirectResponse(url="/login", status_code=302)


@router.get("/login")
def login_page(request: Request):
    # If already authenticated, skip straight to the dashboard
    cookie = request.cookies.get(COOKIE_NAME)
    if cookie:
        from epiproc.db.sessions import get_session_with_user
        sid = verify_cookie(cookie)
        if sid and get_session_with_user(sid):
            return RedirectResponse(url=_landing_url(), status_code=302)
    reason = request.query_params.get("reason", "")
    error = "Your account has expired. Please contact your administrator." if reason == "expired" else ""
    notice = "Password set successfully. You can now sign in." if reason == "password_set" else ""
    return templates.TemplateResponse(request, "login.html", {"error": error, "notice": notice})


@router.post("/login")
@limiter.limit(RATE_LOGIN)
async def login_submit(
    request: Request,
    username: Annotated[str, Form()],
    password: Annotated[str, Form()],
):
    ip = _request_ip(request)
    ua = request.headers.get("user-agent", "")
    user = authenticate(username, password)
    if not user:
        audit_log("login_failed", username=username, ip=ip, user_agent=ua)
        return templates.TemplateResponse(
            request,
            "login.html",
            {"error": "Invalid username or password.", "prefill_user": username},
            status_code=401,
        )

    if user.get("mfa_secret"):
        # Step 1 of 2: password OK, MFA still required
        response = RedirectResponse(url="/login/mfa", status_code=303)
        response.set_cookie(
            MFA_PENDING_COOKIE,
            make_mfa_cookie(username),
            httponly=True,
            secure=COOKIE_SECURE,
            samesite="lax",
            max_age=300,
        )
        return response

    audit_log("login", username=username, ip=ip, user_agent=ua)
    from epiproc.db.sessions import create_session
    from epiproc.db.users import record_last_login
    record_last_login(user["id"])
    session_id = create_session(user["id"], ip=ip, user_agent=ua)
    response = RedirectResponse(url=_landing_url(), status_code=303)
    response.set_cookie(
        COOKIE_NAME,
        make_cookie(session_id),
        httponly=True,
        secure=COOKIE_SECURE,
        samesite="lax",
        max_age=60 * 60 * 24 * 14,
    )
    return response


# ── MFA second step ───────────────────────────────────────────────────────────

@router.get("/login/mfa")
def mfa_page(request: Request):
    pending = request.cookies.get(MFA_PENDING_COOKIE, "")
    if not pending or not verify_mfa_cookie(pending):
        return RedirectResponse(url="/login", status_code=302)
    return templates.TemplateResponse(request, "mfa.html")


@router.post("/login/mfa")
@limiter.limit(RATE_MFA)
async def mfa_submit(
    request: Request,
    code: Annotated[str, Form()],
):
    ip = _request_ip(request)
    ua = request.headers.get("user-agent", "")

    pending = request.cookies.get(MFA_PENDING_COOKIE, "")
    username = verify_mfa_cookie(pending) if pending else None
    if not username:
        return RedirectResponse(url="/login", status_code=302)

    from epiproc.db.users import get_user_by_username
    user = get_user_by_username(username)
    if not user or not user.get("mfa_secret") or not verify_totp(user["mfa_secret"], code):
        audit_log("login_failed", username=username, ip=ip, user_agent=ua,
                  detail={"reason": "bad_totp"})
        return templates.TemplateResponse(
            request,
            "mfa.html",
            {"error": "Invalid code. Please try again."},
            status_code=401,
        )

    audit_log("login", username=username, ip=ip, user_agent=ua)
    from epiproc.db.sessions import create_session
    from epiproc.db.users import record_last_login
    record_last_login(user["id"])
    session_id = create_session(user["id"], ip=ip, user_agent=ua)
    response = RedirectResponse(url=_landing_url(), status_code=303)
    response.set_cookie(
        COOKIE_NAME,
        make_cookie(session_id),
        httponly=True,
        secure=COOKIE_SECURE,
        samesite="lax",
        max_age=60 * 60 * 24 * 14,
    )
    response.delete_cookie(MFA_PENDING_COOKIE)
    return response


@router.get("/logout")
def logout(request: Request):
    ip = _request_ip(request)
    cookie = request.cookies.get(COOKIE_NAME)
    username = "anonymous"
    if cookie:
        from epiproc.db.sessions import get_session_with_user, revoke_session
        sid = verify_cookie(cookie)
        if sid:
            result = get_session_with_user(sid)
            if result:
                username = result.get("username", "anonymous")
            try:
                revoke_session(sid)
            except Exception:
                pass
    audit_log("logout", username=username, ip=ip, user_agent=request.headers.get("user-agent", ""))
    response = RedirectResponse(url="/login", status_code=302)
    response.delete_cookie(COOKIE_NAME)
    return response
