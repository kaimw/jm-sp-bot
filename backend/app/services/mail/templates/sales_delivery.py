"""销售订单发货通知邮件模板（国内仓/海外仓）

依据 7 个邮件示例综合整理。
"""
from __future__ import annotations

from typing import Any

from backend.app.models import MiddlePlatformOrder, MiddlePlatformOrderItem


def render_sales_delivery_mail(
    order: MiddlePlatformOrder,
    items: list[MiddlePlatformOrderItem],
    to_emails: list[str],
    cc_emails: list[str] | None = None,
    *,
    warehouse: str = "",
    erp_bill_no: str = "",
    special_requirements: list[str] | None = None,
    sales_name: str = "",
    customer_name: str = "",
    order_date: str = "",
    purchase_order_no: str = "",
    recipients: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """渲染销售订单发货通知邮件

    参数：
      order: 中台订单
      items: 订单明细
      to_emails: 收件人列表
      warehouse: 发货仓库（如 武汉仓 / 欧洲仓）
      erp_bill_no: 金蝶销售单号（国内仓必带，海外仓不带）
      special_requirements: 特殊需求列表（红色加粗）
      sales_name: 销售姓名
      customer_name: 客户名称
      recipients: 多收货人时每行的收货人信息
    """
    is_domestic = "武汉" in warehouse or "国内" in warehouse
    is_overseas = not is_domestic

    subject = _build_subject(customer_name, order_date, purchase_order_no, erp_bill_no, is_domestic, order.crm_order_no)
    body = _build_body(sales_name, customer_name, items, warehouse, special_requirements, recipients, is_overseas, erp_bill_no)

    return {
        "to": to_emails,
        "cc": cc_emails or [],
        "subject": subject,
        "body": body,
        "mail_type": "sales_delivery",
    }


def _build_subject(customer: str, date_str: str, po_no: str, erp_no: str, is_domestic: bool, crm_order_no: str) -> str:
    if is_domestic:
        return f"采购订单-JM-CGDD-{po_no or 'XXXX'}（{customer}）{date_str}【销售编号：{erp_no}】"
    else:
        return f"{customer}，销售订单{date_str}-{crm_order_no}，【销售编号：{erp_no}】"


def _build_body(
    sales_name: str,
    customer: str,
    items: list[MiddlePlatformOrderItem],
    warehouse: str,
    special_requirements: list[str] | None,
    recipients: list[dict[str, Any]] | None,
    is_overseas: bool,
    erp_bill_no: str,
) -> str:
    lines: list[str] = []

    # 称呼
    lines.append(f"{'单主管' if is_overseas else '张主管'}，你好！\n")

    # 销售 + 客户
    if sales_name:
        lines.append(f"现有{sales_name}的{customer}有以下设备需要出货，麻烦尽快安排，谢谢！\n")
    else:
        lines.append(f"现有{customer}有以下设备需要出货，麻烦尽快安排，谢谢！\n")

    # 特殊需求（红色加粗）
    if special_requirements:
        for req in special_requirements:
            lines.append(f"  **{req}**\n")

    # 物料表
    has_multi_recipients = recipients and len(recipients) > 1
    if has_multi_recipients:
        lines.append("物料编号丨物料名称丨规格型号丨采购数量丨单位丨收件人\n")
    else:
        lines.append("物料编号丨物料名称丨规格型号丨采购数量丨单位\n")
    lines.append("-" * 60 + "\n")
    for item in items:
        sku = item.sku_code or ""
        name = item.product_name or ""
        qty = int(item.quantity or 1)
        if has_multi_recipients and recipients:
            # 按收货人分组展示
            for rcp in recipients:
                lines.append(f"{sku}丨{name}丨{qty}丨台丨{rcp.get('contact', '')}\n")
        else:
            lines.append(f"{sku}丨{name}丨{qty}丨台\n")

    lines.append("\n")

    # 发货仓标注
    if is_overseas:
        lines.append(f"  （{warehouse}发货）\n")
    if special_requirements:
        lines.append("\n")

    # 收货信息
    if recipients:
        for i, rcp in enumerate(recipients):
            lines.append(f"  收货信息{i + 1}：\n")
            lines.append(f"    收货人：{rcp.get('contact', '')}\n")
            lines.append(f"    电话：{rcp.get('phone', '')}\n")
            if rcp.get('email'):
                lines.append(f"    邮箱：{rcp.get('email', '')}\n")
            lines.append(f"    地址：{rcp.get('address', '')}\n\n")

    return "".join(lines)
