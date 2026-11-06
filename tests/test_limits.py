from __future__ import annotations

from typing import Any

import pytest
from app.errors import RateLimitError
from app.services.limits import RateLimiter, client_ip
from starlette.requests import Request


class Clock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


def test_allows_up_to_the_limit_then_blocks_and_recovers() -> None:
    clock = Clock()
    limiter = RateLimiter(clock=clock)
    assert [limiter.hit("b", "k", 3, 60)[0] for _ in range(3)] == [True, True, True]
    allowed, retry_after = limiter.hit("b", "k", 3, 60)
    assert not allowed
    assert retry_after == 60
    clock.now += 59
    assert limiter.hit("b", "k", 3, 60) == (False, 1)
    clock.now += 1.1
    assert limiter.hit("b", "k", 3, 60)[0]


def test_keys_and_buckets_are_independent() -> None:
    limiter = RateLimiter()
    assert limiter.hit("chat", "alice", 1, 60)[0]
    assert not limiter.hit("chat", "alice", 1, 60)[0]
    assert limiter.hit("chat", "bob", 1, 60)[0]
    assert limiter.hit("upload", "alice", 1, 60)[0]


def test_denied_hits_are_not_recorded() -> None:
    clock = Clock()
    limiter = RateLimiter(clock=clock)
    limiter.hit("b", "k", 1, 10)
    for _ in range(50):
        limiter.hit("b", "k", 1, 10)
    clock.now += 10.5
    assert limiter.hit("b", "k", 1, 10)[0]


def test_check_raises_a_429_with_retry_after() -> None:
    limiter = RateLimiter()
    limiter.check("b", "k", 1, 30)
    with pytest.raises(RateLimitError) as caught:
        limiter.check("b", "k", 1, 30)
    assert caught.value.status_code == 429
    assert caught.value.headers["Retry-After"] == "30"


def test_count_and_reset() -> None:
    clock = Clock()
    limiter = RateLimiter(clock=clock)
    for _ in range(3):
        limiter.hit("b", "k", 10, 60)
    assert limiter.count("b", "k", 60) == 3
    clock.now += 61
    assert limiter.count("b", "k", 60) == 0
    limiter.hit("b", "k", 10, 60)
    limiter.reset("b", "k")
    assert limiter.count("b", "k", 60) == 0


def test_memory_stays_bounded_under_a_flood_of_distinct_keys() -> None:
    clock = Clock()
    limiter = RateLimiter(max_keys=1000, clock=clock, sweep_every=100)
    for i in range(20_000):
        limiter.hit("ip", f"10.0.{i // 256}.{i % 256}", 5, 60)
        if i % 500 == 0:
            clock.now += 30
    assert limiter.size <= 1100


def test_stale_keys_are_swept() -> None:
    clock = Clock()
    limiter = RateLimiter(clock=clock, sweep_every=10)
    for i in range(50):
        limiter.hit("ip", f"k{i}", 5, 10)
    clock.now += 100
    for _ in range(20):
        limiter.hit("ip", "fresh", 100, 10)
    assert limiter.size < 10


def request_with(headers: dict[str, str], peer: str | None = "203.0.113.5") -> Request:
    scope: dict[str, Any] = {
        "type": "http",
        "headers": [(k.lower().encode(), v.encode()) for k, v in headers.items()],
        "client": (peer, 1234) if peer else None,
    }
    return Request(scope)


def test_forwarded_headers_are_ignored_without_trusted_proxies() -> None:
    request = request_with({"X-Forwarded-For": "1.2.3.4"})
    assert client_ip(request, 0) == "203.0.113.5"


def test_with_one_trusted_proxy_the_rightmost_entry_is_the_client() -> None:
    request = request_with({"X-Forwarded-For": "6.6.6.6, 198.51.100.7"})
    assert client_ip(request, 1) == "198.51.100.7"  # left entries are attacker-controlled


def test_with_two_trusted_proxies_the_second_from_right_is_the_client() -> None:
    request = request_with({"X-Forwarded-For": "6.6.6.6, 198.51.100.7, 10.0.0.2"})
    assert client_ip(request, 2) == "198.51.100.7"


def test_short_or_invalid_forwarded_headers_fall_back_to_the_peer() -> None:
    assert client_ip(request_with({}), 1) == "203.0.113.5"
    assert client_ip(request_with({"X-Forwarded-For": "not-an-ip"}), 1) == "203.0.113.5"
    assert client_ip(request_with({"X-Forwarded-For": "1.1.1.1"}), 2) == "203.0.113.5"
    assert client_ip(request_with({}, peer=None), 0) == "unknown"


def test_ipv6_is_normalised() -> None:
    request = request_with({"X-Forwarded-For": "2001:DB8:0:0:0:0:0:1"})
    assert client_ip(request, 1) == "2001:db8::1"
