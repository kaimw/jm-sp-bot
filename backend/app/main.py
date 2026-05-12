from __future__ import annotations

import asyncio
import base64
import binascii
import contextlib
import csv
import hmac
import logging
import re
import uuid
from datetime import datetime, timedelta
from pathlib import Path
from io import StringIO

from fastapi import Depends, FastAPI, HTTPException, Query, Request
from fastapi.responses import FileResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from sqlalchemy import func, or_, text
from sqlalchemy.orm import Session

from backend.app.config import settings
from backend.app.database import SessionLocal, database_runtime_info, init_db
from backend.app.models import (
    AuditEvent,
    AttachmentAsset,
    BackupJob,
    ExceptionCase,
    ExtractionEvidence,
    MailMessage,
    MailWorkflowMatch,
    MailTemplate,
    MaintenanceAction,
    MaintenanceSession,
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
    WorkflowDefinition,
    WorkflowImportJob,
    WorkflowVersion,
    now_utc,
)
from backend.app.schemas import (
    AdminPasswordRequest,
    DemoOrderRequest,
    DepartmentUpsert,
    ExceptionRequirementPatchRequest,
    ExceptionResolveRequest,
    InitialReviewConfigUpdate,
    LoginRequest,
    MailRuntimeConfigUpdate,
    ModelChatTestRequest,
    ModelProviderUpdate,
    OutboundBulkCancelRequest,
    ProductionFeedbackRequest,
    ProductionQuestionRequest,
    TaskClearRequest,
    SalesReplyRequest,
    SelfMaintenanceActionApplyRequest,
    SelfMaintenanceCodePlanRequest,
    SelfMaintenanceDiagnoseRequest,
    SelfMaintenanceHandoffRequest,
    SelfMaintenanceImplementationReportRequest,
    SelfMaintenanceReviewRequest,
    SelfMaintenanceSessionArchiveRequest,
    SelfMaintenanceValidationRequest,
    TaskManualCloseRequest,
    TemplateUpdate,
    WorkflowContactMapUpdate,
    WorkflowChatGenerateRequest,
    WorkflowChatSaveRequest,
    WorkflowImportRequest,
    WorkflowSimulationRequest,
    WorkflowVersionUpdateRequest,
    WeeklyReportRecipientsUpdate,
    ProductSPUCreate,
    ProductSKUCreate,
    ChannelPricingUpdate,
    PromotionRuleCreate,
    PromotionRuleUpdate,
)
from backend.app.config import MAIL_LOGIN_MIN_INTERVAL_SECONDS, MAIL_WORKER_MIN_INTERVAL_SECONDS
from backend.app.services.auth import COOKIE_NAME, create_session_token, parse_session_token
from backend.app.services.bootstrap import seed_defaults, set_config
from backend.app.services.e2e_mail import run_tencent_mail_e2e
from backend.app.services.initial_review import (
    DEFAULT_REQUIRED_FIELDS,
    FIELD_LABELS,
    OPERATOR_OPTIONS,
    dedupe_initial_review_rules,
    initial_review_config,
    remember_deleted_workflow_review_rules,
    sync_workflow_review_rules_to_initial_review,
)
from backend.app.services.jsonutil import as_list, dumps, loads
from backend.app.services.jobs import run_pending_jobs
from backend.app.services.mail_adapter import AUTO_WORKFLOW_MAIL_TYPES, send_pending_smtp, sync_imap_mailbox
from backend.app.services.mail_worker import configured_mail_worker_interval_seconds, get_mail_worker_status, run_mail_auto_worker_once
from backend.app.services.mail_throttle import clamp_mail_interval_seconds
from backend.app.services.model_provider import call_model, extract_chat_content, resolve_api_key
from backend.app.services.operations import cleanup_preview, create_backup, execute_cleanup, storage_usage, weekly_report_csv
from backend.app.services.pdf import simple_pdf
from backend.app.services.self_maintenance import (
    apply_maintenance_action,
    archive_maintenance_session,
    build_self_maintenance_context,
    create_code_patch_plan,
    create_maintenance_handoff_package,
    create_maintenance_diagnosis,
    maintenance_session_timeline,
    read_maintenance_handoff_package,
    report_maintenance_implementation,
    review_maintenance_implementation,
    run_maintenance_validation,
    serialize_maintenance_action,
    serialize_maintenance_session,
)
from backend.app.services.workflow_rules import (
    activate_workflow_version,
    chat_generate_workflow_rule,
    deactivate_workflow_version,
    delete_workflow_version,
    import_structured_workflow_rules,
    import_workflow_document,
    list_workflow_rules,
    rollback_workflow_version,
    save_workflow_version_rules,
    workflow_version_diff,
)
from backend.app.services.workflow import (
    approve_task,
    create_inbound_mail,
    create_task_from_mail,
    dashboard,
    enqueue_weekly_report,
    force_close_task_manual,
    apply_exception_requirement_patch,
    process_inbound_mail,
    record_production_question,
    record_production_feedback,
    record_sales_reply,
    retry_outbound_mail,
    resolve_exception_case,
    set_weekly_report_recipients,
    weekly_report,
    weekly_report_mail_body,
    weekly_report_recipients,
    weekly_report_subject,
)
from backend.app.services.products import (
    create_spu,
    create_sku,
    set_channel_pricing,
    create_promotion_rule,
    get_spus,
    get_skus,
    get_channel_pricing,
    get_promotions,
    update_promotion_rule,
    delete_promotion_rule,
    toggle_promotion_rule,
)


app = FastAPI(title="商务生产任务单智能体 MVP")

PUBLIC_API_PATHS = {"/api/auth/login", "/api/auth/logout", "/api/auth/me"}
logger = logging.getLogger(__name__)
mail_worker_task: asyncio.Task | None = None
EMAIL_ADDRESS_PATTERN = re.compile(r"^[^@\s,;]+@[^@\s,;]+\.[^@\s,;]+$")


@app.middleware("http")
async def add_request_id_and_auth(request: Request, call_next):
    request_id = request.headers.get("X-Request-ID") or str(uuid.uuid4())
    if request.method != "OPTIONS" and request.url.path.startswith("/api/") and request.url.path not in PUBLIC_API_PATHS:
        username = parse_session_token(request.cookies.get(COOKIE_NAME))
        if username is None:
            response = JSONResponse({"detail": "not authenticated"}, status_code=401)
            response.headers["X-Request-ID"] = request_id
            return response
        request.state.username = username
    response = await call_next(request)
    response.headers["X-Request-ID"] = request_id
    if request.url.path == "/" or request.url.path.startswith("/static/"):
        response.headers["Cache-Control"] = "no-store"
    return response


def get_session():
    session = SessionLocal()
    try:
        yield session
    finally:
        session.close()


def page_response(query, serializer, page: int, page_size: int, extra: dict | None = None) -> dict:
    total = query.order_by(None).count()
    total_pages = max(1, (total + page_size - 1) // page_size)
    page = min(page, total_pages)
    rows = query.offset((page - 1) * page_size).limit(page_size).all()
    return {
        "items": [serializer(row) for row in rows],
        "page": page,
        "page_size": page_size,
        "total": total,
        "total_pages": total_pages,
        **(extra or {}),
    }


def distinct_values(session: Session, column) -> list[str]:
    return [
        str(row[0])
        for row in session.query(column).distinct().order_by(column).all()
        if row[0] not in (None, "")
    ]


@app.on_event("startup")
async def startup() -> None:
    init_db()
    with SessionLocal() as session:
        seed_defaults(session)
        session.commit()
    if settings.mail_auto_worker_enabled:
        global mail_worker_task
        mail_worker_task = asyncio.create_task(mail_auto_worker_loop())


@app.on_event("shutdown")
async def shutdown() -> None:
    if mail_worker_task is not None:
        mail_worker_task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await mail_worker_task


async def mail_auto_worker_loop() -> None:
    await asyncio.sleep(await asyncio.to_thread(configured_mail_worker_interval_seconds))
    while True:
        try:
            result = await asyncio.to_thread(run_mail_auto_worker_once)
            logger.info("mail auto worker result: %s", result)
        except Exception:
            logger.exception("mail auto worker iteration failed")
        await asyncio.sleep(await asyncio.to_thread(configured_mail_worker_interval_seconds))


@app.get("/health")
def health(session: Session = Depends(get_session)) -> dict:
    readiness = runtime_startup_readiness(session)
    queues = system_queue_health(session)
    return {
        "status": "ok",
        "ready": readiness["ready"],
        "missing": readiness["missing"],
        "bot_enabled": system_config_bool(session, "bot_enabled", False),
        "database": database_health(session),
        "queues": queues,
    }


@app.get("/api/system/health")
def system_health(session: Session = Depends(get_session)) -> dict:
    return {
        "readiness": runtime_startup_readiness(session),
        "bot_enabled": system_config_bool(session, "bot_enabled", False),
        "database": database_health(session),
        "worker": get_mail_worker_status(configured_worker_interval_seconds(session)),
        "queues": system_queue_health(session),
    }


def database_health(session: Session) -> dict:
    info = database_runtime_info()
    try:
        session.execute(text("SELECT 1"))
    except Exception as exc:
        return {**info, "ok": False, "error_type": exc.__class__.__name__}
    return {**info, "ok": True}


@app.post("/api/auth/login")
def login(payload: LoginRequest) -> Response:
    if payload.username != settings.admin_username or payload.password != settings.admin_password:
        raise HTTPException(status_code=401, detail="invalid username or password")
    response = JSONResponse({"authenticated": True, "username": payload.username})
    response.set_cookie(
        key=COOKIE_NAME,
        value=create_session_token(payload.username),
        max_age=settings.auth_session_seconds,
        httponly=True,
        samesite="lax",
    )
    return response


@app.post("/api/auth/logout")
def logout() -> Response:
    response = JSONResponse({"authenticated": False})
    response.delete_cookie(COOKIE_NAME)
    return response


@app.get("/api/auth/me")
def me(request: Request) -> dict:
    username = parse_session_token(request.cookies.get(COOKIE_NAME))
    return {"authenticated": username is not None, "username": username}


@app.post("/api/bootstrap")
def bootstrap(session: Session = Depends(get_session)) -> dict:
    seed_defaults(session)
    session.commit()
    return {"ok": True}


def normalize_email_values(values: list[str] | tuple[str, ...] | None) -> list[str]:
    normalized: list[str] = []
    for value in values or []:
        for item in re.split(r"[,，;；\s]+", str(value or "")):
            email = item.strip()
            if email:
                normalized.append(email)
    return normalized


def invalid_email_addresses(values: list[str] | tuple[str, ...] | None) -> list[str]:
    return [email for email in normalize_email_values(values) if not EMAIL_ADDRESS_PATTERN.fullmatch(email)]


def clear_task_records(session: Session) -> dict:
    tasks_to_clear = session.query(ProductionTask).all()
    task_ids = [task.id for task in tasks_to_clear]
    requirement_ids = [task.requirement_id for task in tasks_to_clear]
    version_ids = [
        row.id
        for row in session.query(ProductionTaskVersion.id).filter(ProductionTaskVersion.task_id.in_(task_ids)).all()
    ] if task_ids else []

    mail_updates = 0
    outbound_updates = 0
    exception_updates = 0
    question_deletes = 0
    version_deletes = 0
    binding_deletes = 0
    evidence_deletes = 0
    requirement_deletes = 0
    task_deletes = 0

    if task_ids:
        mail_updates = (
            session.query(MailMessage)
            .filter(MailMessage.related_task_id.in_(task_ids))
            .update({MailMessage.related_task_id: None}, synchronize_session=False)
        )
        exception_updates = (
            session.query(ExceptionCase)
            .filter(ExceptionCase.related_task_id.in_(task_ids))
            .update({ExceptionCase.related_task_id: None}, synchronize_session=False)
        )
        outbound_query = session.query(OutboundMailJob).filter(OutboundMailJob.related_task_id.in_(task_ids))
        if version_ids:
            outbound_query = session.query(OutboundMailJob).filter(
                or_(OutboundMailJob.related_task_id.in_(task_ids), OutboundMailJob.related_version_id.in_(version_ids))
            )
        outbound_updates = outbound_query.update(
            {OutboundMailJob.related_task_id: None, OutboundMailJob.related_version_id: None},
            synchronize_session=False,
        )
        question_deletes = (
            session.query(QuestionAndReply)
            .filter(QuestionAndReply.task_id.in_(task_ids))
            .delete(synchronize_session=False)
        )
        version_deletes = (
            session.query(ProductionTaskVersion)
            .filter(ProductionTaskVersion.task_id.in_(task_ids))
            .delete(synchronize_session=False)
        )
        task_deletes = (
            session.query(ProductionTask)
            .filter(ProductionTask.id.in_(task_ids))
            .delete(synchronize_session=False)
        )

    if requirement_ids:
        binding_deletes = (
            session.query(RequirementWorkflowBinding)
            .filter(RequirementWorkflowBinding.requirement_id.in_(requirement_ids))
            .delete(synchronize_session=False)
        )
        evidence_deletes = (
            session.query(ExtractionEvidence)
            .filter(ExtractionEvidence.requirement_id.in_(requirement_ids))
            .delete(synchronize_session=False)
        )
        requirement_deletes = (
            session.query(OrderRequirement)
            .filter(OrderRequirement.id.in_(requirement_ids))
            .delete(synchronize_session=False)
        )

    return {
        "task_count": task_deletes,
        "requirement_count": requirement_deletes,
        "version_count": version_deletes,
        "question_count": question_deletes,
        "binding_count": binding_deletes,
        "evidence_count": evidence_deletes,
        "mail_links_cleared": mail_updates,
        "outbound_links_cleared": outbound_updates,
        "exception_links_cleared": exception_updates,
    }


def clear_workflow_records(session: Session) -> dict:
    match_deletes = session.query(MailWorkflowMatch).delete(synchronize_session=False)
    import_job_deletes = session.query(WorkflowImportJob).delete(synchronize_session=False)
    version_deletes = session.query(WorkflowVersion).delete(synchronize_session=False)
    definition_deletes = session.query(WorkflowDefinition).delete(synchronize_session=False)
    return {
        "workflow_definition_count": definition_deletes,
        "workflow_version_count": version_deletes,
        "workflow_import_job_count": import_job_deletes,
        "mail_workflow_match_count": match_deletes,
    }


def clear_remaining_requirement_records(session: Session) -> dict:
    requirement_ids = [row.id for row in session.query(OrderRequirement.id).all()]
    if not requirement_ids:
        return {
            "orphan_requirement_count": 0,
            "orphan_binding_count": 0,
            "orphan_evidence_count": 0,
        }
    binding_deletes = (
        session.query(RequirementWorkflowBinding)
        .filter(RequirementWorkflowBinding.requirement_id.in_(requirement_ids))
        .delete(synchronize_session=False)
    )
    evidence_deletes = (
        session.query(ExtractionEvidence)
        .filter(ExtractionEvidence.requirement_id.in_(requirement_ids))
        .delete(synchronize_session=False)
    )
    requirement_deletes = (
        session.query(OrderRequirement)
        .filter(OrderRequirement.id.in_(requirement_ids))
        .delete(synchronize_session=False)
    )
    return {
        "orphan_requirement_count": requirement_deletes,
        "orphan_binding_count": binding_deletes,
        "orphan_evidence_count": evidence_deletes,
    }


def reset_initial_review_records(session: Session) -> dict:
    rules_row = session.get(SystemConfig, "initial_review_rules_json")
    deleted_ids_row = session.get(SystemConfig, "initial_review_workflow_rule_deleted_ids_json")
    required_fields_row = session.get(SystemConfig, "initial_review_required_fields_json")
    rules = loads(rules_row.value if rules_row is not None else "[]", [])
    deleted_ids = loads(deleted_ids_row.value if deleted_ids_row is not None else "[]", [])
    required_fields = loads(required_fields_row.value if required_fields_row is not None else "[]", [])
    rule_count = len(rules) if isinstance(rules, list) else 0
    deleted_id_count = len(deleted_ids) if isinstance(deleted_ids, list) else 0
    required_field_count = len(required_fields) if isinstance(required_fields, list) else 0
    set_config(session, "initial_review_enabled", "true", is_secret=False)
    set_config(session, "initial_review_required_fields_json", dumps(DEFAULT_REQUIRED_FIELDS), is_secret=False)
    set_config(session, "initial_review_rules_json", "[]", is_secret=False)
    set_config(session, "initial_review_workflow_rule_deleted_ids_json", "[]", is_secret=False)
    return {
        "initial_review_rule_count": rule_count,
        "initial_review_deleted_rule_marker_count": deleted_id_count,
        "initial_review_required_field_count": required_field_count,
    }


def runtime_startup_readiness(session: Session, overrides: dict | None = None) -> dict:
    overrides = overrides or {}
    missing: list[str] = []
    model = session.query(ModelProviderConfig).filter_by(status="Active").first()
    if model is None or not (model.api_base or "").strip():
        missing.append("Dify API Base")
    if model is None or not resolve_api_key(session, model).strip():
        missing.append("Dify API Key")

    saved_bot_email = session.get(SystemConfig, "bot_email")
    bot_email = str(overrides.get("bot_email") or (saved_bot_email.value if saved_bot_email else "")).strip()
    if not bot_email:
        missing.append("bot邮箱")

    incoming_password = overrides.get("bot_email_password")
    saved_password = session.get(SystemConfig, "bot_email_password")
    if not (str(incoming_password or "").strip() or (saved_password is not None and str(saved_password.value or "").strip())):
        missing.append("bot邮箱密码")

    departments = session.query(ProductionDepartment).filter_by(status="Active").all()
    production_main_recipients: list[str] = []
    invalid_production_main_recipients: list[str] = []
    for dept in departments:
        recipients = normalize_email_values(as_list(dept.mail_to_json))
        production_main_recipients.extend(recipients)
        invalid_production_main_recipients.extend(invalid_email_addresses(recipients))
    if not production_main_recipients:
        missing.append("生产部门主送邮箱")
    elif invalid_production_main_recipients:
        missing.append(f"生产部门主送邮箱格式不合法：{', '.join(invalid_production_main_recipients)}")

    return {"ready": not missing, "missing": missing}


def system_config_bool(session: Session, key: str, default: bool = False) -> bool:
    row = session.get(SystemConfig, key)
    if row is None or row.value in (None, ""):
        return default
    return str(row.value).strip().lower() in {"1", "true", "yes", "on"}


def configured_worker_interval_seconds(session: Session) -> int:
    row = session.get(SystemConfig, "mail_auto_worker_interval_seconds")
    try:
        value = int(row.value) if row is not None else settings.mail_auto_worker_interval_seconds
    except (TypeError, ValueError):
        value = settings.mail_auto_worker_interval_seconds
    return max(MAIL_WORKER_MIN_INTERVAL_SECONDS, value)


def seconds_since(value: datetime | None) -> int | None:
    if value is None:
        return None
    now = now_utc()
    if value.tzinfo is None:
        now = now.replace(tzinfo=None)
    return max(0, int((now - value).total_seconds()))


def config_int(session: Session, key: str, default: int) -> int:
    row = session.get(SystemConfig, key)
    try:
        return int(row.value) if row is not None else default
    except (TypeError, ValueError):
        return default


def system_queue_health(session: Session) -> dict:
    outbound_counts = dict(
        session.query(OutboundMailJob.status, func.count(OutboundMailJob.id))
        .group_by(OutboundMailJob.status)
        .all()
    )
    processing_counts = dict(
        session.query(ProcessingJob.status, func.count(ProcessingJob.id))
        .group_by(ProcessingJob.status)
        .all()
    )
    oldest_pending = (
        session.query(OutboundMailJob)
        .filter(OutboundMailJob.status == "Pending")
        .order_by(OutboundMailJob.created_at)
        .first()
    )
    pending_auto = (
        session.query(OutboundMailJob)
        .filter(OutboundMailJob.status == "Pending", OutboundMailJob.mail_type.in_(AUTO_WORKFLOW_MAIL_TYPES))
        .count()
    )
    pending_manual = max(0, int(outbound_counts.get("Pending", 0) or 0) - pending_auto)
    return {
        "outbound": {
            "counts": outbound_counts,
            "pending_auto_dispatchable": pending_auto,
            "pending_manual_only": pending_manual,
            "oldest_pending_id": oldest_pending.id if oldest_pending else None,
            "oldest_pending_age_seconds": seconds_since(oldest_pending.created_at) if oldest_pending else None,
            "single_run_send_limit": 1,
        },
        "processing": {"counts": processing_counts},
    }


def require_admin_password(admin_password: str) -> None:
    if not hmac.compare_digest(admin_password or "", settings.admin_password or ""):
        raise HTTPException(status_code=403, detail="invalid admin password")


@app.get("/api/config")
def config(session: Session = Depends(get_session)) -> dict:
    configs = {row.key: ("***" if row.is_secret else row.value) for row in session.query(SystemConfig).all()}
    model = session.query(ModelProviderConfig).filter_by(status="Active").first()
    return {
        "configs": configs,
        "model": None
        if model is None
        else {
            "title": model.title,
            "provider": model.provider,
            "model_name": model.model_name,
            "api_base": model.api_base,
            "credential_ref": model.credential_ref,
        },
        "startup_readiness": runtime_startup_readiness(session),
    }


@app.put("/api/config/mail")
def update_mail_config(payload: MailRuntimeConfigUpdate, session: Session = Depends(get_session)) -> dict:
    values = payload.model_dump(exclude_unset=True)
    if "mail_auto_worker_interval_seconds" in values and values["mail_auto_worker_interval_seconds"] not in (None, ""):
        requested_interval = int(values["mail_auto_worker_interval_seconds"])
        if requested_interval < MAIL_WORKER_MIN_INTERVAL_SECONDS:
            raise HTTPException(status_code=400, detail=f"worker 执行周期不能低于 {MAIL_WORKER_MIN_INTERVAL_SECONDS} 秒")
        values["mail_auto_worker_interval_seconds"] = requested_interval
    if "mail_rate_limit_interval_seconds" in values and values["mail_rate_limit_interval_seconds"] not in (None, ""):
        requested_interval = int(values["mail_rate_limit_interval_seconds"])
        if requested_interval < MAIL_LOGIN_MIN_INTERVAL_SECONDS:
            raise HTTPException(status_code=400, detail=f"邮箱登录/发信间隔不能低于 {MAIL_LOGIN_MIN_INTERVAL_SECONDS} 秒")
        values["mail_rate_limit_interval_seconds"] = clamp_mail_interval_seconds(requested_interval)
    if values.get("bot_enabled") is True:
        readiness = runtime_startup_readiness(session, values)
        if not readiness["ready"]:
            raise HTTPException(status_code=400, detail=f"系统启动前配置不完整：缺少 {'、'.join(readiness['missing'])}")
    secret_keys = {"bot_email_password", "baidu_map_ak", "e2e_sales_password", "e2e_production_password"}
    for key, value in values.items():
        if value in (None, ""):
            continue
        set_config(session, key, str(value), is_secret=key in secret_keys)
    session.commit()
    return config(session)


@app.post("/api/system/business-data/clear")
def clear_business_data(payload: AdminPasswordRequest, request: Request, session: Session = Depends(get_session)) -> dict:
    require_admin_password(payload.admin_password)
    task_detail = clear_task_records(session)
    orphan_requirement_detail = clear_remaining_requirement_records(session)
    workflow_detail = clear_workflow_records(session)
    initial_review_detail = reset_initial_review_records(session)
    actor = getattr(request.state, "username", "system")
    detail = {
        **task_detail,
        **orphan_requirement_detail,
        **workflow_detail,
        **initial_review_detail,
    }
    session.add(
        AuditEvent(
            event_type="BusinessDataCleared",
            actor=actor,
            related_object_type="System",
            related_object_id="business-data",
            detail=dumps(detail),
            created_at=now_utc(),
        )
    )
    session.commit()
    return {
        "ok": True,
        "cleared": detail,
        "task_count": task_detail["task_count"],
        "workflow_count": workflow_detail["workflow_definition_count"],
        "initial_review_rule_count": initial_review_detail["initial_review_rule_count"],
    }


@app.get("/api/initial-review/rules")
def get_initial_review_rules(session: Session = Depends(get_session)) -> dict:
    config = initial_review_config(session, include_workflow_rules=True)
    session.commit()
    return config


@app.put("/api/initial-review/rules")
def update_initial_review_rules(payload: InitialReviewConfigUpdate, session: Session = Depends(get_session)) -> dict:
    allowed_fields = set(FIELD_LABELS)
    allowed_operators = {item["key"] for item in OPERATOR_OPTIONS}
    required_fields = [field for field in payload.required_fields if field in allowed_fields and field != "source_text"]
    payload_rule_ids = {str(rule.id) for rule in payload.rules if rule.id}
    remember_deleted_workflow_review_rules(session, payload_rule_ids)
    rules = []
    for rule in payload.rules:
        data = rule.model_dump()
        if data.get("read_only") or data.get("is_builtin") or str(data.get("id") or "").startswith("builtin-"):
            continue
        if data["field"] not in allowed_fields:
            raise HTTPException(status_code=400, detail=f"unsupported review field: {data['field']}")
        if data["operator"] not in allowed_operators:
            raise HTTPException(status_code=400, detail=f"unsupported review operator: {data['operator']}")
        if not data.get("id"):
            data["id"] = str(uuid.uuid4())
        rules.append(data)
    rules = dedupe_initial_review_rules(rules)
    set_config(session, "initial_review_enabled", "true" if payload.enabled else "false")
    set_config(session, "initial_review_required_fields_json", dumps(required_fields))
    set_config(session, "initial_review_rules_json", dumps(rules))
    session.commit()
    return initial_review_config(session, include_workflow_rules=True)


@app.post("/api/workflows/import")
def import_workflow_rules(payload: WorkflowImportRequest, request: Request, session: Session = Depends(get_session)) -> dict:
    file_content: bytes | None = None
    if payload.file_content_base64:
        try:
            file_content = base64.b64decode(payload.file_content_base64, validate=True)
        except (binascii.Error, ValueError) as exc:
            raise HTTPException(status_code=400, detail="file_content_base64 is invalid") from exc
    if not payload.file_path and not file_content and not payload.raw_text:
        raise HTTPException(status_code=400, detail="file upload or raw_text is required")
    try:
        result = import_workflow_document(
            session,
            file_path=payload.file_path,
            raw_text=payload.raw_text,
            file_name=payload.file_name,
            file_content=file_content,
            prefer_llm=payload.prefer_llm,
            auto_publish=payload.auto_publish,
            actor=getattr(request.state, "username", "system"),
        )
        sync_workflow_review_rules_to_initial_review(session)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    session.commit()
    return result


@app.post("/api/workflows/chat/generate")
def workflow_chat_generate(payload: WorkflowChatGenerateRequest, session: Session = Depends(get_session)) -> dict:
    turns = [item.model_dump() for item in payload.messages if item.content.strip()]
    if not turns:
        raise HTTPException(status_code=400, detail="messages is required")
    try:
        result = chat_generate_workflow_rule(
            session,
            messages=turns,
            current_rule=payload.current_rule,
            edit_version_id=payload.edit_version_id,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    session.commit()
    return result


@app.post("/api/workflows/chat/save")
def workflow_chat_save(
    payload: WorkflowChatSaveRequest,
    request: Request,
    session: Session = Depends(get_session),
) -> dict:
    if not payload.compiled_rule:
        raise HTTPException(status_code=400, detail="compiled_rule is required")
    try:
        actor = getattr(request.state, "username", "system")
        if payload.edit_version_id:
            version = save_workflow_version_rules(
                session,
                payload.edit_version_id,
                compiled_rules=payload.compiled_rule,
                actor=actor,
                activate=payload.activate,
            )
            result = {
                "job_id": None,
                "file_name": "workflow-chat.json",
                "source_asset_ref": "workflow-chat-edit",
                "llm_used": True,
                "validation_errors": [],
                "diffs": [],
                "created_versions": [
                    {
                        "id": version.id,
                        "workflow_id": version.workflow_id,
                        "version_no": version.version_no,
                        "status": version.status,
                    }
                ],
                "updated_version": serialize_workflow_version(version),
            }
            sync_workflow_review_rules_to_initial_review(session)
        else:
            result = import_structured_workflow_rules(
                session,
                rules=[payload.compiled_rule],
                auto_publish=payload.activate,
                actor=actor,
                source_asset_ref="workflow-chat",
                file_name="workflow-chat.json",
                llm_used=True,
            )
            sync_workflow_review_rules_to_initial_review(session)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    session.commit()
    return result


@app.get("/api/workflows")
def workflows(
    q: str | None = None,
    only_active: bool = False,
    session: Session = Depends(get_session),
) -> dict:
    rows = list_workflow_rules(session, only_active=only_active)
    if q and q.strip():
        keyword = q.strip().lower()
        rows = [
            row
            for row in rows
            if keyword in str(row.get("workflow_code", "")).lower() or keyword in str(row.get("workflow_name", "")).lower()
        ]
    return {"items": rows, "total": len(rows)}


@app.post("/api/workflows/versions/{version_id}/activate")
def activate_workflow(version_id: str, request: Request, session: Session = Depends(get_session)) -> dict:
    try:
        version = activate_workflow_version(session, version_id, actor=getattr(request.state, "username", "system"))
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    session.commit()
    return serialize_workflow_version(version)


@app.post("/api/workflows/versions/{version_id}/deactivate")
def deactivate_workflow(version_id: str, session: Session = Depends(get_session)) -> dict:
    try:
        version = deactivate_workflow_version(session, version_id)
    except ValueError as exc:
        detail = str(exc)
        status = 404 if "not found" in detail else 400
        raise HTTPException(status_code=status, detail=detail) from exc
    session.commit()
    return serialize_workflow_version(version)


@app.delete("/api/workflows/versions/{version_id}")
def remove_workflow_version(version_id: str, session: Session = Depends(get_session)) -> dict:
    try:
        delete_workflow_version(session, version_id)
    except ValueError as exc:
        detail = str(exc)
        status = 404 if "not found" in detail else 400
        raise HTTPException(status_code=status, detail=detail) from exc
    session.commit()
    return {"deleted": True, "version_id": version_id}


@app.put("/api/workflows/versions/{version_id}")
def update_workflow_version(
    version_id: str,
    payload: WorkflowVersionUpdateRequest,
    request: Request,
    session: Session = Depends(get_session),
) -> dict:
    try:
        version = save_workflow_version_rules(
            session,
            version_id,
            compiled_rules=payload.compiled_rules,
            actor=getattr(request.state, "username", "system"),
            activate=payload.activate,
        )
        sync_workflow_review_rules_to_initial_review(session)
    except ValueError as exc:
        detail = str(exc)
        status = 404 if "not found" in detail else 400
        raise HTTPException(status_code=status, detail=detail) from exc
    session.commit()
    return serialize_workflow_version(version)


@app.get("/api/workflows/versions/{version_id}/diff")
def workflow_version_diff_api(
    version_id: str,
    compare_to: str | None = None,
    session: Session = Depends(get_session),
) -> dict:
    try:
        return workflow_version_diff(session, version_id, compare_to_version_id=compare_to)
    except ValueError as exc:
        detail = str(exc)
        status = 404 if "not found" in detail else 400
        raise HTTPException(status_code=status, detail=detail) from exc


@app.post("/api/workflows/versions/{version_id}/rollback")
def workflow_version_rollback_api(version_id: str, request: Request, session: Session = Depends(get_session)) -> dict:
    try:
        version = rollback_workflow_version(session, version_id, actor=getattr(request.state, "username", "system"))
        sync_workflow_review_rules_to_initial_review(session)
    except ValueError as exc:
        detail = str(exc)
        status = 404 if "not found" in detail else 400
        raise HTTPException(status_code=status, detail=detail) from exc
    session.commit()
    return serialize_workflow_version(version)


@app.post("/api/workflows/simulate")
def workflow_simulate(payload: WorkflowSimulationRequest, session: Session = Depends(get_session)) -> dict:
    before_outbound_ids = {row.id for row in session.query(OutboundMailJob.id).all()}
    before_exception_ids = {row.id for row in session.query(ExceptionCase.id).all()}
    before_audit_ids = {row.id for row in session.query(AuditEvent.id).all()}
    mail: MailMessage | None = None
    task: ProductionTask | None = None
    error = ""
    try:
        if not payload.use_llm:
            for model in session.query(ModelProviderConfig).filter_by(status="Active").all():
                model.status = "SimulationDisabled"
            session.flush()
        mail = create_inbound_mail(
            session,
            from_address=payload.from_address,
            subject=payload.subject,
            body_text=payload.body_text,
            dedupe_key=f"simulation:{uuid.uuid4()}",
        )
        task = create_task_from_mail(session, mail)
        session.flush()
        requirement = (
            session.query(OrderRequirement)
            .filter_by(source_mail_id=mail.id)
            .order_by(OrderRequirement.created_at.desc())
            .first()
            if mail is not None
            else None
        )
        binding = (
            session.query(RequirementWorkflowBinding).filter_by(requirement_id=requirement.id).one_or_none()
            if requirement is not None
            else None
        )
        match = (
            session.query(MailWorkflowMatch)
            .filter_by(mail_id=mail.id)
            .order_by(MailWorkflowMatch.created_at.desc())
            .first()
            if mail is not None
            else None
        )
        outbounds = session.query(OutboundMailJob).filter(~OutboundMailJob.id.in_(before_outbound_ids)).order_by(OutboundMailJob.created_at).all()
        exceptions = session.query(ExceptionCase).filter(~ExceptionCase.id.in_(before_exception_ids)).order_by(ExceptionCase.created_at).all()
        audits = session.query(AuditEvent).filter(~AuditEvent.id.in_(before_audit_ids)).order_by(AuditEvent.created_at).all()
        result = {
            "classification": mail.classification if mail is not None else None,
            "classification_confidence": mail.classification_confidence if mail is not None else 0,
            "would_create_task": task is not None,
            "task": serialize_task(task, include_versions=True) if task is not None else None,
            "requirement": serialize_requirement_summary(requirement) if requirement is not None else None,
            "workflow": serialize_requirement_workflow_binding(binding) if binding is not None else None,
            "workflow_match": {
                "workflow_version_id": match.workflow_version_id,
                "workflow_code": match.workflow_code,
                "confidence": match.confidence,
                "detail": loads(match.match_detail_json, {}),
            }
            if match is not None
            else None,
            "outbound_mails": [serialize_outbound_mail(row, session) for row in outbounds],
            "exceptions": [serialize_exception(row) for row in exceptions],
            "audits": [serialize_audit_event(row) for row in audits],
        }
    except Exception as exc:
        error = str(exc)
        result = {"would_create_task": False, "error": error}
    finally:
        session.rollback()
    if error:
        return result
    return result


@app.get("/api/workflow-import-jobs")
def workflow_import_jobs(
    q: str | None = None,
    parse_status: str | None = None,
    status: str | None = None,
    page: int = Query(1, ge=1),
    page_size: int = Query(10, ge=1, le=100),
    session: Session = Depends(get_session),
) -> dict:
    query = session.query(WorkflowImportJob)
    if q and q.strip():
        pattern = f"%{q.strip()}%"
        query = query.filter(
            or_(
                WorkflowImportJob.file_name.ilike(pattern),
                WorkflowImportJob.source_asset_ref.ilike(pattern),
                WorkflowImportJob.source_text.ilike(pattern),
            )
        )
    if parse_status and parse_status.strip():
        query = query.filter(WorkflowImportJob.parse_status == parse_status.strip())
    if status and status.strip():
        query = query.filter(WorkflowImportJob.status == status.strip())
    return page_response(
        query.order_by(WorkflowImportJob.created_at.desc()),
        serialize_workflow_import_job,
        page,
        page_size,
        {
            "parse_status_options": distinct_values(session, WorkflowImportJob.parse_status),
            "status_options": distinct_values(session, WorkflowImportJob.status),
        },
    )


@app.get("/api/workflows/contact-map")
def workflow_contact_map(session: Session = Depends(get_session)) -> dict:
    row = session.get(SystemConfig, "workflow_contact_map_json")
    mapping = loads(row.value if row is not None else "{}", {})
    if not isinstance(mapping, dict):
        mapping = {}
    return {"mapping": mapping}


@app.put("/api/workflows/contact-map")
def update_workflow_contact_map(payload: WorkflowContactMapUpdate, session: Session = Depends(get_session)) -> dict:
    normalized: dict[str, str | list[str]] = {}
    for key, value in payload.mapping.items():
        name = str(key).strip()
        if not name:
            continue
        if isinstance(value, list):
            emails = [str(item).strip() for item in value if str(item).strip()]
            normalized[name] = emails
        else:
            email = str(value).strip()
            if email:
                normalized[name] = email
    set_config(session, "workflow_contact_map_json", dumps(normalized), is_secret=False)
    session.commit()
    return {"mapping": normalized}


@app.post("/api/e2e/tencent-mail/run")
def run_tencent_mail_e2e_api(session: Session = Depends(get_session)) -> dict:
    try:
        result = run_tencent_mail_e2e(session)
    except Exception as exc:
        session.commit()
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    session.commit()
    return result


@app.put("/api/model-providers/active")
def update_model_provider(payload: ModelProviderUpdate, session: Session = Depends(get_session)) -> dict:
    model = session.query(ModelProviderConfig).filter_by(status="Active").first()
    if model is None:
        model = ModelProviderConfig(
            title=payload.title or "Dify deepseekV3",
            provider=payload.provider or "openai",
            model_name=payload.model_name or "DeepSeek-V3",
            api_base=payload.api_base or "http://192.168.10.55:5000/v1",
            credential_ref=payload.credential_ref or "env:MODEL_API_KEY",
            status="Active",
        )
        session.add(model)
        session.flush()
    for field in ("title", "provider", "model_name", "api_base", "credential_ref"):
        value = getattr(payload, field)
        if value not in (None, ""):
            setattr(model, field, value)
    if payload.api_key:
        set_config(session, "model_api_key", payload.api_key, is_secret=True)
        model.credential_ref = "config:model_api_key"
    session.commit()
    return config(session)["model"] or {}


@app.get("/api/dashboard")
def dashboard_api(session: Session = Depends(get_session)) -> dict:
    return dashboard(session)


@app.post("/api/demo/order")
def demo_order(payload: DemoOrderRequest, session: Session = Depends(get_session)) -> dict:
    mail = create_inbound_mail(
        session,
        from_address=str(payload.from_address),
        subject=payload.subject,
        body_text=payload.body_text,
    )
    result = process_inbound_mail(session, mail)
    task = result if isinstance(result, ProductionTask) else None
    session.commit()
    return {"mail_id": mail.id, "classification": mail.classification, "task_id": task.id if task else None}


@app.get("/api/mails")
def mails(
    q: str | None = None,
    classification: str | None = None,
    direction: str | None = None,
    from_address: str | None = None,
    page: int = Query(1, ge=1),
    page_size: int = Query(10, ge=1, le=100),
    session: Session = Depends(get_session),
) -> dict:
    query = session.query(MailMessage)
    if q and q.strip():
        pattern = f"%{q.strip()}%"
        query = query.filter(
            or_(
                MailMessage.id.ilike(pattern),
                MailMessage.subject.ilike(pattern),
                MailMessage.from_address.ilike(pattern),
                MailMessage.body_text.ilike(pattern),
                MailMessage.classification.ilike(pattern),
                MailMessage.related_task_id.ilike(pattern),
            )
        )
    if classification and classification.strip():
        query = query.filter(MailMessage.classification == classification.strip())
    if direction and direction.strip():
        query = query.filter(MailMessage.direction == direction.strip())
    if from_address and from_address.strip():
        query = query.filter(MailMessage.from_address.ilike(f"%{from_address.strip()}%"))
    return page_response(
        query.order_by(MailMessage.created_at.desc()),
        serialize_mail,
        page,
        page_size,
        {
            "classification_options": distinct_values(session, MailMessage.classification),
            "direction_options": distinct_values(session, MailMessage.direction),
        },
    )


@app.get("/api/mails/{mail_id}")
def mail_detail(mail_id: str, session: Session = Depends(get_session)) -> dict:
    mail = session.get(MailMessage, mail_id)
    if mail is None:
        raise HTTPException(status_code=404, detail="mail not found")
    data = serialize_mail(mail)
    data["body_text"] = mail.body_text
    data["attachments"] = [serialize_attachment(row) for row in session.query(AttachmentAsset).filter_by(mail_id=mail.id).all()]
    return data


@app.post("/api/mails/{mail_id}/reprocess")
def reprocess_mail(mail_id: str, session: Session = Depends(get_session)) -> dict:
    mail = session.get(MailMessage, mail_id)
    if mail is None:
        raise HTTPException(status_code=404, detail="mail not found")
    result = process_inbound_mail(session, mail)
    session.commit()
    return {"mail": serialize_mail(mail), "result_type": type(result).__name__ if result is not None else None}


@app.get("/api/tasks")
def tasks(
    q: str | None = None,
    status: str | None = None,
    customer: str | None = None,
    product: str | None = None,
    salesperson: str | None = None,
    order_no: str | None = None,
    delivery: str | None = None,
    page: int = Query(1, ge=1),
    page_size: int = Query(10, ge=1, le=100),
    session: Session = Depends(get_session),
) -> dict:
    query = session.query(ProductionTask).join(OrderRequirement, ProductionTask.requirement_id == OrderRequirement.id)
    if q and q.strip():
        pattern = f"%{q.strip()}%"
        query = query.filter(
            or_(
                ProductionTask.task_no.ilike(pattern),
                ProductionTask.status.ilike(pattern),
                OrderRequirement.customer_name.ilike(pattern),
                OrderRequirement.salesperson_email.ilike(pattern),
                OrderRequirement.product_summary.ilike(pattern),
                OrderRequirement.quantity_text.ilike(pattern),
                OrderRequirement.expected_delivery_date.ilike(pattern),
                OrderRequirement.external_order_no.ilike(pattern),
            )
        )
    if status and status.strip():
        query = query.filter(ProductionTask.status == status.strip())
    if customer and customer.strip():
        query = query.filter(OrderRequirement.customer_name.ilike(f"%{customer.strip()}%"))
    if product and product.strip():
        query = query.filter(OrderRequirement.product_summary.ilike(f"%{product.strip()}%"))
    if salesperson and salesperson.strip():
        query = query.filter(OrderRequirement.salesperson_email.ilike(f"%{salesperson.strip()}%"))
    if order_no and order_no.strip():
        query = query.filter(OrderRequirement.external_order_no.ilike(f"%{order_no.strip()}%"))
    if delivery and delivery.strip():
        query = query.filter(OrderRequirement.expected_delivery_date.ilike(f"%{delivery.strip()}%"))

    return page_response(
        query.order_by(ProductionTask.created_at.desc()),
        serialize_task,
        page,
        page_size,
        {"status_options": distinct_values(session, ProductionTask.status)},
    )


@app.post("/api/tasks/clear")
def clear_tasks(payload: TaskClearRequest, request: Request, session: Session = Depends(get_session)) -> dict:
    require_admin_password(payload.admin_password)
    detail = clear_task_records(session)
    actor = getattr(request.state, "username", "system")
    session.add(
        AuditEvent(
            event_type="TaskListCleared",
            actor=actor,
            related_object_type="ProductionTask",
            related_object_id="bulk-clear",
            detail=dumps(detail),
            created_at=now_utc(),
        )
    )
    session.commit()
    return {"cleared": detail["task_count"], **detail}


@app.get("/api/tasks/{task_id}")
def task_detail(task_id: str, session: Session = Depends(get_session)) -> dict:
    task = session.get(ProductionTask, task_id)
    if task is None:
        raise HTTPException(status_code=404, detail="task not found")
    data = serialize_task(task, include_versions=True)
    binding = session.query(RequirementWorkflowBinding).filter_by(requirement_id=task.requirement_id).one_or_none()
    if binding is not None:
        data["workflow"] = serialize_requirement_workflow_binding(binding)
    return data


def task_trace_graph(session: Session, task: ProductionTask) -> dict:
    requirement = task.requirement
    source_mail = session.get(MailMessage, requirement.source_mail_id) if requirement.source_mail_id else None
    versions = session.query(ProductionTaskVersion).filter_by(task_id=task.id).order_by(ProductionTaskVersion.version_no).all()
    version_ids = [row.id for row in versions]
    outbound_query = session.query(OutboundMailJob).filter(OutboundMailJob.related_task_id == task.id)
    if version_ids:
        outbound_query = session.query(OutboundMailJob).filter(
            or_(OutboundMailJob.related_task_id == task.id, OutboundMailJob.related_version_id.in_(version_ids))
        )
    outbounds = outbound_query.order_by(OutboundMailJob.created_at).all()
    questions = session.query(QuestionAndReply).filter_by(task_id=task.id).order_by(QuestionAndReply.created_at).all()
    exceptions = session.query(ExceptionCase).filter_by(related_task_id=task.id).order_by(ExceptionCase.created_at).all()
    evidences = session.query(ExtractionEvidence).filter_by(requirement_id=requirement.id).order_by(ExtractionEvidence.created_at).all()
    binding = session.query(RequirementWorkflowBinding).filter_by(requirement_id=requirement.id).one_or_none()

    nodes: list[dict] = []
    edges: list[dict] = []

    def add_node(node_id: str, node_type: str, label: str, *, status: str = "", meta: dict | None = None) -> None:
        if not any(item["id"] == node_id for item in nodes):
            nodes.append({"id": node_id, "type": node_type, "label": label, "status": status, "meta": meta or {}})

    def add_edge(source: str, target: str, label: str) -> None:
        if source and target and not any(item["source"] == source and item["target"] == target and item["label"] == label for item in edges):
            edges.append({"source": source, "target": target, "label": label})

    if source_mail is not None:
        add_node(source_mail.id, "mail", source_mail.subject or "来源邮件", status=source_mail.classification or "", meta=serialize_mail(source_mail))
        add_edge(source_mail.id, requirement.id, "抽取")
    add_node(requirement.id, "requirement", requirement.internal_order_no, status=requirement.status, meta=serialize_requirement_summary(requirement))
    add_node(task.id, "task", task.task_no, status=task.status, meta=serialize_task(task))
    add_edge(requirement.id, task.id, "生成任务")
    if binding is not None:
        workflow_node_id = binding.workflow_version_id or f"workflow:{binding.workflow_code or 'unknown'}"
        add_node(workflow_node_id, "workflow", binding.workflow_name or binding.workflow_code or "命中流程", status=str(binding.match_confidence), meta=serialize_requirement_workflow_binding(binding))
        add_edge(workflow_node_id, requirement.id, "规则初审")

    for version in versions:
        add_node(version.id, "task_version", f"V{version.version_no}", status=version.status, meta={"subject": version.subject})
        add_edge(task.id, version.id, "版本")
    for job in outbounds:
        add_node(job.id, "outbound_mail", job.subject, status=job.status, meta=serialize_outbound_mail(job, session))
        add_edge(job.related_version_id or task.id, job.id, job.mail_type)
    for question in questions:
        add_node(question.id, "question", question.question_text[:80], status=question.status, meta=serialize_question(question))
        add_edge(task.id, question.id, "生产疑问")
        if question.production_question_mail_id:
            add_edge(question.production_question_mail_id, question.id, "提出疑问")
        if question.sales_reply_mail_id:
            add_edge(question.sales_reply_mail_id, question.id, "销售答复")
    for exception in exceptions:
        add_node(exception.id, "exception", exception.exception_type, status=exception.status, meta=serialize_exception(exception))
        add_edge(task.id, exception.id, "异常")
    for evidence in evidences:
        add_node(evidence.id, "evidence", evidence.field_name, status=str(evidence.confidence), meta=serialize_evidence(evidence))
        add_edge(evidence.source_mail_id or requirement.id, evidence.id, "证据")
        add_edge(evidence.id, requirement.id, "支撑字段")

    object_ids = {task.id, requirement.id, *(row.id for row in versions), *(row.id for row in outbounds), *(row.id for row in questions), *(row.id for row in exceptions)}
    if source_mail is not None:
        object_ids.add(source_mail.id)
    audits = (
        session.query(AuditEvent)
        .filter(AuditEvent.related_object_id.in_(object_ids))
        .order_by(AuditEvent.created_at.desc())
        .limit(100)
        .all()
    )
    timeline = [
        {
            "type": "audit",
            "title": row.event_type,
            "status": row.related_object_type,
            "detail": loads(row.detail, {}),
            "created_at": row.created_at.isoformat(),
        }
        for row in audits
    ]
    return {"nodes": nodes, "edges": edges, "timeline": timeline}


@app.get("/api/tasks/{task_id}/workflow")
def task_workflow(task_id: str, session: Session = Depends(get_session)) -> dict:
    task = session.get(ProductionTask, task_id)
    if task is None:
        raise HTTPException(status_code=404, detail="task not found")

    requirement = task.requirement
    source_mail = session.get(MailMessage, requirement.source_mail_id) if requirement.source_mail_id else None
    versions = (
        session.query(ProductionTaskVersion)
        .filter_by(task_id=task.id)
        .order_by(ProductionTaskVersion.version_no)
        .all()
    )
    outbounds = (
        session.query(OutboundMailJob)
        .filter_by(related_task_id=task.id)
        .order_by(OutboundMailJob.created_at)
        .all()
    )
    questions = sessionless_sorted_questions(task)
    issue_job = next((job for job in outbounds if job.mail_type in {"TaskIssue", "SalesReplyTaskReissue"}), None)
    feedback_job = next((job for job in outbounds if job.mail_type in {"ProductionConfirmed", "ProductionRejected"}), None)

    def step(key: str, title: str, status: str, detail: str = "", at=None) -> dict:
        return {
            "key": key,
            "title": title,
            "status": status,
            "detail": detail,
            "created_at": at.isoformat() if at else None,
        }

    issue_status = "pending"
    issue_detail = "等待生成生产任务单邮件"
    if issue_job is not None:
        issue_status = "done" if issue_job.status == "Sent" else "current"
        issue_detail = f"{issue_job.mail_type} / {issue_job.status}"

    production_status = "pending"
    production_detail = "等待生产部门确认、驳回或提出疑问"
    if task.status == "Closed":
        production_status = "done"
        production_detail = task.closed_reason or "生产已确认"
    elif task.status in {"ProductionQuestioned", "CancelReview"} or any(row.status == "AwaitingSalesReply" for row in questions):
        production_status = "current"
        production_detail = "生产疑问沟通中"
    elif task.status in {"TaskIssued", "Reissued"}:
        production_status = "current"

    steps = [
        step("received", "销售需求邮件", "done", source_mail.subject if source_mail else "来源邮件已记录", source_mail.created_at if source_mail else task.created_at),
        step("review", "自动初审", "done", "订单信息已通过初审", task.created_at),
        step("draft", "生成任务单", "done", f"当前版本 V{task.current_version_no}", task.created_at),
        step("issue", "自动下达生产", issue_status, issue_detail, issue_job.created_at if issue_job else None),
        step("production", "生产处理", production_status, production_detail, feedback_job.created_at if feedback_job else None),
        step("closed", "闭环", "done" if task.status == "Closed" else "pending", task.closed_reason or "", task.updated_at),
    ]

    object_ids = {task.id, requirement.id}
    if source_mail is not None:
        object_ids.add(source_mail.id)
    object_ids.update(version.id for version in versions)
    object_ids.update(job.id for job in outbounds)
    audits = (
        session.query(AuditEvent)
        .filter(AuditEvent.related_object_id.in_(object_ids))
        .order_by(AuditEvent.created_at.desc())
        .limit(50)
        .all()
    )
    exceptions = (
        session.query(ExceptionCase)
        .filter(ExceptionCase.related_task_id == task.id)
        .order_by(ExceptionCase.created_at.desc())
        .all()
    )

    timeline = [
        {
            "type": "audit",
            "title": row.event_type,
            "status": "记录",
            "detail": loads(row.detail, {}),
            "created_at": row.created_at.isoformat(),
        }
        for row in audits
    ]
    timeline.extend(
        {
            "type": "outbound",
            "title": job.mail_type,
            "status": job.status,
            "detail": {"subject": job.subject, "to": as_list(job.to_json), "cc": as_list(job.cc_json)},
            "created_at": job.created_at.isoformat(),
        }
        for job in outbounds
    )
    timeline.extend(
        {
            "type": "question",
            "title": "生产疑问",
            "status": row.status,
            "detail": {"question": row.question_text, "reply": row.reply_text},
            "created_at": row.created_at.isoformat(),
        }
        for row in questions
    )
    timeline.extend(
        {
            "type": "exception",
            "title": row.exception_type,
            "status": row.status,
            "detail": loads(row.detail, {}),
            "created_at": row.created_at.isoformat(),
        }
        for row in exceptions
    )
    timeline.sort(key=lambda item: item["created_at"], reverse=True)

    return {
        "task": serialize_task(task, include_versions=True),
        "steps": steps,
        "timeline": timeline[:80],
        "trace": task_trace_graph(session, task),
    }


@app.get("/api/tasks/{task_id}/trace")
def task_trace(task_id: str, session: Session = Depends(get_session)) -> dict:
    task = session.get(ProductionTask, task_id)
    if task is None:
        raise HTTPException(status_code=404, detail="task not found")
    return {"task": serialize_task(task), **task_trace_graph(session, task)}


@app.post("/api/tasks/{task_id}/approve")
def approve(task_id: str, session: Session = Depends(get_session)) -> dict:
    try:
        job = approve_task(session, task_id)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    session.commit()
    return {"outbound_job_id": job.id, "status": job.status}


@app.post("/api/tasks/{task_id}/manual-close")
def manual_close_task(task_id: str, payload: TaskManualCloseRequest, request: Request, session: Session = Depends(get_session)) -> dict:
    try:
        jobs = force_close_task_manual(
            session,
            task_id,
            reason=payload.note,
            actor=getattr(request.state, "username", "business-owner"),
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    session.commit()
    return {
        "closed": True,
        "outbound_jobs": [{"id": job.id, "mail_type": job.mail_type, "status": job.status} for job in jobs],
    }


@app.post("/api/tasks/{task_id}/production-feedback")
def feedback(task_id: str, payload: ProductionFeedbackRequest, session: Session = Depends(get_session)) -> dict:
    try:
        job = record_production_feedback(session, task_id, payload.feedback_type, payload.note)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    session.commit()
    return {"outbound_job_id": job.id, "status": job.status}


@app.post("/api/tasks/{task_id}/production-question")
def production_question(task_id: str, payload: ProductionQuestionRequest, session: Session = Depends(get_session)) -> dict:
    try:
        job = record_production_question(session, task_id, payload.question_text)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    session.commit()
    return {"outbound_job_id": job.id, "status": job.status}


@app.post("/api/tasks/{task_id}/sales-reply")
def sales_reply(task_id: str, payload: SalesReplyRequest, session: Session = Depends(get_session)) -> dict:
    try:
        version = record_sales_reply(session, task_id, payload.reply_text)
    except ValueError as exc:
        session.commit()
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    session.commit()
    return {"version_id": version.id, "version_no": version.version_no, "status": version.status}


@app.get("/api/tasks/{task_id}/questions")
def task_questions(task_id: str, session: Session = Depends(get_session)) -> list[dict]:
    task = session.get(ProductionTask, task_id)
    if task is None:
        raise HTTPException(status_code=404, detail="task not found")
    rows = session.query(QuestionAndReply).filter_by(task_id=task_id).order_by(QuestionAndReply.created_at.desc()).all()
    return [serialize_question(row) for row in rows]


@app.get("/api/tasks/{task_id}/evidence")
def task_evidence(task_id: str, session: Session = Depends(get_session)) -> list[dict]:
    task = session.get(ProductionTask, task_id)
    if task is None:
        raise HTTPException(status_code=404, detail="task not found")
    rows = session.query(ExtractionEvidence).filter_by(requirement_id=task.requirement_id).order_by(ExtractionEvidence.created_at).all()
    return [serialize_evidence(row) for row in rows]


@app.get("/api/outbound-mails")
def outbound_mails(
    q: str | None = None,
    status: str | None = None,
    mail_type: str | None = None,
    recipient: str | None = None,
    page: int = Query(1, ge=1),
    page_size: int = Query(10, ge=1, le=100),
    session: Session = Depends(get_session),
) -> dict:
    query = session.query(OutboundMailJob)
    if q and q.strip():
        pattern = f"%{q.strip()}%"
        query = query.filter(
            or_(
                OutboundMailJob.subject.ilike(pattern),
                OutboundMailJob.mail_type.ilike(pattern),
                OutboundMailJob.status.ilike(pattern),
                OutboundMailJob.to_json.ilike(pattern),
                OutboundMailJob.cc_json.ilike(pattern),
            )
        )
    if status and status.strip():
        query = query.filter(OutboundMailJob.status == status.strip())
    if mail_type and mail_type.strip():
        query = query.filter(OutboundMailJob.mail_type == mail_type.strip())
    if recipient and recipient.strip():
        pattern = f"%{recipient.strip()}%"
        query = query.filter(or_(OutboundMailJob.to_json.ilike(pattern), OutboundMailJob.cc_json.ilike(pattern)))
    return page_response(
        query.order_by(OutboundMailJob.created_at.desc()),
        lambda row: serialize_outbound_mail(row, session),
        page,
        page_size,
        {
            "status_options": distinct_values(session, OutboundMailJob.status),
            "mail_type_options": distinct_values(session, OutboundMailJob.mail_type),
        },
    )


def filtered_outbound_query(
    session: Session,
    *,
    q: str | None = None,
    status: str | None = None,
    mail_type: str | None = None,
    recipient: str | None = None,
):
    query = session.query(OutboundMailJob)
    if q and q.strip():
        pattern = f"%{q.strip()}%"
        query = query.filter(
            or_(
                OutboundMailJob.subject.ilike(pattern),
                OutboundMailJob.mail_type.ilike(pattern),
                OutboundMailJob.status.ilike(pattern),
                OutboundMailJob.to_json.ilike(pattern),
                OutboundMailJob.cc_json.ilike(pattern),
            )
        )
    if status and status.strip():
        query = query.filter(OutboundMailJob.status == status.strip())
    if mail_type and mail_type.strip():
        query = query.filter(OutboundMailJob.mail_type == mail_type.strip())
    if recipient and recipient.strip():
        pattern = f"%{recipient.strip()}%"
        query = query.filter(or_(OutboundMailJob.to_json.ilike(pattern), OutboundMailJob.cc_json.ilike(pattern)))
    return query


def outbound_pending_diagnosis(session: Session, row: OutboundMailJob) -> dict | None:
    if row.status != "Pending":
        return None

    recipients = as_list(row.to_json) + as_list(row.cc_json)
    age_seconds = seconds_since(row.created_at) or 0
    queue_position = (
        session.query(func.count(OutboundMailJob.id))
        .filter(OutboundMailJob.status == "Pending", OutboundMailJob.created_at <= row.created_at)
        .scalar()
        or 1
    )
    interval_seconds = configured_worker_interval_seconds(session)
    auto_dispatchable = row.mail_type in AUTO_WORKFLOW_MAIL_TYPES
    details: list[str] = []

    if not recipients:
        return {
            "severity": "invalid",
            "reason": "缺少收件人，发送时会失败",
            "details": ["请补充主送或取消该外发任务"],
            "pending_age_seconds": age_seconds,
            "queue_position": queue_position,
            "auto_dispatchable": auto_dispatchable,
        }

    if not system_config_bool(session, "bot_enabled", False):
        return {
            "severity": "blocked",
            "reason": "系统已停用，自动 worker 不会消费",
            "details": ["可在右上角启动系统，或手动发送待发邮件"],
            "pending_age_seconds": age_seconds,
            "queue_position": queue_position,
            "auto_dispatchable": auto_dispatchable,
        }

    if not (session.get(SystemConfig, "bot_email_password") and session.get(SystemConfig, "bot_email_password").value):
        return {
            "severity": "blocked",
            "reason": "Bot 邮箱密码未配置",
            "details": ["请在接入配置中配置邮箱密码后再发送"],
            "pending_age_seconds": age_seconds,
            "queue_position": queue_position,
            "auto_dispatchable": auto_dispatchable,
        }

    if not auto_dispatchable:
        return {
            "severity": "manual",
            "reason": "该类型不在自动 worker 消费范围内",
            "details": ["需要手动触发发送，或把该类型纳入自动发送白名单"],
            "pending_age_seconds": age_seconds,
            "queue_position": queue_position,
            "auto_dispatchable": False,
        }

    worker = get_mail_worker_status(interval_seconds)
    if not worker.get("auto_worker_enabled"):
        return {
            "severity": "blocked",
            "reason": "自动 worker 未启用",
            "details": ["需要手动发送，或以 MAIL_AUTO_WORKER_ENABLED=true 启动服务"],
            "pending_age_seconds": age_seconds,
            "queue_position": queue_position,
            "auto_dispatchable": True,
        }

    expected_wait_seconds = max(0, (int(queue_position) - 1) * interval_seconds)
    details.append(f"当前队列位置第 {queue_position} 位")
    details.append(f"worker 周期 {interval_seconds} 秒，单轮最多发送 1 封")
    if expected_wait_seconds:
        details.append(f"按当前限速预计至少等待 {expected_wait_seconds} 秒")
    if worker.get("last_finished_at"):
        details.append(f"最近 worker 完成时间：{worker['last_finished_at']}")
    else:
        details.append("当前进程尚未记录 worker 完成时间")

    configured_threshold = config_int(session, "outbound_pending_age_alert_seconds", 3600)
    delayed_threshold = max(configured_threshold, interval_seconds * max(1, int(queue_position)), interval_seconds)
    severity = "delayed" if age_seconds > delayed_threshold else "waiting"
    reason = "超过预计等待时间，需检查 worker 心跳或 SMTP 状态" if severity == "delayed" else "等待下一轮 worker 消费"
    return {
        "severity": severity,
        "reason": reason,
        "details": details,
        "pending_age_seconds": age_seconds,
        "queue_position": queue_position,
        "estimated_min_wait_seconds": expected_wait_seconds,
        "auto_dispatchable": True,
    }


@app.post("/api/outbound-mails/send-pending")
def send_pending_outbound(limit: int = 20, session: Session = Depends(get_session)) -> dict:
    try:
        result = send_pending_smtp(session, limit=limit)
    except RuntimeError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    session.commit()
    return result


@app.post("/api/outbound-mails/cancel-pending")
def cancel_pending_outbound(payload: OutboundBulkCancelRequest, request: Request, session: Session = Depends(get_session)) -> dict:
    if payload.status and payload.status.strip() and payload.status.strip() != "Pending":
        return {"cancelled": 0, "matched": 0, "status": "Cancelled", "skipped": "only Pending outbound mails can be cancelled"}

    query = filtered_outbound_query(
        session,
        q=payload.q,
        status="Pending",
        mail_type=payload.mail_type,
        recipient=payload.recipient,
    )
    if payload.ids:
        query = query.filter(OutboundMailJob.id.in_(payload.ids))
    rows = query.order_by(OutboundMailJob.created_at).limit(payload.limit).all()
    now = now_utc()
    actor = getattr(request.state, "username", "system")
    for row in rows:
        row.status = "Cancelled"
        session.add(
            AuditEvent(
                event_type="OutboundMailCancelled",
                actor=actor,
                related_object_type="OutboundMailJob",
                related_object_id=row.id,
                detail=dumps({"mail_type": row.mail_type, "subject": row.subject, "previous_status": "Pending"}),
                created_at=now,
            )
        )
    session.commit()
    return {
        "cancelled": len(rows),
        "matched": len(rows),
        "status": "Cancelled",
        "limit": payload.limit,
    }


@app.post("/api/outbound-mails/{job_id}/retry")
def retry_outbound(job_id: str, session: Session = Depends(get_session)) -> dict:
    try:
        job = retry_outbound_mail(session, job_id)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    session.commit()
    return {"outbound_job_id": job.id, "status": job.status}


@app.get("/api/outbound-mails/diagnostics")
def outbound_mail_diagnostics(
    hours: int = Query(24, ge=1, le=168),
    limit: int = Query(10, ge=1, le=50),
    session: Session = Depends(get_session),
) -> dict:
    return outbound_mail_diagnostics_data(session, hours=hours, limit=limit)


def outbound_mail_diagnostics_data(session: Session, *, hours: int = 24, limit: int = 10) -> dict:
    since = now_utc() - timedelta(hours=hours)
    status_counts = dict(
        session.query(OutboundMailJob.status, func.count(OutboundMailJob.id))
        .group_by(OutboundMailJob.status)
        .all()
    )
    failed_jobs = (
        session.query(OutboundMailJob)
        .filter(OutboundMailJob.status == "Failed")
        .order_by(OutboundMailJob.created_at.desc())
        .limit(limit)
        .all()
    )
    failed_cases = (
        session.query(ExceptionCase)
        .filter(ExceptionCase.exception_type == "OutboundMailSendFailed", ExceptionCase.created_at >= since)
        .order_by(ExceptionCase.created_at.desc())
        .limit(max(limit, 20))
        .all()
    )

    by_type: dict[str, int] = {}
    trend: dict[str, int] = {}
    recent_failures = []
    for case in failed_cases:
        detail = loads(case.detail, {})
        mail_type = str(detail.get("mail_type") or "Unknown")
        by_type[mail_type] = by_type.get(mail_type, 0) + 1
        bucket = case.created_at.strftime("%Y-%m-%d %H:00")
        trend[bucket] = trend.get(bucket, 0) + 1
        if len(recent_failures) < limit:
            recent_failures.append(
                {
                    "id": case.id,
                    "outbound_job_id": detail.get("outbound_job_id"),
                    "mail_type": mail_type,
                    "subject": detail.get("subject"),
                    "error": detail.get("error"),
                    "created_at": case.created_at.isoformat(),
                    "status": case.status,
                }
            )

    failed_total = int(status_counts.get("Failed", 0) or 0)
    pending_age_threshold = config_int(session, "outbound_pending_age_alert_seconds", 3600)
    failed_threshold = config_int(session, "outbound_failed_alert_threshold", 1)
    oldest_pending = (
        session.query(OutboundMailJob)
        .filter(OutboundMailJob.status == "Pending")
        .order_by(OutboundMailJob.created_at)
        .first()
    )
    oldest_pending_age = seconds_since(oldest_pending.created_at) if oldest_pending else None
    alerts = []
    if failed_total >= failed_threshold:
        alerts.append(
            {
                "level": "High",
                "type": "outbound_failed_threshold",
                "message": f"外发失败数 {failed_total} 已达到阈值 {failed_threshold}",
            }
        )
    if oldest_pending_age is not None and oldest_pending_age >= pending_age_threshold:
        alerts.append(
            {
                "level": "Medium",
                "type": "outbound_pending_age_threshold",
                "message": f"最早 Pending 已等待 {oldest_pending_age} 秒，超过阈值 {pending_age_threshold} 秒",
                "outbound_job_id": oldest_pending.id,
            }
        )

    return {
        "window_hours": hours,
        "thresholds": {
            "failed_count": failed_threshold,
            "pending_age_seconds": pending_age_threshold,
        },
        "alert_recipients": outbound_alert_recipients(session),
        "alerts": alerts,
        "status_counts": status_counts,
        "failed_by_type": [{"mail_type": key, "count": value} for key, value in sorted(by_type.items(), key=lambda item: item[1], reverse=True)],
        "failure_trend": [{"hour": key, "count": trend[key]} for key in sorted(trend)],
        "recent_failures": recent_failures,
        "dead_letters": [serialize_outbound_mail(job, session) for job in failed_jobs],
    }


def outbound_alert_recipients(session: Session) -> list[str]:
    configured = session.get(SystemConfig, "outbound_alert_to_json")
    recipients = as_list(configured.value) if configured is not None else []
    if not recipients:
        ops = session.get(SystemConfig, "ops_cc_email")
        if ops is not None and str(ops.value or "").strip():
            recipients.append(str(ops.value).strip())
    if not recipients:
        ceo = session.get(SystemConfig, "ceo_email")
        if ceo is not None and str(ceo.value or "").strip():
            recipients.append(str(ceo.value).strip())
    return list(dict.fromkeys(item for item in recipients if item))


@app.post("/api/outbound-mails/diagnostics/notify")
def notify_outbound_diagnostics(
    request: Request,
    hours: int = Query(24, ge=1, le=168),
    session: Session = Depends(get_session),
) -> dict:
    data = outbound_mail_diagnostics_data(session, hours=hours, limit=10)
    alerts = data.get("alerts") or []
    if not alerts:
        return {"queued": False, "reason": "no outbound diagnostics alerts", "alerts": []}
    recipients = outbound_alert_recipients(session)
    if not recipients:
        raise HTTPException(status_code=400, detail="outbound alert recipients are not configured")

    now = now_utc()
    alert_types = ",".join(sorted(str(item.get("type") or "") for item in alerts))
    idem = f"outbound-alert:{now.strftime('%Y%m%d%H')}:{alert_types}:{','.join(recipients)}"
    existing = session.query(OutboundMailJob).filter_by(idempotency_key=idem).one_or_none()
    if existing is not None:
        return {"queued": False, "outbound_job_id": existing.id, "status": existing.status, "reason": "already queued in this hour"}

    subject = f"[外发队列告警][{now.strftime('%Y-%m-%d %H:%M')}] 请处理异常邮件队列"
    body_lines = [
        "运维同事好，",
        "",
        "系统检测到外发队列存在需要处理的异常：",
        *[f"- {item.get('message')}" for item in alerts],
        "",
        f"状态统计：{dumps(data.get('status_counts') or {})}",
        "",
        "请登录商务部小J后台查看【外发】诊断信息并处理。",
    ]
    job = OutboundMailJob(
        mail_type="OutboundAlert",
        to_json=dumps(recipients),
        cc_json=dumps([]),
        subject=subject,
        body="\n".join(body_lines),
        idempotency_key=idem,
        status="Pending",
    )
    session.add(job)
    session.flush()
    actor = getattr(request.state, "username", "system")
    session.add(
        AuditEvent(
            event_type="OutboundDiagnosticsAlertQueued",
            actor=actor,
            related_object_type="OutboundMailJob",
            related_object_id=job.id,
            detail=dumps({"alerts": alerts, "to": recipients}),
            created_at=now,
        )
    )
    session.commit()
    return {"queued": True, "outbound_job_id": job.id, "status": job.status, "to": recipients, "alerts": alerts}


@app.get("/api/outbound-mails/diagnostics/export.csv")
def outbound_mail_diagnostics_csv(
    hours: int = Query(24, ge=1, le=168),
    limit: int = Query(100, ge=1, le=500),
    session: Session = Depends(get_session),
) -> Response:
    data = outbound_mail_diagnostics_data(session, hours=hours, limit=limit)
    buffer = StringIO()
    writer = csv.DictWriter(
        buffer,
        fieldnames=["section", "created_at", "outbound_job_id", "mail_type", "subject", "status", "error", "to", "cc"],
    )
    writer.writeheader()
    for failure in data["recent_failures"]:
        writer.writerow(
            {
                "section": "recent_failure",
                "created_at": failure.get("created_at", ""),
                "outbound_job_id": failure.get("outbound_job_id", ""),
                "mail_type": failure.get("mail_type", ""),
                "subject": failure.get("subject", ""),
                "status": failure.get("status", ""),
                "error": failure.get("error", ""),
                "to": "",
                "cc": "",
            }
        )
    for job in data["dead_letters"]:
        writer.writerow(
            {
                "section": "dead_letter",
                "created_at": job.get("created_at", ""),
                "outbound_job_id": job.get("id", ""),
                "mail_type": job.get("mail_type", ""),
                "subject": job.get("subject", ""),
                "status": job.get("status", ""),
                "error": "",
                "to": ", ".join(job.get("to") or []),
                "cc": ", ".join(job.get("cc") or []),
            }
        )
    return Response(
        buffer.getvalue(),
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": 'attachment; filename="outbound-mail-diagnostics.csv"'},
    )


@app.get("/api/outbound-mails/{job_id}")
def outbound_mail_detail(job_id: str, session: Session = Depends(get_session)) -> dict:
    row = session.get(OutboundMailJob, job_id)
    if row is None:
        raise HTTPException(status_code=404, detail="outbound mail not found")
    return serialize_outbound_mail(row, session, include_body=True)


@app.post("/api/mailbox/sync")
def mailbox_sync(limit: int = 20, session: Session = Depends(get_session)) -> dict:
    try:
        result = sync_imap_mailbox(session, limit=limit)
    except RuntimeError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    session.commit()
    return result


@app.post("/api/mailbox/auto-run-once")
def mailbox_auto_run_once() -> dict:
    try:
        return run_mail_auto_worker_once()
    except RuntimeError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.get("/api/jobs")
def list_jobs(
    q: str | None = None,
    status: str | None = None,
    job_type: str | None = None,
    page: int = Query(1, ge=1),
    page_size: int = Query(10, ge=1, le=100),
    session: Session = Depends(get_session),
) -> dict:
    query = session.query(ProcessingJob)
    if q and q.strip():
        pattern = f"%{q.strip()}%"
        query = query.filter(
            or_(
                ProcessingJob.id.ilike(pattern),
                ProcessingJob.job_type.ilike(pattern),
                ProcessingJob.status.ilike(pattern),
                ProcessingJob.error_message.ilike(pattern),
            )
        )
    if status and status.strip():
        query = query.filter(ProcessingJob.status == status.strip())
    if job_type and job_type.strip():
        query = query.filter(ProcessingJob.job_type == job_type.strip())
    return page_response(
        query.order_by(ProcessingJob.created_at.desc()),
        serialize_processing_job,
        page,
        page_size,
        {
            "status_options": distinct_values(session, ProcessingJob.status),
            "job_type_options": distinct_values(session, ProcessingJob.job_type),
        },
    )


@app.post("/api/jobs/clear")
def clear_jobs(payload: AdminPasswordRequest, request: Request, session: Session = Depends(get_session)) -> dict:
    require_admin_password(payload.admin_password)
    deleted = session.query(ProcessingJob).delete(synchronize_session=False)
    actor = getattr(request.state, "username", "system")
    session.add(
        AuditEvent(
            event_type="ProcessingJobsCleared",
            actor=actor,
            related_object_type="ProcessingJob",
            related_object_id="bulk-clear",
            detail=dumps({"job_count": deleted}),
            created_at=now_utc(),
        )
    )
    session.commit()
    return {"cleared": deleted}


@app.post("/api/jobs/run-pending")
def run_jobs(limit: int = 20, session: Session = Depends(get_session)) -> dict:
    result = run_pending_jobs(session, limit=limit)
    session.commit()
    return result


@app.get("/api/attachments")
def list_attachments(
    mail_id: str | None = None,
    q: str | None = None,
    parse_status: str | None = None,
    content_type: str | None = None,
    page: int = Query(1, ge=1),
    page_size: int = Query(10, ge=1, le=100),
    session: Session = Depends(get_session),
) -> dict:
    query = session.query(AttachmentAsset)
    if mail_id:
        query = query.filter(AttachmentAsset.mail_id.ilike(f"%{mail_id.strip()}%"))
    if q and q.strip():
        pattern = f"%{q.strip()}%"
        query = query.filter(
            or_(
                AttachmentAsset.file_name.ilike(pattern),
                AttachmentAsset.mail_id.ilike(pattern),
                AttachmentAsset.content_type.ilike(pattern),
                AttachmentAsset.parse_status.ilike(pattern),
                AttachmentAsset.parse_error.ilike(pattern),
                AttachmentAsset.extracted_text.ilike(pattern),
            )
        )
    if parse_status and parse_status.strip():
        query = query.filter(AttachmentAsset.parse_status == parse_status.strip())
    if content_type and content_type.strip():
        query = query.filter(AttachmentAsset.content_type == content_type.strip())
    return page_response(
        query.order_by(AttachmentAsset.created_at.desc()),
        serialize_attachment,
        page,
        page_size,
        {
            "parse_status_options": distinct_values(session, AttachmentAsset.parse_status),
            "content_type_options": distinct_values(session, AttachmentAsset.content_type),
        },
    )


@app.post("/api/attachments/clear")
def clear_attachments(payload: AdminPasswordRequest, request: Request, session: Session = Depends(get_session)) -> dict:
    require_admin_password(payload.admin_password)
    evidence_links = (
        session.query(ExtractionEvidence)
        .filter(ExtractionEvidence.source_attachment_id.isnot(None))
        .update({ExtractionEvidence.source_attachment_id: None}, synchronize_session=False)
    )
    session.query(AttachmentAsset).update({AttachmentAsset.parent_attachment_id: None}, synchronize_session=False)
    deleted = session.query(AttachmentAsset).delete(synchronize_session=False)
    actor = getattr(request.state, "username", "system")
    session.add(
        AuditEvent(
            event_type="AttachmentsCleared",
            actor=actor,
            related_object_type="AttachmentAsset",
            related_object_id="bulk-clear",
            detail=dumps({"attachment_count": deleted, "evidence_links_cleared": evidence_links}),
            created_at=now_utc(),
        )
    )
    session.commit()
    return {"cleared": deleted, "evidence_links_cleared": evidence_links}


@app.get("/api/storage/usage")
def storage_usage_api() -> dict:
    return storage_usage()


@app.get("/api/exceptions")
def list_exceptions(
    status: str | None = "Open",
    q: str | None = None,
    severity: str | None = None,
    exception_type: str | None = None,
    page: int = Query(1, ge=1),
    page_size: int = Query(10, ge=1, le=100),
    session: Session = Depends(get_session),
) -> dict:
    query = session.query(ExceptionCase)
    if q and q.strip():
        pattern = f"%{q.strip()}%"
        query = query.filter(
            or_(
                ExceptionCase.id.ilike(pattern),
                ExceptionCase.related_task_id.ilike(pattern),
                ExceptionCase.exception_type.ilike(pattern),
                ExceptionCase.severity.ilike(pattern),
                ExceptionCase.status.ilike(pattern),
                ExceptionCase.detail.ilike(pattern),
            )
        )
    if status and status.strip() != "__all__":
        query = query.filter(ExceptionCase.status == status.strip())
    if severity and severity.strip():
        query = query.filter(ExceptionCase.severity == severity.strip())
    if exception_type and exception_type.strip():
        query = query.filter(ExceptionCase.exception_type == exception_type.strip())
    return page_response(
        query.order_by(ExceptionCase.created_at.desc()),
        serialize_exception,
        page,
        page_size,
        {
            "status_options": distinct_values(session, ExceptionCase.status),
            "severity_options": distinct_values(session, ExceptionCase.severity),
            "exception_type_options": distinct_values(session, ExceptionCase.exception_type),
        },
    )


@app.post("/api/exceptions/clear")
def clear_exceptions(payload: AdminPasswordRequest, request: Request, session: Session = Depends(get_session)) -> dict:
    require_admin_password(payload.admin_password)
    deleted = session.query(ExceptionCase).delete(synchronize_session=False)
    actor = getattr(request.state, "username", "system")
    session.add(
        AuditEvent(
            event_type="ExceptionsCleared",
            actor=actor,
            related_object_type="ExceptionCase",
            related_object_id="bulk-clear",
            detail=dumps({"exception_count": deleted}),
            created_at=now_utc(),
        )
    )
    session.commit()
    return {"cleared": deleted}


@app.get("/api/exceptions/{exception_id}")
def exception_detail(exception_id: str, session: Session = Depends(get_session)) -> dict:
    case = session.get(ExceptionCase, exception_id)
    if case is None:
        raise HTTPException(status_code=404, detail="exception not found")
    return serialize_exception(case)


@app.post("/api/exceptions/{exception_id}/resolve")
def resolve_exception(exception_id: str, payload: ExceptionResolveRequest, session: Session = Depends(get_session)) -> dict:
    try:
        case = resolve_exception_case(session, exception_id, payload.note)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    session.commit()
    return serialize_exception(case)


@app.post("/api/exceptions/{exception_id}/apply-requirement-patch")
def patch_exception_requirement(
    exception_id: str,
    payload: ExceptionRequirementPatchRequest,
    session: Session = Depends(get_session),
) -> dict:
    values = payload.model_dump(exclude={"clear_risk_flags"}, exclude_unset=True)
    try:
        task = apply_exception_requirement_patch(
            session,
            exception_id,
            values,
            clear_risk_flags=payload.clear_risk_flags,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    session.commit()
    return {"task": serialize_task(task) if task is not None else None}


@app.post("/api/model-providers/test")
def test_model_provider(session: Session = Depends(get_session)) -> dict:
    config = session.query(ModelProviderConfig).filter_by(status="Active").first()
    if config is None:
        raise HTTPException(status_code=400, detail="active model provider is not configured")
    try:
        output = call_model(
            session,
            config,
            task_type="HealthCheck",
            messages=[
                {"role": "system", "content": "你是商务生产任务单智能体。"},
                {"role": "user", "content": "请回复 JSON：{\"ok\": true}"},
            ],
        )
    except Exception as exc:
        session.commit()
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    session.commit()
    return {"ok": True, "raw": output}


@app.post("/api/model-providers/chat")
def chat_model_provider(payload: ModelChatTestRequest, session: Session = Depends(get_session)) -> dict:
    message = payload.message.strip()
    if not message:
        raise HTTPException(status_code=400, detail="message is required")
    config = session.query(ModelProviderConfig).filter_by(status="Active").first()
    if config is None:
        raise HTTPException(status_code=400, detail="active model provider is not configured")
    system_prompt = (payload.system_prompt or "你是商务生产任务单智能体，请用简洁中文回答。").strip()
    messages = []
    if system_prompt:
        messages.append({"role": "system", "content": system_prompt})
    messages.append({"role": "user", "content": message})
    try:
        output = call_model(
            session,
            config,
            task_type="ChatTest",
            messages=messages,
        )
    except Exception as exc:
        session.commit()
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    session.commit()
    return {"ok": True, "reply": extract_chat_content(output), "raw": output}


@app.get("/api/self-maintenance/context")
def self_maintenance_context(session: Session = Depends(get_session)) -> dict:
    return build_self_maintenance_context(session)


@app.post("/api/self-maintenance/diagnose")
def self_maintenance_diagnose(
    payload: SelfMaintenanceDiagnoseRequest,
    request: Request,
    session: Session = Depends(get_session),
) -> dict:
    try:
        row = create_maintenance_diagnosis(
            session,
            user_message=payload.message,
            actor=getattr(request.state, "username", "system"),
            use_llm=payload.use_llm,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    session.add(
        AuditEvent(
            event_type="SelfMaintenanceDiagnosed",
            actor=getattr(request.state, "username", "system"),
            related_object_type="MaintenanceSession",
            related_object_id=row.id,
            detail=dumps({"risk_level": row.risk_level}),
            created_at=now_utc(),
        )
    )
    session.commit()
    return serialize_maintenance_session(row, include_context=True)


@app.post("/api/self-maintenance/code-plan")
def self_maintenance_code_plan(
    payload: SelfMaintenanceCodePlanRequest,
    request: Request,
    session: Session = Depends(get_session),
) -> dict:
    try:
        row = create_code_patch_plan(
            session,
            user_message=payload.message,
            actor=getattr(request.state, "username", "system"),
            source_session_id=payload.session_id,
            use_llm=payload.use_llm,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    session.add(
        AuditEvent(
            event_type="SelfMaintenanceCodePlanCreated",
            actor=getattr(request.state, "username", "system"),
            related_object_type="MaintenanceSession",
            related_object_id=row.id,
            detail=dumps({"risk_level": row.risk_level}),
            created_at=now_utc(),
        )
    )
    session.commit()
    return self_maintenance_session_detail(row.id, session)


@app.get("/api/self-maintenance/sessions")
def list_self_maintenance_sessions(
    include_archived: bool = False,
    status: str | None = None,
    risk_level: str | None = None,
    page: int = Query(1, ge=1),
    page_size: int = Query(10, ge=1, le=100),
    session: Session = Depends(get_session),
) -> dict:
    query = session.query(MaintenanceSession)
    if not include_archived:
        query = query.filter(MaintenanceSession.status != "Archived")
    if status and status.strip():
        query = query.filter(MaintenanceSession.status == status.strip())
    if risk_level and risk_level.strip():
        query = query.filter(MaintenanceSession.risk_level == risk_level.strip())
    return page_response(
        query.order_by(MaintenanceSession.created_at.desc()),
        serialize_maintenance_session,
        page,
        page_size,
        {
            "status_options": distinct_values(session, MaintenanceSession.status),
            "risk_level_options": distinct_values(session, MaintenanceSession.risk_level),
        },
    )


@app.get("/api/self-maintenance/sessions/{session_id}")
def self_maintenance_session_detail(session_id: str, session: Session = Depends(get_session)) -> dict:
    row = session.get(MaintenanceSession, session_id)
    if row is None:
        raise HTTPException(status_code=404, detail="maintenance session not found")
    actions = (
        session.query(MaintenanceAction)
        .filter_by(session_id=session_id)
        .order_by(MaintenanceAction.created_at)
        .all()
    )
    return {
        **serialize_maintenance_session(row, include_context=True),
        "actions": [serialize_maintenance_action(action) for action in actions],
        "timeline": maintenance_session_timeline(session, session_id)["timeline"],
    }


@app.get("/api/self-maintenance/sessions/{session_id}/timeline")
def self_maintenance_session_timeline(session_id: str, session: Session = Depends(get_session)) -> dict:
    try:
        return maintenance_session_timeline(session, session_id)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@app.post("/api/self-maintenance/sessions/{session_id}/archive")
def self_maintenance_session_archive(
    session_id: str,
    payload: SelfMaintenanceSessionArchiveRequest,
    request: Request,
    session: Session = Depends(get_session),
) -> dict:
    require_admin_password(payload.admin_password)
    actor = getattr(request.state, "username", "system")
    try:
        row = archive_maintenance_session(session, session_id, note=payload.note, actor=actor)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    session.commit()
    return serialize_maintenance_session(row, include_context=True)


@app.get("/api/self-maintenance/actions")
def list_self_maintenance_actions(
    action_type: str | None = None,
    status: str | None = None,
    page: int = Query(1, ge=1),
    page_size: int = Query(10, ge=1, le=100),
    session: Session = Depends(get_session),
) -> dict:
    query = session.query(MaintenanceAction)
    if action_type and action_type.strip():
        query = query.filter(MaintenanceAction.action_type == action_type.strip())
    if status and status.strip():
        query = query.filter(MaintenanceAction.status == status.strip())
    return page_response(
        query.order_by(MaintenanceAction.created_at.desc()),
        serialize_maintenance_action,
        page,
        page_size,
        {
            "action_type_options": distinct_values(session, MaintenanceAction.action_type),
            "status_options": distinct_values(session, MaintenanceAction.status),
        },
    )


@app.get("/api/self-maintenance/actions/{action_id}")
def self_maintenance_action_detail(action_id: str, session: Session = Depends(get_session)) -> dict:
    action = session.get(MaintenanceAction, action_id)
    if action is None:
        raise HTTPException(status_code=404, detail="maintenance action not found")
    maintenance_session = session.get(MaintenanceSession, action.session_id)
    payload = serialize_maintenance_action(action)
    payload["session"] = serialize_maintenance_session(maintenance_session) if maintenance_session else None
    payload["timeline"] = maintenance_session_timeline(session, action.session_id)["timeline"] if maintenance_session else []
    return payload


@app.post("/api/self-maintenance/actions/{action_id}/apply")
def self_maintenance_action_apply(
    action_id: str,
    payload: SelfMaintenanceActionApplyRequest,
    request: Request,
    session: Session = Depends(get_session),
) -> dict:
    require_admin_password(payload.admin_password)
    actor = getattr(request.state, "username", "system")
    try:
        action = apply_maintenance_action(session, action_id, actor=actor)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    session.add(
        AuditEvent(
            event_type="SelfMaintenanceActionApplied",
            actor=actor,
            related_object_type="MaintenanceAction",
            related_object_id=action.id,
            detail=dumps({"action_type": action.action_type, "result": loads(action.result_json, {})}),
            created_at=now_utc(),
        )
    )
    session.commit()
    return serialize_maintenance_action(action)


@app.post("/api/self-maintenance/actions/{action_id}/implementation")
def self_maintenance_action_implementation(
    action_id: str,
    payload: SelfMaintenanceImplementationReportRequest,
    request: Request,
    session: Session = Depends(get_session),
) -> dict:
    require_admin_password(payload.admin_password)
    actor = getattr(request.state, "username", "system")
    try:
        action = report_maintenance_implementation(
            session,
            action_id,
            status=payload.status,
            summary=payload.summary,
            changed_files=payload.changed_files,
            tests=payload.tests,
            residual_risks=payload.residual_risks,
            actor=actor,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    session.add(
        AuditEvent(
            event_type="SelfMaintenanceImplementationReported",
            actor=actor,
            related_object_type="MaintenanceAction",
            related_object_id=action.id,
            detail=dumps({"status": action.status, "result": loads(action.result_json, {}).get("implementation", {})}),
            created_at=now_utc(),
        )
    )
    session.commit()
    return serialize_maintenance_action(action)


@app.post("/api/self-maintenance/actions/{action_id}/handoff")
def self_maintenance_action_handoff(
    action_id: str,
    payload: SelfMaintenanceHandoffRequest,
    request: Request,
    session: Session = Depends(get_session),
) -> dict:
    require_admin_password(payload.admin_password)
    actor = getattr(request.state, "username", "system")
    try:
        action = create_maintenance_handoff_package(session, action_id, actor=actor)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    session.add(
        AuditEvent(
            event_type="SelfMaintenanceHandoffCreated",
            actor=actor,
            related_object_type="MaintenanceAction",
            related_object_id=action.id,
            detail=dumps({"status": action.status, "handoff": loads(action.result_json, {}).get("handoff", {})}),
            created_at=now_utc(),
        )
    )
    session.commit()
    return serialize_maintenance_action(action)


@app.post("/api/self-maintenance/actions/{action_id}/validate")
def self_maintenance_action_validate(
    action_id: str,
    payload: SelfMaintenanceValidationRequest,
    request: Request,
    session: Session = Depends(get_session),
) -> dict:
    require_admin_password(payload.admin_password)
    actor = getattr(request.state, "username", "system")
    try:
        action = run_maintenance_validation(
            session,
            action_id,
            selected_commands=payload.commands or None,
            timeout_seconds=payload.timeout_seconds,
            actor=actor,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    session.add(
        AuditEvent(
            event_type="SelfMaintenanceValidationCompleted",
            actor=actor,
            related_object_type="MaintenanceAction",
            related_object_id=action.id,
            detail=dumps({"status": action.status, "validation": loads(action.result_json, {}).get("validation", {})}),
            created_at=now_utc(),
        )
    )
    session.commit()
    return serialize_maintenance_action(action)


@app.get("/api/self-maintenance/actions/{action_id}/handoff")
def self_maintenance_action_handoff_detail(
    action_id: str,
    session: Session = Depends(get_session),
) -> dict:
    try:
        return read_maintenance_handoff_package(session, action_id)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.post("/api/self-maintenance/actions/{action_id}/review")
def self_maintenance_action_review(
    action_id: str,
    payload: SelfMaintenanceReviewRequest,
    request: Request,
    session: Session = Depends(get_session),
) -> dict:
    require_admin_password(payload.admin_password)
    actor = getattr(request.state, "username", "system")
    try:
        action = review_maintenance_implementation(
            session,
            action_id,
            decision=payload.decision,
            note=payload.note,
            actor=actor,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    session.add(
        AuditEvent(
            event_type="SelfMaintenanceImplementationReviewed",
            actor=actor,
            related_object_type="MaintenanceAction",
            related_object_id=action.id,
            detail=dumps({"status": action.status, "review": loads(action.result_json, {}).get("review", {})}),
            created_at=now_utc(),
        )
    )
    session.commit()
    return serialize_maintenance_action(action)


@app.get("/api/departments")
def list_departments(
    q: str | None = None,
    status: str | None = None,
    page: int = Query(1, ge=1),
    page_size: int = Query(10, ge=1, le=100),
    session: Session = Depends(get_session),
) -> dict:
    query = session.query(ProductionDepartment)
    if q and q.strip():
        pattern = f"%{q.strip()}%"
        query = query.filter(
            or_(
                ProductionDepartment.department_code.ilike(pattern),
                ProductionDepartment.department_name.ilike(pattern),
                ProductionDepartment.mail_to_json.ilike(pattern),
                ProductionDepartment.mail_cc_json.ilike(pattern),
                ProductionDepartment.status.ilike(pattern),
            )
        )
    if status and status.strip():
        query = query.filter(ProductionDepartment.status == status.strip())
    else:
        query = query.filter(ProductionDepartment.status != "Deleted")
    return page_response(
        query.order_by(ProductionDepartment.department_code),
        serialize_department,
        page,
        page_size,
        {
            "status_options": [
                value
                for value in distinct_values(session, ProductionDepartment.status)
                if value != "Deleted"
            ]
        },
    )


def save_production_department(payload: DepartmentUpsert, session: Session) -> dict:
    department_code = str(payload.department_code or "").strip()
    department_name = str(payload.department_name or "").strip()
    if not department_code:
        raise HTTPException(status_code=400, detail="部门编码必填")
    if not department_name:
        raise HTTPException(status_code=400, detail="部门名称必填")
    mail_to = normalize_email_values(payload.mail_to)
    mail_cc = normalize_email_values(payload.mail_cc)
    invalid_to = invalid_email_addresses(mail_to)
    invalid_cc = invalid_email_addresses(mail_cc)
    if invalid_to:
        raise HTTPException(status_code=400, detail=f"主送邮箱格式不合法：{', '.join(invalid_to)}")
    if invalid_cc:
        raise HTTPException(status_code=400, detail=f"抄送邮箱格式不合法：{', '.join(invalid_cc)}")
    dept = session.query(ProductionDepartment).filter_by(department_code=department_code).one_or_none()
    if dept is None:
        dept = ProductionDepartment(department_code=department_code)
        session.add(dept)
    dept.department_name = department_name
    dept.mail_to_json = dumps(mail_to)
    dept.mail_cc_json = dumps(mail_cc)
    dept.status = "Active"
    session.commit()
    return {"ok": True, "department_id": dept.id}


@app.post("/api/departments")
def create_or_update_department(payload: DepartmentUpsert, session: Session = Depends(get_session)) -> dict:
    return save_production_department(payload, session)


@app.delete("/api/departments/{department_id}")
def delete_department(department_id: str, request: Request, session: Session = Depends(get_session)) -> dict:
    dept = session.get(ProductionDepartment, department_id)
    if dept is None or dept.status == "Deleted":
        raise HTTPException(status_code=404, detail="production department not found")
    dept.status = "Deleted"
    dept.updated_at = now_utc()
    session.flush()
    bot_was_enabled = system_config_bool(session, "bot_enabled", False)
    readiness = runtime_startup_readiness(session)
    bot_disabled = False
    if bot_was_enabled and not readiness["ready"]:
        set_config(session, "bot_enabled", "false", is_secret=False)
        bot_disabled = True
    actor = getattr(request.state, "username", "system")
    session.add(
        AuditEvent(
            event_type="ProductionDepartmentDeleted",
            actor=actor,
            related_object_type="ProductionDepartment",
            related_object_id=dept.id,
            detail=dumps(
                {
                    "department_code": dept.department_code,
                    "department_name": dept.department_name,
                    "bot_disabled": bot_disabled,
                    "startup_missing": readiness["missing"],
                }
            ),
            created_at=now_utc(),
        )
    )
    session.commit()
    return {"ok": True, "department": serialize_department(dept), "bot_disabled": bot_disabled}


@app.put("/api/departments/default")
def upsert_default_department(payload: DepartmentUpsert, session: Session = Depends(get_session)) -> dict:
    return save_production_department(payload, session)


@app.get("/api/templates/production-task")
def get_task_template(session: Session = Depends(get_session)) -> dict:
    template = session.query(MailTemplate).filter_by(template_code="production_task", status="Active").first()
    if template is None:
        raise HTTPException(status_code=404, detail="template not found")
    return serialize_template(template)


@app.put("/api/templates/production-task")
def update_task_template(payload: TemplateUpdate, session: Session = Depends(get_session)) -> dict:
    latest_count = session.query(MailTemplate).filter_by(template_code="production_task").count()
    template = MailTemplate(
        template_code="production_task",
        template_name="生产任务单模板",
        template_type="TaskIssue",
        subject_template=payload.subject_template,
        body_template=payload.body_template,
        uploaded_asset_ref=payload.uploaded_asset_ref,
        version=f"v{latest_count + 1}",
    )
    session.query(MailTemplate).filter_by(template_code="production_task", status="Active").update({"status": "Disabled"})
    session.add(template)
    session.commit()
    return serialize_template(template)


@app.get("/api/reports/weekly")
def report(session: Session = Depends(get_session)) -> dict:
    return weekly_report(session)


@app.get("/api/reports/weekly/preview")
def weekly_report_preview(session: Session = Depends(get_session)) -> dict:
    report_data = weekly_report(session)
    generated_at = datetime.fromisoformat(report_data["generated_at"])
    recipients = weekly_report_recipients(session)
    return {
        "generated_at": report_data["generated_at"],
        "generated_at_label": report_data.get("generated_at_label", report_data["generated_at"]),
        "subject": weekly_report_subject(generated_at),
        "body": weekly_report_mail_body(report_data),
        "to": recipients["to"],
        "cc": recipients["cc"],
        "reporting_period": report_data.get("reporting_period"),
        "periods": report_data["periods"],
    }


@app.get("/api/reports/weekly/recipients")
def weekly_report_recipient_config(session: Session = Depends(get_session)) -> dict:
    return weekly_report_recipients(session)


@app.put("/api/reports/weekly/recipients")
def update_weekly_report_recipients(payload: WeeklyReportRecipientsUpdate, session: Session = Depends(get_session)) -> dict:
    recipients = set_weekly_report_recipients(
        session,
        [str(email) for email in payload.to],
        [str(email) for email in payload.cc],
    )
    session.commit()
    return recipients


@app.post("/api/reports/weekly/enqueue")
def enqueue_report(session: Session = Depends(get_session)) -> dict:
    try:
        job = enqueue_weekly_report(session, force_new=True)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    session.commit()
    return {"outbound_job_id": job.id, "status": job.status}


@app.get("/api/reports/weekly/export.pdf")
def report_pdf(session: Session = Depends(get_session)) -> Response:
    report_data = weekly_report(session)
    lines = [
        "公司抬头：积木易搭",
        "页眉：商务生产任务单周报",
        "签章：公司签章占位",
        "",
        *weekly_report_mail_body(report_data).splitlines(),
    ]
    return Response(simple_pdf("商务生产任务单周报", lines), media_type="application/pdf")


@app.get("/api/reports/weekly/export.csv")
def report_csv(session: Session = Depends(get_session)) -> Response:
    return Response(
        weekly_report_csv(session),
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": 'attachment; filename="weekly-production-tasks.csv"'},
    )


@app.post("/api/cleanup/preview")
def cleanup_preview_api(session: Session = Depends(get_session)) -> dict:
    result = cleanup_preview(session)
    session.commit()
    return result


@app.post("/api/cleanup/run")
def cleanup_run_api(cleanup_job_id: str | None = None, session: Session = Depends(get_session)) -> dict:
    try:
        result = execute_cleanup(session, cleanup_job_id)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    session.commit()
    return result


@app.get("/api/backups")
def backups(
    q: str | None = None,
    status: str | None = None,
    backup_type: str | None = None,
    page: int = Query(1, ge=1),
    page_size: int = Query(10, ge=1, le=100),
    session: Session = Depends(get_session),
) -> dict:
    query = session.query(BackupJob)
    if q and q.strip():
        pattern = f"%{q.strip()}%"
        query = query.filter(
            or_(
                BackupJob.id.ilike(pattern),
                BackupJob.backup_type.ilike(pattern),
                BackupJob.status.ilike(pattern),
                BackupJob.storage_ref.ilike(pattern),
                BackupJob.manifest_json.ilike(pattern),
            )
        )
    if status and status.strip():
        query = query.filter(BackupJob.status == status.strip())
    if backup_type and backup_type.strip():
        query = query.filter(BackupJob.backup_type == backup_type.strip())
    return page_response(
        query.order_by(BackupJob.created_at.desc()),
        serialize_backup,
        page,
        page_size,
        {
            "status_options": distinct_values(session, BackupJob.status),
            "backup_type_options": distinct_values(session, BackupJob.backup_type),
        },
    )


@app.post("/api/backups/run")
def run_backup(session: Session = Depends(get_session)) -> dict:
    job = create_backup(session)
    session.commit()
    return {"backup_job_id": job.id, "status": job.status, "storage_ref": job.storage_ref}


@app.post("/api/backups/clear")
def clear_backups(payload: AdminPasswordRequest, request: Request, session: Session = Depends(get_session)) -> dict:
    require_admin_password(payload.admin_password)
    deleted = session.query(BackupJob).delete(synchronize_session=False)
    actor = getattr(request.state, "username", "system")
    session.add(
        AuditEvent(
            event_type="BackupsCleared",
            actor=actor,
            related_object_type="BackupJob",
            related_object_id="bulk-clear",
            detail=dumps({"backup_count": deleted}),
            created_at=now_utc(),
        )
    )
    session.commit()
    return {"cleared": deleted}


@app.get("/api/audit-events")
def audit_events(
    q: str | None = None,
    event_type: str | None = None,
    actor: str | None = None,
    related_object_type: str | None = None,
    page: int = Query(1, ge=1),
    page_size: int = Query(10, ge=1, le=100),
    session: Session = Depends(get_session),
) -> dict:
    query = session.query(AuditEvent)
    if q and q.strip():
        pattern = f"%{q.strip()}%"
        query = query.filter(
            or_(
                AuditEvent.id.ilike(pattern),
                AuditEvent.event_type.ilike(pattern),
                AuditEvent.actor.ilike(pattern),
                AuditEvent.related_object_type.ilike(pattern),
                AuditEvent.related_object_id.ilike(pattern),
                AuditEvent.detail.ilike(pattern),
            )
        )
    if event_type and event_type.strip():
        query = query.filter(AuditEvent.event_type == event_type.strip())
    if actor and actor.strip():
        query = query.filter(AuditEvent.actor.ilike(f"%{actor.strip()}%"))
    if related_object_type and related_object_type.strip():
        query = query.filter(AuditEvent.related_object_type == related_object_type.strip())
    return page_response(
        query.order_by(AuditEvent.created_at.desc()),
        serialize_audit_event,
        page,
        page_size,
        {
            "event_type_options": distinct_values(session, AuditEvent.event_type),
            "actor_options": distinct_values(session, AuditEvent.actor),
            "related_object_type_options": distinct_values(session, AuditEvent.related_object_type),
        },
    )


@app.post("/api/audit-events/clear")
def clear_audit_events(payload: AdminPasswordRequest, session: Session = Depends(get_session)) -> dict:
    require_admin_password(payload.admin_password)
    deleted = session.query(AuditEvent).delete(synchronize_session=False)
    session.commit()
    return {"cleared": deleted}


def serialize_department(row: ProductionDepartment) -> dict:
    return {
        "id": row.id,
        "department_code": row.department_code,
        "department_name": row.department_name,
        "mail_to": as_list(row.mail_to_json),
        "mail_cc": as_list(row.mail_cc_json),
        "status": row.status,
    }


def serialize_outbound_mail(row: OutboundMailJob, session: Session | None = None, *, include_body: bool = False) -> dict:
    payload = {
        "id": row.id,
        "mail_type": row.mail_type,
        "to": as_list(row.to_json),
        "cc": as_list(row.cc_json),
        "subject": row.subject,
        "status": row.status,
        "created_at": row.created_at.isoformat(),
        "pending_diagnosis": outbound_pending_diagnosis(session, row) if session is not None else None,
    }
    if include_body:
        payload.update(
            {
                "body": row.body,
                "related_task_id": row.related_task_id,
                "related_version_id": row.related_version_id,
                "idempotency_key": row.idempotency_key,
            }
        )
    return payload


def serialize_processing_job(row: ProcessingJob) -> dict:
    return {
        "id": row.id,
        "job_type": row.job_type,
        "status": row.status,
        "attempt_count": row.attempt_count,
        "error_message": row.error_message,
        "created_at": row.created_at.isoformat(),
    }


def serialize_backup(row: BackupJob) -> dict:
    return {
        "id": row.id,
        "backup_type": row.backup_type,
        "status": row.status,
        "storage_ref": row.storage_ref,
        "manifest": loads(row.manifest_json, {}),
        "created_at": row.created_at.isoformat(),
    }


def serialize_audit_event(row: AuditEvent) -> dict:
    return {
        "id": row.id,
        "event_type": row.event_type,
        "actor": row.actor,
        "related_object_type": row.related_object_type,
        "related_object_id": row.related_object_id,
        "detail": loads(row.detail, {}),
        "created_at": row.created_at.isoformat(),
    }


def serialize_template(template: MailTemplate) -> dict:
    return {
        "id": template.id,
        "template_code": template.template_code,
        "template_name": template.template_name,
        "subject_template": template.subject_template,
        "body_template": template.body_template,
        "uploaded_asset_ref": template.uploaded_asset_ref,
        "version": template.version,
        "status": template.status,
    }


def serialize_workflow_version(row: WorkflowVersion) -> dict:
    rules = loads(row.compiled_rules_json, {})
    if not isinstance(rules, dict):
        rules = {}
    return {
        "id": row.id,
        "workflow_id": row.workflow_id,
        "workflow_code": rules.get("workflow_code"),
        "workflow_name": rules.get("workflow_name"),
        "version_no": row.version_no,
        "status": row.status,
        "created_by": row.created_by,
        "approved_by": row.approved_by,
        "source_asset_ref": row.source_asset_ref,
        "compiled_rules": rules,
        "created_at": row.created_at.isoformat(),
        "approved_at": row.approved_at.isoformat() if row.approved_at else None,
    }


def serialize_workflow_import_job(row: WorkflowImportJob) -> dict:
    return {
        "id": row.id,
        "file_name": row.file_name,
        "source_asset_ref": row.source_asset_ref,
        "parse_status": row.parse_status,
        "status": row.status,
        "validation_errors": loads(row.validation_errors_json, []),
        "diff": loads(row.diff_json, []),
        "created_at": row.created_at.isoformat(),
        "updated_at": row.updated_at.isoformat(),
    }


def serialize_requirement_workflow_binding(row: RequirementWorkflowBinding) -> dict:
    return {
        "id": row.id,
        "workflow_version_id": row.workflow_version_id,
        "workflow_code": row.workflow_code,
        "workflow_name": row.workflow_name,
        "match_confidence": row.match_confidence,
        "route_to": as_list(row.route_to_json),
        "route_cc": as_list(row.route_cc_json),
        "required_fields": as_list(row.required_fields_json),
        "required_attachments": as_list(row.required_attachments_json),
        "missing_fields": as_list(row.missing_fields_json),
        "unresolved_contacts": as_list(row.unresolved_contacts_json),
    }


def serialize_requirement_summary(row: OrderRequirement) -> dict:
    return {
        "id": row.id,
        "source_mail_id": row.source_mail_id,
        "internal_order_no": row.internal_order_no,
        "external_order_no": row.external_order_no,
        "customer_name": row.customer_name,
        "salesperson_name": row.salesperson_name,
        "salesperson_email": row.salesperson_email,
        "product_summary": row.product_summary,
        "quantity_text": row.quantity_text,
        "expected_delivery_date": row.expected_delivery_date,
        "missing_fields": as_list(row.missing_fields_json),
        "risk_flags": as_list(row.risk_flags_json),
        "status": row.status,
        "created_at": row.created_at.isoformat(),
    }


def serialize_mail(row: MailMessage) -> dict:
    return {
        "id": row.id,
        "direction": row.direction,
        "from_address": row.from_address,
        "to": as_list(row.to_json),
        "cc": as_list(row.cc_json),
        "subject": row.subject,
        "classification": row.classification,
        "classification_confidence": row.classification_confidence,
        "related_task_id": row.related_task_id,
        "created_at": row.created_at.isoformat(),
    }


def serialize_attachment(row: AttachmentAsset) -> dict:
    return {
        "id": row.id,
        "mail_id": row.mail_id,
        "parent_attachment_id": row.parent_attachment_id,
        "file_name": row.file_name,
        "content_type": row.content_type,
        "file_size": row.file_size,
        "file_hash": row.file_hash,
        "storage_ref": row.storage_ref,
        "parse_status": row.parse_status,
        "archive_path": row.archive_path,
        "archive_depth": row.archive_depth,
        "parse_error": row.parse_error,
        "text_preview": (row.extracted_text or "")[:300],
        "created_at": row.created_at.isoformat(),
    }


def serialize_task(task: ProductionTask, include_versions: bool = False) -> dict:
    data = {
        "id": task.id,
        "task_no": task.task_no,
        "status": task.status,
        "customer_name": task.requirement.customer_name,
        "external_order_no": task.requirement.external_order_no,
        "salesperson_email": task.requirement.salesperson_email,
        "product_summary": task.requirement.product_summary,
        "quantity_text": task.requirement.quantity_text,
        "expected_delivery_date": task.requirement.expected_delivery_date,
        "target_mail_to": as_list(task.target_mail_to_json),
        "target_mail_cc": as_list(task.target_mail_cc_json),
        "created_at": task.created_at.isoformat(),
    }
    if include_versions:
        data["versions"] = [
            {
                "id": version.id,
                "version_no": version.version_no,
                "subject": version.subject,
                "body": version.body,
                "status": version.status,
            }
            for version in task.versions
        ]
        data["questions"] = [
            serialize_question(row)
            for row in sessionless_sorted_questions(task)
        ]
    return data


def sessionless_sorted_questions(task: ProductionTask) -> list[QuestionAndReply]:
    questions = getattr(task, "questions", None)
    if questions is None:
        return []
    return sorted(questions, key=lambda row: row.created_at, reverse=True)


def serialize_question(row: QuestionAndReply) -> dict:
    return {
        "id": row.id,
        "task_id": row.task_id,
        "production_question_mail_id": row.production_question_mail_id,
        "sales_reply_mail_id": row.sales_reply_mail_id,
        "question_text": row.question_text,
        "reply_text": row.reply_text,
        "status": row.status,
        "created_at": row.created_at.isoformat(),
        "updated_at": row.updated_at.isoformat(),
    }


def serialize_evidence(row: ExtractionEvidence) -> dict:
    return {
        "id": row.id,
        "requirement_id": row.requirement_id,
        "field_name": row.field_name,
        "field_value": row.field_value,
        "source_type": row.source_type,
        "source_mail_id": row.source_mail_id,
        "source_attachment_id": row.source_attachment_id,
        "evidence_text": row.evidence_text,
        "confidence": row.confidence,
        "created_at": row.created_at.isoformat(),
    }


def serialize_exception(row: ExceptionCase) -> dict:
    detail = loads(row.detail, {})
    return {
        "id": row.id,
        "related_task_id": row.related_task_id,
        "exception_type": row.exception_type,
        "severity": row.severity,
        "detail": detail,
        "detail_text": row.detail,
        "status": row.status,
        "created_at": row.created_at.isoformat(),
    }


STATIC_DIR = Path(__file__).parent / "static"
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")


@app.get("/")
def index() -> FileResponse:
    return FileResponse(STATIC_DIR / "index.html")
# ==========================================
# Product Management APIs
# ==========================================

@app.get("/api/products/spu")
def list_products_spu_api(
    q: str = Query("", description="搜索 SPU 代码或名称"),
    page: int = Query(1, ge=1),
    page_size: int = Query(10, ge=1, le=100),
    session: Session = Depends(get_session)
) -> dict:
    skip = (page - 1) * page_size
    items, total = get_spus(session, skip=skip, limit=page_size, query=q)
    return {
        "items": [
            {
                "id": spu.id,
                "spu_id": spu.spu_id,
                "name": spu.name,
                "brand": spu.brand,
                "category": spu.category,
                "created_at": spu.created_at.isoformat(),
            } for spu in items
        ],
        "total": total,
        "page": page,
        "page_size": page_size,
        "total_pages": max(1, (total + page_size - 1) // page_size)
    }

@app.post("/api/products/spu")
def create_product_spu_api(payload: ProductSPUCreate, session: Session = Depends(get_session)) -> dict:
    spu = create_spu(session, spu_id=payload.spu_id, name=payload.name, brand=payload.brand, category=payload.category)
    session.commit()
    return {"id": spu.id, "spu_id": spu.spu_id}


import tempfile
import os
from fastapi import UploadFile, File
from backend.app.services.excel_import import preview_excel_import, confirm_excel_import

@app.post("/api/products/import/preview")
def api_preview_product_import(file: UploadFile = File(...), session: Session = Depends(get_session)) -> dict:
    with tempfile.NamedTemporaryFile(delete=False, suffix=".xlsx") as tmp:
        contents = file.file.read()
        tmp.write(contents)
        tmp_path = tmp.name
    
    try:
        preview = preview_excel_import(tmp_path, session)
    finally:
        os.remove(tmp_path)
    
    return preview

@app.post("/api/products/import/confirm")
def api_confirm_product_import(data: dict, session: Session = Depends(get_session)) -> dict:
    counts = confirm_excel_import(data, session)
    session.commit()
    return {"message": "导入成功", "counts": counts}


@app.get("/api/products/sku")
def list_products_sku_api(
    spu_id: str = Query(None, description="所属 SPU ID (Code)"),
    spu_uuid: str = Query(None, description="所属 SPU UUID"),
    page: int = Query(1, ge=1),
    page_size: int = Query(10, ge=1, le=100),
    session: Session = Depends(get_session)
) -> dict:
    skip = (page - 1) * page_size
    items, total = get_skus(session, skip=skip, limit=page_size, spu_id=spu_id, spu_uuid=spu_uuid)
    return {
        "items": [
            {
                "id": sku.id,
                "spu_id": sku.spu.spu_id,
                "sku_id": sku.sku_id,
                "status": sku.status,
                "attributes": loads(sku.attributes_json, {}),
                "created_at": sku.created_at.isoformat(),
            } for sku in items
        ],
        "total": total,
        "page": page,
        "page_size": page_size,
        "total_pages": max(1, (total + page_size - 1) // page_size)
    }

@app.post("/api/products/sku")
def create_product_sku_api(payload: ProductSKUCreate, session: Session = Depends(get_session)) -> dict:
    sku = create_sku(session, spu_uuid=payload.spu_uuid, sku_id=payload.sku_id, attributes=payload.attributes)
    session.commit()
    return {"id": sku.id, "sku_id": sku.sku_id}


@app.get("/api/pricing")
def list_channel_pricing_api(
    sku_id: str = Query(None, description="按 SKU ID (Code) 筛选"),
    sku_uuid: str = Query(None, description="按 SKU UUID 筛选"),
    page: int = Query(1, ge=1),
    page_size: int = Query(10, ge=1, le=100),
    session: Session = Depends(get_session)
) -> dict:
    skip = (page - 1) * page_size
    items, total = get_channel_pricing(session, skip=skip, limit=page_size, sku_id=sku_id, sku_uuid=sku_uuid)
    return {
        "items": [
            {
                "id": p.id,
                "sku_id": p.sku.sku_id,
                "channel": p.channel,
                "tier_a_price": p.tier_a_price,
                "tier_b_price": p.tier_b_price,
                "tier_c_price": p.tier_c_price,
                "map_price": p.map_price,
                "promo_start_time": p.promo_start_time.isoformat() if p.promo_start_time else None,
                "promo_end_time": p.promo_end_time.isoformat() if p.promo_end_time else None,
                "currency": p.currency,
                "updated_at": p.updated_at.isoformat() if p.updated_at else None,
            } for p in items
        ],
        "total": total,
        "page": page,
        "page_size": page_size,
        "total_pages": max(1, (total + page_size - 1) // page_size)
    }

@app.post("/api/pricing")
def set_channel_pricing_api(payload: ChannelPricingUpdate, session: Session = Depends(get_session)) -> dict:
    pricing = set_channel_pricing(
        session,
        sku_uuid=payload.sku_uuid,
        channel=payload.channel,
        tier_a_price=payload.tier_a_price,
        tier_b_price=payload.tier_b_price,
        tier_c_price=payload.tier_c_price,
        map_price=payload.map_price,
        promo_start_time=payload.promo_start_time,
        promo_end_time=payload.promo_end_time,
        currency=payload.currency
    )
    session.commit()
    return {"id": pricing.id, "sku_uuid": pricing.sku_uuid, "channel": pricing.channel}


@app.get("/api/promotions")
def list_promotions_api(
    page: int = Query(1, ge=1),
    page_size: int = Query(10, ge=1, le=100),
    session: Session = Depends(get_session)
) -> dict:
    skip = (page - 1) * page_size
    items, total = get_promotions(session, skip=skip, limit=page_size)
    return {
        "items": [
            {
                "id": rule.id,
                "name": rule.name,
                "channel": rule.channel,
                "is_active": rule.is_active,
                "start_time": rule.start_time.isoformat() if rule.start_time else None,
                "end_time": rule.end_time.isoformat() if rule.end_time else None,
                "priority": rule.priority,
                "discount_type": rule.discount_type,
                "discount_value": rule.discount_value,
                "created_at": rule.created_at.isoformat(),
            } for rule in items
        ],
        "total": total,
        "page": page,
        "page_size": page_size,
        "total_pages": max(1, (total + page_size - 1) // page_size)
    }

@app.post("/api/promotions")
def create_promotion_api(payload: PromotionRuleCreate, session: Session = Depends(get_session)) -> dict:
    try:
        rule = create_promotion_rule(
            session,
            name=payload.name,
            discount_type=payload.discount_type,
            discount_value=payload.discount_value,
            channel=payload.channel,
            start_time=payload.start_time,
            end_time=payload.end_time,
            priority=payload.priority
        )
        res = {"id": rule.id, "name": rule.name}
        session.commit()
        return res
    except Exception as e:
        session.rollback()
        logger.exception("Error in create_promotion_api")
        raise HTTPException(status_code=500, detail=str(e))

@app.patch("/api/promotions/{rule_id}")
def update_promotion_api(rule_id: str, payload: PromotionRuleUpdate, session: Session = Depends(get_session)) -> dict:
    try:
        rule = update_promotion_rule(session, rule_id, **payload.dict(exclude_unset=True))
        if not rule:
            raise HTTPException(status_code=404, detail="Promotion rule not found")
        res = {"id": rule.id, "name": rule.name}
        session.commit()
        return res
    except Exception as e:
        session.rollback()
        logger.exception("Error in update_promotion_api")
        raise HTTPException(status_code=500, detail=str(e))

@app.delete("/api/promotions/{rule_id}")
def delete_promotion_api(rule_id: str, session: Session = Depends(get_session)) -> dict:
    try:
        success = delete_promotion_rule(session, rule_id)
        if not success:
            raise HTTPException(status_code=404, detail="Promotion rule not found")
        session.commit()
        return {"success": True}
    except Exception as e:
        session.rollback()
        logger.exception("Error in delete_promotion_api")
        raise HTTPException(status_code=500, detail=str(e))

@app.post("/api/promotions/{rule_id}/toggle")
def toggle_promotion_api(rule_id: str, is_active: bool = Query(...), session: Session = Depends(get_session)) -> dict:
    try:
        rule = toggle_promotion_rule(session, rule_id, is_active)
        if not rule:
            raise HTTPException(status_code=404, detail="Promotion rule not found")
        res = {"id": rule.id, "is_active": rule.is_active}
        session.commit()
        return res
    except Exception as e:
        session.rollback()
        logger.exception("Error in toggle_promotion_api")
        raise HTTPException(status_code=500, detail=str(e))
