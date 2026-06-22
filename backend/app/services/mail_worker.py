from __future__ import annotations

import logging
from datetime import datetime, timezone
from time import monotonic

from sqlalchemy.orm import Session

from backend.app.config import MAIL_WORKER_MIN_INTERVAL_SECONDS, settings
from backend.app.database import SessionLocal
from backend.app.models import OutboundMailJob, ProcessingJob
from backend.app.services.crm_sync import schedule_crm_order_sync_if_due
from backend.app.services.oms.material_sync import oms_material_sync_due
from backend.app.services.jobs import run_pending_jobs, schedule_oms_status_poll_if_due
from backend.app.services.jsonutil import dumps
from backend.app.services.mail_adapter import (
    AUTO_WORKFLOW_MAIL_TYPES,
    OUTBOUND_PRIORITY_NOTIFY,
    send_pending_auto_workflow_mails_smtp,
    send_outbound_jobs_smtp,
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
    "last_mail_io": None,
}


def _iso_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def run_mail_auto_worker_once() -> dict:
    started = monotonic()
    _WORKER_STATUS["run_count"] = int(_WORKER_STATUS.get("run_count") or 0) + 1
    _WORKER_STATUS["last_started_at"] = _iso_now()
    _WORKER_STATUS["last_error"] = None
    
    with SessionLocal() as session:
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
            
            result = {
                "enabled": True,
                "oms_material_sync": schedule_oms_material_sync_if_due(session),
                "crm_order_sync": schedule_crm_order_sync_if_due(session),
                "oms_status_poll": schedule_oms_status_poll_if_due(session),
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

            if not get_config(session, "bot_email_password", ""):
                result["skipped"] = "bot_email_password is not configured"
                return _finish_worker_run(result, started)

            # 2. 高优先级通道技能
            high_priority_count = _pending_high_priority_count(session)
            if high_priority_count > 0:
                result["high_priority_mails"] = _send_high_priority_mails(session, limit=settings.mail_auto_worker_limit)
                _WORKER_STATUS["last_mail_io"] = "high_priority_send"
                session.commit()

            # 3. 收件同步技能
            # 低优先级外发队列可能长期堆积。这里与收件交替执行，避免新订单邮件
            # 被旧的自动通知 backlog 饿死；同一邮箱登录仍保持至少 60 秒间隔。
            low_priority_count = pending_auto_workflow_mail_count(session)
            should_sync = high_priority_count == 0 and (
                low_priority_count == 0 or _WORKER_STATUS.get("last_mail_io") != "sync"
            )
            if should_sync:
                result["synced"] = sync_imap_mailbox(session, limit=settings.mail_auto_worker_limit)
                _WORKER_STATUS["last_mail_io"] = "sync"
                session.commit()
                try:
                    result["processed_after_sync"] = run_pending_jobs(session, limit=settings.mail_auto_worker_limit)
                    session.commit()
                except Exception as exc:
                    session.rollback()
                    logger.exception("mail auto worker post-sync processing failed")
                    result["processed_after_sync"] = {"completed": 0, "failed": 0, "total": 0, "error": str(exc)}
            else:
                result["synced"] = {"imported": 0, "queued": 0, "skipped": "mail IO alternates with outbound queue"}

            # 4. 低优先级通道技能
            if high_priority_count == 0 and low_priority_count > 0 and not should_sync:
                result["low_priority_mails"] = send_pending_auto_workflow_mails_smtp(session, limit=settings.mail_auto_worker_limit)
                _WORKER_STATUS["last_mail_io"] = "low_priority_send"
                session.commit()
            else:
                result["low_priority_mails"] = {
                    "sent": 0,
                    "failed": 0,
                    "total": 0,
                    "skipped": "mail IO alternates with inbound sync" if low_priority_count > 0 else "no pending outbound mail",
                }

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


def schedule_oms_material_sync_if_due(session: Session) -> dict:
    if not oms_material_sync_due(session):
        return {"queued": False, "reason": "not due"}
    existing = (
        session.query(ProcessingJob)
        .filter(ProcessingJob.job_type == "sync_oms_materials", ProcessingJob.status.in_(["Pending", "Running"]))
        .first()
    )
    if existing is not None:
        return {"queued": False, "reason": "already queued", "job_id": existing.id}
    job = ProcessingJob(job_type="sync_oms_materials", payload_json=dumps({"source": "auto"}), status="Pending")
    session.add(job)
    session.commit()
    return {"queued": True, "job_id": job.id}


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
    now = datetime.now(timezone.utc)
    if session is not None:
        return (
            session.query(OutboundMailJob)
            .filter(
                OutboundMailJob.mail_type.in_(AUTO_WORKFLOW_MAIL_TYPES),
                OutboundMailJob.status == "Pending",
                (OutboundMailJob.next_retry_at.is_(None)) | (OutboundMailJob.next_retry_at <= now),
            )
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


# 高优先级通道：发送 priority < 40（收件回执、业务推进、任务单）
_HIGH_PRIORITY_THRESHOLD = OUTBOUND_PRIORITY_NOTIFY

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
    job_ids = [
        row.id
        for row in (
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
    ]
    if not job_ids:
        return {"sent": 0, "failed": 0, "total": 0}
    return send_outbound_jobs_smtp(session, job_ids)


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
