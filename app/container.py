"""Service container: constructs and holds long-lived clients and services."""

from __future__ import annotations

from dataclasses import dataclass

from app.config import Settings
from app.db import create_supabase
from app.repository import Repository


@dataclass
class Services:
    repo: Repository

    async def aclose(self) -> None:
        """Release network resources on shutdown."""


def build_services(settings: Settings) -> Services:
    return Services(repo=Repository(create_supabase(settings)))
