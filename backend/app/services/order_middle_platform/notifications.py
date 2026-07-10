"""order_middle_platform — notifications"""
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
from backend.app.services.order_middle_platform.serializers import order_dashboard

def create_exception_case(
    session: Session,
    order: MiddlePlatformOrder,
    exception_type: ExceptionType,
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
    session.add(AuditEvent(event_type="ExceptionCaseCreated", related_object_type="MiddlePlatformOrder", related_object_id=order.id, detail=dumps({"exception_type": exception_type.value, "trace_id": trace_id})))
    return case




def exception_due_at(severity: str) -> datetime:
    hours_by_severity = {
        "Critical": 4,
        "High": 24,
        "Medium": 72,
        "Low": 168,
    }
    return now_utc() + timedelta(hours=hours_by_severity.get(str(severity or "Medium"), 72))




def build_context_pack(
    session: Session,
    order: MiddlePlatformOrder,
    exception_type: ExceptionType,
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
            "type": exception_type.value,
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
    to_addresses, cc_addresses = validation_failure_recipients_for_order(session, order)
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
    digest_source = "|".join(
        [
            order.order_no,
            order.crm_order_no or "",
            ";".join(f"{result.rule_code}:{result.reason}" for result in failed),
        ]
    )
    idempotency_key = f"v2-validation-failed:{hashlib.sha256(digest_source.encode('utf-8')).hexdigest()}"
    subject = f"[订单预审未通过][{order.crm_order_no or order.order_no}] {order.customer_name or ''}".strip()
    body = build_validation_failure_mail_body(session, order, failed, exception_case)
    existing = session.query(OutboundMailJob).filter(OutboundMailJob.idempotency_key == idempotency_key).first()
    if existing is not None:
        cancel_stale_pending_validation_failure_notifications(session, order, keep_idempotency_key=idempotency_key, trace_id=trace_id)
        if existing.status in {"Cancelled", "Failed"}:
            previous_status = existing.status
            existing.to_json = dumps(to_addresses)
            existing.cc_json = dumps(cc_addresses)
            existing.subject = subject
            existing.body = body
            existing.status = "Pending"
            existing.attempt_count = 0
            existing.next_retry_at = None
            existing.last_error = None
            existing.locked_by = None
            existing.locked_until = None
            existing.sending_started_at = None
            existing.sent_at = None
            existing.priority = 20
            session.add(
                AuditEvent(
                    event_type="ValidationFailureNotificationRequeued",
                    related_object_type="MiddlePlatformOrder",
                    related_object_id=order.id,
                    detail=dumps({"to": to_addresses, "cc": cc_addresses, "exception_case_id": exception_case.id, "trace_id": trace_id, "previous_status": previous_status}),
                )
            )
        return existing

    cancel_stale_pending_validation_failure_notifications(session, order, keep_idempotency_key=idempotency_key, trace_id=trace_id)
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




def cancel_stale_pending_validation_failure_notifications(
    session: Session,
    order: MiddlePlatformOrder,
    *,
    keep_idempotency_key: str,
    trace_id: str = "",
) -> int:
    subject_prefix = f"[订单预审未通过][{order.crm_order_no or order.order_no}]"
    stale_jobs = (
        session.query(OutboundMailJob)
        .filter(
            OutboundMailJob.mail_type == "V2ValidationFailed",
            OutboundMailJob.status == "Pending",
            OutboundMailJob.subject.ilike(f"{subject_prefix}%"),
            OutboundMailJob.idempotency_key != keep_idempotency_key,
        )
        .all()
    )
    for job in stale_jobs:
        job.status = "Cancelled"
        job.last_error = "superseded by newer validation failure notification"
    if stale_jobs:
        session.add(
            AuditEvent(
                event_type="ValidationFailureNotificationSuperseded",
                related_object_type="MiddlePlatformOrder",
                related_object_id=order.id,
                detail=dumps({"cancelled_count": len(stale_jobs), "trace_id": trace_id}),
            )
        )
    return len(stale_jobs)




def validation_failure_recipients(session: Session) -> tuple[list[str], list[str]]:
    configured_to = config_list(session, "v2_validation_failure_to_json", [])
    configured_cc = config_list(session, "v2_validation_failure_cc_json", [])
    ops = config_value(session, "ops_cc_email", "").strip()
    ceo = config_value(session, "ceo_email", "").strip()
    to_addresses = configured_to or ([ops] if ops else ([ceo] if ceo else []))
    cc_addresses = configured_cc or ([ceo] if ceo and ceo not in to_addresses else [])
    return unique_emails(to_addresses), unique_emails(cc_addresses)




def validation_failure_recipients_for_order(session: Session, order: MiddlePlatformOrder) -> tuple[list[str], list[str]]:
    sales_email = ""
    if order.crm_order is not None:
        sales_email = str(order.crm_order.sales_user_email or "").strip()
    if not sales_email:
        sales_email = str(getattr(order, "sales_user_email", "") or "").strip()
    sales_to = unique_emails([sales_email])
    if not sales_to:
        system_owner_to = unique_emails([config_value(session, "crm_system_owner_email", "").strip()])
        if system_owner_to:
            return system_owner_to, []
        configured_to, configured_cc = validation_failure_recipients(session)
        return configured_to, configured_cc
    return sales_to, []




def exception_notify_recipients(session: Session, exception_type: ExceptionType) -> tuple[list[str], list[str]]:
    """按异常类型查找通知邮箱配置。
    优先级: 类型专属(v2_exception_{type}_to_json) > 大类(v2_exception_crm/oms_to_json) > 通用兜底
    """
    type_key = exception_type.value.lower()
    to_list = config_list(session, f"v2_exception_{type_key}_to_json", [])
    cc_list = config_list(session, f"v2_exception_{type_key}_cc_json", [])
    if to_list or cc_list:
        return unique_emails(to_list), unique_emails(cc_list)
    # 大类回退
    if exception_type.value.startswith("CRM_"):
        to_list = config_list(session, "v2_exception_crm_to_json", [])
        cc_list = config_list(session, "v2_exception_crm_cc_json", [])
    elif exception_type.value.startswith("OMS_"):
        to_list = config_list(session, "v2_exception_oms_to_json", [])
        cc_list = config_list(session, "v2_exception_oms_cc_json", [])
    if to_list or cc_list:
        return unique_emails(to_list), unique_emails(cc_list)
    return validation_failure_recipients(session)




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
        f"预审时间：{format_beijing_time(exception_case.created_at or now_utc(), include_seconds=True)}（北京时间）",
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
        "需处理事项：",
    ])
    item_index = 1
    for result in failed:
        formatted = format_validation_result_for_mail(order, result, item_index)
        if formatted:
            lines.extend(formatted)
            item_index += 1
    lines.extend(
        [
            "",
            "处理建议：",
            "- 请按上方事项分别补齐 CRM 字段、客户映射、商品/SKU 主数据、库存或附件资料。",
            "- 处理完成后重新同步该订单，系统会自动重新预审。",
            "",
            config_value(session, "bot_signature", "积木易搭AI机器人"),
        ]
    )
    return "\n".join(lines)


PHASE_ONE_FIELD_LABELS = {
    "sales_user_name": "销售负责人",
    "sales_user_email": "销售邮箱",
    "owner_department": "归属部门",
    "order_date": "订单日期",
    "settlement_method": "结算方式",
    "receipt_contact": "收货联系人",
    "receipt_phone": "收货联系电话",
    "receipt_address": "收货地址",
    "currency": "币种",
    "attachment_files": "关键附件",
    "order_items": "订单商品明细",
}




def format_validation_result_for_mail(order: MiddlePlatformOrder, result: ValidationResult, index: int) -> list[str]:
    if result.rule_code == "ATTACHMENT_PRODUCT_CONSISTENCY":
        return format_attachment_consistency_result_for_mail(result, index)
    if result.rule_code == "PHASE1_COMPLETE_PRE_REVIEW_FIELDS":
        return format_phase_one_result_for_mail(order, result, index)
    if result.rule_code in {"KNOWN_ACTIVE_SKU", "SKU_MAPPING_MISSING"}:
        return format_sku_result_for_mail(result, index)
    if result.rule_code == "CUSTOMER_MAPPING":
        return [
            f"{index}. 客户映射",
            f"   当前值：{order.customer_name or '未填写'}",
            "   不通过原因：系统未找到该客户对应的中台/OMS 客户映射。",
            "   处理要求：请维护客户映射，或确认 OMS 客户资料是否已建立。",
        ]
    if result.rule_code == "HAS_ORDER_ITEMS":
        return [
            f"{index}. 商品明细",
            "   当前值：未解析到商品明细",
            "   不通过原因：CRM 订单没有解析到任何明细行。",
            "   处理要求：请确认 CRM 订单中已填写商品、规格、数量等信息。",
        ]
    return [
        f"{index}. {validation_result_title(result)}",
        f"   当前值：{validation_current_value(result)}",
        f"   不通过原因：{clean_validation_text(result.reason)}",
        "   处理要求：请按原因修正后重新同步该订单。",
    ]




def format_phase_one_result_for_mail(order: MiddlePlatformOrder, result: ValidationResult, index: int) -> list[str]:
    crm = order.crm_order
    raw = loads(crm.raw_json, {}) if crm else {}
    lines = [f"{index}. 一期完整性预审"]
    sub_index = 1
    refs = result.evidence_refs or []
    for ref in refs:
        field_match = re.fullmatch(r"(.+?)\(([\w_]+)\)", ref.strip())
        if field_match:
            raw_label, field = field_match.groups()
            label = PHASE_ONE_FIELD_LABELS.get(field, raw_label)
            lines.extend(
                [
                    f"   {sub_index}) {label}",
                    "      当前值：未填写",
                    f"      不通过原因：{label}缺失。",
                    f"      处理要求：请在 CRM 订单中补充{label}。",
                ]
            )
            sub_index += 1
            continue
        if ref.startswith("收货地址不是可邮寄详细地址："):
            value = ref.split("：", 1)[1].strip()
            lines.extend(
                [
                    f"   {sub_index}) 收货地址",
                    f"      当前值：{value or '未填写'}",
                    "      不通过原因：收货地址不是可邮寄的详细地址。",
                    "      处理要求：请补充省市区、街道、门牌号等完整地址。",
                ]
            )
            sub_index += 1
            continue
        if ref.startswith("CRM 审批状态未通过："):
            value = ref.split("：", 1)[1].strip()
            lines.extend(
                [
                    f"   {sub_index}) CRM 审批状态",
                    f"      当前值：{value or '未填写'}",
                    "      不通过原因：CRM 审批状态未达到可履约条件。",
                    "      处理要求：请完成 CRM 审批后重新同步。",
                ]
            )
            sub_index += 1
            continue
        if ref.startswith("CRM 订单生命状态异常："):
            value = ref.split("：", 1)[1].strip()
            lines.extend(
                [
                    f"   {sub_index}) CRM 订单生命状态",
                    f"      当前值：{value or '未填写'}",
                    "      不通过原因：CRM 订单当前生命状态不允许继续履约。",
                    "      处理要求：请确认订单状态已恢复为正常/有效后重新同步。",
                ]
            )
            sub_index += 1
            continue
        if ref == "附件未识别到盖章/签字 PO 或盖章/签字合同":
            attachments = loads(crm.attachment_files_json, []) if crm else []
            value = "、".join(str(item) for item in attachments) if isinstance(attachments, list) else str(attachments or "")
            lines.extend(
                [
                    f"   {sub_index}) 关键附件",
                    f"      当前值：{value or '未上传'}",
                    "      不通过原因：附件中未识别到盖章/签字 PO 或盖章/签字合同。",
                    "      处理要求：请补充有效盖章/签字文件，或确认附件解析结果。",
                ]
            )
            sub_index += 1
            continue
        if raw and "life_status" in str(ref):
            value = str(raw.get("life_status") or "").strip()
            lines.extend(
                [
                    f"   {sub_index}) CRM 订单生命状态",
                    f"      当前值：{value or '未填写'}",
                    "      不通过原因：CRM 订单当前生命状态不允许继续履约。",
                    "      处理要求：请确认订单状态已恢复为正常/有效后重新同步。",
                ]
            )
            sub_index += 1
            continue
        lines.extend(
            [
                f"   {sub_index}) 基础资料",
                f"      当前值：{validation_current_value(result)}",
                f"      不通过原因：{clean_validation_text(ref)}",
                "      处理要求：请按原因补充或修正 CRM 订单资料。",
            ]
        )
        sub_index += 1
    if sub_index == 1:
        lines.extend(
            [
                "   当前值：资料不完整或不符合预审要求",
                f"   不通过原因：{clean_validation_text(result.reason)}",
                "   处理要求：请补齐 CRM 订单基础资料后重新同步。",
            ]
        )
    return lines




def format_sku_result_for_mail(result: ValidationResult, index: int) -> list[str]:
    issues = sku_match_issues(result)
    lines = [f"{index}. 商品/SKU 匹配问题"]
    if not issues:
        current_value = "未匹配到标准 SKU"
        if result.rule_code == "SKU_MAPPING_MISSING":
            current_value = "渠道 SKU 未匹配中台标准 SKU"
        lines.extend(
            [
                f"   当前值：{current_value}",
                f"   不通过原因：{clean_validation_text(result.reason)}",
                "   处理要求：请维护标准 SKU、商品别名或渠道 SKU 映射。",
            ]
        )
        return lines
    for item_index, issue in enumerate(issues, start=1):
        product_name = str(issue.get("product_name") or "").strip() or "未填写"
        reason = str(issue.get("reason") or "").strip()
        candidates = issue.get("candidates") if isinstance(issue.get("candidates"), list) else []
        lines.extend(
            [
                f"   {item_index}. CRM 商品：{product_name}",
                f"      当前值：{sku_current_value(reason)}",
                f"      不通过原因：{sku_failure_reason(reason)}",
                "      处理要求：请补充型号、版本、套装内容或标准 SKU 编码。",
            ]
        )
        if candidates:
            lines.extend(["", "      可能匹配项（按相似度排序）："])
            for candidate_index, candidate in enumerate(sorted_sku_candidates(candidates), start=1):
                sku_id = str(candidate.get("sku_id") or "-").strip()
                name = str(candidate.get("product_name") or candidate.get("matched_value") or candidate.get("spu_id") or "-").strip()
                confidence = int(candidate.get("confidence") or 0)
                lines.append(f"      {candidate_index}）{sku_id}｜{name}｜相似度 {confidence}%")
        else:
            lines.extend(["", "      可能匹配项（按相似度排序）：暂无高置信度候选项"])
    return lines




def format_attachment_consistency_result_for_mail(result: ValidationResult, index: int) -> list[str]:
    issue_text = result.reason
    prefix = "CRM 订单产品与附件解析内容不一致："
    if issue_text.startswith(prefix):
        issue_text = issue_text[len(prefix):]
    rows = []
    for raw_issue in [part.strip() for part in re.split(r"[；;]", issue_text) if part.strip()]:
        rows.append(_attachment_consistency_issue_row(raw_issue))
    lines = [
        f"{index}. 附件商品一致性",
        "   当前值：CRM 订单产品与附件解析内容不一致",
        "   不通过原因：CRM 订单产品与附件解析内容不一致",
        "   处理要求：请核对 CRM 商品明细和附件中的产品、数量、单价、金额。",
    ]
    if not rows:
        return lines
    lines.extend(
        [
            "  对比明细：",
            "  | 商品 | 对比项 | CRM | 附件 | 结论 |",
            "  | --- | --- | --- | --- | --- |",
        ]
    )
    for row in rows:
        lines.append(
            "  | "
            + " | ".join(
                _mail_table_cell(row[key])
                for key in ["product", "field", "crm", "attachment", "conclusion"]
            )
            + " |"
        )
    return lines




def validation_result_title(result: ValidationResult) -> str:
    names = {
        "REQUIRED_HEAD_FIELDS": "订单头基础字段",
        "POSITIVE_ORDER_AMOUNT": "订单金额",
        "AMOUNT_CONSISTENCY": "金额一致性",
        "RULE_SKU_BOM_MATCH": "SKU/BOM 匹配",
        "RULE_CONTRACT_AMOUNT_CONSISTENCY": "合同金额一致性",
        "LOCAL_INVENTORY_AVAILABLE": "本地库存",
    }
    return names.get(result.rule_code, "预审检查")




def validation_current_value(result: ValidationResult) -> str:
    refs = [clean_validation_text(ref) for ref in result.evidence_refs or [] if str(ref or "").strip()]
    return "；".join(refs[:3]) if refs else "见不通过原因"




def clean_validation_text(value: str) -> str:
    text = str(value or "").strip()
    text = re.sub(r"\(([a-zA-Z_][a-zA-Z0-9_]*)(?:[,，]\s*[a-zA-Z_][a-zA-Z0-9_]*)*\)", "", text)
    text = text.replace("CRM.customer_name=", "客户名称：")
    text = text.replace("OMS.customer.query=", "OMS 客户查询：")
    text = text.replace("配置项 v2_customer_mapping_json", "客户映射配置")
    text = text.replace("SKU_MATCH_JSON:", "")
    return text




def suggested_actions(exception_type: ExceptionType, failed: list[dict[str, Any]]) -> list[str]:
    if exception_type == ExceptionType.OMS_BLOCKED:
        return ["检查 OMS 接口连通性与幂等键", "确认发货通知单字段是否满足 OMS 必填项", "修复后从异常台重放 OMS 下推"]
    if any(item.get("rule_code") == "KNOWN_ACTIVE_SKU" for item in failed):
        return ["在主数据维护 SKU 或补充 CRM 明细映射", "处理完成后重新触发订单预审"]
    if any(item.get("rule_code") == "HAS_ORDER_ITEMS" for item in failed):
        return ["检查 CRM 抓取字段是否包含订单明细", "补齐明细同步配置后重新抓取订单"]
    return ["核对 CRM 订单头字段与附件证据", "处理完成后重新触发订单预审"]


# order_dashboard 结果缓存（TTL 30秒），避免每次列表刷新都重复计算全表聚合
_dashboard_cache: dict[str, Any] = {"result": None, "expires_at": 0.0}




def exception_policy(exception_type: ExceptionType, severity: str) -> dict[str, Any]:
    policies: dict[ExceptionType, dict[str, Any]] = {
        ExceptionType.VALIDATION_BLOCKED: {"source_system": "CRM", "responsible_role": "商务/销售", "can_auto_retry": False, "freeze_order_flow": True},
        ExceptionType.SKU_MAPPING_MISSING: {"source_system": "CRM", "responsible_role": "商品/主数据管理员", "can_auto_retry": False, "freeze_order_flow": True},
        ExceptionType.OMS_REQUIRED_FIELDS_MISSING: {"source_system": "OMS", "responsible_role": "物流/IT", "can_auto_retry": False, "freeze_order_flow": True},
        ExceptionType.OMS_BLOCKED: {"source_system": "OMS", "responsible_role": "IT 运维/物流", "can_auto_retry": False, "freeze_order_flow": True},
        ExceptionType.OMS_STATUS_CONFLICT: {"source_system": "OMS", "responsible_role": "IT 运维/物流", "can_auto_retry": True, "freeze_order_flow": True},
        ExceptionType.CRM_CHANGED_AFTER_OMS_ACCEPTED: {"source_system": "CRM", "responsible_role": "商务主管/物流/IT", "can_auto_retry": False, "freeze_order_flow": True},
        ExceptionType.CRM_CHANGED_DURING_OMS_PENDING: {"source_system": "CRM", "responsible_role": "商务/物流/IT", "can_auto_retry": False, "freeze_order_flow": True},
        ExceptionType.CRM_CHANGED_DURING_OMS_RETRY: {"source_system": "CRM", "responsible_role": "商务主管/物流/IT", "can_auto_retry": False, "freeze_order_flow": True},
        ExceptionType.CRM_CHANGED_DURING_PICKING: {"source_system": "CRM", "responsible_role": "商务主管/仓库/物流", "can_auto_retry": False, "freeze_order_flow": True},
        ExceptionType.CRM_CANCELLED_AFTER_OMS_ACCEPTED: {"source_system": "CRM", "responsible_role": "商务主管/物流", "can_auto_retry": False, "freeze_order_flow": True},
        ExceptionType.CRM_CANCELLED_DURING_OMS_PENDING: {"source_system": "CRM", "responsible_role": "商务/物流", "can_auto_retry": False, "freeze_order_flow": True},
        ExceptionType.CRM_CANCELLED_DURING_OMS_RETRY: {"source_system": "CRM", "responsible_role": "商务主管/物流/IT", "can_auto_retry": False, "freeze_order_flow": True},
        ExceptionType.CRM_CHANGED_AFTER_SHIPPED: {"source_system": "CRM", "responsible_role": "商务主管/财务/物流", "can_auto_retry": False, "freeze_order_flow": False},
        ExceptionType.CRM_CANCELLED_AFTER_SHIPPED: {"source_system": "CRM", "responsible_role": "商务主管/财务/物流", "can_auto_retry": False, "freeze_order_flow": False},
        ExceptionType.MANUAL_REPLAY_WITHOUT_FIX: {"source_system": "Manual", "responsible_role": "商务/IT", "can_auto_retry": False, "freeze_order_flow": True},
    }
    default_source = "System" if severity in {"Low", "Medium"} else "CRM"
    return policies.get(
        exception_type,
        {"source_system": default_source, "responsible_role": "商务/IT", "can_auto_retry": severity in {"Low", "Medium"}, "freeze_order_flow": severity in {"High", "Critical"}},
    )




def sku_match_issues(result: ValidationResult) -> list[dict[str, Any]]:
    issues: list[dict[str, Any]] = []
    for ref in result.evidence_refs or []:
        if not ref.startswith("SKU_MATCH_JSON:"):
            continue
        payload = loads(ref.split(":", 1)[1], {})
        if isinstance(payload, dict):
            issues.append(payload)
    return issues




def sorted_sku_candidates(candidates: list[Any]) -> list[dict[str, Any]]:
    dict_candidates = [item for item in candidates if isinstance(item, dict)]
    return sorted(
        dict_candidates,
        key=lambda item: (int(item.get("confidence") or 0), int(item.get("score") or 0)),
        reverse=True,
    )




def sku_current_value(reason: str) -> str:
    if reason == "ambiguous":
        return "候选 SKU 过多，无法自动选择"
    if reason == "low_confidence":
        return "候选 SKU 相似度不足，无法自动选择"
    if reason == "not_found":
        return "未匹配到标准 SKU"
    return "未匹配到唯一标准 SKU"




def sku_failure_reason(reason: str) -> str:
    if reason == "ambiguous":
        return "商品描述过于泛化，匹配到多个相似 SKU。"
    if reason == "low_confidence":
        return "系统找到相似商品，但相似度不足，无法自动判断。"
    if reason == "not_found":
        return "系统没有找到足够相似的标准商品或别名。"
    return "系统无法根据当前商品信息确认唯一标准 SKU。"




def _attachment_consistency_issue_row(raw_issue: str) -> dict[str, str]:
    product, _, message = raw_issue.partition("：")
    product = product.strip() or "-"
    message = message.strip() or raw_issue.strip()
    crm = _extract_issue_value(message, "CRM")
    attachment = _extract_issue_value(message, "附件")
    field = "商品名称/关键词"
    conclusion = message
    if "数量" in message:
        field = "数量"
    elif "单价" in message:
        field = "单价"
    elif "明细总价" in message or "总价" in message or "金额" in message:
        field = "明细总价"

    if "未出现可匹配的商品名称" in message:
        conclusion = "附件未匹配到 CRM 商品关键词"
        attachment = "未匹配"
    elif "未识别到可比对" in message:
        conclusion = "附件未识别到可比对字段"
        attachment = "未识别"
    elif "不一致" in message:
        conclusion = "不一致"
    return {
        "product": product,
        "field": field,
        "crm": crm or "-",
        "attachment": attachment or "-",
        "conclusion": conclusion,
    }




def _extract_issue_value(message: str, label: str) -> str:
    match = re.search(rf"{label}=([^，；;、)）]+)", message)
    return match.group(1).strip() if match else ""




def _mail_table_cell(value: str) -> str:
    return str(value or "-").replace("|", "/").replace("\n", " ").strip()




