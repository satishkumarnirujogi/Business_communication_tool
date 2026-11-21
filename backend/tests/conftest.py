import os
from datetime import UTC, datetime, timedelta

import pytest

from app.data import PostgresStore, SQLiteStore, set_store
from app.data.demo import build_demo
from app.rules import get_rules


def seeded(store):
    store.init_schema(reset=True)
    store.replace_all(build_demo(get_rules()))
    return store


@pytest.fixture
def sqlite_store(tmp_path):
    store = seeded(SQLiteStore(tmp_path / "clinic.db"))
    set_store(store)
    yield store
    set_store(None)


def _store_params():
    params = ["sqlite"]
    if os.getenv("TEST_DATABASE_URL"):
        params.append("postgres")
    return params


@pytest.fixture(params=_store_params())
def sql_store(request, tmp_path):
    """Every SQL backend available: SQLite always; Postgres when TEST_DATABASE_URL is set (it is wiped!)."""
    if request.param == "sqlite":
        store = SQLiteStore(tmp_path / "clinic.db")
    else:
        store = PostgresStore(os.environ["TEST_DATABASE_URL"])
    seeded(store)
    set_store(store)
    yield store
    set_store(None)


def bookable_slot(store, min_days: int = 3):
    """An open slot far enough ahead to be bookable and cancellable (48h rule)."""
    rules = get_rules()
    now = datetime.now(UTC)
    for s in store.open_slots(now + timedelta(days=min_days), now + timedelta(days=12)):
        if rules.slot_bookable(s["start_at"], s["end_at"], now).allowed:
            return s
    raise AssertionError("no bookable slot in demo data")
