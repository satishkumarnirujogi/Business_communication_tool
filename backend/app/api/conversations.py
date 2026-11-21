"""Conversations for staff: search, transcript, take over, reply directly, hand back to the assistant."""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from .. import audit
from ..agent.engine import HUMAN_NOTICE, staff_message
from ..auth import Principal
from ..channels.router import deliver
from ..data import get_store
from .deps import require_staff

router = APIRouter(prefix="/api/admin/conversations", tags=["conversations"])

RELEASED = {
    "en": "You're back with the virtual assistant. How else can I help?",
    "de": "Sie sind wieder mit dem virtuellen Assistenten verbunden. Wie kann ich sonst helfen?",
}


def _conv(conversation_id: str) -> dict:
    conv = get_store().get_conversation(conversation_id)
    if not conv:
        raise HTTPException(404, "Conversation not found.")
    return conv


@router.get("")
def list_conversations(q: str = "", status: str | None = None, limit: int = 50, offset: int = 0,
                       _: Principal = Depends(require_staff)):
    if status and status not in ("bot", "human", "closed"):
        raise HTTPException(400, "status must be bot, human or closed.")
    return get_store().list_conversations(q=q[:200], status=status, limit=max(1, min(limit, 200)), offset=max(0, offset))


@router.get("/{conversation_id}")
def get_conversation(conversation_id: str, after: int = 0, _: Principal = Depends(require_staff)):
    store = get_store()
    conv = _conv(conversation_id)
    tasks = [t for t in store.list_tasks(None, 500) if t["session_id"] == conversation_id]
    return {"conversation": conv, "messages": store.conversation_messages(conversation_id, after_id=after), "tasks": tasks}


@router.post("/{conversation_id}/takeover")
def take_over(conversation_id: str, p: Principal = Depends(require_staff)):
    conv = _conv(conversation_id)
    store = get_store()
    store.set_conversation_status(conversation_id, "human", taken_over_by=p.name or p.label)
    if conv["status"] != "human":
        notice = HUMAN_NOTICE.get(conv["language"]) or HUMAN_NOTICE["en"]
        store.add_message(conversation_id, "system", notice)
        deliver(conversation_id, notice, notice=True)
    audit.record("conversation.takeover", "allowed", actor=p.label or "staff", session_id=conversation_id)
    return get_store().get_conversation(conversation_id)


class ReplyIn(BaseModel):
    text: str = Field(..., min_length=1, max_length=2000)


@router.post("/{conversation_id}/reply")
def reply(conversation_id: str, body: ReplyIn, p: Principal = Depends(require_staff)):
    conv = _conv(conversation_id)
    if conv["status"] != "human":  # replying means taking over
        take_over(conversation_id, p)
    msg = staff_message(conversation_id, body.text.strip(), p.name or "Clinic team")
    audit.record("conversation.reply", "allowed", actor=p.label or "staff", session_id=conversation_id,
                 params={"text": body.text})
    return msg


@router.post("/{conversation_id}/release")
def release(conversation_id: str, p: Principal = Depends(require_staff)):
    conv = _conv(conversation_id)
    store = get_store()
    store.set_conversation_status(conversation_id, "bot")
    if conv["status"] == "human":
        notice = RELEASED.get(conv["language"]) or RELEASED["en"]
        store.add_message(conversation_id, "system", notice)
        deliver(conversation_id, notice, notice=True)
    audit.record("conversation.release", "allowed", actor=p.label or "staff", session_id=conversation_id)
    return store.get_conversation(conversation_id)


@router.post("/{conversation_id}/close")
def close(conversation_id: str, p: Principal = Depends(require_staff)):
    _conv(conversation_id)
    audit.record("conversation.close", "allowed", actor=p.label or "staff", session_id=conversation_id)
    return get_store().set_conversation_status(conversation_id, "closed")
