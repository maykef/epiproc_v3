"""Job worker — the only orchestrator. Polls `jobs`, runs one at a time per slot.

Ported shape from v1's scripts/job_worker.py:
  - SELECT ... FOR UPDATE SKIP LOCKED  (safe for multiple workers)
  - GPU semaphore = gpu_slots (1), API semaphore = api_slots (2)
  - startup requeue of jobs whose worker_pid is dead
Job types: onboard | extract | categorise | report.
"""
from __future__ import annotations

import os
import signal
import threading
import time

import psycopg

from epiproc.settings import settings

POLL_INTERVAL = 5
_gpu_sem = threading.Semaphore(settings.gpu_slots)
_api_sem = threading.Semaphore(settings.api_slots)
_stop = threading.Event()


def _requeue_stuck(conn) -> None:  # noqa: ANN001
    rows = conn.execute(
        "SELECT id, worker_pid FROM jobs WHERE status='running'"
    ).fetchall()
    for r in rows:
        pid = r["worker_pid"]
        alive = False
        if pid:
            try:
                os.kill(pid, 0)
                alive = True
            except OSError:
                alive = False
        if not alive:
            conn.execute(
                "UPDATE jobs SET status='queued', worker_pid=NULL WHERE id=%s", (r["id"],)
            )
    conn.commit()


def _run_job(job: dict) -> None:
    """Dispatch a claimed job to the right engine stage."""
    jtype = job["job_type"]
    if jtype == "categorise":
        from epiproc.db.pool import init_pool
        from epiproc.ingest.categorise import categorise_all
        init_pool()
        params = job.get("params") or {}
        with _gpu_sem:
            n = categorise_all(only_uncategorised=params.get("only_uncategorised", False))
        print(f"[worker] categorise job {job['id']}: {n} items")
        return
    # onboard/extract/report land in later phases.
    raise NotImplementedError(f"_run_job({jtype}) — not yet implemented")


def poll() -> None:
    from psycopg.rows import dict_row
    conn = psycopg.connect(settings.pg_dsn, row_factory=dict_row, autocommit=False)
    _requeue_stuck(conn)
    print("[worker] polling jobs every", POLL_INTERVAL, "s")
    while not _stop.is_set():
        # claim one queued job atomically
        with conn.transaction():
            job = conn.execute(
                "SELECT * FROM jobs WHERE status='queued' "
                "ORDER BY created_at LIMIT 1 FOR UPDATE SKIP LOCKED"
            ).fetchone()
            if job:
                conn.execute(
                    "UPDATE jobs SET status='running', started_at=now(), worker_pid=%s WHERE id=%s",
                    (os.getpid(), job["id"]),
                )
        if not job:
            time.sleep(POLL_INTERVAL)
            continue
        try:
            _run_job(job)
            conn.execute(
                "UPDATE jobs SET status='done', finished_at=now() WHERE id=%s AND status='running'",
                (job["id"],),
            )
        except NotImplementedError as e:
            conn.execute(
                "UPDATE jobs SET status='error', error=%s, finished_at=now() WHERE id=%s",
                (str(e), job["id"]),
            )
        conn.commit()


def _sigterm(*_):  # noqa: ANN001
    _stop.set()


if __name__ == "__main__":
    signal.signal(signal.SIGTERM, _sigterm)
    signal.signal(signal.SIGINT, _sigterm)
    poll()
