"""Writes every agent action to the audit_log table of the active data store."""

from __future__ import annotations

import json
import logging

from .data import Tx, get_store
from .privacy import mask, mask_text

log = logging.getLogger("clinic.audit")


def _json(value):
    """JSON-safe copy with emails, phone numbers and birth dates masked."""
    return None if value is None else mask(json.loads(json.dumps(value, default=str)))


def record(
    action: str,
    decision: str,
    *,
    params: dict | None = None,
    rule: str | None = None,
    reason: str | None = None,
    result: dict | None = None,
    session_id: str | None = None,
    actor: str = "agent",
    tx: Tx | None = None,
) -> None:
    """Insert one audit row (timestamp, actor, action/tool, inputs, decision, result, conversation id).
    Personal data in inputs, results and reasons is masked before it is stored.

    Pass `tx` to write inside the caller's transaction, so a booking or cancellation and its
    audit row commit (or roll back) together. Without `tx` the row is written on its own; a
    failure there is logged rather than breaking the chat.
    """
    row = dict(session_id=session_id, actor=actor, action=action, params=_json(params), decision=decision,
               rule=rule, reason=mask_text(reason) if reason else reason, result=_json(result))
    if tx is not None:
        tx.audit(row)
        return
    try:
        get_store().audit(row)
    except Exception:
        log.exception("audit write failed: %s %s", action, decision)
