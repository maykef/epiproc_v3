"""Password-set flow for invited users.

GET  /reset/{token}  — show password-set form (validates token first)
POST /reset/{token}  — set password and redirect to login
"""
from __future__ import annotations

from fastapi import APIRouter, Form, Request
from fastapi.responses import RedirectResponse

from epiproc.db.users import consume_invite_token, get_invite_token
from epiproc.web.auth import hash_password
from epiproc.web.templates import templates

router = APIRouter(tags=["reset"])


@router.get("/reset/{token}")
def reset_page(request: Request, token: str):
    record = get_invite_token(token)
    if record is None:
        return templates.TemplateResponse(
            request,
            "reset.html",
            {"invalid": True},
            status_code=410,
        )
    return templates.TemplateResponse(
        request,
        "reset.html",
        {"token": token, "username": record["username"]},
    )


@router.post("/reset/{token}")
async def reset_submit(
    request: Request,
    token: str,
    password: str = Form(...),
    password2: str = Form(...),
):
    if password != password2:
        record = get_invite_token(token)
        return templates.TemplateResponse(
            request,
            "reset.html",
            {
                "token": token,
                "username": record["username"] if record else "",
                "error": "Passwords do not match.",
            },
            status_code=422,
        )

    if len(password) < 12:
        record = get_invite_token(token)
        return templates.TemplateResponse(
            request,
            "reset.html",
            {
                "token": token,
                "username": record["username"] if record else "",
                "error": "Password must be at least 12 characters.",
            },
            status_code=422,
        )

    ok = consume_invite_token(token, hash_password(password))
    if not ok:
        return templates.TemplateResponse(
            request,
            "reset.html",
            {"invalid": True},
            status_code=410,
        )

    return RedirectResponse("/login?reason=password_set", status_code=303)
