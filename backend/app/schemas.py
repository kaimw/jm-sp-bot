from __future__ import annotations

from datetime import datetime
from typing import Any

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


class TaskManualCloseRequest(BaseModel):
    note: str = ""


class TaskClearRequest(BaseModel):
    admin_password: str


class AdminPasswordRequest(BaseModel):
    admin_password: str


class ExceptionResolveRequest(BaseModel):
    note: str = ""
    actor: str = "operator"
    confirm_risk: bool = False
    resolution_evidence: dict[str, Any] | None = None


class ExceptionAssignRequest(BaseModel):
    assignee: str
    note: str = ""
    actor: str = "operator"


class ExceptionReopenRequest(BaseModel):
    note: str = ""
    actor: str = "operator"


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


class OutboundBulkCancelRequest(BaseModel):
    q: str | None = None
    status: str | None = None
    mail_type: str | None = None
    recipient: str | None = None
    ids: list[str] = []
    limit: int = Field(default=500, ge=1, le=5000)


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
    mail_rate_limit_interval_seconds: int | None = None
    ceo_email: str | None = None
    ops_cc_email: str | None = None
    zip_max_bytes: int | None = None
    zip_max_depth: int | None = None
    storage_budget_bytes: int | None = None
    non_target_retention_days: int | None = None
    bot_enabled: bool | None = None
    llm_fallback_enabled: bool | None = None
    conversation_max_rounds: int | None = None
    outbound_failed_alert_threshold: int | None = None
    outbound_pending_age_alert_seconds: int | None = None
    baidu_map_ak: str | None = None
    e2e_sales_email: str | None = None
    e2e_sales_password: str | None = None
    e2e_production_email: str | None = None
    e2e_production_password: str | None = None


class ErpRuntimeConfigUpdate(BaseModel):
    erp_enabled: bool | None = None
    erp_server_url: str | None = None
    erp_acct_id: str | None = None
    erp_username: str | None = None
    erp_app_id: str | None = None
    erp_app_sec: str | None = None
    erp_lcid: int | None = None
    erp_material_sync_enabled: bool | None = None
    erp_material_sync_interval_seconds: int | None = None
    erp_material_form_id: str | None = None
    erp_material_field_keys: str | None = None


class CrmRuntimeConfigUpdate(BaseModel):
    crm_sync_enabled: bool | None = None
    crm_username: str | None = None
    crm_password: str | None = None
    crm_api_key: str | None = None
    crm_sync_interval_seconds: int | None = None
    crm_cdp_url: str | None = None
    crm_fxiaoke_request_file: str | None = None
    crm_fxiaoke_request_json: str | None = None
    crm_fxiaoke_detail_request_file: str | None = None
    crm_fxiaoke_detail_request_json: str | None = None
    crm_sync_page_size: int | None = None
    crm_sync_timeout_seconds: int | None = None
    v2_crm_phase1_scope_enabled: bool | None = None
    v2_crm_phase1_scope_json: str | None = None


class OmsRuntimeConfigUpdate(BaseModel):
    oms_enabled: bool | None = None
    oms_mock_success: bool | None = None
    oms_auto_confirm_delivery_notice: bool | None = None
    oms_inventory_review_enabled: bool | None = None
    oms_inventory_missing_blocks: bool | None = None
    v2_validation_failure_notification_enabled: bool | None = None
    v2_oms_blocked_notification_enabled: bool | None = None
    v2_validation_failure_to_json: str | None = None
    v2_validation_failure_cc_json: str | None = None
    v2_oms_blocked_to_json: str | None = None
    v2_oms_blocked_cc_json: str | None = None
    oms_retry_base_delay_seconds: int | None = None
    oms_retry_multiplier: int | None = None
    oms_max_retries: int | None = None
    oms_jackyun_gateway_url: str | None = None
    oms_jackyun_app_key: str | None = None
    oms_jackyun_app_secret: str | None = None
    oms_jackyun_version: str | None = None
    oms_jackyun_content_type: str | None = None
    oms_jackyun_timeout_seconds: int | None = None
    oms_owner_code: str | None = None
    oms_warehouse_code: str | None = None
    oms_shop_code: str | None = None
    oms_logistic_code: str | None = None
    oms_order_type: str | None = None
    oms_create_order_method: str | None = None


class DeliveryNoticeConfirmRequest(BaseModel):
    confirmed_by: str = "operator"


class ErpBillQueryRequest(BaseModel):
    form_id: str
    field_keys: str
    filter_string: str = ""
    order_string: str = ""
    limit: int = Field(default=20, ge=1, le=200)
    start_row: int = Field(default=0, ge=0)


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


class WorkflowImportRequest(BaseModel):
    file_path: str | None = None
    file_name: str | None = None
    file_content_base64: str | None = None
    raw_text: str | None = None
    prefer_llm: bool = True
    auto_publish: bool = True


class WorkflowContactMapUpdate(BaseModel):
    mapping: dict[str, str | list[str]] = {}


class WorkflowVersionUpdateRequest(BaseModel):
    compiled_rules: dict[str, Any]
    activate: bool = False


class WorkflowSimulationRequest(BaseModel):
    from_address: str = "sales@jimuyida.com"
    subject: str
    body_text: str
    use_llm: bool = False


class WorkflowChatMessage(BaseModel):
    role: str
    content: str


class WorkflowChatGenerateRequest(BaseModel):
    messages: list[WorkflowChatMessage] = []
    current_rule: dict[str, Any] | None = None
    edit_version_id: str | None = None


class WorkflowChatSaveRequest(BaseModel):
    compiled_rule: dict[str, Any]
    activate: bool = False
    edit_version_id: str | None = None


# ==========================================
# Product Management Schemas
# ==========================================

class ProductSPUCreate(BaseModel):
    spu_id: str
    name: str
    brand: str | None = None
    category: str | None = None


class ProductSKUCreate(BaseModel):
    spu_uuid: str
    sku_id: str
    attributes: dict[str, Any] = {}


class ChannelPricingUpdate(BaseModel):
    sku_uuid: str
    channel: str = "default"
    tier_a_price: int | None = None
    tier_b_price: int | None = None
    tier_c_price: int | None = None
    map_price: int | None = None
    promo_start_time: datetime | None = None
    promo_end_time: datetime | None = None
    currency: str = "USD"


class PromotionRuleCreate(BaseModel):
    name: str
    sku_uuid: str
    channel: str | None = None
    discount_type: str = Field(pattern="^(percentage|fixed_amount|PERCENTAGE|FIXED_AMOUNT)$")
    discount_value: int
    priority: int = 0
    start_time: datetime | None = None
    end_time: datetime | None = None

class PromotionRuleUpdate(BaseModel):
    name: str | None = None
    sku_uuid: str | None = None
    channel: str | None = None
    discount_type: str | None = Field(None, pattern="^(percentage|fixed_amount|PERCENTAGE|FIXED_AMOUNT)$")
    discount_value: int | None = None
    priority: int | None = None
    start_time: datetime | None = None
    end_time: datetime | None = None
    is_active: bool | None = None
