from __future__ import annotations

import hashlib
import logging
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from sqlalchemy.orm import Session

from backend.app.models import (
    MailMessage,
    ModelProviderConfig,
    ProductionDepartment,
    RequirementWorkflowBinding,
    SystemConfig,
    WorkflowDefinition,
    WorkflowImportJob,
    WorkflowVersion,
    MailWorkflowMatch,
    now_utc,
)
from backend.app.services.attachment_parser import parse_docx
from backend.app.services.jsonutil import as_list, dumps, loads
from backend.app.services.initial_review import OPERATOR_OPTIONS, FIELD_LABELS
from backend.app.services.llm_fallback import parse_json_object
from backend.app.services.model_provider import call_model, extract_chat_content, resolve_api_key

logger = logging.getLogger(__name__)


FLOW_HEADER_PATTERN = re.compile(r"流程([一二三四五六七八九十\d]+)[:：]\s*(.+)")
LINE_SPLIT_PATTERN = re.compile(r"[、,，;/；\s]+")
EMAIL_ADDRESS_PATTERN = re.compile(r"^[^@\s,;]+@[^@\s,;]+\.[^@\s,;]+$")
EMAIL_TOKEN_PATTERN = re.compile(r"[^@\s,;<>，；、]+@[^@\s,;<>，；、]+\.[^@\s,;<>，；、]+")
CORE_FIELD_LABELS = {
    "customer_name": "客户名称",
    "product_summary": "物料/规格",
    "quantity_text": "数量",
    "expected_delivery_date": "期望交期",
    "external_order_no": "订单号",
}
CONTACT_DYNAMIC_LABELS = {"下单销售", "销售直属领导"}
DEFAULT_TEMPLATE_SUBJECT = "[生产任务单][{{task_no}}][{{customer_name}}][{{product_summary}}][V{{version_no}}]"
DEFAULT_TEMPLATE_BODY = """生产部同事好：

请根据以下信息安排生产评估和排产。

任务单编号：{{task_no}}
版本：V{{version_no}}
客户名称：{{customer_name}}
销售人员：{{salesperson_name}} <{{salesperson_email}}>

物料/规格：{{product_summary}}
数量：{{quantity_text}}
期望交期：{{expected_delivery_date}}
流程类型：{{workflow_name}}

请确认是否可以安排生产。如信息不足，请直接回复本邮件说明疑问点。

{{bot_signature}}
"""
TASK_TEMPLATE_BODY_REQUIRED_TOKENS = (
    "{{task_no}}",
    "{{version_no}}",
    "{{customer_name}}",
    "{{product_summary}}",
    "{{quantity_text}}",
    "{{expected_delivery_date}}",
    "{{workflow_name}}",
)
FIELD_HINTS = {
    "material_details": {"label": "物料详情描述", "keywords": ["物料详情描述", "物料详情", "物料编码", "规格型号"]},
    "logistics_method": {"label": "物流发货方式", "keywords": ["物流发货方式", "物流方式"]},
    "shipping_time_requirement": {"label": "出货时间要求", "keywords": ["出货时间要求", "发货时间要求"]},
    "customer_receiver_info": {"label": "客户收件信息", "keywords": ["客户收件信息", "收件信息"]},
    "delivery_requirement": {"label": "交付要求", "keywords": ["交付要求"]},
    "shipping_warehouse": {"label": "出货仓", "keywords": ["出货仓", "借货仓"]},
    "borrow_time": {"label": "借用时间", "keywords": ["借用时间"]},
    "return_time": {"label": "归还时间", "keywords": ["归还时间"]},
    "sample_approval_screenshot": {"label": "样机借用审批截图", "keywords": ["样机借用审批截图"]},
}
MATERIAL_DETAIL_CHILD_LABELS = {"物料编码", "产品编码", "商品编码", "物料名称", "产品名称", "商品名称", "数量", "需求数量", "规格型号", "型号"}
ALLOWED_REVIEW_FIELDS = set(FIELD_LABELS)
ALLOWED_REVIEW_OPERATORS = {item["key"] for item in OPERATOR_OPTIONS}
WORKFLOW_CHAT_SYSTEM_PROMPT = (
    "你是商务流程规则生成助手。你需要通过多轮问答帮助用户梳理下单流程，并输出可直接落库的流程规则。"
    "你每次都必须只返回 JSON 对象，不要返回任何额外解释。"
    "JSON 固定结构为："
    "{\"assistant_reply\":\"...\",\"ready\":true|false,"
    "\"workflow_rule\":{\"workflow_code\":\"...\",\"workflow_name\":\"...\","
    "\"match\":{\"any_keywords\":[],\"all_keywords\":[],\"warehouse\":\"\",\"order_type\":\"\",\"subject_patterns\":[]},"
    "\"routing\":{\"to_names\":[],\"cc_names\":[]},"
    "\"subject_template\":\"...\",\"body_template\":\"...\","
    "\"required_fields\":[],\"required_attachments\":[],\"review_rules\":[],"
    "\"conversation_policy\":{\"max_question_rounds\":3,\"on_exceeded\":\"close_task\",\"message\":\"\"}}"
    "}"
    "当信息不足时，ready=false，assistant_reply 用简洁中文提出下一步问题，workflow_rule 可为 null。"
    "当信息充分时，ready=true，并给出完整 workflow_rule。"
    "review_rules 的 field/operator 必须使用系统支持的值。"
)
WORKFLOW_GUIDED_PLACEHOLDERS = {"", "-", "待定", "待确认", "未知", "未定", "n/a", "na", "none", "无"}
GENERIC_MATCH_KEYWORDS = {"流程", "订单", "下单", "销售", "邮件", "生产", "规则"}
GUIDED_REQUIRED_CORE_FIELDS = ("customer_name", "product_summary", "quantity_text", "expected_delivery_date")


@dataclass(frozen=True)
class WorkflowMatchResult:
    version: WorkflowVersion
    rule: dict[str, Any]
    confidence: int
    reasons: list[str]


@dataclass(frozen=True)
class _WorkflowScoredCandidate:
    version: WorkflowVersion
    rule: dict[str, Any]
    score: int
    reasons: list[str]


def _get_config(session: Session, key: str, fallback: str = "") -> str:
    row = session.get(SystemConfig, key)
    return row.value if row is not None else fallback


def _active_model(session: Session) -> ModelProviderConfig | None:
    model = session.query(ModelProviderConfig).filter_by(status="Active").first()
    if model is None:
        return None
    if not resolve_api_key(session, model):
        return None
    return model


def _parse_json_array(text: str) -> list[dict[str, Any]]:
    clean = text.strip()
    if clean.startswith("```"):
        clean = re.sub(r"^```(?:json)?\s*", "", clean)
        clean = re.sub(r"\s*```$", "", clean)
    try:
        loaded = loads(clean, [])
        if isinstance(loaded, list):
            return [item for item in loaded if isinstance(item, dict)]
    except Exception:
        pass
    obj = parse_json_object(clean)
    if isinstance(obj.get("workflows"), list):
        return [item for item in obj["workflows"] if isinstance(item, dict)]
    match = re.search(r"\[.*\]", clean, flags=re.DOTALL)
    if not match:
        return []
    loaded = loads(match.group(0), [])
    return [item for item in loaded if isinstance(item, dict)] if isinstance(loaded, list) else []


def _normalize_list(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, list):
        raw = value
    else:
        raw = LINE_SPLIT_PATTERN.split(str(value))
    result: list[str] = []
    for item in raw:
        text = str(item).strip()
        if text and text not in result:
            result.append(text)
    return result


def _normalize_email_list(value: Any) -> list[str]:
    if value is None:
        return []
    raw = value if isinstance(value, list) else [value]
    result: list[str] = []
    for item in raw:
        for match in EMAIL_TOKEN_PATTERN.findall(str(item or "")):
            email = match.strip().strip("。.,，;；:：)]}）】>")
            if _is_email_address(email) and email not in result:
                result.append(email)
    return result


def _normalize_unique_text(value: Any) -> str:
    return re.sub(r"\s+", "", str(value or "")).strip().lower()


def _is_email_address(value: Any) -> bool:
    return bool(EMAIL_ADDRESS_PATTERN.fullmatch(str(value or "").strip()))


def _department_code_from_contact(value: str) -> str:
    clean = re.sub(r"[^0-9A-Za-z_]+", "_", str(value or "").strip().lower()).strip("_")
    digest = hashlib.sha1(str(value or "").strip().encode("utf-8")).hexdigest()[:8]
    return f"auto_{clean[:32] or 'department'}_{digest}"


def _production_department_lookup(session: Session) -> dict[str, ProductionDepartment]:
    lookup: dict[str, ProductionDepartment] = {}
    departments = session.query(ProductionDepartment).filter_by(status="Active").all()
    for department in departments:
        keys = [department.department_code, department.department_name]
        keys.extend(as_list(department.mail_to_json))
        for key in keys:
            normalized = _normalize_unique_text(key)
            if normalized and normalized not in lookup:
                lookup[normalized] = department
    return lookup


def _production_department_main_emails(session: Session) -> set[str]:
    emails: set[str] = set()
    for department in session.query(ProductionDepartment).filter_by(status="Active").all():
        for email in as_list(department.mail_to_json):
            email_text = str(email or "").strip()
            if _is_email_address(email_text):
                emails.add(email_text.lower())
    return emails


def _ensure_production_department_for_contact(
    session: Session,
    contact: str,
    lookup: dict[str, ProductionDepartment],
    *,
    create_missing: bool = True,
) -> tuple[ProductionDepartment | None, list[str]]:
    contact_text = str(contact or "").strip()
    if not contact_text:
        return None, []
    existing = lookup.get(_normalize_unique_text(contact_text))
    if existing is not None:
        return existing, [email for email in as_list(existing.mail_to_json) if _is_email_address(email)]
    if not create_missing:
        return None, []

    department = ProductionDepartment(
        department_code=_department_code_from_contact(contact_text),
        department_name=contact_text,
        mail_to_json=dumps([contact_text] if _is_email_address(contact_text) else []),
        mail_cc_json=dumps([]),
        status="Active",
    )
    session.add(department)
    session.flush()
    lookup[_normalize_unique_text(department.department_code)] = department
    lookup[_normalize_unique_text(department.department_name)] = department
    for email in as_list(department.mail_to_json):
        lookup[_normalize_unique_text(email)] = department
    return department, [email for email in as_list(department.mail_to_json) if _is_email_address(email)]


def _sync_rule_routing_with_production_departments(
    session: Session,
    rule: dict[str, Any],
    *,
    create_missing: bool = True,
) -> dict[str, Any]:
    routing = rule.get("routing") if isinstance(rule.get("routing"), dict) else {}
    lookup = _production_department_lookup(session)
    bound_to: list[str] = []
    for contact in _normalize_list(routing.get("to_names") or routing.get("to")):
        _department, emails = _ensure_production_department_for_contact(session, contact, lookup, create_missing=create_missing)
        candidates = emails if emails else [contact]
        for candidate in candidates:
            if candidate and candidate not in bound_to:
                bound_to.append(candidate)

    bound_cc: list[str] = []
    for contact in _normalize_list(routing.get("cc_names") or routing.get("cc")):
        if _is_email_address(contact):
            candidate = contact
        else:
            department = lookup.get(_normalize_unique_text(contact))
            department_emails = as_list(department.mail_cc_json) if department is not None else []
            candidate = next((email for email in department_emails if _is_email_address(email)), contact)
        if candidate and candidate not in bound_cc:
            bound_cc.append(candidate)

    return {
        **rule,
        "routing": {
            **routing,
            "to_names": bound_to,
            "cc_names": bound_cc,
        },
    }


def _validate_production_main_recipient_selection(session: Session, rule: dict[str, Any]) -> list[str]:
    routing = rule.get("routing") if isinstance(rule.get("routing"), dict) else {}
    to_names = _normalize_list(routing.get("to_names") or routing.get("to"))
    workflow_name = str(rule.get("workflow_name") or rule.get("workflow_code") or "流程")
    if not to_names:
        return [f"{workflow_name} 主送人必填。"]
    allowed = _production_department_main_emails(session)
    if not allowed:
        return ["请先在【生产邮箱】配置至少一个启用生产部门的主送邮箱。"]
    invalid = [item for item in to_names if not _is_email_address(item) or item.lower() not in allowed]
    if invalid:
        return [f"{workflow_name} 主送人只能从生产部门邮箱列表选择：{', '.join(invalid)}"]
    return []


def _normalize_review_rules(value: Any) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        return []
    rules: list[dict[str, Any]] = []
    for index, item in enumerate(value, start=1):
        if not isinstance(item, dict):
            continue
        field = str(item.get("field") or "source_text").strip() or "source_text"
        if field not in ALLOWED_REVIEW_FIELDS:
            field = "source_text"
        operator = str(item.get("operator") or "contains").strip() or "contains"
        if operator not in ALLOWED_REVIEW_OPERATORS:
            operator = "contains"
        name = str(item.get("name") or f"流程规则-{index}").strip() or f"流程规则-{index}"
        message = str(item.get("message") or f"{FIELD_LABELS.get(field, field)} 未通过流程规则：{name}").strip()
        rules.append(
            {
                "id": str(item.get("id") or f"workflow-rule-{index}"),
                "name": name,
                "field": field,
                "operator": operator,
                "value": str(item.get("value") or ""),
                "message": message,
                "enabled": bool(item.get("enabled", True)),
            }
        )
    return rules


def _normalize_conversation_policy(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict):
        return {}
    raw_rounds = value.get("max_question_rounds")
    try:
        max_rounds = int(raw_rounds)
    except (TypeError, ValueError):
        max_rounds = 0
    if max_rounds <= 0:
        return {}
    return {
        "max_question_rounds": max(1, min(max_rounds, 20)),
        "on_exceeded": str(value.get("on_exceeded") or "close_task").strip() or "close_task",
        "message": str(value.get("message") or "").strip(),
    }


def _auto_review_rules(name: str, match: dict[str, Any]) -> list[dict[str, Any]]:
    suggestions: list[dict[str, Any]] = []
    any_keywords = _normalize_list(match.get("any_keywords"))
    top_keywords = [keyword for keyword in any_keywords if keyword][:2]
    for index, keyword in enumerate(top_keywords, start=1):
        suggestions.append(
            {
                "id": f"auto-keyword-{index}",
                "name": f"流程关键词建议：{keyword}",
                "field": "source_text",
                "operator": "contains",
                "value": keyword,
                "message": f"邮件内容未体现流程关键词：{keyword}",
                # 自动生成先给建议，人工确认后启用。
                "enabled": False,
            }
        )
    warehouse = str(match.get("warehouse") or "").strip().lower()
    if warehouse == "wuhan":
        suggestions.append(
            {
                "id": "auto-warehouse-wuhan",
                "name": "仓库建议：武汉仓",
                "field": "source_text",
                "operator": "contains",
                "value": "武汉仓",
                "message": "当前邮件未体现武汉仓信息",
                "enabled": False,
            }
        )
    elif warehouse == "overseas":
        suggestions.append(
            {
                "id": "auto-warehouse-overseas",
                "name": "仓库建议：海外仓",
                "field": "source_text",
                "operator": "contains",
                "value": "海外仓",
                "message": "当前邮件未体现海外仓信息",
                "enabled": False,
            }
        )
    if not suggestions:
        suggestions.append(
            {
                "id": "auto-placeholder",
                "name": f"{name}流程复核建议",
                "field": "source_text",
                "operator": "contains",
                "value": "",
                "message": "请编辑并启用该流程专属初审规则",
                "enabled": False,
            }
        )
    return suggestions


def _is_placeholder_text(value: str) -> bool:
    return value.strip().lower() in WORKFLOW_GUIDED_PLACEHOLDERS


def _has_meaningful_match(match: dict[str, Any]) -> bool:
    any_keywords = [item for item in _normalize_list(match.get("any_keywords")) if not _is_placeholder_text(item)]
    all_keywords = [item for item in _normalize_list(match.get("all_keywords")) if not _is_placeholder_text(item)]
    subject_patterns = [item for item in _normalize_list(match.get("subject_patterns")) if not _is_placeholder_text(item)]
    meaningful_keywords = [item for item in any_keywords + all_keywords if item.lower() not in GENERIC_MATCH_KEYWORDS]
    return bool(meaningful_keywords or subject_patterns)


def _workflow_definition_gaps(rule: dict[str, Any] | None) -> list[dict[str, str]]:
    if not isinstance(rule, dict):
        return [{"code": "workflow_name", "question": "请先告诉我这个新流程的名称。"}]

    gaps: list[dict[str, str]] = []
    workflow_name = str(rule.get("workflow_name") or "").strip()
    if not workflow_name or _is_placeholder_text(workflow_name):
        gaps.append({"code": "workflow_name", "question": "请确认流程名称，例如“海外仓补单流程”。"})

    routing = rule.get("routing") if isinstance(rule.get("routing"), dict) else {}
    to_names = _normalize_list(routing.get("to_names") or routing.get("to"))
    if not to_names:
        gaps.append({"code": "routing_to", "question": "该流程邮件主送给谁？请提供姓名或角色（可多个）。"})

    match = rule.get("match") if isinstance(rule.get("match"), dict) else {}
    if not _has_meaningful_match(match):
        gaps.append({"code": "match", "question": "请补充该流程的判定特征：关键词、主题模式或仓库/订单类型。"})

    required_fields = _normalize_list(rule.get("required_fields"))
    missing_core = [CORE_FIELD_LABELS[key] for key in GUIDED_REQUIRED_CORE_FIELDS if key not in required_fields]
    if missing_core:
        gaps.append({"code": "required_fields", "question": f"该流程必填字段还缺少：{'、'.join(missing_core)}。是否补齐？"})

    return gaps


def _build_guided_followup(gaps: list[dict[str, str]]) -> tuple[str, list[str]]:
    if not gaps:
        return "", []
    questions = [item["question"] for item in gaps if item.get("question")]
    if not questions:
        return "", []
    first = questions[0]
    if len(questions) > 1:
        return f"{first}（当前还剩 {len(questions)} 项待确认，我会逐项引导。）", questions
    return first, questions


def _clean_workflow_name_candidate(value: str) -> str:
    text = re.sub(r"\s+", "", str(value or "").strip())
    return text.strip("“”\"'《》「」[]（）()，,。.;；:：")


def _is_generic_workflow_name(value: str) -> bool:
    text = _clean_workflow_name_candidate(value)
    if not text:
        return True
    if _is_placeholder_text(text):
        return True
    return text in {"流程", "新流程", "该流程", "这个流程", "此流程", "这个新流程"}


def _extract_workflow_name_from_text(text: str) -> str:
    if not text:
        return ""
    raw = str(text).strip()
    patterns = [
        r"(?:流程名称|新流程名称|名称)\s*(?:是|为|叫)\s*[“\"'《]?\s*([^”\"'》\n，,；;。]{2,40}?流程)\s*[”\"'》]?",
        r"就是\s*[“\"'《]?\s*([^”\"'》\n，,；;。]{2,40}?流程)\s*[”\"'》]?",
        r"叫\s*[“\"'《]?\s*([^”\"'》\n，,；;。]{2,40}?流程)\s*[”\"'》]?",
    ]
    for pattern in patterns:
        match = re.search(pattern, raw, flags=re.IGNORECASE)
        if not match:
            continue
        candidate = _clean_workflow_name_candidate(match.group(1))
        if candidate and not _is_generic_workflow_name(candidate):
            return candidate

    quoted = re.search(r"[“\"'《](.{2,40}?流程)[”\"'》]", raw)
    if quoted:
        candidate = _clean_workflow_name_candidate(quoted.group(1))
        if candidate and not _is_generic_workflow_name(candidate):
            return candidate

    compact = _clean_workflow_name_candidate(raw)
    if compact.endswith("流程") and len(compact) <= 40 and not _is_generic_workflow_name(compact):
        return compact
    return ""


def _infer_workflow_name_from_turns(turns: list[dict[str, Any]], current_rule: dict[str, Any] | None) -> str:
    if isinstance(current_rule, dict):
        existing = _clean_workflow_name_candidate(current_rule.get("workflow_name") or "")
        if existing and not _is_generic_workflow_name(existing):
            return existing
    for item in reversed(turns):
        role = str(item.get("role") or "user").strip().lower()
        if role != "user":
            continue
        content = str(item.get("content") or "").strip()
        if not content:
            continue
        candidate = _extract_workflow_name_from_text(content)
        if candidate:
            return candidate
    return ""


def _find_workflow_version_for_chat_edit(
    session: Session,
    turns: list[dict[str, Any]],
    edit_version_id: str | None = None,
) -> WorkflowVersion | None:
    if edit_version_id:
        return session.get(WorkflowVersion, edit_version_id)

    user_text = " ".join(
        str(item.get("content") or "")
        for item in turns
        if str(item.get("role") or "user").strip().lower() == "user"
    )
    if not user_text:
        return None
    compact_text = _clean_workflow_name_candidate(user_text)
    lower_text = user_text.lower()
    edit_intents = ("编辑", "修改", "更新", "重新", "调整", "补充", "增加", "删除", "改")
    if not any(token in user_text for token in edit_intents):
        return None

    rows = (
        session.query(WorkflowVersion)
        .join(WorkflowDefinition, WorkflowDefinition.id == WorkflowVersion.workflow_id)
        .order_by(WorkflowDefinition.workflow_code, WorkflowVersion.version_no.desc())
        .all()
    )
    for row in rows:
        definition = session.get(WorkflowDefinition, row.workflow_id)
        if definition is None:
            continue
        workflow_name = _clean_workflow_name_candidate(definition.workflow_name)
        workflow_code = str(definition.workflow_code or "").strip().lower()
        if workflow_name and workflow_name in compact_text:
            return row
        if workflow_code and workflow_code in lower_text:
            return row
    return None


def _normalize_workflow_code(name: str, fallback_index: int) -> str:
    base = re.sub(r"\s+", "", name)
    if not base:
        base = f"workflow_{fallback_index}"
    digest = hashlib.sha1(base.encode("utf-8")).hexdigest()[:10]
    safe = re.sub(r"[^0-9A-Za-z_]+", "_", base).strip("_").lower()
    if safe and len(safe) <= 32:
        return f"{safe}_{digest[:6]}"
    return f"workflow_{fallback_index}_{digest}"


def _extract_line_value(text: str, label: str) -> str:
    match = re.search(rf"{re.escape(label)}[:：]\s*(.+)", text)
    return match.group(1).strip() if match else ""


def _extract_template_block(text: str) -> str:
    marker = "邮件内容模板"
    index = text.find(marker)
    if index < 0:
        return ""
    block = text[index + len(marker) :]
    block = re.sub(r"^[\s:：]+", "", block)
    block = re.split(r"附件[:：]", block, maxsplit=1)[0]
    lines = [line.rstrip() for line in block.splitlines()]
    return "\n".join(line for line in lines if line.strip())


def _required_fields_from_text(name: str, section_text: str) -> list[str]:
    fields = ["customer_name", "product_summary", "quantity_text", "expected_delivery_date"]
    if any(keyword in section_text for keyword in ["物料详情描述", "物料编码", "规格型号"]):
        fields.append("material_details")
    if "物流发货方式" in section_text:
        fields.append("logistics_method")
    if any(keyword in section_text for keyword in ["出货时间要求", "发货时间要求"]):
        fields.append("shipping_time_requirement")
    if "客户收件信息" in section_text:
        fields.append("customer_receiver_info")
    if "交付要求" in section_text:
        fields.append("delivery_requirement")
    if any(keyword in section_text for keyword in ["出货仓", "借货仓"]):
        fields.append("shipping_warehouse")
    if "借用时间" in section_text:
        fields.append("borrow_time")
    if "归还时间" in section_text:
        fields.append("return_time")
    if "样机借用审批截图" in section_text or "样机借用" in name:
        fields.append("sample_approval_screenshot")
    if "样机借用" in name:
        fields.append("sample_approval_screenshot")
    unique: list[str] = []
    for field in fields:
        if field not in unique:
            unique.append(field)
    return unique


def _match_hints_from_name(name: str) -> dict[str, Any]:
    match: dict[str, Any] = {"any_keywords": _normalize_list(re.split(r"[\\/、，,（）() ]+", name))}
    if "武汉仓" in name:
        match["warehouse"] = "wuhan"
    elif "海外仓" in name:
        match["warehouse"] = "overseas"
    if "样机借用" in name:
        match["order_type"] = "sample_borrow"
    elif "补单" in name:
        match["order_type"] = "refill"
    elif "样机赠送" in name:
        match["order_type"] = "sample_gift"
    else:
        match["order_type"] = "normal_sales"
    return match


def _build_rule_from_section(index: int, name: str, section_text: str) -> dict[str, Any]:
    to_names = _normalize_list(_extract_line_value(section_text, "邮件收件人"))
    cc_names = _normalize_list(_extract_line_value(section_text, "邮件抄送人"))
    subject_template = _extract_line_value(section_text, "邮件主题") or DEFAULT_TEMPLATE_SUBJECT
    body_template = _extract_template_block(section_text) or DEFAULT_TEMPLATE_BODY
    attachment_line = _extract_line_value(section_text, "附件")
    required_attachments = _normalize_list(re.sub(r"[【】\[\]]", "", attachment_line))
    return {
        "workflow_code": _normalize_workflow_code(name, index),
        "workflow_name": name,
        "match": _match_hints_from_name(name),
        "routing": {"to_names": to_names, "cc_names": cc_names},
        "subject_template": subject_template,
        "body_template": body_template,
        "required_fields": _required_fields_from_text(name, section_text),
        "required_attachments": required_attachments,
        "review_rules": [],
    }


def workflow_template_field_label(field: str) -> str:
    if field in CORE_FIELD_LABELS:
        return CORE_FIELD_LABELS[field]
    hints = FIELD_HINTS.get(field)
    if hints:
        return str(hints.get("label") or field)
    return field


def _template_has_variable(template: str, field: str) -> bool:
    return re.search(r"\{\{\s*" + re.escape(field) + r"\s*\}\}", template or "") is not None


def append_required_field_template_lines(body_template: str, required_fields: list[str]) -> str:
    body = body_template.strip() or DEFAULT_TEMPLATE_BODY
    missing_fields = [
        field
        for field in required_fields
        if field
        and field not in CORE_FIELD_LABELS
        and not _template_has_variable(body, field)
    ]
    if not missing_fields:
        return body
    lines = ["流程必填信息："]
    for field in missing_fields:
        lines.append(f"{workflow_template_field_label(field)}：{{{{{field}}}}}")
    return "\n\n".join([body.rstrip(), "\n".join(lines)])


def _ensure_task_template_variables(
    subject_template: str,
    body_template: str,
    required_fields: list[str] | None = None,
) -> tuple[str, str]:
    subject = subject_template.strip() or DEFAULT_TEMPLATE_SUBJECT
    body = body_template.strip() or DEFAULT_TEMPLATE_BODY
    if "{{task_no}}" not in subject:
        subject = DEFAULT_TEMPLATE_SUBJECT
    missing_tokens = [token for token in TASK_TEMPLATE_BODY_REQUIRED_TOKENS if token not in body]
    if missing_tokens:
        original_body = body
        body = DEFAULT_TEMPLATE_BODY.rstrip()
        if original_body and original_body != DEFAULT_TEMPLATE_BODY.strip():
            body = "\n\n".join([body, "原流程邮件模板：", original_body])
    body = append_required_field_template_lines(body, required_fields or [])
    return subject, body


def parse_workflows_by_rules(source_text: str) -> list[dict[str, Any]]:
    lines = [line.strip() for line in source_text.splitlines() if line.strip()]
    sections: list[tuple[str, str]] = []
    current_name = ""
    current_lines: list[str] = []
    for line in lines:
        matched = FLOW_HEADER_PATTERN.match(line)
        if matched:
            if current_name:
                sections.append((current_name, "\n".join(current_lines)))
            current_name = matched.group(2).strip()
            current_lines = [line]
            continue
        if current_name:
            current_lines.append(line)
    if current_name:
        sections.append((current_name, "\n".join(current_lines)))

    rules: list[dict[str, Any]] = []
    for index, (name, section_text) in enumerate(sections, start=1):
        rules.append(_build_rule_from_section(index, name, section_text))
    return rules


def parse_workflows_by_llm(session: Session, source_text: str) -> list[dict[str, Any]]:
    model = _active_model(session)
    if model is None:
        return []
    try:
        output = call_model(
            session,
            model,
            task_type="WorkflowImportParse",
            related_object_type="WorkflowImportJob",
            related_object_id=None,
            stream=True,
            messages=[
                    {
                        "role": "system",
                        "content": (
                            "你是流程规则结构化助手。只返回 JSON 数组，不要解释。"
                            "每个对象字段必须包含：workflow_code, workflow_name, match, routing, subject_template, body_template, required_fields, required_attachments, review_rules。"
                            "routing 包含 to_names/cc_names。"
                            "required_fields 仅允许核心字段 customer_name/product_summary/quantity_text/expected_delivery_date/external_order_no "
                            "和扩展字段 material_details/logistics_method/shipping_time_requirement/customer_receiver_info/delivery_requirement/"
                            "shipping_warehouse/borrow_time/return_time/sample_approval_screenshot。"
                            "review_rules 是流程专属初审规则数组，每条包含 id/name/field/operator/value/message/enabled。"
                            "field 仅允许 customer_name/product_summary/quantity_text/expected_delivery_date/external_order_no/salesperson_email/source_text。"
                            "operator 仅允许 required/contains/not_contains/regex/not_regex/min_number/max_number/date_format/date_after_or_equal/date_before_or_equal。"
                        ),
                    },
                {
                    "role": "user",
                    "content": f"请把以下流程文档结构化：\n{source_text[:12000]}",
                },
            ],
        )
        return _parse_json_array(extract_chat_content(output))
    except Exception:
        logger.exception("workflow import llm parsing failed, fallback to rule parser")
        return []


def _normalize_rule(rule: dict[str, Any], fallback_index: int, *, email_only_routing: bool = False) -> dict[str, Any]:
    name = str(rule.get("workflow_name") or rule.get("name") or "").strip()
    if not name:
        name = f"流程_{fallback_index}"
    code = str(rule.get("workflow_code") or "").strip() or _normalize_workflow_code(name, fallback_index)
    match = rule.get("match")
    if not isinstance(match, dict):
        match = _match_hints_from_name(name)
    routing = rule.get("routing")
    if not isinstance(routing, dict):
        routing = {}
    if email_only_routing:
        to_names = _normalize_email_list(routing.get("to_names") or routing.get("to"))
        cc_names = _normalize_email_list(routing.get("cc_names") or routing.get("cc"))
    else:
        to_names = _normalize_list(routing.get("to_names") or routing.get("to"))
        cc_names = _normalize_list(routing.get("cc_names") or routing.get("cc"))
    subject_template = str(rule.get("subject_template") or "").strip() or DEFAULT_TEMPLATE_SUBJECT
    body_template = str(rule.get("body_template") or "").strip() or DEFAULT_TEMPLATE_BODY
    required_fields = _normalize_list(rule.get("required_fields"))
    if not required_fields:
        required_fields = _required_fields_from_text(name, body_template)
    subject_template, body_template = _ensure_task_template_variables(subject_template, body_template, required_fields)
    required_attachments = _normalize_list(rule.get("required_attachments"))
    review_rules = _normalize_review_rules(rule.get("review_rules"))
    if not review_rules:
        review_rules = _auto_review_rules(name, match)
    conversation_policy = _normalize_conversation_policy(rule.get("conversation_policy"))
    return {
        "workflow_code": code,
        "workflow_name": name,
        "match": {
            "any_keywords": _normalize_list(match.get("any_keywords")),
            "all_keywords": _normalize_list(match.get("all_keywords")),
            "warehouse": str(match.get("warehouse") or "").strip(),
            "order_type": str(match.get("order_type") or "").strip(),
            "subject_patterns": _normalize_list(match.get("subject_patterns")),
        },
        "routing": {"to_names": to_names, "cc_names": cc_names},
        "subject_template": subject_template,
        "body_template": body_template,
        "required_fields": required_fields,
        "required_attachments": required_attachments,
        "review_rules": review_rules,
        "conversation_policy": conversation_policy,
    }


def _rules_diff(old: dict[str, Any], new: dict[str, Any]) -> list[dict[str, Any]]:
    changes: list[dict[str, Any]] = []
    for key in [
        "workflow_name",
        "match",
        "routing",
        "subject_template",
        "body_template",
        "required_fields",
        "required_attachments",
        "review_rules",
        "conversation_policy",
    ]:
        if old.get(key) != new.get(key):
            changes.append({"field": key, "before": old.get(key), "after": new.get(key)})
    return changes


def workflow_version_diff(
    session: Session,
    version_id: str,
    *,
    compare_to_version_id: str | None = None,
) -> dict[str, Any]:
    version = session.get(WorkflowVersion, version_id)
    if version is None:
        raise ValueError("workflow version not found")
    definition = session.get(WorkflowDefinition, version.workflow_id)
    if definition is None:
        raise ValueError("workflow definition not found")

    base: WorkflowVersion | None = None
    if compare_to_version_id:
        base = session.get(WorkflowVersion, compare_to_version_id)
        if base is None:
            raise ValueError("compare workflow version not found")
        if base.workflow_id != version.workflow_id:
            raise ValueError("compare workflow version belongs to another workflow")
    else:
        base = (
            session.query(WorkflowVersion)
            .filter(WorkflowVersion.workflow_id == version.workflow_id, WorkflowVersion.version_no < version.version_no)
            .order_by(WorkflowVersion.version_no.desc())
            .first()
        )

    current_rules = loads(version.compiled_rules_json, {})
    if not isinstance(current_rules, dict):
        current_rules = {}
    base_rules = loads(base.compiled_rules_json, {}) if base is not None else {}
    if not isinstance(base_rules, dict):
        base_rules = {}
    changes = _rules_diff(base_rules, current_rules)
    return {
        "workflow_id": definition.id,
        "workflow_code": definition.workflow_code,
        "workflow_name": definition.workflow_name,
        "current": {
            "id": version.id,
            "version_no": version.version_no,
            "status": version.status,
            "rules": current_rules,
        },
        "base": {
            "id": base.id,
            "version_no": base.version_no,
            "status": base.status,
            "rules": base_rules,
        }
        if base is not None
        else None,
        "changed": bool(changes),
        "changes": changes,
    }


def rollback_workflow_version(session: Session, version_id: str, *, actor: str = "system") -> WorkflowVersion:
    version = session.get(WorkflowVersion, version_id)
    if version is None:
        raise ValueError("workflow version not found")
    if version.status == "Active":
        return version
    return activate_workflow_version(session, version_id, actor=actor)


def _validate_rule(rule: dict[str, Any]) -> list[str]:
    errors: list[str] = []
    workflow_name = str(rule.get("workflow_name") or rule.get("workflow_code") or "流程")
    routing = rule.get("routing") if isinstance(rule.get("routing"), dict) else {}
    if not _normalize_list(routing.get("to_names")):
        errors.append(f"{workflow_name} 缺少收件人。")
    review_rules = _normalize_review_rules(rule.get("review_rules"))
    for review_rule in review_rules:
        if review_rule["field"] not in ALLOWED_REVIEW_FIELDS:
            errors.append(f"{workflow_name} 存在不支持的流程审查字段：{review_rule['field']}")
        if review_rule["operator"] not in ALLOWED_REVIEW_OPERATORS:
            errors.append(f"{workflow_name} 存在不支持的流程审查操作符：{review_rule['operator']}")
    return errors


def _text_from_source_content(content: bytes, file_name: str) -> str:
    suffix = Path(file_name).suffix.lower()
    if suffix == ".docx":
        return parse_docx(content)
    if suffix in {".txt", ".md"}:
        return content.decode("utf-8", errors="replace")
    raise ValueError("unsupported import file type, only .docx/.txt/.md are supported")


def _read_source_text(
    file_path: str | None,
    raw_text: str | None,
    *,
    file_name: str | None = None,
    file_content: bytes | None = None,
) -> tuple[str, str, str]:
    if raw_text and raw_text.strip():
        return raw_text.strip(), "raw-text", "workflow-rules.txt"
    if file_content is not None:
        clean_name = str(file_name or "workflow-rules.txt").strip() or "workflow-rules.txt"
        return _text_from_source_content(file_content, clean_name), f"uploaded:{clean_name}", clean_name
    if not file_path:
        raise ValueError("file_path or raw_text is required")
    path = Path(file_path).expanduser()
    if not path.exists() or not path.is_file():
        raise ValueError("file_path not found")
    return _text_from_source_content(path.read_bytes(), path.name), str(path), path.name


def _persist_workflow_rules(
    session: Session,
    *,
    file_name: str,
    source_asset_ref: str,
    source_text: str,
    normalized_rules: list[dict[str, Any]],
    actor: str = "system",
    auto_publish: bool = True,
    llm_used: bool = False,
    llm_output: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    job = WorkflowImportJob(
        file_name=file_name,
        source_asset_ref=source_asset_ref,
        source_text=source_text,
        parse_status="Running",
        status="Draft",
    )
    session.add(job)
    session.flush()

    validation_errors: list[str] = []
    if not normalized_rules:
        validation_errors.append("未识别到流程规则，请检查文档格式。")

    created_versions: list[WorkflowVersion] = []
    diffs: list[dict[str, Any]] = []
    existing_definitions = session.query(WorkflowDefinition).all()
    existing_names = {_normalize_unique_text(row.workflow_name) for row in existing_definitions}
    batch_codes: set[str] = set()
    batch_names: set[str] = set()
    for rule in normalized_rules:
        workflow_code = str(rule.get("workflow_code") or "").strip()
        workflow_name = str(rule.get("workflow_name") or "").strip()
        workflow_name_key = _normalize_unique_text(workflow_name)
        if workflow_code in batch_codes:
            validation_errors.append(f"{workflow_name or workflow_code} 与本次导入的其他流程编码重复：{workflow_code}")
            continue
        if workflow_name_key and workflow_name_key in batch_names:
            validation_errors.append(f"{workflow_name} 与本次导入的其他流程名称重复。")
            continue
        batch_codes.add(workflow_code)
        batch_names.add(workflow_name_key)
        definition = session.query(WorkflowDefinition).filter_by(workflow_code=rule["workflow_code"]).one_or_none()
        if definition is None:
            if workflow_name_key and workflow_name_key in existing_names:
                validation_errors.append(f"流程已存在，请编辑原流程，不要重复新建：{workflow_name}")
                continue
            definition = WorkflowDefinition(
                workflow_code=rule["workflow_code"],
                workflow_name=rule["workflow_name"],
                status="Active",
            )
            session.add(definition)
            session.flush()

        latest = (
            session.query(WorkflowVersion)
            .filter_by(workflow_id=definition.id)
            .order_by(WorkflowVersion.version_no.desc())
            .first()
        )
        current_rules = loads(latest.compiled_rules_json, {}) if latest is not None else {}
        if latest is not None and current_rules != rule:
            validation_errors.append(f"流程已存在，请编辑原流程，不要重复新建：{definition.workflow_name}")
            continue
        if latest is None:
            definition.workflow_name = rule["workflow_name"]
            definition.updated_at = now_utc()
        if current_rules == rule:
            diffs.append({"workflow_code": rule["workflow_code"], "changed": False, "changes": []})
            if latest is not None and auto_publish:
                rule_errors = _validate_rule(rule)
                rule_errors.extend(_validate_production_main_recipient_selection(session, rule))
                if rule_errors:
                    validation_errors.extend(rule_errors)
                else:
                    session.query(WorkflowVersion).filter(
                        WorkflowVersion.workflow_id == definition.id,
                        WorkflowVersion.id != latest.id,
                        WorkflowVersion.status == "Active",
                    ).update({"status": "Archived", "updated_at": now_utc()})
                    latest.status = "Active"
                    latest.approved_by = actor
                    latest.approved_at = now_utc()
                    latest.updated_at = now_utc()
            continue

        rule = _sync_rule_routing_with_production_departments(session, rule)
        rule_errors = _validate_rule(rule)
        if auto_publish:
            rule_errors.extend(_validate_production_main_recipient_selection(session, rule))
        if auto_publish:
            validation_errors.extend(rule_errors)
        can_publish_rule = auto_publish and not rule_errors
        version_no = (latest.version_no + 1) if latest is not None else 1
        version = WorkflowVersion(
            workflow_id=definition.id,
            version_no=version_no,
            source_asset_ref=source_asset_ref,
            source_text=source_text,
            compiled_rules_json=dumps(rule),
            status="Active" if can_publish_rule else "Draft",
            created_by=actor,
            approved_by=actor if can_publish_rule else None,
            approved_at=now_utc() if can_publish_rule else None,
        )
        session.add(version)
        session.flush()
        created_versions.append(version)
        if can_publish_rule:
            session.query(WorkflowVersion).filter(
                WorkflowVersion.workflow_id == definition.id,
                WorkflowVersion.id != version.id,
                WorkflowVersion.status == "Active",
            ).update({"status": "Archived", "updated_at": now_utc()})
        diffs.append(
            {
                "workflow_code": rule["workflow_code"],
                "changed": True,
                "changes": _rules_diff(current_rules, rule) if isinstance(current_rules, dict) else [{"field": "all"}],
                "new_version": version_no,
            }
        )

    job.parse_status = "CompletedWithWarnings" if validation_errors else "Completed"
    job.status = "Published" if auto_publish and not validation_errors else "Draft"
    job.llm_output_json = dumps(llm_output if llm_output is not None else ([] if not llm_used else normalized_rules))
    job.validation_errors_json = dumps(validation_errors)
    job.diff_json = dumps(diffs)
    job.updated_at = now_utc()
    return {
        "job_id": job.id,
        "file_name": file_name,
        "source_asset_ref": source_asset_ref,
        "llm_used": llm_used,
        "validation_errors": validation_errors,
        "diffs": diffs,
        "created_versions": [
            {"id": row.id, "workflow_id": row.workflow_id, "version_no": row.version_no, "status": row.status}
            for row in created_versions
        ],
    }


def import_workflow_document(
    session: Session,
    *,
    file_path: str | None,
    raw_text: str | None,
    file_name: str | None = None,
    file_content: bytes | None = None,
    prefer_llm: bool = True,
    auto_publish: bool = True,
    actor: str = "system",
) -> dict[str, Any]:
    source_text, asset_ref, resolved_file_name = _read_source_text(
        file_path,
        raw_text,
        file_name=file_name,
        file_content=file_content,
    )
    rule_candidates = parse_workflows_by_llm(session, source_text) if prefer_llm else []
    llm_used = bool(rule_candidates)
    if not rule_candidates:
        rule_candidates = parse_workflows_by_rules(source_text)
    normalized = [
        _normalize_rule(item, index, email_only_routing=True)
        for index, item in enumerate(rule_candidates, start=1)
    ]
    return _persist_workflow_rules(
        session,
        file_name=resolved_file_name,
        source_asset_ref=asset_ref,
        source_text=source_text,
        normalized_rules=normalized,
        actor=actor,
        auto_publish=auto_publish,
        llm_used=llm_used,
        llm_output=rule_candidates if llm_used else [],
    )


def import_structured_workflow_rules(
    session: Session,
    *,
    rules: list[dict[str, Any]],
    actor: str = "system",
    auto_publish: bool = False,
    source_text: str | None = None,
    source_asset_ref: str = "workflow-chat",
    file_name: str = "workflow-chat.json",
    llm_used: bool = True,
) -> dict[str, Any]:
    normalized = [_normalize_rule(item, index) for index, item in enumerate(rules, start=1) if isinstance(item, dict)]
    payload_text = source_text.strip() if isinstance(source_text, str) and source_text.strip() else dumps({"workflows": rules})
    return _persist_workflow_rules(
        session,
        file_name=file_name,
        source_asset_ref=source_asset_ref,
        source_text=payload_text,
        normalized_rules=normalized,
        actor=actor,
        auto_publish=auto_publish,
        llm_used=llm_used,
        llm_output=rules if llm_used else [],
    )


def list_workflow_rules(session: Session, *, only_active: bool = False) -> list[dict[str, Any]]:
    query = session.query(WorkflowVersion).join(WorkflowDefinition, WorkflowDefinition.id == WorkflowVersion.workflow_id)
    if only_active:
        query = query.filter(WorkflowVersion.status == "Active")
    rows = query.order_by(WorkflowDefinition.workflow_code, WorkflowVersion.version_no.desc()).all()
    result: list[dict[str, Any]] = []
    for row in rows:
        definition = session.get(WorkflowDefinition, row.workflow_id)
        if definition is None:
            continue
        result.append(
            {
                "definition_id": definition.id,
                "workflow_code": definition.workflow_code,
                "workflow_name": definition.workflow_name,
                "version_id": row.id,
                "version_no": row.version_no,
                "status": row.status,
                "approved_at": row.approved_at.isoformat() if row.approved_at else None,
                "rules": loads(row.compiled_rules_json, {}),
                "is_builtin": False,
                "editable": row.status != "Active",
                "deletable": row.status != "Active",
                "activatable": row.status != "Active",
                "deactivatable": row.status == "Active",
            }
        )
    builtin_rows = _builtin_default_workflow_rows(session)
    if only_active:
        builtin_rows = [row for row in builtin_rows if row.get("status") in {"Active", "BuiltIn"}]
    result.extend(builtin_rows)
    return result


def _builtin_default_workflow_rows(session: Session) -> list[dict[str, Any]]:
    to_names: list[str] = []
    cc_names: list[str] = []
    for department in session.query(ProductionDepartment).filter_by(status="Active").all():
        for email in as_list(department.mail_to_json):
            if email and email not in to_names:
                to_names.append(email)
        for email in as_list(department.mail_cc_json):
            if email and email not in cc_names:
                cc_names.append(email)
    rule = {
        "workflow_code": "builtin_default_order_flow",
        "workflow_name": "默认下单流程（内置）",
        "match": {
            "any_keywords": [],
            "all_keywords": [],
            "warehouse": "",
            "order_type": "normal_sales",
            "subject_patterns": [],
        },
        "routing": {"to_names": to_names, "cc_names": cc_names},
        "subject_template": DEFAULT_TEMPLATE_SUBJECT,
        "body_template": DEFAULT_TEMPLATE_BODY,
        "required_fields": list(CORE_FIELD_LABELS.keys()),
        "required_attachments": [],
        "review_rules": [],
    }
    return [
        {
            "definition_id": "builtin-default-order-flow",
            "workflow_code": rule["workflow_code"],
            "workflow_name": rule["workflow_name"],
            "version_id": "builtin-default-order-flow-v1",
            "version_no": 1,
            "status": "BuiltIn",
            "approved_at": None,
            "rules": rule,
            "is_builtin": True,
            "editable": False,
            "deletable": False,
            "activatable": False,
            "deactivatable": False,
        }
    ]


def activate_workflow_version(session: Session, version_id: str, actor: str = "system") -> WorkflowVersion:
    version = session.get(WorkflowVersion, version_id)
    if version is None:
        raise ValueError("workflow version not found")
    rules = loads(version.compiled_rules_json, {})
    if not isinstance(rules, dict):
        rules = {}
    errors = _validate_rule(rules)
    errors.extend(_validate_production_main_recipient_selection(session, rules))
    if errors:
        raise ValueError("；".join(errors))
    session.query(WorkflowVersion).filter(
        WorkflowVersion.workflow_id == version.workflow_id,
        WorkflowVersion.id != version.id,
        WorkflowVersion.status == "Active",
    ).update({"status": "Archived", "updated_at": now_utc()})
    version.status = "Active"
    version.approved_by = actor
    version.approved_at = now_utc()
    version.updated_at = now_utc()
    return version


def deactivate_workflow_version(session: Session, version_id: str) -> WorkflowVersion:
    version = session.get(WorkflowVersion, version_id)
    if version is None:
        raise ValueError("workflow version not found")
    if version.status != "Active":
        raise ValueError("workflow version is not active")
    version.status = "Archived"
    version.updated_at = now_utc()
    return version


def delete_workflow_version(session: Session, version_id: str) -> None:
    version = session.get(WorkflowVersion, version_id)
    if version is None:
        raise ValueError("workflow version not found")
    if version.status == "Active":
        raise ValueError("active workflow version must be deactivated before delete")

    binding_count = (
        session.query(RequirementWorkflowBinding)
        .filter(RequirementWorkflowBinding.workflow_version_id == version.id)
        .count()
    )
    if binding_count > 0:
        raise ValueError("workflow version is already referenced by tasks and cannot be deleted")

    match_count = (
        session.query(MailWorkflowMatch)
        .filter(MailWorkflowMatch.workflow_version_id == version.id)
        .count()
    )
    if match_count > 0:
        raise ValueError("workflow version is already referenced by mails and cannot be deleted")

    workflow_id = version.workflow_id
    session.delete(version)
    session.flush()

    remaining = session.query(WorkflowVersion).filter(WorkflowVersion.workflow_id == workflow_id).count()
    if remaining == 0:
        definition = session.get(WorkflowDefinition, workflow_id)
        if definition is not None:
            session.delete(definition)
            session.flush()


def save_workflow_version_rules(
    session: Session,
    version_id: str,
    *,
    compiled_rules: dict[str, Any],
    actor: str = "system",
    activate: bool = False,
) -> WorkflowVersion:
    version = session.get(WorkflowVersion, version_id)
    if version is None:
        raise ValueError("workflow version not found")
    if version.status == "Active":
        raise ValueError("active workflow version must be deactivated before edit")
    definition = session.get(WorkflowDefinition, version.workflow_id)
    if definition is None:
        raise ValueError("workflow definition not found")

    normalized = _normalize_rule(compiled_rules, fallback_index=max(version.version_no, 1))
    normalized["workflow_code"] = definition.workflow_code
    if not str(normalized.get("workflow_name") or "").strip():
        normalized["workflow_name"] = definition.workflow_name
    session.flush()
    normalized = _sync_rule_routing_with_production_departments(session, normalized, create_missing=False)
    errors = _validate_rule(normalized)
    errors.extend(_validate_production_main_recipient_selection(session, normalized))
    if errors:
        raise ValueError("；".join(errors))

    target = version
    target.compiled_rules_json = dumps(normalized)
    target.updated_at = now_utc()
    if not target.created_by:
        target.created_by = actor
    if activate:
        target.status = "Active"
        target.approved_by = actor
        target.approved_at = now_utc()
        session.query(WorkflowVersion).filter(
            WorkflowVersion.workflow_id == target.workflow_id,
            WorkflowVersion.id != target.id,
            WorkflowVersion.status == "Active",
        ).update({"status": "Archived", "updated_at": now_utc()})
    else:
        target.status = "Draft"
    session.flush()

    return target


def chat_generate_workflow_rule(
    session: Session,
    *,
    messages: list[dict[str, Any]],
    current_rule: dict[str, Any] | None = None,
    edit_version_id: str | None = None,
) -> dict[str, Any]:
    model = _active_model(session)
    if model is None:
        raise ValueError("active model provider is not configured")
    turns = [item for item in messages if isinstance(item, dict)]
    if not turns:
        raise ValueError("messages is required")

    editing_version = _find_workflow_version_for_chat_edit(session, turns, edit_version_id)
    editing_rule: dict[str, Any] | None = None
    editing_definition: WorkflowDefinition | None = None
    if editing_version is not None:
        editing_definition = session.get(WorkflowDefinition, editing_version.workflow_id)
        loaded_rule = loads(editing_version.compiled_rules_json, {})
        if isinstance(loaded_rule, dict):
            editing_rule = loaded_rule
        if current_rule is None and editing_rule is not None:
            current_rule = editing_rule

    model_messages: list[dict[str, str]] = [{"role": "system", "content": WORKFLOW_CHAT_SYSTEM_PROMPT}]
    if editing_rule is not None and editing_definition is not None:
        model_messages.append(
            {
                "role": "system",
                "content": (
                    "当前任务是编辑已有流程，不是新增流程。必须在以下原流程规则基础上修改并返回完整 workflow_rule；"
                    "除非用户明确要求改名，否则保持 workflow_code 和 workflow_name 不变。\n"
                    f"编辑版本ID：{editing_version.id if editing_version is not None else ''}\n"
                    f"{dumps(editing_rule)}"
                ),
            }
        )
    elif isinstance(current_rule, dict) and current_rule:
        model_messages.append(
            {
                "role": "system",
                "content": (
                    "当前已有流程草稿（可按需改写后返回完整 workflow_rule）：\n"
                    f"{dumps(current_rule)}"
                ),
            }
        )

    for item in turns[-16:]:
        role = str(item.get("role") or "user").strip().lower()
        if role not in {"user", "assistant"}:
            role = "user"
        content = str(item.get("content") or "").strip()
        if not content:
            continue
        model_messages.append({"role": role, "content": content[:4000]})

    output = call_model(
        session,
        model,
        task_type="WorkflowChatGenerate",
        messages=model_messages,
    )
    content = extract_chat_content(output).strip()
    payload = parse_json_object(content)
    if not payload:
        payload = {"assistant_reply": content} if content else {}

    reply = str(payload.get("assistant_reply") or payload.get("reply") or "").strip()
    if not reply:
        reply = "已更新流程草稿。"

    candidate = payload.get("workflow_rule")
    compiled_rule: dict[str, Any] | None = None
    validation_errors: list[str] = []
    if isinstance(candidate, dict):
        merged = dict(current_rule) if isinstance(current_rule, dict) else {}
        merged.update(candidate)
        compiled_rule = _normalize_rule(merged, fallback_index=1)
        if editing_rule is not None:
            compiled_rule["workflow_code"] = str(editing_rule.get("workflow_code") or compiled_rule.get("workflow_code") or "")
            compiled_rule["workflow_name"] = str(editing_rule.get("workflow_name") or compiled_rule.get("workflow_name") or "")
        validation_errors = _validate_rule(compiled_rule)
    elif isinstance(current_rule, dict) and current_rule:
        compiled_rule = _normalize_rule(current_rule, fallback_index=1)
        if editing_rule is not None:
            compiled_rule["workflow_code"] = str(editing_rule.get("workflow_code") or compiled_rule.get("workflow_code") or "")
            compiled_rule["workflow_name"] = str(editing_rule.get("workflow_name") or compiled_rule.get("workflow_name") or "")
        validation_errors = _validate_rule(compiled_rule)

    inferred_workflow_name = "" if editing_rule is not None else _infer_workflow_name_from_turns(turns, compiled_rule)
    if inferred_workflow_name:
        merged = dict(compiled_rule) if isinstance(compiled_rule, dict) else {}
        merged["workflow_name"] = inferred_workflow_name
        compiled_rule = _normalize_rule(merged, fallback_index=1)
        validation_errors = _validate_rule(compiled_rule)

    gaps = _workflow_definition_gaps(compiled_rule)
    guided_question, pending_questions = _build_guided_followup(gaps)
    ready = compiled_rule is not None and not validation_errors and not gaps
    notification = ""
    if ready:
        notification = "已有流程规则已更新，请保存草稿或直接启用。" if editing_rule is not None else "流程定义已完成，已自动生成该流程对应规则。请保存草稿或直接启用。"
    elif guided_question and guided_question not in reply:
        reply = f"{reply}\n\n{guided_question}".strip()

    return {
        "reply": reply,
        "ready": ready,
        "compiled_rule": compiled_rule,
        "validation_errors": validation_errors,
        "next_question": guided_question if not ready else "",
        "pending_questions": pending_questions if not ready else [],
        "notification": notification,
        "edit_version_id": editing_version.id if editing_version is not None else "",
        "edit_workflow_name": editing_rule.get("workflow_name") if isinstance(editing_rule, dict) else "",
    }


def _text_for_match(mail: MailMessage, source_text: str) -> str:
    return f"{mail.subject}\n{source_text}".lower()


def _to_int(value: Any, fallback: int = 0) -> int:
    try:
        return int(float(str(value).strip()))
    except Exception:
        return fallback


def _workflow_score(rule: dict[str, Any], mail: MailMessage, source_text: str) -> tuple[int, list[str]]:
    score = 0
    reasons: list[str] = []
    compact = re.sub(r"\s+", "", _text_for_match(mail, source_text))
    match = rule.get("match") if isinstance(rule.get("match"), dict) else {}

    any_keywords = _normalize_list(match.get("any_keywords"))
    hits = [keyword for keyword in any_keywords if keyword and keyword.lower() in compact]
    if hits:
        score += min(45, len(hits) * 12)
        reasons.append(f"命中关键词: {', '.join(hits[:6])}")

    all_keywords = _normalize_list(match.get("all_keywords"))
    if all_keywords:
        if all(keyword.lower() in compact for keyword in all_keywords):
            score += 20
            reasons.append("命中全部关键词")
        else:
            score -= 20

    subject_patterns = _normalize_list(match.get("subject_patterns"))
    if subject_patterns and any(pattern.lower() in (mail.subject or "").lower() for pattern in subject_patterns):
        score += 20
        reasons.append("命中主题模式")

    warehouse = str(match.get("warehouse") or "").strip().lower()
    if warehouse == "wuhan":
        if "武汉仓" in compact:
            score += 15
            reasons.append("命中武汉仓")
        if "海外仓" in compact:
            score -= 8
    elif warehouse == "overseas":
        if "海外仓" in compact:
            score += 15
            reasons.append("命中海外仓")

    order_type = str(match.get("order_type") or "").strip().lower()
    if order_type == "sample_borrow" and "样机借用" in compact:
        score += 12
        reasons.append("命中样机借用")
    elif order_type == "refill" and "补单" in compact:
        score += 12
        reasons.append("命中补单")
    elif order_type == "sample_gift" and "样机赠送" in compact:
        score += 12
        reasons.append("命中样机赠送")
    elif order_type == "normal_sales" and any(word in compact for word in ["销售订单", "采购订单", "下单", "排产"]):
        score += 8
        reasons.append("命中常规订单词")

    return score, reasons


def _llm_match_workflow(
    session: Session,
    mail: MailMessage,
    source_text: str,
    candidates: list[_WorkflowScoredCandidate],
) -> tuple[_WorkflowScoredCandidate, int, str] | None:
    model = _active_model(session)
    if model is None or not candidates:
        return None

    candidate_items = []
    for item in candidates[:8]:
        candidate_items.append(
            {
                "workflow_code": item.rule.get("workflow_code"),
                "workflow_name": item.rule.get("workflow_name"),
                "match": item.rule.get("match"),
                "required_fields": item.rule.get("required_fields"),
                "required_attachments": item.rule.get("required_attachments"),
                "heuristic_score": item.score,
            }
        )

    try:
        output = call_model(
            session,
            model,
            task_type="WorkflowRoutingMatch",
            related_object_type="MailMessage",
            related_object_id=mail.id,
            messages=[
                {
                    "role": "system",
                    "content": (
                        "你是邮件流程路由判定器。只返回 JSON，不要解释。"
                        "输出格式：{\"workflow_code\":\"...|NONE\",\"confidence\":0-100,\"reason\":\"...\"}。"
                        "workflow_code 必须是候选里的编码，无法判定就返回 NONE。"
                    ),
                },
                {
                    "role": "user",
                    "content": (
                        f"邮件主题：{mail.subject}\n"
                        f"发件人：{mail.from_address}\n"
                        f"邮件内容：\n{source_text[:8000]}\n\n"
                        f"候选流程：{dumps(candidate_items)}\n"
                        "请返回最合适的 workflow_code。"
                    ),
                },
            ],
        )
    except Exception:
        return None

    parsed = parse_json_object(extract_chat_content(output))
    selected_code = str(parsed.get("workflow_code") or "").strip()
    if not selected_code:
        return None
    if selected_code.upper() in {"NONE", "NO_MATCH", "NULL"}:
        return None
    confidence = max(0, min(98, _to_int(parsed.get("confidence"), fallback=0)))
    reason = str(parsed.get("reason") or "").strip()
    for item in candidates:
        if str(item.rule.get("workflow_code") or "") == selected_code:
            return item, confidence, reason
    return None


def match_workflow_for_mail(session: Session, mail: MailMessage, source_text: str) -> WorkflowMatchResult | None:
    versions = session.query(WorkflowVersion).filter_by(status="Active").all()
    scored: list[_WorkflowScoredCandidate] = []
    for version in versions:
        rule = loads(version.compiled_rules_json, {})
        if not isinstance(rule, dict):
            continue
        score, reasons = _workflow_score(rule, mail, source_text)
        scored.append(_WorkflowScoredCandidate(version=version, rule=rule, score=score, reasons=reasons))

    if not scored:
        return None

    scored.sort(key=lambda item: item.score, reverse=True)
    best = scored[0]
    best_score = best.score

    llm_selected = _llm_match_workflow(session, mail, source_text, scored) if len(scored) > 1 else None
    if llm_selected is not None:
        llm_candidate, llm_confidence, llm_reason = llm_selected
        if llm_confidence >= 55:
            llm_reasons = list(llm_candidate.reasons)
            if llm_reason:
                llm_reasons.append(f"LLM判定: {llm_reason}")
            return WorkflowMatchResult(
                version=llm_candidate.version,
                rule=llm_candidate.rule,
                confidence=llm_confidence,
                reasons=llm_reasons,
            )

    if best_score < 20:
        return None
    confidence = max(0, min(98, 40 + best_score))
    return WorkflowMatchResult(version=best.version, rule=best.rule, confidence=confidence, reasons=best.reasons)


def upsert_mail_workflow_match(session: Session, mail: MailMessage, match: WorkflowMatchResult | None) -> MailWorkflowMatch:
    row = session.query(MailWorkflowMatch).filter_by(mail_id=mail.id).one_or_none()
    if row is None:
        row = MailWorkflowMatch(mail_id=mail.id)
        session.add(row)
    if match is None:
        row.workflow_version_id = None
        row.workflow_code = None
        row.confidence = 0
        row.match_detail_json = dumps({"matched": False})
    else:
        row.workflow_version_id = match.version.id
        row.workflow_code = str(match.rule.get("workflow_code") or "")
        row.confidence = match.confidence
        row.match_detail_json = dumps(
            {
                "matched": True,
                "workflow_name": match.rule.get("workflow_name"),
                "reasons": match.reasons,
                "score_confidence": match.confidence,
            }
        )
    return row


def extract_workflow_fields(source_text: str, required_fields: list[str]) -> dict[str, str]:
    extracted: dict[str, str] = {}
    for field in required_fields:
        if field in CORE_FIELD_LABELS:
            continue
        hints = FIELD_HINTS.get(field)
        if hints is None:
            continue
        value = _extract_workflow_field_value(source_text, field, [str(item) for item in hints["keywords"]])
        if value:
            extracted[field] = value
    return extracted


def _field_line_label(line: str) -> str | None:
    match = re.match(r"\s*([^:：]{1,30})\s*[:：]", line)
    if not match:
        return None
    return match.group(1).strip()


def _workflow_field_stop_labels(current_field: str) -> set[str]:
    labels = {"附件", "邮件主题", "邮件内容模板", "邮件收件人", "邮件抄送人"}
    labels.update(CORE_FIELD_LABELS.values())
    for hints in FIELD_HINTS.values():
        labels.add(str(hints.get("label") or ""))
        labels.update(str(item) for item in hints.get("keywords", []))
    hints = FIELD_HINTS.get(current_field) or {}
    labels.discard(str(hints.get("label") or ""))
    for keyword in hints.get("keywords", []):
        labels.discard(str(keyword))
    if current_field == "material_details":
        labels.difference_update(MATERIAL_DETAIL_CHILD_LABELS)
    return {item for item in labels if item}


def _collect_following_field_lines(lines: list[str], start_index: int, current_field: str) -> list[str]:
    stop_labels = _workflow_field_stop_labels(current_field)
    values: list[str] = []
    for line in lines[start_index + 1 :]:
        stripped = line.strip()
        if not stripped:
            if values:
                break
            continue
        label = _field_line_label(stripped)
        if label and label in stop_labels:
            break
        values.append(stripped)
    return values


def _extract_workflow_field_value(source_text: str, field: str, keywords: list[str]) -> str:
    lines = [line.rstrip() for line in source_text.splitlines()]
    for index, line in enumerate(lines):
        stripped = line.strip()
        if not stripped:
            continue
        for keyword in keywords:
            match = re.match(rf"^\s*{re.escape(keyword)}\s*[:：]\s*(.*)$", stripped)
            if match:
                first_value = match.group(1).strip()
                values = [first_value] if first_value else []
                if field == "material_details" or not first_value:
                    values.extend(_collect_following_field_lines(lines, index, field))
                return "\n".join(item for item in values if item).strip() or keyword
            if keyword in stripped:
                return stripped[:500]
    return ""


def resolve_contact_emails(
    session: Session,
    names: list[str],
    *,
    salesperson_email: str | None,
) -> tuple[list[str], list[str]]:
    mapping = loads(_get_config(session, "workflow_contact_map_json", "{}"), {})
    if not isinstance(mapping, dict):
        mapping = {}
    department_lookup = _production_department_lookup(session)
    emails: list[str] = []
    unresolved: list[str] = []
    for name in names:
        key = str(name).strip()
        if not key:
            continue
        if "@" in key:
            if key not in emails:
                emails.append(key)
            continue
        if key in {"下单销售", "下单销售员", "下单销售人员"} and salesperson_email:
            if salesperson_email not in emails:
                emails.append(salesperson_email)
            continue
        department = department_lookup.get(_normalize_unique_text(key))
        if department is not None:
            department_emails = [item for item in as_list(department.mail_to_json) if _is_email_address(item)]
            if department_emails:
                for item in department_emails:
                    if item not in emails:
                        emails.append(item)
                continue
        mapped = mapping.get(key)
        if isinstance(mapped, str) and mapped.strip():
            if mapped not in emails:
                emails.append(mapped)
            continue
        if isinstance(mapped, list):
            for item in mapped:
                item_text = str(item).strip()
                if item_text and item_text not in emails:
                    emails.append(item_text)
            if mapped:
                continue
        unresolved.append(key)
    return emails, unresolved


def workflow_binding_for_requirement(session: Session, requirement_id: str) -> RequirementWorkflowBinding | None:
    return session.query(RequirementWorkflowBinding).filter_by(requirement_id=requirement_id).one_or_none()
