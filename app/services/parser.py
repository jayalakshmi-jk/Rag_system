"""Turn an uploaded file into pages of plain text. No OCR: image-only PDFs are rejected."""

from __future__ import annotations

import io
import logging
import re
from dataclasses import dataclass

from pypdf import PdfReader
from pypdf.errors import PyPdfError

from app.errors import AppError

logger = logging.getLogger(__name__)

ALLOWED_EXTENSIONS = {".pdf", ".txt", ".md"}
_TEXT_PAGE_CHARS = 3_500  # plain-text files have no pages; group paragraphs into virtual ones
_CONTROL_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")
_WS_RE = re.compile(r"[ \t ]+")
_BLANKS_RE = re.compile(r"\n{3,}")

NO_TEXT_MESSAGE = "This PDF does not contain extractable text. OCR is not currently supported."


class DocumentRejectedError(AppError):
    """The file is unusable; the message is safe to show to the user."""

    def __init__(
        self, message: str, code: str = "INVALID_DOCUMENT", status_code: int = 422
    ) -> None:
        super().__init__(status_code, code, message)


@dataclass(frozen=True)
class Page:
    page_no: int
    text: str


def clean_text(text: str) -> str:
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    text = _CONTROL_RE.sub("", text)
    text = "\n".join(_WS_RE.sub(" ", line).strip() for line in text.split("\n"))
    return _BLANKS_RE.sub("\n\n", text).strip()


def extension_of(file_name: str) -> str:
    dot = file_name.rfind(".")
    return file_name[dot:].lower() if dot != -1 else ""


def sniff(data: bytes, extension: str) -> None:
    """Validate content against the claimed type (extensions and MIME headers are not trusted)."""
    if not data:
        raise DocumentRejectedError("The file is empty.")
    if extension == ".pdf":
        if not data.lstrip(b"\x00\t\n\r ")[:5] == b"%PDF-":
            raise DocumentRejectedError("This file is not a valid PDF.")
        return
    if b"\x00" in data[:8192]:
        raise DocumentRejectedError("This file is not a plain-text document.")


def parse_document(data: bytes, extension: str, *, max_pages: int) -> list[Page]:
    if extension == ".pdf":
        return parse_pdf(data, max_pages=max_pages)
    return parse_text(data)


def parse_pdf(data: bytes, *, max_pages: int) -> list[Page]:
    try:
        reader = PdfReader(io.BytesIO(data), strict=False)
        if reader.is_encrypted:
            raise DocumentRejectedError(
                "This PDF is password-protected. Remove the password and retry."
            )
        total = len(reader.pages)
        if total == 0:
            raise DocumentRejectedError("This PDF has no pages.")
        if total > max_pages:
            raise DocumentRejectedError(
                f"This PDF has {total} pages. The limit is {max_pages} pages.",
                code="TOO_MANY_PAGES",
            )
        pages: list[Page] = []
        for index, page in enumerate(reader.pages, start=1):
            try:
                text = clean_text(page.extract_text() or "")
            except Exception:  # one bad page must not sink the document
                logger.warning("pdf page extraction failed", extra={"page": index})
                text = ""
            pages.append(Page(index, text))
    except DocumentRejectedError:
        raise
    except (PyPdfError, ValueError, KeyError, TypeError, RecursionError, OSError) as exc:
        raise DocumentRejectedError("This PDF could not be read. It may be damaged.") from exc
    if not any(p.text for p in pages):
        raise DocumentRejectedError(NO_TEXT_MESSAGE, code="NO_EXTRACTABLE_TEXT")
    return pages


def _decode(data: bytes) -> str:
    for encoding in (
        ("utf-8-sig", "utf-16") if data[:2] in (b"\xff\xfe", b"\xfe\xff") else ("utf-8-sig",)
    ):
        try:
            return data.decode(encoding)
        except UnicodeDecodeError:
            continue
    try:
        return data.decode("cp1252")
    except UnicodeDecodeError as exc:
        raise DocumentRejectedError("This text file uses an unsupported encoding.") from exc


def parse_text(data: bytes) -> list[Page]:
    text = clean_text(_decode(data))
    if not text:
        raise DocumentRejectedError("This file has no readable text.", code="NO_EXTRACTABLE_TEXT")
    pages: list[Page] = []
    current: list[str] = []
    size = 0
    for block in text.split("\n\n"):
        if current and size + len(block) > _TEXT_PAGE_CHARS:
            pages.append(Page(len(pages) + 1, "\n\n".join(current)))
            current, size = [], 0
        # A single huge block (no blank lines) is split on line boundaries.
        if len(block) > _TEXT_PAGE_CHARS * 2:
            for line_group in _split_long(block):
                pages.append(Page(len(pages) + 1, line_group))
            continue
        current.append(block)
        size += len(block) + 2
    if current:
        pages.append(Page(len(pages) + 1, "\n\n".join(current)))
    return pages


def _split_long(block: str) -> list[str]:
    parts: list[str] = []
    buf: list[str] = []
    size = 0
    for line in block.split("\n"):
        while len(line) > _TEXT_PAGE_CHARS:
            cut = line.rfind(" ", 0, _TEXT_PAGE_CHARS)
            cut = cut if cut > 0 else _TEXT_PAGE_CHARS
            if buf:
                parts.append("\n".join(buf))
                buf, size = [], 0
            parts.append(line[:cut])
            line = line[cut:].lstrip()
        if size + len(line) > _TEXT_PAGE_CHARS and buf:
            parts.append("\n".join(buf))
            buf, size = [], 0
        buf.append(line)
        size += len(line) + 1
    if buf:
        parts.append("\n".join(buf))
    return [p for p in parts if p.strip()]
