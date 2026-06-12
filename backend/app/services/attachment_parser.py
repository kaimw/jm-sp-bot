from __future__ import annotations

import io
import re
import subprocess
import tempfile
import zipfile
from dataclasses import dataclass, field
from pathlib import Path

from docx import Document
from openpyxl import load_workbook
from PIL import Image, ImageEnhance, ImageOps


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
        if suffix in {".png", ".jpg", ".jpeg", ".tif", ".tiff", ".bmp", ".webp"}:
            return ParsedAttachment(file_name=file_name, status="Parsed", text=parse_image_text(content, suffix), archive_depth=depth)
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
            text = repair_mojibake_text(page.extract_text() or "")
            if text.strip():
                parts.append(f"[page {index}]\n{text.strip()}")
        if parts:
            extracted = "\n\n".join(parts)
            if is_meaningful_text(extracted):
                return extracted
    except ModuleNotFoundError:
        pass
    except Exception:
        pass

    text = content.decode("latin-1", errors="ignore")
    matches = re.findall(r"\(([^()]*)\)\s*Tj", text)
    if not matches:
        matches = re.findall(r"\(([^()]*)\)", text)
    decoded = [decode_pdf_literal(match).strip() for match in matches]
    result = "\n".join(item for item in decoded if item)
    if result and is_meaningful_text(result):
        return result
    ocr_text = parse_pdf_by_ocr(content)
    if ocr_text:
        return ocr_text
    return result


def is_meaningful_text(value: str) -> bool:
    text = str(value or "").strip()
    if len(text) < 20:
        return bool(text)
    readable = sum(1 for char in text if char.isalnum() or "\u4e00" <= char <= "\u9fff")
    controls = sum(1 for char in text if ord(char) < 32 and char not in "\n\r\t")
    return readable / max(1, len(text)) >= 0.08 and controls / max(1, len(text)) <= 0.02


def parse_image_text(content: bytes, suffix: str = ".png") -> str:
    image = Image.open(io.BytesIO(content))
    if image.mode not in {"RGB", "L"}:
        image = image.convert("RGB")
    return run_tesseract(image, suffix=suffix)


def parse_pdf_by_ocr(content: bytes, *, max_pages: int = 3) -> str:
    try:
        import fitz  # type: ignore
    except ModuleNotFoundError:
        return ""
    parts: list[str] = []
    document = fitz.open(stream=content, filetype="pdf")
    try:
        for page_index in range(min(max_pages, len(document))):
            page = document.load_page(page_index)
            pixmap = page.get_pixmap(matrix=fitz.Matrix(3, 3), alpha=False)
            image = Image.open(io.BytesIO(pixmap.tobytes("png")))
            crop_text = parse_pdf_priority_crops(image)
            if crop_text.strip():
                parts.append(f"[ocr page {page_index + 1} priority crops]\n{crop_text.strip()}")
            text = run_tesseract(image, suffix=".png", psm="6")
            sparse_text = run_tesseract(image, suffix=".png", psm="12")
            page_text = "\n".join(item for item in (text.strip(), sparse_text.strip()) if item)
            if page_text.strip():
                parts.append(f"[ocr page {page_index + 1}]\n{page_text.strip()}")
    finally:
        document.close()
    return "\n\n".join(parts)


def parse_pdf_priority_crops(image: Image.Image) -> str:
    width, height = image.size
    crops = [
        image.crop((int(width * 0.03), int(height * 0.16), int(width * 0.54), int(height * 0.46))),
        image.crop((int(width * 0.03), int(height * 0.12), int(width * 0.58), int(height * 0.52))),
    ]
    parts: list[str] = []
    for crop in crops:
        prepared = ImageEnhance.Contrast(ImageOps.grayscale(crop)).enhance(1.6)
        if min(prepared.size) < 900:
            prepared = prepared.resize((prepared.width * 2, prepared.height * 2))
        for psm in ("6", "12"):
            text = run_tesseract(prepared, suffix=".png", psm=psm)
            if text.strip():
                parts.append(text.strip())
    return "\n".join(parts)


def run_tesseract(image: Image.Image, *, suffix: str = ".png", psm: str = "6") -> str:
    with tempfile.NamedTemporaryFile(suffix=suffix) as handle:
        image.save(handle.name)
        completed = subprocess.run(
            ["tesseract", handle.name, "stdout", "-l", "chi_sim+eng", "--psm", psm],
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
    if completed.returncode != 0:
        raise ValueError((completed.stderr or "OCR failed").strip())
    return repair_mojibake_text(completed.stdout)


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
        return repair_mojibake_text(decoded)


def repair_mojibake_text(value: str) -> str:
    text = str(value or "")
    if not text:
        return ""
    if any(marker in text for marker in ("Ã", "Â", "å", "æ", "è", "é", "ï¼")):
        for source_encoding in ("latin-1", "cp1252"):
            try:
                raw = text.encode(source_encoding)
            except UnicodeEncodeError:
                continue
            for target_encoding in ("utf-8", "gb18030", "gbk", "big5"):
                try:
                    repaired = raw.decode(target_encoding)
                except UnicodeDecodeError:
                    continue
                if repaired != text and "\ufffd" not in repaired:
                    return repaired
    return text


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
