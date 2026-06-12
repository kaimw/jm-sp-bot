from __future__ import annotations

import hashlib
import json
import os
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from sqlalchemy import func, or_
from sqlalchemy.orm import Session

from backend.app.models import CrmOrderSnapshot, CrmSalesOrder, OrderAttachment, CrmSyncRun, ProcessingJob, SystemConfig, now_utc
from backend.app.services.crm_attachment_cache import cache_order_attachment_file
from backend.app.services.crm_attachment_extraction import enrich_order_from_registered_attachments
from backend.app.services.bootstrap import set_config
from backend.app.services.jsonutil import dumps, loads
from backend.app.services.order_middle_platform import enqueue_crm_order_parsed_event


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


def run_crm_integration_test(session: Session) -> dict[str, Any]:
    node_bin = config_value(session, "crm_node_bin", "node").strip() or "node"
    timeout_seconds = max(30, min(600, config_int(session, "crm_sync_timeout_seconds", 120)))
    script_path = Path(__file__).resolve().parents[3] / "scripts" / "fxiaoke_integration_smoke.mjs"
    if not script_path.exists():
        raise RuntimeError(f"CRM 接入测试脚本不存在：{script_path}")

    completed = subprocess.run(
        [node_bin, str(script_path)],
        cwd=str(Path(__file__).resolve().parents[3]),
        env={**os.environ},
        capture_output=True,
        text=True,
        timeout=timeout_seconds,
        check=False,
    )
    stdout = (completed.stdout or "").strip()
    stderr = (completed.stderr or "").strip()
    if completed.returncode != 0:
        raise RuntimeError(stderr or stdout or "CRM 接入测试执行失败")
    try:
        output = json.loads(stdout)
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"CRM 接入测试输出不是 JSON：{stdout[:500]}") from exc
    output["command"] = {
        "script": str(script_path),
        "timeout_seconds": timeout_seconds,
        "stderr": stderr,
    }
    return output


def fetch_sales_orders_via_replay(session: Session) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    request_path = config_value(session, "crm_fxiaoke_request_file", "").strip()
    request_json = config_value(session, "crm_fxiaoke_request_json", "").strip()
    detail_request_path = config_value(session, "crm_fxiaoke_detail_request_file", "").strip()
    detail_request_json = config_value(session, "crm_fxiaoke_detail_request_json", "").strip()
    cdp_url = config_value(session, "crm_cdp_url", DEFAULT_CDP_URL).strip() or DEFAULT_CDP_URL
    node_bin = config_value(session, "crm_node_bin", "node").strip() or "node"
    page_size = str(max(1, config_int(session, "crm_sync_page_size", 20)))
    script_path = Path(__file__).resolve().parents[3] / "scripts" / "fxiaoke_replay_sales_orders.mjs"

    if not script_path.exists():
        raise RuntimeError(f"CRM 同步脚本不存在：{script_path}")
    if not request_path and not request_json:
        raise RuntimeError("请先配置 crm_fxiaoke_request_file 或 crm_fxiaoke_request_json")

    temp_request_path: Path | None = None
    temp_detail_request_path: Path | None = None
    try:
        if request_json and not request_path:
            temp_request_path = Path("/private/tmp") / f"fxiaoke-list-request-{hashlib.sha1(request_json.encode()).hexdigest()[:12]}.json"
            temp_request_path.write_text(request_json, encoding="utf-8")
            request_path = str(temp_request_path)
        if detail_request_json and not detail_request_path:
            temp_detail_request_path = Path("/private/tmp") / f"fxiaoke-detail-request-{hashlib.sha1(detail_request_json.encode()).hexdigest()[:12]}.json"
            temp_detail_request_path.write_text(detail_request_json, encoding="utf-8")
            detail_request_path = str(temp_detail_request_path)

        command = [node_bin, str(script_path), f"--request={request_path}"]
        if detail_request_path:
            command.append(f"--detail-request={detail_request_path}")
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
            "detail_request_file": detail_request_path,
            "json_path": json_path,
            "csv_path": output.get("csvPath"),
            "pages": output.get("pages", []),
            "detail_pages": output.get("detailPages", []),
        }
    finally:
        if temp_request_path is not None:
            try:
                temp_request_path.unlink()
            except FileNotFoundError:
                pass
        if temp_detail_request_path is not None:
            try:
                temp_detail_request_path.unlink()
            except FileNotFoundError:
                pass


def payload_hash(row: dict[str, Any]) -> str:
    stable = json.dumps(row, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(stable.encode("utf-8")).hexdigest()


def normalized_text(value: Any) -> str:
    return str(value or "").strip()


def normalized_lower(value: Any) -> str:
    return normalized_text(value).lower()


def config_json(session: Session, key: str, default: Any) -> Any:
    try:
        return loads(config_value(session, key, ""), default)
    except Exception:
        return default


def phase_one_scope_result(session: Session, row: dict[str, Any], existing: CrmSalesOrder | None) -> tuple[bool, str | None]:
    if not config_bool(session, "v2_crm_phase1_scope_enabled", True):
        return True, None
    scope = config_json(session, "v2_crm_phase1_scope_json", {})
    approved_values = {str(item).strip().lower() for item in scope.get("approved_values", []) if str(item).strip()}
    cancelled_values = {str(item).strip().lower() for item in scope.get("cancelled_values", []) if str(item).strip()}
    approval_status = normalized_lower(row.get("approval_status"))
    life_status = normalized_lower(row.get("life_status"))
    if approval_status in cancelled_values or life_status in cancelled_values:
        if existing is not None:
            return True, None
        return False, "crm_order_cancelled_before_middle_platform"
    if approval_status and approved_values and approval_status not in approved_values:
        return False, f"approval_status_not_in_phase1_scope:{row.get('approval_status')}"
    list_filters = {
        "include_owner_departments": row.get("owner_department"),
        "include_settlement_methods": row.get("settlement_method"),
        "include_customer_names": row.get("customer_name"),
    }
    for key, value in list_filters.items():
        allowed = {str(item).strip() for item in scope.get(key, []) if str(item).strip()}
        if allowed and normalized_text(value) not in allowed:
            return False, f"{key}_not_in_phase1_scope:{value or ''}"
    return True, None


def upsert_crm_sales_orders(session: Session, rows: list[dict[str, Any]]) -> dict[str, int]:
    created = 0
    updated = 0
    unchanged = 0
    ignored = 0
    changed_orders: list[CrmSalesOrder] = []
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
        was_new = existing is None
        row_changed = False
        if existing is None:
            existing = CrmSalesOrder(
                source_system=DEFAULT_SOURCE_SYSTEM,
                crm_order_id=crm_order_id or crm_order_no,
                crm_order_no=crm_order_no or crm_order_id,
                payload_hash=digest,
            )
            session.add(existing)
            created += 1
            row_changed = True
        elif existing.payload_hash == digest:
            unchanged += 1
        else:
            updated += 1
            row_changed = True

        apply_order_row(existing, row, digest)
        session.flush()
        snapshot = save_order_snapshot(session, existing, row, digest)
        existing.latest_snapshot_id = snapshot.id
        sync_order_attachments(session, existing, row, digest)
        enrich_order_from_registered_attachments(session, existing)
        in_scope, ignore_reason = phase_one_scope_result(session, row, None if was_new else existing)
        if in_scope:
            existing.scope_status = "InScope"
            existing.scope_ignore_reason = None
            if row_changed:
                changed_orders.append(existing)
        else:
            existing.scope_status = "Ignored"
            existing.scope_ignore_reason = ignore_reason
            existing.sync_status = "Ignored"
            ignored += 1
    session.flush()
    queued = 0
    for order in changed_orders:
        enqueue_crm_order_parsed_event(session, order)
        queued += 1
    return {"created": created, "updated": updated, "unchanged": unchanged, "ignored": ignored, "queued_events": queued, "total": created + updated + unchanged}


def save_order_snapshot(session: Session, order: CrmSalesOrder, row: dict[str, Any], digest: str) -> CrmOrderSnapshot:
    existing = (
        session.query(CrmOrderSnapshot)
        .filter(
            CrmOrderSnapshot.source_system == order.source_system,
            CrmOrderSnapshot.crm_order_id == order.crm_order_id,
            CrmOrderSnapshot.payload_hash == digest,
        )
        .first()
    )
    previous_latest = session.query(CrmOrderSnapshot).filter(
        CrmOrderSnapshot.source_system == order.source_system,
        CrmOrderSnapshot.crm_order_id == order.crm_order_id,
        CrmOrderSnapshot.is_latest.is_(True),
    ).all()
    for snapshot in previous_latest:
        snapshot.is_latest = False
    if existing is not None:
        existing.crm_sales_order_id = order.id
        existing.crm_order_no = order.crm_order_no
        existing.raw_json = dumps(row)
        existing.parse_status = "Parsed"
        existing.is_latest = True
        existing.captured_at = now_utc()
        return existing
    latest_version = (
        session.query(func.max(CrmOrderSnapshot.version))
        .filter(
            CrmOrderSnapshot.source_system == order.source_system,
            CrmOrderSnapshot.crm_order_id == order.crm_order_id,
        )
        .scalar()
        or 0
    )
    snapshot = CrmOrderSnapshot(
        crm_sales_order_id=order.id,
        source_system=order.source_system,
        crm_order_id=order.crm_order_id,
        crm_order_no=order.crm_order_no,
        payload_hash=digest,
        version=int(latest_version) + 1,
        is_latest=True,
        parse_status="Parsed",
        raw_json=dumps(row),
    )
    session.add(snapshot)
    session.flush()
    return snapshot


def extract_attachment_records(row: dict[str, Any]) -> list[dict[str, Any]]:
    raw_attachments = row.get("attachments")
    records: list[dict[str, Any]] = []
    seen: set[str] = set()

    def add_record(record: dict[str, Any]) -> None:
        key = "|".join([
            normalized_lower(record.get("source_file_id")),
            normalized_lower(record.get("file_name")),
            normalized_lower(record.get("file_url")),
        ])
        fallback_key = normalized_lower(record.get("file_name"))
        dedupe_key = key if key.strip("|") else fallback_key
        if not dedupe_key or dedupe_key in seen:
            return
        seen.add(dedupe_key)
        records.append(record)

    if isinstance(raw_attachments, list):
        for item in raw_attachments:
            if isinstance(item, dict):
                name = normalized_text(item.get("file_name") or item.get("name") or item.get("filename"))
                if not name:
                    continue
                add_record({
                    "file_name": name,
                    "file_url": normalized_text(item.get("file_url") or item.get("url")) or None,
                    "source_file_id": normalized_text(item.get("file_id") or item.get("id")) or None,
                    "attachment_type": normalized_text(item.get("type") or item.get("attachment_type")) or None,
                    "raw": item,
                })
            elif normalized_text(item):
                add_record({"file_name": normalized_text(item), "raw": item})
    for name in [item.strip() for item in str(row.get("attachment_files") or "").split(";") if item.strip()]:
        add_record({"file_name": name, "raw": name})
    return records


def attachment_fingerprint(record: dict[str, Any]) -> str:
    stable = "|".join([
        normalized_text(record.get("source_file_id")),
        normalized_text(record.get("file_name")),
        normalized_text(record.get("file_url")),
    ])
    return hashlib.sha256(stable.encode("utf-8")).hexdigest()


def sync_order_attachments(session: Session, order: CrmSalesOrder, row: dict[str, Any], digest: str) -> None:
    for record in extract_attachment_records(row):
        fingerprint = attachment_fingerprint(record)
        existing = (
            session.query(OrderAttachment)
            .filter(
                OrderAttachment.source_system == order.source_system,
                OrderAttachment.crm_order_id == order.crm_order_id,
                OrderAttachment.payload_hash == digest,
                OrderAttachment.fingerprint == fingerprint,
            )
            .first()
        )
        payload = {
            "crm_sales_order_id": order.id,
            "source_system": order.source_system,
            "crm_order_id": order.crm_order_id,
            "crm_order_no": order.crm_order_no,
            "payload_hash": digest,
            "attachment_type": record.get("attachment_type"),
            "file_name": record["file_name"],
            "file_url": record.get("file_url"),
            "source_file_id": record.get("source_file_id"),
            "fingerprint": fingerprint,
            "parse_status": "Registered",
            "evidence_json": dumps({"source": "crm_order_detail", "payload_hash": digest}),
            "raw_json": dumps(record.get("raw")),
            "captured_at": now_utc(),
        }
        if existing is None:
            existing = OrderAttachment(**payload)
            session.add(existing)
            session.flush()
        else:
            for key, value in payload.items():
                setattr(existing, key, value)
        if existing.file_url:
            cache_order_attachment_file(session, existing)


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
    text = str(settlement_method or "").strip().upper()
    enum_mapping = {
        "OPTION1": "CNY",
        "RMB": "CNY",
        "人民币": "CNY",
        "元": "CNY",
    }
    if text in enum_mapping:
        return enum_mapping[text]
    for code in ("CNY", "USD", "EUR", "JPY", "HKD"):
        if code in text:
            return code
    return None


def apply_order_row(order: CrmSalesOrder, row: dict[str, Any], digest: str) -> None:
    def keep_existing(key: str, current: Any) -> str | None:
        value = str(row.get(key) or "").strip()
        if value:
            return value
        existing = str(current or "").strip()
        return existing or None

    order.crm_order_id = keep_existing("crm_order_id", order.crm_order_id) or ""
    order.crm_order_no = keep_existing("crm_order_no", order.crm_order_no) or ""
    order.customer_id = keep_existing("customer_id", order.customer_id)
    order.customer_name = keep_existing("customer_name", order.customer_name)
    order.opportunity_id = keep_existing("opportunity_id", order.opportunity_id)
    order.opportunity_name = keep_existing("opportunity_name", order.opportunity_name)
    order.sales_user_id = keep_existing("sales_user_id", order.sales_user_id)
    order.sales_user_name = keep_existing("sales_user_name", order.sales_user_name)
    order.owner_department = keep_existing("owner_department", order.owner_department)
    order.life_status = keep_existing("life_status", order.life_status)
    order.approval_status = keep_existing("approval_status", order.approval_status)
    order.order_date = keep_existing("order_date", order.order_date)
    order.settlement_method = keep_existing("settlement_method", order.settlement_method)
    order.currency = infer_currency(order.settlement_method)
    order.order_amount = keep_existing("order_amount", order.order_amount)
    order.received_amount = keep_existing("received_amount", order.received_amount)
    order.receivable_amount = keep_existing("receivable_amount", order.receivable_amount)
    order.invoice_amount = keep_existing("invoice_amount", order.invoice_amount)
    order.product_amount = keep_existing("product_amount", order.product_amount)
    order.logistics_status = keep_existing("logistics_status", order.logistics_status)
    order.shipment_status = keep_existing("shipment_status", order.shipment_status)
    order.invoice_status = keep_existing("invoice_status", order.invoice_status)
    order.receipt_contact = keep_existing("receipt_contact", order.receipt_contact)
    order.receipt_phone = keep_existing("receipt_phone", order.receipt_phone)
    order.receipt_address = keep_existing("receipt_address", order.receipt_address)
    order.delivery_date = keep_existing("delivery_date", order.delivery_date)
    order.remark = keep_existing("remark", order.remark)
    attachment_names = [item.strip() for item in str(row.get("attachment_files") or "").split(";") if item.strip()]
    if not attachment_names:
        attachment_names = [normalized_text(item.get("file_name")) for item in row.get("attachments", []) if isinstance(item, dict) and normalized_text(item.get("file_name"))]
    if attachment_names:
        order.attachment_files_json = dumps(attachment_names)
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
