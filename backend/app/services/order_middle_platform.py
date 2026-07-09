from __future__ import annotations

import base64
import hashlib
import logging
import re
import time
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal, InvalidOperation
from enum import Enum
from typing import Any, Protocol

logger = logging.getLogger(__name__)

from sqlalchemy import func, or_
from sqlalchemy.orm import Session

from backend.app.services.rules import (
    BlockerLevel,
    DEFAULT_RULES,
    OrderContext,
    OrderValidationRule,
    ValidationResult,
    is_review_rule_enabled,
    register_rule,
    remove_rule,
)
from backend.app.services.rules.helpers import (
    config_bool,
    config_dict,
    config_int,
    config_list,
    config_value,
    inventory_available_quantity,
    is_approved_status,
    parse_decimal,
)
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
from backend.app.services.jsonutil import dumps, loads
from backend.app.services.storage import save_attachment
from backend.app.services.task_scheduler import RetryPolicy, next_retry_at
from backend.app.services.time_utils import format_beijing_time

from backend.app.models import (
    AuditEvent,
    ChannelPricing,
    CrmSalesOrder,
    DeliveryNotice,
    ExceptionCase,
    IntegrationEvent,
    MiddlePlatformOrder,
    MiddlePlatformOrderItem,
    OrderAttachment,
    OutboundMailJob,
    ProcessingJob,
    ProductSKU,
    SystemConfig,
    User,
    now_utc,
)


class IllegalStateTransition(ValueError):
    pass


class DuplicateEventException(ValueError):
    pass


def payload_fingerprint(payload: Any) -> str:
    return hashlib.sha256(dumps(payload).encode("utf-8")).hexdigest()


def record_integration_event(
    session: Session,
    *,
    source_system: str,
    event_type: str,
    biz_key: str,
    payload: Any,
    trace_id: str = "",
    status: str = "Pending",
    retry_count: int = 0,
    error_message: str | None = None,
    response: Any | None = None,
) -> IntegrationEvent:
    payload_hash = payload_fingerprint(payload)
    event = (
        session.query(IntegrationEvent)
        .filter(
            IntegrationEvent.event_type == event_type,
            IntegrationEvent.biz_key == biz_key,
            IntegrationEvent.payload_hash == payload_hash,
        )
        .first()
    )
    if event is None:
        event = IntegrationEvent(
            trace_id=trace_id or str(uuid.uuid4()),
            source_system=source_system,
            event_type=event_type,
            biz_key=biz_key,
            payload_hash=payload_hash,
            request_json=dumps(payload),
        )
        session.add(event)
    event.status = status
    event.retry_count = retry_count
    event.error_message = error_message
    event.response_json = dumps(response) if response is not None else event.response_json
    event.updated_at = now_utc()
    return event


class InvalidCrmOrderParsedEvent(ValueError):
    pass


class OrderStatus(str, Enum):
    CRM_APPROVED = "CRM_APPROVED"
    IMPORTED = "IMPORTED"
    VALIDATING = "VALIDATING"
    VALIDATION_BLOCKED = "VALIDATION_BLOCKED"
    VALIDATED = "VALIDATED"
    # ERP 制单状态（一期新增）
    ERP_PENDING = "ERP_PENDING"
    ERP_SAVING = "ERP_SAVING"
    ERP_SAVED = "ERP_SAVED"
    ERP_FAILED = "ERP_FAILED"
    DELIVERY_NOTICE_READY = "DELIVERY_NOTICE_READY"
    OMS_PENDING = "OMS_PENDING"
    OMS_RETRYING = "OMS_RETRYING"
    OMS_BLOCKED = "OMS_BLOCKED"
    OMS_ACCEPTED = "OMS_ACCEPTED"
    PICKING = "PICKING"
    SHIPPED = "SHIPPED"
    FULFILLMENT_ARCHIVED = "FULFILLMENT_ARCHIVED"
    SIGNED = "SIGNED"
    FINANCE_CHECKING = "FINANCE_CHECKING"
    FINANCE_EXCEPTION = "FINANCE_EXCEPTION"
    CLOSED = "CLOSED"
    CANCELLED = "CANCELLED"
    OUT_OF_SCOPE = "OUT_OF_SCOPE"


class OrderEvent(str, Enum):
    ORDER_SNAPSHOT_FETCHED = "OrderSnapshotFetched"
    CRM_SNAPSHOT_CHANGED = "CrmSnapshotChanged"
    START_VALIDATION = "StartValidation"
    RULES_PASSED = "RulesPassed"
    RULES_FAILED_CRITICAL = "RulesFailedCritical"
    EXCEPTION_RESOLVED_AND_REVALIDATE = "ExceptionResolvedAndRevalidate"
    # ERP 制单事件（一期新增）
    ERP_SAVE_STARTED = "ErpSaveStarted"
    ERP_SAVE_SUCCESS = "ErpSaveSuccess"
    ERP_SAVE_FAILED = "ErpSaveFailed"
    ERP_SUBMIT_FAILED = "ErpSubmitFailed"
    ERP_AUDIT_FAILED = "ErpAuditFailed"
    EXCEPTION_RESOLVED_AND_RE_ERP = "ExceptionResolvedAndReErp"
    DELIVERY_NOTICE_CREATED = "DeliveryNoticeCreated"
    ENQUEUE_OMS_PUSH = "EnqueueOmsPush"
    OMS_PUSH_SUCCESS = "OmsPushSuccess"
    FIRST_OMS_PUSH_FAILED = "FirstOmsPushFailed"
    RETRY_TIMER_DUE_AND_OMS_SUCCESS = "RetryTimerDueAndOmsSuccess"
    RETRY_FAILED_BUT_UNDER_MAX_RETRIES = "RetryFailedButUnderMaxRetries"
    RETRY_REACHED_MAX_RETRIES = "RetryReachedMaxRetries"
    EXCEPTION_RESOLVED_AND_REPLAY = "ExceptionResolvedAndReplay"
    OMS_PICKING_STARTED = "OmsPickingStarted"
    OMS_SHIPPED = "OmsShipped"
    ARCHIVE_PHASE1_FULFILLMENT = "ArchivePhase1Fulfillment"
    LOGISTICS_SIGNED = "LogisticsSigned"
    START_FINANCE_CHECK = "StartFinanceCheck"
    FINANCE_CHECK_FAILED = "FinanceCheckFailed"
    FINANCE_CHECK_PASSED = "FinanceCheckPassed"
    CANCEL_CONFIRMED = "CancelConfirmed"


# ═══════════════════════════════════════════════════════
# ExceptionType —— 统一异常类型枚举


class ExceptionType(str, Enum):
    """V2 订单中台统一异常类型枚举，对应设计文档 §5.2.2"""
    VALIDATION_BLOCKED = "VALIDATION_BLOCKED"
    SKU_MAPPING_MISSING = "SKU_MAPPING_MISSING"
    CUSTOMER_MAPPING_MISSING = "CUSTOMER_MAPPING_MISSING"
    INVENTORY_SHORTAGE = "INVENTORY_SHORTAGE"
    WAREHOUSE_OR_LOGISTICS_MISSING = "WAREHOUSE_OR_LOGISTICS_MISSING"
    CRM_CHANGED_BEFORE_OMS_PUSH = "CRM_CHANGED_BEFORE_OMS_PUSH"
    CRM_CHANGED_AFTER_OMS_ACCEPTED = "CRM_CHANGED_AFTER_OMS_ACCEPTED"
    CRM_CHANGED_DURING_OMS_PENDING = "CRM_CHANGED_DURING_OMS_PENDING"
    CRM_CHANGED_DURING_OMS_RETRY = "CRM_CHANGED_DURING_OMS_RETRY"
    CRM_CHANGED_DURING_PICKING = "CRM_CHANGED_DURING_PICKING"
    CRM_CHANGED_AFTER_SHIPPED = "CRM_CHANGED_AFTER_SHIPPED"
    CRM_CANCELLED_BEFORE_OMS_PUSH = "CRM_CANCELLED_BEFORE_OMS_PUSH"
    CRM_CANCELLED_AFTER_OMS_ACCEPTED = "CRM_CANCELLED_AFTER_OMS_ACCEPTED"
    CRM_CANCELLED_DURING_OMS_PENDING = "CRM_CANCELLED_DURING_OMS_PENDING"
    CRM_CANCELLED_DURING_OMS_RETRY = "CRM_CANCELLED_DURING_OMS_RETRY"
    CRM_CANCELLED_AFTER_SHIPPED = "CRM_CANCELLED_AFTER_SHIPPED"
    CRM_DETAIL_SYNC_FAILED = "CRM_DETAIL_SYNC_FAILED"
    CRM_ATTACHMENT_MISSING = "CRM_ATTACHMENT_MISSING"
    CRM_ATTACHMENT_PARSE_FAILED = "CRM_ATTACHMENT_PARSE_FAILED"
    OMS_BLOCKED = "OMS_BLOCKED"
    OMS_REQUIRED_FIELDS_MISSING = "OMS_REQUIRED_FIELDS_MISSING"
    OMS_PUSH_TIMEOUT = "OMS_PUSH_TIMEOUT"
    OMS_VALIDATION_FAILED = "OMS_VALIDATION_FAILED"
    OMS_IDEMPOTENCY_CONFLICT = "OMS_IDEMPOTENCY_CONFLICT"
    OMS_STATUS_CONFLICT = "OMS_STATUS_CONFLICT"
    JOB_LOCK_CONFLICT = "JOB_LOCK_CONFLICT"
    DUPLICATE_EVENT_REPLAYED = "DUPLICATE_EVENT_REPLAYED"
    MANUAL_CONFIRM_WITH_STALE_PREVIEW = "MANUAL_CONFIRM_WITH_STALE_PREVIEW"
    MANUAL_REPLAY_WITHOUT_FIX = "MANUAL_REPLAY_WITHOUT_FIX"
    UNAUTHORIZED_STATE_OVERRIDE = "UNAUTHORIZED_STATE_OVERRIDE"
    INTEGRATION_CONFIG_INVALID = "INTEGRATION_CONFIG_INVALID"


STATE_TRANSITIONS: dict[tuple[OrderStatus, OrderEvent], OrderStatus] = {
    (OrderStatus.CRM_APPROVED, OrderEvent.ORDER_SNAPSHOT_FETCHED): OrderStatus.IMPORTED,
    (OrderStatus.IMPORTED, OrderEvent.CRM_SNAPSHOT_CHANGED): OrderStatus.IMPORTED,
    (OrderStatus.VALIDATION_BLOCKED, OrderEvent.CRM_SNAPSHOT_CHANGED): OrderStatus.IMPORTED,
    (OrderStatus.VALIDATED, OrderEvent.CRM_SNAPSHOT_CHANGED): OrderStatus.IMPORTED,
    (OrderStatus.ERP_PENDING, OrderEvent.CRM_SNAPSHOT_CHANGED): OrderStatus.IMPORTED,
    (OrderStatus.ERP_SAVING, OrderEvent.CRM_SNAPSHOT_CHANGED): OrderStatus.IMPORTED,
    (OrderStatus.ERP_SAVED, OrderEvent.CRM_SNAPSHOT_CHANGED): OrderStatus.IMPORTED,
    (OrderStatus.ERP_FAILED, OrderEvent.CRM_SNAPSHOT_CHANGED): OrderStatus.IMPORTED,
    (OrderStatus.DELIVERY_NOTICE_READY, OrderEvent.CRM_SNAPSHOT_CHANGED): OrderStatus.VALIDATION_BLOCKED,
    (OrderStatus.OMS_PENDING, OrderEvent.CRM_SNAPSHOT_CHANGED): OrderStatus.VALIDATION_BLOCKED,
    (OrderStatus.OMS_RETRYING, OrderEvent.CRM_SNAPSHOT_CHANGED): OrderStatus.VALIDATION_BLOCKED,
    (OrderStatus.OMS_BLOCKED, OrderEvent.CRM_SNAPSHOT_CHANGED): OrderStatus.VALIDATION_BLOCKED,
    (OrderStatus.IMPORTED, OrderEvent.START_VALIDATION): OrderStatus.VALIDATING,
    (OrderStatus.VALIDATING, OrderEvent.RULES_PASSED): OrderStatus.VALIDATED,
    (OrderStatus.VALIDATING, OrderEvent.RULES_FAILED_CRITICAL): OrderStatus.VALIDATION_BLOCKED,
    (OrderStatus.VALIDATION_BLOCKED, OrderEvent.EXCEPTION_RESOLVED_AND_REVALIDATE): OrderStatus.VALIDATING,
    # ERP 制单跃迁（一期新增）
    (OrderStatus.VALIDATED, OrderEvent.ERP_SAVE_STARTED): OrderStatus.ERP_PENDING,
    (OrderStatus.ERP_PENDING, OrderEvent.ERP_SAVE_STARTED): OrderStatus.ERP_SAVING,
    (OrderStatus.ERP_SAVING, OrderEvent.ERP_SAVE_SUCCESS): OrderStatus.ERP_SAVED,
    (OrderStatus.ERP_SAVING, OrderEvent.ERP_SAVE_FAILED): OrderStatus.ERP_FAILED,
    (OrderStatus.ERP_SAVING, OrderEvent.ERP_SUBMIT_FAILED): OrderStatus.ERP_FAILED,
    (OrderStatus.ERP_SAVING, OrderEvent.ERP_AUDIT_FAILED): OrderStatus.ERP_FAILED,
    (OrderStatus.ERP_FAILED, OrderEvent.EXCEPTION_RESOLVED_AND_RE_ERP): OrderStatus.ERP_PENDING,
    (OrderStatus.ERP_SAVED, OrderEvent.DELIVERY_NOTICE_CREATED): OrderStatus.DELIVERY_NOTICE_READY,
    (OrderStatus.VALIDATED, OrderEvent.DELIVERY_NOTICE_CREATED): OrderStatus.DELIVERY_NOTICE_READY,
    (OrderStatus.DELIVERY_NOTICE_READY, OrderEvent.ENQUEUE_OMS_PUSH): OrderStatus.OMS_PENDING,
    (OrderStatus.OMS_PENDING, OrderEvent.OMS_PUSH_SUCCESS): OrderStatus.OMS_ACCEPTED,
    (OrderStatus.OMS_PENDING, OrderEvent.FIRST_OMS_PUSH_FAILED): OrderStatus.OMS_RETRYING,
    (OrderStatus.OMS_PENDING, OrderEvent.RETRY_REACHED_MAX_RETRIES): OrderStatus.OMS_BLOCKED,
    (OrderStatus.OMS_RETRYING, OrderEvent.RETRY_TIMER_DUE_AND_OMS_SUCCESS): OrderStatus.OMS_ACCEPTED,
    (OrderStatus.OMS_RETRYING, OrderEvent.RETRY_FAILED_BUT_UNDER_MAX_RETRIES): OrderStatus.OMS_RETRYING,
    (OrderStatus.OMS_RETRYING, OrderEvent.RETRY_REACHED_MAX_RETRIES): OrderStatus.OMS_BLOCKED,
    (OrderStatus.OMS_BLOCKED, OrderEvent.EXCEPTION_RESOLVED_AND_REPLAY): OrderStatus.OMS_PENDING,
    (OrderStatus.OMS_ACCEPTED, OrderEvent.OMS_PICKING_STARTED): OrderStatus.PICKING,
    (OrderStatus.PICKING, OrderEvent.OMS_SHIPPED): OrderStatus.SHIPPED,
    (OrderStatus.VALIDATED, OrderEvent.ARCHIVE_PHASE1_FULFILLMENT): OrderStatus.FULFILLMENT_ARCHIVED,
    (OrderStatus.SHIPPED, OrderEvent.ARCHIVE_PHASE1_FULFILLMENT): OrderStatus.FULFILLMENT_ARCHIVED,
    (OrderStatus.FULFILLMENT_ARCHIVED, OrderEvent.LOGISTICS_SIGNED): OrderStatus.SIGNED,
    (OrderStatus.SIGNED, OrderEvent.START_FINANCE_CHECK): OrderStatus.FINANCE_CHECKING,
    (OrderStatus.FINANCE_CHECKING, OrderEvent.FINANCE_CHECK_FAILED): OrderStatus.FINANCE_EXCEPTION,
    (OrderStatus.FINANCE_CHECKING, OrderEvent.FINANCE_CHECK_PASSED): OrderStatus.CLOSED,
}


def transition_order(
    session: Session,
    order: MiddlePlatformOrder,
    event: OrderEvent,
    *,
    operator_type: str = "System",
    trace_id: str = "",
    detail: dict[str, Any] | None = None,
) -> None:
    current = OrderStatus(order.status)
    target = STATE_TRANSITIONS.get((current, event))
    if target is None and event == OrderEvent.CANCEL_CONFIRMED and current not in {OrderStatus.CLOSED, OrderStatus.CANCELLED}:
        target = OrderStatus.CANCELLED
    if target is None:
        raise IllegalStateTransition(f"非法订单状态跃迁：{current.value} + {event.value}")
    if target == current and event not in {OrderEvent.RETRY_FAILED_BUT_UNDER_MAX_RETRIES, OrderEvent.CRM_SNAPSHOT_CHANGED}:
        return
    old_status = order.status
    order.status = target.value
    order.version += 1
    order.updated_at = now_utc()
    if target == OrderStatus.IMPORTED and order.imported_at is None:
        order.imported_at = now_utc()
    if target == OrderStatus.VALIDATED:
        order.validated_at = now_utc()
    session.add(
        AuditEvent(
            event_type="OrderStatusChanged",
            actor=operator_type,
            related_object_type="MiddlePlatformOrder",
            related_object_id=order.id,
            detail=dumps(
                {
                    "order_no": order.order_no,
                    "from_status": old_status,
                    "to_status": target.value,
                    "event": event.value,
                    "operator_type": operator_type,
                    "trace_id": trace_id,
                    **(detail or {}),
                }
            ),
        )
    )


# ══════════════════════════════════════════════
# ERP 制单处理（一期新增）
# ══════════════════════════════════════════════

def _erp_config_ready(session: Session) -> bool:
    """检查金蝶写入配置是否就绪"""
    enabled = session.get(SystemConfig, "erp_write_enabled")
    if not enabled or enabled.value != "true":
        return False
    try:
        kingdee_config_from_session(session)
        return True
    except KingdeeConfigError:
        return False


def process_erp_billing(session: Session, order: MiddlePlatformOrder, *, trace_id: str = "") -> dict[str, Any]:
    """执行 ERP 制单全流程：Save → Submit → Audit

    预审通过后调用。备货→武汉仓跳过此流程。
    """
    if should_skip_erp_billing(order):
        transition_order(session, order, OrderEvent.DELIVERY_NOTICE_CREATED, trace_id=trace_id)
        return {"order_id": order.id, "order_no": order.order_no, "status": order.status, "erp_skipped": True}

    if not _erp_config_ready(session):
        transition_order(session, order, OrderEvent.ERP_SAVE_FAILED, trace_id=trace_id,
                         detail={"reason": "金蝶写入未配置", "error_type": "KingdeeConfigError"})
        create_exception_case(session, order, ExceptionType.OMS_BLOCKED, "High",
                              "ERP 制单失败：金蝶写入未配置", [], trace_id=trace_id)
        return {"order_id": order.id, "order_no": order.order_no, "status": order.status, "erp_success": False, "error": "金蝶写入未配置"}

    # 预审通过后分配订单号
    order.order_no = generate_middle_order_no(session)
    transition_order(session, order, OrderEvent.ERP_SAVE_STARTED, trace_id=trace_id)
    session.flush()

    try:
        config = kingdee_config_from_session(session)
        client = KingdeeClient(config)

        # Step 1: Save
        bill_model = build_sales_order_model(session, order, order.items)
        save_result = client.save_bill(
            form_id="SAL_SaleOrder",
            model=bill_model,
            need_return_fields=["FBillNo", "FDate"],
        )
        if not save_result.get("ok"):
            error_msg = save_result.get("message") or "金蝶 Save 失败"
            result = save_result.get("result")
            if isinstance(result, dict):
                rs = result.get("ResponseStatus")
                if isinstance(rs, dict):
                    errors = rs.get("Errors", [])
                    if isinstance(errors, list):
                        details = [str(e.get("Message", "") or e.get("FieldName", "") or e) for e in errors if isinstance(e, dict)]
                        if details:
                            error_msg = f"{error_msg} | {'; '.join(details)}"
            transition_order(session, order, OrderEvent.ERP_SAVE_FAILED, trace_id=trace_id,
                             detail={"step": "save", "error": error_msg})
            create_exception_case(session, order, ExceptionType.OMS_BLOCKED, "High",
                                  f"ERP 制单失败(Save)：{error_msg}", [], trace_id=trace_id)
            return {"order_id": order.id, "order_no": order.order_no, "status": order.status, "erp_success": False, "error": error_msg}

        # 提取金蝶 FBillNo
        erp_bill_no = None
        rd = save_result.get("result")
        if isinstance(rd, dict):
            erp_bill_no = rd.get("Number")
        if erp_bill_no:
            order.erp_bill_no = erp_bill_no

        # Step 2: Submit
        bill_id = rd.get("Id") if isinstance(rd, dict) else None
        if bill_id:
            sub_result = client.submit_bill(form_id="SAL_SaleOrder", bill_ids=[bill_id])
            if not sub_result.get("ok"):
                error_msg = sub_result.get("message") or "金蝶 Submit 失败"
                transition_order(session, order, OrderEvent.ERP_SUBMIT_FAILED, trace_id=trace_id,
                                 detail={"step": "submit", "error": error_msg, "bill_id": bill_id})
                create_exception_case(session, order, ExceptionType.OMS_BLOCKED, "High",
                                      f"ERP 制单失败(Submit)：{error_msg}", [], trace_id=trace_id)
                return {"order_id": order.id, "order_no": order.order_no, "status": order.status, "erp_success": False, "error": error_msg}

            # Step 3: Audit
            aud_result = client.audit_bill(form_id="SAL_SaleOrder", bill_ids=[bill_id])
            if not aud_result.get("ok"):
                error_msg = aud_result.get("message") or "金蝶 Audit 失败（可能是审批流阻塞）"
                transition_order(session, order, OrderEvent.ERP_AUDIT_FAILED, trace_id=trace_id,
                                 detail={"step": "audit", "error": error_msg, "bill_id": bill_id})
                create_exception_case(session, order, ExceptionType.OMS_BLOCKED, "High",
                                      f"ERP 制单失败(Audit)：{error_msg}", [], trace_id=trace_id)
                return {"order_id": order.id, "order_no": order.order_no, "status": order.status, "erp_success": False, "error": error_msg}

        # 全部成功
        transition_order(session, order, OrderEvent.ERP_SAVE_SUCCESS, trace_id=trace_id,
                         detail={"erp_bill_no": erp_bill_no})

        # 触发发货通知邮件（一期核心：ERP 制单成功后自动通知物流）
        try:
            warehouse_code = ""
            notice = latest_delivery_notice(session, order)
            if notice and notice.warehouse_code:
                warehouse_code = notice.warehouse_code
            enqueue_delivery_notice_mail(
                session, order, order.items,
                warehouse=warehouse_code,
                special_requirements=None,
            )
        except Exception as mail_exc:
            logger.warning("发送发货通知邮件失败（不影响主流程）: %s", mail_exc)

        return {"order_id": order.id, "order_no": order.order_no, "status": order.status, "erp_success": True, "erp_bill_no": erp_bill_no}

    except KingdeeConfigError as exc:
        error_msg = f"金蝶配置错误：{exc}"
        transition_order(session, order, OrderEvent.ERP_SAVE_FAILED, trace_id=trace_id,
                         detail={"step": "config", "error": error_msg})
        create_exception_case(session, order, ExceptionType.OMS_BLOCKED, "High", error_msg, [], trace_id=trace_id)
        return {"order_id": order.id, "order_no": order.order_no, "status": order.status, "erp_success": False, "error": error_msg}
    except Exception as exc:
        error_msg = f"ERP 制单异常：{exc}"
        transition_order(session, order, OrderEvent.ERP_SAVE_FAILED, trace_id=trace_id,
                         detail={"step": "exception", "error": error_msg})
        create_exception_case(session, order, ExceptionType.OMS_BLOCKED, "High", error_msg, [], trace_id=trace_id)
        return {"order_id": order.id, "order_no": order.order_no, "status": order.status, "erp_success": False, "error": error_msg}


def retry_erp_billing(session: Session, order: MiddlePlatformOrder, *, trace_id: str = "") -> dict[str, Any]:
    """重试 ERP 制单（从 ERP_FAILED → ERP_PENDING → 重新制单）"""
    if order.status != OrderStatus.ERP_FAILED.value:
        return {"order_id": order.id, "error": f"当前状态不允许重试：{order.status}"}
    transition_order(session, order, OrderEvent.EXCEPTION_RESOLVED_AND_RE_ERP, trace_id=trace_id)
    return process_erp_billing(session, order, trace_id=trace_id)


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


def upsert_middle_platform_order(session: Session, crm_order: CrmSalesOrder) -> MiddlePlatformOrder:
    order = (
        session.query(MiddlePlatformOrder)
        .filter(MiddlePlatformOrder.source_system == crm_order.source_system, MiddlePlatformOrder.crm_order_id == crm_order.crm_order_id)
        .first()
    )
    if order is None:
        order = MiddlePlatformOrder(
            order_no=_generate_temp_order_no(crm_order),
            source_system=crm_order.source_system,
            crm_sales_order_id=crm_order.id,
            crm_order_id=crm_order.crm_order_id,
            crm_order_no=crm_order.crm_order_no,
            payload_hash=crm_order.payload_hash,
        )
        session.add(order)
        session.flush()
    order.crm_sales_order_id = crm_order.id
    order.crm_order_no = crm_order.crm_order_no
    order.payload_hash = crm_order.payload_hash
    raw = loads(crm_order.raw_json, {})
    order.source_policy = "CRM_ONLY"
    order.platform_order_no = raw_text_from_keys(raw, ["platform_order_no", "platformOrderNo", "trade_no", "tradeNo", "external_order_no", "externalOrderNo"])
    order.shop_code = raw_text_from_keys(raw, ["shop_code", "shopCode", "store_code", "storeCode"])
    order.channel_code = raw_text_from_keys(raw, ["channel_code", "channelCode", "platform", "channel"])
    order.fulfillment_type = normalized_fulfillment_type(raw)
    order.customer_name = crm_order.customer_name
    order.sales_user_name = crm_order.sales_user_name
    order.currency = crm_order.currency or "CNY"
    order.order_amount = parse_decimal(crm_order.order_amount)
    # 订单类型自动识别（FR-003）：优先取 CRM 原始类型，否则金额>0+有附件=销售，其余=备货
    order.order_type = infer_order_type(crm_order)
    # 主体编码：从 CRM 结算方式/客户渠道推断
    order.entity_code = _infer_entity_code(crm_order)
    order.fulfillment_entity = infer_fulfillment_entity(crm_order, order.entity_code)
    order.updated_at = now_utc()
    sync_middle_order_items(session, order, crm_order)
    session.flush()
    session.refresh(order, attribute_names=["items"])
    return order


def _infer_entity_code(crm_order: CrmSalesOrder) -> str:
    """从 CRM 订单数据推断主体编码

    优先通过 CRM 业务类型 (record_type) 进行精确映射；
    如果没有匹配，则通过归属部门、结算方式和币种进行推断兜底。
    """
    raw = loads(crm_order.raw_json, {})
    record_type = None
    if isinstance(raw, dict):
        if "detail_raw" in raw and isinstance(raw["detail_raw"], dict):
            val = raw["detail_raw"].get("Value", {})
            if isinstance(val, dict):
                data = val.get("data", {})
                if isinstance(data, dict):
                    record_type = data.get("record_type")
        if not record_type:
            record_type = raw.get("record_type")

    from sqlalchemy.orm import object_session
    from backend.app.models import CrmBusinessTypeMapping
    session = object_session(crm_order)
    if session and record_type:
        mapping = session.query(CrmBusinessTypeMapping).filter(
            CrmBusinessTypeMapping.business_type_code == record_type,
            CrmBusinessTypeMapping.is_active == True
        ).first()
        if mapping:
            return mapping.entity_code

    # CRM 业务类型 (record_type) -> 主体编码
    record_type_mapping = {
        "record_hnH91__c": "SZ",     # 深圳积木易搭科技技术有限公司
        "default__c": "SZ",          # 家e搭软件订单
        "record_s417r__c": "HK",     # 香港积木易搭订单
        "record_ltF03__c": "US",     # 美国积木易搭订单
        "record_a60d1__c": "WH_RX",  # 武汉睿数订单
        "record_3fYB1__c": "WH",     # 武汉尺子订单
        "record_UqY5M__c": "SZ_3D",  # 积木三维订单
        "record_ib3Iw__c": "GZ",     # 广州积木易搭订单
    }

    if record_type in record_type_mapping:
        return record_type_mapping[record_type]

    # 兜底规则 1: 部门名称包含睿数
    dept = (crm_order.owner_department or "").strip()
    if "睿数" in dept or "ruishu" in dept.lower():
        return "WH_RX"

    # 兜底规则 2: 海外币种/结算方式推断
    settlement = (crm_order.settlement_method or "").strip()
    currency = (crm_order.currency or "").strip()
    oversea_keywords = ("海外", "境外", "香港", "美元", "USD", "HKD", "欧元", "EUR", "export", "oversea", "international")
    if any(kw in settlement.lower() for kw in oversea_keywords if kw.isascii()) or \
       any(kw in settlement for kw in oversea_keywords if not kw.isascii()):
        return "HK"
    if currency in ("USD", "HKD", "EUR"):
        return "HK"
    return "SZ"


def infer_order_type(crm_order: CrmSalesOrder) -> str:
    raw = loads(crm_order.raw_json, {})
    raw_type = raw_text_from_keys(raw, ["order_type", "orderType", "business_type", "businessType", "订单类型", "业务类型"])
    if raw_type:
        normalized = raw_type.strip().lower()
        if normalized in {"sales_order", "sale", "sales", "销售订单"} or "销售" in raw_type:
            return "SALES_ORDER"
        if normalized in {"stock_replenishment", "stock", "replenishment", "备货订单"} or any(token in raw_type for token in ("备货", "补货")):
            return "STOCK_REPLENISHMENT"

    has_attachments = bool(crm_order.attachment_files_json and crm_order.attachment_files_json != "[]")
    amount = parse_decimal(crm_order.order_amount) or 0
    return "SALES_ORDER" if amount > 0 and has_attachments else "STOCK_REPLENISHMENT"


def infer_fulfillment_entity(crm_order: CrmSalesOrder, entity_code: str | None) -> str | None:
    raw = loads(crm_order.raw_json, {})
    return raw_text_from_keys(
        raw,
        [
            "fulfillment_entity",
            "fulfillmentEntity",
            "shipping_entity",
            "shippingEntity",
            "warehouse_entity",
            "warehouseEntity",
            "出货主体",
            "发货主体",
        ],
    ) or entity_code


def ensure_middle_order_business_fields(session: Session, order: MiddlePlatformOrder) -> bool:
    crm_order = order.crm_order or session.get(CrmSalesOrder, order.crm_sales_order_id)
    if crm_order is None:
        return False

    changed = False
    if not order.order_type:
        order.order_type = infer_order_type(crm_order)
        changed = True
    if not order.entity_code:
        order.entity_code = _infer_entity_code(crm_order)
        changed = True
    if not order.fulfillment_entity:
        order.fulfillment_entity = infer_fulfillment_entity(crm_order, order.entity_code)
        changed = True
    if changed:
        order.updated_at = now_utc()
        session.flush()
    return changed


def _generate_temp_order_no(crm_order: CrmSalesOrder) -> str:
    base = re.sub(r"[^A-Za-z0-9_-]+", "-", crm_order.crm_order_no or crm_order.crm_order_id).strip("-")[:42]
    if not base:
        base = hashlib.sha1(crm_order.id.encode("utf-8")).hexdigest()[:10]
    return f"MP-{base}"


def sync_middle_order_items(session: Session, order: MiddlePlatformOrder, crm_order: CrmSalesOrder) -> None:
    existing = list(order.items)
    for item in existing:
        session.delete(item)
    session.flush()
    source_items = list(crm_order.items)
    if not source_items:
        return
    raw = loads(crm_order.raw_json, {})
    source_payloads = []
    for source in source_items:
        source_raw = loads(source.raw_json, {})
        source_payloads.append(
            {
                **source_raw,
                "sku_code": source.sku_code,
                "product_name": source.product_name,
                "specification": source.specification,
                "quantity": source.quantity,
                "unit_price": source.unit_price,
                "line_amount": source.line_amount,
            }
        )
    for source in apportioned_order_item_payloads(raw, source_payloads):
        session.add(
            MiddlePlatformOrderItem(
                order_id=order.id,
                sku_code=standard_sku_code_for_item(session, order, source),
                product_name=source.get("product_name"),
                shop_sku_code=raw_text_from_keys(source, ["shop_sku_code", "shopSkuCode", "platform_sku", "platformSku", "seller_sku", "sellerSku"]),
                channel_code=order.channel_code,
                quantity=parse_decimal(source.get("quantity")),
                unit_price=parse_decimal(source.get("unit_price")),
                line_amount=parse_decimal(source.get("line_amount")),
                raw_json=dumps(source),
            )
        )


def apportioned_order_item_payloads(order_raw: dict[str, Any], items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    payloads = [dict(item) for item in items]
    if not payloads:
        return payloads
    total_discount = decimal_from_keys(order_raw, ["total_discount", "discount_amount", "promotion_discount", "order_discount_amount", "coupon_amount"]) or Decimal("0")
    shipping_fee = decimal_from_keys(order_raw, ["shipping_fee", "freight_amount", "postage", "logistics_fee"]) or Decimal("0")
    if total_discount == 0 and shipping_fee == 0:
        return payloads
    raw_amounts: list[Decimal] = []
    for item in payloads:
        quantity = parse_decimal(item.get("quantity") or item.get("qty")) or Decimal("0")
        unit_price = parse_decimal(item.get("unit_price")) or Decimal("0")
        line_amount = parse_decimal(item.get("line_amount") or item.get("amount")) or (quantity * unit_price).quantize(Decimal("0.01"))
        raw_amounts.append(line_amount)
    total_raw = sum(raw_amounts, Decimal("0")).quantize(Decimal("0.01"))
    if total_raw <= 0:
        return payloads
    total_paid = decimal_from_keys(order_raw, ["total_paid_amount", "paid_amount", "actual_amount", "payment_amount"])
    if total_paid is None:
        total_paid = (total_raw - total_discount + shipping_fee).quantize(Decimal("0.01"))
    allocated_sum = Decimal("0")
    for index, item in enumerate(payloads):
        if index == len(payloads) - 1:
            net_amount = (total_paid - allocated_sum).quantize(Decimal("0.01"))
        else:
            ratio = raw_amounts[index] / total_raw
            net_amount = (raw_amounts[index] - (total_discount * ratio) + (shipping_fee * ratio)).quantize(Decimal("0.01"))
            allocated_sum += net_amount
        apportionment = {
            "raw_line_amount": str(raw_amounts[index]),
            "total_raw_amount": str(total_raw),
            "total_discount": str(total_discount.quantize(Decimal("0.01"))),
            "shipping_fee": str(shipping_fee.quantize(Decimal("0.01"))),
            "total_paid_amount": str(total_paid),
            "net_line_amount": str(net_amount),
            "method": "proportional_with_last_line_correction",
        }
        item["line_amount"] = str(net_amount)
        item["apportionment"] = apportionment
    return payloads


def decimal_from_keys(data: dict[str, Any], keys: list[str]) -> Decimal | None:
    for key in keys:
        value = parse_decimal(data.get(key))
        if value is not None:
            return value
    return None


def raw_text_from_keys(data: dict[str, Any], keys: list[str]) -> str | None:
    for key in keys:
        value = str(data.get(key) or "").strip()
        if value:
            return value
    return None


def normalized_fulfillment_type(raw: dict[str, Any]) -> str | None:
    if is_platform_fulfilled_raw(raw):
        return "PLATFORM_FULFILLED"
    text = " ".join(
        str(raw.get(key) or "").strip().lower()
        for key in ["fulfillment_type", "fulfillmentType", "delivery_method", "deliveryMethod", "shipping_method", "shippingMethod", "logistics_mode", "logisticsMode"]
        if str(raw.get(key) or "").strip()
    )
    if any(token in text for token in ["fbm", "merchant fulfilled", "seller fulfilled", "商家自配送", "自配送", "自履约", "海外仓"]):
        return "MERCHANT_FULFILLED"
    return raw_text_from_keys(raw, ["fulfillment_type", "fulfillmentType"])


def standard_sku_code_for_item(session: Session, order: MiddlePlatformOrder, item: dict[str, Any]) -> str | None:
    direct = str(item.get("sku_code") or item.get("sku_id") or "").strip()
    if direct:
        return direct
    shop_sku = raw_text_from_keys(item, ["shop_sku_code", "shopSkuCode", "platform_sku", "platformSku", "seller_sku", "sellerSku"])
    if shop_sku:
        channel_candidates = [order.channel_code, order.shop_code, "default", None]
        for channel in channel_candidates:
            query = session.query(ChannelPricing).join(ProductSKU, ProductSKU.id == ChannelPricing.sku_uuid).filter(
                ChannelPricing.channel_sku_id == shop_sku,
                ProductSKU.status == "Active",
            )
            if channel:
                query = query.filter(ChannelPricing.channel == channel)
            row = query.order_by(ChannelPricing.updated_at.desc()).first()
            if row and row.sku and row.sku.sku_id:
                item["sku_mapping"] = {
                    "source": "channel_pricing",
                    "shop_sku_code": shop_sku,
                    "channel": row.channel,
                    "standard_sku_code": row.sku.sku_id,
                }
                return row.sku.sku_id
    product_name = raw_text_from_keys(item, ["product_name", "name", "productName", "产品名称", "商品名称"])
    if product_name:
        match = match_sku_by_product_name(session, product_name)
        item["sku_mapping"] = {
            "source": "product_name_semantic",
            "product_name": product_name,
            **match,
        }
        if match.get("matched") and match.get("sku_id"):
            return str(match["sku_id"])
    return shop_sku


def run_validation_chain(session: Session, order: MiddlePlatformOrder, rules: list[OrderValidationRule] | None = None) -> list[ValidationResult]:
    crm_order = session.get(CrmSalesOrder, order.crm_sales_order_id)
    if crm_order is None:
        raise RuntimeError("CRM order missing for validation")
    context = OrderContext(order=order, crm_order=crm_order, items=list(order.items), session=session)
    results: list[ValidationResult] = []
    for rule in rules or DEFAULT_RULES:
        if rules is None and not is_review_rule_enabled(session, rule.get_rule_code()):
            continue
        if not rule.supports(context):
            continue
        result = rule.validate(context)
        results.append(result)
    return results


def validation_blocker_summary(validation_results: list[ValidationResult]) -> str:
    failed = [result for result in validation_results if not result.passed]
    if not failed:
        return ""
    critical = [result for result in failed if result.blocker_level == BlockerLevel.CRITICAL]
    scoped = critical or failed
    parts = [f"{result.rule_code}：{result.reason}" for result in scoped if result.reason]
    return "；".join(parts) or scoped[0].reason


def is_platform_fulfilled_order(order: MiddlePlatformOrder, crm_order: CrmSalesOrder) -> bool:
    raw = loads(crm_order.raw_json, {})
    if order.fulfillment_type == "PLATFORM_FULFILLED":
        return True
    return is_platform_fulfilled_raw(raw)


def is_platform_fulfilled_raw(raw: dict[str, Any]) -> bool:
    values = [
        raw.get("fulfillment_type"),
        raw.get("fulfillmentType"),
        raw.get("delivery_method"),
        raw.get("deliveryMethod"),
        raw.get("shipping_method"),
        raw.get("shippingMethod"),
        raw.get("logistics_mode"),
        raw.get("logisticsMode"),
        raw.get("warehouse_mode"),
        raw.get("warehouseMode"),
        raw.get("channel_fulfillment_type"),
        raw.get("channelFulfillmentType"),
        raw.get("order_type"),
        raw.get("orderType"),
    ]
    text = " ".join(str(value or "").strip().lower() for value in values if str(value or "").strip())
    if not text:
        return False
    platform_tokens = [
        "platform_fulfilled",
        "platform fulfilled",
        "platform-fulfilled",
        "fba",
        "fulfillment by amazon",
        "amazon fulfillment",
        "平台履约",
        "平台自送",
        "平台配送",
        "亚马逊配送",
        "亚马逊物流",
    ]
    return any(token in text for token in platform_tokens)


def archive_platform_fulfilled_order(session: Session, order: MiddlePlatformOrder, crm_order: CrmSalesOrder, *, trace_id: str = "") -> None:
    raw = loads(crm_order.raw_json, {})
    evidence = {
        "fulfillment_type": raw.get("fulfillment_type") or raw.get("fulfillmentType"),
        "delivery_method": raw.get("delivery_method") or raw.get("deliveryMethod"),
        "shipping_method": raw.get("shipping_method") or raw.get("shippingMethod"),
        "logistics_mode": raw.get("logistics_mode") or raw.get("logisticsMode"),
        "warehouse_mode": raw.get("warehouse_mode") or raw.get("warehouseMode"),
        "channel_fulfillment_type": raw.get("channel_fulfillment_type") or raw.get("channelFulfillmentType"),
        "order_type": raw.get("order_type") or raw.get("orderType"),
    }
    transition_order(
        session,
        order,
        OrderEvent.ARCHIVE_PHASE1_FULFILLMENT,
        trace_id=trace_id,
        detail={
            "reason": "platform_fulfilled_skip_oms",
            "evidence": {key: value for key, value in evidence.items() if value not in (None, "")},
        },
    )
    summary = loads(order.validation_summary_json, {})
    summary["fulfillment"] = {
        "type": "PLATFORM_FULFILLED",
        "phase1_action": "SKIP_OMS_AND_ARCHIVE",
        "reason": "CRM 标识为平台履约/FBA/平台自送，一期不生成中台发货通知或下推 OMS。",
        "evidence": {key: value for key, value in evidence.items() if value not in (None, "")},
    }
    order.validation_summary_json = dumps(summary)
    session.add(
        AuditEvent(
            event_type="PlatformFulfilledOrderArchived",
            related_object_type="MiddlePlatformOrder",
            related_object_id=order.id,
            detail=dumps({"order_no": order.order_no, "crm_order_no": order.crm_order_no, "trace_id": trace_id}),
        )
    )


def latest_delivery_notice(session: Session, order: MiddlePlatformOrder) -> DeliveryNotice | None:
    return (
        session.query(DeliveryNotice)
        .filter(DeliveryNotice.order_id == order.id)
        .order_by(DeliveryNotice.created_at.desc())
        .first()
    )


def create_delivery_notice(session: Session, order: MiddlePlatformOrder) -> DeliveryNotice:
    existing = latest_delivery_notice(session, order)
    if existing is not None:
        return existing
    notice_no = f"DN-{order.order_no.removeprefix('MP-')}"
    split_preview = build_delivery_split_preview(session, order, notice_no)
    payload = build_jackyun_delivery_payload(session, order, notice_no, split_preview)

    # 多仓库路由：优先用地址自动推荐，其次用全局默认
    from backend.app.services.rules.warehouse_routing import warehouse_routing as route_warehouse
    routed_wh = route_warehouse(
        session,
        receipt_address=order.crm_order.receipt_address or "",
        channel_code=order.channel_code or "",
        shop_code=order.shop_code or "",
    )
    warehouse_code = routed_wh or config_value(session, "oms_warehouse_code", "").strip() or None

    notice = DeliveryNotice(
        notice_no=notice_no,
        order_id=order.id,
        notice_version=1,
        source_snapshot_hash=order.payload_hash,
        status="Previewed",
        oms_idempotency_key=hashlib.sha256(f"{order.order_no}:{order.payload_hash}".encode("utf-8")).hexdigest(),
        oms_method=config_value(session, "oms_create_order_method", "wms.order.create") or "wms.order.create",
        owner_code=config_value(session, "oms_owner_code", "").strip() or None,
        warehouse_code=warehouse_code,
        shop_code=config_value(session, "oms_shop_code", "").strip() or None,
        logistic_code=config_value(session, "oms_logistic_code", "").strip() or None,
        split_preview_json=dumps(split_preview),
        payload_json=dumps(payload),
    )
    session.add(notice)
    session.flush()
    return notice


def build_delivery_split_preview(session: Session, order: MiddlePlatformOrder, notice_no: str) -> dict[str, Any]:
    warehouse_code = config_value(session, "oms_warehouse_code", "").strip()
    warehouse_name = warehouse_code or "未配置仓库"
    groups = [
        {
            "group_no": f"{notice_no}-G1",
            "warehouse_code": warehouse_code,
            "warehouse_name": warehouse_name,
            "items": [
                {
                    "sku_code": item.sku_code,
                    "product_name": item.product_name,
                    "quantity": str(item.quantity) if item.quantity is not None else None,
                    "available_quantity": str(inventory_available_quantity(session, item.sku_code or "") or "") if item.sku_code else "",
                    "line_amount": str(item.line_amount) if item.line_amount is not None else None,
                }
                for item in order.items
            ],
        }
    ]
    return {
        "strategy": "single_warehouse_default",
        "requires_confirmation": True,
        "groups": groups,
        "warnings": delivery_preview_warnings(session, order),
    }


def delivery_preview_warnings(session: Session, order: MiddlePlatformOrder) -> list[str]:
    warnings: list[str] = []
    required_config = {
        "oms_owner_code": "货主CODE",
        "oms_warehouse_code": "仓库CODE",
        "oms_shop_code": "店铺CODE",
        "oms_logistic_code": "物流方式编码",
    }
    for key, label in required_config.items():
        if not config_value(session, key, "").strip():
            warnings.append(f"未配置{label}，真实下推前必须补齐")
    for item in order.items:
        if not item.sku_code:
            warnings.append(f"明细 {item.product_name or item.id} 未映射 SKU")
    recommended = {"delivery_date": "期望交期"}
    for field, label in recommended.items():
        if not str(getattr(order.crm_order, field, "") or "").strip():
            warnings.append(f"{label}未识别，将尽量从 CRM 附件或 LLM 兜底补全")
    return warnings


def validate_delivery_notice_for_oms(session: Session, notice: DeliveryNotice) -> list[str]:
    payload = loads(notice.payload_json, {})
    order_payload = payload.get("order") if isinstance(payload, dict) else {}
    if not isinstance(order_payload, dict):
        order_payload = {}
    required = [
        ("ownerCode", "货主CODE"),
        ("warehouseCode", "仓库CODE"),
        ("shopCode", "店铺CODE"),
        ("logisticCode", "物流方式编码"),
        ("erporderNo", "外部订单号"),
    ]
    missing = [label for field, label in required if not str(order_payload.get(field) or "").strip()]
    details = payload.get("orderDetailList") if isinstance(payload, dict) else []
    if not isinstance(details, list) or not details:
        missing.append("订单明细")
    else:
        for index, item in enumerate(details, start=1):
            if not isinstance(item, dict):
                missing.append(f"第 {index} 行明细")
                continue
            if not str(item.get("skuId") or "").strip():
                missing.append(f"第 {index} 行 SKU")
            try:
                quantity = Decimal(str(item.get("sellCount") or "0"))
            except InvalidOperation:
                quantity = Decimal("0")
            if quantity <= 0:
                missing.append(f"第 {index} 行数量")
    order_info = payload.get("orderInfo") if isinstance(payload, dict) else None
    if not isinstance(order_info, dict):
        missing.append("orderInfo")
    else:
        required_order_info = [
            ("receiverName", "收货人"),
            ("receiverAddress", "收货地址"),
            ("receiverMobile", "联系方式电话"),
        ]
        missing.extend(label for field, label in required_order_info if not str(order_info.get(field) or "").strip())
        if str(order_info.get("receiverAddress") or "").strip() and not is_detailed_receipt_address(order_info.get("receiverAddress")):
            missing.append("可邮寄详细收货地址")
    return missing


def build_jackyun_delivery_payload(session: Session, order: MiddlePlatformOrder, notice_no: str, split_preview: dict[str, Any]) -> dict[str, Any]:
    items = []
    for item in order.items:
        items.append(
            {
                "skuId": item.sku_code,
                "sellCount": str(item.quantity) if item.quantity is not None else "0",
                "goodsName": item.product_name or "",
            }
        )
    raw = loads(order.crm_order.raw_json, {})
    receiver_phone = raw.get("receipt_phone") or order.crm_order.receipt_phone or ""
    order_info = {
        "receiverName": raw.get("receipt_contact") or order.crm_order.receipt_contact or "",
        "receiverMobile": receiver_phone,
        "receiverPhone": receiver_phone,
        "receiverAddress": raw.get("receipt_address") or order.crm_order.receipt_address or "",
        "deliveryDate": raw.get("delivery_date") or order.crm_order.delivery_date or "",
        "buyerMemo": f"CRM订单 {order.crm_order_no}",
    }
    return {
        "order": {
            "ownerCode": config_value(session, "oms_owner_code", "").strip(),
            "warehouseCode": config_value(session, "oms_warehouse_code", "").strip(),
            "shopCode": config_value(session, "oms_shop_code", "").strip(),
            "erporderNo": notice_no,
            "orderType": config_value(session, "oms_order_type", "201").strip() or "201",
            "logisticCode": config_value(session, "oms_logistic_code", "").strip(),
            "remark": f"中台订单 {order.order_no}",
        },
        "orderDetailList": items,
        "orderInfo": order_info,
        "splitPreview": split_preview,
    }


def confirm_delivery_notice(session: Session, notice: DeliveryNotice, *, confirmed_by: str = "operator", trace_id: str = "") -> ProcessingJob:
    order = session.get(MiddlePlatformOrder, notice.order_id)
    if order is None:
        raise RuntimeError("middle platform order not found")
    if order.status not in {OrderStatus.DELIVERY_NOTICE_READY.value, OrderStatus.OMS_BLOCKED.value}:
        raise IllegalStateTransition(f"当前状态不允许确认发货单：{order.status}")
    if notice.status not in {"Previewed", "Blocked", "Retrying", "Created"}:
        raise RuntimeError(f"发货通知当前状态不允许确认：{notice.status}")
    missing = validate_delivery_notice_for_oms(session, notice)
    if missing:
        notice.status = "Blocked"
        notice.last_error = "OMS 下推必填字段缺失：" + "、".join(missing)
        notice.updated_at = now_utc()
        create_exception_case(
            session,
            order,
            ExceptionType.OMS_REQUIRED_FIELDS_MISSING,
            "Critical",
            notice.last_error,
            [],
            trace_id=trace_id or notice.oms_idempotency_key,
        )
        raise RuntimeError(notice.last_error)
    notice.status = "Confirmed"
    notice.confirmed_by = confirmed_by
    notice.confirmed_at = now_utc()
    notice.version += 1
    notice.updated_at = now_utc()
    job = enqueue_oms_push(session, notice)
    event = OrderEvent.ENQUEUE_OMS_PUSH if order.status == OrderStatus.DELIVERY_NOTICE_READY.value else OrderEvent.EXCEPTION_RESOLVED_AND_REPLAY
    transition_order(session, order, event, trace_id=trace_id, detail={"notice_no": notice.notice_no, "confirmed_by": confirmed_by})
    return job


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


def platform_fulfillment_required(order: MiddlePlatformOrder) -> bool:
    if not order.platform_order_no:
        return False
    fulfillment_type = str(order.fulfillment_type or "").strip().upper()
    if fulfillment_type == "PLATFORM_FULFILLED":
        return False
    return bool(order.channel_code or order.shop_code)


def enqueue_platform_fulfillment_sync(session: Session, order: MiddlePlatformOrder, notice: DeliveryNotice, *, trace_id: str = "") -> ProcessingJob | None:
    if not platform_fulfillment_required(order):
        notice.platform_fulfillment_status = "NotRequired"
        return None
    if not notice.waybill_no:
        return None
    if notice.platform_fulfillment_status == "Synced" and notice.platform_fulfillment_synced_waybill_no == notice.waybill_no:
        return None
    if notice.platform_fulfillment_status in {"Pending", "Running"}:
        return None
    payload = {
        "notice_id": notice.id,
        "order_id": order.id,
        "platform_order_no": order.platform_order_no,
        "waybill_no": notice.waybill_no,
        "trace_id": trace_id,
        "retry_count": notice.platform_fulfillment_retry_count,
    }
    existing = (
        session.query(ProcessingJob)
        .filter(
            ProcessingJob.job_type == "PLATFORM_FULFILLMENT_SYNC",
            ProcessingJob.payload_json == dumps(payload),
            ProcessingJob.status.in_(["Pending", "Running"]),
        )
        .first()
    )
    if existing is not None:
        return existing
    notice.platform_fulfillment_status = "Pending"
    notice.platform_fulfillment_error = None
    notice.updated_at = now_utc()
    job = ProcessingJob(job_type="PLATFORM_FULFILLMENT_SYNC", payload_json=dumps(payload), status="Pending")
    session.add(job)
    session.add(AuditEvent(event_type="PlatformFulfillmentSyncQueued", related_object_type="DeliveryNotice", related_object_id=notice.id, detail=dumps(payload)))
    record_integration_event(session, source_system=str(order.channel_code or "PLATFORM").upper(), event_type="PLATFORM_FULFILLMENT_SYNC", biz_key=str(order.platform_order_no or notice.notice_no), payload=payload, trace_id=trace_id, status="Pending", retry_count=notice.platform_fulfillment_retry_count)
    return job


def process_platform_fulfillment_sync(session: Session, payload: dict[str, Any]) -> dict[str, Any]:
    notice = session.get(DeliveryNotice, payload.get("notice_id"))
    if notice is None:
        raise RuntimeError("delivery notice not found")
    order = session.get(MiddlePlatformOrder, notice.order_id)
    if order is None:
        raise RuntimeError("middle platform order not found")
    if not platform_fulfillment_required(order):
        notice.platform_fulfillment_status = "NotRequired"
        notice.platform_fulfillment_error = None
        return {"notice_id": notice.id, "order_id": order.id, "skipped": True, "reason": "platform_fulfillment_not_required"}
    if not notice.waybill_no:
        raise RuntimeError("运单号缺失，不允许回传平台履约")
    if notice.platform_fulfillment_status == "Synced" and notice.platform_fulfillment_synced_waybill_no == notice.waybill_no:
        return {"notice_id": notice.id, "order_id": order.id, "skipped": True, "reason": "already_synced", "waybill_no": notice.waybill_no}
    notice.platform_fulfillment_status = "Running"
    notice.updated_at = now_utc()
    try:
        result = push_platform_fulfillment(session, order, notice)
    except Exception as exc:
        return handle_platform_fulfillment_sync_failure(session, notice, order, str(exc), trace_id=str(payload.get("trace_id") or ""))
    notice.platform_fulfillment_status = "Synced"
    notice.platform_fulfillment_error = None
    notice.platform_fulfillment_retry_count = 0
    notice.platform_fulfillment_synced_at = now_utc()
    notice.platform_fulfillment_synced_waybill_no = notice.waybill_no
    notice.updated_at = now_utc()
    session.add(
        AuditEvent(
            event_type="PlatformFulfillmentSynced",
            related_object_type="DeliveryNotice",
            related_object_id=notice.id,
            detail=dumps(
                {
                    "platform_order_no": order.platform_order_no,
                    "shop_code": order.shop_code,
                    "channel_code": order.channel_code,
                    "waybill_no": notice.waybill_no,
                    "result": result,
                }
            ),
        )
    )
    record_integration_event(
        session,
        source_system=str(order.channel_code or "PLATFORM").upper(),
        event_type="PLATFORM_FULFILLMENT_SYNC",
        biz_key=str(order.platform_order_no or notice.notice_no),
        payload=payload,
        trace_id=str(payload.get("trace_id") or ""),
        status="Succeeded",
        retry_count=notice.platform_fulfillment_retry_count,
        response=result,
    )
    return {"notice_id": notice.id, "order_id": order.id, "waybill_no": notice.waybill_no, "platform_result": result}


def push_platform_fulfillment(session: Session, order: MiddlePlatformOrder, notice: DeliveryNotice) -> dict[str, Any]:
    payload = {
        "platform_order_no": order.platform_order_no,
        "shop_code": order.shop_code,
        "channel_code": order.channel_code,
        "waybill_no": notice.waybill_no,
        "carrier": notice.logistic_code,
        "notice_no": notice.notice_no,
        "oms_order_no": notice.oms_order_no,
    }
    if config_bool(session, "platform_fulfillment_mock_success", True):
        return {"ok": True, "mode": "mock", "payload": payload}
    webhook_url = config_value(session, "platform_fulfillment_webhook_url", "").strip()
    if not webhook_url:
        raise RuntimeError("平台履约回传未配置 webhook 或渠道适配器")
    raise RuntimeError("平台履约回传 webhook adapter 未启用，请配置具体渠道适配器")


def handle_platform_fulfillment_sync_failure(session: Session, notice: DeliveryNotice, order: MiddlePlatformOrder, message: str, *, trace_id: str = "") -> dict[str, Any]:
    notice.platform_fulfillment_retry_count += 1
    notice.platform_fulfillment_error = message
    notice.updated_at = now_utc()
    max_retries = max(1, config_int(session, "platform_fulfillment_sync_max_retries", 3))
    if notice.platform_fulfillment_retry_count >= max_retries:
        import logging
        logger = logging.getLogger(__name__)
        logger.error(f"[DLQ_ALERT] Platform fulfillment sync failed permanently for order {order.order_no} / platform_order_no {order.platform_order_no} after {max_retries} retries. Error: {message}")
        notice.platform_fulfillment_status = "Blocked"
        create_exception_case(session, order, ExceptionType.OMS_STATUS_CONFLICT, "High", f"运单号回传平台失败：{message}", [], trace_id=trace_id or notice.id)
        session.add(
            AuditEvent(
                event_type="PlatformFulfillmentSyncBlocked",
                related_object_type="DeliveryNotice",
                related_object_id=notice.id,
                detail=dumps({"error": message, "retry_count": notice.platform_fulfillment_retry_count, "waybill_no": notice.waybill_no}),
            )
        )
        record_integration_event(
            session,
            source_system=str(order.channel_code or "PLATFORM").upper(),
            event_type="PLATFORM_FULFILLMENT_SYNC",
            biz_key=str(order.platform_order_no or notice.notice_no),
            payload={"notice_id": notice.id, "order_id": order.id, "platform_order_no": order.platform_order_no, "waybill_no": notice.waybill_no},
            trace_id=trace_id,
            status="Dead",
            retry_count=notice.platform_fulfillment_retry_count,
            error_message=message,
        )
        return {"notice_id": notice.id, "order_id": order.id, "blocked": True, "error": message}
    notice.platform_fulfillment_status = "Retrying"
    next_retry_at = calculate_next_retry_at(session, notice.platform_fulfillment_retry_count)
    payload = {
        "notice_id": notice.id,
        "order_id": order.id,
        "platform_order_no": order.platform_order_no,
        "waybill_no": notice.waybill_no,
        "trace_id": trace_id,
        "retry_count": notice.platform_fulfillment_retry_count,
    }
    session.add(ProcessingJob(job_type="PLATFORM_FULFILLMENT_SYNC", payload_json=dumps(payload), status="Pending", next_retry_at=next_retry_at))
    session.add(
        AuditEvent(
            event_type="PlatformFulfillmentSyncRetryQueued",
            related_object_type="DeliveryNotice",
            related_object_id=notice.id,
            detail=dumps({"error": message, "retry_count": notice.platform_fulfillment_retry_count, "next_retry_at": next_retry_at.isoformat()}),
        )
    )
    record_integration_event(
        session,
        source_system=str(order.channel_code or "PLATFORM").upper(),
        event_type="PLATFORM_FULFILLMENT_SYNC",
        biz_key=str(order.platform_order_no or notice.notice_no),
        payload=payload,
        trace_id=trace_id,
        status="Retrying",
        retry_count=notice.platform_fulfillment_retry_count,
        error_message=message,
    )
    return {"notice_id": notice.id, "order_id": order.id, "retrying": True, "next_retry_at": next_retry_at.isoformat(), "error": message}


def print_oms_waybill(session: Session, notice: DeliveryNotice) -> dict[str, Any]:
    payload = {
        "deliveryNo": notice.oms_order_no or notice.notice_no,
        "printType": "pdf",
        "needLabelCode": True,
    }
    if not config_bool(session, "oms_enabled", False) or config_bool(session, "oms_mock_success", True):
        return {"ok": True, "mode": "mock", "data": {"waybillNo": f"MOCK-{notice.notice_no}", "printData": ""}, "raw": {"request": payload}}
    client = jackyun_client_from_session(session)
    result = client.print_delivery_label(payload)
    if not result.get("ok"):
        raise RuntimeError(result.get("message") or dumps(result.get("raw", result)))
    return result


def extract_waybill_no(result: dict[str, Any]) -> str:
    for container in [result.get("data"), result.get("raw"), result]:
        if not isinstance(container, dict):
            continue
        for key in ("waybillNo", "waybill_no", "waybill", "trackingNo", "tracking_no", "logisticsNo"):
            value = str(container.get(key) or "").strip()
            if value:
                return value
    return ""


def extract_print_data(result: dict[str, Any]) -> str:
    for container in [result.get("data"), result.get("raw"), result]:
        if not isinstance(container, dict):
            continue
        for key in ("printData", "print_data", "pdf", "pdfBase64", "labelData", "url"):
            value = str(container.get(key) or "").strip()
            if value:
                return value
    return ""


def save_waybill_outbound_proof(session: Session, order: MiddlePlatformOrder, notice: DeliveryNotice, result: dict[str, Any]) -> OrderAttachment | None:
    crm_order = order.crm_order
    waybill_no = extract_waybill_no(result)
    print_data = extract_print_data(result)
    fingerprint = hashlib.sha256(f"OutboundProof|{notice.id}|{waybill_no}".encode("utf-8")).hexdigest()
    existing = (
        session.query(OrderAttachment)
        .filter(
            OrderAttachment.source_system == order.source_system,
            OrderAttachment.crm_order_id == order.crm_order_id,
            OrderAttachment.payload_hash == order.payload_hash,
            OrderAttachment.fingerprint == fingerprint,
        )
        .first()
    )
    if existing is not None:
        return existing
    evidence: dict[str, Any] = {
        "source": "wms-cross.delivery.print",
        "notice_id": notice.id,
        "notice_no": notice.notice_no,
        "oms_order_no": notice.oms_order_no,
        "waybill_no": waybill_no,
    }
    file_url = None
    file_name = f"{notice.notice_no}-waybill.pdf"
    if print_data.startswith(("http://", "https://")):
        file_url = print_data
        file_name = f"{notice.notice_no}-waybill.url"
        evidence["external_url"] = print_data
    elif print_data:
        try:
            content = base64.b64decode(print_data, validate=True)
            storage_ref, digest = save_attachment(file_name, content)
            evidence["local_storage_ref"] = storage_ref
            evidence["local_file_hash"] = digest
            evidence["local_file_size"] = len(content)
        except Exception as exc:
            evidence["print_data_unstored"] = {"reason": str(exc)[:500], "sha256": hashlib.sha256(print_data.encode("utf-8")).hexdigest()}
    attachment = OrderAttachment(
        crm_sales_order_id=crm_order.id if crm_order else None,
        source_system=order.source_system,
        crm_order_id=order.crm_order_id,
        crm_order_no=order.crm_order_no,
        payload_hash=order.payload_hash,
        attachment_type="OutboundProof",
        file_name=file_name,
        file_url=file_url,
        source_file_id=notice.id,
        fingerprint=fingerprint,
        parse_status="Registered",
        evidence_json=dumps(evidence),
        raw_json=dumps({"waybill_no": waybill_no, "print_result": result}),
    )
    session.add(attachment)
    session.flush()
    return attachment


def handle_oms_waybill_print_failure(session: Session, notice: DeliveryNotice, order: MiddlePlatformOrder, message: str, *, trace_id: str = "") -> dict[str, Any]:
    notice.print_retry_count += 1
    notice.print_error = message
    notice.updated_at = now_utc()
    max_retries = max(1, config_int(session, "oms_waybill_print_max_retries", 3))
    if notice.print_retry_count >= max_retries:
        notice.print_status = "Blocked"
        create_exception_case(session, order, ExceptionType.OMS_STATUS_CONFLICT, "High", f"跨境面单打印失败：{message}", [], trace_id=trace_id or notice.id)
        session.add(AuditEvent(event_type="OmsWaybillPrintBlocked", related_object_type="DeliveryNotice", related_object_id=notice.id, detail=dumps({"error": message, "retry_count": notice.print_retry_count})))
        record_integration_event(
            session,
            source_system="OMS",
            event_type="OMS_WAYBILL_PRINT",
            biz_key=notice.notice_no,
            payload={"notice_id": notice.id, "order_id": order.id, "trace_id": trace_id},
            trace_id=trace_id,
            status="Dead",
            retry_count=notice.print_retry_count,
            error_message=message,
        )
        return {"notice_id": notice.id, "order_id": order.id, "blocked": True, "error": message}
    notice.print_status = "Retrying"
    next_retry_at = calculate_next_retry_at(session, notice.print_retry_count)
    payload = {"notice_id": notice.id, "order_id": order.id, "trace_id": trace_id, "retry_count": notice.print_retry_count}
    session.add(ProcessingJob(job_type="OMS_WAYBILL_PRINT", payload_json=dumps(payload), status="Pending", next_retry_at=next_retry_at))
    session.add(AuditEvent(event_type="OmsWaybillPrintRetryQueued", related_object_type="DeliveryNotice", related_object_id=notice.id, detail=dumps({"error": message, "retry_count": notice.print_retry_count, "next_retry_at": next_retry_at.isoformat()})))
    record_integration_event(session, source_system="OMS", event_type="OMS_WAYBILL_PRINT", biz_key=notice.notice_no, payload=payload, trace_id=trace_id, status="Retrying", retry_count=notice.print_retry_count, error_message=message)
    return {"notice_id": notice.id, "order_id": order.id, "retrying": True, "next_retry_at": next_retry_at.isoformat(), "error": message}


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


def create_exception_case(
    session: Session,
    order: MiddlePlatformOrder,
    exception_type: ExceptionType,
    severity: str,
    reason: str,
    validation_results: list[ValidationResult],
    *,
    trace_id: str = "",
) -> ExceptionCase:
    context_pack = build_context_pack(session, order, exception_type, severity, reason, validation_results, trace_id=trace_id)
    existing = (
        session.query(ExceptionCase)
        .filter(ExceptionCase.exception_type == exception_type, ExceptionCase.status == "Open", ExceptionCase.detail.ilike(f"%{order.order_no}%"))
        .first()
    )
    if existing is not None:
        existing.severity = severity
        existing.detail = dumps(context_pack)
        existing.due_at = existing.due_at or exception_due_at(severity)
        existing.updated_at = now_utc()
        return existing
    case = ExceptionCase(exception_type=exception_type, severity=severity, detail=dumps(context_pack), status="Open", due_at=exception_due_at(severity))
    session.add(case)
    session.flush()
    enqueue_exception_diagnosis(session, case, source="order-middle-platform")
    session.add(AuditEvent(event_type="ExceptionCaseCreated", related_object_type="MiddlePlatformOrder", related_object_id=order.id, detail=dumps({"exception_type": exception_type.value, "trace_id": trace_id})))
    return case


def exception_due_at(severity: str) -> datetime:
    hours_by_severity = {
        "Critical": 4,
        "High": 24,
        "Medium": 72,
        "Low": 168,
    }
    return now_utc() + timedelta(hours=hours_by_severity.get(str(severity or "Medium"), 72))


def enqueue_validation_failure_notification(
    session: Session,
    order: MiddlePlatformOrder,
    validation_results: list[ValidationResult],
    exception_case: ExceptionCase,
    *,
    trace_id: str = "",
) -> OutboundMailJob | None:
    if not config_bool(session, "v2_validation_failure_notification_enabled", True):
        return None
    to_addresses, cc_addresses = validation_failure_recipients_for_order(session, order)
    if not to_addresses:
        session.add(
            AuditEvent(
                event_type="ValidationFailureNotificationSkipped",
                related_object_type="MiddlePlatformOrder",
                related_object_id=order.id,
                detail=dumps({"reason": "missing_recipients", "trace_id": trace_id}),
            )
        )
        return None

    failed = [result for result in validation_results if not result.passed]
    digest_source = "|".join(
        [
            order.order_no,
            order.crm_order_no or "",
            ";".join(f"{result.rule_code}:{result.reason}" for result in failed),
        ]
    )
    idempotency_key = f"v2-validation-failed:{hashlib.sha256(digest_source.encode('utf-8')).hexdigest()}"
    subject = f"[订单预审未通过][{order.crm_order_no or order.order_no}] {order.customer_name or ''}".strip()
    body = build_validation_failure_mail_body(session, order, failed, exception_case)
    existing = session.query(OutboundMailJob).filter(OutboundMailJob.idempotency_key == idempotency_key).first()
    if existing is not None:
        cancel_stale_pending_validation_failure_notifications(session, order, keep_idempotency_key=idempotency_key, trace_id=trace_id)
        if existing.status in {"Cancelled", "Failed"}:
            previous_status = existing.status
            existing.to_json = dumps(to_addresses)
            existing.cc_json = dumps(cc_addresses)
            existing.subject = subject
            existing.body = body
            existing.status = "Pending"
            existing.attempt_count = 0
            existing.next_retry_at = None
            existing.last_error = None
            existing.locked_by = None
            existing.locked_until = None
            existing.sending_started_at = None
            existing.sent_at = None
            existing.priority = 20
            session.add(
                AuditEvent(
                    event_type="ValidationFailureNotificationRequeued",
                    related_object_type="MiddlePlatformOrder",
                    related_object_id=order.id,
                    detail=dumps({"to": to_addresses, "cc": cc_addresses, "exception_case_id": exception_case.id, "trace_id": trace_id, "previous_status": previous_status}),
                )
            )
        return existing

    cancel_stale_pending_validation_failure_notifications(session, order, keep_idempotency_key=idempotency_key, trace_id=trace_id)
    job = OutboundMailJob(
        mail_type="V2ValidationFailed",
        to_json=dumps(to_addresses),
        cc_json=dumps(cc_addresses),
        subject=subject,
        body=body,
        idempotency_key=idempotency_key,
        status="Pending",
        priority=20,
    )
    session.add(job)
    session.add(
        AuditEvent(
            event_type="ValidationFailureNotificationQueued",
            related_object_type="MiddlePlatformOrder",
            related_object_id=order.id,
            detail=dumps({"to": to_addresses, "cc": cc_addresses, "exception_case_id": exception_case.id, "trace_id": trace_id}),
        )
    )
    return job


def cancel_stale_pending_validation_failure_notifications(
    session: Session,
    order: MiddlePlatformOrder,
    *,
    keep_idempotency_key: str,
    trace_id: str = "",
) -> int:
    subject_prefix = f"[订单预审未通过][{order.crm_order_no or order.order_no}]"
    stale_jobs = (
        session.query(OutboundMailJob)
        .filter(
            OutboundMailJob.mail_type == "V2ValidationFailed",
            OutboundMailJob.status == "Pending",
            OutboundMailJob.subject.ilike(f"{subject_prefix}%"),
            OutboundMailJob.idempotency_key != keep_idempotency_key,
        )
        .all()
    )
    for job in stale_jobs:
        job.status = "Cancelled"
        job.last_error = "superseded by newer validation failure notification"
    if stale_jobs:
        session.add(
            AuditEvent(
                event_type="ValidationFailureNotificationSuperseded",
                related_object_type="MiddlePlatformOrder",
                related_object_id=order.id,
                detail=dumps({"cancelled_count": len(stale_jobs), "trace_id": trace_id}),
            )
        )
    return len(stale_jobs)


def validation_failure_recipients(session: Session) -> tuple[list[str], list[str]]:
    configured_to = config_list(session, "v2_validation_failure_to_json", [])
    configured_cc = config_list(session, "v2_validation_failure_cc_json", [])
    ops = config_value(session, "ops_cc_email", "").strip()
    ceo = config_value(session, "ceo_email", "").strip()
    to_addresses = configured_to or ([ops] if ops else ([ceo] if ceo else []))
    cc_addresses = configured_cc or ([ceo] if ceo and ceo not in to_addresses else [])
    return unique_emails(to_addresses), unique_emails(cc_addresses)


def validation_failure_recipients_for_order(session: Session, order: MiddlePlatformOrder) -> tuple[list[str], list[str]]:
    sales_email = ""
    if order.crm_order is not None:
        sales_email = str(order.crm_order.sales_user_email or "").strip()
    if not sales_email:
        sales_email = str(getattr(order, "sales_user_email", "") or "").strip()
    sales_to = unique_emails([sales_email])
    if not sales_to:
        system_owner_to = unique_emails([config_value(session, "crm_system_owner_email", "").strip()])
        if system_owner_to:
            return system_owner_to, []
        configured_to, configured_cc = validation_failure_recipients(session)
        return configured_to, configured_cc
    return sales_to, []


def exception_notify_recipients(session: Session, exception_type: ExceptionType) -> tuple[list[str], list[str]]:
    """按异常类型查找通知邮箱配置。
    优先级: 类型专属(v2_exception_{type}_to_json) > 大类(v2_exception_crm/oms_to_json) > 通用兜底
    """
    type_key = exception_type.value.lower()
    to_list = config_list(session, f"v2_exception_{type_key}_to_json", [])
    cc_list = config_list(session, f"v2_exception_{type_key}_cc_json", [])
    if to_list or cc_list:
        return unique_emails(to_list), unique_emails(cc_list)
    # 大类回退
    if exception_type.value.startswith("CRM_"):
        to_list = config_list(session, "v2_exception_crm_to_json", [])
        cc_list = config_list(session, "v2_exception_crm_cc_json", [])
    elif exception_type.value.startswith("OMS_"):
        to_list = config_list(session, "v2_exception_oms_to_json", [])
        cc_list = config_list(session, "v2_exception_oms_cc_json", [])
    if to_list or cc_list:
        return unique_emails(to_list), unique_emails(cc_list)
    return validation_failure_recipients(session)


def unique_emails(values: list[str]) -> list[str]:
    seen: set[str] = set()
    emails: list[str] = []
    for value in values:
        text = str(value or "").strip()
        if not text or "@" not in text:
            continue
        key = text.lower()
        if key in seen:
            continue
        seen.add(key)
        emails.append(text)
    return emails


def validation_evidence_summary(session: Session, order: MiddlePlatformOrder) -> list[str]:
    attachments = (
        session.query(OrderAttachment)
        .filter(
            OrderAttachment.crm_order_id == order.crm_order_id,
            OrderAttachment.payload_hash == order.payload_hash,
        )
        .order_by(OrderAttachment.created_at)
        .all()
    )
    summary = [f"CRM 详情快照：payload_hash={order.payload_hash}"]
    if not attachments:
        summary.append("订单附件：未登记到附件记录")
        return summary
    for item in attachments[:10]:
        evidence = loads(item.evidence_json, {})
        source = evidence.get("source") or "crm_order_detail"
        attachment_type = f" / {item.attachment_type}" if item.attachment_type else ""
        summary.append(f"附件：{item.file_name}{attachment_type}，来源：{source}")
    if len(attachments) > 10:
        summary.append(f"其余附件：{len(attachments) - 10} 个")
    return summary


def classify_validation_missing_materials(failed: list[ValidationResult]) -> list[str]:
    materials: list[str] = []
    for result in failed:
        refs = result.evidence_refs or []
        text = " ".join([result.rule_code, result.reason, *refs])
        if "客户" in text or result.rule_code == "CUSTOMER_MAPPING":
            materials.append("客户资料/客户主数据映射")
        if "附件" in text or "合同" in text or "采购" in text or "PO" in text:
            materials.append("合同、客户 PO、盖章件等关键附件")
        if "收货" in text or "交期" in text:
            materials.append("收货信息与期望交期")
        if "SKU" in text or "商品" in text or "明细" in text:
            materials.append("商品明细、SKU 主数据或数量")
        if "金额" in text or "应收" in text or "已收" in text:
            materials.append("订单金额、商品金额与收款信息")
        if "库存" in text:
            materials.append("库存快照")
    deduped: list[str] = []
    for material in materials:
        if material not in deduped:
            deduped.append(material)
    return deduped or ["CRM 订单基础资料"]


def build_validation_failure_mail_body(
    session: Session,
    order: MiddlePlatformOrder,
    failed: list[ValidationResult],
    exception_case: ExceptionCase,
) -> str:
    crm = order.crm_order
    missing_materials = classify_validation_missing_materials(failed)
    evidence_summary = validation_evidence_summary(session, order)
    lines = [
        "相关同事好，",
        "",
        "CRM 同步发现新订单，但一期完整预审未通过，流程已中断，暂不会生成发货通知或下推 OMS。",
        "",
        f"中台订单号：{order.order_no}",
        f"CRM 订单号：{order.crm_order_no or ''}",
        f"客户名称：{order.customer_name or ''}",
        f"销售负责人：{order.sales_user_name or crm.sales_user_name or ''}",
        f"订单金额：{order.order_amount or ''} {order.currency or ''}".strip(),
        f"异常编号：{exception_case.id}",
        f"预审时间：{format_beijing_time(exception_case.created_at or now_utc(), include_seconds=True)}（北京时间）",
        "",
        "缺少或需修正的基础资料：",
    ]
    lines.extend(f"- {item}" for item in missing_materials)
    lines.extend([
        "",
        "证据来源：",
    ])
    lines.extend(f"- {item}" for item in evidence_summary)
    lines.extend([
        "",
        "需处理事项：",
    ])
    item_index = 1
    for result in failed:
        formatted = format_validation_result_for_mail(order, result, item_index)
        if formatted:
            lines.extend(formatted)
            item_index += 1
    lines.extend(
        [
            "",
            "处理建议：",
            "- 请按上方事项分别补齐 CRM 字段、客户映射、商品/SKU 主数据、库存或附件资料。",
            "- 处理完成后重新同步该订单，系统会自动重新预审。",
            "",
            config_value(session, "bot_signature", "积木易搭AI机器人"),
        ]
    )
    return "\n".join(lines)


PHASE_ONE_FIELD_LABELS = {
    "sales_user_name": "销售负责人",
    "sales_user_email": "销售邮箱",
    "owner_department": "归属部门",
    "order_date": "订单日期",
    "settlement_method": "结算方式",
    "receipt_contact": "收货联系人",
    "receipt_phone": "收货联系电话",
    "receipt_address": "收货地址",
    "currency": "币种",
    "attachment_files": "关键附件",
    "order_items": "订单商品明细",
}


def format_validation_result_for_mail(order: MiddlePlatformOrder, result: ValidationResult, index: int) -> list[str]:
    if result.rule_code == "ATTACHMENT_PRODUCT_CONSISTENCY":
        return format_attachment_consistency_result_for_mail(result, index)
    if result.rule_code == "PHASE1_COMPLETE_PRE_REVIEW_FIELDS":
        return format_phase_one_result_for_mail(order, result, index)
    if result.rule_code in {"KNOWN_ACTIVE_SKU", "SKU_MAPPING_MISSING"}:
        return format_sku_result_for_mail(result, index)
    if result.rule_code == "CUSTOMER_MAPPING":
        return [
            f"{index}. 客户映射",
            f"   当前值：{order.customer_name or '未填写'}",
            "   不通过原因：系统未找到该客户对应的中台/OMS 客户映射。",
            "   处理要求：请维护客户映射，或确认 OMS 客户资料是否已建立。",
        ]
    if result.rule_code == "HAS_ORDER_ITEMS":
        return [
            f"{index}. 商品明细",
            "   当前值：未解析到商品明细",
            "   不通过原因：CRM 订单没有解析到任何明细行。",
            "   处理要求：请确认 CRM 订单中已填写商品、规格、数量等信息。",
        ]
    return [
        f"{index}. {validation_result_title(result)}",
        f"   当前值：{validation_current_value(result)}",
        f"   不通过原因：{clean_validation_text(result.reason)}",
        "   处理要求：请按原因修正后重新同步该订单。",
    ]


def format_phase_one_result_for_mail(order: MiddlePlatformOrder, result: ValidationResult, index: int) -> list[str]:
    crm = order.crm_order
    raw = loads(crm.raw_json, {}) if crm else {}
    lines = [f"{index}. 一期完整性预审"]
    sub_index = 1
    refs = result.evidence_refs or []
    for ref in refs:
        field_match = re.fullmatch(r"(.+?)\(([\w_]+)\)", ref.strip())
        if field_match:
            raw_label, field = field_match.groups()
            label = PHASE_ONE_FIELD_LABELS.get(field, raw_label)
            lines.extend(
                [
                    f"   {sub_index}) {label}",
                    "      当前值：未填写",
                    f"      不通过原因：{label}缺失。",
                    f"      处理要求：请在 CRM 订单中补充{label}。",
                ]
            )
            sub_index += 1
            continue
        if ref.startswith("收货地址不是可邮寄详细地址："):
            value = ref.split("：", 1)[1].strip()
            lines.extend(
                [
                    f"   {sub_index}) 收货地址",
                    f"      当前值：{value or '未填写'}",
                    "      不通过原因：收货地址不是可邮寄的详细地址。",
                    "      处理要求：请补充省市区、街道、门牌号等完整地址。",
                ]
            )
            sub_index += 1
            continue
        if ref.startswith("CRM 审批状态未通过："):
            value = ref.split("：", 1)[1].strip()
            lines.extend(
                [
                    f"   {sub_index}) CRM 审批状态",
                    f"      当前值：{value or '未填写'}",
                    "      不通过原因：CRM 审批状态未达到可履约条件。",
                    "      处理要求：请完成 CRM 审批后重新同步。",
                ]
            )
            sub_index += 1
            continue
        if ref.startswith("CRM 订单生命状态异常："):
            value = ref.split("：", 1)[1].strip()
            lines.extend(
                [
                    f"   {sub_index}) CRM 订单生命状态",
                    f"      当前值：{value or '未填写'}",
                    "      不通过原因：CRM 订单当前生命状态不允许继续履约。",
                    "      处理要求：请确认订单状态已恢复为正常/有效后重新同步。",
                ]
            )
            sub_index += 1
            continue
        if ref == "附件未识别到盖章/签字 PO 或盖章/签字合同":
            attachments = loads(crm.attachment_files_json, []) if crm else []
            value = "、".join(str(item) for item in attachments) if isinstance(attachments, list) else str(attachments or "")
            lines.extend(
                [
                    f"   {sub_index}) 关键附件",
                    f"      当前值：{value or '未上传'}",
                    "      不通过原因：附件中未识别到盖章/签字 PO 或盖章/签字合同。",
                    "      处理要求：请补充有效盖章/签字文件，或确认附件解析结果。",
                ]
            )
            sub_index += 1
            continue
        if raw and "life_status" in str(ref):
            value = str(raw.get("life_status") or "").strip()
            lines.extend(
                [
                    f"   {sub_index}) CRM 订单生命状态",
                    f"      当前值：{value or '未填写'}",
                    "      不通过原因：CRM 订单当前生命状态不允许继续履约。",
                    "      处理要求：请确认订单状态已恢复为正常/有效后重新同步。",
                ]
            )
            sub_index += 1
            continue
        lines.extend(
            [
                f"   {sub_index}) 基础资料",
                f"      当前值：{validation_current_value(result)}",
                f"      不通过原因：{clean_validation_text(ref)}",
                "      处理要求：请按原因补充或修正 CRM 订单资料。",
            ]
        )
        sub_index += 1
    if sub_index == 1:
        lines.extend(
            [
                "   当前值：资料不完整或不符合预审要求",
                f"   不通过原因：{clean_validation_text(result.reason)}",
                "   处理要求：请补齐 CRM 订单基础资料后重新同步。",
            ]
        )
    return lines


def format_sku_result_for_mail(result: ValidationResult, index: int) -> list[str]:
    issues = sku_match_issues(result)
    lines = [f"{index}. 商品/SKU 匹配问题"]
    if not issues:
        current_value = "未匹配到标准 SKU"
        if result.rule_code == "SKU_MAPPING_MISSING":
            current_value = "渠道 SKU 未匹配中台标准 SKU"
        lines.extend(
            [
                f"   当前值：{current_value}",
                f"   不通过原因：{clean_validation_text(result.reason)}",
                "   处理要求：请维护标准 SKU、商品别名或渠道 SKU 映射。",
            ]
        )
        return lines
    for item_index, issue in enumerate(issues, start=1):
        product_name = str(issue.get("product_name") or "").strip() or "未填写"
        reason = str(issue.get("reason") or "").strip()
        candidates = issue.get("candidates") if isinstance(issue.get("candidates"), list) else []
        lines.extend(
            [
                f"   {item_index}. CRM 商品：{product_name}",
                f"      当前值：{sku_current_value(reason)}",
                f"      不通过原因：{sku_failure_reason(reason)}",
                "      处理要求：请补充型号、版本、套装内容或标准 SKU 编码。",
            ]
        )
        if candidates:
            lines.extend(["", "      可能匹配项（按相似度排序）："])
            for candidate_index, candidate in enumerate(sorted_sku_candidates(candidates), start=1):
                sku_id = str(candidate.get("sku_id") or "-").strip()
                name = str(candidate.get("product_name") or candidate.get("matched_value") or candidate.get("spu_id") or "-").strip()
                confidence = int(candidate.get("confidence") or 0)
                lines.append(f"      {candidate_index}）{sku_id}｜{name}｜相似度 {confidence}%")
        else:
            lines.extend(["", "      可能匹配项（按相似度排序）：暂无高置信度候选项"])
    return lines


def sku_match_issues(result: ValidationResult) -> list[dict[str, Any]]:
    issues: list[dict[str, Any]] = []
    for ref in result.evidence_refs or []:
        if not ref.startswith("SKU_MATCH_JSON:"):
            continue
        payload = loads(ref.split(":", 1)[1], {})
        if isinstance(payload, dict):
            issues.append(payload)
    return issues


def sorted_sku_candidates(candidates: list[Any]) -> list[dict[str, Any]]:
    dict_candidates = [item for item in candidates if isinstance(item, dict)]
    return sorted(
        dict_candidates,
        key=lambda item: (int(item.get("confidence") or 0), int(item.get("score") or 0)),
        reverse=True,
    )


def sku_current_value(reason: str) -> str:
    if reason == "ambiguous":
        return "候选 SKU 过多，无法自动选择"
    if reason == "low_confidence":
        return "候选 SKU 相似度不足，无法自动选择"
    if reason == "not_found":
        return "未匹配到标准 SKU"
    return "未匹配到唯一标准 SKU"


def sku_failure_reason(reason: str) -> str:
    if reason == "ambiguous":
        return "商品描述过于泛化，匹配到多个相似 SKU。"
    if reason == "low_confidence":
        return "系统找到相似商品，但相似度不足，无法自动判断。"
    if reason == "not_found":
        return "系统没有找到足够相似的标准商品或别名。"
    return "系统无法根据当前商品信息确认唯一标准 SKU。"


def validation_result_title(result: ValidationResult) -> str:
    names = {
        "REQUIRED_HEAD_FIELDS": "订单头基础字段",
        "POSITIVE_ORDER_AMOUNT": "订单金额",
        "AMOUNT_CONSISTENCY": "金额一致性",
        "RULE_SKU_BOM_MATCH": "SKU/BOM 匹配",
        "RULE_CONTRACT_AMOUNT_CONSISTENCY": "合同金额一致性",
        "LOCAL_INVENTORY_AVAILABLE": "本地库存",
    }
    return names.get(result.rule_code, "预审检查")


def validation_current_value(result: ValidationResult) -> str:
    refs = [clean_validation_text(ref) for ref in result.evidence_refs or [] if str(ref or "").strip()]
    return "；".join(refs[:3]) if refs else "见不通过原因"


def clean_validation_text(value: str) -> str:
    text = str(value or "").strip()
    text = re.sub(r"\(([a-zA-Z_][a-zA-Z0-9_]*)(?:[,，]\s*[a-zA-Z_][a-zA-Z0-9_]*)*\)", "", text)
    text = text.replace("CRM.customer_name=", "客户名称：")
    text = text.replace("OMS.customer.query=", "OMS 客户查询：")
    text = text.replace("配置项 v2_customer_mapping_json", "客户映射配置")
    text = text.replace("SKU_MATCH_JSON:", "")
    return text


def format_attachment_consistency_result_for_mail(result: ValidationResult, index: int) -> list[str]:
    issue_text = result.reason
    prefix = "CRM 订单产品与附件解析内容不一致："
    if issue_text.startswith(prefix):
        issue_text = issue_text[len(prefix):]
    rows = []
    for raw_issue in [part.strip() for part in re.split(r"[；;]", issue_text) if part.strip()]:
        rows.append(_attachment_consistency_issue_row(raw_issue))
    lines = [
        f"{index}. 附件商品一致性",
        "   当前值：CRM 订单产品与附件解析内容不一致",
        "   不通过原因：CRM 订单产品与附件解析内容不一致",
        "   处理要求：请核对 CRM 商品明细和附件中的产品、数量、单价、金额。",
    ]
    if not rows:
        return lines
    lines.extend(
        [
            "  对比明细：",
            "  | 商品 | 对比项 | CRM | 附件 | 结论 |",
            "  | --- | --- | --- | --- | --- |",
        ]
    )
    for row in rows:
        lines.append(
            "  | "
            + " | ".join(
                _mail_table_cell(row[key])
                for key in ["product", "field", "crm", "attachment", "conclusion"]
            )
            + " |"
        )
    return lines


def _attachment_consistency_issue_row(raw_issue: str) -> dict[str, str]:
    product, _, message = raw_issue.partition("：")
    product = product.strip() or "-"
    message = message.strip() or raw_issue.strip()
    crm = _extract_issue_value(message, "CRM")
    attachment = _extract_issue_value(message, "附件")
    field = "商品名称/关键词"
    conclusion = message
    if "数量" in message:
        field = "数量"
    elif "单价" in message:
        field = "单价"
    elif "明细总价" in message or "总价" in message or "金额" in message:
        field = "明细总价"

    if "未出现可匹配的商品名称" in message:
        conclusion = "附件未匹配到 CRM 商品关键词"
        attachment = "未匹配"
    elif "未识别到可比对" in message:
        conclusion = "附件未识别到可比对字段"
        attachment = "未识别"
    elif "不一致" in message:
        conclusion = "不一致"
    return {
        "product": product,
        "field": field,
        "crm": crm or "-",
        "attachment": attachment or "-",
        "conclusion": conclusion,
    }


def _extract_issue_value(message: str, label: str) -> str:
    match = re.search(rf"{label}=([^，；;、)）]+)", message)
    return match.group(1).strip() if match else ""


def _mail_table_cell(value: str) -> str:
    return str(value or "-").replace("|", "/").replace("\n", " ").strip()


def build_context_pack(
    session: Session,
    order: MiddlePlatformOrder,
    exception_type: ExceptionType,
    severity: str,
    reason: str,
    validation_results: list[ValidationResult],
    *,
    trace_id: str = "",
) -> dict[str, Any]:
    failed = [result.as_dict() for result in validation_results if not result.passed]
    failed_results = [result for result in validation_results if not result.passed]
    policy = exception_policy(exception_type, severity)
    evidence_summary = validation_evidence_summary(session, order)
    evidence_refs = []
    for result in failed:
        evidence_refs.extend(result.get("evidenceRefs") or [])
    return {
        "context_type": "V2_ORDER_EXCEPTION",
        "trace_id": trace_id,
        "exception": {
            "type": exception_type.value,
            "severity": severity,
            "summary": reason,
            "risk_level": severity,
            "likely_reason": reason,
            "source_system": policy["source_system"],
            "responsible_role": policy["responsible_role"],
            "can_auto_retry": policy["can_auto_retry"],
            "freeze_order_flow": policy["freeze_order_flow"],
            "suggested_actions": suggested_actions(exception_type, failed),
            "evidence_refs": list(dict.fromkeys(evidence_refs + evidence_summary)),
        },
        "order": {
            "order_no": order.order_no,
            "status": order.status,
            "crm_order_id": order.crm_order_id,
            "crm_order_no": order.crm_order_no,
            "customer_name": order.customer_name,
            "amount": str(order.order_amount) if order.order_amount is not None else None,
            "currency": order.currency,
        },
        "validation": {
            "failed_rules": failed,
            "missing_materials": classify_validation_missing_materials(failed_results),
            "evidence_summary": evidence_summary,
        },
    }


def exception_policy(exception_type: ExceptionType, severity: str) -> dict[str, Any]:
    policies: dict[ExceptionType, dict[str, Any]] = {
        ExceptionType.VALIDATION_BLOCKED: {"source_system": "CRM", "responsible_role": "商务/销售", "can_auto_retry": False, "freeze_order_flow": True},
        ExceptionType.SKU_MAPPING_MISSING: {"source_system": "CRM", "responsible_role": "商品/主数据管理员", "can_auto_retry": False, "freeze_order_flow": True},
        ExceptionType.OMS_REQUIRED_FIELDS_MISSING: {"source_system": "OMS", "responsible_role": "物流/IT", "can_auto_retry": False, "freeze_order_flow": True},
        ExceptionType.OMS_BLOCKED: {"source_system": "OMS", "responsible_role": "IT 运维/物流", "can_auto_retry": False, "freeze_order_flow": True},
        ExceptionType.OMS_STATUS_CONFLICT: {"source_system": "OMS", "responsible_role": "IT 运维/物流", "can_auto_retry": True, "freeze_order_flow": True},
        ExceptionType.CRM_CHANGED_AFTER_OMS_ACCEPTED: {"source_system": "CRM", "responsible_role": "商务主管/物流/IT", "can_auto_retry": False, "freeze_order_flow": True},
        ExceptionType.CRM_CHANGED_DURING_OMS_PENDING: {"source_system": "CRM", "responsible_role": "商务/物流/IT", "can_auto_retry": False, "freeze_order_flow": True},
        ExceptionType.CRM_CHANGED_DURING_OMS_RETRY: {"source_system": "CRM", "responsible_role": "商务主管/物流/IT", "can_auto_retry": False, "freeze_order_flow": True},
        ExceptionType.CRM_CHANGED_DURING_PICKING: {"source_system": "CRM", "responsible_role": "商务主管/仓库/物流", "can_auto_retry": False, "freeze_order_flow": True},
        ExceptionType.CRM_CANCELLED_AFTER_OMS_ACCEPTED: {"source_system": "CRM", "responsible_role": "商务主管/物流", "can_auto_retry": False, "freeze_order_flow": True},
        ExceptionType.CRM_CANCELLED_DURING_OMS_PENDING: {"source_system": "CRM", "responsible_role": "商务/物流", "can_auto_retry": False, "freeze_order_flow": True},
        ExceptionType.CRM_CANCELLED_DURING_OMS_RETRY: {"source_system": "CRM", "responsible_role": "商务主管/物流/IT", "can_auto_retry": False, "freeze_order_flow": True},
        ExceptionType.CRM_CHANGED_AFTER_SHIPPED: {"source_system": "CRM", "responsible_role": "商务主管/财务/物流", "can_auto_retry": False, "freeze_order_flow": False},
        ExceptionType.CRM_CANCELLED_AFTER_SHIPPED: {"source_system": "CRM", "responsible_role": "商务主管/财务/物流", "can_auto_retry": False, "freeze_order_flow": False},
        ExceptionType.MANUAL_REPLAY_WITHOUT_FIX: {"source_system": "Manual", "responsible_role": "商务/IT", "can_auto_retry": False, "freeze_order_flow": True},
    }
    default_source = "System" if severity in {"Low", "Medium"} else "CRM"
    return policies.get(
        exception_type,
        {"source_system": default_source, "responsible_role": "商务/IT", "can_auto_retry": severity in {"Low", "Medium"}, "freeze_order_flow": severity in {"High", "Critical"}},
    )


def suggested_actions(exception_type: ExceptionType, failed: list[dict[str, Any]]) -> list[str]:
    if exception_type == ExceptionType.OMS_BLOCKED:
        return ["检查 OMS 接口连通性与幂等键", "确认发货通知单字段是否满足 OMS 必填项", "修复后从异常台重放 OMS 下推"]
    if any(item.get("rule_code") == "KNOWN_ACTIVE_SKU" for item in failed):
        return ["在主数据维护 SKU 或补充 CRM 明细映射", "处理完成后重新触发订单预审"]
    if any(item.get("rule_code") == "HAS_ORDER_ITEMS" for item in failed):
        return ["检查 CRM 抓取字段是否包含订单明细", "补齐明细同步配置后重新抓取订单"]
    return ["核对 CRM 订单头字段与附件证据", "处理完成后重新触发订单预审"]


# order_dashboard 结果缓存（TTL 30秒），避免每次列表刷新都重复计算全表聚合
_dashboard_cache: dict[str, Any] = {"result": None, "expires_at": 0.0}


def order_dashboard(session: Session) -> dict[str, Any]:
    now = time.time()
    if _dashboard_cache["result"] is not None and now < _dashboard_cache["expires_at"]:
        return _dashboard_cache["result"]

    status_counts = {
        status: count
        for status, count in session.query(MiddlePlatformOrder.status, func.count(MiddlePlatformOrder.id)).group_by(MiddlePlatformOrder.status).all()
    }
    open_exceptions = (
        session.query(ExceptionCase)
        .filter(ExceptionCase.status == "Open", or_(ExceptionCase.exception_type == ExceptionType.VALIDATION_BLOCKED.value, ExceptionCase.exception_type == ExceptionType.OMS_BLOCKED.value))
        .count()
    )
    total = sum(status_counts.values())
    passed = sum(status_counts.get(status, 0) for status in [OrderStatus.VALIDATED.value, OrderStatus.DELIVERY_NOTICE_READY.value, OrderStatus.OMS_PENDING.value, OrderStatus.OMS_RETRYING.value, OrderStatus.OMS_ACCEPTED.value, OrderStatus.PICKING.value, OrderStatus.SHIPPED.value, OrderStatus.FULFILLMENT_ARCHIVED.value])
    result = {
        "total_orders": total,
        "status_counts": status_counts,
        "stp_rate": round((passed / total) * 100, 2) if total else 0,
        "open_exceptions": open_exceptions,
        "oms_retrying": status_counts.get(OrderStatus.OMS_RETRYING.value, 0),
        "oms_blocked": status_counts.get(OrderStatus.OMS_BLOCKED.value, 0),
    }
    _dashboard_cache["result"] = result
    _dashboard_cache["expires_at"] = now + 30.0  # 30 秒 TTL
    return result


def list_middle_orders(session: Session, *, q: str = "", status: str = "", page: int = 1, page_size: int = 20, current_user: User | None = None) -> dict[str, Any]:
    query = session.query(MiddlePlatformOrder)
    
    # Enforce data visibility scope for sales/business operators
    if current_user is not None and hasattr(current_user, "role") and current_user.role == "business_operator":
        filter_expr = (MiddlePlatformOrder.sales_user_name == current_user.username)
        if current_user.department:
            filter_expr = filter_expr | MiddlePlatformOrder.crm_order.has(CrmSalesOrder.owner_department.ilike(current_user.department))
        query = query.filter(filter_expr)

    if q.strip():
        pattern = f"%{q.strip()}%"
        query = query.filter(
            or_(
                MiddlePlatformOrder.order_no.ilike(pattern),
                MiddlePlatformOrder.crm_order_no.ilike(pattern),
                MiddlePlatformOrder.customer_name.ilike(pattern),
            )
        )
    if status.strip():
        query = query.filter(MiddlePlatformOrder.status == status.strip())
    total = query.count()
    rows = query.order_by(MiddlePlatformOrder.created_at.desc()).offset((page - 1) * page_size).limit(page_size).all()
    for row in rows:
        ensure_middle_order_business_fields(session, row)
    return {
        "items": [serialize_middle_order(row, current_user=current_user) for row in rows],
        "total": total,
        "page": page,
        "page_size": page_size,
        "total_pages": max(1, (total + page_size - 1) // page_size),
        "summary": order_dashboard(session),
        "status_options": [status.value for status in OrderStatus],
    }


def serialize_middle_order(order: MiddlePlatformOrder, *, include_detail: bool = False, current_user: User | None = None) -> dict[str, Any]:
    sales_user = order.sales_user_name
    dept = order.crm_order.owner_department if order.crm_order else None
    mask = should_mask_financials(current_user, sales_user, dept)

    data = {
        "id": order.id,
        "order_no": order.order_no,
        "crm_sales_order_id": order.crm_sales_order_id,
        "crm_order_id": order.crm_order_id,
        "crm_order_no": order.crm_order_no,
        "source_policy": order.source_policy,
        "platform_order_no": order.platform_order_no,
        "shop_code": order.shop_code,
        "channel_code": order.channel_code,
        "fulfillment_type": order.fulfillment_type,
        "customer_name": order.customer_name,
        "sales_user_name": order.sales_user_name,
        "currency": order.currency,
        "order_amount": "***" if mask else (str(order.order_amount) if order.order_amount is not None else None),
        "order_type": order.order_type,
        "entity_code": order.entity_code,
        "fulfillment_entity": order.fulfillment_entity,
        "erp_bill_no": order.erp_bill_no,
        "status": order.status,
        "validation_summary": loads(order.validation_summary_json, {}),
        "version": order.version,
        "created_at": order.created_at.isoformat() if order.created_at else None,
        "updated_at": order.updated_at.isoformat() if order.updated_at else None,
    }
    if include_detail:
        crm_order = order.crm_order
        data["receipt"] = {
            "contact": crm_order.receipt_contact if crm_order else "",
            "phone": crm_order.receipt_phone if crm_order else "",
            "address": crm_order.receipt_address if crm_order else "",
            "delivery_date": crm_order.delivery_date if crm_order else "",
            "logistics_status": crm_order.logistics_status if crm_order else "",
            "shipment_status": crm_order.shipment_status if crm_order else "",
        }
        data["flow"] = {
            "source_policy": order.source_policy,
            "status": order.status,
            "imported_at": order.imported_at.isoformat() if order.imported_at else None,
            "validated_at": order.validated_at.isoformat() if order.validated_at else None,
            "created_at": order.created_at.isoformat() if order.created_at else None,
            "updated_at": order.updated_at.isoformat() if order.updated_at else None,
            "version": order.version,
            "erp_bill_no": order.erp_bill_no,
            "validation_summary": loads(order.validation_summary_json, {}),
        }
        from sqlalchemy.orm import object_session
        from backend.app.models import ProductSPU, ProductSKU
        session = object_session(order)
        data["items"] = []
        for item in order.items:
            official_name = None
            official_spu_id = None
            sku_record = None
            if session and item.sku_code:
                sku_record = session.query(ProductSKU).filter(ProductSKU.sku_id == item.sku_code).first()
                if sku_record:
                    spu_record = session.query(ProductSPU).filter(ProductSPU.id == sku_record.spu_uuid).first()
                    if spu_record:
                        official_name = spu_record.name
                        official_spu_id = spu_record.spu_id
                        if sku_record.model:
                            official_name += f"({sku_record.model})"
            # Fallback: sku_code 不是标准物料编码时，按产品名称匹配 SPU
            if official_name is None and session and item.product_name:
                name_clean = item.product_name.strip()
                if name_clean:
                    spu_by_name = session.query(ProductSPU).filter(
                        ProductSPU.name == name_clean
                    ).first()
                    if spu_by_name:
                        official_name = spu_by_name.name
                        official_spu_id = spu_by_name.spu_id
            data["items"].append({
                "id": item.id,
                "sku_code": item.sku_code,
                "official_sku_code": official_spu_id or item.sku_code,
                "shop_sku_code": item.shop_sku_code,
                "channel_code": item.channel_code,
                "product_name": item.product_name,
                "official_product_name": official_name,
                "quantity": str(item.quantity) if item.quantity is not None else None,
                "unit_price": "***" if mask else (str(item.unit_price) if item.unit_price is not None else None),
                "line_amount": "***" if mask else (str(item.line_amount) if item.line_amount is not None else None),
                "logistics": item_logistics_summary(loads(item.raw_json, {})),
            })
        data["delivery_notices"] = [
            {
                "id": notice.id,
                "notice_no": notice.notice_no,
                "notice_version": notice.notice_version,
                "source_snapshot_hash": notice.source_snapshot_hash,
                "status": notice.status,
                "oms_method": notice.oms_method,
                "oms_order_no": notice.oms_order_no,
                "owner_code": notice.owner_code,
                "warehouse_code": notice.warehouse_code,
                "shop_code": notice.shop_code,
                "logistic_code": notice.logistic_code,
                "waybill_no": notice.waybill_no,
                "print_status": notice.print_status,
                "print_error": notice.print_error,
                "print_retry_count": notice.print_retry_count,
                "platform_fulfillment_status": notice.platform_fulfillment_status,
                "platform_fulfillment_error": notice.platform_fulfillment_error,
                "platform_fulfillment_retry_count": notice.platform_fulfillment_retry_count,
                "platform_fulfillment_synced_waybill_no": notice.platform_fulfillment_synced_waybill_no,
                "platform_fulfillment_synced_at": notice.platform_fulfillment_synced_at.isoformat() if notice.platform_fulfillment_synced_at else None,
                "retry_count": notice.retry_count,
                "next_retry_at": notice.next_retry_at.isoformat() if notice.next_retry_at else None,
                "last_error": notice.last_error,
                "split_preview": loads(notice.split_preview_json, {}),
                "payload": loads(notice.payload_json, {}),
                "confirmed_by": notice.confirmed_by,
                "confirmed_at": notice.confirmed_at.isoformat() if notice.confirmed_at else None,
                "pushed_at": notice.pushed_at.isoformat() if notice.pushed_at else None,
                "version": notice.version,
            }
            for notice in sorted(order.delivery_notices, key=lambda row: row.created_at, reverse=True)
        ]
    return data


def item_logistics_summary(raw: dict[str, Any]) -> dict[str, Any]:
    return {
        "contact": raw_text_from_keys(raw, ["receipt_contact", "receiver_name", "receiverName", "recipient_name", "recipientName", "收货人", "联系人"]),
        "phone": raw_text_from_keys(raw, ["receipt_phone", "receiver_phone", "receiverPhone", "recipient_phone", "recipientPhone", "收货电话", "联系电话"]),
        "address": raw_text_from_keys(raw, ["receipt_address", "receiver_address", "receiverAddress", "recipient_address", "recipientAddress", "收货地址", "地址"]),
        "delivery_date": raw_text_from_keys(raw, ["delivery_date", "deliveryDate", "expected_delivery_date", "expectedDeliveryDate", "期望交期", "交期"]),
        "shipping_method": raw_text_from_keys(raw, ["shipping_method", "shippingMethod", "delivery_method", "deliveryMethod", "logistics_mode", "logisticsMode", "物流方式", "配送方式"]),
        "warehouse_code": raw_text_from_keys(raw, ["warehouse_code", "warehouseCode", "warehouse", "仓库"]),
    }
