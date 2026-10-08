"""Integration tests for the SQL migrations against a real Postgres + pgvector.

Set TEST_DATABASE_URL to a superuser connection string (for example
postgresql://postgres:postgres@localhost:5432/postgres). Each module run creates and drops its
own throw-away database. Without the variable these tests are skipped.
"""

from __future__ import annotations

import os
import threading
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

import psycopg
import pytest
from psycopg import sql
from psycopg.conninfo import make_conninfo
from psycopg.types.json import Jsonb

pytestmark = pytest.mark.db

ROOT = Path(__file__).resolve().parents[2]
DIM = 1024


@pytest.fixture(scope="module")
def db_url() -> Iterator[str]:
    admin_url = os.environ.get("TEST_DATABASE_URL")
    if not admin_url:
        pytest.skip("TEST_DATABASE_URL is not set")
    name = f"rag_test_{uuid.uuid4().hex[:10]}"
    with psycopg.connect(admin_url, autocommit=True) as admin:
        admin.execute(sql.SQL("create database {}").format(sql.Identifier(name)))
        url = make_conninfo(admin_url, dbname=name)
        try:
            with psycopg.connect(url, autocommit=True) as conn:
                conn.execute((ROOT / "tests/db/bootstrap.sql").read_text())
                for migration in sorted((ROOT / "supabase/migrations").glob("*.sql")):
                    conn.execute(migration.read_text())
            yield url
        finally:
            admin.execute(
                sql.SQL("drop database if exists {} with (force)").format(sql.Identifier(name))
            )


@pytest.fixture
def conn(db_url: str) -> Iterator[psycopg.Connection]:
    with psycopg.connect(db_url, autocommit=True) as connection:
        yield connection


@contextmanager
def as_role(
    conn: psycopg.Connection, role: str, user_id: uuid.UUID | None = None
) -> Iterator[None]:
    """Run statements the way PostgREST would: as `role`, with the caller's JWT subject set."""
    with conn.transaction():
        conn.execute(sql.SQL("set local role {}").format(sql.Identifier(role)))
        if user_id is not None:
            conn.execute("select set_config('request.jwt.claim.sub', %s, true)", (str(user_id),))
        yield


def vec(**dims: float) -> str:
    """A 1024-dim vector literal with the given {index: value} entries set (others are 0)."""
    values = [0.0] * DIM
    for index, value in dims.items():
        values[int(index.removeprefix("d"))] = value
    return "[" + ",".join(str(v) for v in values) + "]"


def add_user(conn: psycopg.Connection, name: str | None = None) -> uuid.UUID:
    user_id = uuid.uuid4()
    meta = {"name": name} if name else {}
    conn.execute(
        "insert into auth.users (id, email, raw_user_meta_data) values (%s, %s, %s)",
        (user_id, f"{user_id}@example.test", Jsonb(meta)),
    )
    return user_id


def add_doc(
    conn: psycopg.Connection, user_id: uuid.UUID, title: str, status: str = "ready"
) -> uuid.UUID:
    row = conn.execute(
        "insert into documents (user_id, title, status) values (%s, %s, %s) returning id",
        (user_id, title, status),
    ).fetchone()
    assert row is not None
    doc_id: uuid.UUID = row[0]
    return doc_id


def add_chunk(
    conn: psycopg.Connection,
    user_id: uuid.UUID,
    doc_id: uuid.UUID,
    content: str,
    embedding: str,
    *,
    index: int = 0,
    page: int = 1,
) -> uuid.UUID:
    row = conn.execute(
        "insert into chunks (document_id, user_id, chunk_index, page_no, content, embedding) "
        "values (%s, %s, %s, %s, %s, %s::vector) returning id",
        (doc_id, user_id, index, page, content, embedding),
    ).fetchone()
    assert row is not None
    chunk_id: uuid.UUID = row[0]
    return chunk_id


def search(
    conn: psycopg.Connection,
    user_id: uuid.UUID,
    text: str,
    emb: str,
    doc_ids: list[uuid.UUID] | None = None,
    k: int = 20,
) -> list[tuple[uuid.UUID, uuid.UUID, str, int, float]]:
    rows = conn.execute(
        "select chunk_id, document_id, title, page_no, score "
        "from hybrid_search(%s, %s::vector, %s, %s, %s)",
        (text, emb, user_id, doc_ids, k),
    ).fetchall()
    return [(r[0], r[1], r[2], r[3], r[4]) for r in rows]


# ---------------------------------------------------------------------------- profiles
def test_profile_is_created_on_signup(conn: psycopg.Connection) -> None:
    uid = add_user(conn, "Priya")
    row = conn.execute("select name from profiles where id = %s", (uid,)).fetchone()
    assert row == ("Priya",)


# --------------------------------------------------------------------------------- RLS
def test_rls_users_only_read_their_own_rows(conn: psycopg.Connection) -> None:
    a, b = add_user(conn), add_user(conn)
    doc_a, doc_b = add_doc(conn, a, "A"), add_doc(conn, b, "B")
    add_chunk(conn, a, doc_a, "alpha", vec(d0=1))
    add_chunk(conn, b, doc_b, "bravo", vec(d0=1))
    conv_a = conn.execute(
        "insert into conversations (user_id, title) values (%s, 'a') returning id", (a,)
    ).fetchone()
    conv_b = conn.execute(
        "insert into conversations (user_id, title) values (%s, 'b') returning id", (b,)
    ).fetchone()
    assert conv_a
    assert conv_b
    msg_a = conn.execute(
        "insert into messages (conversation_id, role, content) values (%s, 'user', 'hi a') "
        "returning id",
        (conv_a[0],),
    ).fetchone()
    conn.execute(
        "insert into messages (conversation_id, role, content) values (%s, 'user', 'hi b')",
        (conv_b[0],),
    )
    assert msg_a
    conn.execute("select consume_question(%s, 5)", (a,))
    conn.execute("select consume_question(%s, 5)", (b,))
    conn.execute(
        "insert into document_pages (document_id, user_id, page_no, content) values (%s,%s,1,'p')",
        (doc_b, b),
    )
    conn.execute(
        "insert into message_feedback (message_id, user_id, rating) values (%s, %s, 'up')",
        (msg_a[0], a),
    )

    tables = [
        "profiles", "documents", "document_pages", "chunks", "conversations", "messages",
        "message_feedback", "usage_daily",
    ]  # fmt: skip
    for table in tables:
        with as_role(conn, "authenticated", a):
            visible = conn.execute(
                sql.SQL("select count(*) from {} where false or true").format(sql.Identifier(table))
            ).fetchone()
            owners = conn.execute(
                sql.SQL("select count(*) from {}").format(sql.Identifier(table))
            ).fetchone()
        assert visible == owners
    with as_role(conn, "authenticated", a):
        assert conn.execute("select content from chunks").fetchall() == [("alpha",)]
        assert conn.execute("select title from documents").fetchall() == [("A",)]
        assert conn.execute("select content from messages").fetchall() == [("hi a",)]
        assert conn.execute("select count(*) from document_pages").fetchone() == (0,)
        assert conn.execute("select count(*) from profiles").fetchone() == (1,)
        assert conn.execute("select count(*) from usage_daily").fetchone() == (1,)
        # Knowing the other user's UUID gives nothing.
        assert conn.execute(
            "select count(*) from documents where id = %s", (doc_b,)
        ).fetchone() == (0,)
        assert conn.execute("select count(*) from chunks where user_id = %s", (b,)).fetchone() == (
            0,
        )


def test_authenticated_users_cannot_write_application_tables(conn: psycopg.Connection) -> None:
    a = add_user(conn)
    doc = add_doc(conn, a, "mine")
    conn.execute("select consume_question(%s, 5)", (a,))
    statements = [
        ("update usage_daily set questions = 0", ()),
        ("insert into documents (user_id, title) values (%s, 'x')", (a,)),
        ("update documents set status = 'failed' where id = %s", (doc,)),
        ("delete from documents where id = %s", (doc,)),
        ("insert into conversations (user_id) values (%s)", (a,)),
        ("update profiles set name = 'x'", ()),
    ]
    for statement, params in statements:
        with pytest.raises(psycopg.errors.InsufficientPrivilege), as_role(conn, "authenticated", a):
            conn.execute(statement, params)
    assert conn.execute(
        "select questions from usage_daily where user_id = %s", (a,)
    ).fetchone() == (1,)


def test_anon_has_no_access(conn: psycopg.Connection) -> None:
    for table in ("documents", "chunks", "conversations", "messages", "usage_daily", "profiles"):
        with pytest.raises(psycopg.errors.InsufficientPrivilege), as_role(conn, "anon"):
            conn.execute(sql.SQL("select * from {}").format(sql.Identifier(table)))


def test_privileged_functions_are_not_callable_by_users(conn: psycopg.Connection) -> None:
    a = add_user(conn)
    calls = [
        ("select consume_question(%s, 1000000)", (a,)),
        ("select refund_question(%s)", (a,)),
        ("select * from hybrid_search('x', %s::vector, %s, null, 5)", (vec(d0=1), a)),
        ("select * from search_conversations(%s, 'x')", (a,)),
    ]
    for role in ("authenticated", "anon"):
        for statement, params in calls:
            with pytest.raises(psycopg.errors.InsufficientPrivilege), as_role(conn, role, a):
                conn.execute(statement, params)


# ------------------------------------------------------------------------------ quota
def test_consume_question_respects_limit_and_refund(conn: psycopg.Connection) -> None:
    a = add_user(conn)
    results = [conn.execute("select consume_question(%s, 3)", (a,)).fetchone() for _ in range(5)]
    assert [r[0] for r in results if r] == [True, True, True, False, False]
    conn.execute("select refund_question(%s)", (a,))
    assert conn.execute("select consume_question(%s, 3)", (a,)).fetchone() == (True,)
    assert conn.execute("select consume_question(%s, 3)", (a,)).fetchone() == (False,)
    # Refund never goes below zero.
    b = add_user(conn)
    conn.execute("select refund_question(%s)", (b,))
    assert conn.execute("select count(*) from usage_daily where user_id = %s", (b,)).fetchone() == (
        0,
    )


def test_consume_question_is_atomic_under_concurrency(
    db_url: str, conn: psycopg.Connection
) -> None:
    a = add_user(conn)
    granted: list[bool] = []
    lock = threading.Lock()
    barrier = threading.Barrier(20)

    def worker() -> None:
        with psycopg.connect(db_url, autocommit=True) as c:
            barrier.wait()
            row = c.execute("select consume_question(%s, 5)", (a,)).fetchone()
            with lock:
                granted.append(bool(row and row[0]))

    threads = [threading.Thread(target=worker) for _ in range(20)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert granted.count(True) == 5
    assert conn.execute(
        "select questions from usage_daily where user_id = %s", (a,)
    ).fetchone() == (5,)


# -------------------------------------------------------------------------- retrieval
def test_hybrid_search_fuses_vector_and_full_text_ranks(conn: psycopg.Connection) -> None:
    a = add_user(conn)
    doc = add_doc(conn, a, "db-notes.pdf")
    both = add_chunk(conn, a, doc, "postgres indexing basics", vec(d0=1), index=0, page=3)
    vector_only = add_chunk(conn, a, doc, "gardening tips", vec(d0=0.6, d1=0.8), index=1, page=4)
    text_only = add_chunk(conn, a, doc, "tuning postgres performance", vec(d2=1), index=2, page=5)

    rows = search(conn, a, "postgres or indexing", vec(d0=1))
    ids = [r[0] for r in rows]
    assert ids[0] == both
    assert set(ids) == {both, vector_only, text_only}
    top = rows[0]
    assert top[2] == "db-notes.pdf"
    assert top[3] == 3
    assert top[4] == pytest.approx(2 / 61)
    # Every chunk is within the vector top-20, so ranks are: vector both=1, vector_only=2,
    # text_only=3; full text: both=1 (two terms matched), text_only=2 (one term matched).
    scores = {r[0]: r[4] for r in rows}
    assert scores[vector_only] == pytest.approx(1 / 62)
    assert scores[text_only] == pytest.approx(1 / 63 + 1 / 62)
    assert ids == [both, text_only, vector_only]


def test_hybrid_search_never_returns_other_users_chunks(conn: psycopg.Connection) -> None:
    a, b = add_user(conn), add_user(conn)
    doc_a, doc_b = add_doc(conn, a, "A"), add_doc(conn, b, "B")
    mine = add_chunk(conn, a, doc_a, "shared keyword here", vec(d0=1))
    theirs = add_chunk(conn, b, doc_b, "shared keyword here", vec(d0=1))
    ids = [r[0] for r in search(conn, a, "shared", vec(d0=1))]
    assert ids == [mine]
    assert theirs not in ids
    # Passing the other user's document id as a filter does not widen access either.
    assert search(conn, a, "shared", vec(d0=1), doc_ids=[doc_b]) == []


def test_hybrid_search_respects_selected_documents_and_ready_status(
    conn: psycopg.Connection,
) -> None:
    a = add_user(conn)
    doc1, doc2 = add_doc(conn, a, "one"), add_doc(conn, a, "two")
    busy = add_doc(conn, a, "busy", status="processing")
    broken = add_doc(conn, a, "broken", status="failed")
    c1 = add_chunk(conn, a, doc1, "common term", vec(d0=1))
    c2 = add_chunk(conn, a, doc2, "common term", vec(d0=1))
    add_chunk(conn, a, busy, "common term", vec(d0=1))
    add_chunk(conn, a, broken, "common term", vec(d0=1))

    assert {r[0] for r in search(conn, a, "common", vec(d0=1))} == {c1, c2}
    assert [r[0] for r in search(conn, a, "common", vec(d0=1), doc_ids=[doc1])] == [c1]
    assert search(conn, a, "common", vec(d0=1), doc_ids=[busy, broken]) == []


def test_hybrid_search_limits_results_and_handles_empty_text(conn: psycopg.Connection) -> None:
    a = add_user(conn)
    doc = add_doc(conn, a, "many")
    for i in range(30):
        add_chunk(conn, a, doc, f"chunk number {i}", vec(d0=1, d1=i / 100), index=i)
    assert len(search(conn, a, "number", vec(d0=1), k=7)) == 7
    # Blank text query falls back to vector-only retrieval instead of failing.
    assert len(search(conn, a, "", vec(d0=1), k=5)) == 5


# ----------------------------------------------------------------------- conversations
def test_search_conversations_matches_titles_and_messages_and_escapes_wildcards(
    conn: psycopg.Connection,
) -> None:
    a, b = add_user(conn), add_user(conn)

    def conversation(user: uuid.UUID, title: str, *messages: str) -> uuid.UUID:
        row = conn.execute(
            "insert into conversations (user_id, title) values (%s, %s) returning id", (user, title)
        ).fetchone()
        assert row
        for text in messages:
            conn.execute(
                "insert into messages (conversation_id, role, content) values (%s, 'user', %s)",
                (row[0], text),
            )
        return row[0]  # type: ignore[no-any-return]

    by_title = conversation(a, "Normalization notes")
    by_message = conversation(a, "Untitled", "explain 3NF please")
    percent = conversation(a, "Discount 100% off")
    conversation(b, "Normalization notes (someone else)")

    def find(q: str) -> set[uuid.UUID]:
        rows = conn.execute("select id from search_conversations(%s, %s)", (a, q)).fetchall()
        return {r[0] for r in rows}

    assert find("normalization") == {by_title}
    assert find("3nf") == {by_message}
    assert find("100%") == {percent}
    assert find("%") == {percent}  # a bare % is literal, not "match everything"
    assert find("_") == set()


def test_new_messages_touch_the_conversation(conn: psycopg.Connection) -> None:
    a = add_user(conn)
    row = conn.execute(
        "insert into conversations (user_id, title, updated_at) "
        "values (%s, 't', now() - interval '3 days') returning id",
        (a,),
    ).fetchone()
    assert row
    conn.execute(
        "insert into messages (conversation_id, role, content) values (%s, 'user', 'hello')",
        (row[0],),
    )
    age = conn.execute(
        "select now() - updated_at < interval '1 minute' from conversations where id = %s",
        (row[0],),
    ).fetchone()
    assert age == (True,)


def test_feedback_is_unique_per_user_message_and_validates_reason(conn: psycopg.Connection) -> None:
    a = add_user(conn)
    conv = conn.execute(
        "insert into conversations (user_id) values (%s) returning id", (a,)
    ).fetchone()
    assert conv
    msg = conn.execute(
        "insert into messages (conversation_id, role, content) values (%s, 'assistant', 'x') "
        "returning id",
        (conv[0],),
    ).fetchone()
    assert msg
    conn.execute(
        "insert into message_feedback (message_id, user_id, rating, reason) "
        "values (%s, %s, 'down', 'wrong_citation')",
        (msg[0], a),
    )
    with pytest.raises(psycopg.errors.UniqueViolation):
        conn.execute(
            "insert into message_feedback (message_id, user_id, rating) values (%s, %s, 'up')",
            (msg[0], a),
        )
    with pytest.raises(psycopg.errors.CheckViolation):
        conn.execute(
            "insert into message_feedback (message_id, user_id, rating, reason) "
            "values (%s, %s, 'down', 'because')",
            (msg[0], add_user(conn)),
        )


# --------------------------------------------------------------------- account deletion
def test_deleting_the_auth_user_removes_all_application_data(conn: psycopg.Connection) -> None:
    a, keep = add_user(conn), add_user(conn)
    doc = add_doc(conn, a, "gone")
    add_chunk(conn, a, doc, "text", vec(d0=1))
    conn.execute(
        "insert into document_pages (document_id, user_id, page_no, content) values (%s,%s,1,'p')",
        (doc, a),
    )
    conv = conn.execute(
        "insert into conversations (user_id) values (%s) returning id", (a,)
    ).fetchone()
    assert conv
    msg = conn.execute(
        "insert into messages (conversation_id, role, content) "
        "values (%s, 'user', 'q') returning id",
        (conv[0],),
    ).fetchone()
    assert msg
    conn.execute(
        "insert into message_feedback (message_id, user_id, rating) values (%s, %s, 'up')",
        (msg[0], a),
    )
    conn.execute("select consume_question(%s, 5)", (a,))
    keep_doc = add_doc(conn, keep, "stays")

    conn.execute("delete from auth.users where id = %s", (a,))

    for table, column in [
        ("profiles", "id"), ("documents", "user_id"), ("document_pages", "user_id"),
        ("chunks", "user_id"), ("conversations", "user_id"), ("message_feedback", "user_id"),
        ("usage_daily", "user_id"),
    ]:  # fmt: skip
        row = conn.execute(
            sql.SQL("select count(*) from {} where {} = %s").format(
                sql.Identifier(table), sql.Identifier(column)
            ),
            (a,),
        ).fetchone()
        assert row == (0,), table
    assert conn.execute("select count(*) from messages where id = %s", (msg[0],)).fetchone() == (0,)
    assert conn.execute("select count(*) from documents where id = %s", (keep_doc,)).fetchone() == (
        1,
    )
