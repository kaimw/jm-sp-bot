"""order_middle_platform — events"""
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
from backend.app.services.order_middle_platform.delivery import confirm_delivery_notice
from backend.app.services.order_middle_platform.delivery import create_delivery_notice
from backend.app.services.order_middle_platform.delivery import latest_delivery_notice
from backend.app.services.order_middle_platform.enums import DuplicateEventException
from backend.app.services.order_middle_platform.enums import ExceptionType
from backend.app.services.order_middle_platform.enums import InvalidCrmOrderParsedEvent
from backend.app.services.order_middle_platform.enums import OrderEvent
from backend.app.services.order_middle_platform.enums import OrderStatus
from backend.app.services.order_middle_platform.enums import transition_order
from backend.app.services.order_middle_platform.erp_billing import process_erp_billing
from backend.app.services.order_middle_platform.notifications import create_exception_case
from backend.app.services.order_middle_platform.notifications import enqueue_validation_failure_notification
from backend.app.services.order_middle_platform.platform_fulfillment import archive_platform_fulfilled_order
from backend.app.services.order_middle_platform.platform_fulfillment import is_platform_fulfilled_order
from backend.app.services.order_middle_platform.utils import record_integration_event
from backend.app.services.order_middle_platform.utils import run_validation_chain
from backend.app.services.order_middle_platform.utils import upsert_middle_platform_order
from backend.app.services.order_middle_platform.utils import validation_blocker_summary

def crm_order_parsed_event(crm_order: CrmSalesOrder, *, trace_id: str | None = None) -> dict[str, Any]:
    order_items = crm_order_parsed_event_items(crm_order)
    return {
        "trace_id": trace_id or f"crm-{crm_order.id}",
        "event_type": "CRM_ORDER_PARSED",
        "event_version": "1.0",
        "source_system": crm_order.source_system.upper(),
        "data": {
            "crm_order_id": crm_order.crm_order_id,
            "payload_hash": crm_order.payload_hash,
            "crm_sales_order_id": crm_order.id,
            "order_head": {
                "crm_order_no": crm_order.crm_order_no,
                "customer_name": crm_order.customer_name,
                "amount": float(parse_decimal(crm_order.order_amount) or Decimal("0")),
                "currency": crm_order.currency or "CNY",
                "sales_user_name": crm_order.sales_user_name,
            },
            "order_items": order_items,
        },
    }




def crm_order_parsed_event_items(crm_order: CrmSalesOrder) -> list[dict[str, Any]]:
    return [
        {
            "sku_code": item.sku_code,
            "product_name": item.product_name,
            "specification": item.specification,
            "quantity": item.quantity,
            "unit_price": item.unit_price,
            "line_amount": item.line_amount,
            "raw": loads(item.raw_json, {}),
        }
        for item in sorted(crm_order.items, key=lambda row: row.created_at)
    ]




def enqueue_crm_order_parsed_event(session: Session, crm_order: CrmSalesOrder, *, trace_id: str | None = None) -> ProcessingJob:
    payload = crm_order_parsed_event(crm_order, trace_id=trace_id)
    existing = (
        session.query(ProcessingJob)
        .filter(
            ProcessingJob.job_type == "CRM_ORDER_PARSED",
            ProcessingJob.payload_json == dumps(payload),
            ProcessingJob.status.in_(["Pending", "Running", "Completed"]),
        )
        .first()
    )
    if existing is not None:
        return existing
    job = ProcessingJob(job_type="CRM_ORDER_PARSED", payload_json=dumps(payload), status="Pending")
    session.add(job)
    session.add(AuditEvent(event_type="CrmOrderParsedEventQueued", related_object_type="CrmSalesOrder", related_object_id=crm_order.id, detail=dumps(payload)))
    record_integration_event(
        session,
        source_system=crm_order.source_system.upper(),
        event_type="CRM_ORDER_PARSED",
        biz_key=crm_order.crm_order_id,
        payload=payload,
        trace_id=str(payload.get("trace_id") or ""),
        status="Pending",
    )
    return job




def process_crm_order_parsed_event(session: Session, payload: dict[str, Any]) -> dict[str, Any]:
    validate_crm_order_parsed_event(payload)
    data = payload.get("data") or {}
    trace_id = str(payload.get("trace_id") or "")
    force_revalidate = bool(payload.get("force_revalidate"))
    payload_hash = str(data.get("payload_hash") or "").strip()
    crm_order = None
    crm_sales_order_id = str(data.get("crm_sales_order_id") or "").strip()
    if crm_sales_order_id:
        crm_order = session.get(CrmSalesOrder, crm_sales_order_id)
    if crm_order is None:
        crm_order_id = str(data.get("crm_order_id") or "").strip()
        crm_order = session.query(CrmSalesOrder).filter(CrmSalesOrder.crm_order_id == crm_order_id).first()
    if crm_order is None:
        raise RuntimeError("CRM order not found for CRM_ORDER_PARSED event")
    if payload_hash and crm_order.payload_hash != payload_hash:
        raise InvalidCrmOrderParsedEvent("CRM_ORDER_PARSED payload_hash 与本地 CRM 快照不一致")
    existing_order = (
        session.query(MiddlePlatformOrder)
        .filter(MiddlePlatformOrder.source_system == crm_order.source_system, MiddlePlatformOrder.crm_order_id == crm_order.crm_order_id)
        .first()
    )
    if existing_order is not None and payload_hash and existing_order.payload_hash == payload_hash and existing_order.status != OrderStatus.CRM_APPROVED.value and not force_revalidate:
        raise DuplicateEventException(f"重复 CRM_ORDER_PARSED 事件：{crm_order.crm_order_id}/{payload_hash}")
    if existing_order is not None and is_crm_order_cancelled(crm_order):
        return handle_crm_cancel_confirmed(session, existing_order, crm_order, payload_hash=payload_hash, trace_id=trace_id)
    if existing_order is not None and existing_order.status == OrderStatus.CANCELLED.value and not is_crm_order_cancelled(crm_order):
        reactivate_cancelled_order_from_crm(session, existing_order, crm_order, trace_id=trace_id)
    if existing_order is not None and payload_hash and existing_order.payload_hash != payload_hash and existing_order.status != OrderStatus.CRM_APPROVED.value:
        change_result = handle_crm_snapshot_changed(session, existing_order, crm_order, new_payload_hash=payload_hash, trace_id=trace_id)
        if not change_result.get("continue_processing", False):
            return change_result

    extraction_result = enrich_order_from_registered_attachments(session, crm_order)
    if extraction_result is not None:
        session.add(
            AuditEvent(
                event_type="CrmAttachmentOmsFieldsExtracted",
                related_object_type="CrmSalesOrder",
                related_object_id=crm_order.id,
                detail=dumps({"trace_id": trace_id, "result": extraction_result.as_dict()}),
            )
        )

    order = upsert_middle_platform_order(session, crm_order)
    if force_revalidate:
        reset_order_for_force_revalidate(session, order, trace_id=trace_id)
    if order.status == OrderStatus.CRM_APPROVED.value:
        transition_order(session, order, OrderEvent.ORDER_SNAPSHOT_FETCHED, trace_id=trace_id)
    if order.status in {OrderStatus.IMPORTED.value, OrderStatus.VALIDATION_BLOCKED.value}:
        event = OrderEvent.START_VALIDATION if order.status == OrderStatus.IMPORTED.value else OrderEvent.EXCEPTION_RESOLVED_AND_REVALIDATE
        transition_order(session, order, event, trace_id=trace_id)
        validation_results = run_validation_chain(session, order)
        order.validation_summary_json = dumps({"results": [result.as_dict() for result in validation_results]})
        blocking_failure = next((result for result in validation_results if not result.passed), None)
        if blocking_failure is not None:
            failed_codes = [result.rule_code for result in validation_results if not result.passed]
            transition_order(session, order, OrderEvent.RULES_FAILED_CRITICAL, trace_id=trace_id, detail={"rule_code": blocking_failure.rule_code, "failed_rule_codes": failed_codes})
            exception_case = create_exception_case(session, order, ExceptionType.VALIDATION_BLOCKED, "High", validation_blocker_summary(validation_results), validation_results, trace_id=trace_id)
            enqueue_validation_failure_notification(session, order, validation_results, exception_case, trace_id=trace_id)
            return {"order_id": order.id, "order_no": order.order_no, "status": order.status, "validation_passed": False}
        transition_order(session, order, OrderEvent.RULES_PASSED, trace_id=trace_id)

    # ── ERP 制单（预审通过后自动执行）──
    notice = None
    if order.status == OrderStatus.VALIDATED.value and config_bool(session, "erp_write_enabled", False):
        erp_result = process_erp_billing(session, order, trace_id=trace_id)
        if erp_result.get("erp_skipped"):
            pass  # 备货→武汉仓跳过制单，直接到发货通知
        elif not erp_result.get("erp_success"):
            return {"order_id": order.id, "order_no": order.order_no, "status": order.status,
                    "validation_passed": True, "erp_success": False, "erp_error": erp_result.get("error")}

    # ── 发货通知（ERP_SAVED 后或跳过制单时）──
    if order.status in {OrderStatus.ERP_SAVED.value, OrderStatus.VALIDATED.value}:
        if is_platform_fulfilled_order(order, crm_order):
            archive_platform_fulfilled_order(session, order, crm_order, trace_id=trace_id)
        else:
            notice = create_delivery_notice(session, order)
            transition_order(session, order, OrderEvent.DELIVERY_NOTICE_CREATED, trace_id=trace_id, detail={"notice_no": notice.notice_no})
    if order.status == OrderStatus.DELIVERY_NOTICE_READY.value:
        notice = notice or latest_delivery_notice(session, order)
        if notice is None:
            notice = create_delivery_notice(session, order)
        if config_bool(session, "oms_auto_confirm_delivery_notice", False):
            confirm_delivery_notice(session, notice, confirmed_by="auto", trace_id=trace_id)
    return {"order_id": order.id, "order_no": order.order_no, "status": order.status, "validation_passed": order.status != OrderStatus.VALIDATION_BLOCKED.value}




def is_crm_order_cancelled(crm_order: CrmSalesOrder) -> bool:
    evidence = crm_cancel_evidence(crm_order)
    text = " ".join(str(value).strip().lower() for value in evidence.values() if value is not None)
    if not text:
        return False
    return any(token in text for token in ["cancel", "cancelled", "canceled", "void", "invalid", "撤销", "取消", "作废", "无效", "关闭"])




def crm_cancel_evidence(crm_order: CrmSalesOrder) -> dict[str, Any]:
    raw = loads(crm_order.raw_json, {})
    return {
        "life_status": crm_order.life_status or raw.get("life_status"),
        "approval_status": crm_order.approval_status or raw.get("approval_status"),
        "cancel_status": raw.get("cancel_status") or raw.get("cancelStatus"),
        "cancelled_at": raw.get("cancelled_at") or raw.get("cancelledAt"),
        "is_cancelled": raw.get("is_cancelled") or raw.get("isCancelled"),
    }




def crm_change_exception_type(status: OrderStatus) -> ExceptionType:
    if status == OrderStatus.OMS_ACCEPTED:
        return ExceptionType.CRM_CHANGED_AFTER_OMS_ACCEPTED
    if status == OrderStatus.PICKING:
        return ExceptionType.CRM_CHANGED_DURING_PICKING
    if status in {OrderStatus.SHIPPED, OrderStatus.FULFILLMENT_ARCHIVED, OrderStatus.SIGNED, OrderStatus.FINANCE_CHECKING, OrderStatus.FINANCE_EXCEPTION}:
        return ExceptionType.CRM_CHANGED_AFTER_SHIPPED
    return ExceptionType.CRM_CHANGED_AFTER_OMS_ACCEPTED




def expire_delivery_notices(session: Session, order: MiddlePlatformOrder, *, reason: str, target_status: str = "Stale") -> int:
    count = 0
    for notice in order.delivery_notices:
        if notice.status in {"Previewed", "Created", "Confirmed", "Retrying", "Blocked"}:
            old_status = notice.status
            notice.status = target_status
            notice.last_error = reason
            notice.updated_at = now_utc()
            count += 1
            session.add(
                AuditEvent(
                    event_type="DeliveryNoticeExpired",
                    related_object_type="DeliveryNotice",
                    related_object_id=notice.id,
                    detail=dumps({"notice_no": notice.notice_no, "from_status": old_status, "to_status": target_status, "reason": reason}),
                )
            )
    return count




def cancel_oms_push_jobs(session: Session, order: MiddlePlatformOrder, *, reason: str) -> int:
    notice_ids = {notice.id for notice in order.delivery_notices}
    if not notice_ids:
        return 0
    count = 0
    jobs = session.query(ProcessingJob).filter(ProcessingJob.job_type == "OMS_PUSH_NOTICE", ProcessingJob.status.in_(["Pending", "Failed"])).all()
    for job in jobs:
        payload = loads(job.payload_json, {})
        if payload.get("notice_id") not in notice_ids:
            continue
        job.status = "Cancelled"
        job.error_message = reason
        job.updated_at = now_utc()
        count += 1
    if count:
        session.add(
            AuditEvent(
                event_type="OmsPushJobsCancelled",
                related_object_type="MiddlePlatformOrder",
                related_object_id=order.id,
                detail=dumps({"cancelled_count": count, "reason": reason}),
            )
        )
    return count




def validate_crm_order_parsed_event(payload: dict[str, Any]) -> None:
    if not isinstance(payload, dict):
        raise InvalidCrmOrderParsedEvent("CRM_ORDER_PARSED 事件必须是 JSON 对象")
    if payload.get("event_type") != "CRM_ORDER_PARSED":
        raise InvalidCrmOrderParsedEvent("event_type 必须为 CRM_ORDER_PARSED")
    data = payload.get("data")
    if not isinstance(data, dict):
        raise InvalidCrmOrderParsedEvent("data 必须是 JSON 对象")
    missing = [field for field in ("crm_order_id", "payload_hash", "order_head") if not data.get(field)]
    if missing:
        raise InvalidCrmOrderParsedEvent(f"CRM_ORDER_PARSED 缺少字段：{', '.join(missing)}")
    if not isinstance(data.get("order_head"), dict):
        raise InvalidCrmOrderParsedEvent("order_head 必须是 JSON 对象")




def reset_order_for_force_revalidate(session: Session, order: MiddlePlatformOrder, *, trace_id: str = "") -> bool:
    if order.status not in {OrderStatus.VALIDATED.value, OrderStatus.DELIVERY_NOTICE_READY.value}:
        return False
    old_status = order.status
    expired_notices = expire_delivery_notices(session, order, reason="force_revalidate_before_oms_push")
    cancel_oms_push_jobs(session, order, reason="force_revalidate_before_oms_push")
    order.status = OrderStatus.IMPORTED.value
    order.validated_at = None
    order.updated_at = now_utc()
    session.add(
        AuditEvent(
            event_type="OrderForceRevalidateReset",
            related_object_type="MiddlePlatformOrder",
            related_object_id=order.id,
            detail=dumps({"from_status": old_status, "to_status": order.status, "expired_notices": expired_notices, "trace_id": trace_id}),
        )
    )
    return True




def reactivate_cancelled_order_from_crm(session: Session, order: MiddlePlatformOrder, crm_order: CrmSalesOrder, *, trace_id: str = "") -> None:
    old_status = order.status
    order.status = OrderStatus.CRM_APPROVED.value
    order.validated_at = None
    order.validation_summary_json = "{}"
    order.updated_at = now_utc()
    session.add(
        AuditEvent(
            event_type="CancelledOrderReactivatedFromCrm",
            related_object_type="MiddlePlatformOrder",
            related_object_id=order.id,
            detail=dumps(
                {
                    "from_status": old_status,
                    "to_status": order.status,
                    "crm_order_no": crm_order.crm_order_no,
                    "payload_hash": crm_order.payload_hash,
                    "trace_id": trace_id,
                }
            ),
        )
    )




def handle_crm_snapshot_changed(
    session: Session,
    order: MiddlePlatformOrder,
    crm_order: CrmSalesOrder,
    *,
    new_payload_hash: str,
    trace_id: str = "",
) -> dict[str, Any]:
    # 对比历史快照的 CRM 修改时间，如果 CRM 修改时间完全一致，说明不是 CRM 侧的主动修改，忽略本次变更事件
    from backend.app.models import CrmOrderSnapshot
    current_snapshot = (
        session.query(CrmOrderSnapshot)
        .filter(
            CrmOrderSnapshot.source_system == crm_order.source_system,
            CrmOrderSnapshot.crm_order_id == crm_order.crm_order_id,
            CrmOrderSnapshot.payload_hash == order.payload_hash,
        )
        .first()
    )
    latest_snapshot = (
        session.query(CrmOrderSnapshot)
        .filter(
            CrmOrderSnapshot.source_system == crm_order.source_system,
            CrmOrderSnapshot.crm_order_id == crm_order.crm_order_id,
            CrmOrderSnapshot.payload_hash == new_payload_hash,
        )
        .first()
    )
    if current_snapshot and latest_snapshot:
        try:
            curr_raw = loads(current_snapshot.raw_json, {})
            late_raw = loads(latest_snapshot.raw_json, {})
            curr_up = curr_raw.get("updated_at") or curr_raw.get("last_modified_time")
            late_up = late_raw.get("updated_at") or late_raw.get("last_modified_time")
            if curr_up and curr_up == late_up:
                order.payload_hash = new_payload_hash
                session.flush()
                logger.info(
                    "CRM order %s payload_hash changed due to non-CRM updates (e.g. OCR/enrichment/signatures). Skipping invalidation.",
                    crm_order.crm_order_no
                )
                return {"continue_processing": True}
        except Exception as e:
            logger.warning("Compare CRM update time failed: %s", e)

    status = OrderStatus(order.status)
    detail = {
        "crm_order_id": crm_order.crm_order_id,
        "crm_order_no": crm_order.crm_order_no,
        "old_payload_hash": order.payload_hash,
        "new_payload_hash": new_payload_hash,
        "trace_id": trace_id,
    }
    if status in {OrderStatus.IMPORTED, OrderStatus.VALIDATION_BLOCKED, OrderStatus.VALIDATED}:
        expire_delivery_notices(session, order, reason="crm_snapshot_changed")
        transition_order(session, order, OrderEvent.CRM_SNAPSHOT_CHANGED, trace_id=trace_id, detail=detail)
        return {"continue_processing": True}

    # ERP_SAVED + CRM 变更 → Q6：先物理冲销金蝶单据，再退回预审
    if status == OrderStatus.ERP_SAVED:
        if order.erp_bill_no:
            try:
                config = kingdee_config_from_session(session)
                client = KingdeeClient(config)
                # 通过 FBillNo 查询单据内码
                query_result = client.execute_bill_query(
                    form_id="SAL_SaleOrder",
                    field_keys="FID,FBillNo",
                    filter_string=f"FBillNo = '{order.erp_bill_no}'",
                    limit=1,
                )
                bill_internal_id = None
                items = normalize_query_rows(query_result.get("raw"))
                if items and isinstance(items, list) and len(items) > 0:
                    row = items[0]
                    if isinstance(row, list) and len(row) > 0:
                        bill_internal_id = row[0]
                if bill_internal_id:
                    unaudit_r = client.un_audit_bill(form_id="SAL_SaleOrder", bill_ids=[bill_internal_id])
                    if not unaudit_r.get("ok"):
                        logger.warning("Q6 UnAudit 失败: %s", unaudit_r.get("message"))
                    cancel_r = client.cancel_bill(form_id="SAL_SaleOrder", bill_ids=[bill_internal_id])
                    if not cancel_r.get("ok"):
                        logger.warning("Q6 Cancel 失败: %s", cancel_r.get("message"))
                else:
                    logger.warning("Q6 未找到金蝶单据: %s", order.erp_bill_no)
            except Exception as exc:
                logger.warning("Q6 ERP 冲销异常（不影响流程继续）: %s", exc)

        expire_delivery_notices(session, order, reason="crm_snapshot_changed_after_erp_saved")
        transition_order(session, order, OrderEvent.CRM_SNAPSHOT_CHANGED, trace_id=trace_id, detail=detail | {"q6_erp_reverted": True})
        return {"continue_processing": True, "q6_erp_reverted": True}

    if status == OrderStatus.DELIVERY_NOTICE_READY:
        expire_delivery_notices(session, order, reason="crm_snapshot_changed_before_oms_push")
        transition_order(session, order, OrderEvent.CRM_SNAPSHOT_CHANGED, trace_id=trace_id, detail=detail)
        case = create_exception_case(
            session,
            order,
            ExceptionType.CRM_CHANGED_BEFORE_OMS_PUSH,
            "High",
            "CRM 订单在发货通知生成后发生变更，旧发货预览已作废，需重新预审。",
            [],
            trace_id=trace_id,
        )
        return {"order_id": order.id, "order_no": order.order_no, "status": order.status, "exception_case_id": case.id, "continue_processing": False}

    if status in {OrderStatus.OMS_PENDING, OrderStatus.OMS_RETRYING, OrderStatus.OMS_BLOCKED}:
        cancel_oms_push_jobs(session, order, reason="crm_snapshot_changed")
        expire_delivery_notices(session, order, reason="crm_snapshot_changed_during_oms_push")
        transition_order(session, order, OrderEvent.CRM_SNAPSHOT_CHANGED, trace_id=trace_id, detail=detail)
        exception_type = ExceptionType.CRM_CHANGED_DURING_OMS_PENDING if status == OrderStatus.OMS_PENDING else ExceptionType.CRM_CHANGED_DURING_OMS_RETRY
        case = create_exception_case(
            session,
            order,
            exception_type,
            "Critical",
            "CRM 订单在 OMS 下推或重试期间发生变更，已暂停旧下推任务，需人工确认后重新生成发货预览。",
            [],
            trace_id=trace_id,
        )
        return {"order_id": order.id, "order_no": order.order_no, "status": order.status, "exception_case_id": case.id, "continue_processing": False}

    case_type = crm_change_exception_type(status)
    case = create_exception_case(
        session,
        order,
        case_type,
        "Critical",
        "CRM 订单在 OMS 已接收或履约执行后发生变更，系统已冻结自动处理，需人工判断下游改单/拦截/差异处理。",
        [],
        trace_id=trace_id,
    )
    session.add(
        AuditEvent(
            event_type="CrmSnapshotChangedAfterDownstreamAccepted",
            related_object_type="MiddlePlatformOrder",
            related_object_id=order.id,
            detail=dumps(detail | {"exception_type": case_type}),
        )
    )
    return {"order_id": order.id, "order_no": order.order_no, "status": order.status, "exception_case_id": case.id, "continue_processing": False}




def handle_crm_cancel_confirmed(
    session: Session,
    order: MiddlePlatformOrder,
    crm_order: CrmSalesOrder,
    *,
    payload_hash: str = "",
    trace_id: str = "",
) -> dict[str, Any]:
    status = OrderStatus(order.status)
    detail = {
        "crm_order_id": crm_order.crm_order_id,
        "crm_order_no": crm_order.crm_order_no,
        "old_payload_hash": order.payload_hash,
        "cancel_payload_hash": payload_hash or crm_order.payload_hash,
        "trace_id": trace_id,
        "evidence": crm_cancel_evidence(crm_order),
    }
    if status in {
        OrderStatus.CRM_APPROVED,
        OrderStatus.IMPORTED,
        OrderStatus.VALIDATING,
        OrderStatus.VALIDATION_BLOCKED,
        OrderStatus.VALIDATED,
        OrderStatus.DELIVERY_NOTICE_READY,
    }:
        cancel_oms_push_jobs(session, order, reason="crm_cancelled_before_oms_push")
        expire_delivery_notices(session, order, reason="crm_cancelled_before_oms_push", target_status="Cancelled")
        transition_order(session, order, OrderEvent.CANCEL_CONFIRMED, trace_id=trace_id, detail=detail)
        case = create_exception_case(
            session,
            order,
            ExceptionType.CRM_CANCELLED_BEFORE_OMS_PUSH,
            "High",
            "CRM 订单已撤销/作废，未下推 OMS 的中台流程已取消。",
            [],
            trace_id=trace_id,
        )
        return {"order_id": order.id, "order_no": order.order_no, "status": order.status, "exception_case_id": case.id, "cancelled": True}

    if status == OrderStatus.OMS_PENDING:
        cancel_oms_push_jobs(session, order, reason="crm_cancelled_during_oms_pending")
        expire_delivery_notices(session, order, reason="crm_cancelled_during_oms_pending", target_status="Cancelled")
        transition_order(session, order, OrderEvent.CANCEL_CONFIRMED, trace_id=trace_id, detail=detail)
        case = create_exception_case(
            session,
            order,
            ExceptionType.CRM_CANCELLED_DURING_OMS_PENDING,
            "Critical",
            "CRM 订单在 OMS 待推送期间撤销，已取消待推任务并终止中台流程。",
            [],
            trace_id=trace_id,
        )
        return {"order_id": order.id, "order_no": order.order_no, "status": order.status, "exception_case_id": case.id, "cancelled": True}

    if status in {OrderStatus.OMS_RETRYING, OrderStatus.OMS_BLOCKED}:
        cancel_oms_push_jobs(session, order, reason="crm_cancelled_during_oms_retry")
        expire_delivery_notices(session, order, reason="crm_cancelled_during_oms_retry", target_status="Cancelled")
        transition_order(session, order, OrderEvent.CANCEL_CONFIRMED, trace_id=trace_id, detail=detail)
        case = create_exception_case(
            session,
            order,
            ExceptionType.CRM_CANCELLED_DURING_OMS_RETRY,
            "Critical",
            "CRM 订单在 OMS 重试/阻断期间撤销，已停止自动重试，需确认下游是否已创建单据。",
            [],
            trace_id=trace_id,
        )
        return {"order_id": order.id, "order_no": order.order_no, "status": order.status, "exception_case_id": case.id, "cancelled": True}

    case_type = ExceptionType.CRM_CANCELLED_AFTER_SHIPPED if status in {OrderStatus.SHIPPED, OrderStatus.FULFILLMENT_ARCHIVED, OrderStatus.SIGNED, OrderStatus.FINANCE_CHECKING, OrderStatus.FINANCE_EXCEPTION} else ExceptionType.CRM_CANCELLED_AFTER_OMS_ACCEPTED
    case = create_exception_case(
        session,
        order,
        case_type,
        "Critical",
        "CRM 订单在 OMS 已接收或已发货后撤销，系统不自动回滚下游单据，需人工处理拦截、取消或售后差异。",
        [],
        trace_id=trace_id,
    )
    session.add(
        AuditEvent(
            event_type="CrmCancelConfirmedAfterDownstreamAccepted",
            related_object_type="MiddlePlatformOrder",
            related_object_id=order.id,
            detail=dumps(detail | {"exception_type": case_type}),
        )
    )
    return {"order_id": order.id, "order_no": order.order_no, "status": order.status, "exception_case_id": case.id, "cancelled": False}




