"""Retrieval: embed the question, run hybrid search, rerank, and fit the context budget.

Everything is scoped to one user's READY documents inside the database function; this layer
adds query shaping and a rerank hook (``Reranker``) that is a no-op until a reranker is added.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from typing import Any, Protocol

from app.config import Settings
from app.logging import log_event
from app.repository import Repository
from app.services.chunker import estimate_tokens
from app.services.embedder import Embedder

logger = logging.getLogger(__name__)

_WORD_RE = re.compile(r"[\w஀-௿]+", re.UNICODE)
_STOPWORD_TEXT = (
    "a an the of to in on at for from by with and or but is are was were be been being "
    "do does did what which who whom whose when where why how this that these those it its "
    "as if then than about into over under can could should would will shall may might "
    "me my you your we our they their i he she him her us them please tell explain give "
    "show list enna epdi yaar enga eppo idhu adhu oru "
    "என்ன என்றால் எது எப்படி ஏன் யார் எங்கே எப்போது இது அது ஒரு"
)
_STOPWORDS = frozenset(_STOPWORD_TEXT.split())
MAX_QUERY_TERMS = 12


@dataclass(frozen=True)
class Source:
    chunk_id: str
    document_id: str
    title: str
    page_no: int | None
    section_title: str | None
    chunk_index: int
    content: str
    score: float

    @classmethod
    def from_row(cls, row: dict[str, Any]) -> Source:
        return cls(
            chunk_id=str(row["chunk_id"]),
            document_id=str(row["document_id"]),
            title=str(row["title"]),
            page_no=row.get("page_no"),
            section_title=row.get("section_title"),
            chunk_index=int(row["chunk_index"]),
            content=str(row["content"]),
            score=float(row["score"]),
        )


class Reranker(Protocol):
    """Plug a cross-encoder here later; receives the fused candidates, returns them reordered."""

    def rerank(self, query: str, candidates: list[Source]) -> list[Source]: ...


class NoopReranker:
    def rerank(self, query: str, candidates: list[Source]) -> list[Source]:
        return candidates


def keyword_query(question: str) -> str:
    """Natural-language question -> OR-joined significant terms for full-text search.

    The database parses this with ``websearch_to_tsquery`` where ``or`` means any-of, which is
    far better for recall than requiring every word of a sentence to appear in one chunk.
    """
    seen: list[str] = []
    for word in _WORD_RE.findall(question.lower()):
        if word in _STOPWORDS or (len(word) < 2 and word.isascii()):
            continue
        if word not in seen:
            seen.append(word)
    return " or ".join(seen[:MAX_QUERY_TERMS])


class Retriever:
    def __init__(
        self,
        repo: Repository,
        embedder: Embedder,
        settings: Settings,
        reranker: Reranker | None = None,
    ) -> None:
        self._repo = repo
        self._embedder = embedder
        self._settings = settings
        self._reranker = reranker or NoopReranker()

    def has_documents(self, user_id: str, document_ids: list[str] | None = None) -> bool:
        return self._repo.count_ready_documents(user_id, document_ids) > 0

    def search(
        self, user_id: str, question: str, document_ids: list[str] | None = None
    ) -> list[Source]:
        settings = self._settings
        vector = self._embedder.embed_query(question)
        rows = self._repo.hybrid_search(
            user_id,
            keyword_query(question),
            vector,
            document_ids,
            settings.retrieval_candidates,
            settings.retrieval_candidates,
        )
        candidates = [Source.from_row(r) for r in rows]
        ranked = self._reranker.rerank(question, candidates)
        chosen = self._fit_budget(ranked)
        log_event(
            logger,
            "retrieval",
            user_id=user_id,
            candidates=len(candidates),
            chosen=len(chosen),
            scoped=document_ids is not None,
        )
        return chosen

    def _fit_budget(self, ranked: list[Source]) -> list[Source]:
        """Keep the best chunks up to the chunk count and context-token budget."""
        chosen: list[Source] = []
        used = 0
        for source in ranked:
            cost = estimate_tokens(source.content)
            if chosen and used + cost > self._settings.max_context_tokens:
                continue
            chosen.append(source)
            used += cost
            if len(chosen) >= self._settings.context_chunks:
                break
        return chosen
