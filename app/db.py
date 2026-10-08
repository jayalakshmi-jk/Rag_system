"""Supabase client factory and a single choke point for executing queries with retries."""

from __future__ import annotations

import logging
import random
import time
from typing import Any, Protocol

import httpx
from postgrest.exceptions import APIError
from supabase import Client, create_client
from supabase.lib.client_options import SyncClientOptions

from app.config import Settings
from app.errors import DatabaseError
from app.logging import log_event

logger = logging.getLogger(__name__)

_TRANSIENT_PG_CODES = {"08000", "08003", "08006", "57P01", "53300", "40001", "40P01"}
_BACKOFF_SECONDS = (0.2, 0.6, 1.5)
_JITTER = random.SystemRandom()


class _Executable(Protocol):
    def execute(self) -> Any: ...


def create_supabase(settings: Settings) -> Client:
    """Create the server-side (service role) client. The service key never leaves the server."""
    options = SyncClientOptions(
        postgrest_client_timeout=settings.http_timeout_seconds,
        storage_client_timeout=int(settings.http_timeout_seconds),
        function_client_timeout=int(settings.http_timeout_seconds),
        auto_refresh_token=False,
        persist_session=False,
    )
    return create_client(
        settings.supabase_url,
        settings.supabase_service_key.get_secret_value(),
        options=options,
    )


def _is_transient(exc: BaseException) -> bool:
    if isinstance(exc, httpx.TransportError):
        return True
    if isinstance(exc, httpx.HTTPStatusError):
        return exc.response.status_code in {429, 500, 502, 503, 504}
    if isinstance(exc, APIError):
        code = str(exc.code or "")
        if code.startswith("PGRST00") or code in _TRANSIENT_PG_CODES:
            return True
        return code.isdigit() and len(code) == 3 and (code == "429" or int(code) >= 500)
    return False


def run(builder: _Executable, *, attempts: int = 3) -> Any:
    """Execute a PostgREST request with retries on transient failures.

    Returns the response ``data``. Raises DatabaseError (503 when transient, 500 otherwise).
    """
    last: BaseException | None = None
    for attempt in range(attempts):
        try:
            return builder.execute().data
        except Exception as exc:
            if not isinstance(exc, httpx.HTTPError | APIError):
                raise
            last = exc
            if not _is_transient(exc):
                code = str(exc.code) if isinstance(exc, APIError) else None
                log_event(
                    logger, "db_error", level=logging.ERROR, pg_code=code, error=type(exc).__name__
                )
                raise DatabaseError(pg_code=code) from None
            if attempt < attempts - 1:
                time.sleep(
                    _BACKOFF_SECONDS[min(attempt, len(_BACKOFF_SECONDS) - 1)]
                    * (0.75 + _JITTER.random() / 2)
                )
    log_event(
        logger, "db_unavailable", level=logging.ERROR, error=type(last).__name__ if last else None
    )
    raise DatabaseError(transient=True) from None
