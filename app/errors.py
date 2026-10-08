"""Consistent API error model: {"error": {"code", "message", "request_id"}}."""

from __future__ import annotations

import logging
from typing import Any

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

from app.logging import log_event, request_id_var

logger = logging.getLogger(__name__)


class AppError(Exception):
    """An error with a stable machine code and a message that is safe to show to users."""

    def __init__(
        self,
        status_code: int,
        code: str,
        message: str,
        *,
        headers: dict[str, str] | None = None,
    ) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.code = code
        self.message = message
        self.headers = headers or {}


class NotFoundError(AppError):
    def __init__(self, what: str = "Resource") -> None:
        super().__init__(404, "NOT_FOUND", f"{what} not found.")


class AuthError(AppError):
    def __init__(
        self, message: str = "Authentication required.", code: str = "UNAUTHORIZED"
    ) -> None:
        super().__init__(401, code, message, headers={"WWW-Authenticate": "Bearer"})


class DatabaseError(AppError):
    def __init__(self, pg_code: str | None = None, *, transient: bool = False) -> None:
        if transient:
            super().__init__(
                503,
                "DATABASE_UNAVAILABLE",
                "The database is temporarily unavailable. Please try again.",
            )
        else:
            super().__init__(500, "DATABASE_ERROR", "A database error occurred. Please try again.")
        self.pg_code = pg_code


class ProviderError(AppError):
    """An upstream provider (Anthropic, Voyage) failed. Message is user-safe."""

    def __init__(self, provider: str, message: str, *, status_code: int = 502) -> None:
        super().__init__(status_code, f"{provider.upper()}_ERROR", message)
        self.provider = provider


def error_body(code: str, message: str, request_id: str | None = None) -> dict[str, dict[str, str]]:
    return {
        "error": {
            "code": code,
            "message": message,
            "request_id": request_id or request_id_var.get(),
        }
    }


def error_response(
    status_code: int, code: str, message: str, headers: dict[str, str] | None = None
) -> JSONResponse:
    return JSONResponse(error_body(code, message), status_code=status_code, headers=headers)


_HTTP_CODES = {
    400: "BAD_REQUEST",
    401: "UNAUTHORIZED",
    403: "FORBIDDEN",
    404: "NOT_FOUND",
    405: "METHOD_NOT_ALLOWED",
    409: "CONFLICT",
    413: "PAYLOAD_TOO_LARGE",
    415: "UNSUPPORTED_MEDIA_TYPE",
    422: "VALIDATION_ERROR",
    429: "RATE_LIMITED",
}


def _validation_message(errors: list[Any]) -> str:
    parts = []
    for err in errors[:3]:
        loc = [str(p) for p in err.get("loc", ()) if p not in {"body", "query", "path"}]
        field = ".".join(loc) or "request"
        parts.append(f"{field}: {err.get('msg', 'invalid value')}")
    return "Invalid request. " + "; ".join(parts) if parts else "Invalid request."


def install_exception_handlers(app: FastAPI) -> None:
    @app.exception_handler(AppError)
    async def _app_error(_: Request, exc: AppError) -> JSONResponse:
        if exc.status_code >= 500:
            log_event(
                logger, "app_error", level=logging.WARNING, code=exc.code, status=exc.status_code
            )
        return error_response(exc.status_code, exc.code, exc.message, exc.headers)

    @app.exception_handler(RequestValidationError)
    async def _validation(_: Request, exc: RequestValidationError) -> JSONResponse:
        # Only field names and messages are returned; input values are never echoed.
        return error_response(422, "VALIDATION_ERROR", _validation_message(list(exc.errors())))

    @app.exception_handler(StarletteHTTPException)
    async def _http(_: Request, exc: StarletteHTTPException) -> JSONResponse:
        code = _HTTP_CODES.get(exc.status_code, "HTTP_ERROR")
        message = {404: "Not found.", 405: "Method not allowed."}.get(exc.status_code)
        if message is None:
            message = exc.detail if isinstance(exc.detail, str) else "Request failed."
        return error_response(exc.status_code, code, message, dict(exc.headers or {}))
