"""Application factory.

Run with: uvicorn app.main:create_app --factory
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import APIRouter, Depends, FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from starlette.responses import Response
from starlette.types import Scope

from app.auth import enforce_ip_limit
from app.config import Settings, load_settings
from app.container import Services, build_services
from app.errors import install_exception_handlers
from app.logging import log_event, setup_logging
from app.middleware import RequestContextMiddleware
from app.routes import documents, health, me
from app.routes.pages import build_pages_router

logger = logging.getLogger(__name__)

STATIC_DIR = Path(__file__).resolve().parent.parent / "static"


class RevalidatingStaticFiles(StaticFiles):
    """Static assets are not fingerprinted, so make browsers revalidate (ETag) on every load."""

    async def get_response(self, path: str, scope: Scope) -> Response:
        response = await super().get_response(path, scope)
        response.headers["Cache-Control"] = "no-cache"
        return response


def create_app(
    settings: Settings | None = None,
    services: Services | None = None,
    static_dir: Path | None = None,
) -> FastAPI:
    settings = settings or load_settings()
    setup_logging(settings.log_level)
    services = services or build_services(settings)

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        try:
            recovered = await asyncio.to_thread(
                services.repo.recover_stale_documents, settings.ingest_timeout_seconds + 60
            )
            if recovered:
                log_event(logger, "recovered_stale_documents", count=recovered)
        except Exception as exc:  # startup must not fail because the DB is briefly unavailable
            log_event(
                logger, "stale_recovery_skipped", level=logging.WARNING, error=type(exc).__name__
            )
        log_event(logger, "startup", env=settings.app_env)
        yield
        await services.aclose()

    app = FastAPI(
        title="RAG Document Chat",
        version="1.0.0",
        lifespan=lifespan,
        docs_url=None,
        redoc_url=None,
        openapi_url=None if settings.is_production else "/openapi.json",
    )
    app.state.settings = settings
    app.state.services = services

    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.origins,
        allow_methods=["GET", "POST", "PATCH", "DELETE", "OPTIONS"],
        allow_headers=["Authorization", "Content-Type", "X-Request-ID"],
        expose_headers=["X-Request-ID"],
        allow_credentials=False,
        max_age=600,
    )
    # Added last so it is the outermost layer.
    app.add_middleware(RequestContextMiddleware, settings=settings)

    install_exception_handlers(app)
    app.include_router(health.router)
    api = APIRouter(prefix="/api", dependencies=[Depends(enforce_ip_limit)])
    api.include_router(me.router)
    api.include_router(documents.router)
    app.include_router(api)
    static_dir = static_dir or STATIC_DIR
    app.include_router(build_pages_router(static_dir))
    app.mount("/static", RevalidatingStaticFiles(directory=static_dir), name="static")
    return app
