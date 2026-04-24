from __future__ import annotations

import asyncio
import base64
import binascii
import contextlib
import logging
import uuid
from datetime import datetime
from pathlib import Path

from fastapi import Depends, FastAPI, HTTPException, Query, Request
from fastapi.responses import FileResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from sqlalchemy import or_
from sqlalchemy.orm import Session

from backend.app.config import settings
from backend.app.database import SessionLocal, init_db
from backend.app.models import (
    AuditEvent,
    AttachmentAsset,
    BackupJob,
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
    WorkflowImportJob,
    WorkflowVersion,
    now_utc,
)
from backend.app.schemas import (
    DemoOrderRequest,
    DepartmentUpsert,
    ExceptionRequirementPatchRequest,
    ExceptionResolveRequest,
    InitialReviewConfigUpdate,
    LoginRequest,
    MailRuntimeConfigUpdate,
    ModelChatTestRequest,
    ModelProviderUpdate,
    ProductionFeedbackRequest,
    ProductionQuestionRequest,
    SalesReplyRequest,
    TaskManualCloseRequest,
    TemplateUpdate,
    WorkflowContactMapUpdate,
    WorkflowChatGenerateRequest,
    WorkflowChatSaveRequest,
    WorkflowImportRequest,
    WorkflowVersionUpdateRequest,
    WeeklyReportRecipientsUpdate,
)
from backend.app.config import MAIL_LOGIN_MIN_INTERVAL_SECONDS, MAIL_WORKER_MIN_INTERVAL_SECONDS
from backend.app.services.auth import COOKIE_NAME, create_session_token, parse_session_token
from backend.app.services.bootstrap import seed_defaults, set_config
from backend.app.services.e2e_mail import run_tencent_mail_e2e
from backend.app.services.initial_review import (
    FIELD_LABELS,
    OPERATOR_OPTIONS,
    dedupe_initial_review_rules,
    initial_review_config,
    remember_deleted_workflow_review_rules,
    sync_workflow_review_rules_to_initial_review,
)
from backend.app.services.jsonutil import as_list, dumps, loads
from backend.app.services.jobs import run_pending_jobs
from backend.app.services.mail_adapter import send_pending_smtp, sync_imap_mailbox
from backend.app.services.mail_worker import configured_mail_worker_interval_seconds, run_mail_auto_worker_once
from backend.app.services.mail_throttle import clamp_mail_interval_seconds
from backend.app.services.model_provider import call_model, extract_chat_content
from backend.app.services.operations import cleanup_preview, create_backup, execute_cleanup, storage_usage, weekly_report_csv
from backend.app.services.pdf import simple_pdf
from backend.app.services.workflow_rules import (
    activate_workflow_version,
    chat_generate_workflow_rule,
    deactivate_workflow_version,
    delete_workflow_version,
    import_structured_workflow_rules,
    import_workflow_document,
    list_workflow_rules,
    save_workflow_version_rules,
)
from backend.app.services.workflow import (
    approve_task,
    create_inbound_mail,
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


app = FastAPI(title="商务生产任务单智能体 MVP")

PUBLIC_API_PATHS = {"/api/auth/login", "/api/auth/logout", "/api/auth/me"}
logger = logging.getLogger(__name__)
mail_worker_task: asyncio.Task | None = None


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
def health() -> dict:
    return {"status": "ok"}


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
    secret_keys = {"bot_email_password", "e2e_sales_password", "e2e_production_password"}
    for key, value in values.items():
        if value in (None, ""):
            continue
        set_config(session, key, str(value), is_secret=key in secret_keys)
    session.commit()
    return config(session)


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
    }


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
        serialize_outbound_mail,
        page,
        page_size,
        {
            "status_options": distinct_values(session, OutboundMailJob.status),
            "mail_type_options": distinct_values(session, OutboundMailJob.mail_type),
        },
    )


@app.post("/api/outbound-mails/send-pending")
def send_pending_outbound(limit: int = 20, session: Session = Depends(get_session)) -> dict:
    try:
        result = send_pending_smtp(session, limit=limit)
    except RuntimeError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    session.commit()
    return result


@app.post("/api/outbound-mails/{job_id}/retry")
def retry_outbound(job_id: str, session: Session = Depends(get_session)) -> dict:
    try:
        job = retry_outbound_mail(session, job_id)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    session.commit()
    return {"outbound_job_id": job.id, "status": job.status}


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
    return page_response(
        query.order_by(ProductionDepartment.department_code),
        serialize_department,
        page,
        page_size,
        {"status_options": distinct_values(session, ProductionDepartment.status)},
    )


@app.put("/api/departments/default")
def upsert_default_department(payload: DepartmentUpsert, session: Session = Depends(get_session)) -> dict:
    dept = session.query(ProductionDepartment).filter_by(department_code=payload.department_code).one_or_none()
    if dept is None:
        dept = ProductionDepartment(department_code=payload.department_code)
        session.add(dept)
    dept.department_name = payload.department_name
    dept.mail_to_json = dumps([str(email) for email in payload.mail_to])
    dept.mail_cc_json = dumps([str(email) for email in payload.mail_cc])
    dept.status = "Active"
    session.commit()
    return {"ok": True, "department_id": dept.id}


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


def serialize_department(row: ProductionDepartment) -> dict:
    return {
        "id": row.id,
        "department_code": row.department_code,
        "department_name": row.department_name,
        "mail_to": as_list(row.mail_to_json),
        "mail_cc": as_list(row.mail_cc_json),
        "status": row.status,
    }


def serialize_outbound_mail(row: OutboundMailJob) -> dict:
    return {
        "id": row.id,
        "mail_type": row.mail_type,
        "to": as_list(row.to_json),
        "cc": as_list(row.cc_json),
        "subject": row.subject,
        "status": row.status,
        "created_at": row.created_at.isoformat(),
    }


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
