"""Phase 8: demo mode, Docker-friendly config writes, demo flag in the public API."""

from __future__ import annotations

import os
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app import config_files
from app.api import chat
from app.config import reload_settings
from app.data import SQLiteStore, set_store
from app.demo_seed import DEMO_CATEGORIES, DEMO_DOCS, seed_if_empty


@pytest.fixture
def empty_store(tmp_path):
    store = SQLiteStore(tmp_path / "empty.db")
    store.init_schema(reset=True)
    set_store(store)
    yield store
    set_store(None)


def test_demo_mode_fills_only_an_empty_database(empty_store):
    assert seed_if_empty(empty_store) is True
    doctors = empty_store.list_doctors(active_only=False)
    assert doctors and empty_store.appointment_detail("DEMO0001") is not None
    # a clinic that already has doctors is never overwritten
    empty_store.save_doctor({"id": doctors[0]["id"], "full_name": "Dr. Real Person", "specialty": "GP",
                             "languages": "en", "active": True})
    assert seed_if_empty(empty_store) is False
    assert any(d["full_name"] == "Dr. Real Person" for d in empty_store.list_doctors(active_only=False))


def test_demo_documents_exist():
    for name in DEMO_CATEGORIES:
        assert (DEMO_DOCS / name).is_file(), name


def test_env_write_survives_a_bind_mounted_file(tmp_path: Path, monkeypatch):
    """Docker mounts .env as a single file; renaming over it fails, so the text is written in place."""
    env = tmp_path / ".env"
    env.write_text("# keep me\nLLM_MODEL=old\n", encoding="utf-8")

    def busy(*_a, **_k):
        raise OSError(16, "Device or resource busy")

    monkeypatch.setattr(os, "replace", busy)
    config_files.update_env({"LLM_MODEL": "new-model", "DEMO_MODE": "false"}, path=env)
    text = env.read_text(encoding="utf-8")
    assert "# keep me" in text and "LLM_MODEL=new-model" in text and "DEMO_MODE=false" in text
    assert [p.name for p in tmp_path.iterdir()] == [".env"]  # no temp files left behind


def test_demo_flag_is_public(monkeypatch, sqlite_store):
    app = FastAPI()
    app.include_router(chat.router)
    client = TestClient(app)
    monkeypatch.setenv("DEMO_MODE", "true")
    reload_settings()
    try:
        assert client.get("/api/clinic").json()["demo"] is True
        assert client.get("/api/health").json()["demo"] is True
    finally:
        monkeypatch.delenv("DEMO_MODE")
        reload_settings()
    assert client.get("/api/clinic").json()["demo"] is False
