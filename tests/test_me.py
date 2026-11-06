"""GET /api/me and DELETE /api/me (account deletion)."""

from __future__ import annotations

import uuid
from collections.abc import Callable

import pytest
from app.config import Settings
from app.container import Services
from app.main import create_app
from fastapi.testclient import TestClient
from supabase_auth.errors import AuthApiError

from tests.conftest import TestUser, make_token
from tests.fakes.supabase import FakeSupabase

CONFIRM = {"confirm": "DELETE"}


def test_me_requires_authentication(client: TestClient) -> None:
    assert client.get("/api/me").status_code == 401
    assert client.request("DELETE", "/api/me", json=CONFIRM).status_code == 401


def test_me_returns_profile_quota_and_limits(
    client: TestClient, user: TestUser, settings: Settings
) -> None:
    body = client.get("/api/me", headers=user.headers).json()
    assert body["id"] == user.id
    assert body["email"] == "student@example.com"
    assert body["name"] == "Test User"
    assert body["quota"] == {
        "daily_limit": settings.daily_question_limit,
        "used_today": 0,
        "remaining": settings.daily_question_limit,
    }
    assert body["limits"] == {
        "max_upload_mb": settings.max_upload_mb,
        "max_question_length": settings.max_question_length,
    }
    assert "service" not in str(body).lower()


def test_me_reflects_questions_used_today(
    client: TestClient, user: TestUser, fake_db: FakeSupabase, settings: Settings
) -> None:
    fake_db.rpc("consume_question", {"p_user": user.id, "p_limit": 50}).execute()
    fake_db.rpc("consume_question", {"p_user": user.id, "p_limit": 50}).execute()
    quota = client.get("/api/me", headers=user.headers).json()["quota"]
    assert quota["used_today"] == 2
    assert quota["remaining"] == settings.daily_question_limit - 2


def test_each_user_only_sees_their_own_usage(
    client: TestClient, new_user: Callable[..., TestUser], fake_db: FakeSupabase
) -> None:
    alice, bob = new_user(name="Alice"), new_user(name="Bob")
    fake_db.rpc("consume_question", {"p_user": alice.id, "p_limit": 50}).execute()
    assert client.get("/api/me", headers=alice.headers).json()["quota"]["used_today"] == 1
    bobs = client.get("/api/me", headers=bob.headers).json()
    assert bobs["quota"]["used_today"] == 0
    assert bobs["name"] == "Bob"
    assert bobs["id"] == bob.id


def test_user_id_cannot_be_chosen_by_the_client(
    client: TestClient, new_user: Callable[..., TestUser]
) -> None:
    alice, bob = new_user(name="Alice"), new_user(name="Bob")
    response = client.get(
        f"/api/me?user_id={bob.id}", headers={**alice.headers, "X-User-Id": bob.id}
    )
    assert response.json()["id"] == alice.id
    assert response.json()["name"] == "Alice"


# -------------------------------------------------------------------- deletion
def seed_user_data(fake_db: FakeSupabase, user_id: str) -> None:
    doc = (
        fake_db.table("documents").insert({"user_id": user_id, "title": "Notes"}).execute().data[0]
    )
    fake_db.table("chunks").insert(
        {"document_id": doc["id"], "user_id": user_id, "chunk_index": 0, "content": "text"}
    ).execute()
    fake_db.table("document_pages").insert(
        {"document_id": doc["id"], "user_id": user_id, "page_no": 1, "content": "text"}
    ).execute()
    conv = (
        fake_db.table("conversations").insert({"user_id": user_id, "title": "t"}).execute().data[0]
    )
    msg = (
        fake_db.table("messages")
        .insert({"conversation_id": conv["id"], "role": "user", "content": "q"})
        .execute()
        .data[0]
    )
    fake_db.table("message_feedback").insert(
        {"message_id": msg["id"], "user_id": user_id, "rating": "up"}
    ).execute()
    fake_db.rpc("consume_question", {"p_user": user_id, "p_limit": 50}).execute()


def test_deleting_an_account_removes_every_trace_and_nothing_else(
    client: TestClient, new_user: Callable[..., TestUser], fake_db: FakeSupabase
) -> None:
    leaving, staying = new_user(name="Leaving"), new_user(name="Staying")
    seed_user_data(fake_db, leaving.id)
    seed_user_data(fake_db, staying.id)

    response = client.request("DELETE", "/api/me", headers=leaving.headers, json=CONFIRM)

    assert response.status_code == 204
    assert response.content == b""
    assert not fake_db.user_has_data(leaving.id)
    assert leaving.id not in fake_db.auth_users
    assert fake_db.tables["messages"]  # the other user's data remains
    assert fake_db.tables["chunks"]
    assert all(r["user_id"] == staying.id for r in fake_db.tables["chunks"])
    assert fake_db.user_has_data(staying.id)
    assert client.get("/api/me", headers=staying.headers).status_code == 200


def test_token_of_a_deleted_account_stops_working_immediately(
    client: TestClient, user: TestUser
) -> None:
    assert client.get("/api/me", headers=user.headers).status_code == 200
    deleted = client.request("DELETE", "/api/me", headers=user.headers, json=CONFIRM)
    assert deleted.status_code == 204
    after = client.get("/api/me", headers=user.headers)
    assert after.status_code == 401
    assert after.json()["error"]["code"] == "ACCOUNT_NOT_FOUND"


@pytest.mark.parametrize(
    "body",
    [
        None,
        {},
        {"confirm": "delete"},
        {"confirm": "yes"},
        {"confirm": ""},
        {"confirm": "DELETE", "x": 1},
    ],
)
def test_deletion_requires_the_exact_confirmation(
    client: TestClient, user: TestUser, fake_db: FakeSupabase, body: dict[str, object] | None
) -> None:
    response = client.request("DELETE", "/api/me", headers=user.headers, json=body)
    assert response.status_code == 422
    assert user.id in fake_db.auth_users
    assert fake_db.user_has_data(user.id)


def test_deletion_is_idempotent_when_the_auth_user_is_already_gone(
    client: TestClient, user: TestUser, fake_db: FakeSupabase
) -> None:
    seed_user_data(fake_db, user.id)
    fake_db.auth_users.discard(user.id)  # removed elsewhere, rows still present
    response = client.request("DELETE", "/api/me", headers=user.headers, json=CONFIRM)
    assert response.status_code == 204
    assert not fake_db.user_has_data(user.id)


def test_provider_failure_during_deletion_is_a_clean_error_and_keeps_the_data(
    client: TestClient, user: TestUser, fake_db: FakeSupabase
) -> None:
    seed_user_data(fake_db, user.id)
    fake_db.auth.admin.fail_with = AuthApiError("db exploded: secret detail", 500, None)
    response = client.request("DELETE", "/api/me", headers=user.headers, json=CONFIRM)
    assert response.status_code == 502
    assert response.json()["error"]["code"] == "ACCOUNT_DELETE_FAILED"
    assert "secret detail" not in response.text
    assert fake_db.user_has_data(user.id)  # nothing was half-deleted
    assert client.get("/api/me", headers=user.headers).status_code == 200

    fake_db.auth.admin.fail_with = None
    retry = client.request("DELETE", "/api/me", headers=user.headers, json=CONFIRM)
    assert retry.status_code == 204


def test_account_deletion_is_rate_limited(
    make_settings: Callable[..., Settings], services: Services, new_user: Callable[..., TestUser]
) -> None:
    app = create_app(make_settings(rate_limit_account_per_hour=2), services)
    someone = new_user()
    with TestClient(app) as http:
        codes = [
            http.request(
                "DELETE", "/api/me", headers=someone.headers, json={"confirm": "nope"}
            ).status_code
            for _ in range(3)
        ]
    assert codes == [422, 422, 429]


def test_unknown_user_id_in_token_cannot_reach_data(client: TestClient) -> None:
    ghost = {"Authorization": f"Bearer {make_token(str(uuid.uuid4()))}"}
    assert client.get("/api/me", headers=ghost).status_code == 401
