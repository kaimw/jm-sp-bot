"""order_middle_platform — oms"""
from __future__ import annotations

import hashlib
import json
import logging
import re
import time
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal, InvalidOperation
from enum import Enum
from typing import Any, Protocol

from sqlalchemy import func, or_
from sqlalchemy.orm import Session

from backend.app.models import (
    AuditEvent, ChannelPricing, CrmSalesOrder, DeliveryNotice, ExceptionCase,
    IntegrationEvent, MiddlePlatformOrder, MiddlePlatformOrderItem, OrderAttachment,
    OutboundMailJob, ProcessingJob, ProductSKU, SystemConfig, User, now_utc,
)
from backend.app.services.jsonutil import dumps, loads
from backend.app.services.rules import BlockerLevel, DEFAULT_RULES, OrderContext, OrderValidationRule, ValidationResult, is_review_rule_enabled, register_rule, remove_rule
from backend.app.services.rules.helpers import config_bool, config_dict, config_int, config_list, config_value, inventory_available_quantity, is_approved_status, parse_decimal
from backend.app.services.auth import should_mask_financials
from backend.app.services.crm_attachment_extraction import enrich_order_from_registered_attachments
from backend.app.services.exception_diagnosis import enqueue_exception_diagnosis
from backend.app.services.address_quality import is_detailed_receipt_address
from backend.app.services.oms.jackyun_client import JackyunConfigError, jackyun_client_from_session
from backend.app.services.erp.kingdee_client import KingdeeClient, kingdee_config_from_session, KingdeeConfigError, normalize_query_rows
from backend.app.services.erp.sales_order_mapper import build_sales_order_model, should_skip_erp_billing
from backend.app.services.order_no_generator import generate_middle_order_no
from backend.app.services.mail_template_service import enqueue_delivery_notice_mail
from backend.app.services.products import match_sku_by_product_name
from backend.app.services.storage import save_attachment
from backend.app.services.task_scheduler import RetryPolicy, next_retry_at
from backend.app.services.time_utils import format_beijing_time

logger = logging.getLogger(__name__)

# Cross-module references within this package
from backend.app.services.order_middle_platform.enums import ExceptionType
from backend.app.services.order_middle_platform.enums import IllegalStateTransition
from backend.app.services.order_middle_platform.enums import OrderEvent
from backend.app.services.order_middle_platform.enums import OrderStatus
from backend.app.services.order_middle_platform.enums import transition_order
from backend.app.services.order_middle_platform.notifications import create_exception_case
from backend.app.services.order_middle_platform.notifications import unique_emails
from backend.app.services.order_middle_platform.notifications import validation_failure_recipients
from backend.app.services.order_middle_platform.platform_fulfillment import enqueue_platform_fulfillment_sync
from backend.app.services.order_middle_platform.utils import extract_waybill_no
from backend.app.services.order_middle_platform.utils import print_oms_waybill
from backend.app.services.order_middle_platform.utils import record_integration_event
from backend.app.services.order_middle_platform.utils import save_waybill_outbound_proof

def enqueue_oms_push(session: Session, notice: DeliveryNotice) -> ProcessingJob:
    order = session.get(MiddlePlatformOrder, notice.order_id)
    if order is None:
        raise RuntimeError("middle platform order not found")
    payload = oms_push_job_payload(order, notice)
    existing = (
        session.query(ProcessingJob)
        .filter(
            ProcessingJob.job_type == "OMS_PUSH_NOTICE",
            ProcessingJob.payload_json == dumps(payload),
            ProcessingJob.status.in_(["Pending", "Running"]),
        )
        .first()
    )
    if existing is not None:
        return existing
    job = ProcessingJob(job_type="OMS_PUSH_NOTICE", payload_json=dumps(payload), status="Pending", next_retry_at=notice.next_retry_at)
    session.add(job)
    session.add(AuditEvent(event_type="OmsPushQueued", related_object_type="DeliveryNotice", related_object_id=notice.id, detail=dumps(payload)))
    record_integration_event(
        session,
        source_system="OMS",
        event_type="OMS_PUSH_NOTICE",
        biz_key=notice.notice_no,
        payload=payload,
        trace_id=notice.oms_idempotency_key,
        status="Pending",
        retry_count=notice.retry_count,
    )
    return job




def oms_push_job_payload(order: MiddlePlatformOrder, notice: DeliveryNotice) -> dict[str, Any]:
    return {
        "delivery_notice_id": notice.id,
        "notice_id": notice.id,
        "order_id": notice.order_id,
        "source_snapshot_hash": notice.source_snapshot_hash or order.payload_hash,
        "notice_version": notice.notice_version,
        "notice_lock_version": notice.version,
        "oms_idempotency_key": notice.oms_idempotency_key,
        "idempotency_key": notice.oms_idempotency_key,
    }




def process_oms_push_notice(session: Session, payload: dict[str, Any]) -> dict[str, Any]:
    notice = session.get(DeliveryNotice, payload.get("delivery_notice_id") or payload.get("notice_id"))
    if notice is None:
        raise RuntimeError("delivery notice not found")
    order = session.get(MiddlePlatformOrder, notice.order_id)
    if order is None:
        raise RuntimeError("middle platform order not found")
    stale = stale_oms_push_reason(order, notice, payload)
    if stale:
        detail = {
            "skipped_reason": stale,
            "payload": payload,
            "current_payload_hash": order.payload_hash,
            "current_notice_version": notice.version,
        }
        session.add(AuditEvent(event_type="OmsPushSkipped", related_object_type="DeliveryNotice", related_object_id=notice.id, detail=dumps(detail)))
        return {"notice_id": notice.id, "order_id": order.id, "status": order.status, "skipped": True, "skipped_reason": stale}
    if notice.status not in {"Confirmed", "Retrying"}:
        raise RuntimeError("发货通知未确认，不允许下推 OMS")
    if notice.next_retry_at and notice.next_retry_at > now_utc():
        raise RuntimeError(f"OMS retry not due until {notice.next_retry_at.isoformat()}")
    try:
        result = push_notice_to_oms(session, notice)
    except Exception as exc:
        return handle_oms_push_failure(session, notice, order, str(exc))
    notice.status = "Accepted"
    notice.pushed_at = now_utc()
    notice.last_error = None
    notice.updated_at = now_utc()
    event = OrderEvent.OMS_PUSH_SUCCESS if order.status == OrderStatus.OMS_PENDING.value else OrderEvent.RETRY_TIMER_DUE_AND_OMS_SUCCESS
    transition_order(session, order, event, trace_id=str(payload.get("idempotency_key") or ""), detail={"oms_result": result})
    record_integration_event(
        session,
        source_system="OMS",
        event_type="OMS_PUSH_NOTICE",
        biz_key=notice.notice_no,
        payload=payload,
        trace_id=str(payload.get("idempotency_key") or ""),
        status="Succeeded",
        retry_count=notice.retry_count,
        response=result,
    )
    return {"notice_id": notice.id, "order_id": order.id, "status": order.status, "oms_result": result}




def stale_oms_push_reason(order: MiddlePlatformOrder, notice: DeliveryNotice, payload: dict[str, Any]) -> str:
    source_snapshot_hash = str(payload.get("source_snapshot_hash") or "").strip()
    if source_snapshot_hash and source_snapshot_hash != order.payload_hash:
        return "stale_payload_hash"
    if notice.source_snapshot_hash and notice.source_snapshot_hash != order.payload_hash:
        return "stale_payload_hash"
    if "notice_version" in payload:
        try:
            expected_version = int(payload.get("notice_version"))
        except (TypeError, ValueError):
            return "stale_notice_version"
        if expected_version != notice.notice_version:
            return "stale_notice_version"
    if "notice_lock_version" in payload:
        try:
            expected_lock_version = int(payload.get("notice_lock_version"))
        except (TypeError, ValueError):
            return "stale_notice_lock_version"
        if expected_lock_version != notice.version:
            return "stale_notice_lock_version"
    return ""




def process_oms_status_update(session: Session, payload: dict[str, Any]) -> dict[str, Any]:
    notice = resolve_delivery_notice_for_oms_status(session, payload)
    if notice is None:
        raise RuntimeError("delivery notice not found for OMS status update")
    order = session.get(MiddlePlatformOrder, notice.order_id)
    if order is None:
        raise RuntimeError("middle platform order not found")
    normalized = normalize_oms_fulfillment_status(payload.get("oms_status") or payload.get("status") or payload.get("delivery_status"))
    if normalized is None:
        raise RuntimeError(f"未知 OMS 状态：{payload.get('oms_status') or payload.get('status') or payload.get('delivery_status')}")
    raw_detail = {
        "notice_no": notice.notice_no,
        "oms_order_no": notice.oms_order_no,
        "oms_status": payload.get("oms_status") or payload.get("status") or payload.get("delivery_status"),
        "normalized_status": normalized,
        "raw": payload.get("raw") or payload,
    }
    if normalized == "accepted":
        session.add(AuditEvent(event_type="OmsStatusAcceptedObserved", related_object_type="MiddlePlatformOrder", related_object_id=order.id, detail=dumps(raw_detail)))
        return {"notice_id": notice.id, "order_id": order.id, "status": order.status, "normalized_status": normalized}
    if normalized == "picking":
        if order.status == OrderStatus.OMS_ACCEPTED.value:
            notice.status = "Picking"
            notice.updated_at = now_utc()
            transition_order(session, order, OrderEvent.OMS_PICKING_STARTED, trace_id=str(payload.get("trace_id") or ""), detail=raw_detail)
            enqueue_oms_waybill_print(session, notice, trace_id=str(payload.get("trace_id") or ""))
        elif order.status in {OrderStatus.PICKING.value, OrderStatus.SHIPPED.value, OrderStatus.FULFILLMENT_ARCHIVED.value, OrderStatus.CLOSED.value}:
            session.add(AuditEvent(event_type="OmsPickingStatusObserved", related_object_type="MiddlePlatformOrder", related_object_id=order.id, detail=dumps(raw_detail)))
            if order.status in {OrderStatus.PICKING.value, OrderStatus.SHIPPED.value}:
                enqueue_oms_waybill_print(session, notice, trace_id=str(payload.get("trace_id") or ""))
        else:
            raise IllegalStateTransition(f"当前状态不允许同步 OMS 拣货状态：{order.status}")
    elif normalized == "shipped":
        if order.status == OrderStatus.OMS_ACCEPTED.value:
            transition_order(session, order, OrderEvent.OMS_PICKING_STARTED, trace_id=str(payload.get("trace_id") or ""), detail={**raw_detail, "auto_picking": True})
            enqueue_oms_waybill_print(session, notice, trace_id=str(payload.get("trace_id") or ""))
        if order.status == OrderStatus.PICKING.value:
            notice.status = "Shipped"
            notice.updated_at = now_utc()
            transition_order(session, order, OrderEvent.OMS_SHIPPED, trace_id=str(payload.get("trace_id") or ""), detail=raw_detail)
            enqueue_oms_waybill_print(session, notice, trace_id=str(payload.get("trace_id") or ""))
            transition_order(
                session,
                order,
                OrderEvent.ARCHIVE_PHASE1_FULFILLMENT,
                trace_id=str(payload.get("trace_id") or ""),
                detail={**raw_detail, "archive_reason": "oms_shipped"},
            )
        elif order.status in {OrderStatus.SHIPPED.value, OrderStatus.FULFILLMENT_ARCHIVED.value, OrderStatus.CLOSED.value}:
            session.add(AuditEvent(event_type="OmsShippedStatusObserved", related_object_type="MiddlePlatformOrder", related_object_id=order.id, detail=dumps(raw_detail)))
            if order.status == OrderStatus.SHIPPED.value:
                enqueue_oms_waybill_print(session, notice, trace_id=str(payload.get("trace_id") or ""))
        else:
            raise IllegalStateTransition(f"当前状态不允许同步 OMS 发货状态：{order.status}")
    return {"notice_id": notice.id, "order_id": order.id, "status": order.status, "normalized_status": normalized}




def enqueue_oms_waybill_print(session: Session, notice: DeliveryNotice, *, trace_id: str = "") -> ProcessingJob | None:
    if notice.waybill_no:
        return None
    if notice.print_status in {"Pending", "Running", "Printed"}:
        return None
    payload = {"notice_id": notice.id, "order_id": notice.order_id, "trace_id": trace_id, "retry_count": notice.print_retry_count}
    existing = (
        session.query(ProcessingJob)
        .filter(
            ProcessingJob.job_type == "OMS_WAYBILL_PRINT",
            ProcessingJob.payload_json == dumps(payload),
            ProcessingJob.status.in_(["Pending", "Running"]),
        )
        .first()
    )
    if existing is not None:
        return existing
    notice.print_status = "Pending"
    notice.print_error = None
    notice.updated_at = now_utc()
    job = ProcessingJob(job_type="OMS_WAYBILL_PRINT", payload_json=dumps(payload), status="Pending")
    session.add(job)
    session.add(AuditEvent(event_type="OmsWaybillPrintQueued", related_object_type="DeliveryNotice", related_object_id=notice.id, detail=dumps(payload)))
    record_integration_event(session, source_system="OMS", event_type="OMS_WAYBILL_PRINT", biz_key=notice.notice_no, payload=payload, trace_id=trace_id, status="Pending", retry_count=notice.print_retry_count)
    return job




def process_oms_waybill_print(session: Session, payload: dict[str, Any]) -> dict[str, Any]:
    notice = session.get(DeliveryNotice, payload.get("notice_id"))
    if notice is None:
        raise RuntimeError("delivery notice not found")
    order = session.get(MiddlePlatformOrder, notice.order_id)
    if order is None:
        raise RuntimeError("middle platform order not found")
    # Acquire a PostgreSQL advisory lock on notice.notice_no to prevent concurrent prints
    is_postgres = (session.bind.dialect.name == "postgresql") if (session.bind and hasattr(session.bind, "dialect")) else False
    if is_postgres:
        import hashlib
        from sqlalchemy import text
        hash_val = int(hashlib.sha256(notice.notice_no.encode("utf-8")).hexdigest()[:16], 16)
        bigint_key = (hash_val & 0x7FFFFFFFFFFFFFFF) - (hash_val & 0x8000000000000000)
        locked = session.execute(text("SELECT pg_try_advisory_xact_lock(:key)"), {"key": bigint_key}).scalar()
        if not locked:
            raise RuntimeError(f"Could not acquire advisory lock for notice {notice.notice_no}, printing is in progress by another worker.")
        session.refresh(notice)
        session.refresh(order)

    if notice.waybill_no:
        notice.print_status = "Printed"
        notice.print_error = None
        return {"notice_id": notice.id, "order_id": order.id, "waybill_no": notice.waybill_no, "skipped": True}
    if order.status not in {OrderStatus.PICKING.value, OrderStatus.SHIPPED.value, OrderStatus.FULFILLMENT_ARCHIVED.value}:
        raise RuntimeError(f"当前状态不允许打印跨境面单：{order.status}")
    notice.print_status = "Running"
    notice.updated_at = now_utc()
    try:
        result = print_oms_waybill(session, notice)
    except Exception as exc:
        return handle_oms_waybill_print_failure(session, notice, order, str(exc), trace_id=str(payload.get("trace_id") or ""))
    waybill_no = extract_waybill_no(result)
    if not waybill_no:
        return handle_oms_waybill_print_failure(session, notice, order, "吉客云面单响应缺少 waybillNo", trace_id=str(payload.get("trace_id") or ""))
    notice.waybill_no = waybill_no
    notice.print_status = "Printed"
    notice.print_error = None
    notice.print_retry_count = 0
    notice.updated_at = now_utc()
    proof = save_waybill_outbound_proof(session, order, notice, result)
    session.add(AuditEvent(event_type="OmsWaybillPrinted", related_object_type="DeliveryNotice", related_object_id=notice.id, detail=dumps({"waybill_no": waybill_no, "attachment_id": proof.id if proof else None})))
    record_integration_event(session, source_system="OMS", event_type="OMS_WAYBILL_PRINT", biz_key=notice.notice_no, payload=payload, trace_id=str(payload.get("trace_id") or ""), status="Succeeded", retry_count=notice.print_retry_count, response=result)
    platform_job = enqueue_platform_fulfillment_sync(session, order, notice, trace_id=str(payload.get("trace_id") or ""))
    return {
        "notice_id": notice.id,
        "order_id": order.id,
        "waybill_no": waybill_no,
        "attachment_id": proof.id if proof else None,
        "platform_fulfillment_job_id": platform_job.id if platform_job is not None else None,
    }




def push_notice_to_oms(session: Session, notice: DeliveryNotice) -> dict[str, Any]:
    if not config_bool(session, "oms_enabled", False) or config_bool(session, "oms_mock_success", True):
        return {"accepted": True, "mode": "mock", "idempotency_key": notice.oms_idempotency_key}
    client = jackyun_client_from_session(session)
    payload = loads(notice.payload_json, {})
    result = client.create_delivery_order(payload, method=notice.oms_method or "wms.order.create")
    if not result.get("ok"):
        if is_oms_idempotency_conflict(result):
            lookup = lookup_existing_oms_order(session, client, notice, payload)
            if lookup.get("found"):
                notice.oms_order_no = lookup.get("oms_order_no") or notice.oms_order_no
                return {
                    "accepted": True,
                    "mode": "jackyun",
                    "idempotency_conflict_resolved": True,
                    "oms_order_no": notice.oms_order_no,
                    "raw": lookup.get("raw"),
                }
        raise RuntimeError(result.get("message") or dumps(result.get("raw", result)))
    data = result.get("data") if isinstance(result.get("data"), dict) else {}
    raw = result.get("raw") if isinstance(result.get("raw"), dict) else {}
    notice.oms_order_no = str(data.get("orderNo") or raw.get("orderNo") or raw.get("order_no") or "").strip() or None
    return {"accepted": True, "mode": "jackyun", "oms_order_no": notice.oms_order_no, "raw": result.get("raw")}




def is_oms_idempotency_conflict(result: dict[str, Any]) -> bool:
    text = " ".join(
        str(value or "")
        for value in [
            result.get("code"),
            result.get("sub_code"),
            result.get("message"),
            dumps(result.get("raw", {})),
        ]
    ).lower()
    return any(token in text for token in ["duplicate", "duplicated", "exists", "existed", "idempot", "重复", "已存在", "幂等"])




def lookup_existing_oms_order(session: Session, client: Any, notice: DeliveryNotice, payload: dict[str, Any]) -> dict[str, Any]:
    erp_order_no = str((payload.get("order") or {}).get("erporderNo") or notice.notice_no).strip()
    if not erp_order_no:
        return {"found": False, "reason": "missing_erporderNo"}
    query_payload = {
        "pageIndex": 1,
        "pageSize": 20,
        "erporderNo": erp_order_no,
    }
    result = client.query_delivery_orders(query_payload)
    if not result.get("ok"):
        return {"found": False, "raw": result.get("raw"), "message": result.get("message")}
    raw = result.get("raw")
    data = result.get("data")
    candidates = extract_oms_order_candidates(data) + extract_oms_order_candidates(raw)
    for candidate in candidates:
        candidate_erp_no = str(candidate.get("erporderNo") or candidate.get("erp_order_no") or candidate.get("outerOrderNo") or "").strip()
        if candidate_erp_no and candidate_erp_no != erp_order_no:
            continue
        oms_order_no = str(candidate.get("orderNo") or candidate.get("order_no") or candidate.get("tradeNo") or candidate.get("trade_no") or "").strip()
        return {"found": True, "oms_order_no": oms_order_no or None, "raw": raw}
    return {"found": False, "raw": raw}




def extract_oms_order_candidates(value: Any) -> list[dict[str, Any]]:
    if isinstance(value, list):
        return [item for item in value if isinstance(item, dict)]
    if not isinstance(value, dict):
        return []
    candidates: list[dict[str, Any]] = []
    for key in ("rows", "list", "data", "result", "items", "orderList"):
        nested = value.get(key)
        if isinstance(nested, list):
            candidates.extend(item for item in nested if isinstance(item, dict))
        elif isinstance(nested, dict):
            candidates.extend(extract_oms_order_candidates(nested))
    if any(key in value for key in ("orderNo", "order_no", "tradeNo", "erporderNo", "outerOrderNo")):
        candidates.append(value)
    return candidates




def handle_oms_push_failure(session: Session, notice: DeliveryNotice, order: MiddlePlatformOrder, message: str) -> dict[str, Any]:
    notice.retry_count += 1
    notice.last_error = message
    notice.updated_at = now_utc()
    max_retries = max(1, config_int(session, "oms_max_retries", notice.max_retries or 3))
    notice.max_retries = max_retries
    if notice.retry_count >= max_retries:
        notice.status = "Blocked"
        transition_order(session, order, OrderEvent.RETRY_REACHED_MAX_RETRIES, detail={"notice_no": notice.notice_no, "error": message})
        exception_case = create_exception_case(session, order, ExceptionType.OMS_BLOCKED, "Critical", message, [], trace_id=notice.oms_idempotency_key)
        enqueue_oms_blocked_notification(session, order, notice, exception_case, message)
        record_integration_event(
            session,
            source_system="OMS",
            event_type="OMS_PUSH_NOTICE",
            biz_key=notice.notice_no,
            payload=oms_push_job_payload(order, notice),
            trace_id=notice.oms_idempotency_key,
            status="Dead",
            retry_count=notice.retry_count,
            error_message=message,
        )
        return {"notice_id": notice.id, "order_id": order.id, "status": order.status, "blocked": True}
    notice.status = "Retrying"
    notice.next_retry_at = calculate_next_retry_at(session, notice.retry_count)
    event = OrderEvent.FIRST_OMS_PUSH_FAILED if order.status == OrderStatus.OMS_PENDING.value else OrderEvent.RETRY_FAILED_BUT_UNDER_MAX_RETRIES
    transition_order(session, order, event, detail={"notice_no": notice.notice_no, "error": message, "next_retry_at": notice.next_retry_at.isoformat()})
    payload = oms_push_job_payload(order, notice)
    session.add(ProcessingJob(job_type="OMS_PUSH_NOTICE", payload_json=dumps(payload), status="Pending", next_retry_at=notice.next_retry_at))
    record_integration_event(
        session,
        source_system="OMS",
        event_type="OMS_PUSH_NOTICE",
        biz_key=notice.notice_no,
        payload=payload,
        trace_id=notice.oms_idempotency_key,
        status="Retrying",
        retry_count=notice.retry_count,
        error_message=message,
    )
    return {"notice_id": notice.id, "order_id": order.id, "status": order.status, "next_retry_at": notice.next_retry_at.isoformat()}




def resolve_delivery_notice_for_oms_status(session: Session, payload: dict[str, Any]) -> DeliveryNotice | None:
    notice_id = str(payload.get("notice_id") or "").strip()
    if notice_id:
        return session.get(DeliveryNotice, notice_id)
    oms_order_no = str(payload.get("oms_order_no") or payload.get("orderNo") or payload.get("order_no") or "").strip()
    if oms_order_no:
        notice = session.query(DeliveryNotice).filter(DeliveryNotice.oms_order_no == oms_order_no).order_by(DeliveryNotice.created_at.desc()).first()
        if notice is not None:
            return notice
    notice_no = str(payload.get("notice_no") or payload.get("erporderNo") or payload.get("erp_order_no") or "").strip()
    if notice_no:
        return session.query(DeliveryNotice).filter(DeliveryNotice.notice_no == notice_no).order_by(DeliveryNotice.created_at.desc()).first()
    order_id = str(payload.get("order_id") or "").strip()
    if order_id:
        return session.query(DeliveryNotice).filter(DeliveryNotice.order_id == order_id).order_by(DeliveryNotice.created_at.desc()).first()
    return None




def normalize_oms_fulfillment_status(value: Any) -> str | None:
    text = str(value or "").strip().lower()
    if not text:
        return None
    accepted = {"accepted", "created", "new", "已接单", "已创建", "已入库", "已下推"}
    picking = {"picking", "pick", "processing", "warehouse_processing", "拣货", "拣货中", "配货中", "仓库处理中", "已审核"}
    shipped = {"shipped", "delivered", "sent", "出库", "已出库", "已发货", "发货", "已揽收"}
    if text in accepted:
        return "accepted"
    if text in picking:
        return "picking"
    if text in shipped:
        return "shipped"
    if any(token in text for token in ["shipped", "delivered", "已发货", "已出库", "出库"]):
        return "shipped"
    if any(token in text for token in ["picking", "拣货", "配货", "仓库处理"]):
        return "picking"
    if any(token in text for token in ["accepted", "created", "已接单", "已创建"]):
        return "accepted"
    return None




def poll_oms_status_updates(session: Session, *, limit: int = 50) -> dict[str, Any]:
    if not config_bool(session, "oms_enabled", False) or config_bool(session, "oms_mock_success", True):
        return {"skipped": True, "reason": "oms_disabled_or_mock", "checked": 0, "updated": 0, "failed": 0}
    client = jackyun_client_from_session(session)
    notices = (
        session.query(DeliveryNotice)
        .filter(DeliveryNotice.status.in_(["Accepted", "Picking"]))
        .order_by(DeliveryNotice.updated_at)
        .limit(max(1, limit))
        .all()
    )
    checked = 0
    updated = 0
    failed: list[dict[str, str]] = []
    for notice in notices:
        checked += 1
        query_payload = {"pageIndex": 1, "pageSize": 20, "erporderNo": notice.notice_no}
        if notice.oms_order_no:
            query_payload["orderNo"] = notice.oms_order_no
        result = client.query_delivery_orders(query_payload)
        if not result.get("ok"):
            failed.append({"notice_id": notice.id, "error": str(result.get("message") or result.get("code") or "query failed")})
            continue
        candidate = match_oms_order_candidate(notice, extract_oms_order_candidates(result.get("data")) + extract_oms_order_candidates(result.get("raw")))
        if candidate is None:
            failed.append({"notice_id": notice.id, "error": "oms order not found"})
            continue
        oms_status = oms_candidate_status(candidate)
        if not oms_status:
            failed.append({"notice_id": notice.id, "error": "oms status missing"})
            continue
        before_status = session.get(MiddlePlatformOrder, notice.order_id).status if notice.order_id else ""
        process_oms_status_update(
            session,
            {
                "notice_id": notice.id,
                "oms_status": oms_status,
                "oms_order_no": candidate.get("orderNo") or candidate.get("order_no") or notice.oms_order_no,
                "raw": candidate,
                "trace_id": "oms-status-poll",
            },
        )
        after_status = session.get(MiddlePlatformOrder, notice.order_id).status if notice.order_id else ""
        if after_status != before_status or notice.status in {"Picking", "Shipped"}:
            updated += 1
    return {"skipped": False, "checked": checked, "updated": updated, "failed": len(failed), "failures": failed[:20]}




def match_oms_order_candidate(notice: DeliveryNotice, candidates: list[dict[str, Any]]) -> dict[str, Any] | None:
    for candidate in candidates:
        erp_no = str(candidate.get("erporderNo") or candidate.get("erp_order_no") or candidate.get("outerOrderNo") or "").strip()
        oms_no = str(candidate.get("orderNo") or candidate.get("order_no") or candidate.get("tradeNo") or candidate.get("trade_no") or "").strip()
        if erp_no and erp_no == notice.notice_no:
            return candidate
        if notice.oms_order_no and oms_no and oms_no == notice.oms_order_no:
            return candidate
    return candidates[0] if len(candidates) == 1 else None




def oms_candidate_status(candidate: dict[str, Any]) -> str:
    for key in ("deliveryStatus", "delivery_status", "orderStatus", "order_status", "wmsStatus", "status", "processStatus"):
        value = str(candidate.get(key) or "").strip()
        if value:
            return value
    return ""




def enqueue_oms_blocked_notification(
    session: Session,
    order: MiddlePlatformOrder,
    notice: DeliveryNotice,
    exception_case: ExceptionCase,
    message: str,
) -> OutboundMailJob | None:
    if not config_bool(session, "v2_oms_blocked_notification_enabled", True):
        return None
    to_addresses, cc_addresses = oms_blocked_recipients(session)
    if not to_addresses:
        session.add(
            AuditEvent(
                event_type="OmsBlockedNotificationSkipped",
                related_object_type="DeliveryNotice",
                related_object_id=notice.id,
                detail=dumps({"reason": "missing_recipients", "exception_case_id": exception_case.id}),
            )
        )
        return None
    digest_source = "|".join([notice.id, str(notice.retry_count), message])
    idempotency_key = f"v2-oms-blocked:{hashlib.sha256(digest_source.encode('utf-8')).hexdigest()}"
    existing = session.query(OutboundMailJob).filter(OutboundMailJob.idempotency_key == idempotency_key).first()
    if existing is not None:
        return existing
    subject = f"[OMS下推阻塞][{order.crm_order_no or order.order_no}] {notice.notice_no}"
    body = build_oms_blocked_mail_body(session, order, notice, exception_case, message)
    job = OutboundMailJob(
        mail_type="V2OmsBlocked",
        to_json=dumps(to_addresses),
        cc_json=dumps(cc_addresses),
        subject=subject,
        body=body,
        idempotency_key=idempotency_key,
        status="Pending",
        priority=10,
    )
    session.add(job)
    session.add(
        AuditEvent(
            event_type="OmsBlockedNotificationQueued",
            related_object_type="DeliveryNotice",
            related_object_id=notice.id,
            detail=dumps({"to": to_addresses, "cc": cc_addresses, "exception_case_id": exception_case.id}),
        )
    )
    return job




def oms_blocked_recipients(session: Session) -> tuple[list[str], list[str]]:
    configured_to = config_list(session, "v2_oms_blocked_to_json", [])
    configured_cc = config_list(session, "v2_oms_blocked_cc_json", [])
    if configured_to or configured_cc:
        return unique_emails(configured_to), unique_emails(configured_cc)
    return validation_failure_recipients(session)




def build_oms_blocked_mail_body(
    session: Session,
    order: MiddlePlatformOrder,
    notice: DeliveryNotice,
    exception_case: ExceptionCase,
    message: str,
) -> str:
    lines = [
        "相关同事好，",
        "",
        "OMS/WMS 发货单下推重试已达到上限，系统已冻结自动重试并生成高危异常，避免重复建单或错发。",
        "",
        f"中台订单号：{order.order_no}",
        f"CRM 订单号：{order.crm_order_no or ''}",
        f"客户名称：{order.customer_name or ''}",
        f"发货通知单：{notice.notice_no}",
        f"发货通知状态：{notice.status}",
        f"重试次数：{notice.retry_count}/{notice.max_retries}",
        f"异常编号：{exception_case.id}",
        f"失败原因：{message}",
        "",
        "处理建议：",
        "- 先核对 OMS 是否已存在同一幂等键或发货单号，避免重复创建。",
        "- 修复主数据、仓库、接口配置或订单字段后，在异常台填写修复证据并重放 OMS。",
        "- 未确认下游状态前，不要手工关闭高危异常。",
        "",
        config_value(session, "bot_signature", "积木易搭AI机器人"),
    ]
    return "\n".join(lines)




def calculate_next_retry_at(session: Session, retry_count: int) -> datetime:
    base_delay = max(1, config_int(session, "oms_retry_base_delay_seconds", 60))
    multiplier = max(1, config_int(session, "oms_retry_multiplier", 3))
    return next_retry_at(
        retry_count,
        RetryPolicy(base_delay_seconds=base_delay, multiplier=multiplier, max_delay_seconds=None, jitter_seconds=5),
    )




