from __future__ import annotations

import hashlib
import json
import os
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from sqlalchemy import or_
from sqlalchemy.orm import Session

from backend.app.models import CrmSalesOrder, CrmSyncRun, ProcessingJob, SystemConfig, now_utc
from backend.app.services.bootstrap import set_config
from backend.app.services.jsonutil import dumps, loads


DEFAULT_SOURCE_SYSTEM = "fxiaoke"
DEFAULT_CDP_URL = "http://127.0.0.1:9333"


def config_value(session: Session, key: str, default: str = "") -> str:
    row = session.get(SystemConfig, key)
    if row is None or row.value is None:
        return default
    return str(row.value)


def config_bool(session: Session, key: str, default: bool = False) -> bool:
    value = config_value(session, key, "")
    if value == "":
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def config_int(session: Session, key: str, default: int) -> int:
    try:
        return int(config_value(session, key, str(default)))
    except (TypeError, ValueError):
        return default


def crm_order_sync_due(session: Session, *, now: datetime | None = None) -> bool:
    if not config_bool(session, "crm_sync_enabled", False):
        return False
    interval = max(60, config_int(session, "crm_sync_interval_seconds", 3600))
    last_sync = config_value(session, "crm_sales_orders_last_sync_at", "").strip()
    if not last_sync:
        return True
    try:
        last = datetime.fromisoformat(last_sync)
    except ValueError:
        return True
    current = now or now_utc()
    if last.tzinfo is None and current.tzinfo is not None:
        current = current.replace(tzinfo=None)
    return (current - last).total_seconds() >= interval


def schedule_crm_order_sync_if_due(session: Session) -> dict[str, Any]:
    if not crm_order_sync_due(session):
        return {"queued": False, "reason": "not due"}
    existing = (
        session.query(ProcessingJob)
        .filter(ProcessingJob.job_type == "sync_crm_sales_orders", ProcessingJob.status.in_(["Pending", "Running"]))
        .first()
    )
    if existing is not None:
        return {"queued": False, "reason": "already queued", "job_id": existing.id}
    job = ProcessingJob(job_type="sync_crm_sales_orders", payload_json=dumps({"source": "auto"}), status="Pending")
    session.add(job)
    session.commit()
    return {"queued": True, "job_id": job.id}


def queue_crm_order_sync(session: Session, *, source: str = "manual") -> dict[str, Any]:
    existing = (
        session.query(ProcessingJob)
        .filter(ProcessingJob.job_type == "sync_crm_sales_orders", ProcessingJob.status.in_(["Pending", "Running"]))
        .first()
    )
    if existing is not None:
        return {"queued": False, "reason": "already queued", "job_id": existing.id}
    job = ProcessingJob(job_type="sync_crm_sales_orders", payload_json=dumps({"source": source}), status="Pending")
    session.add(job)
    session.commit()
    return {"queued": True, "job_id": job.id}


def run_crm_sales_order_sync(session: Session, *, trigger: str = "manual") -> dict[str, Any]:
    sync_run = CrmSyncRun(source_system=DEFAULT_SOURCE_SYSTEM, sync_type="sales_orders", status="Running", trigger=trigger)
    session.add(sync_run)
    session.flush()

    try:
        rows, command_summary = fetch_sales_orders_via_replay(session)
        result = upsert_crm_sales_orders(session, rows)
        sync_run.status = "Completed"
        sync_run.finished_at = now_utc()
        sync_run.created_count = result["created"]
        sync_run.updated_count = result["updated"]
        sync_run.unchanged_count = result["unchanged"]
        sync_run.total_count = result["total"]
        sync_run.detail_json = dumps({"command": command_summary, "source_total": len(rows)})
        set_config(session, "crm_sales_orders_last_sync_at", now_utc().isoformat(), is_secret=False)
        session.commit()
        return {"ok": True, "sync_run_id": sync_run.id, **result, "command": command_summary}
    except Exception as exc:
        session.rollback()
        sync_run = session.get(CrmSyncRun, sync_run.id)
        if sync_run is None:
            sync_run = CrmSyncRun(source_system=DEFAULT_SOURCE_SYSTEM, sync_type="sales_orders", status="Failed", trigger=trigger)
            session.add(sync_run)
        sync_run.status = "Failed"
        sync_run.finished_at = now_utc()
        sync_run.error_message = str(exc)
        sync_run.detail_json = dumps({"error_type": exc.__class__.__name__})
        session.commit()
        raise


def fetch_sales_orders_via_replay(session: Session) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    request_path = config_value(session, "crm_fxiaoke_request_file", "").strip()
    request_json = config_value(session, "crm_fxiaoke_request_json", "").strip()
    cdp_url = config_value(session, "crm_cdp_url", DEFAULT_CDP_URL).strip() or DEFAULT_CDP_URL
    node_bin = config_value(session, "crm_node_bin", "node").strip() or "node"
    page_size = str(max(1, config_int(session, "crm_sync_page_size", 20)))
    script_path = Path(__file__).resolve().parents[3] / "scripts" / "fxiaoke_replay_sales_orders.mjs"

    if not script_path.exists():
        raise RuntimeError(f"CRM 同步脚本不存在：{script_path}")
    if not request_path and not request_json:
        raise RuntimeError("请先配置 crm_fxiaoke_request_file 或 crm_fxiaoke_request_json")

    temp_request_path: Path | None = None
    try:
        if request_json and not request_path:
            temp_request_path = Path("/private/tmp") / f"fxiaoke-list-request-{hashlib.sha1(request_json.encode()).hexdigest()[:12]}.json"
            temp_request_path.write_text(request_json, encoding="utf-8")
            request_path = str(temp_request_path)

        command = [node_bin, str(script_path), f"--request={request_path}"]
        env = {"FXIAOKE_CDP_URL": cdp_url, "FXIAOKE_PAGE_SIZE": page_size}
        completed = subprocess.run(
            command,
            cwd=str(Path(__file__).resolve().parents[3]),
            env={**os.environ, **env},
            capture_output=True,
            text=True,
            timeout=max(30, config_int(session, "crm_sync_timeout_seconds", 120)),
            check=False,
        )
        if completed.returncode != 0:
            raise RuntimeError((completed.stderr or completed.stdout or "CRM 同步脚本执行失败").strip())
        output = json.loads(completed.stdout)
        json_path = output.get("jsonPath")
        if not json_path:
            raise RuntimeError("CRM 同步脚本未返回 jsonPath")
        data = json.loads(Path(json_path).read_text(encoding="utf-8"))
        rows = data.get("rows") or []
        if not isinstance(rows, list):
            raise RuntimeError("CRM 同步脚本返回 rows 格式错误")
        return rows, {
            "cdp_url": cdp_url,
            "request_file": request_path,
            "json_path": json_path,
            "csv_path": output.get("csvPath"),
            "pages": output.get("pages", []),
        }
    finally:
        if temp_request_path is not None:
            try:
                temp_request_path.unlink()
            except FileNotFoundError:
                pass


def payload_hash(row: dict[str, Any]) -> str:
    stable = json.dumps(row, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(stable.encode("utf-8")).hexdigest()


def upsert_crm_sales_orders(session: Session, rows: list[dict[str, Any]]) -> dict[str, int]:
    created = 0
    updated = 0
    unchanged = 0
    for row in rows:
        crm_order_id = str(row.get("crm_order_id") or "").strip()
        crm_order_no = str(row.get("crm_order_no") or "").strip()
        if not crm_order_id and not crm_order_no:
            continue
        digest = payload_hash(row)
        filters = []
        if crm_order_id:
            filters.append(CrmSalesOrder.crm_order_id == crm_order_id)
        if crm_order_no:
            filters.append(CrmSalesOrder.crm_order_no == crm_order_no)
        existing = (
            session.query(CrmSalesOrder)
            .filter(CrmSalesOrder.source_system == DEFAULT_SOURCE_SYSTEM, or_(*filters))
            .first()
        )
        if existing is None:
            existing = CrmSalesOrder(
                source_system=DEFAULT_SOURCE_SYSTEM,
                crm_order_id=crm_order_id or crm_order_no,
                crm_order_no=crm_order_no or crm_order_id,
                payload_hash=digest,
            )
            session.add(existing)
            created += 1
        elif existing.payload_hash == digest:
            unchanged += 1
        else:
            updated += 1

        apply_order_row(existing, row, digest)
    session.flush()
    return {"created": created, "updated": updated, "unchanged": unchanged, "total": created + updated + unchanged}


def parse_datetime(value: Any) -> datetime | None:
    text = str(value or "").strip()
    if not text:
        return None
    try:
        normalized = text.replace("Z", "+00:00")
        return datetime.fromisoformat(normalized)
    except ValueError:
        return None


def infer_currency(settlement_method: str | None) -> str | None:
    text = str(settlement_method or "").upper()
    for code in ("CNY", "USD", "EUR", "JPY", "HKD"):
        if code in text:
            return code
    return None


def apply_order_row(order: CrmSalesOrder, row: dict[str, Any], digest: str) -> None:
    order.crm_order_id = str(row.get("crm_order_id") or order.crm_order_id or "").strip()
    order.crm_order_no = str(row.get("crm_order_no") or order.crm_order_no or "").strip()
    order.customer_id = str(row.get("customer_id") or "").strip() or None
    order.customer_name = str(row.get("customer_name") or "").strip() or None
    order.opportunity_id = str(row.get("opportunity_id") or "").strip() or None
    order.opportunity_name = str(row.get("opportunity_name") or "").strip() or None
    order.sales_user_id = str(row.get("sales_user_id") or "").strip() or None
    order.sales_user_name = str(row.get("sales_user_name") or "").strip() or None
    order.owner_department = str(row.get("owner_department") or "").strip() or None
    order.life_status = str(row.get("life_status") or "").strip() or None
    order.approval_status = str(row.get("approval_status") or "").strip() or None
    order.order_date = str(row.get("order_date") or "").strip() or None
    order.settlement_method = str(row.get("settlement_method") or "").strip() or None
    order.currency = infer_currency(order.settlement_method)
    order.order_amount = str(row.get("order_amount") or "").strip() or None
    order.received_amount = str(row.get("received_amount") or "").strip() or None
    order.receivable_amount = str(row.get("receivable_amount") or "").strip() or None
    order.invoice_amount = str(row.get("invoice_amount") or "").strip() or None
    order.product_amount = str(row.get("product_amount") or "").strip() or None
    order.logistics_status = str(row.get("logistics_status") or "").strip() or None
    order.shipment_status = str(row.get("shipment_status") or "").strip() or None
    order.invoice_status = str(row.get("invoice_status") or "").strip() or None
    order.receipt_contact = str(row.get("receipt_contact") or "").strip() or None
    order.receipt_address = str(row.get("receipt_address") or "").strip() or None
    order.delivery_date = str(row.get("delivery_date") or "").strip() or None
    order.remark = str(row.get("remark") or "").strip() or None
    order.attachment_files_json = dumps([item.strip() for item in str(row.get("attachment_files") or "").split(";") if item.strip()])
    order.raw_json = dumps(row)
    order.payload_hash = digest
    order.sync_status = "Synced"
    order.synced_at = now_utc()
    order.source_created_at = parse_datetime(row.get("created_at"))
    order.source_updated_at = parse_datetime(row.get("updated_at"))
    order.updated_at = now_utc()


def latest_crm_sync_run(session: Session) -> CrmSyncRun | None:
    return session.query(CrmSyncRun).order_by(CrmSyncRun.started_at.desc()).first()


def crm_order_summary(session: Session) -> dict[str, Any]:
    rows = session.query(CrmSalesOrder).all()
    total = len(rows)
    latest = latest_crm_sync_run(session)
    pending_job = (
        session.query(ProcessingJob)
        .filter(ProcessingJob.job_type == "sync_crm_sales_orders", ProcessingJob.status.in_(["Pending", "Running"]))
        .order_by(ProcessingJob.created_at)
        .first()
    )
    def amount_sum(field: str) -> float:
        total_amount = 0.0
        for row in rows:
            value = getattr(row, field, None)
            if value in (None, ""):
                continue
            try:
                total_amount += float(str(value).replace(",", ""))
            except ValueError:
                continue
        return round(total_amount, 2)

    latest_serialized = serialize_sync_run(latest) if latest else None
    return {
        "total": total,
        "total_orders": total,
        "total_order_amount": amount_sum("order_amount"),
        "total_received_amount": amount_sum("received_amount"),
        "total_receivable_amount": amount_sum("receivable_amount"),
        "last_sync_at": config_value(session, "crm_sales_orders_last_sync_at", ""),
        "sync_enabled": config_bool(session, "crm_sync_enabled", False),
        "sync_interval_seconds": config_int(session, "crm_sync_interval_seconds", 3600),
        "cdp_url": config_value(session, "crm_cdp_url", DEFAULT_CDP_URL),
        "request_file": config_value(session, "crm_fxiaoke_request_file", ""),
        "has_request_json": bool(config_value(session, "crm_fxiaoke_request_json", "").strip()),
        "latest_run": latest_serialized,
        "last_run": latest_serialized,
        "pending_job": {"id": pending_job.id, "status": pending_job.status} if pending_job else None,
    }


def serialize_sync_run(row: CrmSyncRun) -> dict[str, Any]:
    return {
        "id": row.id,
        "source_system": row.source_system,
        "sync_type": row.sync_type,
        "status": row.status,
        "trigger": row.trigger,
        "started_at": row.started_at.isoformat() if row.started_at else None,
        "finished_at": row.finished_at.isoformat() if row.finished_at else None,
        "created_count": row.created_count,
        "updated_count": row.updated_count,
        "unchanged_count": row.unchanged_count,
        "total_count": row.total_count,
        "error_message": row.error_message,
        "detail": loads(row.detail_json, {}),
    }
