"""Upload, ingestion lifecycle, listing, rename, delete, page text and isolation."""

from __future__ import annotations

import hashlib
from collections.abc import Callable
from typing import Any

import pytest
from app.config import Settings
from app.container import Services
from app.errors import ProviderError
from app.main import create_app
from app.services.ingest import IngestService
from fastapi.testclient import TestClient

from tests.conftest import TestUser
from tests.fakes.embedder import FakeEmbedder, provider_down
from tests.fakes.supabase import FakeSupabase
from tests.pdfs import encrypt_pdf, make_pdf

LONG = " ".join(f"photosynthesis{i % 7} converts light into chemical energy" for i in range(200))


def upload(
    client: TestClient,
    user: TestUser,
    data: bytes,
    name: str = "notes.txt",
    mime: str = "text/plain",
    **form: str,
) -> Any:
    return client.post(
        "/api/documents", headers=user.headers, files={"file": (name, data, mime)}, data=form
    )


def pdf(pages: list[str] | None = None) -> bytes:
    return make_pdf(pages or [LONG, "Second page about mitochondria and cells."])


# --------------------------------------------------------------------- lifecycle
def test_pdf_upload_is_processed_to_ready(
    client: TestClient, user: TestUser, fake_db: FakeSupabase
) -> None:
    response = upload(client, user, pdf(), "Biology Notes.pdf", "application/pdf")
    assert response.status_code == 202
    body = response.json()
    assert body["title"] == "Biology Notes"
    assert body["file_name"] == "Biology Notes.pdf"
    assert body["status"] == "processing"
    assert "file_hash" not in body
    assert "user_id" not in body

    doc = client.get(f"/api/documents/{body['id']}", headers=user.headers).json()
    assert doc["status"] == "ready"
    assert doc["processing_stage"] == "ready"
    assert doc["processing_progress"] == 100
    assert doc["page_count"] == 2
    assert doc["word_count"] > 200
    assert doc["error"] is None
    assert doc["processing_completed_at"]

    chunks = fake_db.rows("chunks", document_id=body["id"])
    assert chunks
    assert all(c["user_id"] == user.id for c in chunks)
    assert all(len(c["embedding"]) == 1024 for c in chunks)
    assert {c["page_no"] for c in chunks} == {1, 2}
    assert len(fake_db.rows("document_pages", document_id=body["id"])) == 2


def test_text_and_markdown_uploads_work(client: TestClient, user: TestUser) -> None:
    md = b"# Guide\n\n" + LONG.encode()
    for name, mime, data in [("a.txt", "text/plain", LONG.encode()), ("b.md", "text/markdown", md)]:
        doc_id = upload(client, user, data, name, mime).json()["id"]
        assert (
            client.get(f"/api/documents/{doc_id}", headers=user.headers).json()["status"] == "ready"
        )


def test_progress_stages_are_reported_in_order(
    client: TestClient, user: TestUser, fake_db: FakeSupabase
) -> None:
    upload(client, user, pdf(), "a.pdf", "application/pdf")
    stages = []
    for table, op, info in fake_db.queries:
        payload = info["payload"]
        if table == "documents" and op == "update" and "processing_stage" in payload:
            stages.append(payload["processing_stage"])
    stages = stages[stages.index("parsing") :]  # skip the startup recovery sweep
    assert stages[0] == "parsing"
    assert stages.index("chunking") < stages.index("embedding") < stages.index("indexing")
    assert stages[-1] == "ready"


def test_embedding_uses_batches(
    client: TestClient, user: TestUser, fake_embedder: FakeEmbedder
) -> None:
    fake_embedder.batch_size = 2
    upload(client, user, (LONG + "\n\n" + LONG * 2).encode())
    assert len(fake_embedder.calls) >= 2


# ------------------------------------------------------------------ validation
@pytest.mark.parametrize(
    ("name", "mime", "data", "status", "code"),
    [
        ("virus.exe", "application/octet-stream", b"MZ", 415, "UNSUPPORTED_FILE_TYPE"),
        ("noext", "text/plain", b"hello", 415, "UNSUPPORTED_FILE_TYPE"),
        ("slides.pptx", "application/vnd.ms-powerpoint", b"PK", 415, "UNSUPPORTED_FILE_TYPE"),
        ("fake.pdf", "application/pdf", b"just text, not a pdf", 415, "UNSUPPORTED_FILE_TYPE"),
        ("note.txt", "image/png", b"hello", 415, "UNSUPPORTED_FILE_TYPE"),
        ("bin.txt", "text/plain", b"abc\x00\x01\x02def", 415, "UNSUPPORTED_FILE_TYPE"),
        ("empty.txt", "text/plain", b"", 415, "UNSUPPORTED_FILE_TYPE"),
    ],
)
def test_invalid_uploads_are_rejected_before_anything_is_stored(
    client: TestClient,
    user: TestUser,
    fake_db: FakeSupabase,
    name: str,
    mime: str,
    data: bytes,
    status: int,
    code: str,
) -> None:
    response = upload(client, user, data, name, mime)
    assert (response.status_code, response.json()["error"]["code"]) == (status, code)
    assert fake_db.tables["documents"] == []


def test_generic_mime_type_is_accepted_when_content_matches(
    client: TestClient, user: TestUser
) -> None:
    assert upload(client, user, pdf(), "a.pdf", "application/octet-stream").status_code == 202


def test_oversized_upload_is_rejected(
    make_settings: Callable[..., Settings], services: Services, user: TestUser
) -> None:
    app = create_app(make_settings(max_upload_mb=1), services)
    with TestClient(app) as http:
        big = b"a" * (1024 * 1024 + 10)
        response = upload(http, user, big)
    assert response.status_code == 413
    assert response.json()["error"]["code"] == "FILE_TOO_LARGE"


def test_missing_file_is_a_validation_error(client: TestClient, user: TestUser) -> None:
    assert client.post("/api/documents", headers=user.headers).status_code == 422


def test_path_traversal_in_file_names_is_neutralised(client: TestClient, user: TestUser) -> None:
    body = upload(client, user, LONG.encode(), "../../etc/passwd.txt").json()
    assert body["file_name"] == "passwd.txt"
    body = upload(client, user, (LONG + " x").encode(), "C:\\Users\\me\\report.txt").json()
    assert body["file_name"] == "report.txt"


def test_upload_requires_authentication(client: TestClient) -> None:
    response = client.post("/api/documents", files={"file": ("a.txt", b"x", "text/plain")})
    assert response.status_code == 401


def test_document_count_limit(
    make_settings: Callable[..., Settings], services: Services, user: TestUser
) -> None:
    app = create_app(make_settings(max_documents_per_user=1), services)
    with TestClient(app) as http:
        assert upload(http, user, LONG.encode()).status_code == 202
        second = upload(http, user, b"another file entirely " * 20)
    assert second.status_code == 409
    assert second.json()["error"]["code"] == "DOCUMENT_LIMIT_REACHED"


def test_upload_is_rate_limited(
    make_settings: Callable[..., Settings], services: Services, user: TestUser
) -> None:
    app = create_app(make_settings(rate_limit_upload_per_hour=2), services)
    with TestClient(app) as http:
        codes = [upload(http, user, f"doc {i} ".encode() * 30).status_code for i in range(3)]
    assert codes == [202, 202, 429]


# ------------------------------------------------------------------ duplicates
def test_duplicate_content_is_detected_and_can_be_forced(
    client: TestClient, user: TestUser, fake_db: FakeSupabase
) -> None:
    first = upload(client, user, LONG.encode(), "a.txt").json()
    dup = upload(client, user, LONG.encode(), "renamed.txt")
    assert dup.status_code == 409
    error = dup.json()["error"]
    assert error["code"] == "DUPLICATE_DOCUMENT"
    assert error["message"] == "This document already exists."
    assert error["existing_document_id"] == first["id"]
    assert len(fake_db.tables["documents"]) == 1

    forced = upload(client, user, LONG.encode(), "again.txt", allow_duplicate="true")
    assert forced.status_code == 202
    assert len(fake_db.tables["documents"]) == 2
    stored = fake_db.tables["documents"][0]["file_hash"]
    assert stored == hashlib.sha256(LONG.encode()).hexdigest()


def test_same_file_by_another_user_is_not_a_duplicate(
    client: TestClient, new_user: Callable[..., TestUser]
) -> None:
    alice, bob = new_user(name="Alice"), new_user(name="Bob")
    assert upload(client, alice, LONG.encode()).status_code == 202
    assert upload(client, bob, LONG.encode()).status_code == 202


def test_failed_documents_do_not_block_a_retry(
    client: TestClient, user: TestUser, fake_embedder: FakeEmbedder
) -> None:
    fake_embedder.fail_with = provider_down()
    assert upload(client, user, LONG.encode()).status_code == 202
    fake_embedder.fail_with = None
    assert upload(client, user, LONG.encode()).status_code == 202


# --------------------------------------------------------------------- failures
def test_image_only_pdf_fails_with_the_ocr_message_and_stores_nothing(
    client: TestClient, user: TestUser, fake_db: FakeSupabase
) -> None:
    doc_id = upload(client, user, make_pdf(["", ""]), "scan.pdf", "application/pdf").json()["id"]
    doc = client.get(f"/api/documents/{doc_id}", headers=user.headers).json()
    assert doc["status"] == "failed"
    assert doc["processing_stage"] == "failed"
    assert (
        doc["error"]
        == "This PDF does not contain extractable text. OCR is not currently supported."
    )
    assert fake_db.tables["chunks"] == []
    assert fake_db.tables["document_pages"] == []


def test_encrypted_and_corrupt_pdfs_fail_cleanly(client: TestClient, user: TestUser) -> None:
    locked = upload(
        client, user, encrypt_pdf(make_pdf(["secret words"]), "pw"), "l.pdf", "application/pdf"
    )
    broken = upload(client, user, b"%PDF-1.4\nthis is not really a pdf", "b.pdf", "application/pdf")
    for response, fragment in [(locked, "password"), (broken, "could not be read")]:
        doc = client.get(f"/api/documents/{response.json()['id']}", headers=user.headers).json()
        assert doc["status"] == "failed"
        assert fragment in doc["error"]


def test_page_limit_failure_is_reported(
    make_settings: Callable[..., Settings], services: Services, user: TestUser
) -> None:
    limited = make_settings(max_document_pages=2)
    services.ingest = IngestService(services.repo, services.embedder, limited)
    app = create_app(limited, services)
    with TestClient(app) as http:
        doc_id = upload(
            http, user, make_pdf(["a b", "c d", "e f"]), "x.pdf", "application/pdf"
        ).json()["id"]
        doc = http.get(f"/api/documents/{doc_id}", headers=user.headers).json()
    assert doc["status"] == "failed"
    assert "limit is 2 pages" in doc["error"]


def test_embedding_outage_midway_fails_the_document_and_removes_partial_data(
    client: TestClient, user: TestUser, fake_db: FakeSupabase, fake_embedder: FakeEmbedder
) -> None:
    fake_embedder.batch_size = 1
    fake_embedder.fail_with = ProviderError(
        "voyage", "The embedding service is temporarily unavailable."
    )
    fake_embedder.fail_after_batches = 1
    doc_id = upload(client, user, (LONG * 3).encode()).json()["id"]
    doc = client.get(f"/api/documents/{doc_id}", headers=user.headers).json()
    assert doc["status"] == "failed"
    assert "temporarily unavailable" in doc["error"]
    assert fake_db.tables["chunks"] == []


def test_unexpected_errors_never_leak_details(
    client: TestClient, user: TestUser, fake_embedder: FakeEmbedder
) -> None:
    fake_embedder.fail_with = RuntimeError("secret internal stack detail 10.0.0.5")
    doc_id = upload(client, user, LONG.encode()).json()["id"]
    doc = client.get(f"/api/documents/{doc_id}", headers=user.headers).json()
    assert doc["status"] == "failed"
    assert "secret" not in doc["error"]
    assert "10.0.0.5" not in doc["error"]
    assert "try uploading" in doc["error"]


def test_database_failure_while_indexing_fails_the_document(
    client: TestClient, user: TestUser, fake_db: FakeSupabase
) -> None:
    fake_db.fail(table="chunks", op="insert", times=10)
    doc_id = upload(client, user, LONG.encode()).json()["id"]
    doc = client.get(f"/api/documents/{doc_id}", headers=user.headers).json()
    assert doc["status"] == "failed"
    assert fake_db.tables["document_pages"] == []


def test_timeout_stops_processing(
    make_settings: Callable[..., Settings], services: Services, user: TestUser
) -> None:
    limited = make_settings(ingest_timeout_seconds=30)
    ticks = iter(range(0, 1000, 20))  # every check advances the clock by 20 seconds
    services.ingest = IngestService(
        services.repo, services.embedder, limited, clock=lambda: float(next(ticks))
    )
    with TestClient(create_app(limited, services)) as http:
        doc_id = upload(http, user, LONG.encode()).json()["id"]
    doc = services.repo.get_document(user.id, doc_id)
    assert doc is not None
    assert doc["status"] == "failed"
    assert "too long" in doc["error"]


# ----------------------------------------------------------- list/get/rename/delete
def test_list_is_newest_first_and_only_shows_own_documents(
    client: TestClient, new_user: Callable[..., TestUser]
) -> None:
    alice, bob = new_user(name="Alice"), new_user(name="Bob")
    a1 = upload(client, alice, b"first document text " * 20, "one.txt").json()["id"]
    a2 = upload(client, alice, b"second document text " * 20, "two.txt").json()["id"]
    upload(client, bob, b"bobs private document " * 20, "bob.txt")
    listed = client.get("/api/documents", headers=alice.headers).json()["documents"]
    assert [d["id"] for d in listed] == [a2, a1]
    assert all("bob" not in d["title"].lower() for d in listed)


def test_rename(client: TestClient, user: TestUser) -> None:
    doc_id = upload(client, user, LONG.encode()).json()["id"]
    response = client.patch(
        f"/api/documents/{doc_id}", headers=user.headers, json={"title": "  My   Notes  "}
    )
    assert response.status_code == 200
    assert response.json()["title"] == "My Notes"


@pytest.mark.parametrize(
    "body", [{"title": ""}, {"title": "   "}, {"title": "x" * 201}, {}, {"title": "a", "x": 1}]
)
def test_invalid_rename_is_rejected(
    client: TestClient, user: TestUser, body: dict[str, Any]
) -> None:
    doc_id = upload(client, user, LONG.encode()).json()["id"]
    assert (
        client.patch(f"/api/documents/{doc_id}", headers=user.headers, json=body).status_code == 422
    )


def test_delete_removes_document_chunks_and_pages(
    client: TestClient, user: TestUser, fake_db: FakeSupabase
) -> None:
    doc_id = upload(client, user, pdf(), "a.pdf", "application/pdf").json()["id"]
    assert client.delete(f"/api/documents/{doc_id}", headers=user.headers).status_code == 204
    assert fake_db.tables["documents"] == []
    assert fake_db.tables["chunks"] == []
    assert fake_db.tables["document_pages"] == []
    assert client.delete(f"/api/documents/{doc_id}", headers=user.headers).status_code == 404


def test_malformed_ids_are_422_and_unknown_ids_404(client: TestClient, user: TestUser) -> None:
    assert client.get("/api/documents/not-a-uuid", headers=user.headers).status_code == 422
    missing = "00000000-0000-4000-8000-000000000000"
    for method in ("get", "delete"):
        assert (
            getattr(client, method)(f"/api/documents/{missing}", headers=user.headers).status_code
            == 404
        )
    assert (
        client.patch(
            f"/api/documents/{missing}", headers=user.headers, json={"title": "x"}
        ).status_code
        == 404
    )


def test_page_text_for_the_source_panel(client: TestClient, user: TestUser) -> None:
    doc_id = upload(client, user, pdf(), "a.pdf", "application/pdf").json()["id"]
    page = client.get(f"/api/documents/{doc_id}/pages/2", headers=user.headers).json()
    assert page["page_no"] == 2
    assert page["page_count"] == 2
    assert "mitochondria" in page["content"]
    assert client.get(f"/api/documents/{doc_id}/pages/3", headers=user.headers).status_code == 404
    assert client.get(f"/api/documents/{doc_id}/pages/0", headers=user.headers).status_code == 404


# -------------------------------------------------------------------- isolation
def test_users_cannot_read_change_or_delete_each_others_documents(
    client: TestClient, new_user: Callable[..., TestUser], fake_db: FakeSupabase
) -> None:
    alice, mallory = new_user(name="Alice"), new_user(name="Mallory")
    doc_id = upload(client, alice, pdf(), "private.pdf", "application/pdf").json()["id"]
    h = mallory.headers
    assert client.get(f"/api/documents/{doc_id}", headers=h).status_code == 404
    assert client.get(f"/api/documents/{doc_id}/pages/1", headers=h).status_code == 404
    assert (
        client.patch(f"/api/documents/{doc_id}", headers=h, json={"title": "pwned"}).status_code
        == 404
    )
    assert client.delete(f"/api/documents/{doc_id}", headers=h).status_code == 404
    assert client.get("/api/documents", headers=h).json()["documents"] == []
    still = client.get(f"/api/documents/{doc_id}", headers=alice.headers).json()
    assert still["title"] == "private"
    assert still["status"] == "ready"
    assert fake_db.rows("chunks", document_id=doc_id)


def test_client_supplied_user_id_is_ignored(
    client: TestClient, new_user: Callable[..., TestUser]
) -> None:
    alice, mallory = new_user(name="Alice"), new_user(name="Mallory")
    response = client.post(
        "/api/documents",
        headers=mallory.headers,
        files={"file": ("a.txt", LONG.encode(), "text/plain")},
        data={"user_id": alice.id},
    )
    assert response.status_code == 202
    assert client.get("/api/documents", headers=alice.headers).json()["documents"] == []
    assert len(client.get("/api/documents", headers=mallory.headers).json()["documents"]) == 1


def test_document_content_is_never_logged(
    client: TestClient, user: TestUser, caplog: pytest.LogCaptureFixture
) -> None:
    secret = "ultra-confidential-passphrase-xyz"
    with caplog.at_level("DEBUG"):
        upload(client, user, f"{secret} {LONG}".encode(), "report.txt")
    assert secret not in caplog.text
