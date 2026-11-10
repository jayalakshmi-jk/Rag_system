"""Conversation history: list, search, open, rename, delete, and answer feedback."""

from __future__ import annotations

import uuid
from typing import Annotated

from fastapi import APIRouter, Query, Response

from app.auth import CurrentUser
from app.deps import ServicesDep
from app.errors import NotFoundError
from app.schemas import (
    ConversationDetail,
    ConversationList,
    ConversationOut,
    FeedbackRequest,
    MessageOut,
    RenameConversationRequest,
)

router = APIRouter(tags=["conversations"])


@router.get("/conversations", response_model=ConversationList)
def list_conversations(
    user: CurrentUser,
    services: ServicesDep,
    q: Annotated[str | None, Query(max_length=100)] = None,
) -> ConversationList:
    text = (q or "").strip()
    found = (
        services.repo.search_conversations(user.id, text)
        if text
        else services.repo.list_conversations(user.id)
    )
    return ConversationList(conversations=[ConversationOut.model_validate(c) for c in found])


@router.get("/conversations/{conversation_id}", response_model=ConversationDetail)
def get_conversation(
    conversation_id: uuid.UUID, user: CurrentUser, services: ServicesDep
) -> ConversationDetail:
    conversation = services.repo.get_conversation(user.id, str(conversation_id))
    if conversation is None:
        raise NotFoundError("Conversation")
    messages = services.repo.list_messages(str(conversation_id))
    ratings = services.repo.feedback_for(user.id, [str(m["id"]) for m in messages])
    return ConversationDetail(
        **conversation,
        messages=[MessageOut(**m, feedback=ratings.get(str(m["id"]))) for m in messages],
    )


@router.patch("/conversations/{conversation_id}", response_model=ConversationOut)
def rename_conversation(
    conversation_id: uuid.UUID,
    body: RenameConversationRequest,
    user: CurrentUser,
    services: ServicesDep,
) -> ConversationOut:
    if not services.repo.rename_conversation(user.id, str(conversation_id), body.title):
        raise NotFoundError("Conversation")
    conversation = services.repo.get_conversation(user.id, str(conversation_id))
    if conversation is None:
        raise NotFoundError("Conversation")
    return ConversationOut.model_validate(conversation)


@router.delete("/conversations/{conversation_id}", status_code=204)
def delete_conversation(
    conversation_id: uuid.UUID, user: CurrentUser, services: ServicesDep
) -> Response:
    if not services.repo.delete_conversation(user.id, str(conversation_id)):
        raise NotFoundError("Conversation")
    return Response(status_code=204)


@router.put("/messages/{message_id}/feedback", status_code=204)
def set_feedback(
    message_id: uuid.UUID, body: FeedbackRequest, user: CurrentUser, services: ServicesDep
) -> Response:
    if services.repo.get_assistant_message_owner(user.id, str(message_id)) is None:
        raise NotFoundError("Message")
    services.repo.set_feedback(user.id, str(message_id), body.rating, body.reason)
    return Response(status_code=204)


@router.delete("/messages/{message_id}/feedback", status_code=204)
def clear_feedback(message_id: uuid.UUID, user: CurrentUser, services: ServicesDep) -> Response:
    if services.repo.get_assistant_message_owner(user.id, str(message_id)) is None:
        raise NotFoundError("Message")
    services.repo.clear_feedback(user.id, str(message_id))
    return Response(status_code=204)
