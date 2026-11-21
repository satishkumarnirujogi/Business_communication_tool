"""Table definitions shared by every SQL backend (SQLite and Postgres).

Double booking is impossible at the database level: `uq_appointments_doctor_slot_booked`
is a partial unique index on (doctor_id, slot_start) for booked appointments.
"""

from __future__ import annotations

from datetime import UTC, datetime

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.types import TypeDecorator

metadata = sa.MetaData()


class UTCDateTime(TypeDecorator):
    """Timezone-aware UTC datetimes on every backend (SQLite has no timezone type)."""

    impl = sa.DateTime(timezone=True)
    cache_ok = True

    def process_bind_param(self, value, dialect):
        if value is None:
            return None
        if value.tzinfo is None:
            raise ValueError("naive datetime passed to the database; use timezone-aware datetimes")
        value = value.astimezone(UTC)
        return value.replace(tzinfo=None) if dialect.name == "sqlite" else value

    def process_result_value(self, value, dialect):
        if value is None:
            return None
        if isinstance(value, str):
            value = datetime.fromisoformat(value)
        return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


JSONType = sa.JSON().with_variant(JSONB(), "postgresql")


def _now() -> datetime:
    return datetime.now(UTC)


doctors = sa.Table(
    "doctors", metadata,
    sa.Column("id", sa.Integer, primary_key=True),
    sa.Column("full_name", sa.String(200), nullable=False),
    sa.Column("specialty", sa.String(100), nullable=False),
    sa.Column("languages", sa.String(100), nullable=False, server_default="en"),  # comma-separated codes
    sa.Column("bio", sa.Text, nullable=False, server_default=""),
    sa.Column("photo_url", sa.String(500), nullable=False, server_default=""),
    sa.Column("active", sa.Boolean, nullable=False, server_default=sa.true()),
)

services = sa.Table(
    "services", metadata,
    sa.Column("id", sa.Integer, primary_key=True),
    sa.Column("name", sa.String(200), nullable=False),
    sa.Column("duration_minutes", sa.Integer, nullable=False),
    sa.Column("price", sa.Numeric(10, 2)),  # null = price on request
    sa.Column("description", sa.Text, nullable=False, server_default=""),
    sa.Column("color", sa.String(7), nullable=False, server_default=""),  # calendar colour, e.g. #0f9d94
    sa.Column("active", sa.Boolean, nullable=False, server_default=sa.true()),
    sa.CheckConstraint("duration_minutes > 0", name="ck_services_duration"),
)

doctor_services = sa.Table(
    "doctor_services", metadata,
    sa.Column("doctor_id", sa.Integer, sa.ForeignKey("doctors.id", ondelete="CASCADE"), primary_key=True),
    sa.Column("service_id", sa.Integer, sa.ForeignKey("services.id", ondelete="CASCADE"), primary_key=True),
)

# Weekly availability template; slots are generated from it (minus exceptions) by app.scheduling.
availability = sa.Table(
    "availability", metadata,
    sa.Column("id", sa.Integer, primary_key=True),
    sa.Column("doctor_id", sa.Integer, sa.ForeignKey("doctors.id", ondelete="CASCADE"), nullable=False),
    sa.Column("weekday", sa.Integer, nullable=False),  # 0 = Monday
    sa.Column("start_time", sa.String(5), nullable=False),  # "09:00", clinic local time
    sa.Column("end_time", sa.String(5), nullable=False),
    sa.CheckConstraint("weekday between 0 and 6", name="ck_availability_weekday"),
)

# Holidays, sick days and one-off extra hours. doctor_id NULL = the whole clinic.
schedule_exceptions = sa.Table(
    "schedule_exceptions", metadata,
    sa.Column("id", sa.Integer, primary_key=True),
    sa.Column("doctor_id", sa.Integer, sa.ForeignKey("doctors.id", ondelete="CASCADE")),
    sa.Column("kind", sa.String(10), nullable=False),  # off | extra
    sa.Column("start_at", UTCDateTime, nullable=False),
    sa.Column("end_at", UTCDateTime, nullable=False),
    sa.Column("note", sa.String(200), nullable=False, server_default=""),
    sa.CheckConstraint("kind in ('off','extra')", name="ck_exceptions_kind"),
    sa.CheckConstraint("end_at > start_at", name="ck_exceptions_order"),
)

# Generated grid cells (rules.yaml booking.slot_minutes long). An appointment occupies one or more
# consecutive cells, recorded in appointment_slots.
slots = sa.Table(
    "slots", metadata,
    sa.Column("id", sa.Integer, primary_key=True),
    sa.Column("doctor_id", sa.Integer, sa.ForeignKey("doctors.id", ondelete="CASCADE"), nullable=False),
    sa.Column("start_at", UTCDateTime, nullable=False),
    sa.Column("end_at", UTCDateTime, nullable=False),
    sa.Column("is_booked", sa.Boolean, nullable=False, server_default=sa.false()),
    sa.UniqueConstraint("doctor_id", "start_at", name="uq_slots_doctor_start"),
    sa.CheckConstraint("end_at > start_at", name="ck_slots_order"),
)
sa.Index("ix_slots_open", slots.c.start_at, slots.c.is_booked)

patients = sa.Table(
    "patients", metadata,
    sa.Column("id", sa.Integer, primary_key=True),
    sa.Column("full_name", sa.String(200), nullable=False),
    sa.Column("email", sa.String(320), nullable=False, unique=True),
    sa.Column("phone", sa.String(50), nullable=False),
    sa.Column("date_of_birth", sa.Date),
    sa.Column("created_at", UTCDateTime, nullable=False, default=_now),
)

appointments = sa.Table(
    "appointments", metadata,
    sa.Column("id", sa.Integer, primary_key=True),
    sa.Column("reference", sa.String(16), nullable=False, unique=True),
    sa.Column("patient_id", sa.Integer, sa.ForeignKey("patients.id"), nullable=False),
    sa.Column("doctor_id", sa.Integer, sa.ForeignKey("doctors.id"), nullable=False),
    sa.Column("slot_id", sa.Integer, sa.ForeignKey("slots.id", ondelete="SET NULL")),
    sa.Column("slot_start", UTCDateTime, nullable=False),
    sa.Column("slot_end", UTCDateTime, nullable=False),
    sa.Column("service_id", sa.Integer, sa.ForeignKey("services.id", ondelete="SET NULL")),
    sa.Column("reason", sa.String(200), nullable=False, server_default=""),
    sa.Column("status", sa.String(20), nullable=False, server_default="booked"),
    sa.Column("created_at", UTCDateTime, nullable=False, default=_now),
    sa.Column("cancelled_at", UTCDateTime),
    sa.CheckConstraint("status in ('booked','cancelled','completed','no_show')", name="ck_appointments_status"),
)
sa.Index(
    "uq_appointments_doctor_slot_booked",
    appointments.c.doctor_id, appointments.c.slot_start,
    unique=True,
    sqlite_where=appointments.c.status == "booked",
    postgresql_where=appointments.c.status == "booked",
)
sa.Index("ix_appointments_patient", appointments.c.patient_id)

# Which grid cells a booked appointment occupies. slot_id is UNIQUE, so two bookings can never share a
# cell, even when they start at different times (overlap-proof double-booking protection).
# Rows are deleted when the appointment is cancelled.
appointment_slots = sa.Table(
    "appointment_slots", metadata,
    sa.Column("appointment_id", sa.Integer, sa.ForeignKey("appointments.id", ondelete="CASCADE"), nullable=False),
    sa.Column("slot_id", sa.Integer, sa.ForeignKey("slots.id"), nullable=False, unique=True),
    sa.PrimaryKeyConstraint("appointment_id", "slot_id"),
)

audit_log = sa.Table(
    "audit_log", metadata,
    sa.Column("id", sa.Integer, primary_key=True),
    sa.Column("created_at", UTCDateTime, nullable=False, default=_now),
    sa.Column("session_id", sa.String(64)),
    sa.Column("actor", sa.String(20), nullable=False, server_default="agent"),
    sa.Column("action", sa.String(64), nullable=False),
    sa.Column("params", JSONType),
    sa.Column("decision", sa.String(20), nullable=False),
    sa.Column("rule", sa.String(100)),
    sa.Column("reason", sa.Text),
    sa.Column("result", JSONType),
    sa.CheckConstraint("decision in ('allowed','denied','error','escalated')", name="ck_audit_decision"),
)
sa.Index("ix_audit_created", audit_log.c.created_at)

# Work items for staff: handoffs from the assistant, emergencies, callbacks (Phase 6 shows them as a queue).
staff_tasks = sa.Table(
    "staff_tasks", metadata,
    sa.Column("id", sa.Integer, primary_key=True),
    sa.Column("created_at", UTCDateTime, nullable=False, default=_now),
    sa.Column("updated_at", UTCDateTime, nullable=False, default=_now),
    sa.Column("session_id", sa.String(64)),
    sa.Column("kind", sa.String(20), nullable=False),        # urgent | medical_question | callback | complaint | question | other
    sa.Column("priority", sa.String(10), nullable=False, server_default="normal"),  # normal | urgent
    sa.Column("summary", sa.Text, nullable=False),
    sa.Column("patient_name", sa.String(200), nullable=False, server_default=""),
    sa.Column("contact", sa.String(320), nullable=False, server_default=""),  # how to reach the patient
    sa.Column("status", sa.String(20), nullable=False, server_default="open"),
    sa.Column("note", sa.Text, nullable=False, server_default=""),
    sa.CheckConstraint("status in ('open','in_progress','done')", name="ck_staff_tasks_status"),
    sa.CheckConstraint("priority in ('normal','urgent')", name="ck_staff_tasks_priority"),
)
sa.Index("ix_staff_tasks_status", staff_tasks.c.status, staff_tasks.c.created_at)

# Chat conversations (any channel) and their transcript. Staff can take a conversation over.
conversations = sa.Table(
    "conversations", metadata,
    sa.Column("id", sa.String(64), primary_key=True),  # = chat session id
    sa.Column("channel", sa.String(20), nullable=False, server_default="web"),
    sa.Column("language", sa.String(5), nullable=False, server_default="en"),
    sa.Column("status", sa.String(10), nullable=False, server_default="bot"),  # bot | human | closed
    sa.Column("patient_name", sa.String(200), nullable=False, server_default=""),
    sa.Column("taken_over_by", sa.String(200), nullable=False, server_default=""),
    sa.Column("escalated", sa.Boolean, nullable=False, server_default=sa.false()),
    sa.Column("message_count", sa.Integer, nullable=False, server_default="0"),
    sa.Column("started_at", UTCDateTime, nullable=False, default=_now),
    sa.Column("last_message_at", UTCDateTime, nullable=False, default=_now),
    sa.CheckConstraint("status in ('bot','human','closed')", name="ck_conversations_status"),
)
sa.Index("ix_conversations_last", conversations.c.last_message_at)

conversation_messages = sa.Table(
    "conversation_messages", metadata,
    sa.Column("id", sa.Integer, primary_key=True),
    sa.Column("conversation_id", sa.String(64), sa.ForeignKey("conversations.id", ondelete="CASCADE"), nullable=False),
    sa.Column("created_at", UTCDateTime, nullable=False, default=_now),
    sa.Column("role", sa.String(10), nullable=False),  # patient | assistant | staff | system
    sa.Column("author", sa.String(200), nullable=False, server_default=""),
    sa.Column("content", sa.Text, nullable=False),
    sa.Column("meta", JSONType),
    sa.CheckConstraint("role in ('patient','assistant','staff','system')", name="ck_messages_role"),
)
sa.Index("ix_messages_conversation", conversation_messages.c.conversation_id, conversation_messages.c.id)

# Phase 7 channels. One thread per (channel, address): the same WhatsApp number or email address always
# continues the same conversation.
channel_threads = sa.Table(
    "channel_threads", metadata,
    sa.Column("id", sa.Integer, primary_key=True),
    sa.Column("channel", sa.String(20), nullable=False),        # whatsapp | email
    sa.Column("address", sa.String(320), nullable=False),       # E.164 phone number or lower-case email
    sa.Column("conversation_id", sa.String(64), sa.ForeignKey("conversations.id", ondelete="CASCADE"), nullable=False),
    sa.Column("display_name", sa.String(200), nullable=False, server_default=""),
    sa.Column("subject", sa.String(300), nullable=False, server_default=""),   # email: thread subject
    sa.Column("last_external_id", sa.String(300), nullable=False, server_default=""),  # email: Message-ID to reply to
    sa.Column("created_at", UTCDateTime, nullable=False, default=_now),
    sa.Column("last_inbound_at", UTCDateTime, nullable=False, default=_now),
    sa.UniqueConstraint("channel", "address", name="uq_channel_threads_address"),
)
sa.Index("ix_channel_threads_conversation", channel_threads.c.conversation_id)

# Webhook/IMAP message ids already handled, so provider retries never produce a second reply.
channel_inbound = sa.Table(
    "channel_inbound", metadata,
    sa.Column("channel", sa.String(20), nullable=False),
    sa.Column("external_id", sa.String(300), nullable=False),
    sa.Column("received_at", UTCDateTime, nullable=False, default=_now),
    sa.PrimaryKeyConstraint("channel", "external_id"),
)

# Day-before reminders. `channel`/`conversation_id` remember where the patient booked; `sent_for` is the
# appointment start the reminder was sent for, so a rescheduled appointment gets a new reminder.
appointment_reminders = sa.Table(
    "appointment_reminders", metadata,
    sa.Column("appointment_id", sa.Integer, sa.ForeignKey("appointments.id", ondelete="CASCADE"), primary_key=True),
    sa.Column("channel", sa.String(20), nullable=False, server_default="web"),
    sa.Column("conversation_id", sa.String(64)),
    sa.Column("language", sa.String(5), nullable=False, server_default="en"),
    sa.Column("status", sa.String(10), nullable=False, server_default="pending"),
    sa.Column("sent_for", UTCDateTime),
    sa.Column("sent_via", sa.String(20), nullable=False, server_default=""),
    sa.Column("attempts", sa.Integer, nullable=False, server_default="0"),
    sa.Column("error", sa.Text, nullable=False, server_default=""),
    sa.Column("updated_at", UTCDateTime, nullable=False, default=_now),
    sa.CheckConstraint("status in ('pending','sent','failed','skipped')", name="ck_reminders_status"),
)

# Staff accounts (admin dashboard). Not touched by demo seeding.
users = sa.Table(
    "users", metadata,
    sa.Column("id", sa.Integer, primary_key=True),
    sa.Column("email", sa.String(320), nullable=False, unique=True),
    sa.Column("name", sa.String(200), nullable=False),
    sa.Column("role", sa.String(10), nullable=False),  # admin | staff
    sa.Column("password_hash", sa.String(300), nullable=False),
    sa.Column("active", sa.Boolean, nullable=False, server_default=sa.true()),
    sa.Column("created_at", UTCDateTime, nullable=False, default=_now),
    sa.Column("last_login_at", UTCDateTime),
    sa.CheckConstraint("role in ('admin','staff')", name="ck_users_role"),
)

auth_sessions = sa.Table(
    "auth_sessions", metadata,
    sa.Column("token_hash", sa.String(64), primary_key=True),  # sha256 of the bearer token; the token itself is never stored
    sa.Column("user_id", sa.Integer, sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
    sa.Column("created_at", UTCDateTime, nullable=False, default=_now),
    sa.Column("expires_at", UTCDateTime, nullable=False),
)

# ---- clinic knowledge base (documents admins upload; vectors live in Chroma or pgvector) ----
KB_CATEGORIES = ("policy", "rules", "services", "prices", "offers", "doctors", "faq", "other")

kb_documents = sa.Table(
    "kb_documents", metadata,
    sa.Column("id", sa.Integer, primary_key=True),
    sa.Column("name", sa.String(300), nullable=False),          # file name or URL shown as the source
    sa.Column("name_key", sa.String(300), nullable=False, unique=True),  # lowercased name: re-upload replaces
    sa.Column("category", sa.String(20), nullable=False),
    sa.Column("source_type", sa.String(10), nullable=False),    # file | url
    sa.Column("url", sa.String(2000)),
    sa.Column("stored_path", sa.String(500)),                   # original upload, for re-indexing
    sa.Column("sha256", sa.String(64)),
    sa.Column("size_bytes", sa.Integer),
    sa.Column("status", sa.String(20), nullable=False, server_default="processing"),
    sa.Column("error", sa.Text),
    sa.Column("chunk_count", sa.Integer, nullable=False, server_default="0"),
    sa.Column("embedding_model", sa.String(200)),
    sa.Column("created_at", UTCDateTime, nullable=False, default=_now),
    sa.Column("updated_at", UTCDateTime, nullable=False, default=_now),
    sa.CheckConstraint("status in ('processing','ready','failed')", name="ck_kb_documents_status"),
    sa.CheckConstraint("category in ('" + "','".join(KB_CATEGORIES) + "')", name="ck_kb_documents_category"),
)

kb_chunks = sa.Table(
    "kb_chunks", metadata,
    sa.Column("id", sa.Integer, primary_key=True),
    sa.Column("document_id", sa.Integer, sa.ForeignKey("kb_documents.id", ondelete="CASCADE"), nullable=False),
    sa.Column("idx", sa.Integer, nullable=False),
    sa.Column("text", sa.Text, nullable=False),
    sa.Column("page", sa.Integer),  # PDFs: page the chunk starts on
    sa.Column("tokens", sa.Integer, nullable=False),
    sa.UniqueConstraint("document_id", "idx", name="uq_kb_chunks_doc_idx"),
)

# Insert order (parents first); reverse for deletes.
TABLE_ORDER = ["doctors", "services", "doctor_services", "availability", "schedule_exceptions", "slots", "patients",
               "appointments", "appointment_slots", "audit_log", "staff_tasks", "conversations",
               "conversation_messages", "channel_threads", "channel_inbound", "appointment_reminders"]
TABLES = {name: metadata.tables[name] for name in TABLE_ORDER}
KB_TABLES = ["kb_documents", "kb_chunks"]  # not touched by seeding/replace_all
APP_TABLES = ["users", "auth_sessions"]       # not touched by seeding/replace_all
