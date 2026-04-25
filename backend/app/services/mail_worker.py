from __future__ import annotations

import logging
from datetime import datetime, timezone
from time import monotonic

from sqlalchemy.orm import Session

from backend.app.config import MAIL_WORKER_MIN_INTERVAL_SECONDS, settings
from backend.app.database import SessionLocal
from backend.app.models import OutboundMailJob
from backend.app.services.jobs import run_pending_jobs
from backend.app.services.mail_adapter import AUTO_WORKFLOW_MAIL_TYPES, send_pending_auto_workflow_mails_smtp, sync_imap_mailbox
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
            if not get_config(session, "bot_email_password", ""):
                return _finish_worker_run({"enabled": True, "skipped": "bot_email_password is not configured"}, started)

            result = {
                "enabled": True,
                "synced": {"imported": 0, "queued": 0},
                "processed": {"completed": 0, "failed": 0, "total": 0},
                "auto_workflow_mails": {"sent": 0, "failed": 0, "total": 0},
            }
            try:
                result["processed"] = run_pending_jobs(session, limit=settings.mail_auto_worker_limit)
                session.commit()
            except Exception as exc:
                session.rollback()
                logger.exception("mail auto worker processing failed")
                result["processed"] = {"completed": 0, "failed": 0, "total": 0, "error": str(exc)}

            try:
                pending_auto_count = pending_auto_workflow_mail_count(session)
            except Exception as exc:
                pending_auto_count = 0
                logger.exception("mail auto worker pending count failed")
                result["auto_workflow_mails"] = {"sent": 0, "failed": 0, "total": 0, "error": str(exc)}

            if pending_auto_count > 0:
                try:
                    result["auto_workflow_mails"] = send_pending_auto_workflow_mails_smtp(session, limit=settings.mail_auto_worker_limit)
                    session.commit()
                except Exception as exc:
                    session.rollback()
                    logger.exception("mail auto worker auto workflow send failed")
                    result["auto_workflow_mails"] = {"sent": 0, "failed": 0, "total": 0, "error": str(exc)}
                result["synced"] = {"imported": 0, "queued": 0, "skipped": "pending outbound mail has priority"}
                return _finish_worker_run(result, started)

            try:
                result["synced"] = sync_imap_mailbox(session, limit=settings.mail_auto_worker_limit)
                session.commit()
            except Exception as exc:
                session.rollback()
                logger.exception("mail auto worker sync failed")
                result["synced"] = {"imported": 0, "queued": 0, "error": str(exc)}
            return _finish_worker_run(result, started)
        except Exception as exc:
            _WORKER_STATUS["last_error"] = str(exc)
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
