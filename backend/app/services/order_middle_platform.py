from __future__ import annotations

import base64
import hashlib
import re
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal, InvalidOperation
from enum import Enum
from typing import Any, Protocol

from sqlalchemy import func, or_
from sqlalchemy.orm import Session

from backend.app.models import (
    AuditEvent,
    CrmSalesOrder,
    DeliveryNotice,
    ExceptionCase,
    IntegrationEvent,
    MiddlePlatformOrder,
    MiddlePlatformOrderItem,
    OrderAttachment,
    OutboundMailJob,
    ProcessingJob,
    ProductInventorySnapshot,
    ProductSKU,
    ChannelPricing,
    SystemConfig,
    User,
    now_utc,
)
from backend.app.services.auth import should_mask_financials
from backend.app.services.exception_diagnosis import enqueue_exception_diagnosis
from backend.app.services.address_quality import is_detailed_receipt_address
from backend.app.services.oms.jackyun_client import JackyunConfigError, jackyun_client_from_session
from backend.app.services.jsonutil import dumps, loads
from backend.app.services.storage import save_attachment
from backend.app.services.task_scheduler import RetryPolicy, next_retry_at


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


class OrderEvent(str, Enum):
    ORDER_SNAPSHOT_FETCHED = "OrderSnapshotFetched"
    CRM_SNAPSHOT_CHANGED = "CrmSnapshotChanged"
    START_VALIDATION = "StartValidation"
    RULES_PASSED = "RulesPassed"
    RULES_FAILED_CRITICAL = "RulesFailedCritical"
    EXCEPTION_RESOLVED_AND_REVALIDATE = "ExceptionResolvedAndRevalidate"
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


class BlockerLevel(str, Enum):
    NONE = "NONE"
    LOW = "LOW"
    HIGH = "HIGH"
    CRITICAL = "CRITICAL"


STATE_TRANSITIONS: dict[tuple[OrderStatus, OrderEvent], OrderStatus] = {
    (OrderStatus.CRM_APPROVED, OrderEvent.ORDER_SNAPSHOT_FETCHED): OrderStatus.IMPORTED,
    (OrderStatus.IMPORTED, OrderEvent.CRM_SNAPSHOT_CHANGED): OrderStatus.IMPORTED,
    (OrderStatus.VALIDATION_BLOCKED, OrderEvent.CRM_SNAPSHOT_CHANGED): OrderStatus.IMPORTED,
    (OrderStatus.VALIDATED, OrderEvent.CRM_SNAPSHOT_CHANGED): OrderStatus.IMPORTED,
    (OrderStatus.DELIVERY_NOTICE_READY, OrderEvent.CRM_SNAPSHOT_CHANGED): OrderStatus.VALIDATION_BLOCKED,
    (OrderStatus.OMS_PENDING, OrderEvent.CRM_SNAPSHOT_CHANGED): OrderStatus.VALIDATION_BLOCKED,
    (OrderStatus.OMS_RETRYING, OrderEvent.CRM_SNAPSHOT_CHANGED): OrderStatus.VALIDATION_BLOCKED,
    (OrderStatus.OMS_BLOCKED, OrderEvent.CRM_SNAPSHOT_CHANGED): OrderStatus.VALIDATION_BLOCKED,
    (OrderStatus.IMPORTED, OrderEvent.START_VALIDATION): OrderStatus.VALIDATING,
    (OrderStatus.VALIDATING, OrderEvent.RULES_PASSED): OrderStatus.VALIDATED,
    (OrderStatus.VALIDATING, OrderEvent.RULES_FAILED_CRITICAL): OrderStatus.VALIDATION_BLOCKED,
    (OrderStatus.VALIDATION_BLOCKED, OrderEvent.EXCEPTION_RESOLVED_AND_REVALIDATE): OrderStatus.VALIDATING,
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


@dataclass
class ValidationResult:
    rule_code: str
    passed: bool
    blocker_level: BlockerLevel = BlockerLevel.NONE
    reason: str = ""
    evidence_refs: list[str] | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "rule_code": self.rule_code,
            "passed": self.passed,
            "blockerLevel": self.blocker_level.value,
            "reason": self.reason,
            "evidenceRefs": self.evidence_refs or [],
        }


@dataclass
class OrderContext:
    order: MiddlePlatformOrder
    crm_order: CrmSalesOrder
    items: list[MiddlePlatformOrderItem]
    session: Session


class OrderValidationRule(Protocol):
    def get_rule_code(self) -> str:
        ...

    def supports(self, context: OrderContext) -> bool:
        ...

    def validate(self, context: OrderContext) -> ValidationResult:
        ...


class RequiredHeadFieldsRule:
    required_fields = ("crm_order_id", "crm_order_no", "customer_name")

    def get_rule_code(self) -> str:
        return "REQUIRED_HEAD_FIELDS"

    def supports(self, context: OrderContext) -> bool:
        return True

    def validate(self, context: OrderContext) -> ValidationResult:
        missing = [field for field in self.required_fields if not getattr(context.order, field, None)]
        if missing:
            return ValidationResult(
                self.get_rule_code(),
                False,
                BlockerLevel.CRITICAL,
                f"订单头字段缺失：{', '.join(missing)}",
                missing,
            )
        return ValidationResult(self.get_rule_code(), True)


class PhaseOneCompletenessRule:
    def get_rule_code(self) -> str:
        return "PHASE1_COMPLETE_PRE_REVIEW_FIELDS"

    def supports(self, context: OrderContext) -> bool:
        return True

    def validate(self, context: OrderContext) -> ValidationResult:
        crm = context.crm_order
        missing: list[str] = []
        invalid: list[str] = []
        raw = loads(crm.raw_json, {})
        attachments = loads(crm.attachment_files_json, [])
        if not isinstance(attachments, list):
            attachments = []

        required = [
            ("approval_status", "CRM 审批状态"),
            ("sales_user_name", "销售负责人"),
            ("owner_department", "归属部门"),
            ("order_date", "订单日期"),
            ("settlement_method", "结算方式"),
            ("receipt_contact", "收货联系人"),
            ("receipt_phone", "收货联系电话"),
            ("receipt_address", "收货地址"),
        ]
        for field, label in required:
            if not str(getattr(crm, field, "") or "").strip():
                missing.append(f"{label}({field})")
        if str(crm.receipt_address or "").strip() and not is_detailed_receipt_address(crm.receipt_address):
            invalid.append(f"收货地址不是可邮寄详细地址：{crm.receipt_address}")

        if not str(context.order.currency or "").strip():
            missing.append("币种(currency)")
        if not attachments:
            missing.append("关键附件(attachment_files)")
        if not context.items:
            missing.append("订单商品明细(order_items)")

        approval_status = str(crm.approval_status or "").strip()
        if approval_status and not is_approved_status(context.session, approval_status):
            invalid.append(f"CRM 审批状态未通过：{approval_status}")

        attachment_names = "；".join(str(item) for item in attachments)
        if attachments and config_bool(context.session, "v2_review_require_key_attachment", True):
            if not re.search(r"(合同|采购|订单|PO|盖章|签章|回签)", attachment_names, re.IGNORECASE):
                invalid.append("附件未识别到合同/采购订单/签章等关键凭证")

        if raw.get("life_status") and str(raw.get("life_status")).lower() not in {"normal", "active", "正常"}:
            invalid.append(f"CRM 订单生命状态异常：{raw.get('life_status')}")

        if missing or invalid:
            parts = []
            if missing:
                parts.append("缺少字段：" + "、".join(missing))
            if invalid:
                parts.append("预审不通过：" + "；".join(invalid))
            return ValidationResult(
                self.get_rule_code(),
                False,
                BlockerLevel.CRITICAL,
                "；".join(parts),
                missing + invalid,
            )
        return ValidationResult(self.get_rule_code(), True)


class CustomerMappingRule:
    def get_rule_code(self) -> str:
        return "CUSTOMER_MAPPING"

    def supports(self, context: OrderContext) -> bool:
        return config_bool(context.session, "v2_review_customer_mapping_required", True)

    def validate(self, context: OrderContext) -> ValidationResult:
        crm = context.crm_order
        customer_id = str(crm.customer_id or "").strip()
        customer_name = str(crm.customer_name or context.order.customer_name or "").strip()
        if not customer_name:
            return ValidationResult(self.get_rule_code(), False, BlockerLevel.CRITICAL, "客户名称缺失，无法完成客户主数据映射。", ["CRM.customer_name"])
        mapping = config_dict(context.session, "v2_customer_mapping_json", {})
        candidates = [customer_id, customer_name]
        for key in candidates:
            if key and key in mapping:
                mapped = mapping.get(key) or {}
                customer_code = str(mapped.get("customer_code") or mapped.get("code") or key).strip()
                if customer_code:
                    return ValidationResult(self.get_rule_code(), True, evidence_refs=[f"客户映射：{customer_name}->{customer_code}"])
        if customer_id:
            return ValidationResult(self.get_rule_code(), True, evidence_refs=[f"CRM 客户ID：{customer_id}"])
        return ValidationResult(
            self.get_rule_code(),
            False,
            BlockerLevel.CRITICAL,
            f"客户未在一期客户映射表中维护：{customer_name}",
            [f"CRM.customer_name={customer_name}", "配置项 v2_customer_mapping_json"],
        )


class PositiveAmountRule:
    def get_rule_code(self) -> str:
        return "POSITIVE_ORDER_AMOUNT"

    def supports(self, context: OrderContext) -> bool:
        return True

    def validate(self, context: OrderContext) -> ValidationResult:
        if context.order.order_amount is None:
            return ValidationResult(self.get_rule_code(), False, BlockerLevel.CRITICAL, "订单金额为空，需人工确认 CRM 金额。")
        if Decimal(str(context.order.order_amount)) <= Decimal("0"):
            return ValidationResult(self.get_rule_code(), False, BlockerLevel.CRITICAL, "订单金额必须大于 0。")
        return ValidationResult(self.get_rule_code(), True)


class AmountConsistencyRule:
    def get_rule_code(self) -> str:
        return "AMOUNT_CONSISTENCY"

    def supports(self, context: OrderContext) -> bool:
        return True

    def validate(self, context: OrderContext) -> ValidationResult:
        crm = context.crm_order
        order_amount = parse_decimal(crm.order_amount)
        product_amount = parse_decimal(crm.product_amount)
        received_amount = parse_decimal(crm.received_amount)
        receivable_amount = parse_decimal(crm.receivable_amount)
        failures: list[str] = []

        if product_amount is None:
            failures.append("商品金额(product_amount)缺失")
        elif order_amount is not None and abs(order_amount - product_amount) > Decimal("0.01"):
            failures.append(f"订单金额 {order_amount} 与商品金额 {product_amount} 不一致")

        if received_amount is not None and receivable_amount is not None and order_amount is not None:
            if abs((received_amount + receivable_amount) - order_amount) > Decimal("0.01"):
                failures.append(f"已收+应收 {received_amount + receivable_amount} 与订单金额 {order_amount} 不一致")

        if failures:
            return ValidationResult(self.get_rule_code(), False, BlockerLevel.CRITICAL, "；".join(failures), failures)
        return ValidationResult(self.get_rule_code(), True)


class HasOrderItemsRule:
    def get_rule_code(self) -> str:
        return "HAS_ORDER_ITEMS"

    def supports(self, context: OrderContext) -> bool:
        return True

    def validate(self, context: OrderContext) -> ValidationResult:
        if not context.items:
            return ValidationResult(self.get_rule_code(), False, BlockerLevel.CRITICAL, "CRM 订单未解析到任何明细行。")
        missing_qty = [item.sku_code or item.product_name or item.id for item in context.items if item.quantity is None or Decimal(str(item.quantity)) <= 0]
        if missing_qty:
            return ValidationResult(self.get_rule_code(), False, BlockerLevel.CRITICAL, f"订单明细数量缺失或不合法：{', '.join(missing_qty[:5])}")
        return ValidationResult(self.get_rule_code(), True)


class KnownSkuRule:
    def get_rule_code(self) -> str:
        return "KNOWN_ACTIVE_SKU"

    def supports(self, context: OrderContext) -> bool:
        return bool(context.items)

    def validate(self, context: OrderContext) -> ValidationResult:
        sku_codes = sorted({str(item.sku_code or "").strip() for item in context.items if str(item.sku_code or "").strip()})
        if not sku_codes:
            return ValidationResult(self.get_rule_code(), False, BlockerLevel.HIGH, "订单明细未提供 SKU 编码，需人工映射。")
        known = {
            row[0]
            for row in context.session.query(ProductSKU.sku_id)
            .filter(ProductSKU.sku_id.in_(sku_codes), ProductSKU.status == "Active")
            .all()
        }
        missing = [sku for sku in sku_codes if sku not in known]
        if missing:
            unmapped_shop_skus = [item.shop_sku_code for item in context.items if item.sku_code in missing and item.shop_sku_code]
            if unmapped_shop_skus:
                return ValidationResult("SKU_MAPPING_MISSING", False, BlockerLevel.CRITICAL, f"CRM 渠道 SKU 未匹配中台标准 SKU：{', '.join(unmapped_shop_skus[:10])}", unmapped_shop_skus)
            return ValidationResult(self.get_rule_code(), False, BlockerLevel.CRITICAL, f"SKU 未在主数据启用：{', '.join(missing[:10])}", missing)
        return ValidationResult(self.get_rule_code(), True)


class LocalInventoryAvailableRule:
    def get_rule_code(self) -> str:
        return "LOCAL_INVENTORY_AVAILABLE"

    def supports(self, context: OrderContext) -> bool:
        return config_bool(context.session, "oms_inventory_review_enabled", True) and bool(context.items)

    def validate(self, context: OrderContext) -> ValidationResult:
        missing_blocks = config_bool(context.session, "oms_inventory_missing_blocks", False)
        failures: list[str] = []
        unknown: list[str] = []
        for item in context.items:
            sku_code = str(item.sku_code or "").strip()
            if not sku_code:
                continue
            required = Decimal(str(item.quantity or 0))
            available = inventory_available_quantity(context.session, sku_code)
            if available is None:
                unknown.append(sku_code)
                continue
            if available < required:
                failures.append(f"{sku_code} 可用 {available} < 需求 {required}")
        if failures:
            return ValidationResult(self.get_rule_code(), False, BlockerLevel.CRITICAL, "库存可用量不足：" + "；".join(failures[:8]), failures)
        if unknown and missing_blocks:
            return ValidationResult(self.get_rule_code(), False, BlockerLevel.CRITICAL, "未找到库存快照：" + "、".join(unknown[:8]), unknown)
        if unknown:
            return ValidationResult(self.get_rule_code(), False, BlockerLevel.HIGH, "部分 SKU 暂无库存快照，已允许进入人工发货单确认：" + "、".join(unknown[:8]), unknown)
        return ValidationResult(self.get_rule_code(), True)


DEFAULT_RULES: list[OrderValidationRule] = [
    RequiredHeadFieldsRule(),
    PhaseOneCompletenessRule(),
    CustomerMappingRule(),
    PositiveAmountRule(),
    AmountConsistencyRule(),
    HasOrderItemsRule(),
    KnownSkuRule(),
    LocalInventoryAvailableRule(),
]


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


def config_list(session: Session, key: str, default: list[str] | None = None) -> list[str]:
    raw = config_value(session, key, "")
    if raw:
        parsed = loads(raw, None)
        if isinstance(parsed, list):
            return [str(item).strip() for item in parsed if str(item).strip()]
        return [item.strip() for item in raw.split(",") if item.strip()]
    return list(default or [])


def config_dict(session: Session, key: str, default: dict[str, Any] | None = None) -> dict[str, Any]:
    raw = config_value(session, key, "")
    if raw:
        parsed = loads(raw, None)
        if isinstance(parsed, dict):
            return parsed
    return dict(default or {})


def config_int(session: Session, key: str, default: int) -> int:
    try:
        return int(config_value(session, key, str(default)))
    except (TypeError, ValueError):
        return default


def is_approved_status(session: Session, value: str) -> bool:
    allowed = config_list(
        session,
        "v2_review_crm_approved_values",
        ["approved", "审批通过", "已审批", "已通过", "complete", "completed", "passed"],
    )
    normalized = value.strip().lower()
    return normalized in {item.strip().lower() for item in allowed}


def inventory_available_quantity(session: Session, sku_code: str) -> Decimal | None:
    rows = (
        session.query(ProductInventorySnapshot)
        .filter(ProductInventorySnapshot.material_code == sku_code, ProductInventorySnapshot.status == "Active")
        .all()
    )
    if not rows:
        return None
    total = Decimal("0")
    for row in rows:
        source = loads(row.source_payload_json, {})
        raw_available = (
            source.get("canUseQuantity")
            or source.get("availableQuantity")
            or source.get("available_quantity")
            or source.get("qty")
            or row.qty
        )
        total += parse_decimal(raw_available) or Decimal("0")
    return total


def parse_decimal(value: Any) -> Decimal | None:
    text = str(value or "").strip().replace(",", "")
    if not text:
        return None
    try:
        return Decimal(text).quantize(Decimal("0.01"))
    except (InvalidOperation, ValueError):
        return None


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


def crm_order_parsed_event(crm_order: CrmSalesOrder, *, trace_id: str | None = None) -> dict[str, Any]:
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
            "order_items": [
                {
                    "sku_code": item.sku_code,
                    "quantity": item.quantity,
                    "unit_price": item.unit_price,
                    "line_amount": item.line_amount,
                }
                for item in sorted(crm_order.items, key=lambda row: row.created_at)
            ],
        },
    }


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
    if existing_order is not None and payload_hash and existing_order.payload_hash == payload_hash and existing_order.status != OrderStatus.CRM_APPROVED.value:
        raise DuplicateEventException(f"重复 CRM_ORDER_PARSED 事件：{crm_order.crm_order_id}/{payload_hash}")
    if existing_order is not None and is_crm_order_cancelled(crm_order):
        return handle_crm_cancel_confirmed(session, existing_order, crm_order, payload_hash=payload_hash, trace_id=trace_id)
    if existing_order is not None and payload_hash and existing_order.payload_hash != payload_hash and existing_order.status != OrderStatus.CRM_APPROVED.value:
        change_result = handle_crm_snapshot_changed(session, existing_order, crm_order, new_payload_hash=payload_hash, trace_id=trace_id)
        if not change_result.get("continue_processing", False):
            return change_result

    order = upsert_middle_platform_order(session, crm_order)
    if order.status == OrderStatus.CRM_APPROVED.value:
        transition_order(session, order, OrderEvent.ORDER_SNAPSHOT_FETCHED, trace_id=trace_id)
    if order.status in {OrderStatus.IMPORTED.value, OrderStatus.VALIDATION_BLOCKED.value}:
        event = OrderEvent.START_VALIDATION if order.status == OrderStatus.IMPORTED.value else OrderEvent.EXCEPTION_RESOLVED_AND_REVALIDATE
        transition_order(session, order, event, trace_id=trace_id)
        validation_results = run_validation_chain(session, order)
        order.validation_summary_json = dumps({"results": [result.as_dict() for result in validation_results]})
        critical = next((result for result in validation_results if result.blocker_level == BlockerLevel.CRITICAL), None)
        if critical is not None:
            transition_order(session, order, OrderEvent.RULES_FAILED_CRITICAL, trace_id=trace_id, detail={"rule_code": critical.rule_code})
            exception_case = create_exception_case(session, order, "VALIDATION_BLOCKED", "High", critical.reason, validation_results, trace_id=trace_id)
            enqueue_validation_failure_notification(session, order, validation_results, exception_case, trace_id=trace_id)
            return {"order_id": order.id, "order_no": order.order_no, "status": order.status, "validation_passed": False}
        transition_order(session, order, OrderEvent.RULES_PASSED, trace_id=trace_id)

    notice = None
    if order.status == OrderStatus.VALIDATED.value:
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

    if status == OrderStatus.DELIVERY_NOTICE_READY:
        expire_delivery_notices(session, order, reason="crm_snapshot_changed_before_oms_push")
        transition_order(session, order, OrderEvent.CRM_SNAPSHOT_CHANGED, trace_id=trace_id, detail=detail)
        case = create_exception_case(
            session,
            order,
            "CRM_CHANGED_BEFORE_OMS_PUSH",
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
        exception_type = "CRM_CHANGED_DURING_OMS_PENDING" if status == OrderStatus.OMS_PENDING else "CRM_CHANGED_DURING_OMS_RETRY"
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
            "CRM_CANCELLED_BEFORE_OMS_PUSH",
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
            "CRM_CANCELLED_DURING_OMS_PENDING",
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
            "CRM_CANCELLED_DURING_OMS_RETRY",
            "Critical",
            "CRM 订单在 OMS 重试/阻断期间撤销，已停止自动重试，需确认下游是否已创建单据。",
            [],
            trace_id=trace_id,
        )
        return {"order_id": order.id, "order_no": order.order_no, "status": order.status, "exception_case_id": case.id, "cancelled": True}

    case_type = "CRM_CANCELLED_AFTER_SHIPPED" if status in {OrderStatus.SHIPPED, OrderStatus.FULFILLMENT_ARCHIVED, OrderStatus.SIGNED, OrderStatus.FINANCE_CHECKING, OrderStatus.FINANCE_EXCEPTION} else "CRM_CANCELLED_AFTER_OMS_ACCEPTED"
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


def crm_change_exception_type(status: OrderStatus) -> str:
    if status == OrderStatus.OMS_ACCEPTED:
        return "CRM_CHANGED_AFTER_OMS_ACCEPTED"
    if status == OrderStatus.PICKING:
        return "CRM_CHANGED_DURING_PICKING"
    if status in {OrderStatus.SHIPPED, OrderStatus.FULFILLMENT_ARCHIVED, OrderStatus.SIGNED, OrderStatus.FINANCE_CHECKING, OrderStatus.FINANCE_EXCEPTION}:
        return "CRM_CHANGED_AFTER_SHIPPED"
    return "CRM_CHANGED_AFTER_OMS_ACCEPTED"


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
            order_no=generate_middle_order_no(crm_order),
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
    order.updated_at = now_utc()
    sync_middle_order_items(session, order, crm_order)
    session.flush()
    session.refresh(order, attribute_names=["items"])
    return order


def generate_middle_order_no(crm_order: CrmSalesOrder) -> str:
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
        raw = loads(crm_order.raw_json, {})
        raw_items = raw.get("items") or raw.get("order_items") or []
        if isinstance(raw_items, list):
            item_payloads = apportioned_order_item_payloads(raw, [item for item in raw_items if isinstance(item, dict)])
            for index, raw_item in enumerate(item_payloads):
                if not isinstance(raw_item, dict):
                    continue
                session.add(
                    MiddlePlatformOrderItem(
                        order_id=order.id,
                        sku_code=standard_sku_code_for_item(session, order, raw_item),
                        product_name=str(raw_item.get("product_name") or raw_item.get("name") or "").strip() or None,
                        shop_sku_code=raw_text_from_keys(raw_item, ["shop_sku_code", "shopSkuCode", "platform_sku", "platformSku", "seller_sku", "sellerSku"]),
                        channel_code=order.channel_code,
                        quantity=parse_decimal(raw_item.get("quantity") or raw_item.get("qty")),
                        unit_price=parse_decimal(raw_item.get("unit_price")),
                        line_amount=parse_decimal(raw_item.get("line_amount") or raw_item.get("amount")),
                        raw_json=dumps({"index": index, **raw_item}),
                    )
                )
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
    if not shop_sku:
        return None
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
    return shop_sku


def run_validation_chain(session: Session, order: MiddlePlatformOrder, rules: list[OrderValidationRule] | None = None) -> list[ValidationResult]:
    crm_order = session.get(CrmSalesOrder, order.crm_sales_order_id)
    if crm_order is None:
        raise RuntimeError("CRM order missing for validation")
    context = OrderContext(order=order, crm_order=crm_order, items=list(order.items), session=session)
    results: list[ValidationResult] = []
    for rule in rules or DEFAULT_RULES:
        if not rule.supports(context):
            continue
        result = rule.validate(context)
        results.append(result)
        if result.blocker_level == BlockerLevel.CRITICAL:
            break
    return results


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
    notice = DeliveryNotice(
        notice_no=notice_no,
        order_id=order.id,
        notice_version=1,
        source_snapshot_hash=order.payload_hash,
        status="Previewed",
        oms_idempotency_key=hashlib.sha256(f"{order.order_no}:{order.payload_hash}".encode("utf-8")).hexdigest(),
        oms_method=config_value(session, "oms_create_order_method", "wms.order.create") or "wms.order.create",
        owner_code=config_value(session, "oms_owner_code", "").strip() or None,
        warehouse_code=config_value(session, "oms_warehouse_code", "").strip() or None,
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
            "OMS_REQUIRED_FIELDS_MISSING",
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
        create_exception_case(session, order, "OMS_STATUS_CONFLICT", "High", f"运单号回传平台失败：{message}", [], trace_id=trace_id or notice.id)
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
        create_exception_case(session, order, "OMS_STATUS_CONFLICT", "High", f"跨境面单打印失败：{message}", [], trace_id=trace_id or notice.id)
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
        exception_case = create_exception_case(session, order, "OMS_BLOCKED", "Critical", message, [], trace_id=notice.oms_idempotency_key)
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
    exception_type: str,
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
    session.add(AuditEvent(event_type="ExceptionCaseCreated", related_object_type="MiddlePlatformOrder", related_object_id=order.id, detail=dumps({"exception_type": exception_type, "trace_id": trace_id})))
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
    to_addresses, cc_addresses = validation_failure_recipients(session)
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
    digest_source = "|".join([order.order_no, order.payload_hash or "", ",".join(result.rule_code for result in failed)])
    idempotency_key = f"v2-validation-failed:{hashlib.sha256(digest_source.encode('utf-8')).hexdigest()}"
    existing = session.query(OutboundMailJob).filter(OutboundMailJob.idempotency_key == idempotency_key).first()
    if existing is not None:
        return existing

    subject = f"[订单预审未通过][{order.crm_order_no or order.order_no}] {order.customer_name or ''}".strip()
    body = build_validation_failure_mail_body(session, order, failed, exception_case)
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


def validation_failure_recipients(session: Session) -> tuple[list[str], list[str]]:
    configured_to = config_list(session, "v2_validation_failure_to_json", [])
    configured_cc = config_list(session, "v2_validation_failure_cc_json", [])
    ops = config_value(session, "ops_cc_email", "").strip()
    ceo = config_value(session, "ceo_email", "").strip()
    to_addresses = configured_to or ([ops] if ops else ([ceo] if ceo else []))
    cc_addresses = configured_cc or ([ceo] if ceo and ceo not in to_addresses else [])
    return unique_emails(to_addresses), unique_emails(cc_addresses)


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
        "预审失败项：",
    ])
    for result in failed:
        refs = "；".join(result.evidence_refs or [])
        suffix = f"（{refs}）" if refs else ""
        lines.append(f"- {result.rule_code}：{result.reason}{suffix}")
    lines.extend(
        [
            "",
            "处理建议：",
            "- 请在 CRM 补齐缺失字段、修正未通过项或补充关键附件。",
            "- 处理完成后重新同步该订单，系统会自动重新预审。",
            "",
            config_value(session, "bot_signature", "积木易搭AI机器人"),
        ]
    )
    return "\n".join(lines)


def build_context_pack(
    session: Session,
    order: MiddlePlatformOrder,
    exception_type: str,
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
            "type": exception_type,
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


def exception_policy(exception_type: str, severity: str) -> dict[str, Any]:
    policies: dict[str, dict[str, Any]] = {
        "VALIDATION_BLOCKED": {"source_system": "CRM", "responsible_role": "商务/销售", "can_auto_retry": False, "freeze_order_flow": True},
        "SKU_MAPPING_MISSING": {"source_system": "CRM", "responsible_role": "商品/主数据管理员", "can_auto_retry": False, "freeze_order_flow": True},
        "OMS_REQUIRED_FIELDS_MISSING": {"source_system": "OMS", "responsible_role": "物流/IT", "can_auto_retry": False, "freeze_order_flow": True},
        "OMS_BLOCKED": {"source_system": "OMS", "responsible_role": "IT 运维/物流", "can_auto_retry": False, "freeze_order_flow": True},
        "OMS_STATUS_CONFLICT": {"source_system": "OMS", "responsible_role": "IT 运维/物流", "can_auto_retry": True, "freeze_order_flow": True},
        "CRM_CHANGED_AFTER_OMS_ACCEPTED": {"source_system": "CRM", "responsible_role": "商务主管/物流/IT", "can_auto_retry": False, "freeze_order_flow": True},
        "CRM_CHANGED_DURING_OMS_PENDING": {"source_system": "CRM", "responsible_role": "商务/物流/IT", "can_auto_retry": False, "freeze_order_flow": True},
        "CRM_CHANGED_DURING_OMS_RETRY": {"source_system": "CRM", "responsible_role": "商务主管/物流/IT", "can_auto_retry": False, "freeze_order_flow": True},
        "CRM_CHANGED_DURING_PICKING": {"source_system": "CRM", "responsible_role": "商务主管/仓库/物流", "can_auto_retry": False, "freeze_order_flow": True},
        "CRM_CANCELLED_AFTER_OMS_ACCEPTED": {"source_system": "CRM", "responsible_role": "商务主管/物流", "can_auto_retry": False, "freeze_order_flow": True},
        "CRM_CANCELLED_DURING_OMS_PENDING": {"source_system": "CRM", "responsible_role": "商务/物流", "can_auto_retry": False, "freeze_order_flow": True},
        "CRM_CANCELLED_DURING_OMS_RETRY": {"source_system": "CRM", "responsible_role": "商务主管/物流/IT", "can_auto_retry": False, "freeze_order_flow": True},
        "CRM_CHANGED_AFTER_SHIPPED": {"source_system": "CRM", "responsible_role": "商务主管/财务/物流", "can_auto_retry": False, "freeze_order_flow": False},
        "CRM_CANCELLED_AFTER_SHIPPED": {"source_system": "CRM", "responsible_role": "商务主管/财务/物流", "can_auto_retry": False, "freeze_order_flow": False},
        "MANUAL_REPLAY_WITHOUT_FIX": {"source_system": "Manual", "responsible_role": "商务/IT", "can_auto_retry": False, "freeze_order_flow": True},
    }
    default_source = "System" if severity in {"Low", "Medium"} else "CRM"
    return policies.get(
        exception_type,
        {"source_system": default_source, "responsible_role": "商务/IT", "can_auto_retry": severity in {"Low", "Medium"}, "freeze_order_flow": severity in {"High", "Critical"}},
    )


def suggested_actions(exception_type: str, failed: list[dict[str, Any]]) -> list[str]:
    if exception_type == "OMS_BLOCKED":
        return ["检查 OMS 接口连通性与幂等键", "确认发货通知单字段是否满足 OMS 必填项", "修复后从异常台重放 OMS 下推"]
    if any(item.get("rule_code") == "KNOWN_ACTIVE_SKU" for item in failed):
        return ["在主数据维护 SKU 或补充 CRM 明细映射", "处理完成后重新触发订单预审"]
    if any(item.get("rule_code") == "HAS_ORDER_ITEMS" for item in failed):
        return ["检查 CRM 抓取字段是否包含订单明细", "补齐明细同步配置后重新抓取订单"]
    return ["核对 CRM 订单头字段与附件证据", "处理完成后重新触发订单预审"]


def order_dashboard(session: Session) -> dict[str, Any]:
    status_counts = {
        status: count
        for status, count in session.query(MiddlePlatformOrder.status, func.count(MiddlePlatformOrder.id)).group_by(MiddlePlatformOrder.status).all()
    }
    open_exceptions = (
        session.query(ExceptionCase)
        .filter(ExceptionCase.status == "Open", or_(ExceptionCase.exception_type == "VALIDATION_BLOCKED", ExceptionCase.exception_type == "OMS_BLOCKED"))
        .count()
    )
    total = sum(status_counts.values())
    passed = sum(status_counts.get(status, 0) for status in [OrderStatus.VALIDATED.value, OrderStatus.DELIVERY_NOTICE_READY.value, OrderStatus.OMS_PENDING.value, OrderStatus.OMS_RETRYING.value, OrderStatus.OMS_ACCEPTED.value, OrderStatus.PICKING.value, OrderStatus.SHIPPED.value, OrderStatus.FULFILLMENT_ARCHIVED.value])
    return {
        "total_orders": total,
        "status_counts": status_counts,
        "stp_rate": round((passed / total) * 100, 2) if total else 0,
        "open_exceptions": open_exceptions,
        "oms_retrying": status_counts.get(OrderStatus.OMS_RETRYING.value, 0),
        "oms_blocked": status_counts.get(OrderStatus.OMS_BLOCKED.value, 0),
    }


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
        "status": order.status,
        "validation_summary": loads(order.validation_summary_json, {}),
        "version": order.version,
        "created_at": order.created_at.isoformat() if order.created_at else None,
        "updated_at": order.updated_at.isoformat() if order.updated_at else None,
    }
    if include_detail:
        data["items"] = [
            {
                "id": item.id,
                "sku_code": item.sku_code,
                "shop_sku_code": item.shop_sku_code,
                "channel_code": item.channel_code,
                "product_name": item.product_name,
                "quantity": str(item.quantity) if item.quantity is not None else None,
                "unit_price": "***" if mask else (str(item.unit_price) if item.unit_price is not None else None),
                "line_amount": "***" if mask else (str(item.line_amount) if item.line_amount is not None else None),
            }
            for item in order.items
        ]
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
            for notice in order.delivery_notices
        ]
    return data
