"""First-run setup wizard and the admin Settings page (AI model, clinic & branding, rules, data source)."""

from __future__ import annotations

import time
from typing import Any, Literal

from fastapi import APIRouter, Depends, File, HTTPException, UploadFile
from pydantic import BaseModel, Field

from .. import audit
from ..auth import AuthError, Principal, check_password_strength, hash_password, new_session, public_user
from ..config import ROOT, ConfigError, get_settings
from ..config_files import read_rules_raw, reload_all, update_clinic, update_env, write_rules
from ..data import build_store, get_store
from ..llm.provider import LLMConfig, test_connection
from ..rules import DAYS
from .deps import require_admin

router = APIRouter(prefix="/api", tags=["settings"])
BRANDING_DIR = ROOT / "data" / "branding"
LOGO_TYPES = {"image/png": "png", "image/jpeg": "jpg", "image/webp": "webp", "image/svg+xml": "svg"}


# ---- shared models ----------------------------------------------------------------------
class LLMIn(BaseModel):
    provider: str = Field(..., min_length=1, max_length=50)
    model: str = Field(..., min_length=1, max_length=200)
    api_key: str | None = Field(None, max_length=500)  # empty / None = keep the saved key
    base_url: str | None = Field(None, max_length=500)
    tool_mode: Literal["auto", "native", "json"] = "auto"


class BrandingIn(BaseModel):
    primary_color: str | None = Field(None, pattern="^#[0-9a-fA-F]{6}$")
    logo_url: str | None = Field(None, max_length=500)


class ClinicIn(BaseModel):
    name: str = Field(..., min_length=1, max_length=200)
    phone: str = Field(..., min_length=3, max_length=50)
    email: str = Field("", max_length=320)
    address: str = Field("", max_length=300)
    website: str = Field("", max_length=300)
    timezone: str = Field("UTC", max_length=64)
    languages: list[str] = Field(default_factory=lambda: ["en"])
    currency: str = Field("EUR", max_length=8)
    branding: BrandingIn = Field(default_factory=BrandingIn)


class DataSourceIn(BaseModel):
    kind: str = Field(..., pattern="^(sqlite|postgres|excel|google_sheets)$")
    database_url: str | None = Field(None, max_length=1000)
    excel_path: str | None = Field(None, max_length=500)
    google_sheets_id: str | None = Field(None, max_length=200)
    google_service_account_file: str | None = Field(None, max_length=500)


def _llm_cfg(body: LLMIn) -> LLMConfig:
    s = get_settings()
    return LLMConfig(provider=body.provider.strip().lower(), model=body.model.strip(),
                     api_key=(body.api_key or "").strip() or s.secret("llm_api_key"),
                     base_url=(body.base_url or "").strip() or None, tool_mode=body.tool_mode)


def _save_llm(body: LLMIn) -> None:
    values = {"LLM_PROVIDER": body.provider.strip().lower(), "LLM_MODEL": body.model.strip(),
              "LLM_BASE_URL": (body.base_url or "").strip(), "LLM_TOOL_MODE": body.tool_mode}
    if key := (body.api_key or "").strip():
        values["LLM_API_KEY"] = key
    update_env(values)


def _save_clinic(body: ClinicIn) -> None:
    langs = [x.strip().lower() for x in body.languages if x.strip().lower() in ("en", "de")]
    if not langs:
        raise HTTPException(400, "Choose at least one language (English or German).")
    branding = {k: v for k, v in body.branding.model_dump().items() if v is not None}
    try:
        update_clinic({"name": body.name.strip(), "phone": body.phone.strip(), "email": body.email.strip(),
                       "address": body.address.strip(), "website": body.website.strip(), "timezone": body.timezone.strip(),
                       "languages": langs, "currency": body.currency.strip().upper() or "EUR",
                       **({"branding": branding} if branding else {})})
    except ConfigError as e:
        raise HTTPException(400, str(e)) from None


def _data_source_env(body: DataSourceIn) -> dict[str, str]:
    values = {"DATA_SOURCE": body.kind}
    if body.kind == "postgres":
        if not (body.database_url or "").strip() and not get_settings().secret("database_url"):
            raise HTTPException(400, "Enter the Postgres connection string.")
        if url := (body.database_url or "").strip():
            values["DATABASE_URL"] = url
    if body.kind == "excel" and body.excel_path:
        values["EXCEL_PATH"] = body.excel_path.strip()
    if body.kind == "google_sheets":
        if not (body.google_sheets_id or get_settings().google_sheets_id):
            raise HTTPException(400, "Enter the Google Sheet id.")
        if body.google_sheets_id:
            values["GOOGLE_SHEETS_ID"] = body.google_sheets_id.strip()
        if body.google_service_account_file:
            values["GOOGLE_SERVICE_ACCOUNT_FILE"] = body.google_service_account_file.strip()
    return values


# ---- rules (structured form over rules.yaml) -----------------------------------------------
BOOKING_KEYS = ("slot_minutes", "default_duration_minutes", "min_lead_time_hours", "max_days_ahead",
                "max_search_span_days", "max_active_appointments_per_patient")


def rules_form() -> dict:
    raw = read_rules_raw()
    esc = (raw.get("escalation") or {}).get("categories") or {}
    return {
        "opening_hours": {d: (list(raw["opening_hours"].get(d)) if raw["opening_hours"].get(d) else None) for d in DAYS},
        "closed_dates": [str(x) for x in (raw.get("closed_dates") or [])],
        "booking": {k: raw["booking"].get(k) for k in BOOKING_KEYS},
        "cancellation": {"min_notice_hours": raw["cancellation"]["min_notice_hours"],
                         "policy_text": " ".join(str(raw["cancellation"]["policy_text"]).split())},
        "reschedule": {"min_notice_hours": (raw.get("reschedule") or {}).get("min_notice_hours",
                                                                               raw["cancellation"]["min_notice_hours"])},
        "escalation_keywords": {name: list(cat.get("keywords") or []) for name, cat in esc.items()},
    }


class RulesIn(BaseModel):
    opening_hours: dict[str, list[str] | None]
    closed_dates: list[str] = []
    booking: dict[str, int]
    cancellation: dict[str, Any]
    reschedule: dict[str, int] = {}
    escalation_keywords: dict[str, list[str]] = {}


def save_rules(body: RulesIn) -> None:
    raw = read_rules_raw()
    for d in DAYS:
        hours = body.opening_hours.get(d)
        if hours and (len(hours) != 2 or hours[0] >= hours[1]):
            raise HTTPException(400, f"{d.title()}: the closing time must be after the opening time.")
        raw["opening_hours"][d] = list(hours) if hours else None
    raw["closed_dates"] = sorted({d.strip() for d in body.closed_dates if d.strip()})
    for k in BOOKING_KEYS:
        if k in body.booking:
            if int(body.booking[k]) < (0 if k == "min_lead_time_hours" else 1):
                raise HTTPException(400, f"{k.replace('_', ' ')} must be a positive number.")
            raw["booking"][k] = int(body.booking[k])
    if "min_notice_hours" in body.cancellation:
        raw["cancellation"]["min_notice_hours"] = int(body.cancellation["min_notice_hours"])
    if body.cancellation.get("policy_text"):
        raw["cancellation"]["policy_text"] = str(body.cancellation["policy_text"]).strip()
    if body.reschedule.get("min_notice_hours") is not None:
        raw.setdefault("reschedule", {})["min_notice_hours"] = int(body.reschedule["min_notice_hours"])
    cats = (raw.get("escalation") or {}).get("categories") or {}
    for name, words in body.escalation_keywords.items():
        if name in cats:
            cleaned = list(dict.fromkeys(w.strip().lower() for w in words if w.strip()))
            if not cleaned:
                raise HTTPException(400, f"Keep at least one keyword for '{name}'.")
            cats[name]["keywords"] = cleaned
    try:
        write_rules(raw)
    except ConfigError as e:
        raise HTTPException(400, str(e)) from None


# ==========================================================================================
# Setup wizard (only while no user account exists)
# ==========================================================================================
def _needs_setup() -> bool:
    try:
        return get_store().count_users() == 0
    except Exception:
        return True


def _setup_only() -> None:
    if not _needs_setup():
        raise HTTPException(403, "Setup is already complete. Sign in instead.")


@router.get("/setup/status")
def setup_status():
    s = get_settings()
    return {"needs_setup": _needs_setup(), "clinic_name": s.clinic.name, "data_source": s.data_source,
            "llm_configured": s.llm_configured}


@router.get("/setup/defaults", dependencies=[Depends(_setup_only)])
def setup_defaults():
    s = get_settings()
    c = s.clinic
    return {"clinic": {"name": c.name, "phone": c.phone, "email": c.email, "address": c.address, "website": c.website,
                       "timezone": c.timezone, "languages": c.languages, "currency": c.currency,
                       "branding": c.branding.model_dump()},
            "llm": {"provider": s.llm_provider, "model": s.llm_model, "base_url": s.llm_base_url or "",
                    "tool_mode": s.llm_tool_mode, "api_key_set": bool(s.llm_api_key)},
            "data_source": {"kind": s.data_source}, "opening_hours": rules_form()["opening_hours"]}


@router.post("/setup/test-llm", dependencies=[Depends(_setup_only)])
def setup_test_llm(body: LLMIn):
    return test_connection(_llm_cfg(body))


class AdminIn(BaseModel):
    name: str = Field(..., min_length=1, max_length=200)
    email: str = Field(..., max_length=320)
    password: str = Field(..., max_length=200)


class SetupIn(BaseModel):
    clinic: ClinicIn
    llm: LLMIn
    data_source: DataSourceIn
    opening_hours: dict[str, list[str] | None]
    admin: AdminIn


@router.post("/setup/complete")
def setup_complete(body: SetupIn):
    _setup_only()
    email = body.admin.email.strip().lower()
    if "@" not in email:
        raise HTTPException(400, "Enter a valid email address for the admin account.")
    try:
        check_password_strength(body.admin.password)
    except AuthError as e:
        raise HTTPException(400, str(e)) from None

    _save_clinic(body.clinic)
    form = rules_form()
    form["opening_hours"] = body.opening_hours
    save_rules(RulesIn(**form))
    _save_llm(body.llm)
    current = get_settings().data_source
    env = _data_source_env(body.data_source)
    restart = body.data_source.kind != current or (body.data_source.kind == "postgres" and "DATABASE_URL" in env)
    update_env(env)
    reload_all()

    # The admin account goes into the store the clinic will use from now on.
    target = get_store() if not restart else build_store(get_settings())
    try:
        target.init_schema()
    except Exception as e:
        raise HTTPException(400, f"Couldn't open the new data source: {e}") from None
    if target.count_users():
        raise HTTPException(403, "Setup is already complete. Sign in instead.")
    user = target.create_user(email=email, name=body.admin.name.strip(), role="admin",
                              password_hash=hash_password(body.admin.password))
    audit.record("setup.complete", "allowed", actor=email, params={"data_source": body.data_source.kind,
                                                                   "llm_provider": body.llm.provider})
    if restart:
        return {"ok": True, "restart_required": True, "user": public_user(user),
                "message": "Saved. Restart the server so it uses the new data source, then sign in."}
    token, expires = new_session(target, user["id"])
    return {"ok": True, "restart_required": False, "token": token, "expires_at": expires.isoformat(),
            "user": public_user(user)}


# ==========================================================================================
# Settings (admins)
# ==========================================================================================
@router.get("/admin/settings")
def get_all(_: Principal = Depends(require_admin)):
    s = get_settings()
    c = s.clinic
    return {
        "llm": {"provider": s.llm_provider, "model": s.llm_model, "base_url": s.llm_base_url or "",
                "tool_mode": s.llm_tool_mode, "api_key_set": bool(s.llm_api_key)},
        "clinic": {"name": c.name, "phone": c.phone, "email": c.email, "address": c.address, "website": c.website,
                   "timezone": c.timezone, "languages": c.languages, "currency": c.currency,
                   "branding": c.branding.model_dump()},
        "data_source": {"kind": s.data_source, "sqlite_path": str(s.sqlite_path), "excel_path": str(s.excel_path),
                        "google_sheets_id": s.google_sheets_id, "database_url_set": bool(s.database_url)},
        "knowledge": {"embedding_provider": s.embedding_provider, "embedding_model": s.embedding_model or "default",
                      "vector_store": s.kb_vector_store},
    }


@router.post("/admin/settings/llm/test")
def test_llm(body: LLMIn, _: Principal = Depends(require_admin)):
    return test_connection(_llm_cfg(body))


@router.put("/admin/settings/llm")
def put_llm(body: LLMIn, p: Principal = Depends(require_admin)):
    _save_llm(body)
    reload_all()
    audit.record("settings.llm", "allowed", actor=p.label,
                 params={"provider": body.provider, "model": body.model, "key_changed": bool(body.api_key)})
    return {"ok": True}


@router.put("/admin/settings/clinic")
def put_clinic(body: ClinicIn, p: Principal = Depends(require_admin)):
    _save_clinic(body)
    audit.record("settings.clinic", "allowed", actor=p.label, params={"name": body.name})
    return {"ok": True}


@router.post("/admin/settings/logo")
async def upload_logo(file: UploadFile = File(...), p: Principal = Depends(require_admin)):
    ext = LOGO_TYPES.get(file.content_type or "")
    if not ext:
        raise HTTPException(400, "Upload a PNG, JPG, WebP or SVG image.")
    data = await file.read(1024 * 1024 + 1)
    if len(data) > 1024 * 1024:
        raise HTTPException(400, "The logo must be smaller than 1 MB.")
    if ext == "svg" and (b"<script" in data.lower() or b"javascript:" in data.lower()):
        raise HTTPException(400, "This SVG contains scripts; export it again without them or upload a PNG.")
    BRANDING_DIR.mkdir(parents=True, exist_ok=True)
    for old in BRANDING_DIR.glob("logo.*"):
        old.unlink()
    (BRANDING_DIR / f"logo.{ext}").write_bytes(data)
    url = f"/branding/logo.{ext}?v={int(time.time())}"
    update_clinic({"branding": {"logo_url": url}})
    audit.record("settings.logo", "allowed", actor=p.label)
    return {"logo_url": url}


@router.get("/admin/settings/rules")
def get_rules_form(_: Principal = Depends(require_admin)):
    return rules_form()


@router.put("/admin/settings/rules")
def put_rules(body: RulesIn, p: Principal = Depends(require_admin)):
    from ..scheduling import regenerate

    save_rules(body)
    slots = regenerate()
    audit.record("settings.rules", "allowed", actor=p.label, result={"slots_added": slots["added"],
                                                                      "slots_removed": slots["removed"]})
    return {"ok": True, "slots": {"added": slots["added"], "removed": slots["removed"],
                                  "conflicts": [c.get("reference") for c in slots["conflicts"]]}}


@router.put("/admin/settings/data-source")
def put_data_source(body: DataSourceIn, p: Principal = Depends(require_admin)):
    update_env(_data_source_env(body))
    reload_all()
    audit.record("settings.data_source", "allowed", actor=p.label, params={"kind": body.kind})
    return {"ok": True, "restart_required": True,
            "message": "Saved. Restart the server to switch the data source. Staff accounts live in the data store, "
                       "so create them again (or run setup) in the new one."}
