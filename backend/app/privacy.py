"""Mask personal data (emails, phone numbers, birth dates) before it reaches logs or the audit trail.

The audit log still shows *that* a patient was identified and what happened, but not their contact details:
  jana.novak@example.com -> j***@e***.com      +49 151 2345678 -> +** *** ****678      born 03.04.1985 -> born **.**.****
Appointment dates and times are left alone; only dates that look like birth dates are masked.
"""

from __future__ import annotations

import logging
import re
from typing import Any

EMAIL = re.compile(r"(?<![\w.+-])([\w.+-])[\w.+-]*@([\w-])[\w-]*((?:\.[\w-]+)*\.[a-z]{2,})", re.IGNORECASE)
PHONE = re.compile(r"(?<![\w])\+?\(?\d[\d\s\-()/.]{5,}\d(?![\w])")
DATE_PREFIX = re.compile(r"^\d{4}-\d{1,2}-\d{1,2}(\D|$)")  # 2026-11-24 / 2026-11-24 09 (the :00 isn't matched)
DATE_DOTTED = re.compile(r"^\d{1,2}[./]\d{1,2}[./]\d{2,4}$")
TIME_RANGE = re.compile(r"^\d{1,2}[:.]\d{2}\s*[-–]\s*\d{1,2}[:.]\d{2}$")
BIRTH = re.compile(
    r"(?i)\b(born|birth\w*|dob|d\.o\.b\.?|geb\.?|geboren|geburts\w*)\b(\W{0,20})"
    r"(\d{1,4}[./-]\d{1,2}[./-]\d{1,4})"
)

SENSITIVE_KEYS = {"email", "phone", "contact", "date_of_birth", "dob", "birth_date", "birthday", "geburtsdatum"}


def mask_email(value: str) -> str:
    return EMAIL.sub(lambda m: f"{m.group(1)}***@{m.group(2)}***{m.group(3)}", value)


def _mask_phone_match(m: re.Match) -> str:
    raw = m.group(0)
    digits = re.sub(r"\D", "", raw)
    stripped = raw.strip()
    # Leave dates, times and short numbers alone; they are needed to understand the log.
    if (len(digits) < 7 or DATE_PREFIX.match(stripped) or DATE_DOTTED.match(stripped)
            or TIME_RANGE.match(stripped)):
        return raw
    keep = 3
    out, seen = [], 0
    total = len(digits)
    for ch in raw:
        if ch.isdigit():
            seen += 1
            out.append(ch if seen > total - keep else "*")
        else:
            out.append(ch)
    return "".join(out)


DATE_OR_TIME = re.compile(r"\d{4}-\d{1,2}-\d{1,2}|\d{1,2}\.\d{1,2}\.\d{2,4}|\d{1,2}/\d{1,2}/\d{4}|\d{1,2}:\d{2}")


def mask_phone(value: str) -> str:
    # Shield dates and times first so "09:00 / 24.11.2026" can't be mistaken for a phone number.
    kept: list[str] = []

    def hold(m: re.Match) -> str:
        kept.append(m.group(0))
        return f"\x00{len(kept) - 1}\x00"

    shielded = PHONE.sub(_mask_phone_match, DATE_OR_TIME.sub(hold, value))
    return re.sub(r"\x00(\d+)\x00", lambda m: kept[int(m.group(1))], shielded)


def mask_birth_dates(value: str) -> str:
    return BIRTH.sub(lambda m: m.group(1) + m.group(2) + re.sub(r"\d", "*", m.group(3)), value)


def mask_text(value: str) -> str:
    return mask_phone(mask_email(mask_birth_dates(value)))


def mask_value(key: str, value: Any) -> Any:
    if not isinstance(value, str):
        return value
    k = key.lower()
    if k in SENSITIVE_KEYS or "birth" in k:
        if "@" in value:
            return mask_email(value)
        if k in ("phone", "contact"):
            masked = mask_phone(value)
            return masked if masked != value else re.sub(r"\d(?=\d{3})", "*", value)
        return re.sub(r"\d", "*", value)  # birth dates and anything else sensitive: hide every digit
    return mask_text(value)


def mask(obj: Any, key: str = "") -> Any:
    """Recursively mask a JSON-like structure (dicts by key name, free text by pattern)."""
    if isinstance(obj, dict):
        return {k: mask(v, str(k)) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [mask(v, key) for v in obj]
    return mask_value(key, obj)


class MaskingFilter(logging.Filter):
    """Logging filter: masks personal data in every log line (attach to handlers)."""

    def filter(self, record: logging.LogRecord) -> bool:
        try:
            msg = record.getMessage()
        except Exception:
            return True
        masked = mask_text(msg)
        if masked != msg:
            record.msg, record.args = masked, None
        return True


def install_log_masking() -> None:
    root = logging.getLogger()
    for handler in root.handlers:
        if not any(isinstance(f, MaskingFilter) for f in handler.filters):
            handler.addFilter(MaskingFilter())
