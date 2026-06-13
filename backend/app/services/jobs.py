from __future__ import annotations

import uuid
from datetime import timedelta

from sqlalchemy import and_, or_
from sqlalchemy.orm import Session

from backend.app.config import settings
from backend.app.models import MailMessage, ProcessingJob, now_utc
from backend.app.services.crm_sync import run_crm_sales_order_sync
from backend.app.services.erp.material_sync import sync_erp_materials
from backend.app.services.exception_diagnosis import diagnose_exception_case
from backend.app.services.jsonutil import loads
from backend.app.services.order_middle_platform import DuplicateEventException, poll_oms_status_updates, process_crm_order_parsed_event, process_oms_push_notice, process_oms_status_update, process_oms_waybill_print, process_platform_fulfillment_sync
from backend.app.services.workflow import process_inbound_mail


def run_platform_sync_async(job_id: str, payload: dict) -> None:
    from backend.app.database import SessionLocal
    from backend.app.services.order_middle_platform import process_platform_fulfillment_sync
    import logging
    logger = logging.getLogger(__name__)

    with SessionLocal() as db_session:
        job = db_session.get(ProcessingJob, job_id)
        if not job:
            return
        try:
            process_platform_fulfillment_sync(db_session, payload)
            job.status = "Completed"
            job.error_message = None
            clear_processing_lock(job)
            job.version += 1
            job.updated_at = now_utc()
            db_session.commit()
        except Exception as exc:
            db_session.rollback()
            job = db_session.get(ProcessingJob, job_id)
            if job:
                job.status = "Failed"
                job.error_message = str(exc)
                clear_processing_lock(job)
                job.version += 1
                job.updated_at = now_utc()
                db_session.commit()
            logger.exception(f"Async platform fulfillment sync failed for job {job_id}: {exc}")


def run_pending_jobs(session: Session, *, limit: int = 20) -> dict:
    recover_stale_processing_jobs(session)
    worker = f"processing-{uuid.uuid4()}"
    job_ids = [
        row.id
        for row in (
            session.query(ProcessingJob.id)
            .filter_by(status="Pending")
            .filter(or_(ProcessingJob.next_retry_at.is_(None), ProcessingJob.next_retry_at <= now_utc()))
            .order_by(ProcessingJob.created_at)
            .limit(limit)
            .all()
        )
    ]
    completed = 0
    failed = 0
    handled = 0
    for job_id in job_ids:
        claimed = (
            session.query(ProcessingJob)
            .filter(ProcessingJob.id == job_id, ProcessingJob.status == "Pending")
            .update(
                {
                    "status": "Running",
                    "attempt_count": ProcessingJob.attempt_count + 1,
                    "version": ProcessingJob.version + 1,
                    "locked_by": worker,
                    "locked_until": now_utc() + timedelta(seconds=settings.processing_job_lease_seconds),
                    "started_at": now_utc(),
                    "updated_at": now_utc(),
                },
                synchronize_session=False,
            )
        )
        session.commit()
        if claimed != 1:
            continue
        session.expire_all()
        handled += 1
        job = session.get(ProcessingJob, job_id)
        if job is None:
            continue
        duplicate = None
        if job.job_type != "OMS_PUSH_NOTICE" and job.job_type != "PLATFORM_FULFILLMENT_SYNC":
            duplicate = (
                session.query(ProcessingJob)
                .filter(
                    ProcessingJob.id != job.id,
                    ProcessingJob.job_type == job.job_type,
                    ProcessingJob.payload_json == job.payload_json,
                    ProcessingJob.status.in_(["Running", "Completed"]),
                    or_(
                        ProcessingJob.created_at < job.created_at,
                        and_(ProcessingJob.created_at == job.created_at, ProcessingJob.id < job.id),
                    ),
                )
                .order_by(ProcessingJob.created_at, ProcessingJob.id)
                .first()
            )
        if duplicate is not None:
            job.status = "Completed"
            job.error_message = f"Skipped duplicate processing job {duplicate.id}"
            job.version += 1
            job.updated_at = now_utc()
            completed += 1
            session.commit()
            continue
        try:
            payload = loads(job.payload_json, {})
            if job.job_type == "process_inbound_mail":
                mail = session.get(MailMessage, payload["mail_id"])
                if mail is None:
                    raise RuntimeError("mail not found")
                process_inbound_mail(session, mail)
            elif job.job_type == "sync_erp_materials":
                sync_erp_materials(session)
            elif job.job_type == "sync_crm_sales_orders":
                payload = loads(job.payload_json, {})
                run_crm_sales_order_sync(session, trigger=str(payload.get("source") or "job"))
            elif job.job_type == "CRM_ORDER_PARSED":
                try:
                    process_crm_order_parsed_event(session, loads(job.payload_json, {}))
                except DuplicateEventException as exc:
                    job.error_message = str(exc)
                    job.status = "Completed"
                    clear_processing_lock(job)
                    completed += 1
                    job.version += 1
                    job.updated_at = now_utc()
                    session.commit()
                    continue
            elif job.job_type == "OMS_PUSH_NOTICE":
                process_oms_push_notice(session, loads(job.payload_json, {}))
            elif job.job_type == "OMS_STATUS_SYNC":
                process_oms_status_update(session, loads(job.payload_json, {}))
            elif job.job_type == "OMS_STATUS_POLL":
                poll_oms_status_updates(session, limit=int(payload.get("limit") or 50))
            elif job.job_type == "OMS_WAYBILL_PRINT":
                process_oms_waybill_print(session, payload)
            elif job.job_type == "PLATFORM_FULFILLMENT_SYNC":
                from backend.app.services.order_middle_platform import config_bool
                if config_bool(session, "platform_fulfillment_sync_async", True):
                    import threading
                    thread = threading.Thread(
                        target=run_platform_sync_async,
                        args=(job.id, payload),
                        name=f"platform-sync-{job.id}"
                    )
                    thread.start()
                    continue
                else:
                    process_platform_fulfillment_sync(session, payload)
            elif job.job_type == "DIAGNOSE_EXCEPTION":
                diagnose_exception_case(session, str(payload.get("exception_id") or ""))
            else:
                raise RuntimeError(f"unknown job type: {job.job_type}")
            job.status = "Completed"
            job.error_message = None
            clear_processing_lock(job)
            completed += 1
            job.version += 1
            job.updated_at = now_utc()
            session.commit()
        except Exception as exc:
            session.rollback()
            job = session.get(ProcessingJob, job_id)
            if job is None:
                continue
            job.status = "Failed"
            job.error_message = str(exc)
            clear_processing_lock(job)
            job.version += 1
            job.updated_at = now_utc()
            failed += 1
            session.commit()
    return {"completed": completed, "failed": failed, "total": handled}


def clear_processing_lock(job: ProcessingJob) -> None:
    job.locked_by = None
    job.locked_until = None
    job.started_at = None


def recover_stale_processing_jobs(session: Session) -> int:
    now = now_utc()
    stale_jobs = (
        session.query(ProcessingJob)
        .filter(
            ProcessingJob.status == "Running",
            ProcessingJob.locked_until.is_not(None),
            ProcessingJob.locked_until < now,
        )
        .all()
    )
    for job in stale_jobs:
        job.status = "Pending"
        job.error_message = "processing lease expired; job returned to pending"
        clear_processing_lock(job)
        job.version += 1
        job.updated_at = now
    if stale_jobs:
        session.commit()
    return len(stale_jobs)
