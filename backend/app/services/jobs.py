from __future__ import annotations

from sqlalchemy import and_, or_
from sqlalchemy.orm import Session

from backend.app.models import MailMessage, ProcessingJob, now_utc
from backend.app.services.jsonutil import loads
from backend.app.services.workflow import process_inbound_mail


def run_pending_jobs(session: Session, *, limit: int = 20) -> dict:
    job_ids = [
        row.id
        for row in (
            session.query(ProcessingJob.id)
            .filter_by(status="Pending")
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
                    "updated_at": now_utc(),
                },
                synchronize_session=False,
            )
        )
        session.commit()
        if claimed != 1:
            continue
        handled += 1
        job = session.get(ProcessingJob, job_id)
        if job is None:
            continue
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
            else:
                raise RuntimeError(f"unknown job type: {job.job_type}")
            job.status = "Completed"
            job.error_message = None
            completed += 1
            job.updated_at = now_utc()
            session.commit()
        except Exception as exc:
            session.rollback()
            job = session.get(ProcessingJob, job_id)
            if job is None:
                continue
            job.status = "Failed"
            job.error_message = str(exc)
            job.updated_at = now_utc()
            failed += 1
            session.commit()
    return {"completed": completed, "failed": failed, "total": handled}
