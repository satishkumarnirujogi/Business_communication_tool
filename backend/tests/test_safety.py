"""Phase 5: emergencies, enforced confirmations, reschedule, directory tools, language, PII masking."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace as NS

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.agent import engine
from app.agent.language import detect, is_confirmation
from app.agent.tools import run_tool
from app.api import admin
from app.api.deps import current_principal
from app.auth import TOKEN_PRINCIPAL
from app.llm.provider import LLMConfig
from app.privacy import mask, mask_text
from app.rules import get_rules

TZ = get_rules().tz


# ---- fake LLM driven by a script of callables ------------------------------------
def _resp(content=None, calls=None):
    tool_calls = [NS(id=f"c{i}", function=NS(name=n, arguments=json.dumps(a))) for i, (n, a) in enumerate(calls or [])]
    return NS(choices=[NS(message=NS(content=content, tool_calls=tool_calls or None))])


def _last_tool_payload(messages):
    return json.loads(next(m for m in reversed(messages) if m["role"] == "tool")["content"])


@pytest.fixture
def llm(monkeypatch, sqlite_store):
    """`llm.script` is a list of functions(messages) -> response, consumed one per LLM call."""
    box = NS(script=[], calls=[])

    def fake_complete(messages, *, tools=None, cfg=None, **kw):
        box.calls.append([dict(m) for m in messages])
        if not box.script:
            raise AssertionError("LLM called unexpectedly")
        return box.script.pop(0)(messages)

    monkeypatch.setattr(engine, "complete", fake_complete)
    monkeypatch.setattr(engine.LLMConfig, "from_settings", classmethod(lambda cls, s=None: LLMConfig("fake", "m", "k")))
    engine._sessions.clear()
    return box


def _bookable(sqlite_store):
    rules = get_rules()
    now = datetime.now(UTC)
    for s in sqlite_store.open_slots(now + timedelta(days=4), now + timedelta(days=12), limit=2000):
        local = s["start_at"].astimezone(TZ)
        if local.minute == 0 and rules.slot_bookable(s["start_at"], s["start_at"] + timedelta(minutes=30), now).allowed:
            return s
    raise AssertionError("no slot")


# ---- emergencies -------------------------------------------------------------------
@pytest.mark.parametrize("text,lang,category,needle", [
    ("I have chest pain right now", "en", "urgent", "112"),
    ("Ich habe starke Brustschmerzen und Atemnot", "de", "urgent", "Notfall"),
    ("Ich will mich umbringen", "de", "crisis", "0800 111 0 111"),
    ("I feel suicidal", "en", "crisis", "112"),
])
def test_emergency_stops_everything(llm, sqlite_store, text, lang, category, needle):
    out = engine.handle_message(None, text)  # llm.script is empty: any LLM call would fail the test
    assert out["escalation"] == {"category": category} and out["language"] == lang and needle in out["reply"]
    tasks = sqlite_store.list_tasks()
    assert tasks[0]["priority"] == "urgent" and tasks[0]["kind"] == category
    if category == "urgent":
        assert "0800" not in out["reply"]  # the crisis line belongs to the crisis message only


def test_emergency_drops_pending_booking(llm, sqlite_store):
    slot = _bookable(sqlite_store)
    args = {"slot_id": slot["id"], "full_name": "Ann Example", "email": "ann@example.com", "phone": "+1 555 0170", "reason": "x"}
    llm.script = [lambda m: _resp(calls=[("book_appointment", args)]), lambda m: _resp("Please confirm …")]
    out = engine.handle_message(None, "Book that for me")
    sess = engine._sessions[out["session_id"]]
    assert sess.get("pending")
    engine.handle_message(out["session_id"], "help I can't breathe")
    assert "pending" not in sess


def test_medical_question_creates_task_and_tells_model(llm, sqlite_store):
    llm.script = [lambda m: _resp("Ich kann keine medizinischen Ratschläge geben …")]
    out = engine.handle_message(None, "Welche Dosis Ibuprofen soll ich nehmen?")
    assert out["escalation"] == {"category": "medical_question"} and out["language"] == "de"
    assert sqlite_store.list_tasks()[0]["kind"] == "medical_question"
    sent = llm.calls[0]
    assert "German" in sent[0]["content"] and "Ratschläge" in sent[-1]["content"]


# ---- enforced confirmation ------------------------------------------------------------
def test_booking_needs_a_yes_in_a_later_message(llm, sqlite_store):
    slot = _bookable(sqlite_store)
    args = {"slot_id": slot["id"], "full_name": "Ann Example", "email": "ann@example.com", "phone": "+1 555 0170", "reason": "x"}
    seen = {}

    def first(m):
        return _resp(calls=[("book_appointment", args)])

    def eager_retry(m):  # the model tries to confirm on its own in the same turn
        seen["preview"] = _last_tool_payload(m)
        return _resp(calls=[("book_appointment", {**args, "confirmation_id": seen["preview"]["confirmation_id"]})])

    def ask(m):
        seen["eager"] = _last_tool_payload(m)
        return _resp("Shall I book Dr. … for you?")

    llm.script = [first, eager_retry, ask]
    out = engine.handle_message(None, "Book the first time please")
    assert seen["preview"]["needs_confirmation"] and seen["preview"]["summary"]["patient"] == "Ann Example"
    assert seen["eager"]["needs_confirmation"]           # refused: no yes from the patient yet
    assert sqlite_store.patient_by_email("ann@example.com") is None

    # "no" -> still not booked
    llm.script = [lambda m: _resp(calls=[("book_appointment", {**args, "confirmation_id": seen["preview"]["confirmation_id"]})]),
                  lambda m: _resp("Okay, I won't book it.")]
    engine.handle_message(out["session_id"], "no, not that one")
    assert sqlite_store.patient_by_email("ann@example.com") is None

    # "yes" -> booked
    def confirm(m):
        return _resp(calls=[("book_appointment", {**args, "confirmation_id": seen["preview"]["confirmation_id"]})])

    def done(m):
        seen["done"] = _last_tool_payload(m)
        return _resp("Booked!")

    llm.script = [confirm, done]
    engine.handle_message(out["session_id"], "Yes please")
    assert seen["done"]["ok"] and seen["done"]["reference"]
    assert "pending" not in engine._sessions[out["session_id"]]
    # the reminder will go back to where the patient booked (Phase 7)
    start = sqlite_store.appointment_detail(seen["done"]["reference"])["slot_start"]
    row = next(r for r in sqlite_store.reminder_candidates(start, start + timedelta(minutes=1))
               if r["reference"] == seen["done"]["reference"])
    assert (row["r_channel"], row["r_conversation_id"]) == ("web", out["session_id"])


def test_changed_details_need_a_new_confirmation(llm, sqlite_store):
    slot = _bookable(sqlite_store)
    args = {"slot_id": slot["id"], "full_name": "Ann Example", "email": "ann@example.com", "phone": "+1 555 0170", "reason": "x"}
    seen = {}
    llm.script = [lambda m: _resp(calls=[("book_appointment", args)]),
                  lambda m: seen.setdefault("p", _last_tool_payload(m)) and _resp("Confirm?")]
    sid = engine.handle_message(None, "book it")["session_id"]
    other = {**args, "full_name": "Someone Else", "confirmation_id": seen["p"]["confirmation_id"]}
    llm.script = [lambda m: _resp(calls=[("book_appointment", other)]),
                  lambda m: seen.setdefault("q", _last_tool_payload(m)) and _resp("Confirm the new details?")]
    engine.handle_message(sid, "yes")
    assert seen["q"]["needs_confirmation"] and seen["q"]["confirmation_id"] != seen["p"]["confirmation_id"]
    assert sqlite_store.patient_by_email("ann@example.com") is None


def test_invalid_booking_is_denied_before_asking(llm, sqlite_store):
    args = {"slot_id": 999999, "full_name": "Ann Example", "email": "ann@example.com", "phone": "+1 555 0170", "reason": "x"}
    seen = {}
    llm.script = [lambda m: _resp(calls=[("book_appointment", args)]),
                  lambda m: seen.setdefault("p", _last_tool_payload(m)) and _resp("That slot doesn't exist.")]
    engine.handle_message(None, "book slot 999999")
    assert seen["p"]["denied_by_rule"] == "input"


# ---- reschedule, directory, tasks --------------------------------------------------
def test_reschedule_moves_cells_and_keeps_reference(sqlite_store):
    first = _bookable(sqlite_store)
    who = {"full_name": "Re Schedule", "email": "re@example.com", "phone": "+1 555 0181"}
    booked, err = run_tool("book_appointment", {"slot_id": first["id"], "reason": "x", **who}, "s")
    assert not err
    target = next(s for s in sqlite_store.open_slots(first["start_at"] + timedelta(hours=2), first["start_at"] + timedelta(days=3),
                                                      doctor_id=first["doctor_id"], limit=500))
    moved, err = run_tool("reschedule_appointment", {"reference": booked["reference"], "email": who["email"],
                                                     "phone": who["phone"], "new_slot_id": target["id"]}, "s")
    assert not err and moved["reference"] == booked["reference"]
    detail = sqlite_store.appointment_detail(booked["reference"])
    assert detail["slot_start"] == target["start_at"] and detail["status"] == "booked"
    slots = {s["id"]: s for s in sqlite_store.export_all(["slots"])["slots"]}
    assert not slots[first["id"]]["is_booked"] and slots[target["id"]]["is_booked"]


def test_reschedule_respects_notice_and_identity(sqlite_store):
    detail = sqlite_store.appointment_detail("DEMO0001")
    target = _bookable(sqlite_store)
    p, err = run_tool("reschedule_appointment", {"reference": "DEMO0001", "email": "liam.carter@example.com",
                                                 "phone": "+1-555-0111", "new_slot_id": target["id"]}, "s")
    if detail["slot_start"] - datetime.now(UTC) < timedelta(hours=48):
        assert err and p["denied_by_rule"] == "reschedule.min_notice_hours"
    p, err = run_tool("reschedule_appointment", {"reference": "DEMO0002", "email": "liam.carter@example.com",
                                                 "phone": "+1-555-0111", "new_slot_id": target["id"]}, "s")
    assert err and p["denied_by_rule"] == "identity"


def test_directory_tools(sqlite_store):
    p, _ = run_tool("list_services", {"doctor_id": 4}, "s")
    assert {s["name"] for s in p["services"]} == {"Skin check", "Acne consultation"}
    assert p["services"][0]["price"].endswith("EUR")
    prenatal = next(s for s in run_tool("list_services", {}, "s")[0]["services"] if s["name"] == "Prenatal visit")
    assert prenatal["price"] == "on request"
    p, _ = run_tool("list_doctors", {"service_id": 4}, "s")
    assert {d["name"] for d in p["doctors"]} == {"Dr. Amelia Hart", "Dr. Rohan Mehta", "Dr. Sofia Alvarez"}
    assert "es" in next(d for d in p["doctors"] if d["name"] == "Dr. Sofia Alvarez")["languages"]


def test_staff_tasks_tool_and_api(sqlite_store):
    p, err = run_tool("create_staff_task", {"kind": "callback", "summary": "Call back about invoice",
                                            "patient_name": "Ann", "contact": "+1 555 0170"}, "s")
    assert not err and p["task_id"]
    app = FastAPI()
    app.include_router(admin.router)
    app.dependency_overrides[current_principal] = lambda: TOKEN_PRINCIPAL
    c = TestClient(app)
    assert c.get("/api/admin/tasks").json()[0]["summary"] == "Call back about invoice"
    r = c.patch(f"/api/admin/tasks/{p['task_id']}", json={"status": "done", "note": "Called"})
    assert r.json()["status"] == "done"
    assert c.patch(f"/api/admin/tasks/{p['task_id']}", json={"status": "archived"}).status_code == 422


# ---- privacy and language ------------------------------------------------------------
def test_audit_rows_are_masked(sqlite_store):
    slot = _bookable(sqlite_store)
    run_tool("book_appointment", {"slot_id": slot["id"], "full_name": "Mask Me", "email": "mask.me@example.com",
                                  "phone": "+49 151 2345678", "reason": "born 03.04.1985"}, "s")
    row = next(r for r in sqlite_store.list_audit(5) if r["action"] == "book_appointment")
    dumped = json.dumps(row, default=str)
    assert "mask.me@example.com" not in dumped and "2345678" not in dumped and "1985" not in dumped
    assert "m***@e***.com" in dumped and "+** *** ****678" in dumped


def test_masking_keeps_appointment_dates():
    text = "jana@example.com +49 151 2345678 on 2026-11-24 09:00 / 24.11.2026, born 03.04.1985, ref 0ED77F95"
    out = mask_text(text)
    assert "2026-11-24 09:00" in out and "24.11.2026" in out and "0ED77F95" in out
    assert "jana@" not in out and "2345678" not in out and "1985" not in out
    assert mask({"date_of_birth": "1990-01-02", "date_from": "2026-11-24"}) == {"date_of_birth": "****-**-**",
                                                                                "date_from": "2026-11-24"}


@pytest.mark.parametrize("text,prev,want", [
    ("Haben Sie morgen einen Termin frei?", "en", "de"), ("Any slots tomorrow?", "de", "en"),
    ("DEMO0001", "de", "de"), ("ok", "en", "en"), ("Ja, bitte", "en", "de"),
])
def test_language_detection(text, prev, want):
    assert detect(text, previous=prev) == want


@pytest.mark.parametrize("text,want", [("yes", True), ("Ja, gerne", True), ("ok", True), ("yes but at 10", False),
                                       ("no", False), ("nein danke", False), ("what time again?", False)])
def test_confirmation_words(text, want):
    assert is_confirmation(text) is want
