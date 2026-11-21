"""Who may call the admin API.

`require_staff`: any signed-in staff or admin user.  `require_admin`: admins only.
Auth: `Authorization: Bearer <session token>` from /api/auth/login, or the X-Admin-Token header
(ADMIN_TOKEN in .env) as a break-glass admin key.
"""

import secrets
from datetime import UTC, datetime

from fastapi import Depends, Header, HTTPException

from ..auth import TOKEN_PRINCIPAL, Principal, token_hash
from ..config import get_settings
from ..data import get_store


def current_principal(authorization: str | None = Header(None), x_admin_token: str | None = Header(None)) -> Principal:
    if authorization and authorization.lower().startswith("bearer "):
        token = authorization[7:].strip()
        user = get_store().session_user(token_hash(token), datetime.now(UTC)) if token else None
        if user:
            return Principal(user["id"], user["name"], user["email"], user["role"])
        raise HTTPException(status_code=401, detail="Your session has expired. Please sign in again.")
    expected = get_settings().secret("admin_token")
    if x_admin_token:
        if expected and secrets.compare_digest(x_admin_token, expected):
            return TOKEN_PRINCIPAL
        raise HTTPException(status_code=401, detail="Admin token missing or wrong (send it as X-Admin-Token).")
    raise HTTPException(status_code=401, detail="Please sign in.")


def require_staff(p: Principal = Depends(current_principal)) -> Principal:
    return p


def require_admin(p: Principal = Depends(current_principal)) -> Principal:
    if not p.is_admin:
        raise HTTPException(status_code=403, detail="Only clinic admins can do this.")
    return p
