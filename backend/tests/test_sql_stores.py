"""SQL stores through the real agent tools: booking, double booking, lookup, cancellation, audit."""

import threading
from datetime import UTC, datetime, timedelta

import pytest

from app.agent.tools import run_tool
from app.data import SlotTaken

from .conftest import bookable_slot

PATIENT = {"full_name": "Test Patient", "email": "test.patient@example.com", "phone": "+1 555 0199", "reason": "Check-up"}


def book(slot_id, **over):
    return run_tool("book_appointment", {"slot_id": slot_id, **PATIENT, **over}, "test-session")


def test_demo_data_counts(sql_store):
    assert len(sql_store.list_doctors()) == 6
    services = sql_store.list_services()
    assert len(services) == 10 and services[0]["doctor_ids"] == [1, 2]
    assert sql_store.list_availability(doctor_id=1)


def test_book_lookup_cancel_rebook(sql_store):
    slot = bookable_slot(sql_store)
    payload, err = book(slot["id"])
    assert not err and payload["ok"] and payload["new_patient_registered"]
    ref = payload["reference"]

    payload, err = run_tool("lookup_appointment", {"email": PATIENT["email"], "phone": "+15550199"}, "s")
    assert not err and [a["reference"] for a in payload["appointments"]] == [ref]
    assert payload["appointments"][0]["can_cancel_online"]

    payload, err = run_tool("cancel_appointment", {"reference": ref, "email": PATIENT["email"], "phone": PATIENT["phone"]}, "s")
    assert not err and payload["status"] == "cancelled"

    # The slot is free again and can be booked (the unique index only covers *booked* rows).
    payload, err = book(slot["id"])
    assert not err and payload["ok"]


def test_second_booking_of_same_slot_is_denied(sql_store):
    slot = bookable_slot(sql_store)
    assert book(slot["id"])[0]["ok"]
    payload, err = book(slot["id"], email="someone.else@example.com", full_name="Someone Else")
    assert err and payload["denied_by_rule"] == "slot_unavailable"


def test_unique_index_blocks_double_booking_even_without_the_flag(sql_store):
    """Simulates a stale is_booked flag: the database itself must still refuse the second booking."""
    slot = bookable_slot(sql_store)
    step = slot["end_at"] - slot["start_at"]

    def raw_book(name, email, start, cells):
        with sql_store.transaction() as tx:
            p = tx.insert_patient(name, email, "+1 555 0001")
            cs = tx.cells_for_update(slot["doctor_id"], start, start + cells * step)
            a = tx.insert_appointment(patient_id=p["id"], doctor_id=slot["doctor_id"], start=start,
                                      end=start + cells * step, slot_id=cs[0]["id"], reason="")
            tx.occupy_cells(a["id"], [c["id"] for c in cs])

    raw_book("A", "a@example.com", slot["start_at"], 2)
    # Same start time -> the (doctor, start) index refuses it.
    with pytest.raises(SlotTaken):
        raw_book("B", "b@example.com", slot["start_at"], 1)
    # Different start but overlapping cells -> the appointment_slots unique index refuses it.
    with pytest.raises(SlotTaken):
        raw_book("C", "c@example.com", slot["start_at"] + step, 2)
    # The failed transactions rolled back completely (patients B and C were not created).
    assert sql_store.patient_by_email("b@example.com") is None
    assert sql_store.patient_by_email("c@example.com") is None


def test_concurrent_bookings_only_one_wins(sql_store):
    slot = bookable_slot(sql_store)
    results, barrier = [], threading.Barrier(5)

    def worker(i):
        barrier.wait()
        results.append(book(slot["id"], email=f"p{i}@example.com", full_name=f"Patient {i}")[0])

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(5)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert sum(r["ok"] for r in results) == 1
    assert all(r.get("denied_by_rule") == "slot_unavailable" for r in results if not r["ok"])


def test_identity_and_48h_rules(sql_store):
    # DEMO0001 is the earliest slot, normally < 48h away.
    with sql_store.transaction() as tx:
        start = tx.appointment_for_update("DEMO0001")["slot_start"]
    payload, err = run_tool("cancel_appointment", {"reference": "DEMO0001", "email": "liam.carter@example.com",
                                                   "phone": "+1-555-0111"}, "s")
    if start - datetime.now(UTC) < timedelta(hours=48):
        assert err and payload["denied_by_rule"] == "cancellation.min_notice_hours"
    else:
        assert not err
    payload, err = run_tool("lookup_appointment", {"email": "liam.carter@example.com", "phone": "+1-555-9999"}, "s")
    assert err and payload["denied_by_rule"] == "identity"


def test_slot_search_and_audit(sql_store):
    today = datetime.now(UTC).date()
    payload, err = run_tool("check_available_slots", {"date_from": str(today + timedelta(days=3)),
                                                      "date_to": str(today + timedelta(days=9)), "specialty": "derm"}, "s")
    assert not err and payload["slots"] and {s["specialty"] for s in payload["slots"]} == {"Dermatology"}
    actions = [r["action"] for r in sql_store.list_audit(5)]
    assert actions[0] == "check_available_slots"
