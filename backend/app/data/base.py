"""The DataStore interface every data source implements.

Reads are plain methods. Writes that must be atomic (booking, cancelling) run inside
`store.transaction()`, which yields a `Tx`. The agent's tools do their rule checks between
Tx calls, so a check and the write it guards commit (or roll back) together.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Callable
from contextlib import AbstractContextManager
from datetime import datetime
from typing import Any

Row = dict[str, Any]


class SlotTaken(Exception):
    """The database refused a second booking for the same doctor and start time."""


class Tx(ABC):
    @abstractmethod
    def slot_for_update(self, slot_id: int) -> Row | None:
        """Slot joined with doctor (`doctor`, `doctor_active`), locked until commit."""

    @abstractmethod
    def patient_by_email(self, email: str) -> Row | None: ...

    @abstractmethod
    def count_active_appointments(self, patient_id: int, now: datetime) -> int: ...

    @abstractmethod
    def insert_patient(self, full_name: str, email: str, phone: str) -> Row: ...

    @abstractmethod
    def cells_for_update(self, doctor_id: int, start: datetime, end: datetime) -> list[Row]:
        """The doctor's grid cells starting in [start, end), ordered, locked until commit."""

    @abstractmethod
    def insert_appointment(self, *, patient_id: int, doctor_id: int, start: datetime, end: datetime,
                           slot_id: int | None, reason: str, service_id: int | None = None) -> Row:
        """Returns the row incl. `reference`. Raises SlotTaken on a double booking."""

    @abstractmethod
    def occupy_cells(self, appointment_id: int, slot_ids: list[int]) -> None:
        """Link cells to the appointment and mark them booked. Raises SlotTaken if any cell is taken."""

    @abstractmethod
    def release_cells(self, appointment_id: int) -> list[int]:
        """Unlink and free the appointment's cells (on cancellation). Returns the freed slot ids."""

    @abstractmethod
    def set_slot_booked(self, slot_id: int, booked: bool) -> None: ...

    @abstractmethod
    def appointment_for_update(self, reference: str) -> Row | None:
        """Appointment joined with patient email/phone and doctor name, locked until commit."""

    @abstractmethod
    def cancel_appointment(self, appointment_id: int, now: datetime) -> None: ...

    @abstractmethod
    def move_appointment(self, appointment_id: int, *, doctor_id: int, start: datetime, end: datetime,
                         slot_id: int, service_id: int | None) -> None:
        """Reschedule in place (same reference). Raises SlotTaken if the new start is taken."""

    @abstractmethod
    def audit(self, row: Row) -> None: ...


class DataStore(ABC):
    kind: str = "base"
    engine: Any  # every store keeps its data in SQL; the knowledge base shares that database

    def __init__(self) -> None:
        self._listeners: list[Callable[[str], None]] = []

    # ---- lifecycle ---------------------------------------------------------
    @abstractmethod
    def init_schema(self, reset: bool = False) -> None:
        """Create missing tables. reset=True drops everything first (demo seeding only)."""

    @abstractmethod
    def health(self) -> None:
        """Raise if the store can't be reached."""

    def sync(self) -> dict:
        """Pull edits from an external editing source (Sheets/Excel). No-op for SQL stores."""
        return {"synced": False, "reason": f"{self.kind} has no external source"}

    def close(self) -> None:  # noqa: B027 - optional hook, most stores have nothing to release
        """Release connections when the app shuts down."""

    # ---- change notifications (used for write-back to Sheets/Excel) ---------
    def on_change(self, fn: Callable[[str], None]) -> None:
        self._listeners.append(fn)

    def _changed(self, what: str) -> None:
        for fn in self._listeners:
            try:
                fn(what)
            except Exception:  # a listener must never break a booking
                pass

    # ---- reads -------------------------------------------------------------
    @abstractmethod
    def list_doctors(self, active_only: bool = True) -> list[Row]: ...

    @abstractmethod
    def list_services(self, active_only: bool = True) -> list[Row]:
        """Each row has `doctor_ids`."""

    @abstractmethod
    def list_availability(self, doctor_id: int | None = None) -> list[Row]: ...

    @abstractmethod
    def open_slots(
        self, start: datetime, end: datetime, *, doctor_id: int | None = None,
        specialty: str | None = None, limit: int = 200,
    ) -> list[Row]:
        """Unbooked slots of active doctors with start in [start, end). Rows include `doctor`, `specialty`."""

    @abstractmethod
    def patient_by_email(self, email: str) -> Row | None: ...

    @abstractmethod
    def patient_appointments(self, patient_id: int, since: datetime, reference: str | None = None) -> list[Row]: ...

    @abstractmethod
    def list_audit(self, limit: int = 50) -> list[Row]: ...

    # ---- calendar (Phase 4) ------------------------------------------------
    @abstractmethod
    def list_exceptions(self, start: datetime | None = None, end: datetime | None = None,
                        doctor_id: int | None = None) -> list[Row]:
        """Exceptions overlapping [start, end); clinic-wide ones (doctor_id NULL) are always included."""

    @abstractmethod
    def appointments_between(self, start: datetime, end: datetime, doctor_id: int | None = None,
                             include_cancelled: bool = False) -> list[Row]:
        """For the calendar: rows with doctor, patient, service name/colour."""

    @abstractmethod
    def appointment_detail(self, reference: str) -> Row | None: ...

    @abstractmethod
    def add_availability(self, doctor_id: int, weekday: int, start_time: str, end_time: str) -> Row: ...

    @abstractmethod
    def update_availability(self, availability_id: int, **values) -> Row | None: ...

    @abstractmethod
    def delete_availability(self, availability_id: int) -> bool: ...

    @abstractmethod
    def add_exception(self, *, doctor_id: int | None, kind: str, start_at: datetime, end_at: datetime, note: str = "") -> Row: ...

    @abstractmethod
    def delete_exception(self, exception_id: int) -> bool: ...

    # ---- staff tasks (Phase 5) -------------------------------------------------
    @abstractmethod
    def add_task(self, *, kind: str, summary: str, priority: str = "normal", session_id: str | None = None,
                 patient_name: str = "", contact: str = "") -> Row: ...

    @abstractmethod
    def list_tasks(self, status: str | None = None, limit: int = 100) -> list[Row]: ...

    @abstractmethod
    def update_task(self, task_id: int, **values) -> Row | None: ...

    # ---- conversations (Phase 6) -----------------------------------------------
    @abstractmethod
    def touch_conversation(self, conversation_id: str, *, channel: str = "web", language: str | None = None,
                           escalated: bool | None = None, patient_name: str | None = None) -> Row: ...

    @abstractmethod
    def add_message(self, conversation_id: str, role: str, content: str, *, author: str = "",
                    meta: dict | None = None) -> Row: ...

    @abstractmethod
    def get_conversation(self, conversation_id: str) -> Row | None: ...

    @abstractmethod
    def conversation_messages(self, conversation_id: str, after_id: int = 0, limit: int = 500) -> list[Row]: ...

    @abstractmethod
    def list_conversations(self, *, q: str = "", status: str | None = None, limit: int = 50, offset: int = 0) -> list[Row]: ...

    @abstractmethod
    def set_conversation_status(self, conversation_id: str, status: str, taken_over_by: str = "") -> Row | None: ...

    @abstractmethod
    def conversation_stats(self, start: datetime, end: datetime) -> dict: ...

    # ---- users and sessions -----------------------------------------------------
    # ---- channels (WhatsApp, email) and reminders ----------------------------------
    @abstractmethod
    def channel_thread(self, channel: str, address: str, *, display_name: str = "", subject: str = "",
                       external_id: str = "", language: str | None = None) -> Row:
        """Get or create the thread (and its conversation) for this sender; records the inbound message time."""

    @abstractmethod
    def thread_for_conversation(self, conversation_id: str) -> Row | None: ...

    @abstractmethod
    def mark_inbound(self, channel: str, external_id: str) -> bool:
        """Remember a provider message id. False if it was seen before (a retry)."""

    @abstractmethod
    def set_reminder_source(self, reference: str, *, channel: str, conversation_id: str | None, language: str) -> None:
        """Remember where the patient booked or rescheduled, so the reminder goes back there."""

    @abstractmethod
    def reminder_candidates(self, start: datetime, end: datetime) -> list[Row]:
        """Booked appointments starting in [start, end) with their reminder row (r_* columns, may be None)
        and the booking conversation's thread address (thread_address)."""

    @abstractmethod
    def save_reminder(self, appointment_id: int, *, status: str, sent_for: datetime | None, sent_via: str = "",
                      error: str = "", attempt: bool = True, at: datetime | None = None) -> None: ...

    @abstractmethod
    def count_users(self) -> int: ...

    @abstractmethod
    def get_user(self, *, user_id: int | None = None, email: str | None = None) -> Row | None: ...

    @abstractmethod
    def list_users(self) -> list[Row]: ...

    @abstractmethod
    def create_user(self, *, email: str, name: str, role: str, password_hash: str) -> Row: ...

    @abstractmethod
    def update_user(self, user_id: int, **values) -> Row | None: ...

    @abstractmethod
    def create_session(self, token_hash: str, user_id: int, expires_at: datetime) -> None: ...

    @abstractmethod
    def session_user(self, token_hash: str, now: datetime) -> Row | None: ...

    @abstractmethod
    def delete_session(self, token_hash: str) -> None: ...

    # ---- directory editing --------------------------------------------------------
    @abstractmethod
    def save_doctor(self, values: dict) -> Row: ...

    @abstractmethod
    def save_service(self, values: dict, doctor_ids: list[int]) -> Row: ...

    # ---- audit search ---------------------------------------------------------------
    @abstractmethod
    def search_audit(self, *, limit: int = 100, offset: int = 0, action: str | None = None,
                     decision: str | None = None, actor: str | None = None, session_id: str | None = None,
                     q: str | None = None, since: datetime | None = None, until: datetime | None = None) -> list[Row]: ...

    @abstractmethod
    def sync_slots(self, desired: dict[int, set[tuple[datetime, datetime]]], start: datetime, end: datetime) -> dict:
        """Make the grid in [start, end) for the doctors in `desired` match it. Booked cells are never removed;
        they are reported as conflicts instead."""

    # ---- writes ------------------------------------------------------------
    @abstractmethod
    def transaction(self) -> AbstractContextManager[Tx]: ...

    @abstractmethod
    def audit(self, row: Row) -> None:
        """Write one audit row in its own transaction."""

    @abstractmethod
    def replace_all(self, data: dict[str, list[Row]]) -> None:
        """Replace the given tables' contents (seeding and imports)."""

    @abstractmethod
    def export_all(self, tables: list[str] | None = None) -> dict[str, list[Row]]: ...
