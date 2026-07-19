"""Job-queue CRUD.

Ported from v1 dashboard_app/api/db.py. Single-DB: no `SET search_path` and no
tenant column. The v3 jobs table carries no `tenant`/`username` columns (one
container == one customer), so those are folded into params for provenance.
"""
from __future__ import annotations

import json

from epiproc.db.pool import pool


def create_job(job_type: str, params: dict | None = None) -> str:
    with pool().connection() as conn:
        row = conn.execute(
            """INSERT INTO jobs (job_type, params)
               VALUES (%s, %s::jsonb)
               RETURNING id""",
            (job_type, json.dumps(params or {})),
        ).fetchone()
    return str(row["id"])


def get_job(job_id: str) -> dict | None:
    with pool().connection() as conn:
        row = conn.execute(
            "SELECT * FROM jobs WHERE id = %s", (job_id,)
        ).fetchone()
    return dict(row) if row else None


def cancel_job(job_id: str) -> bool:
    with pool().connection() as conn:
        result = conn.execute(
            """UPDATE jobs
               SET status = 'cancelled', finished_at = NOW()
               WHERE id = %s AND status IN ('queued', 'running')
               RETURNING id""",
            (job_id,),
        ).fetchone()
    return result is not None
