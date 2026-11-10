"""Service container: constructs and holds long-lived clients and services."""

from __future__ import annotations

from dataclasses import dataclass

from app.config import Settings
from app.db import create_supabase
from app.repository import Repository
from app.services.chat import ChatService
from app.services.embedder import Embedder, VoyageEmbedder
from app.services.ingest import IngestService
from app.services.limits import RateLimiter
from app.services.llm import LLM, AnthropicLLM
from app.services.retriever import Retriever
from app.tokens import AccountCache, TokenVerifier


@dataclass
class Services:
    repo: Repository
    verifier: TokenVerifier
    limiter: RateLimiter
    accounts: AccountCache
    embedder: Embedder
    ingest: IngestService
    retriever: Retriever
    llm: LLM
    chat: ChatService

    async def aclose(self) -> None:
        """Release network resources on shutdown."""


def build_services(settings: Settings) -> Services:
    repo = Repository(create_supabase(settings))
    embedder = VoyageEmbedder(settings)
    retriever = Retriever(repo, embedder, settings)
    llm = AnthropicLLM(settings)
    return Services(
        repo=repo,
        verifier=TokenVerifier(settings),
        limiter=RateLimiter(),
        accounts=AccountCache(settings.account_cache_seconds),
        embedder=embedder,
        ingest=IngestService(repo, embedder, settings),
        retriever=retriever,
        llm=llm,
        chat=ChatService(repo, retriever, llm, settings),
    )
