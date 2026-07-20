"""Defect #2 — a bad job must fail the JOB, not crash the worker.

Previously poll() caught only NotImplementedError, so any other error from a job
propagated, killed the worker, exited the container, and the job (still marked
'running') was re-queued and retried forever. This drives one poll() iteration
with a job whose handler raises, against a fake connection, and asserts the job
is marked 'error' and poll() returns normally instead of propagating.
"""
from __future__ import annotations

from epiproc import worker


class _Result:
    def __init__(self, rows):
        self._rows = rows

    def fetchone(self):
        return self._rows[0] if self._rows else None

    def fetchall(self):
        return self._rows


class _Txn:
    def __enter__(self):
        return None

    def __exit__(self, *a):
        return False


class _FakeConn:
    def __init__(self, jobs):
        self._jobs = list(jobs)
        self.executed = []

    def transaction(self):
        return _Txn()

    def execute(self, sql, params=()):
        self.executed.append((" ".join(sql.split()), params))
        s = " ".join(sql.split())
        if s.startswith("SELECT id, worker_pid FROM jobs"):
            return _Result([])                      # _requeue_stuck: nothing stuck
        if s.startswith("SELECT * FROM jobs WHERE status='queued'"):
            return _Result([self._jobs.pop(0)] if self._jobs else [])
        return _Result([])

    def rollback(self):
        self.executed.append(("ROLLBACK", ()))

    def commit(self):
        pass


def test_bad_job_is_marked_error_and_worker_survives(monkeypatch):
    job = {"id": 7, "job_type": "categorise", "params": {}}
    conn = _FakeConn([job])
    monkeypatch.setattr(worker.psycopg, "connect", lambda *a, **k: conn)

    # The job handler blows up with a non-NotImplementedError; stop the loop too.
    def boom(_job):
        worker._stop.set()
        raise ValueError("bad invoice data")
    monkeypatch.setattr(worker, "_run_job", boom)

    worker._stop.clear()
    try:
        worker.poll()          # must return, not raise
    finally:
        worker._stop.clear()

    # The failing job was marked 'error' (not left 'running' to be retried forever).
    error_updates = [
        (sql, params) for (sql, params) in conn.executed
        if "status='error'" in sql and 7 in params
    ]
    assert error_updates, f"job not marked error; executed={conn.executed}"
    assert ("ROLLBACK", ()) in conn.executed
