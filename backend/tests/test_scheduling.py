"""Slot generation, service-length booking and the admin calendar API."""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.agent.tools import run_tool
from app.api import calendar
from app.api.deps import current_principal
from app.auth import TOKEN_PRINCIPAL
from app.rules import get_rules
from app.scheduling import booking_minutes, cells, regenerate, working_intervals

R = get_rules()
TZ = R.tz
THU = date(2026, 11, 19)  # a Thursday; clinic open 09:00-17:00


def at(d: date, hhmm: str) -> datetime:
    h, m = map(int, hhmm.split(":"))
    return datetime(d.year, d.month, d.day, h, m, tzinfo=TZ)


def block(doctor, weekday, a, b):
    return {"doctor_id": doctor, "weekday": weekday, "start_time": a, "end_time": b}


def exc(doctor, kind, a, b):
    return {"doctor_id": doctor, "kind": kind, "start_at": a, "end_at": b}


# ---- generator ---------------------------------------------------------------
def test_template_is_clipped_to_opening_hours_and_aligned():
    iv = working_intervals(R, [block(1, 3, "08:00", "10:10")], [], 1, THU, THU)
    assert iv == [(at(THU, "09:00"), at(THU, "10:10"))]
    c = sorted(cells(iv, 15))
    assert [a.astimezone(TZ).strftime("%H:%M") for a, _ in c] == ["09:00", "09:15", "09:30", "09:45"]  # 10:00 cell won't fit


def test_off_and_extra_exceptions():
    avail = [block(1, 3, "09:00", "13:00")]
    excs = [exc(1, "off", at(THU, "10:00"), at(THU, "11:00")),           # doctor's blocked hour
            exc(None, "off", at(THU, "12:30"), at(THU, "17:00")),        # clinic-wide afternoon closure
            exc(1, "extra", at(THU, "14:00"), at(THU, "15:00")),         # extra hours (inside the closure!)
            exc(2, "extra", at(THU, "16:00"), at(THU, "17:00"))]         # another doctor's extra: ignored
    iv = working_intervals(R, avail, excs, 1, THU, THU)
    assert iv == [(at(THU, "09:00"), at(THU, "10:00")), (at(THU, "11:00"), at(THU, "12:30"))]


def test_closed_days_and_holidays_produce_nothing():
    sunday, christmas = date(2026, 11, 22), date(2026, 12, 25)
    avail = [block(1, d, "09:00", "12:00") for d in range(7)]
    assert working_intervals(R, avail, [], 1, sunday, sunday) == []
    assert working_intervals(R, avail, [], 1, christmas, christmas) == []


def test_booking_minutes_rounds_up_to_the_grid():
    assert booking_minutes(R, 20) == 30 and booking_minutes(R, 45) == 45 and booking_minutes(R, None) == 30


# ---- regenerate + booking ------------------------------------------------------
def test_regenerate_is_idempotent_and_keeps_bookings(sqlite_store):
    assert regenerate(sqlite_store) == {"added": 0, "removed": 0, "conflicts": []}  # demo grid already matches
    appt = next(iter(sqlite_store.appointments_between(datetime.now(UTC), datetime.now(UTC) + timedelta(days=31))))
    day = appt["slot_start"].astimezone(TZ).date()
    sqlite_store.add_exception(doctor_id=appt["doctor_id"], kind="off", start_at=at(day, "00:00"),
                               end_at=at(day + timedelta(days=1), "00:00"))
    summary = regenerate(sqlite_store)
    assert summary["removed"] > 0 and {c["reference"] for c in summary["conflicts"]} == {appt["reference"]}
    assert sqlite_store.appointment_detail(appt["reference"])["status"] == "booked"


def _search(service_id=None, days=(5, 9), **extra):
    today = datetime.now(TZ).date()
    args = {"date_from": str(today + timedelta(days=days[0])), "date_to": str(today + timedelta(days=days[1])), **extra}
    if service_id:
        args["service_id"] = service_id
    payload, err = run_tool("check_available_slots", args, "s")
    assert not err, payload
    return payload


def test_service_search_only_offers_doctors_and_times_that_fit(sqlite_store):
    p = _search(service_id=6)  # Skin check: Dr. Watanabe only, 30 minutes
    assert p["slots"] and {s["doctor_id"] for s in p["slots"]} == {4}
    assert all(s["duration_minutes"] == 30 for s in p["slots"])
    p = _search(service_id=3)  # Health check-up, 45 min: every offered start must have 45 free minutes
    assert p["slots"] and {s["doctor_id"] for s in p["slots"]} <= {1, 2}


def test_long_service_books_consecutive_cells_and_cancel_frees_them(sqlite_store):
    slot = _search(service_id=2)["slots"][0]  # Extended consultation, 40 -> 45 minutes
    payload, err = run_tool("book_appointment", {"slot_id": slot["slot_id"], "service_id": 2, "full_name": "Long Visit",
                                                 "email": "long@example.com", "phone": "+1 555 0142", "reason": "x"}, "s")
    assert not err and payload["duration_minutes"] == 45
    detail = sqlite_store.appointment_detail(payload["reference"])
    assert detail["slot_end"] - detail["slot_start"] == timedelta(minutes=45) and detail["service"] == "Extended consultation"
    start = detail["slot_start"]
    booked = [s for s in sqlite_store.export_all(["slots"])["slots"] if s["is_booked"] and s["doctor_id"] == detail["doctor_id"]
              and start <= s["start_at"] < start + timedelta(minutes=45)]
    assert len(booked) == 3

    # A 30-minute booking starting inside that visit is refused (overlap), even with a different start time.
    inner = next(s for s in sqlite_store.export_all(["slots"])["slots"]
                 if s["doctor_id"] == detail["doctor_id"] and s["start_at"] == start + timedelta(minutes=15))
    p2, err = run_tool("book_appointment", {"slot_id": inner["id"], "full_name": "Overlap Test", "email": "o@example.com",
                                            "phone": "+1 555 0143", "reason": "x"}, "s")
    assert err and p2["denied_by_rule"] == "slot_unavailable"

    payload, err = run_tool("cancel_appointment", {"reference": payload["reference"], "email": "long@example.com",
                                                   "phone": "+1 555 0142"}, "s")
    assert not err
    assert not any(s["is_booked"] for s in sqlite_store.export_all(["slots"])["slots"] if s["id"] in {b["id"] for b in booked})


def test_service_not_offered_by_doctor_is_denied(sqlite_store):
    slot = _search(specialty="Dermatology")["slots"][0]
    payload, err = run_tool("book_appointment", {"slot_id": slot["slot_id"], "service_id": 8, "full_name": "Wrong Doc",
                                                 "email": "w@example.com", "phone": "+1 555 0144", "reason": "x"}, "s")
    assert err and payload["denied_by_rule"] == "service.doctor"


# ---- admin calendar API --------------------------------------------------------
@pytest.fixture
def client(sqlite_store):
    app = FastAPI()
    app.include_router(calendar.router)
    app.dependency_overrides[current_principal] = lambda: TOKEN_PRINCIPAL
    return TestClient(app)


def test_calendar_view_and_edits(client, sqlite_store):
    today = datetime.now(TZ).date()
    monday = today + timedelta(days=7 - today.weekday())
    rng = {"start": f"{monday}T00:00:00", "end": f"{monday + timedelta(days=7)}T00:00:00"}
    data = client.get("/api/admin/calendar", params={**rng, "doctor_id": 5}).json()
    assert data["slot_minutes"] == 15 and data["working"] and all(w["doctor_id"] == 5 for w in data["working"])

    # Drag-create a Friday afternoon block for Dr. Fischer -> new bookable cells.
    r = client.post("/api/admin/availability", json={"doctor_id": 5, "weekday": 4, "start_time": "14:00", "end_time": "18:00"})
    added = r.json()["slots"]["added"]
    assert r.status_code == 201 and added % 12 == 0 and added >= 48  # 14:00-17:00 (closing) = 12 cells, every Friday
    assert "opening hours" in r.json()["warnings"][0]
    block_id = r.json()["availability"]["id"]

    # A day off removes that day's cells.
    r = client.post("/api/admin/exceptions", json={"doctor_id": 5, "kind": "off", "start": f"{monday}T00:00",
                                                   "end": f"{monday + timedelta(days=1)}T00:00", "note": "Training"})
    assert r.status_code == 201 and r.json()["slots"]["removed"] > 0
    assert client.delete(f"/api/admin/exceptions/{r.json()['exception']['id']}").json()["slots"]["added"] > 0
    assert client.delete(f"/api/admin/availability/{block_id}").json()["slots"]["removed"] == added

    for bad in [{"doctor_id": 5, "weekday": 1, "start_time": "15:00", "end_time": "14:00"},
                {"doctor_id": 99, "weekday": 1, "start_time": "09:00", "end_time": "10:00"},
                {"doctor_id": 5, "weekday": 1, "start_time": "9am", "end_time": "10:00"}]:
        assert client.post("/api/admin/availability", json=bad).status_code == 400


def test_staff_can_view_and_cancel_inside_48h(client):
    detail = client.get("/api/admin/appointments/DEMO0001").json()
    assert detail["patient"] == "Liam Carter" and detail["service"] == "General consultation"
    r = client.post("/api/admin/appointments/demo0001/cancel", json={"note": "Patient called"})
    assert r.status_code == 200 and r.json()["status"] == "cancelled"
    assert client.post("/api/admin/appointments/DEMO0001/cancel").status_code == 400  # already cancelled
