from __future__ import annotations

import hashlib
import re
from pathlib import Path

from backend.app.config import settings


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def safe_name(file_name: str) -> str:
    cleaned = re.sub(r"[^A-Za-z0-9._-]+", "_", file_name).strip("._")
    return cleaned or "attachment.bin"


def save_attachment(file_name: str, content: bytes) -> tuple[str, str]:
    digest = sha256_bytes(content)
    storage_dir = Path(settings.attachment_storage_dir)
    storage_dir.mkdir(parents=True, exist_ok=True)
    path = storage_dir / f"{digest[:16]}-{safe_name(file_name)}"
    if not path.exists():
        path.write_bytes(content)
    return str(path), digest


def read_storage(storage_ref: str) -> bytes:
    return Path(storage_ref).read_bytes()
