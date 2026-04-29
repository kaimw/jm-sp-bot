from __future__ import annotations

import shlex
import subprocess
from datetime import timedelta
from pathlib import Path
from typing import Any

from sqlalchemy import func
from sqlalchemy.orm import Session

from backend.app.models import (
    AuditEvent,
    ExceptionCase,
    MaintenanceAction,
    MaintenanceSession,
    ModelCallLog,
    ModelProviderConfig,
    OutboundMailJob,
    ProcessingJob,
    SystemConfig,
    now_utc,
)
from backend.app.services.jsonutil import dumps, loads
from backend.app.services.model_provider import call_model, extract_chat_content, resolve_api_key
from backend.app.services.bootstrap import set_config

SAFE_CONFIG_KEYS = {
    "bot_enabled",
    "bot_email",
    "bot_display_name",
    "imap_host",
    "imap_port",
    "smtp_host",
    "smtp_port",
    "mail_auto_worker_interval_seconds",
    "mail_rate_limit_interval_seconds",
    "llm_fallback_enabled",
    "conversation_max_rounds",
    "outbound_failed_alert_threshold",
    "outbound_pending_age_alert_seconds",
    "non_target_retention_days",
}

CONFIG_PATCH_ALLOWED_KEYS = {
    "llm_fallback_enabled",
    "conversation_max_rounds",
    "outbound_failed_alert_threshold",
    "outbound_pending_age_alert_seconds",
    "mail_auto_worker_interval_seconds",
    "mail_rate_limit_interval_seconds",
}

CONFIG_PATCH_LIMITS = {
    "conversation_max_rounds": (1, 20),
    "outbound_failed_alert_threshold": (1, 100),
    "outbound_pending_age_alert_seconds": (60, 86400),
    "mail_auto_worker_interval_seconds": (60, 86400),
    "mail_rate_limit_interval_seconds": (60, 86400),
}

CODEBASE_AREAS = [
    {
        "area": "API orchestration",
        "files": ["backend/app/main.py", "backend/app/schemas.py"],
        "when": ["endpoint", "api", "接口", "页面调用", "权限"],
    },
    {
        "area": "Self-maintenance",
        "files": ["backend/app/services/self_maintenance.py", "tests/test_workflow.py"],
        "when": ["自维护", "诊断", "修复", "维护", "配置草案"],
    },
    {
        "area": "Workflow and email business logic",
        "files": ["backend/app/services/workflow.py", "backend/app/services/jobs.py", "tests/test_workflow.py"],
        "when": ["订单", "任务单", "异常", "缺字段", "生产", "销售", "入库"],
    },
    {
        "area": "Mail adapters",
        "files": ["backend/app/services/mail_adapter.py", "backend/app/services/mail_worker.py", "tests/test_workflow.py"],
        "when": ["imap", "smtp", "邮箱", "外发", "同步", "worker"],
    },
    {
        "area": "Attachment parsing",
        "files": ["backend/app/services/attachment_parser.py", "backend/app/services/storage.py", "tests/test_workflow.py"],
        "when": ["附件", "docx", "xlsx", "pdf", "zip", "解析"],
    },
    {
        "area": "Admin UI",
        "files": ["backend/app/static/index.html", "backend/app/static/app.js", "backend/app/static/styles.css"],
        "when": ["管理台", "页面", "按钮", "前端", "显示"],
    },
    {
        "area": "Database and migration",
        "files": ["backend/app/models.py", "scripts/migrate_sqlite_to_database.py", "tests/test_database_migration.py"],
        "when": ["数据库", "迁移", "表", "字段", "模型"],
    },
]

VALIDATION_COMMANDS = [
    "python3 -m compileall backend scripts",
    "python3 -m pytest",
    "node --check backend/app/static/app.js",
]

MAINTENANCE_OUTPUT_DIR = Path("data/maintenance")

MAINTENANCE_ACTION_LABELS = {
    "operator_review": "人工排障建议",
    "config_patch": "配置修复草案",
    "code_patch_plan": "代码修复草案",
    "llm_failure": "模型诊断失败",
}

MAINTENANCE_AUDIT_LABELS = {
    "SelfMaintenanceDiagnosed": "诊断已生成",
    "SelfMaintenanceCodePlanCreated": "代码修复草案已生成",
    "SelfMaintenanceActionApplied": "配置修复已应用",
    "SelfMaintenanceImplementationReported": "实现结果已回填",
    "SelfMaintenanceHandoffCreated": "交接包已生成",
    "SelfMaintenanceValidationCompleted": "验证已完成",
    "SelfMaintenanceImplementationReviewed": "人工复核已记录",
    "SelfMaintenanceSessionArchived": "维护会话已归档",
    "MaintenanceRunnerHandoffCreated": "runner 交接包已生成",
    "MaintenanceRunnerValidationCompleted": "runner 验证已完成",
    "MaintenanceRunnerImplementationReported": "runner 实现结果已回填",
    "MaintenanceRunnerImplementationReviewed": "runner 复核已记录",
}


def _counts_by_status(session: Session, model) -> dict[str, int]:
    return {str(status): int(count) for status, count in session.query(model.status, func.count(model.id)).group_by(model.status).all()}


def _seconds_since(value) -> int | None:
    if value is None:
        return None
    current = now_utc()
    if value.tzinfo is None:
        current = current.replace(tzinfo=None)
    return max(0, int((current - value).total_seconds()))


def _safe_configs(session: Session) -> dict[str, str]:
    rows = session.query(SystemConfig).filter(SystemConfig.key.in_(SAFE_CONFIG_KEYS)).all()
    return {row.key: ("***" if row.is_secret else row.value) for row in rows}


def _recent_exceptions(session: Session, limit: int = 8) -> list[dict[str, Any]]:
    rows = (
        session.query(ExceptionCase)
        .filter(ExceptionCase.status == "Open")
        .order_by(ExceptionCase.created_at.desc())
        .limit(limit)
        .all()
    )
    return [
        {
            "id": row.id,
            "exception_type": row.exception_type,
            "severity": row.severity,
            "related_task_id": row.related_task_id,
            "detail": loads(row.detail, {}),
            "created_at": row.created_at.isoformat(),
        }
        for row in rows
    ]


def _recent_processing_failures(session: Session, limit: int = 8) -> list[dict[str, Any]]:
    rows = (
        session.query(ProcessingJob)
        .filter(ProcessingJob.status == "Failed")
        .order_by(ProcessingJob.updated_at.desc())
        .limit(limit)
        .all()
    )
    return [
        {
            "id": row.id,
            "job_type": row.job_type,
            "attempt_count": row.attempt_count,
            "error_message": row.error_message,
            "updated_at": row.updated_at.isoformat(),
        }
        for row in rows
    ]


def _recent_outbound_failures(session: Session, limit: int = 8) -> list[dict[str, Any]]:
    rows = (
        session.query(OutboundMailJob)
        .filter(OutboundMailJob.status == "Failed")
        .order_by(OutboundMailJob.created_at.desc())
        .limit(limit)
        .all()
    )
    return [
        {
            "id": row.id,
            "mail_type": row.mail_type,
            "subject": row.subject,
            "related_task_id": row.related_task_id,
            "created_at": row.created_at.isoformat(),
        }
        for row in rows
    ]


def _recent_model_failures(session: Session, limit: int = 8) -> list[dict[str, Any]]:
    rows = (
        session.query(ModelCallLog)
        .filter(ModelCallLog.status == "Failed")
        .order_by(ModelCallLog.created_at.desc())
        .limit(limit)
        .all()
    )
    return [
        {
            "id": row.id,
            "task_type": row.task_type,
            "related_object_type": row.related_object_type,
            "related_object_id": row.related_object_id,
            "latency_ms": row.latency_ms,
            "error_message": row.error_message,
            "created_at": row.created_at.isoformat(),
        }
        for row in rows
    ]


def build_self_maintenance_context(session: Session) -> dict[str, Any]:
    since = now_utc() - timedelta(hours=24)
    oldest_pending_outbound = (
        session.query(OutboundMailJob)
        .filter(OutboundMailJob.status == "Pending")
        .order_by(OutboundMailJob.created_at)
        .first()
    )
    active_model = session.query(ModelProviderConfig).filter_by(status="Active").first()
    model_ready = bool(active_model and active_model.api_base.strip() and resolve_api_key(session, active_model).strip())
    return {
        "generated_at": now_utc().isoformat(),
        "runtime": {
            "model_ready": model_ready,
            "active_model": None
            if active_model is None
            else {
                "title": active_model.title,
                "provider": active_model.provider,
                "model_name": active_model.model_name,
                "api_base": active_model.api_base,
            },
            "safe_configs": _safe_configs(session),
        },
        "queues": {
            "processing_counts": _counts_by_status(session, ProcessingJob),
            "outbound_counts": _counts_by_status(session, OutboundMailJob),
            "oldest_pending_outbound_id": oldest_pending_outbound.id if oldest_pending_outbound else None,
            "oldest_pending_outbound_age_seconds": _seconds_since(oldest_pending_outbound.created_at) if oldest_pending_outbound else None,
        },
        "exceptions": {
            "open_count": session.query(ExceptionCase).filter_by(status="Open").count(),
            "created_last_24h": session.query(ExceptionCase).filter(ExceptionCase.created_at >= since).count(),
            "recent_open": _recent_exceptions(session),
        },
        "failures": {
            "processing": _recent_processing_failures(session),
            "outbound": _recent_outbound_failures(session),
            "model": _recent_model_failures(session),
        },
        "activity": {
            "audit_events_last_24h": session.query(AuditEvent).filter(AuditEvent.created_at >= since).count(),
            "model_calls_last_24h": session.query(ModelCallLog).filter(ModelCallLog.created_at >= since).count(),
            "model_failures_last_24h": session.query(ModelCallLog).filter(ModelCallLog.created_at >= since, ModelCallLog.status == "Failed").count(),
        },
    }


def assess_risk_level(context: dict[str, Any]) -> str:
    processing_failed = int(context["queues"]["processing_counts"].get("Failed", 0) or 0)
    outbound_failed = int(context["queues"]["outbound_counts"].get("Failed", 0) or 0)
    pending_age = int(context["queues"].get("oldest_pending_outbound_age_seconds") or 0)
    open_exceptions = int(context["exceptions"].get("open_count") or 0)
    if processing_failed > 0 or outbound_failed >= 5 or pending_age >= 24 * 3600:
        return "High"
    if outbound_failed > 0 or open_exceptions > 0 or pending_age >= 3600:
        return "Medium"
    return "Low"


def _config_bool(configs: dict[str, str], key: str, default: bool = False) -> bool:
    raw = configs.get(key)
    if raw in (None, ""):
        return default
    return str(raw).strip().lower() in {"1", "true", "yes", "on"}


def _config_int(configs: dict[str, str], key: str, default: int) -> int:
    try:
        return int(str(configs.get(key, default)).strip())
    except (TypeError, ValueError):
        return default


def _config_patch_action(title: str, detail: str, changes: list[dict[str, str]], *, risk: str = "Low") -> dict[str, Any]:
    return {
        "type": "config_patch",
        "title": title,
        "risk": risk,
        "requires_approval": True,
        "detail": detail,
        "changes": changes,
    }


def propose_config_patches(context: dict[str, Any]) -> list[dict[str, Any]]:
    configs = context.get("runtime", {}).get("safe_configs", {}) or {}
    patches: list[dict[str, Any]] = []
    if context.get("runtime", {}).get("model_ready") and not _config_bool(configs, "llm_fallback_enabled", True):
        patches.append(
            _config_patch_action(
                "启用 LLM 兜底识别",
                "当前模型已就绪，但 LLM 兜底识别关闭；非标准销售邮件更容易进入异常队列。",
                [
                    {
                        "key": "llm_fallback_enabled",
                        "before": str(configs.get("llm_fallback_enabled", "")),
                        "after": "true",
                        "reason": "模型可用时启用兜底识别，降低 NonTarget 误判和缺字段异常。",
                    }
                ],
            )
        )
    conversation_rounds = _config_int(configs, "conversation_max_rounds", 3)
    if conversation_rounds < 2:
        patches.append(
            _config_patch_action(
                "提高订单沟通最大轮数",
                "最大沟通轮数过低，销售补充信息还未闭环就可能提前关闭会话。",
                [
                    {
                        "key": "conversation_max_rounds",
                        "before": str(conversation_rounds),
                        "after": "3",
                        "reason": "保持 MVP 默认 3 轮，兼顾自动闭环和人工介入。",
                    }
                ],
            )
        )
    failed_alert_threshold = _config_int(configs, "outbound_failed_alert_threshold", 1)
    if failed_alert_threshold > 5:
        patches.append(
            _config_patch_action(
                "降低外发失败告警阈值",
                "外发失败告警阈值偏高，SMTP 或收件人异常可能被延迟发现。",
                [
                    {
                        "key": "outbound_failed_alert_threshold",
                        "before": str(failed_alert_threshold),
                        "after": "1",
                        "reason": "生产邮件链路应在首次失败后就提醒运维排查。",
                    }
                ],
            )
        )
    pending_alert_seconds = _config_int(configs, "outbound_pending_age_alert_seconds", 3600)
    if pending_alert_seconds > 7200:
        patches.append(
            _config_patch_action(
                "缩短外发 Pending 告警时间",
                "Pending 超时告警时间超过 2 小时，任务单或销售回执积压会较晚暴露。",
                [
                    {
                        "key": "outbound_pending_age_alert_seconds",
                        "before": str(pending_alert_seconds),
                        "after": "3600",
                        "reason": "恢复 1 小时告警窗口，便于及时发现外发队列阻塞。",
                    }
                ],
                risk="Medium",
            )
        )
    auto_worker_interval = _config_int(configs, "mail_auto_worker_interval_seconds", 60)
    pending_age = int(context.get("queues", {}).get("oldest_pending_outbound_age_seconds") or 0)
    if pending_age >= 3600 and auto_worker_interval > 300:
        patches.append(
            _config_patch_action(
                "缩短邮件 worker 周期",
                "外发或入库队列已有积压，当前 worker 周期偏长。",
                [
                    {
                        "key": "mail_auto_worker_interval_seconds",
                        "before": str(auto_worker_interval),
                        "after": "300",
                        "reason": "队列积压时将自动 worker 周期收敛到 5 分钟。",
                    }
                ],
                risk="Medium",
            )
        )
    return patches


def propose_actions(context: dict[str, Any]) -> list[dict[str, Any]]:
    actions: list[dict[str, Any]] = []
    queues = context["queues"]
    runtime = context["runtime"]
    actions.extend(propose_config_patches(context))
    if not runtime.get("model_ready"):
        actions.append(
            {
                "type": "configuration_check",
                "title": "检查模型 Provider 配置",
                "risk": "Low",
                "requires_approval": True,
                "detail": "当前模型未就绪，LLM 兜底分类、流程助手和自维护诊断都可能无法工作。",
            }
        )
    if int(queues["processing_counts"].get("Failed", 0) or 0) > 0:
        actions.append(
            {
                "type": "operator_review",
                "title": "复核失败入库任务",
                "risk": "Medium",
                "requires_approval": False,
                "detail": "存在 Failed 入库任务，优先查看 error_message、来源邮件和附件解析结果。",
            }
        )
    if int(queues["outbound_counts"].get("Failed", 0) or 0) > 0:
        actions.append(
            {
                "type": "operator_review",
                "title": "复核失败外发邮件",
                "risk": "Medium",
                "requires_approval": False,
                "detail": "存在 Failed 外发任务，先确认 SMTP 配置、账号密码、限频和收件人地址。",
            }
        )
    pending_age = int(queues.get("oldest_pending_outbound_age_seconds") or 0)
    if pending_age >= 3600:
        actions.append(
            {
                "type": "operator_review",
                "title": "处理积压外发队列",
                "risk": "Medium",
                "requires_approval": False,
                "detail": "最早 Pending 外发已超过 1 小时，区分自动可发邮件和需要商务确认的人工邮件。",
            }
        )
    if int(context["exceptions"].get("open_count") or 0) > 0:
        actions.append(
            {
                "type": "operator_review",
                "title": "处理开放异常",
                "risk": "Low",
                "requires_approval": False,
                "detail": "异常队列中仍有 Open 记录，优先处理缺字段、路由缺失和附件解析失败。",
            }
        )
    if not actions:
        actions.append(
            {
                "type": "monitoring",
                "title": "保持观察",
                "risk": "Low",
                "requires_approval": False,
                "detail": "未发现明显阻塞项，可继续观察队列水位、模型调用失败率和异常新增量。",
            }
        )
    return actions


def fallback_diagnosis(user_message: str, context: dict[str, Any], actions: list[dict[str, Any]]) -> str:
    risk = assess_risk_level(context)
    queues = context["queues"]
    exceptions = context["exceptions"]
    lines = [
        f"## 诊断结论",
        f"- 风险等级：{risk}",
        f"- 处理队列状态：{queues['processing_counts'] or {}}",
        f"- 外发队列状态：{queues['outbound_counts'] or {}}",
        f"- 开放异常数量：{exceptions.get('open_count', 0)}",
        "",
        "## 证据",
        f"- 最早 Pending 外发：{queues.get('oldest_pending_outbound_id') or '无'}，等待 {queues.get('oldest_pending_outbound_age_seconds') or 0} 秒",
        f"- 最近 24 小时模型失败：{context['activity'].get('model_failures_last_24h', 0)} 次",
        f"- 管理员问题：{user_message}",
        "",
        "## 建议动作",
    ]
    lines.extend([f"- {item['title']}：{item['detail']}" for item in actions])
    return "\n".join(lines)


def _llm_diagnosis(session: Session, user_message: str, context: dict[str, Any], actions: list[dict[str, Any]]) -> str:
    config = session.query(ModelProviderConfig).filter_by(status="Active").first()
    if config is None:
        return ""
    prompt = (
        "你是商务生产任务单智能体的自维护诊断助手。"
        "只能诊断、解释证据、提出需要人工确认的建议，不得声称已经执行修复。"
        "请用 Markdown 输出：诊断结论、证据、建议动作、风险和需人工确认事项。"
    )
    payload = {
        "user_message": user_message,
        "system_context": context,
        "proposed_actions": actions,
    }
    output = call_model(
        session,
        config,
        task_type="SelfMaintenanceDiagnosis",
        messages=[
            {"role": "system", "content": prompt},
            {"role": "user", "content": dumps(payload)},
        ],
        related_object_type="MaintenanceSession",
    )
    return extract_chat_content(output).strip()


def _suggest_code_areas(message: str) -> list[dict[str, Any]]:
    lowered = message.lower()
    selected = []
    for area in CODEBASE_AREAS:
        if any(keyword.lower() in lowered for keyword in area["when"]):
            selected.append({"area": area["area"], "files": area["files"]})
    if not selected:
        selected = [
            {"area": "API orchestration", "files": ["backend/app/main.py", "backend/app/schemas.py"]},
            {"area": "Business service layer", "files": ["backend/app/services/workflow.py", "tests/test_workflow.py"]},
        ]
    return selected


def fallback_code_plan(user_message: str, context: dict[str, Any], source_session: MaintenanceSession | None = None) -> dict[str, Any]:
    suggested_areas = _suggest_code_areas(user_message)
    files = []
    for area in suggested_areas:
        for file_name in area["files"]:
            if file_name not in files:
                files.append(file_name)
    problem_basis = [
        f"用户问题：{user_message}",
        f"开放异常：{context.get('exceptions', {}).get('open_count', 0)}",
        f"处理队列状态：{context.get('queues', {}).get('processing_counts', {})}",
        f"外发队列状态：{context.get('queues', {}).get('outbound_counts', {})}",
    ]
    if source_session is not None:
        problem_basis.append(f"来源诊断会话：{source_session.id}，风险等级：{source_session.risk_level}")
    plan_md = "\n".join(
        [
            "## 修复草案",
            "- 先复现问题并锁定失败状态、异常记录或页面行为。",
            "- 在服务层实现最小修复，API 层只做参数校验和编排。",
            "- 为新增或修复行为补充聚焦测试，避免真实邮箱、真实模型或真实发信依赖。",
            "- 运行编译、后端测试和前端脚本语法检查后再进入人工评审。",
            "",
            "## 建议改动范围",
            *[f"- {area['area']}：{', '.join(area['files'])}" for area in suggested_areas],
            "",
            "## 验证命令",
            *[f"- `{command}`" for command in VALIDATION_COMMANDS],
            "",
            "## 风险边界",
            "- 不自动发送真实邮件。",
            "- 不回显或写入真实密钥。",
            "- 不自动清理有效订单邮件。",
            "- 不在业务进程内直接改代码或部署。",
        ]
    )
    return {
        "type": "code_patch_plan",
        "title": "代码修复草案",
        "risk": "Medium",
        "requires_approval": True,
        "detail": "该草案用于人工或外部维护 runner 实施，不会在当前业务进程内直接修改代码。",
        "problem_basis": problem_basis,
        "suggested_areas": suggested_areas,
        "suggested_files": files,
        "validation_commands": VALIDATION_COMMANDS,
        "plan_md": plan_md,
    }


def _llm_code_plan(
    session: Session,
    user_message: str,
    context: dict[str, Any],
    fallback: dict[str, Any],
    source_session: MaintenanceSession | None,
) -> dict[str, Any]:
    config = session.query(ModelProviderConfig).filter_by(status="Active").first()
    if config is None:
        return fallback
    prompt = (
        "你是该 Python/FastAPI 商务生产任务单项目的维护架构助手。"
        "请只生成代码修复草案，不要声称已经修改代码。"
        "输出必须是 JSON 对象，字段包括：title, risk, detail, problem_basis, suggested_areas, suggested_files, validation_commands, plan_md。"
        "suggested_files 必须只使用仓库内相对路径。validation_commands 必须包含 compileall 和 pytest。"
    )
    payload = {
        "user_message": user_message,
        "system_context": context,
        "source_session": serialize_maintenance_session(source_session, include_context=False) if source_session is not None else None,
        "fallback_plan": fallback,
        "codebase_areas": CODEBASE_AREAS,
    }
    output = call_model(
        session,
        config,
        task_type="SelfMaintenanceCodePlan",
        messages=[
            {"role": "system", "content": prompt},
            {"role": "user", "content": dumps(payload)},
        ],
        related_object_type="MaintenanceSession",
        related_object_id=source_session.id if source_session is not None else None,
    )
    text = extract_chat_content(output).strip()
    parsed = loads(text, {})
    if not isinstance(parsed, dict):
        return fallback
    plan = {**fallback, **parsed}
    plan["type"] = "code_patch_plan"
    plan["requires_approval"] = True
    if not isinstance(plan.get("suggested_files"), list) or not plan["suggested_files"]:
        plan["suggested_files"] = fallback["suggested_files"]
    if not isinstance(plan.get("validation_commands"), list) or not plan["validation_commands"]:
        plan["validation_commands"] = fallback["validation_commands"]
    if not str(plan.get("plan_md") or "").strip():
        plan["plan_md"] = fallback["plan_md"]
    return plan


def create_maintenance_diagnosis(
    session: Session,
    *,
    user_message: str,
    actor: str = "system",
    use_llm: bool = True,
) -> MaintenanceSession:
    message = user_message.strip()
    if not message:
        raise ValueError("message is required")
    context = build_self_maintenance_context(session)
    actions = propose_actions(context)
    risk_level = assess_risk_level(context)
    diagnosis = ""
    if use_llm:
        try:
            diagnosis = _llm_diagnosis(session, message, context, actions)
        except Exception as exc:
            diagnosis = fallback_diagnosis(message, context, actions)
            actions.insert(
                0,
                {
                    "type": "llm_failure",
                    "title": "模型诊断调用失败",
                    "risk": "Low",
                    "requires_approval": False,
                    "detail": str(exc),
                },
            )
    if not diagnosis:
        diagnosis = fallback_diagnosis(message, context, actions)
    maintenance_session = MaintenanceSession(
        user_message=message,
        status="Completed",
        risk_level=risk_level,
        collected_context_json=dumps(context),
        diagnosis_md=diagnosis,
        proposed_actions_json=dumps(actions),
        created_by=actor,
    )
    session.add(maintenance_session)
    session.flush()
    action_rows = []
    for action in actions:
        row = MaintenanceAction(
            session_id=maintenance_session.id,
            action_type=str(action.get("type") or "operator_review"),
            status="Proposed",
            input_json=dumps(action),
        )
        session.add(row)
        action_rows.append((action, row))
    session.flush()
    for action, row in action_rows:
        action["action_id"] = row.id
        action["action_status"] = row.status
    maintenance_session.proposed_actions_json = dumps(actions)
    session.flush()
    return maintenance_session


def archive_maintenance_session(
    session: Session,
    session_id: str,
    *,
    note: str = "",
    actor: str = "system",
) -> MaintenanceSession:
    maintenance_session = session.get(MaintenanceSession, session_id)
    if maintenance_session is None:
        raise ValueError("maintenance session not found")
    if maintenance_session.status == "Archived":
        raise ValueError("maintenance session is already archived")
    maintenance_session.status = "Archived"
    session.add(
        AuditEvent(
            event_type="SelfMaintenanceSessionArchived",
            actor=actor,
            related_object_type="MaintenanceSession",
            related_object_id=maintenance_session.id,
            detail=dumps({"note": note.strip()}),
            created_at=now_utc(),
        )
    )
    session.flush()
    return maintenance_session


def create_code_patch_plan(
    session: Session,
    *,
    user_message: str,
    actor: str = "system",
    source_session_id: str | None = None,
    use_llm: bool = True,
) -> MaintenanceSession:
    message = user_message.strip()
    if not message:
        raise ValueError("message is required")
    source_session = session.get(MaintenanceSession, source_session_id) if source_session_id else None
    if source_session_id and source_session is None:
        raise ValueError("source maintenance session not found")
    context = build_self_maintenance_context(session)
    fallback = fallback_code_plan(message, context, source_session)
    plan = fallback
    if use_llm:
        try:
            plan = _llm_code_plan(session, message, context, fallback, source_session)
        except Exception as exc:
            plan = {
                **fallback,
                "llm_error": str(exc),
                "detail": f"{fallback['detail']} 模型生成失败，已使用本地草案。",
            }
    risk_level = str(plan.get("risk") or "Medium")
    maintenance_session = MaintenanceSession(
        user_message=message,
        status="Planned",
        risk_level=risk_level,
        collected_context_json=dumps(context),
        diagnosis_md=str(plan.get("plan_md") or ""),
        proposed_actions_json=dumps([]),
        created_by=actor,
    )
    session.add(maintenance_session)
    session.flush()
    action = MaintenanceAction(
        session_id=maintenance_session.id,
        action_type="code_patch_plan",
        status="Proposed",
        input_json=dumps(plan),
    )
    session.add(action)
    session.flush()
    plan["action_id"] = action.id
    plan["action_status"] = action.status
    maintenance_session.proposed_actions_json = dumps([plan])
    session.flush()
    return maintenance_session


def code_plan_action_payload(action: MaintenanceAction) -> dict[str, Any]:
    payload = loads(action.input_json, {})
    if not isinstance(payload, dict):
        raise ValueError("code patch plan payload is invalid")
    return payload


def maintenance_runner_commands(action_id: str) -> list[str]:
    return [
        f"python3 scripts/maintenance_runner.py handoff --action-id {action_id}",
        f"python3 scripts/maintenance_runner.py validate --action-id {action_id}",
        f'python3 scripts/maintenance_runner.py complete --action-id {action_id} --summary "补丁已完成，等待人工复核"',
        f'python3 scripts/maintenance_runner.py review --action-id {action_id} --decision ReviewAccepted --note "人工复核通过"',
    ]


def text_tail(value: str, limit: int = 4000) -> str:
    if len(value) <= limit:
        return value
    return value[-limit:]


def run_maintenance_validation_command(command: str, cwd: Path, timeout_seconds: int) -> dict[str, Any]:
    completed = subprocess.run(
        shlex.split(command),
        cwd=str(cwd),
        capture_output=True,
        text=True,
        timeout=timeout_seconds,
        check=False,
    )
    return {
        "command": command,
        "exit_code": completed.returncode,
        "stdout_tail": text_tail(completed.stdout or ""),
        "stderr_tail": text_tail(completed.stderr or ""),
    }


def allowed_maintenance_validation_commands(
    plan: dict[str, Any],
    selected_commands: list[str] | None = None,
) -> list[str]:
    raw_commands = selected_commands or plan.get("validation_commands") or VALIDATION_COMMANDS
    if not isinstance(raw_commands, list) or not raw_commands:
        raise ValueError("validation commands are required")
    allowed = set(VALIDATION_COMMANDS)
    commands = []
    for command in raw_commands:
        command_text = str(command).strip()
        if command_text not in allowed:
            raise ValueError(f"validation command is not allowed: {command_text}")
        if command_text not in commands:
            commands.append(command_text)
    return commands


def render_maintenance_code_plan_report(action: MaintenanceAction, *, output_dir: Path = MAINTENANCE_OUTPUT_DIR) -> Path:
    if action.action_type != "code_patch_plan":
        raise ValueError("only code_patch_plan actions can be exported")
    plan = code_plan_action_payload(action)
    output_dir.mkdir(parents=True, exist_ok=True)
    target = output_dir / f"maintenance-action-{action.id}.md"
    lines = [
        f"# Maintenance Code Plan {action.id}",
        "",
        f"- Status: {action.status}",
        f"- Created at: {action.created_at.isoformat()}",
        f"- Risk: {plan.get('risk', 'Medium')}",
        f"- Title: {plan.get('title', '代码修复草案')}",
        "",
        "## Detail",
        str(plan.get("detail") or ""),
        "",
        "## Suggested Files",
        *[f"- {item}" for item in plan.get("suggested_files", [])],
        "",
        "## Validation Commands",
        *[f"- `{item}`" for item in plan.get("validation_commands", [])],
        "",
        "## Runner Commands",
        *[f"- `{item}`" for item in maintenance_runner_commands(action.id)],
        "",
        "## Plan",
        str(plan.get("plan_md") or ""),
    ]
    target.write_text("\n".join(lines).strip() + "\n", encoding="utf-8")
    return target


def run_maintenance_validation(
    session: Session,
    action_id: str,
    *,
    selected_commands: list[str] | None = None,
    timeout_seconds: int = 300,
    output_dir: Path = MAINTENANCE_OUTPUT_DIR,
    cwd: Path = Path("."),
    actor: str = "system",
    command_runner=run_maintenance_validation_command,
) -> MaintenanceAction:
    action = session.get(MaintenanceAction, action_id)
    if action is None:
        raise ValueError("maintenance action not found")
    if action.action_type != "code_patch_plan":
        raise ValueError("only code_patch_plan actions can run validation")
    if action.status in {"ReviewAccepted", "ReviewRejected"}:
        raise ValueError(f"maintenance action cannot run validation in status: {action.status}")
    plan = code_plan_action_payload(action)
    commands = allowed_maintenance_validation_commands(plan, selected_commands)
    report_path = render_maintenance_code_plan_report(action, output_dir=output_dir)
    results = []
    for command in commands:
        result = command_runner(command, cwd, timeout_seconds)
        if hasattr(result, "as_dict"):
            result = result.as_dict()
        results.append(dict(result))
    failed = [result for result in results if int(result.get("exit_code") or 0) != 0]
    validation = {
        "report_path": str(report_path),
        "commands": results,
        "validated_by": actor,
        "validated_at": now_utc().isoformat(),
    }
    previous_result = loads(action.result_json, {})
    if not isinstance(previous_result, dict):
        previous_result = {}
    action.status = "ValidationFailed" if failed else "Validated"
    action.result_json = dumps({**previous_result, "validation": validation, "report_path": str(report_path), "commands": results})
    action.error_message = "; ".join(f"{result.get('command')} exited {result.get('exit_code')}" for result in failed) or None
    maintenance_session = session.get(MaintenanceSession, action.session_id)
    if maintenance_session is not None:
        proposed_actions = loads(maintenance_session.proposed_actions_json, [])
        if isinstance(proposed_actions, list):
            for item in proposed_actions:
                if isinstance(item, dict) and item.get("action_id") == action.id:
                    item["action_status"] = action.status
                    item["validation_result"] = validation
                    item["validation_error"] = action.error_message
            maintenance_session.proposed_actions_json = dumps(proposed_actions)
    session.flush()
    return action


def build_maintenance_handoff_payload(
    action: MaintenanceAction,
    maintenance_session: MaintenanceSession | None = None,
) -> dict[str, Any]:
    plan = code_plan_action_payload(action)
    context = loads(maintenance_session.collected_context_json, {}) if maintenance_session is not None else {}
    return {
        "action": {
            "id": action.id,
            "session_id": action.session_id,
            "status": action.status,
            "created_at": action.created_at.isoformat(),
        },
        "plan": plan,
        "context": context,
        "runner_commands": maintenance_runner_commands(action.id),
        "safety_boundaries": [
            "Do not send real email during implementation or validation.",
            "Do not print, write, or commit runtime secrets.",
            "Do not delete valid order emails or attachment data.",
            "Do not deploy automatically; produce a patch and validation report for review.",
        ],
        "recommended_workflow": [
            "Create or switch to a dedicated git branch.",
            "Inspect the suggested files and reproduce the reported behavior where possible.",
            "Implement the smallest scoped patch.",
            "Run the validation commands from this handoff package.",
            "Attach the command results and residual risks to the review note.",
        ],
    }


def create_maintenance_handoff_package(
    session: Session,
    action_id: str,
    *,
    actor: str = "system",
    output_dir: Path = MAINTENANCE_OUTPUT_DIR,
) -> MaintenanceAction:
    action = session.get(MaintenanceAction, action_id)
    if action is None:
        raise ValueError("maintenance action not found")
    if action.action_type != "code_patch_plan":
        raise ValueError("only code_patch_plan actions can create handoff packages")
    if action.status in {"PatchReady", "PatchFailed", "ReviewAccepted", "ReviewRejected", "NeedsRevision"}:
        raise ValueError(f"maintenance action cannot create handoff in status: {action.status}")
    maintenance_session = session.get(MaintenanceSession, action.session_id)
    output_dir.mkdir(parents=True, exist_ok=True)
    payload = build_maintenance_handoff_payload(action, maintenance_session)
    json_path = output_dir / f"maintenance-action-{action.id}.json"
    markdown_path = render_maintenance_code_plan_report(action, output_dir=output_dir)
    handoff = {
        "json_path": str(json_path),
        "markdown_path": str(markdown_path),
        "created_by": actor,
        "created_at": now_utc().isoformat(),
        "runner_commands": maintenance_runner_commands(action.id),
    }
    json_path.write_text(dumps(payload), encoding="utf-8")
    previous_result = loads(action.result_json, {})
    if not isinstance(previous_result, dict):
        previous_result = {}
    action.status = "HandoffReady"
    action.result_json = dumps({**previous_result, "handoff": handoff})
    action.error_message = None
    if maintenance_session is not None:
        proposed_actions = loads(maintenance_session.proposed_actions_json, [])
        if isinstance(proposed_actions, list):
            for item in proposed_actions:
                if isinstance(item, dict) and item.get("action_id") == action.id:
                    item["action_status"] = action.status
                    item["handoff"] = handoff
            maintenance_session.proposed_actions_json = dumps(proposed_actions)
    session.flush()
    return action


def _resolve_handoff_file(path_value: str, *, expected_name: str, output_dir: Path) -> Path:
    base = output_dir if output_dir.is_absolute() else Path.cwd() / output_dir
    base = base.resolve()
    candidate = Path(path_value) if path_value else output_dir / expected_name
    candidate = candidate if candidate.is_absolute() else Path.cwd() / candidate
    candidate = candidate.resolve()
    if candidate.name != expected_name:
        raise ValueError("handoff file name does not match maintenance action")
    try:
        candidate.relative_to(base)
    except ValueError as exc:
        raise ValueError("handoff file is outside maintenance output directory") from exc
    return candidate


def read_maintenance_handoff_package(
    session: Session,
    action_id: str,
    *,
    output_dir: Path = MAINTENANCE_OUTPUT_DIR,
) -> dict[str, Any]:
    action = session.get(MaintenanceAction, action_id)
    if action is None:
        raise ValueError("maintenance action not found")
    if action.action_type != "code_patch_plan":
        raise ValueError("only code_patch_plan actions can have handoff packages")
    result = loads(action.result_json, {})
    handoff = result.get("handoff") if isinstance(result, dict) else None
    if not isinstance(handoff, dict):
        raise ValueError("handoff package has not been created")
    markdown_name = f"maintenance-action-{action.id}.md"
    json_name = f"maintenance-action-{action.id}.json"
    markdown_path = _resolve_handoff_file(str(handoff.get("markdown_path") or ""), expected_name=markdown_name, output_dir=output_dir)
    json_path = _resolve_handoff_file(str(handoff.get("json_path") or ""), expected_name=json_name, output_dir=output_dir)
    markdown_content = markdown_path.read_text(encoding="utf-8") if markdown_path.exists() else ""
    json_content = json_path.read_text(encoding="utf-8") if json_path.exists() else ""
    parsed_json = loads(json_content, {}) if json_content else {}
    return {
        "action_id": action.id,
        "status": action.status,
        "handoff": handoff,
        "markdown": {
            "path": str(markdown_path),
            "exists": markdown_path.exists(),
            "content": markdown_content,
        },
        "json": {
            "path": str(json_path),
            "exists": json_path.exists(),
            "content": parsed_json,
            "raw": json_content,
        },
    }


def _timeline_item(
    *,
    event_type: str,
    title: str,
    status: str,
    created_at: str,
    detail: Any = None,
    action_id: str | None = None,
    source: str = "derived",
) -> dict[str, Any]:
    return {
        "event_type": event_type,
        "title": title,
        "status": status,
        "created_at": created_at,
        "detail": detail or {},
        "action_id": action_id,
        "source": source,
    }


def maintenance_session_timeline(session: Session, session_id: str) -> dict[str, Any]:
    maintenance_session = session.get(MaintenanceSession, session_id)
    if maintenance_session is None:
        raise ValueError("maintenance session not found")
    actions = (
        session.query(MaintenanceAction)
        .filter_by(session_id=session_id)
        .order_by(MaintenanceAction.created_at)
        .all()
    )
    action_ids = [row.id for row in actions]
    object_ids = [session_id, *action_ids]
    audit_rows = (
        session.query(AuditEvent)
        .filter(AuditEvent.related_object_id.in_(object_ids))
        .order_by(AuditEvent.created_at)
        .all()
        if object_ids
        else []
    )
    timeline = [
        _timeline_item(
            event_type="MaintenanceSessionCreated",
            title="维护会话创建",
            status=maintenance_session.status,
            created_at=maintenance_session.created_at.isoformat(),
            detail={"message": maintenance_session.user_message, "risk_level": maintenance_session.risk_level},
            source="session",
        )
    ]
    for action in actions:
        input_payload = loads(action.input_json, {})
        result_payload = loads(action.result_json, {})
        action_title = str(input_payload.get("title") or MAINTENANCE_ACTION_LABELS.get(action.action_type, action.action_type))
        timeline.append(
            _timeline_item(
                event_type="MaintenanceActionProposed",
                title=action_title,
                status=action.status,
                created_at=action.created_at.isoformat(),
                detail={"action_type": action.action_type, "detail": input_payload.get("detail", "")},
                action_id=action.id,
                source="action",
            )
        )
        if isinstance(result_payload, dict) and result_payload.get("handoff"):
            handoff = result_payload["handoff"]
            if isinstance(handoff, dict) and handoff.get("created_at"):
                timeline.append(
                    _timeline_item(
                        event_type="MaintenanceHandoffCreated",
                        title="交接包已生成",
                        status=action.status,
                        created_at=str(handoff["created_at"]),
                        detail={
                            "markdown_path": handoff.get("markdown_path", ""),
                            "json_path": handoff.get("json_path", ""),
                        },
                        action_id=action.id,
                        source="result",
                    )
                )
        if isinstance(result_payload, dict) and result_payload.get("validation"):
            validation = result_payload["validation"]
            if isinstance(validation, dict) and validation.get("validated_at"):
                timeline.append(
                    _timeline_item(
                        event_type="MaintenanceValidationCompleted",
                        title="验证已完成",
                        status=action.status,
                        created_at=str(validation["validated_at"]),
                        detail={
                            "report_path": validation.get("report_path", ""),
                            "commands": validation.get("commands", []),
                        },
                        action_id=action.id,
                        source="result",
                    )
                )
        if isinstance(result_payload, dict) and result_payload.get("implementation"):
            implementation = result_payload["implementation"]
            if isinstance(implementation, dict) and implementation.get("reported_at"):
                timeline.append(
                    _timeline_item(
                        event_type="MaintenanceImplementationReported",
                        title="实现结果已回填",
                        status=str(implementation.get("status") or action.status),
                        created_at=str(implementation["reported_at"]),
                        detail={
                            "summary": implementation.get("summary", ""),
                            "changed_files": implementation.get("changed_files", []),
                            "tests": implementation.get("tests", []),
                            "residual_risks": implementation.get("residual_risks", []),
                        },
                        action_id=action.id,
                        source="result",
                    )
                )
        if isinstance(result_payload, dict) and result_payload.get("review"):
            review = result_payload["review"]
            if isinstance(review, dict) and review.get("reviewed_at"):
                timeline.append(
                    _timeline_item(
                        event_type="MaintenanceImplementationReviewed",
                        title="人工复核已记录",
                        status=str(review.get("decision") or action.status),
                        created_at=str(review["reviewed_at"]),
                        detail={"note": review.get("note", ""), "reviewed_by": review.get("reviewed_by", "")},
                        action_id=action.id,
                        source="result",
                    )
                )
        if isinstance(result_payload, dict) and result_payload.get("applied"):
            timeline.append(
                _timeline_item(
                    event_type="MaintenanceConfigApplied",
                    title="配置修复已应用",
                    status=action.status,
                    created_at=(action.approved_at or action.created_at).isoformat(),
                    detail={"applied": result_payload.get("applied", [])},
                    action_id=action.id,
                    source="result",
                )
            )
    for audit in audit_rows:
        timeline.append(
            _timeline_item(
                event_type=audit.event_type,
                title=MAINTENANCE_AUDIT_LABELS.get(audit.event_type, audit.event_type),
                status=audit.event_type,
                created_at=audit.created_at.isoformat(),
                detail=loads(audit.detail, {}),
                action_id=audit.related_object_id if audit.related_object_id in action_ids else None,
                source="audit",
            )
        )
    timeline.sort(key=lambda item: item["created_at"])
    return {
        "session": serialize_maintenance_session(maintenance_session, include_context=False),
        "actions": [serialize_maintenance_action(action) for action in actions],
        "timeline": timeline,
    }


def _validate_config_change(change: dict[str, Any]) -> tuple[str, str]:
    key = str(change.get("key") or "").strip()
    if key not in CONFIG_PATCH_ALLOWED_KEYS:
        raise ValueError(f"unsupported config patch key: {key}")
    value = str(change.get("after") or "").strip()
    if value == "":
        raise ValueError(f"config patch value is required: {key}")
    if key == "llm_fallback_enabled":
        if value.lower() not in {"true", "false"}:
            raise ValueError("llm_fallback_enabled must be true or false")
        return key, value.lower()
    if key in CONFIG_PATCH_LIMITS:
        minimum, maximum = CONFIG_PATCH_LIMITS[key]
        try:
            number = int(value)
        except ValueError as exc:
            raise ValueError(f"{key} must be an integer") from exc
        if number < minimum or number > maximum:
            raise ValueError(f"{key} must be between {minimum} and {maximum}")
        return key, str(number)
    return key, value


def apply_maintenance_action(session: Session, action_id: str, *, actor: str = "system") -> MaintenanceAction:
    action = session.get(MaintenanceAction, action_id)
    if action is None:
        raise ValueError("maintenance action not found")
    if action.status != "Proposed":
        raise ValueError(f"maintenance action is not applicable in status: {action.status}")
    payload = loads(action.input_json, {})
    if action.action_type != "config_patch" or payload.get("type") != "config_patch":
        raise ValueError("only config_patch maintenance actions can be applied")
    changes = payload.get("changes")
    if not isinstance(changes, list) or not changes:
        raise ValueError("config patch changes are required")
    applied = []
    for change in changes:
        if not isinstance(change, dict):
            raise ValueError("config patch change must be an object")
        key, value = _validate_config_change(change)
        before_row = session.get(SystemConfig, key)
        before = before_row.value if before_row is not None else ""
        set_config(session, key, value, is_secret=False)
        applied.append(
            {
                "key": key,
                "before": before,
                "after": value,
                "reason": change.get("reason", ""),
            }
        )
    action.status = "Completed"
    action.approved_by = actor
    action.approved_at = now_utc()
    action.result_json = dumps({"applied": applied})
    action.error_message = None
    maintenance_session = session.get(MaintenanceSession, action.session_id)
    if maintenance_session is not None:
        proposed_actions = loads(maintenance_session.proposed_actions_json, [])
        if isinstance(proposed_actions, list):
            for item in proposed_actions:
                if isinstance(item, dict) and item.get("action_id") == action.id:
                    item["action_status"] = action.status
                    item["applied_result"] = {"applied": applied}
            maintenance_session.proposed_actions_json = dumps(proposed_actions)
    session.flush()
    return action


def report_maintenance_implementation(
    session: Session,
    action_id: str,
    *,
    status: str,
    summary: str,
    changed_files: list[str] | None = None,
    tests: list[str] | None = None,
    residual_risks: list[str] | None = None,
    actor: str = "system",
) -> MaintenanceAction:
    action = session.get(MaintenanceAction, action_id)
    if action is None:
        raise ValueError("maintenance action not found")
    if action.action_type != "code_patch_plan":
        raise ValueError("only code_patch_plan actions can receive implementation reports")
    if status not in {"PatchReady", "PatchFailed"}:
        raise ValueError("unsupported implementation status")
    summary = summary.strip()
    if not summary:
        raise ValueError("summary is required")
    implementation = {
        "status": status,
        "summary": summary,
        "changed_files": [str(item).strip() for item in (changed_files or []) if str(item).strip()],
        "tests": [str(item).strip() for item in (tests or []) if str(item).strip()],
        "residual_risks": [str(item).strip() for item in (residual_risks or []) if str(item).strip()],
        "reported_by": actor,
        "reported_at": now_utc().isoformat(),
    }
    previous_result = loads(action.result_json, {})
    if not isinstance(previous_result, dict):
        previous_result = {}
    action.status = status
    action.result_json = dumps({**previous_result, "implementation": implementation})
    action.error_message = summary if status == "PatchFailed" else None
    maintenance_session = session.get(MaintenanceSession, action.session_id)
    if maintenance_session is not None:
        proposed_actions = loads(maintenance_session.proposed_actions_json, [])
        if isinstance(proposed_actions, list):
            for item in proposed_actions:
                if isinstance(item, dict) and item.get("action_id") == action.id:
                    item["action_status"] = action.status
                    item["implementation"] = implementation
            maintenance_session.proposed_actions_json = dumps(proposed_actions)
    session.flush()
    return action


def review_maintenance_implementation(
    session: Session,
    action_id: str,
    *,
    decision: str,
    note: str = "",
    actor: str = "system",
) -> MaintenanceAction:
    action = session.get(MaintenanceAction, action_id)
    if action is None:
        raise ValueError("maintenance action not found")
    if action.action_type != "code_patch_plan":
        raise ValueError("only code_patch_plan actions can be reviewed")
    if action.status not in {"PatchReady", "PatchFailed"}:
        raise ValueError(f"maintenance action is not reviewable in status: {action.status}")
    if decision not in {"ReviewAccepted", "ReviewRejected", "NeedsRevision"}:
        raise ValueError("unsupported review decision")
    previous_result = loads(action.result_json, {})
    if not isinstance(previous_result, dict):
        previous_result = {}
    review = {
        "decision": decision,
        "note": note.strip(),
        "reviewed_by": actor,
        "reviewed_at": now_utc().isoformat(),
    }
    action.status = decision
    action.result_json = dumps({**previous_result, "review": review})
    action.error_message = review["note"] if decision in {"ReviewRejected", "NeedsRevision"} and review["note"] else None
    maintenance_session = session.get(MaintenanceSession, action.session_id)
    if maintenance_session is not None:
        proposed_actions = loads(maintenance_session.proposed_actions_json, [])
        if isinstance(proposed_actions, list):
            for item in proposed_actions:
                if isinstance(item, dict) and item.get("action_id") == action.id:
                    item["action_status"] = action.status
                    item["review"] = review
            maintenance_session.proposed_actions_json = dumps(proposed_actions)
    session.flush()
    return action


def serialize_maintenance_session(row: MaintenanceSession, include_context: bool = False) -> dict[str, Any]:
    payload = {
        "id": row.id,
        "user_message": row.user_message,
        "status": row.status,
        "risk_level": row.risk_level,
        "diagnosis_md": row.diagnosis_md,
        "proposed_actions": loads(row.proposed_actions_json, []),
        "created_by": row.created_by,
        "created_at": row.created_at.isoformat(),
    }
    if include_context:
        payload["collected_context"] = loads(row.collected_context_json, {})
    return payload


def serialize_maintenance_action(row: MaintenanceAction) -> dict[str, Any]:
    payload = {
        "id": row.id,
        "session_id": row.session_id,
        "action_type": row.action_type,
        "status": row.status,
        "input": loads(row.input_json, {}),
        "result": loads(row.result_json, {}),
        "error_message": row.error_message,
        "created_at": row.created_at.isoformat(),
        "approved_by": row.approved_by,
        "approved_at": row.approved_at.isoformat() if row.approved_at else None,
    }
    if row.action_type == "code_patch_plan":
        payload["runner_commands"] = maintenance_runner_commands(row.id)
    return payload
