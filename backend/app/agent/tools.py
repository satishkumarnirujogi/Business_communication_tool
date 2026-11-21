"""LLM tools. Every call: parse input -> validate against rules.yaml -> execute via the DataStore -> audit.

Write tools (book, reschedule, cancel) are gated: the first call is a dry run that checks every rule and
returns a summary plus a confirmation_id. The action only runs when the tool is called again with that id
AND the patient's next message was a clear "yes" (checked by the engine, not by the model).
"""

from __future__ import annotations

import contextvars
import json
import logging
import re
import secrets
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, time, timedelta

from .. import audit
from ..data import SlotTaken, get_store
from ..data.models import KB_CATEGORIES
from ..rules import get_rules
from ..scheduling import booking_minutes

log = logging.getLogger("clinic.tools")
EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
IDENTITY_FAIL = (
    "We couldn't verify those details against our records. "
    "Please double-check the email and phone number, or call the clinic."
)


class Denied(Exception):
    def __init__(self, rule: str | None, reason: str | None):
        rule, reason = rule or "rules", reason or ""
        super().__init__(reason)
        self.rule, self.reason = rule, reason


@dataclass
class ToolContext:
    """Per-call context from the agent engine. `state` is the chat session (holds the pending confirmation)."""

    session_id: str
    turn: int = 0
    lang: str = "en"
    state: dict = field(default_factory=dict)


_CTX: contextvars.ContextVar[ToolContext | None] = contextvars.ContextVar("tool_ctx", default=None)


class _Preview(Exception):
    """Raised inside a write transaction on the dry run: everything was checked; roll back and show this."""

    def __init__(self, summary: dict):
        super().__init__("preview")
        self.summary = summary


CONFIRM_INSTRUCTIONS = (
    "Nothing has been changed yet. Show the patient this summary (in their language) and ask them to confirm. "
    "Only after the patient answers yes in their next message, call this tool again with the same arguments "
    "plus confirmation_id."
)
WAITING_INSTRUCTIONS = (
    "The patient has not confirmed yet. Ask them to confirm the summary; call again with confirmation_id only "
    "after they say yes."
)


def _gate(tool: str, args: dict, run) -> Outcome:
    """Run `run(dry_run)` behind the confirmation gate (see module docstring)."""
    ctx = _CTX.get()
    if ctx is None:  # direct calls (tests, staff API) are not chat turns
        return run(False)
    key = json.dumps({k: v for k, v in args.items() if k != "confirmation_id"}, sort_keys=True, default=str)
    pending = ctx.state.get("pending")
    cid = args.get("confirmation_id")
    same = bool(cid and pending and pending["id"] == cid and pending["tool"] == tool and pending["key"] == key)
    if same and pending is not None and pending.get("approved"):
        ctx.state.pop("pending", None)
        return run(False)
    try:
        run(True)
        raise RuntimeError(f"{tool}: dry run did not stop")  # programming error
    except _Preview as p:
        summary = p.summary
    if same:
        return Outcome({"needs_confirmation": True, "confirmation_id": cid, "summary": summary,
                        "instructions": WAITING_INSTRUCTIONS}, audit_result={"awaiting_confirmation": True})
    new_id = secrets.token_hex(4)
    ctx.state["pending"] = {"id": new_id, "tool": tool, "key": key, "turn": ctx.turn, "approved": False,
                            "summary": summary}
    return Outcome({"needs_confirmation": True, "confirmation_id": new_id, "summary": summary,
                    "instructions": CONFIRM_INSTRUCTIONS}, audit_result={"awaiting_confirmation": True, "summary": summary})


@dataclass
class Outcome:
    result: dict
    audited: bool = False  # True when the handler already wrote the audit row in its transaction
    audit_result: dict | None = None  # smaller summary for the audit log (instead of `result`)


def _now() -> datetime:
    return datetime.now(UTC)


def _local(dt: datetime) -> str:
    return dt.astimezone(get_rules().tz).strftime("%a %d %b %Y, %H:%M")


def _digits(s: str) -> str:
    return re.sub(r"\D", "", s or "")


def _identity(args: dict) -> tuple[str, str]:
    email = (args.get("email") or "").strip().lower()
    phone = (args.get("phone") or "").strip()
    if not EMAIL_RE.match(email):
        raise Denied("input", "A valid email address is required.")
    if len(_digits(phone)) < 7:
        raise Denied("input", "A valid phone number is required.")
    return email, phone


def _parse_date(value, field: str) -> date:
    try:
        return date.fromisoformat(str(value))
    except ValueError:
        raise Denied("input", f"{field} must be a date in YYYY-MM-DD format.") from None


# --------------------------------------------------------------------------
# Tool schemas (provider-neutral; converted per provider in agent.engine)
# --------------------------------------------------------------------------
TOOLS = [
    {
        "name": "search_clinic_knowledge",
        "description": (
            "Search the clinic's own documents (policies, house rules, services, prices, offers, doctor profiles, FAQ). "
            "Use it for every question about the clinic that the other tools don't answer, in any language. "
            "Answer only from the returned passages and name their source file."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "The patient's question, rephrased as a short search query"},
                "category": {"type": "string", "enum": list(KB_CATEGORIES),
                             "description": "Optional: only search one kind of document"},
            },
            "required": ["query"],
            "additionalProperties": False,
        },
    },
    {
        "name": "check_available_slots",
        "description": (
            "Find open appointment start times between two dates (inclusive, clinic local time). "
            "Pass service_id when the patient wants a specific service, so the time fits its length and only "
            "doctors who offer it are returned. "
            "Optionally filter by doctor_id or specialty (e.g. 'General Practice', 'Pediatrics', 'Dermatology'). "
            "Only slots that satisfy clinic rules are returned."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "date_from": {"type": "string", "description": "YYYY-MM-DD"},
                "date_to": {"type": "string", "description": "YYYY-MM-DD"},
                "doctor_id": {"type": "integer"},
                "specialty": {"type": "string"},
                "service_id": {"type": "integer"},
            },
            "required": ["date_from", "date_to"],
            "additionalProperties": False,
        },
    },
    {
        "name": "book_appointment",
        "description": (
            "Book a start time from check_available_slots for a patient (full name, email and phone needed). "
            "New patients are registered automatically. The first call returns a summary to confirm."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "slot_id": {"type": "integer", "description": "slot_id of the start time from check_available_slots"},
                "service_id": {"type": "integer", "description": "The same service_id used in the search, if any"},
                "full_name": {"type": "string"},
                "email": {"type": "string"},
                "phone": {"type": "string"},
                "reason": {"type": "string", "description": "Short non-clinical reason for the visit"},
                "confirmation_id": {"type": "string", "description": "Only after the patient confirmed the summary"},
            },
            "required": ["slot_id", "full_name", "email", "phone", "reason"],
            "additionalProperties": False,
        },
    },
    {
        "name": "lookup_appointment",
        "description": (
            "Look up a patient's appointments. The patient must provide the email AND phone number on file. "
            "Optionally narrow to one booking reference."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "email": {"type": "string"},
                "phone": {"type": "string"},
                "reference": {"type": "string"},
            },
            "required": ["email", "phone"],
            "additionalProperties": False,
        },
    },
    {
        "name": "cancel_appointment",
        "description": (
            "Cancel a booked appointment by reference. The patient must provide the email AND phone on file. "
            "Subject to the cancellation policy. Returns a summary to confirm first."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "reference": {"type": "string"},
                "email": {"type": "string"},
                "phone": {"type": "string"},
                "confirmation_id": {"type": "string", "description": "Only after the patient confirmed the summary"},
            },
            "required": ["reference", "email", "phone"],
            "additionalProperties": False,
        },
    },
    {
        "name": "reschedule_appointment",
        "description": (
            "Move a booked appointment to a new start time from check_available_slots (same reference). The patient "
            "must give the email AND phone on file. Keeps the service unless service_id is given. Subject to the "
            "reschedule notice rule. The first call returns a summary to confirm."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "reference": {"type": "string"},
                "email": {"type": "string"},
                "phone": {"type": "string"},
                "new_slot_id": {"type": "integer"},
                "service_id": {"type": "integer"},
                "confirmation_id": {"type": "string", "description": "Only after the patient confirmed the summary"},
            },
            "required": ["reference", "email", "phone", "new_slot_id"],
            "additionalProperties": False,
        },
    },
    {
        "name": "list_services",
        "description": "The clinic's services with id, length, price and which doctors offer them.",
        "input_schema": {
            "type": "object",
            "properties": {"doctor_id": {"type": "integer", "description": "Only services this doctor offers"}},
            "additionalProperties": False,
        },
    },
    {
        "name": "list_doctors",
        "description": "The clinic's doctors with id, specialty, languages, short bio and the services they offer.",
        "input_schema": {
            "type": "object",
            "properties": {
                "specialty": {"type": "string"},
                "service_id": {"type": "integer", "description": "Only doctors who offer this service"},
            },
            "additionalProperties": False,
        },
    },
    {
        "name": "create_staff_task",
        "description": (
            "Leave a task for clinic staff: a callback request, a question you can't answer, a complaint, "
            "or anything that needs a person. Not for medical or urgent matters (use escalate_to_human)."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "kind": {"type": "string", "enum": ["callback", "question", "complaint", "other"]},
                "summary": {"type": "string", "description": "What staff need to do, in one or two sentences"},
                "patient_name": {"type": "string"},
                "contact": {"type": "string", "description": "Phone or email to reach the patient"},
            },
            "required": ["kind", "summary"],
            "additionalProperties": False,
        },
    },
    {
        "name": "escalate_to_human",
        "description": (
            "Hand the conversation to clinic staff. Use for any medical question, anything urgent, "
            "complaints, or requests you cannot handle with the other tools."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "category": {"type": "string", "enum": ["urgent", "medical_question", "complaint", "other"]},
                "summary": {"type": "string", "description": "One or two sentences for the staff member"},
            },
            "required": ["category", "summary"],
            "additionalProperties": False,
        },
    },
]


# --------------------------------------------------------------------------
# Handlers
# --------------------------------------------------------------------------
NOT_FOUND = (
    "The clinic's documents don't cover this. Tell the patient you don't have that information. Do not guess "
    "or use general knowledge for prices, policies, services or doctor details. Offer to pass the question to "
    "staff with escalate_to_human."
)
USE_SOURCES = (
    "Answer only from these passages and quote prices, dates and policies exactly. End the answer with "
    "(Source: <source>) for each passage you used. If the passages don't actually answer the question, say you "
    "don't have that information, offer escalate_to_human, and don't add a Source line."
)


def search_clinic_knowledge(args: dict, session_id: str) -> Outcome:
    from ..knowledge import get_kb

    query = (args.get("query") or "").strip()
    if len(query) < 2:
        raise Denied("input", "A search query is required.")
    category = args.get("category")
    if category and category not in KB_CATEGORIES:
        category = None
    found = get_kb().search(query, [category] if category else None)
    if not found.found:
        return Outcome({"found": False, "results": [], "instructions": NOT_FOUND},
                       audit_result={"found": False})
    results = [
        {"source": h.source, "page": h.page, "category": h.category, "text": h.text}
        for h in found.hits
    ]
    return Outcome(
        {"found": True, "results": results, "instructions": USE_SOURCES},
        audit_result={"found": True, "sources": sorted({h.source for h in found.hits})},
    )


def _service(store, service_id) -> dict | None:
    if service_id is None:
        return None
    svc = next((x for x in store.list_services() if x["id"] == int(service_id)), None)
    if not svc:
        raise Denied("input", "That service doesn't exist or isn't offered at the moment.")
    return svc


def check_available_slots(args: dict, session_id: str) -> Outcome:
    rules = get_rules()
    store = get_store()
    now = _now()
    today = now.astimezone(rules.tz).date()
    date_from = _parse_date(args.get("date_from"), "date_from")
    date_to = _parse_date(args.get("date_to") or args.get("date_from"), "date_to")

    d = rules.check_slot_search(date_from, date_to, today)
    if not d.allowed:
        raise Denied(d.rule, d.reason)

    svc = _service(store, args.get("service_id"))
    minutes = booking_minutes(rules, svc["duration_minutes"] if svc else None)
    cell = timedelta(minutes=rules.slot_minutes)
    need = minutes // rules.slot_minutes
    # Offer clean start times: on the half hour for 30+ minute visits, otherwise on the grid.
    align = min(max(minutes, rules.slot_minutes), 30)

    window_start = datetime.combine(max(date_from, today), time.min, rules.tz)
    window_end = datetime.combine(date_to + timedelta(days=1), time.min, rules.tz)
    rows = store.open_slots(
        max(window_start, now), window_end + timedelta(minutes=minutes),
        doctor_id=int(args["doctor_id"]) if args.get("doctor_id") is not None else None,
        specialty=args.get("specialty") or None,
        limit=5000,
    )
    if svc:
        rows = [r for r in rows if r["doctor_id"] in svc["doctor_ids"]]
    free = {(r["doctor_id"], r["start_at"]): r for r in rows}

    slots, excluded = [], 0
    for r in rows:
        start_local = r["start_at"].astimezone(rules.tz)
        if r["start_at"] >= window_end or (start_local.hour * 60 + start_local.minute) % align:
            continue
        if not all((r["doctor_id"], r["start_at"] + k * cell) in free for k in range(need)):
            continue  # not enough free time after this start for the whole visit
        end = r["start_at"] + timedelta(minutes=minutes)
        if rules.slot_bookable(r["start_at"], end, now).allowed:
            slots.append({
                "slot_id": r["id"],
                "start": _local(r["start_at"]),
                "start_iso": r["start_at"].astimezone(rules.tz).isoformat(),
                "duration_minutes": minutes,
                "doctor_id": r["doctor_id"],
                "doctor": r["doctor"],
                "specialty": r["specialty"],
            })
        else:
            excluded += 1
    return Outcome({
        "timezone": rules.clinic["timezone"],
        "service": {"service_id": svc["id"], "name": svc["name"], "duration_minutes": minutes} if svc else None,
        "slots": slots[:20],
        "total_open": len(slots),
        "excluded_by_rules": excluded,
    })


def _fit_cells(tx, rules, slot: dict, minutes: int) -> list[dict]:
    start = slot["start_at"]
    cells = tx.cells_for_update(slot["doctor_id"], start, start + timedelta(minutes=minutes))
    step = timedelta(minutes=rules.slot_minutes)
    if len(cells) != minutes // rules.slot_minutes or any(
        c["is_booked"] or c["start_at"] != start + i * step for i, c in enumerate(cells)
    ):
        raise Denied("slot_unavailable", f"That time isn't free for a {minutes}-minute appointment.")
    return cells


def book_appointment(args: dict, session_id: str) -> Outcome:
    rules = get_rules()
    full_name = (args.get("full_name") or "").strip()
    reason = (args.get("reason") or "").strip()[:200]
    if len(full_name) < 2:
        raise Denied("input", "The patient's full name is required.")
    email, phone = _identity(args)
    slot_id = int(args["slot_id"])
    store = get_store()
    svc = _service(store, args.get("service_id"))
    minutes = booking_minutes(rules, svc["duration_minutes"] if svc else None)

    def run(dry_run: bool) -> Outcome:
        with store.transaction() as tx:
            slot = tx.slot_for_update(slot_id)
            if not slot:
                raise Denied("input", "That slot does not exist.")
            if slot["is_booked"] or not slot["doctor_active"]:
                raise Denied("slot_unavailable", "That slot is no longer available.")
            if svc and slot["doctor_id"] not in svc["doctor_ids"]:
                raise Denied("service.doctor", f"{slot['doctor']} doesn't offer {svc['name']}.")
            start, end = slot["start_at"], slot["start_at"] + timedelta(minutes=minutes)
            cells = _fit_cells(tx, rules, slot, minutes)

            patient = tx.patient_by_email(email)
            if patient and _digits(patient["phone"]) != _digits(phone):
                raise Denied("identity", IDENTITY_FAIL)
            active = tx.count_active_appointments(patient["id"], _now()) if patient else 0
            d = rules.check_booking(start, end, _now(), active)
            if not d.allowed:
                raise Denied(d.rule, d.reason)
            if dry_run:
                raise _Preview({
                    "action": "book", "doctor": slot["doctor"], "service": svc["name"] if svc else None,
                    "start": _local(start), "duration_minutes": minutes, "patient": full_name,
                    "email": email, "phone": phone, "reason": reason,
                })

            new_patient = patient is None
            if patient is None:
                patient = tx.insert_patient(full_name, email, phone)
            try:
                appt = tx.insert_appointment(patient_id=patient["id"], doctor_id=slot["doctor_id"], start=start,
                                             end=end, slot_id=slot_id, reason=reason,
                                             service_id=svc["id"] if svc else None)
                tx.occupy_cells(appt["id"], [c["id"] for c in cells])
            except SlotTaken:
                # A unique index caught a booking that raced this one.
                raise Denied("slot_unavailable", "That time was just taken. Please pick another time.") from None
            result = {
                "reference": appt["reference"], "doctor": slot["doctor"], "start": _local(start),
                "start_iso": start.astimezone(rules.tz).isoformat(), "end_iso": end.astimezone(rules.tz).isoformat(),
                "duration_minutes": minutes, "service": svc["name"] if svc else None,
                "patient": patient["full_name"], "new_patient_registered": new_patient,
                "cancellation_policy": rules.cancellation["policy_text"].strip(),
            }
            audit.record("book_appointment", "allowed", params=args, result=result, session_id=session_id, tx=tx)
        return Outcome(result, audited=True)

    return _gate("book_appointment", args, run)


def lookup_appointment(args: dict, session_id: str) -> Outcome:
    rules = get_rules()
    email, phone = _identity(args)
    reference = (args.get("reference") or "").strip().upper()
    now = _now()

    store = get_store()
    patient = store.patient_by_email(email)
    if not patient or _digits(patient["phone"]) != _digits(phone):
        raise Denied("identity", IDENTITY_FAIL)
    rows = store.patient_appointments(patient["id"], now - timedelta(days=30), reference or None)

    appts = []
    for r in rows:
        booked = r["status"] == "booked"
        appts.append({
            "reference": r["reference"],
            "status": r["status"],
            "start": _local(r["start_at"]),
            "doctor": r["doctor"],
            "specialty": r["specialty"],
            "reason": r["reason"],
            "can_cancel_online": booked and rules.check_cancellation(r["start_at"], now).allowed,
            "can_reschedule_online": booked and rules.check_reschedule(r["start_at"], now).allowed,
        })
    return Outcome({
        "patient": patient["full_name"],
        "appointments": appts,
        "cancellation_policy": rules.cancellation["policy_text"].strip(),
    })


def _owned_booking(tx, reference: str, email: str, phone: str, action: str) -> dict:
    row = tx.appointment_for_update(reference)
    # Same message for "not found" and "wrong owner" so references can't be probed.
    if not row or row["email"] != email or _digits(row["phone"]) != _digits(phone):
        raise Denied("identity", IDENTITY_FAIL)
    if row["status"] != "booked":
        raise Denied(f"{action}.status", f"That appointment is already {row['status']}.")
    return row


def cancel_appointment(args: dict, session_id: str) -> Outcome:
    rules = get_rules()
    email, phone = _identity(args)
    reference = (args.get("reference") or "").strip().upper()
    if not reference:
        raise Denied("input", "A booking reference is required.")

    def run(dry_run: bool) -> Outcome:
        with get_store().transaction() as tx:
            row = _owned_booking(tx, reference, email, phone, "cancellation")
            d = rules.check_cancellation(row["slot_start"], _now())
            if not d.allowed:
                raise Denied(d.rule, d.reason)
            if dry_run:
                raise _Preview({"action": "cancel", "reference": reference, "doctor": row["doctor"],
                                "start": _local(row["slot_start"]), "patient": row["patient"]})
            result = _cancel(tx, row)
            audit.record("cancel_appointment", "allowed", params=args, result=result, session_id=session_id, tx=tx)
        return Outcome(result, audited=True)

    return _gate("cancel_appointment", args, run)


def reschedule_appointment(args: dict, session_id: str) -> Outcome:
    rules = get_rules()
    email, phone = _identity(args)
    reference = (args.get("reference") or "").strip().upper()
    if not reference:
        raise Denied("input", "A booking reference is required.")
    new_slot_id = int(args["new_slot_id"])
    store = get_store()

    def run(dry_run: bool) -> Outcome:
        with store.transaction() as tx:
            row = _owned_booking(tx, reference, email, phone, "reschedule")
            now = _now()
            d = rules.check_reschedule(row["slot_start"], now)
            if not d.allowed:
                raise Denied(d.rule, d.reason)
            svc = _service(store, args.get("service_id") if args.get("service_id") is not None else row["service_id"])
            minutes = booking_minutes(rules, svc["duration_minutes"] if svc else None)

            slot = tx.slot_for_update(new_slot_id)
            if not slot or not slot["doctor_active"]:
                raise Denied("input", "That new time does not exist.")
            if svc and slot["doctor_id"] not in svc["doctor_ids"]:
                raise Denied("service.doctor", f"{slot['doctor']} doesn't offer {svc['name']}.")
            tx.release_cells(row["id"])  # the old time may overlap the new one
            cells = _fit_cells(tx, rules, slot, minutes)
            start, end = slot["start_at"], slot["start_at"] + timedelta(minutes=minutes)
            active = tx.count_active_appointments(row["patient_id"], now) - (1 if row["slot_start"] > now else 0)
            d = rules.check_booking(start, end, now, active)
            if not d.allowed:
                raise Denied(d.rule, d.reason)
            if dry_run:
                raise _Preview({"action": "reschedule", "reference": reference, "from": _local(row["slot_start"]),
                                "from_doctor": row["doctor"], "to": _local(start), "to_doctor": slot["doctor"],
                                "service": svc["name"] if svc else None, "duration_minutes": minutes})
            try:
                tx.move_appointment(row["id"], doctor_id=slot["doctor_id"], start=start, end=end, slot_id=slot["id"],
                                    service_id=svc["id"] if svc else None)
                tx.occupy_cells(row["id"], [c["id"] for c in cells])
            except SlotTaken:
                raise Denied("slot_unavailable", "That time was just taken. Please pick another time.") from None
            result = {"reference": reference, "status": "booked", "from": _local(row["slot_start"]),
                      "start": _local(start), "doctor": slot["doctor"], "duration_minutes": minutes,
                      "start_iso": start.astimezone(rules.tz).isoformat(), "end_iso": end.astimezone(rules.tz).isoformat(),
                      "service": svc["name"] if svc else None}
            audit.record("reschedule_appointment", "allowed", params=args, result=result, session_id=session_id, tx=tx)
        return Outcome(result, audited=True)

    return _gate("reschedule_appointment", args, run)


def _cancel(tx, row: dict) -> dict:
    tx.cancel_appointment(row["id"], _now())
    tx.release_cells(row["id"])
    return {"reference": row["reference"], "status": "cancelled", "doctor": row["doctor"], "start": _local(row["slot_start"])}


def staff_cancel(reference: str, note: str = "", staff: str = "staff") -> dict:
    """Cancellation by clinic staff from the calendar: identity and the 48h rule don't apply. Audited."""
    reference = (reference or "").strip().upper()
    with get_store().transaction() as tx:
        row = tx.appointment_for_update(reference)
        if not row:
            raise Denied("input", "Booking not found.")
        if row["status"] != "booked":
            raise Denied("cancellation.status", f"That appointment is already {row['status']}.")
        result = _cancel(tx, row)
        audit.record("cancel_appointment", "allowed", actor=staff, params={"reference": reference, "note": note},
                     result=result, rule="staff_override", tx=tx)
    return result


# ---- directory ------------------------------------------------------------------
def _price(value) -> str:
    if value is None:
        return "on request"
    return f"{float(value):.2f} {get_rules().clinic.get('currency') or 'EUR'}"


def list_services(args: dict, session_id: str) -> Outcome:
    store = get_store()
    names = {d["id"]: d["full_name"] for d in store.list_doctors()}
    doctor_id = args.get("doctor_id")
    out = []
    for s in store.list_services():
        doctors = [d for d in s["doctor_ids"] if d in names]
        if not doctors or (doctor_id is not None and int(doctor_id) not in doctors):
            continue
        out.append({"service_id": s["id"], "name": s["name"], "duration_minutes": s["duration_minutes"],
                    "price": _price(s["price"]), "description": s["description"],
                    "doctors": [{"doctor_id": d, "name": names[d]} for d in doctors]})
    return Outcome({"services": out, "source": "clinic service list"}, audit_result={"count": len(out)})


def list_doctors(args: dict, session_id: str) -> Outcome:
    store = get_store()
    services = store.list_services()
    specialty = (args.get("specialty") or "").strip().lower()
    service_id = args.get("service_id")
    out = []
    for d in store.list_doctors():
        offered = [s for s in services if d["id"] in s["doctor_ids"]]
        if specialty and specialty not in d["specialty"].lower():
            continue
        if service_id is not None and int(service_id) not in {s["id"] for s in offered}:
            continue
        out.append({"doctor_id": d["id"], "name": d["full_name"], "specialty": d["specialty"],
                    "languages": [x.strip() for x in d["languages"].split(",") if x.strip()], "bio": d["bio"],
                    "services": [{"service_id": s["id"], "name": s["name"]} for s in offered]})
    return Outcome({"doctors": out}, audit_result={"count": len(out)})


# ---- people ----------------------------------------------------------------------
TASK_ACK = {
    "en": "I've passed this to our team; someone will get back to you. You can also call {phone}.",
    "de": "Ich habe das an unser Team weitergegeben; jemand meldet sich bei Ihnen. Sie erreichen uns auch unter {phone}.",
}


def _lang() -> str:
    ctx = _CTX.get()
    return ctx.lang if ctx else "en"


def create_staff_task(args: dict, session_id: str) -> Outcome:
    rules = get_rules()
    kind = str(args["kind"]) if args.get("kind") in ("callback", "question", "complaint", "other") else "other"
    summary = (args.get("summary") or "").strip()
    if len(summary) < 5:
        raise Denied("input", "Describe what staff should do.")
    task = get_store().add_task(kind=kind, summary=summary, session_id=session_id,
                                patient_name=(args.get("patient_name") or "").strip(),
                                contact=(args.get("contact") or "").strip())
    msg = (TASK_ACK.get(_lang()) or TASK_ACK["en"]).format(phone=rules.clinic["phone"])
    return Outcome({"task_id": task["id"], "message_for_patient": msg}, audit_result={"task_id": task["id"], "kind": kind})


def escalate_to_human(args: dict, session_id: str) -> Outcome:
    rules = get_rules()
    category = (str(args["category"]) if args.get("category") in ("urgent", "medical_question", "complaint", "other")
                else "other")
    lang = _lang()
    message = rules.escalation_text(category, lang) or (TASK_ACK.get(lang) or TASK_ACK["en"]).format(phone=rules.clinic["phone"])
    task = get_store().add_task(kind=category, summary=(args.get("summary") or "Escalated from chat").strip(),
                                priority="urgent" if category == "urgent" else "normal", session_id=session_id)
    result = {"escalated": True, "category": category, "task_id": task["id"], "message_for_patient": message}
    audit.record("escalate_to_human", "escalated", params=args, result=result, session_id=session_id)
    return Outcome(result, audited=True)


HANDLERS = {
    "search_clinic_knowledge": search_clinic_knowledge,
    "list_services": list_services,
    "list_doctors": list_doctors,
    "check_available_slots": check_available_slots,
    "book_appointment": book_appointment,
    "reschedule_appointment": reschedule_appointment,
    "cancel_appointment": cancel_appointment,
    "lookup_appointment": lookup_appointment,
    "create_staff_task": create_staff_task,
    "escalate_to_human": escalate_to_human,
}


def run_tool(name: str, args: dict, session_id: str, ctx: ToolContext | None = None) -> tuple[dict, bool]:
    """Execute a tool call. Returns (payload for the model, is_error). `ctx` comes from the chat engine."""
    handler = HANDLERS.get(name)
    if handler is None:
        audit.record(name, "denied", params=args, rule="unknown_tool", reason="Unknown tool", session_id=session_id)
        return {"ok": False, "error": f"Unknown tool {name}"}, True
    token = _CTX.set(ctx)
    try:
        outcome = handler(args, session_id)
    except Denied as e:
        audit.record(name, "denied", params=args, rule=e.rule, reason=e.reason, session_id=session_id)
        return {"ok": False, "denied_by_rule": e.rule, "reason": e.reason}, True
    except Exception as e:  # DB down, constraint race, etc.
        log.exception("tool %s failed", name)
        audit.record(name, "error", params=args, reason=type(e).__name__, session_id=session_id)
        return {"ok": False, "error": "Internal error; nothing was changed. Suggest the patient call the clinic."}, True
    finally:
        _CTX.reset(token)
    if not outcome.audited:
        audit.record(name, "allowed", params=args, result=outcome.audit_result or outcome.result, session_id=session_id)
    return {"ok": True, **outcome.result}, False
