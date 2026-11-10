"""Data access layer.

Every method that touches user-owned data takes the *verified* ``user_id`` and filters on it
explicitly. The backend uses the service role key (which bypasses Row Level Security), so these
filters are the primary isolation boundary; RLS is the second layer for any direct client access.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any, Protocol

import httpx
from supabase_auth.errors import AuthApiError, AuthError

from app.db import run
from app.errors import AppError, DatabaseError

Row = dict[str, Any]


class SupabaseLike(Protocol):
    """The slice of the supabase-py client the repository relies on (also implemented by the
    in-memory fake used in tests)."""

    def table(self, table_name: str) -> Any: ...

    def rpc(self, fn: str, params: dict[str, Any] | None = None) -> Any: ...

    @property
    def auth(self) -> Any: ...


def rows(data: Any) -> list[Row]:
    return list(data) if isinstance(data, list) else []


def first(data: Any) -> Row | None:
    items = rows(data)
    return items[0] if items else None


INSERT_BATCH = 100


def now_iso() -> str:
    return datetime.now(UTC).isoformat()


class Repository:
    def __init__(self, client: SupabaseLike) -> None:
        self.client = client

    # ------------------------------------------------------------------ health
    def ping(self) -> None:
        run(self.client.table("profiles").select("id").limit(1), attempts=1)

    # --------------------------------------------------------------- documents
    def recover_stale_documents(self, older_than_seconds: int) -> int:
        """Mark documents stuck in 'processing' (e.g. after a restart) as failed."""
        cutoff = (datetime.now(UTC) - timedelta(seconds=older_than_seconds)).isoformat()
        data = run(
            self.client.table("documents")
            .update(
                {
                    "status": "failed",
                    "processing_stage": "failed",
                    "error": "Processing was interrupted. Please upload the document again.",
                    "processing_completed_at": now_iso(),
                }
            )
            .eq("status", "processing")
            .lt("processing_started_at", cutoff)
        )
        return len(rows(data))

    DOCUMENT_FIELDS = (
        "id,title,file_name,status,error,page_count,file_size,word_count,"
        "processing_stage,processing_progress,processing_started_at,"
        "processing_completed_at,created_at"
    )

    def create_document(
        self,
        user_id: str,
        *,
        title: str,
        file_name: str,
        file_size: int,
        mime_type: str | None,
        file_hash: str,
    ) -> Row:
        data = run(
            self.client.table("documents").insert(
                {
                    "user_id": user_id,
                    "title": title,
                    "file_name": file_name,
                    "file_size": file_size,
                    "mime_type": mime_type,
                    "file_hash": file_hash,
                }
            )
        )
        created = first(data)
        if created is None:
            raise DatabaseError()
        return created

    def list_documents(self, user_id: str) -> list[Row]:
        return rows(
            run(
                self.client.table("documents")
                .select(self.DOCUMENT_FIELDS)
                .eq("user_id", user_id)
                .order("created_at", desc=True)
            )
        )

    def get_document(self, user_id: str, document_id: str) -> Row | None:
        return first(
            run(
                self.client.table("documents")
                .select(self.DOCUMENT_FIELDS)
                .eq("user_id", user_id)
                .eq("id", document_id)
                .limit(1)
            )
        )

    def count_documents(self, user_id: str) -> int:
        return len(rows(run(self.client.table("documents").select("id").eq("user_id", user_id))))

    def find_duplicate(self, user_id: str, file_hash: str) -> Row | None:
        """An existing non-failed document with identical content for this user."""
        data = run(
            self.client.table("documents")
            .select("id,title,status")
            .eq("user_id", user_id)
            .eq("file_hash", file_hash)
            .in_("status", ["ready", "processing"])
            .limit(1)
        )
        return first(data)

    def rename_document(self, user_id: str, document_id: str, title: str) -> Row | None:
        data = run(
            self.client.table("documents")
            .update({"title": title})
            .eq("user_id", user_id)
            .eq("id", document_id)
        )
        return first(data)

    def delete_document(self, user_id: str, document_id: str) -> bool:
        """Delete a document (chunks and pages cascade). Returns False if it was not found."""
        data = run(
            self.client.table("documents").delete().eq("user_id", user_id).eq("id", document_id)
        )
        return bool(rows(data))

    def set_document_progress(
        self, user_id: str, document_id: str, stage: str, progress: int
    ) -> None:
        run(
            self.client.table("documents")
            .update({"processing_stage": stage, "processing_progress": max(0, min(progress, 100))})
            .eq("user_id", user_id)
            .eq("id", document_id)
            .eq("status", "processing")
        )

    def complete_document(
        self, user_id: str, document_id: str, *, page_count: int, word_count: int
    ) -> None:
        run(
            self.client.table("documents")
            .update(
                {
                    "status": "ready",
                    "processing_stage": "ready",
                    "processing_progress": 100,
                    "page_count": page_count,
                    "word_count": word_count,
                    "error": None,
                    "processing_completed_at": now_iso(),
                }
            )
            .eq("user_id", user_id)
            .eq("id", document_id)
        )

    def fail_document(self, user_id: str, document_id: str, message: str) -> None:
        run(
            self.client.table("documents")
            .update(
                {
                    "status": "failed",
                    "processing_stage": "failed",
                    "error": message,
                    "processing_completed_at": now_iso(),
                }
            )
            .eq("user_id", user_id)
            .eq("id", document_id)
        )

    def save_pages(self, user_id: str, document_id: str, pages: list[tuple[int, str]]) -> None:
        for start in range(0, len(pages), INSERT_BATCH):
            batch = pages[start : start + INSERT_BATCH]
            run(
                self.client.table("document_pages").upsert(
                    [
                        {"document_id": document_id, "user_id": user_id, "page_no": n, "content": t}
                        for n, t in batch
                    ],
                    on_conflict="document_id,page_no",
                )
            )

    def save_chunks(self, user_id: str, document_id: str, chunk_rows: list[Row]) -> None:
        for start in range(0, len(chunk_rows), INSERT_BATCH):
            batch = chunk_rows[start : start + INSERT_BATCH]
            run(
                self.client.table("chunks").insert(
                    [{**row, "document_id": document_id, "user_id": user_id} for row in batch]
                )
            )

    def clear_document_content(self, user_id: str, document_id: str) -> None:
        for table in ("chunks", "document_pages"):
            run(
                self.client.table(table)
                .delete()
                .eq("user_id", user_id)
                .eq("document_id", document_id)
            )

    def get_page(self, user_id: str, document_id: str, page_no: int) -> Row | None:
        return first(
            run(
                self.client.table("document_pages")
                .select("page_no,content")
                .eq("user_id", user_id)
                .eq("document_id", document_id)
                .eq("page_no", page_no)
                .limit(1)
            )
        )

    def hybrid_search(
        self,
        user_id: str,
        query_text: str,
        embedding: list[float],
        document_ids: list[str] | None,
        k: int,
        candidates: int,
    ) -> list[Row]:
        """Vector + full-text search fused in SQL; scoped to the user's READY documents."""
        data = run(
            self.client.rpc(
                "hybrid_search",
                {
                    "q_text": query_text,
                    "q_emb": embedding,
                    "p_user": user_id,
                    "doc_ids": document_ids,
                    "k": k,
                    "candidates": candidates,
                },
            )
        )
        return rows(data)

    def count_ready_documents(self, user_id: str, document_ids: list[str] | None = None) -> int:
        query = (
            self.client.table("documents").select("id").eq("user_id", user_id).eq("status", "ready")
        )
        if document_ids is not None:
            query = query.in_("id", document_ids)
        return len(rows(run(query)))

    # ---------------------------------------------------------------- accounts
    def profile_exists(self, user_id: str) -> bool:
        data = run(self.client.table("profiles").select("id").eq("id", user_id).limit(1))
        return first(data) is not None

    def get_profile(self, user_id: str) -> Row | None:
        data = run(
            self.client.table("profiles").select("id,name,created_at").eq("id", user_id).limit(1)
        )
        return first(data)

    def questions_used_today(self, user_id: str) -> int:
        today = datetime.now(UTC).date().isoformat()
        data = run(
            self.client.table("usage_daily")
            .select("questions")
            .eq("user_id", user_id)
            .eq("day", today)
            .limit(1)
        )
        row = first(data)
        return int(row["questions"]) if row else 0

    def delete_account(self, user_id: str) -> None:
        """Permanently delete the auth user. Foreign keys cascade to every application table; the
        explicit deletes afterwards are a safety net and make the operation idempotent."""
        for attempt in range(2):
            try:
                self.client.auth.admin.delete_user(user_id)
                break
            except AuthApiError as exc:
                if exc.status == 404 or exc.code == "user_not_found":
                    break  # already gone: still clean up below
                raise AppError(
                    502, "ACCOUNT_DELETE_FAILED", "Could not delete your account. Please try again."
                ) from None
            except (AuthError, httpx.HTTPError):
                if attempt == 1:
                    raise AppError(
                        503,
                        "ACCOUNT_DELETE_FAILED",
                        "Could not delete your account right now. Please try again.",
                    ) from None
        for table, column in (
            ("documents", "user_id"),
            ("conversations", "user_id"),
            ("usage_daily", "user_id"),
            ("profiles", "id"),
        ):
            run(self.client.table(table).delete().eq(column, user_id))
