"""In-process sliding-window rate limiter and client IP resolution.

State lives in the process, so limits are per instance. That is the documented trade-off for a
single-container deployment; behind several instances use a shared limiter (see README). The
daily question quota is different: it is atomic in the database (``consume_question``) and is
therefore exact across instances.
"""

from __future__ import annotations

import ipaddress
import math
import threading
import time
from collections import deque
from collections.abc import Callable

from starlette.requests import Request

from app.errors import RateLimitError


class RateLimiter:
    """Sliding-window log limiter keyed by (bucket, key). Thread-safe, memory-bounded."""

    def __init__(
        self,
        *,
        max_keys: int = 50_000,
        clock: Callable[[], float] = time.monotonic,
        sweep_every: int = 512,
    ) -> None:
        self._events: dict[tuple[str, str], tuple[float, deque[float]]] = {}
        self._lock = threading.Lock()
        self._max_keys = max_keys
        self._clock = clock
        self._sweep_every = sweep_every
        self._calls = 0

    def hit(self, bucket: str, key: str, limit: int, window: float) -> tuple[bool, int]:
        """Record an attempt. Returns (allowed, retry_after_seconds). Denied hits are not
        recorded, so a blocked client recovers as soon as its oldest event leaves the window."""
        now = self._clock()
        with self._lock:
            self._calls += 1
            if self._calls % self._sweep_every == 0:
                self._sweep(now)
            slot = (bucket, key)
            _, events = self._events.get(slot) or (window, deque())
            cutoff = now - window
            while events and events[0] <= cutoff:
                events.popleft()
            if len(events) >= limit:
                self._events[slot] = (window, events)
                return False, math.ceil(events[0] + window - now)
            events.append(now)
            self._events[slot] = (window, events)
            if len(self._events) > self._max_keys:
                self._sweep(now)
                self._evict_oldest()
            return True, 0

    def count(self, bucket: str, key: str, window: float) -> int:
        """Events recorded for (bucket, key) inside the window, without recording a new one."""
        now = self._clock()
        with self._lock:
            entry = self._events.get((bucket, key))
            if entry is None:
                return 0
            return sum(1 for ts in entry[1] if ts > now - window)

    def reset(self, bucket: str, key: str) -> None:
        with self._lock:
            self._events.pop((bucket, key), None)

    def check(
        self, bucket: str, key: str, limit: int, window: float, message: str | None = None
    ) -> None:
        allowed, retry_after = self.hit(bucket, key, limit, window)
        if not allowed:
            raise RateLimitError(retry_after, message)

    @property
    def size(self) -> int:
        return len(self._events)

    def _sweep(self, now: float) -> None:
        stale = [
            slot
            for slot, (window, events) in self._events.items()
            if not events or events[-1] <= now - window
        ]
        for slot in stale:
            del self._events[slot]

    def _evict_oldest(self) -> None:
        overflow = len(self._events) - self._max_keys
        if overflow > 0:
            for slot in list(self._events)[: overflow + self._max_keys // 10]:
                del self._events[slot]


def client_ip(request: Request, trusted_proxies: int) -> str:
    """Best-effort client IP.

    With ``trusted_proxies`` = N reverse proxies in front of the app, the real client is the
    N-th entry from the right of X-Forwarded-For (entries to its left are client-supplied and
    can be spoofed). With 0, forwarded headers are ignored entirely.
    """
    peer = request.client.host if request.client else "unknown"
    if trusted_proxies <= 0:
        return peer
    forwarded = [part.strip() for part in request.headers.get("x-forwarded-for", "").split(",")]
    forwarded = [part for part in forwarded if part]
    if len(forwarded) < trusted_proxies:
        return peer
    candidate = forwarded[-trusted_proxies]
    try:
        return str(ipaddress.ip_address(candidate))
    except ValueError:
        return peer
