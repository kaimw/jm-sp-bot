from __future__ import annotations

import logging
from datetime import datetime, timezone
from time import monotonic

from sqlalchemy.orm import Session

from backend.app.config import MAIL_WORKER_MIN_INTERVAL_SECONDS, settings
from backend.app.database import SessionLocal
from backend.app.models import OutboundMailJob
from backend.app.services.jobs import run_pending_jobs
from backend.app.services.mail_adapter import (
    AUTO_WORKFLOW_MAIL_TYPES,
    OUTBOUND_PRIORITY_NOTIFY,
    send_pending_auto_workflow_mails_smtp,
    send_pending_smtp,
    sync_imap_mailbox,
)
from backend.app.services.workflow import bot_enabled, get_config


logger = logging.getLogger(__name__)

_WORKER_STATUS: dict = {
    "run_count": 0,
    "last_started_at": None,
    "last_finished_at": None,
    "last_duration_seconds": None,
    "last_result": None,
    "last_error": None,
}


def _iso_now() -> str:
    return datetime.now(timezone.utc).isoformat()


import asyncio
from backend.app.services.skills.executor import SkillExecutor
from backend.app.services.skills.mail_skills import * # Ensure skills are registered

async def run_mail_auto_worker_once() -> dict:
    started = monotonic()
    _WORKER_STATUS["run_count"] = int(_WORKER_STATUS.get("run_count") or 0) + 1
    _WORKER_STATUS["last_started_at"] = _iso_now()
    _WORKER_STATUS["last_error"] = None
    
    with SessionLocal() as session:
        executor = SkillExecutor(session)
        try:
            if not bot_enabled(session):
                return _finish_worker_run(
                    {
                        "enabled": False,
                        "synced": {"imported": 0, "queued": 0, "skipped": "bot is disabled"},
                        "processed": {"completed": 0, "failed": 0, "total": 0, "skipped": "bot is disabled"},
                        "auto_workflow_mails": {"sent": 0, "failed": 0, "total": 0, "skipped": "bot is disabled"},
                    },
                    started,
                )
            
            if not get_config(session, "bot_email_password", ""):
                return _finish_worker_run({"enabled": True, "skipped": "bot_email_password is not configured"}, started)

            result = {
                "enabled": True,
                "synced": {"imported": 0, "queued": 0},
                "processed": {"completed": 0, "failed": 0, "total": 0},
                "high_priority_mails": {"sent": 0, "failed": 0, "total": 0},
                "low_priority_mails": {"sent": 0, "failed": 0, "total": 0},
            }

            # 1. 处理任务队列 (Jobs)
            try:
                result["processed"] = run_pending_jobs(session, limit=settings.mail_auto_worker_limit)
                session.commit()
            except Exception as exc:
                session.rollback()
                logger.exception("mail auto worker processing failed")
                result["processed"] = {"completed": 0, "failed": 0, "total": 0, "error": str(exc)}

            # 2. 高优先级通道技能
            high_priority_count = _pending_high_priority_count(session)
            if high_priority_count > 0:
                skill_res = await executor.run("send_high_priority_mails", limit=settings.mail_auto_worker_limit)
                if skill_res.success:
                    result["high_priority_mails"] = skill_res.data
                else:
                    result["high_priority_mails"] = {"sent": 0, "failed": 0, "total": 0, "error": skill_res.message}
                session.commit()

            # 3. 低优先级通道技能
            low_priority_count = pending_auto_workflow_mail_count(session)
            if low_priority_count > 0:
                skill_res = await executor.run("send_auto_workflow_mails", limit=settings.mail_auto_worker_limit)
                if skill_res.success:
                    result["low_priority_mails"] = skill_res.data
                else:
                    result["low_priority_mails"] = {"sent": 0, "failed": 0, "total": 0, "error": skill_res.message}
                session.commit()

            # 4. 收件同步技能 (只有在没有待发邮件时才同步，避免 SMTP 风控)
            if high_priority_count == 0 and low_priority_count == 0:
                skill_res = await executor.run("receive_mails", limit=settings.mail_auto_worker_limit)
                if skill_res.success:
                    result["synced"] = skill_res.data
                else:
                    result["synced"] = {"imported": 0, "queued": 0, "error": skill_res.message}
                session.commit()
            else:
                result["synced"] = {"imported": 0, "queued": 0, "skipped": "pending outbound mail has priority"}

            # 5. 健康检查
            _check_oldest_pending_age(session)

            return _finish_worker_run(result, started)
        except Exception as exc:
            _WORKER_STATUS["last_error"] = str(exc)
            logger.exception("Mail worker critical failure")
            raise


def _finish_worker_run(result: dict, started: float) -> dict:
    _WORKER_STATUS["last_finished_at"] = _iso_now()
    _WORKER_STATUS["last_duration_seconds"] = round(monotonic() - started, 3)
    _WORKER_STATUS["last_result"] = result
    return result


def get_mail_worker_status(configured_interval_seconds: int | None = None) -> dict:
    return {
        **_WORKER_STATUS,
        "auto_worker_enabled": settings.mail_auto_worker_enabled,
        "configured_interval_seconds": configured_interval_seconds
        if configured_interval_seconds is not None
        else configured_mail_worker_interval_seconds(),
        "auto_worker_limit": settings.mail_auto_worker_limit,
    }


def pending_receipt_ack_count() -> int:
    with SessionLocal() as session:
        return session.query(OutboundMailJob).filter_by(mail_type="SalesReceiptAck", status="Pending").count()


def pending_auto_workflow_mail_count(session: Session | None = None) -> int:
    if session is not None:
        return (
            session.query(OutboundMailJob)
            .filter(OutboundMailJob.mail_type.in_(AUTO_WORKFLOW_MAIL_TYPES), OutboundMailJob.status == "Pending")
            .count()
        )
    with SessionLocal() as owned_session:
        return pending_auto_workflow_mail_count(owned_session)


def configured_mail_worker_interval_seconds() -> int:
    with SessionLocal() as session:
        try:
            value = int(get_config(session, "mail_auto_worker_interval_seconds", str(settings.mail_auto_worker_interval_seconds)))
        except ValueError:
            value = settings.mail_auto_worker_interval_seconds
        return max(MAIL_WORKER_MIN_INTERVAL_SECONDS, value)


# 高优先级通道：仅发 priority <= 30（收件回执、业务推进、任务单）
_HIGH_PRIORITY_THRESHOLD = OUTBOUND_PRIORITY_NOTIFY  # 30 以下为高优先级

# 最老 Pending 邮件超过此秒数时打告警
_OLDEST_PENDING_ALERT_SECONDS = 600  # 10 分钟


def _pending_high_priority_count(session: Session) -> int:
    now = datetime.now(timezone.utc)
    return (
        session.query(OutboundMailJob)
        .filter(
            OutboundMailJob.status == "Pending",
            OutboundMailJob.priority < _HIGH_PRIORITY_THRESHOLD,
            (OutboundMailJob.next_retry_at.is_(None)) | (OutboundMailJob.next_retry_at <= now),
        )
        .count()
    )


def _send_high_priority_mails(session: Session, *, limit: int = 5) -> dict:
    """只发高优先级（priority < NOTIFY）的待发邮件。"""
    now = datetime.now(timezone.utc)
    jobs = (
        session.query(OutboundMailJob)
        .filter(
            OutboundMailJob.status == "Pending",
            OutboundMailJob.priority < _HIGH_PRIORITY_THRESHOLD,
            (OutboundMailJob.next_retry_at.is_(None)) | (OutboundMailJob.next_retry_at <= now),
        )
        .order_by(OutboundMailJob.priority, OutboundMailJob.created_at)
        .limit(limit)
        .all()
    )
    if not jobs:
        return {"sent": 0, "failed": 0, "total": 0}
    return send_pending_smtp(session, limit=limit)


def _check_oldest_pending_age(session: Session) -> None:
    """检查最老的 Pending 邮件年龄，超阈值时打 WARNING 日志，便于快速发现积压。"""
    oldest = (
        session.query(OutboundMailJob)
        .filter(OutboundMailJob.status == "Pending")
        .order_by(OutboundMailJob.created_at)
        .first()
    )
    if oldest is None:
        return
    now = datetime.now(timezone.utc)
    created = oldest.created_at
    if created.tzinfo is None:
        created = created.replace(tzinfo=timezone.utc)
    age_seconds = (now - created).total_seconds()
    if age_seconds > _OLDEST_PENDING_ALERT_SECONDS:
        logger.warning(
            "[OutboundMailAlert] 最老 Pending 邮件已等待 %.0f 秒（阈值 %d 秒），"
            "mail_type=%s id=%s subject=%s",
            age_seconds,
            _OLDEST_PENDING_ALERT_SECONDS,
            oldest.mail_type,
            oldest.id,
            oldest.subject[:60],
        )
