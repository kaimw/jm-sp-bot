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
