"""Client-side usage event ingestion endpoint."""
from __future__ import annotations

from fastapi import APIRouter, Request
from fastapi.responses import Response

from epiproc.web.session import get_session_user

router = APIRouter(include_in_schema=False)


@router.post("/usage/events")
async def record_events(request: Request):
    try:
        user = get_session_user(request)
    except Exception:
        return Response(status_code=204)  # silently drop if not authenticated

    try:
        body = await request.json()
    except Exception:
        return Response(status_code=204)

    if user.get("role") == "admin":
        return Response(status_code=204)

    events = body if isinstance(body, list) else body.get("events", [])
    session_id = getattr(request.state, "session_id", None)

    from epiproc.db.usage import write_usage_events
    write_usage_events(user["username"], session_id, events)
    return Response(status_code=204)
