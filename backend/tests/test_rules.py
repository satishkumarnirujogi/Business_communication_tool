from datetime import UTC, date, datetime, timedelta

from app.rules import get_rules

R = get_rules()
UTC = UTC
# Wednesday 2026-11-18 08:00 UTC (rules.yaml timezone is UTC)
NOW = datetime(2026, 11, 18, 8, 0, tzinfo=UTC)


def slot(day: date, hour: int, minutes: int = 30):
    start = datetime(day.year, day.month, day.day, hour, tzinfo=UTC)
    return start, start + timedelta(minutes=minutes)


def test_opening_hours():
    assert R.within_opening_hours(*slot(date(2026, 11, 19), 9))       # Thu 09:00
    assert not R.within_opening_hours(*slot(date(2026, 11, 19), 17))  # after close
    assert R.within_opening_hours(*slot(date(2026, 11, 21), 12))      # Sat 12:00-12:30
    assert not R.within_opening_hours(*slot(date(2026, 11, 21), 13))  # Sat after 13:00
    assert not R.within_opening_hours(*slot(date(2026, 11, 22), 10))  # Sunday
    assert not R.within_opening_hours(*slot(date(2026, 12, 25), 10))  # closed date


def test_booking_rules():
    assert R.check_booking(*slot(date(2026, 11, 19), 10), NOW, 0).allowed
    assert R.check_booking(*slot(date(2026, 11, 18), 9), NOW, 0).rule == "booking.min_lead_time_hours"
    assert R.check_booking(*slot(date(2026, 11, 18), 7), NOW, 0).rule == "booking.past"
    assert R.check_booking(*slot(date(2027, 1, 20), 10), NOW, 0).rule == "booking.max_days_ahead"
    assert R.check_booking(*slot(date(2026, 11, 22), 10), NOW, 0).rule == "opening_hours"
    assert R.check_booking(*slot(date(2026, 11, 19), 10), NOW, 3).rule == "booking.max_active_appointments_per_patient"


def test_cancellation_48h():
    assert R.check_cancellation(NOW + timedelta(hours=49), NOW).allowed
    d = R.check_cancellation(NOW + timedelta(hours=47), NOW)
    assert not d.allowed and d.rule == "cancellation.min_notice_hours"
    assert R.check_cancellation(NOW - timedelta(hours=1), NOW).rule == "cancellation.past"


def test_search_window():
    today = NOW.date()
    assert R.check_slot_search(today, today + timedelta(days=6), today).allowed
    assert R.check_slot_search(today, today + timedelta(days=20), today).rule == "booking.max_search_span_days"
    assert R.check_slot_search(today + timedelta(days=40), today + timedelta(days=41), today).rule == "booking.max_days_ahead"
    assert R.check_slot_search(today + timedelta(days=2), today, today).rule == "input"


def test_escalation():
    assert R.classify_escalation("I have chest pain right now").category == "urgent"
    assert R.classify_escalation("I can’t breathe").category == "urgent"  # curly apostrophe
    e = R.classify_escalation("What dosage of ibuprofen should I take?")
    assert e.category == "medical_question" and e.action == "handoff"
    assert R.classify_escalation("Can I book a GP appointment on Friday?") is None
    assert R.classify_escalation("my doses") is None  # whole words only
