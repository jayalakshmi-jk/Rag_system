"""Embedding provider behind a small interface. Voyage is the production implementation."""

from __future__ import annotations

import logging
import random
import time
from collections.abc import Callable, Sequence
from typing import Any, Protocol

import httpx

from app.config import Settings
from app.errors import ProviderError
from app.logging import log_event

logger = logging.getLogger(__name__)

VOYAGE_URL = "https://api.voyageai.com/v1/embeddings"
_RETRYABLE = {408, 409, 425, 429, 500, 502, 503, 504, 529}
_JITTER = random.SystemRandom()


class Embedder(Protocol):
    """Swap providers by implementing this; dimension must match the ``vector(N)`` column."""

    dimension: int

    def embed_documents(
        self, texts: Sequence[str], on_batch: Callable[[int, int], None] | None = None
    ) -> list[list[float]]: ...

    def embed_query(self, text: str) -> list[float]: ...


class VoyageEmbedder:
    def __init__(
        self,
        settings: Settings,
        client: httpx.Client | None = None,
        sleep: Callable[[float], None] = time.sleep,
        batch_size: int = 64,
    ) -> None:
        self.dimension = settings.embed_dim
        self._model = settings.embed_model
        self._key = settings.voyage_api_key.get_secret_value()
        self._attempts = settings.provider_max_attempts
        self._client = client or httpx.Client(timeout=settings.http_timeout_seconds)
        self._sleep = sleep
        self._batch = batch_size

    # ------------------------------------------------------------------ public
    def embed_documents(
        self, texts: Sequence[str], on_batch: Callable[[int, int], None] | None = None
    ) -> list[list[float]]:
        out: list[list[float]] = []
        total = len(texts)
        for start in range(0, total, self._batch):
            out.extend(self._request(list(texts[start : start + self._batch]), "document"))
            if on_batch:
                on_batch(min(start + self._batch, total), total)
        return out

    def embed_query(self, text: str) -> list[float]:
        return self._request([text], "query")[0]

    # ----------------------------------------------------------------- internal
    def _request(self, texts: list[str], input_type: str) -> list[list[float]]:
        body: dict[str, Any] = {
            "input": texts,
            "model": self._model,
            "input_type": input_type,
            "output_dimension": self.dimension,
        }
        headers = {"Authorization": f"Bearer {self._key}", "Content-Type": "application/json"}
        last_status: int | None = None
        for attempt in range(1, self._attempts + 1):
            try:
                response = self._client.post(VOYAGE_URL, json=body, headers=headers)
            except httpx.TransportError as exc:
                last_status = None
                log_event(logger, "embed_retry", attempt=attempt, error=type(exc).__name__)
            else:
                if response.status_code == 200:
                    return self._parse(response, len(texts))
                last_status = response.status_code
                if last_status not in _RETRYABLE:
                    log_event(logger, "embed_rejected", status=last_status)
                    raise ProviderError(
                        "voyage", "The embedding service rejected the request.", status_code=502
                    )
                log_event(logger, "embed_retry", attempt=attempt, status=last_status)
                retry_after = _retry_after(response)
                if attempt < self._attempts:
                    self._sleep(retry_after if retry_after is not None else _backoff(attempt))
                    continue
            if attempt < self._attempts:
                self._sleep(_backoff(attempt))
        raise ProviderError(
            "voyage",
            "The embedding service is temporarily unavailable. Please try again.",
            status_code=503 if last_status in {429, 503, 529} or last_status is None else 502,
        )

    def _parse(self, response: httpx.Response, expected: int) -> list[list[float]]:
        try:
            items = sorted(response.json()["data"], key=lambda item: item["index"])
            vectors = [[float(x) for x in item["embedding"]] for item in items]
        except (ValueError, KeyError, TypeError) as exc:
            raise ProviderError(
                "voyage", "The embedding service returned an invalid response."
            ) from exc
        if len(vectors) != expected or any(len(v) != self.dimension for v in vectors):
            raise ProviderError("voyage", "The embedding service returned unexpected vectors.")
        return vectors


def _backoff(attempt: int) -> float:
    return float(min(2 ** (attempt - 1), 8) + _JITTER.uniform(0, 0.5))


def _retry_after(response: httpx.Response) -> float | None:
    try:
        return min(float(response.headers["retry-after"]), 30.0)
    except (KeyError, ValueError):
        return None
