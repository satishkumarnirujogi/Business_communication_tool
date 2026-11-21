"""Staff accounts: scrypt password hashes, opaque bearer sessions, roles (admin | staff).

- Passwords are hashed with scrypt (stdlib) and a random salt; they are never stored or logged.
- A login returns a random token; only its SHA-256 is stored, so a database leak doesn't leak sessions.
- Repeated failed logins are slowed down per email and per client address.
- ADMIN_TOKEN (from .env) still works as a break-glass admin key for scripts and recovery.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import secrets
import threading
import time
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

MIN_PASSWORD = 10
SESSION_HOURS = 12
_N, _R, _P = 2**14, 8, 1


class AuthError(Exception):
    pass


def hash_password(password: str) -> str:
    salt = secrets.token_bytes(16)
    digest = hashlib.scrypt(password.encode(), salt=salt, n=_N, r=_R, p=_P, dklen=32)
    return f"scrypt${_N}${_R}${_P}${base64.b64encode(salt).decode()}${base64.b64encode(digest).decode()}"


def verify_password(password: str, stored: str) -> bool:
    try:
        algo, n, r, p, salt, digest = stored.split("$")
        if algo != "scrypt":
            return False
        calc = hashlib.scrypt(password.encode(), salt=base64.b64decode(salt), n=int(n), r=int(r), p=int(p),
                              dklen=len(base64.b64decode(digest)))
        return hmac.compare_digest(calc, base64.b64decode(digest))
    except (ValueError, TypeError):
        return False


def check_password_strength(password: str) -> None:
    if len(password) < MIN_PASSWORD:
        raise AuthError(f"Use at least {MIN_PASSWORD} characters for the password.")
    if password.lower() == password or password.isalpha():
        raise AuthError("Use a mix of upper- and lower-case letters and at least one number or symbol.")


def token_hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def new_session(store, user_id: int) -> tuple[str, datetime]:
    token = secrets.token_urlsafe(32)
    expires = datetime.now(UTC) + timedelta(hours=SESSION_HOURS)
    store.create_session(token_hash(token), user_id, expires)
    return token, expires


# ---- brute-force protection ----------------------------------------------------------
class _Limiter:
    """At most 5 failures per key in 15 minutes."""

    def __init__(self, limit: int = 5, window: float = 900):
        self.limit, self.window = limit, window
        self._fails: dict[str, list[float]] = {}
        self._lock = threading.Lock()

    def blocked(self, *keys: str) -> bool:
        now = time.monotonic()
        with self._lock:
            for k in keys:
                self._fails[k] = [t for t in self._fails.get(k, []) if now - t < self.window]
                if len(self._fails[k]) >= self.limit:
                    return True
        return False

    def fail(self, *keys: str) -> None:
        with self._lock:
            for k in keys:
                self._fails.setdefault(k, []).append(time.monotonic())

    def reset(self, *keys: str) -> None:
        with self._lock:
            for k in keys:
                self._fails.pop(k, None)


limiter = _Limiter()


@dataclass(frozen=True)
class Principal:
    """Who is calling the admin API."""

    user_id: int | None
    name: str
    email: str
    role: str  # admin | staff

    @property
    def is_admin(self) -> bool:
        return self.role == "admin"

    @property
    def label(self) -> str:
        return self.email or self.name


TOKEN_PRINCIPAL = Principal(None, "Admin token", "", "admin")


def login(store, email: str, password: str, client: str) -> tuple[dict, str, datetime]:
    email = (email or "").strip().lower()
    keys = (f"email:{email}", f"ip:{client}")
    if limiter.blocked(*keys):
        raise AuthError("Too many failed sign-ins. Wait 15 minutes and try again.")
    user = store.get_user(email=email)
    # Same work and the same message whether or not the account exists.
    ok = verify_password(password, user["password_hash"] if user else hash_password("x" * 12)) and bool(user)
    if not ok or not user["active"]:
        limiter.fail(*keys)
        raise AuthError("Wrong email or password.")
    limiter.reset(*keys)
    store.update_user(user["id"], last_login_at=datetime.now(UTC))
    token, expires = new_session(store, user["id"])
    return user, token, expires


def public_user(u: dict) -> dict:
    return {k: u[k] for k in ("id", "email", "name", "role", "active", "created_at", "last_login_at") if k in u}
