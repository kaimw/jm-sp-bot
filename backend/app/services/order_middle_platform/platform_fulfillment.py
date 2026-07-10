"""order_middle_platform — platform_fulfillment"""
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
from backend.app.services.order_middle_platform.enums import OrderEvent
from backend.app.services.order_middle_platform.enums import transition_order
from backend.app.services.order_middle_platform.notifications import create_exception_case
from backend.app.services.order_middle_platform.utils import record_integration_event

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
    from backend.app.services.order_middle_platform.oms import calculate_next_retry_at
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




