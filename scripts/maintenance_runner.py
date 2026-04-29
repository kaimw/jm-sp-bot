from __future__ import annotations

import argparse
import shlex
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

ROOT_DIR = Path(__file__).resolve().parents[1]
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from sqlalchemy.orm import Session

from backend.app.database import SessionLocal, init_db
from backend.app.models import AuditEvent, MaintenanceAction, now_utc
from backend.app.services.jsonutil import dumps, loads
from backend.app.services.self_maintenance import (
    code_plan_action_payload,
    create_maintenance_handoff_package,
    render_maintenance_code_plan_report,
    report_maintenance_implementation,
    review_maintenance_implementation,
    run_maintenance_validation,
)

ALLOWED_VALIDATION_COMMANDS = {
    "python3 -m compileall backend scripts",
    "python3 -m pytest",
    "node --check backend/app/static/app.js",
}


@dataclass(frozen=True)
class CommandResult:
    command: str
    exit_code: int
    stdout_tail: str
    stderr_tail: str

    def as_dict(self) -> dict:
        return {
            "command": self.command,
            "exit_code": self.exit_code,
            "stdout_tail": self.stdout_tail,
            "stderr_tail": self.stderr_tail,
        }


CommandRunner = Callable[[str, Path, int], CommandResult]


def text_tail(value: str, limit: int = 4000) -> str:
    if len(value) <= limit:
        return value
    return value[-limit:]


def run_command(command: str, cwd: Path, timeout_seconds: int) -> CommandResult:
    completed = subprocess.run(
        shlex.split(command),
        cwd=str(cwd),
        capture_output=True,
        text=True,
        timeout=timeout_seconds,
        check=False,
    )
    return CommandResult(
        command=command,
        exit_code=completed.returncode,
        stdout_tail=text_tail(completed.stdout or ""),
        stderr_tail=text_tail(completed.stderr or ""),
    )


def get_code_plan_action(session: Session, action_id: str | None = None) -> MaintenanceAction:
    query = session.query(MaintenanceAction).filter(MaintenanceAction.action_type == "code_patch_plan")
    if action_id:
        action = query.filter(MaintenanceAction.id == action_id).one_or_none()
    else:
        action = query.order_by(MaintenanceAction.created_at.desc()).first()
    if action is None:
        raise ValueError("code patch plan action not found")
    return action


def action_plan(action: MaintenanceAction) -> dict:
    return code_plan_action_payload(action)


def allowed_plan_commands(plan: dict, selected_commands: list[str] | None = None) -> list[str]:
    raw_commands = selected_commands or plan.get("validation_commands") or []
    if not isinstance(raw_commands, list) or not raw_commands:
        raise ValueError("validation commands are required")
    commands = []
    for command in raw_commands:
        command_text = str(command).strip()
        if command_text not in ALLOWED_VALIDATION_COMMANDS:
            raise ValueError(f"validation command is not allowed: {command_text}")
        if command_text not in commands:
            commands.append(command_text)
    return commands


def render_code_plan_report(action: MaintenanceAction, *, output_dir: Path) -> Path:
    return render_maintenance_code_plan_report(action, output_dir=output_dir)


def create_handoff_package(
    session: Session,
    *,
    action_id: str | None = None,
    output_dir: Path = Path("data/maintenance"),
) -> MaintenanceAction:
    action = get_code_plan_action(session, action_id)
    action = create_maintenance_handoff_package(session, action.id, actor="maintenance-runner", output_dir=output_dir)
    session.add(
        AuditEvent(
            event_type="MaintenanceRunnerHandoffCreated",
            actor="maintenance-runner",
            related_object_type="MaintenanceAction",
            related_object_id=action.id,
            detail=dumps(loads(action.result_json, {}).get("handoff", {})),
            created_at=now_utc(),
        )
    )
    session.flush()
    return action


def validate_code_plan_action(
    session: Session,
    *,
    action_id: str | None = None,
    cwd: Path = Path("."),
    output_dir: Path = Path("data/maintenance"),
    timeout_seconds: int = 300,
    selected_commands: list[str] | None = None,
    command_runner: CommandRunner = run_command,
) -> MaintenanceAction:
    action = get_code_plan_action(session, action_id)
    action = run_maintenance_validation(
        session,
        action.id,
        selected_commands=selected_commands,
        timeout_seconds=timeout_seconds,
        output_dir=output_dir,
        cwd=cwd,
        actor="maintenance-runner",
        command_runner=command_runner,
    )
    session.add(
        AuditEvent(
            event_type="MaintenanceRunnerValidationCompleted",
            actor="maintenance-runner",
            related_object_type="MaintenanceAction",
            related_object_id=action.id,
            detail=dumps({"status": action.status, "validation": loads(action.result_json, {}).get("validation", {})}),
            created_at=now_utc(),
        )
    )
    session.flush()
    return action


def list_code_plan_actions(session: Session, limit: int = 10) -> list[dict]:
    rows = (
        session.query(MaintenanceAction)
        .filter(MaintenanceAction.action_type == "code_patch_plan")
        .order_by(MaintenanceAction.created_at.desc())
        .limit(limit)
        .all()
    )
    items = []
    for row in rows:
        plan = action_plan(row)
        items.append(
            {
                "id": row.id,
                "status": row.status,
                "title": plan.get("title", "代码修复草案"),
                "risk": plan.get("risk", "Medium"),
                "created_at": row.created_at.isoformat(),
            }
        )
    return items


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run external maintenance validation for self-maintenance code plans.")
    subparsers = parser.add_subparsers(dest="command", required=True)

    list_parser = subparsers.add_parser("list", help="List recent code patch plan actions.")
    list_parser.add_argument("--limit", type=int, default=10)

    export_parser = subparsers.add_parser("export", help="Export a code patch plan report.")
    export_parser.add_argument("--action-id", default=None)
    export_parser.add_argument("--output-dir", default="data/maintenance")

    handoff_parser = subparsers.add_parser("handoff", help="Create a JSON and Markdown handoff package for a code patch plan.")
    handoff_parser.add_argument("--action-id", default=None)
    handoff_parser.add_argument("--output-dir", default="data/maintenance")

    validate_parser = subparsers.add_parser("validate", help="Run allowed validation commands for a code patch plan.")
    validate_parser.add_argument("--action-id", default=None)
    validate_parser.add_argument("--output-dir", default="data/maintenance")
    validate_parser.add_argument("--timeout-seconds", type=int, default=300)
    validate_parser.add_argument("--command", action="append", dest="commands", help="Allowed validation command to run. Can be repeated.")

    complete_parser = subparsers.add_parser("complete", help="Report implementation results for a code patch plan action.")
    complete_parser.add_argument("--action-id", required=True)
    complete_parser.add_argument("--status", choices=["PatchReady", "PatchFailed"], default="PatchReady")
    complete_parser.add_argument("--summary", required=True)
    complete_parser.add_argument("--changed-file", action="append", dest="changed_files", default=[])
    complete_parser.add_argument("--test", action="append", dest="tests", default=[])
    complete_parser.add_argument("--risk", action="append", dest="residual_risks", default=[])

    review_parser = subparsers.add_parser("review", help="Record the human review decision for an implemented code patch.")
    review_parser.add_argument("--action-id", required=True)
    review_parser.add_argument("--decision", choices=["ReviewAccepted", "ReviewRejected", "NeedsRevision"], required=True)
    review_parser.add_argument("--note", default="")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    init_db()
    with SessionLocal() as session:
        if args.command == "list":
            print(dumps({"items": list_code_plan_actions(session, args.limit)}))
            return
        if args.command == "export":
            action = get_code_plan_action(session, args.action_id)
            report_path = render_code_plan_report(action, output_dir=Path(args.output_dir))
            print(dumps({"action_id": action.id, "report_path": str(report_path)}))
            return
        if args.command == "handoff":
            action = create_handoff_package(session, action_id=args.action_id, output_dir=Path(args.output_dir))
            session.commit()
            print(dumps({"action_id": action.id, "status": action.status, "result": loads(action.result_json, {})}))
            return
        if args.command == "validate":
            action = validate_code_plan_action(
                session,
                action_id=args.action_id,
                output_dir=Path(args.output_dir),
                timeout_seconds=args.timeout_seconds,
                selected_commands=args.commands,
            )
            session.commit()
            print(dumps({"action_id": action.id, "status": action.status, "result": loads(action.result_json, {})}))
            return
        if args.command == "complete":
            action = report_maintenance_implementation(
                session,
                args.action_id,
                status=args.status,
                summary=args.summary,
                changed_files=args.changed_files,
                tests=args.tests,
                residual_risks=args.residual_risks,
                actor="maintenance-runner",
            )
            session.add(
                AuditEvent(
                    event_type="MaintenanceRunnerImplementationReported",
                    actor="maintenance-runner",
                    related_object_type="MaintenanceAction",
                    related_object_id=action.id,
                    detail=dumps({"status": action.status, "implementation": loads(action.result_json, {}).get("implementation", {})}),
                    created_at=now_utc(),
                )
            )
            session.commit()
            print(dumps({"action_id": action.id, "status": action.status, "result": loads(action.result_json, {})}))
            return
        if args.command == "review":
            action = review_maintenance_implementation(
                session,
                args.action_id,
                decision=args.decision,
                note=args.note,
                actor="maintenance-runner",
            )
            session.add(
                AuditEvent(
                    event_type="MaintenanceRunnerImplementationReviewed",
                    actor="maintenance-runner",
                    related_object_type="MaintenanceAction",
                    related_object_id=action.id,
                    detail=dumps({"status": action.status, "review": loads(action.result_json, {}).get("review", {})}),
                    created_at=now_utc(),
                )
            )
            session.commit()
            print(dumps({"action_id": action.id, "status": action.status, "result": loads(action.result_json, {})}))
            return
        raise ValueError(f"unsupported command: {args.command}")


if __name__ == "__main__":
    main()
