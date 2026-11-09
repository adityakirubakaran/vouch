"""FastAPI application factory."""

from __future__ import annotations

import logging
import signal
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Optional

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from ..config import REPO_ROOT, Settings, get_settings
from ..services import Services, build_services, start_services, stop_services
from . import realtime, routes

FRONTEND_DIST = REPO_ROOT / "frontend" / "dist"


def _close_streams_on_exit(svc) -> None:
    """On Ctrl+C, uvicorn waits for open connections to finish before it shuts down,
    and the review screen's live-update stream never finishes on its own. Close those
    streams as soon as the signal arrives, so one Ctrl+C exits cleanly."""
    for sig in (signal.SIGINT, signal.SIGTERM):
        previous = signal.getsignal(sig)
        if not callable(previous):
            continue

        def handler(signum, frame, previous=previous):
            svc.bus.close_all()
            previous(signum, frame)

        try:
            signal.signal(sig, handler)
        except ValueError:  # not the main thread (tests run the app in a worker thread)
            return


def create_app(settings: Optional[Settings] = None, services: Optional[Services] = None) -> FastAPI:
    settings = settings or get_settings()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        svc = services or build_services(settings)
        app.state.services = svc
        if settings.start_engine:
            await start_services(svc)
        _close_streams_on_exit(svc)
        logging.getLogger("vouch").info(
            "Vouch ready: Claude %s, calls %s, email %s, accounting %s",
            svc.llm.mode, svc.calls.resolve_channel(None), settings.email_mode, svc.accounting.mode,
        )
        try:
            yield
        finally:
            await stop_services(svc)

    app = FastAPI(title="Vouch", version="0.1.0", lifespan=lifespan)
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["http://localhost:5173", "http://127.0.0.1:5173"],
        allow_methods=["*"],
        allow_headers=["*"],
    )
    app.include_router(routes.router)
    app.include_router(realtime.router)

    if FRONTEND_DIST.exists():
        app.mount("/assets", StaticFiles(directory=FRONTEND_DIST / "assets"), name="assets")

        @app.get("/{path:path}", include_in_schema=False)
        async def spa(path: str):
            candidate = FRONTEND_DIST / path
            if path and candidate.is_file() and FRONTEND_DIST in candidate.resolve().parents:
                return FileResponse(candidate)
            return FileResponse(FRONTEND_DIST / "index.html")

    return app
