"""Scripted LLM for tests: records every call and yields a canned streaming answer."""

from __future__ import annotations

from collections.abc import Iterator

from app.errors import ProviderError
from app.services.llm import Usage


class FakeLLM:
    def __init__(self) -> None:
        self.answer = "Photosynthesis converts light into chemical energy [1]."
        self.pieces: list[str] | None = None
        self.fail_before_first_token: Exception | None = None
        self.fail_after_tokens: tuple[int, Exception] | None = None
        self.fast_replies: dict[str, str | Exception] = {
            "title": "Photosynthesis basics",
            "suggest": "What is chlorophyll?\nWhere does it happen?\nWhy is it important?",
            "condense": "",
        }
        self.stream_calls: list[dict[str, object]] = []
        self.fast_calls: list[tuple[str, str]] = []

    def stream_answer(
        self, system: str, messages: list[dict[str, str]], max_tokens: int, usage: Usage
    ) -> Iterator[str]:
        self.stream_calls.append({"system": system, "messages": messages, "max_tokens": max_tokens})
        if self.fail_before_first_token is not None:
            raise self.fail_before_first_token
        pieces = self.pieces or [self.answer[i : i + 12] for i in range(0, len(self.answer), 12)]
        for count, piece in enumerate(pieces):
            if self.fail_after_tokens and count >= self.fail_after_tokens[0]:
                raise self.fail_after_tokens[1]
            yield piece
        usage.input_tokens, usage.output_tokens = 111, 22

    def complete_fast(self, system: str, prompt: str, max_tokens: int) -> str:
        kind = (
            "condense"
            if system.startswith("Rewrite")
            else "title"
            if "title" in system
            else "suggest"
        )
        self.fast_calls.append((kind, prompt))
        reply = self.fast_replies[kind]
        if isinstance(reply, Exception):
            raise reply
        if kind == "condense" and not reply:
            return prompt.rsplit("Latest message:", 1)[-1].strip()
        return reply


def llm_down() -> ProviderError:
    return ProviderError("anthropic", "The AI service is busy right now.", status_code=503)
