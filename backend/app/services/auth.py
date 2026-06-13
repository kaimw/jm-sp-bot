from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import time

from backend.app.config import settings
from backend.app.models import User



COOKIE_NAME = "jm_sp_session"


def hash_password(password: str, salt: bytes = None) -> str:
    if salt is None:
        salt = os.urandom(16)
    key = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, 100000)
    return f"{salt.hex()}:{key.hex()}"


def verify_password(password: str, password_hash: str) -> bool:
    try:
        salt_hex, key_hex = password_hash.split(":", 1)
        salt = bytes.fromhex(salt_hex)
        expected = bytes.fromhex(key_hex)
        key = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, 100000)
        return hmac.compare_digest(key, expected)
    except Exception:
        return False



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


def should_mask_financials(user: User | None, sales_user_name: str | None, owner_department: str | None) -> bool:
    if user is None:
        return False
    if not hasattr(user, "role"):
        return False
    if user.role in ("admin", "business_owner", "auditor"):
        return False
    if user.role == "business_operator":
        if user.username == sales_user_name:
            return False
        if user.department and owner_department and user.department.lower() == owner_department.lower():
            return False
    return True

