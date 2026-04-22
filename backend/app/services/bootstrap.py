from __future__ import annotations

from sqlalchemy.orm import Session

from backend.app.config import settings
from backend.app.models import MailTemplate, ModelProviderConfig, ProductionDepartment, SystemConfig, now_utc
from backend.app.services.jsonutil import dumps


DEFAULT_TASK_SUBJECT = "[生产任务单][{{task_no}}][{{customer_name}}][{{product_summary}}][V{{version_no}}]"
DEFAULT_TASK_BODY = """生产部同事好：

请根据以下信息安排生产评估和排产。

任务单编号：{{task_no}}
版本：V{{version_no}}
客户名称：{{customer_name}}
销售人员：{{salesperson_name}} <{{salesperson_email}}>

产品/规格：{{product_summary}}
数量：{{quantity_text}}
期望交期：{{expected_delivery_date}}

请确认是否可以安排生产。如信息不足，请直接回复本邮件说明疑问点。

{{bot_signature}}
"""


def set_config(session: Session, key: str, value: str, *, is_secret: bool = False) -> None:
    config = session.get(SystemConfig, key)
    if config is None:
        session.add(SystemConfig(key=key, value=value, is_secret=is_secret))
        return
    config.value = value
    config.is_secret = is_secret
    config.updated_at = now_utc()


def upsert_config(session: Session, key: str, value: str, *, is_secret: bool = False) -> None:
    config = session.get(SystemConfig, key)
    if config is None:
        session.add(SystemConfig(key=key, value=value, is_secret=is_secret))
        return
    if not config.is_secret:
        config.value = value
        config.updated_at = now_utc()
    config.is_secret = is_secret


def ensure_config(session: Session, key: str, value: str, *, is_secret: bool = False) -> None:
    if session.get(SystemConfig, key) is None:
        session.add(SystemConfig(key=key, value=value, is_secret=is_secret))


def seed_defaults(session: Session) -> None:
    upsert_config(session, "bot_email", settings.bot_email)
    if settings.bot_email_password:
        upsert_config(session, "bot_email_password", settings.bot_email_password, is_secret=True)
    upsert_config(session, "bot_display_name", settings.bot_display_name)
    upsert_config(session, "bot_signature", settings.bot_signature)
    upsert_config(session, "imap_host", settings.imap_host)
    upsert_config(session, "imap_port", str(settings.imap_port))
    upsert_config(session, "smtp_host", settings.smtp_host)
    upsert_config(session, "smtp_port", str(settings.smtp_port))
    upsert_config(session, "mail_auto_worker_interval_seconds", str(settings.mail_auto_worker_interval_seconds))
    upsert_config(session, "ceo_email", settings.ceo_email)
    upsert_config(session, "ops_cc_email", settings.ops_cc_email)
    upsert_config(session, "e2e_sales_email", settings.e2e_sales_email)
    if settings.e2e_sales_password:
        upsert_config(session, "e2e_sales_password", settings.e2e_sales_password, is_secret=True)
    upsert_config(session, "e2e_production_email", settings.e2e_production_email)
    if settings.e2e_production_password:
        upsert_config(session, "e2e_production_password", settings.e2e_production_password, is_secret=True)
    upsert_config(session, "weekly_report_to_json", dumps([settings.ceo_email]), is_secret=False)
    upsert_config(session, "weekly_report_cc_json", dumps([settings.ops_cc_email]), is_secret=False)
    upsert_config(session, "zip_max_bytes", str(settings.zip_max_bytes), is_secret=False)
    upsert_config(session, "zip_max_depth", str(settings.zip_max_depth), is_secret=False)
    upsert_config(session, "storage_budget_bytes", str(settings.storage_budget_bytes), is_secret=False)
    upsert_config(session, "non_target_retention_days", str(settings.non_target_retention_days), is_secret=False)
    ensure_config(session, "initial_review_enabled", "true", is_secret=False)
    ensure_config(session, "initial_review_required_fields_json", dumps(["customer_name", "product_summary", "quantity_text", "expected_delivery_date"]), is_secret=False)
    ensure_config(session, "initial_review_rules_json", "[]", is_secret=False)
    ensure_config(session, "llm_fallback_enabled", "true", is_secret=False)
    ensure_config(session, "conversation_max_rounds", "3", is_secret=False)

    template = (
        session.query(MailTemplate)
        .filter(MailTemplate.template_code == "production_task", MailTemplate.version == "v1")
        .one_or_none()
    )
    if template is None:
        session.add(
            MailTemplate(
                template_code="production_task",
                template_name="生产任务单默认模板",
                template_type="TaskIssue",
                subject_template=DEFAULT_TASK_SUBJECT,
                body_template=DEFAULT_TASK_BODY,
                version="v1",
            )
        )

    model_config = session.query(ModelProviderConfig).filter(ModelProviderConfig.title == settings.model_title).one_or_none()
    if model_config is None:
        session.add(
            ModelProviderConfig(
                title=settings.model_title,
                provider=settings.model_provider,
                model_name=settings.model_name,
                api_base=settings.model_api_base,
                credential_ref="env:MODEL_API_KEY",
                status="Active",
            )
        )

    default_department = session.query(ProductionDepartment).filter_by(department_code="default").one_or_none()
    if default_department is None:
        session.add(
            ProductionDepartment(
                department_code="default",
                department_name="默认生产部门",
                mail_to_json=dumps([]),
                mail_cc_json=dumps([]),
                status="Active",
            )
        )
