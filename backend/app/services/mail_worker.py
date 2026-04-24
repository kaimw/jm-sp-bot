from __future__ import annotations

import logging

from sqlalchemy.orm import Session

from backend.app.config import MAIL_WORKER_MIN_INTERVAL_SECONDS, settings
from backend.app.database import SessionLocal
from backend.app.models import OutboundMailJob
from backend.app.services.jobs import run_pending_jobs
from backend.app.services.mail_adapter import AUTO_WORKFLOW_MAIL_TYPES, send_pending_auto_workflow_mails_smtp, sync_imap_mailbox
from backend.app.services.workflow import bot_enabled, get_config


logger = logging.getLogger(__name__)


def run_mail_auto_worker_once() -> dict:
    with SessionLocal() as session:
        if not bot_enabled(session):
            return {
                "enabled": False,
                "synced": {"imported": 0, "queued": 0, "skipped": "bot is disabled"},
                "processed": {"completed": 0, "failed": 0, "total": 0, "skipped": "bot is disabled"},
                "auto_workflow_mails": {"sent": 0, "failed": 0, "total": 0, "skipped": "bot is disabled"},
            }
        if not get_config(session, "bot_email_password", ""):
            return {"enabled": True, "skipped": "bot_email_password is not configured"}

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

        if pending_auto_workflow_mail_count(session) > 0:
            try:
                result["auto_workflow_mails"] = send_pending_auto_workflow_mails_smtp(session, limit=settings.mail_auto_worker_limit)
                session.commit()
            except Exception as exc:
                session.rollback()
                logger.exception("mail auto worker auto workflow send failed")
                result["auto_workflow_mails"] = {"sent": 0, "failed": 0, "total": 0, "error": str(exc)}
            result["synced"] = {"imported": 0, "queued": 0, "skipped": "pending outbound mail has priority"}
            return result

        try:
            result["synced"] = sync_imap_mailbox(session, limit=settings.mail_auto_worker_limit)
            session.commit()
        except Exception as exc:
            session.rollback()
            logger.exception("mail auto worker sync failed")
            result["synced"] = {"imported": 0, "queued": 0, "error": str(exc)}
        return result


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
