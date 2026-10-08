"""Liveness and readiness probes."""

from __future__ import annotations

from fastapi import APIRouter

from app.deps import ServicesDep
from app.errors import AppError

router = APIRouter(tags=["health"])


@router.get("/healthz")
def healthz() -> dict[str, str]:
    """Liveness: the process is up. Does not touch dependencies."""
    return {"status": "ok"}


@router.get("/readyz")
def readyz(services: ServicesDep) -> dict[str, object]:
    """Readiness: the database is reachable."""
    try:
        services.repo.ping()
    except AppError:
        raise AppError(
            503, "NOT_READY", "The service is not ready: database is unreachable."
        ) from None
    return {"status": "ready", "checks": {"database": "ok"}}
