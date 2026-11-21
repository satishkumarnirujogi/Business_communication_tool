"""Spreadsheet layout (Excel and Google Sheets share it) and cell parsing.

One tab per table. Staff edit doctors, services, weekly availability and exceptions (holidays, sick
days, extra hours); bookable slots are generated from those. The app owns patients and appointments,
which are mirrored back for visibility.
Dates and times are clinic-local; ISO text ("2026-11-24 09:00") is the safest format.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from typing import Any
from zoneinfo import ZoneInfo

WEEKDAYS = ["monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"]
SERIAL_EPOCH = datetime(1899, 12, 30)  # spreadsheet day 0


@dataclass(frozen=True)
class Col:
    name: str
    kind: str  # int | str | bool | float | datetime | time | weekday | ids
    help: str
    required: bool = True


TABS: dict[str, list[Col]] = {
    "doctors": [
        Col("id", "int", "Unique number for this doctor. Never reuse or change it once patients have bookings."),
        Col("full_name", "str", "Name shown to patients, e.g. Dr. Amelia Hart."),
        Col("specialty", "str", "e.g. General Practice, Pediatrics, Dermatology."),
        Col("languages", "str", "Language codes, comma-separated, e.g. en, de.", required=False),
        Col("bio", "str", "One or two sentences about the doctor.", required=False),
        Col("photo_url", "str", "Link to a photo (optional).", required=False),
        Col("active", "bool", "TRUE to offer appointments, FALSE to hide the doctor. Deleting the row also hides them.", required=False),
    ],
    "services": [
        Col("id", "int", "Unique number for this service."),
        Col("name", "str", "e.g. General consultation."),
        Col("duration_minutes", "int", "Length of the appointment in minutes."),
        Col("price", "float", "Price in the clinic's currency. Leave empty for 'price on request'.", required=False),
        Col("description", "str", "Short description for patients.", required=False),
        Col("doctor_ids", "ids", "Doctor ids who offer this service, comma-separated, e.g. 1, 2.", required=False),
        Col("color", "str", "Calendar colour as a hex code, e.g. #0f9d94.", required=False),
        Col("active", "bool", "TRUE to offer, FALSE to hide.", required=False),
    ],
    "availability": [
        Col("doctor_id", "int", "Doctor id from the doctors tab."),
        Col("weekday", "weekday", "Monday ... Sunday."),
        Col("start_time", "time", "Start of the working block, e.g. 09:00."),
        Col("end_time", "time", "End of the working block, e.g. 13:00."),
    ],
    "exceptions": [
        Col("doctor_id", "int", "Doctor id, or leave empty for the whole clinic.", required=False),
        Col("kind", "str", "off (holiday, sick day, blocked time) or extra (one-off extra hours)."),
        Col("start", "datetime", "Start, clinic local time: YYYY-MM-DD HH:MM (a whole day: 00:00)."),
        Col("end", "datetime", "End, clinic local time: YYYY-MM-DD HH:MM (a whole day: 00:00 the next day)."),
        Col("note", "str", "e.g. Sick day, Conference.", required=False),
    ],
    "patients": [
        Col("id", "int", "Set by the app.", required=False),
        Col("full_name", "str", "Patient name."),
        Col("email", "str", "Patient email (used to identify them)."),
        Col("phone", "str", "Patient phone (used to identify them)."),
        Col("created_at", "datetime", "Set by the app.", required=False),
    ],
    "appointments": [
        Col("reference", "str", "Booking reference. Written by the app; edits here are overwritten."),
        Col("status", "str", "booked, cancelled, completed or no_show."),
        Col("start", "datetime", "Clinic local time."),
        Col("end", "datetime", "Clinic local time."),
        Col("doctor_id", "int", "Doctor id."),
        Col("doctor", "str", "Doctor name."),
        Col("service", "str", "Service booked.", required=False),
        Col("patient_id", "int", "Patient id."),
        Col("patient", "str", "Patient name."),
        Col("reason", "str", "Reason given when booking.", required=False),
        Col("created_at", "datetime", "When the booking was made."),
    ],
}
STAFF_TABS = ["doctors", "services", "availability", "exceptions", "patients"]
MIRROR_TABS = ["patients", "appointments"]  # rewritten by the app after every sync

README_ROWS = [
    ["Clinic Assistant data file"],
    [""],
    ["How it works"],
    ["The assistant reads this file every 2 minutes (and when staff press 'Sync'). Bookable times are generated from"],
    ["the availability and exceptions tabs. Bookings are made in the app's own database, which prevents double"],
    ["booking, and are then written back to the patients and appointments tabs."],
    ["Edit doctors, services, availability and exceptions here. Don't rename tabs or column headers."],
    ["Dates/times are clinic local time. Write them as text: 2026-11-24 09:00 (dates) or 09:00 (times)."],
    [""],
    ["Tab", "Column", "What to enter"],
]
for _tab, _cols in TABS.items():
    for _c in _cols:
        README_ROWS.append([_tab, _c.name, _c.help + ("" if _c.required else " (optional)")])


class CellError(ValueError):
    pass


def _blank(v: Any) -> bool:
    return v is None or (isinstance(v, str) and not v.strip())


def parse_cell(value: Any, kind: str, tz: ZoneInfo) -> Any:
    """Turn a spreadsheet cell (Excel typed value, or Google UNFORMATTED_VALUE) into a Python value."""
    if _blank(value):
        return None
    if kind == "str":
        if isinstance(value, float) and value.is_integer():
            value = int(value)
        return str(value).strip()
    if kind == "int":
        if isinstance(value, bool):
            raise CellError("must be a whole number")
        if isinstance(value, (int, float)) and float(value).is_integer():
            return int(value)
        try:
            return int(str(value).strip())
        except ValueError:
            raise CellError("must be a whole number") from None
    if kind == "float":
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            return float(value)
        try:
            return float(str(value).strip().replace(",", "."))
        except ValueError:
            raise CellError("must be a number, e.g. 45 or 45.50") from None
    if kind == "bool":
        if isinstance(value, bool):
            return value
        s = str(value).strip().lower()
        if s in {"true", "yes", "y", "1", "ja", "wahr", "x"}:
            return True
        if s in {"false", "no", "n", "0", "nein", "falsch"}:
            return False
        raise CellError("must be TRUE or FALSE")
    if kind == "ids":
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            return [int(value)]
        try:
            return [int(x) for x in str(value).replace(";", ",").split(",") if x.strip()]
        except ValueError:
            raise CellError("must be numbers separated by commas, e.g. 1, 2") from None
    if kind == "weekday":
        s = str(value).strip().lower()
        for i, d in enumerate(WEEKDAYS):
            if s == d or s == d[:3]:
                return i
        raise CellError("must be a weekday name, e.g. Monday")
    if kind == "time":
        if isinstance(value, datetime):
            return value.strftime("%H:%M")
        if isinstance(value, time):
            return value.strftime("%H:%M")
        if isinstance(value, (int, float)) and not isinstance(value, bool) and 0 <= value < 1:
            minutes = round(value * 24 * 60)
            return f"{minutes // 60:02d}:{minutes % 60:02d}"
        try:
            return time.fromisoformat(str(value).strip().zfill(5)).strftime("%H:%M")
        except ValueError:
            raise CellError("must be a time like 09:00") from None
    if kind == "datetime":
        if isinstance(value, datetime):
            dt = value
        elif isinstance(value, date):
            dt = datetime.combine(value, time.min)
        elif isinstance(value, (int, float)) and not isinstance(value, bool):
            dt = SERIAL_EPOCH + timedelta(days=float(value))
            dt = dt.replace(second=0, microsecond=0) + timedelta(minutes=round(dt.second / 60))
        else:
            s = str(value).strip().replace("T", " ")
            dt = None
            for fmt in ("%Y-%m-%d %H:%M", "%Y-%m-%d %H:%M:%S", "%d.%m.%Y %H:%M", "%Y-%m-%d"):
                try:
                    dt = datetime.strptime(s[:19] if "%S" in fmt else s, fmt)
                    break
                except ValueError:
                    continue
            if dt is None:
                try:
                    dt = datetime.fromisoformat(s)
                except ValueError:
                    raise CellError("must be a date and time like 2026-11-24 09:00") from None
        assert dt is not None
        return dt.replace(tzinfo=tz) if dt.tzinfo is None else dt
    raise ValueError(f"unknown column kind {kind}")


def format_cell(value: Any, kind: str, tz: ZoneInfo) -> Any:
    """Python value -> cell value written to the spreadsheet (plain text/numbers, locale-independent)."""
    if value is None:
        return ""
    if kind == "datetime":
        return value.astimezone(tz).strftime("%Y-%m-%d %H:%M")
    if kind == "bool":
        return bool(value)
    if kind == "ids":
        return ", ".join(str(i) for i in value)
    if kind == "weekday":
        return WEEKDAYS[value].title()
    if kind == "float":
        return float(value)
    return value


def parse_tab(tab: str, raw: list[list[Any]], tz: ZoneInfo) -> tuple[list[dict], list[str]]:
    """Header row + data rows -> (parsed rows, readable warnings). Bad rows are skipped, not fatal."""
    cols = TABS[tab]
    if not raw:
        return [], [f"Tab '{tab}' is empty or missing."]
    header = [str(h).strip().lower() if h is not None else "" for h in raw[0]]
    missing = [c.name for c in cols if c.required and c.name not in header]
    if missing:
        return [], [f"Tab '{tab}' is missing the column(s): {', '.join(missing)}."]
    index = {name: i for i, name in enumerate(header) if name}
    rows, warnings = [], []
    for n, values in enumerate(raw[1:], start=2):
        if all(_blank(v) for v in values):
            continue
        row, problems = {}, []
        for c in cols:
            i = index.get(c.name)
            cell = values[i] if i is not None and i < len(values) else None
            try:
                row[c.name] = parse_cell(cell, c.kind, tz)
            except CellError as e:
                problems.append(f"{c.name} {e}")
                continue
            if row[c.name] is None and c.required:
                problems.append(f"{c.name} is empty")
        if problems:
            warnings.append(f"{tab} row {n} skipped: {'; '.join(problems)}.")
        else:
            rows.append(row)
    return rows, warnings


def build_tab(tab: str, rows: list[dict], tz: ZoneInfo) -> list[list[Any]]:
    cols = TABS[tab]
    return [[c.name for c in cols]] + [[format_cell(r.get(c.name), c.kind, tz) for c in cols] for r in rows]
