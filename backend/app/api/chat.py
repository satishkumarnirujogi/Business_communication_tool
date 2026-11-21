"""Public patient API: chat (plain or streamed), new messages from staff, clinic profile, health."""

import json
import logging
import queue
import threading

from fastapi import APIRouter, HTTPException
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

from ..agent import handle_message
from ..agent.language import t
from ..config import get_settings
from ..data import get_store
from ..rules import get_rules

router = APIRouter(prefix="/api")
log = logging.getLogger("clinic.chat")


class ChatIn(BaseModel):
    message: str = Field(..., max_length=2000)
    session_id: str | None = Field(None, max_length=64)
    language: str | None = Field(None, pattern="^(en|de)$")  # the widget's DE/EN toggle


@router.post("/chat")
def chat(body: ChatIn):
    # Sync def: FastAPI runs it in a threadpool, so blocking DB/LLM calls are fine.
    return handle_message(body.session_id, body.message, lang_hint=body.language)


@router.post("/chat/stream")
def chat_stream(body: ChatIn):
    """Server-sent events: {"type":"delta","text"} while the answer is written, {"type":"reset"} if a
    preamble is replaced, {"type":"status","tool"} while tools run, then {"type":"done", ...full result}."""
    events: queue.Queue = queue.Queue()

    def run():
        try:
            result = handle_message(body.session_id, body.message, lang_hint=body.language, emit=events.put)
            events.put({"type": "done", **result})
        except Exception:
            log.exception("chat stream failed")
            phone = get_rules().clinic["phone"]
            events.put({"type": "error", "message": t("error", body.language or "en", phone=phone)})
        finally:
            events.put(None)

    threading.Thread(target=run, name="chat-stream", daemon=True).start()

    def stream():
        while True:
            item = events.get()
            if item is None:
                break
            yield f"data: {json.dumps(item, default=str)}\n\n"

    return StreamingResponse(stream(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


@router.get("/chat/{session_id}/messages")
def new_messages(session_id: str, after: int = 0):
    """Messages the patient hasn't seen yet (staff replies, notices). The session id is the access key."""
    if len(session_id) < 16:
        raise HTTPException(404, "Conversation not found.")
    store = get_store()
    conv = store.get_conversation(session_id)
    if not conv:
        raise HTTPException(404, "Conversation not found.")
    rows = [m for m in store.conversation_messages(session_id, after_id=after) if m["role"] != "patient"]
    return {
        "status": conv["status"],
        "last_id": max([m["id"] for m in rows], default=after),
        "messages": [{"id": m["id"], "role": m["role"], "author": m["author"] if m["role"] == "staff" else "",
                      "content": m["content"], "created_at": m["created_at"], "meta": m["meta"]} for m in rows],
    }


@router.get("/clinic")
def clinic():
    """Public clinic profile for the chat UI header."""
    c = get_settings().clinic
    return {"name": c.name, "phone": c.phone, "address": c.address, "website": c.website, "timezone": c.timezone,
            "languages": c.languages, "branding": c.branding.model_dump(), "demo": get_settings().demo_mode}


@router.get("/health")
def health():
    get_rules()  # fails loudly if rules.yaml is broken
    store = get_store()
    store.health()
    s = get_settings()
    return {"ok": True, "data_source": store.kind, "llm_configured": s.llm_configured, "demo": s.demo_mode}
