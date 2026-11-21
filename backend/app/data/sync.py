"""Background sync for spreadsheet stores: every N seconds, and shortly after each booking change."""

from __future__ import annotations

import logging
import threading

from .base import DataStore

log = logging.getLogger("clinic.sync")

DEBOUNCE_SECONDS = 2.0


class SyncWorker:
    def __init__(self, store: DataStore, interval_seconds: float):
        self.store = store
        self.interval = interval_seconds
        self._wake = threading.Event()
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, name="sheet-sync", daemon=True)
        store.on_change(lambda _what: self.trigger())

    def start(self) -> None:
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        self._wake.set()
        self._thread.join(timeout=10)

    def trigger(self) -> None:
        """Ask for a sync soon (used after bookings, so the sheet shows them within seconds)."""
        self._wake.set()

    def _run(self) -> None:
        while not self._stop.is_set():
            self._wake.wait(timeout=self.interval)
            if self._stop.is_set():
                break
            if self._wake.is_set():
                self._wake.clear()
                self._stop.wait(DEBOUNCE_SECONDS)  # several bookings in a burst -> one sync
            self.store.sync()
