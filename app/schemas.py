"""Pydantic request and response models."""

from __future__ import annotations

import uuid
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator


class Quota(BaseModel):
    daily_limit: int
    used_today: int
    remaining: int


class ClientLimits(BaseModel):
    max_upload_mb: int
    max_question_length: int


class MeResponse(BaseModel):
    id: str
    email: str | None
    name: str | None
    created_at: str | None
    quota: Quota
    limits: ClientLimits


class DeleteAccountRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    confirm: Literal["DELETE"]


class DocumentOut(BaseModel):
    id: str
    title: str
    file_name: str | None = None
    status: Literal["processing", "ready", "failed"]
    error: str | None = None
    page_count: int | None = None
    file_size: int | None = None
    word_count: int | None = None
    processing_stage: str
    processing_progress: int
    processing_started_at: str | None = None
    processing_completed_at: str | None = None
    created_at: str


class DocumentList(BaseModel):
    documents: list[DocumentOut]


class RenameDocumentRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    title: str = Field(min_length=1, max_length=200)

    @field_validator("title")
    @classmethod
    def _strip(cls, value: str) -> str:
        value = " ".join(value.split())
        if not value:
            raise ValueError("title must not be blank")
        return value


class PageOut(BaseModel):
    page_no: int
    page_count: int | None
    content: str


class ChatBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    message: str
    conversation_id: uuid.UUID | None = None
    document_ids: list[uuid.UUID] | None = Field(default=None, max_length=50)
    language: Literal["auto", "en", "ta", "tanglish"] = "auto"
    style: Literal["concise", "balanced", "detailed"] = "balanced"

    @field_validator("message")
    @classmethod
    def _not_blank(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("message must not be blank")
        return value


class ConversationOut(BaseModel):
    id: str
    title: str | None
    created_at: str
    updated_at: str


class ConversationList(BaseModel):
    conversations: list[ConversationOut]


class MessageOut(BaseModel):
    id: str
    role: Literal["user", "assistant"]
    content: str
    sources: list[dict[str, Any]] | None = None
    created_at: str
    feedback: Literal["up", "down"] | None = None


class ConversationDetail(ConversationOut):
    messages: list[MessageOut]


class RenameConversationRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    title: str = Field(min_length=1, max_length=120)

    @field_validator("title")
    @classmethod
    def _strip(cls, value: str) -> str:
        value = " ".join(value.split())
        if not value:
            raise ValueError("title must not be blank")
        return value


FeedbackReason = Literal[
    "incorrect_answer",
    "wrong_citation",
    "missing_information",
    "too_long",
    "didnt_understand",
    "other",
]


class FeedbackRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    rating: Literal["up", "down"]
    reason: FeedbackReason | None = None

    @field_validator("reason")
    @classmethod
    def _only_with_down(cls, value: str | None, info: Any) -> str | None:
        if value is not None and info.data.get("rating") == "up":
            raise ValueError("reason is only allowed with a down rating")
        return value
