from __future__ import annotations

import hashlib
import json
import os
import subprocess
from pathlib import Path
from typing import Any

import httpx
from sqlalchemy.orm import Session

from backend.app.config import settings
from backend.app.models import OrderAttachment, SystemConfig
from backend.app.services.crypto import decrypt_value
from backend.app.services.jsonutil import dumps, loads
from backend.app.services.storage import safe_name, save_attachment, sha256_bytes


def local_storage_ref(attachment: OrderAttachment) -> str:
    evidence = loads(attachment.evidence_json, {})
    ref = str(evidence.get("local_storage_ref") or "").strip()
    if ref and Path(ref).exists():
        return ref
    return ""


def replace_remote_url_with_local_ref(attachment: OrderAttachment, evidence: dict[str, Any], storage_ref: str) -> None:
    current_url = str(attachment.file_url or "").strip()
    if current_url and current_url != storage_ref and current_url.startswith(("http://", "https://", "//")):
        evidence.setdefault("remote_file_url", current_url)
    attachment.file_url = storage_ref


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
        replace_remote_url_with_local_ref(attachment, evidence, cached)
        attachment.evidence_json = dumps(evidence)
        session.flush()
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
        replace_remote_url_with_local_ref(attachment, evidence, storage_ref)
        attachment.evidence_json = dumps(evidence)
        session.flush()
        return evidence["download_cache"]
    except Exception as exc:
        browser_result = cache_order_attachment_file_via_browser(session, attachment, max_bytes=max_bytes)
        if browser_result.get("status") == "Cached":
            return browser_result
        evidence = loads(attachment.evidence_json, {})
        evidence["download_cache"] = {"status": "Failed", "error": str(exc)[:1000], "browser_fallback": browser_result}
        attachment.evidence_json = dumps(evidence)
        session.flush()
        return evidence["download_cache"]


def config_value(session: Session, key: str, default: str = "") -> str:
    row = session.get(SystemConfig, key)
    if row is None or row.value is None:
        return default
    if row.is_secret:
        return decrypt_value(str(row.value))
    return str(row.value)


def cache_order_attachment_file_via_browser(session: Session, attachment: OrderAttachment, *, max_bytes: int = 20 * 1024 * 1024) -> dict[str, Any]:
    if not attachment.file_url:
        return {"status": "Skipped", "reason": "missing file_url"}
    script_path = Path(__file__).resolve().parents[3] / "scripts" / "fxiaoke_download_attachment.mjs"
    if not script_path.exists():
        return {"status": "Failed", "error": f"script missing: {script_path}"}
    temp_name = hashlib.sha256(f"{attachment.id}|{attachment.file_url}".encode("utf-8")).hexdigest()[:16]
    output_path = Path(settings.attachment_storage_dir) / f"tmp-crm-{temp_name}-{safe_name(attachment.file_name)}"
    payload = {
        "url": attachment.file_url,
        "outputPath": str(output_path),
        "cdpUrl": config_value(session, "crm_cdp_url", "http://127.0.0.1:9333"),
        "username": config_value(session, "crm_username", ""),
        "password": config_value(session, "crm_password", ""),
    }
    completed = subprocess.run(
        [config_value(session, "crm_node_bin", "node") or "node", str(script_path)],
        input=json.dumps(payload),
        cwd=str(Path(__file__).resolve().parents[3]),
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
        env={**os.environ},
    )
    stdout = (completed.stdout or "").strip()
    stderr = (completed.stderr or "").strip()
    if completed.returncode != 0:
        return {"status": "Failed", "error": stderr or stdout or f"node exited {completed.returncode}"}
    try:
        output = json.loads(stdout)
    except json.JSONDecodeError:
        return {"status": "Failed", "error": f"invalid node output: {stdout[:500] or stderr[:500]}"}
    if not output.get("ok"):
        return {"status": "Failed", "error": output.get("error") or stdout[:500]}
    content = output_path.read_bytes()[:max_bytes]
    storage_ref, digest = save_attachment(attachment.file_name, content)
    if output_path.exists() and Path(storage_ref) != output_path:
        output_path.unlink(missing_ok=True)
    evidence = loads(attachment.evidence_json, {})
    evidence["local_storage_ref"] = storage_ref
    evidence["local_file_hash"] = digest
    evidence["local_file_size"] = len(content)
    evidence["download_cache"] = {
        "status": "Cached",
        "storage_ref": storage_ref,
        "file_hash": digest,
        "file_size": len(content),
        "via": "chrome_cdp",
    }
    replace_remote_url_with_local_ref(attachment, evidence, storage_ref)
    attachment.evidence_json = dumps(evidence)
    session.flush()
    return evidence["download_cache"]
