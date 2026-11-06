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
from app.errors import AppError

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
