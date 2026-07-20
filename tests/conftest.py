"""Skip dependency-heavy test modules when their imports aren't installed.

CI runs `pip install -e .[test]`, so every dependency is present and all tests
run. On a bare dev box (no project install) the web/DB modules can't import, so
we ignore just those files rather than erroring the whole collection.
"""
import importlib.util

collect_ignore = []


def _missing(*mods) -> bool:
    return any(importlib.util.find_spec(m) is None for m in mods)


if _missing("slowapi", "fastapi"):
    collect_ignore.append("test_serve_pdf_idor.py")
if _missing("psycopg"):
    collect_ignore += ["test_worker_error_handling.py", "test_invite_token.py"]
