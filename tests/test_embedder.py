from __future__ import annotations

import json
from collections.abc import Callable
from typing import Any

import httpx
import pytest
from app.config import Settings
from app.errors import ProviderError
from app.services.embedder import VoyageEmbedder


def vec(seed: float, dim: int = 1024) -> list[float]:
    return [seed] * dim


def make(
    settings: Settings, handler: Callable[[httpx.Request], httpx.Response], **kw: Any
) -> tuple[VoyageEmbedder, list[float]]:
    sleeps: list[float] = []
    client = httpx.Client(transport=httpx.MockTransport(handler))
    return VoyageEmbedder(settings, client=client, sleep=sleeps.append, **kw), sleeps


def ok(request: httpx.Request) -> httpx.Response:
    body = json.loads(request.content)
    data = [{"index": i, "embedding": vec(float(i))} for i, _ in enumerate(body["input"])]
    return httpx.Response(200, json={"data": data[::-1], "usage": {"total_tokens": 5}})


def test_request_shape_and_auth(settings: Settings) -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return ok(request)

    embedder, _ = make(settings, handler)
    embedder.embed_query("hello")
    request = seen[0]
    body = json.loads(request.content)
    assert request.url == "https://api.voyageai.com/v1/embeddings"
    assert request.headers["authorization"] == "Bearer voyage-test-key"
    assert body == {
        "input": ["hello"],
        "model": settings.embed_model,
        "input_type": "query",
        "output_dimension": settings.embed_dim,
    }


def test_documents_are_batched_ordered_and_report_progress(settings: Settings) -> None:
    sizes: list[int] = []

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        assert body["input_type"] == "document"
        sizes.append(len(body["input"]))
        return ok(request)

    embedder, _ = make(settings, handler, batch_size=3)
    progress: list[tuple[int, int]] = []
    vectors = embedder.embed_documents(
        [f"t{i}" for i in range(7)], lambda d, t: progress.append((d, t))
    )
    assert sizes == [3, 3, 1]
    assert progress == [(3, 7), (6, 7), (7, 7)]
    assert [v[0] for v in vectors] == [0.0, 1.0, 2.0, 0.0, 1.0, 2.0, 0.0]  # re-sorted by index


def test_transient_errors_are_retried_with_backoff(settings: Settings) -> None:
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        if calls["n"] == 1:
            return httpx.Response(429, headers={"retry-after": "2"})
        if calls["n"] == 2:
            raise httpx.ConnectTimeout("slow")
        if calls["n"] == 3:
            return httpx.Response(503)
        return ok(request)

    embedder, sleeps = make(settings, handler)
    assert len(embedder.embed_query("x")) == settings.embed_dim
    assert calls["n"] == 4
    assert sleeps[0] == 2.0
    assert len(sleeps) == 3


def test_gives_up_after_max_attempts_with_a_safe_message(settings: Settings) -> None:
    embedder, _ = make(settings, lambda r: httpx.Response(503, text="internal secret detail"))
    with pytest.raises(ProviderError) as caught:
        embedder.embed_query("x")
    assert caught.value.status_code == 502 or caught.value.status_code == 503
    assert "secret" not in caught.value.message
    assert "voyage-test-key" not in caught.value.message


def test_non_retryable_errors_fail_immediately(settings: Settings) -> None:
    calls = {"n": 0}

    def handler(_: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        return httpx.Response(401, json={"detail": "bad key"})

    embedder, sleeps = make(settings, handler)
    with pytest.raises(ProviderError):
        embedder.embed_query("x")
    assert calls["n"] == 1
    assert sleeps == []


@pytest.mark.parametrize(
    "payload",
    [
        {"nope": 1},
        {"data": [{"index": 0}]},
        {"data": [{"index": 0, "embedding": [0.1, 0.2]}]},  # wrong dimension
        {"data": []},  # wrong count
    ],
)
def test_malformed_responses_are_provider_errors(
    settings: Settings, payload: dict[str, Any]
) -> None:
    embedder, _ = make(settings, lambda r: httpx.Response(200, json=payload))
    with pytest.raises(ProviderError):
        embedder.embed_query("x")


def test_non_json_response_is_a_provider_error(settings: Settings) -> None:
    embedder, _ = make(settings, lambda r: httpx.Response(200, text="<html>"))
    with pytest.raises(ProviderError):
        embedder.embed_query("x")
