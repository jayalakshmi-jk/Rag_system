"""Service container: constructs and holds long-lived clients and services."""

from __future__ import annotations

from dataclasses import dataclass

from app.config import Settings
from app.db import create_supabase
from app.repository import Repository
from app.services.limits import RateLimiter
from app.tokens import AccountCache, TokenVerifier


@dataclass
class Services:
    repo: Repository
    verifier: TokenVerifier
    limiter: RateLimiter
    accounts: AccountCache

    async def aclose(self) -> None:
        """Release network resources on shutdown."""


def build_services(settings: Settings) -> Services:
    return Services(
        repo=Repository(create_supabase(settings)),
        verifier=TokenVerifier(settings),
        limiter=RateLimiter(),
        accounts=AccountCache(settings.account_cache_seconds),
    )
