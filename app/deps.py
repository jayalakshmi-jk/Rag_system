"""Shared FastAPI dependencies."""

from __future__ import annotations

from typing import Annotated, cast

from fastapi import Depends, Request

from app.config import Settings
from app.container import Services


def get_settings(request: Request) -> Settings:
    return cast(Settings, request.app.state.settings)


def get_services(request: Request) -> Services:
    return cast(Services, request.app.state.services)


SettingsDep = Annotated[Settings, Depends(get_settings)]
ServicesDep = Annotated[Services, Depends(get_services)]
