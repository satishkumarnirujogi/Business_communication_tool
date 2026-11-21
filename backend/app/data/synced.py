"""Excel and Google Sheets stores.

A spreadsheet is an *editing source*, not the booking engine. Every store here is a SQLiteStore
(transactions + unique indexes make double booking impossible) that:
  1. pulls staff edits from the spreadsheet (every SYNC_INTERVAL_SECONDS and on demand), then regenerates slots,
  2. writes patients / appointments back to the spreadsheet after each change, and
  3. writes availability / exceptions back when they are edited in the admin calendar.
Spreadsheet problems never block a booking; they are reported on the next sync.
"""

from __future__ import annotations

import logging
import threading
from datetime import UTC, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import sqlalchemy as sa
from sqlalchemy.dialects.sqlite import insert as sqlite_insert

from .models import appointments, availability, doctor_services, doctors, patients, schedule_exceptions, services
from .sources import ExcelSource, GoogleSheetsSource, SourceError, TabularSource
from .sql import SQLiteStore
from .tabular import MIRROR_TABS, README_ROWS, STAFF_TABS, build_tab, parse_tab

log = logging.getLogger("clinic.sync")


class SyncedStore(SQLiteStore):
    kind = "synced"

    def __init__(self, sqlite_path: Path | str, source: TabularSource, tz: ZoneInfo):
        super().__init__(sqlite_path)
        self.source = source
        self.tz = tz
        self._sync_lock = threading.Lock()
        self.last_sync: dict = {}

    # ---- public --------------------------------------------------------------
    def sync(self) -> dict:
        """Pull staff edits, then write the app-owned tabs back. Never raises."""
        with self._sync_lock:
            started = datetime.now(UTC)
            summary: dict = {"synced": False, "source": self.source.label, "at": started.isoformat(), "warnings": []}
            try:
                raw = self.source.read(STAFF_TABS)
                summary["imported"] = self._pull(raw, summary["warnings"])
                from ..scheduling import regenerate

                slots = regenerate(self)
                summary["slots"] = {"added": slots["added"], "removed": slots["removed"]}
                if slots["conflicts"]:
                    summary["warnings"].append(
                        f"{len(slots['conflicts'])} booked time(s) are now outside working hours "
                        f"(bookings: {', '.join(sorted({c['reference'] or '?' for c in slots['conflicts']}))}). "
                        "They were kept; contact those patients if needed."
                    )
                self._push()
                summary["synced"] = True
            except SourceError as e:
                summary["error"] = str(e)
            except Exception as e:  # keep the worker alive; details go to the log
                log.exception("sync failed")
                summary["error"] = f"Unexpected sync error ({type(e).__name__}). See the server log."
            for w in summary["warnings"]:
                log.warning("sync: %s", w)
            if summary.get("error"):
                log.error("sync: %s", summary["error"])
            self.last_sync = summary
            return summary

    def write_source(self, include_readme: bool = True) -> None:
        """Write every tab from the database to the spreadsheet (seeding / template creation)."""
        data = self._sheet_rows()
        tabs = {t: build_tab(t, data[t], self.tz) for t in STAFF_TABS + ["appointments"]}
        if include_readme:
            tabs = {"README": README_ROWS, **tabs}
        self.source.write(tabs)

    # ---- pull ------------------------------------------------------------------
    def _pull(self, raw: dict, warnings: list[str]) -> dict:
        parsed = {}
        for tab in STAFF_TABS:
            rows, w = parse_tab(tab, raw.get(tab) or [], self.tz)
            warnings.extend(w)
            parsed[tab] = None if (not rows and w) else rows  # None = tab unusable, leave the database alone

        counts: dict[str, int] = {}
        with self.engine.connect() as conn, self._begin_write(conn):
            doctor_ids = self._pull_doctors(conn, parsed["doctors"], warnings, counts)
            self._pull_services(conn, parsed["services"], doctor_ids, warnings, counts)
            self._pull_availability(conn, parsed["availability"], doctor_ids, warnings, counts)
            self._pull_exceptions(conn, parsed["exceptions"], doctor_ids, warnings, counts)
            self._pull_patients(conn, parsed["patients"], warnings, counts)
        return counts

    def _pull_doctors(self, conn, rows, warnings, counts) -> set[int]:
        if rows is None:
            return {r.id for r in conn.execute(sa.select(doctors.c.id))}
        seen: set[int] = set()
        for r in rows:
            if r["id"] in seen:
                warnings.append(f"doctors: id {r['id']} appears twice; the second row was ignored.")
                continue
            seen.add(r["id"])
            values = dict(full_name=r["full_name"], specialty=r["specialty"], languages=r["languages"] or "en",
                          bio=r["bio"] or "", photo_url=r["photo_url"] or "",
                          active=True if r["active"] is None else r["active"])
            conn.execute(sqlite_insert(doctors).values(id=r["id"], **values)
                         .on_conflict_do_update(index_elements=["id"], set_=values))
        # Removed rows: hide the doctor (keeps the history of their appointments intact).
        conn.execute(sa.update(doctors).where(doctors.c.id.not_in(seen)).values(active=False))
        counts["doctors"] = len(seen)
        return seen

    def _pull_services(self, conn, rows, doctor_ids, warnings, counts) -> None:
        if rows is None:
            return
        seen: set[int] = set()
        links = []
        for r in rows:
            if r["id"] in seen:
                warnings.append(f"services: id {r['id']} appears twice; the second row was ignored.")
                continue
            if r["duration_minutes"] <= 0:
                warnings.append(f"services: '{r['name']}' needs a duration above 0 minutes; skipped.")
                continue
            seen.add(r["id"])
            values = dict(name=r["name"], duration_minutes=r["duration_minutes"], price=r["price"],
                          description=r["description"] or "", color=r["color"] or "",
                          active=True if r["active"] is None else r["active"])
            conn.execute(sqlite_insert(services).values(id=r["id"], **values)
                         .on_conflict_do_update(index_elements=["id"], set_=values))
            for d in r["doctor_ids"] or []:
                if d in doctor_ids:
                    links.append({"doctor_id": d, "service_id": r["id"]})
                else:
                    warnings.append(f"services: '{r['name']}' lists doctor id {d}, which isn't on the doctors tab.")
        conn.execute(sa.update(services).where(services.c.id.not_in(seen)).values(active=False))
        conn.execute(sa.delete(doctor_services))
        if links:
            conn.execute(sa.insert(doctor_services), list({(x["doctor_id"], x["service_id"]): x for x in links}.values()))
        counts["services"] = len(seen)

    def _pull_availability(self, conn, rows, doctor_ids, warnings, counts) -> None:
        if rows is None:
            return
        keep = []
        for r in rows:
            if r["doctor_id"] not in doctor_ids:
                warnings.append(f"availability: doctor id {r['doctor_id']} isn't on the doctors tab; row skipped.")
            elif r["start_time"] >= r["end_time"]:
                warnings.append(f"availability: {r['start_time']}-{r['end_time']} ends before it starts; row skipped.")
            else:
                keep.append({k: r[k] for k in ("doctor_id", "weekday", "start_time", "end_time")})
        conn.execute(sa.delete(availability))
        if keep:
            conn.execute(sa.insert(availability), keep)
        counts["availability"] = len(keep)

    def _pull_exceptions(self, conn, rows, doctor_ids, warnings, counts) -> None:
        if rows is None:
            return
        keep = []
        for r in rows:
            kind = (r["kind"] or "").strip().lower()
            if kind not in ("off", "extra"):
                warnings.append(f"exceptions: kind must be 'off' or 'extra' (got '{r['kind']}'); row skipped.")
            elif r["doctor_id"] is not None and r["doctor_id"] not in doctor_ids:
                warnings.append(f"exceptions: doctor id {r['doctor_id']} isn't on the doctors tab; row skipped.")
            elif kind == "extra" and r["doctor_id"] is None:
                warnings.append("exceptions: extra hours need a doctor_id; row skipped.")
            elif r["end"] <= r["start"]:
                warnings.append(f"exceptions: the period starting {r['start']:%Y-%m-%d %H:%M} ends before it starts; skipped.")
            else:
                keep.append(dict(doctor_id=r["doctor_id"], kind=kind, start_at=r["start"], end_at=r["end"],
                                 note=r["note"] or ""))
        conn.execute(sa.delete(schedule_exceptions))
        if keep:
            conn.execute(sa.insert(schedule_exceptions), keep)
        counts["exceptions"] = len(keep)

    def _pull_patients(self, conn, rows, warnings, counts) -> None:
        # The app owns patients; staff may add new ones (rows without id).
        if rows is None:
            return
        known = {r.email for r in conn.execute(sa.select(patients.c.email))}
        added = 0
        for r in rows:
            email = r["email"].strip().lower()
            if r["id"] is None and email not in known:
                conn.execute(sa.insert(patients).values(full_name=r["full_name"], email=email, phone=r["phone"]))
                known.add(email)
                added += 1
        counts["patients_added"] = added

    # ---- push ------------------------------------------------------------------
    def _sheet_rows(self) -> dict[str, list[dict]]:
        with self.engine.connect() as conn:
            doc = {r.id: r for r in conn.execute(sa.select(doctors).order_by(doctors.c.id))}
            pat = {r.id: r for r in conn.execute(sa.select(patients).order_by(patients.c.id))}
            svc = {r.id: r.name for r in conn.execute(sa.select(services.c.id, services.c.name))}
            links: dict[int, list[int]] = {}
            for r in conn.execute(sa.select(doctor_services).order_by(doctor_services.c.doctor_id)):
                links.setdefault(r.service_id, []).append(r.doctor_id)
            return {
                "doctors": [dict(r._mapping) for r in doc.values()],
                "services": [{**r._mapping, "doctor_ids": links.get(r.id, [])}
                             for r in conn.execute(sa.select(services).order_by(services.c.id))],
                "availability": [dict(r._mapping) for r in conn.execute(
                    sa.select(availability).order_by(availability.c.doctor_id, availability.c.weekday, availability.c.start_time))],
                "exceptions": [{"doctor_id": r.doctor_id, "kind": r.kind, "start": r.start_at, "end": r.end_at, "note": r.note}
                               for r in conn.execute(sa.select(schedule_exceptions).order_by(schedule_exceptions.c.start_at))],
                "patients": [dict(r._mapping) for r in pat.values()],
                "appointments": [
                    {"reference": r.reference, "status": r.status, "start": r.slot_start, "end": r.slot_end,
                     "doctor_id": r.doctor_id, "doctor": doc[r.doctor_id].full_name if r.doctor_id in doc else "",
                     "service": svc.get(r.service_id, ""),
                     "patient_id": r.patient_id, "patient": pat[r.patient_id].full_name if r.patient_id in pat else "",
                     "reason": r.reason, "created_at": r.created_at}
                    for r in conn.execute(sa.select(appointments).order_by(appointments.c.slot_start))
                ],
            }

    def _push(self, tabs: list[str] = MIRROR_TABS) -> None:
        data = self._sheet_rows()
        self.source.write({t: build_tab(t, data[t], self.tz) for t in tabs})

    # ---- calendar edits made in the admin UI go back to the sheet ---------------
    def _staff_edit(self, fn, tabs: list[str] | None = None):
        self.sync()  # pick up the latest sheet edits first, so they aren't overwritten
        result = fn()
        with self._sync_lock:
            try:
                self._push(tabs or ["availability", "exceptions"])
            except SourceError as e:
                raise SourceError(f"Saved, but the spreadsheet couldn't be updated ({e}). "
                                  "The change will be undone at the next sync unless you add it to the sheet.") from None
        return result

    def save_doctor(self, *a, **k):
        return self._staff_edit(lambda: super(SyncedStore, self).save_doctor(*a, **k), ["doctors"])

    def save_service(self, *a, **k):
        return self._staff_edit(lambda: super(SyncedStore, self).save_service(*a, **k), ["services"])

    def add_availability(self, *a, **k):
        return self._staff_edit(lambda: super(SyncedStore, self).add_availability(*a, **k))

    def update_availability(self, *a, **k):
        return self._staff_edit(lambda: super(SyncedStore, self).update_availability(*a, **k))

    def delete_availability(self, *a, **k):
        return self._staff_edit(lambda: super(SyncedStore, self).delete_availability(*a, **k))

    def add_exception(self, *a, **k):
        return self._staff_edit(lambda: super(SyncedStore, self).add_exception(*a, **k))

    def delete_exception(self, *a, **k):
        return self._staff_edit(lambda: super(SyncedStore, self).delete_exception(*a, **k))


class ExcelStore(SyncedStore):
    kind = "excel"

    def __init__(self, sqlite_path: Path | str, excel_path: Path | str, tz: ZoneInfo):
        super().__init__(sqlite_path, ExcelSource(excel_path), tz)


class GoogleSheetsStore(SyncedStore):
    kind = "google_sheets"

    def __init__(self, sqlite_path: Path | str, sheet_id: str, credentials_file: Path | str | None, tz: ZoneInfo,
                 book=None):
        super().__init__(sqlite_path, GoogleSheetsSource(sheet_id, credentials_file, book=book), tz)
