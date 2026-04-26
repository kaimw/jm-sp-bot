from __future__ import annotations

import csv
import zipfile
from datetime import timedelta
from io import StringIO
from pathlib import Path

from sqlalchemy.orm import Session

from backend.app.config import settings
from backend.app.database import mask_database_url
from backend.app.models import AttachmentAsset, BackupJob, CleanupJob, MailMessage, ProcessingJob, now_utc
from backend.app.services.jsonutil import dumps
from backend.app.services.workflow import add_audit, get_config, weekly_report


def cleanup_preview(session: Session) -> dict:
    retention_days = int(get_config(session, "non_target_retention_days", str(settings.non_target_retention_days)))
    cutoff = now_utc() - timedelta(days=retention_days)
    mails = cleanup_candidate_mails(session, cutoff)
    attachment_rows = (
        session.query(AttachmentAsset)
        .filter(AttachmentAsset.mail_id.in_([mail.id for mail in mails]))
        .all()
        if mails
        else []
    )
    bytes_total = sum(asset.file_size or 0 for asset in attachment_rows)
    payload = {
        "retention_days": retention_days,
        "cutoff_at": cutoff.isoformat(),
        "mail_count": len(mails),
        "attachment_count": len(attachment_rows),
        "bytes_total": bytes_total,
        "mail_ids": [mail.id for mail in mails],
    }
    job = CleanupJob(job_type="NonTargetRetention", status="Preview", cutoff_at=cutoff, preview_json=dumps(payload))
    session.add(job)
    session.flush()
    add_audit(session, "CleanupPreviewCreated", "CleanupJob", job.id, payload)
    return {"cleanup_job_id": job.id, **payload}


def cleanup_candidate_mails(session: Session, cutoff) -> list[MailMessage]:
    return (
        session.query(MailMessage)
        .filter(
            MailMessage.related_task_id.is_(None),
            MailMessage.created_at < cutoff,
            MailMessage.classification.in_(["NonTarget", "BounceOrAutoReply"]),
        )
        .all()
    )


def execute_cleanup(session: Session, cleanup_job_id: str | None = None) -> dict:
    if cleanup_job_id:
        job = session.get(CleanupJob, cleanup_job_id)
        if job is None:
            raise ValueError("cleanup job not found")
        cutoff = job.cutoff_at
    else:
        retention_days = int(get_config(session, "non_target_retention_days", str(settings.non_target_retention_days)))
        cutoff = now_utc() - timedelta(days=retention_days)
        job = CleanupJob(job_type="NonTargetRetention", status="Preview", cutoff_at=cutoff, preview_json="{}")
        session.add(job)
        session.flush()

    mails = cleanup_candidate_mails(session, cutoff)
    mail_ids = [mail.id for mail in mails]
    attachments = (
        session.query(AttachmentAsset)
        .filter(AttachmentAsset.mail_id.in_(mail_ids))
        .all()
        if mail_ids
        else []
    )
    storage_refs = {asset.storage_ref for asset in attachments if asset.storage_ref}
    for asset in attachments:
        session.delete(asset)
    for mail in mails:
        session.delete(mail)
    old_jobs = session.query(ProcessingJob).filter(ProcessingJob.created_at < cutoff, ProcessingJob.status.in_(["Completed", "Failed"])).all()
    for row in old_jobs:
        session.delete(row)
    removed_files = 0
    for storage_ref in storage_refs:
        path = Path(storage_ref)
        if path.exists():
            path.unlink()
            removed_files += 1
    result = {
        "mail_count": len(mails),
        "attachment_count": len(attachments),
        "processing_job_count": len(old_jobs),
        "removed_files": removed_files,
        "cutoff_at": cutoff.isoformat(),
    }
    job.status = "Completed"
    job.result_json = dumps(result)
    job.executed_at = now_utc()
    add_audit(session, "CleanupExecuted", "CleanupJob", job.id, result)
    return {"cleanup_job_id": job.id, **result}


def create_backup(session: Session, backup_type: str = "Manual") -> BackupJob:
    backup_dir = Path("data/backups")
    backup_dir.mkdir(parents=True, exist_ok=True)
    timestamp = now_utc().strftime("%Y%m%d-%H%M%S")
    target = backup_dir / f"jm-sp-bot-{backup_type.lower()}-{timestamp}.zip"
    manifest = {
        "backup_type": backup_type,
        "created_at": now_utc().isoformat(),
        "database_url": mask_database_url(settings.database_url),
        "attachment_storage_dir": settings.attachment_storage_dir,
    }
    with zipfile.ZipFile(target, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        db_path = sqlite_db_path()
        if db_path and db_path.exists():
            archive.write(db_path, arcname="database/app.db")
            manifest["database_file"] = str(db_path)
        attachment_dir = Path(settings.attachment_storage_dir)
        if attachment_dir.exists():
            for path in attachment_dir.rglob("*"):
                if path.is_file():
                    archive.write(path, arcname=f"attachments/{path.relative_to(attachment_dir)}")
        archive.writestr("manifest.json", dumps(manifest))
    job = BackupJob(backup_type=backup_type, status="Completed", storage_ref=str(target), manifest_json=dumps(manifest))
    session.add(job)
    session.flush()
    add_audit(session, "BackupCreated", "BackupJob", job.id, {"storage_ref": str(target), "backup_type": backup_type})
    return job


def sqlite_db_path() -> Path | None:
    if not settings.database_url.startswith("sqlite:///"):
        return None
    raw = settings.database_url.removeprefix("sqlite:///")
    if raw == ":memory:":
        return None
    return Path(raw)


def storage_usage() -> dict:
    attachment_dir = Path(settings.attachment_storage_dir)
    total = 0
    files = 0
    if attachment_dir.exists():
        for path in attachment_dir.rglob("*"):
            if path.is_file():
                files += 1
                total += path.stat().st_size
    return {
        "attachment_files": files,
        "attachment_bytes": total,
        "storage_budget_bytes": settings.storage_budget_bytes,
    }


def weekly_report_csv_rows(session: Session) -> list[dict]:
    rows = []
    report_data = weekly_report(session)
    for key in ["week", "month", "year"]:
        period = report_data["periods"][key]
        stats = period["task_stats"]
        rows.append(
            {
                "section": "任务统计",
                "period": period["label"],
                "product": "",
                "salesperson": "",
                "demand_total": stats["demand_total"],
                "confirmed_total": stats["confirmed_total"],
                "unconfirmed_total": stats["unconfirmed_total"],
                "order_count": "",
            }
        )
        for row in period["confirmed_products"]:
            rows.append(
                {
                    "section": "已确认产品订单统计（分产品）",
                    "period": period["label"],
                    "product": row["product"],
                    "salesperson": "",
                    "demand_total": "",
                    "confirmed_total": "",
                    "unconfirmed_total": "",
                    "order_count": row["order_count"],
                }
            )
        for row in period["unconfirmed_products"]:
            rows.append(
                {
                    "section": "未确认产品订单统计（分产品）",
                    "period": period["label"],
                    "product": row["product"],
                    "salesperson": "",
                    "demand_total": "",
                    "confirmed_total": "",
                    "unconfirmed_total": "",
                    "order_count": row["order_count"],
                }
            )
        for row in period["sales_top10"]:
            rows.append(
                {
                    "section": "销售Top10统计（需求总数和已确认总数）",
                    "period": period["label"],
                    "product": "",
                    "salesperson": row["salesperson"],
                    "demand_total": row["demand_total"],
                    "confirmed_total": row["confirmed_total"],
                    "unconfirmed_total": "",
                    "order_count": "",
                }
            )
    return rows


def weekly_report_csv(session: Session) -> str:
    rows = weekly_report_csv_rows(session)
    fieldnames = [
        "section",
        "period",
        "product",
        "salesperson",
        "demand_total",
        "confirmed_total",
        "unconfirmed_total",
        "order_count",
    ]
    buffer = StringIO()
    writer = csv.DictWriter(buffer, fieldnames=fieldnames)
    writer.writeheader()
    writer.writerows(rows)
    return buffer.getvalue()
