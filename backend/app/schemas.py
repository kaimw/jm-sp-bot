from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field


class DemoOrderRequest(BaseModel):
    from_address: str
    subject: str = Field(default="生产订单需求")
    body_text: str


class LoginRequest(BaseModel):
    username: str
    password: str


class DepartmentUpsert(BaseModel):
    department_code: str = "default"
    department_name: str = "默认生产部门"
    mail_to: list[str]
    mail_cc: list[str] = []


class TemplateUpdate(BaseModel):
    subject_template: str
    body_template: str
    uploaded_asset_ref: str | None = None


class ProductionFeedbackRequest(BaseModel):
    feedback_type: str = Field(pattern="^(confirmed|rejected)$")
    note: str = ""


class ProductionQuestionRequest(BaseModel):
    question_text: str


class SalesReplyRequest(BaseModel):
    reply_text: str


class ExceptionResolveRequest(BaseModel):
    note: str = ""


class ExceptionRequirementPatchRequest(BaseModel):
    customer_name: str | None = None
    product_summary: str | None = None
    quantity_text: str | None = None
    expected_delivery_date: str | None = None
    external_order_no: str | None = None
    salesperson_email: str | None = None
    salesperson_name: str | None = None
    clear_risk_flags: bool = True


class WeeklyReportRecipientsUpdate(BaseModel):
    to: list[str]
    cc: list[str] = []


class MailRuntimeConfigUpdate(BaseModel):
    bot_email: str | None = None
    bot_email_password: str | None = None
    bot_display_name: str | None = None
    bot_signature: str | None = None
    imap_host: str | None = None
    imap_port: int | None = None
    smtp_host: str | None = None
    smtp_port: int | None = None
    mail_auto_worker_interval_seconds: int | None = None
    ceo_email: str | None = None
    ops_cc_email: str | None = None
    zip_max_bytes: int | None = None
    zip_max_depth: int | None = None
    storage_budget_bytes: int | None = None
    non_target_retention_days: int | None = None
    llm_fallback_enabled: bool | None = None
    conversation_max_rounds: int | None = None
    e2e_sales_email: str | None = None
    e2e_sales_password: str | None = None
    e2e_production_email: str | None = None
    e2e_production_password: str | None = None


class InitialReviewRuleUpdate(BaseModel):
    id: str | None = None
    name: str
    field: str
    operator: str
    value: str = ""
    message: str = ""
    enabled: bool = True


class InitialReviewConfigUpdate(BaseModel):
    enabled: bool = True
    required_fields: list[str] = []
    rules: list[InitialReviewRuleUpdate] = []


class ModelProviderUpdate(BaseModel):
    model_config = ConfigDict(protected_namespaces=())

    title: str | None = None
    provider: str | None = None
    model_name: str | None = None
    api_base: str | None = None
    credential_ref: str | None = None
    api_key: str | None = None


class ModelChatTestRequest(BaseModel):
    message: str
    system_prompt: str | None = None
