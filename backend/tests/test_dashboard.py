"""Phase 6 backend: accounts and roles, setup wizard, conversations + take-over, streaming chat, directory,
overview and audit search. Uses a temporary SQLite store; never touches the real .env or config files."""

from __future__ import annotations

import json
from types import SimpleNamespace as NS

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.agent import engine
from app.api import admin, auth, calendar, chat, conversations, directory, knowledge
from app.api import settings as settings_api
from app.auth import hash_password, limiter
from app.config import get_settings
from app.config_files import update_env
from app.llm.provider import LLMConfig

PASSWORD = "Correct-Horse-42"


@pytest.fixture
def client(sqlite_store, monkeypatch):
    app = FastAPI()
    for m in (chat, auth, settings_api, admin, conversations, directory, knowledge, calendar):
        app.include_router(m.router)
    limiter._fails.clear()
    engine._sessions.clear()
    return TestClient(app)


def _user(store, role="admin", email="ada@example.com"):
    return store.create_user(email=email, name=role.title() + " User", role=role, password_hash=hash_password(PASSWORD))


def _login(client, email="ada@example.com", password=PASSWORD):
    r = client.post("/api/auth/login", json={"email": email, "password": password})
    assert r.status_code == 200, r.text
    return {"Authorization": f"Bearer {r.json()['token']}"}


# ---- accounts ----------------------------------------------------------------------------
def test_login_roles_and_logout(client, sqlite_store):
    _user(sqlite_store, "admin")
    _user(sqlite_store, "staff", "sam@example.com")
    assert client.get("/api/admin/overview").status_code == 401
    assert client.post("/api/auth/login", json={"email": "ada@example.com", "password": "nope"}).status_code == 401

    admin_h, staff_h = _login(client), _login(client, "sam@example.com")
    assert client.get("/api/auth/me", headers=admin_h).json()["role"] == "admin"
    assert client.get("/api/admin/overview", headers=staff_h).status_code == 200        # staff: operations
    assert client.get("/api/admin/settings", headers=staff_h).status_code == 403        # admin only
    assert client.get("/api/admin/users", headers=staff_h).status_code == 403
    assert client.get("/api/admin/settings", headers=admin_h).json()["llm"].get("api_key") is None  # never exposed

    client.post("/api/auth/logout", headers=staff_h)
    assert client.get("/api/admin/overview", headers=staff_h).status_code == 401


def test_password_rules_lockout_and_hashing(client, sqlite_store):
    _user(sqlite_store)
    stored = sqlite_store.get_user(email="ada@example.com")["password_hash"]
    assert stored.startswith("scrypt$") and PASSWORD not in stored
    for _ in range(5):
        client.post("/api/auth/login", json={"email": "ada@example.com", "password": "wrong"})
    r = client.post("/api/auth/login", json={"email": "ada@example.com", "password": PASSWORD})
    assert r.status_code == 429  # locked for 15 minutes, even with the right password

    limiter._fails.clear()
    h = _login(client)
    weak = client.post("/api/admin/users", headers=h, json={"email": "x@example.com", "name": "X", "password": "short"})
    assert weak.status_code == 400
    ok = client.post("/api/admin/users", headers=h, json={"email": "Sam@Example.com", "name": "Sam", "password": PASSWORD})
    assert ok.status_code == 201 and ok.json()["email"] == "sam@example.com" and ok.json()["role"] == "staff"
    me = client.get("/api/auth/me", headers=h).json()
    assert client.patch(f"/api/admin/users/{me['id']}", headers=h, json={"role": "staff"}).status_code == 400  # last admin


# ---- setup wizard ---------------------------------------------------------------------------
def test_setup_wizard_creates_first_admin_once(client, sqlite_store, monkeypatch):
    written = {}
    monkeypatch.setattr(settings_api, "update_env", lambda v: written.setdefault("env", {}).update(v))
    monkeypatch.setattr(settings_api, "_save_clinic", lambda body: written.__setitem__("clinic", body.name))
    monkeypatch.setattr(settings_api, "save_rules", lambda body: written.__setitem__("hours", body.opening_hours))
    monkeypatch.setattr(settings_api, "reload_all", lambda: None)

    assert client.get("/api/setup/status").json()["needs_setup"] is True
    body = {
        "clinic": {"name": "Praxis Test", "phone": "+49 30 1234", "languages": ["de", "en"]},
        "llm": {"provider": "openai", "model": "gpt-4o-mini", "api_key": "sk-test"},
        "data_source": {"kind": get_settings().data_source},
        "opening_hours": {"monday": ["08:00", "16:00"], "sunday": None},
        "admin": {"name": "Ada", "email": "ada@example.com", "password": PASSWORD},
    }
    r = client.post("/api/setup/complete", json=body)
    assert r.status_code == 200, r.text
    assert r.json()["restart_required"] is False and r.json()["token"]
    assert written["clinic"] == "Praxis Test" and written["env"]["LLM_API_KEY"] == "sk-test"
    h = {"Authorization": f"Bearer {r.json()['token']}"}
    assert client.get("/api/auth/me", headers=h).json()["role"] == "admin"
    # Setup can't be used again to create a second admin.
    assert client.post("/api/setup/complete", json=body).status_code == 403
    assert client.get("/api/setup/status").json()["needs_setup"] is False


def test_env_writer_keeps_comments(tmp_path):
    env = tmp_path / ".env"
    env.write_text("# keep me\nLLM_MODEL=old\nOTHER=1\n", encoding="utf-8")
    update_env({"LLM_MODEL": "anthropic/claude haiku", "LLM_API_KEY": "k#1", "OTHER": None}, env)
    text = env.read_text(encoding="utf-8")
    assert text.startswith("# keep me\n") and 'LLM_MODEL="anthropic/claude haiku"' in text
    assert 'LLM_API_KEY="k#1"' in text and "OTHER" not in text


# ---- conversations, take-over, streaming --------------------------------------------------------
@pytest.fixture
def fake_llm(monkeypatch):
    box = NS(replies=[], calls=0)

    def fake_complete(messages, *, tools=None, cfg=None, stream=False, **kw):
        box.calls += 1
        text = box.replies.pop(0) if box.replies else "Hello! How can I help?"
        if stream:
            return iter([NS(choices=[NS(delta=NS(content=part, tool_calls=None))]) for part in (text[:5], text[5:])])
        return NS(choices=[NS(message=NS(content=text, tool_calls=None))])

    monkeypatch.setattr(engine, "complete", fake_complete)
    monkeypatch.setattr(engine.LLMConfig, "from_settings", classmethod(lambda cls, s=None: LLMConfig("fake", "m", "k")))
    return box


def test_conversation_is_stored_and_staff_can_take_over(client, sqlite_store, fake_llm):
    _user(sqlite_store, "staff", "sam@example.com")
    h = _login(client, "sam@example.com")
    sid = client.post("/api/chat", json={"message": "Hello, do you have parking?"}).json()["session_id"]

    convs = client.get("/api/admin/conversations", headers=h, params={"q": "parking"}).json()
    assert [c["id"] for c in convs] == [sid] and convs[0]["message_count"] == 2

    client.post(f"/api/admin/conversations/{sid}/takeover", headers=h)
    calls_before = fake_llm.calls
    out = client.post("/api/chat", json={"session_id": sid, "message": "Is anyone there?"}).json()
    assert out["human"] is True and fake_llm.calls == calls_before        # the assistant stays quiet
    client.post(f"/api/admin/conversations/{sid}/reply", headers=h, json={"text": "Hi, Sam from the front desk here."})

    polled = client.get(f"/api/chat/{sid}/messages", params={"after": 0}).json()
    assert polled["status"] == "human"
    assert polled["messages"][-1] == {**polled["messages"][-1], "role": "staff", "content": "Hi, Sam from the front desk here."}

    # Safety still applies while staff are in charge.
    urgent = client.post("/api/chat", json={"session_id": sid, "message": "now I have chest pain"}).json()
    assert urgent["escalation"] == {"category": "urgent"} and "112" in urgent["reply"]

    client.post(f"/api/admin/conversations/{sid}/release", headers=h)
    back = client.post("/api/chat", json={"session_id": sid, "message": "thanks"}).json()
    assert back["human"] is False and fake_llm.calls == calls_before + 1
    detail = client.get(f"/api/admin/conversations/{sid}", headers=h).json()
    assert [m["role"] for m in detail["messages"]].count("staff") == 1 and detail["tasks"]  # the urgent task


def test_unknown_conversation_cant_be_polled(client):
    assert client.get("/api/chat/does-not-exist-0000/messages").status_code == 404
    assert client.get("/api/chat/short/messages").status_code == 404


def test_streaming_chat(client, fake_llm):
    fake_llm.replies = ["We open at nine."]
    with client.stream("POST", "/api/chat/stream", json={"message": "When do you open?"}) as r:
        events = [json.loads(line[6:]) for line in r.iter_lines() if line.startswith("data: ")]
    deltas = "".join(e["text"] for e in events if e["type"] == "delta")
    assert deltas == "We open at nine." and events[-1]["type"] == "done" and events[-1]["reply"] == "We open at nine."
    assert events[-1]["message_id"]


def test_language_toggle_wins(client, fake_llm):
    out = client.post("/api/chat", json={"message": "Hello there, any slots?", "language": "de"}).json()
    assert out["language"] == "de"


# ---- directory, overview, audit ------------------------------------------------------------------
def test_directory_crud_and_overview_and_audit(client, sqlite_store, fake_llm):
    _user(sqlite_store)
    _user(sqlite_store, "staff", "sam@example.com")
    h, staff = _login(client), _login(client, "sam@example.com")

    r = client.post("/api/admin/doctors", headers=h, json={"full_name": "Dr. Neu Test", "specialty": "Neurology"})
    assert r.status_code == 201
    doc_id = r.json()["id"]
    assert client.post("/api/admin/doctors", headers=staff, json={"full_name": "Dr. X", "specialty": "GP"}).status_code == 403
    s = client.post("/api/admin/services", headers=h, json={"name": "Headache consult", "duration_minutes": 30,
                                                            "price": 60, "color": "#123456", "doctor_ids": [doc_id]})
    assert s.status_code == 201 and s.json()["doctor_ids"] == [doc_id]
    bad = client.post("/api/admin/services", headers=h, json={"name": "Ghost", "duration_minutes": 30, "doctor_ids": [999]})
    assert bad.status_code == 400
    assert client.delete(f"/api/admin/doctors/{doc_id}", headers=h).json()["active"] is False

    client.post("/api/chat", json={"message": "Hello"})
    ov = client.get("/api/admin/overview", headers=staff).json()
    assert ov["conversations_today"] == 1 and ov["resolved_automatically_percent"] == 100
    assert "appointments_today" in ov and "open_handoffs" in ov

    rows = client.get("/api/audit", headers=staff, params={"action": "doctor", "q": "neurology"}).json()
    assert rows and all(r["action"].startswith("doctor") for r in rows)
    assert client.get("/api/audit", headers=staff, params={"decision": "denied"}).status_code == 200
