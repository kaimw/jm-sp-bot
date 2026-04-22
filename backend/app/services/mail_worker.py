from __future__ import annotations

import logging

from backend.app.config import settings
from backend.app.database import SessionLocal
from backend.app.models import OutboundMailJob
from backend.app.services.jobs import run_pending_jobs
from backend.app.services.mail_adapter import send_pending_auto_workflow_mails_smtp, sync_imap_mailbox
from backend.app.services.mail_throttle import mail_login_interval_seconds
from backend.app.services.workflow import get_config


logger = logging.getLogger(__name__)


def run_mail_auto_worker_once() -> dict:
    with SessionLocal() as session:
        if not get_config(session, "bot_email_password", ""):
            return {"enabled": True, "skipped": "bot_email_password is not configured"}

        result = {
            "enabled": True,
            "synced": {"imported": 0, "queued": 0},
            "processed": {"completed": 0, "failed": 0, "total": 0},
            "auto_workflow_mails": {"sent": 0, "failed": 0, "total": 0},
        }
        try:
            result["synced"] = sync_imap_mailbox(session, limit=settings.mail_auto_worker_limit)
            session.commit()
        except Exception as exc:
            session.rollback()
            logger.exception("mail auto worker sync failed")
            result["synced"] = {"imported": 0, "queued": 0, "error": str(exc)}
        try:
            result["processed"] = run_pending_jobs(session, limit=settings.mail_auto_worker_limit)
            session.commit()
        except Exception as exc:
            session.rollback()
            logger.exception("mail auto worker processing failed")
            result["processed"] = {"completed": 0, "failed": 0, "total": 0, "error": str(exc)}
        try:
            result["auto_workflow_mails"] = send_pending_auto_workflow_mails_smtp(session, limit=settings.mail_auto_worker_limit)
            session.commit()
        except Exception as exc:
            session.rollback()
            logger.exception("mail auto worker auto workflow send failed")
            result["auto_workflow_mails"] = {"sent": 0, "failed": 0, "total": 0, "error": str(exc)}
        return result


def pending_receipt_ack_count() -> int:
    with SessionLocal() as session:
        return session.query(OutboundMailJob).filter_by(mail_type="SalesReceiptAck", status="Pending").count()


def configured_mail_worker_interval_seconds() -> int:
    with SessionLocal() as session:
        return mail_login_interval_seconds(session)
