from __future__ import annotations

import asyncio
import base64
import binascii
import contextlib
import csv
import hmac
import json
import logging
import os
import re
import subprocess
import threading
import time
import uuid
from datetime import datetime, timedelta
from pathlib import Path
from io import StringIO

from fastapi import Depends, FastAPI, HTTPException, Query, Request
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse, Response, StreamingResponse
from fastapi.staticfiles import StaticFiles
from sqlalchemy import func, or_, text
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session, object_session

from backend.app.config import settings
from backend.app.database import SessionLocal, database_runtime_info, init_db
from backend.app.models import (
    AgentRunLog,
    AuditEvent,
    AttachmentAsset,
    BackupJob,
    CrmOrderItem,
    CrmOrderSnapshot,
    CrmSalesOrder,
    CrmSyncRun,
    OrderAttachment,
    ExceptionCase,
    ExtractionEvidence,
    FulfillmentItem,
    IntegrationEvent,
    LogisticsDepartment,
    LogisticsTask,
    LogisticsTaskVersion,
    MailMessage,
    MailWorkflowMatch,
    MailTemplate,
    ModelCallLog,
    ModelProviderConfig,
    DeliveryNotice,
    MiddlePlatformOrder,
    MiddlePlatformOrderItem,
    OrderRequirement,
    OutboundMailJob,
    ProcessingJob,
    ProductionDepartment,
    ProductSKU,
    QuestionAndReply,
    RequirementWorkflowBinding,
    ProductionTask,
    ProductionTaskVersion,
    SystemConfig,
    User,
    WorkflowDefinition,
    WorkflowImportJob,
    WorkflowVersion,
    now_utc,
    # V2 Phase 1 新增模型
    EntityMapping, CustomerEntityMapping, ProductPrice,
    MailReceiverConfig, OrderSequence, InterEntityTransfer,
    InventoryImportRecord, InventorySnapshotHistory,
    ProductInventorySnapshot, WarehouseEntityMapping,
    MaterialEntityException,
)
from backend.app.schemas import (
    AdminPasswordRequest,
    DemoOrderRequest,
    DepartmentUpsert,
    CrmRuntimeConfigUpdate,
    ErpBillQueryRequest,
    ErpRuntimeConfigUpdate,
    ExceptionRequirementPatchRequest,
    ExceptionAssignRequest,
    ExceptionReopenRequest,
    ExceptionResolveRequest,
    InitialReviewConfigUpdate,
    LoginRequest,
    MailRuntimeConfigUpdate,
    ModelChatTestRequest,
    ModelProviderUpdate,
    OmsRuntimeConfigUpdate,
    OutboundBulkCancelRequest,
    V2ReviewRulesUpdate,
    ProductionFeedbackRequest,
    ProductionQuestionRequest,
    TaskClearRequest,
    SalesReplyRequest,
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
    DeliveryNoticeConfirmRequest,
    PromotionRuleCreate,
    PromotionRuleUpdate,
)
from backend.app.services.crm_attachment_cache import local_storage_ref
from backend.app.config import MAIL_LOGIN_MIN_INTERVAL_SECONDS, MAIL_WORKER_MIN_INTERVAL_SECONDS
from backend.app.services.auth import COOKIE_NAME, create_session_token, parse_session_token, verify_password, should_mask_financials
from backend.app.services.bootstrap import seed_defaults, set_config
from backend.app.services.crm_sync import (
    CrmSyncBusyError,
    crm_order_summary,
    force_sync_crm_order_by_no,
    queue_crm_order_sync,
    retry_crm_order_detail_sync,
    run_crm_integration_test,
    run_crm_sales_order_sync,
    serialize_sync_run,
)
from backend.app.services.e2e_mail import run_tencent_mail_e2e
from backend.app.services.erp.business_queries import (
    inventory_classification_diagnostics,
    list_inventory_warehouses as list_inventory_warehouse_options,
    list_inventory_snapshots,
    list_inventory_type_items,
    list_inventory_type_summary,
    query_inventory,
    save_inventory_classification_rules,
    search_materials,
    sync_inventory_snapshots,
)
from backend.app.services.erp.kingdee_client import execute_bill_query_from_config, normalize_kingdee_server_url, test_kingdee_connection_from_config, test_kingdee_write_permissions_from_config
from backend.app.services.erp.material_sync import sync_erp_materials
from backend.app.services.exception_diagnosis import diagnose_exception_case, enqueue_exception_diagnosis
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
from backend.app.services.rules import DEFAULT_RULES, V2_REVIEW_RULE_STATES_KEY, review_rule_config
from backend.app.services.mail_adapter import AUTO_WORKFLOW_MAIL_TYPES, backfill_mail_received_at, send_pending_smtp, sync_imap_mailbox
from backend.app.services.mail_worker import configured_mail_worker_interval_seconds, get_mail_worker_status, run_mail_auto_worker_once
from backend.app.services.mail_throttle import clamp_mail_interval_seconds
from backend.app.services.model_provider import call_model, extract_chat_content, resolve_api_key
from backend.app.services.operations import cleanup_preview, create_backup, execute_cleanup, storage_usage, weekly_report_csv
from backend.app.services.time_utils import format_beijing_time, to_beijing_time
from backend.app.services.order_middle_platform import (
    confirm_delivery_notice,
    enqueue_crm_order_parsed_event,
    enqueue_oms_push,
    ensure_middle_order_business_fields,
    ExceptionType,
    list_middle_orders,
    OrderStatus,
    order_dashboard,
    poll_oms_status_updates,
    process_crm_order_parsed_event,
    process_erp_billing,
    retry_erp_billing,
    process_oms_status_update,
    serialize_middle_order,
)
from backend.app.services.oms.jackyun_client import JackyunConfigError, jackyun_client_from_session
from backend.app.services.pdf import simple_pdf
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
    get_config,
    apply_exception_requirement_patch,
    process_inbound_mail,
    recipient_hash,
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


def self_maintenance_action_detail(action_id: str, session: Session) -> dict:
    from backend.app.models import MaintenanceAction
    from backend.app.services.self_maintenance import maintenance_session_timeline

    action = session.get(MaintenanceAction, action_id)
    if action is None:
        raise HTTPException(status_code=404, detail="maintenance action not found")
    payload = maintenance_session_timeline(session, action.session_id)
    result = loads(action.result_json, {})
    input_payload = loads(action.input_json, {})
    return {
        "id": action.id,
        "session_id": action.session_id,
        "action_type": action.action_type,
        "status": action.status,
        "input": input_payload,
        "result": result,
        "runner_commands": input_payload.get("runner_commands") or input_payload.get("validation_commands") or [],
        "session": payload["session"],
        "timeline": payload["timeline"],
    }
from backend.app.services.products import (
    create_spu,
    create_sku,
    set_channel_pricing,
    create_promotion_rule,
    get_spus,
    get_skus,
    get_channel_pricing,
    get_promotions,
    promotion_rule_binding_info,
    extract_order_products_for_review,
    review_order_products,
    product_review_readiness,
    match_sku_by_product_name,
    spu_review_aliases,
    suggest_product_review_candidates,
    update_spu_review_aliases,
    update_promotion_rule,
    delete_promotion_rule,
    toggle_promotion_rule,
    preview_alias_import_from_excel,
    confirm_alias_import_from_excel,
)
from backend.app.services.skills.registry import registry
from backend.app.services.skills.factory import SkillFactory

PUBLIC_API_PATHS = {"/api/auth/login", "/api/auth/logout", "/api/auth/me"}
logger = logging.getLogger(__name__)


_secrets_cache = []
_last_cache_time = 0.0
_crm_browser_process: subprocess.Popen | None = None
_crm_browser_meta: dict = {}

def get_secret_values() -> list[str]:
    global _secrets_cache, _last_cache_time
    import time
    now = time.time()
    if now - _last_cache_time < 30.0:
        return _secrets_cache
    try:
        from backend.app.database import SessionLocal
        from backend.app.models import SystemConfig
        with SessionLocal() as session:
            rows = session.query(SystemConfig).filter_by(is_secret=True).all()
            _secrets_cache = [
                row.value for row in rows 
                if row.value and len(row.value) >= 5 and not row.value.startswith("enc:")
            ]
            _last_cache_time = now
    except Exception:
        pass
    return _secrets_cache


class SensitiveDataFormatter(logging.Formatter):
    def __init__(self, fmt=None, datefmt=None, style='%', secrets_getter=None):
        super().__init__(fmt, datefmt, style)
        self.secrets_getter = secrets_getter
        self.sensitive_patterns = [
            r"(?i)(password|passwd|api_key|app_secret|auth_secret|api_key_str)\s*[:=]\s*['\"]([^'\"]+)['\"]",
            r"(?i)(bot_email_password|e2e_sales_password|e2e_production_password|erp_app_sec|crm_password|crm_api_key)\s*=\s*['\"]([^'\"]+)['\"]"
        ]

    def format(self, record: logging.LogRecord) -> str:
        formatted = super().format(record)
        for pattern in self.sensitive_patterns:
            formatted = re.sub(pattern, lambda m: f"{m.group(1)}: '***'" if ":" in m.group(0) else f"{m.group(1)}='***'", formatted)
        if self.secrets_getter:
            secrets = self.secrets_getter()
            for secret in secrets:
                if secret and len(secret) >= 5:
                    formatted = formatted.replace(secret, "***")
        return formatted


def setup_log_scrubbing():
    root_logger = logging.getLogger()
    for handler in root_logger.handlers:
        old_formatter = handler.formatter
        fmt = old_formatter._fmt if old_formatter else None
        datefmt = old_formatter.datefmt if old_formatter else None
        style = old_formatter._style.default_format if old_formatter and hasattr(old_formatter._style, 'default_format') else '%'
        handler.setFormatter(
            SensitiveDataFormatter(
                fmt=fmt,
                datefmt=datefmt,
                style=style,
                secrets_getter=get_secret_values
            )
        )

mail_worker_task: asyncio.Task | None = None
EMAIL_ADDRESS_PATTERN = re.compile(r"^[^@\s,;]+@[^@\s,;]+\.[^@\s,;]+$")
LOGISTICS_TASK_NO_PATTERN = re.compile(r"LT-\d{8}-\d{4}", re.IGNORECASE)


@contextlib.asynccontextmanager
async def lifespan(app_: FastAPI):
    await startup()
    try:
        yield
    finally:
        await shutdown()


app = FastAPI(title="商务生产任务单智能体 MVP", lifespan=lifespan)


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
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


def get_current_user(request: Request, session: Session = Depends(get_session)) -> User:
    username = getattr(request.state, "username", None)
    if not username:
        raise HTTPException(status_code=401, detail="Not authenticated")
    user = session.query(User).filter(User.username == username).first()
    if not user:
        raise HTTPException(status_code=401, detail="User not found")
    return user


def require_role(allowed_roles: list[str]):
    def dependency(user: User = Depends(get_current_user)) -> User:
        if user.role == "admin":
            return user
        if user.role not in allowed_roles:
            raise HTTPException(status_code=403, detail="Permission denied")
        return user
    return dependency



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


async def startup() -> None:
    setup_log_scrubbing()
    init_db()
    with SessionLocal() as session:
        seed_defaults(session)
        backfill_mail_received_at(session)
        readiness = runtime_startup_readiness(session)
        if not readiness["ready"]:
            set_config(session, "bot_enabled", "false", is_secret=False)
        session.commit()
    # 加载动态生成的技能
    registry.load_dynamic_skills()
    
    if settings.mail_auto_worker_enabled:
        global mail_worker_task
        mail_worker_task = asyncio.create_task(mail_auto_worker_loop())


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
        "bot_enabled": system_config_bool(session, "bot_enabled", True),
        "database": database_health(session),
        "queues": queues,
    }


@app.get("/api/system/health")
def system_health(session: Session = Depends(get_session)) -> dict:
    return {
        "readiness": runtime_startup_readiness(session),
        "bot_enabled": system_config_bool(session, "bot_enabled", True),
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
def login(payload: LoginRequest, session: Session = Depends(get_session)) -> Response:
    user = session.query(User).filter(User.username == payload.username).first()
    if not user or not verify_password(payload.password, user.password_hash):
        raise HTTPException(status_code=401, detail="invalid username or password")
    response = JSONResponse({
        "authenticated": True, 
        "username": user.username,
        "role": user.role,
        "department": user.department
    })
    response.set_cookie(
        key=COOKIE_NAME,
        value=create_session_token(user.username),
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
def me(request: Request, session: Session = Depends(get_session)) -> dict:
    username = parse_session_token(request.cookies.get(COOKIE_NAME))
    if not username:
        return {"authenticated": False}
    user = session.query(User).filter(User.username == username).first()
    if not user:
        return {"authenticated": False}
    
    role_names = {
        "admin": "系统管理员",
        "business_owner": "商务负责人",
        "business_operator": "销售/渠道运营",
        "auditor": "财务审计",
        "it_ops": "IT 运维"
    }
    
    return {
        "authenticated": True,
        "username": user.username,
        "role": user.role,
        "role_name": role_names.get(user.role, user.role),
        "department": user.department
    }



@app.post("/api/bootstrap")
def bootstrap(session: Session = Depends(get_session), current_user: User = Depends(require_role(["admin"]))) -> dict:
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
    logistics_tasks_to_clear = session.query(LogisticsTask).all()
    task_ids = [task.id for task in tasks_to_clear]
    logistics_task_ids = [task.id for task in logistics_tasks_to_clear]
    requirement_ids = sorted({task.requirement_id for task in tasks_to_clear} | {task.requirement_id for task in logistics_tasks_to_clear})
    version_ids = [
        row.id
        for row in session.query(ProductionTaskVersion.id).filter(ProductionTaskVersion.task_id.in_(task_ids)).all()
    ] if task_ids else []
    logistics_version_ids = [
        row.id
        for row in session.query(LogisticsTaskVersion.id).filter(LogisticsTaskVersion.logistics_task_id.in_(logistics_task_ids)).all()
    ] if logistics_task_ids else []

    mail_updates = 0
    outbound_updates = 0
    exception_updates = 0
    question_deletes = 0
    version_deletes = 0
    logistics_version_deletes = 0
    fulfillment_item_deletes = 0
    binding_deletes = 0
    evidence_deletes = 0
    requirement_deletes = 0
    task_deletes = 0
    logistics_task_deletes = 0

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

    if logistics_task_ids:
        fulfillment_item_deletes = (
            session.query(FulfillmentItem)
            .filter(FulfillmentItem.logistics_task_id.in_(logistics_task_ids))
            .delete(synchronize_session=False)
        )
        logistics_version_deletes = (
            session.query(LogisticsTaskVersion)
            .filter(LogisticsTaskVersion.logistics_task_id.in_(logistics_task_ids))
            .delete(synchronize_session=False)
        )
        logistics_task_deletes = (
            session.query(LogisticsTask)
            .filter(LogisticsTask.id.in_(logistics_task_ids))
            .delete(synchronize_session=False)
        )

    if task_ids:
        task_deletes = (
            session.query(ProductionTask)
            .filter(ProductionTask.id.in_(task_ids))
            .delete(synchronize_session=False)
        )

    if requirement_ids:
        fulfillment_item_deletes += (
            session.query(FulfillmentItem)
            .filter(FulfillmentItem.requirement_id.in_(requirement_ids))
            .delete(synchronize_session=False)
        )
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
        "logistics_task_count": logistics_task_deletes,
        "requirement_count": requirement_deletes,
        "version_count": version_deletes,
        "logistics_version_count": logistics_version_deletes,
        "fulfillment_item_count": fulfillment_item_deletes,
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

    if not system_config_bool(session, "crm_sync_enabled", False):
        missing.append("CRM同步未启用")
    crm_username = system_config_value(session, "crm_username", overrides).strip()
    crm_password = system_config_value(session, "crm_password", overrides).strip()
    crm_api_key = system_config_value(session, "crm_api_key", overrides).strip()
    if not crm_api_key and not (crm_username and crm_password):
        missing.append("CRM账号密码或API Key")
    crm_system_owner_email = system_config_value(session, "crm_system_owner_email", overrides).strip()
    if not crm_system_owner_email:
        missing.append("CRM系统负责人邮箱")
    elif not re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", crm_system_owner_email):
        missing.append("CRM系统负责人邮箱格式不合法")

    if not system_config_bool(session, "oms_enabled", False):
        missing.append("OMS接入未启用")
    if system_config_bool(session, "oms_mock_success", True):
        missing.append("OMS真实下推未启用")
    oms_app_key = system_config_value(session, "oms_jackyun_app_key", overrides).strip()
    oms_app_secret = system_config_value(session, "oms_jackyun_app_secret", overrides).strip()
    if not oms_app_key:
        missing.append("OMS AppKey")
    if not oms_app_secret:
        missing.append("OMS AppSecret")
    oms_admin_email = system_config_value(session, "oms_admin_email", overrides).strip()
    if not oms_admin_email:
        missing.append("OMS管理员邮箱")
    elif not re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", oms_admin_email):
        missing.append("OMS管理员邮箱格式不合法")

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


def system_config_value(session: Session, key: str, overrides: dict | None = None, default: str = "") -> str:
    overrides = overrides or {}
    if key in overrides and overrides[key] not in (None, ""):
        return str(overrides[key])
    row = session.get(SystemConfig, key)
    if row is None or row.value is None:
        return default
    return str(row.value)


def disable_bot_when_not_ready(session: Session) -> dict:
    readiness = runtime_startup_readiness(session)
    bot_disabled = False
    if system_config_bool(session, "bot_enabled", False) and not readiness["ready"]:
        set_config(session, "bot_enabled", "false", is_secret=False)
        bot_disabled = True
        readiness = runtime_startup_readiness(session)
    return {"readiness": readiness, "bot_disabled": bot_disabled}


def system_config_bool(session: Session, key: str, default: bool = False) -> bool:
    row = session.get(SystemConfig, key)
    if row is None or row.value in (None, ""):
        return default
    return str(row.value).strip().lower() in {"1", "true", "yes", "on"}


def _store_config_value(value) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    return str(value)


def kick_mail_worker_once_async(reason: str = "manual-start") -> None:
    def _run() -> None:
        try:
            result = run_mail_auto_worker_once()
            logger.info("mail worker kickoff completed: reason=%s result=%s", reason, result)
        except Exception:
            logger.exception("mail worker kickoff failed: reason=%s", reason)

    thread = threading.Thread(target=_run, name=f"mail-worker-kickoff-{reason}", daemon=True)
    thread.start()


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


def format_display_time(value: datetime | str | None) -> str:
    if value in (None, ""):
        return ""
    if isinstance(value, str):
        try:
            value = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return value
    return f"{format_beijing_time(value)}（北京时间）"


def config_int(session: Session, key: str, default: int) -> int:
    row = session.get(SystemConfig, key)
    try:
        return int(row.value) if row is not None else default
    except (TypeError, ValueError):
        return default


def crm_cdp_port_from_config(session: Session) -> int:
    port = config_int(session, "crm_cdp_port", 0)
    if port:
        return port
    url = system_config_value(session, "crm_cdp_url", {}, "http://127.0.0.1:9333")
    match = re.search(r":(\d+)(?:/|$)", url)
    if match:
        return int(match.group(1))
    return 9333


def crm_external_browser_pids(port: int, user_data_dir: str) -> list[int]:
    patterns = [
        f"remote-debugging-port={port}",
        f"user-data-dir={user_data_dir}",
        f"fxiaoke_start_cdp_chrome.mjs --port={port}",
    ]
    pids: set[int] = set()
    for pattern in patterns:
        try:
            output = subprocess.check_output(["pgrep", "-f", pattern], text=True, stderr=subprocess.DEVNULL)
        except subprocess.CalledProcessError:
            continue
        for line in output.splitlines():
            try:
                pid = int(line.strip())
            except ValueError:
                continue
            if pid != os.getpid():
                pids.add(pid)
    return sorted(pids)


def crm_cdp_version(port: int) -> dict:
    try:
        import urllib.request
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/json/version", timeout=2) as response:
            return json.loads(response.read().decode("utf-8"))
    except Exception:
        return {}


def crm_browser_status() -> dict:
    global _crm_browser_process, _crm_browser_meta
    process = _crm_browser_process
    running = process is not None and process.poll() is None
    if process is not None and not running:
        _crm_browser_meta = {**_crm_browser_meta, "exit_code": process.returncode}
        _crm_browser_process = None
    return {
        "managed": running,
        "pid": process.pid if running and process is not None else None,
        **_crm_browser_meta,
    }


def stop_crm_browser_process() -> dict:
    global _crm_browser_process, _crm_browser_meta
    process = _crm_browser_process
    if process is None or process.poll() is not None:
        _crm_browser_process = None
        port = int(_crm_browser_meta.get("port") or 0) if _crm_browser_meta.get("port") else 0
        user_data_dir = str(_crm_browser_meta.get("user_data_dir") or "")
        if not port:
            return {"stopped": False, **_crm_browser_meta}
        pids = crm_external_browser_pids(port, user_data_dir or f"/private/tmp/fxiaoke-cdp-profile-{port}")
        for pid in pids:
            with contextlib.suppress(ProcessLookupError, PermissionError):
                os.kill(pid, 15)
        if pids:
            time.sleep(1)
            for pid in pids:
                with contextlib.suppress(ProcessLookupError, PermissionError):
                    os.kill(pid, 9)
            _crm_browser_meta = {**_crm_browser_meta, "stopped": True, "external_pids": pids}
            return {"stopped": True, "external_pids": pids, **_crm_browser_meta}
        return {"stopped": False, **_crm_browser_meta}
    process.terminate()
    try:
        process.wait(timeout=5)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait(timeout=5)
    _crm_browser_meta = {**_crm_browser_meta, "stopped": True, "exit_code": process.returncode}
    _crm_browser_process = None
    return {"stopped": True, **_crm_browser_meta}


def start_crm_browser_process(session: Session, requested_mode: str | None = None) -> dict:
    global _crm_browser_process, _crm_browser_meta
    # Default CRM automation should never pop a browser window. A visible browser
    # is only used when the caller explicitly requests manual login mode.
    mode = (requested_mode or "headless").strip().lower()
    if mode not in {"headless", "headed"}:
        raise HTTPException(status_code=400, detail="CRM 浏览器模式只能是 headless 或 headed")
    current = crm_browser_status()
    if current.get("managed"):
        if current.get("mode") == mode:
            return {**current, "already_running": True}
        stop_crm_browser_process()

    port = crm_cdp_port_from_config(session)
    user_data_dir = system_config_value(session, "crm_cdp_user_data_dir", {}, f"/private/tmp/fxiaoke-cdp-profile-{port}").strip()
    existing_cdp = crm_cdp_version(port)
    if existing_cdp:
        user_agent = str(existing_cdp.get("User-Agent") or "")
        if mode == "headed" and "HeadlessChrome" in user_agent:
            _crm_browser_meta = {"mode": "headless", "cdp_url": f"http://127.0.0.1:{port}", "port": port, "user_data_dir": user_data_dir, "external": True}
            stop_crm_browser_process()
        elif mode == "headless" and "HeadlessChrome" not in user_agent:
            _crm_browser_meta = {"mode": "headed", "cdp_url": f"http://127.0.0.1:{port}", "port": port, "user_data_dir": user_data_dir, "external": True}
            stop_crm_browser_process()
        elif mode == "headed" and "HeadlessChrome" not in user_agent:
            return {
                "managed": False,
                "external": True,
                "already_running": True,
                "mode": "headed",
                "cdp_url": f"http://127.0.0.1:{port}",
                "port": port,
                "user_data_dir": user_data_dir,
                "message": "CRM 人工登录浏览器已在运行，请在桌面或任务栏中切换到该 Chrome 窗口。",
            }
    chrome_bin = system_config_value(session, "crm_chrome_bin", {}, "").strip()
    script_path = Path(__file__).resolve().parents[2] / "scripts" / "fxiaoke_start_cdp_chrome.mjs"
    if not script_path.exists():
        raise HTTPException(status_code=500, detail=f"CRM 专用浏览器启动脚本不存在：{script_path}")
    command = [
        "node",
        str(script_path),
        f"--port={port}",
        f"--user-data-dir={user_data_dir}",
    ]
    if mode == "headed":
        command.append("--headed")
    env = os.environ.copy()
    if chrome_bin:
        env["CHROME_BIN"] = chrome_bin
    try:
        _crm_browser_process = subprocess.Popen(
            command,
            cwd=str(Path(__file__).resolve().parents[2]),
            env=env,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
    except OSError as exc:
        raise HTTPException(status_code=500, detail=f"CRM 专用浏览器启动失败：{exc}") from exc
    _crm_browser_meta = {
        "mode": mode,
        "cdp_url": f"http://127.0.0.1:{port}",
        "port": port,
        "user_data_dir": user_data_dir,
        "started_at": now_utc().isoformat(),
    }
    return crm_browser_status()


def system_queue_health(session: Session) -> dict:
    now = now_utc()
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
        .filter(
            OutboundMailJob.status == "Pending",
            OutboundMailJob.mail_type.in_(AUTO_WORKFLOW_MAIL_TYPES),
            (OutboundMailJob.next_retry_at.is_(None)) | (OutboundMailJob.next_retry_at <= now),
        )
        .count()
    )
    pending_auto_total = (
        session.query(OutboundMailJob)
        .filter(OutboundMailJob.status == "Pending", OutboundMailJob.mail_type.in_(AUTO_WORKFLOW_MAIL_TYPES))
        .count()
    )
    pending_due = (
        session.query(OutboundMailJob)
        .filter(
            OutboundMailJob.status == "Pending",
            (OutboundMailJob.next_retry_at.is_(None)) | (OutboundMailJob.next_retry_at <= now),
        )
        .count()
    )
    pending_future_retry = (
        session.query(OutboundMailJob)
        .filter(OutboundMailJob.status == "Pending", OutboundMailJob.next_retry_at.is_not(None), OutboundMailJob.next_retry_at > now)
        .count()
    )
    sending_stale = (
        session.query(OutboundMailJob)
        .filter(OutboundMailJob.status == "Sending", OutboundMailJob.locked_until.is_not(None), OutboundMailJob.locked_until < now)
        .count()
    )
    processing_stale = (
        session.query(ProcessingJob)
        .filter(ProcessingJob.status == "Running", ProcessingJob.locked_until.is_not(None), ProcessingJob.locked_until < now)
        .count()
    )
    pending_manual = max(0, int(outbound_counts.get("Pending", 0) or 0) - pending_auto_total)
    return {
        "outbound": {
            "counts": outbound_counts,
            "pending_due": pending_due,
            "pending_future_retry": pending_future_retry,
            "pending_auto_dispatchable": pending_auto,
            "pending_auto_total": pending_auto_total,
            "pending_manual_only": pending_manual,
            "sending_stale": sending_stale,
            "oldest_pending_id": oldest_pending.id if oldest_pending else None,
            "oldest_pending_age_seconds": seconds_since(oldest_pending.created_at) if oldest_pending else None,
            "single_run_send_limit": 1,
            "send_lease_seconds": settings.outbound_send_lease_seconds,
        },
        "processing": {"counts": processing_counts, "running_stale": processing_stale, "lease_seconds": settings.processing_job_lease_seconds},
    }


def require_admin_password(admin_password: str) -> None:
    if not hmac.compare_digest(admin_password or "", settings.admin_password or ""):
        raise HTTPException(status_code=403, detail="invalid admin password")


@app.get("/api/config")
def config(session: Session = Depends(get_session), current_user: User = Depends(require_role(["admin", "it_ops"]))) -> dict:
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
def update_mail_config(payload: MailRuntimeConfigUpdate, session: Session = Depends(get_session), current_user: User = Depends(require_role(["admin"]))) -> dict:
    try:
        values = payload.model_dump(exclude_unset=True)
        bot_was_enabled = system_config_bool(session, "bot_enabled", False)
        bot_enable_requested = values.get("bot_enabled") is True
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
                set_config(session, "bot_enabled", "false", is_secret=False)
                session.commit()
                raise HTTPException(status_code=400, detail=f"系统启动前配置不完整：缺少 {'、'.join(readiness['missing'])}")
        secret_keys = {"bot_email_password", "baidu_map_ak", "e2e_sales_password", "e2e_production_password"}
        for key, value in values.items():
            if value is None:
                continue
            if key in secret_keys and str(value).strip() in {"", "***"}:
                continue
            set_config(session, key, _store_config_value(value), is_secret=key in secret_keys)
        session.commit()
        result = config(session)
        if bot_enable_requested and not bot_was_enabled:
            kick_mail_worker_once_async("bot-enabled")
            result["worker_kickoff"] = {"triggered": True, "reason": "bot-enabled"}
        else:
            result["worker_kickoff"] = {"triggered": False}
        return result
    except OperationalError as exc:
        session.rollback()
        if "database is locked" in str(exc).lower():
            raise HTTPException(status_code=503, detail="数据库正被后台任务占用，请稍后重试；如持续出现，请重启服务释放 SQLite 写锁。") from exc
        raise


@app.put("/api/config/erp")
def update_erp_config(payload: ErpRuntimeConfigUpdate, session: Session = Depends(get_session), current_user: User = Depends(require_role(["admin"]))) -> dict:
    values = payload.model_dump(exclude_unset=True)
    if "erp_server_url" in values and values["erp_server_url"] not in (None, ""):
        values["erp_server_url"] = normalize_kingdee_server_url(str(values["erp_server_url"]))
    if "erp_lcid" in values and values["erp_lcid"] not in (None, ""):
        try:
            values["erp_lcid"] = int(values["erp_lcid"])
        except (TypeError, ValueError) as exc:
            raise HTTPException(status_code=400, detail="ERP LCID 必须是数字") from exc
    if "erp_material_sync_interval_seconds" in values and values["erp_material_sync_interval_seconds"] not in (None, ""):
        interval = int(values["erp_material_sync_interval_seconds"])
        if interval < 300:
            raise HTTPException(status_code=400, detail="ERP 物料自动同步周期不能低于 300 秒")
        values["erp_material_sync_interval_seconds"] = interval
    secret_keys = {"erp_app_sec"}
    for key, value in values.items():
        if value is None:
            continue
        if key in secret_keys and str(value).strip() in {"", "***"}:
            continue
        set_config(session, key, str(value), is_secret=key in secret_keys)
    set_config(session, "erp_readonly", "true", is_secret=False)
    set_config(session, "erp_write_enabled", "false", is_secret=False)
    session.commit()
    return config(session)


@app.put("/api/config/crm")
def update_crm_config(payload: CrmRuntimeConfigUpdate, session: Session = Depends(get_session), current_user: User = Depends(require_role(["admin"]))) -> dict:
    values = payload.model_dump(exclude_unset=True)
    if "crm_sync_interval_seconds" in values and values["crm_sync_interval_seconds"] not in (None, ""):
        interval = int(values["crm_sync_interval_seconds"])
        if interval < 60:
            raise HTTPException(status_code=400, detail="CRM 同步周期不能低于 60 秒")
        values["crm_sync_interval_seconds"] = interval
    if "crm_sync_min_order_date" in values and values["crm_sync_min_order_date"] not in (None, ""):
        min_order_date = str(values["crm_sync_min_order_date"]).strip()
        try:
            datetime.strptime(min_order_date, "%Y-%m-%d")
        except ValueError as exc:
            raise HTTPException(status_code=400, detail="CRM 最早下单日期格式必须是 YYYY-MM-DD") from exc
        values["crm_sync_min_order_date"] = min_order_date
    if "crm_sync_page_size" in values and values["crm_sync_page_size"] not in (None, ""):
        page_size = int(values["crm_sync_page_size"])
        if page_size < 1 or page_size > 200:
            raise HTTPException(status_code=400, detail="CRM 每页条数需在 1-200 之间")
        values["crm_sync_page_size"] = page_size
    if "crm_sync_max_pages" in values and values["crm_sync_max_pages"] not in (None, ""):
        max_pages = int(values["crm_sync_max_pages"])
        if max_pages < 0 or max_pages > 200:
            raise HTTPException(status_code=400, detail="CRM 最大同步页数需在 0-200 之间，0 表示按最早下单日期自动停止")
        values["crm_sync_max_pages"] = max_pages
    if "crm_sync_timeout_seconds" in values and values["crm_sync_timeout_seconds"] not in (None, ""):
        timeout = int(values["crm_sync_timeout_seconds"])
        if timeout < 30 or timeout > 600:
            raise HTTPException(status_code=400, detail="CRM 同步超时需在 30-600 秒之间")
        values["crm_sync_timeout_seconds"] = timeout
    if "crm_cdp_browser_mode" in values and values["crm_cdp_browser_mode"] not in (None, ""):
        mode = str(values["crm_cdp_browser_mode"]).strip().lower()
        if mode not in {"headless", "headed"}:
            raise HTTPException(status_code=400, detail="CRM 浏览器模式只能是 headless 或 headed")
        values["crm_cdp_browser_mode"] = mode
    if "crm_cdp_port" in values and values["crm_cdp_port"] not in (None, ""):
        port = int(values["crm_cdp_port"])
        if port < 1024 or port > 65535:
            raise HTTPException(status_code=400, detail="CRM CDP 端口需在 1024-65535 之间")
        values["crm_cdp_port"] = port
        if "crm_cdp_url" not in values or not str(values.get("crm_cdp_url") or "").strip():
            values["crm_cdp_url"] = f"http://127.0.0.1:{port}"
    if "crm_cdp_user_data_dir" in values and values["crm_cdp_user_data_dir"] not in (None, ""):
        values["crm_cdp_user_data_dir"] = str(values["crm_cdp_user_data_dir"]).strip()
    if "crm_chrome_bin" in values and values["crm_chrome_bin"] not in (None, ""):
        values["crm_chrome_bin"] = str(values["crm_chrome_bin"]).strip()
    if "crm_system_owner_email" in values and values["crm_system_owner_email"] not in (None, ""):
        email = str(values["crm_system_owner_email"]).strip()
        if not re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", email):
            raise HTTPException(status_code=400, detail="CRM 系统负责人邮箱格式不正确")
        values["crm_system_owner_email"] = email
    resulting_crm_system_owner_email = str(values.get("crm_system_owner_email") or system_config_value(session, "crm_system_owner_email", {})).strip()
    if not resulting_crm_system_owner_email:
        raise HTTPException(status_code=400, detail="CRM 系统负责人邮箱为必填项")
    if "v2_crm_phase1_scope_json" in values and values["v2_crm_phase1_scope_json"] not in (None, ""):
        scope_config = loads(str(values["v2_crm_phase1_scope_json"]), None)
        if not isinstance(scope_config, dict):
            raise HTTPException(status_code=400, detail="一期纳入范围配置必须是 JSON 对象")
    secret_keys = {"crm_password", "crm_api_key", "crm_fxiaoke_request_json", "crm_fxiaoke_detail_request_json"}
    for key, value in values.items():
        if value is None:
            continue
        if key in secret_keys and str(value).strip() in {"", "***"}:
            continue
        set_config(session, key, str(value), is_secret=key in secret_keys)
    disable_bot_when_not_ready(session)
    session.commit()
    return config(session)


@app.put("/api/config/oms")
def update_oms_config(payload: OmsRuntimeConfigUpdate, session: Session = Depends(get_session), current_user: User = Depends(require_role(["admin"]))) -> dict:
    values = payload.model_dump(exclude_unset=True)
    int_bounds = {
        "oms_retry_base_delay_seconds": (1, 86400),
        "oms_retry_multiplier": (1, 10),
        "oms_max_retries": (1, 20),
        "oms_jackyun_timeout_seconds": (3, 120),
    }
    for key, (minimum, maximum) in int_bounds.items():
        if key in values and values[key] not in (None, ""):
            try:
                value = int(values[key])
            except (TypeError, ValueError) as exc:
                raise HTTPException(status_code=400, detail=f"{key} 必须是数字") from exc
            if value < minimum or value > maximum:
                raise HTTPException(status_code=400, detail=f"{key} 需在 {minimum}-{maximum} 之间")
            values[key] = value
    if "oms_create_order_method" in values and values["oms_create_order_method"] not in (None, ""):
        method = str(values["oms_create_order_method"]).strip()
        if method not in {"wms.order.create", "wms-ods.order.create"}:
            raise HTTPException(status_code=400, detail="发货单创建接口仅支持 wms.order.create 或 wms-ods.order.create")
        values["oms_create_order_method"] = method
    if "oms_admin_email" in values and values["oms_admin_email"] not in (None, ""):
        email = str(values["oms_admin_email"]).strip()
        if not re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", email):
            raise HTTPException(status_code=400, detail="OMS 管理员邮箱格式不正确")
        values["oms_admin_email"] = email
    resulting_oms_admin_email = str(values.get("oms_admin_email") or system_config_value(session, "oms_admin_email", {})).strip()
    if not resulting_oms_admin_email:
        raise HTTPException(status_code=400, detail="OMS 管理员邮箱为必填项")
    if "oms_customer_query_payload_json" in values and values["oms_customer_query_payload_json"] not in (None, ""):
        payload_json = loads(str(values["oms_customer_query_payload_json"]), None)
        if not isinstance(payload_json, dict):
            raise HTTPException(status_code=400, detail="OMS 客户查询参数必须是 JSON 对象")
    secret_keys = {"oms_jackyun_app_secret"}
    for key, value in values.items():
        if value is None:
            continue
        if key in secret_keys and str(value).strip() in {"", "***"}:
            continue
        set_config(session, key, str(value), is_secret=key in secret_keys)
    disable_bot_when_not_ready(session)
    session.commit()
    return config(session)


@app.post("/api/oms/jackyun/test-connection")
def test_jackyun_connection(session: Session = Depends(get_session)) -> dict:
    try:
        client = jackyun_client_from_session(session)
        result = client.search_skus({"pageNo": 1, "pageSize": 1})
        # 构建签名诊断信息（AppSecret 完全遮蔽，用于排查签名错误）
        app_secret_len = len(client.config.app_secret)
        app_secret_masked = client.config.app_secret[0] + "***" + client.config.app_secret[-1] if app_secret_len >= 2 else "***"
        bizcontent_json = '{"pageNo":1,"pageSize":1}'
        # 模拟 sign_params 的拼接过程：按 key 排序 → key+value 拼接 → 前后包裹 secret → 转小写 → MD5
        params_sorted = sorted([
            ("appkey", client.config.app_key),
            ("bizcontent", bizcontent_json),
            ("contenttype", client.config.content_type),
            ("method", "erp-goods.goods.sku.search"),
            ("timestamp", "[CURRENT_TIMESTAMP]"),
            ("version", client.config.version),
        ], key=lambda x: x[0])
        joined = "".join(f"{k}{v}" for k, v in params_sorted)
        sign_string_masked = f"[SECRET_{app_secret_len}chars]{joined}[SECRET_{app_secret_len}chars]".lower()
        return {
            "ok": bool(result.get("ok")),
            "endpoint": client.config.gateway_url,
            "method": "erp-goods.goods.sku.search",
            "code": result.get("code"),
            "sub_code": result.get("sub_code"),
            "message": result.get("message"),
            "_diagnostic": {
                "app_secret_len": app_secret_len,
                "app_secret_masked": app_secret_masked,
                "app_key": client.config.app_key,
                "sign_string_masked": sign_string_masked,
                "note": "sign_string_masked 中 [SECRET_Nchars] 代表 AppSecret 前后包裹的位置，拼接顺序为 appkey→bizcontent→contenttype→method→timestamp→version",
            },
        }
    except JackyunConfigError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.post("/api/erp/test-connection")
def test_erp_connection(session: Session = Depends(get_session)) -> dict:
    return test_kingdee_connection_from_config(session)


@app.post("/api/erp/test-write-permissions")
def test_erp_write_permissions(session: Session = Depends(get_session)) -> dict:
    """一站式测试金蝶写入权限（Save→Submit→Audit→UnAudit→Cancel→Delete）"""
    return test_kingdee_write_permissions_from_config(session)


@app.get("/api/erp/billing-status/{order_id}")
def erp_billing_status(order_id: str, session: Session = Depends(get_session)) -> dict:
    """查询订单的 ERP 制单状态和失败原因"""
    order = session.get(MiddlePlatformOrder, order_id)
    if order is None:
        raise HTTPException(status_code=404, detail="订单不存在")
    return {
        "order_id": order.id,
        "order_no": order.order_no,
        "status": order.status,
        "erp_bill_no": order.erp_bill_no,
        "entity_code": order.entity_code,
        "order_type": order.order_type,
    }


@app.post("/api/erp/billing-retry/{order_id}")
def erp_billing_retry(order_id: str, session: Session = Depends(get_session)) -> dict:
    """重试失败的金蝶制单"""
    order = session.get(MiddlePlatformOrder, order_id)
    if order is None:
        raise HTTPException(status_code=404, detail="订单不存在")
    if order.status != OrderStatus.ERP_FAILED.value:
        raise HTTPException(status_code=400, detail=f"当前状态不允许重试：{order.status}")
    return retry_erp_billing(session, order, trace_id=f"manual-retry-{order.id}")


@app.post("/api/erp/query")
def query_erp_bill(payload: ErpBillQueryRequest, session: Session = Depends(get_session)) -> dict:
    return execute_bill_query_from_config(
        session,
        form_id=payload.form_id.strip(),
        field_keys=payload.field_keys.strip(),
        filter_string=payload.filter_string.strip(),
        order_string=payload.order_string.strip(),
        limit=payload.limit,
        start_row=payload.start_row,
    )


@app.get("/api/erp/materials")
def erp_materials(q: str = "", limit: int = Query(20, ge=1, le=100), include_erp: bool = False, session: Session = Depends(get_session)) -> dict:
    return search_materials(session, q=q, limit=limit, include_erp=include_erp)


@app.get("/api/erp/inventory")
def erp_inventory(
    material_code: str = "",
    warehouse_code: str = "",
    limit: int = Query(50, ge=1, le=200),
    session: Session = Depends(get_session),
) -> dict:
    return query_inventory(session, material_code=material_code, warehouse_code=warehouse_code, limit=limit)


@app.post("/api/system/business-data/clear")
def clear_business_data(payload: AdminPasswordRequest, request: Request, session: Session = Depends(get_session), current_user: User = Depends(require_role(["admin"]))) -> dict:
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
def get_initial_review_rules(session: Session = Depends(get_session), current_user: User = Depends(require_role(["admin", "business_owner"]))) -> dict:
    config = initial_review_config(session, include_workflow_rules=True)
    session.commit()
    return config


@app.put("/api/initial-review/rules")
def update_initial_review_rules(payload: InitialReviewConfigUpdate, session: Session = Depends(get_session), current_user: User = Depends(require_role(["admin", "business_owner"]))) -> dict:
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


@app.get("/api/v2-review/rules")
def get_v2_review_rules(session: Session = Depends(get_session), current_user: User = Depends(require_role(["admin", "business_owner"]))) -> dict:
    return review_rule_config(session)


@app.put("/api/v2-review/rules")
def update_v2_review_rules(payload: V2ReviewRulesUpdate, session: Session = Depends(get_session), current_user: User = Depends(require_role(["admin", "business_owner"]))) -> dict:
    allowed_codes = {rule.get_rule_code() for rule in DEFAULT_RULES}
    states = {}
    for item in payload.rules:
        code = str(item.code or "").strip()
        if code not in allowed_codes:
            raise HTTPException(status_code=400, detail=f"unsupported v2 review rule: {code}")
        states[code] = {"enabled": bool(item.enabled)}
    set_config(session, V2_REVIEW_RULE_STATES_KEY, dumps(states), is_secret=False)
    session.commit()
    return review_rule_config(session)


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
    if payload.api_key and str(payload.api_key).strip() != "***":
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
    mail_received_order = func.coalesce(MailMessage.received_at, MailMessage.created_at)
    return page_response(
        query.order_by(mail_received_order.desc(), MailMessage.created_at.desc()),
        lambda row: serialize_mail(row, session),
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
    data = serialize_mail(mail, session)
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
                ProductionTask.id.ilike(pattern),
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


@app.get("/api/logistics-tasks")
def logistics_tasks(
    q: str | None = None,
    status: str | None = None,
    customer: str | None = None,
    product: str | None = None,
    salesperson: str | None = None,
    order_no: str | None = None,
    page: int = Query(1, ge=1),
    page_size: int = Query(10, ge=1, le=100),
    session: Session = Depends(get_session),
) -> dict:
    query = session.query(LogisticsTask).join(OrderRequirement, LogisticsTask.requirement_id == OrderRequirement.id)
    if q and q.strip():
        pattern = f"%{q.strip()}%"
        query = query.filter(
            or_(
                LogisticsTask.task_no.ilike(pattern),
                LogisticsTask.id.ilike(pattern),
                LogisticsTask.status.ilike(pattern),
                OrderRequirement.customer_name.ilike(pattern),
                OrderRequirement.salesperson_email.ilike(pattern),
                OrderRequirement.product_summary.ilike(pattern),
                OrderRequirement.quantity_text.ilike(pattern),
                OrderRequirement.external_order_no.ilike(pattern),
            )
        )
    if status and status.strip():
        query = query.filter(LogisticsTask.status == status.strip())
    if customer and customer.strip():
        query = query.filter(OrderRequirement.customer_name.ilike(f"%{customer.strip()}%"))
    if product and product.strip():
        query = query.filter(OrderRequirement.product_summary.ilike(f"%{product.strip()}%"))
    if salesperson and salesperson.strip():
        query = query.filter(OrderRequirement.salesperson_email.ilike(f"%{salesperson.strip()}%"))
    if order_no and order_no.strip():
        query = query.filter(OrderRequirement.external_order_no.ilike(f"%{order_no.strip()}%"))

    return page_response(
        query.order_by(LogisticsTask.created_at.desc()),
        serialize_logistics_task,
        page,
        page_size,
        {"status_options": distinct_values(session, LogisticsTask.status)},
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


@app.get("/api/logistics-tasks/{task_id}")
def logistics_task_detail(task_id: str, session: Session = Depends(get_session)) -> dict:
    task = session.get(LogisticsTask, task_id)
    if task is None:
        raise HTTPException(status_code=404, detail="logistics task not found")
    return serialize_logistics_task(task, include_versions=True)


@app.get("/api/logistics-tasks/{task_id}/workflow")
def logistics_task_workflow(task_id: str, session: Session = Depends(get_session)) -> dict:
    task = session.get(LogisticsTask, task_id)
    if task is None:
        raise HTTPException(status_code=404, detail="logistics task not found")
    return build_logistics_task_workflow(session, task)


@app.post("/api/logistics-tasks/{task_id}/manual-close")
def manual_close_logistics_task(task_id: str, payload: TaskManualCloseRequest, request: Request, session: Session = Depends(get_session)) -> dict:
    task = session.get(LogisticsTask, task_id)
    if task is None:
        raise HTTPException(status_code=404, detail="logistics task not found")
    try:
        jobs = force_close_logistics_task_manual(
            session,
            task,
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


def logistics_task_outbounds(session: Session, task: LogisticsTask) -> list[OutboundMailJob]:
    pattern = f"%{task.task_no}%"
    return (
        session.query(OutboundMailJob)
        .filter(or_(OutboundMailJob.subject.ilike(pattern), OutboundMailJob.body.ilike(pattern)))
        .order_by(OutboundMailJob.created_at)
        .all()
    )


def logistics_task_mails(session: Session, task: LogisticsTask) -> list[MailMessage]:
    pattern = f"%{task.task_no}%"
    rows = (
        session.query(MailMessage)
        .filter(or_(MailMessage.subject.ilike(pattern), MailMessage.body_text.ilike(pattern)))
        .order_by(func.coalesce(MailMessage.received_at, MailMessage.created_at))
        .all()
    )
    source_mail = session.get(MailMessage, task.requirement.source_mail_id) if task.requirement.source_mail_id else None
    if source_mail is not None and all(row.id != source_mail.id for row in rows):
        rows.insert(0, source_mail)
    return rows


def build_logistics_task_trace_graph(session: Session, task: LogisticsTask) -> dict:
    requirement = task.requirement
    source_mail = session.get(MailMessage, requirement.source_mail_id) if requirement.source_mail_id else None
    versions = session.query(LogisticsTaskVersion).filter_by(logistics_task_id=task.id).order_by(LogisticsTaskVersion.version_no).all()
    outbounds = logistics_task_outbounds(session, task)
    mails = logistics_task_mails(session, task)
    evidences = session.query(ExtractionEvidence).filter_by(requirement_id=requirement.id).order_by(ExtractionEvidence.created_at).all()
    binding = session.query(RequirementWorkflowBinding).filter_by(requirement_id=requirement.id).one_or_none()
    production_task = session.get(ProductionTask, task.production_task_id) if task.production_task_id else None

    nodes: list[dict] = []
    edges: list[dict] = []

    def add_node(node_id: str, node_type: str, label: str, *, status: str = "", meta: dict | None = None) -> None:
        if not any(item["id"] == node_id for item in nodes):
            nodes.append({"id": node_id, "type": node_type, "label": label, "status": status, "meta": meta or {}})

    def add_edge(source: str, target: str, label: str) -> None:
        if source and target and not any(item["source"] == source and item["target"] == target and item["label"] == label for item in edges):
            edges.append({"source": source, "target": target, "label": label})

    if source_mail is not None:
        add_node(source_mail.id, "mail", source_mail.subject or "来源邮件", status=source_mail.classification or "", meta=serialize_mail(source_mail, session))
        add_edge(source_mail.id, requirement.id, "抽取")
    add_node(requirement.id, "requirement", requirement.internal_order_no, status=requirement.status, meta=serialize_requirement_summary(requirement))
    add_node(task.id, "logistics_task", task.task_no, status=task.status, meta=serialize_logistics_task(task))
    add_edge(requirement.id, task.id, "生成物流任务")
    if binding is not None:
        workflow_node_id = binding.workflow_version_id or f"workflow:{binding.workflow_code or 'unknown'}"
        add_node(workflow_node_id, "workflow", binding.workflow_name or binding.workflow_code or "命中流程", status=str(binding.match_confidence), meta=serialize_requirement_workflow_binding(binding))
        add_edge(workflow_node_id, requirement.id, "规则初审")
    for version in versions:
        add_node(version.id, "logistics_task_version", f"V{version.version_no}", status=version.status, meta={"subject": version.subject})
        add_edge(task.id, version.id, "版本")
    for job in outbounds:
        add_node(job.id, "outbound_mail", job.subject, status=job.status, meta=serialize_outbound_mail(job, session))
        add_edge(task.id, job.id, job.mail_type)
    for mail in mails:
        if source_mail is not None and mail.id == source_mail.id:
            continue
        add_node(mail.id, "mail", mail.subject or "物流回复", status=mail.classification or "", meta=serialize_mail(mail, session))
        add_edge(mail.id, task.id, "物流回复")
    if production_task is not None:
        add_node(production_task.id, "task", production_task.task_no, status=production_task.status, meta=serialize_task(production_task))
        add_edge(task.id, production_task.id, "缺货转生产")
    for evidence in evidences:
        add_node(evidence.id, "evidence", evidence.field_name, status=str(evidence.confidence), meta=serialize_evidence(evidence))
        add_edge(evidence.source_mail_id or requirement.id, evidence.id, "证据")
        add_edge(evidence.id, requirement.id, "支撑字段")

    object_ids = {task.id, requirement.id, *(row.id for row in versions), *(row.id for row in outbounds), *(row.id for row in mails)}
    if source_mail is not None:
        object_ids.add(source_mail.id)
    if production_task is not None:
        object_ids.add(production_task.id)
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


def build_logistics_task_workflow(session: Session, task: LogisticsTask) -> dict:
    requirement = task.requirement
    source_mail = session.get(MailMessage, requirement.source_mail_id) if requirement.source_mail_id else None
    versions = session.query(LogisticsTaskVersion).filter_by(logistics_task_id=task.id).order_by(LogisticsTaskVersion.version_no).all()
    outbounds = logistics_task_outbounds(session, task)
    mails = logistics_task_mails(session, task)
    issue_job = next((job for job in outbounds if job.mail_type == "LogisticsTaskIssue"), None)
    production_task = session.get(ProductionTask, task.production_task_id) if task.production_task_id else None

    def step(key: str, title: str, status: str, detail: str = "", at=None) -> dict:
        return {
            "key": key,
            "title": title,
            "status": status,
            "detail": detail,
            "created_at": at.isoformat() if at else None,
        }

    issue_status = "pending"
    issue_detail = "等待生成物流核查邮件"
    if issue_job is not None:
        issue_status = "done" if issue_job.status == "Sent" else "current"
        issue_detail = f"{issue_job.mail_type} / {issue_job.status}"

    logistics_status = "pending"
    logistics_detail = "等待物流反馈库存满足或缺货"
    if task.status == "Closed":
        logistics_status = "done"
        logistics_detail = task.closed_reason or "物流已闭环"
    elif task.status in {"ProductionRequested", "LogisticsShortageReported"}:
        logistics_status = "done"
        logistics_detail = "物流反馈缺货，已转生产"
    elif task.status == "LogisticsIssued":
        logistics_status = "current"

    production_status = "pending"
    production_detail = "库存满足时无需生产；缺货时自动转生产"
    if production_task is not None:
        production_status = "done" if production_task.status == "Closed" else "current"
        production_detail = f"{production_task.task_no} / {production_task.status}"

    steps = [
        step("received", "销售/订单需求", "done", source_mail.subject if source_mail else "来源需求已记录", source_mail.created_at if source_mail else task.created_at),
        step("review", "自动初审", "done", "订单信息已通过初审", task.created_at),
        step("draft", "生成物流核查单", "done", f"当前版本 V{task.current_version_no}", task.created_at),
        step("issue", "自动下达物流", issue_status, issue_detail, issue_job.created_at if issue_job else None),
        step("logistics", "物流处理", logistics_status, logistics_detail, task.closed_at or task.updated_at),
    ]
    if production_task is not None or task.status in {"ProductionRequested", "LogisticsShortageReported"}:
        steps.append(step("production", "缺货转生产", production_status, production_detail, production_task.created_at if production_task else None))
    steps.append(step("closed", "闭环", "done" if task.status == "Closed" else "pending", task.closed_reason or "", task.closed_at or task.updated_at))

    object_ids = {task.id, requirement.id}
    if source_mail is not None:
        object_ids.add(source_mail.id)
    object_ids.update(version.id for version in versions)
    object_ids.update(job.id for job in outbounds)
    object_ids.update(mail.id for mail in mails)
    audits = (
        session.query(AuditEvent)
        .filter(AuditEvent.related_object_id.in_(object_ids))
        .order_by(AuditEvent.created_at.desc())
        .limit(50)
        .all()
    )
    exceptions = (
        session.query(ExceptionCase)
        .filter(or_(ExceptionCase.detail.ilike(f"%{task.task_no}%"), ExceptionCase.detail.ilike(f"%{task.id}%")))
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
            "type": "mail",
            "title": mail.classification or "物流回复",
            "status": mail.from_address,
            "detail": {"subject": mail.subject},
            "created_at": (mail.received_at or mail.created_at).isoformat(),
        }
        for mail in mails
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
        "task": serialize_logistics_task(task, include_versions=True),
        "steps": steps,
        "timeline": timeline[:80],
        "trace": build_logistics_task_trace_graph(session, task),
    }


def enqueue_manual_close_logistics_sales_notice(session: Session, task: LogisticsTask, *, reason: str) -> OutboundMailJob | None:
    sales_email = (task.requirement.salesperson_email or "").strip()
    if not sales_email:
        return None
    to_addresses = [sales_email]
    cc_addresses: list[str] = []
    idem = f"logistics-manual-close-sales:{task.id}:{recipient_hash(to_addresses, cc_addresses)}"
    existing = session.query(OutboundMailJob).filter_by(idempotency_key=idem).one_or_none()
    if existing is not None:
        return existing
    body_lines = ["销售同事好，", "", f"物流任务 {task.task_no} 已由商务人员手动强制关闭。"]
    if reason:
        body_lines.extend(["", f"关闭说明：{reason}"])
    body_lines.extend(["", get_config(session, "bot_signature", "积木易搭AI机器人")])
    job = OutboundMailJob(
        mail_type="LogisticsManualClosedSales",
        to_json=dumps(to_addresses),
        cc_json=dumps(cc_addresses),
        subject=f"[物流任务手动关闭][{task.task_no}] 商务已关闭任务",
        body="\n".join(body_lines),
        idempotency_key=idem,
        status="Pending",
    )
    session.add(job)
    add_audit_event(session, "LogisticsManualClosedSalesQueued", "LogisticsTask", task.id, {"task_no": task.task_no})
    return job


def enqueue_manual_close_logistics_notice(session: Session, task: LogisticsTask, *, reason: str) -> OutboundMailJob | None:
    to_addresses = as_list(task.target_mail_to_json)
    cc_addresses = as_list(task.target_mail_cc_json)
    if not to_addresses:
        return None
    idem = f"logistics-manual-close-logistics:{task.id}:{recipient_hash(to_addresses, cc_addresses)}"
    existing = session.query(OutboundMailJob).filter_by(idempotency_key=idem).one_or_none()
    if existing is not None:
        return existing
    body_lines = ["物流部同事好，", "", f"物流任务 {task.task_no} 已由商务人员手动强制关闭，请停止后续处理。"]
    if reason:
        body_lines.extend(["", f"关闭说明：{reason}"])
    body_lines.extend(["", get_config(session, "bot_signature", "积木易搭AI机器人")])
    job = OutboundMailJob(
        mail_type="LogisticsManualClosedLogistics",
        to_json=dumps(to_addresses),
        cc_json=dumps(cc_addresses),
        subject=f"[物流任务手动关闭][{task.task_no}] 请停止处理",
        body="\n".join(body_lines),
        idempotency_key=idem,
        status="Pending",
    )
    session.add(job)
    add_audit_event(session, "LogisticsManualClosedLogisticsQueued", "LogisticsTask", task.id, {"task_no": task.task_no})
    return job


def add_audit_event(session: Session, event_type: str, object_type: str, object_id: str, detail: dict, actor: str = "System") -> None:
    session.add(
        AuditEvent(
            event_type=event_type,
            actor=actor,
            related_object_type=object_type,
            related_object_id=object_id,
            detail=dumps(detail),
            created_at=now_utc(),
        )
    )


def force_close_logistics_task_manual(session: Session, task: LogisticsTask, *, reason: str = "", actor: str = "business-owner") -> list[OutboundMailJob]:
    if task.status == "Closed":
        raise ValueError("logistics task is already closed")
    clean_reason = reason.strip()
    task.status = "Closed"
    task.closed_reason = "ManualForceClosed"
    task.closed_at = now_utc()
    task.updated_at = now_utc()
    if not task.production_task_id:
        task.requirement.status = "Closed"
        task.requirement.updated_at = now_utc()
    for item in task.items:
        if item.status not in {"Shipped", "NeedProduction"}:
            item.status = "Closed"
            item.updated_at = now_utc()
    outbound_jobs = [
        enqueue_manual_close_logistics_sales_notice(session, task, reason=clean_reason),
        enqueue_manual_close_logistics_notice(session, task, reason=clean_reason),
    ]
    valid_jobs = [job for job in outbound_jobs if job is not None]
    add_audit_event(
        session,
        "LogisticsTaskManualForceClosed",
        "LogisticsTask",
        task.id,
        {"reason": clean_reason, "outbound_job_ids": [job.id for job in valid_jobs]},
        actor,
    )
    return valid_jobs


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
                OutboundMailJob.body.ilike(pattern),
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
        details.append(f"最近 worker 完成时间：{format_display_time(worker['last_finished_at'])}")
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


@app.post("/api/outbound-mails/clear-queue")
def clear_outbound_queue(
    payload: AdminPasswordRequest, 
    request: Request, 
    session: Session = Depends(get_session)
) -> dict:
    require_admin_password(payload.admin_password)
    
    now = now_utc()
    actor = getattr(request.state, "username", "system")
    
    # We cancel both Pending and Failed jobs as they are part of the active queue
    count = (
        session.query(OutboundMailJob)
        .filter(OutboundMailJob.status.in_(["Pending", "Failed"]))
        .update({OutboundMailJob.status: "Cancelled"}, synchronize_session=False)
    )
    
    if count > 0:
        session.add(
            AuditEvent(
                event_type="OutboundQueueCleared",
                actor=actor,
                related_object_type="OutboundMailJob",
                related_object_id="bulk-clear",
                detail=dumps({"cancelled_count": count, "previous_statuses": ["Pending", "Failed"]}),
                created_at=now,
            )
        )
        session.commit()
    
    return {"cleared": count}



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
        bucket = to_beijing_time(case.created_at).strftime("%Y-%m-%d %H:00")
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

    subject = f"[外发队列告警][{format_beijing_time(now)}] 请处理异常邮件队列"
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
async def mailbox_auto_run_once() -> dict:
    try:
        return await asyncio.to_thread(run_mail_auto_worker_once)
    except RuntimeError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.get("/api/skills/list")
def list_skills() -> list:
    return registry.list_skills()


@app.post("/api/skills/generate")
async def generate_skill(request: Request, session: Session = Depends(get_session)) -> dict:
    body = await request.json()
    requirement = body.get("requirement")
    if not requirement:
        raise HTTPException(status_code=400, detail="requirement is required")
    
    try:
        # 1. 调用 LLM 生成代码
        gen_result = await SkillFactory.generate_skill(session, requirement)
        skill_name = gen_result["skill_name"]
        code = gen_result["code"]
        
        # 2. 保存并热加载
        success = SkillFactory.save_and_load(skill_name, code)
        
        if not success:
            raise HTTPException(status_code=500, detail="Failed to save or load the generated skill")
            
        return {
            "success": True,
            "skill_name": skill_name,
            "message": f"技能 {skill_name} 已生成并成功加载。",
            "code_preview": code[:500] + "..." if len(code) > 500 else code
        }
    except Exception as e:
        logger.exception("Skill generation failed")
        raise HTTPException(status_code=500, detail=str(e))

@app.delete("/api/skills/{name}")
def delete_skill(name: str) -> dict:
    success = SkillFactory.delete_skill(name)
    if not success:
        raise HTTPException(status_code=500, detail=f"Failed to delete skill {name}")
    return {"success": True}


@app.post("/api/skills/{name}/toggle")
def toggle_skill(name: str, active: bool = Query(...)) -> dict:
    success = SkillFactory.toggle_skill(name, active)
    if not success:
        raise HTTPException(status_code=500, detail=f"Failed to toggle skill {name}")
    return {"success": True}

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


@app.get("/api/integration-events")
def list_integration_events(
    q: str | None = None,
    status: str | None = None,
    event_type: str | None = None,
    source_system: str | None = None,
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
    session: Session = Depends(get_session),
) -> dict:
    query = session.query(IntegrationEvent)
    if q and q.strip():
        pattern = f"%{q.strip()}%"
        query = query.filter(
            or_(
                IntegrationEvent.trace_id.ilike(pattern),
                IntegrationEvent.biz_key.ilike(pattern),
                IntegrationEvent.event_type.ilike(pattern),
                IntegrationEvent.error_message.ilike(pattern),
            )
        )
    if status and status.strip():
        query = query.filter(IntegrationEvent.status == status.strip())
    if event_type and event_type.strip():
        query = query.filter(IntegrationEvent.event_type == event_type.strip())
    if source_system and source_system.strip():
        query = query.filter(IntegrationEvent.source_system == source_system.strip())
    return page_response(
        query.order_by(IntegrationEvent.created_at.desc()),
        serialize_integration_event,
        page,
        page_size,
        {
            "status_options": distinct_values(session, IntegrationEvent.status),
            "event_type_options": distinct_values(session, IntegrationEvent.event_type),
            "source_system_options": distinct_values(session, IntegrationEvent.source_system),
        },
    )


@app.get("/api/agent-run-logs")
def list_agent_run_logs(
    q: str | None = None,
    status: str | None = None,
    agent_name: str | None = None,
    task_type: str | None = None,
    page: int = Query(1, ge=1),
    page_size: int = Query(10, ge=1, le=100),
    session: Session = Depends(get_session),
) -> dict:
    query = session.query(AgentRunLog)
    if q and q.strip():
        pattern = f"%{q.strip()}%"
        query = query.filter(
            or_(
                AgentRunLog.id.ilike(pattern),
                AgentRunLog.agent_name.ilike(pattern),
                AgentRunLog.task_type.ilike(pattern),
                AgentRunLog.related_object_type.ilike(pattern),
                AgentRunLog.related_object_id.ilike(pattern),
                AgentRunLog.error_message.ilike(pattern),
            )
        )
    if status and status.strip():
        query = query.filter(AgentRunLog.status == status.strip())
    if agent_name and agent_name.strip():
        query = query.filter(AgentRunLog.agent_name == agent_name.strip())
    if task_type and task_type.strip():
        query = query.filter(AgentRunLog.task_type == task_type.strip())
    return page_response(
        query.order_by(AgentRunLog.started_at.desc()),
        serialize_agent_run_log,
        page,
        page_size,
        {
            "status_options": distinct_values(session, AgentRunLog.status),
            "agent_name_options": distinct_values(session, AgentRunLog.agent_name),
            "task_type_options": distinct_values(session, AgentRunLog.task_type),
        },
    )


@app.get("/api/model-call-logs")
def list_model_call_logs(
    q: str | None = None,
    status: str | None = None,
    task_type: str | None = None,
    page: int = Query(1, ge=1),
    page_size: int = Query(10, ge=1, le=100),
    session: Session = Depends(get_session),
) -> dict:
    query = session.query(ModelCallLog)
    if q and q.strip():
        pattern = f"%{q.strip()}%"
        query = query.filter(
            or_(
                ModelCallLog.id.ilike(pattern),
                ModelCallLog.task_type.ilike(pattern),
                ModelCallLog.related_object_type.ilike(pattern),
                ModelCallLog.related_object_id.ilike(pattern),
                ModelCallLog.status.ilike(pattern),
                ModelCallLog.error_message.ilike(pattern),
            )
        )
    if status and status.strip():
        query = query.filter(ModelCallLog.status == status.strip())
    if task_type and task_type.strip():
        query = query.filter(ModelCallLog.task_type == task_type.strip())
    return page_response(
        query.order_by(ModelCallLog.created_at.desc()),
        serialize_model_call_log,
        page,
        page_size,
        {
            "status_options": distinct_values(session, ModelCallLog.status),
            "task_type_options": distinct_values(session, ModelCallLog.task_type),
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


@app.get("/api/global-exception-ticker")
def global_exception_ticker(session: Session = Depends(get_session)) -> dict:
    return {"items": global_exception_ticker_items(session)}


def global_exception_ticker_items(session: Session, *, limit: int = 8) -> list[dict]:
    items: list[dict] = []
    open_exceptions = (
        session.query(ExceptionCase)
        .filter(ExceptionCase.status.in_(["Open", "Assigned"]), ExceptionCase.severity.in_(["Critical", "High"]))
        .order_by(ExceptionCase.due_at.asc().nullslast(), ExceptionCase.created_at.desc())
        .limit(limit)
        .all()
    )
    for case in open_exceptions:
        sla = exception_sla_status(case)
        priority = 1 if case.severity == "Critical" or sla == "overdue" else 2
        items.append(
            {
                "type": "exception",
                "priority": priority,
                "tone": "danger" if priority == 1 else "warn",
                "title": f"{case.exception_type} · {case.severity}",
                "message": exception_summary(case),
                "href": "#exceptions",
                "related_id": case.id,
                "sla_status": sla,
                "created_at": case.created_at.isoformat() if case.created_at else None,
            }
        )
    failed_jobs = (
        session.query(ProcessingJob)
        .filter(ProcessingJob.status == "Failed")
        .order_by(ProcessingJob.updated_at.desc(), ProcessingJob.created_at.desc())
        .limit(limit)
        .all()
    )
    for job in failed_jobs:
        items.append(
            {
                "type": "processing_dead_letter",
                "priority": 2,
                "tone": "warn",
                "title": f"处理队列失败 · {job.job_type}",
                "message": job.error_message or "处理队列任务失败",
                "href": "#ops",
                "related_id": job.id,
                "created_at": job.updated_at.isoformat() if job.updated_at else job.created_at.isoformat(),
            }
        )
    failed_outbounds = (
        session.query(OutboundMailJob)
        .filter(OutboundMailJob.status == "Failed")
        .order_by(OutboundMailJob.created_at.desc())
        .limit(limit)
        .all()
    )
    for job in failed_outbounds:
        items.append(
            {
                "type": "outbound_dead_letter",
                "priority": 3,
                "tone": "warn",
                "title": f"通知死信 · {job.mail_type}",
                "message": job.last_error or job.subject,
                "href": "#outbound",
                "related_id": job.id,
                "created_at": job.created_at.isoformat() if job.created_at else None,
            }
        )
    return sorted(items, key=lambda item: (item["priority"], item.get("created_at") or ""), reverse=False)[:limit]


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


@app.get("/api/exceptions/{exception_id}/context")
def exception_context(exception_id: str, session: Session = Depends(get_session), current_user: User = Depends(get_current_user)) -> dict:
    case = session.get(ExceptionCase, exception_id)
    if case is None:
        raise HTTPException(status_code=404, detail="exception not found")
    return build_exception_context(session, case, current_user=current_user)


@app.post("/api/exceptions/{exception_id}/diagnosis-feedback")
def exception_diagnosis_feedback(exception_id: str, payload: dict, request: Request, session: Session = Depends(get_session)) -> dict:
    case = session.get(ExceptionCase, exception_id)
    if case is None:
        raise HTTPException(status_code=404, detail="exception not found")
    feedback = str(payload.get("feedback") or "").strip()
    if feedback not in {"accepted", "modified", "rejected"}:
        raise HTTPException(status_code=400, detail="feedback must be accepted, modified, or rejected")
    actor = str(payload.get("actor") or getattr(request.state, "username", "operator") or "operator").strip()
    note = str(payload.get("note") or "").strip()
    detail = loads(case.detail, {})
    history = detail.get("ai_feedback")
    if not isinstance(history, list):
        history = []
    entry = {
        "feedback": feedback,
        "note": note,
        "actor": actor,
        "created_at": now_utc().isoformat(),
    }
    history.append(entry)
    detail["ai_feedback"] = history
    case.detail = dumps(detail)
    case.last_actor = actor
    case.updated_at = now_utc()
    session.add(
        AuditEvent(
            event_type="ExceptionDiagnosisFeedbackRecorded",
            actor=actor,
            related_object_type="ExceptionCase",
            related_object_id=case.id,
            detail=dumps(entry),
            created_at=now_utc(),
        )
    )
    session.commit()
    return {"exception": serialize_exception(case), "feedback": entry}


HIGH_RISK_EXCEPTION_TYPES = {
    "CRM_CHANGED_AFTER_OMS_ACCEPTED",
    "CRM_CANCELLED_AFTER_OMS_ACCEPTED",
    "CRM_CHANGED_DURING_PICKING",
    "CRM_CHANGED_AFTER_SHIPPED",
    "CRM_CANCELLED_AFTER_SHIPPED",
    "OMS_IDEMPOTENCY_CONFLICT",
}


def is_high_risk_exception(case: ExceptionCase) -> bool:
    return case.exception_type in HIGH_RISK_EXCEPTION_TYPES


def validate_high_risk_exception_resolution(session: Session, case: ExceptionCase, payload: ExceptionResolveRequest) -> None:
    if not is_high_risk_exception(case):
        return
    note = payload.note.strip()
    actor = payload.actor.strip()
    missing: list[str] = []
    if not payload.confirm_risk:
        missing.append("二次确认")
    if len(note) < 6:
        missing.append("处理备注")
    if not actor or actor.lower() in {"operator", "system", "admin", "manager", "business-owner"}:
        missing.append("责任人身份")
    if not missing:
        return
    session.add(
        AuditEvent(
            event_type="UNAUTHORIZED_STATE_OVERRIDE",
            actor=actor or "unknown",
            related_object_type="ExceptionCase",
            related_object_id=case.id,
            detail=dumps(
                {
                    "attempted_action": "resolve_exception",
                    "exception_type": case.exception_type,
                    "severity": case.severity,
                    "missing": missing,
                }
            ),
            created_at=now_utc(),
        )
    )
    session.commit()
    raise HTTPException(status_code=403, detail=f"高危异常关闭需补充：{'、'.join(missing)}")


def exception_resolution_evidence(case: ExceptionCase, payload: ExceptionResolveRequest) -> dict:
    detail = loads(case.detail, {})
    provided = payload.resolution_evidence if isinstance(payload.resolution_evidence, dict) else {}
    refs: list[str] = []
    exception_detail = detail.get("exception") if isinstance(detail.get("exception"), dict) else {}
    validation = detail.get("validation") if isinstance(detail.get("validation"), dict) else {}
    order = detail.get("order") if isinstance(detail.get("order"), dict) else {}
    for value in [
        detail.get("evidence_refs"),
        exception_detail.get("evidence_refs"),
        provided.get("evidence_refs"),
    ]:
        if isinstance(value, list):
            refs.extend(str(item) for item in value if str(item).strip())
    for item in validation.get("failed_rules") or []:
        if isinstance(item, dict) and isinstance(item.get("evidenceRefs"), list):
            refs.extend(str(ref) for ref in item["evidenceRefs"] if str(ref).strip())
    if not refs and detail.get("message"):
        refs.append(f"异常详情：{detail.get('message')}")
    evidence = {
        "type": "MANUAL_EXCEPTION_RESOLUTION",
        "exception_type": case.exception_type,
        "severity": case.severity,
        "note": payload.note.strip(),
        "actor": payload.actor.strip(),
        "confirmed_at": now_utc().isoformat(),
        "evidence_refs": list(dict.fromkeys(refs)),
        "order_no": order.get("order_no") or detail.get("order_no"),
        "crm_order_no": order.get("crm_order_no") or detail.get("crm_order_no"),
    }
    evidence.update({key: value for key, value in provided.items() if key not in evidence})
    return evidence


@app.post("/api/exceptions/{exception_id}/resolve")
def resolve_exception(exception_id: str, payload: ExceptionResolveRequest, session: Session = Depends(get_session)) -> dict:
    case = session.get(ExceptionCase, exception_id)
    if case is None:
        raise HTTPException(status_code=404, detail="exception not found")
    validate_high_risk_exception_resolution(session, case, payload)
    try:
        case = resolve_exception_case(session, exception_id, payload.note, actor=payload.actor)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    if case.severity in {"Critical", "High"} or payload.resolution_evidence:
        case.resolution_evidence_json = dumps(exception_resolution_evidence(case, payload))
    session.commit()
    return serialize_exception(case)


@app.post("/api/exceptions/{exception_id}/assign")
def assign_exception(exception_id: str, payload: ExceptionAssignRequest, session: Session = Depends(get_session)) -> dict:
    case = session.get(ExceptionCase, exception_id)
    if case is None:
        raise HTTPException(status_code=404, detail="exception not found")
    assignee = payload.assignee.strip()
    if not assignee:
        raise HTTPException(status_code=400, detail="assignee required")
    case.assignee = assignee
    if case.status == "Open":
        case.status = "Assigned"
    case.last_actor = payload.actor
    case.updated_at = now_utc()
    session.add(
        AuditEvent(
            event_type="ExceptionAssigned",
            actor=payload.actor,
            related_object_type="ExceptionCase",
            related_object_id=case.id,
            detail=dumps({"assignee": assignee, "note": payload.note}),
            created_at=now_utc(),
        )
    )
    session.commit()
    return serialize_exception(case)


@app.post("/api/exceptions/{exception_id}/reopen")
def reopen_exception(exception_id: str, payload: ExceptionReopenRequest, session: Session = Depends(get_session)) -> dict:
    case = session.get(ExceptionCase, exception_id)
    if case is None:
        raise HTTPException(status_code=404, detail="exception not found")
    case.status = "Open"
    case.resolution_note = None
    case.reopened_at = now_utc()
    case.resolved_at = None
    case.last_actor = payload.actor
    case.updated_at = now_utc()
    session.add(
        AuditEvent(
            event_type="ExceptionReopened",
            actor=payload.actor,
            related_object_type="ExceptionCase",
            related_object_id=case.id,
            detail=dumps({"note": payload.note}),
            created_at=now_utc(),
        )
    )
    session.commit()
    return serialize_exception(case)


@app.post("/api/exceptions/{exception_id}/diagnose")
def diagnose_exception(exception_id: str, request: Request, async_job: bool = False, session: Session = Depends(get_session)) -> dict:
    case = session.get(ExceptionCase, exception_id)
    if case is None:
        raise HTTPException(status_code=404, detail="exception not found")
    actor = getattr(request.state, "username", "operator")
    try:
        if async_job:
            job = enqueue_exception_diagnosis(session, case, source=actor)
            session.commit()
            return {"queued": True, "job_id": job.id, "exception_id": case.id}
        diagnosis = diagnose_exception_case(session, case.id, actor=actor)
        session.commit()
        return {"diagnosis": diagnosis, "exception": serialize_exception(case)}
    except ValueError as exc:
        session.rollback()
        raise HTTPException(status_code=400, detail=str(exc)) from exc


def sse_chunk(event: str, data: dict) -> str:
    return f"event: {event}\ndata: {dumps(data)}\n\n"


def diagnose_exception_stream_chunks(session: Session, exception_id: str, *, actor: str = "operator"):
    case = session.get(ExceptionCase, exception_id)
    if case is None:
        yield sse_chunk("error", {"message": "exception not found", "exception_id": exception_id})
        return
    yield sse_chunk("loading", {"message": "正在组装异常 ContextPack", "exception_id": case.id})
    yield sse_chunk("partial", {"message": "已读取异常详情与订单证据", "exception_type": case.exception_type})
    try:
        diagnosis = diagnose_exception_case(session, case.id, actor=actor)
        session.commit()
        yield sse_chunk("done", {"diagnosis": diagnosis, "exception": serialize_exception(case)})
    except Exception as exc:
        session.rollback()
        yield sse_chunk("error", {"message": str(exc), "exception_id": exception_id})


@app.get("/api/exceptions/{exception_id}/diagnose-stream")
def diagnose_exception_stream(exception_id: str, request: Request, session: Session = Depends(get_session)) -> StreamingResponse:
    actor = getattr(request.state, "username", "operator")
    return StreamingResponse(
        diagnose_exception_stream_chunks(session, exception_id, actor=actor),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


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


@app.post("/api/exceptions/{exception_id}/apply-address-correction")
def apply_exception_address_correction(
    exception_id: str,
    session: Session = Depends(get_session),
    current_user: User = Depends(require_role(["admin", "business_owner", "business_operator"])),
) -> dict:
    from backend.app.services.order_middle_platform import (
        run_validation_chain,
        transition_order,
        OrderEvent,
        OrderStatus,
        BlockerLevel,
        is_platform_fulfilled_order,
        archive_platform_fulfilled_order,
        create_delivery_notice,
        confirm_delivery_notice,
        config_bool,
        build_context_pack,
    )
    import hashlib

    case = session.get(ExceptionCase, exception_id)
    if case is None:
        raise HTTPException(status_code=404, detail="异常未找到")
    detail = loads(case.detail, {})
    ai_diag = detail.get("ai_diagnosis", {})
    correction = ai_diag.get("address_correction")
    if not correction:
        raise HTTPException(status_code=400, detail="该异常中未找到可用的 AI 地址修正建议")

    order = exception_middle_order(session, case, detail)
    if not order:
        raise HTTPException(status_code=400, detail="未找到关联的中台订单")

    crm_order = order.crm_order
    if not crm_order:
        raise HTTPException(status_code=400, detail="未找到关联的 CRM 订单")

    # Enforce data visibility scope for sales/business operators
    if hasattr(current_user, "role") and current_user.role == "business_operator":
        is_owner = (crm_order.sales_user_name == current_user.username)
        is_same_dept = bool(current_user.department and crm_order.owner_department and crm_order.owner_department.lower() == current_user.department.lower())
        if not is_owner and not is_same_dept:
            raise HTTPException(status_code=403, detail="没有权限对该订单的异常进行地址修正")

    # Save original values for Audit/Log
    orig_address = crm_order.receipt_address
    orig_contact = crm_order.receipt_contact
    orig_phone = crm_order.receipt_phone

    # Apply corrections
    new_address = correction.get("receipt_address")
    new_contact = correction.get("receipt_contact")
    new_phone = correction.get("receipt_phone")

    if new_address:
        crm_order.receipt_address = new_address
    if new_contact:
        crm_order.receipt_contact = new_contact
    if new_phone:
        crm_order.receipt_phone = new_phone

    # Update raw_json to keep them in sync
    raw = loads(crm_order.raw_json, {})
    if new_address:
        raw["receipt_address"] = new_address
    if new_contact:
        raw["receipt_contact"] = new_contact
    if new_phone:
        raw["receipt_phone"] = new_phone
    
    new_raw_json = dumps(raw)
    crm_order.raw_json = new_raw_json
    crm_order.payload_hash = hashlib.sha256(new_raw_json.encode("utf-8")).hexdigest()
    order.payload_hash = crm_order.payload_hash
    crm_order.updated_at = now_utc()
    order.updated_at = now_utc()

    # Log audit event for correction
    session.add(
        AuditEvent(
            event_type="ExceptionAddressCorrectionApplied",
            actor="operator",
            related_object_type="ExceptionCase",
            related_object_id=case.id,
            detail=dumps({
                "original": {"address": orig_address, "contact": orig_contact, "phone": orig_phone},
                "corrected": {"address": new_address, "contact": new_contact, "phone": new_phone},
                "reason": correction.get("reason")
            }),
            created_at=now_utc()
        )
    )

    # Re-run order validation to try and clear the blocking exception
    trace_id = f"fix-address-{uuid.uuid4()}"
    
    if order.status == OrderStatus.VALIDATION_BLOCKED.value:
        transition_order(session, order, OrderEvent.EXCEPTION_RESOLVED_AND_REVALIDATE, trace_id=trace_id)
    else:
        transition_order(session, order, OrderEvent.START_VALIDATION, trace_id=trace_id)

    validation_results = run_validation_chain(session, order)
    order.validation_summary_json = dumps({"results": [r.as_dict() for r in validation_results]})
    critical = next((r for r in validation_results if r.blocker_level == BlockerLevel.CRITICAL), None)

    if critical is not None:
        transition_order(session, order, OrderEvent.RULES_FAILED_CRITICAL, trace_id=trace_id, detail={"rule_code": critical.rule_code})
        # Keep exception case open, but update detail with new validation summary
        case.detail = dumps(build_context_pack(session, order, ExceptionType(case.exception_type), case.severity, critical.reason, validation_results, trace_id=trace_id))
        session.add(case)
        session.commit()
        return {
            "success": False,
            "message": f"地址修复已应用，但订单仍未通过预审：{critical.reason}",
            "order_status": order.status
        }
    
    # Validation passed!
    transition_order(session, order, OrderEvent.RULES_PASSED, trace_id=trace_id)
    
    # Resolve exception case
    case.status = "Resolved"
    case.resolved_at = now_utc()
    case.resolution_note = f"AI地址修复应用成功。原始地址: {orig_address} -> 修复后: {new_address or orig_address}"
    session.add(case)

    # Continue standard delivery generation flow
    notice = None
    if is_platform_fulfilled_order(order, crm_order):
        archive_platform_fulfilled_order(session, order, crm_order, trace_id=trace_id)
    else:
        notice = create_delivery_notice(session, order)
        transition_order(session, order, OrderEvent.DELIVERY_NOTICE_CREATED, trace_id=trace_id, detail={"notice_no": notice.notice_no})
        if config_bool(session, "oms_auto_confirm_delivery_notice", False):
            confirm_delivery_notice(session, notice, confirmed_by="auto", trace_id=trace_id)

    session.commit()
    return {
        "success": True,
        "message": "AI地址修复一键应用成功，订单已通过预审并自动流转",
        "order_status": order.status,
        "notice_no": notice.notice_no if notice else None
    }


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


@app.get("/api/logistics-departments")
def list_logistics_departments(
    q: str | None = None,
    status: str | None = None,
    page: int = Query(1, ge=1),
    page_size: int = Query(10, ge=1, le=100),
    session: Session = Depends(get_session),
) -> dict:
    query = session.query(LogisticsDepartment)
    if q and q.strip():
        pattern = f"%{q.strip()}%"
        query = query.filter(
            or_(
                LogisticsDepartment.department_code.ilike(pattern),
                LogisticsDepartment.department_name.ilike(pattern),
                LogisticsDepartment.mail_to_json.ilike(pattern),
                LogisticsDepartment.mail_cc_json.ilike(pattern),
                LogisticsDepartment.status.ilike(pattern),
            )
        )
    if status and status.strip():
        query = query.filter(LogisticsDepartment.status == status.strip())
    else:
        query = query.filter(LogisticsDepartment.status != "Deleted")
    return page_response(
        query.order_by(LogisticsDepartment.department_code),
        serialize_logistics_department,
        page,
        page_size,
        {
            "status_options": [
                value
                for value in distinct_values(session, LogisticsDepartment.status)
                if value != "Deleted"
            ]
        },
    )


def save_logistics_department(payload: DepartmentUpsert, session: Session) -> dict:
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
    dept = session.query(LogisticsDepartment).filter_by(department_code=department_code).one_or_none()
    if dept is None:
        dept = LogisticsDepartment(department_code=department_code)
        session.add(dept)
    dept.department_name = department_name
    dept.mail_to_json = dumps(mail_to)
    dept.mail_cc_json = dumps(mail_cc)
    dept.status = "Active"
    dept.updated_at = now_utc()
    session.commit()
    return {"ok": True, "department_id": dept.id}


@app.post("/api/logistics-departments")
def create_or_update_logistics_department(payload: DepartmentUpsert, session: Session = Depends(get_session)) -> dict:
    return save_logistics_department(payload, session)


@app.delete("/api/logistics-departments/{department_id}")
def delete_logistics_department(department_id: str, request: Request, session: Session = Depends(get_session)) -> dict:
    dept = session.get(LogisticsDepartment, department_id)
    if dept is None or dept.status == "Deleted":
        raise HTTPException(status_code=404, detail="logistics department not found")
    dept.status = "Deleted"
    dept.updated_at = now_utc()
    actor = getattr(request.state, "username", "system")
    session.add(
        AuditEvent(
            event_type="LogisticsDepartmentDeleted",
            actor=actor,
            related_object_type="LogisticsDepartment",
            related_object_id=dept.id,
            detail=dumps(
                {
                    "department_code": dept.department_code,
                    "department_name": dept.department_name,
                }
            ),
            created_at=now_utc(),
        )
    )
    session.commit()
    return {"ok": True, "department": serialize_logistics_department(dept)}


@app.put("/api/logistics-departments/default")
def upsert_default_logistics_department(payload: DepartmentUpsert, session: Session = Depends(get_session)) -> dict:
    return save_logistics_department(payload, session)


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


def serialize_logistics_department(row: LogisticsDepartment) -> dict:
    return {
        "id": row.id,
        "department_code": row.department_code,
        "department_name": row.department_name,
        "mail_to": as_list(row.mail_to_json),
        "mail_cc": as_list(row.mail_cc_json),
        "status": row.status,
    }


def serialize_outbound_mail(row: OutboundMailJob, session: Session | None = None, *, include_body: bool = False) -> dict:
    related = infer_related_task_for_outbound(session, row)
    payload = {
        "id": row.id,
        "mail_type": row.mail_type,
        "to": as_list(row.to_json),
        "cc": as_list(row.cc_json),
        "subject": row.subject,
        "status": row.status,
        "created_at": row.created_at.isoformat(),
        "sent_at": row.sent_at.isoformat() if row.sent_at else None,
        "related_task_id": related["id"],
        "related_task_no": related["task_no"],
        "related_task_type": related["type"],
        "pending_diagnosis": outbound_pending_diagnosis(session, row) if session is not None else None,
    }
    if include_body:
        payload.update(
            {
                "body": row.body,
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
        "version": row.version,
        "error_message": row.error_message,
        "created_at": row.created_at.isoformat(),
    }


def serialize_integration_event(row: IntegrationEvent) -> dict:
    return {
        "id": row.id,
        "trace_id": row.trace_id,
        "source_system": row.source_system,
        "event_type": row.event_type,
        "biz_key": row.biz_key,
        "payload_hash": row.payload_hash,
        "status": row.status,
        "retry_count": row.retry_count,
        "error_message": row.error_message,
        "request": loads(row.request_json, {}) if row.request_json else None,
        "response": loads(row.response_json, {}) if row.response_json else None,
        "created_at": row.created_at.isoformat(),
        "updated_at": row.updated_at.isoformat() if row.updated_at else None,
    }


def serialize_agent_run_log(row: AgentRunLog) -> dict:
    return {
        "id": row.id,
        "agent_name": row.agent_name,
        "task_type": row.task_type,
        "related_object_type": row.related_object_type,
        "related_object_id": row.related_object_id,
        "input": loads(row.input_json, {}) if row.input_json else {},
        "output": loads(row.output_json, {}) if row.output_json else {},
        "status": row.status,
        "error_message": row.error_message,
        "started_at": row.started_at.isoformat(),
        "finished_at": row.finished_at.isoformat() if row.finished_at else None,
    }


def serialize_model_call_log(row: ModelCallLog) -> dict:
    return {
        "id": row.id,
        "provider_config_id": row.provider_config_id,
        "task_type": row.task_type,
        "related_object_type": row.related_object_type,
        "related_object_id": row.related_object_id,
        "input_summary": loads(row.input_summary, {}) if row.input_summary else {},
        "output": loads(row.output_json, {}) if row.output_json else {},
        "latency_ms": row.latency_ms,
        "status": row.status,
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


def serialize_mail(row: MailMessage, session: Session | None = None) -> dict:
    active_session = session or object_session(row)
    if active_session is None:
        with SessionLocal() as fallback_session:
            related = infer_related_task_for_mail(fallback_session, row)
    else:
        related = infer_related_task_for_mail(active_session, row)
    return {
        "id": row.id,
        "direction": row.direction,
        "from_address": row.from_address,
        "to": as_list(row.to_json),
        "cc": as_list(row.cc_json),
        "subject": row.subject,
        "classification": row.classification,
        "classification_confidence": row.classification_confidence,
        "related_task_id": related["id"],
        "related_task_no": related["task_no"],
        "related_task_type": related["type"],
        "received_at": (row.received_at or row.created_at).isoformat(),
        "created_at": row.created_at.isoformat(),
    }


def empty_related_task() -> dict[str, str]:
    return {"id": "", "task_no": "", "type": ""}


def production_task_related(session: Session, task_id: str | None) -> dict[str, str]:
    if not task_id:
        return empty_related_task()
    task = session.get(ProductionTask, task_id)
    if task is None:
        return {"id": task_id, "task_no": "", "type": "production"}
    return {"id": task.id, "task_no": task.task_no, "type": "production"}


def logistics_task_related(task: LogisticsTask | None) -> dict[str, str]:
    if task is None:
        return empty_related_task()
    return {"id": task.id, "task_no": task.task_no, "type": "logistics"}


def find_logistics_task_by_text(session: Session, text: str) -> LogisticsTask | None:
    for task_no in LOGISTICS_TASK_NO_PATTERN.findall(text or ""):
        task = session.query(LogisticsTask).filter(func.upper(LogisticsTask.task_no) == task_no.upper()).one_or_none()
        if task is not None:
            return task
    return None


def infer_related_task_for_mail(session: Session, row: MailMessage) -> dict[str, str]:
    if row.related_task_id:
        return production_task_related(session, row.related_task_id)
    task = find_logistics_task_by_text(session, f"{row.subject}\n{row.body_text}")
    if task is not None:
        return logistics_task_related(task)
    task = (
        session.query(LogisticsTask)
        .join(OrderRequirement, OrderRequirement.id == LogisticsTask.requirement_id)
        .filter(OrderRequirement.source_mail_id == row.id)
        .order_by(LogisticsTask.created_at.desc())
        .first()
    )
    return logistics_task_related(task)


def infer_related_task_for_outbound(session: Session | None, row: OutboundMailJob) -> dict[str, str]:
    if session is None:
        return empty_related_task()
    if row.related_task_id:
        return production_task_related(session, row.related_task_id)
    return logistics_task_related(find_logistics_task_by_text(session, f"{row.subject}\n{row.body}"))


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

def serialize_crm_order(row: CrmSalesOrder, *, include_raw: bool = False, current_user: User | None = None) -> dict:
    raw = loads(row.raw_json, {})
    extraction = raw.get("oms_field_extraction") if isinstance(raw, dict) else {}
    if not isinstance(extraction, dict):
        extraction = {}
    validation_errors = extraction.get("validation_errors")
    if isinstance(validation_errors, list):
        contact_validation_errors = validation_errors
    else:
        contact_validation_errors = as_list(validation_errors)

    mask = should_mask_financials(current_user, row.sales_user_name, row.owner_department)

    data = {
        "id": row.id,
        "source_system": row.source_system,
        "crm_order_id": row.crm_order_id,
        "crm_order_no": row.crm_order_no,
        "customer_id": row.customer_id,
        "customer_name": row.customer_name,
        "opportunity_id": row.opportunity_id,
        "opportunity_name": row.opportunity_name,
        "sales_user_id": row.sales_user_id,
        "sales_user_name": row.sales_user_name,
        "owner_department": row.owner_department,
        "life_status": row.life_status,
        "approval_status": row.approval_status,
        "order_date": row.order_date,
        "settlement_method": row.settlement_method,
        "currency": row.currency,
        "order_amount": "***" if mask else row.order_amount,
        "received_amount": "***" if mask else row.received_amount,
        "receivable_amount": "***" if mask else row.receivable_amount,
        "invoice_amount": "***" if mask else row.invoice_amount,
        "product_amount": "***" if mask else row.product_amount,
        "logistics_status": row.logistics_status,
        "shipment_status": row.shipment_status,
        "invoice_status": row.invoice_status,
        "receipt_contact": row.receipt_contact,
        "receipt_phone": row.receipt_phone,
        "receipt_address": row.receipt_address,
        "delivery_date": row.delivery_date,
        "remark": row.remark,
        "attachment_files": as_list(row.attachment_files_json),
        "sync_status": row.sync_status,
        "synced_at": row.synced_at.isoformat() if row.synced_at else None,
        "source_created_at": row.source_created_at.isoformat() if row.source_created_at else None,
        "source_updated_at": row.source_updated_at.isoformat() if row.source_updated_at else None,
        "created_at": row.created_at.isoformat() if row.created_at else None,
        "updated_at": row.updated_at.isoformat() if row.updated_at else None,
        "contact_extraction_confidence": extraction.get("confidence"),
        "contact_extraction_source": extraction.get("source"),
        "contact_extraction_manual_review_required": bool(extraction.get("manual_review_required")),
        "contact_extraction_validation_errors": contact_validation_errors,
    }
    if include_raw:
        if mask:
            raw_masked = dict(raw)
            for f in ("order_amount", "received_amount", "receivable_amount", "invoice_amount", "product_amount"):
                if f in raw_masked:
                    raw_masked[f] = "***"
            data["raw"] = raw_masked
        else:
            data["raw"] = raw
    return data


def serialize_order_attachment(row: OrderAttachment, current_user: User | None = None) -> dict:
    cached_ref = local_storage_ref(row)

    is_financial = False
    if row.attachment_type and row.attachment_type.lower() in ("invoice", "contract", "paymentreceipt", "purchaseorder"):
        is_financial = True

    mask = False
    if is_financial and current_user is not None:
        sales_user = row.crm_order.sales_user_name if row.crm_order else None
        dept = row.crm_order.owner_department if row.crm_order else None
        mask = should_mask_financials(current_user, sales_user, dept)

    has_download = bool(cached_ref or row.file_url) and not mask
    return {
        "id": row.id,
        "file_name": row.file_name,
        "file_url": "" if mask else row.file_url,
        "download_url": f"/api/crm/order-attachments/{row.id}/download" if has_download else "",
        "is_cached": bool(cached_ref) and not mask,
        "source_file_id": row.source_file_id,
        "attachment_type": row.attachment_type,
        "parse_status": row.parse_status,
        "has_download": has_download,
        "source_system": row.source_system,
        "captured_at": row.captured_at.isoformat() if row.captured_at else None,
    }


def resolve_crm_product_material_sku(session: Session, *, raw_sku_code: str | None = None, product_name: str | None = None) -> dict:
    raw_code = str(raw_sku_code or "").strip()
    if raw_code:
        sku = session.query(ProductSKU).filter(ProductSKU.sku_id == raw_code, ProductSKU.status == "Active").first()
        if sku is not None:
            return {"sku_code": sku.sku_id, "sku_match_status": "matched", "sku_match_source": "sku_code", "sku_match_confidence": 100}
    name = str(product_name or "").strip()
    if name:
        match = match_sku_by_product_name(session, name)
        if match.get("matched") and match.get("sku_id"):
            return {
                "sku_code": str(match["sku_id"]),
                "sku_match_status": "matched",
                "sku_match_source": match.get("match_source") or "product_name",
                "sku_match_confidence": match.get("confidence"),
                "sku_matched_value": match.get("matched_value"),
            }
        return {
            "sku_code": "",
            "sku_match_status": "manual_required",
            "sku_match_source": match.get("reason") or "product_name",
            "sku_match_confidence": None,
            "sku_candidates": match.get("candidates") or [],
        }
    return {"sku_code": "", "sku_match_status": "manual_required", "sku_match_source": "missing_product_name", "sku_match_confidence": None}


def serialize_crm_order_item(session: Session, row: CrmOrderItem) -> dict:
    raw = loads(row.raw_json, {})
    product_name = row.product_name or raw.get("product_name") or raw.get("产品名称") or raw.get("商品名称")
    material_sku = resolve_crm_product_material_sku(session, raw_sku_code=row.sku_code, product_name=product_name)
    return {
        "id": row.id,
        "crm_item_id": row.crm_item_id,
        "sku_code": material_sku["sku_code"],
        "crm_raw_sku_code": row.sku_code,
        "sku_match_status": material_sku["sku_match_status"],
        "sku_match_source": material_sku["sku_match_source"],
        "sku_match_confidence": material_sku["sku_match_confidence"],
        "sku_candidates": material_sku.get("sku_candidates", []),
        "product_name": row.product_name,
        "specification": row.specification,
        "quantity": row.quantity,
        "unit_price": row.unit_price,
        "line_amount": row.line_amount,
        "discount": raw.get("discount") or raw.get("discount_amount") or raw.get("折扣") or raw.get("优惠金额"),
        "settlement_method": raw.get("settlement_method") or raw.get("订单结算方式") or raw.get("结算方式"),
        "raw": raw,
    }


def serialize_crm_order_raw_item(session: Session, item: dict, index: int) -> dict:
    raw = item if isinstance(item, dict) else {}
    def pick(keys: list[str]) -> str:
        for key in keys:
            value = raw.get(key)
            if value not in (None, ""):
                return str(value)
        return ""
    raw_sku_code = pick(["sku_code", "skuCode", "sku_id", "product_code", "商品编码", "产品编码", "SKU"])
    product_name = pick(["product_name", "name", "productName", "产品名称", "商品名称", "货物名称"])
    material_sku = resolve_crm_product_material_sku(session, raw_sku_code=raw_sku_code, product_name=product_name)

    return {
        "id": f"raw-{index + 1}",
        "crm_item_id": pick(["crm_item_id", "item_id", "id", "订单产品编号"]) or f"raw-{index + 1}",
        "sku_code": material_sku["sku_code"],
        "crm_raw_sku_code": raw_sku_code,
        "sku_match_status": material_sku["sku_match_status"],
        "sku_match_source": material_sku["sku_match_source"],
        "sku_match_confidence": material_sku["sku_match_confidence"],
        "sku_candidates": material_sku.get("sku_candidates", []),
        "product_name": product_name,
        "specification": pick(["specification", "model", "规格型号", "规格", "型号", "主要规格/详细配置"]),
        "quantity": pick(["quantity", "qty", "数量"]),
        "unit_price": pick(["unit_price", "price", "销售单价", "单价", "价格(元)", "不含税单价（元）"]),
        "line_amount": pick(["line_amount", "amount", "销售订单金额", "小计", "总价", "总金额（含税）", "不含税总价（元）"]),
        "discount": pick(["discount", "discount_amount", "折扣", "优惠金额"]),
        "settlement_method": pick(["settlement_method", "订单结算方式", "结算方式"]),
        "raw": raw,
        "source": "raw_json_fallback",
    }


def crm_order_attachment_payload(session: Session, row: CrmSalesOrder, current_user: User | None = None) -> list[dict]:
    records = (
        session.query(OrderAttachment)
        .filter(
            OrderAttachment.source_system == row.source_system,
            OrderAttachment.crm_order_id == row.crm_order_id,
            OrderAttachment.payload_hash == row.payload_hash,
        )
        .order_by(OrderAttachment.created_at.desc())
        .all()
    )
    if records:
        deduped_by_key: dict[str, dict] = {}
        for item in records:
            key = "|".join([str(item.source_file_id or "").strip().lower(), str(item.file_name or "").strip().lower()])
            current = deduped_by_key.get(key)
            payload = serialize_order_attachment(item, current_user=current_user)
            if current is not None and (current.get("has_download") or not payload.get("has_download")):
                continue
            deduped_by_key[key] = payload
        return list(deduped_by_key.values())
    fallback_names = []
    seen_names = set()
    for name in as_list(row.attachment_files_json):
        key = str(name or "").strip().lower()
        if not key or key in seen_names:
            continue
        seen_names.add(key)
        fallback_names.append(name)
    return [
        {
            "id": "",
            "file_name": name,
            "file_url": None,
            "source_file_id": None,
            "attachment_type": None,
            "parse_status": "NameOnly",
            "has_download": False,
            "source_system": row.source_system,
            "captured_at": row.synced_at.isoformat() if row.synced_at else None,
        }
        for name in fallback_names
    ]


def serialize_crm_order_snapshot(row: CrmOrderSnapshot) -> dict:
    return {
        "id": row.id,
        "crm_order_id": row.crm_order_id,
        "crm_order_no": row.crm_order_no,
        "payload_hash": row.payload_hash,
        "version": row.version,
        "is_latest": row.is_latest,
        "parse_status": row.parse_status,
        "captured_at": row.captured_at.isoformat() if row.captured_at else None,
        "created_at": row.created_at.isoformat() if row.created_at else None,
    }


def crm_order_snapshot_payload(session: Session, row: CrmSalesOrder) -> list[dict]:
    return [
        serialize_crm_order_snapshot(snapshot)
        for snapshot in (
            session.query(CrmOrderSnapshot)
            .filter(CrmOrderSnapshot.source_system == row.source_system, CrmOrderSnapshot.crm_order_id == row.crm_order_id)
            .order_by(CrmOrderSnapshot.version.desc(), CrmOrderSnapshot.created_at.desc())
            .limit(20)
            .all()
        )
    ]


def crm_snapshot_diff_payload(session: Session, row: CrmSalesOrder, *, current_payload_hash: str | None = None) -> dict:
    snapshots = (
        session.query(CrmOrderSnapshot)
        .filter(CrmOrderSnapshot.source_system == row.source_system, CrmOrderSnapshot.crm_order_id == row.crm_order_id)
        .order_by(CrmOrderSnapshot.version.desc(), CrmOrderSnapshot.created_at.desc())
        .all()
    )
    if not snapshots:
        return {"from_version": None, "to_version": None, "changes": []}
    latest = snapshots[0]
    current = next((item for item in snapshots if item.payload_hash == current_payload_hash), None) if current_payload_hash else None
    if current is None:
        current = snapshots[1] if len(snapshots) > 1 else latest
    if current.payload_hash == latest.payload_hash:
        return {
            "from_version": current.version,
            "to_version": latest.version,
            "from_payload_hash": current.payload_hash,
            "to_payload_hash": latest.payload_hash,
            "changes": [],
        }
    old_raw = loads(current.raw_json, {})
    new_raw = loads(latest.raw_json, {})
    fields = [
        ("customer", "客户", "raw_json.customer_name", lambda data: data.get("customer_name")),
        ("amount", "金额", "raw_json.order_amount/product_amount/receivable_amount", snapshot_amount_summary),
        ("sku", "SKU/商品", "raw_json.items[].sku_code", snapshot_sku_summary),
        ("quantity", "数量", "raw_json.items[].quantity", snapshot_quantity_summary),
        ("receiver", "收货信息", "raw_json.receipt_*", snapshot_receiver_summary),
        ("attachments", "附件列表", "raw_json.attachments/attachment_files", snapshot_attachment_summary),
        ("remark", "特殊要求/备注", "raw_json.remark", lambda data: data.get("remark")),
        ("crm_status", "CRM 状态", "raw_json.life_status/approval_status", snapshot_status_summary),
    ]
    changes = []
    for key, label, source_path, getter in fields:
        old_value = normalize_snapshot_diff_value(getter(old_raw))
        new_value = normalize_snapshot_diff_value(getter(new_raw))
        if old_value == new_value:
            continue
        changes.append(
            {
                "field": key,
                "field_label": label,
                "source_path": source_path,
                "old_value": old_value,
                "new_value": new_value,
                "confidence": 1.0,
                "source": "CRM 详情快照",
            }
        )
    return {
        "from_version": current.version,
        "to_version": latest.version,
        "from_payload_hash": current.payload_hash,
        "to_payload_hash": latest.payload_hash,
        "changes": changes,
    }


def normalize_snapshot_diff_value(value) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value.strip()
    if isinstance(value, (list, dict)):
        return dumps(value)
    return str(value).strip()


def snapshot_amount_summary(data: dict) -> dict:
    return {
        "order_amount": data.get("order_amount"),
        "product_amount": data.get("product_amount"),
        "received_amount": data.get("received_amount"),
        "receivable_amount": data.get("receivable_amount"),
    }


def snapshot_items(data: dict) -> list[dict]:
    items = data.get("items")
    return items if isinstance(items, list) else []


def snapshot_sku_summary(data: dict) -> list[str]:
    return [str(item.get("sku_code") or item.get("product_name") or item.get("name") or "").strip() for item in snapshot_items(data)]


def snapshot_quantity_summary(data: dict) -> list[str]:
    return [str(item.get("quantity") or item.get("qty") or "").strip() for item in snapshot_items(data)]


def snapshot_receiver_summary(data: dict) -> dict:
    return {
        "contact": data.get("receipt_contact"),
        "phone": data.get("receipt_phone"),
        "address": data.get("receipt_address"),
        "delivery_date": data.get("delivery_date"),
    }


def snapshot_attachment_summary(data: dict) -> list[str]:
    attachments = data.get("attachments")
    names: list[str] = []
    if isinstance(attachments, list):
        for item in attachments:
            if isinstance(item, dict):
                names.append(str(item.get("file_name") or item.get("name") or item.get("filename") or "").strip())
            else:
                names.append(str(item).strip())
    files = data.get("attachment_files")
    if isinstance(files, str):
        names.extend(part.strip() for part in re.split(r"[;,；，]", files) if part.strip())
    elif isinstance(files, list):
        names.extend(str(item).strip() for item in files if str(item).strip())
    return sorted({name for name in names if name})


def snapshot_status_summary(data: dict) -> dict:
    return {
        "life_status": data.get("life_status"),
        "approval_status": data.get("approval_status"),
        "logistics_status": data.get("logistics_status"),
        "shipment_status": data.get("shipment_status"),
    }


def serialize_crm_order_with_flow(session: Session, row: CrmSalesOrder, current_user: User | None = None) -> dict:
    data = serialize_crm_order(row, include_raw=True, current_user=current_user)
    attachments = crm_order_attachment_payload(session, row, current_user=current_user)
    order_items = (
        session.query(CrmOrderItem)
        .filter(CrmOrderItem.order_id == row.id)
        .order_by(CrmOrderItem.created_at.asc(), CrmOrderItem.crm_item_id.asc())
        .all()
    )
    has_downloadable_attachment = any(item.get("has_download") for item in attachments)
    raw = loads(row.raw_json, {})
    detail_synced = raw.get("detail_sync_status") == "Synced" or bool(raw.get("detail_raw"))
    detail_failed = raw.get("detail_sync_status") == "Failed"
    raw_order_items = raw.get("order_items") if isinstance(raw.get("order_items"), list) else []
    data["attachments"] = attachments
    data["order_items"] = [serialize_crm_order_item(session, item) for item in order_items]
    if not data["order_items"] and raw_order_items:
        data["order_items"] = [
            serialize_crm_order_raw_item(session, item, index)
            for index, item in enumerate(raw_order_items)
            if isinstance(item, dict)
        ]
        data["order_items_source"] = "raw_json_fallback"
    else:
        data["order_items_source"] = "crm_order_items"
    data["snapshots"] = crm_order_snapshot_payload(session, row)
    data["snapshot_diff"] = crm_snapshot_diff_payload(session, row)
    data["crm_detail_status"] = "detail_available" if detail_synced else ("detail_failed" if detail_failed else "list_only")
    if detail_synced:
        data["crm_detail_message"] = "已同步 CRM 订单详情" + ("，附件可下载" if has_downloadable_attachment else "，但 CRM 未返回附件下载地址")
    elif detail_failed:
        data["crm_detail_message"] = f"CRM 订单详情同步失败：{raw.get('detail_sync_error') or '未知错误'}"
    else:
        data["crm_detail_message"] = "当前只同步了 CRM 列表字段；销售、收货、备注、附件下载地址需拉取单条订单详情接口。"
    middle_order = (
        session.query(MiddlePlatformOrder)
        .filter(MiddlePlatformOrder.source_system == row.source_system, MiddlePlatformOrder.crm_order_id == row.crm_order_id)
        .order_by(MiddlePlatformOrder.created_at.desc())
        .first()
    )
    if middle_order is None:
        data["flow"] = {
            "middle_order": None,
            "steps": crm_order_flow_steps(row, None),
            "crm_snapshots": data["snapshots"],
            "snapshot_diff": data["snapshot_diff"],
            "exceptions": [],
            "audit_events": [],
            "processing_jobs": crm_order_processing_jobs(session, row, None),
        }
        return data

    exceptions = (
        session.query(ExceptionCase)
        .filter(ExceptionCase.detail.ilike(f"%{middle_order.order_no}%"))
        .order_by(ExceptionCase.created_at.desc())
        .limit(20)
        .all()
    )
    audit_events = (
        session.query(AuditEvent)
        .filter(AuditEvent.related_object_type == "MiddlePlatformOrder", AuditEvent.related_object_id == middle_order.id)
        .order_by(AuditEvent.created_at.desc())
        .limit(30)
        .all()
    )
    data["flow"] = {
        "middle_order": serialize_middle_order(middle_order, include_detail=True, current_user=current_user),
        "steps": crm_order_flow_steps(row, middle_order),
        "crm_snapshots": data["snapshots"],
        "snapshot_diff": crm_snapshot_diff_payload(session, row, current_payload_hash=middle_order.payload_hash),
        "exceptions": [
            {
                "id": item.id,
                "exception_type": item.exception_type,
                "severity": item.severity,
                "status": item.status,
                "created_at": item.created_at.isoformat() if item.created_at else None,
                "summary": exception_summary(item),
            }
            for item in exceptions
        ],
        "audit_events": [
            {
                "id": item.id,
                "event_type": item.event_type,
                "actor": item.actor,
                "created_at": item.created_at.isoformat() if item.created_at else None,
                "detail": loads(item.detail, {}),
            }
            for item in audit_events
        ],
        "processing_jobs": crm_order_processing_jobs(session, row, middle_order),
    }
    data["flow"]["risk_alert"] = crm_order_flow_risk_alert(data["flow"], middle_order)
    return data


def crm_order_flow_risk_alert(flow: dict, middle_order: MiddlePlatformOrder) -> dict | None:
    risk_exceptions = [
        item
        for item in flow.get("exceptions", [])
        if str(item.get("exception_type") or "").startswith(("CRM_CHANGED", "CRM_CANCELLED"))
    ]
    if not risk_exceptions:
        return None
    latest = risk_exceptions[0]
    snapshots = flow.get("crm_snapshots") or []
    notices = (flow.get("middle_order") or {}).get("delivery_notices") or []
    jobs = flow.get("processing_jobs") or []
    latest_snapshot = snapshots[0] if snapshots else {}
    current_snapshot = next((item for item in snapshots if item.get("payload_hash") == middle_order.payload_hash), latest_snapshot)
    frozen_jobs = [job for job in jobs if job.get("status") in {"Cancelled", "Failed"} and str(job.get("job_type") or "").startswith("OMS")]
    stale_notices = [notice for notice in notices if notice.get("status") in {"Stale", "Cancelled", "Blocked"}]
    downstream_notice = next((notice for notice in notices if notice.get("oms_order_no") or notice.get("status") in {"Accepted", "Picking", "Shipped"}), None)
    return {
        "level": "critical" if latest.get("severity") == "Critical" else "high",
        "exception_type": latest.get("exception_type"),
        "summary": latest.get("summary") or "CRM 变更/撤销待人工处理",
        "current_snapshot_version": current_snapshot.get("version"),
        "latest_snapshot_version": latest_snapshot.get("version"),
        "current_snapshot_hash": current_snapshot.get("payload_hash"),
        "latest_snapshot_hash": latest_snapshot.get("payload_hash"),
        "preview_status": stale_notices[0].get("status") if stale_notices else "Active",
        "oms_job_status": "Frozen" if frozen_jobs else "Active",
        "oms_status": downstream_notice.get("status") if downstream_notice else None,
        "oms_order_no": downstream_notice.get("oms_order_no") if downstream_notice else None,
        "next_actions": crm_risk_next_actions(str(latest.get("exception_type") or "")),
    }


def crm_risk_next_actions(exception_type: str) -> list[str]:
    if "AFTER_SHIPPED" in exception_type:
        return ["不要回滚已发货事实", "创建售后/退货/差异处理", "通知财务关注后续差异"]
    if "AFTER_OMS_ACCEPTED" in exception_type or "DURING_PICKING" in exception_type:
        return ["查看 OMS 单据状态", "必要时通知物流/仓库暂停", "人工判断是否改单、拦截或补差异"]
    if "DURING_OMS" in exception_type:
        return ["确认旧 OMS job 已冻结", "作废旧发货预览", "重新预审后再生成发货通知"]
    return ["刷新 CRM 快照差异", "重新预审", "确认后再继续下游履约"]


def crm_order_flow_steps(row: CrmSalesOrder, middle_order: MiddlePlatformOrder | None) -> list[dict]:
    if middle_order is None:
        return [
            {"key": "crm", "label": "CRM 同步", "status": "done", "description": "CRM 订单已同步到本地镜像", "time": row.synced_at.isoformat() if row.synced_at else None},
            {"key": "imported", "label": "进入中台", "status": "pending", "description": "尚未生成中台订单"},
            {"key": "validation", "label": "完整预审", "status": "pending", "description": "等待投递中台事件"},
            {"key": "notice", "label": "发货预览", "status": "pending", "description": "预审通过后生成"},
            {"key": "oms", "label": "OMS 下推", "status": "pending", "description": "发货通知确认后执行"},
            {"key": "fulfillment", "label": "履约归档", "status": "pending", "description": "OMS 回写后更新"},
        ]
    status = middle_order.status
    notice = latest_order_notice(middle_order)
    return [
        {"key": "crm", "label": "CRM 同步", "status": "done", "description": "CRM 订单已同步并查重", "time": row.synced_at.isoformat() if row.synced_at else None},
        {"key": "imported", "label": "进入中台", "status": step_status(status, ["CRM_APPROVED"], ["IMPORTED", "VALIDATING", "VALIDATION_BLOCKED", "VALIDATED", "DELIVERY_NOTICE_READY", "OMS_PENDING", "OMS_RETRYING", "OMS_BLOCKED", "OMS_ACCEPTED", "PICKING", "SHIPPED", "FULFILLMENT_ARCHIVED", "CLOSED", "CANCELLED"]), "description": f"中台订单号：{middle_order.order_no}", "time": middle_order.imported_at.isoformat() if middle_order.imported_at else middle_order.created_at.isoformat()},
        {"key": "validation", "label": "完整预审", "status": validation_step_status(status), "description": validation_step_description(middle_order), "time": middle_order.validated_at.isoformat() if middle_order.validated_at else None},
        {"key": "notice", "label": "发货预览", "status": delivery_notice_step_status(status, notice), "description": delivery_notice_step_description(status, notice), "time": notice.created_at.isoformat() if status not in {"CRM_APPROVED", "IMPORTED", "VALIDATING", "VALIDATION_BLOCKED", "VALIDATED"} and notice and notice.created_at else None},
        {"key": "oms", "label": "OMS 下推", "status": oms_step_status(status, notice), "description": oms_step_description(status, notice), "time": notice.pushed_at.isoformat() if notice and notice.pushed_at else None},
        {"key": "fulfillment", "label": "履约归档", "status": fulfillment_step_status(status), "description": fulfillment_step_description(status), "time": middle_order.updated_at.isoformat() if status in {"PICKING", "SHIPPED", "FULFILLMENT_ARCHIVED", "CLOSED", "CANCELLED"} and middle_order.updated_at else None},
    ]


def latest_order_notice(order: MiddlePlatformOrder) -> DeliveryNotice | None:
    return sorted(order.delivery_notices, key=lambda item: item.created_at, reverse=True)[0] if order.delivery_notices else None


def step_status(current: str, pending: list[str], done: list[str]) -> str:
    if current == "CANCELLED":
        return "cancelled"
    if current in done:
        return "done"
    if current in pending:
        return "pending"
    return "active"


def validation_step_status(status: str) -> str:
    if status == "VALIDATION_BLOCKED":
        return "blocked"
    if status == "CANCELLED":
        return "cancelled"
    if status in {"VALIDATING", "IMPORTED"}:
        return "active"
    if status in {"VALIDATED", "DELIVERY_NOTICE_READY", "OMS_PENDING", "OMS_RETRYING", "OMS_BLOCKED", "OMS_ACCEPTED", "PICKING", "SHIPPED", "FULFILLMENT_ARCHIVED", "CLOSED"}:
        return "done"
    return "pending"


def validation_step_description(order: MiddlePlatformOrder) -> str:
    summary = loads(order.validation_summary_json, {})
    failed = [item for item in summary.get("results", []) if not item.get("passed")]
    if failed:
        return failed[0].get("reason") or "预审存在阻断项"
    if order.status in {"VALIDATED", "DELIVERY_NOTICE_READY", "OMS_PENDING", "OMS_RETRYING", "OMS_BLOCKED", "OMS_ACCEPTED", "PICKING", "SHIPPED", "FULFILLMENT_ARCHIVED", "CLOSED"}:
        return "预审已通过"
    return "等待或正在执行完整预审"


def delivery_notice_step_status(status: str, notice: DeliveryNotice | None) -> str:
    if status == "CANCELLED":
        return "cancelled"
    if status in {"CRM_APPROVED", "IMPORTED", "VALIDATING", "VALIDATION_BLOCKED", "VALIDATED"}:
        return "pending"
    if notice is None:
        return "active"
    if notice.status in {"Stale", "Cancelled", "Blocked"}:
        return "blocked" if notice.status != "Cancelled" else "cancelled"
    if status in {"DELIVERY_NOTICE_READY", "OMS_PENDING", "OMS_RETRYING", "OMS_BLOCKED", "OMS_ACCEPTED", "PICKING", "SHIPPED", "FULFILLMENT_ARCHIVED", "CLOSED"}:
        return "done"
    return "active"


def delivery_notice_step_description(status: str, notice: DeliveryNotice | None) -> str:
    if notice is None or status in {"CRM_APPROVED", "IMPORTED", "VALIDATING", "VALIDATION_BLOCKED", "VALIDATED"}:
        return "尚未生成发货通知"
    return f"{notice.notice_no} · {notice.status}"


def oms_step_status(status: str, notice: DeliveryNotice | None) -> str:
    if status == "CANCELLED":
        return "cancelled"
    if status in {"OMS_BLOCKED"}:
        return "blocked"
    if status in {"OMS_PENDING", "OMS_RETRYING"}:
        return "active"
    if status in {"OMS_ACCEPTED", "PICKING", "SHIPPED", "FULFILLMENT_ARCHIVED", "CLOSED"}:
        return "done"
    if notice and notice.status in {"Blocked", "Retrying"}:
        return "blocked" if notice.status == "Blocked" else "active"
    return "pending"


def oms_step_description(status: str, notice: DeliveryNotice | None) -> str:
    if notice is None:
        return "等待发货通知确认"
    if notice.oms_order_no:
        return f"OMS 单号：{notice.oms_order_no}"
    if status in {"OMS_PENDING", "OMS_RETRYING", "OMS_BLOCKED"}:
        return notice.last_error or notice.status
    return "确认后下推 OMS"


def fulfillment_step_status(status: str) -> str:
    if status == "CANCELLED":
        return "cancelled"
    if status in {"PICKING"}:
        return "active"
    if status in {"SHIPPED", "FULFILLMENT_ARCHIVED", "CLOSED"}:
        return "done"
    return "pending"


def fulfillment_step_description(status: str) -> str:
    mapping = {
        "PICKING": "OMS/仓库执行中",
        "SHIPPED": "已发货",
        "FULFILLMENT_ARCHIVED": "一期履约已归档",
        "CLOSED": "流程已关闭",
        "CANCELLED": "订单已取消",
    }
    return mapping.get(status, "等待 OMS 履约状态")


def crm_order_processing_jobs(session: Session, row: CrmSalesOrder, middle_order: MiddlePlatformOrder | None) -> list[dict]:
    notice_ids = {notice.id for notice in middle_order.delivery_notices} if middle_order is not None else set()
    jobs = session.query(ProcessingJob).order_by(ProcessingJob.created_at.desc()).limit(200).all()
    matched = []
    for job in jobs:
        payload = loads(job.payload_json, {})
        if payload.get("crm_sales_order_id") == row.id or payload.get("crm_order_id") == row.crm_order_id or payload.get("notice_id") in notice_ids:
            matched.append(
                {
                    "id": job.id,
                    "job_type": job.job_type,
                    "status": job.status,
                    "attempt_count": job.attempt_count,
                    "error_message": job.error_message,
                    "created_at": job.created_at.isoformat() if job.created_at else None,
                    "updated_at": job.updated_at.isoformat() if job.updated_at else None,
                }
            )
        if len(matched) >= 20:
            break
    return matched


def processing_job_matches_crm_order(job: ProcessingJob, crm_order: CrmSalesOrder, middle_order_ids: set[str], notice_ids: set[str]) -> bool:
    payload = loads(job.payload_json, {})
    if not isinstance(payload, dict):
        return False
    data = payload.get("data") if isinstance(payload.get("data"), dict) else {}
    candidates = [payload, data]
    for item in candidates:
        if item.get("crm_sales_order_id") == crm_order.id:
            return True
        if item.get("crm_order_id") == crm_order.crm_order_id:
            return True
        if item.get("crm_order_no") == crm_order.crm_order_no:
            return True
        if item.get("order_id") in middle_order_ids:
            return True
        if item.get("notice_id") in notice_ids:
            return True
    return False


def delete_local_crm_order(session: Session, row: CrmSalesOrder, *, actor: str = "System") -> dict:
    middle_orders = (
        session.query(MiddlePlatformOrder)
        .filter(MiddlePlatformOrder.source_system == row.source_system, MiddlePlatformOrder.crm_order_id == row.crm_order_id)
        .all()
    )
    middle_order_ids = {item.id for item in middle_orders}
    middle_order_nos = {item.order_no for item in middle_orders}
    notices = session.query(DeliveryNotice).filter(DeliveryNotice.order_id.in_(middle_order_ids)).all() if middle_order_ids else []
    notice_ids = {item.id for item in notices}
    related_tokens = {row.id, row.crm_order_id, row.crm_order_no, *middle_order_ids, *middle_order_nos, *notice_ids}
    related_tokens = {str(item) for item in related_tokens if item}

    counts: dict[str, int] = {
        "crm_orders": 1,
        "crm_order_items": 0,
        "snapshots": 0,
        "attachments": 0,
        "middle_orders": len(middle_order_ids),
        "middle_order_items": 0,
        "delivery_notices": len(notice_ids),
        "processing_jobs": 0,
        "integration_events": 0,
        "exceptions": 0,
        "audit_events": 0,
    }

    jobs = session.query(ProcessingJob).all()
    for job in jobs:
        if processing_job_matches_crm_order(job, row, middle_order_ids, notice_ids):
            session.delete(job)
            counts["processing_jobs"] += 1

    if middle_order_ids:
        counts["middle_order_items"] = (
            session.query(MiddlePlatformOrderItem)
            .filter(MiddlePlatformOrderItem.order_id.in_(middle_order_ids))
            .delete(synchronize_session=False)
        )
        session.query(DeliveryNotice).filter(DeliveryNotice.id.in_(notice_ids)).delete(synchronize_session=False)
        session.query(MiddlePlatformOrder).filter(MiddlePlatformOrder.id.in_(middle_order_ids)).delete(synchronize_session=False)

    counts["crm_order_items"] = (
        session.query(CrmOrderItem)
        .filter(CrmOrderItem.source_system == row.source_system, CrmOrderItem.crm_order_id == row.crm_order_id)
        .delete(synchronize_session=False)
    )
    counts["snapshots"] = (
        session.query(CrmOrderSnapshot)
        .filter(CrmOrderSnapshot.source_system == row.source_system, CrmOrderSnapshot.crm_order_id == row.crm_order_id)
        .delete(synchronize_session=False)
    )
    counts["attachments"] = (
        session.query(OrderAttachment)
        .filter(OrderAttachment.source_system == row.source_system, OrderAttachment.crm_order_id == row.crm_order_id)
        .delete(synchronize_session=False)
    )
    counts["integration_events"] = (
        session.query(IntegrationEvent)
        .filter(IntegrationEvent.biz_key.in_([row.crm_order_id, row.crm_order_no]))
        .delete(synchronize_session=False)
    )

    exception_rows = session.query(ExceptionCase).all()
    for case in exception_rows:
        detail = str(case.detail or "")
        if any(token and token in detail for token in related_tokens):
            session.delete(case)
            counts["exceptions"] += 1

    audit_rows = session.query(AuditEvent).filter(AuditEvent.related_object_id.in_(list(related_tokens))).all() if related_tokens else []
    for audit in audit_rows:
        session.delete(audit)
        counts["audit_events"] += 1

    crm_order_no = row.crm_order_no
    crm_order_id = row.crm_order_id
    session.delete(row)
    session.add(
        AuditEvent(
            event_type="CrmOrderLocalDeleted",
            actor=actor,
            related_object_type="CrmSalesOrder",
            related_object_id=crm_order_id,
            detail=dumps({"crm_order_id": crm_order_id, "crm_order_no": crm_order_no, "counts": counts}),
        )
    )
    return counts


def exception_summary(row: ExceptionCase) -> str:
    detail = loads(row.detail, {})
    detail = detail if isinstance(detail, dict) else {}
    order = detail.get("order")
    order = order if isinstance(order, dict) else {}
    order_no = order.get("order_no") or detail.get("order_no") or ""
    crm_order_no = order.get("crm_order_no") or detail.get("crm_order_no") or ""
    prefix = f"[{order_no}]" if order_no else ""
    if crm_order_no and crm_order_no != order_no:
        prefix = f"[{order_no}/{crm_order_no}]" if order_no else f"[{crm_order_no}]"
    exception = detail.get("exception") if isinstance(detail, dict) else {}
    summary = ""
    if isinstance(exception, dict):
        summary = str(exception.get("summary") or exception.get("likely_reason") or "")
    if prefix and summary:
        return f"{prefix} {summary}"[:240]
    if prefix:
        return prefix[:240]
    return summary[:240] if summary else row.exception_type


def serialize_fulfillment_item(row: FulfillmentItem) -> dict:
    return {
        "id": row.id,
        "material_code": row.material_code,
        "material_name": row.material_name,
        "required_quantity": row.required_quantity,
        "available_quantity": row.available_quantity,
        "shortage_quantity": row.shortage_quantity,
        "status": row.status,
        "created_at": row.created_at.isoformat(),
    }


def serialize_logistics_task(task: LogisticsTask, include_versions: bool = False) -> dict:
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
        "production_task_id": task.production_task_id,
        "closed_reason": task.closed_reason,
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
            for version in sorted(task.versions, key=lambda row: row.version_no)
        ]
        data["items"] = [serialize_fulfillment_item(row) for row in sorted(task.items, key=lambda row: row.created_at)]
    return data


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
    sla_status = exception_sla_status(row)
    order = detail.get("order") if isinstance(detail, dict) else {}
    middle_order_no = order.get("order_no") if isinstance(order, dict) else None
    crm_order_no = order.get("crm_order_no") if isinstance(order, dict) else None
    return {
        "id": row.id,
        "related_task_id": row.related_task_id,
        "exception_type": row.exception_type,
        "severity": row.severity,
        "detail": detail,
        "detail_text": row.detail,
        "status": row.status,
        "order_no": middle_order_no,
        "crm_order_no": crm_order_no,
        "requires_confirmation": is_high_risk_exception(row),
        "assignee": row.assignee,
        "resolution_note": row.resolution_note,
        "resolution_evidence": loads(row.resolution_evidence_json, {}) if row.resolution_evidence_json else None,
        "due_at": row.due_at.isoformat() if row.due_at else None,
        "sla_status": sla_status,
        "resolved_at": row.resolved_at.isoformat() if row.resolved_at else None,
        "reopened_at": row.reopened_at.isoformat() if row.reopened_at else None,
        "last_actor": row.last_actor,
        "updated_at": row.updated_at.isoformat() if row.updated_at else None,
        "created_at": row.created_at.isoformat(),
    }


def build_exception_context(session: Session, case: ExceptionCase, current_user: User | None = None) -> dict:
    detail = loads(case.detail, {})
    middle_order = exception_middle_order(session, case, detail)
    crm_order = middle_order.crm_order if middle_order is not None else exception_crm_order(session, detail)
    delivery_notices = list(middle_order.delivery_notices) if middle_order is not None else []
    related_ids = exception_related_ids(case, middle_order, crm_order, delivery_notices)
    audits = related_audit_events(session, related_ids)
    processing_jobs = related_processing_jobs(session, case, middle_order, delivery_notices)
    snapshots = []
    snapshot_diff = {"from_version": None, "to_version": None, "changes": []}
    attachments = []
    if crm_order is not None:
        snapshots = [
            {
                "id": row.id,
                "version": row.version,
                "payload_hash": row.payload_hash,
                "parse_status": row.parse_status,
                "is_latest": row.is_latest,
                "captured_at": row.captured_at.isoformat() if row.captured_at else None,
            }
            for row in (
                session.query(CrmOrderSnapshot)
                .filter(CrmOrderSnapshot.source_system == crm_order.source_system, CrmOrderSnapshot.crm_order_id == crm_order.crm_order_id)
                .order_by(CrmOrderSnapshot.version.desc())
                .limit(5)
                .all()
            )
        ]
        snapshot_diff = crm_snapshot_diff_payload(
            session,
            crm_order,
            current_payload_hash=middle_order.payload_hash if middle_order is not None else None,
        )
        attachments = [
            serialize_order_attachment(row, current_user=current_user)
            for row in (
                session.query(OrderAttachment)
                .filter(
                    OrderAttachment.source_system == crm_order.source_system,
                    OrderAttachment.crm_order_id == crm_order.crm_order_id,
                    OrderAttachment.payload_hash == crm_order.payload_hash,
                )
                .order_by(OrderAttachment.created_at.desc())
                .all()
            )
        ]
    return {
        "exception": serialize_exception(case),
        "context_pack": detail,
        "middle_order": serialize_middle_order(middle_order, include_detail=True, current_user=current_user) if middle_order is not None else None,
        "crm_order": serialize_crm_order(crm_order, current_user=current_user) if crm_order is not None else None,
        "crm_snapshots": snapshots,
        "snapshot_diff": snapshot_diff,
        "order_attachments": attachments,
        "processing_jobs": [serialize_processing_job(row) for row in processing_jobs],
        "audit_events": [serialize_audit_event(row) for row in audits],
        "diagnosis": detail.get("ai_diagnosis") if isinstance(detail, dict) else None,
        "feedback": detail.get("ai_feedback", []) if isinstance(detail, dict) else [],
        "next_actions": exception_context_next_actions(case, detail, middle_order, processing_jobs),
        "oms_replay": oms_replay_gate(middle_order, processing_jobs),
    }


def exception_middle_order(session: Session, case: ExceptionCase, detail: dict) -> MiddlePlatformOrder | None:
    order_data = detail.get("order") if isinstance(detail, dict) else {}
    candidates = [
        str((order_data or {}).get("order_no") or "").strip(),
        str((order_data or {}).get("crm_order_no") or "").strip(),
    ]
    for value in candidates:
        if not value:
            continue
        row = (
            session.query(MiddlePlatformOrder)
            .filter(or_(MiddlePlatformOrder.order_no == value, MiddlePlatformOrder.crm_order_no == value))
            .order_by(MiddlePlatformOrder.created_at.desc())
            .first()
        )
        if row is not None:
            return row
    text = case.detail or ""
    return (
        session.query(MiddlePlatformOrder)
        .filter(or_(text_contains_column(text, MiddlePlatformOrder.order_no), text_contains_column(text, MiddlePlatformOrder.crm_order_no)))
        .order_by(MiddlePlatformOrder.created_at.desc())
        .first()
    )


def exception_crm_order(session: Session, detail: dict) -> CrmSalesOrder | None:
    order_data = detail.get("order") if isinstance(detail, dict) else {}
    crm_order_no = str((order_data or {}).get("crm_order_no") or "").strip()
    crm_order_id = str((order_data or {}).get("crm_order_id") or "").strip()
    if not crm_order_no and not crm_order_id:
        return None
    return (
        session.query(CrmSalesOrder)
        .filter(or_(CrmSalesOrder.crm_order_no == crm_order_no, CrmSalesOrder.crm_order_id == crm_order_id))
        .order_by(CrmSalesOrder.created_at.desc())
        .first()
    )


def text_contains_column(text: str, column):
    if not text:
        return column == "__never__"
    values = re.findall(r"(?:MP|SO|DN)-[A-Za-z0-9_-]+", text)
    if not values:
        return column == "__never__"
    return column.in_(values)


def exception_related_ids(case: ExceptionCase, order: MiddlePlatformOrder | None, crm_order: CrmSalesOrder | None, notices: list[DeliveryNotice]) -> list[tuple[str, str]]:
    ids = [("ExceptionCase", case.id)]
    if order is not None:
        ids.append(("MiddlePlatformOrder", order.id))
    if crm_order is not None:
        ids.append(("CrmSalesOrder", crm_order.id))
    ids.extend(("DeliveryNotice", notice.id) for notice in notices)
    return ids


def related_audit_events(session: Session, related_ids: list[tuple[str, str]]) -> list[AuditEvent]:
    if not related_ids:
        return []
    filters = [((AuditEvent.related_object_type == object_type) & (AuditEvent.related_object_id == object_id)) for object_type, object_id in related_ids]
    return session.query(AuditEvent).filter(or_(*filters)).order_by(AuditEvent.created_at.desc()).limit(40).all()


def related_processing_jobs(session: Session, case: ExceptionCase, order: MiddlePlatformOrder | None, notices: list[DeliveryNotice]) -> list[ProcessingJob]:
    tokens = [case.id]
    if order is not None:
        tokens.extend([order.id, order.order_no, order.crm_order_no])
    tokens.extend(notice.id for notice in notices)
    filters = [ProcessingJob.payload_json.ilike(f"%{token}%") for token in tokens if token]
    if not filters:
        return []
    return session.query(ProcessingJob).filter(or_(*filters)).order_by(ProcessingJob.created_at.desc()).limit(20).all()


def exception_context_next_actions(case: ExceptionCase, detail: dict, order: MiddlePlatformOrder | None, jobs: list[ProcessingJob]) -> list[str]:
    diagnosis = detail.get("ai_diagnosis") if isinstance(detail, dict) else {}
    actions = list((diagnosis or {}).get("recommended_actions") or [])
    if case.status not in {"Resolved", "Closed"}:
        actions.append("处理完成后关闭异常，保留处理说明")
    if order is not None and order.status in {"VALIDATION_BLOCKED", "OMS_BLOCKED"}:
        actions.append("修复主数据或配置后重新触发预审/重放下推")
    if any(job.status == "Failed" for job in jobs):
        actions.append("检查失败队列任务，必要时重新入队")
    return list(dict.fromkeys(actions))


def oms_replay_gate(order: MiddlePlatformOrder | None, jobs: list[ProcessingJob]) -> dict:
    if order is None:
        return {"ready": False, "reason": "未关联中台订单", "missing": ["中台订单"], "evidence_required": True}
    notices = list(order.delivery_notices or [])
    candidate = next((notice for notice in notices if notice.status in {"Blocked", "Retrying"}), None)
    if candidate is None:
        candidate = next((notice for notice in notices if notice.status in {"Confirmed", "Blocked", "Retrying"}), None)
    active_old_jobs = [
        job
        for job in jobs
        if job.job_type == "OMS_PUSH_NOTICE" and job.status in {"Pending", "Running"} and (candidate is None or candidate.id in (job.payload_json or ""))
    ]
    missing: list[str] = []
    if order.status != OrderStatus.OMS_BLOCKED.value:
        missing.append("订单未处于 OMS_BLOCKED")
    if candidate is None:
        missing.append("缺少可重放的发货通知")
    elif candidate.confirmed_at is None:
        missing.append("发货通知未重新确认")
    if active_old_jobs:
        missing.append("仍存在未冻结/未完成的旧 OMS job")
    return {
        "ready": not missing,
        "reason": "可填写修复证据并重放 OMS" if not missing else "、".join(missing),
        "missing": missing,
        "evidence_required": True,
        "notice_id": candidate.id if candidate is not None else None,
        "notice_no": candidate.notice_no if candidate is not None else None,
        "notice_status": candidate.status if candidate is not None else None,
        "active_old_job_ids": [job.id for job in active_old_jobs],
    }


def exception_sla_status(row: ExceptionCase) -> str:
    if row.status in {"Resolved", "Closed"}:
        return "resolved"
    if row.due_at is None:
        return "none"
    now = now_utc()
    due_at = row.due_at
    if due_at.tzinfo is None and now.tzinfo is not None:
        now = now.replace(tzinfo=None)
    if due_at <= now:
        return "overdue"
    if due_at <= now + timedelta(hours=4):
        return "due_soon"
    return "normal"


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
                "review_aliases": spu_review_aliases(spu),
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


@app.put("/api/products/spu/{spu_uuid}/review-aliases")
def update_product_spu_review_aliases_api(spu_uuid: str, payload: dict, session: Session = Depends(get_session)) -> dict:
    try:
        spu = update_spu_review_aliases(session, spu_uuid, payload.get("aliases"))
        session.commit()
        return {"ok": True, "id": spu.id, "spu_id": spu.spu_id, "review_aliases": spu_review_aliases(spu)}
    except ValueError as error:
        session.rollback()
        raise HTTPException(status_code=400, detail=str(error))


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


# ═════════════════════════════════
# 预审别名导入（CRM 产品模板）
# ═════════════════════════════════

@app.post("/api/review-aliases/import/preview")
def api_review_alias_import_preview(file: UploadFile = File(...), session: Session = Depends(get_session)) -> dict:
    """上传 CRM 产品导入模板 Excel，预览别名导入结果。"""
    import tempfile, os
    with tempfile.NamedTemporaryFile(delete=False, suffix=".xlsx") as tmp:
        contents = file.file.read()
        tmp.write(contents)
        tmp_path = tmp.name
    try:
        return preview_alias_import_from_excel(tmp_path, session)
    finally:
        try:
            os.unlink(tmp_path)
        except Exception:
            pass


@app.post("/api/review-aliases/import/confirm")
def api_review_alias_import_confirm(payload: dict, session: Session = Depends(get_session)) -> dict:
    """确认导入预审别名（客户端需回传预览结果的 items）。"""
    items = payload.get("items") or payload.get("preview", {}).get("items")
    if not items:
        raise HTTPException(status_code=400, detail="缺少预览数据，请先上传文件预览")
    preview_data = {"items": items}
    result = confirm_alias_import_from_excel(preview_data, session)
    session.commit()
    return {"message": f"已更新 {result['updated']} 个成品的预审别名", **result}


@app.get("/api/review-aliases/list")
def api_review_alias_list(
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
    q: str = Query(""),
    session: Session = Depends(get_session)
) -> dict:
    """分页列出已有预审别名的物料。"""
    from backend.app.models import ProductSPU

    skip = (page - 1) * page_size
    query = session.query(ProductSPU).filter(
        func.coalesce(func.json_array_length(ProductSPU.extended_info_json, '$.review_aliases'), 0) > 0
    )

    if q.strip():
        pattern = f"%{q.strip()}%"
        query = query.filter(
            or_(ProductSPU.spu_id.ilike(pattern), ProductSPU.name.ilike(pattern))
        )

    total = query.count()
    items = query.order_by(ProductSPU.updated_at.desc()).offset(skip).limit(page_size).all()

    return {
        "items": [
            {
                "spu_uuid": spu.id,
                "spu_id": spu.spu_id,
                "name": spu.name,
                "aliases": spu_review_aliases(spu),
                "alias_count": len(spu_review_aliases(spu)),
                "updated_at": spu.updated_at.isoformat(),
            }
            for spu in items
        ],
        "total": total,
        "page": page,
        "page_size": page_size,
        "total_pages": max(1, (total + page_size - 1) // page_size),
    }


def _apply_db_query_timeout(session: Session, timeout_seconds: int = 10) -> None:
    """为当前数据库会话设置查询超时保护（仅 PostgreSQL）。
    SQLite 由 PRAGMA busy_timeout 保护，无需额外设置。
    """
    if session.bind and session.bind.dialect.name == "postgresql":
        session.execute(text(f"SET LOCAL statement_timeout = '{timeout_seconds * 1000}'"))


# ── CRM 面包屑错误辅助 ──

CRM_CRUMB_STEPS = {
    "db_query": "CRM 数据库查询",
    "db_count": "订单总数统计",
    "db_aggregate": "金额聚合计算",
    "db_status_options": "状态取值去重",
    "sync_enabled_check": "同步开关检查",
    "sync_lock_acquire": "同步锁获取",
    "sync_browser_check": "CDP 浏览器连通性检查",
    "sync_browser_start": "CDP 浏览器启动",
    "sync_browser_cleanup": "CDP 浏览器多余页签清理",
    "sync_script_exec": "Node.js 脚本执行",
    "sync_script_timeout": "脚本执行超时",
    "sync_data_parse": "同步数据解析",
    "sync_upsert": "订单 upsert 入库",
    "sync_detail_fetch": "订单详情同步",
    "sync_detail_lock": "详情同步锁获取",
    "sync_attachment_cache": "附件缓存写入",
    "sync_contact_extract": "LLM 联系人提取",
    "sync_event_enqueue": "中台事件入队",
    "sync_run_save": "同步记录保存",
    "browser_cdp_connect": "CDP 调试端口连接",
    "browser_page_navigate": "浏览器页面导航",
    "browser_login_check": "登录态检查",
    "browser_login_auto": "自动登录尝试",
    "browser_login_cooldown": "登录风控冷却等待",
}


def _crm_exception_breadcrumbs(
    exc: Exception,
    *,
    steps: list[dict[str, str]],
    detail: str = "",
    resolution: str = "",
) -> dict:
    """生成 CRM 面包屑错误结构，用于前端展示故障链路。

    steps: [
        {"step": "step_key", "status": "ok"},
        {"step": "step_key", "status": "fail", "error": "失败原因"},
    ]
    """
    return {
        "error_type": exc.__class__.__name__,
        "detail": detail or str(exc),
        "resolution": resolution or "请确认相关配置或联系 IT 运维",
        "breadcrumbs": [
            {
                "label": CRM_CRUMB_STEPS.get(s["step"], s["step"]),
                "status": s.get("status", "unknown"),
                "error": s.get("error", ""),
            }
            for s in steps
        ],
    }


def _crm_http_error(
    status_code: int,
    breadcrumb_data: dict,
) -> HTTPException:
    """带面包屑的 HTTPException 快捷构造。"""
    return HTTPException(
        status_code=status_code,
        detail=dumps(breadcrumb_data),
    )


@app.get("/api/crm/orders")
def list_crm_orders(
    q: str = "",
    status: str = "",
    customer: str = "",
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, ge=1, le=100),
    session: Session = Depends(get_session),
    current_user: User = Depends(get_current_user),
) -> dict:
    _apply_db_query_timeout(session, timeout_seconds=15)

    try:
        query = session.query(CrmSalesOrder).filter(or_(CrmSalesOrder.scope_status.is_(None), CrmSalesOrder.scope_status != "Ignored"))

        # Enforce data visibility scope for sales/business operators
        if hasattr(current_user, "role") and current_user.role == "business_operator":
            filter_expr = (CrmSalesOrder.sales_user_name == current_user.username)
            if current_user.department:
                filter_expr = filter_expr | (CrmSalesOrder.owner_department.ilike(current_user.department))
            query = query.filter(filter_expr)

        if q.strip():
            pattern = f"%{q.strip()}%"
            query = query.filter(
                or_(
                    CrmSalesOrder.crm_order_no.ilike(pattern),
                    CrmSalesOrder.customer_name.ilike(pattern),
                    CrmSalesOrder.opportunity_name.ilike(pattern),
                    CrmSalesOrder.sales_user_name.ilike(pattern),
                )
            )
        if status.strip():
            query = query.filter(CrmSalesOrder.life_status == status.strip())
        if customer.strip():
            query = query.filter(CrmSalesOrder.customer_name.ilike(f"%{customer.strip()}%"))
        total = query.count()
        rows = (
            query.order_by(CrmSalesOrder.order_date.desc(), CrmSalesOrder.created_at.desc())
            .offset((page - 1) * page_size)
            .limit(page_size)
            .all()
        )
        return {
            "items": [serialize_crm_order(row, current_user=current_user) for row in rows],
            "total": total,
            "page": page,
            "page_size": page_size,
            "total_pages": max(1, (total + page_size - 1) // page_size),
            "summary": crm_order_summary(session),
            "status_options": [row[0] for row in session.query(CrmSalesOrder.life_status).distinct().all() if row[0]],
        }
    except Exception as exc:
        raise _crm_http_error(500, _crm_exception_breadcrumbs(
            exc,
            steps=[{"step": "db_query", "status": "fail", "error": str(exc)[:120]}],
            detail=f"CRM 订单列表查询失败: {exc}",
            resolution="检查数据库连接或联系 IT 运维",
        ))


@app.get("/api/crm/orders/{order_id}")
def get_crm_order(order_id: str, session: Session = Depends(get_session), current_user: User = Depends(get_current_user)) -> dict:
    try:
        row = session.get(CrmSalesOrder, order_id)
        if row is None:
            raise HTTPException(status_code=404, detail="CRM 订单不存在")

        # Enforce data visibility scope for sales/business operators
        if hasattr(current_user, "role") and current_user.role == "business_operator":
            is_owner = (row.sales_user_name == current_user.username)
            is_same_dept = bool(current_user.department and row.owner_department and row.owner_department.lower() == current_user.department.lower())
            if not is_owner and not is_same_dept:
                raise HTTPException(status_code=403, detail="没有权限访问此订单")

        return serialize_crm_order_with_flow(session, row, current_user=current_user)
    except HTTPException:
        raise
    except Exception as exc:
        raise _crm_http_error(500, _crm_exception_breadcrumbs(
            exc,
            steps=[{"step": "db_query", "status": "fail", "error": str(exc)[:120]}],
            detail=f"CRM 订单详情查询失败: {exc}",
            resolution="检查数据库连接或联系 IT 运维",
        ))


@app.post("/api/crm/orders/{order_id}/retry-detail-sync")
def retry_crm_order_detail(order_id: str, session: Session = Depends(get_session), current_user: User = Depends(get_current_user)) -> dict:
    try:
        row = session.get(CrmSalesOrder, order_id)
        if row is None:
            raise HTTPException(status_code=404, detail="CRM 订单不存在")

        # Enforce data visibility scope for sales/business operators
        if hasattr(current_user, "role") and current_user.role == "business_operator":
            is_owner = (row.sales_user_name == current_user.username)
            is_same_dept = bool(current_user.department and row.owner_department and row.owner_department.lower() == current_user.department.lower())
            if not is_owner and not is_same_dept:
                raise HTTPException(status_code=403, detail="没有权限操作此订单")

        result = retry_crm_order_detail_sync(session, row)
        session.commit()
        refreshed = session.get(CrmSalesOrder, order_id) or row
        return {"retry": result, "order": serialize_crm_order_with_flow(session, refreshed, current_user=current_user)}
    except HTTPException:
        raise
    except CrmSyncBusyError as exc:
        session.rollback()
        return {"ok": False, "busy": True, "message": str(exc)}
    except Exception as exc:
        session.commit()
        raise _crm_http_error(400, _crm_exception_breadcrumbs(
            exc,
            steps=[
                {"step": "sync_detail_lock", "status": "ok"},
                {"step": "sync_detail_fetch", "status": "fail", "error": str(exc)[:200]},
            ],
            detail=f"CRM 订单详情同步失败: {exc}",
            resolution="确认该订单在 CRM 中存在完整详情数据",
        ))


@app.delete("/api/crm/orders/{order_id}")
def delete_crm_order(order_id: str, session: Session = Depends(get_session), current_user: User = Depends(require_role(["admin", "it_ops"]))) -> dict:
    row = session.get(CrmSalesOrder, order_id)
    if row is None:
        raise HTTPException(status_code=404, detail="CRM 订单不存在")
    try:
        crm_order_no = row.crm_order_no
        crm_order_id = row.crm_order_id
        counts = delete_local_crm_order(session, row, actor=getattr(current_user, "username", "System") or "System")
        session.commit()
        return {"ok": True, "crm_order_id": crm_order_id, "crm_order_no": crm_order_no, "deleted": counts}
    except Exception as exc:
        session.rollback()
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.get("/api/crm/order-attachments/{attachment_id}/download")
def download_crm_order_attachment(attachment_id: str, session: Session = Depends(get_session), current_user: User = Depends(get_current_user)):
    row = session.get(OrderAttachment, attachment_id)
    if row is None:
        raise HTTPException(status_code=404, detail="附件不存在")
        
    is_financial = False
    if row.attachment_type and row.attachment_type.lower() in ("invoice", "contract", "paymentreceipt", "purchaseorder"):
        is_financial = True
        
    if is_financial:
        sales_user = row.crm_order.sales_user_name if row.crm_order else None
        dept = row.crm_order.owner_department if row.crm_order else None
        if should_mask_financials(current_user, sales_user, dept):
            raise HTTPException(status_code=403, detail="没有权限下载此类型的附件")
            
    cached_ref = local_storage_ref(row)
    if cached_ref:
        return FileResponse(cached_ref, filename=row.file_name)
    if row.file_url:
        return RedirectResponse(row.file_url)
    raise HTTPException(status_code=404, detail="附件暂无下载地址，请重新同步 CRM 订单详情")


@app.get("/api/crm/sync/summary")
def get_crm_sync_summary(session: Session = Depends(get_session)) -> dict:
    _apply_db_query_timeout(session, timeout_seconds=15)
    try:
        runs = session.query(CrmSyncRun).order_by(CrmSyncRun.started_at.desc()).limit(10).all()
        return {**crm_order_summary(session), "runs": [serialize_sync_run(row) for row in runs]}
    except Exception as exc:
        raise _crm_http_error(500, _crm_exception_breadcrumbs(
            exc,
            steps=[{"step": "db_query", "status": "fail", "error": str(exc)[:120]}],
            detail=f"CRM 同步摘要查询失败: {exc}",
            resolution="刷新重试，如持续失败联系 IT 运维",
        ))


@app.get("/api/crm/browser/status")
def get_crm_browser_status(current_user: User = Depends(require_role(["admin", "it_ops"]))) -> dict:
    return crm_browser_status()


@app.post("/api/crm/browser/start")
def start_crm_browser(
    payload: dict | None = None,
    session: Session = Depends(get_session),
    current_user: User = Depends(require_role(["admin", "it_ops"])),
) -> dict:
    mode = (payload or {}).get("mode")
    return start_crm_browser_process(session, requested_mode=str(mode) if mode else None)


@app.post("/api/crm/browser/stop")
def stop_crm_browser(current_user: User = Depends(require_role(["admin", "it_ops"]))) -> dict:
    return stop_crm_browser_process()


@app.post("/api/crm/sync/queue")
def queue_crm_sync(session: Session = Depends(get_session)) -> dict:
    try:
        return queue_crm_order_sync(session, source="manual")
    except Exception as exc:
        raise _crm_http_error(400, _crm_exception_breadcrumbs(
            exc,
            steps=[{"step": "sync_enabled_check", "status": "fail", "error": str(exc)[:120]}],
            detail=f"CRM 同步投递失败: {exc}",
            resolution="检查「系统接入」页 CRM 同步开关是否开启",
        ))


@app.post("/api/crm/sync/run")
def run_crm_sync_now(session: Session = Depends(get_session)) -> dict:
    try:
        return run_crm_sales_order_sync(session, trigger="manual")
    except CrmSyncBusyError as exc:
        session.rollback()
        return {"ok": False, "busy": True, "message": str(exc)}
    except Exception as exc:
        raise _crm_http_error(400, _crm_exception_breadcrumbs(
            exc,
            steps=[
                {"step": "sync_lock_acquire", "status": "ok"},
                {"step": "sync_browser_check", "status": "ok"},
                {"step": "sync_script_exec", "status": "fail", "error": str(exc)[:200]},
            ],
            detail=f"CRM 同步执行失败: {exc}",
            resolution="检查 CDP 浏览器状态和 CRM 账号登录态",
        ))


@app.post("/api/crm/sync/orders/{crm_order_no}/force")
def force_sync_crm_order(crm_order_no: str, session: Session = Depends(get_session), current_user: User = Depends(require_role(["admin", "it_ops"]))) -> dict:
    try:
        result = force_sync_crm_order_by_no(session, crm_order_no)
        session.commit()
        return result
    except CrmSyncBusyError as exc:
        session.rollback()
        return {"ok": False, "busy": True, "message": str(exc)}
    except Exception as exc:
        session.rollback()
        raise _crm_http_error(400, _crm_exception_breadcrumbs(
            exc,
            steps=[
                {"step": "sync_lock_acquire", "status": "ok"},
                {"step": "sync_detail_fetch", "status": "fail", "error": f"订单 {crm_order_no} 强制同步失败: {str(exc)[:200]}"},
            ],
            detail=f"强制同步 CRM 订单 {crm_order_no} 失败: {exc}",
            resolution="确认该订单号在 CRM 中存在且 CDP 浏览器正常运行",
        ))


@app.post("/api/crm/sync/test-connection")
def test_crm_sync_connection(session: Session = Depends(get_session)) -> dict:
    try:
        return run_crm_integration_test(session)
    except Exception as exc:
        raise _crm_http_error(400, _crm_exception_breadcrumbs(
            exc,
            steps=[
                {"step": "sync_browser_check", "status": "fail", "error": str(exc)[:200]},
            ],
            detail=f"CRM 连接测试失败: {exc}",
            resolution="确认 CDP 浏览器已启动，CRM 账号密码配置正确",
        ))


@app.get("/api/v2/order-dashboard")
def v2_order_dashboard(session: Session = Depends(get_session)) -> dict:
    return order_dashboard(session)


@app.get("/api/v2/orders")
def v2_list_orders(
    q: str = "",
    status: str = "",
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, ge=1, le=100),
    session: Session = Depends(get_session),
    current_user: User = Depends(get_current_user),
) -> dict:
    return list_middle_orders(session, q=q, status=status, page=page, page_size=page_size, current_user=current_user)


@app.get("/api/v2/orders/{order_id}")
def v2_get_order(order_id: str, session: Session = Depends(get_session), current_user: User = Depends(get_current_user)) -> dict:
    row = session.get(MiddlePlatformOrder, order_id)
    if row is None:
        raise HTTPException(status_code=404, detail="中台订单不存在")
        
    # Enforce data visibility scope for sales/business operators
    if current_user.role == "business_operator":
        is_owner = (row.sales_user_name == current_user.username)
        is_same_dept = bool(current_user.department and row.crm_order and row.crm_order.owner_department and row.crm_order.owner_department.lower() == current_user.department.lower())
        if not is_owner and not is_same_dept:
            raise HTTPException(status_code=403, detail="没有权限访问此中台订单")
            
    ensure_middle_order_business_fields(session, row)

    # ── 幂等性自愈检测：如果本地无单号，或单号是错误的中台单号格式 (MP-开头的单号)，尝试去金蝶重新查询真实的金蝶单号 ──
    is_wrong_format = bool(row.erp_bill_no and row.erp_bill_no.startswith("MP-"))
    if (not row.erp_bill_no or is_wrong_format) and row.order_no:
        try:
            from backend.app.services.erp.kingdee_client import KingdeeClient, kingdee_config_from_session, normalize_query_rows
            config = kingdee_config_from_session(session)
            client = KingdeeClient(config)
            query_result = client.execute_bill_query(
                form_id="SAL_SaleOrder",
                field_keys="FID,FBillNo",
                filter_string=f"FNote LIKE '%{row.order_no}%' AND FBillNo NOT LIKE 'MP-%'",
                limit=1,
            )
            existing_items = normalize_query_rows(query_result.get("raw"))
            if existing_items and len(existing_items) > 0 and len(existing_items[0]) > 0:
                row.erp_bill_no = existing_items[0][1]
                session.commit()
        except Exception as check_exc:
            logger.warning("获取订单详情时金蝶前置防重 Check 异常 (忽略): %s", check_exc)

    data = serialize_middle_order(row, include_detail=True, current_user=current_user)
    
    # 注入金蝶登录账号作为制单执行人名称
    erp_username = ""
    row_cfg = session.get(SystemConfig, "erp_username")
    if row_cfg and row_cfg.value:
        erp_username = row_cfg.value.strip()
    data["erp_username"] = erp_username
    
    # 注入金蝶制单提交的完成时间
    erp_completed_at = ""
    if row.erp_bill_no:
        from backend.app.models import AuditEvent
        from datetime import timedelta
        # 优先从审计日志中查找 ErpSaveSuccess 或订单状态变更
        ae = (
            session.query(AuditEvent)
            .filter(
                AuditEvent.related_object_id == row.id,
                AuditEvent.event_type == "OrderStatusChanged"
            )
            .order_by(AuditEvent.created_at.desc())
            .first()
        )
        if ae:
            local_time = ae.created_at + timedelta(hours=8)
            erp_completed_at = local_time.strftime("%Y-%m-%d %H:%M:%S")
        elif row.updated_at:
            local_time = row.updated_at + timedelta(hours=8)
            erp_completed_at = local_time.strftime("%Y-%m-%d %H:%M:%S")
            
    data["erp_completed_at"] = erp_completed_at
    
    return data


@app.get("/api/v2/orders/{order_id}/mail-preview")
def preview_order_delivery_mail(
    order_id: str,
    session: Session = Depends(get_session),
    current_user: User = Depends(get_current_user),
) -> dict:
    order = session.get(MiddlePlatformOrder, order_id)
    if order is None:
        raise HTTPException(status_code=404, detail="中台订单不存在")
        
    from backend.app.services.order_middle_platform import latest_delivery_notice
    notice = latest_delivery_notice(session, order)
    if notice is None:
        raise HTTPException(status_code=400, detail="未找到该订单的发货通知预览记录")
        
    # 确定发货仓库
    warehouse_code = notice.warehouse_code or ""

    from backend.app.services.mail_template_service import _today_str

    # 为销售订单合并多源收件人
    if order.order_type != "STOCK_REPLENISHMENT":
        merged_to: list[str] = []
        merged_cc: list[str] = []

        # 物流通知部门的主送和抄送
        logistics_depts = session.query(LogisticsDepartment).filter(LogisticsDepartment.status == "Active").all()
        for dept in logistics_depts:
            if dept.mail_to_json:
                merged_to.extend(json.loads(dept.mail_to_json))
            if dept.mail_cc_json:
                merged_cc.extend(json.loads(dept.mail_cc_json))

        # 生产通知部门的主送和抄送
        prod_depts = session.query(ProductionDepartment).filter(ProductionDepartment.status == "Active").all()
        for dept in prod_depts:
            if dept.mail_to_json:
                merged_to.extend(json.loads(dept.mail_to_json))
            if dept.mail_cc_json:
                merged_cc.extend(json.loads(dept.mail_cc_json))

        # CRM 同步的销售邮箱
        if order.crm_order and order.crm_order.sales_user_email:
            merged_to.append(order.crm_order.sales_user_email)

        # 商务抄送邮箱（来源于系统配置）
        biz_cc = system_config_value(session, "ops_cc_email").strip()
        if biz_cc:
            merged_cc.append(biz_cc)

        # 去重
        seen_to: set[str] = set()
        seen_cc: set[str] = set()
        to_emails = [e for e in merged_to if e and e.strip() and not (e in seen_to or seen_to.add(e))]
        cc_emails = [e for e in merged_cc if e and e.strip() and not (e in seen_cc or seen_cc.add(e))]
    else:
        # 备货订单保持原有 scene 逻辑
        from backend.app.services.mail_template_service import _resolve_scene, _get_receivers
        scene = _resolve_scene(order, warehouse_code)
        to_emails, cc_emails = _get_receivers(session, scene)
    
    # Render mail
    is_domestic = "武汉" in warehouse_code or "国内" in warehouse_code
    erp_bill_no = order.erp_bill_no or "【暂无金蝶单号】"
    customer = order.customer_name or ""
    sales_name = order.sales_user_name or ""
    date_str = _today_str()
    
    recipients = None
    if order.crm_order:
        recipients = [{
            "contact": order.crm_order.receipt_contact or "",
            "phone": order.crm_order.receipt_phone or "",
            "address": order.crm_order.receipt_address or ""
        }]
        
    from backend.app.services.mail.templates.sales_delivery import render_sales_delivery_mail
    from backend.app.services.mail.templates.stock_replenishment import render_replenishment_mail
    
    if order.order_type == "STOCK_REPLENISHMENT":
        mail_data = render_replenishment_mail(
            order=order,
            items=list(order.items),
            to_emails=to_emails,
            cc_emails=cc_emails,
            warehouse=warehouse_code,
            erp_bill_no="" if is_domestic and order.order_type == "STOCK_REPLENISHMENT" else erp_bill_no,
            special_requirements=None,
            demand_desc=customer,
            order_date=date_str,
        )
    else:
        mail_data = render_sales_delivery_mail(
            order=order,
            items=list(order.items),
            to_emails=to_emails,
            cc_emails=cc_emails,
            warehouse=warehouse_code,
            erp_bill_no=erp_bill_no,
            special_requirements=None,
            sales_name=sales_name,
            customer_name=customer,
            order_date=date_str,
            recipients=recipients,
        )
        
    return {
        "to": mail_data["to"],
        "cc": mail_data["cc"],
        "subject": mail_data["subject"],
        "body": mail_data["body"],
    }


@app.post("/api/v2/orders/{order_id}/push-erp-and-delivery")
def push_erp_and_delivery(
    order_id: str,
    session: Session = Depends(get_session),
    current_user: User = Depends(get_current_user),
) -> dict:
    order = session.get(MiddlePlatformOrder, order_id)
    if order is None:
        raise HTTPException(status_code=404, detail="中台订单不存在")
        
    # Enforce data visibility scope for sales/business operators
    if current_user.role == "business_operator":
        is_owner = (order.sales_user_name == current_user.username)
        is_same_dept = bool(current_user.department and order.crm_order and order.crm_order.owner_department and order.crm_order.owner_department.lower() == current_user.department.lower())
        if not is_owner and not is_same_dept:
            raise HTTPException(status_code=403, detail="没有权限访问此中台订单")
            
    from backend.app.services.order_middle_platform import should_skip_erp_billing, process_erp_billing, latest_delivery_notice, confirm_delivery_notice
    
    trace_id = f"manual-push-{uuid.uuid4()}"
    
    # 1. 如果没有金蝶单号且不是备货→武汉仓（应跳过制单），则调用金蝶制单
    erp_success = True
    erp_error = None
    
    if not order.erp_bill_no and not should_skip_erp_billing(order):
        # 临时将 erp_write_enabled 开启，以便手动触发制单不受自动开关限制
        orig_val = session.get(SystemConfig, "erp_write_enabled")
        orig_str = orig_val.value if orig_val else "false"
        
        from backend.app.services.bootstrap import set_config
        set_config(session, "erp_write_enabled", "true")
        session.commit()
        
        try:
            erp_result = process_erp_billing(session, order, trace_id=trace_id)
            session.commit()
            if not erp_result.get("erp_success") and not erp_result.get("erp_skipped"):
                erp_success = False
                erp_error = erp_result.get("error")
        finally:
            set_config(session, "erp_write_enabled", orig_str)
            session.commit()
            
    if not erp_success:
        raise HTTPException(status_code=400, detail=f"金蝶制单失败：{erp_error}")
        
    # 2. 获取发货通知并确认（下推 OMS）
    notice = latest_delivery_notice(session, order)
    if not notice:
        raise HTTPException(status_code=400, detail="未找到该订单的发货通知预览记录")
        
    if notice.status in {"Previewed", "Blocked", "Retrying"}:
        try:
            confirm_delivery_notice(session, notice, confirmed_by=current_user.username or "operator", trace_id=trace_id)
            session.commit()
        except Exception as exc:
            session.rollback()
            raise HTTPException(status_code=400, detail=f"确认并推送失败：{str(exc)}")
            
    # 3. 再次确保下推邮件已经 enqueued（以防万一）
    from backend.app.services.mail_template_service import enqueue_delivery_notice_mail
    try:
        enqueue_delivery_notice_mail(
            session, order, list(order.items),
            warehouse=notice.warehouse_code or "",
            special_requirements=None,
        )
        session.commit()
    except Exception as mail_exc:
        logger.warning("发送发货通知邮件失败: %s", mail_exc)
        
    return {"success": True, "order_no": order.order_no, "erp_bill_no": order.erp_bill_no}


@app.post("/api/v2/orders/{order_id}/wizard-create-erp-bill")
def wizard_create_erp_bill(
    order_id: str,
    session: Session = Depends(get_session),
    current_user: User = Depends(get_current_user),
) -> dict:
    order = session.get(MiddlePlatformOrder, order_id)
    if order is None:
        raise HTTPException(status_code=404, detail="中台订单不存在")
        
    # Enforce visibility check
    if current_user.role == "business_operator":
        is_owner = (order.sales_user_name == current_user.username)
        is_same_dept = bool(current_user.department and order.crm_order and order.crm_order.owner_department and order.crm_order.owner_department.lower() == current_user.department.lower())
        if not is_owner and not is_same_dept:
            raise HTTPException(status_code=403, detail="没有权限访问此中台订单")
            
    from backend.app.services.order_middle_platform import should_skip_erp_billing, process_erp_billing
    
    if order.erp_bill_no:
        return {"success": True, "erp_bill_no": order.erp_bill_no, "already_exists": True}
        
    if should_skip_erp_billing(order):
        return {"success": True, "erp_bill_no": None, "skipped": True}
        
    # 临时将 erp_write_enabled 开启，以便手动触发制单不受自动开关限制
    orig_val = session.get(SystemConfig, "erp_write_enabled")
    orig_str = orig_val.value if orig_val else "false"
    
    from backend.app.services.bootstrap import set_config
    set_config(session, "erp_write_enabled", "true")
    session.commit()
    
    try:
        trace_id = f"manual-wizard-erp-{uuid.uuid4()}"
        erp_result = process_erp_billing(session, order, trace_id=trace_id)
        
        # 无论成功或失败，都在金蝶处理完毕后，将订单流转状态重置回 DELIVERY_NOTICE_READY 状态，以便用户在向导中重试或继续下一步
        from backend.app.services.order_middle_platform import transition_order, OrderEvent
        transition_order(session, order, OrderEvent.DELIVERY_NOTICE_CREATED, trace_id=trace_id)
        session.commit()
        
        if erp_result.get("erp_success") or erp_result.get("erp_skipped"):
            erp_username = ""
            row_cfg = session.get(SystemConfig, "erp_username")
            if row_cfg and row_cfg.value:
                erp_username = row_cfg.value.strip()
            
            from datetime import datetime
            erp_completed_at = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            return {
                "success": True, 
                "erp_bill_no": order.erp_bill_no, 
                "erp_username": erp_username,
                "erp_completed_at": erp_completed_at
            }
        else:
            raise HTTPException(status_code=400, detail=erp_result.get("error") or "金蝶制单失败")
    finally:
        set_config(session, "erp_write_enabled", orig_str)
        session.commit()


@app.post("/api/v2/orders/{order_id}/wizard-delete-erp-bill")
def wizard_delete_erp_bill(
    order_id: str,
    session: Session = Depends(get_session),
    current_user: User = Depends(get_current_user),
) -> dict:
    order = session.get(MiddlePlatformOrder, order_id)
    if order is None:
        raise HTTPException(status_code=404, detail="中台订单不存在")
        
    if current_user.role == "business_operator":
        is_owner = (order.sales_user_name == current_user.username)
        is_same_dept = bool(current_user.department and order.crm_order and order.crm_order.owner_department and order.crm_order.owner_department.lower() == current_user.department.lower())
        if not is_owner and not is_same_dept:
            raise HTTPException(status_code=403, detail="没有权限访问此中台订单")
            
    if not order.erp_bill_no:
        return {"success": True, "message": "该订单暂无关联的金蝶单号，无需删除"}
        
    from backend.app.services.erp.kingdee_client import KingdeeClient
    from backend.app.services.order_middle_platform import kingdee_config_from_session, normalize_query_rows
    
    try:
        config = kingdee_config_from_session(session)
        client = KingdeeClient(config)
        
        # 1. 查询 FID
        query_result = client.execute_bill_query(
            form_id="SAL_SaleOrder",
            field_keys="FID,FBillNo",
            filter_string=f"FBillNo = '{order.erp_bill_no}'",
            limit=1,
        )
        bill_internal_id = None
        items = normalize_query_rows(query_result.get("raw"))
        if items and isinstance(items, list) and len(items) > 0:
            row = items[0]
            if isinstance(row, list) and len(row) > 0:
                bill_internal_id = row[0]
                
        if bill_internal_id:
            # 2. 反审核
            unaudit_r = client.un_audit_bill(form_id="SAL_SaleOrder", bill_ids=[bill_internal_id])
            # 不论反审核是否由于本来就没有审核而报错，都继续执行删除
            
            # 3. 删除
            delete_r = client.delete_bill(form_id="SAL_SaleOrder", bill_ids=[bill_internal_id])
            if not delete_r.get("ok"):
                raise Exception(delete_r.get("message") or "金蝶删除 API 调用失败")
        else:
            logger.warning("删除金蝶订单时，未在金蝶中找到单号为 %s 的单据，直接从本地清理单号", order.erp_bill_no)
            
        # 4. 清理本地单号并提交
        order.erp_bill_no = None
        session.commit()
        return {"success": True, "message": "金蝶订单删除成功"}
    except Exception as exc:
        session.rollback()
        raise HTTPException(status_code=400, detail=f"金蝶订单删除失败：{str(exc)}")



@app.post("/api/v2/orders/{order_id}/wizard-confirm-delivery-and-email")
def wizard_confirm_delivery_and_email(
    order_id: str,
    payload: dict | None = None,
    session: Session = Depends(get_session),
    current_user: User = Depends(get_current_user),
) -> dict:
    order = session.get(MiddlePlatformOrder, order_id)
    if order is None:
        raise HTTPException(status_code=404, detail="中台订单不存在")

    if current_user.role == "business_operator":
        is_owner = (order.sales_user_name == current_user.username)
        is_same_dept = bool(current_user.department and order.crm_order and order.crm_order.owner_department and order.crm_order.owner_department.lower() == current_user.department.lower())
        if not is_owner and not is_same_dept:
            raise HTTPException(status_code=403, detail="没有权限访问此中台订单")

    from backend.app.services.order_middle_platform import latest_delivery_notice, confirm_delivery_notice

    notice = latest_delivery_notice(session, order)
    if not notice:
        raise HTTPException(status_code=400, detail="未找到该订单的发货通知记录")

    trace_id = f"manual-wizard-confirm-{uuid.uuid4()}"

    if notice.status in {"Previewed", "Blocked", "Retrying"}:
        try:
            confirm_delivery_notice(session, notice, confirmed_by=current_user.username or "operator", trace_id=trace_id)
            session.commit()
        except Exception as exc:
            session.rollback()
            raise HTTPException(status_code=400, detail=f"确认并推送失败：{str(exc)}")

    # 如果用户提供了自定义邮件内容，直接使用；否则走模板渲染
    payload = payload or {}
    custom_to = payload.get("to")
    custom_cc = payload.get("cc")
    custom_subject = payload.get("subject")
    custom_body = payload.get("body")

    import re
    _EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")

    from backend.app.services.mail_template_service import enqueue_delivery_notice_mail
    try:
        if custom_subject and custom_body:
            # 校验收件人/抄送邮箱格式
            if custom_to:
                if isinstance(custom_to, list):
                    for addr in custom_to:
                        if not _EMAIL_RE.match(str(addr or "").strip()):
                            raise HTTPException(status_code=400, detail=f"收件人邮箱格式不正确：{addr}")
                elif isinstance(custom_to, str):
                    custom_to = [a.strip() for a in custom_to.split(",") if a.strip()]
                    for addr in custom_to:
                        if not _EMAIL_RE.match(addr):
                            raise HTTPException(status_code=400, detail=f"收件人邮箱格式不正确：{addr}")
            if custom_cc:
                if isinstance(custom_cc, list):
                    for addr in custom_cc:
                        if addr and not _EMAIL_RE.match(str(addr or "").strip()):
                            raise HTTPException(status_code=400, detail=f"抄送邮箱格式不正确：{addr}")
                elif isinstance(custom_cc, str):
                    custom_cc = [a.strip() for a in custom_cc.split(",") if a.strip()]
                    for addr in custom_cc:
                        if addr and not _EMAIL_RE.match(addr):
                            raise HTTPException(status_code=400, detail=f"抄送邮箱格式不正确：{addr}")

            # 直接用用户编辑的内容创建发信任务
            job = OutboundMailJob(
                mail_type="sales_delivery",
                to_json=dumps(custom_to or []),
                cc_json=dumps(custom_cc or []),
                subject=custom_subject,
                body=custom_body,
                idempotency_key=f"delivery-notice-{order.order_no}-{order.version}-custom",
                status="Pending",
                priority=20,
            )
            session.add(job)
            session.commit()
        else:
            enqueue_delivery_notice_mail(
                session, order, list(order.items),
                warehouse=notice.warehouse_code or "",
                special_requirements=None,
            )
            session.commit()
    except HTTPException:
        raise
    except Exception as mail_exc:
        logger.warning("发送发货通知邮件失败: %s", mail_exc)

    return {"success": True}




@app.post("/api/crm/orders/{order_id}/queue-v2")
def queue_crm_order_to_v2(order_id: str, session: Session = Depends(get_session)) -> dict:
    row = session.get(CrmSalesOrder, order_id)
    if row is None:
        raise HTTPException(status_code=404, detail="CRM 订单不存在")
    job = enqueue_crm_order_parsed_event(session, row, trace_id=f"manual-{uuid.uuid4()}")
    session.commit()
    return {"queued": True, "job_id": job.id, "job_type": job.job_type}


@app.post("/api/crm/orders/{order_id}/process-v2")
def process_crm_order_to_v2(order_id: str, session: Session = Depends(get_session)) -> dict:
    row = session.get(CrmSalesOrder, order_id)
    if row is None:
        raise HTTPException(status_code=404, detail="CRM 订单不存在")
    try:
        job = enqueue_crm_order_parsed_event(session, row, trace_id=f"manual-{uuid.uuid4()}")
        payload = loads(job.payload_json, {})
        payload["force_revalidate"] = True
        result = process_crm_order_parsed_event(session, payload)
        job.status = "Completed"
        job.error_message = None
        job.updated_at = now_utc()
        session.commit()
        return result
    except Exception as exc:
        session.rollback()
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.post("/api/v2/delivery-notices/{notice_id}/replay-oms")
def replay_v2_delivery_notice(notice_id: str, payload: dict | None = None, session: Session = Depends(get_session)) -> dict:
    notice = session.get(DeliveryNotice, notice_id)
    if notice is None:
        raise HTTPException(status_code=404, detail="发货通知不存在")
    if notice.confirmed_at is None:
        raise HTTPException(status_code=400, detail="发货通知未确认，请先确认拆单预览")
    order = session.get(MiddlePlatformOrder, notice.order_id)
    if order is None:
        raise HTTPException(status_code=404, detail="中台订单不存在")

    payload = payload or {}
    actor = str(payload.get("actor") or payload.get("confirmed_by") or payload.get("operator") or "operator").strip() or "operator"
    repair_evidence = str(payload.get("repair_evidence") or payload.get("evidence") or payload.get("note") or "").strip()
    if order.status == OrderStatus.OMS_BLOCKED.value:
        if not repair_evidence:
            detail = {
                "order_no": order.order_no,
                "crm_order_no": order.crm_order_no,
                "notice_no": notice.notice_no,
                "notice_status": notice.status,
                "order_status": order.status,
                "risk": ExceptionType.MANUAL_REPLAY_WITHOUT_FIX.value,
                "action": "blocked",
            }
            session.add(
                AuditEvent(
                    event_type="ManualReplayWithoutFixBlocked",
                    related_object_type="DeliveryNotice",
                    related_object_id=notice.id,
                    detail=dumps(detail),
                )
            )
            existing = (
                session.query(ExceptionCase)
                .filter(
                    ExceptionCase.exception_type == ExceptionType.MANUAL_REPLAY_WITHOUT_FIX.value,
                    ExceptionCase.status == "Open",
                    ExceptionCase.detail.ilike(f"%{order.order_no}%"),
                )
                .first()
            )
            if existing is None:
                session.add(
                    ExceptionCase(
                        exception_type=ExceptionType.MANUAL_REPLAY_WITHOUT_FIX.value,
                        severity="High",
                        detail=dumps({**detail, "summary": "未填写修复证据，禁止重放 OMS"}),
                        status="Open",
                        due_at=now_utc() + timedelta(hours=24),
                    )
                )
            session.commit()
            raise HTTPException(status_code=400, detail="OMS 阻塞订单重放前必须填写修复证据")
        session.add(
            AuditEvent(
                event_type="OmsReplayRepairEvidenceRecorded",
                related_object_type="DeliveryNotice",
                related_object_id=notice.id,
                detail=dumps(
                    {
                        "order_no": order.order_no,
                        "crm_order_no": order.crm_order_no,
                        "notice_no": notice.notice_no,
                        "repair_evidence": repair_evidence,
                        "actor": actor,
                    }
                ),
            )
        )
        try:
            job = confirm_delivery_notice(session, notice, confirmed_by=actor, trace_id=f"replay-{uuid.uuid4()}")
        except Exception as exc:
            session.rollback()
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        resolved_cases = (
            session.query(ExceptionCase)
            .filter(
                ExceptionCase.exception_type.in_(["OMS_BLOCKED", ExceptionType.MANUAL_REPLAY_WITHOUT_FIX.value]),
                ExceptionCase.status == "Open",
                ExceptionCase.detail.ilike(f"%{order.order_no}%"),
            )
            .all()
        )
        for case in resolved_cases:
            case.status = "Resolved"
            case.resolution_note = f"OMS 重放修复证据：{repair_evidence}"
            case.resolution_evidence_json = dumps(
                {
                    "type": "OMS_REPLAY",
                    "notice_id": notice.id,
                    "notice_no": notice.notice_no,
                    "repair_evidence": repair_evidence,
                    "actor": actor,
                    "recorded_at": now_utc().isoformat(),
                }
            )
            case.resolved_at = now_utc()
            case.updated_at = now_utc()
            case.last_actor = actor
            session.add(
                AuditEvent(
                    event_type="ExceptionResolvedForOmsReplay",
                    actor=actor,
                    related_object_type="ExceptionCase",
                    related_object_id=case.id,
                    detail=dumps({"notice_no": notice.notice_no, "repair_evidence": repair_evidence}),
                )
            )
        session.commit()
        return {"queued": True, "job_id": job.id, "notice_id": notice.id, "repair_evidence_recorded": True, "resolved_exceptions": len(resolved_cases)}

    job = enqueue_oms_push(session, notice)
    session.add(
        AuditEvent(
            event_type="OmsReplayQueued",
            related_object_type="DeliveryNotice",
            related_object_id=notice.id,
            detail=dumps({"order_no": order.order_no, "notice_no": notice.notice_no, "actor": actor, "order_status": order.status}),
        )
    )
    session.commit()
    return {"queued": True, "job_id": job.id, "notice_id": notice.id}


@app.post("/api/v2/delivery-notices/{notice_id}/confirm")
def confirm_v2_delivery_notice(
    notice_id: str,
    payload: DeliveryNoticeConfirmRequest,
    session: Session = Depends(get_session),
) -> dict:
    notice = session.get(DeliveryNotice, notice_id)
    if notice is None:
        raise HTTPException(status_code=404, detail="发货通知不存在")
    try:
        job = confirm_delivery_notice(session, notice, confirmed_by=payload.confirmed_by or "operator", trace_id=f"confirm-{uuid.uuid4()}")
        session.commit()
        return {"confirmed": True, "job_id": job.id, "notice_id": notice.id}
    except Exception as exc:
        session.rollback()
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.post("/api/v2/delivery-notices/{notice_id}/sync-oms-status")
def sync_v2_delivery_notice_oms_status(
    notice_id: str,
    payload: dict,
    session: Session = Depends(get_session),
) -> dict:
    notice = session.get(DeliveryNotice, notice_id)
    if notice is None:
        raise HTTPException(status_code=404, detail="发货通知不存在")
    status_payload = {**payload, "notice_id": notice.id, "trace_id": str(payload.get("trace_id") or f"oms-status-{uuid.uuid4()}")}
    try:
        result = process_oms_status_update(session, status_payload)
        session.commit()
        return result
    except Exception as exc:
        session.rollback()
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.post("/api/v2/oms/status-poll")
def poll_v2_oms_status(
    limit: int = Query(default=50, ge=1, le=200),
    async_job: bool = False,
    session: Session = Depends(get_session),
) -> dict:
    if async_job:
        payload = {"limit": limit}
        job = ProcessingJob(job_type="OMS_STATUS_POLL", payload_json=dumps(payload), status="Pending")
        session.add(job)
        session.commit()
        return {"queued": True, "job_id": job.id}
    try:
        result = poll_oms_status_updates(session, limit=limit)
        session.commit()
        return result
    except Exception as exc:
        session.rollback()
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.post("/api/products/erp-sync")
def sync_products_from_erp(session: Session = Depends(get_session)) -> dict:
    result = sync_erp_materials(session)
    session.commit()
    return result


@app.post("/api/products/oms-sync")
def sync_products_from_oms(session: Session = Depends(get_session)) -> dict:
    from backend.app.services.oms.material_sync import sync_oms_materials
    result = sync_oms_materials(session)
    session.commit()
    return result


@app.post("/api/products/review-preview")
def preview_product_review_api(payload: dict, session: Session = Depends(get_session)) -> dict:
    text = str(payload.get("text") or payload.get("product_summary") or "").strip()
    source_text = str(payload.get("source_text") or "").strip()
    channel = str(payload.get("channel") or "default").strip() or "default"
    if not text and not source_text:
        raise HTTPException(status_code=400, detail="请提供需要测试的订单物料文本")
    extracted_items = extract_order_products_for_review(session, text, source_text, channel=channel)
    reviewed_items = review_order_products(session, extracted_items, channel=channel) if extracted_items else []
    suggestions = suggest_product_review_candidates(session, f"{text}\n{source_text}", limit=5) if not reviewed_items else []
    status_counts: dict[str, int] = {}
    risk_flags: list[str] = []
    for item in reviewed_items:
        review = item.get("review") or {}
        status = review.get("status") or "Unknown"
        status_counts[status] = status_counts.get(status, 0) + 1
        risk_flags.extend(str(flag) for flag in review.get("risk_flags") or [] if str(flag).strip())
    return {
        "ok": True,
        "channel": channel,
        "items": reviewed_items,
        "suggestions": suggestions,
        "alias_candidate": suggestions[0]["suggested_alias"] if suggestions else "",
        "summary": {
            "matched_count": len(reviewed_items),
            "suggestion_count": len(suggestions),
            "status_counts": status_counts,
            "risk_flags": risk_flags,
        },
    }


@app.get("/api/products/review-readiness")
def product_review_readiness_api(
    channel: str = "default",
    limit: int = Query(20, ge=1, le=100),
    session: Session = Depends(get_session),
) -> dict:
    return product_review_readiness(session, channel=channel, limit=limit)


@app.get("/api/products/inventory")
def list_product_inventory(
    q: str = "",
    material_code: str = "",
    warehouse_code: str = "",
    low_stock_only: bool = False,
    countable_only: bool = True,
    measure_type: str = "",
    inventory_scope: str = "",
    threshold: float = Query(1, ge=0),
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
    session: Session = Depends(get_session),
) -> dict:
    try:
        _apply_db_query_timeout(session, timeout_seconds=30)
        return list_inventory_snapshots(
            session,
            q=q,
            material_code=material_code,
            warehouse_code=warehouse_code,
            low_stock_only=low_stock_only,
            countable_only=countable_only,
            measure_type=measure_type,
            inventory_scope=inventory_scope,
            threshold=threshold,
            page=page,
            page_size=page_size,
        )
    except Exception as exc:
        raise _crm_http_error(500, _crm_exception_breadcrumbs(
            exc,
            steps=[{"step": "db_query", "status": "fail", "error": str(exc)[:120]}],
            detail=f"库存数据查询失败: {exc}",
            resolution="确认 ERP 物料同步是否完成，或联系 IT 运维检查数据库",
        ))


@app.get("/api/products/inventory/warehouses")
def list_product_inventory_warehouses(
    q: str = "",
    limit: int = Query(30, ge=1, le=100),
    session: Session = Depends(get_session),
) -> dict:
    return list_inventory_warehouse_options(session, q=q, limit=limit)


@app.get("/api/products/inventory/types")
def list_product_inventory_types(
    q: str = "",
    warehouse_code: str = "",
    low_stock_only: bool = False,
    countable_only: bool = True,
    measure_type: str = "",
    inventory_scope: str = "",
    threshold: float = Query(1, ge=0),
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
    session: Session = Depends(get_session),
) -> dict:
    return list_inventory_type_summary(
        session,
        q=q,
        warehouse_code=warehouse_code,
        low_stock_only=low_stock_only,
        countable_only=countable_only,
        measure_type=measure_type,
        inventory_scope=inventory_scope,
        threshold=threshold,
        page=page,
        page_size=page_size,
    )


@app.get("/api/products/inventory/type-items")
def list_product_inventory_type_items(
    material_type: str = Query(..., min_length=1),
    parent_category: str = "",
    q: str = "",
    warehouse_code: str = "",
    stock_status: str = "",
    low_stock_only: bool = False,
    countable_only: bool = True,
    measure_type: str = "",
    inventory_scope: str = "",
    threshold: float = Query(1, ge=0),
    page: int = Query(1, ge=1),
    page_size: int = Query(100, ge=1, le=500),
    session: Session = Depends(get_session),
) -> dict:
    return list_inventory_type_items(
        session,
        material_type=material_type,
        parent_category=parent_category,
        q=q,
        warehouse_code=warehouse_code,
        stock_status=stock_status,
        low_stock_only=low_stock_only,
        countable_only=countable_only,
        measure_type=measure_type,
        inventory_scope=inventory_scope,
        threshold=threshold,
        page=page,
        page_size=page_size,
    )


@app.get("/api/products/inventory/classification-rules")
def get_inventory_classification_rules(session: Session = Depends(get_session)) -> dict:
    return inventory_classification_diagnostics(session)


@app.put("/api/products/inventory/classification-rules")
def update_inventory_classification_rules(payload: dict, session: Session = Depends(get_session)) -> dict:
    rules = payload.get("rules") if isinstance(payload.get("rules"), dict) else payload
    normalized = save_inventory_classification_rules(session, rules)
    session.commit()
    return {"ok": True, "rules": normalized, "diagnostics": inventory_classification_diagnostics(session)}


@app.post("/api/products/inventory/erp-sync")
def sync_inventory_from_erp(session: Session = Depends(get_session)) -> dict:
    raise HTTPException(status_code=400, detail="同步 ERP 库存功能暂时不可用")


@app.post("/api/products/inventory/import-excel")
def api_import_inventory_excel(file: UploadFile = File(...), session: Session = Depends(get_session)) -> dict:
    with tempfile.NamedTemporaryFile(delete=False, suffix=".xlsx") as tmp:
        contents = file.file.read()
        tmp.write(contents)
        tmp_path = tmp.name

    try:
        from backend.app.services.inventory_import_service import import_inventory_excel
        result = import_inventory_excel(session, tmp_path, operated_by="admin")
        return result
    finally:
        try:
            os.unlink(tmp_path)
        except Exception:
            pass


@app.post("/api/inventory/import")
def api_inventory_import_upload(file: UploadFile = File(...), session: Session = Depends(get_session)) -> dict:
    """新版库存Excel导入（支持海外库存总表格式）"""
    with tempfile.NamedTemporaryFile(delete=False, suffix=".xlsx") as tmp:
        contents = file.file.read()
        tmp.write(contents)
        tmp_path = tmp.name
    try:
        from backend.app.services.inventory_import_service import import_inventory_excel
        result = import_inventory_excel(session, tmp_path, operated_by="admin")
        return result
    finally:
        try:
            os.unlink(tmp_path)
        except Exception:
            pass


@app.get("/api/inventory/import-records")
def api_inventory_import_records(session: Session = Depends(get_session)) -> dict:
    """获取库存导入历史记录"""
    from backend.app.services.inventory_import_service import list_import_records
    return {"items": list_import_records(session)}


@app.get("/api/inventory/trends")
def api_inventory_trends(material_code: str = "", warehouse: str = "", days: int = 90, session: Session = Depends(get_session)) -> dict:
    """查询库存变化走势"""
    from backend.app.services.inventory_import_service import get_inventory_trends
    return {"items": get_inventory_trends(session, material_code=material_code, warehouse=warehouse, days=days)}


@app.get("/api/inventory/parse-preview")
def api_inventory_parse_preview(session: Session = Depends(get_session)) -> dict:
    """预览最近一次导入的库存数据（调试用）"""
    from backend.app.services.inventory_import_service import parse_inventory_excel
    import glob
    archives = sorted(glob.glob("data/inventory_archives/*.xlsx"), reverse=True)
    if not archives:
        return {"ok": False, "error": "暂无归档文件"}
    result = parse_inventory_excel(archives[0])
    return result


@app.get("/api/inventory/template")
def api_inventory_download_template():
    """下载库存导入模板（海外库存总表格式）"""
    import os.path
    template_path = "data/库存导入模板.xlsx"
    if not os.path.exists(template_path):
        from fastapi.responses import JSONResponse
        return JSONResponse(status_code=404, content={"error": "模板文件不存在"})
    return FileResponse(template_path, filename="库存导入模板.xlsx", media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")


@app.get("/api/products/sku")
def list_products_sku_api(
    spu_id: str = Query(None, description="所属 SPU ID (Code)"),
    spu_uuid: str = Query(None, description="所属 SPU UUID"),
    q: str = Query("", description="搜索 SKU、SPU、成品名称或预审别名"),
    crm_semantic: bool = Query(False, description="是否启用 CRM 语义匹配"),
    page: int = Query(1, ge=1),
    page_size: int = Query(10, ge=1, le=100),
    session: Session = Depends(get_session)
) -> dict:
    if not isinstance(crm_semantic, bool):
        crm_semantic = False
    if not isinstance(q, str):
        q = ""
    if not isinstance(spu_id, str):
        spu_id = None
    if not isinstance(spu_uuid, str):
        spu_uuid = None
    skip = (page - 1) * page_size
    items, total = get_skus(session, skip=skip, limit=page_size, spu_id=spu_id, spu_uuid=spu_uuid, query=q, crm_semantic=crm_semantic)
    return {
        "items": [
            {
                "id": sku.id,
                "spu_uuid": sku.spu.id if sku.spu else None,
                "spu_id": sku.spu.spu_id if sku.spu else None,
                "spu_name": sku.spu.name if sku.spu else None,
                "sku_id": sku.sku_id,
                "model": sku.model,
                "brand": sku.spu.brand if sku.spu else None,
                "category": sku.spu.category if sku.spu else None,
                "status": sku.status,
                "review_aliases": spu_review_aliases(sku.spu) if sku.spu else [],
                "attributes": loads(sku.attributes_json, {}),
                "created_at": sku.created_at.isoformat(),
            } for sku in items
        ],
        "total": total,
        "page": page,
        "page_size": page_size,
        "total_pages": max(1, (total + page_size - 1) // page_size)
    }

@app.get("/api/products/sku/{sku_id}/realtime-stock")
def get_sku_realtime_stock_api(sku_id: str, session: Session = Depends(get_session)) -> dict:
    from backend.app.models import ProductSKU, ProductInventorySnapshot
    sku = session.query(ProductSKU).filter_by(sku_id=sku_id).first()
    if sku is None:
        raise HTTPException(status_code=404, detail="SKU 在中台主数据中不存在")
    try:
        from backend.app.services.oms.material_sync import query_oms_realtime_stock
        stocks = query_oms_realtime_stock(session, sku_id)
        
        # Query Excel stock snapshots
        excel_snapshots = session.query(ProductInventorySnapshot).filter_by(material_code=sku_id).all()
        
        matched_excel_ids = set()
        for item in stocks:
            wh_code = item.get("warehouse_code") or ""
            wh_name = item.get("warehouse_name") or ""
            
            # Find matching excel snapshot
            matched_s = None
            # 1. Exact match
            for s in excel_snapshots:
                if s.id in matched_excel_ids:
                    continue
                if wh_code.lower() == s.warehouse_code.lower() or wh_name.lower() == s.warehouse_name.lower():
                    matched_s = s
                    break
            # 2. Try mappings/substrings
            if not matched_s:
                for s in excel_snapshots:
                    if s.id in matched_excel_ids:
                        continue
                    s_name = s.warehouse_name.lower().strip()
                    # Alias rules
                    mappings = {"c1": "美西", "c3": "德国", "b1": "amazon us", "b2": "amazon de", "b3": "amazon uk", "b5": "cz amazon us"}
                    kw = mappings.get(wh_code.lower())
                    if (kw and kw in s_name) or (s_name in wh_name.lower() or wh_name.lower() in s_name):
                        matched_s = s
                        break
            
            if matched_s:
                item["excel_qty"] = int(matched_s.base_qty)
                matched_excel_ids.add(matched_s.id)
            else:
                item["excel_qty"] = None
                
        # Append unmatched excel snapshots
        for s in excel_snapshots:
            if s.id not in matched_excel_ids:
                stocks.append({
                    "warehouse_code": s.warehouse_code,
                    "warehouse_name": s.warehouse_name,
                    "quantity": None,
                    "usable_quantity": None,
                    "excel_qty": int(s.base_qty)
                })
                
        return {
            "ok": True,
            "sku_id": sku_id,
            "stocks": stocks
        }
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

@app.post("/api/products/sku")
def create_product_sku_api(payload: ProductSKUCreate, session: Session = Depends(get_session)) -> dict:
    try:
        sku = create_sku(session, spu_uuid=payload.spu_uuid, sku_id=payload.sku_id, attributes=payload.attributes)
        session.commit()
        return {"id": sku.id, "sku_id": sku.sku_id}
    except ValueError as error:
        session.rollback()
        raise HTTPException(status_code=400, detail=str(error))


@app.get("/api/pricing")
def list_channel_pricing_api(
    sku_id: str = Query(None, description="按 SKU ID (Code) 筛选"),
    sku_uuid: str = Query(None, description="按 SKU UUID 筛选"),
    q: str = Query("", description="搜索 SKU、SPU、成品名称或预审别名"),
    page: int = Query(1, ge=1),
    page_size: int = Query(10, ge=1, le=100),
    session: Session = Depends(get_session)
) -> dict:
    skip = (page - 1) * page_size
    items, total = get_channel_pricing(session, skip=skip, limit=page_size, sku_id=sku_id, sku_uuid=sku_uuid, query=q)
    return {
        "items": [
            {
                "id": p.id,
                "sku_uuid": p.sku_uuid,
                "sku_id": p.sku.sku_id,
                "spu_id": p.sku.spu.spu_id,
                "spu_name": p.sku.spu.name,
                "review_aliases": spu_review_aliases(p.sku.spu),
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
    try:
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
    except ValueError as error:
        session.rollback()
        raise HTTPException(status_code=400, detail=str(error))


@app.get("/api/promotions")
def list_promotions_api(
    q: str = Query("", description="搜索促销名、SKU、成品、渠道或绑定状态"),
    page: int = Query(1, ge=1),
    page_size: int = Query(10, ge=1, le=100),
    session: Session = Depends(get_session)
) -> dict:
    skip = (page - 1) * page_size
    items, total = get_promotions(session, skip=skip, limit=page_size, query=q)
    def serialize_promotion(rule):
        binding = promotion_rule_binding_info(session, rule)
        return {
            "id": rule.id,
            "sku_uuid": rule.sku_uuid,
            "sku_id": rule.sku.sku_id if rule.sku else "",
            "spu_id": rule.sku.spu.spu_id if rule.sku and rule.sku.spu else "",
            "spu_name": rule.sku.spu.name if rule.sku and rule.sku.spu else "",
            "binding_status": binding["status"],
            "binding_label": binding["label"],
            "binding_valid": binding["is_valid"],
            "name": rule.name,
            "channel": rule.channel,
            "is_active": rule.is_active,
            "start_time": rule.start_time.isoformat() if rule.start_time else None,
            "end_time": rule.end_time.isoformat() if rule.end_time else None,
            "priority": rule.priority,
            "discount_type": rule.discount_type,
            "discount_value": rule.discount_value,
            "created_at": rule.created_at.isoformat(),
        }
    return {
        "items": [serialize_promotion(rule) for rule in items],
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
            sku_uuid=payload.sku_uuid,
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
    except ValueError as e:
        session.rollback()
        raise HTTPException(status_code=400, detail=str(e))
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
    except ValueError as e:
        session.rollback()
        raise HTTPException(status_code=400, detail=str(e))
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


# ═══════════════════════════════════════
# V2 Phase 1 — 管理台 CRUD API
# ═══════════════════════════════════════

# ── 主体-仓库映射 ──

@app.get("/api/config/entity-mappings")
def list_entity_mappings(session: Session = Depends(get_session)) -> dict:
    items = session.query(EntityMapping).order_by(EntityMapping.entity_code).all()
    return {"items": [{"id": m.id, "entity_code": m.entity_code, "entity_name": m.entity_name,
                       "erp_org_id": m.erp_org_id, "warehouses": json.loads(m.warehouses_json) if m.warehouses_json else [],
                       "finance_notify": json.loads(m.finance_notify_json) if m.finance_notify_json else [],
                       "is_active": m.is_active} for m in items]}


@app.get("/api/config/entity-mappings/{code}")
def get_entity_mapping(code: str, session: Session = Depends(get_session)) -> dict:
    m = session.query(EntityMapping).filter(EntityMapping.entity_code == code).first()
    if m is None:
        raise HTTPException(status_code=404, detail="主体映射不存在")
    return {"id": m.id, "entity_code": m.entity_code, "entity_name": m.entity_name,
            "erp_org_id": m.erp_org_id, "warehouses": json.loads(m.warehouses_json) if m.warehouses_json else [],
            "finance_notify": json.loads(m.finance_notify_json) if m.finance_notify_json else [],
            "is_active": m.is_active}


@app.post("/api/config/entity-mappings")
def upsert_entity_mapping(payload: dict, session: Session = Depends(get_session)) -> dict:
    code = payload.get("entity_code", "").strip()
    if not code:
        raise HTTPException(status_code=400, detail="entity_code 必填")
    m = session.query(EntityMapping).filter(EntityMapping.entity_code == code).first()
    if m is None:
        m = EntityMapping(entity_code=code)
        session.add(m)
    m.entity_name = payload.get("entity_name", m.entity_name)
    m.erp_org_id = payload.get("erp_org_id", m.erp_org_id)
    m.warehouses_json = json.dumps(payload.get("warehouses", []), ensure_ascii=False)
    m.finance_notify_json = json.dumps(payload.get("finance_notify", []), ensure_ascii=False)
    m.is_active = payload.get("is_active", m.is_active)
    m.updated_at = now_utc()
    session.commit()
    return {"ok": True, "entity_code": code}


@app.delete("/api/config/entity-mappings/{code}")
def delete_entity_mapping(code: str, session: Session = Depends(get_session)) -> dict:
    m = session.query(EntityMapping).filter(EntityMapping.entity_code == code).first()
    if m is None:
        raise HTTPException(status_code=404, detail="Not found")
    session.delete(m)
    session.commit()
    return {"ok": True}


# ── 客户-主体映射 ──

@app.get("/api/config/customer-entity-mappings")
def list_customer_entity_mappings(session: Session = Depends(get_session)) -> dict:
    items = session.query(CustomerEntityMapping).order_by(CustomerEntityMapping.customer_name).all()
    return {"items": [{"id": m.id, "customer_name": m.customer_name, "entity_code": m.entity_code,
                       "warehouse": m.warehouse, "remark": m.remark, "is_active": m.is_active} for m in items]}


@app.post("/api/config/customer-entity-mappings")
def upsert_customer_entity_mapping(payload: dict, session: Session = Depends(get_session)) -> dict:
    name = payload.get("customer_name", "").strip()
    if not name:
        raise HTTPException(status_code=400, detail="customer_name 必填")
    m = session.query(CustomerEntityMapping).filter(CustomerEntityMapping.customer_name == name).first()
    if m is None:
        m = CustomerEntityMapping(customer_name=name)
        session.add(m)
    m.entity_code = payload.get("entity_code", m.entity_code)
    m.warehouse = payload.get("warehouse", m.warehouse)
    m.remark = payload.get("remark", "")
    m.is_active = payload.get("is_active", m.is_active)
    m.updated_at = now_utc()
    session.commit()
    return {"ok": True, "customer_name": name}


@app.delete("/api/config/customer-entity-mappings/{name}")
def delete_customer_entity_mapping(name: str, session: Session = Depends(get_session)) -> dict:
    m = session.query(CustomerEntityMapping).filter(CustomerEntityMapping.customer_name == name).first()
    if m is None:
        raise HTTPException(status_code=404, detail="Not found")
    session.delete(m)
    session.commit()
    return {"ok": True}


# ── CRM 业务类型-主体映射 ──
@app.get("/api/config/crm-business-type-mappings")
def list_crm_business_type_mappings(session: Session = Depends(get_session)) -> dict:
    from backend.app.models import CrmBusinessTypeMapping
    items = session.query(CrmBusinessTypeMapping).order_by(CrmBusinessTypeMapping.business_type_code).all()
    return {"items": [{"id": m.id, "business_type_code": m.business_type_code, "business_type_name": m.business_type_name,
                       "entity_code": m.entity_code, "is_active": m.is_active} for m in items]}


@app.post("/api/config/crm-business-type-mappings")
def upsert_crm_business_type_mapping(payload: dict, session: Session = Depends(get_session)) -> dict:
    from backend.app.models import CrmBusinessTypeMapping
    code = payload.get("business_type_code", "").strip()
    if not code:
        raise HTTPException(status_code=400, detail="business_type_code 必填")
    m = session.query(CrmBusinessTypeMapping).filter(CrmBusinessTypeMapping.business_type_code == code).first()
    if m is None:
        m = CrmBusinessTypeMapping(business_type_code=code)
        session.add(m)
    m.business_type_name = payload.get("business_type_name", m.business_type_name or "")
    m.entity_code = payload.get("entity_code", m.entity_code or "")
    m.is_active = payload.get("is_active", m.is_active)
    m.updated_at = now_utc()
    session.commit()
    return {"ok": True, "business_type_code": code}


@app.delete("/api/config/crm-business-type-mappings/{code}")
def delete_crm_business_type_mapping(code: str, session: Session = Depends(get_session)) -> dict:
    from backend.app.models import CrmBusinessTypeMapping
    m = session.query(CrmBusinessTypeMapping).filter(CrmBusinessTypeMapping.business_type_code == code).first()
    if m is None:
        raise HTTPException(status_code=404, detail="Not found")
    session.delete(m)
    session.commit()
    return {"ok": True}


# ── 产品价格（按主体维度） ──

@app.get("/api/config/product-prices")
def list_product_prices(entity_code: str = "", sku_id: str = "", page: int = 1, page_size: int = 100, session: Session = Depends(get_session)) -> dict:
    q = session.query(ProductPrice)
    if entity_code:
        q = q.filter(ProductPrice.entity_code == entity_code)
    if sku_id:
        q = q.filter(ProductPrice.sku_id.like(f"%{sku_id}%"))
    total = q.count()
    items = q.order_by(ProductPrice.sku_id, ProductPrice.entity_code).offset((page - 1) * page_size).limit(page_size).all()
    return {"items": [{"id": p.id, "sku_id": p.sku_id, "entity_code": p.entity_code,
                       "unit_price": p.unit_price, "currency": p.currency, "is_active": p.is_active} for p in items],
            "total": total}


@app.post("/api/config/product-prices")
def upsert_product_price(payload: dict, session: Session = Depends(get_session)) -> dict:
    sku_id = payload.get("sku_id", "").strip()
    entity = payload.get("entity_code", "").strip()
    if not sku_id or not entity:
        raise HTTPException(status_code=400, detail="sku_id 和 entity_code 必填")
    p = session.query(ProductPrice).filter(ProductPrice.sku_id == sku_id, ProductPrice.entity_code == entity).first()
    if p is None:
        p = ProductPrice(sku_id=sku_id, entity_code=entity)
        session.add(p)
    p.unit_price = int(payload.get("unit_price", p.unit_price or 0))
    p.currency = payload.get("currency", p.currency or "CNY")
    p.is_active = payload.get("is_active", p.is_active)
    p.updated_at = now_utc()
    session.commit()
    return {"ok": True}


# ── 收件人配置 ──

@app.get("/api/config/mail-receivers")
def list_mail_receivers(session: Session = Depends(get_session)) -> dict:
    items = session.query(MailReceiverConfig).order_by(MailReceiverConfig.scene).all()
    return {"items": [{"id": r.id, "scene": r.scene, "to": json.loads(r.to_json) if r.to_json else [],
                       "cc": json.loads(r.cc_json) if r.cc_json else [], "is_active": r.is_active} for r in items]}


@app.post("/api/config/mail-receivers")
def upsert_mail_receiver(payload: dict, session: Session = Depends(get_session)) -> dict:
    scene = payload.get("scene", "").strip()
    if not scene:
        raise HTTPException(status_code=400, detail="scene 必填")
    r = session.query(MailReceiverConfig).filter(MailReceiverConfig.scene == scene).first()
    if r is None:
        r = MailReceiverConfig(scene=scene)
        session.add(r)
    r.to_json = json.dumps(payload.get("to", []), ensure_ascii=False)
    r.cc_json = json.dumps(payload.get("cc", []), ensure_ascii=False)
    r.is_active = payload.get("is_active", r.is_active)
    r.updated_at = now_utc()
    session.commit()
    return {"ok": True, "scene": scene}


# ── 仓库-主体映射 ──

@app.get("/api/config/warehouse-entity")
def list_warehouse_entity(session: Session = Depends(get_session)) -> dict:
    items = session.query(WarehouseEntityMapping).order_by(WarehouseEntityMapping.warehouse).all()
    return {"items": [{"id": m.id, "warehouse": m.warehouse, "entity_code": m.entity_code, "is_active": m.is_active} for m in items]}


@app.post("/api/config/warehouse-entity")
def upsert_warehouse_entity(payload: dict, session: Session = Depends(get_session)) -> dict:
    wh = payload.get("warehouse", "").strip()
    if not wh:
        raise HTTPException(status_code=400, detail="warehouse 必填")
    m = session.query(WarehouseEntityMapping).filter(WarehouseEntityMapping.warehouse == wh).first()
    if m is None:
        m = WarehouseEntityMapping(warehouse=wh)
        session.add(m)
    m.entity_code = payload.get("entity_code", m.entity_code)
    m.is_active = payload.get("is_active", m.is_active)
    m.updated_at = now_utc()
    session.commit()
    return {"ok": True, "warehouse": wh}


# ── 物料例外表 ──

@app.get("/api/config/material-entity")
def list_material_entity(session: Session = Depends(get_session)) -> dict:
    items = session.query(MaterialEntityException).order_by(MaterialEntityException.material_code).all()
    return {"items": [{"id": m.id, "material_code": m.material_code, "entity_code": m.entity_code, "is_active": m.is_active} for m in items]}


@app.post("/api/config/material-entity")
def upsert_material_entity(payload: dict, session: Session = Depends(get_session)) -> dict:
    mc = payload.get("material_code", "").strip()
    if not mc:
        raise HTTPException(status_code=400, detail="material_code 必填")
    m = session.query(MaterialEntityException).filter(MaterialEntityException.material_code == mc).first()
    if m is None:
        m = MaterialEntityException(material_code=mc)
        session.add(m)
    m.entity_code = payload.get("entity_code", m.entity_code)
    m.is_active = payload.get("is_active", m.is_active)
    m.updated_at = now_utc()
    session.commit()
    return {"ok": True, "material_code": mc}


# ── 库存 Excel 导入 ──

@app.post("/api/inventory/import-excel")
def import_inventory_excel(data: dict, session: Session = Depends(get_session)) -> dict:
    """导入库存数据（由前端上传解析后的 JSON 数据）"""
    rows = data.get("rows", [])
    warehouse = data.get("warehouse", "").strip()
    if not rows or not warehouse:
        raise HTTPException(status_code=400, detail="rows 和 warehouse 必填")
    created = updated = 0
    ts = now_utc()
    for row in rows:
        mat_code = str(row.get("material_code") or row.get("物料编码") or "").strip()
        mat_name = str(row.get("material_name") or row.get("物料名称") or "").strip()
        qty = float(row.get("quantity") or row.get("库存数量") or 0)
        if not mat_code:
            continue
        snap = session.query(ProductInventorySnapshot).filter(
            ProductInventorySnapshot.material_code == mat_code,
            ProductInventorySnapshot.warehouse_code == warehouse,
        ).first()
        if snap is None:
            snap = ProductInventorySnapshot(material_code=mat_code, warehouse_code=warehouse)
            session.add(snap)
            created += 1
        else:
            updated += 1
        snap.material_name = mat_name or snap.material_name
        snap.warehouse_name = warehouse
        snap.qty = qty
        snap.base_qty = qty
        snap.synced_at = ts
        snap.status = "Active"
        snap.updated_at = ts
    session.commit()
    return {"ok": True, "warehouse": warehouse, "created": created, "updated": updated, "total": len(rows)}


@app.get("/api/inventory/snapshots")
def list_raw_inventory_snapshots(
    warehouse: str = "",
    q: str = "",
    stock_status: str = "",
    page: int = 1,
    page_size: int = 50,
    include_total: bool = False,
    session: Session = Depends(get_session),
) -> dict:
    try:
        page = max(1, page)
        page_size = max(1, min(page_size, 200))
        q_obj = session.query(ProductInventorySnapshot)
        if warehouse:
            q_obj = q_obj.filter(ProductInventorySnapshot.warehouse_code == warehouse)
        if stock_status == "out_of_stock":
            q_obj = q_obj.filter(ProductInventorySnapshot.qty <= 0)
        if q.strip():
            pattern = f"%{q.strip()}%"
            from sqlalchemy import or_
            q_obj = q_obj.filter(
                or_(
                    ProductInventorySnapshot.material_code.like(pattern),
                    ProductInventorySnapshot.material_name.like(pattern),
                    ProductInventorySnapshot.warehouse_code.like(pattern),
                )
            )
        rows = (
            q_obj.with_entities(
                ProductInventorySnapshot.id,
                ProductInventorySnapshot.material_code,
                ProductInventorySnapshot.material_name,
                ProductInventorySnapshot.warehouse_code,
                ProductInventorySnapshot.qty,
                ProductInventorySnapshot.synced_at,
            )
            .order_by(ProductInventorySnapshot.warehouse_code, ProductInventorySnapshot.material_code)
            .offset((page - 1) * page_size)
            .limit(page_size + 1)
            .all()
        )
        has_more = len(rows) > page_size
        items = rows[:page_size]
        if include_total:
            total = q_obj.order_by(None).count()
            total_pages = max(1, (total + page_size - 1) // page_size)
            total_estimated = False
        else:
            total = (page - 1) * page_size + len(items) + (1 if has_more else 0)
            total_pages = page + (1 if has_more else 0)
            total_estimated = has_more
        return {
            "items": [{"id": s.id, "material_code": s.material_code, "material_name": s.material_name or "",
                       "warehouse_code": s.warehouse_code, "qty": s.qty, "synced_at": s.synced_at.isoformat() if s.synced_at else None} for s in items],
            "total": total,
            "total_estimated": total_estimated,
            "has_more": has_more,
            "page": page,
            "page_size": page_size,
            "total_pages": total_pages,
        }
    except Exception as e:
        logger.exception("库存快照查询失败")
        return {"items": [], "total": 0, "page": page, "page_size": page_size, "total_pages": 0, "error": str(e)}


@app.get("/api/inventory/warehouses")
def list_inventory_warehouses(session: Session = Depends(get_session)) -> dict:
    try:
        rows = session.query(ProductInventorySnapshot.warehouse_code).distinct().order_by(ProductInventorySnapshot.warehouse_code).all()
        return {"warehouses": [r[0] for r in rows if r[0]]}
    except Exception as e:
        return {"warehouses": []}
