from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date, timedelta

from sqlalchemy.orm import Session

from backend.app.models import OrderRequirement, ProductionTask, SystemConfig, WorkflowVersion, now_utc
from backend.app.services.jsonutil import dumps, loads


def get_config(session: Session, key: str, fallback: str = "") -> str:
    config = session.get(SystemConfig, key)
    return config.value if config is not None else fallback


FIELD_OPTIONS = [
    {"key": "customer_name", "label": "客户名称"},
    {"key": "product_summary", "label": "物料/规格"},
    {"key": "quantity_text", "label": "数量"},
    {"key": "expected_delivery_date", "label": "期望交期"},
    {"key": "external_order_no", "label": "订单号"},
    {"key": "salesperson_email", "label": "销售邮箱"},
    {"key": "source_text", "label": "邮件全文"},
]

FIELD_LABELS = {item["key"]: item["label"] for item in FIELD_OPTIONS}

DEFAULT_REQUIRED_FIELDS = ["customer_name", "product_summary", "quantity_text", "expected_delivery_date"]

OPERATOR_OPTIONS = [
    {"key": "required", "label": "必填"},
    {"key": "contains", "label": "必须包含"},
    {"key": "not_contains", "label": "不能包含"},
    {"key": "regex", "label": "正则匹配"},
    {"key": "not_regex", "label": "正则不匹配"},
    {"key": "min_number", "label": "数字不小于"},
    {"key": "max_number", "label": "数字不大于"},
    {"key": "date_format", "label": "日期格式有效"},
    {"key": "date_after_or_equal", "label": "日期不早于"},
    {"key": "date_before_or_equal", "label": "日期不晚于"},
]

SYSTEM_INITIAL_REVIEW_RULES = [
    {
        "id": "builtin-required-core-fields",
        "name": "核心字段完整性检查",
        "field": "source_text",
        "operator": "system_check",
        "value": "客户名称、物料/规格、数量、期望交期",
        "message": "客户名称、物料/规格、数量、期望交期为系统必填信息，缺失时会自动要求销售补充。",
        "enabled": True,
        "read_only": True,
        "is_builtin": True,
    },
    {
        "id": "builtin-parser-risk-flags",
        "name": "解析风险拦截",
        "field": "source_text",
        "operator": "system_check",
        "value": "AI解析置信度、附件解析异常、关键信息冲突",
        "message": "AI解析存在风险或信息冲突时，系统会阻止自动创建任务并进入异常处理。",
        "enabled": True,
        "read_only": True,
        "is_builtin": True,
    },
    {
        "id": "builtin-duplicate-submission",
        "name": "24小时重复提交检查",
        "field": "source_text",
        "operator": "system_check",
        "value": "同一销售、同一订单号或同一客户/物料/数量/交期",
        "message": "同一销售在24小时内重复提交同一需求时，系统会回复已提交并提示不要重复提交。",
        "enabled": True,
        "read_only": True,
        "is_builtin": True,
    },
]

WORKFLOW_REVIEW_RULE_DELETED_IDS_KEY = "initial_review_workflow_rule_deleted_ids_json"
INITIAL_REVIEW_RULES_KEY = "initial_review_rules_json"


@dataclass(frozen=True)
class ReviewFailure:
    field: str
    field_label: str
    rule_name: str
    message: str
    workflow_code: str = ""
    workflow_name: str = ""
    workflow_version_id: str = ""
    is_workflow_rule: bool = False


@dataclass(frozen=True)
class ReviewResult:
    enabled: bool
    passed: bool
    missing_fields: list[str]
    risk_flags: list[str]
    failures: list[ReviewFailure]


def _set_config(session: Session, key: str, value: str, *, is_secret: bool = False) -> None:
    config = session.get(SystemConfig, key)
    if config is None:
        session.add(SystemConfig(key=key, value=value, is_secret=is_secret))
        return
    config.value = value
    config.is_secret = is_secret
    config.updated_at = now_utc()


def _review_rule_signature(rule: dict) -> tuple[str, str, str]:
    field = str(rule.get("field") or "source_text").strip().lower()
    operator = str(rule.get("operator") or "contains").strip().lower()
    value = re.sub(r"\s+", "", str(rule.get("value") or "")).strip()
    return field, operator, value


def dedupe_initial_review_rules(rules: list[dict]) -> list[dict]:
    result: list[dict] = []
    seen: set[tuple[str, str, str]] = set()
    for rule in rules:
        if not isinstance(rule, dict):
            continue
        signature = _review_rule_signature(rule)
        if signature in seen:
            continue
        seen.add(signature)
        result.append(rule)
    return result


def _workflow_review_rules_for_sync(session: Session) -> list[dict]:
    rows = (
        session.query(WorkflowVersion)
        .filter(WorkflowVersion.status != "Archived")
        .order_by(WorkflowVersion.created_at.desc())
        .all()
    )
    rules: list[dict] = []
    seen: set[str] = set()
    for row in rows:
        compiled_rules = loads(row.compiled_rules_json, {})
        if not isinstance(compiled_rules, dict):
            continue
        workflow_code = str(compiled_rules.get("workflow_code") or "").strip()
        workflow_name = str(compiled_rules.get("workflow_name") or workflow_code or "未命名流程").strip()
        for index, rule in enumerate(compiled_rules.get("review_rules", []), start=1):
            if not isinstance(rule, dict):
                continue
            original_id = str(rule.get("id") or f"workflow-rule-{index}").strip()
            display_id = f"workflow:{row.id}:{original_id}"
            if display_id in seen:
                continue
            seen.add(display_id)
            display_rule = dict(rule)
            display_rule["id"] = display_id
            display_rule["name"] = str(rule.get("name") or "流程初审规则")
            display_rule["message"] = str(rule.get("message") or "未填写未通过原因")
            display_rule["enabled"] = bool(rule.get("enabled", True))
            display_rule["is_workflow_rule"] = True
            display_rule["workflow_version_id"] = row.id
            display_rule["workflow_code"] = workflow_code
            display_rule["workflow_name"] = workflow_name
            rules.append(display_rule)
    return rules


def sync_workflow_review_rules_to_initial_review(session: Session) -> list[dict]:
    raw_custom_rules = loads(get_config(session, INITIAL_REVIEW_RULES_KEY, "[]"), [])
    if not isinstance(raw_custom_rules, list):
        raw_custom_rules = []
    custom_rules = dedupe_initial_review_rules(raw_custom_rules)
    deleted_ids = loads(get_config(session, WORKFLOW_REVIEW_RULE_DELETED_IDS_KEY, "[]"), [])
    deleted_id_set = {str(item) for item in deleted_ids if str(item).strip()}
    existing_ids = {str(rule.get("id") or "") for rule in custom_rules if isinstance(rule, dict)}
    existing_signatures = {_review_rule_signature(rule) for rule in custom_rules if isinstance(rule, dict)}

    changed = len(custom_rules) != len(raw_custom_rules)
    for rule in _workflow_review_rules_for_sync(session):
        rule_id = str(rule.get("id") or "")
        signature = _review_rule_signature(rule)
        if not rule_id or rule_id in existing_ids or rule_id in deleted_id_set or signature in existing_signatures:
            continue
        custom_rules.append(rule)
        existing_ids.add(rule_id)
        existing_signatures.add(signature)
        changed = True

    if changed:
        _set_config(session, INITIAL_REVIEW_RULES_KEY, dumps(custom_rules), is_secret=False)
    return [rule for rule in custom_rules if isinstance(rule, dict)]


def remember_deleted_workflow_review_rules(session: Session, payload_rule_ids: set[str]) -> None:
    custom_rules = sync_workflow_review_rules_to_initial_review(session)
    deleted_ids = loads(get_config(session, WORKFLOW_REVIEW_RULE_DELETED_IDS_KEY, "[]"), [])
    deleted_id_set = {str(item) for item in deleted_ids if str(item).strip()}
    for rule in custom_rules:
        rule_id = str(rule.get("id") or "")
        if rule_id.startswith("workflow:") and rule_id not in payload_rule_ids:
            deleted_id_set.add(rule_id)
    if deleted_id_set != {str(item) for item in deleted_ids if str(item).strip()}:
        _set_config(session, WORKFLOW_REVIEW_RULE_DELETED_IDS_KEY, dumps(sorted(deleted_id_set)), is_secret=False)


def initial_review_config(session: Session, *, include_workflow_rules: bool = False) -> dict:
    custom_rules = (
        sync_workflow_review_rules_to_initial_review(session)
        if include_workflow_rules
        else loads(get_config(session, INITIAL_REVIEW_RULES_KEY, "[]"), [])
    )
    if not include_workflow_rules:
        custom_rules = [
            rule
            for rule in custom_rules
            if isinstance(rule, dict)
            and not rule.get("is_workflow_rule")
            and not str(rule.get("id") or "").startswith("workflow:")
        ]
    display_rules = [dict(rule) for rule in SYSTEM_INITIAL_REVIEW_RULES] + [rule for rule in custom_rules if isinstance(rule, dict)]
    return {
        "enabled": get_config(session, "initial_review_enabled", "true").lower() in {"1", "true", "yes", "on"},
        "required_fields": loads(
            get_config(session, "initial_review_required_fields_json", dumps(DEFAULT_REQUIRED_FIELDS)),
            DEFAULT_REQUIRED_FIELDS,
        ),
        "rules": display_rules,
        "field_options": FIELD_OPTIONS,
        "operator_options": OPERATOR_OPTIONS,
    }


def _field_value(requirement: OrderRequirement, source_text: str, field: str) -> str:
    if field == "source_text":
        return source_text or ""
    return str(getattr(requirement, field, "") or "")


def _first_number(value: str) -> float | None:
    match = re.search(r"-?\d+(?:\.\d+)?", value or "")
    if not match:
        return None
    return float(match.group(0))


def _parse_date(value: str) -> date | None:
    match = re.search(r"(\d{4})[-/.年](\d{1,2})[-/.月](\d{1,2})", value or "")
    if not match:
        return None
    year, month, day = (int(part) for part in match.groups())
    try:
        return date(year, month, day)
    except ValueError:
        return None


def _rule_passes(target: str, operator: str, expected: str) -> bool:
    if operator == "required":
        return bool(target.strip())
    if operator == "contains":
        return bool(expected) and expected in target
    if operator == "not_contains":
        return bool(expected) and expected not in target
    if operator == "regex":
        return bool(expected) and re.search(expected, target, flags=re.IGNORECASE | re.MULTILINE) is not None
    if operator == "not_regex":
        return bool(expected) and re.search(expected, target, flags=re.IGNORECASE | re.MULTILINE) is None
    if operator == "min_number":
        actual = _first_number(target)
        threshold = _first_number(expected)
        return actual is not None and threshold is not None and actual >= threshold
    if operator == "max_number":
        actual = _first_number(target)
        threshold = _first_number(expected)
        return actual is not None and threshold is not None and actual <= threshold
    if operator == "date_format":
        return _parse_date(target) is not None
    if operator == "date_after_or_equal":
        actual = _parse_date(target)
        threshold = _parse_date(expected)
        return actual is not None and threshold is not None and actual >= threshold
    if operator == "date_before_or_equal":
        actual = _parse_date(target)
        threshold = _parse_date(expected)
        return actual is not None and threshold is not None and actual <= threshold
    return True


def _normalize_compare_text(value: str | None) -> str:
    compact = re.sub(r"\s+", "", str(value or "")).strip().lower()
    return compact


def _same_requirement_content(current: OrderRequirement, existing: OrderRequirement) -> bool:
    current_order_no = _normalize_compare_text(current.external_order_no)
    existing_order_no = _normalize_compare_text(existing.external_order_no)
    if current_order_no and existing_order_no:
        return current_order_no == existing_order_no

    fields = ("customer_name", "product_summary", "quantity_text", "expected_delivery_date")
    for field in fields:
        left = _normalize_compare_text(getattr(current, field, ""))
        right = _normalize_compare_text(getattr(existing, field, ""))
        if not left or not right or left != right:
            return False
    return True


def find_recent_duplicate_requirement(session: Session, requirement: OrderRequirement, *, hours: int = 24) -> tuple[OrderRequirement, ProductionTask | None] | tuple[None, None]:
    salesperson_email = (requirement.salesperson_email or "").strip().lower()
    if not salesperson_email:
        return None, None

    cutoff_at = now_utc() - timedelta(hours=hours)
    candidates = (
        session.query(OrderRequirement)
        .filter(
            OrderRequirement.id != requirement.id,
            OrderRequirement.created_at >= cutoff_at,
            OrderRequirement.salesperson_email.is_not(None),
        )
        .order_by(OrderRequirement.created_at.desc())
        .all()
    )
    for candidate in candidates:
        if candidate.source_mail_id == requirement.source_mail_id:
            continue
        if _normalize_compare_text(candidate.salesperson_email) != salesperson_email:
            continue
        if not _same_requirement_content(requirement, candidate):
            continue
        task = (
            session.query(ProductionTask)
            .filter_by(requirement_id=candidate.id)
            .order_by(ProductionTask.created_at.desc())
            .first()
        )
        return candidate, task
    return None, None


def evaluate_initial_review(
    session: Session,
    requirement: OrderRequirement,
    *,
    source_text: str,
    parser_risk_flags: list[str] | None = None,
    required_fields_override: list[str] | None = None,
    rules_override: list[dict] | None = None,
    include_duplicate_check: bool = True,
) -> ReviewResult:
    config = initial_review_config(session)
    enabled = bool(config["enabled"])
    if not enabled:
        return ReviewResult(enabled=False, passed=True, missing_fields=[], risk_flags=[], failures=[])

    required_fields = required_fields_override if required_fields_override is not None else list(config["required_fields"])
    rules = rules_override if rules_override is not None else list(config["rules"])

    failures: list[ReviewFailure] = []
    missing_fields: list[str] = []
    rule_risk_flags: list[str] = []
    for field in required_fields:
        label = FIELD_LABELS.get(field, field)
        if not _field_value(requirement, source_text, field).strip():
            missing_fields.append(label)
            failures.append(
                ReviewFailure(
                    field=field,
                    field_label=label,
                    rule_name="必填字段",
                    message=f"{label}不能为空",
                )
            )

    risk_flags = list(parser_risk_flags or [])
    for flag in risk_flags:
        failures.append(
            ReviewFailure(
                field="source_text",
                field_label=FIELD_LABELS["source_text"],
                rule_name="内置风险识别",
                message=flag,
            )
        )

    if include_duplicate_check:
        duplicate_requirement, duplicate_task = find_recent_duplicate_requirement(session, requirement, hours=24)
        if duplicate_requirement is not None:
            duplicate_message = "同一需求在24小时内已提交，请勿重复提交。"
            if duplicate_task is not None:
                duplicate_message = f"{duplicate_message} 已存在任务号：{duplicate_task.task_no}。"
            failures.append(
                ReviewFailure(
                    field="source_text",
                    field_label=FIELD_LABELS["source_text"],
                    rule_name="重复提交检查",
                    message=duplicate_message,
                )
            )
            rule_risk_flags.append(duplicate_message)

    for rule in rules:
        if not isinstance(rule, dict) or not rule.get("enabled", True):
            continue
        if rule.get("read_only") or rule.get("is_builtin"):
            continue
        field = str(rule.get("field") or "source_text")
        operator = str(rule.get("operator") or "contains")
        expected = str(rule.get("value") or "")
        target = _field_value(requirement, source_text, field)
        try:
            passed = _rule_passes(target, operator, expected)
        except re.error as exc:
            passed = False
            expected = f"{expected} ({exc})"
        if passed:
            continue
        label = FIELD_LABELS.get(field, field)
        name = str(rule.get("name") or "自定义规则")
        message = str(rule.get("message") or f"{label} 未通过规则：{name}")
        failures.append(ReviewFailure(field=field, field_label=label, rule_name=name, message=message))
        rule_risk_flags.append(message)

    return ReviewResult(
        enabled=True,
        passed=not failures,
        missing_fields=missing_fields,
        risk_flags=risk_flags + rule_risk_flags,
        failures=failures,
    )


def serialize_review_failures(failures: list[ReviewFailure]) -> list[dict]:
    return [
        {
            "field": failure.field,
            "field_label": failure.field_label,
            "rule_name": failure.rule_name,
            "message": failure.message,
            "workflow_code": failure.workflow_code,
            "workflow_name": failure.workflow_name,
            "workflow_version_id": failure.workflow_version_id,
            "is_workflow_rule": failure.is_workflow_rule,
        }
        for failure in failures
    ]
