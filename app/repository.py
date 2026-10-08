"""Data access layer.

Every method that touches user-owned data takes the *verified* ``user_id`` and filters on it
explicitly. The backend uses the service role key (which bypasses Row Level Security), so these
filters are the primary isolation boundary; RLS is the second layer for any direct client access.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any, Protocol

from app.db import run

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
