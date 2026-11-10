"""Deterministic offline embedder: hashed bag-of-words, so overlapping words mean similarity."""

from __future__ import annotations

import hashlib
import math
import re
from collections.abc import Callable, Sequence

from app.errors import ProviderError


class FakeEmbedder:
    def __init__(self, dimension: int = 1024) -> None:
        self.dimension = dimension
        self.calls: list[list[str]] = []
        self.fail_with: Exception | None = None
        self.fail_after_batches: int | None = None
        self.batch_size = 64

    def _vector(self, text: str) -> list[float]:
        vec = [0.0] * self.dimension
        for word in re.findall(r"\w+", text.lower()):
            digest = hashlib.sha256(word.encode()).digest()
            vec[int.from_bytes(digest[:4], "big") % self.dimension] += 1.0
        norm = math.sqrt(sum(x * x for x in vec)) or 1.0
        return [x / norm for x in vec]

    def embed_documents(
        self, texts: Sequence[str], on_batch: Callable[[int, int], None] | None = None
    ) -> list[list[float]]:
        out: list[list[float]] = []
        for number, start in enumerate(range(0, len(texts), self.batch_size)):
            if self.fail_with is not None and (
                self.fail_after_batches is None or number >= self.fail_after_batches
            ):
                raise self.fail_with
            batch = list(texts[start : start + self.batch_size])
            self.calls.append(batch)
            out.extend(self._vector(t) for t in batch)
            if on_batch:
                on_batch(min(start + self.batch_size, len(texts)), len(texts))
        return out

    def embed_query(self, text: str) -> list[float]:
        if self.fail_with is not None:
            raise self.fail_with
        return self._vector(text)


def provider_down() -> ProviderError:
    return ProviderError(
        "voyage", "The embedding service is temporarily unavailable.", status_code=503
    )
