from __future__ import annotations

import uuid
from datetime import datetime, timezone

from sqlalchemy import Boolean, DateTime, ForeignKey, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship

from backend.app.database import Base


def new_id() -> str:
    return str(uuid.uuid4())


def now_utc() -> datetime:
    return datetime.now(timezone.utc)


class SystemConfig(Base):
    __tablename__ = "system_configs"

    key: Mapped[str] = mapped_column(String(128), primary_key=True)
    value: Mapped[str] = mapped_column(Text, nullable=False)
    value_type: Mapped[str] = mapped_column(String(32), default="string", nullable=False)
    is_secret: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now_utc, nullable=False)


class MailTemplate(Base):
    __tablename__ = "mail_templates"
    __table_args__ = (UniqueConstraint("template_code", "version", name="uq_template_code_version"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    template_code: Mapped[str] = mapped_column(String(64), nullable=False)
    template_name: Mapped[str] = mapped_column(String(128), nullable=False)
    template_type: Mapped[str] = mapped_column(String(64), nullable=False)
    subject_template: Mapped[str] = mapped_column(Text, nullable=False)
    body_template: Mapped[str] = mapped_column(Text, nullable=False)
    uploaded_asset_ref: Mapped[str | None] = mapped_column(Text)
    version: Mapped[str] = mapped_column(String(32), default="v1", nullable=False)
    status: Mapped[str] = mapped_column(String(32), default="Active", nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now_utc, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now_utc, nullable=False)


class ProductionDepartment(Base):
    __tablename__ = "production_departments"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    department_code: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)
    department_name: Mapped[str] = mapped_column(String(128), nullable=False)
    mail_to_json: Mapped[str] = mapped_column(Text, default="[]", nullable=False)
    mail_cc_json: Mapped[str] = mapped_column(Text, default="[]", nullable=False)
    status: Mapped[str] = mapped_column(String(32), default="Active", nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now_utc, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now_utc, nullable=False)


class ModelProviderConfig(Base):
    __tablename__ = "model_provider_configs"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    title: Mapped[str] = mapped_column(String(128), nullable=False)
    provider: Mapped[str] = mapped_column(String(64), nullable=False)
    model_name: Mapped[str] = mapped_column(String(128), nullable=False)
    api_base: Mapped[str] = mapped_column(Text, nullable=False)
    credential_ref: Mapped[str | None] = mapped_column(Text)
    status: Mapped[str] = mapped_column(String(32), default="Active", nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now_utc, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now_utc, nullable=False)


class WorkflowDefinition(Base):
    __tablename__ = "workflow_definitions"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    workflow_code: Mapped[str] = mapped_column(String(128), unique=True, nullable=False)
    workflow_name: Mapped[str] = mapped_column(String(255), nullable=False)
    status: Mapped[str] = mapped_column(String(32), default="Active", nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now_utc, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now_utc, nullable=False)


class WorkflowVersion(Base):
    __tablename__ = "workflow_versions"
    __table_args__ = (UniqueConstraint("workflow_id", "version_no", name="uq_workflow_version"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    workflow_id: Mapped[str] = mapped_column(String(36), ForeignKey("workflow_definitions.id"), nullable=False)
    version_no: Mapped[int] = mapped_column(Integer, nullable=False)
    source_asset_ref: Mapped[str | None] = mapped_column(Text)
    source_text: Mapped[str | None] = mapped_column(Text)
    compiled_rules_json: Mapped[str] = mapped_column(Text, default="{}", nullable=False)
    status: Mapped[str] = mapped_column(String(32), default="Draft", nullable=False)
    created_by: Mapped[str | None] = mapped_column(String(128))
    approved_by: Mapped[str | None] = mapped_column(String(128))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now_utc, nullable=False)
    approved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now_utc, nullable=False)


class WorkflowImportJob(Base):
    __tablename__ = "workflow_import_jobs"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    file_name: Mapped[str] = mapped_column(Text, nullable=False)
    source_asset_ref: Mapped[str | None] = mapped_column(Text)
    source_text: Mapped[str] = mapped_column(Text, default="", nullable=False)
    parse_status: Mapped[str] = mapped_column(String(32), default="Pending", nullable=False)
    llm_output_json: Mapped[str] = mapped_column(Text, default="[]", nullable=False)
    validation_errors_json: Mapped[str] = mapped_column(Text, default="[]", nullable=False)
    diff_json: Mapped[str] = mapped_column(Text, default="[]", nullable=False)
    status: Mapped[str] = mapped_column(String(32), default="Draft", nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now_utc, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now_utc, nullable=False)


class MailMessage(Base):
    __tablename__ = "mail_messages"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    direction: Mapped[str] = mapped_column(String(16), nullable=False)
    from_address: Mapped[str] = mapped_column(String(255), nullable=False)
    to_json: Mapped[str] = mapped_column(Text, default="[]", nullable=False)
    cc_json: Mapped[str] = mapped_column(Text, default="[]", nullable=False)
    subject: Mapped[str] = mapped_column(Text, nullable=False)
    body_text: Mapped[str] = mapped_column(Text, default="", nullable=False)
    classification: Mapped[str | None] = mapped_column(String(64))
    classification_confidence: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    related_task_id: Mapped[str | None] = mapped_column(String(36), ForeignKey("production_tasks.id"))
    dedupe_key: Mapped[str] = mapped_column(String(512), unique=True, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now_utc, nullable=False)


class AttachmentAsset(Base):
    __tablename__ = "attachment_assets"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    mail_id: Mapped[str] = mapped_column(String(36), ForeignKey("mail_messages.id"), nullable=False)
    parent_attachment_id: Mapped[str | None] = mapped_column(String(36), ForeignKey("attachment_assets.id"))
    file_name: Mapped[str] = mapped_column(Text, nullable=False)
    content_type: Mapped[str | None] = mapped_column(String(128))
    file_size: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    file_hash: Mapped[str] = mapped_column(String(128), nullable=False)
    storage_ref: Mapped[str] = mapped_column(Text, nullable=False)
    parse_status: Mapped[str] = mapped_column(String(32), default="Pending", nullable=False)
    extracted_text: Mapped[str | None] = mapped_column(Text)
    parse_error: Mapped[str | None] = mapped_column(Text)
    archive_path: Mapped[str | None] = mapped_column(Text)
    archive_depth: Mapped[int | None] = mapped_column(Integer)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now_utc, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now_utc, nullable=False)


class ProcessingJob(Base):
    __tablename__ = "processing_jobs"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    job_type: Mapped[str] = mapped_column(String(64), nullable=False)
    payload_json: Mapped[str] = mapped_column(Text, default="{}", nullable=False)
    status: Mapped[str] = mapped_column(String(32), default="Pending", nullable=False)
    attempt_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    error_message: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now_utc, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now_utc, nullable=False)


class ModelCallLog(Base):
    __tablename__ = "model_call_logs"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    provider_config_id: Mapped[str] = mapped_column(String(36), ForeignKey("model_provider_configs.id"), nullable=False)
    task_type: Mapped[str] = mapped_column(String(64), nullable=False)
    related_object_type: Mapped[str | None] = mapped_column(String(64))
    related_object_id: Mapped[str | None] = mapped_column(String(36))
    input_summary: Mapped[str | None] = mapped_column(Text)
    output_json: Mapped[str | None] = mapped_column(Text)
    latency_ms: Mapped[int | None] = mapped_column(Integer)
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    error_message: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now_utc, nullable=False)


class MaintenanceSession(Base):
    __tablename__ = "maintenance_sessions"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    user_message: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[str] = mapped_column(String(32), default="Completed", nullable=False)
    risk_level: Mapped[str] = mapped_column(String(32), default="Low", nullable=False)
    collected_context_json: Mapped[str] = mapped_column(Text, default="{}", nullable=False)
    diagnosis_md: Mapped[str] = mapped_column(Text, default="", nullable=False)
    proposed_actions_json: Mapped[str] = mapped_column(Text, default="[]", nullable=False)
    created_by: Mapped[str] = mapped_column(String(128), default="system", nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now_utc, nullable=False)


class MaintenanceAction(Base):
    __tablename__ = "maintenance_actions"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    session_id: Mapped[str] = mapped_column(String(36), ForeignKey("maintenance_sessions.id"), nullable=False)
    action_type: Mapped[str] = mapped_column(String(64), nullable=False)
    status: Mapped[str] = mapped_column(String(32), default="Proposed", nullable=False)
    input_json: Mapped[str] = mapped_column(Text, default="{}", nullable=False)
    result_json: Mapped[str] = mapped_column(Text, default="{}", nullable=False)
    error_message: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now_utc, nullable=False)
    approved_by: Mapped[str | None] = mapped_column(String(128))
    approved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class OrderRequirement(Base):
    __tablename__ = "order_requirements"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    source_mail_id: Mapped[str] = mapped_column(String(36), ForeignKey("mail_messages.id"), nullable=False)
    internal_order_no: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)
    external_order_no: Mapped[str | None] = mapped_column(String(128))
    customer_name: Mapped[str | None] = mapped_column(String(255))
    salesperson_name: Mapped[str | None] = mapped_column(String(128))
    salesperson_email: Mapped[str | None] = mapped_column(String(255))
    product_summary: Mapped[str | None] = mapped_column(Text)
    expected_delivery_date: Mapped[str | None] = mapped_column(String(32))
    quantity_text: Mapped[str | None] = mapped_column(String(128))
    missing_fields_json: Mapped[str] = mapped_column(Text, default="[]", nullable=False)
    risk_flags_json: Mapped[str] = mapped_column(Text, default="[]", nullable=False)
    status: Mapped[str] = mapped_column(String(32), default="Extracted", nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now_utc, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now_utc, nullable=False)


class RequirementWorkflowBinding(Base):
    __tablename__ = "requirement_workflow_bindings"
    __table_args__ = (UniqueConstraint("requirement_id", name="uq_requirement_workflow_binding"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    requirement_id: Mapped[str] = mapped_column(String(36), ForeignKey("order_requirements.id"), nullable=False)
    workflow_version_id: Mapped[str | None] = mapped_column(String(36), ForeignKey("workflow_versions.id"))
    workflow_code: Mapped[str | None] = mapped_column(String(128))
    workflow_name: Mapped[str | None] = mapped_column(String(255))
    match_confidence: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    route_to_json: Mapped[str] = mapped_column(Text, default="[]", nullable=False)
    route_cc_json: Mapped[str] = mapped_column(Text, default="[]", nullable=False)
    subject_template: Mapped[str | None] = mapped_column(Text)
    body_template: Mapped[str | None] = mapped_column(Text)
    required_fields_json: Mapped[str] = mapped_column(Text, default="[]", nullable=False)
    required_attachments_json: Mapped[str] = mapped_column(Text, default="[]", nullable=False)
    extracted_fields_json: Mapped[str] = mapped_column(Text, default="{}", nullable=False)
    missing_fields_json: Mapped[str] = mapped_column(Text, default="[]", nullable=False)
    unresolved_contacts_json: Mapped[str] = mapped_column(Text, default="[]", nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now_utc, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now_utc, nullable=False)


class ExtractionEvidence(Base):
    __tablename__ = "extraction_evidence"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    requirement_id: Mapped[str] = mapped_column(String(36), ForeignKey("order_requirements.id"), nullable=False)
    field_name: Mapped[str] = mapped_column(String(64), nullable=False)
    field_value: Mapped[str] = mapped_column(Text, nullable=False)
    source_type: Mapped[str] = mapped_column(String(32), nullable=False)
    source_mail_id: Mapped[str | None] = mapped_column(String(36), ForeignKey("mail_messages.id"))
    source_attachment_id: Mapped[str | None] = mapped_column(String(36), ForeignKey("attachment_assets.id"))
    evidence_text: Mapped[str] = mapped_column(Text, nullable=False)
    confidence: Mapped[int] = mapped_column(Integer, default=80, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now_utc, nullable=False)


class ProductionTask(Base):
    __tablename__ = "production_tasks"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    task_no: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)
    requirement_id: Mapped[str] = mapped_column(String(36), ForeignKey("order_requirements.id"), nullable=False)
    current_version_no: Mapped[int] = mapped_column(Integer, default=1, nullable=False)
    status: Mapped[str] = mapped_column(String(64), default="TaskDrafted", nullable=False)
    production_department_id: Mapped[str | None] = mapped_column(String(36), ForeignKey("production_departments.id"))
    target_mail_to_json: Mapped[str] = mapped_column(Text, default="[]", nullable=False)
    target_mail_cc_json: Mapped[str] = mapped_column(Text, default="[]", nullable=False)
    issued_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    confirmed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    manual_takeover: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    closed_reason: Mapped[str | None] = mapped_column(String(128))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now_utc, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now_utc, nullable=False)

    requirement: Mapped[OrderRequirement] = relationship()
    department: Mapped[ProductionDepartment | None] = relationship()
    versions: Mapped[list["ProductionTaskVersion"]] = relationship(back_populates="task")
    questions: Mapped[list["QuestionAndReply"]] = relationship(back_populates="task")


class ProductionTaskVersion(Base):
    __tablename__ = "production_task_versions"
    __table_args__ = (UniqueConstraint("task_id", "version_no", name="uq_task_version"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    task_id: Mapped[str] = mapped_column(String(36), ForeignKey("production_tasks.id"), nullable=False)
    version_no: Mapped[int] = mapped_column(Integer, nullable=False)
    subject: Mapped[str] = mapped_column(Text, nullable=False)
    body: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[str] = mapped_column(String(32), default="Draft", nullable=False)
    approved_by: Mapped[str | None] = mapped_column(String(128))
    approved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now_utc, nullable=False)

    task: Mapped[ProductionTask] = relationship(back_populates="versions")


class QuestionAndReply(Base):
    __tablename__ = "question_and_replies"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    task_id: Mapped[str] = mapped_column(String(36), ForeignKey("production_tasks.id"), nullable=False)
    production_question_mail_id: Mapped[str | None] = mapped_column(String(36), ForeignKey("mail_messages.id"))
    sales_reply_mail_id: Mapped[str | None] = mapped_column(String(36), ForeignKey("mail_messages.id"))
    question_text: Mapped[str] = mapped_column(Text, nullable=False)
    reply_text: Mapped[str | None] = mapped_column(Text)
    status: Mapped[str] = mapped_column(String(32), default="AwaitingSalesReply", nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now_utc, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now_utc, nullable=False)

    task: Mapped[ProductionTask] = relationship(back_populates="questions")


class OutboundMailJob(Base):
    __tablename__ = "outbound_mail_jobs"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    related_task_id: Mapped[str | None] = mapped_column(String(36), ForeignKey("production_tasks.id"))
    related_version_id: Mapped[str | None] = mapped_column(String(36), ForeignKey("production_task_versions.id"))
    mail_type: Mapped[str] = mapped_column(String(64), nullable=False)
    to_json: Mapped[str] = mapped_column(Text, default="[]", nullable=False)
    cc_json: Mapped[str] = mapped_column(Text, default="[]", nullable=False)
    subject: Mapped[str] = mapped_column(Text, nullable=False)
    body: Mapped[str] = mapped_column(Text, nullable=False)
    idempotency_key: Mapped[str] = mapped_column(String(512), unique=True, nullable=False)
    status: Mapped[str] = mapped_column(String(32), default="Pending", nullable=False)
    # 重试相关：指数退避自动重试
    attempt_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    next_retry_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_error: Mapped[str | None] = mapped_column(String(1000), nullable=True)
    # 优先级（越小越高）：10=收件回执 20=业务推进 30=任务单 40=通知 60=周报
    priority: Mapped[int] = mapped_column(Integer, default=40, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now_utc, nullable=False)


class ExceptionCase(Base):
    __tablename__ = "exception_cases"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    related_task_id: Mapped[str | None] = mapped_column(String(36), ForeignKey("production_tasks.id"))
    exception_type: Mapped[str] = mapped_column(String(64), nullable=False)
    severity: Mapped[str] = mapped_column(String(32), default="Medium", nullable=False)
    detail: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[str] = mapped_column(String(32), default="Open", nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now_utc, nullable=False)


class AuditEvent(Base):
    __tablename__ = "audit_events"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    event_type: Mapped[str] = mapped_column(String(64), nullable=False)
    actor: Mapped[str] = mapped_column(String(128), default="System", nullable=False)
    related_object_type: Mapped[str] = mapped_column(String(64), nullable=False)
    related_object_id: Mapped[str] = mapped_column(String(36), nullable=False)
    detail: Mapped[str] = mapped_column(Text, default="{}", nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now_utc, nullable=False)


class MailWorkflowMatch(Base):
    __tablename__ = "mail_workflow_matches"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    mail_id: Mapped[str] = mapped_column(String(36), ForeignKey("mail_messages.id"), nullable=False)
    workflow_version_id: Mapped[str | None] = mapped_column(String(36), ForeignKey("workflow_versions.id"))
    workflow_code: Mapped[str | None] = mapped_column(String(128))
    confidence: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    match_detail_json: Mapped[str] = mapped_column(Text, default="{}", nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now_utc, nullable=False)


class CleanupJob(Base):
    __tablename__ = "cleanup_jobs"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    job_type: Mapped[str] = mapped_column(String(32), default="NonTargetRetention", nullable=False)
    status: Mapped[str] = mapped_column(String(32), default="Preview", nullable=False)
    cutoff_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    preview_json: Mapped[str] = mapped_column(Text, default="{}", nullable=False)
    result_json: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now_utc, nullable=False)
    executed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class BackupJob(Base):
    __tablename__ = "backup_jobs"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    backup_type: Mapped[str] = mapped_column(String(32), default="Manual", nullable=False)
    status: Mapped[str] = mapped_column(String(32), default="Completed", nullable=False)
    storage_ref: Mapped[str] = mapped_column(Text, nullable=False)
    manifest_json: Mapped[str] = mapped_column(Text, default="{}", nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now_utc, nullable=False)


# ==========================================
# Product Management Module Models
# ==========================================

class ProductSPU(Base):
    __tablename__ = "product_spus"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    spu_id: Mapped[str] = mapped_column(String(128), unique=True, nullable=False)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    name_en: Mapped[str | None] = mapped_column(String(255))
    brand: Mapped[str | None] = mapped_column(String(128))
    category: Mapped[str | None] = mapped_column(String(128))
    product_line: Mapped[str | None] = mapped_column(String(128))
    product_type: Mapped[str | None] = mapped_column(String(128))
    positioning: Mapped[str | None] = mapped_column(String(128))
    launch_time: Mapped[str | None] = mapped_column(String(64))
    lifecycle: Mapped[str | None] = mapped_column(String(64))
    extended_info_json: Mapped[str] = mapped_column(Text, default="{}", nullable=False)
    status: Mapped[str] = mapped_column(String(32), default="Active", nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now_utc, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now_utc, nullable=False)

    skus: Mapped[list["ProductSKU"]] = relationship(back_populates="spu")


class ProductSKU(Base):
    __tablename__ = "product_skus"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    spu_uuid: Mapped[str] = mapped_column(String(36), ForeignKey("product_spus.id"), nullable=False)
    sku_id: Mapped[str] = mapped_column(String(128), unique=True, nullable=False)
    model: Mapped[str | None] = mapped_column(String(128))
    version: Mapped[str | None] = mapped_column(String(128))
    barcode: Mapped[str | None] = mapped_column(String(128))
    cost_price: Mapped[int | None] = mapped_column(Integer)
    msrp: Mapped[int | None] = mapped_column(Integer)
    attributes_json: Mapped[str] = mapped_column(Text, default="{}", nullable=False)
    supply_info_json: Mapped[str] = mapped_column(Text, default="{}", nullable=False)
    media_info_json: Mapped[str] = mapped_column(Text, default="{}", nullable=False)
    status: Mapped[str] = mapped_column(String(32), default="Active", nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now_utc, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now_utc, nullable=False)

    spu: Mapped[ProductSPU] = relationship(back_populates="skus")
    channel_pricings: Mapped[list["ChannelPricing"]] = relationship(back_populates="sku")


class ChannelPricing(Base):
    __tablename__ = "channel_pricings"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    sku_uuid: Mapped[str] = mapped_column(String(36), ForeignKey("product_skus.id"), nullable=False)
    channel: Mapped[str] = mapped_column(String(64), nullable=False)
    channel_sku_id: Mapped[str | None] = mapped_column(String(128))
    listing_id: Mapped[str | None] = mapped_column(String(128))
    status: Mapped[str | None] = mapped_column(String(64))
    tier_a_price: Mapped[int | None] = mapped_column(Integer) # Stored as cents
    tier_b_price: Mapped[int | None] = mapped_column(Integer)
    tier_c_price: Mapped[int | None] = mapped_column(Integer)
    map_price: Mapped[int | None] = mapped_column(Integer) # Minimum Advertised Price
    max_price: Mapped[int | None] = mapped_column(Integer) # Maximum Price
    promo_start_time: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    promo_end_time: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    currency: Mapped[str] = mapped_column(String(16), default="USD", nullable=False)
    stock_quantity: Mapped[int | None] = mapped_column(Integer)
    manager: Mapped[str | None] = mapped_column(String(128))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now_utc, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now_utc, nullable=False)

    sku: Mapped[ProductSKU] = relationship(back_populates="channel_pricings")


class PromotionRule(Base):
    __tablename__ = "promotion_rules"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    channel: Mapped[str | None] = mapped_column(String(64))
    start_time: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    end_time: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    priority: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    discount_type: Mapped[str] = mapped_column(String(32), nullable=False) # e.g. PERCENTAGE, FIXED_AMOUNT
    discount_value: Mapped[int] = mapped_column(Integer, nullable=False)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now_utc, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now_utc, nullable=False)
