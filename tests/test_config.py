from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

import pytest
from app.config import ConfigError, Settings, load_settings

REQUIRED = {
    "SUPABASE_URL": "https://example.supabase.co",
    "SUPABASE_ANON_KEY": "anon-secret-value",
    "SUPABASE_SERVICE_KEY": "service-secret-value",
    "ANTHROPIC_API_KEY": "anthropic-secret-value",
    "VOYAGE_API_KEY": "voyage-secret-value",
}


@pytest.fixture
def clean_env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> pytest.MonkeyPatch:
    """No .env file and no leaked variables from the developer's shell."""
    monkeypatch.chdir(tmp_path)
    for field in Settings.model_fields:
        monkeypatch.delenv(field.upper(), raising=False)
    return monkeypatch


def set_env(monkeypatch: pytest.MonkeyPatch, **extra: str) -> None:
    for key, value in {**REQUIRED, **extra}.items():
        monkeypatch.setenv(key, value)


def test_valid_environment_loads_with_defaults(clean_env: pytest.MonkeyPatch) -> None:
    set_env(clean_env)
    loaded = load_settings()
    assert loaded.app_env == "development"
    assert loaded.embed_dim == 1024
    assert loaded.embed_model == "voyage-4"
    assert loaded.daily_question_limit == 50
    assert loaded.origins == ["http://localhost:8000"]
    assert loaded.max_upload_bytes == 25 * 1024 * 1024
    assert loaded.supabase_jwt_secret is None


def test_missing_required_settings_fail_fast_with_a_clear_message(
    clean_env: pytest.MonkeyPatch,
) -> None:
    with pytest.raises(ConfigError) as caught:
        load_settings()
    message = str(caught.value)
    for name in REQUIRED:
        assert f"{name} is required" in message
    assert "Traceback" not in message


def test_error_messages_never_echo_secret_values(clean_env: pytest.MonkeyPatch) -> None:
    set_env(clean_env, EMBED_DIM="999", SUPABASE_URL="not-a-url", ANTHROPIC_API_KEY="   ")
    with pytest.raises(ConfigError) as caught:
        load_settings()
    message = str(caught.value)
    assert "EMBED_DIM" in message
    assert "SUPABASE_URL" in message
    assert "ANTHROPIC_API_KEY" in message
    for secret in ("anon-secret-value", "service-secret-value", "voyage-secret-value"):
        assert secret not in message


def test_secrets_are_masked_in_repr(settings: Settings) -> None:
    text = repr(settings) + str(settings.model_dump())
    for secret in ("anon-test-key", "service-test-key", "anthropic-test-key", "voyage-test-key"):
        assert secret not in text


@pytest.mark.parametrize("dim", [128, 768, 2048])
def test_unsupported_embedding_dimensions_are_rejected(
    make_settings: Callable[..., Settings], dim: int
) -> None:
    with pytest.raises(ValueError, match="one of"):
        make_settings(embed_dim=dim)


@pytest.mark.parametrize("origins", ["*", "localhost:8000", "http://x.test/path", ""])
def test_cors_origins_must_be_explicit(
    make_settings: Callable[..., Settings], origins: str
) -> None:
    with pytest.raises(ValueError):
        make_settings(allowed_origins=origins)


def test_production_requires_https(make_settings: Callable[..., Settings]) -> None:
    with pytest.raises(ValueError, match="https"):
        make_settings(app_env="production", allowed_origins="http://app.example.com")
    with pytest.raises(ValueError, match="https"):
        make_settings(
            app_env="production",
            allowed_origins="https://app.example.com",
            supabase_url="http://example.supabase.co",
        )
    ok = make_settings(app_env="production", allowed_origins="https://app.example.com")
    assert ok.is_production


def test_origins_are_normalised(make_settings: Callable[..., Settings]) -> None:
    configured = make_settings(allowed_origins="https://a.example.com/, https://b.example.com")
    assert configured.origins == ["https://a.example.com", "https://b.example.com"]
