"""Health checks, request IDs, security headers, CORS, error format and static serving."""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

from app.config import Settings
from app.container import Services
from app.errors import AppError
from app.main import create_app
from fastapi import FastAPI
from fastapi.testclient import TestClient

from tests.fakes.supabase import FakeSupabase


# ----------------------------------------------------------------------------- health
def test_healthz_is_a_cheap_liveness_probe(client: TestClient, fake_db: FakeSupabase) -> None:
    fake_db.queries.clear()  # startup recovery already ran once
    fake_db.down = RuntimeError("database must not be touched by liveness")
    response = client.get("/healthz")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}
    assert fake_db.queries == []


def test_readyz_reports_ready_when_database_answers(client: TestClient) -> None:
    response = client.get("/readyz")
    assert response.status_code == 200
    assert response.json() == {"status": "ready", "checks": {"database": "ok"}}


def test_readyz_is_503_when_database_is_down(client: TestClient, fake_db: FakeSupabase) -> None:
    fake_db.fail(table="profiles", times=5)
    response = client.get("/readyz")
    assert response.status_code == 503
    body = response.json()
    assert body["error"]["code"] == "NOT_READY"
    assert "request_id" in body["error"]


# ----------------------------------------------------------------------- request ids
def test_every_response_carries_a_request_id(client: TestClient) -> None:
    first = client.get("/healthz").headers["X-Request-ID"]
    second = client.get("/healthz").headers["X-Request-ID"]
    assert len(first) >= 8
    assert first != second


def test_valid_incoming_request_id_is_kept_and_unsafe_ones_replaced(client: TestClient) -> None:
    kept = client.get("/healthz", headers={"X-Request-ID": "trace-abc12345"})
    assert kept.headers["X-Request-ID"] == "trace-abc12345"
    for unsafe in ("short", "has spaces in it!!", "x" * 200, "<script>alert(1)</script>"):
        replaced = client.get("/healthz", headers={"X-Request-ID": unsafe})
        assert replaced.headers["X-Request-ID"] != unsafe


# ----------------------------------------------------------------- security headers
def test_security_headers_are_set_on_every_response(client: TestClient, settings: Settings) -> None:
    for path in ("/healthz", "/", "/static/style.css", "/does-not-exist"):
        headers = client.get(path).headers
        assert headers["X-Content-Type-Options"] == "nosniff"
        assert headers["X-Frame-Options"] == "DENY"
        assert headers["Referrer-Policy"] == "strict-origin-when-cross-origin"
        assert "camera=()" in headers["Permissions-Policy"]
        csp = headers["Content-Security-Policy"]
        assert "default-src 'self'" in csp
        assert "frame-ancestors 'none'" in csp
        assert "script-src 'self'" in csp
        assert "'unsafe-inline'" not in csp
        assert "'unsafe-eval'" not in csp
        assert f"connect-src 'self' {settings.supabase_origin}" in csp


def test_hsts_only_in_production(
    make_settings: Callable[..., Settings], services: Services, client: TestClient
) -> None:
    assert "Strict-Transport-Security" not in client.get("/healthz").headers
    prod = make_settings(app_env="production", allowed_origins="https://app.example.com")
    with TestClient(create_app(prod, services)) as prod_client:
        assert "max-age=" in prod_client.get("/healthz").headers["Strict-Transport-Security"]


def test_api_responses_are_never_cached(client: TestClient) -> None:
    assert client.get("/api/does-not-exist").headers["Cache-Control"] == "no-store"


# ----------------------------------------------------------------------------- CORS
def test_cors_allows_only_configured_origins(client: TestClient) -> None:
    allowed = client.options(
        "/api/anything",
        headers={"Origin": "http://localhost:8000", "Access-Control-Request-Method": "POST"},
    )
    assert allowed.headers["access-control-allow-origin"] == "http://localhost:8000"
    assert "access-control-allow-credentials" not in allowed.headers

    denied = client.options(
        "/api/anything",
        headers={"Origin": "https://evil.example", "Access-Control-Request-Method": "POST"},
    )
    assert "access-control-allow-origin" not in denied.headers
    simple = client.get("/healthz", headers={"Origin": "https://evil.example"})
    assert "access-control-allow-origin" not in simple.headers


# ------------------------------------------------------------------------ error format
def test_unknown_route_uses_standard_error_shape(client: TestClient) -> None:
    response = client.get("/api/nope")
    assert response.status_code == 404
    error = response.json()["error"]
    assert error["code"] == "NOT_FOUND"
    assert error["request_id"] == response.headers["X-Request-ID"]
    assert set(error) == {"code", "message", "request_id"}


def test_unexpected_exceptions_return_500_without_a_stack_trace(
    settings: Settings, services: Services
) -> None:
    app = create_app(settings, services)

    @app.get("/boom")
    def boom() -> None:
        raise RuntimeError("secret internal detail: /srv/app/credentials.py")

    with TestClient(app, raise_server_exceptions=False) as test_client:
        response = test_client.get("/boom")
    assert response.status_code == 500
    assert response.json()["error"]["code"] == "INTERNAL_ERROR"
    assert "secret internal detail" not in response.text
    assert "Traceback" not in response.text
    assert response.headers["X-Request-ID"]
    assert response.headers["X-Content-Type-Options"] == "nosniff"


def test_app_errors_and_validation_errors_use_the_standard_shape(
    settings: Settings, services: Services
) -> None:
    app = create_app(settings, services)

    @app.get("/teapot")
    def teapot() -> None:
        raise AppError(418, "TEAPOT", "I am a teapot.")

    @app.get("/number/{value}")
    def number(value: int) -> dict[str, int]:
        return {"value": value}

    with TestClient(app) as test_client:
        teapot_response = test_client.get("/teapot")
        invalid = test_client.get("/number/not-a-number")
    assert teapot_response.status_code == 418
    assert teapot_response.json()["error"] == {
        "code": "TEAPOT",
        "message": "I am a teapot.",
        "request_id": teapot_response.headers["X-Request-ID"],
    }
    assert invalid.status_code == 422
    assert invalid.json()["error"]["code"] == "VALIDATION_ERROR"
    assert "not-a-number" not in invalid.text  # input values are never echoed


def test_interactive_docs_are_disabled_and_openapi_is_dev_only(
    client: TestClient, make_settings: Callable[..., Settings], services: Services
) -> None:
    assert client.get("/docs").status_code == 404
    assert client.get("/redoc").status_code == 404
    assert client.get("/openapi.json").status_code == 200
    prod = make_settings(app_env="production", allowed_origins="https://app.example.com")
    with TestClient(create_app(prod, services)) as prod_client:
        assert prod_client.get("/openapi.json").status_code == 404


# ------------------------------------------------------------------- pages and static
def test_pages_are_served(client: TestClient) -> None:
    for path in ("/", "/login", "/app"):
        response = client.get(path)
        assert response.status_code == 200
        assert response.headers["content-type"].startswith("text/html")
        assert response.headers["Cache-Control"] == "no-cache"


def test_static_assets_revalidate_and_traversal_is_blocked(client: TestClient) -> None:
    css = client.get("/static/style.css")
    assert css.status_code == 200
    assert css.headers["Cache-Control"] == "no-cache"
    assert client.get("/static/../app/config.py").status_code in {400, 404}
    assert client.get("/static/%2e%2e/app/config.py").status_code in {400, 404}
    assert client.get("/static/nope.js").status_code == 404


def test_public_client_config_contains_no_server_secrets(client: TestClient) -> None:
    response = client.get("/config.json")
    assert response.status_code == 200
    body = response.json()
    assert body["supabase_url"] == "https://example.supabase.co"
    assert body["supabase_anon_key"] == "anon-test-key"
    text = response.text
    for secret in ("service-test-key", "anthropic-test-key", "voyage-test-key"):
        assert secret not in text


def test_app_can_be_built_with_a_custom_static_directory(
    settings: Settings, services: Services, tmp_path: Path
) -> None:
    (tmp_path / "index.html").write_text("<h1>custom</h1>")
    with TestClient(create_app(settings, services, static_dir=tmp_path)) as test_client:
        assert "custom" in test_client.get("/").text


def test_app_factory_returns_independent_apps(settings: Settings, services: Services) -> None:
    first: FastAPI = create_app(settings, services)
    second: FastAPI = create_app(settings, services)
    assert first is not second
