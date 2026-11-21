"""Turn weekly availability templates + exceptions into bookable grid cells.

working time(day, doctor) = (weekly template blocks  ∪  'extra' exceptions)  ∩  clinic opening hours
                            −  'off' exceptions (the doctor's own and clinic-wide)
cells = working time cut into rules.yaml `booking.slot_minutes` pieces, aligned to the clock (09:00, 09:15, ...)
"""

from __future__ import annotations

import math
from collections.abc import Iterable
from datetime import UTC, date, datetime, time, timedelta

from ..rules import Rules

Interval = tuple[datetime, datetime]


def booking_minutes(rules: Rules, service_minutes: int | None) -> int:
    """A service's length rounded up to the grid (20 min on a 15-min grid books 30 min)."""
    minutes = service_minutes or rules.default_duration
    return math.ceil(minutes / rules.slot_minutes) * rules.slot_minutes


def _merge(intervals: Iterable[Interval]) -> list[Interval]:
    out: list[Interval] = []
    for a, b in sorted(intervals):
        if out and a <= out[-1][1]:
            out[-1] = (out[-1][0], max(out[-1][1], b))
        else:
            out.append((a, b))
    return out


def _subtract(intervals: list[Interval], holes: list[Interval]) -> list[Interval]:
    out = []
    for a, b in intervals:
        pieces = [(a, b)]
        for ha, hb in holes:
            nxt = []
            for pa, pb in pieces:
                if hb <= pa or ha >= pb:
                    nxt.append((pa, pb))
                    continue
                if pa < ha:
                    nxt.append((pa, ha))
                if hb < pb:
                    nxt.append((hb, pb))
            pieces = nxt
        out.extend(pieces)
    return out


def _intersect(intervals: list[Interval], window: Interval) -> list[Interval]:
    wa, wb = window
    return [(max(a, wa), min(b, wb)) for a, b in intervals if min(b, wb) > max(a, wa)]


def working_intervals(rules: Rules, availability: list[dict], exceptions: list[dict], doctor_id: int,
                      day_from: date, day_to: date) -> list[Interval]:
    """Working time for one doctor, [day_from, day_to] inclusive, as aware datetimes in the clinic timezone."""
    tz = rules.tz
    blocks = [a for a in availability if a["doctor_id"] == doctor_id]
    mine = [e for e in exceptions if e["doctor_id"] in (doctor_id, None)]
    extra = [(e["start_at"].astimezone(tz), e["end_at"].astimezone(tz)) for e in mine
             if e["kind"] == "extra" and e["doctor_id"] == doctor_id]
    off = [(e["start_at"].astimezone(tz), e["end_at"].astimezone(tz)) for e in mine if e["kind"] == "off"]

    out: list[Interval] = []
    day = day_from
    while day <= day_to:
        window = rules.opening_window(day)
        if window:
            opening = (datetime.combine(day, window[0], tz), datetime.combine(day, window[1], tz))
            todays = [
                (datetime.combine(day, time.fromisoformat(b["start_time"]), tz),
                 datetime.combine(day, time.fromisoformat(b["end_time"]), tz))
                for b in blocks if b["weekday"] == day.weekday()
            ]
            day_start = datetime.combine(day, time.min, tz)
            todays += _intersect(extra, (day_start, day_start + timedelta(days=1)))
            out.extend(_intersect(_merge(todays), opening))
        day += timedelta(days=1)
    return _merge(_subtract(_merge(out), off))


def cells(intervals: list[Interval], slot_minutes: int) -> set[tuple[datetime, datetime]]:
    """Clock-aligned cells fully inside the intervals, in UTC."""
    step = timedelta(minutes=slot_minutes)
    out = set()
    for a, b in intervals:
        midnight = a.replace(hour=0, minute=0, second=0, microsecond=0)
        k = math.ceil((a - midnight) / step)
        t = midnight + k * step
        while t + step <= b:
            out.add((t.astimezone(UTC), (t + step).astimezone(UTC)))
            t += step
    return out


def desired_cells(rules: Rules, doctor_ids: list[int], availability: list[dict], exceptions: list[dict],
                  day_from: date, day_to: date) -> dict[int, set[tuple[datetime, datetime]]]:
    return {
        d: cells(working_intervals(rules, availability, exceptions, d, day_from, day_to), rules.slot_minutes)
        for d in doctor_ids
    }
