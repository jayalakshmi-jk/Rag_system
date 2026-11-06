"""JWT verification and the CurrentUser dependency."""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import logging
import time
import uuid
from collections.abc import Callable
from types import SimpleNamespace
from typing import Any

import jwt
import pytest
from app.auth import CurrentUser
from app.config import Settings
from app.container import Services
from app.errors import AppError
from app.main import create_app
from app.tokens import AccountCache, SupabaseKeyResolver, TokenVerifier
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec, rsa
from fastapi import FastAPI
from fastapi.testclient import TestClient

from tests.conftest import ISSUER, JWT_SECRET, TestUser, make_token
from tests.fakes.supabase import FakeSupabase


def b64(data: dict[str, Any]) -> str:
    return base64.urlsafe_b64encode(json.dumps(data).encode()).rstrip(b"=").decode()


@pytest.fixture
def verifier(settings: Settings) -> TokenVerifier:
    return TokenVerifier(settings)


def reject(verifier: TokenVerifier, token: str) -> AppError:
    with pytest.raises(AppError) as caught:
        verifier.verify(token)
    return caught.value


# ------------------------------------------------------------------ happy path
def test_valid_token_yields_the_user_from_verified_claims(verifier: TokenVerifier) -> None:
    uid = str(uuid.uuid4())
    user = verifier.verify(make_token(uid, email="a@b.co"))
    assert user.id == uid
    assert user.email == "a@b.co"


def test_user_id_is_normalised_to_canonical_uuid(verifier: TokenVerifier) -> None:
    uid = uuid.uuid4()
    assert verifier.verify(make_token(str(uid).upper())).id == str(uid)


# ------------------------------------------------------------------ rejection matrix
def test_expired_token_has_its_own_code_so_the_client_can_refresh(verifier: TokenVerifier) -> None:
    err = reject(verifier, make_token(expires_in=-120))
    assert (err.status_code, err.code) == (401, "TOKEN_EXPIRED")


def test_small_clock_skew_is_tolerated(verifier: TokenVerifier) -> None:
    assert verifier.verify(make_token(expires_in=-3)).id


@pytest.mark.parametrize(
    "token",
    [
        pytest.param(make_token(secret="x" * 48), id="wrong-signature"),
        pytest.param(make_token(aud="anon"), id="wrong-audience"),
        pytest.param(make_token(aud=None), id="missing-audience"),
        pytest.param(make_token(iss="https://evil.example/auth/v1"), id="wrong-issuer"),
        pytest.param(make_token(iss=None), id="missing-issuer"),
        pytest.param(make_token(exp=None), id="missing-expiry"),
        pytest.param(make_token(sub="not-a-uuid"), id="non-uuid-subject"),
        pytest.param(make_token(role="service_role"), id="service-role"),
        pytest.param(make_token(is_anonymous=True), id="anonymous-user"),
        pytest.param("", id="empty"),
        pytest.param("garbage", id="not-a-jwt"),
        pytest.param("a.b.c", id="malformed-parts"),
        pytest.param("x" * 9000, id="oversized"),
    ],
)
def test_invalid_tokens_are_rejected_with_401(verifier: TokenVerifier, token: str) -> None:
    err = reject(verifier, token)
    assert (err.status_code, err.code) == (401, "INVALID_TOKEN")
    if token:
        assert token[:20] not in err.message


def test_missing_subject_is_rejected(verifier: TokenVerifier) -> None:
    now = int(time.time())
    token = jwt.encode(
        {"aud": "authenticated", "iss": ISSUER, "exp": now + 60}, JWT_SECRET, algorithm="HS256"
    )
    assert reject(verifier, token).code == "INVALID_TOKEN"


def test_alg_none_is_rejected_even_with_a_valid_looking_payload(verifier: TokenVerifier) -> None:
    payload = {
        "sub": str(uuid.uuid4()),
        "aud": "authenticated",
        "iss": ISSUER,
        "exp": int(time.time()) + 3600,
    }
    for alg in ("none", "None", "NONE", "nOnE"):
        forged = f"{b64({'alg': alg, 'typ': 'JWT'})}.{b64(payload)}."
        assert reject(verifier, forged).code == "INVALID_TOKEN"


def test_unsupported_algorithms_are_rejected(verifier: TokenVerifier) -> None:
    for alg in ("HS384", "HS512", "PS256", "ES512", "RS512"):
        token = f"{b64({'alg': alg, 'typ': 'JWT'})}.{b64({'sub': 'x'})}.c2ln"
        assert reject(verifier, token).code == "INVALID_TOKEN"


def test_tampered_payload_is_rejected(verifier: TokenVerifier) -> None:
    token = make_token(str(uuid.uuid4()))
    head, _, sig = token.split(".")
    tampered = {
        "sub": str(uuid.uuid4()),
        "aud": "authenticated",
        "iss": ISSUER,
        "exp": 9999999999,
    }
    assert reject(verifier, f"{head}.{b64(tampered)}.{sig}").code == "INVALID_TOKEN"


def test_hs256_is_refused_when_no_secret_is_configured(
    make_settings: Callable[..., Settings],
) -> None:
    no_secret = TokenVerifier(make_settings(supabase_jwt_secret=""))
    assert reject(no_secret, make_token()).code == "INVALID_TOKEN"


# ------------------------------------------------------- asymmetric keys / confusion
class FixedKeyResolver:
    """Stands in for the JWKS client: serves one public key for asymmetric algorithms."""

    def __init__(self, public_key: Any, secret: str | None = None) -> None:
        self.public_key, self.secret = public_key, secret

    def key_for(self, token: str, algorithm: str) -> Any:
        if algorithm == "HS256":
            if self.secret is None:
                raise jwt.InvalidTokenError("no secret")
            return self.secret
        return self.public_key


def claims() -> dict[str, Any]:
    return {
        "sub": str(uuid.uuid4()),
        "aud": "authenticated",
        "iss": ISSUER,
        "role": "authenticated",
        "exp": int(time.time()) + 3600,
    }


def test_es256_tokens_verify_against_the_published_public_key(settings: Settings) -> None:
    private = ec.generate_private_key(ec.SECP256R1())
    verifier = TokenVerifier(settings, FixedKeyResolver(private.public_key()))
    token = jwt.encode(claims(), private, algorithm="ES256", headers={"kid": "k1"})
    assert verifier.verify(token).id


def test_es256_token_signed_by_another_key_is_rejected(settings: Settings) -> None:
    trusted = ec.generate_private_key(ec.SECP256R1())
    attacker = ec.generate_private_key(ec.SECP256R1())
    verifier = TokenVerifier(settings, FixedKeyResolver(trusted.public_key()))
    token = jwt.encode(claims(), attacker, algorithm="ES256")
    assert reject(verifier, token).code == "INVALID_TOKEN"


def test_rs256_token_cannot_be_verified_with_an_ec_key(settings: Settings) -> None:
    """Regression: PyJWT raises TypeError for a key of the wrong type; that must be a 401."""
    ec_key = ec.generate_private_key(ec.SECP256R1())
    rsa_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    verifier = TokenVerifier(settings, FixedKeyResolver(ec_key.public_key()))
    token = jwt.encode(claims(), rsa_key, algorithm="RS256")
    assert reject(verifier, token).code == "INVALID_TOKEN"


def test_public_key_cannot_be_abused_as_an_hmac_secret(settings: Settings) -> None:
    """Classic algorithm-confusion attack: sign HS256 using the RSA public key as the secret."""
    rsa_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    public_pem = rsa_key.public_key().public_bytes(
        serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo
    )
    verifier = TokenVerifier(settings, FixedKeyResolver(rsa_key.public_key(), secret=JWT_SECRET))
    forged = b64({"alg": "HS256", "typ": "JWT"}) + "." + b64(claims())
    digest = hmac.new(public_pem, forged.encode(), hashlib.sha256).digest()
    signature = base64.urlsafe_b64encode(digest).rstrip(b"=").decode()
    assert reject(verifier, f"{forged}.{signature}").code == "INVALID_TOKEN"


def test_jwks_key_type_must_match_the_token_algorithm(settings: Settings) -> None:
    """A token claiming RS256 must not be checked against a published ES256 key."""
    resolver = SupabaseKeyResolver(settings)
    published = SimpleNamespace(algorithm_name="ES256", key="the-ec-key")
    resolver._jwks = SimpleNamespace(get_signing_key_from_jwt=lambda _t: published)  # type: ignore[assignment]
    with pytest.raises(jwt.InvalidTokenError):
        resolver.key_for("t", "RS256")
    assert resolver.key_for("t", "ES256") == "the-ec-key"
    with pytest.raises(jwt.InvalidTokenError):
        resolver.key_for("t", "HS384")


def test_jwks_outage_is_a_503_not_a_401(settings: Settings) -> None:
    class DownResolver:
        def key_for(self, token: str, algorithm: str) -> Any:
            raise jwt.PyJWKClientConnectionError("jwks endpoint unreachable")

    err = reject(TokenVerifier(settings, DownResolver()), make_token())
    assert (err.status_code, err.code) == (503, "AUTH_UNAVAILABLE")


def test_unknown_key_id_is_a_401(settings: Settings) -> None:
    class UnknownKid:
        def key_for(self, token: str, algorithm: str) -> Any:
            raise jwt.PyJWKClientError("kid not found")

    assert reject(TokenVerifier(settings, UnknownKid()), make_token()).code == "INVALID_TOKEN"


def test_custom_issuer_can_be_configured(make_settings: Callable[..., Settings]) -> None:
    custom = TokenVerifier(make_settings(supabase_jwt_issuer="https://auth.example.com/auth/v1/"))
    assert custom.verify(make_token(iss="https://auth.example.com/auth/v1")).id
    assert reject(custom, make_token()).code == "INVALID_TOKEN"


# ----------------------------------------------------------------- the dependency
@pytest.fixture
def protected(settings: Settings, services: Services) -> TestClient:
    app: FastAPI = create_app(settings, services)

    @app.get("/api/whoami")
    def whoami(user: CurrentUser) -> dict[str, str | None]:
        return {"id": user.id, "email": user.email}

    return TestClient(app)


def whoami_app(settings: Settings, services: Services) -> FastAPI:
    app = create_app(settings, services)

    @app.get("/api/whoami")
    def whoami(u: CurrentUser) -> dict[str, str]:
        return {"id": u.id}

    return app


def test_protected_route_requires_a_bearer_token(protected: TestClient) -> None:
    response = protected.get("/api/whoami")
    assert response.status_code == 401
    assert response.headers["www-authenticate"] == "Bearer"
    assert response.json()["error"]["code"] == "UNAUTHORIZED"


@pytest.mark.parametrize(
    "header",
    ["Basic abc", "Bearer", "Bearer ", "Bearer a b", "bearer", "Token abc", "abc"],
)
def test_malformed_authorization_headers_are_401(protected: TestClient, header: str) -> None:
    assert protected.get("/api/whoami", headers={"Authorization": header}).status_code == 401


def test_valid_token_for_an_existing_account_is_accepted(
    protected: TestClient, user: TestUser
) -> None:
    response = protected.get("/api/whoami", headers=user.headers)
    assert response.status_code == 200
    assert response.json()["id"] == user.id


def test_scheme_is_case_insensitive(protected: TestClient, user: TestUser) -> None:
    token = user.headers["Authorization"].split(" ", 1)[1]
    response = protected.get("/api/whoami", headers={"Authorization": f"bearer {token}"})
    assert response.status_code == 200


def test_validly_signed_token_for_a_deleted_account_is_refused(protected: TestClient) -> None:
    ghost = make_token(str(uuid.uuid4()))
    response = protected.get("/api/whoami", headers={"Authorization": f"Bearer {ghost}"})
    assert response.status_code == 401
    assert response.json()["error"]["code"] == "ACCOUNT_NOT_FOUND"


def test_account_existence_is_cached_to_spare_the_database(
    protected: TestClient, user: TestUser, fake_db: FakeSupabase
) -> None:
    protected.get("/api/whoami", headers=user.headers)
    before = sum(1 for q in fake_db.queries if q[0] == "profiles")
    for _ in range(5):
        protected.get("/api/whoami", headers=user.headers)
    assert sum(1 for q in fake_db.queries if q[0] == "profiles") == before


def test_account_cache_expires() -> None:
    now = [0.0]
    cache = AccountCache(60, clock=lambda: now[0])
    cache.remember("u")
    assert cache.is_fresh("u")
    now[0] = 61
    assert not cache.is_fresh("u")
    cache.remember("u")
    cache.forget("u")
    assert not cache.is_fresh("u")


def test_database_outage_during_account_check_is_503(
    protected: TestClient, user: TestUser, fake_db: FakeSupabase
) -> None:
    fake_db.fail(table="profiles", times=10)
    response = protected.get("/api/whoami", headers=user.headers)
    assert response.status_code == 503
    assert response.json()["error"]["code"] == "DATABASE_UNAVAILABLE"


def test_repeated_bad_tokens_get_rate_limited_per_ip(
    make_settings: Callable[..., Settings], services: Services
) -> None:
    app = whoami_app(make_settings(rate_limit_auth_failures_per_minute=3), services)
    with TestClient(app) as http:
        bad = {"Authorization": "Bearer nope"}
        statuses = [http.get("/api/whoami", headers=bad).status_code for _ in range(5)]
    assert statuses[:3] == [401, 401, 401]
    assert statuses[3:] == [429, 429]


def test_failed_attempts_do_not_block_valid_users_on_other_ips(
    make_settings: Callable[..., Settings], services: Services, user: TestUser
) -> None:
    settings = make_settings(rate_limit_auth_failures_per_minute=2, trusted_proxy_count=1)
    with TestClient(whoami_app(settings, services)) as http:
        for _ in range(3):
            http.get(
                "/api/whoami", headers={"Authorization": "Bearer bad", "X-Forwarded-For": "9.9.9.9"}
            )
        blocked = http.get("/api/whoami", headers={**user.headers, "X-Forwarded-For": "9.9.9.9"})
        other = http.get("/api/whoami", headers={**user.headers, "X-Forwarded-For": "8.8.8.8"})
    assert blocked.status_code == 429
    assert other.status_code == 200


def test_per_user_rate_limit(
    make_settings: Callable[..., Settings], services: Services, user: TestUser
) -> None:
    app = whoami_app(make_settings(rate_limit_user_per_minute=3), services)
    with TestClient(app) as http:
        codes = [http.get("/api/whoami", headers=user.headers).status_code for _ in range(5)]
    assert codes == [200, 200, 200, 429, 429]


def test_per_ip_rate_limit_covers_unauthenticated_api_calls(
    make_settings: Callable[..., Settings], services: Services
) -> None:
    app = create_app(make_settings(rate_limit_ip_per_minute=2), services)
    with TestClient(app) as http:
        codes = [http.get("/api/me").status_code for _ in range(4)]
        health = http.get("/healthz").status_code
        page = http.get("/").status_code
    assert codes == [401, 401, 429, 429]
    assert health == 200  # probes are never rate limited
    assert page == 200  # neither are static pages


def test_rate_limit_response_has_retry_after_and_standard_error(
    make_settings: Callable[..., Settings], services: Services
) -> None:
    app = create_app(make_settings(rate_limit_ip_per_minute=1), services)
    with TestClient(app) as http:
        http.get("/api/me")
        limited = http.get("/api/me")
    assert limited.status_code == 429
    assert 1 <= int(limited.headers["Retry-After"]) <= 60
    assert limited.json()["error"]["code"] == "RATE_LIMITED"


def test_authenticated_request_is_logged_with_the_user_id(
    protected: TestClient, user: TestUser, caplog: pytest.LogCaptureFixture
) -> None:
    with caplog.at_level(logging.INFO, logger="app.request"):
        protected.get("/api/whoami", headers=user.headers)
    fields = [getattr(r, "fields", {}) for r in caplog.records if r.name == "app.request"]
    assert any(f.get("user_id") == user.id and f.get("status") == 200 for f in fields)
    token = user.headers["Authorization"].split(" ", 1)[1]
    assert not any(token in str(f) for f in fields)
