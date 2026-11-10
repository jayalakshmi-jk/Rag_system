from __future__ import annotations

import pytest
from app.services.chunker import OVERLAP_WORDS, TARGET_WORDS, chunk_pages, estimate_tokens
from app.services.parser import (
    NO_TEXT_MESSAGE,
    DocumentRejectedError,
    Page,
    clean_text,
    extension_of,
    parse_document,
    parse_pdf,
    parse_text,
    sniff,
)

from tests.pdfs import encrypt_pdf, make_pdf


# ----------------------------------------------------------------------- parser
def test_pdf_pages_keep_their_numbers_and_blank_pages_are_kept_empty() -> None:
    pages = parse_pdf(make_pdf(["First page text", "", "Third page"]), max_pages=10)
    assert [(p.page_no, p.text) for p in pages] == [
        (1, "First page text"),
        (2, ""),
        (3, "Third page"),
    ]


def test_image_only_pdf_gets_the_ocr_message() -> None:
    with pytest.raises(DocumentRejectedError) as caught:
        parse_pdf(make_pdf(["", ""]), max_pages=10)
    assert caught.value.message == NO_TEXT_MESSAGE
    assert caught.value.code == "NO_EXTRACTABLE_TEXT"


def test_encrypted_pdf_is_rejected_clearly() -> None:
    with pytest.raises(DocumentRejectedError, match="password"):
        parse_pdf(encrypt_pdf(make_pdf(["secret text"]), "pw"), max_pages=10)


def test_page_limit_is_enforced() -> None:
    with pytest.raises(DocumentRejectedError) as caught:
        parse_pdf(make_pdf(["a", "b", "c"]), max_pages=2)
    assert caught.value.code == "TOO_MANY_PAGES"


@pytest.mark.parametrize("data", [b"%PDF-1.4 garbage", b"%PDF-1.7\n1 0 obj << >> endobj", b"%PDF-"])
def test_corrupt_pdfs_are_rejected_not_crashed(data: bytes) -> None:
    with pytest.raises(DocumentRejectedError):
        parse_pdf(data, max_pages=10)


def test_sniff_checks_content_against_extension() -> None:
    sniff(make_pdf(["x"]), ".pdf")
    sniff(b"hello", ".txt")
    with pytest.raises(DocumentRejectedError):
        sniff(b"MZ\x90\x00 not a pdf", ".pdf")
    with pytest.raises(DocumentRejectedError):
        sniff(b"\x00\x01\x02binary", ".txt")
    with pytest.raises(DocumentRejectedError, match="empty"):
        sniff(b"", ".txt")


def test_extension_of() -> None:
    assert extension_of("Notes.PDF") == ".pdf"
    assert extension_of("archive.tar.gz") == ".gz"
    assert extension_of("noext") == ""


def test_text_decoding_handles_bom_utf16_and_legacy_encodings() -> None:
    assert parse_text("﻿வணக்கம் world".encode())[0].text == "வணக்கம் world"
    assert parse_text("hello".encode("utf-16"))[0].text == "hello"
    assert parse_text("café".encode("cp1252"))[0].text == "café"


def test_whitespace_only_text_is_rejected() -> None:
    with pytest.raises(DocumentRejectedError):
        parse_text(b"  \n\n \t ")


def test_long_text_is_split_into_virtual_pages_without_losing_content() -> None:
    paragraphs = [f"Paragraph {i} " + "word " * 120 for i in range(40)]
    pages = parse_text("\n\n".join(paragraphs).encode())
    assert len(pages) > 3
    assert [p.page_no for p in pages] == list(range(1, len(pages) + 1))
    joined = "\n\n".join(p.text for p in pages)
    assert all(f"Paragraph {i} " in joined for i in range(40))


def test_a_single_giant_line_is_still_paginated() -> None:
    pages = parse_text(("lorem " * 5000).encode())
    assert len(pages) >= 5
    assert all(len(p.text) <= 7_100 for p in pages)


def test_clean_text_strips_control_characters_and_collapses_blank_lines() -> None:
    assert clean_text("a\x00b\r\n\r\n\r\n\r\nc   d") == "ab\n\nc d"


def test_parse_document_dispatches_on_extension() -> None:
    assert parse_document(b"# Title\nbody", ".md", max_pages=5)[0].text.startswith("# Title")
    assert parse_document(make_pdf(["pdf body"]), ".pdf", max_pages=5)[0].text == "pdf body"


# ---------------------------------------------------------------------- chunker
def words(n: int, prefix: str = "w") -> str:
    return " ".join(f"{prefix}{i}" for i in range(n))


def test_chunks_never_cross_pages_and_keep_page_numbers() -> None:
    pages = [Page(1, words(500, "a")), Page(2, words(500, "b")), Page(7, words(100, "c"))]
    chunks = chunk_pages(pages)
    for chunk in chunks:
        assert chunk.page_no in {1, 2, 7}
        prefix = {1: "a", 2: "b", 7: "c"}[chunk.page_no]
        assert all(w.startswith(prefix) for w in chunk.content.split())
    assert [c.index for c in chunks] == list(range(len(chunks)))


def test_chunk_size_target_and_overlap() -> None:
    chunks = chunk_pages([Page(1, words(1000))])
    assert len(chunks) >= 3
    for chunk in chunks:
        assert len(chunk.content.split()) <= TARGET_WORDS + 5
    first, second = chunks[0].content.split(), chunks[1].content.split()
    assert first[-OVERLAP_WORDS:] == second[:OVERLAP_WORDS]


def test_all_words_are_covered() -> None:
    text = words(1234)
    chunks = chunk_pages([Page(1, text)])
    seen = {w for c in chunks for w in c.content.split()}
    assert seen == set(text.split())


def test_markdown_headings_become_section_titles_and_carry_across_pages() -> None:
    page1 = Page(1, "# Introduction\n\n" + words(80, "x") + "\n\n## Methods\n\n" + words(80, "y"))
    page2 = Page(2, words(80, "z"))
    chunks = chunk_pages([page1, page2])
    titles = {c.section_title for c in chunks}
    assert "Introduction" in titles
    assert "Methods" in titles
    assert chunks[-1].page_no == 2
    assert chunks[-1].section_title == "Methods"
    assert not any(c.content.startswith("#") for c in chunks)


def test_numbered_and_uppercase_headings_are_detected() -> None:
    page = Page(1, "1. Scope\n" + words(70, "p") + "\n\nTERMS AND CONDITIONS\n" + words(70, "q"))
    sections = [c.section_title for c in chunk_pages([page])]
    assert "1. Scope" in sections
    assert "TERMS AND CONDITIONS" in sections


def test_sentences_ending_in_punctuation_are_not_headings() -> None:
    chunks = chunk_pages([Page(1, "This is a short sentence.\n" + words(70))])
    assert chunks[0].section_title is None


def test_tiny_tail_is_merged_not_orphaned() -> None:
    chunks = chunk_pages([Page(1, words(305))])
    assert all(len(c.content.split()) >= 60 for c in chunks)


def test_empty_pages_produce_no_chunks() -> None:
    assert chunk_pages([Page(1, ""), Page(2, "   ")]) == []


def test_tamil_text_is_chunked_intact() -> None:
    text = " ".join(["இது ஒரு சோதனை வாக்கியம்"] * 100)
    chunks = chunk_pages([Page(1, text)])
    assert chunks
    assert all("இது" in c.content for c in chunks)


def test_token_estimate_is_positive_and_monotonic() -> None:
    assert estimate_tokens("") == 0
    assert 0 < estimate_tokens("hello world") < estimate_tokens("hello world " * 50)
