"""The channel adapter interface. The agent engine never knows which channel a message came from beyond a
name; every channel turns its provider's format into an `Inbound` and sends plain text back."""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime

from ..data.base import Row


class ChannelError(RuntimeError):
    """Sending failed or the channel is not set up. The message is meant for clinic staff."""


@dataclass
class Inbound:
    channel: str                 # whatsapp | email
    address: str                 # who wrote: +4930123456 or someone@example.com
    text: str
    external_id: str = ""        # provider message id, for de-duplication
    display_name: str = ""
    subject: str = ""            # email only
    unsupported: bool = False    # e.g. a voice note or photo; the patient gets a "text only" reply


@dataclass
class Reminder:
    """What a reminder says, in data form (WhatsApp templates need the pieces separately)."""
    text: str
    subject: str
    language: str
    params: list[str] = field(default_factory=list)  # name, date and time, doctor, reference


class Channel(ABC):
    name: str = ""
    label: str = ""
    max_chars: int = 4000

    @abstractmethod
    def problems(self) -> list[str]:
        """Settings that are missing or wrong, as sentences. Empty means ready."""

    @property
    def configured(self) -> bool:
        return not self.problems()

    @abstractmethod
    def send(self, thread: Row, text: str) -> str:
        """Send a reply in an existing thread. Returns the provider's message id. Raises ChannelError."""

    def send_reminder(self, address: str, reminder: Reminder, *, last_inbound_at: datetime | None) -> str:
        """Send a reminder (possibly outside a conversation window). Default: a plain message."""
        return self.send({"address": address, "subject": reminder.subject, "last_external_id": ""}, reminder.text)

    def require_ready(self) -> None:
        problems = self.problems()
        if problems:
            raise ChannelError(f"{self.label} is not set up: " + " ".join(problems))


def split_text(text: str, limit: int) -> list[str]:
    """Split a long reply at paragraph or line breaks so each part fits the provider's limit."""
    text = text.strip()
    parts: list[str] = []
    while len(text) > limit:
        cut = max(text.rfind("\n\n", 0, limit), text.rfind("\n", 0, limit), text.rfind(". ", 0, limit))
        if cut <= limit // 3:
            cut = text.rfind(" ", 0, limit)  # no sentence break in reach: at least keep words whole
        cut = cut + 1 if cut > 0 else limit
        parts.append(text[:cut].strip())
        text = text[cut:].strip()
    if text:
        parts.append(text)
    return parts
