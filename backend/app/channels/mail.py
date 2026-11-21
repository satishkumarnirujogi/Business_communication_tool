"""Email: the assistant reads the clinic inbox over IMAP and answers over SMTP.

Loop protection: automatic mail (auto-replies, mailing lists, bounces, our own messages) is never answered,
and every reply carries `Auto-Submitted: auto-replied` (RFC 3834) so other robots don't answer us either.
Only the new part of a reply is read: quoted history ("> ...", "On ... wrote:") is cut off.
"""

from __future__ import annotations

import email
import email.policy
import imaplib
import logging
import re
import smtplib
import ssl
import threading
from email.message import EmailMessage
from email.utils import formataddr, make_msgid, parseaddr

from bs4 import BeautifulSoup

from ..config import Settings, get_settings
from ..data.base import Row
from .base import Channel, ChannelError, Inbound, Reminder

log = logging.getLogger("clinic.email")

MAX_BODY = 4000
_ROBOT = re.compile(r"^(mailer-daemon|postmaster|no-?reply|do-?not-?reply|bounces?)([+@.-]|$)", re.I)
_QUOTE_START = [
    re.compile(r"^\s*On .{0,200}wrote:\s*$", re.I | re.S),
    re.compile(r"^\s*Am .{0,200}schrieb .{0,200}:\s*$", re.I | re.S),
    re.compile(r"^\s*-{2,}\s*(Original Message|Ursprüngliche Nachricht|Forwarded message)", re.I),
    re.compile(r"^\s*(From|Von|Sent|Gesendet):\s", re.I),
    re.compile(r"^_{10,}\s*$"),
]


def strip_quoted(text: str) -> str:
    """Keep only what the sender wrote now: drop quoted history and a trailing '-- ' signature."""
    lines = text.replace("\r\n", "\n").split("\n")
    out: list[str] = []
    for i, line in enumerate(lines):
        if line.startswith(">"):
            break
        # "On Tue, 3 Nov 2026 at 10:00, Clinic <x@y> wrote:" is often wrapped over two lines
        joined = line + " " + (lines[i + 1] if i + 1 < len(lines) else "")
        if any(p.match(line) for p in _QUOTE_START) or (_QUOTE_START[0].match(joined) and line.strip().startswith("On ")):
            break
        if line.rstrip() == "--":
            break
        out.append(line)
    return re.sub(r"\n{3,}", "\n\n", "\n".join(out)).strip()


def is_automatic(msg: EmailMessage, own_address: str) -> str | None:
    """Why this message must not be answered, or None."""
    sender = parseaddr(msg.get("From", ""))[1].lower()
    if not sender or "@" not in sender:
        return "no sender address"
    if own_address and sender == own_address.lower():
        return "sent by the clinic address itself"
    if _ROBOT.match(sender.split("@")[0] + "@"):
        return "sent by an automatic address"
    auto = (msg.get("Auto-Submitted") or "no").strip().lower()
    if auto != "no":
        return f"Auto-Submitted: {auto}"
    if (msg.get("Precedence") or "").strip().lower() in ("bulk", "list", "junk", "auto_reply"):
        return "bulk or list mail"
    if msg.get("List-Id") or msg.get("List-Unsubscribe"):
        return "mailing list"
    if msg.get("X-Autoreply") or msg.get("X-Autorespond") or (msg.get("X-Auto-Response-Suppress") or "").lower() == "all":
        return "auto-reply"
    if msg.get_content_type() == "multipart/report":
        return "delivery report"
    return None


def body_text(msg: EmailMessage) -> str:
    part = msg.get_body(preferencelist=("plain", "html"))
    if part is None:
        return ""
    try:
        content = part.get_content()
    except (LookupError, UnicodeDecodeError):
        raw = part.get_payload(decode=True)
        content = raw.decode("utf-8", "replace") if isinstance(raw, bytes) else str(raw or "")
    if part.get_content_type() == "text/html":
        content = BeautifulSoup(content, "html.parser").get_text("\n")
    return content


def parse_message(raw: bytes, own_address: str) -> tuple[Inbound | None, str]:
    """Returns (inbound, why-skipped)."""
    msg = email.message_from_bytes(raw, policy=email.policy.default)
    assert isinstance(msg, EmailMessage)
    why = is_automatic(msg, own_address)
    name, sender = parseaddr(msg.get("From", ""))
    message_id = (msg.get("Message-ID") or "").strip()
    if why:
        return None, why
    text = strip_quoted(body_text(msg))[:MAX_BODY]
    subject = re.sub(r"\s+", " ", str(msg.get("Subject") or "")).strip()
    if not text and subject:
        text = subject
    return Inbound(channel="email", address=sender.lower(), text=text, external_id=message_id or "",
                   display_name=name, subject=subject, unsupported=not text), ""


def reply_subject(subject: str, fallback: str) -> str:
    subject = subject.strip() or fallback
    return subject if re.match(r"^(re|aw|antw)\s*:", subject, re.I) else f"Re: {subject}"


class EmailChannel(Channel):
    name, label = "email", "Email"
    max_chars = 20000

    def __init__(self, s: Settings):
        self.s = s
        self.address = s.email_address.strip()
        self.clinic_name = s.clinic.name
        self._box: imaplib.IMAP4 | None = None

    def problems(self) -> list[str]:
        s, out = self.s, []
        if not s.email_enabled:
            out.append("EMAIL_ENABLED is off.")
        if "@" not in self.address:
            out.append("EMAIL_ADDRESS (the clinic inbox) is missing.")
        if not s.smtp_host:
            out.append("SMTP_HOST is missing.")
        if not s.imap_host:
            out.append("IMAP_HOST is missing.")
        if not (s.imap_user or self.address) or not s.secret("imap_password"):
            out.append("IMAP_USER / IMAP_PASSWORD are missing.")
        return out

    def can_send(self) -> bool:
        """Outbound only (reminders) needs SMTP but not IMAP."""
        return bool(self.s.email_enabled and "@" in self.address and self.s.smtp_host)

    # -- outbound
    def _smtp(self) -> smtplib.SMTP:
        s = self.s
        try:
            if s.smtp_security == "ssl":
                conn: smtplib.SMTP = smtplib.SMTP_SSL(s.smtp_host, s.smtp_port, timeout=20,
                                                      context=ssl.create_default_context())
            else:
                conn = smtplib.SMTP(s.smtp_host, s.smtp_port, timeout=20)
                if s.smtp_security == "starttls":
                    conn.starttls(context=ssl.create_default_context())
            if s.smtp_user or s.secret("smtp_password"):
                conn.login(s.smtp_user or self.address, s.secret("smtp_password") or "")
            return conn
        except smtplib.SMTPAuthenticationError:
            raise ChannelError("Email: the mail server rejected the SMTP user name or password.") from None
        except (OSError, smtplib.SMTPException) as e:
            raise ChannelError(f"Email: could not connect to {s.smtp_host}:{s.smtp_port} ({type(e).__name__}: {e}). "
                               "Check SMTP_HOST, SMTP_PORT and SMTP_SECURITY.") from None

    def _send(self, to: str, subject: str, text: str, in_reply_to: str = "") -> str:
        if not self.can_send():
            raise ChannelError("Email is not set up: turn it on and fill in EMAIL_ADDRESS and SMTP_HOST.")
        msg = EmailMessage()
        msg["From"] = formataddr((self.s.email_display_name or self.clinic_name, self.address))
        msg["To"] = to
        msg["Subject"] = subject
        msg["Auto-Submitted"] = "auto-replied"
        msg["Message-ID"] = make_msgid(domain=self.address.split("@")[-1])
        if in_reply_to:
            msg["In-Reply-To"] = in_reply_to
            msg["References"] = in_reply_to
        msg.set_content(text)
        conn = self._smtp()
        try:
            conn.send_message(msg)
        except smtplib.SMTPRecipientsRefused:
            raise ChannelError("Email: the mail server refused the recipient address.") from None
        except smtplib.SMTPException as e:
            raise ChannelError(f"Email: sending failed ({type(e).__name__}: {e}).") from None
        finally:
            try:
                conn.quit()
            except Exception:
                pass
        return msg["Message-ID"]

    def send(self, thread: Row, text: str) -> str:
        subject = reply_subject(thread.get("subject") or "", f"Your message to {self.clinic_name}")
        return self._send(thread["address"], subject, text, thread.get("last_external_id") or "")

    def send_reminder(self, address, reminder: Reminder, *, last_inbound_at=None) -> str:
        return self._send(address, reminder.subject, reminder.text)

    # -- inbound
    def fetch_unseen(self, limit: int = 20) -> list[tuple[str, bytes]]:
        """(uid, raw message) for unread mail. Messages are marked read only after they're handled."""
        s = self.s
        try:
            box = (imaplib.IMAP4_SSL(s.imap_host, s.imap_port, ssl_context=ssl.create_default_context(), timeout=30)
                   if s.imap_port != 143 else imaplib.IMAP4(s.imap_host, s.imap_port, timeout=30))
            if s.imap_port == 143:
                box.starttls(ssl_context=ssl.create_default_context())
            box.login(s.imap_user or self.address, s.secret("imap_password") or "")
        except imaplib.IMAP4.error:
            raise ChannelError("Email: the mail server rejected the IMAP user name or password.") from None
        except OSError as e:
            raise ChannelError(f"Email: could not connect to {s.imap_host}:{s.imap_port} ({type(e).__name__}).") from None
        self._box = box
        try:
            typ, _ = box.select(s.imap_folder or "INBOX")
            if typ != "OK":
                raise ChannelError(f"Email: the folder '{s.imap_folder}' doesn't exist on the mail server.")
            typ, data = box.uid("search", "UNSEEN")
            uids = [u.decode() for u in (data[0] or b"").split()[:limit]] if typ == "OK" else []
            out = []
            for uid in uids:
                typ, parts = box.uid("fetch", uid, "(BODY.PEEK[])")
                raw = next((p[1] for p in parts or [] if isinstance(p, tuple)), None)
                if typ == "OK" and raw:
                    out.append((uid, raw))
            return out
        except Exception:
            self.close_inbox()
            raise

    def mark_seen(self, uid: str) -> None:
        if self._box is None:
            return
        try:
            self._box.uid("store", uid, "+FLAGS", "(\\Seen)")
        except Exception:
            log.warning("could not mark an email as read")

    def close_inbox(self) -> None:
        box = getattr(self, "_box", None)
        self._box = None
        if box is not None:
            try:
                box.logout()
            except Exception:
                pass


class EmailPoller:
    """Checks the inbox every EMAIL_POLL_SECONDS while the email channel is configured."""

    def __init__(self) -> None:
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, name="email-poller", daemon=True)
        self.last_error = ""

    def start(self) -> None:
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        self._thread.join(timeout=10)

    def poll_once(self) -> int:
        from .router import receive

        ch = EmailChannel(get_settings())
        if not ch.configured:
            return 0
        handled = 0
        try:
            for uid, raw in ch.fetch_unseen():
                try:
                    inbound, why = parse_message(raw, ch.address)
                    if inbound is None:
                        log.info("email skipped: %s", why)
                    else:
                        receive(inbound)
                        handled += 1
                except Exception:
                    log.exception("could not handle an email")
                ch.mark_seen(uid)  # also for failures, so one broken email can't block the inbox
            self.last_error = ""
        except ChannelError as e:
            if str(e) != self.last_error:
                log.error("%s", e)
            self.last_error = str(e)
        finally:
            ch.close_inbox()
        return handled

    def _run(self) -> None:
        while not self._stop.is_set():
            try:
                self.poll_once()
            except Exception:
                log.exception("email polling failed")
            self._stop.wait(get_settings().email_poll_seconds)
