"""发货通知邮件模板服务

负责在 ERP 制单成功后，根据订单类型（销售/备货）和发货仓库，
选择对应模板，生成 OutboundMailJob 加入外发队列。
"""

from __future__ import annotations

import json
import uuid
from typing import Any

from sqlalchemy.orm import Session

from backend.app.models import (
    MailReceiverConfig,
    MiddlePlatformOrder,
    MiddlePlatformOrderItem,
    OutboundMailJob,
    now_utc,
)
from backend.app.services.mail.templates.sales_delivery import render_sales_delivery_mail
from backend.app.services.mail.templates.stock_replenishment import render_replenishment_mail


def enqueue_delivery_notice_mail(
    session: Session,
    order: MiddlePlatformOrder,
    items: list[MiddlePlatformOrderItem],
    *,
    warehouse: str = "",
    special_requirements: list[str] | None = None,
    recipients: list[dict[str, Any]] | None = None,
) -> OutboundMailJob | None:
    """根据订单类型和仓库，生成发货通知邮件并加入外发队列

    在 ERP_SAVED 后触发。
    """
    # 确定收件人
    scene = _resolve_scene(order, warehouse)
    to_emails, cc_emails = _get_receivers(session, scene)

    if not to_emails:
        return None

    # 准备模板参数
    is_domestic = "武汉" in warehouse
    erp_bill_no = order.erp_bill_no or ""
    customer = order.customer_name or ""
    sales_name = order.sales_user_name or ""
    date_str = _today_str()

    if not recipients and order.crm_order:
        recipients = [{
            "contact": order.crm_order.receipt_contact or "",
            "phone": order.crm_order.receipt_phone or "",
            "address": order.crm_order.receipt_address or ""
        }]

    # 选择模板
    if order.order_type == "STOCK_REPLENISHMENT":
        mail_data = render_replenishment_mail(
            order=order,
            items=items,
            to_emails=to_emails,
            cc_emails=cc_emails,
            warehouse=warehouse,
            erp_bill_no="" if is_domestic and order.order_type == "STOCK_REPLENISHMENT" else erp_bill_no,
            special_requirements=special_requirements,
            demand_desc=customer,
            order_date=date_str,
        )
    else:
        mail_data = render_sales_delivery_mail(
            order=order,
            items=items,
            to_emails=to_emails,
            cc_emails=cc_emails,
            warehouse=warehouse,
            erp_bill_no=erp_bill_no if is_domestic else erp_bill_no,
            special_requirements=special_requirements,
            sales_name=sales_name,
            customer_name=customer,
            order_date=date_str,
            recipients=recipients,
        )

    # 去重键
    idempotency_key = f"delivery-notice-{order.order_no}-{order.version}"

    # 检查是否已存在
    existing = session.query(OutboundMailJob).filter(OutboundMailJob.idempotency_key == idempotency_key).first()
    if existing is not None:
        return existing

    # 创建外发任务
    job = OutboundMailJob(
        mail_type=mail_data["mail_type"],
        to_json=json.dumps(mail_data["to"], ensure_ascii=False),
        cc_json=json.dumps(mail_data["cc"], ensure_ascii=False),
        subject=mail_data["subject"],
        body=mail_data["body"],
        idempotency_key=idempotency_key,
        status="Pending",
        priority=20,
    )
    session.add(job)
    session.flush()
    return job


def _resolve_scene(order: MiddlePlatformOrder, warehouse: str) -> str:
    """根据订单类型和仓库确定场景编码"""
    is_replenishment = order.order_type == "STOCK_REPLENISHMENT"
    is_domestic = "武汉" in warehouse
    if is_replenishment:
        return "replenishment_domestic" if is_domestic else "replenishment_overseas"
    return "domestic_delivery" if is_domestic else "overseas_delivery"


def _get_receivers(session: Session, scene: str) -> tuple[list[str], list[str]]:
    """从 MailReceiverConfig 中获取收件人配置"""
    config = session.query(MailReceiverConfig).filter(
        MailReceiverConfig.scene == scene,
        MailReceiverConfig.is_active == True,
    ).first()
    if config is None:
        return [], []
    to_list: list[str] = json.loads(config.to_json) if config.to_json else []
    cc_list: list[str] = json.loads(config.cc_json) if config.cc_json else []
    return to_list, cc_list


def _today_str() -> str:
    from datetime import date
    return date.today().strftime("%m.%d")
