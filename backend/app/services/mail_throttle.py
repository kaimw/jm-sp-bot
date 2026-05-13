from __future__ import annotations

import math
import threading
import time
from datetime import datetime, timedelta, timezone

from sqlalchemy.orm import Session

from backend.app.config import MAIL_LOGIN_MIN_INTERVAL_SECONDS, settings
from backend.app.models import SystemConfig
from backend.app.services.workflow import get_config


_lock = threading.Lock()
_last_login_attempts: dict[str, float] = {}
_last_send_attempts: dict[str, float] = {}


def clamp_mail_interval_seconds(value: int | str | None) -> int:
    try:
        parsed = int(value) if value is not None else settings.mail_rate_limit_interval_seconds
    except (TypeError, ValueError):
        parsed = settings.mail_rate_limit_interval_seconds
    return max(MAIL_LOGIN_MIN_INTERVAL_SECONDS, parsed)


def mail_login_interval_seconds(session: Session) -> int:
    return clamp_mail_interval_seconds(
        get_config(session, "mail_rate_limit_interval_seconds", str(settings.mail_rate_limit_interval_seconds))
    )


def reserve_mail_login(protocol: str, username: str, *, interval_seconds: int | None = None) -> None:
    interval = clamp_mail_interval_seconds(interval_seconds)
    key = username.lower()
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


def reserve_mail_send(username: str, *, interval_seconds: int | None = None, blocking: bool = False) -> None:
    interval = clamp_mail_interval_seconds(interval_seconds)
    key = username.lower()
    now = time.monotonic()
    with _lock:
        previous = _last_send_attempts.get(key)
        if previous is not None:
            remaining = interval - (now - previous)
            if remaining > 0:
                if blocking:
                    # 阻塞模式：等待冷却后继续，规避腾讯企业邮箱发信频率风控
                    _last_send_attempts[key] = now + remaining
                    # 释放锁后再 sleep，避免持锁阻塞
                else:
                    wait_seconds = max(1, math.ceil(remaining))
                    raise RuntimeError(
                        f"{username} 的 SMTP 发信触发频率保护，请 {wait_seconds} 秒后重试；单账号发信间隔不能低于 "
                        f"{MAIL_LOGIN_MIN_INTERVAL_SECONDS} 秒。"
                    )
            else:
                _last_send_attempts[key] = now
                return
        else:
            _last_send_attempts[key] = now
            return

    # 只有 blocking=True 且需要等待时才走到这里
    time.sleep(remaining)


def _throttle_config_key(kind: str, username: str) -> str:
    safe_username = username.lower().replace(":", "_").replace("/", "_")
    return f"mail_throttle.{kind}.{safe_username}.reserved_until"


def mail_send_reserved_until(session: Session, username: str) -> datetime | None:
    row = session.get(SystemConfig, _throttle_config_key("smtp_send", username))
    if row is None or not row.value:
        return None
    try:
        value = datetime.fromisoformat(row.value)
    except ValueError:
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value


def seconds_until_mail_send(session: Session, username: str, *, interval_seconds: int | None = None) -> int:
    reserved_until = mail_send_reserved_until(session, username)
    if reserved_until is None:
        return 0
    now = datetime.now(timezone.utc)
    if reserved_until <= now:
        return 0
    return max(1, math.ceil((reserved_until - now).total_seconds()))


def reserve_mail_send_slot(session: Session, username: str, *, interval_seconds: int | None = None) -> datetime | None:
    """Reserve one SMTP send slot without sleeping.

    Returns None when a slot is available and reserved. Returns the future
    datetime when the caller should retry.
    """
    interval = clamp_mail_interval_seconds(interval_seconds)
    now = datetime.now(timezone.utc)
    reserved_until = mail_send_reserved_until(session, username)
    if reserved_until is not None and reserved_until > now:
        return reserved_until
    next_reserved_until = now + timedelta(seconds=interval)
    key = _throttle_config_key("smtp_send", username)
    row = session.get(SystemConfig, key)
    if row is None:
        row = SystemConfig(key=key, value=next_reserved_until.isoformat(), value_type="string", is_secret=False)
        session.add(row)
    else:
        row.value = next_reserved_until.isoformat()
        row.updated_at = now
    session.flush()
    return None


def reset_mail_login_throttle() -> None:
    with _lock:
        _last_login_attempts.clear()
        _last_send_attempts.clear()
