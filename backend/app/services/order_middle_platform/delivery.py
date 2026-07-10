"""order_middle_platform — delivery"""
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
from backend.app.services.order_middle_platform.oms import enqueue_oms_push

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




