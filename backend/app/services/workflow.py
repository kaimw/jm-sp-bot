from __future__ import annotations

import hashlib
import re
from datetime import datetime, timedelta, timezone

from sqlalchemy import func, or_
from sqlalchemy.orm import Session

from backend.app.models import (
    AuditEvent,
    AttachmentAsset,
    ExceptionCase,
    ExtractionEvidence,
    MailMessage,
    MailTemplate,
    ModelProviderConfig,
    OrderRequirement,
    OutboundMailJob,
    ProcessingJob,
    ProductionDepartment,
    QuestionAndReply,
    RequirementWorkflowBinding,
    ProductionTask,
    ProductionTaskVersion,
    SystemConfig,
    WorkflowVersion,
    now_utc,
)
from backend.app.services.jsonutil import as_list, dumps, loads
from backend.app.services.initial_review import ReviewFailure, evaluate_initial_review, find_recent_duplicate_requirement, serialize_review_failures
from backend.app.services.llm_fallback import LLMMailClassification, classify_mail_with_llm, extract_requirement_with_llm
from backend.app.services.model_provider import call_model, extract_chat_content
from backend.app.services.parser import ExtractedRequirement, classify_mail, extract_requirement
from backend.app.services.templates import render_template
from backend.app.services.workflow_rules import (
    CORE_FIELD_LABELS,
    extract_workflow_fields,
    match_workflow_for_mail,
    resolve_contact_emails,
    upsert_mail_workflow_match,
    workflow_binding_for_requirement,
)


TASK_NO_PATTERN = re.compile(r"PT-\d{8}-\d{4}", re.IGNORECASE)
REQUIREMENT_NO_PATTERN = re.compile(r"REQ-\d{8}-\d{4}", re.IGNORECASE)
INCOMPLETE_REPLY_KEYWORDS = ["待确认", "不确定", "稍后", "无法确认", "再确认"]
QUESTION_INDICATORS = ["?", "？", "疑问", "请确认", "信息不足", "未写明", "没有写明", "不明确", "哪个", "哪一", "国内", "海外"]
PRODUCTION_PENDING_QUERY_KEYWORDS = ["查询待确认", "待确认任务", "待确认生产任务", "未确认任务", "待排产任务", "当前待确认"]
STATUS_QUERY_KEYWORDS = ["查询", "查一下", "查看", "统计", "状态", "进度", "处理到哪", "到哪了", "需求", "订单", "任务"]
STATUS_QUERY_INTENT_KEYWORDS = ["状态", "进度", "处理到哪", "到哪了", "统计", "汇总", "列表", "明细", "多少", "有哪些", "查询", "查看"]
PRODUCTION_CONFIRM_KEYWORDS = ["确认", "确认排产", "可以生产", "已排产", "安排生产", "同意排产", "同意生产", "同意安排生产", "确认生产"]
PRODUCTION_EXPLICIT_CONFIRM_KEYWORDS = ["确认排产", "可以生产", "已排产", "安排生产", "同意排产", "同意生产", "同意安排生产", "确认生产"]
PRODUCTION_TERMINATE_KEYWORDS = ["终止生产", "停止生产", "暂停生产", "取消生产", "终止排产", "停止排单", "停止该任务", "终止该任务"]
SALES_ACK_CLASSIFICATIONS = {
    "SalesOrderRequirement",
    "SalesClarificationReply",
    "OrderChangeRequest",
    "OrderCancelRequest",
}
REPORT_TIMEZONE = timezone(timedelta(hours=8))


def get_config(session: Session, key: str, fallback: str = "") -> str:
    config = session.get(SystemConfig, key)
    return config.value if config is not None else fallback


def bot_enabled(session: Session) -> bool:
    return get_config(session, "bot_enabled", "true").lower() in {"1", "true", "yes", "on"}


def add_audit(session: Session, event_type: str, object_type: str, object_id: str, detail: dict, actor: str = "System") -> None:
    session.add(
        AuditEvent(
            event_type=event_type,
            actor=actor,
            related_object_type=object_type,
            related_object_id=object_id,
            detail=dumps(detail),
        )
    )


SEVERITY_RANK = {"Low": 1, "Medium": 2, "High": 3, "Critical": 4}


def record_exception_case(
    session: Session,
    *,
    exception_type: str,
    severity: str,
    detail: dict | str,
    related_task_id: str | None = None,
    source_mail_id: str | None = None,
) -> ExceptionCase:
    detail_data = detail.copy() if isinstance(detail, dict) else {"message": str(detail)}
    source_mail_id = source_mail_id or detail_data.get("source_mail_id") or detail_data.get("mail_id")
    if source_mail_id:
        detail_data["source_mail_id"] = str(source_mail_id)

    entry = {
        "exception_type": exception_type,
        "severity": severity,
        "detail": detail_data,
        "created_at": now_utc().isoformat(),
    }
    merged_detail = detail_data.copy()
    merged_detail["exceptions"] = [entry]
    if source_mail_id:
        merged_detail["source_mail_id"] = str(source_mail_id)

    existing = None
    if source_mail_id:
        existing = (
            session.query(ExceptionCase)
            .filter(
                or_(
                    ExceptionCase.detail.like(f'%"source_mail_id":"{source_mail_id}"%'),
                    ExceptionCase.detail.like(f'%"mail_id":"{source_mail_id}"%'),
                ),
            )
            .order_by(ExceptionCase.created_at.desc())
            .first()
        )

    if existing is None:
        case = ExceptionCase(
            related_task_id=related_task_id,
            exception_type=exception_type,
            severity=severity,
            detail=dumps(merged_detail),
        )
        session.add(case)
        session.flush()
        return case

    existing_detail = loads(existing.detail, {})
    if not isinstance(existing_detail, dict):
        existing_detail = {"message": str(existing.detail)}
    existing_entries = existing_detail.get("exceptions")
    if not isinstance(existing_entries, list):
        previous_type = existing.exception_type
        previous_severity = existing.severity
        previous_detail = {key: value for key, value in existing_detail.items() if key != "exceptions"}
        existing_entries = [
            {
                "exception_type": previous_type,
                "severity": previous_severity,
                "detail": previous_detail,
                "created_at": existing.created_at.isoformat(),
            }
        ]

    if not any(item.get("exception_type") == exception_type and item.get("detail") == detail_data for item in existing_entries if isinstance(item, dict)):
        existing_entries.append(entry)

    for key, value in detail_data.items():
        if key in {"missing_fields", "risk_flags", "review_failures"} and isinstance(value, list):
            current = existing_detail.get(key)
            if not isinstance(current, list):
                current = []
            for item in value:
                if item not in current:
                    current.append(item)
            existing_detail[key] = current
        elif key not in existing_detail or existing_detail.get(key) in (None, "", [], {}):
            existing_detail[key] = value

    existing_detail["source_mail_id"] = str(source_mail_id) if source_mail_id else existing_detail.get("source_mail_id")
    existing_detail["exception_types"] = sorted({str(item.get("exception_type")) for item in existing_entries if isinstance(item, dict) and item.get("exception_type")})
    existing_detail["exceptions"] = existing_entries
    existing.related_task_id = existing.related_task_id or related_task_id
    existing.status = "Open"
    if len(existing_detail["exception_types"]) > 1:
        existing.exception_type = "MailExceptions"
    existing.severity = severity if SEVERITY_RANK.get(severity, 0) > SEVERITY_RANK.get(existing.severity, 0) else existing.severity
    existing.detail = dumps(existing_detail)
    return existing


def next_sequence(session: Session, model: type, column_name: str) -> int:
    return int(session.query(func.count(getattr(model, column_name))).scalar() or 0) + 1


def make_task_no(session: Session) -> str:
    return f"PT-{datetime.now().strftime('%Y%m%d')}-{next_sequence(session, ProductionTask, 'id'):04d}"


def recipient_hash(to_addresses: list[str], cc_addresses: list[str]) -> str:
    raw = dumps({"to": sorted(to_addresses), "cc": sorted(cc_addresses)})
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]


def primary_department(session: Session) -> ProductionDepartment:
    department = session.query(ProductionDepartment).filter_by(status="Active").first()
    if department is None:
        department = ProductionDepartment(department_code="default", department_name="默认生产部门")
        session.add(department)
        session.flush()
    return department


def production_department_addresses(session: Session) -> set[str]:
    addresses: set[str] = set()
    for department in session.query(ProductionDepartment).filter_by(status="Active").all():
        addresses.update(address.lower() for address in as_list(department.mail_to_json))
        addresses.update(address.lower() for address in as_list(department.mail_cc_json))
    return addresses


def looks_like_question(text: str) -> bool:
    return any(indicator in text for indicator in QUESTION_INDICATORS)


def looks_like_pending_task_query(text: str) -> bool:
    return any(keyword in text for keyword in PRODUCTION_PENDING_QUERY_KEYWORDS)


def looks_like_production_termination(text: str) -> bool:
    compact = re.sub(r"\s+", "", text)
    return any(keyword in compact for keyword in PRODUCTION_TERMINATE_KEYWORDS)


def looks_like_production_confirmation(text: str) -> bool:
    compact = re.sub(r"\s+", "", text)
    if any(keyword in compact for keyword in PRODUCTION_EXPLICIT_CONFIRM_KEYWORDS):
        return True
    if "同意" in compact and any(keyword in compact for keyword in ["排产", "生产", "安排"]):
        return True
    if compact in {"确认", "已确认", "确认了", "可以", "可以的", "没问题"}:
        return True
    if "确认" in compact and not looks_like_question(compact) and not any(word in compact for word in ["不确认", "待确认", "无法确认", "请确认"]):
        return True
    return False


def conversation_max_rounds(session: Session, task: ProductionTask | None = None) -> int:
    if task is not None:
        binding = workflow_binding_for_requirement(session, task.requirement_id)
        if binding is not None and binding.workflow_version_id:
            version = session.get(WorkflowVersion, binding.workflow_version_id)
            rules = loads(version.compiled_rules_json, {}) if version is not None else {}
            policy = rules.get("conversation_policy") if isinstance(rules, dict) else {}
            if isinstance(policy, dict):
                try:
                    workflow_rounds = int(policy.get("max_question_rounds") or 0)
                except (TypeError, ValueError):
                    workflow_rounds = 0
                if workflow_rounds > 0:
                    return max(1, workflow_rounds)
    raw = get_config(session, "conversation_max_rounds", "3")
    try:
        value = int(raw)
    except ValueError:
        value = 3
    return max(1, value)


def conversation_round_count(session: Session, task: ProductionTask) -> int:
    return session.query(QuestionAndReply).filter_by(task_id=task.id).count()


def active_task_template(session: Session) -> MailTemplate:
    template = (
        session.query(MailTemplate)
        .filter(MailTemplate.template_code == "production_task", MailTemplate.status == "Active")
        .order_by(MailTemplate.created_at.desc())
        .first()
    )
    if template is None:
        raise RuntimeError("missing production task template")
    return template


def find_task_for_mail(session: Session, mail: MailMessage) -> ProductionTask | None:
    if mail.related_task_id:
        task = session.get(ProductionTask, mail.related_task_id)
        if task is not None:
            return task

    text = f"{mail.subject}\n{mail.body_text}"
    for task_no in TASK_NO_PATTERN.findall(text):
        task = session.query(ProductionTask).filter(func.upper(ProductionTask.task_no) == task_no.upper()).one_or_none()
        if task is not None:
            return task

    extracted = extract_requirement(mail.subject, text, mail.from_address)
    if extracted.external_order_no:
        task = (
            session.query(ProductionTask)
            .join(OrderRequirement, OrderRequirement.id == ProductionTask.requirement_id)
            .filter(OrderRequirement.external_order_no == extracted.external_order_no)
            .order_by(ProductionTask.created_at.desc())
            .first()
        )
        if task is not None:
            return task

    open_question = (
        session.query(QuestionAndReply)
        .join(ProductionTask, ProductionTask.id == QuestionAndReply.task_id)
        .join(OrderRequirement, OrderRequirement.id == ProductionTask.requirement_id)
        .filter(
            QuestionAndReply.status == "AwaitingSalesReply",
            OrderRequirement.salesperson_email == mail.from_address,
        )
        .order_by(QuestionAndReply.created_at.desc())
        .first()
    )
    return open_question.task if open_question is not None else None


def create_inbound_mail(
    session: Session,
    *,
    from_address: str,
    subject: str,
    body_text: str,
    dedupe_key: str | None = None,
) -> MailMessage:
    if dedupe_key is None:
        digest = hashlib.sha256(f"{from_address}|{subject}|{body_text}".encode("utf-8")).hexdigest()
        dedupe_key = f"inbound:{digest}"
    existing = session.query(MailMessage).filter_by(dedupe_key=dedupe_key).one_or_none()
    if existing is not None:
        return existing

    classification, confidence = classify_mail(subject, body_text, from_address)
    mail = MailMessage(
        direction="Inbound",
        from_address=from_address,
        subject=subject,
        body_text=body_text,
        classification=classification,
        classification_confidence=confidence,
        dedupe_key=dedupe_key,
    )
    session.add(mail)
    session.flush()
    add_audit(session, "MailReceived", "MailMessage", mail.id, {"classification": classification, "confidence": confidence})
    return mail


def enqueue_sales_receipt_ack(
    session: Session,
    mail: MailMessage,
    *,
    allow_order_requirement: bool = False,
    task_no: str | None = None,
) -> OutboundMailJob | None:
    from_address = (mail.from_address or "").strip()
    if not from_address or mail.classification not in SALES_ACK_CLASSIFICATIONS:
        return None
    if mail.classification == "SalesOrderRequirement" and not allow_order_requirement:
        return None
    sender = from_address.lower()
    bot_email = get_config(session, "bot_email", "bot.market@jimuyida.com").lower()
    if sender == bot_email or sender in production_department_addresses(session):
        return None

    to_addresses = [from_address]
    cc_addresses: list[str] = []
    idem = f"sales-receipt-ack:{mail.id}:{recipient_hash(to_addresses, cc_addresses)}"
    existing = session.query(OutboundMailJob).filter_by(idempotency_key=idem).one_or_none()
    if existing is not None:
        return existing

    subject = mail.subject.strip() if mail.subject else "生产订单需求"
    if not subject.lower().startswith("re:"):
        subject = f"Re: {subject}"
    if mail.classification == "SalesOrderRequirement" and task_no:
        body = "\n".join(
            [
                "销售同事好，",
                "",
                "您的需求邮件已通过系统初审并创建生产任务。",
                f"任务号：{task_no}",
                f"原邮件主题：{mail.subject or ''}",
                "",
                "后续如需变更请邮件回复“订单变更 + 任务号”；如需撤回请邮件回复“撤回需求 + 任务号”（生产确认排单前有效）。",
                "",
                get_config(session, "bot_signature", "积木易搭AI机器人"),
            ]
        )
    else:
        body = "\n".join(
            [
                "销售同事好，",
                "",
                "您的邮件已收到，系统已进入排队处理中。",
                f"原邮件主题：{mail.subject or ''}",
                f"入库编号：{mail.id}",
                "",
                "后续如果订单信息缺失或生产部有疑问，我会继续邮件通知您补充确认。",
                "",
                get_config(session, "bot_signature", "积木易搭AI机器人"),
            ]
        )
    job = OutboundMailJob(
        mail_type="SalesReceiptAck",
        to_json=dumps(to_addresses),
        cc_json=dumps(cc_addresses),
        subject=subject,
        body=body,
        idempotency_key=idem,
        status="Pending",
    )
    session.add(job)
    session.flush()
    add_audit(
        session,
        "SalesReceiptAckQueued",
        "MailMessage",
        mail.id,
        {"to": to_addresses, "classification": mail.classification, "task_no": task_no or ""},
    )
    return job


def mail_text_with_attachments(session: Session, mail: MailMessage) -> str:
    attachment_parts = [
        f"[附件:{asset.file_name}]\n{asset.extracted_text}"
        for asset in session.query(AttachmentAsset).filter_by(mail_id=mail.id).all()
        if asset.extracted_text
    ]
    return "\n\n".join([mail.body_text, *attachment_parts]).strip()


def workflow_context_text_for_requirement(
    session: Session,
    requirement: OrderRequirement,
    current_mail: MailMessage,
    current_source_text: str,
) -> str:
    parts: list[str] = []
    origin_mail = session.get(MailMessage, requirement.source_mail_id) if requirement.source_mail_id else None
    if origin_mail is not None and origin_mail.id != current_mail.id:
        origin_text = mail_text_with_attachments(session, origin_mail)
        if origin_text:
            parts.append(origin_text)
    if current_source_text:
        parts.append(current_source_text)
    deduped: list[str] = []
    for item in parts:
        text = item.strip()
        if text and text not in deduped:
            deduped.append(text)
    return "\n\n".join(deduped).strip()


def create_extraction_evidence(session: Session, requirement: OrderRequirement, mail: MailMessage, source_text: str) -> None:
    values = {
        "customer_name": requirement.customer_name,
        "product_summary": requirement.product_summary,
        "quantity_text": requirement.quantity_text,
        "expected_delivery_date": requirement.expected_delivery_date,
        "external_order_no": requirement.external_order_no,
        "salesperson_email": requirement.salesperson_email,
    }
    attachments = session.query(AttachmentAsset).filter_by(mail_id=mail.id).all()
    for field_name, value in values.items():
        if not value:
            continue
        source_type = "MailBody"
        source_attachment_id = None
        evidence_text = find_evidence_line(source_text, value)
        for asset in attachments:
            if asset.extracted_text and value in asset.extracted_text:
                source_type = "Attachment"
                source_attachment_id = asset.id
                evidence_text = find_evidence_line(asset.extracted_text, value)
                break
        session.add(
            ExtractionEvidence(
                requirement_id=requirement.id,
                field_name=field_name,
                field_value=value,
                source_type=source_type,
                source_mail_id=mail.id,
                source_attachment_id=source_attachment_id,
                evidence_text=evidence_text or value,
                confidence=90 if source_type == "MailBody" else 85,
            )
        )


def find_evidence_line(text: str, value: str) -> str:
    for line in text.splitlines():
        if value in line:
            return line.strip()[:500]
    return value[:500]


def route_is_configured(session: Session) -> bool:
    return bool(as_list(primary_department(session).mail_to_json))


WORKFLOW_EXTRA_FIELD_LABELS = {
    "material_details": "物料详情描述",
    "logistics_method": "物流发货方式",
    "shipping_time_requirement": "出货时间要求",
    "customer_receiver_info": "客户收件信息",
    "delivery_requirement": "交付要求",
    "shipping_warehouse": "出货仓/借货仓",
    "borrow_time": "借用时间",
    "return_time": "归还时间",
    "sample_approval_screenshot": "样机借用审批截图",
}


def workflow_field_label(field: str) -> str:
    return CORE_FIELD_LABELS.get(field) or WORKFLOW_EXTRA_FIELD_LABELS.get(field) or field


def workflow_required_field_missing(requirement: OrderRequirement, extracted_fields: dict[str, str], field: str) -> bool:
    if field in CORE_FIELD_LABELS:
        return not bool(getattr(requirement, field, "") or "")
    return not bool((extracted_fields or {}).get(field))


def upsert_requirement_workflow_binding(
    session: Session,
    requirement: OrderRequirement,
    mail: MailMessage,
    source_text: str,
) -> tuple[RequirementWorkflowBinding | None, list[ReviewFailure], list[str], list[str]]:
    match = match_workflow_for_mail(session, mail, source_text)
    upsert_mail_workflow_match(session, mail, match)
    if match is None:
        return None, [], [], []

    rule = match.rule
    routing = rule.get("routing") if isinstance(rule.get("routing"), dict) else {}
    to_names = [str(item) for item in routing.get("to_names", []) if str(item).strip()]
    cc_names = [str(item) for item in routing.get("cc_names", []) if str(item).strip()]
    to_emails, unresolved_to = resolve_contact_emails(
        session,
        to_names,
        salesperson_email=requirement.salesperson_email,
    )
    cc_emails, unresolved_cc = resolve_contact_emails(
        session,
        cc_names,
        salesperson_email=requirement.salesperson_email,
    )
    required_fields = [str(item) for item in rule.get("required_fields", []) if str(item).strip()]
    extracted_fields = extract_workflow_fields(source_text, required_fields)
    missing_labels: list[str] = []
    failures: list[ReviewFailure] = []
    risk_flags: list[str] = []

    for field in required_fields:
        if workflow_required_field_missing(requirement, extracted_fields, field):
            label = workflow_field_label(field)
            if label not in missing_labels:
                missing_labels.append(label)
            failures.append(
                ReviewFailure(
                    field=field,
                    field_label=label,
                    rule_name="流程规则必填字段",
                    message=f"{label}缺失，未通过流程规则校验。",
                )
            )

    required_attachments = [str(item) for item in rule.get("required_attachments", []) if str(item).strip()]
    for attachment_hint in required_attachments:
        if attachment_hint and attachment_hint not in source_text:
            message = f"缺少流程要求附件：{attachment_hint}"
            failures.append(
                ReviewFailure(
                    field="source_text",
                    field_label="邮件全文",
                    rule_name="流程附件校验",
                    message=message,
                )
            )
            risk_flags.append(message)

    review_rules = [item for item in rule.get("review_rules", []) if isinstance(item, dict)]
    if review_rules:
        review_result = evaluate_initial_review(
            session,
            requirement,
            source_text=source_text,
            parser_risk_flags=[],
            required_fields_override=[],
            rules_override=review_rules,
            include_duplicate_check=False,
        )
        failures.extend(review_result.failures)
        for flag in review_result.risk_flags:
            if flag and flag not in risk_flags:
                risk_flags.append(flag)

    unresolved_contacts = sorted(set(unresolved_to + unresolved_cc))
    for name in unresolved_contacts:
        message = f"流程收件人未映射邮箱：{name}"
        failures.append(
            ReviewFailure(
                field="source_text",
                field_label="流程路由",
                rule_name="流程路由校验",
                message=message,
            )
        )
        risk_flags.append(message)

    binding = workflow_binding_for_requirement(session, requirement.id)
    if binding is None:
        binding = RequirementWorkflowBinding(requirement_id=requirement.id)
        session.add(binding)
    binding.workflow_version_id = match.version.id
    binding.workflow_code = str(rule.get("workflow_code") or "")
    binding.workflow_name = str(rule.get("workflow_name") or "")
    binding.match_confidence = match.confidence
    binding.route_to_json = dumps(to_emails)
    binding.route_cc_json = dumps(cc_emails)
    binding.subject_template = str(rule.get("subject_template") or "") or "[生产任务单][{{task_no}}][{{customer_name}}][{{product_summary}}][V{{version_no}}]"
    binding.body_template = str(rule.get("body_template") or "") or (
        "生产部同事好：\n\n"
        "请根据以下信息安排生产评估和排产。\n\n"
        "任务单编号：{{task_no}}\n"
        "版本：V{{version_no}}\n"
        "客户名称：{{customer_name}}\n"
        "销售人员：{{salesperson_name}} <{{salesperson_email}}>\n\n"
        "产品/规格：{{product_summary}}\n"
        "数量：{{quantity_text}}\n"
        "期望交期：{{expected_delivery_date}}\n\n"
        "请确认是否可以安排生产。如信息不足，请直接回复本邮件说明疑问点。\n\n"
        "{{bot_signature}}\n"
    )
    binding.required_fields_json = dumps(required_fields)
    binding.required_attachments_json = dumps(required_attachments)
    binding.extracted_fields_json = dumps(extracted_fields)
    binding.missing_fields_json = dumps(missing_labels)
    binding.unresolved_contacts_json = dumps(unresolved_contacts)
    binding.updated_at = now_utc()
    session.flush()
    return binding, failures, missing_labels, risk_flags


def routing_for_requirement(
    session: Session,
    requirement: OrderRequirement,
) -> tuple[list[str], list[str], RequirementWorkflowBinding | None]:
    binding = workflow_binding_for_requirement(session, requirement.id)
    if binding is not None and as_list(binding.route_to_json):
        return as_list(binding.route_to_json), as_list(binding.route_cc_json), binding
    department = primary_department(session)
    return as_list(department.mail_to_json), as_list(department.mail_cc_json), binding


def task_template_for_requirement(
    session: Session,
    requirement: OrderRequirement,
) -> tuple[str, str, RequirementWorkflowBinding | None]:
    binding = workflow_binding_for_requirement(session, requirement.id)
    if binding is not None and binding.subject_template and binding.body_template:
        return binding.subject_template, binding.body_template, binding
    template = active_task_template(session)
    return template.subject_template, template.body_template, binding


def merge_extracted_requirement(
    extracted: ExtractedRequirement,
    llm_fields: dict[str, str],
    from_address: str,
) -> ExtractedRequirement:
    values = {
        "customer_name": extracted.customer_name or llm_fields.get("customer_name"),
        "salesperson_name": extracted.salesperson_name,
        "salesperson_email": extracted.salesperson_email or from_address or None,
        "product_summary": extracted.product_summary or llm_fields.get("product_summary"),
        "quantity_text": extracted.quantity_text or llm_fields.get("quantity_text"),
        "expected_delivery_date": extracted.expected_delivery_date or llm_fields.get("expected_delivery_date"),
        "external_order_no": extracted.external_order_no or llm_fields.get("external_order_no"),
    }
    missing = [
        label
        for label, value in [
            ("客户名称", values["customer_name"]),
            ("产品/规格", values["product_summary"]),
            ("数量", values["quantity_text"]),
            ("期望交期", values["expected_delivery_date"]),
        ]
        if not value
    ]
    return ExtractedRequirement(
        customer_name=values["customer_name"],
        salesperson_name=values["salesperson_name"],
        salesperson_email=values["salesperson_email"],
        product_summary=values["product_summary"],
        quantity_text=values["quantity_text"],
        expected_delivery_date=values["expected_delivery_date"],
        external_order_no=values["external_order_no"],
        missing_fields=missing,
        risk_flags=extracted.risk_flags,
    )


def extract_requirement_with_fallback(
    session: Session,
    mail: MailMessage,
    source_text: str,
) -> ExtractedRequirement:
    extracted = extract_requirement(mail.subject, source_text, mail.from_address)
    if not extracted.missing_fields:
        return extracted
    try:
        llm_fields = extract_requirement_with_llm(session, mail, source_text)
    except Exception as exc:
        add_audit(session, "LLMRequirementExtractionFailed", "MailMessage", mail.id, {"error": str(exc)[:1000]})
        return extracted
    if not llm_fields:
        return extracted
    merged = merge_extracted_requirement(extracted, llm_fields, mail.from_address)
    add_audit(
        session,
        "LLMRequirementExtractionApplied",
        "MailMessage",
        mail.id,
        {"filled_fields": sorted(field for field, value in llm_fields.items() if value)},
    )
    return merged


def create_task_from_mail(session: Session, mail: MailMessage) -> ProductionTask | None:
    if mail.related_task_id:
        existing_task = session.get(ProductionTask, mail.related_task_id)
        if existing_task is not None:
            enqueue_sales_receipt_ack(session, mail, allow_order_requirement=True, task_no=existing_task.task_no)
            return existing_task
    existing_requirements = (
        session.query(OrderRequirement)
        .filter_by(source_mail_id=mail.id)
        .order_by(OrderRequirement.created_at, OrderRequirement.id)
        .all()
    )
    if existing_requirements:
        requirement_ids = [row.id for row in existing_requirements]
        existing_task = (
            session.query(ProductionTask)
            .filter(ProductionTask.requirement_id.in_(requirement_ids))
            .order_by(ProductionTask.created_at, ProductionTask.id)
            .first()
        )
        if existing_task is not None:
            enqueue_sales_receipt_ack(session, mail, allow_order_requirement=True, task_no=existing_task.task_no)
            return existing_task
        existing_requirement = existing_requirements[0]
        if existing_requirement.status == "ReviewFailed":
            missing_fields = as_list(existing_requirement.missing_fields_json)
            if missing_fields:
                enqueue_missing_field_request(session, existing_requirement, [str(field) for field in missing_fields])
            return None
        return None

    if mail.classification != "SalesOrderRequirement":
        record_exception_case(
            session,
            exception_type="NonRequirementMail",
            severity="Low",
            detail={"source_mail_id": mail.id, "classification": mail.classification, "message": f"邮件分类为 {mail.classification}，未创建生产任务。"},
            source_mail_id=mail.id,
        )
        return None

    source_text = mail_text_with_attachments(session, mail)
    extracted = extract_requirement_with_fallback(session, mail, source_text)
    requirement_no = f"REQ-{datetime.now().strftime('%Y%m%d')}-{next_sequence(session, OrderRequirement, 'id'):04d}"
    requirement = OrderRequirement(
        source_mail_id=mail.id,
        internal_order_no=requirement_no,
        external_order_no=extracted.external_order_no,
        customer_name=extracted.customer_name,
        salesperson_name=extracted.salesperson_name,
        salesperson_email=extracted.salesperson_email,
        product_summary=extracted.product_summary,
        expected_delivery_date=extracted.expected_delivery_date,
        quantity_text=extracted.quantity_text,
        missing_fields_json="[]",
        risk_flags_json="[]",
        status="Extracted",
    )
    session.add(requirement)
    session.flush()
    create_extraction_evidence(session, requirement, mail, source_text)

    workflow_binding, workflow_failures, workflow_missing_fields, workflow_risk_flags = upsert_requirement_workflow_binding(
        session,
        requirement,
        mail,
        source_text,
    )
    if workflow_binding is None:
        review = evaluate_initial_review(session, requirement, source_text=source_text, parser_risk_flags=extracted.risk_flags)
    else:
        review = evaluate_initial_review(
            session,
            requirement,
            source_text=source_text,
            parser_risk_flags=extracted.risk_flags,
            required_fields_override=[],
            rules_override=[],
        )
    all_failures = [*review.failures, *workflow_failures]
    merged_missing_fields: list[str] = []
    for label in [*review.missing_fields, *workflow_missing_fields]:
        if label and label not in merged_missing_fields:
            merged_missing_fields.append(label)
    merged_risk_flags: list[str] = []
    for flag in [*review.risk_flags, *workflow_risk_flags]:
        if flag and flag not in merged_risk_flags:
            merged_risk_flags.append(flag)
    requirement.missing_fields_json = dumps(merged_missing_fields)
    requirement.risk_flags_json = dumps(merged_risk_flags)
    requirement.status = "ReviewFailed" if all_failures else "TaskCreated"

    if all_failures:
        record_exception_case(
            session,
            exception_type="ReviewNeedManual",
            severity="High" if merged_risk_flags else "Medium",
            detail={
                "requirement_id": requirement.id,
                "source_mail_id": mail.id,
                "missing_fields": merged_missing_fields,
                "risk_flags": merged_risk_flags,
                "review_failures": serialize_review_failures(all_failures),
                "workflow_code": workflow_binding.workflow_code if workflow_binding is not None else None,
                "workflow_name": workflow_binding.workflow_name if workflow_binding is not None else None,
            },
            source_mail_id=mail.id,
        )
        enqueue_initial_review_rejection(session, requirement, all_failures)
        add_audit(
            session,
            "RequirementReviewFailed",
            "OrderRequirement",
            requirement.id,
            {"missing": merged_missing_fields, "risk_flags": merged_risk_flags},
        )
        return None

    to_addresses, _cc_addresses, binding = routing_for_requirement(session, requirement)
    if not to_addresses:
        requirement.status = "ReviewFailed"
        routing_message = "生产部门邮箱未配置"
        routing_hint = "请先在【生产邮箱】页面配置主送邮箱，保存后重新处理该订单。"
        if binding is not None and binding.workflow_code:
            routing_message = f"流程 {binding.workflow_name or binding.workflow_code} 的收件邮箱未配置"
            routing_hint = "请先在【流程规则】中配置联系人映射（workflow_contact_map_json），再重新处理该订单。"
        record_exception_case(
            session,
            exception_type="RoutingMissing",
            severity="High",
            detail={
                "requirement_id": requirement.id,
                "source_mail_id": mail.id,
                "missing_fields": [],
                "risk_flags": [routing_message],
                "message": f"订单初审已通过，但{routing_message}，系统无法自动创建生产任务。",
                "action_hint": routing_hint,
                "workflow_code": binding.workflow_code if binding is not None else None,
            },
            source_mail_id=mail.id,
        )
        add_audit(session, "RoutingMissing", "OrderRequirement", requirement.id, {"source_mail_id": mail.id})
        return None

    task = draft_task_from_requirement(session, requirement, mail)
    approve_task(session, task.id, actor="System")
    enqueue_sales_receipt_ack(session, mail, allow_order_requirement=True, task_no=task.task_no)
    return task


def find_requirement_for_supplement_reply(session: Session, mail: MailMessage) -> OrderRequirement | None:
    text = f"{mail.subject}\n{mail.body_text}"
    for requirement_no in REQUIREMENT_NO_PATTERN.findall(text):
        requirement = (
            session.query(OrderRequirement)
            .filter(func.upper(OrderRequirement.internal_order_no) == requirement_no.upper())
            .one_or_none()
        )
        if requirement is not None:
            return requirement

    if "订单信息待补充" not in text:
        return None
    from_address = (mail.from_address or "").strip()
    if not from_address:
        return None
    return (
        session.query(OrderRequirement)
        .filter(
            OrderRequirement.salesperson_email == from_address,
            OrderRequirement.status == "ReviewFailed",
        )
        .order_by(OrderRequirement.created_at.desc())
        .first()
    )


def existing_task_for_requirement(session: Session, requirement: OrderRequirement) -> ProductionTask | None:
    return (
        session.query(ProductionTask)
        .filter_by(requirement_id=requirement.id)
        .order_by(ProductionTask.created_at.desc())
        .first()
    )


def apply_requirement_supplement_updates(
    session: Session,
    requirement: OrderRequirement,
    mail: MailMessage,
    source_text: str,
) -> tuple[list[str], list[str]]:
    extracted = extract_requirement("", source_text, mail.from_address)
    if extracted.missing_fields:
        try:
            llm_fields = extract_requirement_with_llm(session, mail, source_text)
        except Exception as exc:
            add_audit(session, "LLMRequirementSupplementExtractionFailed", "MailMessage", mail.id, {"error": str(exc)[:1000]})
        else:
            if llm_fields:
                extracted = merge_extracted_requirement(extracted, llm_fields, mail.from_address)
                add_audit(
                    session,
                    "LLMRequirementSupplementExtractionApplied",
                    "MailMessage",
                    mail.id,
                    {"filled_fields": sorted(field for field, value in llm_fields.items() if value)},
                )
    updates: list[str] = []
    field_pairs = [
        ("customer_name", extracted.customer_name, "客户名称"),
        ("product_summary", extracted.product_summary, "产品/规格"),
        ("quantity_text", extracted.quantity_text, "数量"),
        ("expected_delivery_date", extracted.expected_delivery_date, "期望交期"),
        ("external_order_no", extracted.external_order_no, "订单号"),
    ]
    for attr, value, label in field_pairs:
        if value and value != getattr(requirement, attr):
            setattr(requirement, attr, value)
            updates.append(f"{label}：{value}")
    if not requirement.salesperson_email:
        requirement.salesperson_email = mail.from_address or None
    if updates:
        requirement.updated_at = now_utc()
    return updates, extracted.risk_flags


def resolve_requirement_review_exceptions(session: Session, requirement: OrderRequirement, source_mail: MailMessage) -> None:
    patterns = [requirement.id, requirement.source_mail_id, source_mail.id]
    cases = (
        session.query(ExceptionCase)
        .filter(ExceptionCase.status == "Open")
        .filter(or_(*[ExceptionCase.detail.like(f"%{pattern}%") for pattern in patterns if pattern]))
        .all()
    )
    for case in cases:
        if case.exception_type not in {"ReviewNeedManual", "MailTaskLinkFailed", "MailExceptions", "NonTarget"}:
            continue
        detail = loads(case.detail, {})
        detail["auto_resolved_by"] = "RequirementSupplementReply"
        detail["resolved_source_mail_id"] = source_mail.id
        case.detail = dumps(detail)
        case.status = "Resolved"


def handle_requirement_supplement_reply(session: Session, mail: MailMessage) -> ProductionTask | None:
    requirement = find_requirement_for_supplement_reply(session, mail)
    if requirement is None:
        return None

    existing_task = existing_task_for_requirement(session, requirement)
    if existing_task is not None:
        mail.related_task_id = existing_task.id
        return existing_task

    source_text = mail_text_with_attachments(session, mail)
    review_source_text = workflow_context_text_for_requirement(session, requirement, mail, source_text)
    updates, parser_risk_flags = apply_requirement_supplement_updates(session, requirement, mail, source_text)
    workflow_binding, workflow_failures, workflow_missing_fields, workflow_risk_flags = upsert_requirement_workflow_binding(
        session,
        requirement,
        mail,
        review_source_text,
    )
    if workflow_binding is None:
        review = evaluate_initial_review(
            session,
            requirement,
            source_text=review_source_text,
            parser_risk_flags=parser_risk_flags,
        )
    else:
        review = evaluate_initial_review(
            session,
            requirement,
            source_text=review_source_text,
            parser_risk_flags=parser_risk_flags,
            required_fields_override=[],
            rules_override=[],
        )
    all_failures = [*review.failures, *workflow_failures]
    merged_missing_fields: list[str] = []
    for label in [*review.missing_fields, *workflow_missing_fields]:
        if label and label not in merged_missing_fields:
            merged_missing_fields.append(label)
    merged_risk_flags: list[str] = []
    for flag in [*review.risk_flags, *workflow_risk_flags]:
        if flag and flag not in merged_risk_flags:
            merged_risk_flags.append(flag)
    requirement.missing_fields_json = dumps(merged_missing_fields)
    requirement.risk_flags_json = dumps(merged_risk_flags)

    if all_failures:
        requirement.status = "ReviewFailed"
        record_exception_case(
            session,
            exception_type="ReviewNeedManual",
            severity="High" if merged_risk_flags else "Medium",
            detail={
                "requirement_id": requirement.id,
                "source_mail_id": mail.id,
                "original_source_mail_id": requirement.source_mail_id,
                "missing_fields": merged_missing_fields,
                "risk_flags": merged_risk_flags,
                "review_failures": serialize_review_failures(all_failures),
                "supplement_updates": updates,
                "workflow_code": workflow_binding.workflow_code if workflow_binding is not None else None,
            },
            source_mail_id=mail.id,
        )
        enqueue_initial_review_rejection(session, requirement, all_failures, idempotency_source=mail.id)
        add_audit(
            session,
            "RequirementSupplementStillFailed",
            "OrderRequirement",
            requirement.id,
            {"source_mail_id": mail.id, "missing": merged_missing_fields, "risk_flags": merged_risk_flags, "updates": updates},
        )
        return None

    to_addresses, _cc_addresses, binding = routing_for_requirement(session, requirement)
    if not to_addresses:
        requirement.status = "ReviewFailed"
        routing_message = "生产部门邮箱未配置"
        routing_hint = "请先在【生产邮箱】页面配置主送邮箱，保存后重新处理该订单。"
        if binding is not None and binding.workflow_code:
            routing_message = f"流程 {binding.workflow_name or binding.workflow_code} 的收件邮箱未配置"
            routing_hint = "请先在【流程规则】中配置联系人映射（workflow_contact_map_json），再重新处理该订单。"
        record_exception_case(
            session,
            exception_type="RoutingMissing",
            severity="High",
            detail={
                "requirement_id": requirement.id,
                "source_mail_id": mail.id,
                "missing_fields": [],
                "risk_flags": [routing_message],
                "message": f"订单初审已通过，但{routing_message}，系统无法自动创建生产任务。",
                "action_hint": routing_hint,
                "workflow_code": binding.workflow_code if binding is not None else None,
            },
            source_mail_id=mail.id,
        )
        add_audit(session, "RoutingMissing", "OrderRequirement", requirement.id, {"source_mail_id": mail.id})
        return None

    requirement.status = "TaskCreated"
    task = draft_task_from_requirement(session, requirement, mail)
    issue_job = approve_task(session, task.id, actor="System")
    issue_job.mail_type = "RequirementSupplementTaskIssue"
    mail.classification = "RequirementSupplementReply"
    mail.classification_confidence = max(mail.classification_confidence, 90)
    resolve_requirement_review_exceptions(session, requirement, mail)
    add_audit(
        session,
        "RequirementSupplementAccepted",
        "OrderRequirement",
        requirement.id,
        {"source_mail_id": mail.id, "task_id": task.id, "updates": updates, "outbound_job": issue_job.id},
    )
    return task


def enqueue_initial_review_rejection(
    session: Session,
    requirement: OrderRequirement,
    failures: list[object],
    *,
    idempotency_source: str | None = None,
) -> OutboundMailJob | None:
    salesperson_email = requirement.salesperson_email or ""
    if not salesperson_email:
        return None
    ops_email = get_config(session, "ops_cc_email", "jinlei@jimuyida.com")
    to_addresses = [salesperson_email]
    cc_addresses = [ops_email] if ops_email else []
    source_key = idempotency_source or requirement.source_mail_id or requirement.id
    idem = f"initial-review-rejected:{source_key}:{recipient_hash(to_addresses, cc_addresses)}"
    existing = session.query(OutboundMailJob).filter_by(idempotency_key=idem).one_or_none()
    if existing is not None:
        return existing
    reason_lines = []
    duplicate_submission = False
    for failure in failures:
        message = getattr(failure, "message", str(failure)).strip()
        rule_name = getattr(failure, "rule_name", "")
        if rule_name == "重复提交检查" or "请勿重复提交" in message:
            duplicate_submission = True
        if message and message not in reason_lines:
            reason_lines.append(message)
    if not reason_lines:
        reason_lines = ["订单信息未通过系统初审，请补充完整后回复本邮件。"]
    mail_type = "RequirementSupplementRequest"
    subject = f"[订单信息待补充][{requirement.internal_order_no}] 请补充生产任务单信息"
    if duplicate_submission:
        duplicate_task_no = ""
        _, duplicate_task = find_recent_duplicate_requirement(session, requirement, hours=24)
        if duplicate_task is not None:
            duplicate_task_no = duplicate_task.task_no
        mail_type = "DuplicateSubmissionNotice"
        subject = f"[重复提交提醒][{requirement.internal_order_no}] 需求已提交，请勿重复提交"
        body = "\n".join(
            [
                "销售同事好，",
                "",
                "系统检测到同一需求在24小时内已提交并受理，本次不会重复创建任务。",
                *([f"已受理任务号：{duplicate_task_no}"] if duplicate_task_no else []),
                "",
                "如需调整内容，请回复“订单变更 + 任务号”；如需撤回，请回复“撤回需求 + 任务号”（生产确认排单前有效）。",
                "",
                get_config(session, "bot_signature", "积木易搭AI机器人"),
            ]
        )
    else:
        body = "\n".join(
            [
                "销售同事好，",
                "",
                "收到订单需求后，系统初审未通过，请按以下原因补充或修正后回复本邮件：",
                *[f"- {reason}" for reason in reason_lines],
                "",
                f"当前识别客户：{requirement.customer_name or '未识别'}",
                f"当前识别产品：{requirement.product_summary or '未识别'}",
                f"当前识别数量：{requirement.quantity_text or '未识别'}",
                f"当前识别交期：{requirement.expected_delivery_date or '未识别'}",
                f"当前识别订单号：{requirement.external_order_no or '未识别'}",
                "",
                get_config(session, "bot_signature", "积木易搭AI机器人"),
            ]
        )
    job = OutboundMailJob(
        mail_type=mail_type,
        to_json=dumps(to_addresses),
        cc_json=dumps(cc_addresses),
        subject=subject,
        body=body,
        idempotency_key=idem,
        status="Pending",
    )
    session.add(job)
    add_audit(session, "InitialReviewRejectedQueued", "OrderRequirement", requirement.id, {"reasons": reason_lines})
    return job


def enqueue_missing_field_request(session: Session, requirement: OrderRequirement, missing_fields: list[str]) -> OutboundMailJob | None:
    class MissingFieldFailure:
        def __init__(self, field: str) -> None:
            self.message = f"{field}不能为空"

    return enqueue_initial_review_rejection(session, requirement, [MissingFieldFailure(field) for field in missing_fields])


def draft_task_from_requirement(
    session: Session,
    requirement: OrderRequirement,
    mail: MailMessage | None = None,
) -> ProductionTask:
    existing_task = session.query(ProductionTask).filter_by(requirement_id=requirement.id).one_or_none()
    if existing_task is not None:
        return existing_task

    route_to, route_cc, _binding = routing_for_requirement(session, requirement)
    department = primary_department(session)
    task = ProductionTask(
        task_no=make_task_no(session),
        requirement_id=requirement.id,
        current_version_no=1,
        status="TaskDrafted",
        production_department_id=department.id,
        target_mail_to_json=dumps(route_to),
        target_mail_cc_json=dumps(route_cc),
    )
    session.add(task)
    session.flush()

    if mail is not None:
        mail.related_task_id = task.id
    subject_template, body_template, _binding = task_template_for_requirement(session, requirement)
    context = task_context(session, task, version_no=1)
    version = ProductionTaskVersion(
        task_id=task.id,
        version_no=1,
        subject=render_template(subject_template, context),
        body=render_template(body_template, context),
        status="Draft",
    )
    session.add(version)
    session.flush()
    requirement.status = "TaskCreated"
    requirement.updated_at = now_utc()
    add_audit(session, "TaskDrafted", "ProductionTask", task.id, {"task_no": task.task_no})
    return task


def task_context(session: Session, task: ProductionTask, version_no: int | None = None) -> dict[str, str | int | None]:
    requirement = task.requirement
    context: dict[str, str | int | None] = {
        "task_no": task.task_no,
        "version_no": version_no or task.current_version_no,
        "customer_name": requirement.customer_name,
        "salesperson_name": requirement.salesperson_name,
        "salesperson_email": requirement.salesperson_email,
        "product_summary": requirement.product_summary,
        "quantity_text": requirement.quantity_text,
        "expected_delivery_date": requirement.expected_delivery_date,
        "external_order_no": requirement.external_order_no,
        "bot_signature": get_config(session, "bot_signature", "积木易搭AI机器人"),
    }
    binding = workflow_binding_for_requirement(session, requirement.id)
    if binding is not None:
        context["workflow_code"] = binding.workflow_code
        context["workflow_name"] = binding.workflow_name or binding.workflow_code
        extracted_fields = loads(binding.extracted_fields_json, {})
        if isinstance(extracted_fields, dict):
            for key, value in extracted_fields.items():
                if key and value not in (None, ""):
                    context[str(key)] = str(value)
    return context


def approve_task(session: Session, task_id: str, actor: str = "business-owner") -> OutboundMailJob:
    task = session.get(ProductionTask, task_id)
    if task is None:
        raise ValueError("task not found")
    if task.status not in {"TaskDrafted", "TaskIssued", "Reissued", "ReissueDrafted"}:
        raise ValueError(f"task status {task.status} cannot be approved")
    version_no = task.current_version_no or 1
    version = (
        session.query(ProductionTaskVersion)
        .filter_by(task_id=task_id, version_no=version_no)
        .one_or_none()
    )
    if version is None:
        version = (
            session.query(ProductionTaskVersion)
            .filter_by(task_id=task_id)
            .order_by(ProductionTaskVersion.version_no.desc())
            .first()
        )
    if version is None:
        raise ValueError("task version not found")
    task.current_version_no = version.version_no
    to_addresses = as_list(task.target_mail_to_json)
    if not to_addresses:
        raise ValueError("production department email is not configured")
    cc_addresses = as_list(task.target_mail_cc_json)
    idem = f"task-issue:{task.id}:v{version.version_no}:{recipient_hash(to_addresses, cc_addresses)}"
    existing = session.query(OutboundMailJob).filter_by(idempotency_key=idem).one_or_none()
    if existing is not None:
        return existing

    version.status = "Sent"
    version.approved_by = actor
    version.approved_at = now_utc()
    task.status = "TaskIssued" if version.version_no == 1 else "Reissued"
    task.issued_at = now_utc()
    task.updated_at = now_utc()
    job = OutboundMailJob(
        related_task_id=task.id,
        related_version_id=version.id,
        mail_type="TaskIssue",
        to_json=dumps(to_addresses),
        cc_json=dumps(cc_addresses),
        subject=version.subject,
        body=version.body,
        idempotency_key=idem,
        status="Pending",
    )
    session.add(job)
    add_audit(session, "TaskApprovedForSend", "ProductionTask", task.id, {"actor": actor, "outbound_job": job.id}, actor)
    return job


def record_production_question(
    session: Session,
    task_id: str,
    question_text: str,
    *,
    source_mail: MailMessage | None = None,
) -> OutboundMailJob:
    task = session.get(ProductionTask, task_id)
    if task is None:
        raise ValueError("task not found")
    if task.status == "Closed":
        if source_mail is not None:
            source_mail.related_task_id = task.id
        raise ValueError(f"task is closed: {task.closed_reason or 'Closed'}")
    requirement = task.requirement
    salesperson_email = requirement.salesperson_email or ""
    ops_email = get_config(session, "ops_cc_email", "jinlei@jimuyida.com")
    clean_question = question_text.strip() or "生产部提出疑问，请补充详细信息。"

    existing_question = None
    if source_mail is not None:
        source_mail.related_task_id = task.id
        existing_question = (
            session.query(QuestionAndReply)
            .filter_by(task_id=task.id, production_question_mail_id=source_mail.id)
            .one_or_none()
        )
    if existing_question is None and conversation_round_count(session, task) >= conversation_max_rounds(session, task):
        return close_conversation_for_max_rounds(session, task, source_mail=source_mail)

    if source_mail is not None:
        if existing_question is None:
            session.add(
                QuestionAndReply(
                    task_id=task.id,
                    production_question_mail_id=source_mail.id,
                    question_text=clean_question,
                    status="AwaitingSalesReply",
                )
            )
    else:
        existing_open = (
            session.query(QuestionAndReply)
            .filter_by(task_id=task.id, status="AwaitingSalesReply")
            .order_by(QuestionAndReply.created_at.desc())
            .first()
        )
        if existing_open is None:
            session.add(QuestionAndReply(task_id=task.id, question_text=clean_question, status="AwaitingSalesReply"))

    task.status = "ProductionQuestioned"
    task.updated_at = now_utc()

    to_addresses = [salesperson_email] if salesperson_email else []
    cc = [ops_email]
    idem_source = source_mail.id if source_mail is not None else hashlib.sha256(clean_question.encode("utf-8")).hexdigest()[:16]
    idem = f"production-question:{task.id}:{idem_source}:{recipient_hash(to_addresses, cc)}"
    existing = session.query(OutboundMailJob).filter_by(idempotency_key=idem).one_or_none()
    if existing is not None:
        return existing

    if not to_addresses:
        record_exception_case(
            session,
            related_task_id=task.id,
            exception_type="MissingSalespersonEmail",
            severity="High",
            detail={
                "source_mail_id": source_mail.id if source_mail else None,
                "task_no": task.task_no,
                "message": "生产疑问无法转发：订单未识别销售发起人邮箱。",
            },
            source_mail_id=source_mail.id if source_mail else None,
        )

    body = "\n".join(
        [
            f"销售同事好，生产部对任务 {task.task_no} 提出以下疑问，请补充确认：",
            "",
            clean_question,
            "",
            "当前任务信息：",
            f"客户名称：{requirement.customer_name or ''}",
            f"产品/规格：{requirement.product_summary or ''}",
            f"数量：{requirement.quantity_text or ''}",
            f"期望交期：{requirement.expected_delivery_date or ''}",
            "",
            get_config(session, "bot_signature", "积木易搭AI机器人"),
        ]
    )
    job = OutboundMailJob(
        related_task_id=task.id,
        mail_type="ProductionQuestionForward",
        to_json=dumps(to_addresses),
        cc_json=dumps(cc),
        subject=f"[生产疑问][{task.task_no}] 请补充确认",
        body=body,
        idempotency_key=idem,
        status="Pending",
    )
    session.add(job)
    add_audit(session, "ProductionQuestionForwarded", "ProductionTask", task.id, {"source_mail_id": source_mail.id if source_mail else None})
    if source_mail is not None:
        enqueue_production_question_receipt(session, task, source_mail, clean_question)
    return job


def close_conversation_for_max_rounds(
    session: Session,
    task: ProductionTask,
    *,
    source_mail: MailMessage | None = None,
) -> OutboundMailJob:
    requirement = task.requirement
    if source_mail is not None:
        source_mail.related_task_id = task.id
    task.status = "Closed"
    task.closed_reason = "ConversationMaxRounds"
    task.updated_at = now_utc()

    sales_email = requirement.salesperson_email or ""
    production_addresses = []
    if source_mail is not None and source_mail.from_address:
        production_addresses.append(source_mail.from_address)
    production_addresses.extend(as_list(task.target_mail_to_json))
    to_addresses = []
    for address in [sales_email, *production_addresses]:
        if address and address not in to_addresses:
            to_addresses.append(address)
    ops_email = get_config(session, "ops_cc_email", "jinlei@jimuyida.com")
    cc_addresses = [ops_email] if ops_email else []
    idem = f"conversation-max-rounds:{task.id}:{recipient_hash(to_addresses, cc_addresses)}"
    existing = session.query(OutboundMailJob).filter_by(idempotency_key=idem).one_or_none()
    if existing is not None:
        return existing

    max_rounds = conversation_max_rounds(session, task)
    close_message = ""
    binding = workflow_binding_for_requirement(session, task.requirement_id)
    if binding is not None and binding.workflow_version_id:
        version = session.get(WorkflowVersion, binding.workflow_version_id)
        rules = loads(version.compiled_rules_json, {}) if version is not None else {}
        policy = rules.get("conversation_policy") if isinstance(rules, dict) else {}
        if isinstance(policy, dict):
            close_message = str(policy.get("message") or "").strip()
    body = "\n".join(
        [
            "各位好，",
            "",
            close_message or f"任务 {task.task_no} 的订单沟通会话已达到当前流程允许的最大往返次数（{max_rounds} 轮）。",
            "本次订单需求已关闭，请销售重新发起完整的订单需求邮件，或由商务人工介入处理。",
            "",
            "当前订单信息：",
            f"客户名称：{requirement.customer_name or ''}",
            f"产品/规格：{requirement.product_summary or ''}",
            f"数量：{requirement.quantity_text or ''}",
            f"期望交期：{requirement.expected_delivery_date or ''}",
            "",
            get_config(session, "bot_signature", "积木易搭AI机器人"),
        ]
    )
    job = OutboundMailJob(
        related_task_id=task.id,
        mail_type="ConversationClosedMaxRounds",
        to_json=dumps(to_addresses),
        cc_json=dumps(cc_addresses),
        subject=f"[订单沟通关闭][{task.task_no}] 已达到最大沟通轮数",
        body=body,
        idempotency_key=idem,
        status="Pending",
    )
    session.add(job)
    record_exception_case(
        session,
        related_task_id=task.id,
        exception_type="ConversationMaxRounds",
        severity="Medium",
        detail={"task_no": task.task_no, "max_rounds": max_rounds, "source_mail_id": source_mail.id if source_mail else None},
        source_mail_id=source_mail.id if source_mail else None,
    )
    add_audit(
        session,
        "ConversationClosedMaxRounds",
        "ProductionTask",
        task.id,
        {"max_rounds": max_rounds, "source_mail_id": source_mail.id if source_mail else None},
    )
    return job


def enqueue_production_question_receipt(
    session: Session,
    task: ProductionTask,
    source_mail: MailMessage,
    question_text: str,
) -> OutboundMailJob | None:
    to_address = (source_mail.from_address or "").strip()
    if not to_address:
        return None
    to_addresses = [to_address]
    cc_addresses: list[str] = []
    idem = f"production-question-receipt:{source_mail.id}:{recipient_hash(to_addresses, cc_addresses)}"
    existing = session.query(OutboundMailJob).filter_by(idempotency_key=idem).one_or_none()
    if existing is not None:
        return existing

    subject = source_mail.subject.strip() if source_mail.subject else f"生产疑问已收到 - {task.task_no}"
    if not subject.lower().startswith("re:"):
        subject = f"Re: {subject}"
    body = "\n".join(
        [
            "生产部同事好，",
            "",
            f"您关于任务 {task.task_no} 的疑问已收到，系统已转发销售人员补充确认。",
            "",
            "疑问内容：",
            question_text,
            "",
            "销售补充后，系统会更新生产任务单并重新下发生产。",
            "",
            get_config(session, "bot_signature", "积木易搭AI机器人"),
        ]
    )
    job = OutboundMailJob(
        related_task_id=task.id,
        mail_type="ProductionQuestionReceipt",
        to_json=dumps(to_addresses),
        cc_json=dumps(cc_addresses),
        subject=subject,
        body=body,
        idempotency_key=idem,
        status="Pending",
    )
    session.add(job)
    add_audit(session, "ProductionQuestionReceiptQueued", "ProductionTask", task.id, {"source_mail_id": source_mail.id})
    return job


def pending_confirmation_tasks_for_production(session: Session, production_email: str) -> list[ProductionTask]:
    sender = (production_email or "").lower().strip()
    production_addresses = production_department_addresses(session)
    rows = (
        session.query(ProductionTask)
        .filter(ProductionTask.status.in_(["TaskIssued", "Reissued"]))
        .order_by(ProductionTask.issued_at.desc(), ProductionTask.created_at.desc())
        .all()
    )
    if not sender or sender in production_addresses:
        return rows
    return [
        task
        for task in rows
        if sender in {address.lower() for address in as_list(task.target_mail_to_json) + as_list(task.target_mail_cc_json)}
    ]


def looks_like_status_query(text: str) -> bool:
    compact = re.sub(r"\s+", "", text)
    if looks_like_pending_task_query(compact):
        return False
    has_query_word = any(keyword in compact for keyword in STATUS_QUERY_KEYWORDS)
    has_intent_word = any(keyword in compact for keyword in STATUS_QUERY_INTENT_KEYWORDS)
    has_business_word = any(keyword in compact for keyword in ["需求", "订单", "任务", "排产", "生产"])
    return has_query_word and has_intent_word and has_business_word


def active_model_provider(session: Session) -> ModelProviderConfig | None:
    return session.query(ModelProviderConfig).filter_by(status="Active").first()


def status_label(status: str | None) -> str:
    labels = {
        "ReviewPending": "待初审",
        "ReviewFailed": "初审未通过/待补充",
        "ReviewPassed": "初审通过",
        "TaskDrafted": "任务草稿",
        "TaskIssued": "已下达生产",
        "ProductionQuestioned": "生产疑问/待销售补充",
        "ReissueDrafted": "重发草稿",
        "Reissued": "已重新下达",
        "CancelReview": "变更/取消待确认",
        "Closed": "已关闭",
    }
    return labels.get(status or "", status or "未知")


def task_brief(task: ProductionTask) -> dict[str, object]:
    requirement = task.requirement
    return {
        "task_no": task.task_no,
        "customer": requirement.customer_name or "未识别客户",
        "product": requirement.product_summary or "未识别产品",
        "quantity": requirement.quantity_text or "未识别数量",
        "expected_delivery": requirement.expected_delivery_date or "未识别交期",
        "external_order_no": requirement.external_order_no or "未识别订单号",
        "salesperson": requirement.salesperson_email or requirement.salesperson_name or "未知销售",
        "status": task.status,
        "status_label": status_label(task.status),
        "issued_at": task.issued_at.isoformat() if task.issued_at else "",
        "confirmed_at": task.confirmed_at.isoformat() if task.confirmed_at else "",
        "closed_reason": task.closed_reason or "",
    }


def requirement_brief(requirement: OrderRequirement, task: ProductionTask | None = None) -> dict[str, object]:
    return {
        "requirement_no": requirement.internal_order_no,
        "task_no": task.task_no if task else "",
        "customer": requirement.customer_name or "未识别客户",
        "product": requirement.product_summary or "未识别产品",
        "quantity": requirement.quantity_text or "未识别数量",
        "expected_delivery": requirement.expected_delivery_date or "未识别交期",
        "external_order_no": requirement.external_order_no or "未识别订单号",
        "requirement_status": requirement.status,
        "requirement_status_label": status_label(requirement.status),
        "task_status": task.status if task else "",
        "task_status_label": status_label(task.status) if task else "未生成生产任务",
        "created_at": requirement.created_at.isoformat(),
    }


def status_counts(rows: list[dict[str, object]], key: str) -> dict[str, int]:
    counts: dict[str, int] = {}
    for row in rows:
        raw = str(row.get(key) or "Unknown")
        label = status_label(raw)
        counts[label] = counts.get(label, 0) + 1
    return counts


def tasks_for_production_query(session: Session, production_email: str) -> list[ProductionTask]:
    sender = (production_email or "").lower().strip()
    production_addresses = production_department_addresses(session)
    rows = (
        session.query(ProductionTask)
        .join(OrderRequirement, OrderRequirement.id == ProductionTask.requirement_id)
        .order_by(ProductionTask.created_at.desc())
        .all()
    )
    if not sender or sender in production_addresses:
        return rows
    return [
        task
        for task in rows
        if sender in {address.lower() for address in as_list(task.target_mail_to_json) + as_list(task.target_mail_cc_json)}
    ]


def query_requested_task_nos(text: str) -> set[str]:
    return {task_no.upper() for task_no in TASK_NO_PATTERN.findall(text)}


def filter_tasks_by_requested_nos(tasks: list[ProductionTask], requested_task_nos: set[str]) -> list[ProductionTask]:
    if not requested_task_nos:
        return tasks
    return [task for task in tasks if task.task_no.upper() in requested_task_nos]


def build_sales_query_data(session: Session, source_mail: MailMessage, query_text: str) -> dict[str, object]:
    sender = (source_mail.from_address or "").lower().strip()
    requirements = (
        session.query(OrderRequirement)
        .filter(func.lower(OrderRequirement.salesperson_email) == sender)
        .order_by(OrderRequirement.created_at.desc())
        .all()
    )
    tasks = (
        session.query(ProductionTask)
        .join(OrderRequirement, OrderRequirement.id == ProductionTask.requirement_id)
        .filter(func.lower(OrderRequirement.salesperson_email) == sender)
        .order_by(ProductionTask.created_at.desc())
        .all()
    )
    requested_task_nos = query_requested_task_nos(query_text)
    tasks = filter_tasks_by_requested_nos(tasks, requested_task_nos)
    task_by_requirement = {task.requirement_id: task for task in tasks}
    requirement_rows = [
        requirement_brief(requirement, task_by_requirement.get(requirement.id))
        for requirement in requirements
        if not requested_task_nos or (task_by_requirement.get(requirement.id) and task_by_requirement[requirement.id].task_no.upper() in requested_task_nos)
    ]
    task_rows = [task_brief(task) for task in tasks]
    return {
        "role": "sales",
        "sender": source_mail.from_address,
        "query": query_text,
        "requested_task_nos": sorted(requested_task_nos),
        "summary": {
            "submitted_requirements": len(requirement_rows),
            "production_tasks": len(task_rows),
            "task_status_counts": status_counts(task_rows, "status"),
            "requirement_status_counts": status_counts(requirement_rows, "requirement_status"),
        },
        "latest_requirements": requirement_rows[:10],
        "latest_tasks": task_rows[:10],
    }


def build_production_query_data(session: Session, source_mail: MailMessage, query_text: str) -> dict[str, object]:
    tasks = tasks_for_production_query(session, source_mail.from_address)
    requested_task_nos = query_requested_task_nos(query_text)
    tasks = filter_tasks_by_requested_nos(tasks, requested_task_nos)
    task_rows = [task_brief(task) for task in tasks]
    return {
        "role": "production",
        "sender": source_mail.from_address,
        "query": query_text,
        "requested_task_nos": sorted(requested_task_nos),
        "summary": {
            "accepted_tasks": len(task_rows),
            "task_status_counts": status_counts(task_rows, "status"),
            "pending_confirmation": sum(1 for row in task_rows if row.get("status") in {"TaskIssued", "Reissued"}),
            "confirmed": sum(1 for row in task_rows if row.get("status") == "Closed" and row.get("closed_reason") == "ScheduledConfirmed"),
            "questioned": sum(1 for row in task_rows if row.get("status") == "ProductionQuestioned"),
        },
        "latest_tasks": task_rows[:10],
    }


def fallback_status_query_body(role: str, data: dict[str, object]) -> str:
    summary = data.get("summary") if isinstance(data.get("summary"), dict) else {}
    latest_tasks = data.get("latest_tasks") if isinstance(data.get("latest_tasks"), list) else []
    title = "您提交的需求状态及统计" if role == "sales" else "生产侧受理需求状态及统计"
    count_label = "提交需求数" if role == "sales" else "受理任务数"
    total_key = "submitted_requirements" if role == "sales" else "accepted_tasks"
    status_counts_text = "，".join(f"{key} {value}" for key, value in dict(summary.get("task_status_counts") or {}).items()) or "暂无"
    lines = [
        "您好，",
        "",
        f"以下为{title}：",
        f"- {count_label}：{summary.get(total_key, 0)}",
        f"- 生产任务数：{summary.get('production_tasks', summary.get('accepted_tasks', 0))}",
        f"- 状态分布：{status_counts_text}",
        "",
        "最近任务：",
    ]
    if latest_tasks:
        for index, row in enumerate(latest_tasks, start=1):
            lines.append(
                f"{index}. {row.get('task_no') or '未生成任务'} | {row.get('customer')} | {row.get('product')} | "
                f"{row.get('quantity')} | 交期 {row.get('expected_delivery')} | {row.get('status_label')}"
            )
    else:
        lines.append("- 暂无匹配记录。")
    return "\n".join(lines)


def render_status_query_body_with_llm(session: Session, role: str, data: dict[str, object], source_mail: MailMessage) -> str:
    config = active_model_provider(session)
    if config is None:
        return fallback_status_query_body(role, data)
    role_label = "销售人员" if role == "sales" else "生产部门"
    try:
        output = call_model(
            session,
            config,
            task_type="MailStatusQueryReply",
            related_object_type="MailMessage",
            related_object_id=source_mail.id,
            messages=[
                {
                    "role": "system",
                    "content": (
                        "你是商务生产任务单智能体，负责把结构化订单/生产任务查询结果组织成中文邮件正文。"
                        "只能依据输入数据回答，不要编造不存在的任务、数量、状态或日期。"
                        "语气简洁、商务化。输出邮件正文即可，不要输出主题，不要使用 Markdown 表格。"
                    ),
                },
                {
                    "role": "user",
                    "content": (
                        f"收件人角色：{role_label}\n"
                        f"原始查询：{source_mail.body_text}\n"
                        f"结构化查询结果 JSON：{dumps(data)}\n"
                        "请回复：1）先概述统计；2）列出最近或指定任务明细；3）如无记录，明确说明未查询到。"
                    ),
                },
            ],
        )
        content = extract_chat_content(output).strip()
        if content:
            return content
    except Exception as exc:
        add_audit(session, "LLMStatusQueryReplyFailed", "MailMessage", source_mail.id, {"error": str(exc)[:1000]})
    return fallback_status_query_body(role, data)


def enqueue_status_query_reply(session: Session, source_mail: MailMessage, *, role: str) -> OutboundMailJob | None:
    to_address = (source_mail.from_address or "").strip()
    if not to_address:
        return None
    query_text = f"{source_mail.subject}\n{source_mail.body_text}"
    data = build_sales_query_data(session, source_mail, query_text) if role == "sales" else build_production_query_data(session, source_mail, query_text)
    body = render_status_query_body_with_llm(session, role, data, source_mail)
    signature = get_config(session, "bot_signature", "积木易搭AI机器人")
    if signature and signature not in body:
        body = f"{body.rstrip()}\n\n{signature}"
    to_addresses = [to_address]
    cc_addresses: list[str] = []
    idem = f"{role}-status-query:{source_mail.id}:{recipient_hash(to_addresses, cc_addresses)}"
    existing = session.query(OutboundMailJob).filter_by(idempotency_key=idem).one_or_none()
    if existing is not None:
        return existing
    mail_type = "SalesDemandStatusQueryReply" if role == "sales" else "ProductionDemandStatusQueryReply"
    subject = f"Re: {'需求状态和统计查询' if role == 'sales' else '受理需求状态和统计查询'}"
    job = OutboundMailJob(
        mail_type=mail_type,
        to_json=dumps(to_addresses),
        cc_json=dumps(cc_addresses),
        subject=subject,
        body=body,
        idempotency_key=idem,
        status="Pending",
    )
    session.add(job)
    source_mail.classification = "SalesDemandStatusQuery" if role == "sales" else "ProductionDemandStatusQuery"
    source_mail.classification_confidence = max(source_mail.classification_confidence, 93)
    add_audit(session, f"{mail_type}Queued", "MailMessage", source_mail.id, {"to": to_addresses, "summary": data.get("summary")})
    return job


def handle_status_query_mail_command(session: Session, mail: MailMessage) -> OutboundMailJob | None:
    text = f"{mail.subject}\n{mail.body_text}"
    if not looks_like_status_query(text):
        return None
    sender = (mail.from_address or "").lower().strip()
    if not sender:
        return None
    if sender in production_department_addresses(session):
        return enqueue_status_query_reply(session, mail, role="production")
    bot_email = get_config(session, "bot_email", "bot.market@jimuyida.com").lower()
    if sender == bot_email:
        return None
    return enqueue_status_query_reply(session, mail, role="sales")


def enqueue_production_pending_tasks_reply(session: Session, source_mail: MailMessage) -> OutboundMailJob | None:
    to_address = (source_mail.from_address or "").strip()
    if not to_address:
        return None
    tasks = pending_confirmation_tasks_for_production(session, to_address)
    to_addresses = [to_address]
    cc_addresses: list[str] = []
    idem = f"production-pending-query:{source_mail.id}:{recipient_hash(to_addresses, cc_addresses)}"
    existing = session.query(OutboundMailJob).filter_by(idempotency_key=idem).one_or_none()
    if existing is not None:
        return existing

    if tasks:
        lines = [
            f"{index}. {task.task_no} | {task.requirement.customer_name or '未识别客户'} | {task.requirement.product_summary or '未识别产品'} | {task.requirement.quantity_text or '未识别数量'} | 交期 {task.requirement.expected_delivery_date or '未识别'} | 状态 {task.status}"
            for index, task in enumerate(tasks, start=1)
        ]
        instruction = "如需确认指定任务，请回复：确认排产 任务编号，例如：确认排产 PT-20260422-0001。"
    else:
        lines = ["暂无待确认生产任务。"]
        instruction = ""

    body = "\n".join(
        [
            "生产部同事好，",
            "",
            "当前待确认生产任务如下：",
            *lines,
            "",
            instruction,
            "",
            get_config(session, "bot_signature", "积木易搭AI机器人"),
        ]
    ).strip()
    job = OutboundMailJob(
        mail_type="ProductionPendingTasksQueryReply",
        to_json=dumps(to_addresses),
        cc_json=dumps(cc_addresses),
        subject="Re: 当前待确认生产任务",
        body=body,
        idempotency_key=idem,
        status="Pending",
    )
    session.add(job)
    add_audit(session, "ProductionPendingTasksQueryReplyQueued", "MailMessage", source_mail.id, {"count": len(tasks), "to": to_addresses})
    return job


def enqueue_production_confirmation_receipt(session: Session, task: ProductionTask, source_mail: MailMessage) -> OutboundMailJob | None:
    to_address = (source_mail.from_address or "").strip()
    if not to_address:
        return None
    to_addresses = [to_address]
    cc_addresses: list[str] = []
    idem = f"production-confirmation-receipt:{source_mail.id}:{recipient_hash(to_addresses, cc_addresses)}"
    existing = session.query(OutboundMailJob).filter_by(idempotency_key=idem).one_or_none()
    if existing is not None:
        return existing
    job = OutboundMailJob(
        related_task_id=task.id,
        mail_type="ProductionConfirmationReceipt",
        to_json=dumps(to_addresses),
        cc_json=dumps(cc_addresses),
        subject=f"Re: [生产确认][{task.task_no}] 已记录",
        body="\n".join(
            [
                "生产部同事好，",
                "",
                f"任务 {task.task_no} 的生产确认已记录，系统已关闭该任务并通知相关人员。",
                "",
                get_config(session, "bot_signature", "积木易搭AI机器人"),
            ]
        ),
        idempotency_key=idem,
        status="Pending",
    )
    session.add(job)
    add_audit(session, "ProductionConfirmationReceiptQueued", "ProductionTask", task.id, {"source_mail_id": source_mail.id})
    return job


def enqueue_closed_task_reply_rejected_notice(
    session: Session,
    task: ProductionTask,
    source_mail: MailMessage,
    *,
    reason: str | None = None,
) -> OutboundMailJob | None:
    to_address = (source_mail.from_address or "").strip()
    if not to_address:
        return None
    to_addresses = [to_address]
    cc_addresses: list[str] = []
    idem = f"closed-task-reply-rejected:{source_mail.id}:{recipient_hash(to_addresses, cc_addresses)}"
    existing = session.query(OutboundMailJob).filter_by(idempotency_key=idem).one_or_none()
    if existing is not None:
        return existing
    body = "\n".join(
        [
            "您好，",
            "",
            f"任务 {task.task_no} 已关闭，系统不会再自动处理本次回复。",
            f"关闭原因：{reason or task.closed_reason or 'Closed'}",
            "",
            "如需继续处理，请重新发起完整需求邮件，或联系商务部人工介入。",
            "",
            get_config(session, "bot_signature", "积木易搭AI机器人"),
        ]
    )
    job = OutboundMailJob(
        related_task_id=task.id,
        mail_type="ClosedTaskReplyRejected",
        to_json=dumps(to_addresses),
        cc_json=dumps(cc_addresses),
        subject=f"[任务已关闭][{task.task_no}] 本次回复未自动处理",
        body=body,
        idempotency_key=idem,
        status="Pending",
    )
    session.add(job)
    add_audit(session, "ClosedTaskReplyRejectedQueued", "ProductionTask", task.id, {"source_mail_id": source_mail.id})
    return job


def enqueue_sales_reply_no_open_question_notice(session: Session, task: ProductionTask, source_mail: MailMessage) -> OutboundMailJob | None:
    to_address = (source_mail.from_address or "").strip()
    if not to_address:
        return None
    to_addresses = [to_address]
    cc_addresses: list[str] = []
    idem = f"sales-reply-no-open-question:{source_mail.id}:{recipient_hash(to_addresses, cc_addresses)}"
    existing = session.query(OutboundMailJob).filter_by(idempotency_key=idem).one_or_none()
    if existing is not None:
        return existing
    body = "\n".join(
        [
            "销售同事好，",
            "",
            f"任务 {task.task_no} 当前没有待您答复的生产疑问，系统未重新下发生产任务单。",
            "",
            "如需变更订单，请回复“订单变更 + 任务号”；如需撤回，请回复“撤回需求 + 任务号”（生产确认排单前有效）。",
            "",
            get_config(session, "bot_signature", "积木易搭AI机器人"),
        ]
    )
    job = OutboundMailJob(
        related_task_id=task.id,
        mail_type="SalesReplyNoOpenQuestion",
        to_json=dumps(to_addresses),
        cc_json=dumps(cc_addresses),
        subject=f"[未重新下发][{task.task_no}] 当前无待答复生产疑问",
        body=body,
        idempotency_key=idem,
        status="Pending",
    )
    session.add(job)
    add_audit(session, "SalesReplyNoOpenQuestionQueued", "ProductionTask", task.id, {"source_mail_id": source_mail.id})
    return job


def enqueue_production_terminate_sales_notice(session: Session, task: ProductionTask, source_mail: MailMessage) -> OutboundMailJob | None:
    sales_email = (task.requirement.salesperson_email or "").strip()
    if not sales_email:
        return None
    to_addresses = [sales_email]
    cc_addresses: list[str] = []
    idem = f"production-terminate-sales:{source_mail.id}:{recipient_hash(to_addresses, cc_addresses)}"
    existing = session.query(OutboundMailJob).filter_by(idempotency_key=idem).one_or_none()
    if existing is not None:
        return existing
    body = "\n".join(
        [
            "销售同事好，",
            "",
            f"生产侧已终止任务 {task.task_no}，系统已关闭该任务。",
            "",
            "生产侧说明：",
            (source_mail.body_text or "").strip() or "生产侧未填写说明。",
            "",
            "如需继续处理，请重新发起完整需求邮件，或联系商务部人工介入。",
            "",
            get_config(session, "bot_signature", "积木易搭AI机器人"),
        ]
    )
    job = OutboundMailJob(
        related_task_id=task.id,
        mail_type="ProductionTerminateSalesNotice",
        to_json=dumps(to_addresses),
        cc_json=dumps(cc_addresses),
        subject=f"[生产终止][{task.task_no}] 任务已关闭",
        body=body,
        idempotency_key=idem,
        status="Pending",
    )
    session.add(job)
    add_audit(session, "ProductionTerminateSalesNoticeQueued", "ProductionTask", task.id, {"source_mail_id": source_mail.id})
    return job


def enqueue_production_terminate_receipt(session: Session, task: ProductionTask, source_mail: MailMessage) -> OutboundMailJob | None:
    to_address = (source_mail.from_address or "").strip()
    if not to_address:
        return None
    to_addresses = [to_address]
    cc_addresses: list[str] = []
    idem = f"production-terminate-receipt:{source_mail.id}:{recipient_hash(to_addresses, cc_addresses)}"
    existing = session.query(OutboundMailJob).filter_by(idempotency_key=idem).one_or_none()
    if existing is not None:
        return existing
    body = "\n".join(
        [
            "生产部同事好，",
            "",
            f"任务 {task.task_no} 的生产终止请求已记录，系统已关闭任务并通知销售侧。",
            "",
            get_config(session, "bot_signature", "积木易搭AI机器人"),
        ]
    )
    job = OutboundMailJob(
        related_task_id=task.id,
        mail_type="ProductionTerminateProductionNotice",
        to_json=dumps(to_addresses),
        cc_json=dumps(cc_addresses),
        subject=f"[生产终止已记录][{task.task_no}] 任务已关闭",
        body=body,
        idempotency_key=idem,
        status="Pending",
    )
    session.add(job)
    add_audit(session, "ProductionTerminateProductionNoticeQueued", "ProductionTask", task.id, {"source_mail_id": source_mail.id})
    return job


def record_production_termination(session: Session, task: ProductionTask, source_mail: MailMessage) -> list[OutboundMailJob]:
    source_mail.related_task_id = task.id
    if task.status == "Closed":
        job = enqueue_closed_task_reply_rejected_notice(session, task, source_mail)
        return [job] if job is not None else []
    task.status = "Closed"
    task.closed_reason = "ProductionTerminated"
    task.updated_at = now_utc()
    task.requirement.status = "Closed"
    task.requirement.updated_at = now_utc()
    open_questions = (
        session.query(QuestionAndReply)
        .filter_by(task_id=task.id, status="AwaitingSalesReply")
        .all()
    )
    for question in open_questions:
        question.status = "Answered"
        if not (question.reply_text or "").strip():
            question.reply_text = "生产侧已终止生产，任务关闭。"
        question.updated_at = now_utc()
    jobs = [
        enqueue_production_terminate_sales_notice(session, task, source_mail),
        enqueue_production_terminate_receipt(session, task, source_mail),
    ]
    valid_jobs = [job for job in jobs if job is not None]
    add_audit(session, "ProductionTerminatedTask", "ProductionTask", task.id, {"source_mail_id": source_mail.id, "outbound_job_ids": [job.id for job in valid_jobs]})
    return valid_jobs


def handle_production_mail_command(session: Session, mail: MailMessage) -> object | None:
    if (mail.from_address or "").lower() not in production_department_addresses(session):
        return None
    text = f"{mail.subject}\n{mail.body_text}"
    if looks_like_pending_task_query(text):
        mail.classification = "ProductionPendingTaskQuery"
        mail.classification_confidence = max(mail.classification_confidence, 92)
        return enqueue_production_pending_tasks_reply(session, mail)
    if looks_like_production_termination(text):
        task = find_task_for_mail(session, mail)
        if task is not None:
            mail.classification = "ProductionTerminateRequest"
            mail.classification_confidence = max(mail.classification_confidence, 93)
            return record_production_termination(session, task, mail)
    if not looks_like_production_confirmation(text):
        return None
    task = find_task_for_mail(session, mail)
    if task is None:
        mail.classification = "ProductionScheduleConfirmation"
        mail.classification_confidence = max(mail.classification_confidence, 88)
        record_exception_case(
            session,
            exception_type="ProductionConfirmationTaskLinkFailed",
            severity="Medium",
            detail={
                "source_mail_id": mail.id,
                "subject": mail.subject,
                "body": mail.body_text[:1000],
                "message": "生产确认邮件无法关联到具体生产任务。",
            },
            source_mail_id=mail.id,
        )
        return None
    mail.classification = "ProductionScheduleConfirmation"
    mail.classification_confidence = max(mail.classification_confidence, 92)
    mail.related_task_id = task.id
    job = record_production_feedback(session, task.id, "confirmed", mail.body_text)
    enqueue_production_confirmation_receipt(session, task, mail)
    return job


def _apply_reply_updates(task: ProductionTask, reply_text: str) -> list[str]:
    requirement = task.requirement
    extracted = extract_requirement("", reply_text, requirement.salesperson_email or "")
    updates: list[str] = []
    field_pairs = [
        ("customer_name", extracted.customer_name, "客户名称"),
        ("product_summary", extracted.product_summary, "产品/规格"),
        ("quantity_text", extracted.quantity_text, "数量"),
        ("expected_delivery_date", extracted.expected_delivery_date, "期望交期"),
        ("external_order_no", extracted.external_order_no, "订单号"),
    ]
    for attr, value, label in field_pairs:
        if value and value != getattr(requirement, attr):
            setattr(requirement, attr, value)
            updates.append(f"{label}：{value}")
    if updates:
        requirement.updated_at = now_utc()
    return updates


def record_sales_reply(
    session: Session,
    task_id: str,
    reply_text: str,
    *,
    source_mail: MailMessage | None = None,
) -> ProductionTaskVersion:
    task = session.get(ProductionTask, task_id)
    if task is None:
        raise ValueError("task not found")
    if task.status == "Closed":
        if source_mail is not None:
            source_mail.related_task_id = task.id
        raise ValueError(f"task is closed: {task.closed_reason or 'Closed'}")
    clean_reply = reply_text.strip()
    if not clean_reply:
        raise ValueError("sales reply is empty")
    if source_mail is not None:
        source_mail.related_task_id = task.id

    open_question = (
        session.query(QuestionAndReply)
        .filter_by(task_id=task.id, status="AwaitingSalesReply")
        .order_by(QuestionAndReply.created_at.desc())
        .first()
    )
    if open_question is None:
        record_exception_case(
            session,
            related_task_id=task.id,
            exception_type="SalesReplyWithoutOpenQuestion",
            severity="Medium",
            detail={"source_mail_id": source_mail.id if source_mail else None, "task_no": task.task_no, "reply_text": clean_reply[:1000]},
            source_mail_id=source_mail.id if source_mail else None,
        )
        raise ValueError("no open production question for sales reply")
    if any(keyword in clean_reply for keyword in INCOMPLETE_REPLY_KEYWORDS):
        if open_question is not None:
            open_question.reply_text = clean_reply
            open_question.sales_reply_mail_id = source_mail.id if source_mail else None
            open_question.status = "Incomplete"
            open_question.updated_at = now_utc()
        record_exception_case(
            session,
            related_task_id=task.id,
            exception_type="IncompleteSalesReply",
            severity="Medium",
            detail={"source_mail_id": source_mail.id if source_mail else None, "task_no": task.task_no, "reply_text": clean_reply},
            source_mail_id=source_mail.id if source_mail else None,
        )
        raise ValueError("sales reply is incomplete and needs manual follow-up")

    if source_mail is not None:
        existing_answer = session.query(QuestionAndReply).filter_by(sales_reply_mail_id=source_mail.id).one_or_none()
        if existing_answer is not None:
            version = (
                session.query(ProductionTaskVersion)
                .filter_by(task_id=task.id, version_no=task.current_version_no)
                .one()
            )
            return version

    updates = _apply_reply_updates(task, clean_reply)
    open_question.reply_text = clean_reply
    open_question.sales_reply_mail_id = source_mail.id if source_mail else None
    open_question.status = "Answered"
    open_question.updated_at = now_utc()

    version_no = task.current_version_no + 1
    subject_template, body_template, _ = task_template_for_requirement(session, task.requirement)
    context = task_context(session, task, version_no=version_no)
    body = render_template(body_template, context)
    body = "\n".join(
        [
            body.rstrip(),
            "",
            "销售补充答复：",
            clean_reply,
            "",
            "本次更新：",
            "\n".join(updates) if updates else "销售已补充说明，任务单字段未发生结构化变更。",
        ]
    )
    version = ProductionTaskVersion(
        task_id=task.id,
        version_no=version_no,
        subject=render_template(subject_template, context),
        body=body,
        status="Draft",
    )
    session.add(version)
    task.current_version_no = version_no
    task.status = "ReissueDrafted"
    task.updated_at = now_utc()
    add_audit(
        session,
        "SalesReplyReissueDrafted",
        "ProductionTask",
        task.id,
        {"version_no": version_no, "source_mail_id": source_mail.id if source_mail else None, "updates": updates},
    )
    enqueue_sales_reply_task_reissue(session, task, version)
    return version


def enqueue_sales_reply_task_reissue(
    session: Session,
    task: ProductionTask,
    version: ProductionTaskVersion,
) -> OutboundMailJob | None:
    to_addresses = as_list(task.target_mail_to_json)
    if not to_addresses:
        record_exception_case(
            session,
            related_task_id=task.id,
            exception_type="RoutingMissing",
            severity="High",
            detail={"task_no": task.task_no, "reason": "销售答复后无法重新下达：生产部门邮箱未配置"},
        )
        return None
    cc_addresses = as_list(task.target_mail_cc_json)
    idem = f"sales-reply-task-reissue:{task.id}:v{version.version_no}:{recipient_hash(to_addresses, cc_addresses)}"
    existing = session.query(OutboundMailJob).filter_by(idempotency_key=idem).one_or_none()
    if existing is not None:
        return existing

    version.status = "Sent"
    version.approved_by = "System"
    version.approved_at = now_utc()
    task.status = "Reissued"
    task.issued_at = now_utc()
    task.updated_at = now_utc()
    job = OutboundMailJob(
        related_task_id=task.id,
        related_version_id=version.id,
        mail_type="SalesReplyTaskReissue",
        to_json=dumps(to_addresses),
        cc_json=dumps(cc_addresses),
        subject=version.subject,
        body=version.body,
        idempotency_key=idem,
        status="Pending",
    )
    session.add(job)
    add_audit(
        session,
        "SalesReplyTaskReissueQueued",
        "ProductionTask",
        task.id,
        {"version_no": version.version_no, "outbound_job": job.id},
    )
    return job


def enqueue_sales_reply_reissue_receipt(session: Session, sent_job: OutboundMailJob) -> OutboundMailJob | None:
    if sent_job.mail_type != "SalesReplyTaskReissue" or not sent_job.related_task_id:
        return None
    task = session.get(ProductionTask, sent_job.related_task_id)
    if task is None:
        return None
    sales_email = task.requirement.salesperson_email or ""
    if not sales_email:
        return None
    to_addresses = [sales_email]
    cc_addresses: list[str] = []
    idem = f"sales-reply-reissue-receipt:{sent_job.id}:{recipient_hash(to_addresses, cc_addresses)}"
    existing = session.query(OutboundMailJob).filter_by(idempotency_key=idem).one_or_none()
    if existing is not None:
        return existing

    body = "\n".join(
        [
            "销售同事好，",
            "",
            f"您对任务 {task.task_no} 的补充答复已处理，系统已更新生产任务单并成功重新发送给生产部。",
            "",
            "当前任务信息：",
            f"客户名称：{task.requirement.customer_name or ''}",
            f"产品/规格：{task.requirement.product_summary or ''}",
            f"数量：{task.requirement.quantity_text or ''}",
            f"期望交期：{task.requirement.expected_delivery_date or ''}",
            "",
            get_config(session, "bot_signature", "积木易搭AI机器人"),
        ]
    )
    job = OutboundMailJob(
        related_task_id=task.id,
        related_version_id=sent_job.related_version_id,
        mail_type="SalesReplyReissueReceipt",
        to_json=dumps(to_addresses),
        cc_json=dumps(cc_addresses),
        subject=f"[已重新下达][{task.task_no}] 销售补充已发送生产",
        body=body,
        idempotency_key=idem,
        status="Pending",
    )
    session.add(job)
    add_audit(session, "SalesReplyReissueReceiptQueued", "ProductionTask", task.id, {"source_outbound_job": sent_job.id})
    return job


def enqueue_requirement_supplement_receipt(session: Session, sent_job: OutboundMailJob) -> OutboundMailJob | None:
    if sent_job.mail_type != "RequirementSupplementTaskIssue" or not sent_job.related_task_id:
        return None
    task = session.get(ProductionTask, sent_job.related_task_id)
    if task is None:
        return None
    sales_email = task.requirement.salesperson_email or ""
    if not sales_email:
        return None
    to_addresses = [sales_email]
    cc_addresses: list[str] = []
    idem = f"requirement-supplement-receipt:{sent_job.id}:{recipient_hash(to_addresses, cc_addresses)}"
    existing = session.query(OutboundMailJob).filter_by(idempotency_key=idem).one_or_none()
    if existing is not None:
        return existing

    body = "\n".join(
        [
            "销售同事好，",
            "",
            f"您补充的订单信息已处理，系统已生成任务 {task.task_no} 并成功发送给生产部。",
            "",
            "当前任务信息：",
            f"客户名称：{task.requirement.customer_name or ''}",
            f"产品/规格：{task.requirement.product_summary or ''}",
            f"数量：{task.requirement.quantity_text or ''}",
            f"期望交期：{task.requirement.expected_delivery_date or ''}",
            f"订单号：{task.requirement.external_order_no or ''}",
            "",
            get_config(session, "bot_signature", "积木易搭AI机器人"),
        ]
    )
    job = OutboundMailJob(
        related_task_id=task.id,
        related_version_id=sent_job.related_version_id,
        mail_type="RequirementSupplementAcceptedReceipt",
        to_json=dumps(to_addresses),
        cc_json=dumps(cc_addresses),
        subject=f"[已下达生产][{task.task_no}] 补充信息已发送生产",
        body=body,
        idempotency_key=idem,
        status="Pending",
    )
    session.add(job)
    add_audit(session, "RequirementSupplementReceiptQueued", "ProductionTask", task.id, {"source_outbound_job": sent_job.id})
    return job


def enqueue_sales_withdrawn_notice(session: Session, task: ProductionTask, source_mail: MailMessage) -> OutboundMailJob | None:
    sales_email = (task.requirement.salesperson_email or "").strip()
    if not sales_email:
        return None
    to_addresses = [sales_email]
    cc_addresses: list[str] = []
    idem = f"sales-demand-withdrawn:{source_mail.id}:{recipient_hash(to_addresses, cc_addresses)}"
    existing = session.query(OutboundMailJob).filter_by(idempotency_key=idem).one_or_none()
    if existing is not None:
        return existing
    body = "\n".join(
        [
            "销售同事好，",
            "",
            f"任务 {task.task_no} 已按您的请求撤回，系统已关闭该任务，不会继续推进生产排单。",
            "",
            "如需重新发起，请发送新的订单需求邮件。",
            "",
            get_config(session, "bot_signature", "积木易搭AI机器人"),
        ]
    )
    job = OutboundMailJob(
        related_task_id=task.id,
        mail_type="SalesDemandWithdrawn",
        to_json=dumps(to_addresses),
        cc_json=dumps(cc_addresses),
        subject=f"[需求已撤回][{task.task_no}] 任务已关闭",
        body=body,
        idempotency_key=idem,
        status="Pending",
    )
    session.add(job)
    add_audit(session, "SalesDemandWithdrawnQueued", "ProductionTask", task.id, {"source_mail_id": source_mail.id})
    return job


def enqueue_production_withdrawn_notice(session: Session, task: ProductionTask, source_mail: MailMessage) -> OutboundMailJob | None:
    to_addresses = as_list(task.target_mail_to_json)
    cc_addresses = as_list(task.target_mail_cc_json)
    if not to_addresses:
        return None
    idem = f"production-demand-withdrawn:{source_mail.id}:{recipient_hash(to_addresses, cc_addresses)}"
    existing = session.query(OutboundMailJob).filter_by(idempotency_key=idem).one_or_none()
    if existing is not None:
        return existing
    body = "\n".join(
        [
            "生产部同事好，",
            "",
            f"销售已撤回任务 {task.task_no}，该任务已关闭，请停止后续排单与生产动作。",
            "",
            get_config(session, "bot_signature", "积木易搭AI机器人"),
        ]
    )
    job = OutboundMailJob(
        related_task_id=task.id,
        mail_type="ProductionDemandWithdrawn",
        to_json=dumps(to_addresses),
        cc_json=dumps(cc_addresses),
        subject=f"[需求撤回][{task.task_no}] 请停止排单",
        body=body,
        idempotency_key=idem,
        status="Pending",
    )
    session.add(job)
    add_audit(session, "ProductionDemandWithdrawnQueued", "ProductionTask", task.id, {"source_mail_id": source_mail.id})
    return job


def enqueue_sales_withdraw_rejected_notice(
    session: Session,
    task: ProductionTask,
    source_mail: MailMessage,
    *,
    reason: str,
) -> OutboundMailJob | None:
    sales_email = (task.requirement.salesperson_email or "").strip() or (source_mail.from_address or "").strip()
    if not sales_email:
        return None
    to_addresses = [sales_email]
    cc_addresses: list[str] = []
    idem = f"sales-demand-withdraw-rejected:{source_mail.id}:{recipient_hash(to_addresses, cc_addresses)}"
    existing = session.query(OutboundMailJob).filter_by(idempotency_key=idem).one_or_none()
    if existing is not None:
        return existing
    body = "\n".join(
        [
            "销售同事好，",
            "",
            f"任务 {task.task_no} 撤回失败：{reason}",
            "",
            "如需进一步处理，请联系商务部人工介入。",
            "",
            get_config(session, "bot_signature", "积木易搭AI机器人"),
        ]
    )
    job = OutboundMailJob(
        related_task_id=task.id,
        mail_type="SalesDemandWithdrawRejected",
        to_json=dumps(to_addresses),
        cc_json=dumps(cc_addresses),
        subject=f"[撤回失败][{task.task_no}] 请人工处理",
        body=body,
        idempotency_key=idem,
        status="Pending",
    )
    session.add(job)
    add_audit(session, "SalesDemandWithdrawRejectedQueued", "ProductionTask", task.id, {"source_mail_id": source_mail.id, "reason": reason})
    return job


def enqueue_manual_close_sales_notice(
    session: Session,
    task: ProductionTask,
    *,
    reason: str,
) -> OutboundMailJob | None:
    sales_email = (task.requirement.salesperson_email or "").strip()
    if not sales_email:
        return None
    to_addresses = [sales_email]
    cc_addresses: list[str] = []
    idem = f"manual-close-sales:{task.id}:{recipient_hash(to_addresses, cc_addresses)}"
    existing = session.query(OutboundMailJob).filter_by(idempotency_key=idem).one_or_none()
    if existing is not None:
        return existing
    body_lines = [
        "销售同事好，",
        "",
        f"任务 {task.task_no} 已由商务人员手动强制关闭。",
    ]
    if reason:
        body_lines.extend(["", f"关闭说明：{reason}"])
    body_lines.extend(["", get_config(session, "bot_signature", "积木易搭AI机器人")])
    job = OutboundMailJob(
        related_task_id=task.id,
        mail_type="TaskManualClosedSales",
        to_json=dumps(to_addresses),
        cc_json=dumps(cc_addresses),
        subject=f"[任务手动关闭][{task.task_no}] 商务已关闭任务",
        body="\n".join(body_lines),
        idempotency_key=idem,
        status="Pending",
    )
    session.add(job)
    add_audit(session, "TaskManualClosedSalesQueued", "ProductionTask", task.id, {"task_no": task.task_no})
    return job


def enqueue_manual_close_production_notice(
    session: Session,
    task: ProductionTask,
    *,
    reason: str,
) -> OutboundMailJob | None:
    to_addresses = as_list(task.target_mail_to_json)
    cc_addresses = as_list(task.target_mail_cc_json)
    if not to_addresses:
        return None
    idem = f"manual-close-production:{task.id}:{recipient_hash(to_addresses, cc_addresses)}"
    existing = session.query(OutboundMailJob).filter_by(idempotency_key=idem).one_or_none()
    if existing is not None:
        return existing
    body_lines = [
        "生产部同事好，",
        "",
        f"任务 {task.task_no} 已由商务人员手动强制关闭，请停止后续处理。",
    ]
    if reason:
        body_lines.extend(["", f"关闭说明：{reason}"])
    body_lines.extend(["", get_config(session, "bot_signature", "积木易搭AI机器人")])
    job = OutboundMailJob(
        related_task_id=task.id,
        mail_type="TaskManualClosedProduction",
        to_json=dumps(to_addresses),
        cc_json=dumps(cc_addresses),
        subject=f"[任务手动关闭][{task.task_no}] 请停止处理",
        body="\n".join(body_lines),
        idempotency_key=idem,
        status="Pending",
    )
    session.add(job)
    add_audit(session, "TaskManualClosedProductionQueued", "ProductionTask", task.id, {"task_no": task.task_no})
    return job


def force_close_task_manual(
    session: Session,
    task_id: str,
    *,
    reason: str = "",
    actor: str = "business-owner",
) -> list[OutboundMailJob]:
    task = session.get(ProductionTask, task_id)
    if task is None:
        raise ValueError("task not found")
    if task.status == "Closed":
        raise ValueError("task is already closed")

    task.status = "Closed"
    task.closed_reason = "ManualForceClosed"
    task.manual_takeover = True
    task.updated_at = now_utc()
    task.requirement.status = "Closed"
    task.requirement.updated_at = now_utc()

    open_questions = (
        session.query(QuestionAndReply)
        .filter_by(task_id=task.id, status="AwaitingSalesReply")
        .all()
    )
    for question in open_questions:
        question.status = "Answered"
        if not (question.reply_text or "").strip():
            question.reply_text = "任务已由商务部手动关闭。"
        question.updated_at = now_utc()

    outbound_jobs = [
        enqueue_manual_close_sales_notice(session, task, reason=reason.strip()),
        enqueue_manual_close_production_notice(session, task, reason=reason.strip()),
    ]
    valid_jobs = [job for job in outbound_jobs if job is not None]
    add_audit(
        session,
        "TaskManualForceClosed",
        "ProductionTask",
        task.id,
        {"reason": reason.strip(), "outbound_job_ids": [job.id for job in valid_jobs]},
        actor,
    )
    return valid_jobs


def record_order_change_or_cancel(session: Session, mail: MailMessage, task: ProductionTask) -> ProductionTaskVersion | None:
    mail.related_task_id = task.id
    if mail.classification == "OrderCancelRequest":
        text = f"{mail.subject}\n{mail.body_text}"
        requested_task_nos = query_requested_task_nos(text)
        task_no_upper = task.task_no.upper()
        if not requested_task_nos:
            reason = "未识别到任务号，请按“撤回需求 + 任务号”格式发送。"
            record_exception_case(
                session,
                related_task_id=task.id,
                exception_type="OrderCancelTaskNoMissing",
                severity="Medium",
                detail={"source_mail_id": mail.id, "subject": mail.subject, "body": mail.body_text[:1000], "message": reason},
                source_mail_id=mail.id,
            )
            enqueue_sales_withdraw_rejected_notice(session, task, mail, reason=reason)
            add_audit(session, "OrderCancelRejectedMissingTaskNo", "ProductionTask", task.id, {"mail_id": mail.id})
            return None
        if task_no_upper not in requested_task_nos:
            reason = f"邮件中的任务号与当前任务不一致（当前任务号：{task.task_no}）。"
            record_exception_case(
                session,
                related_task_id=task.id,
                exception_type="OrderCancelTaskNoMismatch",
                severity="Medium",
                detail={"source_mail_id": mail.id, "subject": mail.subject, "body": mail.body_text[:1000], "message": reason},
                source_mail_id=mail.id,
            )
            enqueue_sales_withdraw_rejected_notice(session, task, mail, reason=reason)
            add_audit(session, "OrderCancelRejectedTaskNoMismatch", "ProductionTask", task.id, {"mail_id": mail.id})
            return None
        if task.closed_reason == "ScheduledConfirmed" or task.confirmed_at is not None:
            reason = "生产已确认排单，任务不可自动撤回。"
            record_exception_case(
                session,
                related_task_id=task.id,
                exception_type="OrderCancelAfterProductionConfirmed",
                severity="High",
                detail={"source_mail_id": mail.id, "subject": mail.subject, "body": mail.body_text[:1000], "message": reason},
                source_mail_id=mail.id,
            )
            enqueue_sales_withdraw_rejected_notice(session, task, mail, reason=reason)
            add_audit(session, "OrderCancelRejectedConfirmed", "ProductionTask", task.id, {"mail_id": mail.id})
            return None
        if task.status == "Closed":
            reason = f"任务已关闭（{task.closed_reason or 'Closed'}），无需重复撤回。"
            enqueue_sales_withdraw_rejected_notice(session, task, mail, reason=reason)
            add_audit(session, "OrderCancelRejectedAlreadyClosed", "ProductionTask", task.id, {"mail_id": mail.id})
            return None

        task.status = "Closed"
        task.closed_reason = "WithdrawnBySales"
        task.manual_takeover = False
        task.updated_at = now_utc()
        task.requirement.status = "Closed"
        task.requirement.updated_at = now_utc()
        open_questions = (
            session.query(QuestionAndReply)
            .filter_by(task_id=task.id, status="AwaitingSalesReply")
            .all()
        )
        for question in open_questions:
            question.status = "Answered"
            if not (question.reply_text or "").strip():
                question.reply_text = "销售已撤回需求，任务关闭。"
            question.updated_at = now_utc()
        enqueue_sales_withdrawn_notice(session, task, mail)
        enqueue_production_withdrawn_notice(session, task, mail)
        add_audit(session, "OrderWithdrawnBySales", "ProductionTask", task.id, {"mail_id": mail.id})
        return None

    if task.status == "Closed" or task.closed_reason == "ScheduledConfirmed":
        task.manual_takeover = True
        task.updated_at = now_utc()
        record_exception_case(
            session,
            related_task_id=task.id,
            exception_type="ScheduledOrderChangeManualReview",
            severity="High",
            detail={"source_mail_id": mail.id, "subject": mail.subject, "body": mail.body_text[:1000]},
            source_mail_id=mail.id,
        )
        add_audit(session, "ScheduledOrderChangeManualReview", "ProductionTask", task.id, {"mail_id": mail.id})
        return None

    updates = _apply_reply_updates(task, mail_text_with_attachments(session, mail))
    version_no = task.current_version_no + 1
    subject_template, body_template, _ = task_template_for_requirement(session, task.requirement)
    context = task_context(session, task, version_no=version_no)
    body = "\n".join(
        [
            render_template(body_template, context).rstrip(),
            "",
            "订单变更说明：",
            mail.body_text.strip(),
            "",
            "本次变更：",
            "\n".join(updates) if updates else "系统未识别到结构化字段变化，请人工确认邮件变更内容。",
        ]
    )
    version = ProductionTaskVersion(
        task_id=task.id,
        version_no=version_no,
        subject=render_template(subject_template, context),
        body=body,
        status="Draft",
    )
    session.add(version)
    task.current_version_no = version_no
    task.status = "ReissueDrafted"
    task.updated_at = now_utc()
    add_audit(session, "OrderChangeReissueDrafted", "ProductionTask", task.id, {"mail_id": mail.id, "updates": updates})
    return version


def apply_llm_classification_fallback(session: Session, mail: MailMessage, source_text: str) -> LLMMailClassification | None:
    try:
        result = classify_mail_with_llm(session, mail, source_text)
    except Exception as exc:
        add_audit(session, "LLMMailClassificationFailed", "MailMessage", mail.id, {"error": str(exc)[:1000]})
        return None
    if result is None:
        return None
    add_audit(
        session,
        "LLMMailClassificationApplied",
        "MailMessage",
        mail.id,
        {"classification": result.classification, "confidence": result.confidence, "reason": result.reason},
    )
    if result.classification != "NonTarget":
        mail.classification = result.classification
        mail.classification_confidence = max(mail.classification_confidence, result.confidence)
    return result


def apply_rule_classification_refresh(session: Session, mail: MailMessage, source_text: str) -> bool:
    classification, confidence = classify_mail(mail.subject, source_text, mail.from_address)
    if classification == "NonTarget" or confidence < mail.classification_confidence:
        return False
    mail.classification = classification
    mail.classification_confidence = confidence
    add_audit(
        session,
        "RuleMailClassificationRefreshed",
        "MailMessage",
        mail.id,
        {"classification": classification, "confidence": confidence},
    )
    return True


def handle_classified_mail(session: Session, mail: MailMessage) -> object | None:
    if mail.classification == "SalesOrderRequirement":
        return create_task_from_mail(session, mail)
    if find_requirement_for_supplement_reply(session, mail) is not None:
        return handle_requirement_supplement_reply(session, mail)

    task = find_task_for_mail(session, mail)
    if task is None:
        record_exception_case(
            session,
            exception_type="MailTaskLinkFailed",
            severity="Medium",
            detail={
                "source_mail_id": mail.id,
                "classification": mail.classification,
                "subject": mail.subject,
            },
            source_mail_id=mail.id,
        )
        return None

    if task.status == "Closed" and mail.classification in {"OrderChangeRequest", "ProductionScheduleConfirmation", "ProductionQuestion", "SalesClarificationReply"}:
        return enqueue_closed_task_reply_rejected_notice(session, task, mail)
    if mail.classification == "SalesClarificationReply":
        open_question = (
            session.query(QuestionAndReply)
            .filter_by(task_id=task.id, status="AwaitingSalesReply")
            .order_by(QuestionAndReply.created_at.desc())
            .first()
        )
        if open_question is None:
            mail.related_task_id = task.id
            record_exception_case(
                session,
                related_task_id=task.id,
                exception_type="SalesReplyWithoutOpenQuestion",
                severity="Medium",
                detail={"source_mail_id": mail.id, "task_no": task.task_no, "reply_text": mail.body_text[:1000]},
                source_mail_id=mail.id,
            )
            return enqueue_sales_reply_no_open_question_notice(session, task, mail)

    if mail.classification in {"OrderChangeRequest", "OrderCancelRequest"}:
        return record_order_change_or_cancel(session, mail, task)
    if mail.classification == "ProductionScheduleConfirmation":
        mail.related_task_id = task.id
        return record_production_feedback(session, task.id, "confirmed", mail.body_text)
    if mail.classification == "ProductionQuestion":
        return record_production_question(session, task.id, mail.body_text, source_mail=mail)
    if mail.classification == "SalesClarificationReply":
        return record_sales_reply(session, task.id, mail.body_text, source_mail=mail)
    return None


def record_non_target_exception(
    session: Session,
    mail: MailMessage,
    *,
    llm_result: LLMMailClassification | None = None,
) -> None:
    task = find_task_for_mail(session, mail)
    detail = {
        "source_mail_id": mail.id,
        "subject": mail.subject,
        "from_address": mail.from_address,
        "rule_classification": "NonTarget",
        "llm_classification": llm_result.classification if llm_result else "Unavailable",
        "llm_reason": llm_result.reason if llm_result else "",
        "conversation_task_id": task.id if task else None,
        "task_no": task.task_no if task else None,
    }
    record_exception_case(
        session,
        related_task_id=task.id if task else None,
        exception_type="NonTarget",
        severity="Low",
        detail=detail,
        source_mail_id=mail.id,
    )


def process_inbound_mail(session: Session, mail: MailMessage) -> object | None:
    production_command = handle_production_mail_command(session, mail)
    if production_command is not None:
        return production_command
    status_query_command = handle_status_query_mail_command(session, mail)
    if status_query_command is not None:
        return status_query_command
    if find_requirement_for_supplement_reply(session, mail) is not None:
        return handle_requirement_supplement_reply(session, mail)
    if mail.classification == "SalesOrderRequirement":
        return create_task_from_mail(session, mail)
    if mail.classification in {"NonTarget", "BounceOrAutoReply"}:
        if mail.classification == "NonTarget" and (mail.from_address or "").lower() in production_department_addresses(session):
            task = find_task_for_mail(session, mail)
            if task is not None and looks_like_question(f"{mail.subject}\n{mail.body_text}"):
                mail.classification = "ProductionQuestion"
                mail.classification_confidence = max(mail.classification_confidence, 86)
                return record_production_question(session, task.id, mail.body_text, source_mail=mail)
        if mail.classification == "NonTarget":
            source_text = mail_text_with_attachments(session, mail)
            if apply_rule_classification_refresh(session, mail, source_text):
                return handle_classified_mail(session, mail)
            llm_result = apply_llm_classification_fallback(session, mail, source_text)
            if mail.classification != "NonTarget":
                return handle_classified_mail(session, mail)
            record_non_target_exception(session, mail, llm_result=llm_result)
            return None
        record_exception_case(
            session,
            exception_type=mail.classification,
            severity="Low",
            detail={"source_mail_id": mail.id, "classification": mail.classification, "subject": mail.subject, "message": f"邮件分类为 {mail.classification}，未进入订单流程。"},
            source_mail_id=mail.id,
        )
        return None

    task = find_task_for_mail(session, mail)
    if task is None:
        record_exception_case(
            session,
            exception_type="MailTaskLinkFailed",
            severity="Medium",
            detail={"source_mail_id": mail.id, "classification": mail.classification, "subject": mail.subject, "message": f"邮件分类为 {mail.classification}，但无法关联到生产任务。"},
            source_mail_id=mail.id,
        )
        return None

    if task.status == "Closed" and mail.classification in {"OrderChangeRequest", "ProductionScheduleConfirmation", "ProductionQuestion", "SalesClarificationReply"}:
        return enqueue_closed_task_reply_rejected_notice(session, task, mail)
    if mail.classification == "SalesClarificationReply":
        open_question = (
            session.query(QuestionAndReply)
            .filter_by(task_id=task.id, status="AwaitingSalesReply")
            .order_by(QuestionAndReply.created_at.desc())
            .first()
        )
        if open_question is None:
            mail.related_task_id = task.id
            record_exception_case(
                session,
                related_task_id=task.id,
                exception_type="SalesReplyWithoutOpenQuestion",
                severity="Medium",
                detail={"source_mail_id": mail.id, "task_no": task.task_no, "reply_text": mail.body_text[:1000]},
                source_mail_id=mail.id,
            )
            return enqueue_sales_reply_no_open_question_notice(session, task, mail)

    if mail.classification in {"OrderChangeRequest", "OrderCancelRequest"}:
        return record_order_change_or_cancel(session, mail, task)
    if mail.classification == "ProductionScheduleConfirmation":
        mail.related_task_id = task.id
        return record_production_feedback(session, task.id, "confirmed", mail.body_text)
    if mail.classification == "ProductionQuestion":
        return record_production_question(session, task.id, mail.body_text, source_mail=mail)
    if mail.classification == "SalesClarificationReply":
        return record_sales_reply(session, task.id, mail.body_text, source_mail=mail)

    record_exception_case(
        session,
        related_task_id=task.id,
        exception_type="UnsupportedInboundClassification",
        severity="Low",
        detail={"source_mail_id": mail.id, "classification": mail.classification, "message": f"邮件分类为 {mail.classification}，未触发自动流程。"},
        source_mail_id=mail.id,
    )
    return None


def required_missing_fields(requirement: OrderRequirement) -> list[str]:
    checks = [
        ("客户名称", requirement.customer_name),
        ("产品/规格", requirement.product_summary),
        ("数量", requirement.quantity_text),
        ("期望交期", requirement.expected_delivery_date),
    ]
    return [label for label, value in checks if not value]


def resolve_exception_case(session: Session, exception_id: str, note: str = "", actor: str = "business-owner") -> ExceptionCase:
    case = session.get(ExceptionCase, exception_id)
    if case is None:
        raise ValueError("exception not found")
    case.status = "Resolved"
    add_audit(session, "ExceptionResolved", "ExceptionCase", case.id, {"note": note}, actor)
    return case


def apply_exception_requirement_patch(
    session: Session,
    exception_id: str,
    fields: dict[str, str | None],
    *,
    clear_risk_flags: bool = True,
    actor: str = "business-owner",
) -> ProductionTask | None:
    case = session.get(ExceptionCase, exception_id)
    if case is None:
        raise ValueError("exception not found")
    detail = loads(case.detail, {})
    requirement_id = detail.get("requirement_id")
    if not requirement_id:
        raise ValueError("exception is not linked to an order requirement")
    requirement = session.get(OrderRequirement, requirement_id)
    if requirement is None:
        raise ValueError("order requirement not found")

    allowed_fields = {
        "customer_name",
        "product_summary",
        "quantity_text",
        "expected_delivery_date",
        "external_order_no",
        "salesperson_email",
        "salesperson_name",
    }
    updates: dict[str, str] = {}
    for key, value in fields.items():
        if key in allowed_fields and value not in (None, ""):
            setattr(requirement, key, value)
            updates[key] = value
    missing_fields = required_missing_fields(requirement)
    risk_flags = [] if clear_risk_flags else as_list(requirement.risk_flags_json)
    workflow_failures: list[ReviewFailure] = []
    workflow_missing_fields: list[str] = []
    workflow_risk_flags: list[str] = []
    workflow_binding: RequirementWorkflowBinding | None = None
    mail = session.get(MailMessage, requirement.source_mail_id)
    if mail is not None:
        source_text = mail_text_with_attachments(session, mail)
        workflow_binding, workflow_failures, workflow_missing_fields, workflow_risk_flags = upsert_requirement_workflow_binding(
            session,
            requirement,
            mail,
            source_text,
        )
    merged_missing_fields: list[str] = []
    for label in [*missing_fields, *workflow_missing_fields]:
        if label and label not in merged_missing_fields:
            merged_missing_fields.append(label)
    merged_risk_flags: list[str] = []
    for flag in [*risk_flags, *workflow_risk_flags]:
        if flag and flag not in merged_risk_flags:
            merged_risk_flags.append(flag)

    requirement.missing_fields_json = dumps(merged_missing_fields)
    requirement.risk_flags_json = dumps(merged_risk_flags)
    requirement.updated_at = now_utc()

    detail.update(
        {
            "missing_fields": merged_missing_fields,
            "risk_flags": merged_risk_flags,
            "updates": updates,
            "workflow_review_failures": serialize_review_failures(workflow_failures),
            "workflow_code": workflow_binding.workflow_code if workflow_binding is not None else None,
        }
    )
    case.detail = dumps(detail)
    if merged_missing_fields or merged_risk_flags or workflow_failures:
        requirement.status = "ReviewFailed"
        add_audit(session, "ExceptionPatchIncomplete", "ExceptionCase", case.id, detail, actor)
        return None

    route_to, _route_cc, _binding = routing_for_requirement(session, requirement)
    if not route_to:
        requirement.status = "ReviewFailed"
        detail.update({"risk_flags": ["流程路由邮箱未配置"]})
        case.detail = dumps(detail)
        add_audit(session, "ExceptionPatchRoutingMissing", "ExceptionCase", case.id, detail, actor)
        return None

    task = draft_task_from_requirement(session, requirement, mail)
    approve_task(session, task.id, actor="System")
    case.related_task_id = task.id
    case.status = "Resolved"
    add_audit(session, "ExceptionResolvedByRequirementPatch", "ExceptionCase", case.id, {"task_id": task.id, "updates": updates}, actor)
    return task


def record_production_feedback(session: Session, task_id: str, feedback_type: str, note: str = "") -> OutboundMailJob:
    task = session.get(ProductionTask, task_id)
    if task is None:
        raise ValueError("task not found")
    if task.status == "Closed":
        raise ValueError(f"task is closed: {task.closed_reason or 'Closed'}")
    requirement = task.requirement
    ops_email = get_config(session, "ops_cc_email", "jinlei@jimuyida.com")
    ceo_email = get_config(session, "ceo_email", "dingyong@jimuyida.com")
    salesperson_email = requirement.salesperson_email or ""

    if feedback_type == "confirmed":
        task.status = "Closed"
        task.confirmed_at = now_utc()
        task.closed_reason = "ScheduledConfirmed"
        task.updated_at = now_utc()
        cc = [ceo_email, ops_email]
        if salesperson_email:
            cc.insert(1, salesperson_email)
        to_addresses: list[str] = []
        subject = f"[生产确认][{task.task_no}] 已确认排产"
        body = f"生产部已确认任务 {task.task_no} 安排生产。\n\n{note}".strip()
        mail_type = "ProductionConfirmed"
    elif feedback_type == "rejected":
        task.status = "ProductionQuestioned"
        task.updated_at = now_utc()
        cc = [ops_email]
        to_addresses = [salesperson_email] if salesperson_email else []
        subject = f"[生产驳回][{task.task_no}] 需补充确认"
        body = f"生产部驳回或提出疑问：\n{note or '请补充详细信息。'}"
        mail_type = "ProductionRejected"
    else:
        raise ValueError("feedback_type must be confirmed or rejected")

    idem = f"feedback:{feedback_type}:{task.id}:{recipient_hash(to_addresses, cc)}"
    existing = session.query(OutboundMailJob).filter_by(idempotency_key=idem).one_or_none()
    if existing is not None:
        return existing
    job = OutboundMailJob(
        related_task_id=task.id,
        mail_type=mail_type,
        to_json=dumps(to_addresses),
        cc_json=dumps(cc),
        subject=subject,
        body=body,
        idempotency_key=idem,
        status="Pending",
    )
    session.add(job)
    add_audit(session, mail_type, "ProductionTask", task.id, {"cc": cc, "note": note})
    return job


def dashboard(session: Session) -> dict:
    statuses = dict(
        session.query(ProductionTask.status, func.count(ProductionTask.id))
        .group_by(ProductionTask.status)
        .all()
    )
    return {
        "tasks_total": session.query(ProductionTask).count(),
        "drafted": statuses.get("TaskDrafted", 0) + statuses.get("ReissueDrafted", 0),
        "issued": statuses.get("TaskIssued", 0) + statuses.get("Reissued", 0),
        "questioned": statuses.get("ProductionQuestioned", 0),
        "closed": statuses.get("Closed", 0),
        "exceptions_open": session.query(ExceptionCase).filter(ExceptionCase.status == "Open").count(),
        "outbound_pending": session.query(OutboundMailJob).filter(OutboundMailJob.status == "Pending").count(),
        "outbound_failed": session.query(OutboundMailJob).filter(OutboundMailJob.status == "Failed").count(),
        "change_review": statuses.get("ReissueDrafted", 0) + statuses.get("CancelReview", 0),
    }


def report_local_time(value: datetime) -> datetime:
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(REPORT_TIMEZONE)


def format_report_time(value: datetime) -> str:
    return report_local_time(value).strftime("%Y-%m-%d %H:%M")


def format_report_period(start_at: datetime, end_at: datetime) -> str:
    return f"{format_report_time(start_at)} 至 {format_report_time(end_at)}（北京时间）"


def weekly_report_periods(generated_at: datetime) -> dict[str, dict[str, object]]:
    generated_local = report_local_time(generated_at)
    week_start_local = (generated_local - timedelta(days=generated_local.weekday())).replace(hour=0, minute=0, second=0, microsecond=0)
    month_start_local = generated_local.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    year_start_local = generated_local.replace(month=1, day=1, hour=0, minute=0, second=0, microsecond=0)

    week_start = week_start_local.astimezone(timezone.utc)
    month_start = month_start_local.astimezone(timezone.utc)
    year_start = year_start_local.astimezone(timezone.utc)
    return {
        "week": {"label": "本周", "start_at": week_start, "end_at": generated_at, "range_label": format_report_period(week_start, generated_at)},
        "month": {"label": "本月", "start_at": month_start, "end_at": generated_at, "range_label": format_report_period(month_start, generated_at)},
        "year": {"label": "本年", "start_at": year_start, "end_at": generated_at, "range_label": format_report_period(year_start, generated_at)},
    }


def is_confirmed_task(task: ProductionTask) -> bool:
    return task.status == "Closed" and task.closed_reason == "ScheduledConfirmed"


def product_order_stats(tasks: list[ProductionTask], *, confirmed: bool) -> list[dict[str, object]]:
    counts: dict[str, int] = {}
    for task in tasks:
        if is_confirmed_task(task) != confirmed:
            continue
        product = (task.requirement.product_summary or "未识别产品").strip() or "未识别产品"
        counts[product] = counts.get(product, 0) + 1
    return [{"product": product, "order_count": count} for product, count in sorted(counts.items(), key=lambda item: (-item[1], item[0]))]


def sales_top10_stats(tasks: list[ProductionTask]) -> list[dict[str, object]]:
    stats: dict[str, dict[str, object]] = {}
    for task in tasks:
        requirement = task.requirement
        salesperson = (requirement.salesperson_email or requirement.salesperson_name or "未知销售").strip() or "未知销售"
        row = stats.setdefault(salesperson, {"salesperson": salesperson, "demand_total": 0, "confirmed_total": 0})
        row["demand_total"] = int(row["demand_total"]) + 1
        if is_confirmed_task(task):
            row["confirmed_total"] = int(row["confirmed_total"]) + 1
    return sorted(stats.values(), key=lambda row: (-int(row["demand_total"]), -int(row["confirmed_total"]), str(row["salesperson"])))[:10]


def weekly_report(session: Session) -> dict:
    generated_at = now_utc()
    period_defs = weekly_report_periods(generated_at)
    periods: dict[str, dict[str, object]] = {}
    for key, period in period_defs.items():
        start_at = period["start_at"]
        tasks = (
            session.query(ProductionTask)
            .filter(ProductionTask.created_at >= start_at, ProductionTask.created_at <= generated_at)
            .order_by(ProductionTask.created_at.desc())
            .all()
        )
        confirmed_total = sum(1 for task in tasks if is_confirmed_task(task))
        periods[key] = {
            "label": period["label"],
            "start_at": start_at.isoformat(),
            "end_at": generated_at.isoformat(),
            "range_label": period["range_label"],
            "task_stats": {
                "demand_total": len(tasks),
                "confirmed_total": confirmed_total,
                "unconfirmed_total": len(tasks) - confirmed_total,
            },
            "confirmed_products": product_order_stats(tasks, confirmed=True),
            "unconfirmed_products": product_order_stats(tasks, confirmed=False),
            "sales_top10": sales_top10_stats(tasks),
        }
    return {
        "generated_at": generated_at.isoformat(),
        "generated_at_label": f"{format_report_time(generated_at)}（北京时间）",
        "reporting_period": {
            "label": periods["week"]["label"],
            "start_at": periods["week"]["start_at"],
            "end_at": periods["week"]["end_at"],
            "range_label": periods["week"]["range_label"],
        },
        "periods": periods,
    }


def weekly_report_recipients(session: Session) -> dict[str, list[str]]:
    to_addresses = as_list(get_config(session, "weekly_report_to_json", "[]"))
    cc_addresses = as_list(get_config(session, "weekly_report_cc_json", "[]"))
    if not to_addresses:
        ceo_email = get_config(session, "ceo_email", "dingyong@jimuyida.com")
        to_addresses = [ceo_email] if ceo_email else []
    return {"to": [str(item) for item in to_addresses if item], "cc": [str(item) for item in cc_addresses if item]}


def set_weekly_report_recipients(session: Session, to_addresses: list[str], cc_addresses: list[str]) -> dict[str, list[str]]:
    for key, value in {
        "weekly_report_to_json": dumps(to_addresses),
        "weekly_report_cc_json": dumps(cc_addresses),
    }.items():
        config = session.get(SystemConfig, key)
        if config is None:
            session.add(SystemConfig(key=key, value=value, is_secret=False))
        else:
            config.value = value
            config.is_secret = False
            config.updated_at = now_utc()
    add_audit(session, "WeeklyReportRecipientsUpdated", "SystemConfig", "weekly_report", {"to": to_addresses, "cc": cc_addresses})
    return {"to": to_addresses, "cc": cc_addresses}


def weekly_report_subject(generated_at: datetime) -> str:
    iso = report_local_time(generated_at).isocalendar()
    return f"[商务生产任务单周报][{iso.year}-W{iso.week:02d}]"


def _format_product_stats(rows: list[dict[str, object]]) -> list[str]:
    if not rows:
        return ["- 暂无"]
    return [f"- {row['product']}：{row['order_count']} 单" for row in rows]


def _format_sales_stats(rows: list[dict[str, object]]) -> list[str]:
    if not rows:
        return ["- 暂无"]
    return [
        f"- {index}. {row['salesperson']}：需求 {row['demand_total']} 单，已确认 {row['confirmed_total']} 单"
        for index, row in enumerate(rows, start=1)
    ]


def weekly_report_mail_body(report_data: dict) -> str:
    periods = report_data["periods"]
    reporting_period = report_data.get("reporting_period") or periods["week"]
    period_order = ["week", "month", "year"]
    lines = [
        "各位好，",
        "",
        "以下为商务生产任务单统计周报：",
        f"本次上报周期：{reporting_period['label']}，{reporting_period['range_label']}",
        f"生成时间：{report_data.get('generated_at_label', report_data['generated_at'])}",
        "",
        "统计周期：",
        *[f"- {periods[key]['label']}：{periods[key]['range_label']}" for key in period_order],
        "",
        "一、任务统计",
    ]
    for key in period_order:
        period = periods[key]
        stats = period["task_stats"]
        lines.append(f"- {period['label']}：{period['range_label']}，需求 {stats['demand_total']} 单，已确认 {stats['confirmed_total']} 单，未确认 {stats['unconfirmed_total']} 单")

    lines.append("")
    lines.append("二、已确认产品订单统计（分产品）")
    for key in period_order:
        period = periods[key]
        lines.append(f"{period['label']}：{period['range_label']}")
        lines.extend(_format_product_stats(period["confirmed_products"]))

    lines.append("")
    lines.append("三、未确认产品订单统计（分产品）")
    for key in period_order:
        period = periods[key]
        lines.append(f"{period['label']}：{period['range_label']}")
        lines.extend(_format_product_stats(period["unconfirmed_products"]))

    lines.append("")
    lines.append("四、销售 Top10 统计（需求总数和已确认总数）")
    for key in period_order:
        period = periods[key]
        lines.append(f"{period['label']}：{period['range_label']}")
        lines.extend(_format_sales_stats(period["sales_top10"]))

    lines.extend(
        [
            "",
            "积木易搭AI机器人",
        ]
    )
    return "\n".join(lines)


def enqueue_weekly_report(session: Session, *, force_new: bool = False) -> OutboundMailJob:
    generated_at = now_utc()
    recipients = weekly_report_recipients(session)
    to_addresses = recipients["to"]
    cc_addresses = recipients["cc"]
    if not to_addresses:
        raise ValueError("weekly report recipients are not configured")

    iso = report_local_time(generated_at).isocalendar()
    idem = f"weekly-report:{iso.year}-W{iso.week:02d}:{recipient_hash(to_addresses, cc_addresses)}"
    if force_new:
        idem = f"{idem}:manual:{generated_at.timestamp():.6f}"
    else:
        existing = session.query(OutboundMailJob).filter_by(idempotency_key=idem).one_or_none()
        if existing is not None:
            return existing

    report_data = weekly_report(session)
    job = OutboundMailJob(
        mail_type="WeeklyReport",
        to_json=dumps(to_addresses),
        cc_json=dumps(cc_addresses),
        subject=weekly_report_subject(generated_at),
        body=weekly_report_mail_body(report_data),
        idempotency_key=idem,
        status="Pending",
    )
    session.add(job)
    session.flush()
    add_audit(session, "WeeklyReportQueued", "OutboundMailJob", job.id, {"to": to_addresses, "cc": cc_addresses})
    return job


def retry_outbound_mail(session: Session, job_id: str, actor: str = "business-owner") -> OutboundMailJob:
    job = session.get(OutboundMailJob, job_id)
    if job is None:
        raise ValueError("outbound mail job not found")
    if job.status not in {"Failed", "Pending"}:
        raise ValueError(f"outbound mail status {job.status} cannot be retried")
    previous_status = job.status
    job.status = "Pending"
    add_audit(
        session,
        "OutboundMailRetryQueued",
        "OutboundMailJob",
        job.id,
        {"previous_status": previous_status, "mail_type": job.mail_type, "subject": job.subject},
        actor,
    )
    return job


def enqueue_job(session: Session, job_type: str, payload: dict) -> ProcessingJob:
    payload_json = dumps(payload)
    existing = session.query(ProcessingJob).filter_by(job_type=job_type, payload_json=payload_json).first()
    if existing is not None:
        return existing
    job = ProcessingJob(job_type=job_type, payload_json=payload_json, status="Pending")
    session.add(job)
    session.flush()
    add_audit(session, "ProcessingJobQueued", "ProcessingJob", job.id, {"job_type": job_type})
    return job
