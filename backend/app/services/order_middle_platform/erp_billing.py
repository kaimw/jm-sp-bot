"""order_middle_platform — erp_billing"""
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
from backend.app.services.order_middle_platform.delivery import latest_delivery_notice
from backend.app.services.order_middle_platform.enums import ExceptionType
from backend.app.services.order_middle_platform.enums import OrderEvent
from backend.app.services.order_middle_platform.enums import OrderStatus
from backend.app.services.order_middle_platform.enums import transition_order
from backend.app.services.order_middle_platform.notifications import create_exception_case

def _erp_config_ready(session: Session) -> bool:
    """检查金蝶写入配置是否就绪"""
    enabled = session.get(SystemConfig, "erp_write_enabled")
    if not enabled or enabled.value != "true":
        return False
    try:
        kingdee_config_from_session(session)
        return True
    except KingdeeConfigError:
        return False




def process_erp_billing(session: Session, order: MiddlePlatformOrder, *, trace_id: str = "") -> dict[str, Any]:
    """执行 ERP 制单全流程：Save → Submit → Audit

    预审通过后调用。备货→武汉仓跳过此流程。
    """
    if should_skip_erp_billing(order):
        transition_order(session, order, OrderEvent.DELIVERY_NOTICE_CREATED, trace_id=trace_id)
        return {"order_id": order.id, "order_no": order.order_no, "status": order.status, "erp_skipped": True}

    if not _erp_config_ready(session):
        transition_order(session, order, OrderEvent.ERP_SAVE_FAILED, trace_id=trace_id,
                         detail={"reason": "金蝶写入未配置", "error_type": "KingdeeConfigError"})
        create_exception_case(session, order, ExceptionType.OMS_BLOCKED, "High",
                              "ERP 制单失败：金蝶写入未配置", [], trace_id=trace_id)
        return {"order_id": order.id, "order_no": order.order_no, "status": order.status, "erp_success": False, "error": "金蝶写入未配置"}

    # 预审通过后分配订单号 (若已分配则不再重新分配)
    if not order.order_no:
        order.order_no = generate_middle_order_no(session)
    transition_order(session, order, OrderEvent.ERP_SAVE_STARTED, trace_id=trace_id)
    session.flush()

    try:
        config = kingdee_config_from_session(session)
        client = KingdeeClient(config)

        # ── 前置幂等性防重 Check ──
        # 在真正向金蝶提交制单前，先通过当前中台订单号 (order_no) 查询金蝶备注中是否已生成过该订单
        try:
            query_result = client.execute_bill_query(
                form_id="SAL_SaleOrder",
                field_keys="FID,FBillNo",
                filter_string=f"FNote LIKE '%{order.order_no}%' AND FBillNo NOT LIKE 'MP-%'",
                limit=1,
            )
            existing_items = normalize_query_rows(query_result.get("raw"))
            if existing_items and len(existing_items) > 0 and len(existing_items[0]) > 0:
                bill_id = existing_items[0][0]
                erp_bill_no = existing_items[0][1]

                # 更新本地订单信息并执行状态机跃迁
                order.erp_bill_no = erp_bill_no
                transition_order(session, order, OrderEvent.ERP_SAVE_SUCCESS, trace_id=trace_id,
                                 detail={"erp_bill_no": erp_bill_no, "note": "金蝶已存在该订单，直接建立关联"})
                return {"order_id": order.id, "order_no": order.order_no, "status": order.status, "erp_success": True, "erp_bill_no": erp_bill_no}
        except Exception as check_exc:
            logger.warning("金蝶制单前置幂等性 Check 异常（忽略并继续常规制单流程）: %s", check_exc)

        # Step 1: Save
        bill_model = build_sales_order_model(session, order, order.items)
        save_result = client.save_bill(
            form_id="SAL_SaleOrder",
            model=bill_model,
            need_return_fields=["FBillNo", "FDate"],
        )
        if not save_result.get("ok"):
            error_msg = save_result.get("message") or "金蝶 Save 失败"
            result = save_result.get("result")
            if isinstance(result, dict):
                rs = result.get("ResponseStatus")
                if isinstance(rs, dict):
                    errors = rs.get("Errors", [])
                    if isinstance(errors, list):
                        details = [str(e.get("Message", "") or e.get("FieldName", "") or e) for e in errors if isinstance(e, dict)]
                        if details:
                            error_msg = f"{error_msg} | {'; '.join(details)}"
            transition_order(session, order, OrderEvent.ERP_SAVE_FAILED, trace_id=trace_id,
                             detail={"step": "save", "error": error_msg})
            create_exception_case(session, order, ExceptionType.OMS_BLOCKED, "High",
                                  f"ERP 制单失败(Save)：{error_msg}", [], trace_id=trace_id)
            return {"order_id": order.id, "order_no": order.order_no, "status": order.status, "erp_success": False, "error": error_msg}

        # 提取金蝶 FBillNo 和 FID
        erp_bill_no = None
        bill_id = None
        rd = save_result.get("result")
        if isinstance(rd, dict):
            erp_bill_no = rd.get("Number")
            bill_id = rd.get("Id")

            # 标准金蝶 WebAPI Save 返回结构中，单号和内码保存在 SuccessEntitys
            rs = rd.get("ResponseStatus")
            if isinstance(rs, dict):
                entities = rs.get("SuccessEntitys")
                if isinstance(entities, list) and len(entities) > 0 and isinstance(entities[0], dict):
                    entity = entities[0]
                    if not erp_bill_no:
                        erp_bill_no = entity.get("Number")
                    if not bill_id:
                        bill_id = entity.get("Id")

        if erp_bill_no:
            order.erp_bill_no = erp_bill_no

        if not bill_id:
            error_msg = "未能在金蝶制单返回数据中解析到单据内码 (FID)"
            transition_order(session, order, OrderEvent.ERP_SAVE_FAILED, trace_id=trace_id,
                             detail={"step": "save", "error": error_msg})
            create_exception_case(session, order, ExceptionType.OMS_BLOCKED, "High",
                                  f"ERP 制单失败(Save)：{error_msg}", [], trace_id=trace_id)
            return {"order_id": order.id, "order_no": order.order_no, "status": order.status, "erp_success": False, "error": error_msg}

        # Step 2: Submit
        if bill_id:
            sub_result = client.submit_bill(form_id="SAL_SaleOrder", bill_ids=[bill_id])
            if not sub_result.get("ok"):
                error_msg = sub_result.get("message") or "金蝶 Submit 失败"
                transition_order(session, order, OrderEvent.ERP_SUBMIT_FAILED, trace_id=trace_id,
                                 detail={"step": "submit", "error": error_msg, "bill_id": bill_id})
                create_exception_case(session, order, ExceptionType.OMS_BLOCKED, "High",
                                      f"ERP 制单失败(Submit)：{error_msg}", [], trace_id=trace_id)
                return {"order_id": order.id, "order_no": order.order_no, "status": order.status, "erp_success": False, "error": error_msg}



        # 全部成功
        transition_order(session, order, OrderEvent.ERP_SAVE_SUCCESS, trace_id=trace_id,
                         detail={"erp_bill_no": erp_bill_no})

        # 触发发货通知邮件（一期核心：ERP 制单成功后自动通知物流）
        try:
            warehouse_code = ""
            notice = latest_delivery_notice(session, order)
            if notice and notice.warehouse_code:
                warehouse_code = notice.warehouse_code
            enqueue_delivery_notice_mail(
                session, order, order.items,
                warehouse=warehouse_code,
                special_requirements=None,
            )
        except Exception as mail_exc:
            logger.warning("发送发货通知邮件失败（不影响主流程）: %s", mail_exc)

        return {"order_id": order.id, "order_no": order.order_no, "status": order.status, "erp_success": True, "erp_bill_no": erp_bill_no}

    except KingdeeConfigError as exc:
        error_msg = f"金蝶配置错误：{exc}"
        transition_order(session, order, OrderEvent.ERP_SAVE_FAILED, trace_id=trace_id,
                         detail={"step": "config", "error": error_msg})
        create_exception_case(session, order, ExceptionType.OMS_BLOCKED, "High", error_msg, [], trace_id=trace_id)
        return {"order_id": order.id, "order_no": order.order_no, "status": order.status, "erp_success": False, "error": error_msg}
    except Exception as exc:
        error_msg = f"ERP 制单异常：{exc}"
        transition_order(session, order, OrderEvent.ERP_SAVE_FAILED, trace_id=trace_id,
                         detail={"step": "exception", "error": error_msg})
        create_exception_case(session, order, ExceptionType.OMS_BLOCKED, "High", error_msg, [], trace_id=trace_id)
        return {"order_id": order.id, "order_no": order.order_no, "status": order.status, "erp_success": False, "error": error_msg}




def retry_erp_billing(session: Session, order: MiddlePlatformOrder, *, trace_id: str = "") -> dict[str, Any]:
    """重试 ERP 制单（从 ERP_FAILED → ERP_PENDING → 重新制单）"""
    if order.status != OrderStatus.ERP_FAILED.value:
        return {"order_id": order.id, "error": f"当前状态不允许重试：{order.status}"}
    transition_order(session, order, OrderEvent.EXCEPTION_RESOLVED_AND_RE_ERP, trace_id=trace_id)
    return process_erp_billing(session, order, trace_id=trace_id)




