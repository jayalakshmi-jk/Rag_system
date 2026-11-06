"""Pydantic request and response models."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict


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
