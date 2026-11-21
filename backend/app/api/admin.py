"""Operations API: overview numbers, audit log, handoff queue, data-source status, LLM test."""

from datetime import datetime, time, timedelta
from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from .. import audit
from ..auth import Principal
from ..config import get_settings
from ..data import SyncedStore, get_store
from ..llm.provider import LLMConfig, test_connection
from ..rules import get_rules
from .deps import require_admin, require_staff

router = APIRouter(prefix="/api")


class LLMTestIn(BaseModel):
    """Optional overrides, so settings can be tested before saving them.
    Omitted fields fall back to the current .env values."""

    provider: str | None = Field(None, max_length=50)
    model: str | None = Field(None, max_length=200)
    api_key: str | None = Field(None, max_length=500)
    base_url: str | None = Field(None, max_length=500)


@router.post("/admin/llm/test", dependencies=[Depends(require_admin)])
def llm_test(body: LLMTestIn | None = None):
    cfg = LLMConfig.from_settings(get_settings())
    if body:
        changes = {k: v.strip() for k, v in body.model_dump().items() if v is not None and v.strip()}
        if "provider" in changes:
            changes["provider"] = changes["provider"].lower()
        if changes:
            cfg = LLMConfig(**{**cfg.__dict__, **changes})
    return test_connection(cfg)


# ---- overview -------------------------------------------------------------------------
def _today():
    rules = get_rules()
    today = datetime.now(rules.tz).date()
    start = datetime.combine(today, time.min, rules.tz)
    return start, start + timedelta(days=1)


@router.get("/admin/overview", dependencies=[Depends(require_staff)])
def overview():
    store = get_store()
    start, end = _today()
    appts = store.appointments_between(start, end)
    tasks = store.list_tasks("open", 500) + store.list_tasks("in_progress", 500)
    stats = store.conversation_stats(start, end)
    rate = round(100 * stats["resolved_automatically"] / stats["conversations"]) if stats["conversations"] else None
    tz = get_rules().tz
    return {
        "date": start.date().isoformat(),
        "appointments_today": [
            {"reference": a["reference"], "start": a["slot_start"].astimezone(tz).isoformat(),
             "end": a["slot_end"].astimezone(tz).isoformat(), "patient": a["patient"], "doctor": a["doctor"],
             "service": a["service"], "color": a["color"] or "", "status": a["status"]}
            for a in appts
        ],
        "open_handoffs": len(tasks),
        "urgent_handoffs": sum(1 for t in tasks if t["priority"] == "urgent"),
        "conversations_today": stats["conversations"],
        "resolved_automatically_today": stats["resolved_automatically"],
        "resolved_automatically_percent": rate,
        "live_takeovers": stats["live_takeovers"],
    }


# ---- audit log -------------------------------------------------------------------------
def _when(value: str | None):
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        raise HTTPException(400, "Dates must look like 2026-11-24.") from None
    return dt.replace(tzinfo=get_rules().tz) if dt.tzinfo is None else dt


@router.get("/audit", dependencies=[Depends(require_staff)])
def audit_log(limit: int = 50, offset: int = 0, action: str | None = None, decision: str | None = None,
              actor: str | None = None, session_id: str | None = None, q: str | None = None,
              since: str | None = None, until: str | None = None):
    """Filterable audit trail (personal data in it is already masked)."""
    return get_store().search_audit(
        limit=max(1, min(limit, 500)), offset=max(0, offset), action=action or None, decision=decision or None,
        actor=actor or None, session_id=session_id or None, q=(q or "").strip() or None,
        since=_when(since), until=_when(until) + timedelta(days=1) if until and len(until) == 10 else _when(until),
    )


# ---- data source ----------------------------------------------------------------------
@router.get("/admin/data/status", dependencies=[Depends(require_staff)])
def data_status():
    store = get_store()
    status: dict[str, Any] = {"data_source": store.kind}
    if isinstance(store, SyncedStore):
        status.update(source=store.source.label, last_sync=store.last_sync or None)
    return status


@router.post("/admin/data/sync", dependencies=[Depends(require_staff)])
def data_sync():
    """Pull edits from Google Sheets / Excel now (otherwise it happens every SYNC_INTERVAL_SECONDS)."""
    return get_store().sync()


# ---- staff tasks (handoff queue) ---------------------------------------------------------
class TaskUpdate(BaseModel):
    status: str | None = Field(None, pattern="^(open|in_progress|done)$")
    note: str | None = Field(None, max_length=2000)
    priority: str | None = Field(None, pattern="^(normal|urgent)$")


@router.get("/admin/tasks", dependencies=[Depends(require_staff)])
def list_tasks(status: str | None = None, limit: int = 100):
    return get_store().list_tasks(status or None, max(1, min(limit, 500)))


@router.patch("/admin/tasks/{task_id}")
def update_task(task_id: int, body: TaskUpdate, p: Principal = Depends(require_staff)):
    changes = body.model_dump(exclude_none=True)
    row = get_store().update_task(task_id, **changes)
    if not row:
        raise HTTPException(404, "Task not found.")
    audit.record("task.update", "allowed", actor=p.label or "staff", params={"id": task_id, **changes})
    return row
