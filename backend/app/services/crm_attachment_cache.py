from __future__ import annotations

from pathlib import Path
from typing import Any

import httpx
from sqlalchemy.orm import Session

from backend.app.models import OrderAttachment
from backend.app.services.jsonutil import dumps, loads
from backend.app.services.storage import save_attachment


def local_storage_ref(attachment: OrderAttachment) -> str:
    evidence = loads(attachment.evidence_json, {})
    ref = str(evidence.get("local_storage_ref") or "").strip()
    if ref and Path(ref).exists():
        return ref
    return ""


def cache_order_attachment_file(
    session: Session,
    attachment: OrderAttachment,
    *,
    timeout_seconds: float = 20.0,
    max_bytes: int = 20 * 1024 * 1024,
) -> dict[str, Any]:
    evidence = loads(attachment.evidence_json, {})
    cached = local_storage_ref(attachment)
    if cached:
        return {"status": "Cached", "storage_ref": cached}
    if not attachment.file_url:
        evidence["download_cache"] = {"status": "Skipped", "reason": "missing file_url"}
        attachment.evidence_json = dumps(evidence)
        return evidence["download_cache"]
    try:
        with httpx.Client(timeout=timeout_seconds, follow_redirects=True) as client:
            response = client.get(attachment.file_url)
            response.raise_for_status()
            content = response.content[:max_bytes]
        storage_ref, digest = save_attachment(attachment.file_name, content)
        evidence["local_storage_ref"] = storage_ref
        evidence["local_file_hash"] = digest
        evidence["local_file_size"] = len(content)
        evidence["download_cache"] = {"status": "Cached", "storage_ref": storage_ref, "file_hash": digest, "file_size": len(content)}
        attachment.evidence_json = dumps(evidence)
        session.flush()
        return evidence["download_cache"]
    except Exception as exc:
        evidence["download_cache"] = {"status": "Failed", "error": str(exc)[:1000]}
        attachment.evidence_json = dumps(evidence)
        session.flush()
        return evidence["download_cache"]
