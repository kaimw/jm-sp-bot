from __future__ import annotations

from datetime import datetime, timedelta, timezone


BEIJING_TIMEZONE = timezone(timedelta(hours=8))


def to_beijing_time(value: datetime) -> datetime:
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(BEIJING_TIMEZONE)


def format_beijing_time(value: datetime, *, include_seconds: bool = False) -> str:
    pattern = "%Y-%m-%d %H:%M:%S" if include_seconds else "%Y-%m-%d %H:%M"
    return to_beijing_time(value).strftime(pattern)
