from __future__ import annotations

import json
import logging
from datetime import UTC, datetime, timedelta

import httpx
import pytest
from app.db import run
from app.errors import DatabaseError
from app.logging import JsonFormatter, log_event, redact, request_id_var
from app.repository import Repository

from tests.fakes.supabase import FakeSupabase, api_error


# ---------------------------------------------------------------------------- logging
def format_event(**fields: object) -> dict[str, object]:
    record = logging.LogRecord("t", logging.INFO, __file__, 1, "event", None, None)
    record.fields = fields
    parsed: dict[str, object] = json.loads(JsonFormatter().format(record))
    return parsed


def test_log_lines_are_single_json_objects_with_request_id() -> None:
    token = request_id_var.set("req-12345678")
    try:
        payload = format_event(route="/api/documents", status=200, latency_ms=12.5)
    finally:
        request_id_var.reset(token)
    assert payload["request_id"] == "req-12345678"
    assert payload["route"] == "/api/documents"
    assert payload["status"] == 200
    assert payload["level"] == "info"
    assert "ts" in payload


def test_sensitive_fields_are_redacted_by_name() -> None:
    payload = format_event(
        question="What does my private contract say?",
        content="document text",
        password="hunter2",
        token="abc",
        route="/api/chat",
    )
    text = json.dumps(payload)
    assert "private contract" not in text
    assert "document text" not in text
    assert "hunter2" not in text
    assert payload["question"] == "[redacted]"
    assert payload["route"] == "/api/chat"


def test_secrets_inside_free_text_are_masked() -> None:
    jwt = "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0NTY3ODkwIn0.c2lnbmF0dXJlLXZhbHVl"
    samples = [
        f"Authorization: Bearer {jwt}",
        f"token was {jwt}",
        "key sk-ant-api03-abcdefghijklmnop",
        "voyage pa-abcdefghijklmnopqrstuvwxyz0123",
    ]
    for sample in samples:
        cleaned = redact(sample)
        assert jwt not in cleaned
        assert "sk-ant-api03-abcdefghijklmnop" not in cleaned
        assert "pa-abcdefghijklmnopqrstuvwxyz0123" not in cleaned


def test_log_event_emits_through_the_logging_system(caplog: pytest.LogCaptureFixture) -> None:
    with caplog.at_level(logging.INFO, logger="test.events"):
        log_event(logging.getLogger("test.events"), "something_happened", count=3)
    assert caplog.records[0].getMessage() == "something_happened"
    assert caplog.records[0].fields == {"count": 3}  # type: ignore[attr-defined]


# ------------------------------------------------------------------------ db retries
class FlakyBuilder:
    def __init__(self, failures: list[BaseException], data: object = None) -> None:
        self.failures = failures
        self.calls = 0
        self.data = data if data is not None else [{"ok": True}]

    def execute(self) -> object:
        self.calls += 1
        if self.failures:
            raise self.failures.pop(0)
        return type("R", (), {"data": self.data})()


def test_transient_failures_are_retried_then_succeed() -> None:
    builder = FlakyBuilder([httpx.ConnectError("down"), api_error(code="503")])
    assert run(builder) == [{"ok": True}]
    assert builder.calls == 3


def test_persistent_transient_failure_becomes_503() -> None:
    builder = FlakyBuilder([httpx.ReadTimeout("slow")] * 5)
    with pytest.raises(DatabaseError) as caught:
        run(builder)
    assert caught.value.status_code == 503
    assert caught.value.code == "DATABASE_UNAVAILABLE"
    assert builder.calls == 3


def test_permanent_errors_are_not_retried_and_hide_details() -> None:
    builder = FlakyBuilder([api_error("relation does not exist: secret_table", "42P01")])
    with pytest.raises(DatabaseError) as caught:
        run(builder)
    assert builder.calls == 1
    assert caught.value.status_code == 500
    assert "secret_table" not in caught.value.message


# ------------------------------------------------------------------------ repository
def test_stale_processing_documents_are_marked_failed(fake_db: FakeSupabase) -> None:
    user = fake_db.add_user()
    old = (datetime.now(UTC) - timedelta(hours=2)).isoformat()
    fake_db.tables["documents"] += [
        {"id": "old", "user_id": user, "title": "old", "status": "processing",
         "processing_started_at": old},
        {"id": "fresh", "user_id": user, "title": "fresh", "status": "processing",
         "processing_started_at": datetime.now(UTC).isoformat()},
        {"id": "done", "user_id": user, "title": "done", "status": "ready",
         "processing_started_at": old},
    ]  # fmt: skip
    # The fake fills defaults only through the query builder, so complete the rows by hand.
    for row in fake_db.tables["documents"]:
        row.setdefault("processing_stage", "embedding")
        row.setdefault("error", None)
        row.setdefault("processing_completed_at", None)

    recovered = Repository(fake_db).recover_stale_documents(3600)

    assert recovered == 1
    by_id = {r["id"]: r for r in fake_db.tables["documents"]}
    assert by_id["old"]["status"] == "failed"
    assert by_id["old"]["processing_stage"] == "failed"
    assert "interrupted" in by_id["old"]["error"]
    assert by_id["old"]["processing_completed_at"] is not None
    assert by_id["fresh"]["status"] == "processing"
    assert by_id["done"]["status"] == "ready"
