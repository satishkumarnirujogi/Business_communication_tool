"""Day-before appointment reminders, sent through the channel the patient booked on.

- Booked on WhatsApp -> WhatsApp (a template if the 24-hour window has closed).
- Booked by email, in the web chat, or by staff -> email to the patient's address, if email sending is set up.
  The web chat can't reach a patient who has closed the page.
- Otherwise the reminder is marked skipped with the reason, visible in the admin dashboard.

Sent at REMINDER_SEND_TIME (clinic time) the day before. An appointment booked after that time gets its
reminder straight away, unless it starts within REMINDER_MIN_HOURS. Rescheduled appointments get a new one.
Failures are retried up to three times, 30 minutes apart.
"""

from __future__ import annotations

import logging
import threading
from datetime import UTC, datetime, time, timedelta

from .. import audit
from ..config import get_settings
from ..data import get_store
from ..data.base import Row
from ..rules import get_rules
from .base import Channel, ChannelError, Reminder
from .mail import EmailChannel

log = logging.getLogger("clinic.reminders")

MAX_ATTEMPTS = 3
RETRY_AFTER = timedelta(minutes=30)

WEEKDAYS = {"en": ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"],
            "de": ["Montag", "Dienstag", "Mittwoch", "Donnerstag", "Freitag", "Samstag", "Sonntag"]}
MONTHS = {"en": ["January", "February", "March", "April", "May", "June", "July", "August", "September", "October",
                 "November", "December"],
          "de": ["Januar", "Februar", "März", "April", "Mai", "Juni", "Juli", "August", "September", "Oktober",
                 "November", "Dezember"]}
TEXT = {
    "en": ("Hello {name}, this is a reminder from {clinic}: your appointment{service} with {doctor} is on {when}.\n"
           "Reference: {reference}{address}\n"
           "If you can't make it, please let us know as soon as possible: reply to this message or call {phone}."),
    "de": ("Guten Tag {name}, eine Erinnerung von {clinic}: Ihr Termin{service} bei {doctor} ist am {when}.\n"
           "Buchungsnummer: {reference}{address}\n"
           "Falls Sie den Termin nicht wahrnehmen können, sagen Sie bitte so früh wie möglich Bescheid: antworten "
           "Sie auf diese Nachricht oder rufen Sie {phone} an."),
}
SUBJECT = {"en": "Reminder: your appointment on {date}", "de": "Erinnerung: Ihr Termin am {date}"}


def _utc(dt: datetime) -> datetime:
    return dt if dt.tzinfo else dt.replace(tzinfo=UTC)


def format_when(start: datetime, lang: str) -> tuple[str, str]:
    """('Friday 21 November at 09:30', 'Friday 21 November') in the clinic's timezone."""
    local = _utc(start).astimezone(get_rules().tz)
    lang = lang if lang in WEEKDAYS else "en"
    wd, month = WEEKDAYS[lang][local.weekday()], MONTHS[lang][local.month - 1]
    if lang == "de":
        date = f"{wd}, {local.day}. {month}"
        return f"{date} um {local:%H:%M} Uhr", date
    date = f"{wd} {local.day} {month}"
    return f"{date} at {local:%H:%M}", date


def build_reminder(a: Row, lang: str) -> Reminder:
    clinic = get_rules().clinic
    when, date = format_when(a["slot_start"], lang)
    name = (a.get("patient") or "").strip() or ("there" if lang == "en" else "")
    service = f" ({a['service']})" if a.get("service") else ""
    address = f"\n{'Address' if lang == 'en' else 'Adresse'}: {clinic['address']}" if clinic.get("address") else ""
    text = TEXT.get(lang, TEXT["en"]).format(name=name, clinic=clinic["name"], service=service, doctor=a["doctor"],
                                             when=when, reference=a["reference"], address=address,
                                             phone=clinic["phone"])
    return Reminder(text=text.replace("Guten Tag ,", "Guten Tag,"), language=lang,
                    subject=SUBJECT.get(lang, SUBJECT["en"]).format(date=date),
                    params=[a.get("patient") or "", when, a["doctor"], a["reference"]])


def due_at(start: datetime) -> datetime:
    """REMINDER_SEND_TIME, clinic time, on the day before the appointment."""
    tz = get_rules().tz
    h, m = (int(x) for x in get_settings().reminder_send_time.split(":"))
    day_before = _utc(start).astimezone(tz).date() - timedelta(days=1)
    return datetime.combine(day_before, time(h, m), tzinfo=tz).astimezone(UTC)


def route(a: Row) -> tuple[str | None, str, Channel | None]:
    """(channel name, address, channel) to use, or (None, reason, None)."""
    booked_on = a.get("r_channel") or "staff"
    if booked_on == "whatsapp":
        from . import whatsapp_channel

        ch = whatsapp_channel()
        if ch is not None and ch.configured and a.get("thread_address"):
            return "whatsapp", a["thread_address"], ch
    mail = EmailChannel(get_settings())
    if mail.can_send() and a.get("email"):
        address = a["thread_address"] if booked_on == "email" and a.get("thread_address") else a["email"]
        return "email", address, mail
    if booked_on == "whatsapp":
        return None, "WhatsApp is no longer set up and email sending is off.", None
    return None, "No channel can reach this patient: turn on email sending in Settings → Channels.", None


def run_once(now: datetime | None = None) -> dict[str, int]:
    """Send every reminder that is due. Returns counts per outcome."""
    s = get_settings()
    counts = {"sent": 0, "failed": 0, "skipped": 0}
    if not s.reminders_enabled:
        return counts
    now = now or datetime.now(UTC)
    store = get_store()
    langs = get_rules().clinic.get("languages") or ["en"]
    for a in store.reminder_candidates(now, now + timedelta(hours=48)):
        start = _utc(a["slot_start"])
        sent_for = _utc(a["r_sent_for"]) if a.get("r_sent_for") else None
        status = a.get("r_status")
        if sent_for == start:
            if status in ("sent", "skipped"):
                continue
            if status == "failed" and (a.get("r_attempts", 0) >= MAX_ATTEMPTS
                                       or now - _utc(a["r_updated_at"]) < RETRY_AFTER):
                continue
        if now < due_at(start):
            continue
        attempts_so_far = a.get("r_attempts", 0) if sent_for == start else 0
        params = {"reference": a["reference"]}
        if start - now < timedelta(hours=s.reminder_min_hours):
            store.save_reminder(a["id"], status="skipped", sent_for=start, attempt=False, at=now,
                                error=f"Booked less than {s.reminder_min_hours:g} hours before the appointment.")
            counts["skipped"] += 1
            continue
        name, address, ch = route(a)
        if name is None:
            store.save_reminder(a["id"], status="skipped", sent_for=start, error=address, attempt=False, at=now)
            audit.record("reminder.send", "denied", actor="reminders", rule="reminders.channel", reason=address,
                         params=params)
            counts["skipped"] += 1
            continue
        lang = str(a["r_language"]) if a.get("r_language") in langs else langs[0]
        assert ch is not None
        try:
            ch.send_reminder(address, build_reminder(a, lang), last_inbound_at=a.get("thread_last_inbound_at"))
        except ChannelError as e:
            if attempts_so_far + 1 >= MAX_ATTEMPTS:
                store.add_task(kind="callback", summary=f"Appointment reminder for {a['reference']} could not be sent "
                               f"by {name}: {e}", patient_name=a.get("patient") or "", contact=a.get("phone") or "")
            store.save_reminder(a["id"], status="failed", sent_for=start, sent_via=name, error=str(e), at=now)
            audit.record("reminder.send", "error", actor="reminders", reason=str(e), params={**params, "via": name})
            counts["failed"] += 1
            continue
        store.save_reminder(a["id"], status="sent", sent_for=start, sent_via=name, at=now)
        audit.record("reminder.send", "allowed", actor="reminders", params={**params, "via": name})
        counts["sent"] += 1
    return counts


class ReminderWorker:
    def __init__(self, interval_seconds: float = 300):
        self.interval = interval_seconds
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, name="reminders", daemon=True)

    def start(self) -> None:
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        self._thread.join(timeout=10)

    def _run(self) -> None:
        self._stop.wait(30)  # let startup finish first
        while not self._stop.is_set():
            try:
                counts = run_once()
                if any(counts.values()):
                    log.info("reminders: %s", counts)
            except Exception:
                log.exception("reminder run failed")
            self._stop.wait(self.interval)
