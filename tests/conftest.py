"""Shared fixtures. All external systems (Supabase, Anthropic, Voyage) are replaced by fakes."""

from __future__ import annotations

from collections.abc import Callable, Iterator
from typing import Any

import pytest
from app.config import Settings
from app.container import Services
from app.main import create_app
from app.repository import Repository
from fastapi import FastAPI
from fastapi.testclient import TestClient

from tests.fakes.supabase import FakeSupabase

TEST_SETTINGS: dict[str, Any] = {
    "app_env": "test",
    "supabase_url": "https://example.supabase.co",
    "supabase_anon_key": "anon-test-key",
    "supabase_service_key": "service-test-key",
    "anthropic_api_key": "anthropic-test-key",
    "voyage_api_key": "voyage-test-key",
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
def services(fake_db: FakeSupabase) -> Services:
    return Services(repo=Repository(fake_db))


@pytest.fixture
def app(settings: Settings, services: Services) -> FastAPI:
    return create_app(settings, services)


@pytest.fixture
def client(app: FastAPI) -> Iterator[TestClient]:
    with TestClient(app, raise_server_exceptions=False) as test_client:
        yield test_client
