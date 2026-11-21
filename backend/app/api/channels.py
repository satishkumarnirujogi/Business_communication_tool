"""Settings → Channels: WhatsApp, email and reminders, plus a test message and the reminder list."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any, cast

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field, ValidationError

from .. import audit
from ..auth import Principal
from ..channels import ChannelError, EmailChannel, Reminder, get_channel, whatsapp_channel
from ..channels.reminders import due_at, run_once
from ..channels.whatsapp import normalize_number
from ..config import Settings, _format, get_settings
from ..config_files import reload_all, update_env
from ..data import get_store
from .deps import require_admin, require_staff

router = APIRouter(prefix="/api/admin", tags=["channels"])

PLAIN = ["public_base_url", "whatsapp_provider", "twilio_account_sid", "twilio_whatsapp_number",
         "twilio_reminder_content_sid", "whatsapp_phone_number_id", "whatsapp_graph_version",
         "whatsapp_reminder_template", "email_enabled", "email_address", "email_display_name", "imap_host",
         "imap_port", "imap_user", "imap_folder", "smtp_host", "smtp_port", "smtp_user", "smtp_security",
         "email_poll_seconds", "reminders_enabled", "reminder_send_time", "reminder_min_hours"]
SECRETS = ["twilio_auth_token", "whatsapp_access_token", "whatsapp_app_secret", "whatsapp_verify_token",
           "imap_password", "smtp_password"]


class ChannelsIn(BaseModel):
    """Only the fields that are sent are changed. Secrets: empty = keep the saved value."""
    values: dict[str, str | int | float | bool | None] = Field(default_factory=dict)


class TestIn(BaseModel):
    channel: str = Field(..., pattern="^(whatsapp|email)$")
    to: str = Field(..., min_length=3, max_length=320)


def _status() -> dict:
    s = get_settings()
    base = s.public_base_url
    wa = whatsapp_channel()
    mail = EmailChannel(s)
    hook = {"twilio": "/webhooks/whatsapp/twilio", "meta": "/webhooks/whatsapp/meta"}.get(s.whatsapp_provider, "")
    return {
        "public_base_url": base,
        "whatsapp": {
            "provider": s.whatsapp_provider, "label": wa.label if wa else "Off",
            "configured": bool(wa and wa.configured), "problems": wa.problems() if wa else [],
            "webhook_url": f"{base}{hook}" if hook and base else hook,
            "reminder_template_set": bool(s.twilio_reminder_content_sid if s.whatsapp_provider == "twilio"
                                          else s.whatsapp_reminder_template),
        },
        "email": {"enabled": s.email_enabled, "configured": mail.configured, "can_send": mail.can_send(),
                  "problems": mail.problems()},
        "reminders": {"enabled": s.reminders_enabled, "send_time": s.reminder_send_time,
                      "min_hours": s.reminder_min_hours},
        "values": {k: getattr(s, k) for k in PLAIN},
        "secrets_set": {k: bool(getattr(s, k)) for k in SECRETS},
    }


@router.get("/channels")
def channels_status(_: Principal = Depends(require_admin)):
    return _status()


@router.put("/settings/channels")
def put_channels(body: ChannelsIn, p: Principal = Depends(require_admin)):
    unknown = set(body.values) - set(PLAIN) - set(SECRETS)
    if unknown:
        raise HTTPException(400, f"Unknown setting(s): {', '.join(sorted(unknown))}.")
    changes = {k: v for k, v in body.values.items() if not (k in SECRETS and (v is None or v == ""))}
    try:
        Settings(**cast(dict[str, Any], changes))  # validate on top of the current .env before writing anything
    except ValidationError as e:
        raise HTTPException(400, _format(e, "the channel settings", env_names=True)) from None
    env = {k.upper(): ("" if v is None else str(v).lower() if isinstance(v, bool) else str(v)) for k, v in changes.items()}
    update_env(env)
    reload_all()
    audit.record("settings.channels", "allowed", actor=p.label,
                 params={"changed": sorted(k for k in changes if k not in SECRETS),
                         "secrets_changed": sorted(k for k in changes if k in SECRETS)})
    return _status()


@router.post("/channels/test")
def test_message(body: TestIn, p: Principal = Depends(require_admin)):
    """Send a short test message. WhatsApp: only works if that number wrote to the clinic in the last 24 hours
    (or use the provider's sandbox), because WhatsApp blocks other free-form messages."""
    ch = get_channel(body.channel)
    clinic = get_settings().clinic.name
    text = f"Test message from the {clinic} assistant. If you can read this, the channel works."
    try:
        if body.channel == "whatsapp":
            if ch is None:
                raise ChannelError("WhatsApp is off. Choose Twilio or Meta first.")
            try:
                to = normalize_number(body.to)
            except ValueError as e:
                raise HTTPException(400, str(e)) from None
            ch.require_ready()
            ch.send({"address": to}, text)
        else:
            assert isinstance(ch, EmailChannel)
            if "@" not in body.to:
                raise HTTPException(400, "Enter an email address.")
            ch.send_reminder(body.to.strip(), Reminder(text=text, subject=f"Test from {clinic}", language="en"))
    except ChannelError as e:
        audit.record("channel.test", "error", actor=p.label, reason=str(e), params={"channel": body.channel})
        raise HTTPException(502, str(e)) from None
    audit.record("channel.test", "allowed", actor=p.label, params={"channel": body.channel, "to": body.to})
    return {"ok": True}


@router.get("/reminders")
def upcoming_reminders(days: int = 3, _: Principal = Depends(require_staff)):
    now = datetime.now(UTC)
    rows = get_store().reminder_candidates(now, now + timedelta(days=max(1, min(days, 14))))
    out = []
    for a in rows:
        start = a["slot_start"] if a["slot_start"].tzinfo else a["slot_start"].replace(tzinfo=UTC)
        current = a.get("r_sent_for") is not None and (
            a["r_sent_for"] if a["r_sent_for"].tzinfo else a["r_sent_for"].replace(tzinfo=UTC)) == start
        out.append({
            "reference": a["reference"], "patient": a["patient"], "doctor": a["doctor"], "service": a.get("service"),
            "start": start, "booked_via": a.get("r_channel") or "staff", "due_at": due_at(start),
            "status": (a.get("r_status") if current else None) or "pending",
            "sent_via": a.get("r_sent_via") if current else "", "error": a.get("r_error") if current else "",
            "attempts": a.get("r_attempts") if current else 0,
        })
    return {"enabled": get_settings().reminders_enabled, "reminders": out}


@router.post("/reminders/run")
def run_reminders(p: Principal = Depends(require_admin)):
    counts = run_once()
    audit.record("reminders.run", "allowed", actor=p.label, params=counts)
    return counts
