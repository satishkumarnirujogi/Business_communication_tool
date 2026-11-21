"""Phase 7: channel adapters (WhatsApp via Twilio/Meta, email), the channel router, webhooks, reminders."""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
from datetime import UTC, timedelta
from email.message import EmailMessage

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.agent.tools import run_tool
from app.api import webhooks
from app.channels import ChannelError, EmailChannel, MetaWhatsApp, TwilioWhatsApp, reminders
from app.channels import router as channel_router
from app.channels.base import Inbound, split_text
from app.channels.mail import parse_message, reply_subject, strip_quoted
from app.channels.whatsapp import normalize_number
from app.config import reload_settings

from .conftest import bookable_slot

TWILIO = {"WHATSAPP_PROVIDER": "twilio", "TWILIO_ACCOUNT_SID": "AC" + "0" * 32, "TWILIO_AUTH_TOKEN": "test-token",
          "TWILIO_WHATSAPP_NUMBER": "+4930000000", "PUBLIC_BASE_URL": "https://assistant.example"}
META = {"WHATSAPP_PROVIDER": "meta", "WHATSAPP_ACCESS_TOKEN": "meta-token", "WHATSAPP_PHONE_NUMBER_ID": "123456",
        "WHATSAPP_APP_SECRET": "app-secret", "WHATSAPP_VERIFY_TOKEN": "verify-me"}
EMAIL = {"EMAIL_ENABLED": "true", "EMAIL_ADDRESS": "desk@clinic.example", "SMTP_HOST": "smtp.example",
         "IMAP_HOST": "imap.example", "IMAP_PASSWORD": "pw"}


@pytest.fixture
def env(monkeypatch):
    """env(**vars): apply settings for one test (real env vars win over .env), restored afterwards."""
    def apply(values: dict[str, str]):
        for k, v in values.items():
            monkeypatch.setenv(k, v)
        return reload_settings()
    monkeypatch.setenv("WHATSAPP_PROVIDER", "none")
    monkeypatch.setenv("EMAIL_ENABLED", "false")
    reload_settings()
    channel_router.reset_limits()
    yield apply
    monkeypatch.undo()
    reload_settings()


@pytest.fixture
def outbox(monkeypatch):
    """Capture everything the channels would send."""
    sent: list[dict] = []
    monkeypatch.setattr(TwilioWhatsApp, "_post", lambda self, data: sent.append({"via": "twilio", **data}) or "SM1")
    monkeypatch.setattr(MetaWhatsApp, "_post", lambda self, body: sent.append({"via": "meta", **body}) or "wamid.1")
    monkeypatch.setattr(EmailChannel, "_send", lambda self, to, subject, text, in_reply_to="": sent.append(
        {"via": "email", "to": to, "subject": subject, "text": text, "in_reply_to": in_reply_to}) or "<m@x>")
    return sent


# ---- adapters ------------------------------------------------------------------------------
def test_normalize_number():
    assert normalize_number("whatsapp:+49 30 1234-56") == "+4930123456"
    assert normalize_number("0049301234567") == "+49301234567"
    assert normalize_number("4930123456") == "+4930123456"
    with pytest.raises(ValueError):
        normalize_number("hello")


def test_split_text_respects_limit():
    parts = split_text("First paragraph.\n\n" + "word " * 500, 300)
    assert all(len(p) <= 300 for p in parts) and len(parts) == 9
    assert parts[0].startswith("First paragraph.") and all(w == "word" for p in parts for w in p.split()[2 if p is parts[0] else 0:])


def _twilio_sig(token: str, url: str, params: dict) -> str:
    data = url + "".join(k + params[k] for k in sorted(params))
    return base64.b64encode(hmac.new(token.encode(), data.encode(), hashlib.sha1).digest()).decode()


def test_twilio_signature_and_parse(env):
    ch = TwilioWhatsApp(env(TWILIO))
    url = "https://assistant.example/webhooks/whatsapp/twilio"
    form = {"From": "whatsapp:+491701234567", "Body": "Hi", "MessageSid": "SM9", "ProfileName": "Lena"}
    sig = _twilio_sig("test-token", url, form)
    assert ch.valid_signature(url, form, sig)
    assert not ch.valid_signature(url, {**form, "Body": "changed"}, sig)
    assert not ch.valid_signature(url, form, "")
    msg = ch.parse(form)
    assert (msg.address, msg.text, msg.external_id, msg.display_name) == ("+491701234567", "Hi", "SM9", "Lena")
    assert ch.parse({**form, "Body": "", "NumMedia": "1"}).unsupported
    assert ch.parse({"From": "+4917", "Body": "SMS"}) is None  # plain SMS isn't WhatsApp


def test_twilio_problems_are_readable(env):
    problems = TwilioWhatsApp(env({"WHATSAPP_PROVIDER": "twilio"})).problems()
    assert any("TWILIO_ACCOUNT_SID" in p for p in problems) and any("TWILIO_AUTH_TOKEN" in p for p in problems)
    with pytest.raises(ChannelError, match="not set up"):
        TwilioWhatsApp(env({"WHATSAPP_PROVIDER": "twilio"})).require_ready()


META_PAYLOAD = {"entry": [{"changes": [{"value": {
    "contacts": [{"wa_id": "491701234567", "profile": {"name": "Tom"}}],
    "messages": [
        {"from": "491701234567", "id": "wamid.A", "type": "text", "text": {"body": "Termin morgen?"}},
        {"from": "491701234567", "id": "wamid.B", "type": "interactive",
         "interactive": {"button_reply": {"title": "Ja"}}},
        {"from": "491701234567", "id": "wamid.C", "type": "image", "image": {}},
    ],
    "statuses": [{"id": "wamid.X", "status": "read"}],
}}]}]}


def test_meta_signature_verify_and_parse(env):
    ch = MetaWhatsApp(env(META))
    body = json.dumps(META_PAYLOAD).encode()
    good = "sha256=" + hmac.new(b"app-secret", body, hashlib.sha256).hexdigest()
    assert ch.valid_signature(body, good)
    assert not ch.valid_signature(body + b" ", good)
    assert not ch.valid_signature(body, "sha1=abc")
    assert ch.verify_subscription("subscribe", "verify-me") and not ch.verify_subscription("subscribe", "nope")
    msgs = ch.parse(META_PAYLOAD)
    assert [m.text for m in msgs] == ["Termin morgen?", "Ja", ""]
    assert msgs[0].address == "+491701234567" and msgs[0].display_name == "Tom"
    assert msgs[2].unsupported


def test_whatsapp_reminder_needs_template_outside_24h(env, outbox):
    ch = MetaWhatsApp(env(META))
    r = reminders.Reminder(text="Reminder", subject="", language="de", params=["Tom", "Fr", "Dr. A", "REF1"])
    with pytest.raises(ChannelError, match="template"):
        ch.send_reminder("+491701234567", r, last_inbound_at=None)
    ch = MetaWhatsApp(env({**META, "WHATSAPP_REMINDER_TEMPLATE": "appointment_reminder"}))
    ch.send_reminder("+491701234567", r, last_inbound_at=None)
    assert outbox[-1]["type"] == "template" and outbox[-1]["template"]["language"]["code"] == "de"
    assert [p["text"] for p in outbox[-1]["template"]["components"][0]["parameters"]] == r.params
    from datetime import datetime
    ch.send_reminder("+491701234567", r, last_inbound_at=datetime.now(UTC) - timedelta(hours=2))
    assert outbox[-1]["type"] == "text"  # inside the 24-hour window a normal message is fine


# ---- email ----------------------------------------------------------------------------------
def _mail(**headers) -> bytes:
    m = EmailMessage()
    m["From"] = headers.pop("From", "Lena Test <Lena@Example.com>")
    m["To"] = "desk@clinic.example"
    m["Subject"] = headers.pop("Subject", "Appointment")
    m["Message-ID"] = headers.pop("Message_ID", "<abc@example.com>")
    for k, v in headers.items():
        m[k.replace("_", "-")] = v
    m.set_content(headers.get("body", "Yes, please book it.\n\nOn Tue, 3 Nov 2026, Clinic <desk@clinic.example> wrote:\n> Shall I book?"))
    return m.as_bytes()


def test_strip_quoted_keeps_only_the_new_text():
    assert strip_quoted("Yes please\n\nOn Mon, 2 Nov 2026 at 10:00, Clinic wrote:\n> old") == "Yes please"
    assert strip_quoted("Ja bitte\nAm 02.11.2026 um 10:00 schrieb Praxis:\n> alt") == "Ja bitte"
    assert strip_quoted("Hi\n-----Original Message-----\nFrom: x") == "Hi"
    assert strip_quoted("Thanks\n--\nLena\n+49 170") == "Thanks"


def test_parse_message_and_loop_protection():
    inbound, why = parse_message(_mail(), "desk@clinic.example")
    assert why == "" and inbound.address == "lena@example.com" and inbound.text == "Yes, please book it."
    assert inbound.external_id == "<abc@example.com>" and inbound.display_name == "Lena Test"
    for headers in ({"Auto_Submitted": "auto-replied"}, {"Precedence": "bulk"}, {"List_Id": "<news.example>"},
                    {"From": "MAILER-DAEMON@mx.example"}, {"From": "noreply@shop.example"},
                    {"From": "desk@clinic.example"}):
        inbound, why = parse_message(_mail(**headers), "desk@clinic.example")
        assert inbound is None and why, headers


def test_reply_subject():
    assert reply_subject("Appointment", "x") == "Re: Appointment"
    assert reply_subject("AW: Termin", "x") == "AW: Termin"
    assert reply_subject("", "Your message") == "Re: Your message"


def test_email_problems(env):
    assert "EMAIL_ENABLED is off." in EmailChannel(env({})).problems()
    assert EmailChannel(env(EMAIL)).configured


# ---- router: inbound -> engine -> reply ---------------------------------------------------------
@pytest.fixture
def fake_engine(monkeypatch):
    calls = []

    def handle(sid, text, *, channel="web", **_):
        calls.append((sid, text, channel))
        return {"reply": f"echo: {text}", "session_id": sid}

    monkeypatch.setattr("app.agent.engine.handle_message", handle)
    return calls


def test_router_threads_dedupes_and_replies(env, outbox, fake_engine, sqlite_store):
    env(TWILIO)
    a = Inbound(channel="whatsapp", address="+491701234567", text="Hallo", external_id="SM1", display_name="Tom")
    assert channel_router.receive(a) == "echo: Hallo"
    assert outbox[-1]["To"] == "whatsapp:+491701234567" and outbox[-1]["Body"] == "echo: Hallo"
    assert channel_router.receive(a) is None  # provider retry: ignored
    channel_router.receive(Inbound(channel="whatsapp", address="+491701234567", text="Noch was", external_id="SM2"))
    assert fake_engine[0][0] == fake_engine[1][0]  # same sender, same conversation
    assert fake_engine[0][2] == "whatsapp"
    conv = sqlite_store.get_conversation(fake_engine[0][0])
    assert conv["channel"] == "whatsapp" and conv["patient_name"] == "Tom"
    channel_router.receive(Inbound(channel="whatsapp", address="+4917099999", text="Hi", external_id="SM3"))
    assert fake_engine[2][0] != fake_engine[0][0]


def test_router_text_only_and_rate_limit(env, outbox, fake_engine, sqlite_store, monkeypatch):
    env(TWILIO)
    channel_router.receive(Inbound(channel="whatsapp", address="+4917011111", text="", external_id="M0",
                                   unsupported=True))
    assert "only read text" in outbox[-1]["Body"] and not fake_engine
    monkeypatch.setattr(channel_router, "MAX_PER_HOUR", 3)
    for i in range(6):
        channel_router.receive(Inbound(channel="whatsapp", address="+4917022222", text=f"m{i}", external_id=f"R{i}"))
    assert len(fake_engine) == 3
    assert sum("a lot of messages" in m.get("Body", "") for m in outbox) == 1  # told once, then ignored


def test_email_reply_threads_subject(env, outbox, fake_engine, sqlite_store):
    env(EMAIL)
    inbound, _ = parse_message(_mail(), "desk@clinic.example")
    channel_router.receive(inbound)
    assert outbox[-1]["to"] == "lena@example.com" and outbox[-1]["subject"] == "Re: Appointment"
    assert outbox[-1]["in_reply_to"] == "<abc@example.com>"


def test_staff_reply_is_delivered_to_whatsapp(env, outbox, fake_engine, sqlite_store):
    from app.agent.engine import staff_message

    env(TWILIO)
    channel_router.receive(Inbound(channel="whatsapp", address="+491701234567", text="Hallo", external_id="S1"))
    sid = fake_engine[0][0]
    msg = staff_message(sid, "Hier ist das Praxisteam.", "Anna")
    assert msg["delivery_error"] is None and outbox[-1]["Body"] == "Hier ist das Praxisteam."
    env({"WHATSAPP_PROVIDER": "none"})
    assert "no longer set up" in staff_message(sid, "Noch da?", "Anna")["delivery_error"]


# ---- webhooks -------------------------------------------------------------------------------------
@pytest.fixture
def hooks(monkeypatch):
    got = []
    monkeypatch.setattr(webhooks, "receive", lambda m: got.append(m))
    app = FastAPI()
    app.include_router(webhooks.router)
    return TestClient(app), got


def test_twilio_webhook_checks_signature(env, hooks, sqlite_store):
    env(TWILIO)
    client, got = hooks
    form = {"From": "whatsapp:+491701234567", "Body": "Hi", "MessageSid": "SM5"}
    url = "https://assistant.example/webhooks/whatsapp/twilio"
    assert client.post("/webhooks/whatsapp/twilio", data=form, headers={"X-Twilio-Signature": "bad"}).status_code == 403
    assert not got
    r = client.post("/webhooks/whatsapp/twilio", data=form, headers={"X-Twilio-Signature": _twilio_sig("test-token", url, form)})
    assert r.status_code == 200 and r.text == "<Response/>"
    assert got[0].text == "Hi"


def test_meta_webhook_verify_and_signature(env, hooks, sqlite_store):
    env(META)
    client, got = hooks
    ok = client.get("/webhooks/whatsapp/meta", params={"hub.mode": "subscribe", "hub.verify_token": "verify-me",
                                                        "hub.challenge": "42"})
    assert ok.status_code == 200 and ok.text == "42"
    assert client.get("/webhooks/whatsapp/meta", params={"hub.mode": "subscribe", "hub.verify_token": "x"}).status_code == 403
    body = json.dumps(META_PAYLOAD).encode()
    assert client.post("/webhooks/whatsapp/meta", content=body, headers={"X-Hub-Signature-256": "sha256=00"}).status_code == 403
    sig = "sha256=" + hmac.new(b"app-secret", body, hashlib.sha256).hexdigest()
    assert client.post("/webhooks/whatsapp/meta", content=body, headers={"X-Hub-Signature-256": sig}).status_code == 200
    assert len(got) == 3


def test_webhook_404_when_channel_off(env, hooks):
    client, _ = hooks
    assert client.post("/webhooks/whatsapp/twilio", data={"Body": "x"}).status_code == 404
    assert client.post("/webhooks/whatsapp/meta", content=b"{}").status_code == 404


# ---- reminders -----------------------------------------------------------------------------------
def _book(store, email="rem@example.com"):
    slot = bookable_slot(store)
    payload, err = run_tool("book_appointment", {"slot_id": slot["id"], "full_name": "Rita Reminder", "email": email,
                                                 "phone": "+491701234567", "reason": "check-up"}, "rem-session")
    assert payload.get("ok"), payload
    return payload["reference"], store.appointment_detail(payload["reference"])


def _state(store, ref, appt):
    """This booking's reminder row (demo appointments in the same window are ignored)."""
    start = appt["slot_start"]
    return next(r for r in store.reminder_candidates(start - timedelta(minutes=1), start + timedelta(minutes=1))
                if r["reference"] == ref)


def test_reminder_goes_to_whatsapp_when_booked_there(env, outbox, sqlite_store):
    env({**TWILIO, "TWILIO_REMINDER_CONTENT_SID": "HX123"})
    thread = sqlite_store.channel_thread("whatsapp", "+491701234567", display_name="Rita")
    import sqlalchemy as sa

    from app.data.models import channel_threads
    with sqlite_store.engine.begin() as c:  # the patient last wrote two days ago: the 24-hour window is closed
        c.execute(sa.update(channel_threads).values(last_inbound_at=thread["last_inbound_at"] - timedelta(days=2)))
    ref, appt = _book(sqlite_store)
    sqlite_store.set_reminder_source(ref, channel="whatsapp", conversation_id=thread["conversation_id"], language="de")
    due = reminders.due_at(appt["slot_start"])
    reminders.run_once(due - timedelta(minutes=5))
    assert _state(sqlite_store, ref, appt)["r_status"] == "pending"  # not yet
    reminders.run_once(due + timedelta(minutes=1))
    row = _state(sqlite_store, ref, appt)
    assert (row["r_status"], row["r_sent_via"]) == ("sent", "whatsapp")
    outbox[:] = [m for m in outbox if m.get("via") == "twilio"]
    assert outbox[-1]["ContentSid"] == "HX123"  # last message was long ago: template
    variables = json.loads(outbox[-1]["ContentVariables"])
    assert variables["4"] == ref and "um" in variables["2"]  # German date wording
    before = len(outbox)
    reminders.run_once(due + timedelta(minutes=10))
    assert len(outbox) == before  # only once


def test_web_booking_reminder_falls_back_to_email_or_is_skipped(env, outbox, sqlite_store):
    env({})
    ref, appt = _book(sqlite_store)
    sqlite_store.set_reminder_source(ref, channel="web", conversation_id="web-1", language="en")
    due = reminders.due_at(appt["slot_start"])
    reminders.run_once(due + timedelta(minutes=1))
    row = _state(sqlite_store, ref, appt)
    assert row["r_status"] == "skipped" and "email sending" in row["r_error"]

    # rescheduling changes the start time, so a new reminder is due; now email is on
    env(EMAIL)
    ref2, appt2 = _book(sqlite_store, "rem2@example.com")
    due2 = reminders.due_at(appt2["slot_start"])
    reminders.run_once(due2 + timedelta(minutes=1))
    assert _state(sqlite_store, ref2, appt2)["r_status"] == "sent"
    mail = next(m for m in outbox if m.get("to") == "rem2@example.com")
    assert mail["subject"].startswith("Reminder: your appointment on") and ref2 in mail["text"]


def test_reminder_failure_retries_then_creates_task(env, sqlite_store, monkeypatch):
    env(EMAIL)

    def boom(self, *a, **k):
        raise ChannelError("Email: the mail server rejected the SMTP user name or password.")

    monkeypatch.setattr(EmailChannel, "_send", boom)
    ref, appt = _book(sqlite_store)
    due = reminders.due_at(appt["slot_start"])
    attempts = []
    for minutes in (1, 5, 40, 80, 120, 160):  # retries 30 minutes apart, three attempts at most
        reminders.run_once(due + timedelta(minutes=minutes))
        attempts.append(_state(sqlite_store, ref, appt)["r_attempts"])
    assert attempts == [1, 1, 2, 3, 3, 3]
    assert _state(sqlite_store, ref, appt)["r_status"] == "failed"
    tasks = [t for t in sqlite_store.list_tasks() if ref in t["summary"]]
    assert len(tasks) == 1 and "SMTP" in tasks[0]["summary"]


def test_reminder_skipped_when_booked_too_late(env, outbox, sqlite_store):
    env(EMAIL)
    ref, appt = _book(sqlite_store)
    late = appt["slot_start"] - timedelta(hours=1)
    reminders.run_once(late)
    assert _state(sqlite_store, ref, appt)["r_status"] == "skipped"
    assert not [m for m in outbox if ref in m.get("text", "")]
