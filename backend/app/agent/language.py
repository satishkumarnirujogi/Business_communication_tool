"""Tiny German/English detector and the assistant's fixed texts in both languages.

No model needed: common function words decide; umlauts/ß count for German. Short or ambiguous
messages ("ok", "09:30", "DEMO0001") keep the conversation's previous language.
"""

from __future__ import annotations

import re

DE = set(["ich", "du", "sie", "er", "es", "wir", "ihr", "mein", "meine", "meinen", "mir", "mich", "dich", "ihnen", "und", "oder", "aber", "nicht", "kein", "keine", "ist", "sind", "bin", "habe", "haben", "hat", "war", "wird", "werden", "kann", "können", "könnte", "möchte", "möchten", "würde", "bitte", "danke", "ja", "nein", "der", "die", "das", "den", "dem", "des", "ein", "eine", "einen", "einem", "einer", "wie", "was", "wann", "wo", "wer", "warum", "welche", "welcher", "termin", "termine", "uhr", "morgen", "heute", "nächste", "nächsten", "woche", "montag", "dienstag", "mittwoch", "donnerstag", "freitag", "samstag", "sonntag", "für", "mit", "von", "zu", "zum", "zur", "bei", "auf", "auch", "noch", "schon", "gibt", "geht", "guten", "tag", "hallo", "absagen", "verschieben", "buchen", "arzt", "ärztin", "praxis"])
EN = set(["i", "you", "he", "she", "it", "we", "they", "my", "me", "your", "our", "and", "or", "but", "not", "no", "is", "are", "am", "have", "has", "had", "was", "were", "will", "would", "can", "could", "should", "please", "thanks", "thank", "yes", "the", "a", "an", "how", "what", "when", "where", "who", "why", "which", "appointment", "appointments", "tomorrow", "today", "next", "week", "monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday", "for", "with", "from", "to", "at", "on", "also", "any", "there", "book", "cancel", "reschedule", "doctor", "hello", "hi"])


def detect(text: str, previous: str | None = None, allowed: list[str] | None = None, default: str = "en") -> str:
    allowed = allowed or ["en", "de"]
    words = re.findall(r"[a-zäöüß]+", (text or "").lower())
    de = sum(w in DE for w in words) + 2 * sum(ch in "äöüß" for ch in (text or "").lower())
    en = sum(w in EN for w in words)
    if de >= 2 and de > en:
        lang = "de"
    elif en >= 2 and en > de:
        lang = "en"
    elif de == 1 and en == 0:
        lang = "de"
    elif en == 1 and de == 0:
        lang = "en"
    else:
        lang = previous or default
    if lang not in allowed:
        lang = previous if previous in allowed else (allowed[0] if allowed else default)
    return lang


NAMES = {"en": "English", "de": "German"}

TEXTS = {
    "greeting": {"en": "How can I help you today?", "de": "Wie kann ich Ihnen helfen?"},
    "error": {
        "en": "Sorry, I'm having trouble right now. Please try again or call {phone}.",
        "de": "Entschuldigung, gerade gibt es ein technisches Problem. Bitte versuchen Sie es erneut oder rufen Sie {phone} an.",
    },
    "too_many_steps": {
        "en": "Sorry, that took too many steps. Could you rephrase, or call the clinic?",
        "de": "Entschuldigung, das hat zu viele Schritte gebraucht. Können Sie es anders formulieren oder die Praxis anrufen?",
    },
    "not_understood": {
        "en": "Sorry, I didn't catch that. Could you rephrase?",
        "de": "Entschuldigung, das habe ich nicht verstanden. Können Sie es anders formulieren?",
    },
}


def t(key: str, lang: str, **kw) -> str:
    texts = TEXTS[key]
    return (texts.get(lang) or texts["en"]).format(**kw)


# ---- confirmations -------------------------------------------------------------
YES = re.compile(
    r"(?<!\w)(yes|yeah|yep|yup|sure|ok|okay|correct|confirm(ed)?|please do|go ahead|book it|do it|that'?s right|"
    r"sounds good|perfect|ja|jawohl|genau|richtig|passt|einverstanden|bestätig\w*|gerne|gern|klar|mach das|"
    r"bitte buchen|in ordnung)(?!\w)",
    re.IGNORECASE,
)
NO = re.compile(
    r"(?<!\w)(no|nope|not|don'?t|do not|wait|stop|change|instead|wrong|rather|nein|nicht|doch nicht|warte|stopp|"
    r"lieber|ändern|falsch|anders|stattdessen)(?!\w)",
    re.IGNORECASE,
)


def is_confirmation(text: str) -> bool:
    """A clear yes without hesitation or change requests ("yes" ok; "yes but at 10" is not)."""
    text = (text or "").strip()
    return bool(YES.search(text)) and not NO.search(text) and not re.search(r"\b(but|aber)\b", text, re.IGNORECASE)
