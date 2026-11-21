"""Messaging channels besides the web chat: WhatsApp (Twilio or Meta) and email, plus day-before reminders.

Channels are built from the current settings on every call, so a change in Settings → Channels applies
without a restart.
"""

from __future__ import annotations

from ..config import get_settings
from .base import Channel, ChannelError, Inbound, Reminder
from .mail import EmailChannel
from .whatsapp import MetaWhatsApp, TwilioWhatsApp


def whatsapp_channel() -> TwilioWhatsApp | MetaWhatsApp | None:
    s = get_settings()
    if s.whatsapp_provider == "twilio":
        return TwilioWhatsApp(s)
    if s.whatsapp_provider == "meta":
        return MetaWhatsApp(s)
    return None


def get_channel(name: str) -> Channel | None:
    if name == "whatsapp":
        return whatsapp_channel()
    if name == "email":
        return EmailChannel(get_settings())
    return None


__all__ = ["Channel", "ChannelError", "EmailChannel", "Inbound", "MetaWhatsApp", "Reminder", "TwilioWhatsApp",
           "get_channel", "whatsapp_channel"]
