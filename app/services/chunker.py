"""Structure-aware chunking: ~300 words with ~40 words of overlap, never crossing a page.

Keeping chunks inside one page means every citation points at a real page. Markdown headings
and short title-like lines become ``section_title`` for the chunks beneath them.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from app.services.parser import Page

TARGET_WORDS = 300
OVERLAP_WORDS = 40
MIN_TAIL_WORDS = 60  # a smaller final piece is merged into the previous chunk

_HEADING_RE = re.compile(r"^#{1,6}\s+(.+?)\s*#*$")
_NUMBERED_RE = re.compile(r"^(\d+(\.\d+)*[.)]?|[IVX]+\.)\s+\S")


@dataclass(frozen=True)
class Chunk:
    index: int
    page_no: int
    section_title: str | None
    content: str
    token_count: int


def estimate_tokens(text: str) -> int:
    """Cheap, provider-independent estimate (~1.3 tokens per word, ~4 chars per token floor)."""
    words = len(text.split())
    return max(int(words * 1.3), len(text) // 4 if words else 0)


def _heading_of(line: str) -> str | None:
    stripped = line.strip()
    if not stripped:
        return None
    match = _HEADING_RE.match(stripped)
    if match:
        return match.group(1)[:200]
    words = stripped.split()
    short = len(words) <= 10 and len(stripped) <= 90
    titled = short and not stripped.endswith((".", ",", ";", ":", "?"))
    if titled and (_NUMBERED_RE.match(stripped) or (stripped.isupper() and len(words) >= 2)):
        return stripped
    return None


def _units(
    page: Page, section: str | None
) -> tuple[list[tuple[str | None, list[str]]], str | None]:
    """Split a page into (section, words) paragraphs, tracking the heading in effect."""
    out: list[tuple[str | None, list[str]]] = []
    for para in page.text.split("\n\n"):
        lines = para.split("\n")
        body: list[str] = []
        for line in lines:
            heading = _heading_of(line)
            if heading is not None:
                if body:
                    out.append((section, " ".join(body).split()))
                    body = []
                section = heading
            else:
                body.append(line)
        if body:
            out.append((section, " ".join(body).split()))
    return [(s, w) for s, w in out if w], section


def chunk_pages(pages: list[Page]) -> list[Chunk]:
    chunks: list[Chunk] = []
    section: str | None = None
    for page in pages:
        if not page.text.strip():
            continue
        units, section = _units(page, section)
        pieces: list[tuple[str | None, list[str], bool]] = []  # (section, words, is_window)
        for unit_section, words in units:
            # Oversized paragraphs are cut into target-sized windows with built-in overlap.
            if len(words) > TARGET_WORDS + MIN_TAIL_WORDS:
                step = TARGET_WORDS - OVERLAP_WORDS
                for start in range(0, len(words), step):
                    window = words[start : start + TARGET_WORDS]
                    if start and len(window) < MIN_TAIL_WORDS:
                        break
                    pieces.append((unit_section, window, True))
                    if start + TARGET_WORDS >= len(words):
                        break
            else:
                pieces.append((unit_section, words, False))
        buffer: list[str] = []
        buffer_section: str | None = None
        for piece_section, words, is_window in pieces:
            section_changed = piece_section != buffer_section and len(buffer) >= MIN_TAIL_WORDS
            if buffer and (is_window or section_changed or len(buffer) + len(words) > TARGET_WORDS):
                chunks.append(_make(len(chunks), page.page_no, buffer_section, buffer))
                carry = not (is_window or section_changed)
                buffer = buffer[-OVERLAP_WORDS:] if carry else []
            if not buffer:
                buffer_section = piece_section
            buffer = buffer + words
            if is_window:
                chunks.append(_make(len(chunks), page.page_no, buffer_section, buffer))
                buffer = []
        if buffer:
            if (
                len(buffer) < MIN_TAIL_WORDS
                and chunks
                and chunks[-1].page_no == page.page_no
                and chunks[-1].section_title == buffer_section
            ):
                last = chunks.pop()
                merged = last.content + " " + " ".join(buffer)
                chunks.append(_make(last.index, page.page_no, last.section_title, merged.split()))
            else:
                chunks.append(_make(len(chunks), page.page_no, buffer_section, buffer))
    return chunks


def _make(index: int, page_no: int, section: str | None, words: list[str]) -> Chunk:
    content = " ".join(words)
    return Chunk(index, page_no, section, content, estimate_tokens(content))
