"""POST /api/chat: grounded answers streamed as Server-Sent Events."""

from __future__ import annotations

import json
from collections.abc import Iterator

from fastapi import APIRouter
from fastapi.responses import StreamingResponse

from app.auth import CurrentUser, rate_limited
from app.deps import ServicesDep, SettingsDep
from app.errors import AppError
from app.schemas import ChatBody
from app.services.chat import ChatRequest

router = APIRouter(prefix="/chat", tags=["chat"])


def _sse(event: str, data: dict[str, object]) -> str:
    return f"event: {event}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n"


@router.post("", dependencies=[rate_limited("chat", "rate_limit_chat_per_minute", 60)])
def chat(
    body: ChatBody, user: CurrentUser, settings: SettingsDep, services: ServicesDep
) -> StreamingResponse:
    if len(body.message) > settings.max_question_length:
        raise AppError(
            422,
            "QUESTION_TOO_LONG",
            f"Questions can be at most {settings.max_question_length} characters.",
        )
    request = ChatRequest(
        question=body.message,
        conversation_id=str(body.conversation_id) if body.conversation_id else None,
        document_ids=[str(d) for d in body.document_ids] if body.document_ids else None,
        language=body.language,
        style=body.style,
    )
    prepared = services.chat.prepare(user.id, request)  # raises plain HTTP errors

    def events() -> Iterator[str]:
        for name, data in services.chat.stream(prepared):
            yield _sse(name, data)

    return StreamingResponse(
        events(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-store",
            "X-Accel-Buffering": "no",
            "Connection": "keep-alive",
        },
    )
