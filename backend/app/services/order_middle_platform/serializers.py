"""order_middle_platform — serializers"""
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

# 订单看板缓存（TTL 30秒），避免每次列表刷新都重复计算全表聚合
_dashboard_cache: dict[str, Any] = {"result": None, "expires_at": 0.0}

# Cross-module references within this package
from backend.app.services.order_middle_platform.enums import ExceptionType
from backend.app.services.order_middle_platform.enums import OrderStatus
from backend.app.services.order_middle_platform.utils import ensure_middle_order_business_fields
from backend.app.services.order_middle_platform.utils import raw_text_from_keys

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
        "imported_at": order.imported_at.isoformat() if order.imported_at else None,
        "order_date": order.crm_order.order_date if order.crm_order else None,
        "created_at": order.created_at.isoformat() if order.created_at else None,
        "updated_at": order.updated_at.isoformat() if order.updated_at else None,
        "date_out_of_scope": _date_out_of_scope(order),
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



def _date_out_of_scope(order: MiddlePlatformOrder) -> bool:
    """检查订单下单日期是否在最早同步日期之前"""
    if not order.crm_order or not order.crm_order.order_date:
        return False
    from sqlalchemy.orm import object_session
    session = object_session(order)
    if session is None:
        return False
    from backend.app.models import SystemConfig
    from datetime import date
    cfg = session.get(SystemConfig, "crm_sync_min_order_date")
    if not cfg or not cfg.value:
        return False
    try:
        min_date = date.fromisoformat(cfg.value.strip())
        order_date = order.crm_order.order_date
        if isinstance(order_date, str):
            order_date = date.fromisoformat(order_date[:10])
        return order_date < min_date
    except (ValueError, TypeError):
        return False



def item_logistics_summary(raw: dict[str, Any]) -> dict[str, Any]:
    return {
        "contact": raw_text_from_keys(raw, ["receipt_contact", "receiver_name", "receiverName", "recipient_name", "recipientName", "收货人", "联系人"]),
        "phone": raw_text_from_keys(raw, ["receipt_phone", "receiver_phone", "receiverPhone", "recipient_phone", "recipientPhone", "收货电话", "联系电话"]),
        "address": raw_text_from_keys(raw, ["receipt_address", "receiver_address", "receiverAddress", "recipient_address", "recipientAddress", "收货地址", "地址"]),
        "delivery_date": raw_text_from_keys(raw, ["delivery_date", "deliveryDate", "expected_delivery_date", "expectedDeliveryDate", "期望交期", "交期"]),
        "shipping_method": raw_text_from_keys(raw, ["shipping_method", "shippingMethod", "delivery_method", "deliveryMethod", "logistics_mode", "logisticsMode", "物流方式", "配送方式"]),
        "warehouse_code": raw_text_from_keys(raw, ["warehouse_code", "warehouseCode", "warehouse", "仓库"]),
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
    # 按下单日期降序排列
    query = query.outerjoin(CrmSalesOrder, CrmSalesOrder.id == MiddlePlatformOrder.crm_sales_order_id)
    rows = query.order_by(CrmSalesOrder.order_date.desc().nullslast()).offset((page - 1) * page_size).limit(page_size).all()
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




