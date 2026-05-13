from __future__ import annotations

from pathlib import Path
from typing import Callable

from sqlalchemy import func, or_
from sqlalchemy.orm import Session

from backend.app.models import (
    AuditEvent,
    ExceptionCase,
    MaintenanceAction,
    MaintenanceSession,
    ModelProviderConfig,
    OutboundMailJob,
    ProcessingJob,
    SystemConfig,
    now_utc,
)
from backend.app.services.bootstrap import set_config
from backend.app.services.jsonutil import dumps, loads


ALLOWED_VALIDATION_COMMANDS = {
    "python3 -m compileall backend scripts",
    "python3 -m pytest",
    "node --check backend/app/static/app.js",
}


def build_self_maintenance_context(session: Session) -> dict:
    processing_counts = dict(
        session.query(ProcessingJob.status, func.count(ProcessingJob.id))
        .group_by(ProcessingJob.status)
        .all()
    )
    outbound_counts = dict(
        session.query(OutboundMailJob.status, func.count(OutboundMailJob.id))
        .group_by(OutboundMailJob.status)
        .all()
    )
    model = session.query(ModelProviderConfig).filter_by(status="Active").first()
    model_ready = bool(model and _credential_ref_has_value(session, model.credential_ref))
    return {
        "queues": {
            "processing_counts": processing_counts,
            "outbound_counts": outbound_counts,
        },
        "exceptions": {
            "open_count": session.query(ExceptionCase).filter_by(status="Open").count(),
        },
        "runtime": {
            "model_ready": model_ready,
        },
    }


def create_maintenance_diagnosis(
    session: Session,
    *,
    user_message: str,
    actor: str = "system",
    use_llm: bool = False,
) -> MaintenanceSession:
    context = build_self_maintenance_context(session)
    risk_level = "High" if context["queues"]["processing_counts"].get("Failed") or context["exceptions"]["open_count"] else "Medium"
    row = MaintenanceSession(
        user_message=user_message,
        diagnosis_md=_diagnosis_markdown(context, use_llm=use_llm),
        risk_level=risk_level,
        status="Open",
        created_by=actor,
    )
    session.add(row)
    session.flush()
    _add_audit(session, "MaintenanceSessionCreated", "MaintenanceSession", row.id, {"risk_level": risk_level}, actor)
    _create_action(
        session,
        row,
        "operational_review",
        {
            "title": "复核失败入库任务",
            "description": "检查 Failed/Running 入库任务、异常队列和最近邮件处理日志。",
            "context": context,
        },
        actor=actor,
    )
    _create_action(
        session,
        row,
        "config_patch",
        {
            "title": "恢复外发失败告警阈值",
            "changes": [{"key": "outbound_failed_alert_threshold", "value": "1", "is_secret": False}],
        },
        actor=actor,
    )
    _sync_session_actions(session, row.id)
    return row


def create_code_patch_plan(
    session: Session,
    *,
    user_message: str,
    actor: str = "system",
    use_llm: bool = False,
) -> MaintenanceSession:
    row = MaintenanceSession(
        user_message=user_message,
        diagnosis_md="## 修复草案\n\n建议由维护 runner 生成补丁、运行白名单命令验证后再人工复核。",
        risk_level="Medium",
        status="Planned",
        created_by=actor,
    )
    session.add(row)
    session.flush()
    _add_audit(session, "MaintenanceSessionCreated", "MaintenanceSession", row.id, {"risk_level": row.risk_level}, actor)
    action = _create_action(
        session,
        row,
        "code_patch_plan",
        {
            "title": "管理台自维护页面错误提示优化",
            "risk": "Medium",
            "suggested_files": ["backend/app/static/app.js", "backend/app/static/styles.css"],
            "validation_commands": ["node --check backend/app/static/app.js", "python3 -m pytest"],
            "runner_commands": [
                "python3 scripts/maintenance_runner.py handoff",
                "python3 scripts/maintenance_runner.py validate",
            ],
            "safety_boundaries": ["不自动修改生产配置", "不发送真实邮件", "补丁需人工复核"],
        },
        actor=actor,
    )
    _add_audit(session, "SelfMaintenanceCodePlanCreated", "MaintenanceSession", row.id, {"action_id": action.id}, actor)
    _sync_session_actions(session, row.id)
    return row


def apply_maintenance_action(session: Session, action_id: str, *, actor: str = "system") -> MaintenanceAction:
    action = _get_action(session, action_id)
    payload = loads(action.input_json, {})
    if action.action_type != "config_patch":
        raise ValueError(f"maintenance action type {action.action_type} cannot be applied")
    for change in payload.get("changes", []):
        set_config(
            session,
            str(change["key"]),
            str(change.get("value", "")),
            is_secret=bool(change.get("is_secret", False)),
        )
    action.status = "Completed"
    action.approved_by = actor
    action.result_json = dumps({"applied_by": actor, "changes": payload.get("changes", [])})
    action.updated_at = now_utc()
    _add_audit(session, "MaintenanceActionApplied", "MaintenanceAction", action.id, {"action_type": action.action_type}, actor)
    _sync_session_actions(session, action.session_id)
    return action


def archive_maintenance_session(session: Session, session_id: str, *, note: str = "", actor: str = "system") -> MaintenanceSession:
    row = _get_session(session, session_id)
    row.status = "Archived"
    row.archived_at = now_utc()
    row.updated_at = now_utc()
    _add_audit(session, "SelfMaintenanceSessionArchived", "MaintenanceSession", row.id, {"note": note}, actor)
    return row


def code_plan_action_payload(action: MaintenanceAction) -> dict:
    return loads(action.input_json, {})


def render_maintenance_code_plan_report(action: MaintenanceAction, *, output_dir: Path) -> Path:
    output_dir.mkdir(parents=True, exist_ok=True)
    payload = code_plan_action_payload(action)
    path = output_dir / f"maintenance-action-{action.id}.md"
    path.write_text(_handoff_markdown(action, payload), encoding="utf-8")
    return path


def create_maintenance_handoff_package(
    session: Session,
    action_id: str,
    *,
    actor: str = "system",
    output_dir: Path = Path("data/maintenance"),
) -> MaintenanceAction:
    action = _get_action(session, action_id)
    payload = code_plan_action_payload(action)
    output_dir.mkdir(parents=True, exist_ok=True)
    markdown_path = render_maintenance_code_plan_report(action, output_dir=output_dir)
    json_path = output_dir / f"maintenance-action-{action.id}.json"
    handoff_payload = {
        "action": _serialize_action(action),
        "plan": payload,
        "runner_commands": payload.get("runner_commands", []),
        "safety_boundaries": payload.get("safety_boundaries", []),
    }
    json_path.write_text(dumps(handoff_payload), encoding="utf-8")
    handoff = {
        "created_by": actor,
        "markdown_path": str(markdown_path),
        "json_path": str(json_path),
        "runner_commands": payload.get("runner_commands", []),
    }
    action.status = "HandoffReady"
    action.result_json = dumps({**loads(action.result_json, {}), "handoff": handoff})
    action.updated_at = now_utc()
    _add_audit(session, "MaintenanceHandoffCreated", "MaintenanceAction", action.id, handoff, actor)
    _sync_session_actions(session, action.session_id)
    return action


def read_maintenance_handoff_package(session: Session, action_id: str, *, output_dir: Path = Path("data/maintenance")) -> dict:
    action = _get_action(session, action_id)
    markdown_path = output_dir / f"maintenance-action-{action.id}.md"
    json_path = output_dir / f"maintenance-action-{action.id}.json"
    return {
        "action_id": action.id,
        "markdown": {
            "exists": markdown_path.exists(),
            "content": markdown_path.read_text(encoding="utf-8") if markdown_path.exists() else "",
        },
        "json": {
            "exists": json_path.exists(),
            "content": loads(json_path.read_text(encoding="utf-8"), {}) if json_path.exists() else {},
        },
    }


def run_maintenance_validation(
    session: Session,
    action_id: str,
    *,
    selected_commands: list[str] | None = None,
    timeout_seconds: int = 300,
    output_dir: Path = Path("data/maintenance"),
    cwd: Path = Path("."),
    actor: str = "system",
    command_runner: Callable | None = None,
) -> MaintenanceAction:
    action = _get_action(session, action_id)
    payload = code_plan_action_payload(action)
    commands = selected_commands or payload.get("validation_commands") or []
    if not commands:
        raise ValueError("validation commands are required")
    runner = command_runner or _default_command_runner
    results = []
    for command in commands:
        if str(command).strip() not in ALLOWED_VALIDATION_COMMANDS:
            raise ValueError(f"validation command is not allowed: {command}")
        result = runner(command, cwd, timeout_seconds)
        results.append(result.as_dict() if hasattr(result, "as_dict") else dict(result))
    validation = {"validated_by": actor, "commands": results, "output_dir": str(output_dir)}
    action.status = "Validated"
    action.result_json = dumps({**loads(action.result_json, {}), "validation": validation, "commands": results})
    action.updated_at = now_utc()
    _write_validation_report(action, validation, output_dir)
    _add_audit(session, "MaintenanceValidationCompleted", "MaintenanceAction", action.id, validation, actor)
    _sync_session_actions(session, action.session_id)
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
    action = _get_action(session, action_id)
    implementation = {
        "reported_by": actor,
        "summary": summary,
        "changed_files": changed_files or [],
        "tests": tests or [],
        "residual_risks": residual_risks or [],
    }
    action.status = status
    action.result_json = dumps({**loads(action.result_json, {}), "implementation": implementation})
    action.updated_at = now_utc()
    _add_audit(session, "MaintenanceImplementationReported", "MaintenanceAction", action.id, implementation, actor)
    _sync_session_actions(session, action.session_id)
    return action


def review_maintenance_implementation(
    session: Session,
    action_id: str,
    *,
    decision: str,
    note: str = "",
    actor: str = "system",
) -> MaintenanceAction:
    action = _get_action(session, action_id)
    if action.status not in {"PatchReady", "PatchFailed", "NeedsRevision"}:
        raise ValueError(f"maintenance action status {action.status} is not reviewable")
    review = {"reviewed_by": actor, "decision": decision, "note": note}
    action.status = decision
    action.approved_by = actor
    action.result_json = dumps({**loads(action.result_json, {}), "review": review})
    action.updated_at = now_utc()
    _add_audit(session, "MaintenanceImplementationReviewed", "MaintenanceAction", action.id, review, actor)
    _sync_session_actions(session, action.session_id)
    return action


def maintenance_session_timeline(session: Session, session_id: str) -> dict:
    row = _get_session(session, session_id)
    actions = session.query(MaintenanceAction).filter_by(session_id=session_id).order_by(MaintenanceAction.created_at).all()
    action_ids = [action.id for action in actions]
    audit_filter = AuditEvent.related_object_id == session_id
    if action_ids:
        audit_filter = or_(audit_filter, AuditEvent.related_object_id.in_(action_ids))
    audits = session.query(AuditEvent).filter(audit_filter).order_by(AuditEvent.created_at).all()
    timeline = [
        {
            "event_type": audit.event_type,
            "actor": audit.actor,
            "related_object_type": audit.related_object_type,
            "related_object_id": audit.related_object_id,
            "detail": loads(audit.detail, {}),
            "created_at": audit.created_at.isoformat(),
        }
        for audit in audits
    ]
    return {"session": _serialize_session(row), "actions": [_serialize_action(action) for action in actions], "timeline": timeline}


def _diagnosis_markdown(context: dict, *, use_llm: bool) -> str:
    return (
        "## 诊断结论\n\n"
        f"- 入库失败任务：{context['queues']['processing_counts'].get('Failed', 0)}\n"
        f"- 待发送邮件：{context['queues']['outbound_counts'].get('Pending', 0)}\n"
        f"- 打开异常：{context['exceptions']['open_count']}\n"
        f"- 模型配置可用：{'是' if context['runtime']['model_ready'] else '否'}\n"
    )


def _create_action(session: Session, row: MaintenanceSession, action_type: str, payload: dict, *, actor: str) -> MaintenanceAction:
    action = MaintenanceAction(session_id=row.id, action_type=action_type, status="Proposed", input_json=dumps(payload))
    session.add(action)
    session.flush()
    _add_audit(session, "MaintenanceActionProposed", "MaintenanceAction", action.id, {"action_type": action_type}, actor)
    return action


def _sync_session_actions(session: Session, session_id: str) -> None:
    row = _get_session(session, session_id)
    actions = session.query(MaintenanceAction).filter_by(session_id=session_id).order_by(MaintenanceAction.created_at).all()
    row.proposed_actions_json = dumps([_serialize_action(action) for action in actions])
    row.updated_at = now_utc()
    session.flush()


def _serialize_session(row: MaintenanceSession) -> dict:
    return {
        "id": row.id,
        "status": row.status,
        "risk_level": row.risk_level,
        "user_message": row.user_message,
        "created_at": row.created_at.isoformat(),
    }


def _serialize_action(action: MaintenanceAction) -> dict:
    result = loads(action.result_json, {})
    return {
        "id": action.id,
        "action_id": action.id,
        "session_id": action.session_id,
        "action_type": action.action_type,
        "action_status": action.status,
        "status": action.status,
        "input": loads(action.input_json, {}),
        "result": result,
        "handoff": result.get("handoff"),
        "validation_result": result.get("validation"),
        "implementation": result.get("implementation"),
        "review": result.get("review"),
        "created_at": action.created_at.isoformat(),
    }


def _handoff_markdown(action: MaintenanceAction, payload: dict) -> str:
    commands = "\n".join(f"- {command}" for command in payload.get("validation_commands", []))
    files = "\n".join(f"- {path}" for path in payload.get("suggested_files", []))
    return f"# Maintenance Code Plan {action.id}\n\n## Files\n{files}\n\n## Validation\n{commands}\n"


def _write_validation_report(action: MaintenanceAction, validation: dict, output_dir: Path) -> Path:
    output_dir.mkdir(parents=True, exist_ok=True)
    path = output_dir / f"maintenance-action-{action.id}.md"
    lines = [f"# Maintenance Validation {action.id}", ""]
    for result in validation["commands"]:
        lines.append(f"- {result.get('command')}: {result.get('exit_code')}")
    path.write_text("\n".join(lines), encoding="utf-8")
    return path


def _default_command_runner(command: str, cwd: Path, timeout_seconds: int) -> dict:
    raise ValueError("command_runner is required")


def _credential_ref_has_value(session: Session, credential_ref: str | None) -> bool:
    if not credential_ref:
        return False
    if credential_ref.startswith("config:"):
        row = session.get(SystemConfig, credential_ref.removeprefix("config:"))
        return bool(row and row.value)
    if credential_ref.startswith("env:"):
        return False
    return bool(credential_ref)


def _get_session(session: Session, session_id: str) -> MaintenanceSession:
    row = session.get(MaintenanceSession, session_id)
    if row is None:
        raise ValueError("maintenance session not found")
    return row


def _get_action(session: Session, action_id: str) -> MaintenanceAction:
    action = session.get(MaintenanceAction, action_id)
    if action is None:
        raise ValueError("maintenance action not found")
    return action


def _add_audit(
    session: Session,
    event_type: str,
    related_object_type: str,
    related_object_id: str,
    detail: dict,
    actor: str,
) -> None:
    session.add(
        AuditEvent(
            event_type=event_type,
            actor=actor,
            related_object_type=related_object_type,
            related_object_id=related_object_id,
            detail=dumps(detail),
            created_at=now_utc(),
        )
    )
