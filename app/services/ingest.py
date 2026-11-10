"""Document ingestion: parse -> chunk -> embed -> index, with visible progress.

A document is never left in ``processing``: every path ends in ``ready`` or ``failed``.
Failure messages are user-safe; exception text and document content are never logged.
"""

from __future__ import annotations

import logging
import threading
import time
from collections.abc import Callable

from app.config import Settings
from app.errors import AppError
from app.logging import log_event
from app.repository import Repository
from app.services.chunker import chunk_pages
from app.services.embedder import Embedder
from app.services.parser import parse_document

logger = logging.getLogger(__name__)

GENERIC_FAILURE = "Processing failed unexpectedly. Please try uploading the document again."
TIMEOUT_FAILURE = "Processing took too long and was stopped. Try a smaller document."


class _DeadlineError(Exception):
    pass


class IngestService:
    def __init__(
        self,
        repo: Repository,
        embedder: Embedder,
        settings: Settings,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._clock = clock
        self._repo = repo
        self._embedder = embedder
        self._settings = settings
        self._slots = threading.BoundedSemaphore(settings.ingest_concurrency)

    def run(self, user_id: str, document_id: str, data: bytes, extension: str) -> None:
        """Process one uploaded file. Safe to call from a background thread; never raises."""
        started = self._clock()
        deadline = started + self._settings.ingest_timeout_seconds
        try:
            with self._slots:
                self._process(user_id, document_id, data, extension, deadline)
            log_event(
                logger,
                "ingest_done",
                user_id=user_id,
                document_id=document_id,
                seconds=round(self._clock() - started, 2),
            )
        except Exception as exc:
            self._fail(user_id, document_id, exc)

    # ---------------------------------------------------------------- pipeline
    def _check(self, deadline: float) -> None:
        if self._clock() > deadline:
            raise _DeadlineError

    def _process(
        self, user_id: str, document_id: str, data: bytes, extension: str, deadline: float
    ) -> None:
        repo = self._repo
        repo.set_document_progress(user_id, document_id, "parsing", 5)
        pages = parse_document(data, extension, max_pages=self._settings.max_document_pages)
        self._check(deadline)

        repo.set_document_progress(user_id, document_id, "chunking", 20)
        chunks = chunk_pages(pages)
        if not chunks:
            raise AppError(422, "NO_EXTRACTABLE_TEXT", "No readable text was found.")
        self._check(deadline)

        repo.set_document_progress(user_id, document_id, "embedding", 30)

        def on_batch(done: int, total: int) -> None:
            self._check(deadline)
            repo.set_document_progress(user_id, document_id, "embedding", 30 + 55 * done // total)

        vectors = self._embedder.embed_documents([c.content for c in chunks], on_batch)
        self._check(deadline)

        repo.set_document_progress(user_id, document_id, "indexing", 88)
        repo.clear_document_content(user_id, document_id)  # idempotent re-runs
        repo.save_pages(user_id, document_id, [(p.page_no, p.text) for p in pages])
        repo.set_document_progress(user_id, document_id, "indexing", 93)
        repo.save_chunks(
            user_id,
            document_id,
            [
                {
                    "chunk_index": c.index,
                    "page_no": c.page_no,
                    "section_title": c.section_title,
                    "token_count": c.token_count,
                    "content": c.content,
                    "embedding": vector,
                }
                for c, vector in zip(chunks, vectors, strict=True)
            ],
        )
        words = sum(len(p.text.split()) for p in pages)
        repo.complete_document(user_id, document_id, page_count=len(pages), word_count=words)

    def _fail(self, user_id: str, document_id: str, exc: Exception) -> None:
        if isinstance(exc, _DeadlineError):
            message = TIMEOUT_FAILURE
        elif isinstance(exc, AppError):
            message = exc.message
        else:
            message = GENERIC_FAILURE
        log_event(
            logger,
            "ingest_failed",
            level=logging.WARNING,
            user_id=user_id,
            document_id=document_id,
            error=type(exc).__name__,
        )
        try:
            self._repo.clear_document_content(user_id, document_id)
        except Exception:
            log_event(logger, "ingest_cleanup_failed", level=logging.ERROR, document_id=document_id)
        try:
            self._repo.fail_document(user_id, document_id, message)
        except Exception:
            # Last resort: startup recovery marks stale 'processing' rows as failed.
            log_event(
                logger, "ingest_fail_mark_failed", level=logging.ERROR, document_id=document_id
            )
