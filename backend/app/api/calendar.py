"""Admin calendar API: doctors, services, availability templates, exceptions, bookings."""

from __future__ import annotations

import re
from datetime import date, datetime, timedelta

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field

from .. import audit
from ..agent.tools import Denied, staff_cancel
from ..data import SourceError, get_store
from ..rules import DAYS, get_rules
from ..scheduling import regenerate, working_intervals
from .deps import require_staff

router = APIRouter(prefix="/api/admin", dependencies=[Depends(require_staff)], tags=["calendar"])
HHMM = re.compile(r"^([01]\d|2[0-3]):[0-5]\d$")


def _dt(value: str, field: str) -> datetime:
    """ISO datetime; without an offset it is read as clinic local time."""
    try:
        dt = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    except (ValueError, AttributeError):
        raise HTTPException(400, f"{field} must be a date-time like 2026-11-24T09:00.") from None
    return dt.replace(tzinfo=get_rules().tz) if dt.tzinfo is None else dt


def _iso(dt: datetime) -> str:
    return dt.astimezone(get_rules().tz).isoformat()


def _doctor_ids() -> set[int]:
    return {d["id"] for d in get_store().list_doctors(active_only=False)}


def _regen() -> dict:
    summary = regenerate()
    if summary["conflicts"]:
        summary["warning"] = (
            f"{len(summary['conflicts'])} booked time(s) are now outside working hours and were kept: "
            + ", ".join(sorted({c["reference"] or "?" for c in summary["conflicts"]}))
            + ". Contact those patients to rebook if needed."
        )
    summary["conflicts"] = [{**c, "start_at": _iso(c["start_at"])} for c in summary["conflicts"]]
    return summary


def _staff_write(fn):
    try:
        return fn()
    except SourceError as e:
        raise HTTPException(409, str(e)) from None


# --------------------------------------------------------------------------
@router.get("/doctors")
def doctors():
    return get_store().list_doctors(active_only=False)


@router.get("/services")
def services():
    return get_store().list_services(active_only=False)


@router.get("/calendar")
def calendar(start: str = Query(...), end: str = Query(...), doctor_id: int | None = None):
    """Everything the calendar needs for [start, end): bookings, working time, templates, exceptions."""
    rules = get_rules()
    store = get_store()
    a, b = _dt(start, "start"), _dt(end, "end")
    if b <= a or b - a > timedelta(days=62):
        raise HTTPException(400, "Choose a range of up to 62 days.")
    docs = [d for d in store.list_doctors(active_only=False) if doctor_id is None or d["id"] == doctor_id]
    availability = store.list_availability()
    exceptions = store.list_exceptions(a, b)
    first: date = a.astimezone(rules.tz).date()
    last: date = (b - timedelta(seconds=1)).astimezone(rules.tz).date()
    working = [
        {"doctor_id": d["id"], "start": _iso(x), "end": _iso(y)}
        for d in docs if d["active"]
        for x, y in working_intervals(rules, availability, exceptions, d["id"], first, last)
    ]
    appts = store.appointments_between(a, b, doctor_id, include_cancelled=False)
    return {
        "timezone": rules.clinic["timezone"],
        "slot_minutes": rules.slot_minutes,
        "opening_hours": {
            day: (None if (h := rules._hours[day]) is None else [f"{h[0]:%H:%M}", f"{h[1]:%H:%M}"])
            for day in DAYS
        },
        "appointments": [
            {"reference": r["reference"], "status": r["status"], "doctor_id": r["doctor_id"], "doctor": r["doctor"],
             "patient": r["patient"], "service": r["service"], "color": r["color"] or "", "reason": r["reason"],
             "start": _iso(r["slot_start"]), "end": _iso(r["slot_end"])}
            for r in appts
        ],
        "working": working,
        "availability": [x for x in availability if doctor_id is None or x["doctor_id"] == doctor_id],
        "exceptions": [{**e, "start_at": _iso(e["start_at"]), "end_at": _iso(e["end_at"])} for e in exceptions
                       if doctor_id is None or e["doctor_id"] in (doctor_id, None)],
    }


# ---- weekly availability ---------------------------------------------------
class AvailabilityIn(BaseModel):
    doctor_id: int
    weekday: int = Field(ge=0, le=6)
    start_time: str
    end_time: str


def _check_block(body: AvailabilityIn) -> list[str]:
    if body.doctor_id not in _doctor_ids():
        raise HTTPException(400, "Unknown doctor.")
    if not HHMM.match(body.start_time) or not HHMM.match(body.end_time):
        raise HTTPException(400, "Times must look like 09:00.")
    if body.start_time >= body.end_time:
        raise HTTPException(400, "The end time must be after the start time.")
    rules = get_rules()
    hours = rules._hours[DAYS[body.weekday]]
    if hours is None:
        return [f"The clinic is closed on {DAYS[body.weekday].title()}s (rules.yaml), so no slots are generated."]
    if body.start_time < f"{hours[0]:%H:%M}" or body.end_time > f"{hours[1]:%H:%M}":
        return [f"Only the part within opening hours ({hours[0]:%H:%M}-{hours[1]:%H:%M}) becomes bookable."]
    return []


@router.post("/availability", status_code=201)
def add_availability(body: AvailabilityIn):
    warnings = _check_block(body)
    row = _staff_write(lambda: get_store().add_availability(body.doctor_id, body.weekday, body.start_time, body.end_time))
    audit.record("availability.add", "allowed", actor="staff", params=body.model_dump(), result=row)
    return {"availability": row, "warnings": warnings, "slots": _regen()}


@router.put("/availability/{availability_id}")
def update_availability(availability_id: int, body: AvailabilityIn):
    warnings = _check_block(body)
    row = _staff_write(lambda: get_store().update_availability(availability_id, **body.model_dump()))
    if not row:
        raise HTTPException(404, "Availability block not found.")
    audit.record("availability.update", "allowed", actor="staff", params={"id": availability_id, **body.model_dump()})
    return {"availability": row, "warnings": warnings, "slots": _regen()}


@router.delete("/availability/{availability_id}")
def delete_availability(availability_id: int):
    if not _staff_write(lambda: get_store().delete_availability(availability_id)):
        raise HTTPException(404, "Availability block not found.")
    audit.record("availability.delete", "allowed", actor="staff", params={"id": availability_id})
    return {"deleted": availability_id, "slots": _regen()}


# ---- exceptions --------------------------------------------------------------
class ExceptionIn(BaseModel):
    doctor_id: int | None = None
    kind: str = Field(pattern="^(off|extra)$")
    start: str
    end: str
    note: str = Field("", max_length=200)


@router.post("/exceptions", status_code=201)
def add_exception(body: ExceptionIn):
    if body.doctor_id is not None and body.doctor_id not in _doctor_ids():
        raise HTTPException(400, "Unknown doctor.")
    if body.kind == "extra" and body.doctor_id is None:
        raise HTTPException(400, "Extra hours need a doctor.")
    a, b = _dt(body.start, "start"), _dt(body.end, "end")
    if b <= a:
        raise HTTPException(400, "The end must be after the start.")
    row = _staff_write(lambda: get_store().add_exception(doctor_id=body.doctor_id, kind=body.kind, start_at=a,
                                                         end_at=b, note=body.note))
    audit.record("exception.add", "allowed", actor="staff", params=body.model_dump())
    return {"exception": {**row, "start_at": _iso(row["start_at"]), "end_at": _iso(row["end_at"])}, "slots": _regen()}


@router.delete("/exceptions/{exception_id}")
def delete_exception(exception_id: int):
    if not _staff_write(lambda: get_store().delete_exception(exception_id)):
        raise HTTPException(404, "Exception not found.")
    audit.record("exception.delete", "allowed", actor="staff", params={"id": exception_id})
    return {"deleted": exception_id, "slots": _regen()}


# ---- bookings ----------------------------------------------------------------
@router.get("/appointments/{reference}")
def appointment(reference: str):
    r = get_store().appointment_detail(reference.strip().upper())
    if not r:
        raise HTTPException(404, "Booking not found.")
    return {**{k: r[k] for k in ("reference", "status", "doctor_id", "doctor", "patient", "email", "phone",
                                 "service", "color", "reason")},
            "start": _iso(r["slot_start"]), "end": _iso(r["slot_end"]),
            "created_at": _iso(r["created_at"]),
            "cancelled_at": _iso(r["cancelled_at"]) if r["cancelled_at"] else None}


class CancelIn(BaseModel):
    note: str = Field("", max_length=200)


@router.post("/appointments/{reference}/cancel")
def cancel(reference: str, body: CancelIn | None = None):
    try:
        return staff_cancel(reference, (body.note if body else ""))
    except Denied as e:
        raise HTTPException(400, e.reason) from None


@router.post("/slots/regenerate")
def regenerate_slots():
    return _regen()
