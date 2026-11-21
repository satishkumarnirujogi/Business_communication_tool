"""WhatsApp through Twilio or the Meta WhatsApp Cloud API, using the clinic's own number and credentials.

Inbound: a webhook (app/api/webhooks.py) checks the provider's signature, then hands an `Inbound` to the
router. Outbound: the provider's REST API.

WhatsApp only allows free-form messages within 24 hours of the patient's last message. Reminders are
usually outside that window, so they use an approved message template (WHATSAPP_REMINDER_TEMPLATE for
Meta, TWILIO_REMINDER_CONTENT_SID for Twilio) with four variables: name, date and time, doctor, reference.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import re
from collections.abc import Mapping
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx

from ..config import Settings
from ..data.base import Row
from .base import Channel, ChannelError, Inbound, Reminder, split_text

TIMEOUT = httpx.Timeout(20.0)
WINDOW = timedelta(hours=23, minutes=30)  # a little inside WhatsApp's 24-hour service window
_E164 = re.compile(r"^\+[1-9]\d{6,14}$")


def normalize_number(raw: str) -> str:
    """'whatsapp:+49 30 1234-56' or '4930123456' -> '+4930123456'. Raises ValueError if it isn't a number."""
    digits = re.sub(r"[^\d+]", "", raw.removeprefix("whatsapp:"))
    if digits.startswith("00"):
        digits = "+" + digits[2:]
    if not digits.startswith("+"):
        digits = "+" + digits
    if not _E164.match(digits):
        raise ValueError(f"'{raw}' is not a phone number in international format, e.g. +4930123456")
    return digits


def _in_window(last_inbound_at: datetime | None) -> bool:
    if last_inbound_at is None:
        return False
    if last_inbound_at.tzinfo is None:
        last_inbound_at = last_inbound_at.replace(tzinfo=UTC)
    return datetime.now(UTC) - last_inbound_at < WINDOW


def _http_error(provider: str, r: httpx.Response) -> ChannelError:
    try:
        data = r.json()
    except ValueError:
        data = {}
    detail = (data.get("message") or (data.get("error") or {}).get("message") or r.text or "")[:300]
    if r.status_code in (401, 403):
        hint = "The credentials were rejected. Check the token / auth token in Settings → Channels."
    elif r.status_code == 404:
        hint = "The account, number or phone number ID wasn't found. Check it in Settings → Channels."
    elif r.status_code == 429:
        hint = "The provider is rate limiting. Try again in a minute."
    else:
        hint = "The provider refused the message."
    return ChannelError(f"{provider}: {hint} ({r.status_code}: {detail})")


class TwilioWhatsApp(Channel):
    name, label = "whatsapp", "WhatsApp (Twilio)"
    max_chars = 1600
    API = "https://api.twilio.com/2010-04-01"

    def __init__(self, s: Settings):
        self.sid = s.twilio_account_sid.strip()
        self.token = s.secret("twilio_auth_token") or ""
        self.number = s.twilio_whatsapp_number.strip()
        self.content_sid = s.twilio_reminder_content_sid.strip()

    def problems(self) -> list[str]:
        out = []
        if not self.sid.startswith("AC"):
            out.append("TWILIO_ACCOUNT_SID is missing (it starts with AC).")
        if not self.token:
            out.append("TWILIO_AUTH_TOKEN is missing.")
        try:
            normalize_number(self.number)
        except ValueError:
            out.append("TWILIO_WHATSAPP_NUMBER is missing or not in +4930123456 format.")
        return out

    # -- inbound
    def valid_signature(self, url: str, params: Mapping[str, str], signature: str) -> bool:
        """Twilio: base64(HMAC-SHA1(auth token, full URL + every POST field name and value, sorted by name))."""
        if not self.token or not signature:
            return False
        data = url + "".join(k + params[k] for k in sorted(params))
        expected = base64.b64encode(hmac.new(self.token.encode(), data.encode(), hashlib.sha1).digest()).decode()
        return hmac.compare_digest(expected, signature)

    def parse(self, form: Mapping[str, str]) -> Inbound | None:
        sender = form.get("From", "")
        if not sender.startswith("whatsapp:"):
            return None
        body = (form.get("Body") or "").strip()
        media = int(form.get("NumMedia") or 0)
        return Inbound(channel=self.name, address=normalize_number(sender), text=body,
                       external_id=form.get("MessageSid", ""), display_name=form.get("ProfileName", ""),
                       unsupported=not body and media > 0)

    # -- outbound
    def _post(self, data: dict[str, str]) -> str:
        self.require_ready()
        data = {"From": f"whatsapp:{normalize_number(self.number)}", **data}
        try:
            r = httpx.post(f"{self.API}/Accounts/{self.sid}/Messages.json", data=data,
                           auth=(self.sid, self.token), timeout=TIMEOUT)
        except httpx.HTTPError as e:
            raise ChannelError(f"Twilio could not be reached ({type(e).__name__}). Check the internet connection.") from None
        if r.status_code >= 300:
            raise _http_error("Twilio", r)
        return r.json().get("sid", "")

    def send(self, thread: Row, text: str) -> str:
        ids = [self._post({"To": f"whatsapp:{thread['address']}", "Body": part})
               for part in split_text(text, self.max_chars)]
        return ids[-1] if ids else ""

    def send_reminder(self, address: str, reminder: Reminder, *, last_inbound_at: datetime | None) -> str:
        if _in_window(last_inbound_at):
            return self.send({"address": address}, reminder.text)
        if not self.content_sid:
            raise ChannelError(
                "WhatsApp only allows a template message more than 24 hours after the patient's last message. "
                "Create and approve a reminder template in Twilio and set TWILIO_REMINDER_CONTENT_SID.")
        variables = {str(i + 1): v for i, v in enumerate(reminder.params)}
        return self._post({"To": f"whatsapp:{address}", "ContentSid": self.content_sid,
                           "ContentVariables": json.dumps(variables)})


class MetaWhatsApp(Channel):
    name, label = "whatsapp", "WhatsApp (Meta Cloud API)"
    max_chars = 4000
    API = "https://graph.facebook.com"

    def __init__(self, s: Settings):
        self.token = s.secret("whatsapp_access_token") or ""
        self.phone_id = s.whatsapp_phone_number_id.strip()
        self.app_secret = s.secret("whatsapp_app_secret") or ""
        self.verify_token = s.secret("whatsapp_verify_token") or ""
        self.version = s.whatsapp_graph_version.strip() or "v23.0"
        self.template = s.whatsapp_reminder_template.strip()

    def problems(self) -> list[str]:
        out = []
        if not self.token:
            out.append("WHATSAPP_ACCESS_TOKEN is missing.")
        if not self.phone_id.isdigit():
            out.append("WHATSAPP_PHONE_NUMBER_ID is missing (a number from the Meta app's WhatsApp settings).")
        if not self.app_secret:
            out.append("WHATSAPP_APP_SECRET is missing (needed to check that webhooks really come from Meta).")
        if not self.verify_token:
            out.append("WHATSAPP_VERIFY_TOKEN is missing (any long random text; enter the same in Meta).")
        return out

    # -- inbound
    def valid_signature(self, body: bytes, header: str) -> bool:
        """Meta: X-Hub-Signature-256 = 'sha256=' + HMAC-SHA256(app secret, raw request body)."""
        if not self.app_secret or not header.startswith("sha256="):
            return False
        expected = hmac.new(self.app_secret.encode(), body, hashlib.sha256).hexdigest()
        return hmac.compare_digest(expected, header.removeprefix("sha256="))

    def verify_subscription(self, mode: str, token: str) -> bool:
        return mode == "subscribe" and bool(self.verify_token) and hmac.compare_digest(token, self.verify_token)

    def parse(self, payload: dict[str, Any]) -> list[Inbound]:
        out: list[Inbound] = []
        for entry in payload.get("entry") or []:
            for change in entry.get("changes") or []:
                value = change.get("value") or {}
                names = {c.get("wa_id"): (c.get("profile") or {}).get("name", "") for c in value.get("contacts") or []}
                for m in value.get("messages") or []:  # "statuses" (delivered/read) are ignored
                    kind = m.get("type")
                    if kind == "text":
                        text = (m.get("text") or {}).get("body", "")
                    elif kind == "button":
                        text = (m.get("button") or {}).get("text", "")
                    elif kind == "interactive":
                        i = m.get("interactive") or {}
                        text = (i.get("button_reply") or i.get("list_reply") or {}).get("title", "")
                    else:
                        text = ""
                    try:
                        address = normalize_number(m.get("from", ""))
                    except ValueError:
                        continue
                    out.append(Inbound(channel=self.name, address=address, text=text.strip(),
                                       external_id=m.get("id", ""), display_name=names.get(m.get("from"), ""),
                                       unsupported=not text.strip()))
        return out

    # -- outbound
    def _post(self, body: dict[str, Any]) -> str:
        self.require_ready()
        try:
            r = httpx.post(f"{self.API}/{self.version}/{self.phone_id}/messages", json=body,
                           headers={"Authorization": f"Bearer {self.token}"}, timeout=TIMEOUT)
        except httpx.HTTPError as e:
            raise ChannelError(f"Meta could not be reached ({type(e).__name__}). Check the internet connection.") from None
        if r.status_code >= 300:
            err = _http_error("WhatsApp (Meta)", r)
            if "131047" in r.text or "re-engagement" in r.text.lower():
                err = ChannelError("WhatsApp (Meta): more than 24 hours have passed since the patient's last "
                                   "message, so only an approved template can be sent.")
            raise err
        return ((r.json().get("messages") or [{}])[0]).get("id", "")

    def send(self, thread: Row, text: str) -> str:
        to = thread["address"].lstrip("+")
        ids = [self._post({"messaging_product": "whatsapp", "to": to, "type": "text",
                           "text": {"body": part, "preview_url": False}})
               for part in split_text(text, self.max_chars)]
        return ids[-1] if ids else ""

    def send_reminder(self, address: str, reminder: Reminder, *, last_inbound_at: datetime | None) -> str:
        if _in_window(last_inbound_at):
            return self.send({"address": address}, reminder.text)
        if not self.template:
            raise ChannelError(
                "WhatsApp only allows a template message more than 24 hours after the patient's last message. "
                "Create and approve a reminder template in Meta and set WHATSAPP_REMINDER_TEMPLATE.")
        return self._post({
            "messaging_product": "whatsapp", "to": address.lstrip("+"), "type": "template",
            "template": {"name": self.template, "language": {"code": reminder.language},
                         "components": [{"type": "body", "parameters": [
                             {"type": "text", "text": p} for p in reminder.params]}]},
        })
