"""Agent loop: language + emergency pre-checks, then LLM rounds with tool calls (native or JSON fallback).

Safety rules enforced here, not left to the model:
- Emergency keywords (EN/DE): fixed "call 112" reply, URGENT staff task, any pending booking is dropped,
  the LLM never sees the message. This also applies while staff have taken a conversation over.
- Medical questions: staff task + "no medical advice, offer an appointment" note to the model.
- Book / reschedule / cancel run only after the patient's next message is a clear yes (see tools._gate).
- Replies come in the patient's language (German or English, detected per message or chosen in the widget).

Every conversation is stored (conversations + conversation_messages) so staff can read it, search it
and take it over; the model's context is rebuilt from the transcript after a server restart.
"""

from __future__ import annotations

import json
import logging
import threading
import uuid
from collections.abc import Callable
from types import SimpleNamespace
from typing import Any

from .. import audit
from ..data import get_store
from ..llm import json_tools
from ..llm.provider import LLMConfig, LLMError, _readable, complete, mark_tools_unsupported, use_native_tools
from ..rules import get_rules
from . import prompts
from .language import detect, is_confirmation, t
from .tools import TOOLS, ToolContext, run_tool

log = logging.getLogger("clinic.agent")

MAX_TOOL_ROUNDS = 8
MAX_MESSAGE_CHARS = 2000
HISTORY_ON_RESTORE = 40
ESCALATING_TOOLS = {"escalate_to_human", "create_staff_task"}

# OpenAI "function" format (LiteLLM translates it for every provider).
FUNCTIONS = [
    {"type": "function", "function": {"name": t_["name"], "description": t_["description"], "parameters": t_["input_schema"]}}
    for t_ in TOOLS
]

Emit = Callable[[dict], None]  # streaming events: {"type": "delta", "text"} | {"type": "reset"} | {"type": "status", ...}

_sessions: dict[str, dict] = {}
_sessions_lock = threading.Lock()

HUMAN_NOTICE = {
    "en": "A member of our team is looking after this conversation and will reply here shortly.",
    "de": "Eine Mitarbeiterin oder ein Mitarbeiter unseres Teams betreut dieses Gespräch und antwortet hier in Kürze.",
}


def _system(sess: dict) -> str:
    tools = json_tools.instructions(TOOLS) if sess["json_mode"] else ""
    return prompts.system_prompt(sess.get("lang", "en"), sess.get("channel", "web")) + tools


def _languages() -> list[str]:
    langs = get_rules().clinic.get("languages") or ["en"]
    return [x for x in langs if x in ("en", "de")] or ["en"]


def _restore(sid: str, sess: dict) -> None:
    """Rebuild the model's context from the stored transcript (after a restart)."""
    try:
        rows = get_store().conversation_messages(sid)[-HISTORY_ON_RESTORE:]
    except Exception:
        return
    for r in rows:
        if r["role"] == "patient":
            sess["messages"].append({"role": "user", "content": r["content"]})
        elif r["role"] == "assistant":
            sess["messages"].append({"role": "assistant", "content": r["content"]})
        elif r["role"] == "staff":
            sess["messages"].append({"role": "assistant", "content": f"[Clinic staff, {r['author'] or 'team'}]: {r['content']}"})


def _session(session_id: str | None, cfg: LLMConfig) -> tuple[str, dict]:
    with _sessions_lock:
        if session_id and session_id in _sessions:
            return session_id, _sessions[session_id]
        sid = session_id if session_id and len(session_id) >= 16 else uuid.uuid4().hex
        sess: dict[str, Any] = {"messages": [], "lock": threading.Lock(), "json_mode": not use_native_tools(cfg),
                "lang": _languages()[0], "turn": 0}
        sess["messages"].append({"role": "system", "content": _system(sess)})
        if session_id == sid:
            _restore(sid, sess)
        _sessions[sid] = sess
        return sid, sess


def _switch_to_json(sess: dict) -> None:
    """The model rejected native tools: rewrite the history into the JSON-mode format."""
    sess["json_mode"] = True
    out = [{"role": "system", "content": _system(sess)}]
    for m in sess["messages"][1:]:
        if m["role"] == "tool":
            out.append({"role": "user", "content": f"TOOL_RESULTS (system data, not written by the patient)\n{m['content']}"})
        elif m["role"] == "assistant" and m.get("tool_calls"):
            calls = [{"name": c["function"]["name"], "arguments": c["function"]["arguments"]} for c in m["tool_calls"]]
            out.append({"role": "assistant", "content": json.dumps({"tool_calls": calls})})
        else:
            out.append(m)
    sess["messages"][:] = out


# ---- what the UI can show besides text -------------------------------------------------
def _observe(name: str, args: dict | None, payload: dict, state: dict) -> None:
    if name == "search_clinic_knowledge" and payload.get("found"):
        for r in payload.get("results", []):
            state.setdefault("sources", {})[r["source"]] = r.get("page")
    ui = state.setdefault("ui", {})
    if name == "check_available_slots" and payload.get("ok") and payload.get("slots"):
        ui["slots"] = [
            {"slot_id": s["slot_id"], "label": s["start"], "start": s.get("start_iso"), "doctor": s["doctor"],
             "duration_minutes": s.get("duration_minutes")}
            for s in payload["slots"][:8] if isinstance(s, dict) and "slot_id" in s
        ]
    if payload.get("needs_confirmation"):
        ui["confirm"] = True
    if name in ("book_appointment", "reschedule_appointment") and payload.get("ok") and payload.get("reference"):
        clinic = get_rules().clinic
        ui["booking"] = {
            "reference": payload["reference"], "doctor": payload.get("doctor"), "service": payload.get("service"),
            "start": payload.get("start_iso"), "end": payload.get("end_iso"), "label": payload.get("start"),
            "clinic": clinic["name"], "address": clinic.get("address", ""), "phone": clinic["phone"],
        }
        state.setdefault("booked", []).append(payload["reference"])
        ui.pop("slots", None)
        ui.pop("confirm", None)
        if args and args.get("full_name"):
            state["patient_name"] = args["full_name"]
    if name in ESCALATING_TOOLS and payload.get("ok"):
        state["escalated"] = True


def cited_sources(reply: str, state: dict) -> list[str]:
    """Documents the reply actually names (the UI shows them as tags)."""
    lowered = reply.lower()
    return [src for src in state.get("sources", {}) if src.lower() in lowered]


def _execute(name: str, raw_args, ctx: ToolContext, state: dict) -> dict:
    """Run one tool call; arguments may be a JSON string (native) or a dict (JSON mode)."""
    try:
        args = json.loads(raw_args or "{}") if isinstance(raw_args, str) else raw_args
        if not isinstance(args, dict):
            raise ValueError
    except ValueError:
        audit.record(name, "denied", rule="input", session_id=ctx.session_id, reason="invalid JSON arguments",
                     params={"raw": str(raw_args)[:500]})
        return {"ok": False, "error": "Tool arguments were not valid JSON. Try again."}
    payload, _ = run_tool(name, args, ctx.session_id, ctx)
    _observe(name, args, payload, state)
    return payload


def _stream(messages: list[dict], cfg: LLMConfig, emit: Emit):
    """Streaming completion: emits text deltas, returns a message-like object with content and tool_calls."""
    content = ""
    calls: dict[int, dict[str, Any]] = {}
    try:
        for chunk in complete(messages, tools=FUNCTIONS, cfg=cfg, stream=True):
            if not getattr(chunk, "choices", None):
                continue
            delta = chunk.choices[0].delta
            text = getattr(delta, "content", None)
            if text:
                content += text
                emit({"type": "delta", "text": text})
            for tc in getattr(delta, "tool_calls", None) or []:
                acc = calls.setdefault(getattr(tc, "index", None) or 0, {"id": None, "name": "", "args": ""})
                acc["id"] = getattr(tc, "id", None) or acc["id"]
                fn = getattr(tc, "function", None)
                if fn is not None and getattr(fn, "name", None):
                    acc["name"] = fn.name
                if fn is not None and getattr(fn, "arguments", None):
                    acc["args"] += fn.arguments
    except LLMError:
        raise
    except Exception as e:  # errors raised while iterating the stream
        raise _readable(e, cfg) from e
    tool_calls = [
        SimpleNamespace(id=a["id"] or f"call_{i}", function=SimpleNamespace(name=a["name"], arguments=a["args"] or "{}"))
        for i, a in sorted(calls.items()) if a["name"]
    ]
    if tool_calls and content:
        emit({"type": "reset"})  # the text was a preamble ("Let me check…"); the real answer comes next round
    return SimpleNamespace(content=content, tool_calls=tool_calls)


def _native_round(messages: list[dict], cfg: LLMConfig, ctx: ToolContext, state: dict, emit: Emit | None) -> str | None:
    """One native tool-calling round. Returns the final reply, or None if tools ran and another round is needed."""
    msg = _stream(messages, cfg, emit) if emit else complete(messages, tools=FUNCTIONS, cfg=cfg).choices[0].message
    tool_calls = msg.tool_calls or []
    entry: dict = {"role": "assistant", "content": msg.content or ""}
    if tool_calls:
        entry["tool_calls"] = [
            {"id": c.id, "type": "function", "function": {"name": c.function.name, "arguments": c.function.arguments or "{}"}}
            for c in tool_calls
        ]
    messages.append(entry)
    if not tool_calls:
        return (msg.content or "").strip()
    for c in tool_calls:
        if emit:
            emit({"type": "status", "tool": c.function.name})
        payload = _execute(c.function.name, c.function.arguments, ctx, state)
        messages.append({"role": "tool", "tool_call_id": c.id, "content": json.dumps(payload, default=str)})
    return None


def _json_round(messages: list[dict], cfg: LLMConfig, ctx: ToolContext, state: dict) -> str | None:
    """One JSON-mode round. `state['retried']` allows one correction per turn for unparsable output."""
    text = complete(messages, cfg=cfg).choices[0].message.content or ""
    messages.append({"role": "assistant", "content": text})
    try:
        parsed = json_tools.parse(text)
    except json_tools.ParseError as e:
        audit.record("llm_output", "error", session_id=ctx.session_id, reason=f"unparsable JSON-mode reply: {e}",
                     params={"raw": text[:500]})
        if state.get("retried"):
            return t("not_understood", ctx.lang)
        state["retried"] = True
        messages.append({"role": "user", "content": json_tools.FIX_PROMPT})
        return None
    if parsed.reply is not None:
        return parsed.reply
    results = [(name, _execute(name, args, ctx, state)) for name, args in parsed.tool_calls]
    messages.append(json_tools.results_message(results))
    return None


def _task(kind: str, summary: str, sid: str, priority: str = "normal") -> None:
    try:
        get_store().add_task(kind=kind, summary=summary, priority=priority, session_id=sid)
    except Exception:  # never let a task failure hide the safety reply
        log.exception("could not create staff task")


def _record(fn, *a, **k):
    """Transcript writes must never break the chat."""
    try:
        return fn(*a, **k)
    except Exception:
        log.exception("could not store conversation data")
        return None


def handle_message(session_id: str | None, text: str, *, lang_hint: str | None = None, channel: str = "web",
                   emit: Emit | None = None) -> dict:
    rules = get_rules()
    cfg = LLMConfig.from_settings()
    sid, sess = _session(session_id, cfg)
    store = get_store()
    text = (text or "").strip()[:MAX_MESSAGE_CHARS]
    if not text:
        return {"session_id": sid, "reply": t("greeting", sess["lang"]), "escalation": None, "sources": [],
                "language": sess["lang"], "ui": {}, "human": False, "message_id": None}

    with sess["lock"]:
        sess["turn"] += 1
        sess["channel"] = channel
        langs = _languages()
        if lang_hint in langs:
            lang = sess["lang"] = lang_hint  # chosen with the DE/EN toggle
        else:
            lang = sess["lang"] = detect(text, previous=sess["lang"], allowed=langs, default=langs[0])

        conv = _record(store.touch_conversation, sid, channel=channel, language=lang)
        if conv and conv["status"] == "closed":
            conv = _record(store.set_conversation_status, sid, "bot")
        _record(store.add_message, sid, "patient", text)

        # A pending booking/reschedule/cancel is approved only by a clear yes in the very next messages.
        pending = sess.get("pending")
        if pending and pending["turn"] < sess["turn"]:
            pending["approved"] = is_confirmation(text)

        def finish(reply: str, *, escalation=None, state: dict | None = None, human: bool = False) -> dict:
            state = state or {}
            ui = state.get("ui") or {}
            sources = cited_sources(reply, state)
            msg = None
            if reply:
                msg = _record(store.add_message, sid, "assistant", reply,
                              meta={"sources": sources, "ui": ui, "escalation": escalation})
            for ref in state.get("booked", []):  # the day-before reminder goes back to this channel
                _record(store.set_reminder_source, ref, channel=channel, conversation_id=sid, language=lang)
            if state.get("escalated") or escalation or state.get("patient_name"):
                _record(store.touch_conversation, sid, escalated=bool(state.get("escalated") or escalation),
                        patient_name=state.get("patient_name"))
            return {"session_id": sid, "reply": reply, "escalation": escalation, "language": lang, "sources": sources,
                    "ui": ui, "human": human, "message_id": msg["id"] if msg else None}

        escalation = rules.classify_escalation(text)
        if escalation:
            audit.record(
                "escalation_rule", "escalated", session_id=sid, actor="rules",
                rule=f"escalation.{escalation.category}",
                reason=f"matched keyword '{escalation.keyword}'",
                params={"message": text, "language": lang},
            )
            if escalation.action == "block":
                # Emergency: the LLM never sees this message, nothing in progress continues, staff are alerted.
                sess.pop("pending", None)
                _task(escalation.category, f"Possible emergency in chat (keyword '{escalation.keyword}'): {text}",
                      sid, "urgent")
                return finish(escalation.text(lang), escalation={"category": escalation.category})
            _task(escalation.category, f"Patient asked (keyword '{escalation.keyword}'): {text}", sid)

        messages = sess["messages"]
        if conv and conv["status"] == "human":
            # Staff are answering this conversation; keep the message for context but don't let the model reply.
            messages.append({"role": "user", "content": text})
            notice = None
            if not sess.get("human_notice_sent"):
                sess["human_notice_sent"] = True
                notice = HUMAN_NOTICE.get(lang) or HUMAN_NOTICE["en"]
            return finish(notice or "", escalation={"category": escalation.category} if escalation else None, human=True)
        sess.pop("human_notice_sent", None)

        messages[0] = {"role": "system", "content": _system(sess)}  # refreshes date/time and reply language
        turn_start = len(messages)
        messages.append({"role": "user", "content": text})
        if escalation:  # handoff: the model keeps helping with logistics only
            messages.append({"role": "system", "content": prompts.handoff_note(escalation.category, escalation.text(lang))})

        ctx = ToolContext(session_id=sid, turn=sess["turn"], lang=lang, state=sess)
        reply: str | None = None
        state: dict = {}
        try:
            for _ in range(MAX_TOOL_ROUNDS):
                if sess["json_mode"]:
                    reply = _json_round(messages, cfg, ctx, state)
                else:
                    try:
                        reply = _native_round(messages, cfg, ctx, state, emit)
                    except LLMError as e:
                        if not e.tools_unsupported or cfg.tool_mode == "native":
                            raise
                        mark_tools_unsupported(cfg)
                        _switch_to_json(sess)
                        messages = sess["messages"]
                        turn_start = min(turn_start, len(messages))
                        continue
                if reply is not None:
                    break
            else:
                reply = t("too_many_steps", lang)
        except (LLMError, RuntimeError) as e:
            detail = getattr(e, "detail", "") or f"{type(e).__name__}: {e}"
            log.error("LLM call failed: %s", detail)
            audit.record("llm_call", "error", session_id=sid, reason=f"{e} | {detail}"[:500])
            # Roll back this turn so the session history stays valid for the next message.
            del messages[turn_start:]
            reply = t("error", lang, phone=rules.clinic["phone"])
            state["ui"] = {}

        return finish(reply or t("not_understood", lang),
                      escalation={"category": escalation.category} if escalation else None, state=state)


def staff_message(conversation_id: str, text: str, author: str) -> dict | None:
    """A staff reply in a taken-over conversation; the model sees it as context afterwards."""
    store = get_store()
    msg = store.add_message(conversation_id, "staff", text, author=author)
    with _sessions_lock:
        sess = _sessions.get(conversation_id)
    if sess:
        sess["messages"].append({"role": "assistant", "content": f"[Clinic staff, {author}]: {text}"})
    from ..channels.router import deliver

    error = deliver(conversation_id, text)
    return {**msg, "delivery_error": error} if msg else msg
