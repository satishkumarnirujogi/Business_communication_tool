"""Inbound webhooks for WhatsApp. Every request is checked against the provider's signature before anything
is read; the reply is sent from a background task so the provider gets its 200 straight away (both
providers retry, or give up, if a webhook takes more than a few seconds)."""

from __future__ import annotations

import json
import logging

from fastapi import APIRouter, BackgroundTasks, HTTPException, Request
from fastapi.responses import PlainTextResponse, Response

from .. import audit
from ..channels import MetaWhatsApp, TwilioWhatsApp, whatsapp_channel
from ..channels.router import receive
from ..config import get_settings

router = APIRouter(prefix="/webhooks", tags=["webhooks"], include_in_schema=False)
log = logging.getLogger("clinic.webhooks")


def _public_url(request: Request) -> str:
    """The URL the provider called. Behind a proxy request.url is the internal one, so PUBLIC_BASE_URL wins."""
    base = get_settings().public_base_url
    if not base:
        return str(request.url)
    query = f"?{request.url.query}" if request.url.query else ""
    return f"{base}{request.url.path}{query}"


def _refuse(provider: str, why: str) -> HTTPException:
    log.warning("%s webhook refused: %s", provider, why)
    audit.record("webhook", "denied", actor=provider, rule="webhook.signature", reason=why)
    return HTTPException(403, "Signature check failed.")


def _handle(inbound) -> None:
    try:
        receive(inbound)
    except Exception:
        log.exception("could not handle a WhatsApp message")


@router.post("/whatsapp/twilio")
async def twilio_inbound(request: Request, background: BackgroundTasks):
    ch = whatsapp_channel()
    if not isinstance(ch, TwilioWhatsApp) or not ch.configured:
        raise HTTPException(404, "WhatsApp via Twilio is not set up.")
    form = {k: str(v) for k, v in (await request.form()).items()}
    if not ch.valid_signature(_public_url(request), form, request.headers.get("X-Twilio-Signature", "")):
        raise _refuse("twilio", "X-Twilio-Signature does not match (check TWILIO_AUTH_TOKEN and PUBLIC_BASE_URL)")
    try:
        inbound = ch.parse(form)
    except ValueError as e:
        log.warning("twilio: %s", e)
        inbound = None
    if inbound:
        background.add_task(_handle, inbound)
    return Response("<Response/>", media_type="text/xml")


@router.get("/whatsapp/meta")
def meta_verify(request: Request):
    """Meta calls this once when you save the webhook URL in the app dashboard."""
    ch = whatsapp_channel()
    q = request.query_params
    if isinstance(ch, MetaWhatsApp) and ch.verify_subscription(q.get("hub.mode", ""), q.get("hub.verify_token", "")):
        return PlainTextResponse(q.get("hub.challenge", ""))
    raise _refuse("meta", "verify token does not match WHATSAPP_VERIFY_TOKEN")


@router.post("/whatsapp/meta")
async def meta_inbound(request: Request, background: BackgroundTasks):
    ch = whatsapp_channel()
    if not isinstance(ch, MetaWhatsApp) or not ch.configured:
        raise HTTPException(404, "WhatsApp via Meta is not set up.")
    body = await request.body()
    if not ch.valid_signature(body, request.headers.get("X-Hub-Signature-256", "")):
        raise _refuse("meta", "X-Hub-Signature-256 does not match (check WHATSAPP_APP_SECRET)")
    try:
        payload = json.loads(body or b"{}")
    except ValueError:
        raise HTTPException(400, "Body is not JSON.") from None
    for inbound in ch.parse(payload):
        background.add_task(_handle, inbound)
    return {"ok": True}
