"""Browser tests: real FastAPI app on a socket + fake GoTrue + headless Chromium."""

from __future__ import annotations

import glob
import os
import socket
import threading
import time
from collections.abc import Iterator
from dataclasses import dataclass
from typing import Any

import pytest
import uvicorn
from app.config import Settings
from app.container import Services
from app.main import create_app
from app.repository import Repository
from app.services.chat import ChatService
from app.services.ingest import IngestService
from app.services.limits import RateLimiter
from app.services.retriever import Retriever
from app.tokens import AccountCache, TokenVerifier

from tests.conftest import JWT_SECRET, TEST_SETTINGS
from tests.e2e.fake_gotrue import FakeGoTrue
from tests.fakes.embedder import FakeEmbedder
from tests.fakes.llm import FakeLLM
from tests.fakes.supabase import FakeSupabase

playwright_sync = pytest.importorskip("playwright.sync_api")


@dataclass
class Stack:
    base_url: str
    db: FakeSupabase
    auth: FakeGoTrue
    services: Services
    llm: FakeLLM


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


@pytest.fixture
def stack() -> Iterator[Stack]:
    db = FakeSupabase()
    auth = FakeGoTrue(db, JWT_SECRET)
    auth.start()
    port = free_port()
    base_url = f"http://127.0.0.1:{port}"
    settings = Settings(
        _env_file=None,
        **{
            **TEST_SETTINGS,
            "supabase_url": auth.url,
            "allowed_origins": base_url,
            "supabase_jwt_secret": JWT_SECRET,
        },
    )
    embedder = FakeEmbedder()
    llm = FakeLLM()
    retriever = Retriever(Repository(db), embedder, settings)
    services = Services(
        repo=Repository(db),
        verifier=TokenVerifier(settings),
        limiter=RateLimiter(),
        accounts=AccountCache(settings.account_cache_seconds),
        embedder=embedder,
        ingest=IngestService(Repository(db), embedder, settings),
        retriever=retriever,
        llm=llm,
        chat=ChatService(Repository(db), retriever, llm, settings),
    )
    server = uvicorn.Server(
        uvicorn.Config(
            create_app(settings, services), host="127.0.0.1", port=port, log_level="warning"
        )
    )
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    deadline = time.time() + 10
    while not server.started and time.time() < deadline:
        time.sleep(0.05)
    assert server.started, "test server did not start"
    yield Stack(base_url, db, auth, services, llm)
    server.should_exit = True
    thread.join(timeout=5)
    auth.stop()


@pytest.fixture(scope="session")
def browser() -> Iterator[Any]:
    with playwright_sync.sync_playwright() as pw:
        # CHROMIUM_PATH lets CI or a sandbox point at a pre-installed browser.
        candidates = sorted(glob.glob("/opt/pw-browsers/chromium-*/chrome-linux*/chrome"))
        path = os.environ.get("CHROMIUM_PATH") or (candidates[-1] if candidates else None)
        if path and not os.path.exists(pw.chromium.executable_path):
            instance = pw.chromium.launch(executable_path=path, args=["--no-sandbox"])
        else:
            instance = pw.chromium.launch()
        yield instance
        instance.close()


@pytest.fixture
def page(browser: Any, stack: Stack) -> Iterator[Any]:
    context = browser.new_context(base_url=stack.base_url)
    page = context.new_page()
    page.csp_violations = []
    page.page_errors = []
    page.on(
        "console",
        lambda m: (
            page.csp_violations.append(m.text) if "Content Security Policy" in m.text else None
        ),
    )
    page.on("pageerror", lambda e: page.page_errors.append(str(e)))
    yield page
    context.close()
