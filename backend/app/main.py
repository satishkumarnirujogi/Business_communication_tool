"""FastAPI entry point: `uvicorn app.main:app` from the backend/ folder."""

import logging
import sys
import threading
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import FileResponse, PlainTextResponse
from fastapi.staticfiles import StaticFiles

from .config import ROOT, ConfigError, get_settings
from .data import SchemaOutdated, SyncedStore, get_store
from .data.sync import SyncWorker
from .privacy import install_log_masking
from .rules import get_rules
from .scheduling import SlotWorker

STATIC = Path(__file__).resolve().parent.parent / "static"
log = logging.getLogger("clinic")


def _prepare_knowledge(demo: bool = False) -> None:
    """Open the knowledge base, load the embedding model, and finish documents a restart interrupted
    (or that were indexed with a different embedding model). In demo mode the knowledge base gets the
    demo documents (missing ones are added). Failures leave chat working without it."""
    try:
        from .knowledge import get_kb

        kb = get_kb()
        if demo:
            # Idempotent: unchanged documents are skipped, missing ones (e.g. after an interrupted first start)
            # are added. Other uploaded documents are left alone.
            from .demo_seed import load_demo_knowledge

            load_demo_knowledge(kb)
        pending = kb.reindex_needed()
        if pending:
            log.info("knowledge: re-indexing %d document(s)", len(pending))
            kb.reindex(pending)
        if kb.list_documents():
            kb.embedder.embed_query("warm up")  # first query would otherwise wait for the model to load
    except Exception as e:
        log.error("knowledge base unavailable: %s", e)


def create_app() -> FastAPI:
    settings = get_settings()
    get_rules()  # validate rules.yaml at startup
    logging.basicConfig(level=settings.log_level, format="%(levelname)s %(name)s: %(message)s")
    install_log_masking()  # emails, phone numbers and birth dates never reach the server log
    if not settings.llm_configured:
        log.warning("LLM_PROVIDER / LLM_MODEL are not set; chat will not work until they are.")

    store = get_store()
    try:
        store.init_schema()  # creates missing tables; never drops data
    except SchemaOutdated as e:
        raise ConfigError(str(e)) from None
    except Exception as e:
        raise ConfigError(f"Can't open the {store.kind} database: {type(e).__name__}: {e}"[:500]) from None
    log.info("data source: %s", store.kind)
    if settings.demo_mode:
        from .demo_seed import seed_if_empty

        try:
            seed_if_empty(store)
        except Exception as e:
            log.error("demo mode: could not fill the database: %s", e)

    @asynccontextmanager
    async def lifespan(_app: FastAPI):
        worker = None
        if isinstance(store, SyncedStore):
            worker = SyncWorker(store, settings.sync_interval_seconds)
            threading.Thread(target=store.sync, name="initial-sync", daemon=True).start()
            worker.start()
        threading.Thread(target=_prepare_knowledge, args=(settings.demo_mode,), name="kb-startup", daemon=True).start()
        slot_worker = SlotWorker(store)  # keeps the rolling booking window filled (hourly)
        slot_worker.start()
        from .channels.mail import EmailPoller
        from .channels.reminders import ReminderWorker

        poller, reminders = EmailPoller(), ReminderWorker()  # both idle until their channel is set up
        poller.start()
        reminders.start()
        yield
        reminders.stop()
        poller.stop()
        slot_worker.stop()
        if worker:
            worker.stop()

    from .api import admin, auth, calendar, channels, chat, conversations, directory, knowledge, webhooks
    from .api import settings as settings_api

    app = FastAPI(title=f"{settings.clinic.name} Assistant", lifespan=lifespan)
    for module in (chat, auth, settings_api, admin, conversations, directory, knowledge, calendar, channels, webhooks):
        app.include_router(module.router)

    # ---- web apps (built from frontend/ with `npm run build`) ----------------------------
    dist = ROOT / "frontend" / "dist"
    branding = ROOT / "data" / "branding"
    branding.mkdir(parents=True, exist_ok=True)
    app.mount("/branding", StaticFiles(directory=branding), name="branding")
    missing = PlainTextResponse("The web UI isn't built yet. Run `npm install && npm run build` in frontend/.", 404)

    if dist.is_dir():
        app.mount("/ui", StaticFiles(directory=dist), name="ui")

    @app.get("/", include_in_schema=False)
    def patient_chat():
        page = dist / "chat.html"
        return FileResponse(page) if page.is_file() else FileResponse(STATIC / "index.html")

    @app.get("/admin", include_in_schema=False)
    @app.get("/admin/", include_in_schema=False)
    def admin_app():
        page = dist / "admin.html"
        return FileResponse(page) if page.is_file() else missing

    @app.get("/widget.js", include_in_schema=False)
    def widget():
        script = dist / "widget.js"
        if not script.is_file():
            return missing
        return FileResponse(script, media_type="text/javascript", headers={"Cache-Control": "public, max-age=300"})

    return app


try:
    app = create_app()
except ConfigError as e:
    # Readable message instead of a stack trace for whoever is setting up the clinic.
    print(f"\n{e}\n", file=sys.stderr)
    sys.exit(1)
