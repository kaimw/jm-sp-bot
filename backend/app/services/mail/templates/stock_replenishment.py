"""备货订单发货通知邮件模板

备货邮件不含收货人信息，武汉仓备货不带金蝶销售编号。
"""
from __future__ import annotations

from typing import Any

from backend.app.models import MiddlePlatformOrder, MiddlePlatformOrderItem


def render_replenishment_mail(
    order: MiddlePlatformOrder,
    items: list[MiddlePlatformOrderItem],
    to_emails: list[str],
    cc_emails: list[str] | None = None,
    *,
    warehouse: str = "",
    erp_bill_no: str = "",
    special_requirements: list[str] | None = None,
    demand_desc: str = "",
    purchase_order_no: str = "",
    order_date: str = "",
) -> dict[str, Any]:
    """渲染备货订单发货通知邮件

    武汉仓备货 → 不带销售编号，发给仓管
    海外仓备货 → 带销售编号，发给物流主管
    """
    is_domestic = "武汉" in warehouse

    subject = _build_subject(demand_desc, order_date, purchase_order_no, erp_bill_no, is_domestic)
    body = _build_body(demand_desc, items, warehouse, special_requirements, is_domestic)

    return {
        "to": to_emails,
        "cc": cc_emails or [],
        "subject": subject,
        "body": body,
        "mail_type": "stock_replenishment",
    }


def _build_subject(demand_desc: str, date_str: str, po_no: str, erp_no: str, is_domestic: bool) -> str:
    if is_domestic:
        return f"采购订单-JM-CGDD-{po_no or 'XXXX'}（{demand_desc}）{date_str}【备注：武汉仓备货】"
    else:
        return f"采购订单-JM-CGDD-{po_no or 'XXXX'}（{demand_desc}）{date_str}【销售编号：{erp_no}】"


def _build_body(
    demand_desc: str,
    items: list[MiddlePlatformOrderItem],
    warehouse: str,
    special_requirements: list[str] | None,
    is_domestic: bool,
) -> str:
    lines: list[str] = []

    lines.append(f"{'单主管' if not is_domestic else '张主管'}，你好！\n")
    lines.append(f"现有{demand_desc}有以下设备需要备货出货，麻烦尽快安排，谢谢！\n")

    if special_requirements:
        for req in special_requirements:
            lines.append(f"  **{req}**\n")

    lines.append("\n物料编号丨物料名称丨规格型号丨采购数量丨单位\n")
    lines.append("-" * 60 + "\n")
    for item in items:
        sku = item.sku_code or ""
        name = item.product_name or ""
        qty = int(item.quantity or 1)
        lines.append(f"{sku}丨{name}丨{qty}丨台\n")

    lines.append("\n")
    if not is_domestic:
        lines.append(f"  （{warehouse}发货）\n")

    return "".join(lines)
