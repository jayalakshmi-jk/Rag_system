from __future__ import annotations

import uuid
from collections.abc import Callable

import pytest
from fastapi.testclient import TestClient

from tests.conftest import TestUser
from tests.fakes.supabase import FakeSupabase


def make_conversation(
    db: FakeSupabase, user: TestUser, title: str, *messages: tuple[str, str]
) -> str:
    conv = db.table("conversations").insert({"user_id": user.id, "title": title}).execute().data[0]
    for role, text in messages:
        db.table("messages").insert(
            {"conversation_id": conv["id"], "role": role, "content": text}
        ).execute()
    return str(conv["id"])


def test_list_is_newest_first_and_scoped(
    client: TestClient, fake_db: FakeSupabase, new_user: Callable[..., TestUser]
) -> None:
    alice, bob = new_user(name="Alice"), new_user(name="Bob")
    first = make_conversation(fake_db, alice, "First")
    second = make_conversation(fake_db, alice, "Second")
    make_conversation(fake_db, bob, "Bobs")
    listed = client.get("/api/conversations", headers=alice.headers).json()["conversations"]
    assert [c["id"] for c in listed] == [second, first]


def test_search_matches_titles_and_message_text_and_escapes_wildcards(
    client: TestClient, fake_db: FakeSupabase, user: TestUser
) -> None:
    a = make_conversation(fake_db, user, "Refund policy")
    b = make_conversation(fake_db, user, "Other", ("user", "what about 100% discount?"))
    make_conversation(fake_db, user, "Unrelated", ("user", "weather"))

    def found(q: str) -> set[str]:
        return {
            c["id"]
            for c in client.get("/api/conversations", params={"q": q}, headers=user.headers).json()[
                "conversations"
            ]
        }

    assert found("refund") == {a}
    assert found("100%") == {b}
    assert found("%") == {b}
    assert found("zzz") == set()


def test_search_is_scoped_to_the_caller(
    client: TestClient, fake_db: FakeSupabase, new_user: Callable[..., TestUser]
) -> None:
    alice, bob = new_user(name="Alice"), new_user(name="Bob")
    make_conversation(fake_db, alice, "Secret plans")
    assert (
        client.get("/api/conversations", params={"q": "secret"}, headers=bob.headers).json()[
            "conversations"
        ]
        == []
    )


def test_open_returns_messages_in_order_with_feedback(
    client: TestClient, fake_db: FakeSupabase, user: TestUser
) -> None:
    cid = make_conversation(
        fake_db, user, "T", ("user", "q1"), ("assistant", "a1"), ("user", "q2"), ("assistant", "a2")
    )
    messages = client.get(f"/api/conversations/{cid}", headers=user.headers).json()["messages"]
    assert [m["content"] for m in messages] == ["q1", "a1", "q2", "a2"]
    assistant_id = messages[1]["id"]
    assert (
        client.put(
            f"/api/messages/{assistant_id}/feedback", headers=user.headers, json={"rating": "up"}
        ).status_code
        == 204
    )
    again = client.get(f"/api/conversations/{cid}", headers=user.headers).json()["messages"]
    assert [m["feedback"] for m in again] == [None, "up", None, None]


def test_rename_and_delete(client: TestClient, fake_db: FakeSupabase, user: TestUser) -> None:
    cid = make_conversation(fake_db, user, "Old", ("user", "q"), ("assistant", "a"))
    renamed = client.patch(
        f"/api/conversations/{cid}", headers=user.headers, json={"title": "  New   name "}
    )
    assert renamed.status_code == 200
    assert renamed.json()["title"] == "New name"
    assert client.delete(f"/api/conversations/{cid}", headers=user.headers).status_code == 204
    assert fake_db.tables["messages"] == []
    assert client.delete(f"/api/conversations/{cid}", headers=user.headers).status_code == 404
    assert client.get(f"/api/conversations/{cid}", headers=user.headers).status_code == 404


@pytest.mark.parametrize(
    "body", [{}, {"title": ""}, {"title": "  "}, {"title": "x" * 121}, {"title": "a", "z": 1}]
)
def test_invalid_rename(
    client: TestClient, fake_db: FakeSupabase, user: TestUser, body: dict[str, object]
) -> None:
    cid = make_conversation(fake_db, user, "Old")
    assert (
        client.patch(f"/api/conversations/{cid}", headers=user.headers, json=body).status_code
        == 422
    )


def test_users_cannot_touch_each_others_conversations_or_messages(
    client: TestClient, fake_db: FakeSupabase, new_user: Callable[..., TestUser]
) -> None:
    alice, mallory = new_user(name="Alice"), new_user(name="Mallory")
    cid = make_conversation(fake_db, alice, "Private", ("user", "q"), ("assistant", "a"))
    mid = fake_db.rows("messages", conversation_id=cid)[1]["id"]
    h = mallory.headers
    assert client.get(f"/api/conversations/{cid}", headers=h).status_code == 404
    assert (
        client.patch(f"/api/conversations/{cid}", headers=h, json={"title": "x"}).status_code == 404
    )
    assert client.delete(f"/api/conversations/{cid}", headers=h).status_code == 404
    assert (
        client.put(f"/api/messages/{mid}/feedback", headers=h, json={"rating": "up"}).status_code
        == 404
    )
    assert client.delete(f"/api/messages/{mid}/feedback", headers=h).status_code == 404
    assert (
        client.get(f"/api/conversations/{cid}", headers=alice.headers).json()["title"] == "Private"
    )
    assert fake_db.tables["message_feedback"] == []


def test_feedback_lifecycle(client: TestClient, fake_db: FakeSupabase, user: TestUser) -> None:
    cid = make_conversation(fake_db, user, "T", ("user", "q"), ("assistant", "a"))
    user_msg, assistant_msg = (m["id"] for m in fake_db.rows("messages", conversation_id=cid))
    url = f"/api/messages/{assistant_msg}/feedback"
    assert (
        client.put(
            url, headers=user.headers, json={"rating": "down", "reason": "wrong_citation"}
        ).status_code
        == 204
    )
    assert (
        client.put(url, headers=user.headers, json={"rating": "up"}).status_code == 204
    )  # replaces
    rows = fake_db.rows("message_feedback")
    assert len(rows) == 1
    assert rows[0]["rating"] == "up"
    assert rows[0]["reason"] is None
    assert client.delete(url, headers=user.headers).status_code == 204
    assert fake_db.tables["message_feedback"] == []
    # Only assistant answers can be rated.
    assert (
        client.put(
            f"/api/messages/{user_msg}/feedback", headers=user.headers, json={"rating": "up"}
        ).status_code
        == 404
    )


@pytest.mark.parametrize(
    "body",
    [
        {},
        {"rating": "meh"},
        {"rating": "down", "reason": "because"},
        {"rating": "up", "reason": "other"},
        {"rating": "up", "x": 1},
    ],
)
def test_invalid_feedback(
    client: TestClient, fake_db: FakeSupabase, user: TestUser, body: dict[str, object]
) -> None:
    cid = make_conversation(fake_db, user, "T", ("user", "q"), ("assistant", "a"))
    mid = fake_db.rows("messages", conversation_id=cid)[1]["id"]
    assert (
        client.put(f"/api/messages/{mid}/feedback", headers=user.headers, json=body).status_code
        == 422
    )


def test_unknown_ids(client: TestClient, user: TestUser) -> None:
    missing = uuid.uuid4()
    assert (
        client.put(
            f"/api/messages/{missing}/feedback", headers=user.headers, json={"rating": "up"}
        ).status_code
        == 404
    )
    assert client.get("/api/conversations/not-a-uuid", headers=user.headers).status_code == 422
