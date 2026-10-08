"""Public pages and the public client configuration.

Everything served here is safe for anonymous visitors. ``/config.json`` exposes only the
Supabase URL and the *anon* key, which are public by design (the browser needs them to sign in).
It must never include the service key or any other secret.
"""

from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter, Response
from fastapi.responses import FileResponse, JSONResponse

from app.deps import SettingsDep


def build_pages_router(static_dir: Path) -> APIRouter:
    router = APIRouter(include_in_schema=False)

    def page(name: str) -> FileResponse:
        return FileResponse(static_dir / name, media_type="text/html", headers=_NO_CACHE)

    @router.get("/")
    def landing() -> FileResponse:
        return page("index.html")

    @router.get("/login")
    def login() -> FileResponse:
        return page("login.html")

    @router.get("/app")
    def workspace() -> FileResponse:
        return page("app.html")

    @router.get("/config.json")
    def client_config(settings: SettingsDep) -> JSONResponse:
        return JSONResponse(
            {
                "supabase_url": settings.supabase_url,
                "supabase_anon_key": settings.supabase_anon_key.get_secret_value(),
                "max_upload_mb": settings.max_upload_mb,
                "max_question_length": settings.max_question_length,
            },
            headers=_NO_CACHE,
        )

    @router.get("/favicon.ico")
    def favicon() -> Response:
        return Response(status_code=204)

    return router


_NO_CACHE = {"Cache-Control": "no-cache"}
