"""Loads config/rules.yaml and answers every 'is this allowed?' question the tools ask."""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from functools import lru_cache

import yaml

from .config import ClinicProfile, ConfigError, get_settings

DAYS = ["monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"]


@dataclass(frozen=True)
class Decision:
    allowed: bool
    rule: str | None = None
    reason: str | None = None


ALLOW = Decision(True)


def deny(rule: str, reason: str) -> Decision:
    return Decision(False, rule, reason)


def _messages(value) -> dict[str, str]:
    """rules.yaml `message`: one text, or {language code: text}."""
    if isinstance(value, dict):
        return {str(k).lower(): " ".join(str(v).split()) for k, v in value.items()}
    return {"en": " ".join(str(value).split())}


@dataclass(frozen=True)
class Escalation:
    category: str
    action: str  # "block" | "handoff"
    keyword: str
    messages: tuple[tuple[str, str], ...]

    def text(self, lang: str = "en") -> str:
        m = dict(self.messages)
        return m.get(lang) or m.get("en") or next(iter(m.values()))

    @property
    def message(self) -> str:
        return self.text("en")


class Rules:
    def __init__(self, data: dict, clinic: ClinicProfile):
        self.data = data
        self.clinic = clinic.model_dump()  # name, phone, timezone, ... from clinic.yaml
        self.tz = clinic.tz
        self.booking = data["booking"]
        self.slot_minutes = int(self.booking.get("slot_minutes", 15))
        if self.slot_minutes not in (5, 10, 15, 20, 30, 60):
            raise ValueError("booking.slot_minutes must be 5, 10, 15, 20, 30 or 60")
        self.default_duration = int(self.booking.get("default_duration_minutes", 30))
        if self.default_duration % self.slot_minutes:
            raise ValueError("booking.default_duration_minutes must be a multiple of booking.slot_minutes")
        self.cancellation = data["cancellation"]
        self.closed_dates = {date.fromisoformat(str(d)) for d in data.get("closed_dates") or []}
        self._hours: dict[str, tuple[time, time] | None] = {}
        for day in DAYS:
            spec = data["opening_hours"].get(day)
            self._hours[day] = (time.fromisoformat(spec[0]), time.fromisoformat(spec[1])) if spec else None
        self.reschedule = data.get("reschedule") or {"min_notice_hours": self.cancellation["min_notice_hours"]}
        self._escalations = []
        self.escalation_messages: dict[str, dict[str, str]] = {}
        for name, cat in (data.get("escalation", {}).get("categories") or {}).items():
            msgs = _messages(cat["message"])
            self.escalation_messages[name] = msgs
            for kw in dict.fromkeys(cat["keywords"]):
                pattern = re.compile(r"(?<!\w)" + re.escape(kw.lower()) + r"(?!\w)")
                self._escalations.append((pattern, Escalation(name, cat["action"], kw, tuple(msgs.items()))))

    def escalation_text(self, category: str, lang: str = "en") -> str | None:
        msgs = self.escalation_messages.get(category)
        if not msgs:
            return None
        return msgs.get(lang) or msgs.get("en") or next(iter(msgs.values()))

    # ---- opening hours -------------------------------------------------
    def opening_window(self, local_day: date) -> tuple[time, time] | None:
        if local_day in self.closed_dates:
            return None
        return self._hours[DAYS[local_day.weekday()]]

    def within_opening_hours(self, start: datetime, end: datetime) -> bool:
        s, e = start.astimezone(self.tz), end.astimezone(self.tz)
        if s.date() != e.date():
            return False
        window = self.opening_window(s.date())
        return window is not None and window[0] <= s.time() and e.time() <= window[1]

    # ---- per-action checks ---------------------------------------------
    def check_slot_search(self, date_from: date, date_to: date, today: date) -> Decision:
        max_ahead = self.booking["max_days_ahead"]
        span = self.booking["max_search_span_days"]
        if date_to < date_from:
            return deny("input", "date_to must be on or after date_from.")
        if date_to < today:
            return deny("booking.past", "That date range is in the past.")
        if date_from > today + timedelta(days=max_ahead):
            return deny("booking.max_days_ahead", f"We only take bookings up to {max_ahead} days ahead.")
        if (date_to - date_from).days + 1 > span:
            return deny("booking.max_search_span_days", f"Please search at most {span} days at a time.")
        return ALLOW

    def slot_bookable(self, start: datetime, end: datetime, now: datetime) -> Decision:
        lead = self.booking["min_lead_time_hours"]
        max_ahead = self.booking["max_days_ahead"]
        if start <= now:
            return deny("booking.past", "That slot has already started.")
        if start < now + timedelta(hours=lead):
            return deny("booking.min_lead_time_hours", f"Appointments must be booked at least {lead} hours in advance.")
        if start > now + timedelta(days=max_ahead):
            return deny("booking.max_days_ahead", f"We only take bookings up to {max_ahead} days ahead.")
        if not self.within_opening_hours(start, end):
            return deny("opening_hours", "That time is outside the clinic's opening hours.")
        return ALLOW

    def check_booking(self, start: datetime, end: datetime, now: datetime, active_appointments: int) -> Decision:
        d = self.slot_bookable(start, end, now)
        if not d.allowed:
            return d
        limit = self.booking["max_active_appointments_per_patient"]
        if active_appointments >= limit:
            return deny(
                "booking.max_active_appointments_per_patient",
                f"Patients can hold at most {limit} upcoming appointments.",
            )
        return ALLOW

    def check_cancellation(self, start: datetime, now: datetime) -> Decision:
        hours = self.cancellation["min_notice_hours"]
        if start <= now:
            return deny("cancellation.past", "That appointment has already started or passed.")
        if start - now < timedelta(hours=hours):
            return deny(
                "cancellation.min_notice_hours",
                f"Less than {hours} hours' notice. {self.cancellation['policy_text'].strip()} "
                f"Please call {self.clinic['phone']}.",
            )
        return ALLOW

    def check_reschedule(self, current_start: datetime, now: datetime) -> Decision:
        """Can the *existing* appointment still be moved online? (The new time is checked with check_booking.)"""
        hours = self.reschedule.get("min_notice_hours", self.cancellation["min_notice_hours"])
        if current_start <= now:
            return deny("reschedule.past", "That appointment has already started or passed.")
        if current_start - now < timedelta(hours=hours):
            return deny(
                "reschedule.min_notice_hours",
                f"Appointments can only be moved online up to {hours} hours before they start. "
                f"Please call {self.clinic['phone']}.",
            )
        return ALLOW

    # ---- escalation ------------------------------------------------------
    def classify_escalation(self, text: str) -> Escalation | None:
        lowered = text.lower().replace("’", "'")
        for pattern, esc in self._escalations:  # yaml order: urgent first
            if pattern.search(lowered):
                return esc
        return None

    # ---- prompt summary --------------------------------------------------
    def summary(self) -> str:
        hours = []
        for day in DAYS:
            w = self._hours[day]
            hours.append(f"{day.title()}: {w[0]:%H:%M}-{w[1]:%H:%M}" if w else f"{day.title()}: closed")
        b, c = self.booking, self.cancellation
        closed = ", ".join(sorted(d.isoformat() for d in self.closed_dates)) or "none"
        return (
            f"Clinic: {self.clinic['name']} (phone {self.clinic['phone']}), timezone {self.clinic['timezone']}.\n"
            f"Opening hours: {'; '.join(hours)}. Closed dates: {closed}.\n"
            f"Booking: at least {b['min_lead_time_hours']}h in advance, at most {b['max_days_ahead']} days ahead, "
            f"max {b['max_active_appointments_per_patient']} upcoming appointments per patient.\n"
            f"Cancellation: {c['policy_text'].strip()}"
        )


@lru_cache(maxsize=1)
def get_rules() -> Rules:
    settings = get_settings()
    path = settings.rules_file
    if not path.is_file():
        raise ConfigError(f"Rules file not found at {path}. Copy config/rules.yaml from the repository.")
    try:
        with open(path, encoding="utf-8") as f:
            return Rules(yaml.safe_load(f), settings.clinic)
    except (KeyError, TypeError, ValueError, yaml.YAMLError) as e:
        raise ConfigError(f"rules.yaml is invalid ({type(e).__name__}: {e}). Check the file against the comments in it.") from None
