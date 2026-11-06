"""Shared fixtures. All external systems (Supabase, Anthropic, Voyage) are replaced by fakes."""

from __future__ import annotations

import time
import uuid
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from typing import Any

import jwt
import pytest
from app.config import Settings
from app.container import Services
from app.main import create_app
from app.repository import Repository
from app.services.limits import RateLimiter
from app.tokens import AccountCache, TokenVerifier
from fastapi import FastAPI
from fastapi.testclient import TestClient

from tests.fakes.supabase import FakeSupabase

JWT_SECRET = "unit-test-jwt-secret-that-is-long-enough-for-hs256-signing"
ISSUER = "https://example.supabase.co/auth/v1"

TEST_SETTINGS: dict[str, Any] = {
    "app_env": "test",
    "supabase_url": "https://example.supabase.co",
    "supabase_anon_key": "anon-test-key",
    "supabase_service_key": "service-test-key",
    "anthropic_api_key": "anthropic-test-key",
    "voyage_api_key": "voyage-test-key",
    "supabase_jwt_secret": JWT_SECRET,
    "allowed_origins": "http://localhost:8000",
    "log_level": "WARNING",
}


@pytest.fixture
def make_settings() -> Callable[..., Settings]:
    def factory(**overrides: Any) -> Settings:
        return Settings(_env_file=None, **{**TEST_SETTINGS, **overrides})

    return factory


@pytest.fixture
def settings(make_settings: Callable[..., Settings]) -> Settings:
    return make_settings()


@pytest.fixture(autouse=True)
def _no_backoff_sleep(monkeypatch: pytest.MonkeyPatch) -> None:
    """Retries must not slow the suite down."""
    monkeypatch.setattr("app.db.time.sleep", lambda _seconds: None)


@pytest.fixture
def fake_db() -> FakeSupabase:
    return FakeSupabase()


@pytest.fixture
def services(fake_db: FakeSupabase, settings: Settings) -> Services:
    return Services(
        repo=Repository(fake_db),
        verifier=TokenVerifier(settings),
        limiter=RateLimiter(),
        accounts=AccountCache(settings.account_cache_seconds),
    )


@pytest.fixture
def app(settings: Settings, services: Services) -> FastAPI:
    return create_app(settings, services)


@pytest.fixture
def client(app: FastAPI) -> Iterator[TestClient]:
    with TestClient(app, raise_server_exceptions=False) as test_client:
        yield test_client


def make_token(
    sub: str | None = None,
    *,
    secret: str = JWT_SECRET,
    algorithm: str = "HS256",
    expires_in: int = 3600,
    **claims: Any,
) -> str:
    """Sign an access token the way Supabase Auth would."""
    now = int(time.time())
    payload: dict[str, Any] = {
        "sub": sub or str(uuid.uuid4()),
        "aud": "authenticated",
        "iss": ISSUER,
        "role": "authenticated",
        "email": "student@example.com",
        "iat": now,
        "exp": now + expires_in,
        **claims,
    }
    payload = {k: v for k, v in payload.items() if v is not None}
    return jwt.encode(payload, secret, algorithm=algorithm)


@dataclass
class TestUser:
    __test__ = False  # not a pytest test class

    id: str
    headers: dict[str, str]


@pytest.fixture
def new_user(fake_db: FakeSupabase) -> Callable[..., TestUser]:
    """Create an account (auth user + profile) and return its id and Authorization header."""

    def factory(name: str | None = "Test User", email: str = "student@example.com") -> TestUser:
        user_id = fake_db.add_user(name=name)
        token = make_token(user_id, email=email)
        return TestUser(id=user_id, headers={"Authorization": f"Bearer {token}"})

    return factory


@pytest.fixture
def user(new_user: Callable[..., TestUser]) -> TestUser:
    return new_user()
