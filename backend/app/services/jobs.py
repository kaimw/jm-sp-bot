from __future__ import annotations

import uuid
from typing import Any
from datetime import datetime, timezone, timedelta

from sqlalchemy import and_, or_
from sqlalchemy.orm import Session

from backend.app.config import settings
from backend.app.models import MailMessage, ProcessingJob, now_utc
from backend.app.services.crm_sync import CrmSyncBusyError, run_crm_sales_order_sync
from backend.app.services.erp.material_sync import sync_erp_materials
from backend.app.services.exception_diagnosis import diagnose_exception_case
from backend.app.services.jsonutil import dumps, loads
from backend.app.services.order_middle_platform import DuplicateEventException, poll_oms_status_updates, process_crm_order_parsed_event, process_oms_push_notice, process_oms_status_update, process_oms_waybill_print, process_platform_fulfillment_sync
from backend.app.services.workflow import process_inbound_mail


REPEATABLE_JOB_TYPES = {"OMS_PUSH_NOTICE", "PLATFORM_FULFILLMENT_SYNC", "sync_crm_sales_orders", "OMS_STATUS_POLL"}

# 支持失败重试的 Job 类型（带指数退避）
RETRYABLE_JOB_TYPES = {
    "CRM_ORDER_PARSED",
    "OMS_PUSH_NOTICE",
    "OMS_STATUS_POLL",
    "PLATFORM_FULFILLMENT_SYNC",
    "DIAGNOSE_EXCEPTION",
}
MAX_JOB_ATTEMPTS = 5  # 最大重试次数（含首次）
RETRY_BASE_DELAY_SECONDS = 10  # 基础退避秒数


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


def schedule_oms_status_poll_if_due(session: Session) -> dict[str, Any]:
    """OMS 状态轮询定时调度。每分钟由 Worker 调用一次。"""
    from backend.app.services.order_middle_platform import config_bool, config_int, config_value
    from backend.app.services.bootstrap import set_config
    if not config_bool(session, "oms_enabled", False):
        return {"queued": False, "reason": "oms_disabled"}
    if config_bool(session, "oms_mock_success", True):
        return {"queued": False, "reason": "oms_mock_mode"}
    interval = max(60, config_int(session, "oms_status_poll_interval_seconds", 300))
    last_poll = config_value(session, "oms_status_last_poll_at", "").strip()
    if last_poll:
        try:
            last = datetime.fromisoformat(last_poll)
            if (datetime.now(timezone.utc) - last).total_seconds() < interval:
                return {"queued": False, "reason": "not due"}
        except ValueError:
            pass
    existing = (
        session.query(ProcessingJob)
        .filter(ProcessingJob.job_type == "OMS_STATUS_POLL", ProcessingJob.status.in_(["Pending", "Running"]))
        .first()
    )
    if existing is not None:
        return {"queued": False, "reason": "already queued"}
    job = ProcessingJob(job_type="OMS_STATUS_POLL", payload_json=dumps({"limit": 50, "source": "scheduled"}), status="Pending")
    session.add(job)
    set_config(session, "oms_status_last_poll_at", datetime.now(timezone.utc).isoformat())
    session.commit()
    return {"queued": True, "job_id": job.id}


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
        if job.job_type not in REPEATABLE_JOB_TYPES:
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
            elif job.job_type == "sync_oms_materials":
                from backend.app.services.oms.material_sync import sync_oms_materials
                sync_oms_materials(session)
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
        except CrmSyncBusyError as exc:
            session.rollback()
            job = session.get(ProcessingJob, job_id)
            if job is None:
                continue
            job.status = "Pending"
            job.error_message = str(exc)
            job.next_retry_at = now_utc() + timedelta(seconds=60)
            clear_processing_lock(job)
            job.version += 1
            job.updated_at = now_utc()
            session.commit()
        except Exception as exc:
            session.rollback()
            job = session.get(ProcessingJob, job_id)
            if job is None:
                continue
            should_retry = (
                job.job_type in RETRYABLE_JOB_TYPES
                and (job.attempt_count or 0) < MAX_JOB_ATTEMPTS
            )
            if should_retry:
                attempts = (job.attempt_count or 0)
                delay = min(RETRY_BASE_DELAY_SECONDS * (2 ** (attempts - 1)), 3600)
                job.status = "Pending"
                job.error_message = f"[Attempt {attempts}/{MAX_JOB_ATTEMPTS}] {exc}"
                job.next_retry_at = now_utc() + timedelta(seconds=delay)
            else:
                job.status = "Failed"
                job.error_message = str(exc)
                failed += 1
            clear_processing_lock(job)
            job.version += 1
            job.updated_at = now_utc()
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
