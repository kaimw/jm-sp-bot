"""CRM / 中台订单 → 金蝶销售订单 JSON 映射器

将预审通过的中台订单映射为金蝶 Save API 所需的 JSON 结构。
支持销售订单和备货订单两种场景，备货→武汉仓跳过制单。
"""

from __future__ import annotations

from datetime import date
from typing import Any

from sqlalchemy.orm import Session

from backend.app.models import (
    CustomerEntityMapping,
    EntityMapping,
    MaterialEntityException,
    MiddlePlatformOrder,
    MiddlePlatformOrderItem,
    ProductPrice,
    SystemConfig,
    WarehouseEntityMapping,
)


SALES_ORDER_FTYPE = "XSDD01_SYS"  # 标准销售订单类型


def build_sales_order_model(
    session: Session,
    order: MiddlePlatformOrder,
    items: list[MiddlePlatformOrderItem],
) -> dict[str, Any]:
    """构建金蝶销售订单 Save 所需的 Model JSON

    字段映射依据设计文档 §22.6，补充财务新要求：
      每个物料备注加发货仓 + VAT 信息
      单一收货人→写入客户信息字段
      多收货人→备注加附件说明
    """
    org_id = _resolve_org_id(session, order)
    customer_number = _resolve_customer_number(session, order)
    dept_number = _resolve_dept_number(session)
    saler_number = _resolve_saler_number(session)
    sale_type = _resolve_sale_type(session)

    # 获取发货仓库
    warehouse = _resolve_warehouse(session, order)
    # 获取 VAT 信息（从 CRM 原始数据提取）
    vat_info = _extract_vat(order)
    # 获取收货人信息
    recipient_name, recipient_phone, recipient_address = _resolve_recipient(session, order)
    has_multi_recipients = _has_multi_recipients(session, order)

    entries = _build_order_entries(session, order, items, warehouse=warehouse, vat_info=vat_info)

    model: dict[str, Any] = {
        "FBillTypeID": {"FNUMBER": SALES_ORDER_FTYPE},
        "FBillNo": "",
        "FDate": date.today().isoformat(),
        "FSaleOrgId": {"FNumber": org_id},
        "FCustId": {"FNumber": customer_number},
        "FSaleDeptId": {"FNumber": dept_number},
        "FSalerId": {"FNumber": saler_number},
        "F_UXYO_Assistant": {"FNUMBER": sale_type},
        "FNote": f"中台订单 {order.order_no} | {order.customer_name or ''}",
    }

    # 单一收货人 → 写入客户信息字段
    if recipient_name and not has_multi_recipients:
        model["FReceiveContact"] = recipient_name
        model["FReceiveAddress"] = recipient_address or ""
        model["FReceivePhone"] = recipient_phone or ""
        model["FNote"] += f" | 收货人:{recipient_name} {recipient_phone} {recipient_address}"
    elif has_multi_recipients:
        model["FNote"] += " | 多收货人，客户信息表格见附件"

    if warehouse:
        model["FNote"] += f" | 发货仓:{warehouse}"
    if vat_info:
        model["FNote"] += f" | {vat_info}"

    if entries:
        model["FSaleOrderEntry"] = entries

    return model


def should_skip_erp_billing(order: MiddlePlatformOrder) -> bool:
    """判断是否应跳过 ERP 制单

    规则：备货订单 + 发货仓库为武汉仓 → 跳过
    """
    return bool(order.order_type == "STOCK_REPLENISHMENT" and order.fulfillment_entity == "SZ")


def _resolve_org_id(session: Session, order: MiddlePlatformOrder) -> str:
    """解析金蝶销售组织 ID（FSaleOrgId）

    两层映射（由仓库和物料决定出货主体，非下单主体）：
      Step 1: 查物料例外表（MaterialEntityException）
              特殊物料指定出货组织（如显影剂→厂家代发→深圳）
      Step 2: 查仓库-主体映射表（WarehouseEntityMapping）
              从哪个仓发货，库存组织就填谁
      Step 3: 兜底 → 订单的下单主体（order.entity_code）

    备货订单需先查 CustomerEntityMapping 确定主体。
    """
    entity_code = order.entity_code or "SZ"

    if order.order_type == "STOCK_REPLENISHMENT":
        cust_map = (
            session.query(CustomerEntityMapping)
            .filter(CustomerEntityMapping.customer_name == order.customer_name, CustomerEntityMapping.is_active == True)
            .first()
        )
        if cust_map:
            entity_code = cust_map.entity_code

    # Step 1: 先查物料例外表（按明细行物料号）
    # 如果所有明细行都指向同一例外主体，则使用该主体
    items = list(order.items)
    exception_entity = _resolve_entity_from_material_exception(session, items)
    if exception_entity:
        entity_code = exception_entity

    # Step 2: 查仓库-主体映射表（从订单关联的发货通知取仓库）
    if not exception_entity:
        warehouse_entity = _resolve_entity_from_warehouse(session, order)
        if warehouse_entity:
            entity_code = warehouse_entity

    # 查 EntityMapping 获取 FSaleOrgId
    mapping = session.query(EntityMapping).filter(EntityMapping.entity_code == entity_code, EntityMapping.is_active == True).first()
    if mapping:
        return mapping.erp_org_id

    config = session.get(SystemConfig, "erp_default_org_id")
    return config.value if config and config.value else "100"


def _resolve_entity_from_material_exception(session: Session, items: list) -> str | None:
    """查物料例外表，如果所有明细行的例外主体一致则返回"""
    codes = set()
    for item in items:
        sku = str(item.sku_code or "").strip()
        if not sku:
            continue
        exc = session.query(MaterialEntityException).filter(
            MaterialEntityException.material_code == sku,
            MaterialEntityException.is_active == True,
        ).first()
        if exc:
            codes.add(exc.entity_code)
    if len(codes) == 1:
        return codes.pop()
    return None


def _resolve_entity_from_warehouse(session: Session, order: MiddlePlatformOrder) -> str | None:
    """查仓库-主体映射表"""
    from backend.app.models import DeliveryNotice
    notice = session.query(DeliveryNotice).filter(
        DeliveryNotice.order_id == order.id,
    ).order_by(DeliveryNotice.created_at.desc()).first()
    if not notice or not notice.warehouse_code:
        return None
    mapping = session.query(WarehouseEntityMapping).filter(
        WarehouseEntityMapping.warehouse == notice.warehouse_code,
        WarehouseEntityMapping.is_active == True,
    ).first()
    return mapping.entity_code if mapping else None


def _resolve_customer_number(session: Session, order: MiddlePlatformOrder) -> str:
    """解析金蝶客户编码

    备货订单从 CustomerEntityMapping 取，销售订单从 CRM 信息取。
    一期简化：返回一个默认客户编码（需在实际对接时改为真实映射）。
    """
    if order.order_type == "STOCK_REPLENISHMENT" and order.customer_name:
        cust_map = (
            session.query(CustomerEntityMapping)
            .filter(CustomerEntityMapping.customer_name == order.customer_name, CustomerEntityMapping.is_active == True)
            .first()
        )
        if cust_map:
            # 通过实体配置反查客户编码，一期返回默认值
            pass

    # 一期兜底：使用配置的默认客户
    config = session.get(SystemConfig, "erp_default_customer_number")
    return config.value if config and config.value else "100JM000009"


def _resolve_dept_number(session: Session) -> str:
    config = session.get(SystemConfig, "erp_default_dept_number")
    return config.value if config and config.value else "100BM004.01"


def _resolve_saler_number(session: Session) -> str:
    config = session.get(SystemConfig, "erp_default_saler_number")
    return config.value if config and config.value else "00007_100GW000012_1"


def _resolve_sale_type(session: Session) -> str:
    config = session.get(SystemConfig, "erp_default_sale_type")
    return config.value if config and config.value else "001"


def _get_unit_price(session: Session, sku_id: str, entity_code: str) -> int | None:
    """获取指定主体下的物料内部价格

    先查 ProductPrice 表（按 sku_id + entity_code），
    无匹配时返回 None（由金蝶侧处理）。
    """
    price = (
        session.query(ProductPrice)
        .filter(ProductPrice.sku_id == sku_id, ProductPrice.entity_code == entity_code, ProductPrice.is_active == True)
        .first()
    )
    return price.unit_price if price else None


def _build_order_entries(session: Session, order: MiddlePlatformOrder, items: list[MiddlePlatformOrderItem],
                         *, warehouse: str = "", vat_info: str = "") -> list[dict[str, Any]]:
    """构建金蝶明细行 JSON，每个物料备注加发货仓和 VAT 信息（财务要求）"""
    entity_code = order.entity_code or "SZ"
    entries: list[dict[str, Any]] = []

    for item in items:
        sku_code = item.sku_code or ""
        if not sku_code:
            continue

        # 物料备注：品名 + 发货仓 + VAT
        note_parts = [item.product_name or ""]
        if warehouse:
            note_parts.append(f"仓:{warehouse}")
        if vat_info:
            note_parts.append(vat_info)
        entry_note = " | ".join(p for p in note_parts if p)

        entry: dict[str, Any] = {
            "FMaterialId": {"FNumber": sku_code},
            "FQty": float(item.quantity or 1),
            "FEntryNote": entry_note,
        }

        # 填充价格
        if order.order_type == "STOCK_REPLENISHMENT":
            unit_price = _get_unit_price(session, sku_code, entity_code)
            if unit_price is not None:
                entry["FPrice"] = unit_price
        else:
            if item.unit_price is not None:
                entry["FPrice"] = float(item.unit_price)

        entries.append(entry)

    return entries


# ── 新增辅助函数 ──

def _resolve_warehouse(session: Session, order: MiddlePlatformOrder) -> str:
    """从订单关联的发货通知中获取发货仓库"""
    from backend.app.models import DeliveryNotice
    try:
        notice = session.query(DeliveryNotice).filter(
            DeliveryNotice.order_id == order.id,
        ).order_by(DeliveryNotice.created_at.desc()).first()
        return notice.warehouse_code or "" if notice else ""
    except Exception:
        return ""


def _extract_vat(order: MiddlePlatformOrder) -> str:
    """从 CRM 原始数据提取 VAT 信息
    CRM 备注或 raw_json 中可能包含 VAT: ATU72029707 格式
    """
    import re
    if not order.crm_order:
        return ""
    raw = order.crm_order.raw_json or "{}"
    if not raw or raw == "{}":
        return ""
    # 尝试从 raw_json 中提取 VAT
    vat_match = re.search(r'VAT[:\s]*([A-Z0-9]+)', raw, re.IGNORECASE)
    if vat_match:
        return f"VAT:{vat_match.group(1)}"
    # 从备注字段提取
    remark = order.crm_order.remark or ""
    if remark:
        vat_match = re.search(r'VAT[:\s]*([A-Z0-9]+)', remark, re.IGNORECASE)
        if vat_match:
            return f"VAT:{vat_match.group(1)}"
    return ""


def _resolve_recipient(session: Session, order: MiddlePlatformOrder) -> tuple[str, str, str]:
    """获取单一收货人的姓名、电话、地址"""
    if not order.crm_order:
        return ("", "", "")
    return (
        order.crm_order.receipt_contact or "",
        order.crm_order.receipt_phone or "",
        order.crm_order.receipt_address or "",
    )


def _has_multi_recipients(session: Session, order: MiddlePlatformOrder) -> bool:
    """判断是否有多个收货人（附件中有多行收货信息）"""
    if not order.crm_order:
        return False
    raw = order.crm_order.raw_json or "{}"
    # 简单判断：附件中有多个不同的收货地址信息
    import re
    contacts = re.findall(r'(?:收货人|收件人|联系人)[：:]\s*([^\s，,]+)', raw)
    return len(set(contacts)) > 1 if contacts else False
