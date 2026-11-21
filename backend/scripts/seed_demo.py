"""Fill a data store with the demo clinic. Run from backend/:

    python -m scripts.seed_demo                     # the store in DATA_SOURCE (.env)
    python -m scripts.seed_demo --store excel       # or: sqlite | postgres | google_sheets
    python -m scripts.seed_demo --reset             # drop and recreate tables first (needed after schema changes)
    python -m scripts.seed_demo --template          # (re)build templates/clinic_template.xlsx
    python -m scripts.seed_demo --no-knowledge      # skip the demo documents in demo/knowledge/

Seeding REPLACES all doctors, services, slots, patients, appointments and audit rows in that store,
and uploads the demo documents to the knowledge base (other uploaded documents are kept).
"""

from __future__ import annotations

import argparse
import sys
import tempfile
from pathlib import Path

from app.config import ROOT, ConfigError, get_settings
from app.data import SyncedStore, build_store
from app.data.demo import build_demo
from app.data.synced import ExcelStore
from app.demo_seed import load_demo_knowledge as _load_docs
from app.demo_seed import seed_clinic
from app.rules import get_rules

TEMPLATE = ROOT / "templates" / "clinic_template.xlsx"


def where(store) -> str:
    if isinstance(store, SyncedStore):
        return f"{store.source.label} (+ local database {store.path})"
    if hasattr(store, "path"):
        return f"SQLite file {store.path}"
    return f"{store.kind} database"


def make_template() -> None:
    rules = get_rules()
    data = build_demo(rules, days=2, with_bookings=False)  # example slots only; patients/appointments empty
    with tempfile.TemporaryDirectory() as tmp:
        store = ExcelStore(Path(tmp) / "t.db", TEMPLATE, rules.tz)
        store.init_schema(reset=True)
        store.replace_all(data)
        TEMPLATE.unlink(missing_ok=True)
        store.write_source()
        store.close()
    print(f"wrote {TEMPLATE}")


def load_demo_knowledge(store) -> None:
    """Upload demo/knowledge/* into the knowledge base (same-name documents are replaced)."""
    from app.knowledge.embeddings import build_embedder
    from app.knowledge.service import KnowledgeBase, build_index

    settings = get_settings()
    kb = KnowledgeBase(store.engine, build_index(settings, store.engine, store.kind), build_embedder(settings), settings)
    _load_docs(kb, report=lambda line: print(f"  {line}"))


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--store", choices=["sqlite", "postgres", "excel", "google_sheets"])
    p.add_argument("--reset", action="store_true", help="drop and recreate all tables first")
    p.add_argument("--yes", "-y", action="store_true", help="don't ask for confirmation")
    p.add_argument("--template", action="store_true", help="only rebuild templates/clinic_template.xlsx")
    p.add_argument("--no-knowledge", action="store_true", help="skip uploading the demo documents")
    args = p.parse_args()

    try:
        if args.template:
            make_template()
            return 0
        store = build_store(get_settings(), kind=args.store)
        target = where(store)
        if not args.yes and input(f"This replaces ALL clinic data in {target}. Continue? [y/N] ").strip().lower() != "y":
            print("cancelled")
            return 1
        store.init_schema(reset=args.reset)
        counts = seed_clinic(store)
        print(f"seeded {target}: {counts}")
        if not args.no_knowledge:
            load_demo_knowledge(store)
        store.close()
        return 0
    except ConfigError as e:
        print(f"\n{e}\n", file=sys.stderr)
        return 1
    except Exception as e:
        print(f"\nSeeding failed: {type(e).__name__}: {e}\n", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
