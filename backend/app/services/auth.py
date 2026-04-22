from __future__ import annotations

import base64
import hashlib
import hmac
import json
import time

from backend.app.config import settings


COOKIE_NAME = "jm_sp_session"


def create_session_token(username: str) -> str:
    payload = {
        "u": username,
        "exp": int(time.time()) + settings.auth_session_seconds,
    }
    payload_text = _base64_url_encode(json.dumps(payload, separators=(",", ":")).encode("utf-8"))
    return f"{payload_text}.{_sign(payload_text)}"


def parse_session_token(token: str | None) -> str | None:
    if not token or "." not in token:
        return None
    payload_text, signature = token.rsplit(".", 1)
    if not hmac.compare_digest(signature, _sign(payload_text)):
        return None
    try:
        payload = json.loads(_base64_url_decode(payload_text))
    except (ValueError, json.JSONDecodeError):
        return None
    if int(payload.get("exp") or 0) < int(time.time()):
        return None
    username = payload.get("u")
    return username if isinstance(username, str) and username else None


def _sign(payload_text: str) -> str:
    return hmac.new(settings.auth_secret.encode("utf-8"), payload_text.encode("utf-8"), hashlib.sha256).hexdigest()


def _base64_url_encode(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).decode("ascii").rstrip("=")


def _base64_url_decode(value: str) -> bytes:
    padding = "=" * (-len(value) % 4)
    return base64.urlsafe_b64decode(value + padding)
