from __future__ import annotations

import math
import threading
import time

from sqlalchemy.orm import Session

from backend.app.config import MAIL_LOGIN_MIN_INTERVAL_SECONDS, settings
from backend.app.services.workflow import get_config


_lock = threading.Lock()
_last_login_attempts: dict[str, float] = {}
_last_send_attempts: dict[str, float] = {}


def clamp_mail_interval_seconds(value: int | str | None) -> int:
    try:
        parsed = int(value) if value is not None else settings.mail_auto_worker_interval_seconds
    except (TypeError, ValueError):
        parsed = settings.mail_auto_worker_interval_seconds
    return max(MAIL_LOGIN_MIN_INTERVAL_SECONDS, parsed)


def mail_login_interval_seconds(session: Session) -> int:
    return clamp_mail_interval_seconds(
        get_config(session, "mail_auto_worker_interval_seconds", str(settings.mail_auto_worker_interval_seconds))
    )


def reserve_mail_login(protocol: str, username: str, *, interval_seconds: int | None = None) -> None:
    interval = clamp_mail_interval_seconds(interval_seconds)
    key = f"{protocol.lower()}:{username.lower()}"
    now = time.monotonic()
    with _lock:
        previous = _last_login_attempts.get(key)
        if previous is not None:
            remaining = interval - (now - previous)
            if remaining > 0:
                wait_seconds = max(1, math.ceil(remaining))
                raise RuntimeError(
                    f"{username} 的 {protocol.upper()} 登录触发频率保护，请 {wait_seconds} 秒后重试；最低间隔为 "
                    f"{MAIL_LOGIN_MIN_INTERVAL_SECONDS} 秒。"
                )
        _last_login_attempts[key] = now


def reserve_mail_send(username: str, *, interval_seconds: int | None = None) -> None:
    interval = clamp_mail_interval_seconds(interval_seconds)
    key = username.lower()
    now = time.monotonic()
    with _lock:
        previous = _last_send_attempts.get(key)
        if previous is not None:
            remaining = interval - (now - previous)
            if remaining > 0:
                wait_seconds = max(1, math.ceil(remaining))
                raise RuntimeError(
                    f"{username} 的 SMTP 发信触发频率保护，请 {wait_seconds} 秒后重试；单账号发信间隔不能低于 "
                    f"{MAIL_LOGIN_MIN_INTERVAL_SECONDS} 秒。"
                )
        _last_send_attempts[key] = now


def reset_mail_login_throttle() -> None:
    with _lock:
        _last_login_attempts.clear()
        _last_send_attempts.clear()
