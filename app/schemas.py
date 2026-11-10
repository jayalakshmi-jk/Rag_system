"""Pydantic request and response models."""

from __future__ import annotations

from typing import Literal

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
