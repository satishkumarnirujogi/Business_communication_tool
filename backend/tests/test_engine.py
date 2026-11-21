"""Agent loop with a fake LLM and fake tools (no network, no database)."""

import json
from types import SimpleNamespace as NS

import pytest

from app import audit
from app.agent import engine
from app.llm import provider
from app.llm.provider import LLMConfig, LLMError


def _resp(content=None, tool_calls=None):
    return NS(choices=[NS(message=NS(content=content, tool_calls=tool_calls))])


def _call(id_, name, args):
    return NS(id=id_, function=NS(name=name, arguments=json.dumps(args)))


@pytest.fixture
def harness(monkeypatch):
    """Returns (calls_made_to_llm, tools_run, set_mode, set_replies)."""
    llm_calls, tools_run, state = [], [], {"mode": "auto", "replies": []}

    def fake_complete(messages, *, tools=None, cfg=None, **kw):
        llm_calls.append({"tools": bool(tools), "messages": [dict(m) for m in messages]})
        nxt = state["replies"].pop(0)
        if isinstance(nxt, Exception):
            raise nxt
        return nxt

    def fake_run_tool(name, args, sid, ctx=None):
        tools_run.append((name, args))
        return {"ok": True, "slots": ["Mon 09:00"]}, False

    monkeypatch.setattr(engine, "complete", fake_complete)
    monkeypatch.setattr(engine, "run_tool", fake_run_tool)
    monkeypatch.setattr(audit, "record", lambda *a, **k: None)
    tasks = []
    class FakeStore:  # transcript calls are no-ops; tasks are recorded
        def add_task(self, **k):
            tasks.append(k)

        def __getattr__(self, name):
            return lambda *a, **k: None

    monkeypatch.setattr(engine, "get_store", lambda: FakeStore())
    monkeypatch.setattr(engine.LLMConfig, "from_settings",
                        classmethod(lambda cls, s=None: LLMConfig("fake", "m", "k", tool_mode=state["mode"])))
    monkeypatch.setattr(provider, "_json_models", set())
    engine._sessions.clear()

    def setup(mode, replies):
        state["mode"], state["replies"] = mode, list(replies)

    return llm_calls, tools_run, setup


def test_native_tool_round(harness):
    llm_calls, tools_run, setup = harness
    setup("auto", [
        _resp(tool_calls=[_call("c1", "check_available_slots", {"date_from": "2026-11-20", "date_to": "2026-11-20"})]),
        _resp(content="Dr Hart is free Mon 09:00."),
    ])
    out = engine.handle_message(None, "Any slots Friday?")
    assert out["reply"] == "Dr Hart is free Mon 09:00."
    assert tools_run == [("check_available_slots", {"date_from": "2026-11-20", "date_to": "2026-11-20"})]
    assert all(c["tools"] for c in llm_calls)
    assert llm_calls[1]["messages"][-1]["role"] == "tool"


def test_json_mode_with_one_correction(harness):
    llm_calls, tools_run, setup = harness
    setup("json", [
        _resp(content='{"tool_calls": [{"name": "check_available_slots", "arguments": {"date_from": "2026-11-20"'),  # broken
        _resp(content='{"tool_calls": [{"name": "check_available_slots", "arguments": {"date_from": "2026-11-20", "date_to": "2026-11-20"}}]}'),
        _resp(content='{"reply": "Mon 09:00 is free."}'),
    ])
    out = engine.handle_message(None, "Any slots Friday?")
    assert out["reply"] == "Mon 09:00 is free."
    assert len(tools_run) == 1 and not any(c["tools"] for c in llm_calls)
    assert "## Output format" in llm_calls[0]["messages"][0]["content"]
    assert llm_calls[2]["messages"][-1]["content"].startswith("TOOL_RESULTS")


def test_auto_falls_back_to_json_when_tools_rejected(harness):
    llm_calls, tools_run, setup = harness
    setup("auto", [
        LLMError("Model 'm' does not accept tool calls.", tools_unsupported=True),
        _resp(content='{"reply": "Hello, how can I help?"}'),
    ])
    out = engine.handle_message(None, "hi")
    assert out["reply"] == "Hello, how can I help?"
    assert [c["tools"] for c in llm_calls] == [True, False]
    assert "fake/m" in provider._json_models
    assert engine._sessions[out["session_id"]]["json_mode"]


def test_llm_error_rolls_back_turn(harness):
    _, _, setup = harness
    setup("auto", [LLMError("The API key was rejected by fake. Check LLM_API_KEY.", "401")])
    out = engine.handle_message(None, "hi")
    assert "trouble" in out["reply"]
    assert [m["role"] for m in engine._sessions[out["session_id"]]["messages"]] == ["system"]


def test_urgent_never_reaches_llm(harness):
    llm_calls, _, setup = harness
    setup("auto", [])
    out = engine.handle_message(None, "I have chest pain")
    assert out["escalation"] == {"category": "urgent"} and llm_calls == []
