"""Demo mode: fill an empty database with the demo clinic and its documents.

Used by DEMO_MODE=true (on by default in docker-compose.yml) and by `python -m scripts.seed_demo`.
Seeding on startup only happens when there are no doctors yet, so it never overwrites a real clinic.
"""

from __future__ import annotations

import logging
from collections.abc import Callable

from .config import ROOT
from .data import SyncedStore
from .data.base import DataStore
from .data.demo import build_demo
from .rules import get_rules

log = logging.getLogger("clinic.demo")

DEMO_DOCS = ROOT / "demo" / "knowledge"
DEMO_CATEGORIES = {
    "clinic-policies.md": "policy", "house-rules.txt": "rules", "price-list.csv": "prices", "offers.md": "offers",
    "doctors.md": "doctors", "faq.md": "faq", "faq-de.md": "faq",
}


def seed_clinic(store: DataStore) -> dict[str, int]:
    """Replace all clinic data with the demo clinic. Returns row counts per table."""
    data = build_demo(get_rules())
    store.replace_all(data)
    if isinstance(store, SyncedStore):
        store.write_source()
    return {k: len(v) for k, v in data.items() if k != "audit_log"}


def seed_if_empty(store: DataStore) -> bool:
    if store.list_doctors(active_only=False):
        return False
    counts = seed_clinic(store)
    log.info("demo mode: empty database filled with the demo clinic %s", counts)
    return True


def load_demo_knowledge(kb, report: Callable[[str], None] = log.info) -> None:
    """Upload demo/knowledge/* (documents with the same name are replaced; others are kept)."""
    for name, category in DEMO_CATEGORIES.items():
        path = DEMO_DOCS / name
        if not path.is_file():
            report(f"knowledge: {name} is missing, skipped")
            continue
        doc, unchanged = kb.add_file(name, path.read_bytes(), category)
        status = "unchanged" if unchanged else kb.process(doc["id"])["status"]
        report(f"knowledge: {name} ({category}) -> {status}")
