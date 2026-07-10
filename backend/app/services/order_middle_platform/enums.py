"""order_middle_platform — enums"""
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

class IllegalStateTransition(ValueError):
    pass




class DuplicateEventException(ValueError):
    pass




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
    (OrderStatus.DELIVERY_NOTICE_READY, OrderEvent.ERP_SAVE_STARTED): OrderStatus.ERP_SAVING,
    (OrderStatus.ERP_SAVING, OrderEvent.ERP_SAVE_SUCCESS): OrderStatus.ERP_SAVED,
    (OrderStatus.ERP_SAVING, OrderEvent.ERP_SAVE_FAILED): OrderStatus.ERP_FAILED,
    (OrderStatus.ERP_SAVING, OrderEvent.ERP_SUBMIT_FAILED): OrderStatus.ERP_FAILED,
    (OrderStatus.ERP_SAVING, OrderEvent.ERP_AUDIT_FAILED): OrderStatus.ERP_FAILED,
    (OrderStatus.ERP_FAILED, OrderEvent.EXCEPTION_RESOLVED_AND_RE_ERP): OrderStatus.ERP_PENDING,
    (OrderStatus.ERP_SAVED, OrderEvent.DELIVERY_NOTICE_CREATED): OrderStatus.DELIVERY_NOTICE_READY,
    (OrderStatus.ERP_FAILED, OrderEvent.DELIVERY_NOTICE_CREATED): OrderStatus.DELIVERY_NOTICE_READY,
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


