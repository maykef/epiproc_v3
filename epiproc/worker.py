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
AUTO_SCAN_INTERVAL = 60           # seconds between idle folder scans
# GPU work (extraction, categorisation, discovery) is serialised through this
# single-permit semaphore. The poll loop is single-threaded today so it is never
# contended, but it documents and enforces the GPU=1 ceiling if the worker ever
# runs stages concurrently. v1's separate API semaphore is dropped — the loop
# never ran API jobs in parallel, so it was dead code.
_gpu_sem = threading.Semaphore(settings.gpu_slots)
_stop = threading.Event()

# Postgres going away (sidecar restart, network blip) surfaces as one of these.
# A closed/broken connection raises InterfaceError; a server-side drop raises
# OperationalError — treat both as "reconnect", not "crash the worker".
_DB_DOWN = (psycopg.OperationalError, psycopg.InterfaceError)


def _connect():  # noqa: ANN201
    from psycopg.rows import dict_row
    return psycopg.connect(settings.pg_dsn, row_factory=dict_row, autocommit=False)


def _scan(tag: str) -> dict:
    """Run the folder scan under the GPU slot. Shared by the auto-scan and the
    `extract`/`onboard`/`process` job types."""
    from epiproc.db.pool import init_pool
    from epiproc.ingest.scan import scan_and_process
    init_pool()
    with _gpu_sem:
        return scan_and_process(progress=lambda m: print(f"[worker]{tag} {m}", flush=True))


def _requeue_abandoned(conn) -> None:  # noqa: ANN001
    """Requeue jobs left 'running' that no live worker owns.

    A job whose worker_pid is dead (the worker crashed or was replaced) is safe to
    retry. A job tagged with OUR OWN pid was abandoned when this worker's DB
    connection dropped mid-run, so reclaim it too — otherwise it would sit 'running'
    forever, since the pid liveness check would (correctly) see us alive.
    """
    me = os.getpid()
    rows = conn.execute(
        "SELECT id, worker_pid FROM jobs WHERE status='running'"
    ).fetchall()
    for r in rows:
        pid = r["worker_pid"]
        alive = False
        if pid and pid != me:
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
        from epiproc.ingest.discover import ensure_categories
        init_pool()
        params = job.get("params") or {}
        with _gpu_sem:
            if params.get("rediscover"):
                ensure_categories(progress=lambda m: print(f"[worker] {m}", flush=True), force=True)
            n = categorise_all(only_uncategorised=params.get("only_uncategorised", False))
        print(f"[worker] categorise job {job['id']}: {n} items")
        return
    if jtype in ("extract", "onboard", "process"):
        counts = _scan(f"[job {job['id']}]")
        print(f"[worker] {jtype} job {job['id']}: {counts}")
        return
    # report lands in a later phase.
    raise NotImplementedError(f"_run_job({jtype}) — not yet implemented")


def poll() -> None:
    conn = _connect()
    _requeue_abandoned(conn)
    print("[worker] polling jobs every", POLL_INTERVAL, "s")
    last_scan = 0.0                     # 0 => scan on the first idle cycle (boot)
    while not _stop.is_set():
        try:
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
                # Idle: periodically scan the invoices folder so freshly-dropped PDFs
                # are processed with no manual trigger. Idempotent (ingested_files),
                # so an empty or unchanged folder is nearly free.
                now = time.monotonic()
                if now - last_scan >= AUTO_SCAN_INTERVAL:
                    last_scan = now
                    try:
                        counts = _scan("[auto]")
                        if counts.get("ingested") or counts.get("error"):
                            print(f"[worker][auto] scan: {counts}", flush=True)
                    except Exception as e:  # noqa: BLE001
                        print(f"[worker][auto] scan error: {e}", flush=True)
                time.sleep(POLL_INTERVAL)
                continue
            try:
                _run_job(job)
                conn.execute(
                    "UPDATE jobs SET status='done', finished_at=now() WHERE id=%s AND status='running'",
                    (job["id"],),
                )
            except _DB_DOWN:
                raise                # connection is gone — reconnect in the outer handler
            except Exception as e:  # noqa: BLE001 — a bad job must fail the JOB, never the worker
                # Previously only NotImplementedError was caught, so any other error
                # from _run_job crashed poll() -> the container exited -> compose
                # restarted it -> the still-'running' job was re-queued -> the same
                # bad job retried forever. Mark it failed and keep going.
                conn.rollback()  # discard any half-applied statement on this conn
                conn.execute(
                    "UPDATE jobs SET status='error', error=%s, finished_at=now() "
                    "WHERE id=%s AND status='running'",
                    (str(e)[:1000], job["id"]),
                )
                print(f"[worker] job {job['id']} ({job['job_type']}) failed: {e}", flush=True)
            conn.commit()
        except _DB_DOWN as e:
            # Postgres went away (sidecar restart, network blip). Previously this
            # crashed poll(), and the error-path rollback()/UPDATE above would ALSO
            # throw on the dead connection, masking the original job error and
            # exiting the container. Now we reconnect in place and reclaim whatever
            # job we abandoned mid-run, so a DB blip costs a retry, not a crash.
            print(f"[worker] database connection lost, reconnecting: {e}", flush=True)
            try:
                conn.close()
            except Exception:  # noqa: BLE001
                pass
            time.sleep(POLL_INTERVAL)
            try:
                conn = _connect()
                _requeue_abandoned(conn)
            except _DB_DOWN:
                pass             # still down — the next loop iteration retries


def _sigterm(*_):  # noqa: ANN001
    _stop.set()


if __name__ == "__main__":
    signal.signal(signal.SIGTERM, _sigterm)
    signal.signal(signal.SIGINT, _sigterm)
    poll()
