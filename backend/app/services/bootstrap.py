from __future__ import annotations

from sqlalchemy.orm import Session

from backend.app.config import settings
from backend.app.models import LogisticsDepartment, MailTemplate, ModelProviderConfig, ProductionDepartment, SystemConfig, User, now_utc
from backend.app.services.jsonutil import dumps
from backend.app.services.auth import hash_password



LEGACY_DEFAULT_BAIDU_MAP_AK = "DC5abb7ad1c9c694af28f4732aa163c3"

DEFAULT_TASK_SUBJECT = "[生产任务单][{{task_no}}][{{customer_name}}][{{product_summary}}][V{{version_no}}]"
DEFAULT_TASK_BODY = """生产部同事好：

请根据以下信息安排生产评估和排产。

任务单编号：{{task_no}}
版本：V{{version_no}}
客户名称：{{customer_name}}
销售人员：{{salesperson_name}} <{{salesperson_email}}>

物料/规格：{{product_summary}}
数量：{{quantity_text}}
期望交期：{{expected_delivery_date}}

请确认是否可以安排生产。如信息不足，请直接回复本邮件说明疑问点。

{{bot_signature}}
"""

DEFAULT_LOGISTICS_SUBJECT = "[物流核查单][{{logistics_task_no}}][{{customer_name}}][{{external_order_no}}][V{{version_no}}]"
DEFAULT_LOGISTICS_BODY = """物流部同事好：

请核查以下订单是否可由现有仓储库存直接发货。

物流核查单编号：{{logistics_task_no}}
版本：V{{version_no}}
客户名称：{{customer_name}}
销售人员：{{salesperson_name}} <{{salesperson_email}}>
订单号：{{external_order_no}}

物料/规格：{{product_summary}}
数量：{{quantity_text}}
期望交期：{{expected_delivery_date}}

请按以下格式回复：
库存满足：是/否
可发物料：
缺失物料：
发货单号：
预计/实际发货时间：
备注：

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
    ensure_config(session, "mail_auto_worker_interval_seconds", str(settings.mail_auto_worker_interval_seconds))
    ensure_config(session, "mail_rate_limit_interval_seconds", str(settings.mail_rate_limit_interval_seconds))
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
    ensure_config(session, "v2_review_rule_states_json", "{}", is_secret=False)
    ensure_config(session, "bot_enabled", "true", is_secret=False)
    ensure_config(session, "llm_fallback_enabled", "true", is_secret=False)
    ensure_config(session, "crm_attachment_llm_allow_external_sensitive", "false", is_secret=False)
    ensure_config(session, "conversation_max_rounds", "3", is_secret=False)
    ensure_config(session, "outbound_failed_alert_threshold", "1", is_secret=False)
    ensure_config(session, "outbound_pending_age_alert_seconds", "3600", is_secret=False)
    ensure_config(session, "product_price_review_enabled", "true", is_secret=False)
    ensure_config(session, "product_price_review_require_unit_price", "false", is_secret=False)
    ensure_config(session, "product_price_review_llm_enabled", "false", is_secret=False)
    ensure_config(session, "workflow_contact_map_json", "{}", is_secret=False)
    ensure_config(session, "erp_enabled", "false", is_secret=False)
    ensure_config(session, "erp_readonly", "true", is_secret=False)
    ensure_config(session, "erp_write_enabled", "false", is_secret=False)
    ensure_config(session, "erp_server_url", "", is_secret=False)
    ensure_config(session, "erp_acct_id", "", is_secret=False)
    ensure_config(session, "erp_username", "", is_secret=False)
    ensure_config(session, "erp_app_id", "", is_secret=False)
    ensure_config(session, "erp_app_sec", "", is_secret=True)
    ensure_config(session, "erp_lcid", "2052", is_secret=False)
    ensure_config(session, "erp_material_sync_enabled", "true", is_secret=False)
    ensure_config(session, "erp_material_sync_interval_seconds", "86400", is_secret=False)
    ensure_config(session, "erp_material_form_id", "BD_MATERIAL", is_secret=False)
    ensure_config(session, "erp_material_field_keys", "FNumber,FName,FSpecification,FMaterialGroup.FName,FForbidStatus", is_secret=False)
    ensure_config(session, "erp_material_last_sync_at", "", is_secret=False)
    ensure_config(session, "erp_inventory_alert_threshold", "1", is_secret=False)
    ensure_config(session, "erp_inventory_last_sync_at", "", is_secret=False)
    ensure_config(session, "crm_sync_enabled", "false", is_secret=False)
    ensure_config(session, "crm_username", "", is_secret=False)
    ensure_config(session, "crm_password", "", is_secret=True)
    ensure_config(session, "crm_api_key", "", is_secret=True)
    ensure_config(session, "crm_system_owner_email", "", is_secret=False)
    ensure_config(session, "crm_sync_interval_seconds", "3600", is_secret=False)
    ensure_config(session, "crm_sync_min_order_date", "", is_secret=False)
    ensure_config(session, "crm_cdp_url", "http://127.0.0.1:9333", is_secret=False)
    ensure_config(session, "crm_cdp_browser_mode", "headless", is_secret=False)
    ensure_config(session, "crm_cdp_port", "9333", is_secret=False)
    ensure_config(session, "crm_cdp_user_data_dir", "/private/tmp/fxiaoke-cdp-profile-9333", is_secret=False)
    ensure_config(session, "crm_chrome_bin", "", is_secret=False)
    ensure_config(session, "crm_fxiaoke_request_file", "", is_secret=False)
    ensure_config(session, "crm_fxiaoke_request_json", "", is_secret=True)
    ensure_config(session, "crm_fxiaoke_detail_request_file", "", is_secret=False)
    ensure_config(session, "crm_fxiaoke_detail_request_json", "", is_secret=True)
    ensure_config(session, "crm_sync_page_size", "20", is_secret=False)
    ensure_config(session, "crm_sync_max_pages", "0", is_secret=False)
    ensure_config(session, "crm_sync_timeout_seconds", "120", is_secret=False)
    ensure_config(session, "crm_sync_max_retries", "3", is_secret=False)
    ensure_config(session, "crm_sync_detail_concurrency", "3", is_secret=False)
    ensure_config(session, "crm_sales_orders_last_sync_at", "", is_secret=False)
    ensure_config(session, "v2_crm_phase1_scope_enabled", "true", is_secret=False)
    ensure_config(session, "v2_crm_phase1_scope_json", dumps({
        "approved_values": ["approved", "审批通过", "已审批", "已通过", "complete", "completed", "passed"],
        "approved_life_status_values": ["normal", "正常", "active"],
        "cancelled_values": ["cancelled", "canceled", "撤销", "已撤销", "作废", "已作废", "取消", "已取消"],
        "include_owner_departments": [],
        "include_settlement_methods": [],
        "include_customer_names": [],
    }), is_secret=False)
    ensure_config(session, "oms_enabled", "false", is_secret=False)
    ensure_config(session, "oms_mock_success", "true", is_secret=False)
    ensure_config(session, "oms_auto_confirm_delivery_notice", "false", is_secret=False)
    ensure_config(session, "oms_inventory_review_enabled", "true", is_secret=False)
    upsert_config(session, "oms_inventory_missing_blocks", "true", is_secret=False)
    ensure_config(session, "oms_material_sync_enabled", "true", is_secret=False)
    ensure_config(session, "oms_material_sync_interval_seconds", "86400", is_secret=False)
    ensure_config(session, "oms_material_last_sync_at", "", is_secret=False)
    ensure_config(session, "v2_review_crm_approved_values", dumps(["approved", "审批通过", "已审批", "已通过", "complete", "completed", "passed"]), is_secret=False)
    ensure_config(session, "v2_review_require_key_attachment", "true", is_secret=False)
    ensure_config(session, "v2_review_customer_mapping_required", "true", is_secret=False)
    ensure_config(session, "v2_customer_mapping_json", dumps({
        "亚马逊北美渠道": {"customer_code": "CUST-AMAZON-NA", "customer_name": "亚马逊北美渠道"},
        "库存不足客户": {"customer_code": "CUST-STOCK-CHECK", "customer_name": "库存不足客户"},
        "字段缺失客户": {"customer_code": "CUST-FIELD-CHECK", "customer_name": "字段缺失客户"},
        "缺 SKU 客户": {"customer_code": "CUST-SKU-CHECK", "customer_name": "缺 SKU 客户"},
    }), is_secret=False)
    ensure_config(session, "v2_validation_failure_notification_enabled", "true", is_secret=False)
    ensure_config(session, "v2_oms_blocked_notification_enabled", "true", is_secret=False)
    ensure_config(session, "v2_validation_failure_to_json", "[]", is_secret=False)
    ensure_config(session, "v2_validation_failure_cc_json", "[]", is_secret=False)
    ensure_config(session, "oms_retry_base_delay_seconds", "60", is_secret=False)
    ensure_config(session, "oms_retry_multiplier", "3", is_secret=False)
    ensure_config(session, "oms_max_retries", "3", is_secret=False)
    ensure_config(session, "oms_jackyun_gateway_url", "https://open.jackyun.com/open/openapi/do", is_secret=False)
    ensure_config(session, "oms_jackyun_app_key", "", is_secret=False)
    ensure_config(session, "oms_jackyun_app_secret", "", is_secret=True)
    ensure_config(session, "oms_admin_email", "", is_secret=False)
    ensure_config(session, "oms_customer_query_method", "crm.customer.list.customized,crm.customer.list", is_secret=False)
    ensure_config(session, "oms_customer_query_payload_json", "{}", is_secret=False)
    ensure_config(session, "oms_jackyun_version", "1.0", is_secret=False)
    ensure_config(session, "oms_jackyun_content_type", "json", is_secret=False)
    ensure_config(session, "oms_jackyun_timeout_seconds", "20", is_secret=False)
    ensure_config(session, "oms_owner_code", "", is_secret=False)
    ensure_config(session, "oms_warehouse_code", "", is_secret=False)
    ensure_config(session, "oms_shop_code", "", is_secret=False)
    ensure_config(session, "oms_logistic_code", "", is_secret=False)
    ensure_config(session, "oms_order_type", "201", is_secret=False)
    ensure_config(session, "oms_create_order_method", "wms.order.create", is_secret=False)
    if settings.baidu_map_ak:
        map_config = session.get(SystemConfig, "baidu_map_ak")
        if map_config is None or not (map_config.value or "").strip() or map_config.value == LEGACY_DEFAULT_BAIDU_MAP_AK:
            set_config(session, "baidu_map_ak", settings.baidu_map_ak, is_secret=True)
        elif not map_config.is_secret:
            map_config.is_secret = True
    else:
        ensure_config(session, "baidu_map_ak", "", is_secret=True)

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

    logistics_template = (
        session.query(MailTemplate)
        .filter(MailTemplate.template_code == "logistics_task", MailTemplate.version == "v1")
        .one_or_none()
    )
    if logistics_template is None:
        session.add(
            MailTemplate(
                template_code="logistics_task",
                template_name="物流核查单默认模板",
                template_type="LogisticsTaskIssue",
                subject_template=DEFAULT_LOGISTICS_SUBJECT,
                body_template=DEFAULT_LOGISTICS_BODY,
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

    default_logistics_department = session.query(LogisticsDepartment).filter_by(department_code="default").one_or_none()
    if default_logistics_department is None:
        session.add(
            LogisticsDepartment(
                department_code="default",
                department_name="默认物流部门",
                mail_to_json=dumps([]),
                mail_cc_json=dumps([]),
                status="Active",
            )
        )

    # Seed default RBAC users
    if session.query(User).count() == 0:
        session.add(User(
            username=settings.admin_username,
            password_hash=hash_password(settings.admin_password),
            role="admin",
            department="IT"
        ))
        session.add(User(
            username="owner",
            password_hash=hash_password("owner123"),
            role="business_owner",
            department="Business"
        ))
        session.add(User(
            username="operator",
            password_hash=hash_password("operator123"),
            role="business_operator",
            department="Sales"
        ))
        session.add(User(
            username="operator_other",
            password_hash=hash_password("operator123"),
            role="business_operator",
            department="Logistics"
        ))
        session.add(User(
            username="auditor",
            password_hash=hash_password("auditor123"),
            role="auditor",
            department="Finance"
        ))
        session.add(User(
            username="it_ops",
            password_hash=hash_password("itops123"),
            role="it_ops",
            department="Logistics"
        ))
