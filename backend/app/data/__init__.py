"""Pick the data store from settings: DATA_SOURCE = sqlite | postgres | excel | google_sheets."""

from __future__ import annotations

import threading

from ..config import ConfigError, Settings, get_settings
from .base import DataStore, Row, SlotTaken, Tx
from .sources import SourceError
from .sql import PostgresStore, SchemaOutdated, SQLiteStore
from .synced import ExcelStore, GoogleSheetsStore, SyncedStore

__all__ = [
    "DataStore", "Row", "SlotTaken", "Tx", "SourceError", "SchemaOutdated", "SQLiteStore", "PostgresStore",
    "ExcelStore", "GoogleSheetsStore", "SyncedStore", "build_store", "get_store", "set_store",
]

_store: DataStore | None = None
_lock = threading.Lock()


def build_store(settings: Settings | None = None, kind: str | None = None) -> DataStore:
    s = settings or get_settings()
    kind = kind or s.data_source
    tz = s.clinic.tz
    if kind == "sqlite":
        return SQLiteStore(s.sqlite_path)
    if kind == "postgres":
        url = s.secret("database_url")
        if not url:
            raise ConfigError("DATA_SOURCE is postgres but DATABASE_URL is empty. Add the connection string to .env.")
        return PostgresStore(url)
    if kind == "excel":
        return ExcelStore(s.sqlite_path, s.excel_path, tz)
    if kind == "google_sheets":
        if not s.google_sheets_id:
            raise ConfigError("DATA_SOURCE is google_sheets but GOOGLE_SHEETS_ID is empty. Add the sheet id to .env.")
        return GoogleSheetsStore(s.sqlite_path, s.google_sheets_id, s.google_service_account_file, tz)
    raise ConfigError(f"Unknown DATA_SOURCE '{kind}'. Use sqlite, postgres, excel or google_sheets.")


def get_store() -> DataStore:
    global _store
    with _lock:
        if _store is None:
            _store = build_store()
        return _store


def set_store(store: DataStore | None) -> None:
    """Swap the process-wide store (tests, and later the setup wizard)."""
    global _store
    with _lock:
        if _store is not None and _store is not store:
            _store.close()
        _store = store
