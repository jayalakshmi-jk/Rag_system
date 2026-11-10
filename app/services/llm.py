"""Anthropic access behind a small interface (so tests and future providers can swap in)."""

from __future__ import annotations

import logging
from collections.abc import Iterator
from dataclasses import dataclass
from typing import Protocol

import anthropic

from app.config import Settings
from app.errors import ProviderError
from app.logging import log_event

logger = logging.getLogger(__name__)


@dataclass
class Usage:
    input_tokens: int = 0
    output_tokens: int = 0


class LLM(Protocol):
    def stream_answer(
        self, system: str, messages: list[dict[str, str]], max_tokens: int, usage: Usage
    ) -> Iterator[str]:
        """Yield answer text as it is generated. Raises ProviderError on failure."""
        ...

    def complete_fast(self, system: str, prompt: str, max_tokens: int) -> str:
        """One short completion from the fast, cheap model (rewrites, titles, suggestions)."""
        ...


def to_provider_error(exc: Exception) -> ProviderError:
    """Map SDK exceptions to user-safe errors; raw provider messages are never exposed."""
    if isinstance(exc, anthropic.RateLimitError | anthropic.OverloadedError):
        log_event(logger, "llm_error", level=logging.WARNING, kind=type(exc).__name__)
        return ProviderError(
            "anthropic",
            "The AI service is busy right now. Please try again in a moment.",
            status_code=503,
        )
    if isinstance(exc, anthropic.APITimeoutError | anthropic.APIConnectionError):
        log_event(logger, "llm_error", level=logging.WARNING, kind=type(exc).__name__)
        return ProviderError(
            "anthropic",
            "The AI service did not respond in time. Please try again.",
            status_code=504,
        )
    if isinstance(exc, anthropic.AuthenticationError | anthropic.PermissionDeniedError):
        # Our credentials are wrong: an operator problem, not something the user can fix.
        log_event(logger, "llm_misconfigured", level=logging.ERROR, kind=type(exc).__name__)
        return ProviderError("anthropic", "The AI service is unavailable. Please try again later.")
    log_event(logger, "llm_error", level=logging.ERROR, kind=type(exc).__name__)
    return ProviderError("anthropic", "The AI service failed to answer. Please try again.")


class AnthropicLLM:
    def __init__(self, settings: Settings, client: anthropic.Anthropic | None = None) -> None:
        self._model = settings.claude_model
        self._fast_model = settings.claude_fast_model
        self._client = client or anthropic.Anthropic(
            api_key=settings.anthropic_api_key.get_secret_value(),
            timeout=settings.llm_timeout_seconds,
            max_retries=max(settings.provider_max_attempts - 1, 0),
        )

    def stream_answer(
        self, system: str, messages: list[dict[str, str]], max_tokens: int, usage: Usage
    ) -> Iterator[str]:
        try:
            with self._client.messages.stream(
                model=self._model,
                max_tokens=max_tokens,
                system=system,
                messages=messages,  # type: ignore[arg-type]
            ) as stream:
                yield from stream.text_stream
                final = stream.get_final_message()
                usage.input_tokens = final.usage.input_tokens
                usage.output_tokens = final.usage.output_tokens
        except anthropic.AnthropicError as exc:
            raise to_provider_error(exc) from exc

    def complete_fast(self, system: str, prompt: str, max_tokens: int) -> str:
        try:
            response = self._client.messages.create(
                model=self._fast_model,
                max_tokens=max_tokens,
                system=system,
                messages=[{"role": "user", "content": prompt}],
            )
        except anthropic.AnthropicError as exc:
            raise to_provider_error(exc) from exc
        return "".join(block.text for block in response.content if block.type == "text").strip()
