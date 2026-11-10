from __future__ import annotations

from collections.abc import Callable

import pytest
from app.config import Settings
from app.container import Services
from app.services.retriever import NoopReranker, Retriever, Source, keyword_query
from fastapi.testclient import TestClient

from tests.conftest import TestUser
from tests.fakes.embedder import FakeEmbedder
from tests.fakes.supabase import FakeSupabase


def test_keyword_query_drops_stopwords_and_joins_with_or() -> None:
    assert keyword_query("What is the capital of France?") == "capital or france"
    assert keyword_query("explain photosynthesis, photosynthesis!") == "photosynthesis"


def test_keyword_query_handles_tanglish_tamil_and_empty() -> None:
    assert keyword_query("idhu enna refund policy") == "refund or policy"
    assert keyword_query("ஒளிச்சேர்க்கை என்றால் என்ன?") == "ஒளிச்சேர்க்கை"
    assert keyword_query("?? !!") == ""
    assert keyword_query("") == ""


def test_keyword_query_is_bounded_and_cannot_inject_operators() -> None:
    long = " ".join(f"term{i}" for i in range(50))
    assert keyword_query(long).count(" or ") == 11
    shaped = keyword_query('refund -policy "exact phrase" OR (admin) & !x')
    assert all(ch not in shaped for ch in '"-()&!')


def seed(
    services: Services,
    fake_db: FakeSupabase,
    user_id: str,
    title: str,
    texts: list[str],
    status: str = "ready",
) -> str:
    doc = (
        fake_db.table("documents")
        .insert({"user_id": user_id, "title": title, "status": status})
        .execute()
        .data[0]
    )
    embedder = services.embedder
    vectors = embedder.embed_documents(texts)
    for index, (text, vector) in enumerate(zip(texts, vectors, strict=True)):
        fake_db.table("chunks").insert(
            {
                "document_id": doc["id"], "user_id": user_id, "chunk_index": index,
                "page_no": index + 1, "content": text, "embedding": vector,
            }
        ).execute()  # fmt: skip
    return str(doc["id"])


def test_search_finds_relevant_chunks_with_citation_data(
    services: Services, fake_db: FakeSupabase, user: TestUser
) -> None:
    doc_id = seed(
        services, fake_db, user.id, "Biology",
        ["Photosynthesis converts light into chemical energy in chloroplasts.",
         "The mitochondria produce ATP for the cell.",
         "Databases store tables and rows."],
    )  # fmt: skip
    results = services.retriever.search(user.id, "How does photosynthesis work?")
    assert results[0].content.startswith("Photosynthesis")
    top = results[0]
    assert (top.document_id, top.title, top.page_no) == (doc_id, "Biology", 1)
    assert isinstance(top, Source)
    assert top.score > 0


def test_search_never_returns_another_users_content(
    services: Services, fake_db: FakeSupabase, new_user: Callable[..., TestUser]
) -> None:
    alice, mallory = new_user(name="Alice"), new_user(name="Mallory")
    seed(services, fake_db, alice.id, "Secrets", ["The vault password is swordfish."])
    assert services.retriever.search(mallory.id, "vault password swordfish") == []
    assert len(services.retriever.search(alice.id, "vault password swordfish")) == 1


def test_search_ignores_documents_that_are_not_ready(
    services: Services, fake_db: FakeSupabase, user: TestUser
) -> None:
    seed(
        services,
        fake_db,
        user.id,
        "Half done",
        ["unfinished indexing content"],
        status="processing",
    )
    seed(services, fake_db, user.id, "Broken", ["failed parsing content"], status="failed")
    assert services.retriever.search(user.id, "content") == []
    assert not services.retriever.has_documents(user.id)


def test_search_can_be_limited_to_selected_documents(
    services: Services, fake_db: FakeSupabase, user: TestUser
) -> None:
    one = seed(services, fake_db, user.id, "One", ["alpha topic details"])
    two = seed(services, fake_db, user.id, "Two", ["alpha topic other details"])
    only_two = services.retriever.search(user.id, "alpha topic", [two])
    assert {r.document_id for r in only_two} == {two}
    assert {r.document_id for r in services.retriever.search(user.id, "alpha topic")} == {one, two}


def test_selecting_a_foreign_document_yields_nothing(
    services: Services, fake_db: FakeSupabase, new_user: Callable[..., TestUser]
) -> None:
    alice, mallory = new_user(name="Alice"), new_user(name="Mallory")
    alice_doc = seed(services, fake_db, alice.id, "Private", ["private alpha content"])
    seed(services, fake_db, mallory.id, "Mine", ["my own alpha content"])
    assert services.retriever.search(mallory.id, "alpha content", [alice_doc]) == []
    assert not services.retriever.has_documents(mallory.id, [alice_doc])


def test_context_is_limited_by_chunk_count_and_token_budget(
    make_settings: Callable[..., Settings],
    services: Services,
    fake_db: FakeSupabase,
    user: TestUser,
) -> None:
    texts = [f"shared keyword number {i} " + "filler " * 150 for i in range(12)]
    seed(services, fake_db, user.id, "Long", texts)
    by_count = Retriever(services.repo, services.embedder, make_settings(context_chunks=5))
    assert len(by_count.search(user.id, "shared keyword")) == 5
    tiny = Retriever(
        services.repo, services.embedder, make_settings(max_context_tokens=500, context_chunks=8)
    )
    assert len(tiny.search(user.id, "shared keyword")) <= 3  # ~200 tokens each


def test_best_chunk_is_kept_even_if_it_alone_exceeds_the_budget(
    make_settings: Callable[..., Settings],
    services: Services,
    fake_db: FakeSupabase,
    user: TestUser,
) -> None:
    seed(services, fake_db, user.id, "Big", ["giant " * 2000])
    retriever = Retriever(services.repo, services.embedder, make_settings(max_context_tokens=500))
    assert len(retriever.search(user.id, "giant")) == 1


def test_reranker_hook_reorders_candidates(
    settings: Settings, services: Services, fake_db: FakeSupabase, user: TestUser
) -> None:
    seed(services, fake_db, user.id, "Doc", ["alpha beta", "alpha beta gamma", "alpha"])

    class Reverse:
        def rerank(self, query: str, candidates: list[Source]) -> list[Source]:
            return list(reversed(candidates))

    plain = services.retriever.search(user.id, "alpha beta")
    flipped = Retriever(services.repo, services.embedder, settings, Reverse()).search(
        user.id, "alpha beta"
    )
    assert [s.chunk_id for s in flipped] == [s.chunk_id for s in reversed(plain)]
    assert NoopReranker().rerank("q", plain) == plain


def test_embedding_failure_propagates_as_a_provider_error(
    services: Services, fake_embedder: FakeEmbedder, user: TestUser
) -> None:
    from tests.fakes.embedder import provider_down

    fake_embedder.fail_with = provider_down()
    with pytest.raises(Exception, match="temporarily unavailable"):
        services.retriever.search(user.id, "anything")


def test_retrieval_logs_counts_but_not_the_question(
    services: Services,
    fake_db: FakeSupabase,
    user: TestUser,
    caplog: pytest.LogCaptureFixture,
    client: TestClient,
) -> None:
    seed(services, fake_db, user.id, "Doc", ["alpha content"])
    with caplog.at_level("DEBUG"):
        services.retriever.search(user.id, "my-very-private-question alpha")
    assert "my-very-private-question" not in caplog.text
