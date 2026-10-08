"""Pure-ASGI middleware: request IDs, access logging, security headers, last-resort 500s."""

from __future__ import annotations

import logging
import re
import time
import uuid

from starlette.datastructures import Headers, MutableHeaders
from starlette.responses import JSONResponse
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from app.config import Settings
from app.errors import error_body
from app.logging import log_event, request_id_var

logger = logging.getLogger("app.request")

_REQUEST_ID_RE = re.compile(r"^[A-Za-z0-9_-]{8,64}$")


def build_security_headers(settings: Settings) -> dict[str, str]:
    csp = "; ".join(
        [
            "default-src 'self'",
            "script-src 'self'",
            "style-src 'self'",
            "img-src 'self' data:",
            "font-src 'self'",
            f"connect-src 'self' {settings.supabase_origin}",
            "base-uri 'self'",
            "form-action 'self'",
            "frame-ancestors 'none'",
            "object-src 'none'",
        ]
    )
    headers = {
        "Content-Security-Policy": csp,
        "X-Content-Type-Options": "nosniff",
        "X-Frame-Options": "DENY",
        "Referrer-Policy": "strict-origin-when-cross-origin",
        "Permissions-Policy": "camera=(), microphone=(), geolocation=(), payment=()",
        "Cross-Origin-Opener-Policy": "same-origin",
        "Cross-Origin-Resource-Policy": "same-origin",
    }
    if settings.is_production:
        headers["Strict-Transport-Security"] = "max-age=31536000; includeSubDomains"
    return headers


class RequestContextMiddleware:
    """Assigns a request ID, logs one line per request and applies security headers.

    It is the outermost middleware so even unhandled exceptions are turned into the standard
    JSON error shape (never a stack trace) and still carry the request ID and security headers.
    """

    def __init__(self, app: ASGIApp, settings: Settings) -> None:
        self.app = app
        self.security_headers = build_security_headers(settings)

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        incoming = Headers(scope=scope).get("x-request-id", "")
        request_id = incoming if _REQUEST_ID_RE.fullmatch(incoming) else uuid.uuid4().hex
        token = request_id_var.set(request_id)
        scope.setdefault("state", {})["request_id"] = request_id
        started = time.perf_counter()
        status = 500
        response_started = False
        is_api = str(scope.get("path", "")).startswith("/api/")

        async def send_wrapper(message: Message) -> None:
            nonlocal status, response_started
            if message["type"] == "http.response.start":
                response_started = True
                status = message["status"]
                headers = MutableHeaders(scope=message)
                headers["X-Request-ID"] = request_id
                for name, value in self.security_headers.items():
                    headers.setdefault(name, value)
                if is_api:
                    headers.setdefault("Cache-Control", "no-store")
            await send(message)

        try:
            await self.app(scope, receive, send_wrapper)
        except Exception:
            logger.exception("unhandled_exception")
            if not response_started:
                response = JSONResponse(
                    error_body(
                        "INTERNAL_ERROR",
                        "Something went wrong on our side. Please try again.",
                        request_id,
                    ),
                    status_code=500,
                )
                await response(scope, receive, send_wrapper)
        finally:
            route = scope.get("route")
            user_id = (scope.get("state") or {}).get("user_id")
            log_event(
                logger,
                "request",
                method=scope.get("method"),
                route=getattr(route, "path", None) or "unmatched",
                status=status,
                latency_ms=round((time.perf_counter() - started) * 1000, 1),
                user_id=user_id,
            )
            request_id_var.reset(token)
