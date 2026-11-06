"""Cryptographic verification of Supabase access tokens (no FastAPI dependencies here).

The token is verified (signature, expiry, audience, issuer, required claims) before any claim is
trusted; the user id is taken only from the verified ``sub`` claim. Payloads are never merely
decoded.
"""

from __future__ import annotations

import logging
import threading
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, Protocol

import jwt
from jwt import PyJWKClient
from jwt.exceptions import (
    ExpiredSignatureError,
    InvalidAudienceError,
    InvalidIssuerError,
    InvalidTokenError,
    PyJWKClientConnectionError,
    PyJWKClientError,
)

from app.config import Settings
from app.errors import AppError, AuthError
from app.logging import log_event

logger = logging.getLogger(__name__)

AUDIENCE = "authenticated"
ASYMMETRIC_ALGORITHMS = frozenset({"ES256", "RS256", "EdDSA"})
MAX_TOKEN_LENGTH = 8192
LEEWAY_SECONDS = 10
_REQUIRED_CLAIMS = ["exp", "sub", "aud", "iss"]


@dataclass(frozen=True)
class CurrentUserData:
    id: str
    email: str | None = None


class KeyResolver(Protocol):
    """Finds the verification key for a token header. Never looks at the payload."""

    def key_for(self, token: str, algorithm: str) -> Any: ...


class SupabaseKeyResolver:
    """HS256 uses the configured JWT secret; asymmetric algorithms use the project's JWKS."""

    def __init__(self, settings: Settings) -> None:
        secret = settings.supabase_jwt_secret
        self._secret = secret.get_secret_value() if secret else None
        self._jwks = PyJWKClient(
            settings.jwks_url,
            cache_keys=True,
            lifespan=600,
            timeout=settings.http_timeout_seconds,
            headers={"apikey": settings.supabase_anon_key.get_secret_value()},
        )

    def key_for(self, token: str, algorithm: str) -> Any:
        if algorithm == "HS256":
            if not self._secret:
                raise InvalidTokenError("HS256 tokens are not accepted by this deployment")
            return self._secret
        if algorithm in ASYMMETRIC_ALGORITHMS:
            signing_key = self._jwks.get_signing_key_from_jwt(token)
            # The published key decides which algorithm it may be used with, never the token.
            if signing_key.algorithm_name != algorithm:
                raise InvalidTokenError("token algorithm does not match the signing key")
            return signing_key.key
        raise InvalidTokenError("unsupported signing algorithm")


class TokenVerifier:
    def __init__(self, settings: Settings, resolver: KeyResolver | None = None) -> None:
        self._issuer = settings.jwt_issuer
        self._resolver = resolver or SupabaseKeyResolver(settings)

    def verify(self, token: str) -> CurrentUserData:
        if not token or len(token) > MAX_TOKEN_LENGTH:
            raise AuthError("Invalid access token.", "INVALID_TOKEN")
        try:
            header = jwt.get_unverified_header(token)
            algorithm = header.get("alg")
            if not isinstance(algorithm, str) or algorithm.lower() == "none":
                raise InvalidTokenError("missing or unsafe algorithm")
            key = self._resolver.key_for(token, algorithm)
            claims = jwt.decode(
                token,
                key,
                algorithms=[algorithm],
                audience=AUDIENCE,
                issuer=self._issuer,
                leeway=LEEWAY_SECONDS,
                options={"require": _REQUIRED_CLAIMS},
            )
        except ExpiredSignatureError:
            raise AuthError(
                "Your session has expired. Please sign in again.", "TOKEN_EXPIRED"
            ) from None
        except PyJWKClientConnectionError:
            log_event(logger, "jwks_unreachable", level=logging.ERROR)
            raise AppError(
                503, "AUTH_UNAVAILABLE", "Sign-in verification is temporarily unavailable."
            ) from None
        except (
            InvalidAudienceError,
            InvalidIssuerError,
            PyJWKClientError,
            InvalidTokenError,
            TypeError,  # e.g. a key of the wrong type for the header's algorithm
            ValueError,
        ):
            raise AuthError("Invalid access token.", "INVALID_TOKEN") from None
        return self._user_from_claims(claims)

    @staticmethod
    def _user_from_claims(claims: dict[str, Any]) -> CurrentUserData:
        try:
            user_id = str(uuid.UUID(str(claims["sub"])))
        except ValueError:
            raise AuthError("Invalid access token.", "INVALID_TOKEN") from None
        # Defence in depth: an anonymous or service-role session is never a product user.
        if claims.get("role", "authenticated") != "authenticated" or claims.get("is_anonymous"):
            raise AuthError("Invalid access token.", "INVALID_TOKEN")
        email = claims.get("email")
        return CurrentUserData(id=user_id, email=email if isinstance(email, str) else None)


class AccountCache:
    """Remembers for a short time that an account exists, so deleted accounts lose access quickly
    without a database round trip on every request."""

    def __init__(
        self,
        ttl_seconds: float,
        clock: Callable[[], float] = time.monotonic,
        max_size: int = 20_000,
    ) -> None:
        self._ttl = ttl_seconds
        self._clock = clock
        self._max = max_size
        self._seen: dict[str, float] = {}
        self._lock = threading.Lock()

    def is_fresh(self, user_id: str) -> bool:
        with self._lock:
            expires = self._seen.get(user_id)
            return expires is not None and expires > self._clock()

    def remember(self, user_id: str) -> None:
        if self._ttl <= 0:
            return
        with self._lock:
            if len(self._seen) >= self._max:
                now = self._clock()
                self._seen = {k: v for k, v in self._seen.items() if v > now}
                if len(self._seen) >= self._max:
                    self._seen.clear()
            self._seen[user_id] = self._clock() + self._ttl

    def forget(self, user_id: str) -> None:
        with self._lock:
            self._seen.pop(user_id, None)
