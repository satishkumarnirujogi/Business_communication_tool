"""Channel-independent message flow: inbound message -> conversation -> agent engine -> reply on the same channel.

The engine (app/agent/engine.py) does everything else exactly as for the web chat: emergency detection,
confirmation before booking, staff take-over, the stored transcript.
"""

from __future__ import annotations

import logging
import threading
import time
from collections import defaultdict, deque

from .. import audit
from ..data import get_store
from ..rules import get_rules
from . import get_channel
from .base import ChannelError, Inbound

log = logging.getLogger("clinic.channels")

# Every inbound message costs an LLM call; cap what one sender can trigger.
MAX_PER_HOUR = 30
_recent: dict[str, deque[float]] = defaultdict(deque)
_recent_lock = threading.Lock()
_sender_locks: dict[str, threading.Lock] = defaultdict(threading.Lock)

TEXT_ONLY = {
    "en": "Sorry, I can only read text messages. Please type your question, or call us on {phone}.",
    "de": "Entschuldigung, ich kann nur Textnachrichten lesen. Bitte schreiben Sie Ihre Frage oder rufen Sie uns "
          "unter {phone} an.",
}
SLOW_DOWN = {
    "en": "You've sent a lot of messages in a short time. Please wait a little, or call us on {phone}.",
    "de": "Sie haben in kurzer Zeit sehr viele Nachrichten gesendet. Bitte warten Sie etwas oder rufen Sie uns "
          "unter {phone} an.",
}


def _allow(key: str, now: float | None = None) -> tuple[bool, bool]:
    """(allowed, first_refusal): the first refused message gets a polite reply, later ones are ignored."""
    now = time.monotonic() if now is None else now
    with _recent_lock:
        q = _recent[key]
        while q and now - q[0] > 3600:
            q.popleft()
        if len(q) >= MAX_PER_HOUR:
            first = len(q) == MAX_PER_HOUR
            q.append(now)
            return False, first
        q.append(now)
        return True, False


def reset_limits() -> None:
    with _recent_lock:
        _recent.clear()


def receive(msg: Inbound) -> str | None:
    """Handle one inbound message. Returns the reply that was sent (None if nothing was sent)."""
    from ..agent.engine import handle_message  # late import: the engine imports the data layer

    store = get_store()
    if msg.external_id and not store.mark_inbound(msg.channel, msg.external_id):
        log.info("%s: duplicate delivery ignored", msg.channel)
        return None
    channel = get_channel(msg.channel)
    if channel is None:
        log.warning("%s message received but the channel is turned off", msg.channel)
        return None

    lock = _sender_locks[f"{msg.channel}:{msg.address}"]
    with lock:  # one message per sender at a time, in order
        thread = store.channel_thread(msg.channel, msg.address, display_name=msg.display_name,
                                      subject=msg.subject, external_id=msg.external_id)
        sid = thread["conversation_id"]
        conv = store.get_conversation(sid) or {}
        lang = conv.get("language") or "en"
        phone = get_rules().clinic["phone"]

        allowed, first_refusal = _allow(f"{msg.channel}:{msg.address}")
        if not allowed:
            audit.record("channel.rate_limit", "denied", session_id=sid, actor=msg.channel, rule="channels.rate_limit",
                         reason=f"more than {MAX_PER_HOUR} messages in an hour")
            if not first_refusal:
                return None
            reply = (SLOW_DOWN.get(lang) or SLOW_DOWN["en"]).format(phone=phone)
            store.add_message(sid, "patient", msg.text or "(message)")
            store.add_message(sid, "assistant", reply)
        elif msg.unsupported:
            reply = (TEXT_ONLY.get(lang) or TEXT_ONLY["en"]).format(phone=phone)
            store.touch_conversation(sid, channel=msg.channel)
            store.add_message(sid, "patient", "(photo, voice note or other non-text message)")
            store.add_message(sid, "assistant", reply)
        else:
            result = handle_message(sid, msg.text, channel=msg.channel)
            reply = result.get("reply") or ""
        if not reply:
            return None  # e.g. staff have taken over and already sent the notice
        try:
            channel.send(thread, reply)
        except ChannelError as e:
            log.error("%s reply could not be sent: %s", msg.channel, e)
            audit.record("channel.send", "error", session_id=sid, actor=msg.channel, reason=str(e))
            return None
        return reply


def deliver(conversation_id: str, text: str, *, notice: bool = False) -> str | None:
    """Send a staff message (or a take-over/hand-back notice) to a WhatsApp/email conversation.
    Returns an error message or None. Web conversations need nothing: the chat page polls for it.
    Notices are not emailed (a separate email just to say "a person is reading this" is noise)."""
    store = get_store()
    conv = store.get_conversation(conversation_id)
    if not conv or conv["channel"] not in ("whatsapp", "email") or (notice and conv["channel"] == "email"):
        return None
    channel = get_channel(conv["channel"])
    thread = store.thread_for_conversation(conversation_id)
    if channel is None or thread is None:
        return f"This conversation came in by {conv['channel']}, which is no longer set up, so the patient won't see it."
    try:
        channel.send(thread, text)
    except ChannelError as e:
        audit.record("channel.send", "error", session_id=conversation_id, actor="staff", reason=str(e))
        return f"Saved, but not delivered to the patient: {e}"
    return None
