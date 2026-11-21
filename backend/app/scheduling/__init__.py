"""Doctor calendars: slot generation from weekly templates and exceptions."""

from __future__ import annotations

import logging
import threading
from datetime import UTC, datetime, time, timedelta

from ..rules import get_rules
from .generator import booking_minutes, cells, desired_cells, working_intervals

__all__ = ["booking_minutes", "cells", "desired_cells", "working_intervals", "regenerate", "horizon", "SlotWorker"]

log = logging.getLogger("clinic.scheduling")
_lock = threading.Lock()


def horizon(now: datetime | None = None):
    """(first day, last day, window start, window end): today through max_days_ahead, clinic time."""
    rules = get_rules()
    now = now or datetime.now(UTC)
    today = now.astimezone(rules.tz).date()
    last = today + timedelta(days=int(rules.booking["max_days_ahead"]))
    start = datetime.combine(today, time.min, rules.tz)
    end = datetime.combine(last + timedelta(days=1), time.min, rules.tz)
    return today, last, start, end


def regenerate(store=None, now: datetime | None = None) -> dict:
    """Rebuild the slot grid for the booking window from templates and exceptions. Safe to call any time:
    booked cells are never removed (they are reported as conflicts so staff can rebook those patients)."""
    from ..data import get_store

    store = store or get_store()
    rules = get_rules()
    with _lock:
        first, last, start, end = horizon(now)
        doctor_ids = [d["id"] for d in store.list_doctors(active_only=False)]
        active = {d["id"] for d in store.list_doctors(active_only=True)}
        availability = store.list_availability()
        exceptions = store.list_exceptions(start, end)
        desired = desired_cells(rules, sorted(active), availability, exceptions, first, last)
        for d in doctor_ids:
            desired.setdefault(d, set())  # inactive doctors: remove their free cells
        summary = store.sync_slots(desired, start, end)
    if summary["added"] or summary["removed"]:
        log.info("slots: +%d -%d, %d booking(s) outside working time", summary["added"], summary["removed"],
                 len(summary["conflicts"]))
    return summary


class SlotWorker:
    """Keeps the rolling booking window filled (new days appear at midnight)."""

    def __init__(self, store, interval_seconds: float = 3600):
        self.store, self.interval = store, interval_seconds
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, name="slot-generator", daemon=True)

    def start(self) -> None:
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        self._thread.join(timeout=10)

    def _run(self) -> None:
        while not self._stop.is_set():
            try:
                regenerate(self.store)
            except Exception:
                log.exception("slot generation failed")
            self._stop.wait(self.interval)
