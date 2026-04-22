from __future__ import annotations

import io
import re
import zipfile
from dataclasses import dataclass, field
from pathlib import Path

from docx import Document
from openpyxl import load_workbook


@dataclass
class ParsedAttachment:
    file_name: str
    status: str
    text: str = ""
    error: str | None = None
    archive_path: str | None = None
    archive_depth: int | None = None
    children: list["ParsedAttachment"] = field(default_factory=list)


def parse_attachment(file_name: str, content: bytes, *, max_zip_bytes: int, max_depth: int, depth: int = 0) -> ParsedAttachment:
    suffix = Path(file_name).suffix.lower()
    try:
        if suffix == ".docx":
            return ParsedAttachment(file_name=file_name, status="Parsed", text=parse_docx(content), archive_depth=depth)
        if suffix == ".xlsx":
            return ParsedAttachment(file_name=file_name, status="Parsed", text=parse_xlsx(content), archive_depth=depth)
        if suffix == ".zip":
            return parse_zip(file_name, content, max_zip_bytes=max_zip_bytes, max_depth=max_depth, depth=depth)
        if suffix in {".txt", ".csv"}:
            return ParsedAttachment(file_name=file_name, status="Parsed", text=content.decode("utf-8", errors="replace"), archive_depth=depth)
        if suffix == ".pdf":
            return ParsedAttachment(file_name=file_name, status="Parsed", text=parse_pdf_text(content), archive_depth=depth)
        return ParsedAttachment(file_name=file_name, status="Skipped", error=f"Unsupported file type: {suffix or 'unknown'}", archive_depth=depth)
    except Exception as exc:
        return ParsedAttachment(file_name=file_name, status="Failed", error=str(exc), archive_depth=depth)


def parse_docx(content: bytes) -> str:
    document = Document(io.BytesIO(content))
    parts: list[str] = []
    parts.extend(paragraph.text for paragraph in document.paragraphs if paragraph.text.strip())
    for table in document.tables:
        for row in table.rows:
            cells = [cell.text.strip() for cell in row.cells if cell.text.strip()]
            if cells:
                parts.append(" | ".join(cells))
    return "\n".join(parts)


def parse_xlsx(content: bytes) -> str:
    workbook = load_workbook(io.BytesIO(content), data_only=True, read_only=True)
    parts: list[str] = []
    for sheet in workbook.worksheets:
        parts.append(f"[{sheet.title}]")
        for row in sheet.iter_rows(values_only=True):
            values = [str(value).strip() for value in row if value not in (None, "")]
            if values:
                parts.append(" | ".join(values))
    workbook.close()
    return "\n".join(parts)


def parse_pdf_text(content: bytes) -> str:
    try:
        from pypdf import PdfReader  # type: ignore

        reader = PdfReader(io.BytesIO(content))
        parts = []
        for index, page in enumerate(reader.pages, start=1):
            text = page.extract_text() or ""
            if text.strip():
                parts.append(f"[page {index}]\n{text.strip()}")
        if parts:
            return "\n\n".join(parts)
    except ModuleNotFoundError:
        pass

    text = content.decode("latin-1", errors="ignore")
    matches = re.findall(r"\(([^()]*)\)\s*Tj", text)
    if not matches:
        matches = re.findall(r"\(([^()]*)\)", text)
    decoded = [decode_pdf_literal(match).strip() for match in matches]
    result = "\n".join(item for item in decoded if item)
    if not result:
        raise ValueError("No extractable PDF text found.")
    return result


def decode_pdf_literal(value: str) -> str:
    decoded = (
        value.replace(r"\(", "(")
        .replace(r"\)", ")")
        .replace(r"\\", "\\")
        .replace(r"\n", "\n")
        .replace(r"\r", "\n")
        .replace(r"\t", "\t")
    )
    try:
        return decoded.encode("latin-1").decode("utf-8")
    except UnicodeError:
        return decoded


def parse_zip(file_name: str, content: bytes, *, max_zip_bytes: int, max_depth: int, depth: int) -> ParsedAttachment:
    if len(content) > max_zip_bytes:
        return ParsedAttachment(file_name=file_name, status="Failed", error="ZIP exceeds maximum size.", archive_depth=depth)
    if depth >= max_depth:
        return ParsedAttachment(file_name=file_name, status="Failed", error="ZIP exceeds maximum extraction depth.", archive_depth=depth)

    root = ParsedAttachment(file_name=file_name, status="Parsed", archive_depth=depth)
    total_uncompressed = 0
    with zipfile.ZipFile(io.BytesIO(content)) as archive:
        for info in archive.infolist():
            if info.is_dir():
                continue
            total_uncompressed += info.file_size
            if total_uncompressed > max_zip_bytes:
                root.children.append(
                    ParsedAttachment(file_name=info.filename, status="Failed", error="ZIP uncompressed size exceeds limit.", archive_depth=depth + 1)
                )
                continue
            child_content = archive.read(info)
            child = parse_attachment(info.filename, child_content, max_zip_bytes=max_zip_bytes, max_depth=max_depth, depth=depth + 1)
            child.archive_path = info.filename
            root.children.append(child)
    root.text = "\n\n".join(child.text for child in root.children if child.text)
    return root
