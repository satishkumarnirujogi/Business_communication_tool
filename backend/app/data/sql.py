"""SQL stores: SQLite (default, zero setup) and Postgres (works with Supabase)."""

from __future__ import annotations

import secrets
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import sqlalchemy as sa
from sqlalchemy import event
from sqlalchemy.engine import Connection, Engine
from sqlalchemy.exc import IntegrityError

from .base import DataStore, Row, SlotTaken, Tx
from .models import (
    TABLE_ORDER,
    TABLES,
    appointment_reminders,
    appointment_slots,
    appointments,
    audit_log,
    auth_sessions,
    availability,
    channel_inbound,
    channel_threads,
    conversation_messages,
    conversations,
    doctor_services,
    doctors,
    metadata,
    patients,
    schedule_exceptions,
    services,
    slots,
    staff_tasks,
    users,
)


class SchemaOutdated(RuntimeError):
    pass


def _row(r) -> Row | None:
    return None if r is None else {k: (float(v) if isinstance(v, Decimal) else v) for k, v in dict(r).items()}


def _req(r) -> Row:
    """_row for results that are known to exist (RETURNING, .one())."""
    row = _row(r)
    assert row is not None
    return row


def _rows(result) -> list[Row]:
    return [_req(r) for r in result.mappings()]


def _new_reference() -> str:
    return secrets.token_hex(4).upper()


class SQLTx(Tx):
    def __init__(self, conn: Connection):
        self.conn = conn
        self.changed = False

    def _one(self, stmt) -> Row | None:
        return _row(self.conn.execute(stmt).mappings().first())

    def slot_for_update(self, slot_id: int) -> Row | None:
        stmt = (
            sa.select(slots, doctors.c.full_name.label("doctor"), doctors.c.active.label("doctor_active"))
            .join(doctors, doctors.c.id == slots.c.doctor_id)
            .where(slots.c.id == slot_id)
            .with_for_update(of=slots)
        )
        return self._one(stmt)

    def patient_by_email(self, email: str) -> Row | None:
        return self._one(sa.select(patients).where(patients.c.email == email))

    def count_active_appointments(self, patient_id: int, now: datetime) -> int:
        return self.conn.execute(
            sa.select(sa.func.count()).select_from(appointments).where(
                appointments.c.patient_id == patient_id,
                appointments.c.status == "booked",
                appointments.c.slot_start > now,
            )
        ).scalar_one()

    def insert_patient(self, full_name: str, email: str, phone: str) -> Row:
        self.changed = True
        return _req(self.conn.execute(
            sa.insert(patients).values(full_name=full_name, email=email, phone=phone).returning(patients)).mappings().one())

    def cells_for_update(self, doctor_id: int, start: datetime, end: datetime) -> list[Row]:
        stmt = (
            sa.select(slots).where(slots.c.doctor_id == doctor_id, slots.c.start_at >= start, slots.c.start_at < end)
            .order_by(slots.c.start_at).with_for_update()
        )
        return _rows(self.conn.execute(stmt))

    def insert_appointment(self, *, patient_id, doctor_id, start, end, slot_id, reason, service_id=None) -> Row:
        self.changed = True
        values = dict(
            reference=_new_reference(), patient_id=patient_id, doctor_id=doctor_id, slot_id=slot_id,
            slot_start=start, slot_end=end, service_id=service_id, reason=reason, status="booked",
        )
        try:
            with self.conn.begin_nested():
                return _req(self.conn.execute(sa.insert(appointments).values(**values).returning(appointments)).mappings().one())
        except IntegrityError as e:
            taken = self.conn.execute(
                sa.select(appointments.c.id).where(
                    appointments.c.doctor_id == doctor_id,
                    appointments.c.slot_start == start,
                    appointments.c.status == "booked",
                )
            ).first()
            if taken:
                raise SlotTaken() from e
            raise

    def occupy_cells(self, appointment_id: int, slot_ids: list[int]) -> None:
        self.changed = True
        try:
            with self.conn.begin_nested():
                self.conn.execute(sa.insert(appointment_slots),
                                  [{"appointment_id": appointment_id, "slot_id": sid} for sid in slot_ids])
        except IntegrityError as e:
            raise SlotTaken() from e
        self.conn.execute(sa.update(slots).where(slots.c.id.in_(slot_ids)).values(is_booked=True))

    def release_cells(self, appointment_id: int) -> list[int]:
        self.changed = True
        ids = [r.slot_id for r in self.conn.execute(
            sa.select(appointment_slots.c.slot_id).where(appointment_slots.c.appointment_id == appointment_id))]
        self.conn.execute(sa.delete(appointment_slots).where(appointment_slots.c.appointment_id == appointment_id))
        if ids:
            self.conn.execute(sa.update(slots).where(slots.c.id.in_(ids)).values(is_booked=False))
        return ids

    def set_slot_booked(self, slot_id: int, booked: bool) -> None:
        self.changed = True
        self.conn.execute(sa.update(slots).where(slots.c.id == slot_id).values(is_booked=booked))

    def appointment_for_update(self, reference: str) -> Row | None:
        stmt = (
            sa.select(
                appointments, patients.c.email, patients.c.phone, patients.c.full_name.label("patient"),
                doctors.c.full_name.label("doctor"),
            )
            .join(patients, patients.c.id == appointments.c.patient_id)
            .join(doctors, doctors.c.id == appointments.c.doctor_id)
            .where(appointments.c.reference == reference)
            .with_for_update(of=appointments)
        )
        return self._one(stmt)

    def cancel_appointment(self, appointment_id: int, now: datetime) -> None:
        self.changed = True
        self.conn.execute(
            sa.update(appointments).where(appointments.c.id == appointment_id).values(status="cancelled", cancelled_at=now)
        )

    def move_appointment(self, appointment_id, *, doctor_id, start, end, slot_id, service_id) -> None:
        self.changed = True
        try:
            with self.conn.begin_nested():
                self.conn.execute(sa.update(appointments).where(appointments.c.id == appointment_id).values(
                    doctor_id=doctor_id, slot_start=start, slot_end=end, slot_id=slot_id, service_id=service_id))
        except IntegrityError as e:
            raise SlotTaken() from e

    def audit(self, row: Row) -> None:
        self.conn.execute(sa.insert(audit_log).values(**row))


class SQLStore(DataStore):
    kind = "sql"

    def __init__(self, engine: Engine):
        super().__init__()
        self.engine = engine

    # ---- lifecycle ---------------------------------------------------------
    def init_schema(self, reset: bool = False) -> None:
        if reset:
            self._drop_all()
        else:
            self._check_existing_schema()
        metadata.create_all(self.engine)

    def _drop_all(self) -> None:
        # Clinic data only; the knowledge base survives a demo reset.
        metadata.drop_all(self.engine, tables=[TABLES[n] for n in TABLE_ORDER])

    def _check_existing_schema(self) -> None:
        insp = sa.inspect(self.engine)
        for name, table in metadata.tables.items():
            if not insp.has_table(name):
                continue
            have = {c["name"] for c in insp.get_columns(name)}
            missing = {c.name for c in table.columns} - have
            if missing:
                raise SchemaOutdated(
                    f"The database table '{name}' is from an older version (missing: {', '.join(sorted(missing))}). "
                    "Back up anything you need, then run `python -m scripts.seed_demo --reset` from backend/ "
                    "to recreate the tables."
                )

    def health(self) -> None:
        with self.engine.connect() as conn:
            conn.execute(sa.text("select 1"))

    def close(self) -> None:
        self.engine.dispose()

    # ---- reads -------------------------------------------------------------
    def _read(self, stmt) -> list[Row]:
        with self.engine.connect() as conn:
            return _rows(conn.execute(stmt))

    def list_doctors(self, active_only: bool = True) -> list[Row]:
        stmt = sa.select(doctors).order_by(doctors.c.id)
        return self._read(stmt.where(doctors.c.active) if active_only else stmt)

    def list_services(self, active_only: bool = True) -> list[Row]:
        stmt = sa.select(services).order_by(services.c.id)
        rows = self._read(stmt.where(services.c.active) if active_only else stmt)
        links = self._read(sa.select(doctor_services))
        for r in rows:
            r["doctor_ids"] = [link["doctor_id"] for link in links if link["service_id"] == r["id"]]
        return rows

    def list_availability(self, doctor_id: int | None = None) -> list[Row]:
        stmt = sa.select(availability).order_by(availability.c.doctor_id, availability.c.weekday, availability.c.start_time)
        return self._read(stmt.where(availability.c.doctor_id == doctor_id) if doctor_id is not None else stmt)

    def open_slots(self, start, end, *, doctor_id=None, specialty=None, limit=200) -> list[Row]:
        stmt = (
            sa.select(slots.c.id, slots.c.start_at, slots.c.end_at, doctors.c.id.label("doctor_id"),
                      doctors.c.full_name.label("doctor"), doctors.c.specialty)
            .join(doctors, doctors.c.id == slots.c.doctor_id)
            .where(~slots.c.is_booked, doctors.c.active, slots.c.start_at >= start, slots.c.start_at < end)
            .order_by(slots.c.start_at, doctors.c.id)
            .limit(limit)
        )
        if doctor_id is not None:
            stmt = stmt.where(doctors.c.id == doctor_id)
        if specialty:
            stmt = stmt.where(sa.func.lower(doctors.c.specialty).like(f"%{specialty.strip().lower()}%"))
        return self._read(stmt)

    def patient_by_email(self, email: str) -> Row | None:
        rows = self._read(sa.select(patients).where(patients.c.email == email))
        return rows[0] if rows else None

    def patient_appointments(self, patient_id: int, since: datetime, reference: str | None = None) -> list[Row]:
        stmt = (
            sa.select(appointments.c.reference, appointments.c.status, appointments.c.reason,
                      appointments.c.slot_start.label("start_at"), doctors.c.full_name.label("doctor"), doctors.c.specialty)
            .join(doctors, doctors.c.id == appointments.c.doctor_id)
            .where(appointments.c.patient_id == patient_id, appointments.c.slot_start > since)
            .order_by(appointments.c.slot_start)
        )
        if reference:
            stmt = stmt.where(appointments.c.reference == reference)
        return self._read(stmt)

    def list_audit(self, limit: int = 50) -> list[Row]:
        return self._read(sa.select(audit_log).order_by(audit_log.c.id.desc()).limit(limit))

    # ---- calendar ----------------------------------------------------------
    def list_exceptions(self, start=None, end=None, doctor_id=None) -> list[Row]:
        stmt = sa.select(schedule_exceptions).order_by(schedule_exceptions.c.start_at)
        if start is not None:
            stmt = stmt.where(schedule_exceptions.c.end_at > start)
        if end is not None:
            stmt = stmt.where(schedule_exceptions.c.start_at < end)
        if doctor_id is not None:
            stmt = stmt.where(sa.or_(schedule_exceptions.c.doctor_id == doctor_id, schedule_exceptions.c.doctor_id.is_(None)))
        return self._read(stmt)

    def _appointment_select(self):
        return (
            sa.select(
                appointments, doctors.c.full_name.label("doctor"), patients.c.full_name.label("patient"),
                patients.c.email, patients.c.phone, services.c.name.label("service"), services.c.color.label("color"),
            )
            .join(doctors, doctors.c.id == appointments.c.doctor_id)
            .join(patients, patients.c.id == appointments.c.patient_id)
            .outerjoin(services, services.c.id == appointments.c.service_id)
        )

    def appointments_between(self, start, end, doctor_id=None, include_cancelled=False) -> list[Row]:
        stmt = self._appointment_select().where(appointments.c.slot_start < end, appointments.c.slot_end > start)
        if doctor_id is not None:
            stmt = stmt.where(appointments.c.doctor_id == doctor_id)
        if not include_cancelled:
            stmt = stmt.where(appointments.c.status != "cancelled")
        return self._read(stmt.order_by(appointments.c.slot_start))

    def appointment_detail(self, reference: str) -> Row | None:
        rows = self._read(self._appointment_select().where(appointments.c.reference == reference))
        return rows[0] if rows else None

    def _write(self, fn):
        with self.engine.connect() as conn, self._begin_write(conn):
            result = fn(conn)
        self._changed("schedule")
        return result

    def add_availability(self, doctor_id, weekday, start_time, end_time) -> Row:
        return self._write(lambda c: _row(c.execute(sa.insert(availability).values(
            doctor_id=doctor_id, weekday=weekday, start_time=start_time, end_time=end_time).returning(availability)
        ).mappings().one()))

    def update_availability(self, availability_id, **values) -> Row | None:
        allowed = {k: v for k, v in values.items() if k in ("doctor_id", "weekday", "start_time", "end_time")}
        return self._write(lambda c: _row(c.execute(
            sa.update(availability).where(availability.c.id == availability_id).values(**allowed).returning(availability)
        ).mappings().first()))

    def delete_availability(self, availability_id) -> bool:
        return self._write(lambda c: c.execute(sa.delete(availability).where(availability.c.id == availability_id)).rowcount > 0)

    def add_exception(self, *, doctor_id, kind, start_at, end_at, note="") -> Row:
        return self._write(lambda c: _row(c.execute(sa.insert(schedule_exceptions).values(
            doctor_id=doctor_id, kind=kind, start_at=start_at, end_at=end_at, note=note).returning(schedule_exceptions)
        ).mappings().one()))

    def delete_exception(self, exception_id) -> bool:
        return self._write(lambda c: c.execute(
            sa.delete(schedule_exceptions).where(schedule_exceptions.c.id == exception_id)).rowcount > 0)

    # ---- staff tasks ------------------------------------------------------------
    def add_task(self, *, kind, summary, priority="normal", session_id=None, patient_name="", contact="") -> Row:
        row = self._write(lambda c: _row(c.execute(sa.insert(staff_tasks).values(
            kind=kind, summary=summary[:2000], priority=priority, session_id=session_id,
            patient_name=patient_name[:200], contact=contact[:320]).returning(staff_tasks)).mappings().one()))
        self._changed("tasks")
        return row

    def list_tasks(self, status=None, limit=100) -> list[Row]:
        stmt = sa.select(staff_tasks).order_by(
            sa.case((staff_tasks.c.status == "done", 1), else_=0),
            sa.case((staff_tasks.c.priority == "urgent", 0), else_=1),
            staff_tasks.c.created_at.desc(),
        ).limit(limit)
        return self._read(stmt.where(staff_tasks.c.status == status) if status else stmt)

    def update_task(self, task_id, **values) -> Row | None:
        allowed = {k: v for k, v in values.items() if k in ("status", "note", "priority")}
        allowed["updated_at"] = datetime.now(UTC)
        return self._write(lambda c: _row(c.execute(
            sa.update(staff_tasks).where(staff_tasks.c.id == task_id).values(**allowed).returning(staff_tasks)
        ).mappings().first()))

    # ---- conversations -------------------------------------------------------------
    def touch_conversation(self, conversation_id, *, channel="web", language=None, escalated=None, patient_name=None) -> Row:
        now = datetime.now(UTC)

        def fn(c):
            row = c.execute(sa.select(conversations).where(conversations.c.id == conversation_id)).mappings().first()
            values: dict = {"last_message_at": now}
            if language:
                values["language"] = language
            if escalated:
                values["escalated"] = True
            if patient_name:
                values["patient_name"] = patient_name[:200]
            if row is None:
                c.execute(sa.insert(conversations).values(id=conversation_id, channel=channel, started_at=now, **values))
            else:
                c.execute(sa.update(conversations).where(conversations.c.id == conversation_id).values(**values))
            return _row(c.execute(sa.select(conversations).where(conversations.c.id == conversation_id)).mappings().one())

        return self._write_quiet(fn)

    def add_message(self, conversation_id, role, content, *, author="", meta=None) -> Row:
        def fn(c):
            row = _req(c.execute(sa.insert(conversation_messages).values(
                conversation_id=conversation_id, role=role, content=content, author=author[:200], meta=meta,
            ).returning(conversation_messages)).mappings().one())
            c.execute(sa.update(conversations).where(conversations.c.id == conversation_id).values(
                message_count=conversations.c.message_count + 1, last_message_at=row["created_at"]))
            return row

        return self._write_quiet(fn)

    def get_conversation(self, conversation_id) -> Row | None:
        rows = self._read(sa.select(conversations).where(conversations.c.id == conversation_id))
        return rows[0] if rows else None

    def conversation_messages(self, conversation_id, after_id=0, limit=500) -> list[Row]:
        return self._read(sa.select(conversation_messages).where(
            conversation_messages.c.conversation_id == conversation_id, conversation_messages.c.id > after_id,
        ).order_by(conversation_messages.c.id).limit(limit))

    def list_conversations(self, *, q="", status=None, limit=50, offset=0) -> list[Row]:
        last = (sa.select(conversation_messages.c.content).where(conversation_messages.c.conversation_id == conversations.c.id)
                .order_by(conversation_messages.c.id.desc()).limit(1).scalar_subquery())
        open_tasks = (sa.select(sa.func.count()).select_from(staff_tasks).where(
            staff_tasks.c.session_id == conversations.c.id, staff_tasks.c.status != "done").scalar_subquery())
        stmt = sa.select(conversations, last.label("last_message"), open_tasks.label("open_tasks"))
        if status:
            stmt = stmt.where(conversations.c.status == status)
        if q.strip():
            like = f"%{q.strip().lower()}%"
            hit = sa.select(conversation_messages.c.id).where(
                conversation_messages.c.conversation_id == conversations.c.id,
                sa.func.lower(conversation_messages.c.content).like(like)).exists()
            stmt = stmt.where(sa.or_(hit, sa.func.lower(conversations.c.patient_name).like(like),
                                     sa.func.lower(conversations.c.id).like(like)))
        return self._read(stmt.order_by(conversations.c.last_message_at.desc()).limit(limit).offset(offset))

    def set_conversation_status(self, conversation_id, status, taken_over_by="") -> Row | None:
        return self._write_quiet(lambda c: _row(c.execute(
            sa.update(conversations).where(conversations.c.id == conversation_id)
            .values(status=status, taken_over_by=taken_over_by[:200]).returning(conversations)).mappings().first()))

    def conversation_stats(self, start, end) -> dict:
        with self.engine.connect() as c:
            in_range = sa.and_(conversations.c.started_at >= start, conversations.c.started_at < end)
            total = c.execute(sa.select(sa.func.count()).select_from(conversations).where(in_range)).scalar_one()
            needs_person = sa.or_(
                conversations.c.escalated, conversations.c.taken_over_by != "",
                sa.select(staff_tasks.c.id).where(staff_tasks.c.session_id == conversations.c.id).exists(),
            )
            answered = sa.select(conversation_messages.c.id).where(
                conversation_messages.c.conversation_id == conversations.c.id,
                conversation_messages.c.role == "assistant").exists()
            auto = c.execute(sa.select(sa.func.count()).select_from(conversations).where(
                in_range, answered, sa.not_(needs_person))).scalar_one()
            human = c.execute(sa.select(sa.func.count()).select_from(conversations).where(
                conversations.c.status == "human")).scalar_one()
        return {"conversations": total, "resolved_automatically": auto, "live_takeovers": human}

    def _write_quiet(self, fn):
        """Write without change notifications (chat transcripts don't need a sheet sync)."""
        with self.engine.connect() as conn, self._begin_write(conn):
            return fn(conn)

    # ---- channels and reminders ---------------------------------------------------------
    def channel_thread(self, channel, address, *, display_name="", subject="", external_id="", language=None) -> Row:
        now = datetime.now(UTC)

        def fn(c):
            row = c.execute(sa.select(channel_threads).where(
                channel_threads.c.channel == channel, channel_threads.c.address == address)).mappings().first()
            values: dict = {"last_inbound_at": now}
            if display_name:
                values["display_name"] = display_name[:200]
            if subject:
                values["subject"] = subject[:300]
            if external_id:
                values["last_external_id"] = external_id[:300]
            if row is None:
                cid = uuid.uuid4().hex
                c.execute(sa.insert(conversations).values(
                    id=cid, channel=channel, started_at=now, last_message_at=now,
                    patient_name=(display_name or "")[:200], **({"language": language} if language else {})))
                c.execute(sa.insert(channel_threads).values(
                    channel=channel, address=address, conversation_id=cid, created_at=now, **values))
            else:
                c.execute(sa.update(channel_threads).where(channel_threads.c.id == row["id"]).values(**values))
            return _row(c.execute(sa.select(channel_threads).where(
                channel_threads.c.channel == channel, channel_threads.c.address == address)).mappings().one())

        try:
            return self._write_quiet(fn)
        except IntegrityError:  # two first messages from the same sender at once: the other one won
            return self._write_quiet(fn)

    def thread_for_conversation(self, conversation_id) -> Row | None:
        rows = self._read(sa.select(channel_threads).where(channel_threads.c.conversation_id == conversation_id)
                          .order_by(channel_threads.c.last_inbound_at.desc()).limit(1))
        return rows[0] if rows else None

    def mark_inbound(self, channel, external_id) -> bool:
        now = datetime.now(UTC)
        try:
            self._write_quiet(lambda c: c.execute(sa.insert(channel_inbound).values(
                channel=channel, external_id=external_id[:300], received_at=now)))
        except IntegrityError:
            return False
        if secrets.randbelow(200) == 0:  # occasional cleanup; providers retry for hours, not weeks
            self._write_quiet(lambda c: c.execute(sa.delete(channel_inbound).where(
                channel_inbound.c.received_at < now - timedelta(days=14))))
        return True

    def set_reminder_source(self, reference, *, channel, conversation_id, language) -> None:
        now = datetime.now(UTC)

        def fn(c):
            appt = c.execute(sa.select(appointments.c.id).where(appointments.c.reference == reference)).scalar()
            if appt is None:
                return
            values = {"channel": channel, "conversation_id": conversation_id, "language": language, "updated_at": now}
            exists = c.execute(sa.select(appointment_reminders.c.appointment_id).where(
                appointment_reminders.c.appointment_id == appt)).first()
            if exists:
                c.execute(sa.update(appointment_reminders).where(
                    appointment_reminders.c.appointment_id == appt).values(**values))
            else:
                c.execute(sa.insert(appointment_reminders).values(appointment_id=appt, **values))

        self._write_quiet(fn)

    def reminder_candidates(self, start, end) -> list[Row]:
        r, th = appointment_reminders, channel_threads
        stmt = (
            self._appointment_select()
            .add_columns(
                r.c.channel.label("r_channel"), r.c.conversation_id.label("r_conversation_id"),
                r.c.language.label("r_language"), r.c.status.label("r_status"), r.c.sent_for.label("r_sent_for"),
                r.c.sent_via.label("r_sent_via"), r.c.attempts.label("r_attempts"), r.c.error.label("r_error"),
                r.c.updated_at.label("r_updated_at"),
                th.c.address.label("thread_address"), th.c.last_inbound_at.label("thread_last_inbound_at"),
            )
            .outerjoin(r, r.c.appointment_id == appointments.c.id)
            .outerjoin(th, sa.and_(th.c.conversation_id == r.c.conversation_id, th.c.channel == r.c.channel))
            .where(appointments.c.status == "booked", appointments.c.slot_start >= start, appointments.c.slot_start < end)
            .order_by(appointments.c.slot_start)
        )
        return self._read(stmt)

    def save_reminder(self, appointment_id, *, status, sent_for, sent_via="", error="", attempt=True, at=None) -> None:
        now = at or datetime.now(UTC)

        def fn(c):
            values: dict = {"status": status, "sent_for": sent_for, "sent_via": sent_via, "error": error[:2000],
                            "updated_at": now}
            exists = c.execute(sa.select(appointment_reminders.c.attempts, appointment_reminders.c.sent_for).where(
                appointment_reminders.c.appointment_id == appointment_id)).mappings().first()
            if exists is None:
                c.execute(sa.insert(appointment_reminders).values(
                    appointment_id=appointment_id, attempts=1 if attempt else 0, **values))
            else:
                if exists["sent_for"] != sent_for:  # rescheduled: start counting again
                    values["attempts"] = 1 if attempt else 0
                elif attempt:
                    values["attempts"] = appointment_reminders.c.attempts + 1
                c.execute(sa.update(appointment_reminders).where(
                    appointment_reminders.c.appointment_id == appointment_id).values(**values))

        self._write_quiet(fn)

    # ---- users & sessions ---------------------------------------------------------------
    def count_users(self) -> int:
        with self.engine.connect() as c:
            return c.execute(sa.select(sa.func.count()).select_from(users)).scalar_one()

    def get_user(self, *, user_id=None, email=None) -> Row | None:
        stmt = sa.select(users)
        stmt = stmt.where(users.c.id == user_id) if user_id is not None else stmt.where(users.c.email == (email or "").lower())
        rows = self._read(stmt)
        return rows[0] if rows else None

    def list_users(self) -> list[Row]:
        return self._read(sa.select(users).order_by(users.c.id))

    def create_user(self, *, email, name, role, password_hash) -> Row:
        return self._write_quiet(lambda c: _row(c.execute(sa.insert(users).values(
            email=email.lower(), name=name, role=role, password_hash=password_hash).returning(users)).mappings().one()))

    def update_user(self, user_id, **values) -> Row | None:
        allowed = {k: v for k, v in values.items() if k in ("name", "role", "password_hash", "active", "last_login_at")}

        def fn(c):
            if values.get("active") is False or "password_hash" in values:  # sign out everywhere
                c.execute(sa.delete(auth_sessions).where(auth_sessions.c.user_id == user_id))
            return _row(c.execute(sa.update(users).where(users.c.id == user_id).values(**allowed)
                                  .returning(users)).mappings().first())

        return self._write_quiet(fn)

    def create_session(self, token_hash, user_id, expires_at) -> None:
        def fn(c):
            c.execute(sa.delete(auth_sessions).where(auth_sessions.c.expires_at < datetime.now(UTC)))
            c.execute(sa.insert(auth_sessions).values(token_hash=token_hash, user_id=user_id, expires_at=expires_at))

        self._write_quiet(fn)

    def session_user(self, token_hash, now) -> Row | None:
        rows = self._read(sa.select(users).join(auth_sessions, auth_sessions.c.user_id == users.c.id).where(
            auth_sessions.c.token_hash == token_hash, auth_sessions.c.expires_at > now, users.c.active))
        return rows[0] if rows else None

    def delete_session(self, token_hash) -> None:
        self._write_quiet(lambda c: c.execute(sa.delete(auth_sessions).where(auth_sessions.c.token_hash == token_hash)))

    # ---- directory editing --------------------------------------------------------------
    def save_doctor(self, values: dict) -> Row:
        cols = {k: values[k] for k in ("full_name", "specialty", "languages", "bio", "photo_url", "active") if k in values}

        def fn(c):
            if values.get("id"):
                row = c.execute(sa.update(doctors).where(doctors.c.id == values["id"]).values(**cols)
                                .returning(doctors)).mappings().first()
                if row is None:
                    raise KeyError("doctor")
                return _row(row)
            return _row(c.execute(sa.insert(doctors).values(**cols).returning(doctors)).mappings().one())

        return self._write(fn)

    def save_service(self, values: dict, doctor_ids: list[int]) -> Row:
        cols = {k: values[k] for k in ("name", "duration_minutes", "price", "description", "color", "active") if k in values}

        def fn(c):
            if values.get("id"):
                row = c.execute(sa.update(services).where(services.c.id == values["id"]).values(**cols)
                                .returning(services)).mappings().first()
                if row is None:
                    raise KeyError("service")
            else:
                row = c.execute(sa.insert(services).values(**cols).returning(services)).mappings().one()
            row = _req(row)
            c.execute(sa.delete(doctor_services).where(doctor_services.c.service_id == row["id"]))
            if doctor_ids:
                c.execute(sa.insert(doctor_services), [{"doctor_id": d, "service_id": row["id"]} for d in sorted(set(doctor_ids))])
            row["doctor_ids"] = sorted(set(doctor_ids))
            return row

        return self._write(fn)

    # ---- audit search ---------------------------------------------------------------------
    def search_audit(self, *, limit=100, offset=0, action=None, decision=None, actor=None, session_id=None, q=None,
                     since=None, until=None) -> list[Row]:
        stmt = sa.select(audit_log)
        if action:
            stmt = stmt.where(audit_log.c.action.like(f"{action}%"))
        if decision:
            stmt = stmt.where(audit_log.c.decision == decision)
        if actor:
            stmt = stmt.where(audit_log.c.actor == actor)
        if session_id:
            stmt = stmt.where(audit_log.c.session_id == session_id)
        if since:
            stmt = stmt.where(audit_log.c.created_at >= since)
        if until:
            stmt = stmt.where(audit_log.c.created_at < until)
        if q:
            like = f"%{q.lower()}%"
            stmt = stmt.where(sa.or_(sa.func.lower(audit_log.c.reason).like(like), sa.func.lower(audit_log.c.rule).like(like),
                                     sa.func.lower(sa.cast(audit_log.c.params, sa.Text)).like(like),
                                     sa.func.lower(sa.cast(audit_log.c.result, sa.Text)).like(like)))
        return self._read(stmt.order_by(audit_log.c.id.desc()).limit(limit).offset(offset))

    def sync_slots(self, desired, start, end) -> dict:
        doctor_ids = list(desired)
        added = removed = 0
        conflicts: list[Row] = []
        want = {d: {(a.astimezone(UTC), b.astimezone(UTC)) for a, b in cells} for d, cells in desired.items()}
        with self.engine.connect() as conn, self._begin_write(conn):
            existing = conn.execute(sa.select(slots.c.id, slots.c.doctor_id, slots.c.start_at, slots.c.end_at).where(
                slots.c.doctor_id.in_(doctor_ids), slots.c.start_at >= start, slots.c.start_at < end)).all() if doctor_ids else []
            occupied = {r.slot_id: r.appointment_id for r in conn.execute(sa.select(appointment_slots))}
            to_delete = []
            for r in existing:
                key = (r.start_at, r.end_at)
                if key in want[r.doctor_id]:
                    want[r.doctor_id].discard(key)
                elif r.id in occupied:
                    conflicts.append({"slot_id": r.id, "appointment_id": occupied[r.id], "doctor_id": r.doctor_id,
                                      "start_at": r.start_at})
                else:
                    to_delete.append(r.id)
            for i in range(0, len(to_delete), 500):
                part = to_delete[i:i + 500]
                conn.execute(sa.update(appointments).where(appointments.c.slot_id.in_(part)).values(slot_id=None))
                conn.execute(sa.delete(slots).where(slots.c.id.in_(part)))
            removed = len(to_delete)
            rows = [{"doctor_id": d, "start_at": a, "end_at": b, "is_booked": False} for d, cells in want.items() for a, b in cells]
            if rows:
                conn.execute(sa.insert(slots), rows)
            added = len(rows)
            if conflicts:  # name the affected bookings for staff
                ids = {c["appointment_id"] for c in conflicts}
                refs = {r.id: r.reference for r in conn.execute(
                    sa.select(appointments.c.id, appointments.c.reference).where(appointments.c.id.in_(ids)))}
                for c in conflicts:
                    c["reference"] = refs.get(c["appointment_id"])
        if added or removed:
            self._changed("slots")
        return {"added": added, "removed": removed, "conflicts": conflicts}

    # ---- writes ------------------------------------------------------------
    def _begin_write(self, conn: Connection):
        return conn.begin()

    @contextmanager
    def transaction(self) -> Iterator[Tx]:
        with self.engine.connect() as conn, self._begin_write(conn):
            tx = SQLTx(conn)
            yield tx
        if tx.changed:
            self._changed("appointments")

    def audit(self, row: Row) -> None:
        with self.engine.begin() as conn:
            conn.execute(sa.insert(audit_log).values(**row))

    def replace_all(self, data: dict[str, list[Row]]) -> None:
        names = [n for n in TABLE_ORDER if n in data]
        with self.engine.connect() as conn, self._begin_write(conn):
            for name in reversed(names):
                conn.execute(sa.delete(TABLES[name]))
            for name in names:
                if data[name]:
                    cols = {c.name for c in TABLES[name].columns}
                    conn.execute(sa.insert(TABLES[name]), [{k: v for k, v in r.items() if k in cols} for r in data[name]])
            self._after_bulk_insert(conn, names)
        self._changed("all")

    def _after_bulk_insert(self, conn: Connection, names: list[str]) -> None:
        pass

    def export_all(self, tables: list[str] | None = None) -> dict[str, list[Row]]:
        out = {}
        with self.engine.connect() as conn:
            for name in tables or TABLE_ORDER:
                t = TABLES[name]
                out[name] = _rows(conn.execute(sa.select(t).order_by(*t.primary_key.columns)))
        return out


class SQLiteStore(SQLStore):
    """Default store: one file, no server. Writes take SQLite's write lock up front (BEGIN IMMEDIATE),
    so two bookings can never interleave."""

    kind = "sqlite"

    def __init__(self, path: Path | str):
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        engine = sa.create_engine(f"sqlite:///{path}", connect_args={"check_same_thread": False, "timeout": 15})

        @event.listens_for(engine, "connect")
        def _on_connect(dbapi_conn, _record):
            dbapi_conn.isolation_level = None  # we issue BEGIN ourselves
            cur = dbapi_conn.cursor()
            cur.execute("pragma foreign_keys = on")
            cur.execute("pragma journal_mode = wal")
            cur.execute("pragma busy_timeout = 15000")
            cur.close()

        @event.listens_for(engine, "begin")
        def _on_begin(conn):
            conn.exec_driver_sql("BEGIN IMMEDIATE" if conn.info.get("write") else "BEGIN")

        super().__init__(engine)
        self.path = path

    def _begin_write(self, conn: Connection):
        conn.info["write"] = True
        return _WriteBegin(conn)


class _WriteBegin:
    """conn.begin() that clears the write flag afterwards (connections are pooled)."""

    def __init__(self, conn: Connection):
        self.conn = conn

    def __enter__(self):
        self.trans = self.conn.begin()
        return self.trans.__enter__()

    def __exit__(self, *exc):
        try:
            return self.trans.__exit__(*exc)
        finally:
            self.conn.info.pop("write", None)


class PostgresStore(SQLStore):
    kind = "postgres"

    def __init__(self, url: str):
        if url.startswith("postgres://"):
            url = "postgresql://" + url[len("postgres://"):]
        if url.startswith("postgresql://"):
            url = "postgresql+psycopg://" + url[len("postgresql://"):]
        engine = sa.create_engine(
            url,
            pool_pre_ping=True, pool_size=5, max_overflow=5,
            # prepare_threshold=None: compatible with Supabase's transaction pooler (port 6543).
            connect_args={"prepare_threshold": None, "connect_timeout": 10},
        )
        super().__init__(engine)

    def _drop_all(self) -> None:
        # CASCADE also removes tables from older schema versions with different constraints.
        with self.engine.begin() as conn:
            for name in reversed(TABLE_ORDER):
                conn.execute(sa.text(f'drop table if exists "{name}" cascade'))

    def init_schema(self, reset: bool = False) -> None:
        super().init_schema(reset)
        # Supabase exposes the public schema through its REST API; RLS with no policies locks
        # that door. The app connects directly and is unaffected. Harmless on plain Postgres.
        with self.engine.begin() as conn:
            for name in metadata.tables:
                conn.execute(sa.text(f'alter table "{name}" enable row level security'))

    def _after_bulk_insert(self, conn: Connection, names: list[str]) -> None:
        for name in names:
            if "id" in TABLES[name].c and isinstance(TABLES[name].c.id.type, sa.Integer):
                conn.execute(sa.text(
                    f"select setval(pg_get_serial_sequence('{name}', 'id'), coalesce((select max(id) from \"{name}\"), 0) + 1, false)"
                ))
