"""The signed-in user: profile, today's quota, and account deletion."""

from __future__ import annotations

import logging

from fastapi import APIRouter, Response

from app.auth import CurrentUser, rate_limited
from app.deps import ServicesDep, SettingsDep
from app.logging import log_event
from app.schemas import ClientLimits, DeleteAccountRequest, MeResponse, Quota

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/me", tags=["me"])


@router.get("", response_model=MeResponse)
def get_me(user: CurrentUser, settings: SettingsDep, services: ServicesDep) -> MeResponse:
    profile = services.repo.get_profile(user.id) or {}
    used = services.repo.questions_used_today(user.id)
    limit = settings.daily_question_limit
    return MeResponse(
        id=user.id,
        email=user.email,
        name=profile.get("name"),
        created_at=profile.get("created_at"),
        quota=Quota(daily_limit=limit, used_today=used, remaining=max(limit - used, 0)),
        limits=ClientLimits(
            max_upload_mb=settings.max_upload_mb, max_question_length=settings.max_question_length
        ),
    )


@router.delete(
    "",
    status_code=204,
    dependencies=[rate_limited("account", "rate_limit_account_per_hour", 3600)],
)
def delete_me(body: DeleteAccountRequest, user: CurrentUser, services: ServicesDep) -> Response:
    """Delete the account and all data. The body must be {"confirm": "DELETE"}."""
    del body  # validated by the schema; only its presence matters
    services.repo.delete_account(user.id)
    services.accounts.forget(user.id)
    log_event(logger, "account_deleted", user_id=user.id)
    return Response(status_code=204)
