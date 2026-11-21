"""Demo clinic: 6 doctors, 10 services, weekly availability, 2 weeks of slots, fake patients."""

from __future__ import annotations

from datetime import UTC, date, datetime, time, timedelta
from zoneinfo import ZoneInfo

from ..rules import Rules

DOCTORS = [
    (1, "Dr. Amelia Hart", "General Practice", "en, de", "Family doctor with 15 years of experience in preventive care."),
    (2, "Dr. Rohan Mehta", "General Practice", "en", "GP with a focus on chronic conditions and travel medicine."),
    (3, "Dr. Sofia Alvarez", "Pediatrics", "en, es", "Children's doctor, from newborns to teenagers."),
    (4, "Dr. Kenji Watanabe", "Dermatology", "en", "Skin specialist: skin checks, acne and eczema."),
    (5, "Dr. Lena Fischer", "Gynecology", "de, en", "Gynecologist offering routine exams and prenatal care."),
    (6, "Dr. Marcus Bell", "Orthopedics", "en", "Joint and back pain, sports injuries."),
]

# id, name, minutes, price (None = on request), description, doctor ids
SERVICES = [
    (1, "General consultation", 20, 45.0, "A standard visit for a new or ongoing concern.", [1, 2]),
    (2, "Extended consultation", 40, 80.0, "More time for several concerns or a complex history.", [1, 2]),
    (3, "Health check-up", 45, 90.0, "Routine check-up with blood pressure and basic blood tests.", [1, 2]),
    (4, "Vaccination", 15, 30.0, "Flu, travel and routine vaccinations (vaccine cost extra).", [1, 2, 3]),
    (5, "Child check-up", 30, 60.0, "Developmental check and growth measurements.", [3]),
    (6, "Skin check", 30, 75.0, "Full-body mole and skin check.", [4]),
    (7, "Acne consultation", 30, 70.0, "Assessment and treatment plan for acne.", [4]),
    (8, "Gynecological exam", 30, 85.0, "Routine gynecological examination.", [5]),
    (9, "Prenatal visit", 30, None, "Pregnancy check-up. Usually covered by insurance.", [5]),
    (10, "Joint pain assessment", 30, 70.0, "Examination of joint, back or sports-related pain.", [6]),
]

# doctor id -> list of (weekdays, start, end)
AVAILABILITY = {
    1: [([0, 1, 2, 3, 4], "09:00", "13:00"), ([1, 3], "14:00", "17:00"), ([5], "09:00", "12:00")],
    2: [([0, 2, 4], "10:00", "17:00"), ([1], "09:00", "12:00")],
    3: [([0, 1, 3], "09:00", "15:00"), ([5], "10:00", "13:00")],
    4: [([1, 2], "13:00", "17:00"), ([4], "09:00", "12:00")],
    5: [([0, 3], "09:00", "13:00"), ([2], "14:00", "17:00")],
    6: [([1, 4], "09:00", "13:00"), ([2], "09:00", "11:00")],
}

PATIENTS = [
    (1, "Liam Carter", "liam.carter@example.com", "+1-555-0111"),
    (2, "Olivia Bennett", "olivia.bennett@example.com", "+1-555-0112"),
    (3, "Noah Fischer", "noah.fischer@example.com", "+1-555-0113"),
    (4, "Emma Schneider", "emma.schneider@example.com", "+1-555-0114"),
    (5, "Ava Thompson", "ava.thompson@example.com", "+1-555-0115"),
    (6, "Lucas Wagner", "lucas.wagner@example.com", "+1-555-0116"),
    (7, "Mia Hoffmann", "mia.hoffmann@example.com", "+1-555-0117"),
    (8, "Ethan Brooks", "ethan.brooks@example.com", "+1-555-0118"),
    (9, "Sophie Keller", "sophie.keller@example.com", "+1-555-0119"),
    (10, "James Okafor", "james.okafor@example.com", "+1-555-0120"),
]



COLORS = {1: "#0f9d94", 2: "#0b7a73", 3: "#3b82f6", 4: "#6366f1", 5: "#f59e0b", 6: "#ec4899",
          7: "#a855f7", 8: "#ef4444", 9: "#f97316", 10: "#64748b"}

# (patient id, service id, preferred doctor, days ahead): DEMO0001 is the earliest possible booking.
BOOKINGS = [(1, 1, 1, 0), (2, 3, 2, 3), (3, 4, 3, 4), (4, 6, 4, 5), (5, 10, 6, 6), (6, 5, 3, 7)]
REASONS = ["Annual check-up", "Health check", "Vaccination", "Skin check", "Back pain", "Child check-up"]


def build_demo(rules: Rules, now: datetime | None = None, days: int | None = None, with_bookings: bool = True) -> dict[str, list[dict]]:
    """Demo clinic. Slots come from the real generator (templates + exceptions) for the booking window."""
    from ..scheduling.generator import booking_minutes, desired_cells

    tz: ZoneInfo = rules.tz
    now = now or datetime.now(UTC)
    today = now.astimezone(tz).date()
    days = days if days is not None else int(rules.booking["max_days_ahead"]) + 1

    data: dict[str, list[dict]] = {
        "doctors": [dict(id=i, full_name=n, specialty=s, languages=lang, bio=bio, photo_url="", active=True)
                    for i, n, s, lang, bio in DOCTORS],
        "services": [dict(id=i, name=n, duration_minutes=m, price=p, description=d, color=COLORS[i], active=True)
                     for i, n, m, p, d, _ in SERVICES],
        "doctor_services": [dict(doctor_id=d, service_id=i) for i, *_, docs in SERVICES for d in docs],
        "availability": [],
        "schedule_exceptions": [],
        "slots": [],
        "patients": [],
        "appointments": [],
        "appointment_slots": [],
        "audit_log": [],
    }
    for doctor_id, blocks in AVAILABILITY.items():
        for weekdays, start, end in blocks:
            for wd in weekdays:
                data["availability"].append(dict(doctor_id=doctor_id, weekday=wd, start_time=start, end_time=end))

    # Exceptions: Dr. Mehta is off sick for a day next week; Dr. Watanabe adds a Saturday morning.
    def at(day: date, hhmm: str) -> datetime:
        return datetime.combine(day, time.fromisoformat(hhmm), tz).astimezone(UTC)

    next_mon = today + timedelta(days=7 - today.weekday())
    next_sat = next_mon + timedelta(days=5)
    data["schedule_exceptions"] = [
        dict(id=1, doctor_id=2, kind="off", start_at=at(next_mon + timedelta(days=2), "00:00"),
             end_at=at(next_mon + timedelta(days=3), "00:00"), note="Sick day"),
        dict(id=2, doctor_id=4, kind="extra", start_at=at(next_sat, "09:00"), end_at=at(next_sat, "12:00"),
             note="Extra Saturday skin-check clinic"),
    ]

    cells = desired_cells(rules, [d[0] for d in DOCTORS], data["availability"], data["schedule_exceptions"],
                          today, today + timedelta(days=days - 1))
    slot_id = 0
    index: dict[tuple[int, datetime], dict] = {}
    for doctor_id in sorted(cells):
        for cell_start, cell_end in sorted(cells[doctor_id]):
            slot_id += 1
            row = dict(id=slot_id, doctor_id=doctor_id, start_at=cell_start, end_at=cell_end, is_booked=False)
            data["slots"].append(row)
            index[(doctor_id, cell_start)] = row

    if not with_bookings:
        return data

    created = now - timedelta(days=3)
    data["patients"] = [dict(id=i, full_name=n, email=e, phone=p, created_at=created) for i, n, e, p in PATIENTS]

    step = timedelta(minutes=rules.slot_minutes)
    earliest = now + timedelta(hours=3)
    for n, (patient_id, service_id, doctor_id, ahead) in enumerate(BOOKINGS, start=1):
        minutes = booking_minutes(rules, next(s[2] for s in SERVICES if s[0] == service_id))
        need = minutes // rules.slot_minutes
        not_before = max(earliest, datetime.combine(today + timedelta(days=ahead), time.min, tz))
        for s in sorted((s for s in data["slots"] if s["doctor_id"] == doctor_id), key=lambda s: s["start_at"]):
            run = [index.get((doctor_id, s["start_at"] + k * step)) for k in range(need)]
            if s["start_at"] >= not_before and all(r and not r["is_booked"] for r in run):
                break
        else:
            continue
        found = [r for r in run if r is not None]
        for r in found:
            r["is_booked"] = True
            data["appointment_slots"].append(dict(appointment_id=n, slot_id=r["id"]))
        data["appointments"].append(dict(
            id=n, reference=f"DEMO{n:04d}", patient_id=patient_id, doctor_id=doctor_id, slot_id=found[0]["id"],
            slot_start=found[0]["start_at"], slot_end=found[0]["start_at"] + timedelta(minutes=minutes),
            service_id=service_id, reason=REASONS[n - 1], status="booked", created_at=created,
        ))
    return data
