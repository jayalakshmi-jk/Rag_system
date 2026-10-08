"""An in-memory stand-in for the supabase-py client, faithful enough to run the real Repository.

It implements the PostgREST query-builder subset the application uses (select / insert / update
/ delete / upsert plus eq, neq, in_, lt, lte, gt, gte, ilike, is_, order, limit), foreign-key
cascades, column defaults, the RPC functions from the migrations, and the auth admin API.

Because the *real* Repository code runs against this fake, tests prove that every query carries
the verified user_id filter. The SQL itself (RLS, functions) is tested separately against a real
Postgres in tests/db.
"""

from __future__ import annotations

import copy
import math
import re
import uuid
from collections.abc import Callable
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any

from postgrest.exceptions import APIError

Row = dict[str, Any]


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _uuid() -> str:
    return str(uuid.uuid4())


COLUMNS: dict[str, dict[str, Callable[[], Any] | None]] = {
    "profiles": {"id": None, "name": None, "created_at": _now},
    "documents": {
        "id": _uuid, "user_id": None, "title": None, "file_name": None,
        "status": lambda: "processing", "error": None, "page_count": None, "file_size": None,
        "mime_type": None, "file_hash": None, "word_count": None,
        "processing_stage": lambda: "uploading", "processing_progress": lambda: 0,
        "processing_started_at": _now, "processing_completed_at": None, "summary": None,
        "created_at": _now,
    },
    "document_pages": {"document_id": None, "user_id": None, "page_no": None, "content": None},
    "chunks": {
        "id": _uuid, "document_id": None, "user_id": None, "chunk_index": None, "page_no": None,
        "section_title": None, "token_count": None, "content": None, "embedding": None,
    },
    "conversations": {
        "id": _uuid, "user_id": None, "title": None, "created_at": _now, "updated_at": _now,
    },
    "messages": {
        "id": _uuid, "conversation_id": None, "role": None, "content": None, "sources": None,
        "created_at": _now,
    },
    "message_feedback": {
        "id": _uuid, "message_id": None, "user_id": None, "rating": None, "reason": None,
        "created_at": _now,
    },
    "usage_daily": {
        "user_id": None, "day": lambda: datetime.now(UTC).date().isoformat(),
        "questions": lambda: 0,
    },
}  # fmt: skip

CASCADES: dict[str, list[tuple[str, str]]] = {
    "documents": [("chunks", "document_id"), ("document_pages", "document_id")],
    "conversations": [("messages", "conversation_id")],
    "messages": [("message_feedback", "message_id")],
}
PRIMARY_KEYS = {"documents": "id", "conversations": "id", "messages": "id", "chunks": "id"}


def _in_ids(column: str, ids: set[Any]) -> Callable[[Row], bool]:
    return lambda row: row.get(column) in ids


def _sort_key(column: str) -> Callable[[Row], tuple[bool, Any]]:
    return lambda row: (row.get(column) is None, row.get(column))


class FakeResponse:
    def __init__(self, data: Any, count: int | None = None) -> None:
        self.data = data
        self.count = count


def api_error(message: str = "boom", code: str = "XX000") -> APIError:
    return APIError({"message": message, "code": code, "hint": None, "details": None})


class FakeSupabase:
    def __init__(self) -> None:
        self.tables: dict[str, list[Row]] = {name: [] for name in COLUMNS}
        self.auth_users: set[str] = set()
        self.auth = SimpleNamespace(admin=_FakeAuthAdmin(self))
        self.queries: list[tuple[str, str, dict[str, Any]]] = []
        self._failures: list[dict[str, Any]] = []
        self.down: BaseException | None = None

    # ------------------------------------------------------------------ test helpers
    def add_user(self, user_id: str | None = None, name: str | None = None) -> str:
        user_id = user_id or _uuid()
        self.auth_users.add(user_id)
        self.tables["profiles"].append({"id": user_id, "name": name, "created_at": _now()})
        return user_id

    def fail(
        self,
        table: str | None = None,
        op: str | None = None,
        error: BaseException | None = None,
        times: int = 1,
    ) -> None:
        """Make the next `times` matching requests raise `error` (default: a PostgREST 500)."""
        self._failures.append(
            {"table": table, "op": op, "error": error or api_error(code="500"), "times": times}
        )

    def rows(self, table: str, **match: Any) -> list[Row]:
        return [r for r in self.tables[table] if all(r.get(k) == v for k, v in match.items())]

    def user_has_data(self, user_id: str) -> bool:
        for name, rows in self.tables.items():
            key = "id" if name == "profiles" else "user_id"
            if any(r.get(key) == user_id for r in rows):
                return True
        return False

    # ------------------------------------------------------------------ client API
    def table(self, name: str) -> FakeQuery:
        return FakeQuery(self, name)

    def rpc(self, name: str, params: dict[str, Any] | None = None) -> _FakeRPC:
        return _FakeRPC(self, name, params or {})

    def _check_failure(self, table: str, op: str) -> None:
        if self.down is not None:
            raise self.down
        for failure in self._failures:
            if (
                failure["times"] > 0
                and failure["table"] in (None, table)
                and failure["op"]
                in (
                    None,
                    op,
                )
            ):
                failure["times"] -= 1
                raise failure["error"]

    # --------------------------------------------------------------------- internals
    def _delete_where(self, table: str, predicate: Callable[[Row], bool]) -> list[Row]:
        removed = [r for r in self.tables[table] if predicate(r)]
        self.tables[table] = [r for r in self.tables[table] if not predicate(r)]
        for child, column in CASCADES.get(table, []):
            ids = {r[PRIMARY_KEYS[table]] for r in removed}
            self._delete_where(child, _in_ids(column, ids))
        return removed

    def _delete_user(self, user_id: str) -> None:
        self.auth_users.discard(user_id)
        self._delete_where("documents", lambda r: r["user_id"] == user_id)
        self._delete_where("conversations", lambda r: r["user_id"] == user_id)
        for name in ("message_feedback", "usage_daily", "document_pages", "chunks"):
            self._delete_where(name, lambda r: r["user_id"] == user_id)
        self._delete_where("profiles", lambda r: r["id"] == user_id)


class _FakeAuthAdmin:
    def __init__(self, db: FakeSupabase) -> None:
        self.db = db
        self.deleted: list[str] = []
        self.fail_with: BaseException | None = None

    def delete_user(self, user_id: str, should_soft_delete: bool = False) -> None:
        if self.fail_with is not None:
            raise self.fail_with
        if user_id not in self.db.auth_users:
            raise api_error("User not found", "404")
        self.deleted.append(user_id)
        self.db._delete_user(user_id)


class FakeQuery:
    def __init__(self, db: FakeSupabase, table: str) -> None:
        self.db = db
        self.name = table
        self.op = "select"
        self.columns = "*"
        self.payload: Any = None
        self.on_conflict: str | None = None
        self.filters: list[tuple[str, str, Any]] = []
        self.order_by: list[tuple[str, bool]] = []
        self.limit_n: int | None = None

    # operations
    def select(self, columns: str = "*", **_: Any) -> FakeQuery:
        self.op, self.columns = "select", columns
        return self

    def insert(self, payload: Any, **_: Any) -> FakeQuery:
        self.op, self.payload = "insert", payload
        return self

    def upsert(self, payload: Any, on_conflict: str | None = None, **_: Any) -> FakeQuery:
        self.op, self.payload, self.on_conflict = "upsert", payload, on_conflict
        return self

    def update(self, payload: Row, **_: Any) -> FakeQuery:
        self.op, self.payload = "update", payload
        return self

    def delete(self, **_: Any) -> FakeQuery:
        self.op = "delete"
        return self

    # filters
    def _f(self, kind: str, column: str, value: Any) -> FakeQuery:
        self.filters.append((kind, column, value))
        return self

    def eq(self, column: str, value: Any) -> FakeQuery:
        return self._f("eq", column, value)

    def neq(self, column: str, value: Any) -> FakeQuery:
        return self._f("neq", column, value)

    def in_(self, column: str, values: list[Any]) -> FakeQuery:
        return self._f("in", column, list(values))

    def lt(self, column: str, value: Any) -> FakeQuery:
        return self._f("lt", column, value)

    def lte(self, column: str, value: Any) -> FakeQuery:
        return self._f("lte", column, value)

    def gt(self, column: str, value: Any) -> FakeQuery:
        return self._f("gt", column, value)

    def gte(self, column: str, value: Any) -> FakeQuery:
        return self._f("gte", column, value)

    def ilike(self, column: str, pattern: str) -> FakeQuery:
        return self._f("ilike", column, pattern)

    def is_(self, column: str, value: Any) -> FakeQuery:
        return self._f("is", column, value)

    def order(self, column: str, desc: bool = False, **_: Any) -> FakeQuery:
        self.order_by.append((column, desc))
        return self

    def limit(self, n: int) -> FakeQuery:
        self.limit_n = n
        return self

    # execution
    def _matches(self, row: Row) -> bool:
        for kind, column, value in self.filters:
            actual = row.get(column)
            if kind == "eq" and actual != value:
                return False
            if kind == "neq" and actual == value:
                return False
            if kind == "in" and actual not in value:
                return False
            if kind == "is" and (actual is None) != (value is None):
                return False
            if kind in {"lt", "lte", "gt", "gte"}:
                if actual is None:
                    return False
                ok = {
                    "lt": actual < value,
                    "lte": actual <= value,
                    "gt": actual > value,
                    "gte": actual >= value,
                }[kind]
                if not ok:
                    return False
            if kind == "ilike":
                regex = "^" + ".*".join(re.escape(p) for p in str(value).split("%")) + "$"
                if actual is None or not re.match(regex, str(actual), re.IGNORECASE | re.DOTALL):
                    return False
        return True

    def _fill(self, row: Row) -> Row:
        spec = COLUMNS[self.name]
        unknown = set(row) - set(spec)
        if unknown:
            raise api_error(f"column {sorted(unknown)[0]} does not exist", "42703")
        full: Row = {}
        for column, default in spec.items():
            full[column] = row[column] if column in row else (default() if default else None)
        return full

    def execute(self) -> FakeResponse:
        db = self.db
        db._check_failure(self.name, self.op)
        db.queries.append(
            (self.name, self.op, {"filters": list(self.filters), "payload": self.payload})
        )
        table = db.tables[self.name]
        if self.op == "select":
            rows = [r for r in table if self._matches(r)]
            for column, desc in reversed(self.order_by):
                rows.sort(key=_sort_key(column), reverse=desc)
            if self.limit_n is not None:
                rows = rows[: self.limit_n]
            if self.columns.strip() != "*":
                wanted = [c.strip() for c in self.columns.split(",")]
                rows = [{c: r.get(c) for c in wanted} for r in rows]
            return FakeResponse(copy.deepcopy(rows), len(rows))
        if self.op in {"insert", "upsert"}:
            payload = self.payload if isinstance(self.payload, list) else [self.payload]
            out: list[Row] = []
            for item in payload:
                full = self._fill(item)
                if self.op == "upsert" and self.on_conflict:
                    keys = [k.strip() for k in self.on_conflict.split(",")]
                    existing = next(
                        (r for r in table if all(r.get(k) == full[k] for k in keys)), None
                    )
                    if existing is not None:
                        existing.update(item)
                        out.append(copy.deepcopy(existing))
                        continue
                table.append(full)
                out.append(copy.deepcopy(full))
            return FakeResponse(out)
        if self.op == "update":
            changed = []
            for row in table:
                if self._matches(row):
                    unknown = set(self.payload) - set(row)
                    if unknown:
                        raise api_error(f"column {sorted(unknown)[0]} does not exist", "42703")
                    row.update(copy.deepcopy(self.payload))
                    changed.append(copy.deepcopy(row))
            return FakeResponse(changed)
        removed = db._delete_where(self.name, self._matches)
        return FakeResponse(copy.deepcopy(removed))


class _FakeRPC:
    def __init__(self, db: FakeSupabase, name: str, params: dict[str, Any]) -> None:
        self.db, self.name, self.params = db, name, params

    def execute(self) -> FakeResponse:
        self.db._check_failure(f"rpc:{self.name}", "rpc")
        self.db.queries.append((f"rpc:{self.name}", "rpc", {"payload": dict(self.params)}))
        handler = getattr(self, f"_rpc_{self.name}", None)
        if handler is None:
            raise api_error(f"function {self.name} does not exist", "42883")
        return FakeResponse(handler(**self.params))

    # -- mirrors of the SQL functions in supabase/migrations -------------------------------
    def _usage_row(self, user: str) -> Row | None:
        today = datetime.now(UTC).date().isoformat()
        return next(
            (
                r
                for r in self.db.tables["usage_daily"]
                if r["user_id"] == user and r["day"] == today
            ),
            None,
        )

    def _rpc_consume_question(self, p_user: str, p_limit: int) -> bool:
        row = self._usage_row(p_user)
        if row is None:
            row = {"user_id": p_user, "day": datetime.now(UTC).date().isoformat(), "questions": 0}
            self.db.tables["usage_daily"].append(row)
        if row["questions"] < p_limit:
            row["questions"] += 1
            return True
        return False

    def _rpc_refund_question(self, p_user: str) -> None:
        row = self._usage_row(p_user)
        if row is not None:
            row["questions"] = max(row["questions"] - 1, 0)

    def _rpc_search_conversations(self, p_user: str, q: str, max_rows: int = 50) -> list[Row]:
        needle = q.lower()
        found = []
        for conv in self.db.tables["conversations"]:
            if conv["user_id"] != p_user:
                continue
            in_title = needle in (conv["title"] or "").lower()
            in_messages = any(
                m["conversation_id"] == conv["id"] and needle in m["content"].lower()
                for m in self.db.tables["messages"]
            )
            if in_title or in_messages:
                found.append(copy.deepcopy(conv))
        found.sort(key=lambda c: c["updated_at"], reverse=True)
        return found[:max_rows]

    def _rpc_hybrid_search(
        self,
        q_text: str,
        q_emb: list[float],
        p_user: str,
        doc_ids: list[str] | None = None,
        k: int = 20,
        candidates: int = 20,
    ) -> list[Row]:
        db = self.db
        eligible = {
            d["id"]: d
            for d in db.tables["documents"]
            if d["user_id"] == p_user
            and d["status"] == "ready"
            and (doc_ids is None or d["id"] in doc_ids)
        }
        pool = [
            c
            for c in db.tables["chunks"]
            if c["user_id"] == p_user and c["document_id"] in eligible
        ]

        def cosine_distance(a: list[float], b: list[float]) -> float:
            dot = sum(x * y for x, y in zip(a, b, strict=False))
            na, nb = math.sqrt(sum(x * x for x in a)), math.sqrt(sum(y * y for y in b))
            return 1.0 if na == 0 or nb == 0 else 1.0 - dot / (na * nb)

        with_vec = [c for c in pool if c["embedding"] is not None]
        sem = sorted(with_vec, key=lambda c: (cosine_distance(c["embedding"], q_emb), c["id"]))
        sem = sem[:candidates]

        words = re.findall(r"\w+", (q_text or "").lower())
        any_mode = " or " in f" {(q_text or '').lower()} "
        terms = [w for w in words if w != "or"]

        def matched(chunk: Row) -> int:
            tokens = set(re.findall(r"\w+", chunk["content"].lower()))
            return sum(1 for t in set(terms) if t in tokens)

        ft = []
        for chunk in pool:
            hits = matched(chunk)
            if terms and ((any_mode and hits > 0) or (not any_mode and hits == len(set(terms)))):
                ft.append((hits, chunk))
        ft.sort(key=lambda pair: (-pair[0], pair[1]["id"]))
        ft = ft[:candidates]

        scores: dict[str, float] = {}
        for rank, chunk in enumerate(sem, start=1):
            scores[chunk["id"]] = scores.get(chunk["id"], 0.0) + 1.0 / (60 + rank)
        for rank, (_, chunk) in enumerate(ft, start=1):
            scores[chunk["id"]] = scores.get(chunk["id"], 0.0) + 1.0 / (60 + rank)

        by_id = {c["id"]: c for c in pool}
        ranked = sorted(
            scores,
            key=lambda cid: (-scores[cid], by_id[cid]["document_id"], by_id[cid]["chunk_index"]),
        )
        return [
            {
                "chunk_id": cid,
                "document_id": by_id[cid]["document_id"],
                "title": eligible[by_id[cid]["document_id"]]["title"],
                "page_no": by_id[cid]["page_no"],
                "section_title": by_id[cid]["section_title"],
                "chunk_index": by_id[cid]["chunk_index"],
                "content": by_id[cid]["content"],
                "score": scores[cid],
            }
            for cid in ranked[:k]
        ]
