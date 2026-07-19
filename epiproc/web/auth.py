"""Authentication — verifies passwords from users, upgrades scrypt -> argon2id on login.

Ported from v1 dashboard_app/api/auth.py. Single-DB (no tenant schema).
"""
from __future__ import annotations

import hashlib
import hmac
import secrets
import string
from datetime import date

from argon2 import PasswordHasher
from argon2.exceptions import VerifyMismatchError

_ph = PasswordHasher()


def hash_password(password: str) -> str:
    return _ph.hash(password)


def generate_password(length: int = 16) -> str:
    alphabet = string.ascii_letters + string.digits
    return "".join(secrets.choice(alphabet) for _ in range(length))


def _verify_scrypt(stored_hash: str, password: str) -> bool:
    """Verify werkzeug scrypt hash: scrypt:{N}:{r}:{p}${salt}${hex_hash}"""
    try:
        method_params, salt, stored_hex = stored_hash.split("$")
        _, n, r, p = method_params.split(":")
        derived = hashlib.scrypt(
            password.encode("utf-8"),
            salt=salt.encode("utf-8"),
            n=int(n), r=int(r), p=int(p),
            dklen=64,
            maxmem=64 * 1024 * 1024,
        )
        return hmac.compare_digest(derived.hex(), stored_hex)
    except Exception:
        return False


def _verify_password(stored_hash: str, password: str) -> bool:
    if stored_hash.startswith("scrypt:"):
        return _verify_scrypt(stored_hash, password)
    if stored_hash.startswith("$argon2"):
        try:
            return _ph.verify(stored_hash, password)
        except VerifyMismatchError:
            return False
    return False


def is_account_expired(user: dict) -> bool:
    expires = user.get("expires_at")
    if not expires:
        return False
    try:
        if isinstance(expires, date):
            return date.today() > expires
        return date.today() > date.fromisoformat(str(expires))
    except ValueError:
        return False


def verify_totp(secret: str, code: str) -> bool:
    import pyotp
    try:
        return pyotp.TOTP(secret).verify(code.strip(), valid_window=1)
    except Exception:
        return False


def authenticate(username: str, password: str) -> dict | None:
    from epiproc.db.users import get_user_by_username, update_user_password, record_last_login
    user = get_user_by_username(username)
    if not user:
        return None
    if not _verify_password(user["password_hash"], password):
        return None
    # Transparently upgrade legacy scrypt hash to argon2id
    if user["password_hash"].startswith("scrypt:"):
        try:
            update_user_password(user["id"], hash_password(password))
        except Exception:
            pass
    record_last_login(user["id"])
    return user
