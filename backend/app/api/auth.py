"""Sign-in, sign-out, current user, and user management (admins)."""

from __future__ import annotations

from fastapi import APIRouter, Depends, Header, HTTPException, Request
from pydantic import BaseModel, Field

from .. import audit
from ..auth import AuthError, Principal, check_password_strength, hash_password, login, public_user, token_hash
from ..data import get_store
from .deps import current_principal, require_admin

router = APIRouter(prefix="/api", tags=["auth"])


class LoginIn(BaseModel):
    email: str = Field(..., max_length=320)
    password: str = Field(..., max_length=200)


@router.post("/auth/login")
def do_login(body: LoginIn, request: Request):
    client = request.client.host if request.client else "?"
    try:
        user, token, expires = login(get_store(), body.email, body.password, client)
    except AuthError as e:
        audit.record("auth.login", "denied", actor="staff", params={"email": body.email}, reason=str(e))
        raise HTTPException(status_code=401 if "Wrong" in str(e) else 429, detail=str(e)) from None
    audit.record("auth.login", "allowed", actor="staff", params={"email": user["email"]})
    return {"token": token, "expires_at": expires.isoformat(), "user": public_user(user)}


@router.post("/auth/logout")
def do_logout(authorization: str | None = Header(None)):
    if authorization and authorization.lower().startswith("bearer "):
        get_store().delete_session(token_hash(authorization[7:].strip()))
    return {"ok": True}


@router.get("/auth/me")
def me(p: Principal = Depends(current_principal)):
    return {"id": p.user_id, "name": p.name, "email": p.email, "role": p.role}


class PasswordIn(BaseModel):
    current_password: str = Field(..., max_length=200)
    new_password: str = Field(..., max_length=200)


@router.post("/auth/password")
def change_password(body: PasswordIn, p: Principal = Depends(current_principal)):
    from ..auth import verify_password

    store = get_store()
    user = store.get_user(user_id=p.user_id) if p.user_id else None
    if not user or not verify_password(body.current_password, user["password_hash"]):
        raise HTTPException(400, "The current password is not correct.")
    try:
        check_password_strength(body.new_password)
    except AuthError as e:
        raise HTTPException(400, str(e)) from None
    store.update_user(user["id"], password_hash=hash_password(body.new_password))
    audit.record("auth.password_changed", "allowed", actor="staff", params={"user_id": user["id"]})
    return {"ok": True, "note": "Password changed. Please sign in again."}


# ---- users (admins only) ------------------------------------------------------------
class UserIn(BaseModel):
    email: str = Field(..., max_length=320)
    name: str = Field(..., min_length=1, max_length=200)
    role: str = Field("staff", pattern="^(admin|staff)$")
    password: str = Field(..., max_length=200)


class UserPatch(BaseModel):
    name: str | None = Field(None, max_length=200)
    role: str | None = Field(None, pattern="^(admin|staff)$")
    active: bool | None = None
    password: str | None = Field(None, max_length=200)


@router.get("/admin/users")
def users(_: Principal = Depends(require_admin)):
    return [public_user(u) for u in get_store().list_users()]


@router.post("/admin/users", status_code=201)
def create_user(body: UserIn, p: Principal = Depends(require_admin)):
    store = get_store()
    email = body.email.strip().lower()
    if "@" not in email:
        raise HTTPException(400, "Enter a valid email address.")
    if store.get_user(email=email):
        raise HTTPException(409, "There is already an account with that email.")
    try:
        check_password_strength(body.password)
    except AuthError as e:
        raise HTTPException(400, str(e)) from None
    user = store.create_user(email=email, name=body.name.strip(), role=body.role, password_hash=hash_password(body.password))
    audit.record("user.create", "allowed", actor=p.label, params={"email": email, "role": body.role})
    return public_user(user)


@router.patch("/admin/users/{user_id}")
def update_user(user_id: int, body: UserPatch, p: Principal = Depends(require_admin)):
    store = get_store()
    target = store.get_user(user_id=user_id)
    if not target:
        raise HTTPException(404, "User not found.")
    changes = body.model_dump(exclude_none=True)
    demoting = changes.get("role") == "staff" or changes.get("active") is False
    if demoting and target["role"] == "admin":
        admins = [u for u in store.list_users() if u["role"] == "admin" and u["active"]]
        if len(admins) <= 1:
            raise HTTPException(400, "Keep at least one active admin.")
    if "password" in changes:
        try:
            check_password_strength(changes["password"])
        except AuthError as e:
            raise HTTPException(400, str(e)) from None
        changes["password_hash"] = hash_password(changes.pop("password"))
    user = store.update_user(user_id, **changes)
    if user is None:
        raise HTTPException(404, "User not found.")
    audit.record("user.update", "allowed", actor=p.label,
                 params={"user_id": user_id, **{k: v for k, v in changes.items() if k != "password_hash"}})
    return public_user(user)
