"""Chat orchestration: quota, retrieval, grounded generation, persistence, streaming events.

Events yielded by :meth:`ChatService.stream` (the route turns them into Server-Sent Events):
``meta`` -> ``sources`` -> ``token``* -> ``suggestions``? -> ``done``, or ``error`` at any point.

Quota rule: a question is charged up front (atomically) and refunded whenever the request fails
before the model produced any output, so provider outages never burn a user's allowance.
"""

from __future__ import annotations

import logging
import re
import time
from collections.abc import Iterator
from dataclasses import dataclass
from typing import Any

from app.config import Settings
from app.errors import AppError, NotFoundError, ProviderError
from app.logging import log_event
from app.repository import Repository
from app.services import prompts
from app.services.llm import LLM, Usage
from app.services.prompts import Language, Resolved, Style
from app.services.retriever import Retriever, Source

logger = logging.getLogger(__name__)

Event = tuple[str, dict[str, Any]]
_CITE_STRIP_RE = re.compile(r"\s*\[\d{1,2}\]")
_SNIPPET_CHARS = 400
GENERIC_ERROR = "Something went wrong while answering. Please try again."


@dataclass(frozen=True)
class ChatRequest:
    question: str
    conversation_id: str | None
    document_ids: list[str] | None
    language: Language
    style: Style


@dataclass
class PreparedChat:
    user_id: str
    request: ChatRequest
    conversation: dict[str, Any] | None
    smalltalk: str | None
    resolved: Resolved
    charged: bool


def source_payload(number: int, source: Source) -> dict[str, Any]:
    return {
        "n": number,
        "document_id": source.document_id,
        "title": source.title,
        "page_no": source.page_no,
        "section_title": source.section_title,
        "snippet": source.content[:_SNIPPET_CHARS],
    }


def _llm_history(rows: list[dict[str, Any]]) -> list[dict[str, str]]:
    """Alternating user/assistant turns starting with a user turn; citations stripped."""
    turns: list[dict[str, str]] = []
    for row in rows:
        text = str(row["content"])
        if row["role"] == "assistant":
            text = _CITE_STRIP_RE.sub("", text)
        text = text[:1500]
        if turns and turns[-1]["role"] == row["role"]:
            turns[-1]["content"] += "\n" + text
        else:
            turns.append({"role": str(row["role"]), "content": text})
    while turns and turns[0]["role"] != "user":
        turns.pop(0)
    return turns


class ChatService:
    def __init__(
        self, repo: Repository, retriever: Retriever, llm: LLM, settings: Settings
    ) -> None:
        self._repo = repo
        self._retriever = retriever
        self._llm = llm
        self._settings = settings

    # ----------------------------------------------------------------- prepare
    def prepare(self, user_id: str, request: ChatRequest) -> PreparedChat:
        """Validate and charge the question. Raises AppError; nothing has been streamed yet."""
        conversation = None
        if request.conversation_id:
            conversation = self._repo.get_conversation(user_id, request.conversation_id)
            if conversation is None:
                raise NotFoundError("Conversation")

        ids = list(dict.fromkeys(request.document_ids)) if request.document_ids else None
        if ids is not None:
            if self._repo.count_ready_documents(user_id, ids) != len(ids):
                raise AppError(
                    404,
                    "DOCUMENT_NOT_AVAILABLE",
                    "A selected document was not found or is still processing.",
                )
        elif self._repo.count_ready_documents(user_id) == 0:
            raise AppError(
                409,
                "NO_READY_DOCUMENTS",
                "Upload a document and wait until it is ready before asking questions.",
            )

        resolved = prompts.resolve_language(request.language, request.question)
        smalltalk = prompts.smalltalk_kind(request.question)
        charged = False
        if smalltalk is None:
            limit = self._settings.daily_question_limit
            if not self._repo.consume_question(user_id, limit):
                raise AppError(
                    429,
                    "QUOTA_EXCEEDED",
                    f"You've used all {limit} questions for today. "
                    "Your quota resets at midnight UTC.",
                )
            charged = True
        return PreparedChat(user_id, request, conversation, smalltalk, resolved, charged)

    # ------------------------------------------------------------------ stream
    def stream(self, prepared: PreparedChat) -> Iterator[Event]:
        started = time.monotonic()
        user_id, request = prepared.user_id, prepared.request
        repo = self._repo
        refund_pending = prepared.charged
        conversation = prepared.conversation
        new_conversation = False
        user_message: dict[str, Any] | None = None
        answer_parts: list[str] = []
        usage = Usage()
        completed = False
        status = "error"
        try:
            history_rows = (
                repo.recent_messages(str(conversation["id"]), self._settings.history_messages)
                if conversation
                else []
            )
            if conversation is None:
                conversation = repo.create_conversation(
                    user_id, prompts.fallback_title(request.question)
                )
                new_conversation = True
            conversation_id = str(conversation["id"])
            user_message = repo.add_message(conversation_id, "user", request.question)
            yield (
                "meta",
                {"conversation_id": conversation_id, "user_message_id": user_message["id"]},
            )

            if prepared.smalltalk:
                replies = (
                    prompts.THANKS_REPLY
                    if prepared.smalltalk == "thanks"
                    else prompts.GREETING_REPLY
                )
                text = replies[prepared.resolved]
                yield "sources", {"sources": []}
                yield "token", {"text": text}
                assistant = repo.add_message(conversation_id, "assistant", text, [])
                completed, status = True, "smalltalk"
                yield "done", self._done(user_id, assistant, conversation, [], None, prepared)
                return

            history = _llm_history(history_rows)
            query = self._standalone_query(history, request.question)
            sources = self._retriever.search(user_id, query, request.document_ids)
            yield "sources", {"sources": [source_payload(i, s) for i, s in enumerate(sources, 1)]}

            if not sources:
                text = prompts.NO_ANSWER[prepared.resolved]
                yield "token", {"text": text}
                assistant = repo.add_message(conversation_id, "assistant", text, [])
                self._refund(user_id)
                refund_pending = False
                completed, status = True, "no_sources"
                yield "done", self._done(user_id, assistant, conversation, [], None, prepared)
                return

            grounded = prompts.build_user_message(request.question, sources)
            if history and history[-1]["role"] == "user":  # a dangling turn from a failed run
                messages = [
                    *history[:-1],
                    {"role": "user", "content": f"{history[-1]['content']}\n\n{grounded}"},
                ]
            else:
                messages = [*history, {"role": "user", "content": grounded}]
            system = prompts.build_system_prompt(request.language, request.style, prepared.resolved)
            for piece in self._llm.stream_answer(
                system, messages, self._settings.max_output_tokens, usage
            ):
                if not piece:
                    continue
                refund_pending = False  # the model has produced output: the question is spent
                answer_parts.append(piece)
                yield "token", {"text": piece}

            answer = "".join(answer_parts).strip()
            if not answer:
                raise ProviderError("anthropic", "The AI service returned an empty answer.")
            no_answer = prompts.is_no_answer(answer)
            numbers = [] if no_answer else prompts.cited_numbers(answer, len(sources))
            cited = [source_payload(n, sources[n - 1]) for n in numbers]
            assistant = repo.add_message(conversation_id, "assistant", answer, cited)
            completed, status = True, "no_answer" if no_answer else "answered"

            title = self._title(user_id, conversation_id, request.question, new_conversation)
            suggestions = [] if no_answer else self._suggestions(request.question, answer)
            if suggestions:
                yield "suggestions", {"suggestions": suggestions}
            yield "done", self._done(user_id, assistant, conversation, numbers, title, prepared)
        except GeneratorExit:
            status = "client_disconnected"
            raise
        except Exception as exc:
            code, message = self._describe(exc)
            log_event(
                logger,
                "chat_failed",
                level=logging.WARNING,
                user_id=user_id,
                error=type(exc).__name__,
                code=code,
            )
            yield "error", {"code": code, "message": message}
        finally:
            if not completed:
                self._abandon(
                    prepared,
                    conversation,
                    new_conversation,
                    user_message,
                    answer_parts,
                    refund_pending,
                )
            log_event(
                logger,
                "chat_completed",
                user_id=user_id,
                status=status,
                latency_ms=int((time.monotonic() - started) * 1000),
                input_tokens=usage.input_tokens,
                output_tokens=usage.output_tokens,
                language=prepared.resolved,
                style=request.style,
                scoped=request.document_ids is not None,
            )

    # ----------------------------------------------------------------- helpers
    def _done(
        self,
        user_id: str,
        assistant: dict[str, Any],
        conversation: dict[str, Any],
        numbers: list[int],
        title: str | None,
        prepared: PreparedChat,
    ) -> dict[str, Any]:
        limit = self._settings.daily_question_limit
        try:
            used = self._repo.questions_used_today(user_id)
        except Exception:
            used = limit
        return {
            "message_id": assistant["id"],
            "conversation_id": conversation["id"],
            "cited": numbers,
            "title": title,
            "language": prepared.resolved,
            "quota": {"daily_limit": limit, "remaining": max(limit - used, 0)},
        }

    def _standalone_query(self, history: list[dict[str, str]], question: str) -> str:
        if not history:
            return question
        try:
            pairs = [(t["role"], t["content"]) for t in history]
            rewritten = self._llm.complete_fast(
                prompts.CONDENSE_SYSTEM, prompts.condense_prompt(pairs, question), 150
            )
        except Exception:
            return question  # retrieval simply uses the raw question
        rewritten = " ".join(rewritten.split())
        return rewritten[:500] if rewritten else question

    def _title(self, user_id: str, conversation_id: str, question: str, is_new: bool) -> str | None:
        if not is_new:
            return None
        fallback = prompts.fallback_title(question)
        try:
            raw = self._llm.complete_fast(prompts.TITLE_SYSTEM, question[:500], 30)
            title = prompts.clean_title(raw, fallback)
            self._repo.update_conversation_title(user_id, conversation_id, title)
            return title
        except Exception:
            return fallback

    def _suggestions(self, question: str, answer: str) -> list[str]:
        if not self._settings.suggestions_enabled:
            return []
        try:
            raw = self._llm.complete_fast(
                prompts.SUGGEST_SYSTEM,
                f"Question: {question[:500]}\nAnswer: {_CITE_STRIP_RE.sub('', answer)[:1200]}",
                150,
            )
            return prompts.parse_suggestions(raw)
        except Exception:
            return []

    def _refund(self, user_id: str) -> None:
        try:
            self._repo.refund_question(user_id)
        except Exception:
            log_event(logger, "refund_failed", level=logging.ERROR, user_id=user_id)

    def _abandon(
        self,
        prepared: PreparedChat,
        conversation: dict[str, Any] | None,
        new_conversation: bool,
        user_message: dict[str, Any] | None,
        answer_parts: list[str],
        refund_pending: bool,
    ) -> None:
        """Clean up after a failed or interrupted request (best effort, never raises)."""
        repo, user_id = self._repo, prepared.user_id
        try:
            partial = "".join(answer_parts).strip()
            if conversation is not None and partial:
                # Keep what the user already saw (e.g. they pressed Stop).
                repo.add_message(str(conversation["id"]), "assistant", partial, [])
            elif conversation is not None:
                if new_conversation:
                    repo.delete_conversation(user_id, str(conversation["id"]))
                elif user_message is not None:
                    repo.delete_message(str(conversation["id"]), str(user_message["id"]))
            if refund_pending:
                self._refund(user_id)
        except Exception:
            log_event(logger, "chat_cleanup_failed", level=logging.ERROR, user_id=user_id)

    @staticmethod
    def _describe(exc: Exception) -> tuple[str, str]:
        if isinstance(exc, AppError):
            return exc.code, exc.message
        return "CHAT_FAILED", GENERIC_ERROR
