"""Excel and Google Sheets stores: staff edits sync into SQLite, bookings are written back."""

from __future__ import annotations

from datetime import datetime, timedelta

import openpyxl
import pytest

from app.agent.tools import run_tool
from app.data import ExcelStore, GoogleSheetsStore, set_store
from app.data.demo import build_demo
from app.data.tabular import SERIAL_EPOCH, parse_cell
from app.rules import get_rules

from .conftest import bookable_slot

TZ = get_rules().tz


# --------------------------------------------------------------------------
# Fake Google Sheets (same calls GoogleSheetsSource makes on a gspread Spreadsheet)
# --------------------------------------------------------------------------
class FakeWorksheet:
    def __init__(self, title):
        self.title, self.values = title, []

    def clear(self):
        self.values = []

    def update(self, values, range_name="A1", value_input_option=None):
        assert range_name == "A1"
        self.values = [list(r) for r in values]


class FakeBook:
    def __init__(self):
        self.sheets: dict[str, FakeWorksheet] = {}

    def worksheets(self):
        return list(self.sheets.values())

    def add_worksheet(self, title, rows, cols):
        self.sheets[title] = FakeWorksheet(title)
        return self.sheets[title]

    def values_batch_get(self, ranges, params=None):
        # Like UNFORMATTED_VALUE + SERIAL_NUMBER: return what was stored (RAW strings/numbers/bools).
        return {"valueRanges": [{"values": self.sheets[r.strip("'")].values} for r in ranges]}


# --------------------------------------------------------------------------
@pytest.fixture(params=["excel", "google_sheets"])
def sheet_store(request, tmp_path):
    if request.param == "excel":
        store = ExcelStore(tmp_path / "clinic.db", tmp_path / "clinic.xlsx", TZ)
    else:
        store = GoogleSheetsStore(tmp_path / "clinic.db", "sheet-id", None, TZ, book=FakeBook())
    store.init_schema(reset=True)
    store.replace_all(build_demo(get_rules()))
    store.write_source()
    set_store(store)
    yield store
    set_store(None)


def read(store, tab):
    return store.source.read([tab])[tab]


def write(store, tab, grid):
    store.source.write({tab: grid})


def _day(offset_days: int, weekday: int | None = None):
    d = datetime.now(TZ).date() + timedelta(days=offset_days)
    if weekday is not None:
        d += timedelta(days=(weekday - d.weekday()) % 7)
    return d


def _cells(store, doctor_id, day):
    start = datetime.combine(day, datetime.min.time(), TZ)
    return [s for s in store.open_slots(start, start + timedelta(days=1), doctor_id=doctor_id, limit=1000)]


def test_staff_edits_are_imported_and_slots_regenerated(sheet_store):
    doctors = read(sheet_store, "doctors")
    doctors[1][doctors[0].index("full_name")] = "Dr. Amelia Hart-Novak"   # rename doctor 1
    write(sheet_store, "doctors", doctors[:-1])                            # remove doctor 6 -> hidden

    monday = _day(7, weekday=0)
    saturday = _day(7, weekday=5)
    assert _cells(sheet_store, 1, monday) and not _cells(sheet_store, 2, saturday)
    exc = read(sheet_store, "exceptions")
    exc.append([1, "off", f"{monday} 00:00", f"{monday + timedelta(days=1)} 00:00", "Conference"])
    exc.append([1, "holiday", f"{monday} 00:00", f"{monday} 12:00", ""])          # bad kind -> warning
    write(sheet_store, "exceptions", exc)
    av = read(sheet_store, "availability")
    av.append([2, "Saturday", "09:00", "12:00"])                                   # new weekly block
    write(sheet_store, "availability", av)

    summary = sheet_store.sync()
    assert summary["synced"], summary
    assert any("kind must be" in w for w in summary["warnings"])
    names = {d["id"]: d["full_name"] for d in sheet_store.list_doctors()}
    assert names[1] == "Dr. Amelia Hart-Novak" and 6 not in names
    assert _cells(sheet_store, 1, monday) == []                 # day off
    sat = _cells(sheet_store, 2, saturday)
    assert sat and min(s["start_at"].astimezone(TZ).strftime("%H:%M") for s in sat) == "09:00"
    assert not sheet_store.open_slots(datetime.now(TZ), datetime.now(TZ) + timedelta(days=30), doctor_id=6)


def test_booking_is_written_back(sheet_store):
    slot = bookable_slot(sheet_store)
    payload, err = run_tool("book_appointment", {"slot_id": slot["id"], "full_name": "Sheet Patient",
                                                 "email": "sheet@example.com", "phone": "+1 555 0177", "reason": "x"}, "s")
    assert not err, payload
    sheet_store.sync()  # the background worker does this a few seconds after a booking
    appts = read(sheet_store, "appointments")
    row = next(r for r in appts[1:] if r[0] == payload["reference"])
    assert row[appts[0].index("status")] == "booked"
    assert "sheet@example.com" in [r[2] for r in read(sheet_store, "patients")[1:]]


def test_time_off_over_a_booking_keeps_it_and_warns(sheet_store):
    slot = bookable_slot(sheet_store)
    payload, _ = run_tool("book_appointment", {"slot_id": slot["id"], "full_name": "Pat Test", "email": "p@example.com",
                                               "phone": "+1 555 0100", "reason": "x"}, "s")
    assert payload["ok"]
    day = slot["start_at"].astimezone(TZ).date()
    exc = read(sheet_store, "exceptions")
    exc.append([slot["doctor_id"], "off", f"{day} 00:00", f"{day + timedelta(days=1)} 00:00", "Sick"])
    write(sheet_store, "exceptions", exc)
    summary = sheet_store.sync()
    assert any(payload["reference"] in w for w in summary["warnings"])
    assert sheet_store.appointment_detail(payload["reference"])["status"] == "booked"


def test_calendar_edits_are_written_to_the_sheet(sheet_store):
    sheet_store.add_availability(5, 4, "14:00", "16:00")  # Dr. Fischer, Friday afternoon
    rows = read(sheet_store, "availability")[1:]
    assert [5, "Friday", "14:00", "16:00"] in [list(r[:4]) for r in rows]


def test_unusable_tab_leaves_data_alone(sheet_store):
    write(sheet_store, "doctors", [["wrong", "headers"]])
    summary = sheet_store.sync()
    assert any("missing the column" in w for w in summary["warnings"])
    assert len(sheet_store.list_doctors()) == 6


def test_excel_file_missing_is_readable(tmp_path):
    store = ExcelStore(tmp_path / "c.db", tmp_path / "nope.xlsx", TZ)
    store.init_schema()
    summary = store.sync()
    assert not summary["synced"] and "not found" in summary["error"]


def test_template_has_readme_and_all_tabs():
    from app.config import ROOT

    wb = openpyxl.load_workbook(ROOT / "templates" / "clinic_template.xlsx")
    assert wb.sheetnames == ["README", "doctors", "services", "availability", "exceptions", "patients", "appointments"]


@pytest.mark.parametrize("value,kind,expected", [
    (45, "float", 45.0), ("45,50", "float", 45.5), ("TRUE", "bool", True), (False, "bool", False),
    ("1, 2", "ids", [1, 2]), (3.0, "ids", [3]), ("Mon", "weekday", 0), (0.375, "time", "09:00"), ("9:30", "time", "09:30"),
])
def test_parse_cell(value, kind, expected):
    assert parse_cell(value, kind, TZ) == expected


def test_parse_datetime_formats():
    want = datetime(2026, 11, 24, 9, 0, tzinfo=TZ)
    serial = (datetime(2026, 11, 24, 9, 0) - SERIAL_EPOCH).total_seconds() / 86400
    for v in ["2026-11-24 09:00", "24.11.2026 09:00", datetime(2026, 11, 24, 9, 0), serial]:
        assert parse_cell(v, "datetime", TZ) == want
