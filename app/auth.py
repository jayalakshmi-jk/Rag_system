"""FastAPI authentication dependencies.

Every protected request must carry ``Authorization: Bearer <access token>``; see ``app.tokens``
for the verification itself.
"""

from __future__ import annotations

from typing import Annotated, Any

from fastapi import Depends, Request

from app.deps import ServicesDep, SettingsDep
from app.errors import AuthError, RateLimitError
from app.services.limits import client_ip
from app.tokens import CurrentUserData


def _bearer_token(request: Request) -> str | None:
    header = request.headers.get("authorization", "")
    scheme, _, token = header.partition(" ")
    token = token.strip()
    if scheme.lower() != "bearer" or not token or " " in token:
        return None
    return token


def get_current_user(
    request: Request, settings: SettingsDep, services: ServicesDep
) -> CurrentUserData:
    """FastAPI dependency (sync, so it runs in the threadpool and may do blocking I/O)."""
    ip = client_ip(request, settings.trusted_proxy_count)
    limiter = services.limiter
    failure_limit = settings.rate_limit_auth_failures_per_minute

    if limiter.count("auth-fail", ip, 60) >= failure_limit:
        raise RateLimitError(60, "Too many failed sign-in attempts. Please wait a minute.")
    try:
        token = _bearer_token(request)
        if token is None:
            raise AuthError()
        user = services.verifier.verify(token)
    except AuthError:
        limiter.hit("auth-fail", ip, failure_limit * 2, 60)
        raise

    if not services.accounts.is_fresh(user.id):
        if not services.repo.profile_exists(user.id):
            limiter.hit("auth-fail", ip, failure_limit * 2, 60)
            raise AuthError("This account no longer exists.", "ACCOUNT_NOT_FOUND")
        services.accounts.remember(user.id)

    limiter.check("user", user.id, settings.rate_limit_user_per_minute, 60)
    request.state.user_id = user.id
    return user


CurrentUser = Annotated[CurrentUserData, Depends(get_current_user)]


def enforce_ip_limit(request: Request, settings: SettingsDep, services: ServicesDep) -> None:
    """Router-level dependency: global per-IP request budget for the API."""
    ip = client_ip(request, settings.trusted_proxy_count)
    services.limiter.check("ip", ip, settings.rate_limit_ip_per_minute, 60)


def rate_limited(bucket: str, limit_attr: str, window: float) -> Any:
    """Build a per-user dependency for expensive routes (chat, upload, account)."""

    def dependency(user: CurrentUser, settings: SettingsDep, services: ServicesDep) -> None:
        services.limiter.check(bucket, user.id, getattr(settings, limit_attr), window)

    return Depends(dependency)


__all__ = ["CurrentUser", "enforce_ip_limit", "get_current_user", "rate_limited"]
