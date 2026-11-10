"""Document upload, listing, renaming, deletion and page text for the source panel."""

from __future__ import annotations

import hashlib
import logging
import re
import uuid
from typing import Annotated

from fastapi import APIRouter, BackgroundTasks, File, Form, Request, Response, UploadFile

from app.auth import CurrentUser, rate_limited
from app.deps import ServicesDep, SettingsDep
from app.errors import AppError, NotFoundError
from app.logging import log_event
from app.schemas import DocumentList, DocumentOut, PageOut, RenameDocumentRequest
from app.services.parser import ALLOWED_EXTENSIONS, DocumentRejectedError, extension_of, sniff

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/documents", tags=["documents"])

_MIME_BY_EXT = {
    ".pdf": {"application/pdf", "application/x-pdf"},
    ".txt": {"text/plain"},
    ".md": {"text/markdown", "text/x-markdown", "text/plain"},
}
_GENERIC_MIME = {"application/octet-stream", "binary/octet-stream", ""}
_UNSAFE_NAME_RE = re.compile(r"[\x00-\x1f\x7f/\\]")
_READ_CHUNK = 1024 * 1024


def _safe_name(raw: str | None) -> str:
    name = _UNSAFE_NAME_RE.sub("", (raw or "").replace("\\", "/").split("/")[-1]).strip()
    return name[:200]


def _title_from(name: str) -> str:
    stem = name.rsplit(".", 1)[0] if "." in name else name
    return " ".join(stem.replace("_", " ").split())[:200] or "Untitled"


async def _read_limited(upload: UploadFile, limit: int) -> bytes:
    parts: list[bytes] = []
    size = 0
    while chunk := await upload.read(_READ_CHUNK):
        size += len(chunk)
        if size > limit:
            raise AppError(
                413,
                "FILE_TOO_LARGE",
                f"This file is larger than the {limit // (1024 * 1024)} MB limit.",
            )
        parts.append(chunk)
    return b"".join(parts)


@router.post(
    "",
    status_code=202,
    response_model=DocumentOut,
    dependencies=[rate_limited("upload", "rate_limit_upload_per_hour", 3600)],
)
async def upload_document(
    request: Request,
    background: BackgroundTasks,
    user: CurrentUser,
    settings: SettingsDep,
    services: ServicesDep,
    file: Annotated[UploadFile, File()],
    allow_duplicate: Annotated[bool, Form()] = False,
) -> DocumentOut:
    limit = settings.max_upload_bytes
    declared = request.headers.get("content-length")
    if declared and declared.isdigit() and int(declared) > limit + 64 * 1024:
        raise AppError(
            413,
            "FILE_TOO_LARGE",
            f"This file is larger than the {settings.max_upload_mb} MB limit.",
        )

    name = _safe_name(file.filename)
    extension = extension_of(name)
    if extension not in ALLOWED_EXTENSIONS:
        raise AppError(415, "UNSUPPORTED_FILE_TYPE", "Only PDF, TXT and MD files are supported.")
    mime = (file.content_type or "").split(";")[0].strip().lower()
    if mime not in _GENERIC_MIME and mime not in _MIME_BY_EXT[extension]:
        raise AppError(415, "UNSUPPORTED_FILE_TYPE", "The file type does not match its extension.")

    data = await _read_limited(file, limit)
    try:
        sniff(data, extension)
    except DocumentRejectedError as exc:
        raise AppError(415, "UNSUPPORTED_FILE_TYPE", exc.message) from exc

    repo = services.repo
    if repo.count_documents(user.id) >= settings.max_documents_per_user:
        raise AppError(
            409,
            "DOCUMENT_LIMIT_REACHED",
            f"You can keep up to {settings.max_documents_per_user} documents. "
            "Delete one to add more.",
        )

    file_hash = hashlib.sha256(data).hexdigest()
    if not allow_duplicate:
        existing = repo.find_duplicate(user.id, file_hash)
        if existing:
            raise AppError(
                409,
                "DUPLICATE_DOCUMENT",
                "This document already exists.",
                extra={"existing_document_id": str(existing["id"])},
            )

    created = repo.create_document(
        user.id,
        title=_title_from(name),
        file_name=name,
        file_size=len(data),
        mime_type=mime or None,
        file_hash=file_hash,
    )
    log_event(
        logger,
        "document_uploaded",
        user_id=user.id,
        document_id=created["id"],
        size=len(data),
        ext=extension,
    )
    background.add_task(services.ingest.run, user.id, str(created["id"]), data, extension)
    return DocumentOut.model_validate(created)


@router.get("", response_model=DocumentList)
def list_documents(user: CurrentUser, services: ServicesDep) -> DocumentList:
    docs = services.repo.list_documents(user.id)
    return DocumentList(documents=[DocumentOut.model_validate(d) for d in docs])


def _doc_id(value: uuid.UUID) -> str:
    return str(value)


@router.get("/{document_id}", response_model=DocumentOut)
def get_document(document_id: uuid.UUID, user: CurrentUser, services: ServicesDep) -> DocumentOut:
    doc = services.repo.get_document(user.id, _doc_id(document_id))
    if doc is None:
        raise NotFoundError("Document")
    return DocumentOut.model_validate(doc)


@router.patch("/{document_id}", response_model=DocumentOut)
def rename_document(
    document_id: uuid.UUID, body: RenameDocumentRequest, user: CurrentUser, services: ServicesDep
) -> DocumentOut:
    updated = services.repo.rename_document(user.id, _doc_id(document_id), body.title)
    if updated is None:
        raise NotFoundError("Document")
    doc = services.repo.get_document(user.id, _doc_id(document_id))
    if doc is None:
        raise NotFoundError("Document")
    return DocumentOut.model_validate(doc)


@router.delete("/{document_id}", status_code=204)
def delete_document(document_id: uuid.UUID, user: CurrentUser, services: ServicesDep) -> Response:
    if not services.repo.delete_document(user.id, _doc_id(document_id)):
        raise NotFoundError("Document")
    log_event(logger, "document_deleted", user_id=user.id, document_id=str(document_id))
    return Response(status_code=204)


@router.get("/{document_id}/pages/{page_no}", response_model=PageOut)
def get_page(
    document_id: uuid.UUID, page_no: int, user: CurrentUser, services: ServicesDep
) -> PageOut:
    if page_no < 1:
        raise NotFoundError("Page")
    doc = services.repo.get_document(user.id, _doc_id(document_id))
    if doc is None or doc["status"] != "ready":
        raise NotFoundError("Document")
    page = services.repo.get_page(user.id, _doc_id(document_id), page_no)
    if page is None:
        raise NotFoundError("Page")
    return PageOut(page_no=page["page_no"], page_count=doc["page_count"], content=page["content"])
