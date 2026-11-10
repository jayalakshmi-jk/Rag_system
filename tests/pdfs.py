"""Build small real PDFs for tests (no extra dependency)."""

from __future__ import annotations

import io

from pypdf import PdfReader, PdfWriter


def _escape(text: str) -> str:
    return text.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")


def make_pdf(pages: list[str]) -> bytes:
    """One text line per paragraph; an empty string yields a blank (image-like) page."""
    objects: list[bytes] = []

    def add(body: str) -> int:
        objects.append(body.encode("latin-1"))
        return len(objects)

    font = add("<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>")
    page_ids: list[int] = []
    pages_placeholder = len(pages) * 2 + 2  # id of the /Pages object, added last
    for text in pages:
        lines = list(text.split("\n")) if text else []
        ops = ["BT", "/F1 11 Tf", "14 TL", "50 780 Td"]
        for line in lines:
            ops.append(f"({_escape(line)}) Tj T*")
        ops.append("ET")
        stream = "\n".join(ops) if text else ""
        content = add(f"<< /Length {len(stream)} >>\nstream\n{stream}\nendstream")
        page_ids.append(
            add(
                f"<< /Type /Page /Parent {pages_placeholder} 0 R /MediaBox [0 0 612 792] "
                f"/Contents {content} 0 R /Resources << /Font << /F1 {font} 0 R >> >> >>"
            )
        )
    kids = " ".join(f"{i} 0 R" for i in page_ids)
    pages_id = add(f"<< /Type /Pages /Kids [{kids}] /Count {len(page_ids)} >>")
    assert pages_id == pages_placeholder, (pages_id, pages_placeholder)
    catalog = add(f"<< /Type /Catalog /Pages {pages_id} 0 R >>")

    out = bytearray(b"%PDF-1.4\n")
    offsets = []
    for number, body in enumerate(objects, start=1):
        offsets.append(len(out))
        out += f"{number} 0 obj\n".encode() + body + b"\nendobj\n"
    xref = len(out)
    out += f"xref\n0 {len(objects) + 1}\n0000000000 65535 f \n".encode()
    for offset in offsets:
        out += f"{offset:010d} 00000 n \n".encode()
    out += (
        f"trailer\n<< /Size {len(objects) + 1} /Root {catalog} 0 R >>\nstartxref\n{xref}\n%%EOF\n"
    ).encode()
    return bytes(out)


def encrypt_pdf(data: bytes, password: str) -> bytes:
    writer = PdfWriter()
    for page in PdfReader(io.BytesIO(data)).pages:
        writer.add_page(page)
    writer.encrypt(password)
    buffer = io.BytesIO()
    writer.write(buffer)
    return buffer.getvalue()
