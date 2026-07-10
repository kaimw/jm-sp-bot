"""order_middle_platform — utils"""
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
    from backend.app.services.order_middle_platform.platform_fulfillment import is_platform_fulfilled_raw
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
        # 如果 sku_code 是 CRM UUID（非标准物料编码），标记为潜在 CRM 编码
        item["raw_sku_code"] = direct
        # 检查本地 ProductSKU 是否能直接匹配（即标准物料编码）
        sku = session.query(ProductSKU).filter(ProductSKU.sku_id == direct, ProductSKU.status == "Active").first()
        if sku:
            return direct
        # 不是标准物料编码 → 不返回，继续尝试名称匹配
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




