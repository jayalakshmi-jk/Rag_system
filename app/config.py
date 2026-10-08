"""Application configuration: Pydantic Settings, validated at startup (fail fast)."""

from __future__ import annotations

from typing import Literal
from urllib.parse import urlparse

from pydantic import Field, SecretStr, ValidationError, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

SUPPORTED_EMBED_DIMS = (256, 512, 1024)


class ConfigError(RuntimeError):
    """Raised at startup when required configuration is missing or invalid."""


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    app_env: Literal["development", "test", "production"] = "development"

    # Supabase
    supabase_url: str
    supabase_anon_key: SecretStr
    supabase_service_key: SecretStr
    supabase_jwt_secret: SecretStr | None = None

    # Anthropic
    anthropic_api_key: SecretStr
    claude_model: str = "claude-sonnet-5-5"
    claude_fast_model: str = "claude-haiku-4-5-20251001"

    # Voyage
    voyage_api_key: SecretStr
    embed_model: str = "voyage-4"
    embed_dim: int = 1024

    # Limits and cost controls
    daily_question_limit: int = Field(default=50, ge=1, le=10_000)
    max_upload_mb: int = Field(default=25, ge=1, le=100)
    max_document_pages: int = Field(default=500, ge=1, le=5_000)
    max_question_length: int = Field(default=2_000, ge=50, le=20_000)
    max_context_tokens: int = Field(default=6_000, ge=500, le=50_000)
    max_output_tokens: int = Field(default=1_200, ge=100, le=8_000)
    context_chunks: int = Field(default=6, ge=5, le=8)
    retrieval_candidates: int = Field(default=20, ge=10, le=50)
    history_messages: int = Field(default=8, ge=0, le=30)

    # Rate limits (per process; see README for multi-instance notes)
    rate_limit_ip_per_minute: int = Field(default=240, ge=1)
    rate_limit_user_per_minute: int = Field(default=120, ge=1)
    rate_limit_chat_per_minute: int = Field(default=12, ge=1)
    rate_limit_upload_per_hour: int = Field(default=30, ge=1)
    rate_limit_account_per_hour: int = Field(default=5, ge=1)

    # Ingestion
    ingest_timeout_seconds: int = Field(default=900, ge=30)
    ingest_concurrency: int = Field(default=2, ge=1, le=16)

    # Provider calls
    http_timeout_seconds: float = Field(default=30.0, gt=0)
    llm_timeout_seconds: float = Field(default=90.0, gt=0)
    provider_max_attempts: int = Field(default=4, ge=1, le=8)

    # Web
    allowed_origins: str = "http://localhost:8000"
    trusted_proxy_count: int = Field(default=0, ge=0, le=5)
    log_level: str = "INFO"
    port: int = Field(default=8000, ge=1, le=65535)

    @field_validator("supabase_jwt_secret", mode="before")
    @classmethod
    def _blank_secret_is_none(cls, value: object) -> object:
        if isinstance(value, str) and not value.strip():
            return None
        return value

    @field_validator("supabase_url")
    @classmethod
    def _check_supabase_url(cls, value: str) -> str:
        value = value.strip().rstrip("/")
        parsed = urlparse(value)
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            raise ValueError("must be an http(s) URL such as https://xyz.supabase.co")
        return value

    @field_validator("embed_dim")
    @classmethod
    def _check_embed_dim(cls, value: int) -> int:
        if value not in SUPPORTED_EMBED_DIMS:
            raise ValueError(f"must be one of {', '.join(map(str, SUPPORTED_EMBED_DIMS))}")
        return value

    @field_validator("log_level")
    @classmethod
    def _check_log_level(cls, value: str) -> str:
        level = value.strip().upper()
        if level not in {"DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"}:
            raise ValueError("must be DEBUG, INFO, WARNING, ERROR or CRITICAL")
        return level

    @field_validator(
        "anthropic_api_key", "voyage_api_key", "supabase_anon_key", "supabase_service_key"
    )
    @classmethod
    def _non_empty_secret(cls, value: SecretStr) -> SecretStr:
        if not value.get_secret_value().strip():
            raise ValueError("must not be empty")
        return value

    @field_validator("claude_model", "claude_fast_model", "embed_model")
    @classmethod
    def _non_empty_name(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("must not be empty")
        return value

    @model_validator(mode="after")
    def _check_origins_and_env(self) -> Settings:
        origins = self.origins
        if not origins:
            raise ValueError("ALLOWED_ORIGINS must list at least one origin")
        for origin in origins:
            parsed = urlparse(origin)
            if origin == "*" or parsed.scheme not in {"http", "https"} or not parsed.netloc:
                raise ValueError(
                    f"ALLOWED_ORIGINS entry {origin!r} must be an explicit http(s) origin"
                )
            if parsed.path not in {"", "/"}:
                raise ValueError(f"ALLOWED_ORIGINS entry {origin!r} must not contain a path")
        if self.app_env == "production":
            if urlparse(self.supabase_url).scheme != "https":
                raise ValueError("SUPABASE_URL must use https in production")
            insecure = [o for o in origins if urlparse(o).scheme != "https"]
            if insecure:
                raise ValueError("ALLOWED_ORIGINS must use https in production")
        return self

    @property
    def origins(self) -> list[str]:
        return [o.strip().rstrip("/") for o in self.allowed_origins.split(",") if o.strip()]

    @property
    def supabase_origin(self) -> str:
        parsed = urlparse(self.supabase_url)
        return f"{parsed.scheme}://{parsed.netloc}"

    @property
    def is_production(self) -> bool:
        return self.app_env == "production"

    @property
    def max_upload_bytes(self) -> int:
        return self.max_upload_mb * 1024 * 1024


def _format_validation_error(exc: ValidationError) -> str:
    lines = []
    for err in exc.errors():
        loc = ".".join(str(part) for part in err["loc"]) or "settings"
        name = loc.upper()
        if err["type"] == "missing":
            lines.append(f"  - {name} is required")
        else:
            # Never echo the offending value: it may be a secret.
            lines.append(f"  - {name}: {err['msg'].removeprefix('Value error, ')}")
    return "\n".join(lines)


def load_settings() -> Settings:
    """Load and validate settings; raise ConfigError with a readable message on failure."""
    try:
        return Settings()
    except ValidationError as exc:
        raise ConfigError(
            "Invalid configuration. Fix the following environment variables "
            "(see .env.example):\n" + _format_validation_error(exc)
        ) from None
