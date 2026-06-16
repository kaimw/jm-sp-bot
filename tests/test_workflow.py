from __future__ import annotations

import io
import json
import zipfile
from contextlib import nullcontext
from datetime import datetime, timedelta, timezone
from email.message import EmailMessage

import httpx
import pytest
from docx import Document
from openpyxl import Workbook
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from backend.app.database import Base
from backend.app.models import (
    AuditEvent,
    AttachmentAsset,
    BackupJob,
    ExceptionCase,
    ExtractionEvidence,
    FulfillmentItem,
    LogisticsDepartment,
    LogisticsTask,
    LogisticsTaskVersion,
    MailMessage,
    MailWorkflowMatch,
    MaintenanceAction,
    MaintenanceSession,
    ModelProviderConfig,
    OrderRequirement,
    OutboundMailJob,
    ProcessingJob,
    ProductionDepartment,
    ProductInventorySnapshot,
    PromotionRule,
    ProductSPU,
    ProductSKU,
    ProductionTask,
    ProductionTaskVersion,
    QuestionAndReply,
    RequirementWorkflowBinding,
    SystemConfig,
    WorkflowDefinition,
    WorkflowImportJob,
    WorkflowVersion,
)
from backend.app.services.attachment_parser import parse_attachment
from backend.app.services.auth import create_session_token, parse_session_token
from backend.app.services.bootstrap import seed_defaults, set_config
from backend.app.services.jobs import run_pending_jobs
from backend.app.services.jsonutil import dumps, loads, as_list
from backend.app.services.mail_adapter import (
    parse_email_bytes,
    save_and_parse_attachment,
    send_outbound_jobs_smtp,
    send_pending_auto_workflow_mails_smtp,
    send_pending_receipt_acks_smtp,
    send_pending_smtp,
    sync_imap_mailbox,
    store_incoming_email,
)
from backend.app.services.mail_throttle import clamp_mail_interval_seconds, reset_mail_login_throttle
from backend.app.services.mail_worker import run_mail_auto_worker_once
from backend.app.services.initial_review import initial_review_config, remember_deleted_workflow_review_rules
from backend.app.services.model_provider import build_openai_chat_payload, call_model, extract_chat_content, resolve_api_key
from backend.app.services.operations import cleanup_preview, execute_cleanup, weekly_report_csv
from backend.app.services.pdf import simple_pdf
from backend.app.services.products import create_promotion_rule, create_spu, create_sku, extract_order_products_from_text, get_promotions, parse_price_to_cents, review_order_products, set_channel_pricing, update_spu_review_aliases
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
)
from backend.app.services.workflow import (
    apply_exception_requirement_patch,
    approve_task,
    create_inbound_mail,
    create_task_from_mail,
    dashboard,
    enqueue_weekly_report,
    force_close_task_manual,
    process_inbound_mail,
    record_exception_case,
    record_production_feedback,
    record_production_question,
    retry_outbound_mail,
    set_weekly_report_recipients,
    weekly_report_recipients,
)
from backend.app.services.task_scheduler import RetryPolicy, TaskScheduler
from backend.app.services.workflow_rules import (
    activate_workflow_version,
    deactivate_workflow_version,
    delete_workflow_version,
    chat_generate_workflow_rule,
    import_structured_workflow_rules,
    import_workflow_document,
    list_workflow_rules,
    match_workflow_for_mail,
    rollback_workflow_version,
    save_workflow_version_rules,
    workflow_binding_for_requirement,
    workflow_version_diff,
)
from backend.app.main import assign_exception, diagnose_exception_stream_chunks, global_exception_ticker_items, reopen_exception, resolve_exception, serialize_exception
from backend.app.schemas import ExceptionAssignRequest, ExceptionReopenRequest, ExceptionResolveRequest
from backend.app.models import now_utc
from backend.app.main import self_maintenance_action_detail
from scripts.maintenance_runner import CommandResult, create_handoff_package, validate_code_plan_action


@pytest.fixture(autouse=True)
def reset_mail_throttle_between_tests():
    reset_mail_login_throttle()


def make_session():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(bind=engine)
    Session = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)
    session = Session()
    seed_defaults(session)
    session.commit()
    return session


def reset_db_mail_throttle(session):
    session.query(SystemConfig).filter(SystemConfig.key.like("mail_throttle.%")).delete(synchronize_session=False)
    session.commit()


def configure_department(session):
    department = session.query(ProductionDepartment).filter_by(department_code="default").one()
    department.mail_to_json = dumps(["production@jimuyida.com"])
    session.commit()


def configure_logistics_department(session):
    department = session.query(LogisticsDepartment).filter_by(department_code="default").one()
    department.mail_to_json = dumps(["logistics@jimuyida.com"])
    session.commit()


def configure_crm_oms_access(session):
    set_config(session, "crm_sync_enabled", "true", is_secret=False)
    set_config(session, "crm_username", "crm-user", is_secret=False)
    set_config(session, "crm_password", "crm-secret", is_secret=True)
    set_config(session, "crm_system_owner_email", "crm-owner@example.com", is_secret=False)
    set_config(session, "oms_enabled", "true", is_secret=False)
    set_config(session, "oms_mock_success", "false", is_secret=False)
    set_config(session, "oms_admin_email", "oms-admin@example.com", is_secret=False)
    set_config(session, "oms_jackyun_app_key", "oms-app-key", is_secret=False)
    set_config(session, "oms_jackyun_app_secret", "oms-app-secret", is_secret=True)
    session.commit()


def create_valid_task(session, order_no="SO-001"):
    mail = create_inbound_mail(
        session,
        from_address="sales@jimuyida.com",
        subject=f"生产订单需求 - 测试客户 - {order_no}",
        body_text="\n".join(
            [
                "客户名称：测试客户",
                "产品：积木展示架 A1",
                "数量：120 套",
                "期望交期：2026-05-20",
                f"订单号：{order_no}",
            ]
        ),
    )
    task = create_task_from_mail(session, mail)
    session.commit()
    assert task is not None
    return task


def test_seed_defaults_omit_plaintext_secrets():
    session = make_session()
    assert session.get(SystemConfig, "bot_email").value == "bot.market@jimuyida.com"
    assert session.get(SystemConfig, "mail_auto_worker_interval_seconds").value == "60"
    assert session.get(SystemConfig, "mail_rate_limit_interval_seconds").value == "60"
    assert session.get(SystemConfig, "bot_enabled").value == "true"
    assert session.get(SystemConfig, "outbound_failed_alert_threshold").value == "1"
    assert session.get(SystemConfig, "outbound_pending_age_alert_seconds").value == "3600"
    model = session.query(ModelProviderConfig).one()
    assert model.title == "Dify deepseekV3"
    assert model.credential_ref == "env:MODEL_API_KEY"


def test_baidu_map_ak_is_treated_as_secret_config():
    from backend.app.main import config, update_mail_config
    from backend.app.schemas import MailRuntimeConfigUpdate

    session = make_session()

    map_config = session.get(SystemConfig, "baidu_map_ak")
    assert map_config.is_secret is True
    assert config(session)["configs"]["baidu_map_ak"] == "***"

    update_mail_config(MailRuntimeConfigUpdate(baidu_map_ak="new-baidu-ak"), session)

    map_config = session.get(SystemConfig, "baidu_map_ak")
    assert map_config.value == "new-baidu-ak"
    assert map_config.is_secret is True
    assert config(session)["configs"]["baidu_map_ak"] == "***"


def test_erp_config_masks_app_secret_and_normalizes_server_url():
    from backend.app.main import config, update_erp_config
    from backend.app.schemas import ErpRuntimeConfigUpdate

    session = make_session()

    update_erp_config(
        ErpRuntimeConfigUpdate(
            erp_enabled=True,
            erp_server_url="http://erp.local/K3Cloud/html5/index.aspx?ud=test",
            erp_acct_id="test-db",
            erp_username="erp-user",
            erp_app_id="app-id",
            erp_app_sec="app-secret",
            erp_lcid=2052,
        ),
        session,
    )

    assert session.get(SystemConfig, "erp_server_url").value == "http://erp.local/K3Cloud/"
    assert session.get(SystemConfig, "erp_app_sec").value == "app-secret"
    assert session.get(SystemConfig, "erp_app_sec").is_secret is True
    assert session.get(SystemConfig, "erp_write_enabled").value == "false"
    assert config(session)["configs"]["erp_app_sec"] == "***"


def test_kingdee_connection_test_uses_login_by_app_secret_and_sanitizes_result(monkeypatch):
    from backend.app.main import test_erp_connection, update_erp_config
    from backend.app.schemas import ErpRuntimeConfigUpdate

    captured = {}

    class FakeResponse:
        status_code = 200

        def raise_for_status(self):
            return None

        def json(self):
            return {
                "LoginResultType": 1,
                "KDSVCSessionId": "session-id",
                "Context": {"UserName": "erp-user", "UserId": 1001, "DataCenterName": "测试账套"},
            }

    class FakeClient:
        def __init__(self, timeout):
            self.timeout = timeout

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def post(self, url, json):
            captured["url"] = url
            captured["json"] = json
            return FakeResponse()

    monkeypatch.setattr("backend.app.services.erp.kingdee_client.httpx.Client", FakeClient)
    session = make_session()
    update_erp_config(
        ErpRuntimeConfigUpdate(
            erp_server_url="http://erp.local/k3cloud/",
            erp_acct_id="test-db",
            erp_username="erp-user",
            erp_app_id="app-id",
            erp_app_sec="app-secret",
            erp_lcid=2052,
        ),
        session,
    )

    result = test_erp_connection(session)

    assert result["ok"] is True
    assert result["message"] == "连接成功"
    assert result["context"]["user_name"] == "erp-user"
    assert captured["url"] == "http://erp.local/k3cloud/Kingdee.BOS.WebApi.ServicesStub.AuthService.LoginByAppSecret.common.kdsvc"
    assert captured["json"]["parameters"] == ["test-db", "erp-user", "app-id", "app-secret", 2052]
    assert "app-secret" not in str(result)


def test_kingdee_readonly_query_logs_in_then_calls_execute_bill_query(monkeypatch):
    from backend.app.main import query_erp_bill, update_erp_config
    from backend.app.schemas import ErpBillQueryRequest, ErpRuntimeConfigUpdate

    calls = []

    class FakeResponse:
        def __init__(self, payload):
            self.payload = payload
            self.status_code = 200

        def raise_for_status(self):
            return None

        def json(self):
            return self.payload

    class FakeClient:
        def __init__(self, timeout):
            self.timeout = timeout

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def post(self, url, json):
            calls.append({"url": url, "json": json})
            if "LoginByAppSecret" in url:
                return FakeResponse({"LoginResultType": 1, "KDSVCSessionId": "session-id"})
            return FakeResponse([["MAT-001", "测试物料"]])

    monkeypatch.setattr("backend.app.services.erp.kingdee_client.httpx.Client", FakeClient)
    session = make_session()
    update_erp_config(
        ErpRuntimeConfigUpdate(
            erp_server_url="http://erp.local/k3cloud/",
            erp_acct_id="test-db",
            erp_username="erp-user",
            erp_app_id="app-id",
            erp_app_sec="app-secret",
            erp_lcid=2052,
        ),
        session,
    )

    result = query_erp_bill(
        ErpBillQueryRequest(form_id="BD_MATERIAL", field_keys="FNumber,FName", filter_string="FNumber='MAT-001'", limit=10),
        session,
    )

    assert result["ok"] is True
    assert result["items"] == [["MAT-001", "测试物料"]]
    assert calls[1]["url"] == "http://erp.local/k3cloud/Kingdee.BOS.WebApi.ServicesStub.DynamicFormService.ExecuteBillQuery.common.kdsvc"
    assert calls[1]["json"]["parameters"][0]["FormId"] == "BD_MATERIAL"
    assert calls[1]["json"]["parameters"][0]["FieldKeys"] == "FNumber,FName"
    assert calls[1]["json"]["parameters"][0]["FilterString"] == "FNumber='MAT-001'"
    assert calls[1]["json"]["parameters"][0]["Limit"] == 10


def test_kingdee_query_detects_embedded_error_list():
    from backend.app.services.erp.kingdee_client import is_query_success, normalize_query_rows, query_message

    payload = [
        [
            {
                "Result": {
                    "ResponseStatus": {
                        "IsSuccess": False,
                        "Errors": [{"Message": "标识为“BD_MATERIALGROUP”的业务对象不存在"}],
                    }
                }
            }
        ]
    ]

    assert is_query_success(payload) is False
    assert normalize_query_rows(payload) == []
    assert "业务对象不存在" in query_message(payload)


def test_erp_material_sync_upserts_product_center_spu_and_sku(monkeypatch):
    from backend.app.main import sync_products_from_erp, update_erp_config
    from backend.app.models import ProductSKU, ProductSPU
    from backend.app.schemas import ErpRuntimeConfigUpdate

    def fake_query(config, **kwargs):
        if kwargs["start_row"] > 0:
            return {"ok": True, "items": [], "elapsed_ms": 1}
        return {
            "ok": True,
            "items": [["MAT-001", "测试物料", "规格A", "测试分类", "A"]],
            "elapsed_ms": 1,
        }

    monkeypatch.setattr("backend.app.services.erp.material_sync.execute_bill_query_with_config", fake_query)
    session = make_session()
    update_erp_config(
        ErpRuntimeConfigUpdate(
            erp_enabled=True,
            erp_server_url="http://erp.local/k3cloud/",
            erp_acct_id="test-db",
            erp_username="erp-user",
            erp_app_id="app-id",
            erp_app_sec="app-secret",
            erp_lcid=2052,
            erp_material_field_keys="FNumber,FName,FSpecification,FMaterialGroup.FName,FForbidStatus",
        ),
        session,
    )

    result = sync_products_from_erp(session)

    spu = session.query(ProductSPU).filter_by(spu_id="MAT-001").one()
    sku = session.query(ProductSKU).filter_by(sku_id="MAT-001").one()
    assert result["ok"] is True
    assert result["total"] == 1
    assert result["created_spu"] == 1
    assert result["created_sku"] == 1
    assert spu.name == "测试物料"
    assert spu.category == "测试分类"
    assert sku.spu_uuid == spu.id
    assert loads(sku.attributes_json, {})["erp_specification"] == "规格A"
    assert session.get(SystemConfig, "erp_material_last_sync_at").value


def test_erp_material_sync_skips_duplicate_material_numbers_in_same_batch(monkeypatch):
    from backend.app.main import sync_products_from_erp, update_erp_config
    from backend.app.models import ProductSKU
    from backend.app.schemas import ErpRuntimeConfigUpdate

    def fake_query(config, **kwargs):
        return {
            "ok": True,
            "items": [["MAT-DUP", "重复物料"], ["MAT-DUP", "重复物料"]],
            "elapsed_ms": 1,
        }

    monkeypatch.setattr("backend.app.services.erp.material_sync.execute_bill_query_with_config", fake_query)
    session = make_session()
    update_erp_config(
        ErpRuntimeConfigUpdate(
            erp_enabled=True,
            erp_server_url="http://erp.local/k3cloud/",
            erp_acct_id="test-db",
            erp_username="erp-user",
            erp_app_id="app-id",
            erp_app_sec="app-secret",
            erp_material_field_keys="FNumber,FName",
        ),
        session,
    )

    result = sync_products_from_erp(session)

    assert result["total"] == 1
    assert result["skipped_duplicates"] == 1
    assert session.query(ProductSKU).filter_by(sku_id="MAT-DUP").count() == 1


def test_processing_job_runs_erp_material_sync(monkeypatch):
    from backend.app.models import ProductSPU
    from backend.app.schemas import ErpRuntimeConfigUpdate
    from backend.app.main import update_erp_config

    def fake_query(config, **kwargs):
        return {"ok": True, "items": [["MAT-JOB", "队列物料"]], "elapsed_ms": 1}

    monkeypatch.setattr("backend.app.services.erp.material_sync.execute_bill_query_with_config", fake_query)
    session = make_session()
    update_erp_config(
        ErpRuntimeConfigUpdate(
            erp_enabled=True,
            erp_server_url="http://erp.local/k3cloud/",
            erp_acct_id="test-db",
            erp_username="erp-user",
            erp_app_id="app-id",
            erp_app_sec="app-secret",
            erp_material_field_keys="FNumber,FName",
        ),
        session,
    )
    job = ProcessingJob(job_type="sync_erp_materials", payload_json=dumps({"source": "test"}), status="Pending")
    session.add(job)
    session.commit()

    result = run_pending_jobs(session)
    session.refresh(job)

    assert result["completed"] == 1
    assert job.version == 2
    assert session.query(ProductSPU).filter_by(spu_id="MAT-JOB").one().name == "队列物料"
    assert session.query(ProcessingJob).filter_by(job_type="sync_erp_materials", status="Completed").count() == 1


def test_business_material_search_reads_synced_product_center():
    from backend.app.services.erp.business_queries import search_materials

    session = make_session()
    spu = create_spu(session, spu_id="MAT-SEARCH", name="查询物料", category="同步分类")
    session.flush()
    session.add(ProductSKU(spu_uuid=spu.id, sku_id="MAT-SEARCH", attributes_json=dumps({"erp_specification": "规格S"})))
    session.commit()

    result = search_materials(session, q="SEARCH", limit=10)

    assert result["ok"] is True
    assert result["items"][0]["material_code"] == "MAT-SEARCH"
    assert result["items"][0]["material_name"] == "查询物料"
    assert result["items"][0]["specification"] == "规格S"


def test_business_inventory_query_maps_erp_rows(monkeypatch):
    from backend.app.services.erp.business_queries import query_inventory

    captured = {}

    def fake_query(session, **kwargs):
        captured.update(kwargs)
        return {
            "ok": True,
            "message": "查询成功",
            "elapsed_ms": 12,
            "items": [["MAT-INV", "库存物料", "WH01", "测试仓", 5, 3]],
        }

    monkeypatch.setattr("backend.app.services.erp.business_queries.execute_bill_query_from_config", fake_query)
    session = make_session()

    result = query_inventory(session, material_code="MAT-INV", warehouse_code="WH01", limit=10)

    assert result["ok"] is True
    assert result["items"][0]["material_code"] == "MAT-INV"
    assert result["items"][0]["warehouse_code"] == "WH01"
    assert result["items"][0]["base_qty"] == 5.0
    assert captured["form_id"] == "STK_Inventory"
    assert "FMaterialId.FNumber = 'MAT-INV'" in captured["filter_string"]
    assert "FStockId.FNumber = 'WH01'" in captured["filter_string"]


def test_inventory_snapshot_sync_aggregates_and_lists_alerts(monkeypatch):
    from backend.app.services.erp.business_queries import list_inventory_snapshots, sync_inventory_snapshots
    from backend.app.schemas import ErpRuntimeConfigUpdate
    from backend.app.main import update_erp_config

    def fake_query(config, **kwargs):
        return {
            "ok": True,
            "items": [
                ["MAT-STOCK", "库存物料", "WH01", "主仓", 1, 0],
                ["MAT-STOCK", "库存物料", "WH01", "主仓", 2, 0],
                ["MAT-ZERO", "零库存物料", "WH02", "备仓", 0, 0],
            ],
        }

    monkeypatch.setattr("backend.app.services.erp.business_queries.execute_bill_query_with_config", fake_query)
    session = make_session()
    session.add_all(
        [
            ProductSPU(spu_id="MAT-STOCK", name="库存物料", category="成品"),
            ProductSPU(spu_id="MAT-ZERO", name="零库存物料", category="成品"),
        ]
    )
    session.flush()
    update_erp_config(
        ErpRuntimeConfigUpdate(
            erp_enabled=True,
            erp_server_url="http://erp.local/k3cloud/",
            erp_acct_id="test-db",
            erp_username="erp-user",
            erp_app_id="app-id",
            erp_app_sec="app-secret",
        ),
        session,
    )

    result = sync_inventory_snapshots(session)
    listed = list_inventory_snapshots(session, low_stock_only=True, threshold=1, page=1, page_size=10)

    assert result["ok"] is True
    assert result["total"] == 2
    assert session.query(ProductInventorySnapshot).filter_by(material_code="MAT-STOCK", warehouse_code="WH01").one().base_qty == 3
    assert listed["summary"]["zero_stock_count"] == 1
    assert listed["summary"]["low_stock_count"] == 1
    assert listed["items"][0]["material_code"] == "MAT-ZERO"
    assert listed["items"][0]["alert_level"] == "zero"


def test_inventory_summary_excludes_non_countable_materials_by_default():
    from backend.app.services.erp.business_queries import list_inventory_snapshots

    session = make_session()
    countable = ProductSPU(spu_id="COUNT-001", name="计数物料", category="成品")
    consumable = ProductSPU(spu_id="CONS-001", name="耗材物料", category="工具耗材")
    session.add_all([countable, consumable])
    session.flush()
    session.add_all(
        [
            ProductInventorySnapshot(material_code="COUNT-001", material_name="计数物料", warehouse_code="WH", warehouse_name="主仓", base_qty=2, qty=2),
            ProductInventorySnapshot(material_code="CONS-001", material_name="耗材物料", warehouse_code="WH", warehouse_name="主仓", base_qty=100, qty=100),
        ]
    )
    session.commit()

    default_result = list_inventory_snapshots(session, page=1, page_size=10)
    all_result = list_inventory_snapshots(session, countable_only=False, page=1, page_size=10)

    assert default_result["summary"]["total_rows"] == 1
    assert default_result["summary"]["total_base_qty"] == 2
    assert {item["material_code"] for item in default_result["items"]} == {"COUNT-001"}
    assert all_result["summary"]["total_rows"] == 2
    assert all_result["summary"]["total_base_qty"] == 102


def test_inventory_type_summary_groups_by_material_middle_type():
    from backend.app.services.erp.business_queries import list_inventory_type_summary

    session = make_session()
    session.add_all(
        [
            ProductSPU(spu_id="A-001", name="A1", category="成品", product_type="扫描仪"),
            ProductSPU(spu_id="A-002", name="A2", category="成品", product_type="扫描仪"),
            ProductSPU(spu_id="B-001", name="B1", category="结构件", product_type="支架"),
        ]
    )
    session.flush()
    session.add_all(
        [
            ProductInventorySnapshot(material_code="A-001", material_name="A1", warehouse_code="WH1", warehouse_name="主仓", base_qty=2, qty=2),
            ProductInventorySnapshot(material_code="A-002", material_name="A2", warehouse_code="WH1", warehouse_name="主仓", base_qty=3, qty=3),
            ProductInventorySnapshot(material_code="B-001", material_name="B1", warehouse_code="WH2", warehouse_name="备仓", base_qty=0, qty=0),
        ]
    )
    session.commit()

    result = list_inventory_type_summary(session, countable_only=True, page=1, page_size=10)
    by_type = {item["material_type"]: item for item in result["items"]}

    assert result["total"] == 2
    assert by_type["扫描仪"]["parent_category"] == "成品"
    assert by_type["扫描仪"]["material_count"] == 2
    assert by_type["扫描仪"]["base_qty"] == 5
    assert by_type["支架"]["alert_level"] == "zero"


def test_inventory_type_summary_derives_middle_type_from_material_name():
    from backend.app.services.erp.business_queries import list_inventory_type_summary

    session = make_session()
    session.add_all(
        [
            ProductSPU(spu_id="P-001", name="CR-scan上壳", category="塑胶件"),
            ProductSPU(spu_id="P-002", name="CR-scan下壳", category="塑胶件"),
            ProductSPU(spu_id="S-001", name="Seal双轴转台-底座", category="塑胶件"),
        ]
    )
    session.flush()
    session.add_all(
        [
            ProductInventorySnapshot(material_code="P-001", material_name="CR-scan上壳", warehouse_code="WH1", warehouse_name="主仓", base_qty=2, qty=2),
            ProductInventorySnapshot(material_code="P-002", material_name="CR-scan下壳", warehouse_code="WH1", warehouse_name="主仓", base_qty=3, qty=3),
            ProductInventorySnapshot(material_code="S-001", material_name="Seal双轴转台-底座", warehouse_code="WH1", warehouse_name="主仓", base_qty=4, qty=4),
        ]
    )
    session.commit()

    result = list_inventory_type_summary(session, countable_only=True, page=1, page_size=10)
    by_type = {item["material_type"]: item for item in result["items"]}

    assert by_type["CR-scan"]["material_count"] == 2
    assert by_type["Seal双轴转台"]["material_count"] == 1


def test_inventory_type_items_lists_material_warehouse_details():
    from backend.app.services.erp.business_queries import list_inventory_type_items

    session = make_session()
    session.add_all(
        [
            ProductSPU(spu_id="P-001", name="CR-scan上壳", category="塑胶件"),
            ProductSPU(spu_id="P-002", name="CR-scan下壳", category="塑胶件"),
            ProductSPU(spu_id="S-001", name="Seal双轴转台-底座", category="塑胶件"),
        ]
    )
    session.flush()
    session.add_all(
        [
            ProductInventorySnapshot(material_code="P-001", material_name="CR-scan上壳", warehouse_code="WH1", warehouse_name="主仓", base_qty=2, qty=2),
            ProductInventorySnapshot(material_code="P-002", material_name="CR-scan下壳", warehouse_code="WH2", warehouse_name="备仓", base_qty=0, qty=0),
            ProductInventorySnapshot(material_code="S-001", material_name="Seal双轴转台-底座", warehouse_code="WH1", warehouse_name="主仓", base_qty=4, qty=4),
        ]
    )
    session.commit()

    result = list_inventory_type_items(session, material_type="CR-scan", parent_category="塑胶件", threshold=1, page=1, page_size=10)

    assert result["total"] == 2
    assert result["summary"]["material_count"] == 2
    assert result["summary"]["warehouse_count"] == 2
    assert result["summary"]["base_qty"] == 2
    assert result["summary"]["zero_stock_count"] == 1
    assert [item["material_code"] for item in result["items"]] == ["P-001", "P-002"]
    assert {item["warehouse_name"] for item in result["items"]} == {"主仓", "备仓"}

    searched = list_inventory_type_items(session, material_type="CR-scan", parent_category="塑胶件", q="备仓", page=1, page_size=10)
    assert searched["total"] == 1
    assert searched["items"][0]["material_code"] == "P-002"

    zero_only = list_inventory_type_items(session, material_type="CR-scan", parent_category="塑胶件", stock_status="zero", page=1, page_size=10)
    assert zero_only["total"] == 1
    assert zero_only["summary"]["zero_stock_count"] == 1


def test_system_enable_requires_model_bot_and_department_config():
    from backend.app.main import config, update_mail_config
    from backend.app.schemas import MailRuntimeConfigUpdate

    session = make_session()

    readiness = config(session)["startup_readiness"]
    assert readiness["ready"] is False
    assert "Dify API Key" in readiness["missing"]
    assert "bot邮箱密码" in readiness["missing"]
    assert "生产部门主送邮箱" in readiness["missing"]
    assert "CRM同步未启用" in readiness["missing"]
    assert "CRM账号密码或API Key" in readiness["missing"]
    assert "CRM系统负责人邮箱" in readiness["missing"]
    assert "OMS接入未启用" in readiness["missing"]
    assert "OMS真实下推未启用" in readiness["missing"]
    assert "OMS管理员邮箱" in readiness["missing"]
    assert "OMS AppKey" in readiness["missing"]
    assert "OMS AppSecret" in readiness["missing"]

    with pytest.raises(Exception) as exc:
        update_mail_config(MailRuntimeConfigUpdate(bot_enabled=True), session)

    assert exc.value.status_code == 400
    assert "Dify API Key" in exc.value.detail
    assert "bot邮箱密码" in exc.value.detail
    assert "生产部门主送邮箱" in exc.value.detail
    assert "CRM同步未启用" in exc.value.detail
    assert "CRM系统负责人邮箱" in exc.value.detail
    assert "OMS接入未启用" in exc.value.detail
    assert session.get(SystemConfig, "bot_enabled").value == "false"

    configure_department(session)
    configure_crm_oms_access(session)
    model = session.query(ModelProviderConfig).one()
    set_config(session, "model_api_key", "runtime-secret", is_secret=True)
    model.credential_ref = "config:model_api_key"
    configure_crm_oms_access(session)
    session.commit()

    result = update_mail_config(MailRuntimeConfigUpdate(bot_email_password="mail-secret", bot_enabled=True), session)

    assert result["startup_readiness"]["ready"] is True
    assert session.get(SystemConfig, "bot_enabled").value == "True"


def test_system_enable_rejects_invalid_department_main_email():
    from backend.app.main import runtime_startup_readiness, update_mail_config
    from backend.app.schemas import MailRuntimeConfigUpdate

    session = make_session()
    department = session.query(ProductionDepartment).filter_by(department_code="default").one()
    department.mail_to_json = dumps(["销售直属领导"])
    model = session.query(ModelProviderConfig).one()
    set_config(session, "model_api_key", "runtime-secret", is_secret=True)
    model.credential_ref = "config:model_api_key"
    session.commit()

    readiness = runtime_startup_readiness(session, {"bot_email_password": "mail-secret"})
    assert readiness["ready"] is False
    assert any("生产部门主送邮箱格式不合法" in item for item in readiness["missing"])

    with pytest.raises(Exception) as exc:
        update_mail_config(MailRuntimeConfigUpdate(bot_email_password="mail-secret", bot_enabled=True), session)

    assert exc.value.status_code == 400
    assert "生产部门主送邮箱格式不合法" in exc.value.detail
    assert "销售直属领导" in exc.value.detail
    assert session.get(SystemConfig, "bot_enabled").value == "false"


def test_department_upsert_rejects_invalid_main_email():
    from backend.app.main import upsert_default_department
    from backend.app.schemas import DepartmentUpsert

    session = make_session()

    with pytest.raises(Exception) as exc:
        upsert_default_department(DepartmentUpsert(mail_to=["sales-direct-leader"], mail_cc=[]), session)

    assert exc.value.status_code == 400
    assert "主送邮箱格式不合法" in exc.value.detail


def test_delete_department_hides_it_and_disables_system_when_last_recipient():
    from backend.app.main import delete_department, list_departments, update_mail_config
    from backend.app.schemas import MailRuntimeConfigUpdate

    class Request:
        class State:
            username = "admin"

        state = State()

    session = make_session()
    configure_department(session)
    model = session.query(ModelProviderConfig).one()
    set_config(session, "model_api_key", "runtime-secret", is_secret=True)
    model.credential_ref = "config:model_api_key"
    configure_crm_oms_access(session)
    session.commit()
    update_mail_config(MailRuntimeConfigUpdate(bot_email_password="mail-secret", bot_enabled=True), session)

    department = session.query(ProductionDepartment).filter_by(department_code="default").one()
    result = delete_department(department.id, Request(), session)

    assert result["ok"] is True
    assert result["bot_disabled"] is True
    assert session.get(ProductionDepartment, department.id).status == "Deleted"
    assert session.get(SystemConfig, "bot_enabled").value == "false"
    assert all(row["id"] != department.id for row in list_departments(page=1, page_size=10, session=session)["items"])
    audit = session.query(AuditEvent).filter_by(event_type="ProductionDepartmentDeleted").one()
    assert audit.actor == "admin"


def test_logistics_department_crud_matches_production_email_shape():
    from backend.app.main import create_or_update_logistics_department, delete_logistics_department, list_logistics_departments
    from backend.app.schemas import DepartmentUpsert

    class Request:
        class State:
            username = "admin"

        state = State()

    session = make_session()

    default = session.query(LogisticsDepartment).filter_by(department_code="default").one()
    assert default.department_name == "默认物流部门"

    result = create_or_update_logistics_department(
        DepartmentUpsert(
            department_code="wuhan-logistics",
            department_name="武汉仓物流部",
            mail_to=["logistics@jimuyida.com"],
            mail_cc=["ops@jimuyida.com"],
        ),
        session,
    )
    assert result["ok"] is True

    listing = list_logistics_departments(q="物流", page=1, page_size=10, session=session)
    row = next(item for item in listing["items"] if item["department_code"] == "wuhan-logistics")
    assert row["department_name"] == "武汉仓物流部"
    assert row["mail_to"] == ["logistics@jimuyida.com"]
    assert row["mail_cc"] == ["ops@jimuyida.com"]

    deleted = delete_logistics_department(row["id"], Request(), session)

    assert deleted["ok"] is True
    assert session.get(LogisticsDepartment, row["id"]).status == "Deleted"
    assert all(item["id"] != row["id"] for item in list_logistics_departments(page=1, page_size=10, session=session)["items"])
    audit = session.query(AuditEvent).filter_by(event_type="LogisticsDepartmentDeleted").one()
    assert audit.actor == "admin"


def test_logistics_department_upsert_rejects_invalid_main_email():
    from backend.app.main import create_or_update_logistics_department
    from backend.app.schemas import DepartmentUpsert

    session = make_session()

    with pytest.raises(Exception) as exc:
        create_or_update_logistics_department(DepartmentUpsert(department_code="bad", department_name="坏邮箱", mail_to=["bad-email"], mail_cc=[]), session)

    assert exc.value.status_code == 400
    assert "主送邮箱格式不合法" in exc.value.detail


def create_ecommerce_order_mail(session, order_no="EC-001"):
    return create_inbound_mail(
        session,
        from_address="sales@jimuyida.com",
        subject=f"独立站订单需求 - 测试客户 - {order_no}",
        body_text="\n".join(
            [
                "客户名称：测试客户",
                "产品：物料编码：1050600001，物料名称：树脂大卫头像(带纸盒)",
                "数量：1",
                "期望交期：2026-06-11",
                f"订单号：{order_no}",
                "物流发货方式：顺丰",
                "客户收件信息：陈女士 18818881234 深圳市南山区",
            ]
        ),
    )


def test_ecommerce_order_routes_to_logistics_first():
    session = make_session()
    configure_department(session)
    configure_logistics_department(session)
    mail = create_ecommerce_order_mail(session)

    task = create_task_from_mail(session, mail)
    session.commit()

    assert task is not None
    assert task.task_no.startswith("LT-")
    assert session.query(LogisticsTask).count() == 1
    assert session.query(ProductionTask).count() == 0
    logistics_task = session.query(LogisticsTask).one()
    assert logistics_task.status == "LogisticsIssued"
    assert session.query(FulfillmentItem).filter_by(logistics_task_id=logistics_task.id, status="Pending").count() == 1
    issue = session.query(OutboundMailJob).filter_by(mail_type="LogisticsTaskIssue").one()
    assert as_list(issue.to_json) == ["logistics@jimuyida.com"]
    ack = session.query(OutboundMailJob).filter_by(mail_type="SalesReceiptAck").one()
    assert logistics_task.task_no in ack.body
    assert "物流核查任务" in ack.body


def test_logistics_stock_confirmation_closes_ecommerce_order():
    session = make_session()
    configure_department(session)
    configure_logistics_department(session)
    task = create_task_from_mail(session, create_ecommerce_order_mail(session, "EC-002"))
    session.commit()
    assert isinstance(task, LogisticsTask)

    reply = create_inbound_mail(
        session,
        from_address="logistics@jimuyida.com",
        subject=f"Re: [物流核查单][{task.task_no}] 库存满足",
        body_text="库存满足：是\n已发货\n发货单号：SF123456",
    )
    result = process_inbound_mail(session, reply)
    session.commit()

    session.refresh(task)
    assert result is not None
    assert task.status == "Closed"
    assert task.closed_reason == "LogisticsShipped"
    assert task.requirement.status == "Closed"
    assert session.query(FulfillmentItem).filter_by(logistics_task_id=task.id, status="Shipped").count() == 1
    notice = session.query(OutboundMailJob).filter_by(mail_type="LogisticsShipped").one()
    assert as_list(notice.to_json) == ["sales@jimuyida.com"]

    from backend.app.main import logistics_task_workflow

    workflow = logistics_task_workflow(task.id, session)
    assert "production" not in {step["key"] for step in workflow["steps"]}


def test_logistics_shortage_creates_production_task():
    session = make_session()
    configure_department(session)
    configure_logistics_department(session)
    task = create_task_from_mail(session, create_ecommerce_order_mail(session, "EC-003"))
    session.commit()
    assert isinstance(task, LogisticsTask)

    reply = create_inbound_mail(
        session,
        from_address="logistics@jimuyida.com",
        subject=f"Re: [物流核查单][{task.task_no}] 缺货",
        body_text="库存满足：否\n缺失物料：树脂大卫头像(带纸盒) 缺货 1 件",
    )
    result = process_inbound_mail(session, reply)
    session.commit()

    session.refresh(task)
    assert result is not None
    assert task.status == "ProductionRequested"
    assert task.production_task_id is not None
    production_task = session.get(ProductionTask, task.production_task_id)
    assert production_task is not None
    assert production_task.status == "TaskIssued"
    assert session.query(FulfillmentItem).filter_by(logistics_task_id=task.id, status="NeedProduction").count() == 1
    assert session.query(OutboundMailJob).filter_by(mail_type="TaskIssue", related_task_id=production_task.id).count() == 1

    from backend.app.main import logistics_task_workflow

    workflow = logistics_task_workflow(task.id, session)
    assert "production" in {step["key"] for step in workflow["steps"]}


def test_mail_and_outbound_serializers_infer_logistics_task_links():
    from backend.app.main import serialize_mail, serialize_outbound_mail

    session = make_session()
    configure_department(session)
    configure_logistics_department(session)
    source_mail = create_ecommerce_order_mail(session, "EC-LINK-001")
    task = create_task_from_mail(session, source_mail)
    session.commit()
    assert isinstance(task, LogisticsTask)

    reply = create_inbound_mail(
        session,
        from_address="logistics@jimuyida.com",
        subject=f"Re: [物流核查单][{task.task_no}] 库存满足",
        body_text="库存满足：是\n已发货\n发货单号：SF-LINK",
    )
    process_inbound_mail(session, reply)
    session.commit()

    source_payload = serialize_mail(source_mail, session)
    reply_payload = serialize_mail(reply, session)
    ack_job = session.query(OutboundMailJob).filter_by(mail_type="SalesReceiptAck").one()
    issue_job = session.query(OutboundMailJob).filter_by(mail_type="LogisticsTaskIssue").one()

    assert source_payload["related_task_type"] == "logistics"
    assert source_payload["related_task_no"] == task.task_no
    assert reply_payload["related_task_type"] == "logistics"
    assert reply_payload["related_task_no"] == task.task_no
    assert serialize_outbound_mail(ack_job, session)["related_task_no"] == task.task_no
    assert serialize_outbound_mail(issue_job, session)["related_task_type"] == "logistics"


def test_logistics_task_workflow_and_manual_close_match_task_controls():
    from backend.app.main import logistics_task_workflow, manual_close_logistics_task
    from backend.app.schemas import TaskManualCloseRequest

    class Request:
        class State:
            username = "admin"

        state = State()

    session = make_session()
    configure_department(session)
    configure_logistics_department(session)
    task = create_task_from_mail(session, create_ecommerce_order_mail(session, "EC-MANUAL-001"))
    session.commit()
    assert isinstance(task, LogisticsTask)

    workflow = logistics_task_workflow(task.id, session)
    assert workflow["task"]["task_no"] == task.task_no
    assert any(step["key"] == "issue" for step in workflow["steps"])
    assert workflow["trace"]["nodes"]

    result = manual_close_logistics_task(task.id, TaskManualCloseRequest(note="测试关闭"), Request(), session)
    session.refresh(task)

    assert result["closed"] is True
    assert task.status == "Closed"
    assert task.closed_reason == "ManualForceClosed"
    assert task.requirement.status == "Closed"
    assert session.query(FulfillmentItem).filter_by(logistics_task_id=task.id, status="Closed").count() == 1
    mail_types = {row["mail_type"] for row in result["outbound_jobs"]}
    assert {"LogisticsManualClosedSales", "LogisticsManualClosedLogistics"} <= mail_types


def test_manual_close_logistics_after_shortage_does_not_close_active_production_requirement():
    from backend.app.main import manual_close_logistics_task
    from backend.app.schemas import TaskManualCloseRequest

    class Request:
        class State:
            username = "admin"

        state = State()

    session = make_session()
    configure_department(session)
    configure_logistics_department(session)
    task = create_task_from_mail(session, create_ecommerce_order_mail(session, "EC-MANUAL-PROD-001"))
    session.commit()
    assert isinstance(task, LogisticsTask)

    reply = create_inbound_mail(
        session,
        from_address="logistics@jimuyida.com",
        subject=f"Re: [物流核查单][{task.task_no}] 缺货",
        body_text="库存满足：否\n缺失物料：树脂大卫头像(带纸盒) 缺货 1 件",
    )
    process_inbound_mail(session, reply)
    session.commit()
    session.refresh(task)
    assert task.production_task_id is not None
    production_task = session.get(ProductionTask, task.production_task_id)
    assert production_task.status == "TaskIssued"

    manual_close_logistics_task(task.id, TaskManualCloseRequest(note="只关闭物流跟进"), Request(), session)
    session.refresh(task)
    session.refresh(production_task)

    assert task.status == "Closed"
    assert production_task.status == "TaskIssued"
    assert production_task.requirement.status == "TaskCreated"


def test_clear_task_records_removes_linked_logistics_and_production_tasks():
    from backend.app.main import clear_task_records

    session = make_session()
    configure_department(session)
    configure_logistics_department(session)
    task = create_task_from_mail(session, create_ecommerce_order_mail(session, "EC-CLEAR-001"))
    session.commit()
    assert isinstance(task, LogisticsTask)

    reply = create_inbound_mail(
        session,
        from_address="logistics@jimuyida.com",
        subject=f"Re: [物流核查单][{task.task_no}] 缺货",
        body_text="库存满足：否\n缺失物料：树脂大卫头像(带纸盒) 缺货 1 件",
    )
    process_inbound_mail(session, reply)
    session.commit()

    detail = clear_task_records(session)
    session.commit()

    assert detail["logistics_task_count"] == 1
    assert detail["task_count"] == 1
    assert session.query(LogisticsTask).count() == 0
    assert session.query(ProductionTask).count() == 0
    assert session.query(OrderRequirement).count() == 0


def test_dashboard_includes_period_analytics_for_workbench_charts():
    session = make_session()
    configure_department(session)
    task = create_valid_task(session, "SO-DASH-001")
    task.status = "ProductionQuestioned"
    source_mail = session.get(MailMessage, task.requirement.source_mail_id)
    source_mail.body_text = f"{source_mail.body_text}\n客户收件信息：深圳南山区科技园"
    session.commit()

    data = dashboard(session)

    assert data["tasks_total"] == 1
    assert data["questioned"] == 1
    periods = data["analytics"]["periods"]
    assert set(periods) == {"month", "year"}
    assert periods["month"]["label"] == "月度"
    assert periods["year"]["label"] == "年度"
    assert periods["month"]["trend"][0]["label"] == "1日"
    assert periods["month"]["trend"][-1]["label"].endswith("日")
    assert len(periods["year"]["trend"]) == 12
    assert periods["year"]["trend"][0]["label"] == "1月"
    assert periods["year"]["trend"][-1]["label"] == "12月"
    for period in periods.values():
        assert period["task_stats"]["demand_total"] == 1
        assert period["trend"]
        assert period["status_distribution"][0]["label"] == "生产疑问"
        assert period["sales_top10"][0]["salesperson"] == "sales@jimuyida.com"
        assert period["product_top10"][0]["product"] == "积木展示架 A1"
        assert period["location_points"][0]["city"] == "深圳"


def test_health_reports_readiness_and_queue_counts():
    from backend.app.main import health

    session = make_session()
    session.add(
        OutboundMailJob(
            mail_type="WeeklyReport",
            to_json=dumps(["finance@jimuyida.com"]),
            cc_json=dumps([]),
            subject="周报",
            body="hello",
            idempotency_key="health-weekly-report",
            status="Pending",
        )
    )
    session.add(ProcessingJob(job_type="process_inbound_mail", payload_json=dumps({"mail_id": "demo"}), status="Pending"))
    session.commit()

    result = health(session)

    assert result["status"] == "ok"
    assert result["ready"] is False
    assert result["queues"]["outbound"]["counts"]["Pending"] == 1
    assert result["queues"]["outbound"]["pending_auto_dispatchable"] == 1
    assert result["queues"]["processing"]["counts"]["Pending"] == 1


def test_mail_rate_limit_interval_is_clamped_to_one_minute():
    assert clamp_mail_interval_seconds(30) == 60
    assert clamp_mail_interval_seconds("45") == 60
    assert clamp_mail_interval_seconds(120) == 120


def test_mail_list_search_matches_mail_id():
    from backend.app.main import mails

    session = make_session()
    mail = create_inbound_mail(
        session,
        from_address="sales@jimuyida.com",
        subject="按ID检索邮件",
        body_text="邮件正文",
    )
    session.commit()

    result = mails(q=mail.id, page=1, page_size=10, session=session)

    assert result["total"] == 1
    assert result["items"][0]["id"] == mail.id


def test_task_trace_graph_contains_core_linked_objects():
    from backend.app.main import task_trace

    session = make_session()
    configure_department(session)
    task = create_valid_task(session, order_no="TRACE-001")

    result = task_trace(task.id, session)
    node_types = {node["type"] for node in result["nodes"]}
    edge_labels = {edge["label"] for edge in result["edges"]}

    assert {"mail", "requirement", "task", "task_version", "outbound_mail"}.issubset(node_types)
    assert {"抽取", "生成任务", "版本"}.issubset(edge_labels)
    assert result["task"]["task_no"] == task.task_no
    assert result["timeline"]


def test_auth_token_roundtrip_and_tamper_detection():
    token = create_session_token("admin")
    assert parse_session_token(token) == "admin"
    assert parse_session_token(token + "x") is None


def test_database_url_normalization_masking_and_health():
    from backend.app.database import engine_kwargs, mask_database_url, normalize_database_url
    from backend.app.main import database_health

    assert normalize_database_url("postgres://user:secret@db.example.com:5432/app") == (
        "postgresql+psycopg://user:secret@db.example.com:5432/app"
    )
    assert normalize_database_url("postgresql://user:secret@db.example.com/app") == (
        "postgresql+psycopg://user:secret@db.example.com/app"
    )
    assert normalize_database_url("sqlite:///data/app.db") == "sqlite:///data/app.db"
    assert mask_database_url("postgresql://user:secret@db.example.com/app") == (
        "postgresql+psycopg://user:***@db.example.com/app"
    )
    assert engine_kwargs("sqlite:///data/app.db") == {"connect_args": {"check_same_thread": False}}
    assert engine_kwargs("postgresql+psycopg://user:secret@db.example.com/app") == {"pool_pre_ping": True}

    session = make_session()
    health = database_health(session)
    assert health["ok"] is True
    assert health["dialect"]
    assert "url" in health


def test_runtime_schema_adds_outbound_retry_columns(monkeypatch):
    from sqlalchemy import inspect, text
    import backend.app.database as database

    engine = create_engine("sqlite:///:memory:")
    with engine.begin() as connection:
        connection.execute(
            text(
                """
                CREATE TABLE outbound_mail_jobs (
                    id VARCHAR(36) PRIMARY KEY,
                    related_task_id VARCHAR(36),
                    related_version_id VARCHAR(36),
                    mail_type VARCHAR(64) NOT NULL,
                    to_json TEXT NOT NULL,
                    cc_json TEXT NOT NULL,
                    subject TEXT NOT NULL,
                    body TEXT NOT NULL,
                    idempotency_key VARCHAR(512) UNIQUE NOT NULL,
                    status VARCHAR(32) NOT NULL,
                    created_at DATETIME NOT NULL
                )
                """
            )
        )
        connection.execute(
            text(
                """
                INSERT INTO outbound_mail_jobs (
                    id, mail_type, to_json, cc_json, subject, body,
                    idempotency_key, status, created_at
                )
                VALUES (
                    'job-1', 'TaskIssue', '[]', '[]', 'subject', 'body',
                    'idem-1', 'Pending', '2026-05-13 00:00:00'
                )
                """
            )
        )

    monkeypatch.setattr(database, "engine", engine)
    database.ensure_runtime_schema()

    columns = {column["name"] for column in inspect(engine).get_columns("outbound_mail_jobs")}
    assert {"attempt_count", "next_retry_at", "last_error", "priority"}.issubset(columns)
    with engine.connect() as connection:
        row = connection.execute(text("SELECT attempt_count, priority FROM outbound_mail_jobs WHERE id = 'job-1'")).one()
    assert row.attempt_count == 0
    assert row.priority == 40


def test_order_to_task_approval_flow():
    session = make_session()
    configure_department(session)
    task = create_valid_task(session)

    assert task.status == "TaskIssued"
    job = approve_task(session, task.id, actor="tester")
    session.commit()

    assert job.mail_type == "TaskIssue"
    assert as_list(job.to_json) == ["production@jimuyida.com"]
    assert task.status == "TaskIssued"


def test_production_feedback_default_cc_rules():
    session = make_session()
    configure_department(session)
    task = create_valid_task(session)
    approve_task(session, task.id, actor="tester")
    session.commit()

    confirmed = record_production_feedback(session, task.id, "confirmed", "已确认排产")
    assert as_list(confirmed.cc_json) == [
        "dingyong@jimuyida.com",
        "sales@jimuyida.com",
        "jinlei@jimuyida.com",
    ]

    rejected_task = create_valid_task(session, order_no="SO-002")
    approve_task(session, rejected_task.id, actor="tester")
    rejected = record_production_feedback(session, rejected_task.id, "rejected", "资料不完整")
    assert as_list(rejected.cc_json) == ["jinlei@jimuyida.com"]


def test_production_natural_question_reply_is_routed_and_receipted():
    session = make_session()
    configure_department(session)
    task = create_valid_task(session)
    approve_task(session, task.id, actor="tester")
    session.commit()
    mail = create_inbound_mail(
        session,
        from_address="production@jimuyida.com",
        subject=f"Re: [生产任务单][{task.task_no}][测试客户][G100][V1]",
        body_text="没有写明哪个版本的G100，国内还是海外版？",
    )
    session.add(ProcessingJob(job_type="process_inbound_mail", payload_json=dumps({"mail_id": mail.id}), status="Pending"))
    session.commit()

    result = run_pending_jobs(session)
    session.commit()

    forward = session.query(OutboundMailJob).filter_by(mail_type="ProductionQuestionForward").one()
    receipt = session.query(OutboundMailJob).filter_by(mail_type="ProductionQuestionReceipt").one()
    assert result["completed"] == 1
    assert mail.classification == "ProductionQuestion"
    assert mail.related_task_id == task.id
    assert as_list(forward.to_json) == ["sales@jimuyida.com"]
    assert "没有写明哪个版本" in forward.body
    assert as_list(receipt.to_json) == ["production@jimuyida.com"]
    assert "已转发销售人员补充确认" in receipt.body


def test_production_email_can_query_pending_confirmation_tasks():
    session = make_session()
    configure_department(session)
    task = create_valid_task(session, order_no="SO-PENDING-QUERY")
    session.commit()
    mail = create_inbound_mail(
        session,
        from_address="production@jimuyida.com",
        subject="查询待确认任务",
        body_text="请查询当前待确认生产任务。",
    )

    result = process_mail_direct(session, mail)
    session.commit()

    reply = session.query(OutboundMailJob).filter_by(mail_type="ProductionPendingTasksQueryReply").one()
    assert result == reply
    assert mail.classification == "ProductionPendingTaskQuery"
    assert as_list(reply.to_json) == ["production@jimuyida.com"]
    assert task.task_no in reply.body
    assert "如需确认指定任务" in reply.body


def test_sales_email_can_query_own_demand_status_with_llm(monkeypatch):
    session = make_session()
    configure_department(session)
    own_task = create_valid_task(session, order_no="SO-SALES-QUERY")
    other_mail = create_inbound_mail(
        session,
        from_address="other.sales@jimuyida.com",
        subject="生产订单需求 - 其他客户",
        body_text="\n".join(
            [
                "客户名称：其他客户",
                "产品：G200",
                "数量：10 套",
                "期望交期：2026-06-01",
                "订单号：SO-OTHER-QUERY",
            ]
        ),
    )
    create_task_from_mail(session, other_mail)
    session.commit()
    query_mail = create_inbound_mail(
        session,
        from_address="sales@jimuyida.com",
        subject="查询需求状态",
        body_text="请查询我提交过的需求状态及统计。",
    )
    captured = {}

    def fake_call_model(session, config, *, task_type, messages, related_object_type=None, related_object_id=None):
        captured["task_type"] = task_type
        captured["prompt"] = messages[-1]["content"]
        return {"choices": [{"message": {"content": "销售同事好，您当前有 1 条生产任务，均已下达生产。"}}]}

    monkeypatch.setattr("backend.app.services.workflow.call_model", fake_call_model)

    result = process_mail_direct(session, query_mail)
    session.commit()

    reply = session.query(OutboundMailJob).filter_by(mail_type="SalesDemandStatusQueryReply").one()
    assert result == reply
    assert query_mail.classification == "SalesDemandStatusQuery"
    assert as_list(reply.to_json) == ["sales@jimuyida.com"]
    assert "销售同事好" in reply.body
    assert own_task.task_no in captured["prompt"]
    assert "SO-OTHER-QUERY" not in captured["prompt"]
    assert captured["task_type"] == "MailStatusQueryReply"


def test_production_email_can_query_accepted_demand_status_with_llm(monkeypatch):
    session = make_session()
    configure_department(session)
    task = create_valid_task(session, order_no="SO-PROD-STATUS")
    session.commit()
    query_mail = create_inbound_mail(
        session,
        from_address="production@jimuyida.com",
        subject="查询受理需求统计",
        body_text="请查询生产侧受理需求的状态和统计。",
    )
    captured = {}

    def fake_call_model(session, config, *, task_type, messages, related_object_type=None, related_object_id=None):
        captured["prompt"] = messages[-1]["content"]
        return {"choices": [{"message": {"content": "生产部同事好，当前受理任务 1 条，待确认 1 条。"}}]}

    monkeypatch.setattr("backend.app.services.workflow.call_model", fake_call_model)

    result = process_mail_direct(session, query_mail)
    session.commit()

    reply = session.query(OutboundMailJob).filter_by(mail_type="ProductionDemandStatusQueryReply").one()
    assert result == reply
    assert query_mail.classification == "ProductionDemandStatusQuery"
    assert as_list(reply.to_json) == ["production@jimuyida.com"]
    assert "生产部同事好" in reply.body
    assert task.task_no in captured["prompt"]


def test_production_email_can_confirm_specified_task():
    session = make_session()
    configure_department(session)
    task = create_valid_task(session, order_no="SO-PROD-CONFIRM")
    session.commit()
    mail = create_inbound_mail(
        session,
        from_address="production@jimuyida.com",
        subject="确认排产",
        body_text=f"确认排产 {task.task_no}",
    )

    result = process_mail_direct(session, mail)
    session.commit()

    confirmed = session.query(OutboundMailJob).filter_by(mail_type="ProductionConfirmed", related_task_id=task.id).one()
    receipt = session.query(OutboundMailJob).filter_by(mail_type="ProductionConfirmationReceipt", related_task_id=task.id).one()
    assert result == confirmed
    assert mail.classification == "ProductionScheduleConfirmation"
    assert mail.related_task_id == task.id
    assert task.status == "Closed"
    assert task.closed_reason == "ScheduledConfirmed"
    assert as_list(receipt.to_json) == ["production@jimuyida.com"]


def test_production_email_can_confirm_current_task_by_reply_subject():
    session = make_session()
    configure_department(session)
    task = create_valid_task(session, order_no="SO-PROD-REPLY-CONFIRM")
    session.commit()
    mail = create_inbound_mail(
        session,
        from_address="production@jimuyida.com",
        subject=f"Re: [生产任务单][{task.task_no}][测试客户][G100][V1]",
        body_text="确认",
    )

    process_mail_direct(session, mail)
    session.commit()

    assert mail.related_task_id == task.id
    assert mail.classification == "ProductionScheduleConfirmation"
    assert task.status == "Closed"
    assert session.query(OutboundMailJob).filter_by(mail_type="ProductionConfirmed", related_task_id=task.id).count() == 1


def test_production_reply_agree_schedule_confirms_current_task():
    session = make_session()
    configure_department(session)
    task = create_valid_task(session, order_no="SO-PROD-AGREE-SCHEDULE")
    session.commit()
    mail = create_inbound_mail(
        session,
        from_address="production@jimuyida.com",
        subject=f"Re: [生产任务单][{task.task_no}][江西大学][G200][V1]",
        body_text="收到任务单，同意排产",
    )

    process_mail_direct(session, mail)
    session.commit()

    assert mail.related_task_id == task.id
    assert mail.classification == "ProductionScheduleConfirmation"
    assert task.status == "Closed"
    assert task.closed_reason == "ScheduledConfirmed"
    assert session.query(OutboundMailJob).filter_by(mail_type="ProductionConfirmed", related_task_id=task.id).count() == 1


def test_conversation_closes_when_max_rounds_reached():
    session = make_session()
    configure_department(session)
    set_config(session, "conversation_max_rounds", "1")
    task = create_valid_task(session, order_no="SO-MAX-ROUND")
    approve_task(session, task.id, actor="tester")
    session.add(
        QuestionAndReply(
            task_id=task.id,
            question_text="第一轮疑问",
            reply_text="第一轮答复",
            status="Answered",
        )
    )
    session.commit()
    mail = create_inbound_mail(
        session,
        from_address="production@jimuyida.com",
        subject=f"Re: [生产任务单][{task.task_no}]",
        body_text="没有写明包装方式？",
    )
    session.add(ProcessingJob(job_type="process_inbound_mail", payload_json=dumps({"mail_id": mail.id}), status="Pending"))
    session.commit()

    result = run_pending_jobs(session)
    session.commit()

    close_job = session.query(OutboundMailJob).filter_by(mail_type="ConversationClosedMaxRounds").one()
    case = session.query(ExceptionCase).filter_by(exception_type="ConversationMaxRounds").one()
    assert result["completed"] == 1
    assert task.status == "Closed"
    assert task.closed_reason == "ConversationMaxRounds"
    assert as_list(close_job.to_json) == ["sales@jimuyida.com", "production@jimuyida.com"]
    assert "请销售重新发起完整的订单需求邮件" in close_job.body
    assert case.related_task_id == task.id


def test_workflow_conversation_policy_overrides_global_max_rounds():
    session = make_session()
    configure_department(session)
    set_config(session, "conversation_max_rounds", "3")
    import_structured_workflow_rules(
        session,
        rules=[
            {
                "workflow_name": "轮次限制流程",
                "routing": {"to_names": ["production@jimuyida.com"], "cc_names": []},
                "match": {"any_keywords": ["轮次限制流程", "轮次限制"], "order_type": "normal_sales"},
                "subject_template": "[轮次限制][{{task_no}}]",
                "body_template": "流程类型：轮次限制流程",
                "required_fields": ["customer_name", "product_summary", "quantity_text", "expected_delivery_date"],
                "required_attachments": [],
                "review_rules": [],
                "conversation_policy": {
                    "max_question_rounds": 1,
                    "on_exceeded": "close_task",
                    "message": "本流程最多允许1轮询问答疑，已达到上限。",
                },
            }
        ],
        actor="tester",
        auto_publish=True,
        source_asset_ref="workflow-policy-test",
    )
    mail = create_inbound_mail(
        session,
        from_address="sales@jimuyida.com",
        subject="生产订单需求 - 轮次限制流程",
        body_text="\n".join(
            [
                "客户名称：轮次客户",
                "产品：轮次产品",
                "数量：10台",
                "期望交期：2026-10-20",
                "订单号：SO-WF-MAX-ROUND",
            ]
        ),
    )
    task = create_task_from_mail(session, mail)
    assert task is not None
    approve_task(session, task.id, actor="tester")
    session.add(
        QuestionAndReply(
            task_id=task.id,
            question_text="第一轮疑问",
            reply_text="第一轮答复",
            status="Answered",
        )
    )
    production_mail = create_inbound_mail(
        session,
        from_address="production@jimuyida.com",
        subject=f"Re: [轮次限制][{task.task_no}]",
        body_text="请再确认包装方式？",
    )
    session.commit()

    close_job = record_production_question(session, task.id, production_mail.body_text, source_mail=production_mail)
    session.commit()

    assert close_job.mail_type == "ConversationClosedMaxRounds"
    assert task.status == "Closed"
    assert "本流程最多允许1轮询问答疑" in close_job.body


def make_docx_bytes(text: str) -> bytes:
    document = Document()
    document.add_paragraph(text)
    buffer = io.BytesIO()
    document.save(buffer)
    return buffer.getvalue()


def make_xlsx_bytes(rows: list[list[str]]) -> bytes:
    workbook = Workbook()
    sheet = workbook.active
    sheet.title = "订单"
    for row in rows:
        sheet.append(row)
    buffer = io.BytesIO()
    workbook.save(buffer)
    workbook.close()
    return buffer.getvalue()


def test_word_excel_zip_attachment_parser():
    docx_bytes = make_docx_bytes("客户名称：附件客户")
    xlsx_bytes = make_xlsx_bytes([["产品", "数量"], ["积木展架", "80套"]])
    pdf_bytes = simple_pdf("PDF订单", ["客户名称：PDF客户", "产品：PDF展架"])
    zip_buffer = io.BytesIO()
    with zipfile.ZipFile(zip_buffer, "w") as archive:
        archive.writestr("需求.docx", docx_bytes)
        archive.writestr("订单.xlsx", xlsx_bytes)
        archive.writestr("订单.pdf", pdf_bytes)

    docx = parse_attachment("需求.docx", docx_bytes, max_zip_bytes=1024 * 1024, max_depth=1)
    xlsx = parse_attachment("订单.xlsx", xlsx_bytes, max_zip_bytes=1024 * 1024, max_depth=1)
    pdf = parse_attachment("订单.pdf", pdf_bytes, max_zip_bytes=1024 * 1024, max_depth=1)
    zipped = parse_attachment("资料.zip", zip_buffer.getvalue(), max_zip_bytes=1024 * 1024, max_depth=1)

    assert docx.status == "Parsed"
    assert "附件客户" in docx.text
    assert xlsx.status == "Parsed"
    assert "积木展架 | 80套" in xlsx.text
    assert pdf.status == "Parsed"
    assert "PDF客户" in pdf.text
    assert zipped.status == "Parsed"
    assert len(zipped.children) == 3
    assert "附件客户" in zipped.text
    assert "积木展架 | 80套" in zipped.text
    assert "PDF展架" in zipped.text


def test_email_store_and_processing_queue_creates_task():
    session = make_session()
    configure_department(session)

    message = EmailMessage()
    message["From"] = "销售 <sales@jimuyida.com>"
    message["To"] = "bot.market@jimuyida.com"
    message["Subject"] = "生产订单需求 - 邮箱入库"
    message["Message-ID"] = "<mail-queue-test@jimuyida.com>"
    message.set_content(
        "\n".join(
            [
                "客户名称：邮箱客户",
                "产品：快闪展台",
                "数量：32套",
                "期望交期：2026-06-01",
                "订单号：SO-MAIL-001",
            ]
        )
    )
    message.add_attachment(
        make_docx_bytes("附件补充：表面处理为哑光。"),
        maintype="application",
        subtype="vnd.openxmlformats-officedocument.wordprocessingml.document",
        filename="补充说明.docx",
    )

    incoming = parse_email_bytes(message.as_bytes())
    mail = store_incoming_email(session, incoming)
    assert session.query(OutboundMailJob).filter_by(mail_type="SalesReceiptAck").count() == 0
    session.add(ProcessingJob(job_type="process_inbound_mail", payload_json=dumps({"mail_id": mail.id}), status="Pending"))
    session.commit()

    result = run_pending_jobs(session)
    session.commit()
    assets = session.query(AttachmentAsset).filter_by(mail_id=mail.id).all()
    ack = session.query(OutboundMailJob).filter_by(mail_type="SalesReceiptAck").one()

    assert result["completed"] == 1
    assert result["failed"] == 0
    parsed_assets = [asset for asset in assets if asset.parse_status == "Parsed"]
    raw_assets = [asset for asset in assets if asset.content_type == "message/rfc822"]
    assert len(parsed_assets) == 1
    assert len(raw_assets) == 1
    assert "表面处理" in (parsed_assets[0].extracted_text or "")
    assert mail.related_task_id is not None
    assert as_list(ack.to_json) == ["sales@jimuyida.com"]
    task = session.get(ProductionTask, mail.related_task_id)
    assert task is not None
    assert f"任务号：{task.task_no}" in ack.body
    assert "邮箱入库" in ack.subject

    duplicate = store_incoming_email(session, incoming)
    assert duplicate.id == mail.id
    assert session.query(OutboundMailJob).filter_by(mail_type="SalesReceiptAck").count() == 1


def test_parse_email_bytes_repairs_legacy_gbk_attachment_filename():
    file_name = "采购订单-JM-CGDD-20260522（上海测试公司）04.28.docx"
    raw = (
        b"From: sales@jimuyida.com\r\n"
        b"To: bot.market@jimuyida.com\r\n"
        b"Subject: =?utf-8?b?55Sf5Lqn6K6i5Y2V?=\r\n"
        b"Message-ID: <legacy-gbk-filename@jimuyida.com>\r\n"
        b"MIME-Version: 1.0\r\n"
        b'Content-Type: multipart/mixed; boundary="b1"\r\n'
        b"\r\n"
        b"--b1\r\n"
        b'Content-Type: text/plain; charset="utf-8"\r\n'
        b"\r\n"
        b"body\r\n"
        b"--b1\r\n"
        b"Content-Type: application/vnd.openxmlformats-officedocument.wordprocessingml.document\r\n"
        + b'Content-Disposition: attachment; filename="' + file_name.encode("gb18030") + b'"\r\n'
        + b"\r\n"
        + b"fake-docx\r\n"
        + b"--b1--\r\n"
    )

    incoming = parse_email_bytes(raw)

    assert incoming.attachments[0].file_name == file_name


def test_task_issue_attachment_filename_has_chinese_compatible_headers(monkeypatch):
    session = make_session()
    configure_department(session)
    set_config(session, "bot_enabled", "true", is_secret=False)
    set_config(session, "bot_email_password", "runtime-secret", is_secret=True)
    task = create_valid_task(session, "SO-FILENAME-001")
    file_name = "采购订单-JM-CGDD-20260522（上海测试公司）04.28.docx"
    save_and_parse_attachment(
        session,
        task.requirement.source_mail_id,
        file_name,
        "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        make_docx_bytes("附件正文"),
    )
    job = OutboundMailJob(
        mail_type="TaskIssue",
        to_json=dumps(["production@jimuyida.com"]),
        cc_json=dumps([]),
        subject="生产任务单",
        body="任务单",
        idempotency_key="filename-compatible-task-issue",
        related_task_id=task.id,
        status="Pending",
    )
    session.add(job)
    session.commit()

    class FakeSMTP:
        sent_messages = []

        def __init__(self, host, port, **kwargs):
            pass

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, traceback):
            return False

        def login(self, username, password):
            assert password == "runtime-secret"

        def send_message(self, msg, from_addr, to_addrs):
            self.sent_messages.append(msg)

    monkeypatch.setattr("backend.app.services.mail_adapter.smtplib.SMTP_SSL", FakeSMTP)

    result = send_outbound_jobs_smtp(session, [job.id])
    session.commit()
    raw_message = FakeSMTP.sent_messages[0].as_string()

    assert result == {"sent": 1, "failed": 0, "total": 1}
    assert "filename*0*=" not in raw_message
    assert "filename*=" in raw_message
    assert "%E9%87%87%E8%B4%AD%E8%AE%A2%E5%8D%95" in raw_message


def test_config_backed_model_provider_key_and_payload():
    session = make_session()
    model = session.query(ModelProviderConfig).one()
    set_config(session, "model_api_key", "runtime-secret", is_secret=True)
    model.credential_ref = "config:model_api_key"
    session.commit()

    payload = build_openai_chat_payload(model.model_name, [{"role": "user", "content": "ping"}])

    assert resolve_api_key(session, model) == "runtime-secret"
    assert payload["model"] == "DeepSeek-V3"
    assert payload["messages"][0]["content"] == "ping"


def test_model_provider_extracts_chat_content():
    output = {"choices": [{"message": {"content": "配置可用"}}]}
    assert extract_chat_content(output) == "配置可用"
    assert extract_chat_content({"choices": []}) == ""


def test_model_provider_streaming_collects_sse_chunks(monkeypatch):
    session = make_session()
    model = session.query(ModelProviderConfig).one()
    set_config(session, "model_api_key", "runtime-secret", is_secret=True)
    model.credential_ref = "config:model_api_key"
    session.commit()

    class FakeStreamResponse:
        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def raise_for_status(self):
            return None

        def iter_lines(self):
            yield 'data: {"choices":[{"delta":{"content":"流程"}}]}'
            yield 'data: {"choices":[{"delta":{"content":"导入"}}]}'
            yield "data: [DONE]"

    class FakeClient:
        def __init__(self, *args, **kwargs):
            self.timeout = kwargs.get("timeout")

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def stream(self, method, url, *, headers=None, json=None):
            assert method == "POST"
            assert url.endswith("/chat/completions")
            assert json is not None and json.get("stream") is True
            return FakeStreamResponse()

    monkeypatch.setattr("backend.app.services.model_provider.httpx.Client", FakeClient)

    output = call_model(
        session,
        model,
        task_type="WorkflowImportParse",
        messages=[{"role": "user", "content": "ping"}],
        stream=True,
    )

    assert extract_chat_content(output) == "流程导入"


def test_self_maintenance_context_summarizes_queue_and_failures():
    session = make_session()
    session.add(ProcessingJob(job_type="process_inbound_mail", payload_json=dumps({"mail_id": "missing"}), status="Failed", error_message="mail not found"))
    session.add(OutboundMailJob(mail_type="SalesReceiptAck", to_json=dumps(["sales@jimuyida.com"]), cc_json=dumps([]), subject="回执", body="已收到", status="Pending", idempotency_key="self-maint-pending"))
    record_exception_case(session, exception_type="RoutingMissing", severity="Medium", detail={"message": "生产路由缺失"})
    session.commit()

    context = build_self_maintenance_context(session)

    assert context["queues"]["processing_counts"]["Failed"] == 1
    assert context["queues"]["outbound_counts"]["Pending"] == 1
    assert context["exceptions"]["open_count"] == 1
    assert context["runtime"]["model_ready"] is False


def test_self_maintenance_diagnosis_persists_session_and_actions_without_llm():
    session = make_session()
    session.add(ProcessingJob(job_type="process_inbound_mail", payload_json=dumps({"mail_id": "missing"}), status="Failed", error_message="mail not found"))
    session.commit()

    row = create_maintenance_diagnosis(session, user_message="为什么入库失败？", actor="admin", use_llm=False)
    session.commit()

    assert row.risk_level == "High"
    assert "诊断结论" in row.diagnosis_md
    assert session.query(MaintenanceSession).count() == 1
    actions = session.query(MaintenanceAction).filter_by(session_id=row.id).all()
    assert actions
    assert any(loads(action.input_json, {}).get("title") == "复核失败入库任务" for action in actions)


def test_self_maintenance_archive_session_updates_status_and_timeline():
    session = make_session()
    row = create_maintenance_diagnosis(session, user_message="归档已处理维护会话", actor="admin", use_llm=False)

    archived = archive_maintenance_session(session, row.id, note="已处理完成", actor="admin")
    session.commit()

    assert archived.status == "Archived"
    audit = session.query(AuditEvent).filter_by(event_type="SelfMaintenanceSessionArchived", related_object_id=row.id).one()
    assert loads(audit.detail, {})["note"] == "已处理完成"
    timeline = maintenance_session_timeline(session, row.id)["timeline"]
    assert any(item["event_type"] == "SelfMaintenanceSessionArchived" for item in timeline)


def test_self_maintenance_config_patch_requires_explicit_apply():
    session = make_session()
    set_config(session, "outbound_failed_alert_threshold", "20", is_secret=False)
    session.commit()

    row = create_maintenance_diagnosis(session, user_message="检查告警配置", actor="admin", use_llm=False)
    session.commit()

    action = session.query(MaintenanceAction).filter_by(session_id=row.id, action_type="config_patch").one()
    payload = loads(action.input_json, {})
    assert payload["changes"][0]["key"] == "outbound_failed_alert_threshold"
    assert session.get(SystemConfig, "outbound_failed_alert_threshold").value == "20"

    applied = apply_maintenance_action(session, action.id, actor="admin")
    session.commit()

    assert applied.status == "Completed"
    assert applied.approved_by == "admin"
    assert session.get(SystemConfig, "outbound_failed_alert_threshold").value == "1"
    refreshed = session.get(MaintenanceSession, row.id)
    proposed_actions = loads(refreshed.proposed_actions_json, [])
    assert any(item.get("action_id") == action.id and item.get("action_status") == "Completed" for item in proposed_actions)


def test_self_maintenance_code_patch_plan_is_non_executing_action():
    session = make_session()

    row = create_code_patch_plan(session, user_message="为管理台自维护页面增加更清晰的错误提示", actor="admin", use_llm=False)
    session.commit()

    assert row.status == "Planned"
    assert "修复草案" in row.diagnosis_md
    action = session.query(MaintenanceAction).filter_by(session_id=row.id, action_type="code_patch_plan").one()
    payload = loads(action.input_json, {})
    assert action.status == "Proposed"
    assert "backend/app/static/app.js" in payload["suggested_files"]
    assert "python3 -m pytest" in payload["validation_commands"]
    assert session.query(MaintenanceAction).filter(MaintenanceAction.action_type == "config_patch").count() == 0


def test_self_maintenance_action_detail_includes_session_and_timeline():
    session = make_session()
    row = create_code_patch_plan(session, user_message="查看单个维护动作详情", actor="admin", use_llm=False)
    session.add(
        AuditEvent(
            event_type="SelfMaintenanceCodePlanCreated",
            actor="admin",
            related_object_type="MaintenanceSession",
            related_object_id=row.id,
            detail=dumps({"risk_level": row.risk_level}),
            created_at=now_utc(),
        )
    )
    session.commit()

    action = session.query(MaintenanceAction).filter_by(session_id=row.id, action_type="code_patch_plan").one()
    detail = self_maintenance_action_detail(action.id, session)

    assert detail["id"] == action.id
    assert detail["session"]["id"] == row.id
    assert detail["runner_commands"]
    assert any(item["event_type"] == "SelfMaintenanceCodePlanCreated" for item in detail["timeline"])


def test_self_maintenance_handoff_package_updates_action_and_session(tmp_path):
    session = make_session()
    row = create_code_patch_plan(session, user_message="为管理台自维护页面增加更清晰的错误提示", actor="admin", use_llm=False)
    action = session.query(MaintenanceAction).filter_by(session_id=row.id, action_type="code_patch_plan").one()

    handoff = create_maintenance_handoff_package(session, action.id, actor="admin", output_dir=tmp_path / "handoff")
    session.commit()

    assert handoff.status == "HandoffReady"
    result = loads(handoff.result_json, {})
    assert result["handoff"]["created_by"] == "admin"
    assert "python3 scripts/maintenance_runner.py validate" in "\n".join(result["handoff"]["runner_commands"])
    assert (tmp_path / "handoff" / f"maintenance-action-{action.id}.md").exists()
    assert (tmp_path / "handoff" / f"maintenance-action-{action.id}.json").exists()
    refreshed_session = session.get(MaintenanceSession, row.id)
    proposed_actions = loads(refreshed_session.proposed_actions_json, [])
    assert proposed_actions[0]["action_status"] == "HandoffReady"
    assert proposed_actions[0]["handoff"]["markdown_path"].endswith(f"maintenance-action-{action.id}.md")


def test_self_maintenance_reads_handoff_package_content(tmp_path):
    session = make_session()
    row = create_code_patch_plan(session, user_message="为管理台自维护页面增加更清晰的错误提示", actor="admin", use_llm=False)
    action = session.query(MaintenanceAction).filter_by(session_id=row.id, action_type="code_patch_plan").one()
    create_maintenance_handoff_package(session, action.id, actor="admin", output_dir=tmp_path / "handoff")
    session.commit()

    detail = read_maintenance_handoff_package(session, action.id, output_dir=tmp_path / "handoff")

    assert detail["action_id"] == action.id
    assert detail["markdown"]["exists"] is True
    assert f"Maintenance Code Plan {action.id}" in detail["markdown"]["content"]
    assert detail["json"]["exists"] is True
    assert detail["json"]["content"]["action"]["id"] == action.id
    assert "runner_commands" in detail["json"]["content"]


def test_self_maintenance_validation_runs_allowed_command_and_updates_session(tmp_path):
    session = make_session()
    row = create_code_patch_plan(session, user_message="为管理台自维护页面增加更清晰的错误提示", actor="admin", use_llm=False)
    action = session.query(MaintenanceAction).filter_by(session_id=row.id, action_type="code_patch_plan").one()
    calls = []

    def fake_runner(command, cwd, timeout_seconds):
        calls.append((command, cwd, timeout_seconds))
        return {"command": command, "exit_code": 0, "stdout_tail": "ok", "stderr_tail": ""}

    validated = run_maintenance_validation(
        session,
        action.id,
        selected_commands=["node --check backend/app/static/app.js"],
        timeout_seconds=9,
        output_dir=tmp_path / "reports",
        cwd=tmp_path,
        actor="admin",
        command_runner=fake_runner,
    )
    session.commit()

    assert validated.status == "Validated"
    assert calls == [("node --check backend/app/static/app.js", tmp_path, 9)]
    result = loads(validated.result_json, {})
    assert result["validation"]["validated_by"] == "admin"
    assert result["validation"]["commands"][0]["exit_code"] == 0
    assert result["commands"][0]["exit_code"] == 0
    refreshed_session = session.get(MaintenanceSession, row.id)
    proposed_actions = loads(refreshed_session.proposed_actions_json, [])
    assert proposed_actions[0]["action_status"] == "Validated"
    assert proposed_actions[0]["validation_result"]["commands"][0]["command"] == "node --check backend/app/static/app.js"


def test_self_maintenance_timeline_orders_session_action_and_results(tmp_path):
    session = make_session()
    row = create_code_patch_plan(session, user_message="为管理台自维护页面增加更清晰的错误提示", actor="admin", use_llm=False)
    action = session.query(MaintenanceAction).filter_by(session_id=row.id, action_type="code_patch_plan").one()
    create_maintenance_handoff_package(session, action.id, actor="admin", output_dir=tmp_path / "handoff")
    report_maintenance_implementation(session, action.id, status="PatchReady", summary="补丁已完成，等待人工复核", actor="maintenance-runner")
    review_maintenance_implementation(session, action.id, decision="ReviewAccepted", note="人工复核通过", actor="admin")
    session.commit()

    payload = maintenance_session_timeline(session, row.id)
    events = [item["event_type"] for item in payload["timeline"]]

    assert events[0] == "MaintenanceSessionCreated"
    assert "MaintenanceActionProposed" in events
    assert "MaintenanceHandoffCreated" in events
    assert "MaintenanceImplementationReported" in events
    assert "MaintenanceImplementationReviewed" in events
    assert payload["timeline"] == sorted(payload["timeline"], key=lambda item: item["created_at"])
    assert payload["session"]["id"] == row.id
    assert payload["actions"][0]["id"] == action.id


def test_maintenance_runner_validates_code_plan_with_allowed_command(tmp_path):
    session = make_session()
    row = create_code_patch_plan(session, user_message="为管理台自维护页面增加更清晰的错误提示", actor="admin", use_llm=False)
    action = session.query(MaintenanceAction).filter_by(session_id=row.id, action_type="code_patch_plan").one()
    calls = []

    def fake_runner(command, cwd, timeout_seconds):
        calls.append((command, cwd, timeout_seconds))
        return CommandResult(command=command, exit_code=0, stdout_tail="ok", stderr_tail="")

    validated = validate_code_plan_action(
        session,
        action_id=action.id,
        cwd=tmp_path,
        output_dir=tmp_path / "reports",
        timeout_seconds=12,
        selected_commands=["node --check backend/app/static/app.js"],
        command_runner=fake_runner,
    )
    session.commit()

    assert validated.status == "Validated"
    assert calls == [("node --check backend/app/static/app.js", tmp_path, 12)]
    result = loads(validated.result_json, {})
    assert result["commands"][0]["exit_code"] == 0
    assert (tmp_path / "reports" / f"maintenance-action-{action.id}.md").exists()
    refreshed_session = session.get(MaintenanceSession, row.id)
    proposed_actions = loads(refreshed_session.proposed_actions_json, [])
    assert proposed_actions[0]["action_status"] == "Validated"
    assert proposed_actions[0]["validation_result"]["commands"][0]["exit_code"] == 0
    assert session.query(AuditEvent).filter_by(event_type="MaintenanceRunnerValidationCompleted").count() == 1


def test_maintenance_runner_creates_handoff_package(tmp_path):
    session = make_session()
    row = create_code_patch_plan(session, user_message="为管理台自维护页面增加更清晰的错误提示", actor="admin", use_llm=False)
    action = session.query(MaintenanceAction).filter_by(session_id=row.id, action_type="code_patch_plan").one()

    handoff = create_handoff_package(session, action_id=action.id, output_dir=tmp_path / "handoff")
    session.commit()

    assert handoff.status == "HandoffReady"
    result = loads(handoff.result_json, {})
    markdown_path = tmp_path / "handoff" / f"maintenance-action-{action.id}.md"
    json_path = tmp_path / "handoff" / f"maintenance-action-{action.id}.json"
    assert result["handoff"]["markdown_path"] == str(markdown_path)
    assert result["handoff"]["json_path"] == str(json_path)
    assert markdown_path.exists()
    assert json_path.exists()
    handoff_payload = loads(json_path.read_text(encoding="utf-8"), {})
    assert handoff_payload["action"]["id"] == action.id
    assert "safety_boundaries" in handoff_payload
    refreshed_session = session.get(MaintenanceSession, row.id)
    proposed_actions = loads(refreshed_session.proposed_actions_json, [])
    assert proposed_actions[0]["action_status"] == "HandoffReady"
    assert session.query(AuditEvent).filter_by(event_type="MaintenanceRunnerHandoffCreated").count() == 1


def test_maintenance_implementation_report_updates_action_and_session():
    session = make_session()
    row = create_code_patch_plan(session, user_message="为管理台自维护页面增加更清晰的错误提示", actor="admin", use_llm=False)
    action = session.query(MaintenanceAction).filter_by(session_id=row.id, action_type="code_patch_plan").one()

    reported = report_maintenance_implementation(
        session,
        action.id,
        status="PatchReady",
        summary="补丁已完成，等待人工复核",
        changed_files=["backend/app/static/app.js"],
        tests=["node --check backend/app/static/app.js"],
        residual_risks=["尚未运行浏览器端回归"],
        actor="maintenance-runner",
    )
    session.commit()

    assert reported.status == "PatchReady"
    result = loads(reported.result_json, {})
    assert result["implementation"]["summary"] == "补丁已完成，等待人工复核"
    assert result["implementation"]["changed_files"] == ["backend/app/static/app.js"]
    refreshed_session = session.get(MaintenanceSession, row.id)
    proposed_actions = loads(refreshed_session.proposed_actions_json, [])
    assert proposed_actions[0]["action_status"] == "PatchReady"
    assert proposed_actions[0]["implementation"]["tests"] == ["node --check backend/app/static/app.js"]


def test_maintenance_review_accepts_patch_ready_action():
    session = make_session()
    row = create_code_patch_plan(session, user_message="为管理台自维护页面增加更清晰的错误提示", actor="admin", use_llm=False)
    action = session.query(MaintenanceAction).filter_by(session_id=row.id, action_type="code_patch_plan").one()
    report_maintenance_implementation(
        session,
        action.id,
        status="PatchReady",
        summary="补丁已完成，等待人工复核",
        actor="maintenance-runner",
    )

    reviewed = review_maintenance_implementation(
        session,
        action.id,
        decision="ReviewAccepted",
        note="人工复核通过",
        actor="admin",
    )
    session.commit()

    assert reviewed.status == "ReviewAccepted"
    result = loads(reviewed.result_json, {})
    assert result["review"]["decision"] == "ReviewAccepted"
    assert result["review"]["note"] == "人工复核通过"
    refreshed_session = session.get(MaintenanceSession, row.id)
    proposed_actions = loads(refreshed_session.proposed_actions_json, [])
    assert proposed_actions[0]["action_status"] == "ReviewAccepted"
    assert proposed_actions[0]["review"]["reviewed_by"] == "admin"


def test_maintenance_review_rejects_unreviewable_status():
    session = make_session()
    row = create_code_patch_plan(session, user_message="为管理台自维护页面增加更清晰的错误提示", actor="admin", use_llm=False)
    action = session.query(MaintenanceAction).filter_by(session_id=row.id, action_type="code_patch_plan").one()

    with pytest.raises(ValueError, match="not reviewable"):
        review_maintenance_implementation(session, action.id, decision="ReviewAccepted", actor="admin")

    assert session.get(MaintenanceAction, action.id).status == "Proposed"


def test_maintenance_runner_rejects_unapproved_commands(tmp_path):
    session = make_session()
    row = create_code_patch_plan(session, user_message="为管理台自维护页面增加更清晰的错误提示", actor="admin", use_llm=False)
    action = session.query(MaintenanceAction).filter_by(session_id=row.id, action_type="code_patch_plan").one()

    with pytest.raises(ValueError, match="not allowed"):
        validate_code_plan_action(
            session,
            action_id=action.id,
            cwd=tmp_path,
            output_dir=tmp_path / "reports",
            selected_commands=["rm -rf data"],
            command_runner=lambda command, cwd, timeout_seconds: CommandResult(command, 0, "", ""),
        )

    assert session.get(MaintenanceAction, action.id).status == "Proposed"


def test_llm_fallback_can_classify_and_extract_natural_sales_order(monkeypatch):
    session = make_session()
    configure_department(session)
    model = session.query(ModelProviderConfig).one()
    set_config(session, "model_api_key", "runtime-secret", is_secret=True)
    model.credential_ref = "config:model_api_key"
    session.commit()

    def fake_call_model(session, config, *, task_type, messages, related_object_type=None, related_object_id=None):
        if task_type == "MailClassificationFallback":
            content = dumps({"classification": "SalesOrderRequirement", "confidence": 93, "reason": "自然语言订单需求"})
        else:
            content = dumps(
                {
                    "customer_name": "武汉大学",
                    "product_summary": "G100",
                    "quantity_text": "50套",
                    "expected_delivery_date": "2026-10-20",
                    "external_order_no": "SO-NL-001",
                }
            )
        return {"choices": [{"message": {"content": content}}]}

    monkeypatch.setattr("backend.app.services.llm_fallback.call_model", fake_call_model)
    mail = create_inbound_mail(
        session,
        from_address="sales@jimuyida.com",
        subject="请处理",
        body_text="武汉大学那边的新项目是G100五十套，2026-10-20前交付，编号SO-NL-001。",
    )
    assert mail.classification == "NonTarget"
    session.add(ProcessingJob(job_type="process_inbound_mail", payload_json=dumps({"mail_id": mail.id}), status="Pending"))
    session.commit()

    result = run_pending_jobs(session)
    session.commit()

    ack = session.query(OutboundMailJob).filter_by(mail_type="SalesReceiptAck").one()
    version = session.query(ProductionTaskVersion).one()
    assert result["completed"] == 1
    assert mail.classification == "SalesOrderRequirement"
    assert version.task.requirement.customer_name == "武汉大学"
    assert version.task.requirement.product_summary == "G100"
    assert as_list(ack.to_json) == ["sales@jimuyida.com"]


def test_llm_fallback_non_target_is_ignored_without_exception(monkeypatch):
    session = make_session()
    model = session.query(ModelProviderConfig).one()
    set_config(session, "model_api_key", "runtime-secret", is_secret=True)
    model.credential_ref = "config:model_api_key"
    session.commit()

    def fake_call_model(session, config, *, task_type, messages, related_object_type=None, related_object_id=None):
        return {"choices": [{"message": {"content": dumps({"classification": "NonTarget", "confidence": 91, "reason": "与订单沟通无关"})}}]}

    monkeypatch.setattr("backend.app.services.llm_fallback.call_model", fake_call_model)
    mail = create_inbound_mail(session, from_address="someone@example.com", subject="午餐", body_text="今天吃什么？")

    process_mail_direct(session, mail)
    session.commit()

    assert session.query(ExceptionCase).filter_by(exception_type="NonTarget").count() == 0
    audit = session.query(AuditEvent).filter_by(event_type="NonTargetMailIgnored", related_object_id=mail.id).one()
    detail = loads(audit.detail, {})
    assert detail["llm_classification"] == "NonTarget"
    assert detail["llm_reason"] == "与订单沟通无关"


def test_legacy_order_cancel_mail_without_task_is_ignored_without_exception():
    session = make_session()
    mail = create_inbound_mail(
        session,
        from_address="sales@jimuyida.com",
        subject="取消订单 PT-20260511-0002",
        body_text="取消订单 PT-20260511-0002，请暂停处理。",
    )
    mail.classification = "OrderCancelRequest"
    mail.classification_confidence = 99

    result = process_mail_direct(session, mail)
    session.commit()

    assert result is None
    assert session.query(ExceptionCase).filter_by(exception_type="OrderCancelTaskLinkFailed").count() == 0
    assert session.query(OutboundMailJob).filter_by(mail_type="SalesDemandWithdrawRejected").count() == 0
    audit = session.query(AuditEvent).filter_by(event_type="LegacyMailOrderMutationIgnored", related_object_id=mail.id).one()
    detail = loads(audit.detail, {})
    assert detail["classification"] == "OrderCancelRequest"


def test_source_mail_exceptions_are_merged_into_one_record():
    session = make_session()
    mail = create_inbound_mail(session, from_address="sales@jimuyida.com", subject="异常合并", body_text="测试")

    record_exception_case(
        session,
        exception_type="ReviewNeedManual",
        severity="Medium",
        detail={"source_mail_id": mail.id, "missing_fields": ["期望交期"]},
        source_mail_id=mail.id,
    )
    record_exception_case(
        session,
        exception_type="AttachmentParseFailed",
        severity="High",
        detail={"source_mail_id": mail.id, "attachment_id": "att-1", "error": "解析失败"},
        source_mail_id=mail.id,
    )
    session.commit()

    case = session.query(ExceptionCase).one()
    detail = loads(case.detail, {})
    assert case.exception_type == "MailExceptions"
    assert case.severity == "High"
    assert detail["source_mail_id"] == mail.id
    assert detail["missing_fields"] == ["期望交期"]
    assert detail["exception_types"] == ["AttachmentParseFailed", "ReviewNeedManual"]
    assert len(detail["exceptions"]) == 2


def test_production_question_sales_reply_reissue_flow(monkeypatch):
    session = make_session()
    configure_department(session)
    set_config(session, "bot_enabled", "true", is_secret=False)
    set_config(session, "bot_email_password", "runtime-secret", is_secret=True)
    task = create_valid_task(session, order_no="SO-QUESTION-001")
    original_issue = approve_task(session, task.id, actor="tester")
    session.commit()

    production_message = EmailMessage()
    production_message["From"] = "生产部 <production@jimuyida.com>"
    production_message["To"] = "bot.market@jimuyida.com"
    production_message["Subject"] = f"生产疑问 - {task.task_no}"
    production_message["Message-ID"] = "<production-question@jimuyida.com>"
    production_message.set_content("请确认表面处理和最终交期，当前信息不足。")
    production_mail = store_incoming_email(session, parse_email_bytes(production_message.as_bytes()))
    session.add(ProcessingJob(job_type="process_inbound_mail", payload_json=dumps({"mail_id": production_mail.id}), status="Pending"))
    session.commit()

    question_result = run_pending_jobs(session)
    session.commit()
    question = session.query(QuestionAndReply).filter_by(task_id=task.id).one()
    forward = session.query(OutboundMailJob).filter_by(related_task_id=task.id, mail_type="ProductionQuestionForward").one()

    assert question_result["completed"] == 1
    assert task.status == "ProductionQuestioned"
    assert question.status == "AwaitingSalesReply"
    assert as_list(forward.to_json) == ["sales@jimuyida.com"]
    assert "请确认表面处理" in forward.body

    sales_message = EmailMessage()
    sales_message["From"] = "销售 <sales@jimuyida.com>"
    sales_message["To"] = "bot.market@jimuyida.com"
    sales_message["Subject"] = f"答复生产疑问 - {task.task_no}"
    sales_message["Message-ID"] = "<sales-reply@jimuyida.com>"
    sales_message.set_content(
        "\n".join(
            [
                "答复如下：",
                "产品：积木展示架 A1 哑光版",
                "期望交期：2026-05-22",
            ]
        )
    )
    sales_mail = store_incoming_email(session, parse_email_bytes(sales_message.as_bytes()))
    session.add(ProcessingJob(job_type="process_inbound_mail", payload_json=dumps({"mail_id": sales_mail.id}), status="Pending"))
    session.commit()

    reply_result = run_pending_jobs(session)
    session.commit()
    version = session.query(ProductionTaskVersion).filter_by(task_id=task.id, version_no=2).one()

    assert reply_result["completed"] == 1
    assert question.status == "Answered"
    assert task.status == "Reissued"
    assert task.current_version_no == 2
    assert task.requirement.product_summary == "积木展示架 A1 哑光版"
    assert task.requirement.expected_delivery_date == "2026-05-22"
    assert "销售补充答复" in version.body

    reissue_job = session.query(OutboundMailJob).filter_by(related_task_id=task.id, mail_type="SalesReplyTaskReissue").one()
    assert as_list(reissue_job.to_json) == ["production@jimuyida.com"]
    assert "积木展示架 A1 哑光版" in reissue_job.body
    assert original_issue.status == "Pending"

    class FakeSMTP:
        sent_subjects = []

        def __init__(self, host, port, **kwargs):
            pass

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, traceback):
            return False

        def login(self, username, password):
            assert password == "runtime-secret"

        def send_message(self, msg, from_addr, to_addrs):
            self.sent_subjects.append(msg["Subject"])

    monkeypatch.setattr("backend.app.services.mail_adapter.smtplib.SMTP_SSL", FakeSMTP)
    send_result = send_outbound_jobs_smtp(session, [reissue_job.id])
    session.commit()

    sales_receipt = session.query(OutboundMailJob).filter_by(mail_type="SalesReplyReissueReceipt").one()
    sales_reply_ack = (
        session.query(OutboundMailJob)
        .filter_by(mail_type="SalesReceiptAck", subject=f"Re: 答复生产疑问 - {task.task_no}")
        .one()
    )
    assert send_result == {"sent": 1, "failed": 0, "total": 1}
    assert reissue_job.status == "Sent"
    assert sales_reply_ack.status == "Pending"
    assert sales_receipt.status == "Pending"
    assert FakeSMTP.sent_subjects == [reissue_job.subject]
    assert original_issue.status == "Pending"
    assert "[已重新下达]" in sales_receipt.subject
    assert "已更新生产任务单并成功重新发送给生产部" in sales_receipt.body


def test_sales_reply_after_conversation_closed_is_rejected_without_reissue():
    session = make_session()
    configure_department(session)
    task = create_valid_task(session, order_no="SO-CLOSED-REPLY-001")
    task.status = "Closed"
    task.closed_reason = "ConversationMaxRounds"
    task.requirement.status = "Closed"
    task.current_version_no = 1
    session.commit()

    reply = create_inbound_mail(
        session,
        from_address="sales@jimuyida.com",
        subject=f"答复生产疑问 - {task.task_no}",
        body_text="答复如下：产品：关闭后不应重发",
    )
    result = process_mail_direct(session, reply)
    session.commit()

    reject = session.query(OutboundMailJob).filter_by(mail_type="ClosedTaskReplyRejected", related_task_id=task.id).one()
    assert result == reject
    assert task.status == "Closed"
    assert task.current_version_no == 1
    assert session.query(OutboundMailJob).filter_by(mail_type="SalesReplyTaskReissue", related_task_id=task.id).count() == 0
    assert "已关闭" in reject.body


def test_sales_reply_without_open_question_does_not_reissue_task():
    session = make_session()
    configure_department(session)
    task = create_valid_task(session, order_no="SO-NO-OPEN-QUESTION")
    task.status = "TaskIssued"
    task.current_version_no = 1
    session.commit()

    reply = create_inbound_mail(
        session,
        from_address="sales@jimuyida.com",
        subject=f"答复生产疑问 - {task.task_no}",
        body_text="答复如下：产品：没有待答复疑问时不应重发",
    )
    result = process_mail_direct(session, reply)
    session.commit()

    notice = session.query(OutboundMailJob).filter_by(mail_type="SalesReplyNoOpenQuestion", related_task_id=task.id).one()
    case = session.query(ExceptionCase).filter_by(exception_type="SalesReplyWithoutOpenQuestion", related_task_id=task.id).one()
    assert result == notice
    assert case.related_task_id == task.id
    assert task.status == "TaskIssued"
    assert task.current_version_no == 1
    assert session.query(ProductionTaskVersion).filter_by(task_id=task.id, version_no=2).count() == 0
    assert session.query(OutboundMailJob).filter_by(mail_type="SalesReplyTaskReissue", related_task_id=task.id).count() == 0


def test_exception_patch_can_recover_missing_fields_to_task_draft():
    session = make_session()
    configure_department(session)
    mail = create_inbound_mail(
        session,
        from_address="sales@jimuyida.com",
        subject="生产订单需求 - 缺字段客户",
        body_text="\n".join(
            [
                "客户名称：缺字段客户",
                "产品：移动展示墙",
                "期望交期：2026-06-20",
                "订单号：SO-MISSING-001",
            ]
        ),
    )
    task = create_task_from_mail(session, mail)
    session.commit()
    case = session.query(ExceptionCase).filter_by(exception_type="ReviewNeedManual").one()

    assert task is None
    assert case.status == "Open"

    recovered = apply_exception_requirement_patch(session, case.id, {"quantity_text": "48套"})
    session.commit()

    assert recovered is not None
    assert recovered.status == "TaskIssued"
    assert recovered.requirement.quantity_text == "48套"
    assert case.status == "Resolved"


def test_weekly_report_enqueue_uses_configured_recipients_and_is_idempotent():
    session = make_session()
    configure_department(session)
    task = create_valid_task(session, order_no="SO-REPORT-001")
    approve_task(session, task.id, actor="tester")
    set_weekly_report_recipients(
        session,
        ["finance@jimuyida.com", "sales-director@jimuyida.com"],
        ["dingyong@jimuyida.com"],
    )
    session.commit()

    first = enqueue_weekly_report(session)
    second = enqueue_weekly_report(session)
    session.commit()

    recipients = weekly_report_recipients(session)

    assert first.id == second.id
    assert first.mail_type == "WeeklyReport"
    assert as_list(first.to_json) == ["finance@jimuyida.com", "sales-director@jimuyida.com"]
    assert as_list(first.cc_json) == ["dingyong@jimuyida.com"]
    assert "一、任务统计" in first.body
    assert "本次上报周期：本周" in first.body
    assert "生成时间：" in first.body
    assert "统计周期：" in first.body
    assert "北京时间" in first.body
    assert "二、已确认物料订单统计（分物料）" in first.body
    assert "三、未确认物料订单统计（分物料）" in first.body
    assert "四、销售 Top10 统计（需求总数和已确认总数）" in first.body
    assert "待处理异常" not in first.body
    assert "待发送邮件" not in first.body
    assert recipients["to"] == ["finance@jimuyida.com", "sales-director@jimuyida.com"]


def test_manual_weekly_report_enqueue_creates_new_outbound_each_click():
    session = make_session()
    set_weekly_report_recipients(session, ["finance@jimuyida.com"], ["dingyong@jimuyida.com"])
    session.commit()

    first = enqueue_weekly_report(session, force_new=True)
    second = enqueue_weekly_report(session, force_new=True)
    session.commit()

    assert first.id != second.id
    assert first.status == "Pending"
    assert second.status == "Pending"
    assert session.query(OutboundMailJob).filter_by(mail_type="WeeklyReport").count() == 2
    assert "本次上报周期：本周" in first.body
    assert "北京时间" in first.body
    assert "发送失败邮件" not in first.body
    assert "变更/取消待确认" not in first.body
    assert "风险/异常摘要" not in first.body


def test_smtp_send_marks_success_failure_and_retry(monkeypatch):
    session = make_session()
    set_config(session, "bot_enabled", "true", is_secret=False)
    set_config(session, "bot_email_password", "runtime-secret", is_secret=True)
    ok = OutboundMailJob(
        mail_type="Manual",
        to_json=dumps(["ok@jimuyida.com"]),
        cc_json=dumps([]),
        subject="OK",
        body="hello",
        idempotency_key="smtp-ok",
        status="Pending",
    )
    missing_recipient = OutboundMailJob(
        mail_type="Manual",
        to_json=dumps([]),
        cc_json=dumps([]),
        subject="NO-RECIPIENT",
        body="hello",
        idempotency_key="smtp-no-recipient",
        status="Pending",
    )
    send_failure = OutboundMailJob(
        mail_type="Manual",
        to_json=dumps(["fail@jimuyida.com"]),
        cc_json=dumps([]),
        subject="FAIL",
        body="hello",
        idempotency_key="smtp-fail",
        status="Pending",
    )
    session.add_all([ok, missing_recipient, send_failure])
    session.commit()

    class FakeSMTP:
        sent_messages = []

        def __init__(self, host, port, **kwargs):
            self.host = host
            self.port = port

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, traceback):
            return False

        def login(self, username, password):
            assert username == "bot.market@jimuyida.com"
            assert password == "runtime-secret"

        def send_message(self, msg, from_addr, to_addrs):
            if msg["Subject"] == "FAIL":
                raise RuntimeError("smtp send failed")
            self.sent_messages.append((msg["Subject"], from_addr, to_addrs))

    monkeypatch.setattr("backend.app.services.mail_adapter.smtplib.SMTP_SSL", FakeSMTP)

    result = send_pending_smtp(session, limit=10)
    session.commit()

    assert result["sent"] == 1
    assert result["failed"] == 0
    assert result["total"] == 1
    assert ok.status == "Sent"
    assert missing_recipient.status == "Pending"
    assert send_failure.status == "Pending"
    assert FakeSMTP.sent_messages == [("OK", "bot.market@jimuyida.com", ["ok@jimuyida.com"])]

    reset_mail_login_throttle()
    reset_db_mail_throttle(session)
    result_missing = send_pending_smtp(session, limit=10)
    session.commit()

    assert result_missing["sent"] == 0
    assert result_missing["failed"] == 1
    assert result_missing["total"] == 1
    assert missing_recipient.status == "Failed"
    assert session.query(ExceptionCase).filter_by(exception_type="OutboundMailSendFailed").count() == 1

    reset_mail_login_throttle()
    reset_db_mail_throttle(session)
    result_failure = send_pending_smtp(session, limit=10)
    session.commit()

    assert result_failure["sent"] == 0
    assert result_failure["failed"] == 1
    assert result_failure["total"] == 1
    assert send_failure.status == "Pending"
    assert send_failure.attempt_count == 1
    assert send_failure.next_retry_at is not None

    # 手动清除 next_retry_at 以模拟超过冷却期
    send_failure.next_retry_at = None
    # 模拟多次失败达到最大重试上限
    from backend.app.services.mail_adapter import OUTBOUND_MAX_AUTO_RETRIES
    send_failure.attempt_count = OUTBOUND_MAX_AUTO_RETRIES
    session.commit()

    reset_mail_login_throttle()
    reset_db_mail_throttle(session)
    result2 = send_pending_smtp(session, limit=10)
    session.commit()

    # 达到最大重试次数，send_failure 转为 Failed，产生第2条 ExceptionCase
    assert result2["sent"] == 0
    assert send_failure.status == "Failed"
    assert send_failure.attempt_count == OUTBOUND_MAX_AUTO_RETRIES + 1
    # missing_recipient(1条) + send_failure超限(1条) = 共2条
    assert session.query(ExceptionCase).filter_by(exception_type="OutboundMailSendFailed").count() == 2

    retried = retry_outbound_mail(session, missing_recipient.id)
    session.commit()

    assert retried.status == "Pending"


def test_smtp_send_uses_non_blocking_throttle_and_timeout(monkeypatch):
    session = make_session()
    set_config(session, "bot_enabled", "true", is_secret=False)
    set_config(session, "bot_email_password", "runtime-secret", is_secret=True)
    first = OutboundMailJob(
        mail_type="Manual",
        to_json=dumps(["first@jimuyida.com"]),
        cc_json=dumps([]),
        subject="FIRST",
        body="hello",
        idempotency_key="smtp-first-nonblocking",
        status="Pending",
    )
    second = OutboundMailJob(
        mail_type="Manual",
        to_json=dumps(["second@jimuyida.com"]),
        cc_json=dumps([]),
        subject="SECOND",
        body="hello",
        idempotency_key="smtp-second-nonblocking",
        status="Pending",
    )
    session.add_all([first, second])
    session.commit()

    class FakeSMTP:
        sent_subjects = []
        timeouts = []

        def __init__(self, host, port, **kwargs):
            self.timeouts.append(kwargs.get("timeout"))

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, traceback):
            return False

        def login(self, username, password):
            assert password == "runtime-secret"

        def send_message(self, msg, from_addr, to_addrs):
            self.sent_subjects.append(msg["Subject"])

    monkeypatch.setattr("backend.app.services.mail_adapter.smtplib.SMTP_SSL", FakeSMTP)

    result = send_pending_smtp(session, limit=10)
    throttled = send_pending_smtp(session, limit=10)
    session.commit()

    assert result == {"sent": 1, "failed": 0, "total": 1}
    assert throttled["sent"] == 0
    assert throttled["total"] == 0
    assert "throttled_until" in throttled
    assert first.status == "Sent"
    assert second.status == "Pending"
    assert second.next_retry_at is not None
    assert FakeSMTP.sent_subjects == ["FIRST"]
    assert FakeSMTP.timeouts == [30]


def test_stale_sending_outbound_moves_to_send_unknown_and_can_retry():
    session = make_session()
    set_config(session, "bot_enabled", "true", is_secret=False)
    set_config(session, "bot_email_password", "runtime-secret", is_secret=True)
    stale = OutboundMailJob(
        mail_type="Manual",
        to_json=dumps(["sales@jimuyida.com"]),
        cc_json=dumps([]),
        subject="STALE",
        body="hello",
        idempotency_key="smtp-stale-sending",
        status="Sending",
        locked_by="old-worker",
        locked_until=now_utc() - timedelta(minutes=10),
        sending_started_at=now_utc() - timedelta(minutes=15),
    )
    session.add(stale)
    session.commit()

    result = send_pending_smtp(session, limit=10)
    session.commit()

    assert result == {"sent": 0, "failed": 0, "total": 0}
    assert stale.status == "SendUnknown"
    assert stale.locked_by is None
    assert session.query(ExceptionCase).filter_by(exception_type="OutboundMailSendUnknown").count() == 1

    retried = retry_outbound_mail(session, stale.id)
    session.commit()
    assert retried.status == "Pending"
    assert retried.attempt_count == 0


def test_stale_processing_job_returns_to_pending_then_runs():
    session = make_session()
    stale = ProcessingJob(
        job_type="process_inbound_mail",
        payload_json=dumps({"mail_id": "missing"}),
        status="Running",
        attempt_count=1,
        locked_by="old-worker",
        locked_until=now_utc() - timedelta(minutes=30),
        started_at=now_utc() - timedelta(minutes=40),
    )
    session.add(stale)
    session.commit()

    result = run_pending_jobs(session, limit=10)
    session.commit()

    assert result == {"completed": 0, "failed": 1, "total": 1}
    assert stale.status == "Failed"
    assert stale.attempt_count == 2
    assert stale.version == 3
    assert stale.locked_by is None


def test_task_scheduler_applies_exponential_backoff_cap_and_jitter():
    now = datetime(2026, 6, 13, tzinfo=timezone.utc)
    scheduler = TaskScheduler(RetryPolicy(base_delay_seconds=60, multiplier=3, max_delay_seconds=500, jitter_seconds=0))
    assert scheduler.delay_seconds(1) == 60
    assert scheduler.delay_seconds(3) == 500
    assert scheduler.next_retry_at(2, now=now) == now + timedelta(seconds=180)

    jittered = TaskScheduler(RetryPolicy(base_delay_seconds=60, multiplier=3, max_delay_seconds=None, jitter_seconds=5))
    assert 55 <= jittered.delay_seconds(1) <= 65


def test_smtp_send_skips_when_bot_disabled():
    session = make_session()
    set_config(session, "bot_enabled", "false", is_secret=False)
    set_config(session, "bot_email_password", "runtime-secret", is_secret=True)
    pending = OutboundMailJob(
        mail_type="Manual",
        to_json=dumps(["sales@jimuyida.com"]),
        cc_json=dumps([]),
        subject="BOT DISABLED",
        body="hello",
        idempotency_key="smtp-bot-disabled",
        status="Pending",
    )
    session.add(pending)
    session.commit()

    result = send_pending_smtp(session, limit=10)
    session.commit()

    assert result == {"sent": 0, "failed": 0, "total": 0, "skipped": "bot is disabled"}
    assert pending.status == "Pending"


def test_outbound_mail_detail_includes_full_message_body():
    from backend.app.main import outbound_mail_detail

    session = make_session()
    job = OutboundMailJob(
        mail_type="TaskIssue",
        to_json=dumps(["bot.production@jimuyida.com"]),
        cc_json=dumps(["dingyong@jimuyida.com"]),
        subject="测试外发详情",
        body="这里是完整邮件正文\n包含第二行内容",
        related_task_id="PT-DETAIL",
        idempotency_key="outbound-detail",
        status="Sent",
    )
    session.add(job)
    session.commit()

    detail = outbound_mail_detail(job.id, session)

    assert detail["id"] == job.id
    assert detail["body"] == "这里是完整邮件正文\n包含第二行内容"
    assert detail["to"] == ["bot.production@jimuyida.com"]
    assert detail["cc"] == ["dingyong@jimuyida.com"]
    assert detail["related_task_id"] == "PT-DETAIL"


def test_outbound_list_explains_pending_when_bot_disabled():
    from backend.app.main import outbound_mails

    session = make_session()
    set_config(session, "bot_enabled", "false", is_secret=False)
    pending = OutboundMailJob(
        mail_type="WeeklyReport",
        to_json=dumps(["finance@jimuyida.com"]),
        cc_json=dumps([]),
        subject="待发送周报",
        body="hello",
        idempotency_key="diagnose-pending-disabled",
        status="Pending",
    )
    session.add(pending)
    session.commit()

    result = outbound_mails(status="Pending", page=1, page_size=10, session=session)
    diagnosis = result["items"][0]["pending_diagnosis"]

    assert diagnosis["severity"] == "blocked"
    assert "系统已停用" in diagnosis["reason"]
    assert diagnosis["queue_position"] == 1
    assert diagnosis["auto_dispatchable"] is True


def test_outbound_list_marks_non_auto_pending_as_manual_only():
    from backend.app.main import outbound_mails

    session = make_session()
    set_config(session, "bot_enabled", "true", is_secret=False)
    set_config(session, "bot_email_password", "runtime-secret", is_secret=True)
    pending = OutboundMailJob(
        mail_type="ManualReviewNotice",
        to_json=dumps(["ops@jimuyida.com"]),
        cc_json=dumps([]),
        subject="人工通知",
        body="hello",
        idempotency_key="diagnose-pending-manual",
        status="Pending",
    )
    session.add(pending)
    session.commit()

    result = outbound_mails(status="Pending", page=1, page_size=10, session=session)
    diagnosis = result["items"][0]["pending_diagnosis"]

    assert diagnosis["severity"] == "manual"
    assert "不在自动 worker 消费范围" in diagnosis["reason"]
    assert diagnosis["auto_dispatchable"] is False


def test_outbound_diagnostics_reports_failed_jobs_and_failure_types():
    from backend.app.main import outbound_mail_diagnostics
    from backend.app.services.mail_adapter import mark_outbound_failure

    session = make_session()
    failed = OutboundMailJob(
        mail_type="TaskIssue",
        to_json=dumps(["production@jimuyida.com"]),
        cc_json=dumps([]),
        subject="失败任务单",
        body="hello",
        idempotency_key="diagnostics-failed",
        status="Pending",
    )
    session.add(failed)
    session.flush()
    mark_outbound_failure(session, failed, "smtp send failed")
    session.commit()

    result = outbound_mail_diagnostics(hours=24, limit=10, session=session)

    assert result["status_counts"]["Failed"] == 1
    assert result["failed_by_type"][0] == {"mail_type": "TaskIssue", "count": 1}
    assert result["recent_failures"][0]["error"] == "smtp send failed"
    assert result["dead_letters"][0]["id"] == failed.id
    assert result["alerts"][0]["type"] == "outbound_failed_threshold"


def test_outbound_diagnostics_csv_exports_failures():
    from backend.app.main import outbound_mail_diagnostics_csv
    from backend.app.services.mail_adapter import mark_outbound_failure

    session = make_session()
    failed = OutboundMailJob(
        mail_type="WeeklyReport",
        to_json=dumps(["finance@jimuyida.com"]),
        cc_json=dumps(["ops@jimuyida.com"]),
        subject="失败周报",
        body="hello",
        idempotency_key="diagnostics-csv-failed",
        status="Pending",
    )
    session.add(failed)
    session.flush()
    mark_outbound_failure(session, failed, "smtp export failed")
    session.commit()

    response = outbound_mail_diagnostics_csv(hours=24, limit=100, session=session)
    body = response.body.decode("utf-8")

    assert response.media_type.startswith("text/csv")
    assert "recent_failure" in body
    assert "dead_letter" in body
    assert "smtp export failed" in body
    assert "finance@jimuyida.com" in body


def test_outbound_diagnostics_notify_queues_idempotent_alert_mail():
    from backend.app.main import notify_outbound_diagnostics
    from backend.app.services.mail_adapter import mark_outbound_failure

    class DummyState:
        username = "tester"

    class DummyRequest:
        state = DummyState()

    session = make_session()
    set_config(session, "ops_cc_email", "ops@jimuyida.com", is_secret=False)
    failed = OutboundMailJob(
        mail_type="TaskIssue",
        to_json=dumps(["production@jimuyida.com"]),
        cc_json=dumps([]),
        subject="失败任务单",
        body="hello",
        idempotency_key="diagnostics-notify-failed",
        status="Pending",
    )
    session.add(failed)
    session.flush()
    mark_outbound_failure(session, failed, "smtp notify failed")
    session.commit()

    first = notify_outbound_diagnostics(DummyRequest(), hours=24, session=session)
    second = notify_outbound_diagnostics(DummyRequest(), hours=24, session=session)

    assert first["queued"] is True
    assert first["to"] == ["ops@jimuyida.com"]
    assert session.get(OutboundMailJob, first["outbound_job_id"]).mail_type == "OutboundAlert"
    assert second["queued"] is False
    assert second["reason"] == "already queued in this hour"


def test_cancel_pending_outbound_marks_only_matching_pending_jobs():
    from backend.app.main import cancel_pending_outbound
    from backend.app.schemas import OutboundBulkCancelRequest

    class Request:
        class State:
            username = "tester"

        state = State()

    session = make_session()
    matched = OutboundMailJob(
        mail_type="WeeklyReport",
        to_json=dumps(["finance@jimuyida.com"]),
        cc_json=dumps([]),
        subject="周报-Pending",
        body="hello",
        idempotency_key="cancel-matched",
        status="Pending",
    )
    other_pending = OutboundMailJob(
        mail_type="TaskIssue",
        to_json=dumps(["production@jimuyida.com"]),
        cc_json=dumps([]),
        subject="任务-Pending",
        body="hello",
        idempotency_key="cancel-other",
        status="Pending",
    )
    sent = OutboundMailJob(
        mail_type="WeeklyReport",
        to_json=dumps(["finance@jimuyida.com"]),
        cc_json=dumps([]),
        subject="周报-Sent",
        body="hello",
        idempotency_key="cancel-sent",
        status="Sent",
    )
    session.add_all([matched, other_pending, sent])
    session.commit()

    result = cancel_pending_outbound(
        OutboundBulkCancelRequest(mail_type="WeeklyReport"),
        Request(),
        session,
    )

    assert result["cancelled"] == 1
    assert matched.status == "Cancelled"
    assert other_pending.status == "Pending"
    assert sent.status == "Sent"
    audit = session.query(AuditEvent).filter_by(event_type="OutboundMailCancelled", related_object_id=matched.id).one()
    assert audit.actor == "tester"


def test_clear_tasks_requires_admin_password_and_removes_task_list():
    from backend.app.main import clear_tasks
    from backend.app.schemas import TaskClearRequest

    class Request:
        class State:
            username = "admin"

        state = State()

    session = make_session()
    configure_department(session)
    task = create_valid_task(session, order_no="SO-CLEAR")
    version = session.query(ProductionTaskVersion).filter_by(task_id=task.id).first()
    assert version is not None
    source_mail = session.get(MailMessage, task.requirement.source_mail_id)
    source_mail.related_task_id = task.id
    question = QuestionAndReply(
        task_id=task.id,
        question_text="请补充包装要求",
        status="AwaitingSalesReply",
    )
    outbound = OutboundMailJob(
        related_task_id=task.id,
        related_version_id=version.id,
        mail_type="TaskIssue",
        to_json=dumps(["production@jimuyida.com"]),
        cc_json=dumps([]),
        subject="任务单",
        body="hello",
        idempotency_key="clear-task-outbound",
        status="Pending",
    )
    case = ExceptionCase(
        related_task_id=task.id,
        exception_type="ManualReview",
        severity="Medium",
        detail="测试异常",
        status="Open",
    )
    session.add_all([source_mail, question, outbound, case])
    session.commit()
    task_id = task.id
    requirement_id = task.requirement_id
    source_mail_id = source_mail.id
    outbound_id = outbound.id
    case_id = case.id

    with pytest.raises(Exception) as exc:
        clear_tasks(TaskClearRequest(admin_password="wrong"), Request(), session)
    assert exc.value.status_code == 403
    assert session.query(ProductionTask).count() == 1

    result = clear_tasks(TaskClearRequest(admin_password="admin"), Request(), session)
    session.expire_all()

    assert result["cleared"] == 1
    assert session.query(ProductionTask).count() == 0
    assert session.query(ProductionTaskVersion).filter_by(task_id=task_id).count() == 0
    assert session.query(QuestionAndReply).filter_by(task_id=task_id).count() == 0
    assert session.query(OrderRequirement).filter_by(id=requirement_id).count() == 0
    assert session.query(RequirementWorkflowBinding).filter_by(requirement_id=requirement_id).count() == 0
    assert session.query(ExtractionEvidence).filter_by(requirement_id=requirement_id).count() == 0
    assert session.get(MailMessage, source_mail_id).related_task_id is None
    assert session.get(OutboundMailJob, outbound_id).related_task_id is None
    assert session.get(OutboundMailJob, outbound_id).related_version_id is None
    assert session.get(ExceptionCase, case_id).related_task_id is None
    audit = session.query(AuditEvent).filter_by(event_type="TaskListCleared").one()
    assert audit.actor == "admin"


def test_clear_business_data_removes_entered_flows_tasks_and_review_rules():
    from backend.app.main import clear_business_data
    from backend.app.schemas import AdminPasswordRequest

    class Request:
        class State:
            username = "admin"

        state = State()

    session = make_session()
    configure_department(session)
    set_config(
        session,
        "initial_review_rules_json",
        dumps(
            [
                {
                    "id": "custom-review-rule",
                    "name": "自定义规则",
                    "field": "source_text",
                    "operator": "contains",
                    "value": "测试",
                    "message": "必须包含测试",
                    "enabled": True,
                }
            ]
        ),
        is_secret=False,
    )
    set_config(session, "initial_review_workflow_rule_deleted_ids_json", dumps(["workflow:old"]), is_secret=False)
    import_result = import_structured_workflow_rules(
        session,
        rules=[
            {
                "workflow_code": "clear_business_flow",
                "workflow_name": "清空数据流程",
                "match": {"any_keywords": ["清空数据"], "all_keywords": [], "warehouse": "", "order_type": "", "subject_patterns": []},
                "routing": {"to_names": ["production@jimuyida.com"], "cc_names": []},
                "subject_template": "任务 {{task_no}}",
                "body_template": "流程类型：{{workflow_name}}",
                "required_fields": ["customer_name", "product_summary", "quantity_text", "expected_delivery_date"],
                "review_rules": [],
            }
        ],
        auto_publish=True,
        source_asset_ref="clear-business",
        file_name="clear-business.json",
    )
    version_id = import_result["created_versions"][0]["id"]
    task = create_valid_task(session, order_no="SO-CLEAR-BUSINESS")
    source_mail = session.get(MailMessage, task.requirement.source_mail_id)
    source_mail.related_task_id = task.id
    session.add(
        MailWorkflowMatch(
            mail_id=source_mail.id,
            workflow_version_id=version_id,
            workflow_code="clear_business_flow",
            confidence=100,
            match_detail_json=dumps({"workflow_name": "清空数据流程"}),
        )
    )
    session.commit()

    result = clear_business_data(AdminPasswordRequest(admin_password="admin"), Request(), session)
    session.expire_all()

    assert result["ok"] is True
    assert result["task_count"] == 1
    assert result["workflow_count"] == 1
    assert result["initial_review_rule_count"] == 1
    assert session.query(ProductionTask).count() == 0
    assert session.query(OrderRequirement).count() == 0
    assert session.query(WorkflowDefinition).count() == 0
    assert session.query(WorkflowVersion).count() == 0
    assert session.query(WorkflowImportJob).count() == 0
    assert session.query(MailWorkflowMatch).count() == 0
    assert loads(session.get(SystemConfig, "initial_review_rules_json").value, []) == []
    assert loads(session.get(SystemConfig, "initial_review_workflow_rule_deleted_ids_json").value, []) == []
    display_config = initial_review_config(session, include_workflow_rules=True)
    assert [rule["id"] for rule in display_config["rules"]] == [
        "builtin-required-core-fields",
        "builtin-parser-risk-flags",
        "builtin-duplicate-submission",
    ]
    assert any(row.get("is_builtin") for row in list_workflow_rules(session, only_active=False))
    audit = session.query(AuditEvent).filter_by(event_type="BusinessDataCleared").one()
    assert audit.actor == "admin"


def test_clear_exception_and_ops_lists_require_admin_password():
    from backend.app.main import clear_attachments, clear_audit_events, clear_backups, clear_exceptions, clear_jobs
    from backend.app.schemas import AdminPasswordRequest

    class Request:
        class State:
            username = "admin"

        state = State()

    session = make_session()
    mail = create_inbound_mail(
        session,
        from_address="sales@jimuyida.com",
        subject="附件测试",
        body_text="测试正文",
    )
    attachment = AttachmentAsset(
        mail_id=mail.id,
        file_name="order.docx",
        file_size=12,
        file_hash="hash-clear",
        storage_ref="data/attachments/order.docx",
        parse_status="Completed",
    )
    session.add(attachment)
    session.flush()
    session.add_all(
        [
            ProcessingJob(job_type="process_inbound_mail", payload_json=dumps({"mail_id": mail.id}), status="Pending"),
            ExceptionCase(exception_type="ReviewNeedManual", severity="Medium", detail="缺少字段", status="Open"),
            BackupJob(backup_type="Manual", status="Completed", storage_ref="data/backups/test.zip", manifest_json=dumps({})),
            ExtractionEvidence(
                requirement_id="not-used-in-this-test",
                field_name="customer_name",
                field_value="测试客户",
                source_type="attachment",
                source_attachment_id=attachment.id,
                evidence_text="客户名称：测试客户",
                confidence=90,
            ),
        ]
    )
    session.commit()

    with pytest.raises(Exception) as exc:
        clear_exceptions(AdminPasswordRequest(admin_password="wrong"), Request(), session)
    assert exc.value.status_code == 403
    assert session.query(ExceptionCase).count() == 1

    assert clear_exceptions(AdminPasswordRequest(admin_password="admin"), Request(), session)["cleared"] == 1
    assert clear_jobs(AdminPasswordRequest(admin_password="admin"), Request(), session)["cleared"] == 1
    attachment_result = clear_attachments(AdminPasswordRequest(admin_password="admin"), Request(), session)
    assert attachment_result["cleared"] == 1
    assert attachment_result["evidence_links_cleared"] == 1
    assert clear_backups(AdminPasswordRequest(admin_password="admin"), Request(), session)["cleared"] == 1
    assert session.query(ExceptionCase).count() == 0
    assert session.query(ProcessingJob).count() == 0
    assert session.query(AttachmentAsset).count() == 0
    assert session.query(BackupJob).count() == 0
    assert session.query(ExtractionEvidence).filter(ExtractionEvidence.source_attachment_id.isnot(None)).count() == 0
    assert session.query(AuditEvent).filter(AuditEvent.event_type.in_(["ExceptionsCleared", "ProcessingJobsCleared", "AttachmentsCleared", "BackupsCleared"])).count() == 4

    audit_count = session.query(AuditEvent).count()
    audit_result = clear_audit_events(AdminPasswordRequest(admin_password="admin"), session)
    assert audit_result["cleared"] == audit_count
    assert session.query(AuditEvent).count() == 0


def test_exception_lifecycle_assign_resolve_reopen_tracks_sla_and_audit():
    session = make_session()
    case = ExceptionCase(
        exception_type="ReviewNeedManual",
        severity="Critical",
        detail=dumps({"message": "缺少客户资料"}),
        status="Open",
        due_at=now_utc() + timedelta(hours=2),
    )
    session.add(case)
    session.commit()

    assigned = assign_exception(
        case.id,
        ExceptionAssignRequest(assignee="ops@example.com", note="请运营跟进", actor="manager"),
        session,
    )
    assert assigned["status"] == "Assigned"
    assert assigned["assignee"] == "ops@example.com"
    assert assigned["sla_status"] == "due_soon"

    resolved = resolve_exception(case.id, ExceptionResolveRequest(note="CRM 已补齐", actor="ops@example.com"), session)
    assert resolved["status"] == "Resolved"
    assert resolved["resolution_note"] == "CRM 已补齐"
    assert resolved["resolution_evidence"]["type"] == "MANUAL_EXCEPTION_RESOLUTION"
    assert resolved["resolution_evidence"]["evidence_refs"] == ["异常详情：缺少客户资料"]
    assert resolved["resolved_at"]
    assert resolved["sla_status"] == "resolved"

    reopened = reopen_exception(case.id, ExceptionReopenRequest(note="附件仍缺失", actor="manager"), session)
    assert reopened["status"] == "Open"
    assert reopened["resolution_note"] is None
    assert reopened["reopened_at"]
    assert reopened["sla_status"] == "due_soon"

    events = [row.event_type for row in session.query(AuditEvent).filter_by(related_object_type="ExceptionCase", related_object_id=case.id).all()]
    assert events == ["ExceptionAssigned", "ExceptionResolved", "ExceptionReopened"]
    serialized = serialize_exception(session.get(ExceptionCase, case.id))
    assert serialized["last_actor"] == "manager"


def test_high_risk_exception_resolution_requires_confirmation_actor_and_note():
    session = make_session()
    case = ExceptionCase(
        exception_type="CRM_CHANGED_AFTER_OMS_ACCEPTED",
        severity="Critical",
        detail=dumps({"message": "OMS 已接收后 CRM 被编辑"}),
        status="Open",
        due_at=now_utc() + timedelta(hours=2),
    )
    session.add(case)
    session.commit()

    with pytest.raises(Exception) as exc_info:
        resolve_exception(case.id, ExceptionResolveRequest(note="处理", actor="operator"), session)
    assert getattr(exc_info.value, "status_code", None) == 403
    assert session.query(ExceptionCase).filter_by(id=case.id).one().status == "Open"
    audit = session.query(AuditEvent).filter_by(event_type="UNAUTHORIZED_STATE_OVERRIDE", related_object_id=case.id).one()
    assert "责任人身份" in audit.detail

    resolved = resolve_exception(
        case.id,
        ExceptionResolveRequest(note="已核对 OMS 单据并由商务主管确认", actor="ops@example.com", confirm_risk=True),
        session,
    )
    assert resolved["status"] == "Resolved"
    assert resolved["requires_confirmation"] is True
    assert resolved["last_actor"] == "ops@example.com"
    assert resolved["resolution_evidence"]["type"] == "MANUAL_EXCEPTION_RESOLUTION"
    assert resolved["resolution_evidence"]["evidence_refs"] == ["异常详情：OMS 已接收后 CRM 被编辑"]


def test_global_exception_ticker_prioritizes_p0_sla_and_dead_letters():
    session = make_session()
    overdue = ExceptionCase(
        exception_type="CRM_CHANGED_AFTER_OMS_ACCEPTED",
        severity="Critical",
        detail=dumps({"message": "OMS 已接收后 CRM 变更"}),
        status="Open",
        due_at=now_utc() - timedelta(minutes=5),
    )
    medium = ExceptionCase(
        exception_type="ReviewNeedManual",
        severity="Medium",
        detail=dumps({"message": "普通异常"}),
        status="Open",
    )
    failed_job = ProcessingJob(job_type="OMS_PUSH_NOTICE", payload_json=dumps({"notice_id": "demo"}), status="Failed", error_message="OMS timeout")
    failed_mail = OutboundMailJob(mail_type="V2OmsBlocked", to_json=dumps(["ops@example.com"]), cc_json=dumps([]), subject="OMS 阻塞", body="body", status="Failed", idempotency_key="ticker-mail", last_error="SMTP failed")
    session.add_all([overdue, medium, failed_job, failed_mail])
    session.commit()

    items = global_exception_ticker_items(session)

    assert items[0]["type"] == "exception"
    assert items[0]["tone"] == "danger"
    assert items[0]["sla_status"] == "overdue"
    assert any(item["type"] == "processing_dead_letter" and "OMS timeout" in item["message"] for item in items)
    assert any(item["type"] == "outbound_dead_letter" and item["href"] == "#outbound" for item in items)
    assert all("普通异常" not in item["message"] for item in items)


def test_exception_diagnosis_stream_emits_loading_partial_and_done_events():
    session = make_session()
    case = ExceptionCase(
        exception_type="VALIDATION_BLOCKED",
        severity="High",
        detail=dumps({"exception": {"summary": "SKU 映射缺失"}, "validation": {"failed_rules": [{"rule_code": "SKU_MAPPING", "reason": "未找到 SKU"}]}}),
        status="Open",
    )
    session.add(case)
    session.commit()

    payload = "".join(diagnose_exception_stream_chunks(session, case.id, actor="ops@example.com"))
    assert "event: loading" in payload
    assert "event: partial" in payload
    assert "event: done" in payload
    assert "SKU 映射缺失" in payload or "未找到 SKU" in payload


def test_sync_imap_mailbox_skips_when_bot_disabled():
    session = make_session()
    set_config(session, "bot_enabled", "false", is_secret=False)
    result = sync_imap_mailbox(session, limit=10)
    assert result == {"imported": 0, "queued": 0, "skipped": "bot is disabled"}


def test_mail_auto_worker_skips_when_bot_disabled(monkeypatch):
    session = make_session()
    set_config(session, "bot_enabled", "false", is_secret=False)
    session.add(ProcessingJob(job_type="process_inbound_mail", payload_json=dumps({"mail_id": "demo"}), status="Pending"))
    session.commit()

    monkeypatch.setattr("backend.app.services.mail_worker.SessionLocal", lambda: nullcontext(session))
    result = run_mail_auto_worker_once()

    assert result["enabled"] is False
    assert result["synced"]["skipped"] == "bot is disabled"
    assert result["processed"]["skipped"] == "bot is disabled"
    assert session.query(ProcessingJob).filter_by(status="Pending").count() == 1


def test_mail_auto_worker_future_retry_does_not_block_imap_sync(monkeypatch):
    session = make_session()
    set_config(session, "bot_enabled", "true", is_secret=False)
    set_config(session, "bot_email_password", "runtime-secret", is_secret=True)
    future_retry = OutboundMailJob(
        mail_type="SalesReceiptAck",
        to_json=dumps(["sales@jimuyida.com"]),
        cc_json=dumps([]),
        subject="FUTURE RETRY",
        body="hello",
        idempotency_key="future-retry-does-not-block-sync",
        status="Pending",
        next_retry_at=now_utc() + timedelta(minutes=30),
    )
    session.add(future_retry)
    session.commit()

    monkeypatch.setattr("backend.app.services.mail_worker.SessionLocal", lambda: nullcontext(session))
    monkeypatch.setattr(
        "backend.app.services.mail_worker.sync_imap_mailbox",
        lambda sync_session, limit=20: {"imported": 1, "queued": 1},
    )

    result = run_mail_auto_worker_once()

    assert result["synced"] == {"imported": 1, "queued": 1}
    assert result["high_priority_mails"] == {"sent": 0, "failed": 0, "total": 0}
    assert future_retry.status == "Pending"


def test_mail_auto_worker_sends_auto_task_issue(monkeypatch):
    session = make_session()
    configure_department(session)
    set_config(session, "bot_enabled", "true", is_secret=False)
    set_config(session, "bot_email_password", "runtime-secret", is_secret=True)
    task = create_valid_task(session)
    job = session.query(OutboundMailJob).filter_by(related_task_id=task.id, mail_type="TaskIssue").one()

    assert job.status == "Pending"
    assert job.priority == 30

    class FakeSMTP:
        sent_subjects = []

        def __init__(self, host, port, **kwargs):
            pass

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, traceback):
            return False

        def login(self, username, password):
            assert password == "runtime-secret"

        def send_message(self, msg, from_addr, to_addrs):
            self.sent_subjects.append(msg["Subject"])

    monkeypatch.setattr("backend.app.services.mail_worker.SessionLocal", lambda: nullcontext(session))
    monkeypatch.setattr("backend.app.services.mail_adapter.smtplib.SMTP_SSL", FakeSMTP)

    result = run_mail_auto_worker_once()
    session.commit()

    assert result["high_priority_mails"] == {"sent": 1, "failed": 0, "total": 1}
    assert job.status == "Sent"
    assert FakeSMTP.sent_subjects == [job.subject]


def test_send_selected_smtp_only_sends_requested_jobs(monkeypatch):
    session = make_session()
    set_config(session, "bot_enabled", "true", is_secret=False)
    set_config(session, "bot_email_password", "runtime-secret", is_secret=True)
    selected = OutboundMailJob(
        mail_type="Manual",
        to_json=dumps(["selected@jimuyida.com"]),
        cc_json=dumps([]),
        subject="SELECTED",
        body="hello",
        idempotency_key="smtp-selected",
        status="Pending",
    )
    skipped = OutboundMailJob(
        mail_type="Manual",
        to_json=dumps(["skipped@jimuyida.com"]),
        cc_json=dumps([]),
        subject="SKIPPED",
        body="hello",
        idempotency_key="smtp-skipped",
        status="Pending",
    )
    session.add_all([selected, skipped])
    session.commit()

    class FakeSMTP:
        sent_subjects = []

        def __init__(self, host, port, **kwargs):
            pass

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, traceback):
            return False

        def login(self, username, password):
            assert password == "runtime-secret"

        def send_message(self, msg, from_addr, to_addrs):
            self.sent_subjects.append(msg["Subject"])

    monkeypatch.setattr("backend.app.services.mail_adapter.smtplib.SMTP_SSL", FakeSMTP)

    result = send_outbound_jobs_smtp(session, [selected.id])
    session.commit()

    assert result == {"sent": 1, "failed": 0, "total": 1}
    assert selected.status == "Sent"
    assert skipped.status == "Pending"
    assert FakeSMTP.sent_subjects == ["SELECTED"]


def test_pending_receipt_ack_sender_does_not_send_task_issues(monkeypatch):
    session = make_session()
    set_config(session, "bot_enabled", "true", is_secret=False)
    set_config(session, "bot_email_password", "runtime-secret", is_secret=True)
    ack = OutboundMailJob(
        mail_type="SalesReceiptAck",
        to_json=dumps(["sales@jimuyida.com"]),
        cc_json=dumps([]),
        subject="Re: 生产订单需求",
        body="已收到",
        idempotency_key="ack-only",
        status="Pending",
    )
    task_issue = OutboundMailJob(
        mail_type="TaskIssue",
        to_json=dumps(["production@jimuyida.com"]),
        cc_json=dumps([]),
        subject="生产任务单",
        body="任务单",
        idempotency_key="task-not-auto",
        status="Pending",
    )
    session.add_all([ack, task_issue])
    session.commit()

    class FakeSMTP:
        sent_subjects = []

        def __init__(self, host, port, **kwargs):
            pass

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, traceback):
            return False

        def login(self, username, password):
            assert password == "runtime-secret"

        def send_message(self, msg, from_addr, to_addrs):
            self.sent_subjects.append(msg["Subject"])

    monkeypatch.setattr("backend.app.services.mail_adapter.smtplib.SMTP_SSL", FakeSMTP)

    result = send_pending_receipt_acks_smtp(session, limit=10)
    session.commit()

    assert result == {"sent": 1, "failed": 0, "total": 1}
    assert ack.status == "Sent"
    assert task_issue.status == "Pending"
    assert FakeSMTP.sent_subjects == ["Re: 生产订单需求"]


def test_attachment_text_can_create_task_and_evidence():
    session = make_session()
    configure_department(session)

    message = EmailMessage()
    message["From"] = "销售 <sales@jimuyida.com>"
    message["To"] = "bot.market@jimuyida.com"
    message["Subject"] = "生产订单需求 - 附件订单"
    message["Message-ID"] = "<attachment-only-order@jimuyida.com>"
    message.set_content("订单信息请看附件。")
    message.add_attachment(
        make_docx_bytes(
            "\n".join(
                [
                    "客户名称：附件字段客户",
                    "产品：附件展台",
                    "数量：66套",
                    "期望交期：2026-07-01",
                    "订单号：SO-ATTACH-001",
                ]
            )
        ),
        maintype="application",
        subtype="vnd.openxmlformats-officedocument.wordprocessingml.document",
        filename="订单需求.docx",
    )

    mail = store_incoming_email(session, parse_email_bytes(message.as_bytes()))
    assert session.query(OutboundMailJob).filter_by(mail_type="SalesReceiptAck").count() == 0
    session.add(ProcessingJob(job_type="process_inbound_mail", payload_json=dumps({"mail_id": mail.id}), status="Pending"))
    session.commit()

    result = run_pending_jobs(session)
    session.commit()
    ack = session.query(OutboundMailJob).filter_by(mail_type="SalesReceiptAck").one()
    evidence = session.query(ExtractionEvidence).filter_by(field_name="customer_name").one()

    assert result["completed"] == 1
    assert mail.related_task_id is not None
    assert evidence.source_type == "Attachment"
    assert evidence.field_value == "附件字段客户"
    assert as_list(ack.to_json) == ["sales@jimuyida.com"]


def test_missing_fields_enqueue_supplement_request():
    session = make_session()
    configure_department(session)
    mail = create_inbound_mail(
        session,
        from_address="sales@jimuyida.com",
        subject="生产订单需求 - 缺数量",
        body_text="\n".join(
            [
                "客户名称：缺数量客户",
                "产品：展示柜",
                "期望交期：2026-07-20",
                "订单号：SO-MISS-QTY",
            ]
        ),
    )

    task = create_task_from_mail(session, mail)
    session.commit()
    supplement = session.query(OutboundMailJob).filter_by(mail_type="RequirementSupplementRequest").one()

    assert task is None
    assert "数量" in supplement.body
    assert as_list(supplement.to_json) == ["sales@jimuyida.com"]


def test_short_natural_sales_order_triggers_review_rejection_without_ack():
    session = make_session()
    configure_department(session)
    message = EmailMessage()
    message["From"] = "销售 <sales@jimuyida.com>"
    message["To"] = "bot.market@jimuyida.com"
    message["Subject"] = "会触发初审规则的邮件"
    message["Message-ID"] = "<natural-order-review@jimuyida.com>"
    message.set_content("武汉大学需要G100,10台，请排产")

    mail = store_incoming_email(session, parse_email_bytes(message.as_bytes()))
    session.add(ProcessingJob(job_type="process_inbound_mail", payload_json=dumps({"mail_id": mail.id}), status="Pending"))
    session.commit()

    result = run_pending_jobs(session)
    session.commit()

    supplement = session.query(OutboundMailJob).filter_by(mail_type="RequirementSupplementRequest").one()
    case = session.query(ExceptionCase).filter_by(exception_type="ReviewNeedManual").one()
    detail = loads(case.detail, {})
    assert result["completed"] == 1
    assert mail.classification == "SalesOrderRequirement"
    assert "期望交期" in supplement.body
    assert "期望交期" in detail["missing_fields"]
    assert session.query(OutboundMailJob).filter_by(mail_type="SalesReceiptAck").count() == 0
    assert as_list(supplement.to_json) == ["sales@jimuyida.com"]


def test_sales_reply_to_initial_review_supplement_creates_task_and_receipt_after_send(monkeypatch):
    session = make_session()
    configure_department(session)
    set_config(session, "bot_enabled", "true", is_secret=False)
    set_config(session, "bot_email_password", "runtime-secret", is_secret=True)
    original = create_inbound_mail(
        session,
        from_address="bot.sales@jimuyida.com",
        subject="常州大学-Seal-2000台",
        body_text="常州大学需要Seal 2000台，请排产",
    )
    task = create_task_from_mail(session, original)
    session.commit()
    requirement = session.query(OrderRequirement).filter_by(source_mail_id=original.id).one()
    supplement = session.query(OutboundMailJob).filter_by(mail_type="RequirementSupplementRequest").one()
    supplement.status = "Sent"
    session.commit()

    reply_message = EmailMessage()
    reply_message["From"] = "sales <bot.sales@jimuyida.com>"
    reply_message["To"] = "bot.market@jimuyida.com"
    reply_message["Subject"] = f"Re:{supplement.subject}"
    reply_message["Message-ID"] = "<requirement-supplement-reply@jimuyida.com>"
    reply_message.set_content(
        "\n".join(
            [
                "2027年1月完成",
                "",
                "------------------ Original ------------------",
                supplement.body,
            ]
        )
    )
    reply_mail = store_incoming_email(session, parse_email_bytes(reply_message.as_bytes()))
    session.add(ProcessingJob(job_type="process_inbound_mail", payload_json=dumps({"mail_id": reply_mail.id}), status="Pending"))
    session.commit()

    result = run_pending_jobs(session)
    session.commit()
    created_task = session.query(ProductionTask).filter_by(requirement_id=requirement.id).one()
    issue_job = session.query(OutboundMailJob).filter_by(related_task_id=created_task.id, mail_type="RequirementSupplementTaskIssue").one()

    assert task is None
    assert result["completed"] == 1
    assert requirement.expected_delivery_date == "2027年1月完成"
    assert requirement.status == "TaskCreated"
    assert created_task.status == "TaskIssued"
    assert reply_mail.related_task_id == created_task.id
    assert as_list(issue_job.to_json) == ["production@jimuyida.com"]
    assert session.query(OutboundMailJob).filter_by(mail_type="RequirementSupplementAcceptedReceipt").count() == 0

    class FakeSMTP:
        sent_subjects = []

        def __init__(self, host, port, **kwargs):
            pass

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, traceback):
            return False

        def login(self, username, password):
            assert password == "runtime-secret"

        def send_message(self, msg, from_addr, to_addrs):
            self.sent_subjects.append(msg["Subject"])

    monkeypatch.setattr("backend.app.services.mail_adapter.smtplib.SMTP_SSL", FakeSMTP)
    send_result = send_outbound_jobs_smtp(session, [issue_job.id])
    session.commit()

    receipt = session.query(OutboundMailJob).filter_by(mail_type="RequirementSupplementAcceptedReceipt").one()
    assert send_result == {"sent": 1, "failed": 0, "total": 1}
    assert issue_job.status == "Sent"
    assert receipt.status == "Pending"
    assert "[已下达生产]" in receipt.subject
    assert "补充的订单信息已处理" in receipt.body
    assert FakeSMTP.sent_subjects == [issue_job.subject]


def test_pending_non_target_mail_is_reclassified_by_updated_rules():
    session = make_session()
    configure_department(session)
    mail = create_inbound_mail(
        session,
        from_address="sales@jimuyida.com",
        subject="会触发初审规则的邮件",
        body_text="武汉大学需要G100,10台，请排产",
    )
    mail.classification = "NonTarget"
    mail.classification_confidence = 70
    session.add(ProcessingJob(job_type="process_inbound_mail", payload_json=dumps({"mail_id": mail.id}), status="Pending"))
    session.commit()

    result = run_pending_jobs(session)
    session.commit()

    supplement = session.query(OutboundMailJob).filter_by(mail_type="RequirementSupplementRequest").one()
    assert result["completed"] == 1
    assert mail.classification == "SalesOrderRequirement"
    assert "武汉大学" in supplement.body


def test_duplicate_processing_does_not_duplicate_review_rejection():
    session = make_session()
    configure_department(session)
    mail = create_inbound_mail(
        session,
        from_address="sales@jimuyida.com",
        subject="会触发初审规则的邮件",
        body_text="武汉大学需要G100,10台，请排产",
    )
    session.add(ProcessingJob(job_type="process_inbound_mail", payload_json=dumps({"mail_id": mail.id}), status="Pending"))
    session.commit()
    run_pending_jobs(session)
    session.commit()

    session.add(ProcessingJob(job_type="process_inbound_mail", payload_json=dumps({"mail_id": mail.id}), status="Pending"))
    session.commit()
    run_pending_jobs(session)
    session.commit()

    assert session.query(OrderRequirement).filter_by(source_mail_id=mail.id).count() == 1
    assert session.query(OutboundMailJob).filter_by(mail_type="RequirementSupplementRequest").count() == 1
    assert session.query(ExceptionCase).filter_by(exception_type="ReviewNeedManual").count() == 1


def test_legacy_duplicate_requirements_do_not_duplicate_review_rejection():
    session = make_session()
    configure_department(session)
    mail = create_inbound_mail(
        session,
        from_address="sales@jimuyida.com",
        subject="会触发初审规则的邮件",
        body_text="武汉大学需要G100,10台，请排产",
    )
    task = create_task_from_mail(session, mail)
    session.commit()
    first_requirement = session.query(OrderRequirement).filter_by(source_mail_id=mail.id).one()

    session.add(
        OrderRequirement(
            source_mail_id=mail.id,
            internal_order_no="REQ-DUPLICATE-LEGACY",
            customer_name=first_requirement.customer_name,
            salesperson_email=first_requirement.salesperson_email,
            product_summary=first_requirement.product_summary,
            quantity_text=first_requirement.quantity_text,
            missing_fields_json=first_requirement.missing_fields_json,
            risk_flags_json="[]",
            status="ReviewFailed",
        )
    )
    session.commit()

    reprocessed = create_task_from_mail(session, mail)
    session.commit()

    assert task is None
    assert reprocessed is None
    assert session.query(OrderRequirement).filter_by(source_mail_id=mail.id).count() == 2
    assert session.query(OutboundMailJob).filter_by(mail_type="RequirementSupplementRequest").count() == 1


def test_duplicate_processing_jobs_only_execute_one_business_flow():
    session = make_session()
    configure_department(session)
    mail = create_inbound_mail(
        session,
        from_address="sales@jimuyida.com",
        subject="生产订单需求 - 重复队列",
        body_text="\n".join(
            [
                "客户名称：重复队列客户",
                "产品：G100",
                "数量：10台",
                "期望交期：2026-10-20",
            ]
        ),
    )
    payload = dumps({"mail_id": mail.id})
    session.add_all(
        [
            ProcessingJob(job_type="process_inbound_mail", payload_json=payload, status="Pending"),
            ProcessingJob(job_type="process_inbound_mail", payload_json=payload, status="Pending"),
        ]
    )
    session.commit()

    result = run_pending_jobs(session)
    session.commit()

    skipped = session.query(ProcessingJob).filter(ProcessingJob.error_message.like("Skipped duplicate%")).one()
    assert result == {"completed": 2, "failed": 0, "total": 2}
    assert skipped.status == "Completed"
    assert skipped.version == 2
    assert session.query(OrderRequirement).filter_by(source_mail_id=mail.id).count() == 1
    assert session.query(ProductionTask).count() == 1
    assert session.query(OutboundMailJob).filter_by(mail_type="TaskIssue").count() == 1


def test_custom_initial_review_rule_rejects_sales_order():
    session = make_session()
    configure_department(session)
    set_config(
        session,
        "initial_review_rules_json",
        dumps(
            [
                {
                    "id": "no-rush",
                    "name": "加急订单人工确认",
                    "field": "source_text",
                    "operator": "not_contains",
                    "value": "加急",
                    "message": "加急订单需要商务人工确认后再下达生产。",
                    "enabled": True,
                }
            ]
        ),
    )
    session.commit()
    mail = create_inbound_mail(
        session,
        from_address="sales@jimuyida.com",
        subject="生产订单需求 - 加急客户",
        body_text="\n".join(
            [
                "客户名称：加急客户",
                "产品：展示柜",
                "数量：50套",
                "期望交期：2026-07-20",
                "订单号：SO-RUSH",
                "备注：加急",
            ]
        ),
    )

    task = create_task_from_mail(session, mail)
    session.commit()

    supplement = session.query(OutboundMailJob).filter_by(mail_type="RequirementSupplementRequest").one()
    case = session.query(ExceptionCase).filter_by(exception_type="ReviewNeedManual").one()
    assert task is None
    assert "加急订单需要商务人工确认" in supplement.body
    assert "review_failures" in case.detail
    assert session.query(ProductionTaskVersion).count() == 0


def test_product_price_review_rejects_below_map_price():
    session = make_session()
    configure_department(session)
    spu = create_spu(session, "SPU-G100", "G100 展示柜", category="成品")
    session.add(ProductInventorySnapshot(material_code="SPU-G100", material_name="G100 展示柜", warehouse_code="WH", warehouse_name="成品仓", base_qty=10, qty=10))
    session.flush()
    sku = create_sku(session, spu.id, "G100")
    session.flush()
    set_channel_pricing(session, sku.id, "default", tier_a_price=12000, map_price=10000)
    session.commit()

    mail = create_inbound_mail(
        session,
        from_address="sales@jimuyida.com",
        subject="生产订单需求 - 价格低于限价",
        body_text="\n".join(
            [
                "客户名称：价格客户",
                "产品：G100",
                "数量：10台",
                "单价：90元",
                "期望交期：2026-07-20",
                "订单号：SO-PRICE-LOW",
            ]
        ),
    )

    task = create_task_from_mail(session, mail)
    session.commit()

    case = session.query(ExceptionCase).filter_by(exception_type="ReviewNeedManual").one()
    detail = loads(case.detail, {})
    supplement = session.query(OutboundMailJob).filter_by(mail_type="RequirementSupplementRequest").one()
    requirement = session.query(OrderRequirement).filter_by(source_mail_id=mail.id).one()

    assert task is None
    assert requirement.status == "ReviewFailed"
    assert any("低于最低限价" in flag for flag in detail["risk_flags"])
    assert any("物料 G100 审核失败" in item["message"] for item in detail["review_failures"])
    assert "低于最低限价" in supplement.body
    assert session.query(ProductionTask).count() == 0


def test_order_product_extraction_matches_finished_aliases_only():
    session = make_session()
    finished = create_spu(session, "1300100211", "三维扫描仪 JMK1 RTK(4规）", category="成品")
    material = create_spu(session, "1021900207", "线材", category="电子料")
    session.add_all(
        [
            ProductInventorySnapshot(material_code="1300100211", material_name="三维扫描仪 JMK1 RTK(4规）", warehouse_code="WH", warehouse_name="成品仓", base_qty=5, qty=5),
            ProductInventorySnapshot(material_code="1021900207", material_name="线材", warehouse_code="WH", warehouse_name="材料仓", base_qty=100, qty=100),
        ]
    )
    session.flush()
    create_sku(session, finished.id, "1300100211")
    session.add(ProductSKU(spu_uuid=material.id, sku_id="1021900207", status="Active"))
    session.commit()

    by_full_name = extract_order_products_from_text(session, "客户下单：三维扫描仪 JMK1 RTK(4规） 数量 1 单价 999元")
    by_model = extract_order_products_from_text(session, "客户下单：JMK1 RTK 数量 1 单价 999元")
    by_material = extract_order_products_from_text(session, "客户下单：1021900207 线材 数量 1 单价 1元")

    assert [item["sku_id"] for item in by_full_name] == ["1300100211"]
    assert by_full_name[0]["match_source"] == "product_alias"
    assert [item["sku_id"] for item in by_model] == ["1300100211"]
    assert by_material == []


def test_product_price_parser_accepts_common_order_price_formats():
    assert parse_price_to_cents("USD 999") == 99900
    assert parse_price_to_cents("$999.50") == 99950
    assert parse_price_to_cents("999美元") == 99900
    assert parse_price_to_cents("RMB999/台") == 99900
    assert parse_price_to_cents("999分") == 999


def test_order_product_extraction_reads_currency_and_unit_price_formats():
    session = make_session()
    spu = create_spu(session, "SPU-PRICE-FMT", "格式价格成品", category="成品")
    session.add(ProductInventorySnapshot(material_code="SPU-PRICE-FMT", material_name="格式价格成品", warehouse_code="WH", warehouse_name="成品仓", base_qty=1, qty=1))
    session.flush()
    create_sku(session, spu.id, "SKU-PRICE-FMT")
    session.commit()

    cases = [
        ("客户下单：SKU-PRICE-FMT 数量 1 单价 USD 999", 99900),
        ("客户下单：SKU-PRICE-FMT 数量 1 单价：$999.50", 99950),
        ("客户下单：SKU-PRICE-FMT 数量 1 单价：999美元", 99900),
        ("客户下单：SKU-PRICE-FMT 数量 1 单价：999/台", 99900),
    ]
    for text, expected in cases:
        items = extract_order_products_from_text(session, text)
        assert items[0]["unit_price"] == expected


def test_product_review_preview_api_uses_real_review_chain():
    from backend.app.main import preview_product_review_api

    session = make_session()
    spu = create_spu(session, "SPU-G300", "G300 展示柜", category="成品")
    session.add(ProductInventorySnapshot(material_code="SPU-G300", material_name="G300 展示柜", warehouse_code="WH", warehouse_name="成品仓", base_qty=8, qty=8))
    session.flush()
    sku = create_sku(session, spu.id, "G300")
    session.flush()
    set_channel_pricing(session, sku.id, "default", tier_a_price=12000, map_price=10000)
    session.commit()

    result = preview_product_review_api({"text": "客户下单 G300 展示柜 数量 1 单价 90 元", "channel": "default"}, session)

    assert result["ok"] is True
    assert result["summary"]["matched_count"] == 1
    assert result["items"][0]["sku_id"] == "G300"
    assert result["items"][0]["sku_uuid"] == sku.id
    assert result["items"][0]["spu_id"] == "SPU-G300"
    assert result["items"][0]["product_name"] == "G300 展示柜"
    assert result["items"][0]["pricing_configured"] is True
    assert result["items"][0]["review"]["status"] == "Exception"
    assert any("低于最低限价" in flag for flag in result["summary"]["risk_flags"])


def test_product_review_preview_suggests_candidates_when_unmatched():
    from backend.app.main import preview_product_review_api

    session = make_session()
    spu = create_spu(session, "SPU-JMK-RTK", "三维扫描仪 JMK1 RTK(4规）", category="成品")
    session.add(ProductInventorySnapshot(material_code="SPU-JMK-RTK", material_name="三维扫描仪 JMK1 RTK(4规）", warehouse_code="WH", warehouse_name="成品仓", base_qty=2, qty=2))
    session.flush()
    create_sku(session, spu.id, "SKU-JMK-RTK")
    session.commit()

    result = preview_product_review_api({"text": "客户下单：JMK1RT 数量 1 单价 999 元", "channel": "default"}, session)

    assert result["items"] == []
    assert result["summary"]["matched_count"] == 0
    assert result["summary"]["suggestion_count"] >= 1
    assert result["suggestions"][0]["spu_id"] == "SPU-JMK-RTK"
    assert result["suggestions"][0]["suggested_alias"] == "JMK1RT"


def test_product_review_preview_marks_missing_pricing_for_quick_fix():
    from backend.app.main import preview_product_review_api

    session = make_session()
    spu = create_spu(session, "SPU-NOPRICE", "无价格成品", category="成品")
    session.add(ProductInventorySnapshot(material_code="SPU-NOPRICE", material_name="无价格成品", warehouse_code="WH", warehouse_name="成品仓", base_qty=1, qty=1))
    session.flush()
    sku = create_sku(session, spu.id, "SKU-NOPRICE")
    session.commit()

    result = preview_product_review_api({"text": "客户下单：SKU-NOPRICE 数量 1 单价 99 元", "channel": "default"}, session)

    assert result["items"][0]["sku_uuid"] == sku.id
    assert result["items"][0]["pricing_configured"] is False
    assert any("未配置价格规则" in flag for flag in result["summary"]["risk_flags"])


def test_product_review_readiness_lists_pre_review_blockers():
    from backend.app.main import list_promotions_api, product_review_readiness_api

    session = make_session()
    priced_spu = create_spu(session, "SPU-READY-OK", "已配置成品", category="成品")
    missing_spu = create_spu(session, "SPU-READY-MISS", "缺价格成品", category="成品")
    duplicate_a = create_spu(session, "SPU-READY-DUP-A", "重复别名A", category="成品")
    duplicate_b = create_spu(session, "SPU-READY-DUP-B", "重复别名B", category="成品")
    session.add_all(
        [
            ProductInventorySnapshot(material_code="SPU-READY-OK", material_name="已配置成品", warehouse_code="WH", warehouse_name="成品仓", base_qty=1, qty=1),
            ProductInventorySnapshot(material_code="SPU-READY-MISS", material_name="缺价格成品", warehouse_code="WH", warehouse_name="成品仓", base_qty=1, qty=1),
            ProductInventorySnapshot(material_code="SPU-READY-DUP-A", material_name="重复别名A", warehouse_code="WH", warehouse_name="成品仓", base_qty=1, qty=1),
            ProductInventorySnapshot(material_code="SPU-READY-DUP-B", material_name="重复别名B", warehouse_code="WH", warehouse_name="成品仓", base_qty=1, qty=1),
        ]
    )
    session.flush()
    priced_sku = create_sku(session, priced_spu.id, "SKU-READY-OK")
    create_sku(session, missing_spu.id, "SKU-READY-MISS")
    dup_a_sku = create_sku(session, duplicate_a.id, "SKU-READY-DUP-A")
    dup_b_sku = create_sku(session, duplicate_b.id, "SKU-READY-DUP-B")
    session.flush()
    set_channel_pricing(session, priced_sku.id, "default", map_price=10000)
    set_channel_pricing(session, dup_a_sku.id, "default", map_price=10000)
    set_channel_pricing(session, dup_b_sku.id, "default", map_price=10000)
    update_spu_review_aliases(session, duplicate_a.id, ["通用别名"])
    update_spu_review_aliases(session, duplicate_b.id, ["通用别名"])
    session.add(PromotionRule(name="未绑定促销", channel="default", discount_type="percentage", discount_value=10, is_active=True))
    session.commit()

    result = product_review_readiness_api(channel="default", limit=20, session=session)
    listed_promotions = list_promotions_api(q="", page=1, page_size=10, session=session)
    listed_by_name = list_promotions_api(q="未绑定促销", page=1, page_size=10, session=session)
    listed_by_status = list_promotions_api(q="未绑定", page=1, page_size=10, session=session)
    issue_types = {item["issue_type"] for item in result["issues"]}

    assert result["summary"]["finished_sku_count"] == 4
    assert result["summary"]["missing_price_count"] == 1
    assert result["summary"]["duplicate_alias_count"] == 1
    assert result["summary"]["invalid_promotion_count"] == 1
    assert result["summary"]["blocker_count"] == 3
    assert {"missing_price", "duplicate_alias", "missing_alias", "invalid_promotion"} <= issue_types
    assert any(item.get("sku_uuid") and item["action"] == "configure_pricing" for item in result["issues"])
    assert listed_promotions["items"][0]["name"] == "未绑定促销"
    assert listed_promotions["items"][0]["binding_status"] == "unbound"
    assert listed_promotions["items"][0]["binding_valid"] is False
    assert listed_by_name["items"][0]["id"] == listed_promotions["items"][0]["id"]
    assert listed_by_status["items"][0]["id"] == listed_promotions["items"][0]["id"]


def test_manual_review_aliases_feed_order_product_extraction():
    from backend.app.main import list_channel_pricing_api, list_products_sku_api, list_products_spu_api

    session = make_session()
    spu = create_spu(session, "SPU-ALIAS-001", "三维扫描仪 ALPHA(4规）", category="成品")
    material = create_spu(session, "MAT-ALIAS-001", "旗舰扫描套装包装材料", category="包装盒")
    material.extended_info_json = json.dumps({"review_aliases": ["旗舰扫描套装"]}, ensure_ascii=False)
    session.add(ProductInventorySnapshot(material_code="SPU-ALIAS-001", material_name="三维扫描仪 ALPHA(4规）", warehouse_code="WH", warehouse_name="成品仓", base_qty=3, qty=3))
    session.add(ProductInventorySnapshot(material_code="MAT-ALIAS-001", material_name="旗舰扫描套装包装材料", warehouse_code="WH", warehouse_name="材料仓", base_qty=30, qty=30))
    session.flush()
    sku = create_sku(session, spu.id, "SKU-ALIAS-001")
    session.add(ProductSKU(spu_uuid=material.id, sku_id="MAT-ALIAS-001", status="Active"))
    session.flush()
    set_channel_pricing(session, sku.id, "default", map_price=99900)
    session.commit()

    assert extract_order_products_from_text(session, "客户下单：旗舰扫描套装 数量 1 单价 999元") == []

    update_spu_review_aliases(session, spu.id, ["旗舰扫描套装", "旗舰扫描套装", "成品"])
    session.commit()
    extracted = extract_order_products_from_text(session, "客户下单：旗舰扫描套装 数量 1 单价 999元")
    listed = list_products_spu_api(q="SPU-ALIAS", page=1, page_size=10, session=session)
    listed_by_alias = list_products_spu_api(q="旗舰扫描套装", page=1, page_size=10, session=session)
    sku_listed_by_alias = list_products_sku_api(spu_id=None, spu_uuid=None, q="旗舰扫描套装", page=1, page_size=10, session=session)
    pricing_listed_by_alias = list_channel_pricing_api(sku_id=None, sku_uuid=None, q="旗舰扫描套装", page=1, page_size=10, session=session)

    assert [item["sku_id"] for item in extracted] == ["SKU-ALIAS-001"]
    assert extracted[0]["match_alias"] == "旗舰扫描套装"
    assert listed["items"][0]["review_aliases"] == ["旗舰扫描套装"]
    assert [item["spu_id"] for item in listed_by_alias["items"]] == ["SPU-ALIAS-001"]
    assert [item["sku_id"] for item in sku_listed_by_alias["items"]] == ["SKU-ALIAS-001"]
    assert [item["sku_id"] for item in pricing_listed_by_alias["items"]] == ["SKU-ALIAS-001"]


def test_promotion_rules_bind_to_finished_sku_and_do_not_apply_cross_sku():
    session = make_session()
    promo_spu = create_spu(session, "SPU-PROMO-A", "活动成品A", category="成品")
    other_spu = create_spu(session, "SPU-PROMO-B", "活动成品B", category="成品")
    material_spu = create_spu(session, "MAT-PROMO", "活动材料", category="包装盒")
    session.add_all(
        [
            ProductInventorySnapshot(material_code="SPU-PROMO-A", material_name="活动成品A", warehouse_code="WH", warehouse_name="成品仓", base_qty=1, qty=1),
            ProductInventorySnapshot(material_code="SPU-PROMO-B", material_name="活动成品B", warehouse_code="WH", warehouse_name="成品仓", base_qty=1, qty=1),
            ProductInventorySnapshot(material_code="MAT-PROMO", material_name="活动材料", warehouse_code="WH", warehouse_name="材料仓", base_qty=1, qty=1),
        ]
    )
    session.flush()
    promo_sku = create_sku(session, promo_spu.id, "SKU-PROMO-A")
    other_sku = create_sku(session, other_spu.id, "SKU-PROMO-B")
    material_sku = ProductSKU(spu_uuid=material_spu.id, sku_id="SKU-PROMO-MAT", status="Active")
    session.add(material_sku)
    session.flush()
    set_channel_pricing(session, promo_sku.id, "default", map_price=10000)
    set_channel_pricing(session, other_sku.id, "default", map_price=10000)

    create_promotion_rule(session, name="618活动", sku_uuid=promo_sku.id, discount_type="percentage", discount_value=10)
    with pytest.raises(ValueError, match="成品库存"):
        create_promotion_rule(session, name="材料活动", sku_uuid=material_sku.id, discount_type="percentage", discount_value=10)
    session.commit()

    promotions, total = get_promotions(session)
    applied = review_order_products(session, [{"sku_id": "SKU-PROMO-A", "unit_price": 12000, "promotion_applied": ["618活动"]}])
    mismatched = review_order_products(session, [{"sku_id": "SKU-PROMO-B", "unit_price": 12000, "promotion_applied": ["618活动"]}])

    assert total == 1
    assert promotions[0].sku_uuid == promo_sku.id
    assert applied[0]["review"]["status"] == "Pass"
    assert mismatched[0]["review"]["status"] == "Exception"
    assert any("促销不适用于 SKU SKU-PROMO-B" in flag for flag in mismatched[0]["review"]["risk_flags"])


def test_product_review_uses_bound_promotion_discount_for_min_price():
    session = make_session()
    percent_spu = create_spu(session, "SPU-PROMO-PCT", "比例促销成品", category="成品")
    fixed_spu = create_spu(session, "SPU-PROMO-FIX", "固定促销成品", category="成品")
    session.add_all(
        [
            ProductInventorySnapshot(material_code="SPU-PROMO-PCT", material_name="比例促销成品", warehouse_code="WH", warehouse_name="成品仓", base_qty=1, qty=1),
            ProductInventorySnapshot(material_code="SPU-PROMO-FIX", material_name="固定促销成品", warehouse_code="WH", warehouse_name="成品仓", base_qty=1, qty=1),
        ]
    )
    session.flush()
    percent_sku = create_sku(session, percent_spu.id, "SKU-PROMO-PCT")
    fixed_sku = create_sku(session, fixed_spu.id, "SKU-PROMO-FIX")
    session.flush()
    set_channel_pricing(session, percent_sku.id, "default", map_price=10000)
    set_channel_pricing(session, fixed_sku.id, "default", map_price=10000)
    create_promotion_rule(session, name="九折活动", sku_uuid=percent_sku.id, discount_type="percentage", discount_value=10)
    create_promotion_rule(session, name="减10元", sku_uuid=fixed_sku.id, discount_type="fixed_amount", discount_value=1000)
    session.commit()

    no_promo = review_order_products(session, [{"sku_id": "SKU-PROMO-PCT", "unit_price": 9500}])
    percent_pass = review_order_products(session, [{"sku_id": "SKU-PROMO-PCT", "unit_price": 9000, "promotion_applied": ["九折活动"]}])
    percent_low = review_order_products(session, [{"sku_id": "SKU-PROMO-PCT", "unit_price": 8900, "promotion_applied": ["九折活动"]}])
    fixed_pass = review_order_products(session, [{"sku_id": "SKU-PROMO-FIX", "unit_price": 9000, "promotion_applied": ["减10元"]}])

    assert no_promo[0]["review"]["status"] == "Exception"
    assert percent_pass[0]["review"]["status"] == "Pass"
    assert fixed_pass[0]["review"]["status"] == "Pass"
    assert percent_low[0]["review"]["status"] == "Exception"
    assert any("低于促销最低价" in flag for flag in percent_low[0]["review"]["risk_flags"])


def test_duplicate_active_promotion_rules_block_review_and_readiness():
    from backend.app.main import product_review_readiness_api

    session = make_session()
    spu = create_spu(session, "SPU-PROMO-DUP", "重复促销成品", category="成品")
    session.add(ProductInventorySnapshot(material_code="SPU-PROMO-DUP", material_name="重复促销成品", warehouse_code="WH", warehouse_name="成品仓", base_qty=1, qty=1))
    session.flush()
    sku = create_sku(session, spu.id, "SKU-PROMO-DUP")
    session.flush()
    set_channel_pricing(session, sku.id, "default", map_price=10000)
    update_spu_review_aliases(session, spu.id, ["重复促销成品"])
    create_promotion_rule(session, name="重复活动", sku_uuid=sku.id, discount_type="percentage", discount_value=10)
    create_promotion_rule(session, name="重复活动", sku_uuid=sku.id, discount_type="percentage", discount_value=20)
    session.commit()

    readiness = product_review_readiness_api(channel="default", limit=20, session=session)
    reviewed = review_order_products(session, [{"sku_id": "SKU-PROMO-DUP", "unit_price": 9000, "promotion_applied": ["重复活动"]}])

    assert readiness["summary"]["duplicate_promotion_count"] == 1
    assert any(item["issue_type"] == "duplicate_promotion" for item in readiness["issues"])
    assert reviewed[0]["review"]["status"] == "Exception"
    assert any("促销规则重复" in flag for flag in reviewed[0]["review"]["risk_flags"])


def test_product_rule_validation_rejects_invalid_amounts_and_time_ranges():
    session = make_session()
    spu = create_spu(session, "SPU-RULE-VALID", "规则校验成品", category="成品")
    session.add(ProductInventorySnapshot(material_code="SPU-RULE-VALID", material_name="规则校验成品", warehouse_code="WH", warehouse_name="成品仓", base_qty=1, qty=1))
    session.flush()
    sku = create_sku(session, spu.id, "SKU-RULE-VALID")
    session.flush()
    start = datetime(2026, 6, 2, tzinfo=timezone.utc)
    end = datetime(2026, 6, 1, tzinfo=timezone.utc)

    invalid_cases = [
        (lambda: set_channel_pricing(session, sku.id, "default", map_price=-1), "底价"),
        (lambda: set_channel_pricing(session, sku.id, "default", map_price=10000, promo_start_time=start, promo_end_time=end), "开始时间"),
        (lambda: create_promotion_rule(session, name="超额折扣", sku_uuid=sku.id, discount_type="percentage", discount_value=120), "比例折扣"),
        (lambda: create_promotion_rule(session, name="零元减免", sku_uuid=sku.id, discount_type="fixed_amount", discount_value=0), "固定减免"),
        (lambda: create_promotion_rule(session, name="时间倒挂", sku_uuid=sku.id, discount_type="percentage", discount_value=10, start_time=start, end_time=end), "开始时间"),
    ]
    for action, message in invalid_cases:
        with pytest.raises(ValueError, match=message):
            action()


def test_product_price_review_allows_price_at_or_above_map_price_without_llm(monkeypatch):
    session = make_session()
    configure_department(session)
    spu = create_spu(session, "SPU-G200", "G200 展示柜", category="成品")
    session.add(ProductInventorySnapshot(material_code="SPU-G200", material_name="G200 展示柜", warehouse_code="WH", warehouse_name="成品仓", base_qty=10, qty=10))
    session.flush()
    sku = create_sku(session, spu.id, "G200")
    session.flush()
    set_channel_pricing(session, sku.id, "default", tier_a_price=12000, map_price=10000)
    session.commit()

    def fail_model_call(*args, **kwargs):
        raise AssertionError("price review should not call LLM when exact SKU and price are present")

    monkeypatch.setattr("backend.app.services.products.call_model", fail_model_call, raising=False)
    mail = create_inbound_mail(
        session,
        from_address="sales@jimuyida.com",
        subject="生产订单需求 - 价格合规",
        body_text="\n".join(
            [
                "客户名称：价格合规客户",
                "产品：G200",
                "数量：10台",
                "单价：120元",
                "期望交期：2026-07-20",
                "订单号：SO-PRICE-OK",
            ]
        ),
    )

    task = create_task_from_mail(session, mail)
    session.commit()

    requirement = session.query(OrderRequirement).filter_by(source_mail_id=mail.id).one()
    assert task is not None
    assert requirement.status == "TaskCreated"
    assert session.query(ExceptionCase).filter_by(exception_type="ReviewNeedManual").count() == 0
    assert session.query(OutboundMailJob).filter_by(mail_type="TaskIssue").count() == 1


def test_initial_review_config_includes_readonly_builtin_rules():
    session = make_session()
    set_config(
        session,
        "initial_review_rules_json",
        dumps(
            [
                {
                    "id": "custom-rule",
                    "name": "自定义规则",
                    "field": "source_text",
                    "operator": "contains",
                    "value": "采购订单",
                    "message": "缺少采购订单",
                    "enabled": True,
                }
            ]
        ),
    )
    session.commit()

    config = initial_review_config(session)
    rules = config["rules"]

    assert [rule["id"] for rule in rules[:3]] == [
        "builtin-required-core-fields",
        "builtin-parser-risk-flags",
        "builtin-duplicate-submission",
    ]
    assert all(rule["read_only"] is True and rule["is_builtin"] is True for rule in rules[:3])
    assert rules[-1]["id"] == "custom-rule"


def test_initial_review_config_removes_duplicate_custom_rules():
    session = make_session()
    set_config(
        session,
        "initial_review_rules_json",
        dumps(
            [
                {
                    "id": "rule-1",
                    "name": "采购订单校验",
                    "field": "source_text",
                    "operator": "contains",
                    "value": "采购订单",
                    "message": "缺少采购订单",
                    "enabled": True,
                },
                {
                    "id": "rule-2",
                    "name": "重复采购订单校验",
                    "field": "source_text",
                    "operator": "contains",
                    "value": " 采购 订单 ",
                    "message": "重复项应被清理",
                    "enabled": False,
                },
                {
                    "id": "rule-3",
                    "name": "特批编码校验",
                    "field": "source_text",
                    "operator": "contains",
                    "value": "特批编码",
                    "message": "缺少特批编码",
                    "enabled": True,
                },
            ]
        ),
        is_secret=False,
    )
    session.commit()

    config = initial_review_config(session, include_workflow_rules=True)
    session.commit()

    custom_rules = [rule for rule in config["rules"] if not rule.get("is_builtin")]
    assert [rule["id"] for rule in custom_rules] == ["rule-1", "rule-3"]
    persisted_rules = loads(session.get(SystemConfig, "initial_review_rules_json").value, [])
    assert [rule["id"] for rule in persisted_rules] == ["rule-1", "rule-3"]


def test_pending_auto_workflow_sender_includes_task_issues_and_questions(monkeypatch):
    session = make_session()
    set_config(session, "bot_enabled", "true", is_secret=False)
    set_config(session, "bot_email_password", "runtime-secret", is_secret=True)
    ack = OutboundMailJob(
        mail_type="SalesReceiptAck",
        to_json=dumps(["sales@jimuyida.com"]),
        cc_json=dumps([]),
        subject="Re: 生产订单需求",
        body="已收到",
        idempotency_key="ack-auto",
        status="Pending",
    )
    review = OutboundMailJob(
        mail_type="RequirementSupplementRequest",
        to_json=dumps(["sales@jimuyida.com"]),
        cc_json=dumps([]),
        subject="[订单信息待补充] 请补充生产任务单信息",
        body="初审未通过",
        idempotency_key="review-auto",
        status="Pending",
    )
    question_forward = OutboundMailJob(
        mail_type="ProductionQuestionForward",
        to_json=dumps(["sales@jimuyida.com"]),
        cc_json=dumps([]),
        subject="[生产疑问] 请补充确认",
        body="生产疑问",
        idempotency_key="question-forward-auto",
        status="Pending",
    )
    question_receipt = OutboundMailJob(
        mail_type="ProductionQuestionReceipt",
        to_json=dumps(["production@jimuyida.com"]),
        cc_json=dumps([]),
        subject="Re: [生产任务单]",
        body="已转发销售",
        idempotency_key="question-receipt-auto",
        status="Pending",
    )
    task_issue = OutboundMailJob(
        mail_type="TaskIssue",
        to_json=dumps(["production@jimuyida.com"]),
        cc_json=dumps([]),
        subject="生产任务单",
        body="任务单",
        idempotency_key="task-manual",
        status="Pending",
    )
    weekly_report = OutboundMailJob(
        mail_type="WeeklyReport",
        to_json=dumps(["dingyong@jimuyida.com"]),
        cc_json=dumps(["jinlei@jimuyida.com"]),
        subject="[商务生产任务单周报][2026-W17]",
        body="周报",
        idempotency_key="weekly-report-auto",
        status="Pending",
    )
    production_confirmed = OutboundMailJob(
        mail_type="ProductionConfirmed",
        to_json=dumps(["sales@jimuyida.com"]),
        cc_json=dumps(["dingyong@jimuyida.com", "jinlei@jimuyida.com"]),
        subject="[生产确认] 已确认排产",
        body="已确认",
        idempotency_key="production-confirmed-auto",
        status="Pending",
    )
    confirmation_receipt = OutboundMailJob(
        mail_type="ProductionConfirmationReceipt",
        to_json=dumps(["production@jimuyida.com"]),
        cc_json=dumps([]),
        subject="Re: [生产确认] 已记录",
        body="已记录",
        idempotency_key="production-confirmation-receipt-auto",
        status="Pending",
    )
    session.add_all([ack, review, question_forward, question_receipt, task_issue, weekly_report, production_confirmed, confirmation_receipt])
    session.commit()

    class FakeSMTP:
        sent_subjects = []

        def __init__(self, host, port, **kwargs):
            pass

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, traceback):
            return False

        def login(self, username, password):
            assert password == "runtime-secret"

        def send_message(self, msg, from_addr, to_addrs):
            self.sent_subjects.append(msg["Subject"])

    monkeypatch.setattr("backend.app.services.mail_adapter.smtplib.SMTP_SSL", FakeSMTP)

    result = send_pending_auto_workflow_mails_smtp(session, limit=10)
    session.commit()

    assert result == {"sent": 1, "failed": 0, "total": 1}
    assert ack.status == "Sent"
    assert review.status == "Pending"
    assert question_forward.status == "Pending"
    assert question_receipt.status == "Pending"
    assert task_issue.status == "Pending"
    assert weekly_report.status == "Pending"
    assert production_confirmed.status == "Pending"
    assert confirmation_receipt.status == "Pending"
    assert FakeSMTP.sent_subjects == [ack.subject]


def test_pending_auto_workflow_sender_includes_production_rejected(monkeypatch):
    session = make_session()
    set_config(session, "bot_enabled", "true", is_secret=False)
    set_config(session, "bot_email_password", "runtime-secret", is_secret=True)
    rejected = OutboundMailJob(
        mail_type="ProductionRejected",
        to_json=dumps(["sales@jimuyida.com"]),
        cc_json=dumps(["jinlei@jimuyida.com"]),
        subject="[生产驳回][PT-20260422-0001] 需补充确认",
        body="生产部驳回",
        idempotency_key="production-rejected-auto",
        status="Pending",
    )
    session.add(rejected)
    session.commit()

    class FakeSMTP:
        sent_subjects = []

        def __init__(self, host, port, **kwargs):
            pass

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, traceback):
            return False

        def login(self, username, password):
            assert password == "runtime-secret"

        def send_message(self, msg, from_addr, to_addrs):
            self.sent_subjects.append(msg["Subject"])

    monkeypatch.setattr("backend.app.services.mail_adapter.smtplib.SMTP_SSL", FakeSMTP)

    result = send_pending_auto_workflow_mails_smtp(session, limit=10)
    session.commit()

    assert result == {"sent": 1, "failed": 0, "total": 1}
    assert rejected.status == "Sent"
    assert FakeSMTP.sent_subjects == [rejected.subject]


def test_order_change_and_cancel_are_routed_to_correct_flow():
    session = make_session()
    configure_department(session)
    task = create_valid_task(session, order_no="SO-CHANGE-001")
    approve_task(session, task.id, actor="tester")
    session.commit()

    change_mail = create_inbound_mail(
        session,
        from_address="sales@jimuyida.com",
        subject=f"订单变更 - {task.task_no}",
        body_text="\n".join(
            [
                "订单号：SO-CHANGE-001",
                "产品：积木展示架 B2",
                "数量：150套",
                "期望交期：2026-05-25",
            ]
        ),
    )
    change_result = process_mail_direct(session, change_mail)
    session.commit()

    assert change_result is not None
    assert task.status == "ReissueDrafted"
    assert task.current_version_no == 2
    assert task.requirement.product_summary == "积木展示架 B2"

    cancel_mail = create_inbound_mail(
        session,
        from_address="sales@jimuyida.com",
        subject=f"取消订单 - {task.task_no}",
        body_text="订单取消，请暂停处理。",
    )
    cancel_result = process_mail_direct(session, cancel_mail)
    session.commit()
    sales_notice = session.query(OutboundMailJob).filter_by(mail_type="SalesDemandWithdrawn", related_task_id=task.id).one()
    production_notice = session.query(OutboundMailJob).filter_by(mail_type="ProductionDemandWithdrawn", related_task_id=task.id).one()

    assert cancel_result is None
    assert task.status == "Closed"
    assert task.closed_reason == "WithdrawnBySales"
    assert task.manual_takeover is False
    assert as_list(sales_notice.to_json) == ["sales@jimuyida.com"]
    assert as_list(production_notice.to_json) == ["production@jimuyida.com"]
    assert session.query(ExceptionCase).filter_by(exception_type="OrderCancelManualReview").count() == 0


def test_manual_force_close_task_sends_sales_and_production_notice():
    session = make_session()
    configure_department(session)
    task = create_valid_task(session, order_no="SO-MANUAL-CLOSE-001")
    jobs = force_close_task_manual(session, task.id, reason="商务人工终止", actor="tester")
    session.commit()

    sales_notice = session.query(OutboundMailJob).filter_by(mail_type="TaskManualClosedSales", related_task_id=task.id).one()
    production_notice = session.query(OutboundMailJob).filter_by(mail_type="TaskManualClosedProduction", related_task_id=task.id).one()

    assert len(jobs) == 2
    assert task.status == "Closed"
    assert task.closed_reason == "ManualForceClosed"
    assert task.manual_takeover is True
    assert task.requirement.status == "Closed"
    assert as_list(sales_notice.to_json) == ["sales@jimuyida.com"]
    assert as_list(production_notice.to_json) == ["production@jimuyida.com"]
    assert "商务人工终止" in sales_notice.body
    assert "商务人工终止" in production_notice.body


def test_manual_force_close_closed_task_raises():
    session = make_session()
    configure_department(session)
    task = create_valid_task(session, order_no="SO-MANUAL-CLOSE-002")
    record_production_feedback(session, task.id, "confirmed", "已确认排产")
    session.commit()

    with pytest.raises(ValueError, match="already closed"):
        force_close_task_manual(session, task.id, reason="再次关闭", actor="tester")


def test_production_termination_uses_dedicated_notice_types():
    session = make_session()
    configure_department(session)
    task = create_valid_task(session, order_no="SO-PRODUCTION-TERMINATE")
    session.commit()

    terminate_mail = create_inbound_mail(
        session,
        from_address="production@jimuyida.com",
        subject=f"终止生产 - {task.task_no}",
        body_text=f"生产侧终止生产，请停止该任务 {task.task_no}。",
    )
    result = process_mail_direct(session, terminate_mail)
    session.commit()

    sales_notice = session.query(OutboundMailJob).filter_by(mail_type="ProductionTerminateSalesNotice", related_task_id=task.id).one()
    production_notice = session.query(OutboundMailJob).filter_by(mail_type="ProductionTerminateProductionNotice", related_task_id=task.id).one()
    assert result == [sales_notice, production_notice]
    assert terminate_mail.classification == "ProductionTerminateRequest"
    assert task.status == "Closed"
    assert task.closed_reason == "ProductionTerminated"
    assert as_list(sales_notice.to_json) == ["sales@jimuyida.com"]
    assert as_list(production_notice.to_json) == ["production@jimuyida.com"]
    assert "生产侧已终止" in sales_notice.body
    assert session.query(OutboundMailJob).filter_by(mail_type="SalesDemandWithdrawn", related_task_id=task.id).count() == 0
    assert session.query(OutboundMailJob).filter_by(mail_type="ProductionDemandWithdrawn", related_task_id=task.id).count() == 0


def test_duplicate_sales_requirement_within_24h_sends_no_repeat_notice():
    session = make_session()
    configure_department(session)
    first_mail = create_inbound_mail(
        session,
        from_address="sales@jimuyida.com",
        subject="生产订单需求 - 重复提交1",
        body_text="\n".join(
            [
                "客户名称：重复客户",
                "产品：重复展台",
                "数量：10套",
                "期望交期：2026-08-20",
                "订单号：SO-REPEAT-001",
            ]
        ),
    )
    first_task = create_task_from_mail(session, first_mail)
    assert first_task is not None
    session.commit()

    duplicate_mail = create_inbound_mail(
        session,
        from_address="sales@jimuyida.com",
        subject="生产订单需求 - 重复提交2",
        body_text="\n".join(
            [
                "客户名称：重复客户",
                "产品：重复展台",
                "数量：10套",
                "期望交期：2026-08-20",
                "订单号：SO-REPEAT-001",
            ]
        ),
    )
    duplicate_task = create_task_from_mail(session, duplicate_mail)
    session.commit()

    duplicate_notice = session.query(OutboundMailJob).filter_by(mail_type="DuplicateSubmissionNotice").one()
    assert duplicate_task is None
    assert session.query(ProductionTask).count() == 1
    assert "请勿重复提交" in duplicate_notice.subject
    assert f"已受理任务号：{first_task.task_no}" in duplicate_notice.body
    assert as_list(duplicate_notice.to_json) == ["sales@jimuyida.com"]


def test_sales_cancel_after_production_confirmed_is_rejected():
    session = make_session()
    configure_department(session)
    task = create_valid_task(session, order_no="SO-CANCEL-AFTER-CONFIRMED")
    record_production_feedback(session, task.id, "confirmed", "确认排产")
    session.commit()

    cancel_mail = create_inbound_mail(
        session,
        from_address="sales@jimuyida.com",
        subject=f"撤回需求 - {task.task_no}",
        body_text=f"撤回需求 {task.task_no}",
    )
    result = process_mail_direct(session, cancel_mail)
    session.commit()

    reject_notice = session.query(OutboundMailJob).filter_by(mail_type="SalesDemandWithdrawRejected", related_task_id=task.id).one()
    case = session.query(ExceptionCase).filter_by(exception_type="OrderCancelAfterProductionConfirmed").one()
    assert result is None
    assert task.status == "Closed"
    assert task.closed_reason == "ScheduledConfirmed"
    assert "生产已确认排单" in reject_notice.body
    assert case.related_task_id == task.id


def process_mail_direct(session, mail):
    from backend.app.services.workflow import process_inbound_mail

    return process_inbound_mail(session, mail)


def test_cleanup_preview_execute_and_weekly_csv():
    session = make_session()
    mail = create_inbound_mail(
        session,
        from_address="newsletter@example.com",
        subject="普通通知",
        body_text="这不是订单。",
    )
    mail.classification = "NonTarget"
    mail.created_at = now_utc() - timedelta(days=40)
    session.commit()

    preview = cleanup_preview(session)
    session.commit()
    result = execute_cleanup(session, preview["cleanup_job_id"])
    session.commit()
    csv_text = weekly_report_csv(session)

    assert preview["mail_count"] == 1
    assert result["mail_count"] == 1
    assert session.get(MailMessage, mail.id) is None
    assert "section,period,product,salesperson" in csv_text
    assert "任务统计" in csv_text


def test_workflow_import_doc_without_email_recipients_creates_draft_versions():
    session = make_session()
    result = import_workflow_document(
        session,
        file_path="/Users/kaimao/github/jm-sp-bot/docs/商务部邮件下单流程梳理.docx",
        raw_text=None,
        prefer_llm=False,
        auto_publish=True,
        actor="tester",
    )
    session.commit()

    assert result["validation_errors"]
    assert any("缺少收件人" in item for item in result["validation_errors"])
    assert len(result["created_versions"]) >= 5
    assert session.query(WorkflowImportJob).count() == 1
    assert session.query(WorkflowVersion).filter_by(status="Active").count() == 0
    assert session.query(WorkflowVersion).filter_by(status="Draft").count() >= 5


def test_workflow_import_accepts_uploaded_text_content():
    session = make_session()
    raw_text = """
流程一: 上传流程
邮件收件人：张燕
邮件抄送人：销售直属领导
邮件主题：[上传][{{task_no}}]
邮件内容模板：
流程类型：上传流程
附件：采购订单
""".strip()

    result = import_workflow_document(
        session,
        file_path=None,
        raw_text=None,
        file_name="workflow-rules.txt",
        file_content=raw_text.encode("utf-8"),
        prefer_llm=False,
        auto_publish=False,
        actor="tester",
    )
    session.commit()

    assert result["validation_errors"] == []
    assert result["file_name"] == "workflow-rules.txt"
    assert result["source_asset_ref"] == "uploaded:workflow-rules.txt"
    version = session.get(WorkflowVersion, result["created_versions"][0]["id"])
    assert version is not None
    rules = loads(version.compiled_rules_json, {})
    assert rules["workflow_name"] == "上传流程"
    assert rules["routing"]["to_names"] == []
    assert rules["routing"]["cc_names"] == []
    assert version.status == "Draft"


def test_workflow_import_only_keeps_valid_recipient_emails_from_document():
    session = make_session()
    raw_text = """
流程一: 邮箱提取流程
邮件收件人：张燕 <zhangyan@jimuyida.com>、销售直属领导
邮件抄送人：丁总、cc1@jimuyida.com，cc2@jimuyida.com
邮件主题：[邮箱提取][{{task_no}}]
邮件内容模板：
流程类型：邮箱提取流程
""".strip()

    result = import_workflow_document(
        session,
        file_path=None,
        raw_text=raw_text,
        prefer_llm=False,
        auto_publish=True,
        actor="tester",
    )
    session.commit()

    assert result["validation_errors"] == []
    version = session.get(WorkflowVersion, result["created_versions"][0]["id"])
    rules = loads(version.compiled_rules_json, {})
    assert rules["routing"]["to_names"] == ["zhangyan@jimuyida.com"]
    assert rules["routing"]["cc_names"] == ["cc1@jimuyida.com", "cc2@jimuyida.com"]
    assert session.query(ProductionDepartment).filter_by(department_name="销售直属领导").one_or_none() is None


def test_workflow_with_empty_recipient_cannot_be_enabled():
    session = make_session()
    raw_text = """
流程一: 空收件人流程
邮件收件人：张燕、销售直属领导
邮件抄送人：丁总、金总
邮件主题：[空收件人][{{task_no}}]
邮件内容模板：
流程类型：空收件人流程
""".strip()

    result = import_workflow_document(
        session,
        file_path=None,
        raw_text=raw_text,
        prefer_llm=False,
        auto_publish=True,
        actor="tester",
    )
    session.commit()

    assert any("缺少收件人" in item for item in result["validation_errors"])
    version = session.get(WorkflowVersion, result["created_versions"][0]["id"])
    assert version.status == "Draft"
    rules = loads(version.compiled_rules_json, {})
    assert rules["routing"]["to_names"] == []
    assert rules["routing"]["cc_names"] == []

    with pytest.raises(ValueError) as exc:
        activate_workflow_version(session, version.id, actor="tester")
    assert "缺少收件人" in str(exc.value)


def test_workflow_import_splits_multiple_cc_recipients_by_comma_dunhao_and_space():
    session = make_session()
    raw_text = """
流程一: 多抄送流程
邮件收件人：to-production@jimuyida.com
邮件抄送人：cc1@jimuyida.com cc2@jimuyida.com、cc3@jimuyida.com
邮件主题：[多抄送][{{task_no}}]
邮件内容模板：
流程类型：多抄送流程
""".strip()

    result = import_workflow_document(
        session,
        file_path=None,
        raw_text=raw_text,
        prefer_llm=False,
        auto_publish=False,
        actor="tester",
    )
    session.commit()

    version = session.get(WorkflowVersion, result["created_versions"][0]["id"])
    rules = loads(version.compiled_rules_json, {})
    assert rules["routing"]["cc_names"] == ["cc1@jimuyida.com", "cc2@jimuyida.com", "cc3@jimuyida.com"]


def test_workflow_edit_requires_main_recipient_from_production_department_emails():
    session = make_session()
    raw_text = """
流程一: 主送校验流程
邮件收件人：to-production@jimuyida.com
邮件主题：[主送校验][{{task_no}}]
邮件内容模板：
流程类型：主送校验流程
""".strip()
    result = import_workflow_document(
        session,
        file_path=None,
        raw_text=raw_text,
        prefer_llm=False,
        auto_publish=True,
        actor="tester",
    )
    session.commit()
    version = session.get(WorkflowVersion, result["created_versions"][0]["id"])
    deactivate_workflow_version(session, version.id)
    session.commit()

    rules = loads(version.compiled_rules_json, {})
    rules["routing"]["to_names"] = ["张燕"]
    with pytest.raises(ValueError, match="主送人只能从生产部门邮箱列表选择"):
        save_workflow_version_rules(session, version.id, compiled_rules=rules, actor="tester", activate=True)

    rules["routing"]["to_names"] = ["to-production@jimuyida.com"]
    saved = save_workflow_version_rules(session, version.id, compiled_rules=rules, actor="tester", activate=True)
    session.commit()

    assert saved.status == "Active"


def test_workflow_import_same_doc_is_idempotent_on_versions():
    session = make_session()
    first = import_workflow_document(
        session,
        file_path="/Users/kaimao/github/jm-sp-bot/docs/商务部邮件下单流程梳理.docx",
        raw_text=None,
        prefer_llm=False,
        auto_publish=True,
        actor="tester",
    )
    session.commit()
    before_versions = session.query(WorkflowVersion).count()
    second = import_workflow_document(
        session,
        file_path="/Users/kaimao/github/jm-sp-bot/docs/商务部邮件下单流程梳理.docx",
        raw_text=None,
        prefer_llm=False,
        auto_publish=True,
        actor="tester",
    )
    session.commit()
    after_versions = session.query(WorkflowVersion).count()

    assert len(first["created_versions"]) >= 5
    assert second["validation_errors"]
    assert any("缺少收件人" in item for item in second["validation_errors"])
    assert len(second["created_versions"]) == 0
    assert before_versions == after_versions


def test_workflow_list_includes_builtin_default_order_flow():
    session = make_session()
    configure_department(session)

    rows = list_workflow_rules(session, only_active=False)
    builtin = next((row for row in rows if row.get("is_builtin")), None)

    assert builtin is not None
    assert builtin["workflow_code"] == "builtin_default_order_flow"
    assert builtin["status"] == "BuiltIn"
    assert builtin["editable"] is False
    assert "production@jimuyida.com" in (builtin["rules"].get("routing", {}).get("to_names") or [])
    assert builtin["rules"]["subject_template"]
    assert builtin["rules"]["body_template"]


def test_workflow_version_diff_and_rollback_activate_selected_version():
    session = make_session()
    configure_department(session)
    definition = WorkflowDefinition(
        workflow_code="diff_flow",
        workflow_name="版本差异流程",
        status="Active",
    )
    session.add(definition)
    session.flush()
    base_rule = {
        "workflow_code": "diff_flow",
        "workflow_name": "版本差异流程",
        "match": {"any_keywords": ["差异"], "all_keywords": [], "warehouse": "", "order_type": "", "subject_patterns": []},
        "routing": {"to_names": ["production@jimuyida.com"], "cc_names": []},
        "subject_template": "[V1][{{task_no}}]",
        "body_template": "任务 {{task_no}} {{customer_name}} {{product_summary}} {{quantity_text}} {{expected_delivery_date}} {{workflow_name}} V{{version_no}}",
        "required_fields": ["customer_name"],
        "required_attachments": [],
        "review_rules": [],
    }
    changed_rule = {**base_rule, "subject_template": "[V2][{{task_no}}]", "required_fields": ["customer_name", "product_summary"]}
    v1 = WorkflowVersion(
        workflow_id=definition.id,
        version_no=1,
        compiled_rules_json=dumps(base_rule),
        status="Archived",
        created_by="tester",
    )
    v2 = WorkflowVersion(
        workflow_id=definition.id,
        version_no=2,
        compiled_rules_json=dumps(changed_rule),
        status="Active",
        created_by="tester",
        approved_by="tester",
        approved_at=now_utc(),
    )
    session.add_all([v1, v2])
    session.commit()

    diff = workflow_version_diff(session, v2.id)
    assert diff["base"]["id"] == v1.id
    assert diff["changed"] is True
    assert {item["field"] for item in diff["changes"]} >= {"subject_template", "required_fields"}

    rollback = rollback_workflow_version(session, v1.id, actor="tester")
    session.commit()

    assert rollback.id == v1.id
    assert session.get(WorkflowVersion, v1.id).status == "Active"
    assert session.get(WorkflowVersion, v2.id).status == "Archived"


def test_workflow_simulation_reports_task_creation_without_persisting():
    from backend.app.main import workflow_simulate
    from backend.app.schemas import WorkflowSimulationRequest

    session = make_session()
    configure_department(session)
    before_mail_count = session.query(MailMessage).count()
    before_task_count = session.query(ProductionTask).count()

    result = workflow_simulate(
        WorkflowSimulationRequest(
            from_address="sales@jimuyida.com",
            subject="生产订单需求 - 模拟客户",
            body_text="\n".join(
                [
                    "客户名称：模拟客户",
                    "产品：模拟产品A",
                    "数量：10套",
                    "期望交期：2026-05-20",
                    "订单号：SIM-001",
                ]
            ),
        ),
        session,
    )

    assert result["classification"] == "SalesOrderRequirement"
    assert result["would_create_task"] is True
    assert result["task"]["task_no"].startswith("PT-")
    assert session.query(MailMessage).count() == before_mail_count
    assert session.query(ProductionTask).count() == before_task_count


def test_workflow_import_falls_back_when_llm_timeout(monkeypatch):
    session = make_session()
    raw_text = """
流程一: 常规销售流程
邮件收件人：production@jimuyida.com
邮件抄送人：sales.lead@jimuyida.com
邮件主题：[常规][{{task_no}}]
邮件内容模板：
流程类型：常规销售
附件：采购订单
""".strip()

    def raise_timeout(*args, **kwargs):
        raise httpx.ReadTimeout("timed out")

    monkeypatch.setattr("backend.app.services.workflow_rules.call_model", raise_timeout)
    monkeypatch.setattr("backend.app.services.workflow_rules.resolve_api_key", lambda *args, **kwargs: "mock-key")

    result = import_workflow_document(
        session,
        file_path=None,
        raw_text=raw_text,
        prefer_llm=True,
        auto_publish=True,
        actor="tester",
    )
    session.commit()

    assert result["validation_errors"] == []
    assert result["llm_used"] is False
    assert len(result["created_versions"]) == 1
    version = session.get(WorkflowVersion, result["created_versions"][0]["id"])
    assert version is not None
    rules = loads(version.compiled_rules_json, {})
    assert rules["workflow_name"] == "常规销售流程"
    assert rules["routing"]["to_names"] == ["production@jimuyida.com"]


def test_workflow_import_backfills_task_template_variables_when_missing():
    session = make_session()
    raw_text = """
流程一: 静态模板流程
邮件收件人：张燕
邮件主题：静态主题
邮件内容模板：
张主管，你好！
现有销售订单需要备货出货。
物料详情描述：【含物料编码、物料名称、规格型号、数量】
附件：采购订单
""".strip()

    result = import_workflow_document(
        session,
        file_path=None,
        raw_text=raw_text,
        prefer_llm=False,
        auto_publish=False,
        actor="tester",
    )
    session.commit()

    version = session.get(WorkflowVersion, result["created_versions"][0]["id"])
    rules = loads(version.compiled_rules_json, {})
    assert "{{task_no}}" in rules["subject_template"]
    assert "{{customer_name}}" in rules["subject_template"]
    for token in [
        "{{task_no}}",
        "{{version_no}}",
        "{{customer_name}}",
        "{{product_summary}}",
        "{{quantity_text}}",
        "{{expected_delivery_date}}",
        "{{workflow_name}}",
    ]:
        assert token in rules["body_template"]
    assert "原流程邮件模板" in rules["body_template"]
    assert "张主管，你好" in rules["body_template"]


def test_workflow_chat_generate_returns_normalized_rule(monkeypatch):
    session = make_session()

    def fake_call_model(*args, **kwargs):
        return {
            "choices": [
                {
                    "message": {
                        "content": dumps(
                            {
                                "assistant_reply": "流程信息齐全，已生成草稿。",
                                "ready": True,
                                "workflow_rule": {
                                    "workflow_name": "样机赠送流程",
                                    "match": {
                                        "any_keywords": ["样机赠送", "赠送"],
                                        "warehouse": "wuhan",
                                        "order_type": "sample_gift",
                                    },
                                    "routing": {"to_names": ["洪丹"], "cc_names": ["销售直属领导"]},
                                    "subject_template": "[样机赠送][{{task_no}}]",
                                    "body_template": "流程类型：样机赠送",
                                    "required_fields": ["customer_name", "product_summary", "quantity_text", "expected_delivery_date"],
                                    "required_attachments": ["审批截图"],
                                    "review_rules": [
                                        {
                                            "id": "gift-approval",
                                            "name": "审批截图校验",
                                            "field": "source_text",
                                            "operator": "contains",
                                            "value": "审批截图",
                                            "message": "缺少审批截图说明",
                                            "enabled": True,
                                        }
                                    ],
                                },
                            }
                        )
                    }
                }
            ]
        }

    monkeypatch.setattr("backend.app.services.workflow_rules._active_model", lambda _session: object())
    monkeypatch.setattr("backend.app.services.workflow_rules.call_model", fake_call_model)

    result = chat_generate_workflow_rule(
        session,
        messages=[{"role": "user", "content": "新增样机赠送流程，收件人洪丹。"}],
        current_rule=None,
    )

    assert result["ready"] is True
    assert result["validation_errors"] == []
    assert result["reply"] == "流程信息齐全，已生成草稿。"
    assert "自动生成该流程对应规则" in result["notification"]
    assert result["compiled_rule"]["workflow_name"] == "样机赠送流程"
    assert result["compiled_rule"]["routing"]["to_names"] == ["洪丹"]
    assert len(result["compiled_rule"]["review_rules"]) == 1


def test_workflow_chat_generate_guides_user_when_definition_incomplete(monkeypatch):
    session = make_session()

    def fake_call_model(*args, **kwargs):
        return {
            "choices": [
                {
                    "message": {
                        "content": dumps(
                            {
                                "assistant_reply": "先记录到流程草稿。",
                                "ready": True,
                                "workflow_rule": {
                                    "workflow_name": "新流程",
                                    "routing": {"to_names": [], "cc_names": []},
                                    "match": {"any_keywords": ["流程"], "order_type": "normal_sales"},
                                    "required_fields": ["customer_name", "product_summary", "quantity_text", "expected_delivery_date"],
                                },
                            }
                        )
                    }
                }
            ]
        }

    monkeypatch.setattr("backend.app.services.workflow_rules._active_model", lambda _session: object())
    monkeypatch.setattr("backend.app.services.workflow_rules.call_model", fake_call_model)

    result = chat_generate_workflow_rule(
        session,
        messages=[{"role": "user", "content": "先建一个新流程"}],
        current_rule=None,
    )

    assert result["ready"] is False
    assert result["compiled_rule"] is not None
    assert "主送给谁" in result["next_question"]
    assert result["pending_questions"]
    assert result["notification"] == ""


def test_workflow_chat_generate_backfills_name_from_user_turn_when_rule_missing(monkeypatch):
    session = make_session()

    def fake_call_model(*args, **kwargs):
        return {
            "choices": [
                {
                    "message": {
                        "content": dumps(
                            {
                                "assistant_reply": "流程名称已确认。",
                                "ready": False,
                                "workflow_rule": None,
                            }
                        )
                    }
                }
            ]
        }

    monkeypatch.setattr("backend.app.services.workflow_rules._active_model", lambda _session: object())
    monkeypatch.setattr("backend.app.services.workflow_rules.call_model", fake_call_model)

    result = chat_generate_workflow_rule(
        session,
        messages=[
            {"role": "assistant", "content": "请先告诉我这个新流程的名称。"},
            {"role": "user", "content": "新流程的名称就是“测试流程”"},
        ],
        current_rule=None,
    )

    assert result["ready"] is False
    assert result["compiled_rule"] is not None
    assert result["compiled_rule"]["workflow_name"] == "测试流程"
    assert result["next_question"].startswith("该流程邮件主送给谁")
    assert result["pending_questions"]
    assert "名称" not in result["pending_questions"][0]


def test_workflow_chat_generate_detects_existing_flow_for_edit(monkeypatch):
    session = make_session()
    import_result = import_structured_workflow_rules(
        session,
        rules=[
            {
                "workflow_code": "transfer_flow",
                "workflow_name": "新机调拨流程",
                "routing": {"to_names": ["张燕"], "cc_names": ["销售直属领导"]},
                "match": {"any_keywords": ["新机调拨"], "order_type": "transfer"},
                "subject_template": "[新机调拨][{{task_no}}]",
                "body_template": "流程类型：新机调拨",
                "required_fields": ["customer_name", "product_summary", "quantity_text", "expected_delivery_date"],
                "required_attachments": [],
                "review_rules": [],
            }
        ],
        actor="tester",
        auto_publish=False,
        source_asset_ref="workflow-chat",
    )
    session.commit()
    version_id = import_result["created_versions"][0]["id"]

    def fake_call_model(*args, **kwargs):
        messages = kwargs.get("messages") or []
        assert any("当前任务是编辑已有流程" in item.get("content", "") for item in messages)
        return {
            "choices": [
                {
                    "message": {
                        "content": dumps(
                            {
                                "assistant_reply": "已在原流程上增加必填字段。",
                                "ready": True,
                                "workflow_rule": {
                                    "workflow_name": "新增的错误流程名",
                                    "required_fields": [
                                        "customer_name",
                                        "product_summary",
                                        "quantity_text",
                                        "expected_delivery_date",
                                        "initiator",
                                        "expected_time",
                                    ],
                                },
                            }
                        )
                    }
                }
            ]
        }

    monkeypatch.setattr("backend.app.services.workflow_rules._active_model", lambda _session: object())
    monkeypatch.setattr("backend.app.services.workflow_rules.call_model", fake_call_model)

    result = chat_generate_workflow_rule(
        session,
        messages=[{"role": "user", "content": "我要重新编辑 新机调拨流程，增加必填字段包括发起人和期望时间"}],
        current_rule=None,
    )

    assert result["edit_version_id"] == version_id
    assert result["edit_workflow_name"] == "新机调拨流程"
    assert result["compiled_rule"]["workflow_code"] == "transfer_flow"
    assert result["compiled_rule"]["workflow_name"] == "新机调拨流程"
    assert "initiator" in result["compiled_rule"]["required_fields"]


def test_import_structured_workflow_rules_creates_draft_version():
    session = make_session()
    result = import_structured_workflow_rules(
        session,
        rules=[
            {
                "workflow_name": "对话生成流程",
                "routing": {"to_names": ["张燕"], "cc_names": ["销售直属领导"]},
                "match": {"any_keywords": ["对话流程"], "order_type": "normal_sales"},
                "subject_template": "[对话流程][{{task_no}}]",
                "body_template": "流程类型：对话流程",
                "required_fields": ["customer_name", "product_summary", "quantity_text", "expected_delivery_date"],
                "required_attachments": ["采购订单"],
                "review_rules": [
                    {
                        "id": "chat-rule-1",
                        "name": "采购订单校验",
                        "field": "source_text",
                        "operator": "contains",
                        "value": "采购订单",
                        "message": "缺少采购订单信息",
                        "enabled": True,
                    }
                ],
            }
        ],
        actor="tester",
        auto_publish=False,
        source_asset_ref="workflow-chat",
    )
    session.commit()

    assert result["validation_errors"] == []
    assert len(result["created_versions"]) == 1
    version = session.get(WorkflowVersion, result["created_versions"][0]["id"])
    assert version is not None
    assert version.status == "Draft"
    assert version.source_asset_ref == "workflow-chat"


def test_save_workflow_version_rules_can_activate_after_manual_edit():
    session = make_session()
    raw_text = """
流程一: 常规销售流程
邮件收件人：张燕
邮件抄送人：销售直属领导
邮件主题：[常规][{{task_no}}]
邮件内容模板：
流程类型：常规销售
附件：采购订单
""".strip()
    result = import_workflow_document(
        session,
        file_path=None,
        raw_text=raw_text,
        prefer_llm=False,
        auto_publish=False,
        actor="tester",
    )
    session.commit()

    draft_id = result["created_versions"][0]["id"]
    draft = session.get(WorkflowVersion, draft_id)
    assert draft is not None
    assert draft.status == "Draft"
    rules = loads(draft.compiled_rules_json, {})
    rules["review_rules"] = [
        {
            "id": "manual-review-1",
            "name": "特批编码校验",
            "field": "source_text",
            "operator": "contains",
            "value": "特批编码",
            "message": "邮件缺少特批编码信息",
            "enabled": True,
        }
    ]
    rules["routing"] = {"to_names": ["洪丹"], "cc_names": ["销售直属领导", "商务负责人"]}
    session.add(
        ProductionDepartment(
            department_code="hongdan",
            department_name="洪丹",
            mail_to_json=dumps(["hongdan@jimuyida.com"]),
            mail_cc_json=dumps([]),
            status="Active",
        )
    )

    saved = save_workflow_version_rules(
        session,
        draft_id,
        compiled_rules=rules,
        actor="tester",
        activate=True,
    )
    session.commit()

    assert saved.status == "Active"
    saved_rules = loads(saved.compiled_rules_json, {})
    assert len(saved_rules.get("review_rules", [])) == 1
    assert saved_rules["review_rules"][0]["name"] == "特批编码校验"
    assert saved_rules["routing"]["to_names"] == ["hongdan@jimuyida.com"]
    assert saved_rules["routing"]["cc_names"] == ["销售直属领导", "商务负责人"]


def test_edit_active_workflow_requires_deactivate_first():
    session = make_session()
    raw_text = """
流程一: 常规销售流程
邮件收件人：zhangyan@jimuyida.com
邮件抄送人：sales.lead@jimuyida.com
邮件主题：[常规][{{task_no}}]
邮件内容模板：
流程类型：常规销售
附件：采购订单
""".strip()
    result = import_workflow_document(
        session,
        file_path=None,
        raw_text=raw_text,
        prefer_llm=False,
        auto_publish=True,
        actor="tester",
    )
    session.commit()

    version_id = result["created_versions"][0]["id"]
    active = session.get(WorkflowVersion, version_id)
    assert active is not None
    assert active.status == "Active"

    rules = loads(active.compiled_rules_json, {})
    rules["subject_template"] = "[更新][{{task_no}}]"
    with pytest.raises(ValueError, match="deactivated before edit"):
        save_workflow_version_rules(
            session,
            version_id,
            compiled_rules=rules,
            actor="tester",
            activate=False,
        )


def test_workflow_version_can_be_deactivated_then_updated_in_place_and_deleted():
    session = make_session()
    raw_text = """
流程一: 常规销售流程
邮件收件人：zhangyan@jimuyida.com
邮件抄送人：sales.lead@jimuyida.com
邮件主题：[常规][{{task_no}}]
邮件内容模板：
流程类型：常规销售
附件：采购订单
""".strip()
    result = import_workflow_document(
        session,
        file_path=None,
        raw_text=raw_text,
        prefer_llm=False,
        auto_publish=True,
        actor="tester",
    )
    session.commit()

    version_id = result["created_versions"][0]["id"]
    archived = deactivate_workflow_version(session, version_id)
    session.commit()
    assert archived.status == "Archived"

    rules = loads(archived.compiled_rules_json, {})
    rules["subject_template"] = "[停用后编辑][{{task_no}}]"
    draft = save_workflow_version_rules(
        session,
        version_id,
        compiled_rules=rules,
        actor="tester",
        activate=False,
    )
    session.commit()
    assert draft.id == version_id
    assert draft.status == "Draft"
    assert session.query(WorkflowVersion).count() == 1

    delete_workflow_version(session, version_id)
    session.commit()
    assert session.get(WorkflowVersion, version_id) is None


def test_delete_active_workflow_requires_deactivate_first():
    session = make_session()
    raw_text = """
流程一: 常规销售流程
邮件收件人：zhangyan@jimuyida.com
邮件抄送人：sales.lead@jimuyida.com
邮件主题：[常规][{{task_no}}]
邮件内容模板：
流程类型：常规销售
附件：采购订单
""".strip()
    result = import_workflow_document(
        session,
        file_path=None,
        raw_text=raw_text,
        prefer_llm=False,
        auto_publish=True,
        actor="tester",
    )
    session.commit()

    version_id = result["created_versions"][0]["id"]
    with pytest.raises(ValueError, match="deactivated before delete"):
        delete_workflow_version(session, version_id)


def test_import_workflow_document_rejects_duplicate_workflow_name():
    session = make_session()
    first_text = """
流程一: 新机调拨
邮件收件人：张燕
邮件主题：[新机调拨][{{task_no}}]
邮件内容模板：
流程类型：新机调拨
""".strip()
    second_text = """
流程一: 新机 调拨
邮件收件人：张燕
邮件主题：[新机调拨更新][{{task_no}}]
邮件内容模板：
流程类型：新机调拨更新
""".strip()

    first = import_workflow_document(
        session,
        file_path=None,
        raw_text=first_text,
        prefer_llm=False,
        auto_publish=False,
        actor="tester",
    )
    session.commit()

    second = import_workflow_document(
        session,
        file_path=None,
        raw_text=second_text,
        prefer_llm=False,
        auto_publish=False,
        actor="tester",
    )
    session.commit()

    assert first["created_versions"]
    assert second["created_versions"] == []
    assert any("流程已存在" in message for message in second["validation_errors"])
    assert session.query(WorkflowVersion).count() == 1


def test_import_workflow_document_rejects_duplicate_names_in_same_batch():
    session = make_session()
    raw_text = """
流程一: 重复流程
邮件收件人：张燕
邮件主题：[重复A][{{task_no}}]
邮件内容模板：
流程类型：重复A

流程二: 重复流程
邮件收件人：洪丹
邮件主题：[重复B][{{task_no}}]
邮件内容模板：
流程类型：重复B
""".strip()

    result = import_workflow_document(
        session,
        file_path=None,
        raw_text=raw_text,
        prefer_llm=False,
        auto_publish=False,
        actor="tester",
    )
    session.commit()

    assert len(result["created_versions"]) == 1
    assert any("本次导入的其他流程名称重复" in message for message in result["validation_errors"])


def test_workflow_specific_review_rules_block_and_allow_after_fix():
    session = make_session()
    raw_text = """
流程一: 常规销售流程
邮件收件人：zhangyan@jimuyida.com
邮件抄送人：sales.lead@jimuyida.com
邮件主题：[常规][{{task_no}}]
邮件内容模板：
流程类型：常规销售
附件：采购订单
""".strip()
    import_result = import_workflow_document(
        session,
        file_path=None,
        raw_text=raw_text,
        prefer_llm=False,
        auto_publish=True,
        actor="tester",
    )
    session.commit()
    version_id = import_result["created_versions"][0]["id"]
    active_version = session.get(WorkflowVersion, version_id)
    assert active_version is not None
    custom_rules = loads(active_version.compiled_rules_json, {})
    custom_rules["review_rules"] = [
        {
            "id": "special-code",
            "name": "特批编码校验",
            "field": "source_text",
            "operator": "contains",
            "value": "特批编码",
            "message": "邮件缺少特批编码信息",
            "enabled": True,
            }
        ]
    deactivate_workflow_version(session, version_id)
    save_workflow_version_rules(session, version_id, compiled_rules=custom_rules, actor="tester", activate=True)
    session.commit()

    blocked = create_inbound_mail(
        session,
        from_address="sales@jimuyida.com",
        subject="生产订单需求 - 常规销售流程",
        body_text="\n".join(
            [
                "客户名称：流程客户A",
                "产品：产品A",
                "数量：20台",
                "期望交期：2026-10-20",
                "订单号：SO-WF-REVIEW-001",
                "附件：采购订单",
            ]
        ),
    )
    blocked_task = create_task_from_mail(session, blocked)
    session.commit()

    assert blocked_task is None
    blocked_case = session.query(ExceptionCase).filter_by(exception_type="ReviewNeedManual").order_by(ExceptionCase.created_at.desc()).first()
    assert blocked_case is not None
    blocked_detail = loads(blocked_case.detail, {})
    assert any("特批编码校验" in str(item.get("rule_name", "")) for item in blocked_detail.get("review_failures", []))
    blocked_notice = session.query(OutboundMailJob).filter_by(mail_type="RequirementSupplementRequest").one()
    assert f"流程编号：{custom_rules['workflow_code']}" in blocked_notice.body
    assert f"流程版本ID：{version_id}" in blocked_notice.body
    assert "未满足该流程下的规则" in blocked_notice.body
    assert "特批编码校验：邮件缺少特批编码信息" in blocked_notice.body
    assert all(item.get("workflow_code") == custom_rules["workflow_code"] for item in blocked_detail.get("review_failures", []))

    passed = create_inbound_mail(
        session,
        from_address="sales@jimuyida.com",
        subject="生产订单需求 - 常规销售流程",
        body_text="\n".join(
            [
                "客户名称：流程客户B",
                "产品：产品B",
                "数量：22台",
                "期望交期：2026-10-22",
                "订单号：SO-WF-REVIEW-002",
                "附件：采购订单",
                "特批编码：SP-7788",
            ]
        ),
    )
    passed_task = create_task_from_mail(session, passed)
    session.commit()

    assert passed_task is not None
    assert as_list(passed_task.target_mail_to_json) == ["zhangyan@jimuyida.com"]


def test_workflow_match_prefers_llm_selected_flow_when_multiple_active(monkeypatch):
    session = make_session()
    raw_text = """
流程一: 样机借用 下单
邮件收件人：zhangyan@jimuyida.com
邮件抄送人：sales.lead@jimuyida.com
邮件主题：[样机借用][{{task_no}}]
邮件内容模板：
流程类型：样机借用
附件：样机借用审批截图

流程二: 常规销售 下单
邮件收件人：hongdan@jimuyida.com
邮件抄送人：sales.lead@jimuyida.com
邮件主题：[常规销售][{{task_no}}]
邮件内容模板：
流程类型：常规销售
附件：采购订单
""".strip()
    import_workflow_document(
        session,
        file_path=None,
        raw_text=raw_text,
        prefer_llm=False,
        auto_publish=True,
        actor="tester",
    )
    session.commit()

    versions = session.query(WorkflowVersion).filter_by(status="Active").all()
    code_by_name = {}
    for version in versions:
        rule = loads(version.compiled_rules_json, {})
        code_by_name[str(rule.get("workflow_name"))] = str(rule.get("workflow_code"))
    sample_code = code_by_name["样机借用 下单"]

    def fake_call_model(*args, **kwargs):
        return {
            "choices": [
                {
                    "message": {
                        "content": dumps(
                            {
                                "workflow_code": sample_code,
                                "confidence": 89,
                                "reason": "邮件提到样机借用审批截图，优先走样机借用流程。",
                            }
                        )
                    }
                }
            ]
        }

    monkeypatch.setattr("backend.app.services.workflow_rules._active_model", lambda _session: object())
    monkeypatch.setattr("backend.app.services.workflow_rules.call_model", fake_call_model)

    mail = create_inbound_mail(
        session,
        from_address="sales@jimuyida.com",
        subject="生产订单需求 - 需要样机借用",
        body_text="客户名称：测试客户\n产品：G100\n数量：10台\n期望交期：2026-08-01\n样机借用审批截图：已上传",
    )
    match = match_workflow_for_mail(session, mail, mail.body_text)

    assert match is not None
    assert match.rule["workflow_code"] == sample_code
    assert match.confidence == 89
    assert any("LLM判定" in reason for reason in match.reasons)


def test_supplement_reply_uses_full_context_for_workflow_required_fields():
    session = make_session()
    raw_text = """
流程一: 样机借用 下单
邮件收件人：zhangyan@jimuyida.com
邮件抄送人：sales.lead@jimuyida.com
邮件主题：[样机借用][{{task_no}}]
邮件内容模板：
流程类型：样机借用
附件：样机借用审批截图

流程二: 常规销售 下单
邮件收件人：hongdan@jimuyida.com
邮件抄送人：sales.lead@jimuyida.com
邮件主题：[常规销售][{{task_no}}]
邮件内容模板：
流程类型：常规销售
附件：采购订单
""".strip()
    import_workflow_document(
        session,
        file_path=None,
        raw_text=raw_text,
        prefer_llm=False,
        auto_publish=True,
        actor="tester",
    )
    session.commit()

    original = create_inbound_mail(
        session,
        from_address="sales@jimuyida.com",
        subject="生产订单需求 - 样机借用 下单",
        body_text="\n".join(
            [
                "客户名称：样机客户",
                "产品：样机机型X",
                "数量：5台",
                "订单号：SO-SAMPLE-CTX-001",
                "样机借用审批截图：已附图",
            ]
        ),
    )
    task = create_task_from_mail(session, original)
    session.commit()
    assert task is None

    reply = create_inbound_mail(
        session,
        from_address="sales@jimuyida.com",
        subject="Re: 订单信息待补充",
        body_text="期望交期：2026-09-30",
    )
    created = process_mail_direct(session, reply)
    session.commit()

    assert created is not None
    assert as_list(created.target_mail_to_json) == ["zhangyan@jimuyida.com"]
    binding = session.query(RequirementWorkflowBinding).filter_by(requirement_id=created.requirement_id).one()
    assert binding.workflow_name == "样机借用 下单"
    assert "样机借用审批截图" not in as_list(binding.missing_fields_json)


def test_workflow_review_rules_are_not_mixed_with_global_initial_review_rules():
    session = make_session()
    set_config(
        session,
        "initial_review_rules_json",
        dumps(
            [
                {
                    "id": "global-blocker",
                    "name": "全局阻断规则",
                    "field": "source_text",
                    "operator": "contains",
                    "value": "永远不会出现",
                    "message": "命中全局阻断规则",
                    "enabled": True,
                }
            ]
        ),
        is_secret=False,
    )
    raw_text = """
流程一: 常规销售流程
邮件收件人：zhangyan@jimuyida.com
邮件抄送人：sales.lead@jimuyida.com
邮件主题：[常规][{{task_no}}]
邮件内容模板：
流程类型：常规销售
附件：采购订单
""".strip()
    import_workflow_document(
        session,
        file_path=None,
        raw_text=raw_text,
        prefer_llm=False,
        auto_publish=True,
        actor="tester",
    )
    session.commit()

    mail = create_inbound_mail(
        session,
        from_address="sales@jimuyida.com",
        subject="生产订单需求 - 常规销售流程",
        body_text="\n".join(
            [
                "客户名称：流程客户",
                "产品：常规产品A",
                "数量：30台",
                "期望交期：2026-10-10",
                "订单号：SO-WF-NO-GLOBAL-001",
                "附件：采购订单",
            ]
        ),
    )
    task = create_task_from_mail(session, mail)
    session.commit()

    assert task is not None
    assert as_list(task.target_mail_to_json) == ["zhangyan@jimuyida.com"]
    latest_review_case = (
        session.query(ExceptionCase)
        .filter_by(exception_type="ReviewNeedManual")
        .order_by(ExceptionCase.created_at.desc())
        .first()
    )
    if latest_review_case is not None:
        detail = loads(latest_review_case.detail, {})
        assert not any("全局阻断规则" in str(item.get("rule_name", "")) for item in detail.get("review_failures", []))


def test_initial_review_config_syncs_workflow_review_rules_as_custom_rules():
    session = make_session()
    import_structured_workflow_rules(
        session,
        rules=[
            {
                "workflow_code": "custom_review_flow",
                "workflow_name": "带规则流程",
                "match": {"any_keywords": ["带规则流程"]},
                "routing": {"to_names": ["zhangyan@jimuyida.com"]},
                "subject_template": "[带规则][{{task_no}}]",
                "body_template": "流程类型：{{workflow_name}}",
                "required_fields": [],
                "required_attachments": [],
                "review_rules": [
                    {
                        "id": "workflow-special-code",
                        "name": "特批编码校验",
                        "field": "source_text",
                        "operator": "contains",
                        "value": "特批编码",
                        "message": "邮件缺少特批编码信息",
                        "enabled": True,
                    }
                ],
            }
        ],
        actor="tester",
        auto_publish=True,
    )
    session.commit()

    display_config = initial_review_config(session, include_workflow_rules=True)
    execution_config = initial_review_config(session)

    workflow_rule = next(rule for rule in display_config["rules"] if rule.get("name") == "特批编码校验")
    assert workflow_rule.get("read_only") is not True
    assert workflow_rule.get("is_builtin") is not True
    assert workflow_rule["is_workflow_rule"] is True
    assert workflow_rule["workflow_name"] == "带规则流程"
    assert workflow_rule["id"].startswith("workflow:")
    assert not any(rule.get("name") == "特批编码校验" for rule in execution_config["rules"])


def test_workflow_review_rules_do_not_block_unmatched_mail_as_global_rules():
    session = make_session()
    import_structured_workflow_rules(
        session,
        rules=[
            {
                "workflow_code": "sample_gift_flow",
                "workflow_name": "样机赠送流程",
                "match": {"any_keywords": ["样机赠送"]},
                "routing": {"to_names": ["zhangyan@jimuyida.com"]},
                "subject_template": "[样机赠送][{{task_no}}]",
                "body_template": "流程类型：{{workflow_name}}",
                "required_fields": [],
                "required_attachments": [],
                "review_rules": [
                    {
                        "id": "sample-approval",
                        "name": "样机审批校验",
                        "field": "source_text",
                        "operator": "contains",
                        "value": "样机审批",
                        "message": "请补充样机审批信息",
                        "enabled": True,
                    }
                ],
            }
        ],
        actor="tester",
        auto_publish=True,
    )
    initial_review_config(session, include_workflow_rules=True)
    session.commit()

    mail = create_inbound_mail(
        session,
        from_address="sales@jimuyida.com",
        subject="生产订单需求 - 普通订单",
        body_text="\n".join(
            [
                "客户名称：普通客户",
                "产品：普通产品A",
                "数量：30台",
                "期望交期：2026-10-10",
                "订单号：SO-WF-NO-MATCH-001",
            ]
        ),
    )
    task = create_task_from_mail(session, mail)
    session.commit()

    assert task is not None
    assert workflow_binding_for_requirement(session, task.requirement_id) is None
    assert session.query(OutboundMailJob).filter_by(mail_type="RequirementSupplementRequest").count() == 0


def test_save_workflow_version_rules_dedupes_review_rules_by_signature():
    session = make_session()
    result = import_structured_workflow_rules(
        session,
        rules=[
            {
                "workflow_code": "dedupe_review_flow",
                "workflow_name": "规则去重流程",
                "match": {"any_keywords": ["规则去重流程"]},
                "routing": {"to_names": ["zhangyan@jimuyida.com"]},
                "subject_template": "[规则去重][{{task_no}}]",
                "body_template": "流程类型：{{workflow_name}}",
                "required_fields": [],
                "required_attachments": [],
                "review_rules": [],
            }
        ],
        actor="tester",
        auto_publish=False,
    )
    version_id = result["created_versions"][0]["id"]
    version = session.get(WorkflowVersion, version_id)
    rules = loads(version.compiled_rules_json, {})
    rules["review_rules"] = [
        {
            "id": "local-rule",
            "name": "流程复核建议",
            "field": "source_text",
            "operator": "contains",
            "value": "渠道备货",
            "message": "请编辑并启用该流程专属初审规则",
            "enabled": True,
        },
        {
            "id": f"workflow:{version_id}:local-rule",
            "name": "流程复核建议",
            "field": "source_text",
            "operator": "contains",
            "value": "渠 道 备 货",
            "message": "请编辑并启用该流程专属初审规则",
            "enabled": True,
        },
    ]

    saved = save_workflow_version_rules(session, version_id, compiled_rules=rules, actor="tester", activate=False)
    saved_rules = loads(saved.compiled_rules_json, {})

    assert len(saved_rules["review_rules"]) == 1
    assert saved_rules["review_rules"][0]["id"] == "local-rule"


def test_deleted_workflow_review_rule_is_not_restored_by_sync():
    session = make_session()
    import_structured_workflow_rules(
        session,
        rules=[
            {
                "workflow_code": "deletable_review_flow",
                "workflow_name": "可删除规则流程",
                "match": {"any_keywords": ["可删除规则流程"]},
                "routing": {"to_names": ["zhangyan@jimuyida.com"]},
                "subject_template": "[可删除][{{task_no}}]",
                "body_template": "流程类型：{{workflow_name}}",
                "required_fields": [],
                "required_attachments": [],
                "review_rules": [
                    {
                        "id": "deletable-code",
                        "name": "可删除规则",
                        "field": "source_text",
                        "operator": "contains",
                        "value": "特批编码",
                        "message": "邮件缺少特批编码信息",
                        "enabled": True,
                    }
                ],
            }
        ],
        actor="tester",
        auto_publish=True,
    )
    session.commit()

    first_config = initial_review_config(session, include_workflow_rules=True)
    workflow_rule_id = next(rule["id"] for rule in first_config["rules"] if rule.get("name") == "可删除规则")
    remember_deleted_workflow_review_rules(
        session,
        {str(rule.get("id")) for rule in first_config["rules"] if rule.get("id") != workflow_rule_id},
    )
    set_config(
        session,
        "initial_review_rules_json",
        dumps([rule for rule in first_config["rules"] if rule.get("id") != workflow_rule_id and not rule.get("is_builtin")]),
        is_secret=False,
    )
    session.commit()

    second_config = initial_review_config(session, include_workflow_rules=True)
    assert not any(rule.get("id") == workflow_rule_id for rule in second_config["rules"])


WORKFLOW_CONTACT_MAP = {
    "张燕": "zhangyan@jimuyida.com",
    "单涛": "dantao@jimuyida.com",
    "丁总": "dingyong@jimuyida.com",
    "金总": "jinzong@jimuyida.com",
    "罗总": "luozong@jimuyida.com",
    "张杏": "zhangxing@jimuyida.com",
    "洪丹": "hongdan@jimuyida.com",
    "曾鲜艳": "zengxianyan@jimuyida.com",
    "余烁": "yushuo@jimuyida.com",
    "袁辉": "yuanhui@jimuyida.com",
    "包亚敏": "baoyamin@jimuyida.com",
    "张洁仪": "zhangjieyi@jimuyida.com",
    "邢惠玲": "xinghuiling@jimuyida.com",
    "宋勤红": "songqinhong@jimuyida.com",
    "蒋文俊": "jiangwenjun@jimuyida.com",
    "张文鹏": "zhangwenpeng@jimuyida.com",
    "吴婉真": "wuwanzhen@jimuyida.com",
    "徐升": "xusheng@jimuyida.com",
    "销售直属领导": "sales.lead@jimuyida.com",
}


WORKFLOW_CASES = [
    {
        "name": "武汉仓出货硬件正常销售订单/样机赠送/电商平台/海外电商、渠道备货",
        "subject": "生产订单需求 - 武汉仓出货硬件正常销售订单",
        "expected_to": "zhangyan@jimuyida.com",
        "missing_label": "物流发货方式",
        "lines": [
            "客户名称：流程客户A",
            "产品：武汉仓标准设备A",
            "数量：20台",
            "期望交期：2026-07-01",
            "订单号：SO-WF-MATRIX-001",
            "物料详情描述：编码A1，规格标准版，20台",
            "物流发货方式：顺丰",
            "出货时间要求：2026-06-28",
            "客户收件信息：深圳南山区xx路",
            "交付要求：木箱加固",
            "附件：深圳积木与湖北积木的采购订单文档、海外渠道销售PI、特殊附作等",
        ],
    },
    {
        "name": "武汉仓出货硬件独立站补单/假期订单补单",
        "subject": "生产订单需求 - 武汉仓出货硬件独立站补单",
        "expected_to": "zhangyan@jimuyida.com",
        "missing_label": "物料详情描述",
        "lines": [
            "客户名称：流程客户B",
            "产品：独立站补单设备B",
            "数量：3台",
            "期望交期：2026-07-02",
            "订单号：SO-WF-MATRIX-002",
            "物料详情描述：编码B1，假期补单，3台",
            "附件：深圳积木与湖北积木的采购订单文档、海外渠道销售PI、特殊附作等",
        ],
    },
    {
        "name": "武汉仓出货硬件销售样机借用",
        "subject": "生产订单需求 - 武汉仓出货硬件销售样机借用",
        "expected_to": "zhangyan@jimuyida.com",
        "missing_label": "样机借用审批截图",
        "lines": [
            "客户名称：流程客户C",
            "产品：武汉仓样机C",
            "数量：1台",
            "期望交期：2026-07-03",
            "订单号：SO-WF-MATRIX-003",
            "物料详情描述：编码C1，样机，1台",
            "借用时间：2026-07-03至2026-07-20",
            "物流发货方式：顺丰",
            "出货时间要求：2026-07-03",
            "客户收件信息：广州天河区xx路",
            "样机借用审批截图：已上传",
            "附件：深圳积木与湖北积木的采购订单文档",
        ],
    },
    {
        "name": "海外仓出货硬件销售订单/样机赠送",
        "subject": "生产订单需求 - 海外仓出货硬件销售订单",
        "expected_to": "dantao@jimuyida.com",
        "missing_label": "出货仓/借货仓",
        "lines": [
            "客户名称：流程客户D",
            "产品：海外仓设备D",
            "数量：8台",
            "期望交期：2026-07-04",
            "订单号：SO-WF-MATRIX-004",
            "物料详情描述：编码D1，海外仓设备，8台",
            "物流发货方式：DHL",
            "出货仓：美国仓",
            "客户收件信息：海外客户地址",
            "交付要求：按PI发货",
            "附件：海外渠道销售PI、特殊附作等",
        ],
    },
    {
        "name": "海外仓出货硬件销售样机借用",
        "subject": "生产订单需求 - 海外仓出货硬件销售样机借用",
        "expected_to": "dantao@jimuyida.com",
        "missing_label": "归还时间",
        "lines": [
            "客户名称：流程客户E",
            "产品：海外仓样机E",
            "数量：1台",
            "期望交期：2026-07-05",
            "订单号：SO-WF-MATRIX-005",
            "物料详情描述：编码E1，海外样机，1台",
            "归还时间：2026-08-05",
            "出货仓：德国仓",
            "客户收件信息：海外样机地址",
            "样机借用审批截图：已上传",
        ],
    },
]


def workflow_import_text_with_email_routing() -> str:
    sections: list[str] = []
    for index, case in enumerate(WORKFLOW_CASES, start=1):
        sections.append(
            "\n".join(
                [
                    f"流程{index}: {case['name']}",
                    f"邮件收件人：{case['expected_to']}",
                    "邮件抄送人：sales.lead@jimuyida.com",
                    "邮件主题：[生产任务单][{{task_no}}][{{customer_name}}][{{product_summary}}][V{{version_no}}]",
                    "邮件内容模板：",
                    *case["lines"],
                ]
            )
        )
    return "\n\n".join(sections)


def prepare_imported_workflow_session():
    session = make_session()
    import_workflow_document(
        session,
        file_path=None,
        raw_text=workflow_import_text_with_email_routing(),
        prefer_llm=False,
        auto_publish=True,
        actor="tester",
    )
    session.commit()
    return session


@pytest.mark.parametrize("case", WORKFLOW_CASES, ids=[item["name"] for item in WORKFLOW_CASES])
def test_imported_business_workflow_cases_pass_initial_review_and_route(case):
    session = prepare_imported_workflow_session()
    mail = create_inbound_mail(
        session,
        from_address="bot.sales@jimuyida.com",
        subject=case["subject"],
        body_text="\n".join(case["lines"]),
    )

    task = create_task_from_mail(session, mail)
    session.commit()

    assert task is not None
    assert as_list(task.target_mail_to_json)[0] == case["expected_to"]
    binding = session.query(RequirementWorkflowBinding).filter_by(requirement_id=task.requirement_id).one()
    assert binding.workflow_name == case["name"]
    assert as_list(binding.missing_fields_json) == []


@pytest.mark.parametrize("case", WORKFLOW_CASES, ids=[item["name"] for item in WORKFLOW_CASES])
def test_imported_business_workflow_cases_fail_initial_review_when_required_field_missing(case):
    session = prepare_imported_workflow_session()
    missing_label = case["missing_label"]
    lines = [line for line in case["lines"] if not line.startswith(f"{missing_label.split('/')[0]}：")]
    mail = create_inbound_mail(
        session,
        from_address="bot.sales@jimuyida.com",
        subject=f"{case['subject']} - 缺字段",
        body_text="\n".join(lines),
    )

    task = create_task_from_mail(session, mail)
    session.commit()

    assert task is None
    case_row = (
        session.query(ExceptionCase)
        .filter_by(exception_type="ReviewNeedManual")
        .order_by(ExceptionCase.created_at.desc())
        .first()
    )
    assert case_row is not None
    detail = loads(case_row.detail, {})
    assert any(missing_label in item.get("message", "") for item in detail.get("review_failures", []))
    notice = session.query(OutboundMailJob).filter_by(mail_type="RequirementSupplementRequest").order_by(OutboundMailJob.created_at.desc()).first()
    assert notice is not None
    assert "当前使用流程" in notice.body
    assert "流程编号：" in notice.body
    assert "流程版本ID：" in notice.body
    assert "未满足该流程下的规则" in notice.body
    assert missing_label in notice.body


def test_imported_workflow_routes_task_after_explicit_email_routing():
    session = make_session()
    import_workflow_document(
        session,
        file_path=None,
        raw_text=workflow_import_text_with_email_routing(),
        prefer_llm=False,
        auto_publish=True,
        actor="tester",
    )
    session.commit()

    mail = create_inbound_mail(
        session,
        from_address="sales@jimuyida.com",
        subject="生产订单需求 - 武汉仓出货硬件正常销售订单",
        body_text="\n".join(
            [
                "客户名称：测试客户",
                "产品：G100",
                "数量：20台",
                "期望交期：2026-07-01",
                "订单号：SO-WF-001",
                "物料详情描述：编码A1，规格标准版，20台",
                "物流发货方式：顺丰",
                "出货时间要求：2026-06-28",
                "客户收件信息：深圳南山区xx路",
                "交付要求：木箱加固",
                "附件：深圳积木与湖北积木的采购订单文档、海外渠道销售PI、特殊附作等",
            ]
        ),
    )

    task = create_task_from_mail(session, mail)
    session.commit()

    assert task is not None
    assert as_list(task.target_mail_to_json) == ["zhangyan@jimuyida.com"]
    binding = session.query(RequirementWorkflowBinding).filter_by(requirement_id=task.requirement_id).one()
    assert binding.workflow_code
    assert "物流发货方式" not in as_list(binding.missing_fields_json)


def test_workflow_material_details_are_extracted_and_rendered_in_task_mail():
    session = make_session()
    import_structured_workflow_rules(
        session,
        rules=[
            {
                "workflow_code": "material_detail_flow",
                "workflow_name": "物料详情流程",
                "match": {"subject_patterns": ["物料详情测试"]},
                "routing": {"to_names": ["zhangyan@jimuyida.com"], "cc_names": []},
                "subject_template": "[生产任务单][{{task_no}}][{{customer_name}}][{{product_summary}}][V{{version_no}}]",
                "body_template": "\n".join(
                    [
                        "生产部同事好：",
                        "客户名称：{{customer_name}}",
                        "物料/规格：{{product_summary}}",
                        "数量：{{quantity_text}}",
                        "期望交期：{{expected_delivery_date}}",
                    ]
                ),
                "required_fields": [
                    "customer_name",
                    "product_summary",
                    "quantity_text",
                    "expected_delivery_date",
                    "material_details",
                ],
                "required_attachments": [],
                "review_rules": [],
            }
        ],
        actor="tester",
        auto_publish=True,
        llm_used=False,
    )
    session.commit()
    mail = create_inbound_mail(
        session,
        from_address="sales@jimuyida.com",
        subject="生产订单需求 - 物料详情测试",
        body_text="\n".join(
            [
                "客户名称：测试客户",
                "产品：示例产品A",
                "期望交期：2026-07-01",
                "物料详情描述：",
                "物料编码：MAT-A001",
                "物料名称：示例产品A",
                "数量：120套",
                "物流发货方式：顺丰",
            ]
        ),
    )

    task = create_task_from_mail(session, mail)
    session.commit()

    assert task is not None
    binding = session.query(RequirementWorkflowBinding).filter_by(requirement_id=task.requirement_id).one()
    assert "物料详情描述" not in as_list(binding.missing_fields_json)
    extracted = loads(binding.extracted_fields_json, {})
    assert "物料编码：MAT-A001" in extracted["material_details"]
    assert "物料名称：示例产品A" in extracted["material_details"]
    assert "数量：120套" in extracted["material_details"]
    version = session.query(ProductionTaskVersion).filter_by(task_id=task.id, version_no=1).one()
    assert "物料详情描述：" in version.body
    assert "物料编码：MAT-A001" in version.body
    assert "物料名称：示例产品A" in version.body
    assert "数量：120套" in version.body


def test_one_mail_with_multiple_material_items_creates_multiple_tasks():
    session = make_session()
    configure_department(session)
    mail = create_inbound_mail(
        session,
        from_address="sales@jimuyida.com",
        subject="客户深圳易搭有限公司下单需求",
        body_text="\n".join(
            [
                "客户名称：深圳易搭有限公司",
                "物料详情描述：",
                "1.物料编码：1300100257，物料名称：三维扫描仪 FOX(4规)（国内版）   数量：1",
                "2.物料编码：1050600001，物料名称：树脂大卫头像(带纸盒) 数量：1",
                "",
                "出货时间要求：2026-06-11",
                "订单号：12345678902222333",
                "物流发货方式：顺丰",
                "出货仓：武汉仓",
                "交付要求：正常交付",
                "客户收件信息：",
                "收件人：陈女士",
                "电话：18818881234",
                "地址：广东省深圳市南山区李宁中心，0000",
            ]
        ),
    )

    first_task = create_task_from_mail(session, mail)
    session.commit()

    tasks = (
        session.query(ProductionTask)
        .join(OrderRequirement, OrderRequirement.id == ProductionTask.requirement_id)
        .filter(OrderRequirement.source_mail_id == mail.id)
        .order_by(ProductionTask.created_at, ProductionTask.id)
        .all()
    )
    assert first_task is not None
    assert first_task.id == tasks[0].id
    assert len(tasks) == 2
    assert mail.related_task_id == tasks[0].id
    assert [task.requirement.quantity_text for task in tasks] == ["1", "1"]
    assert "1300100257" in tasks[0].requirement.product_summary
    assert "三维扫描仪 FOX" in tasks[0].requirement.product_summary
    assert "1050600001" in tasks[1].requirement.product_summary
    assert "树脂大卫头像" in tasks[1].requirement.product_summary

    issue_jobs = session.query(OutboundMailJob).filter_by(mail_type="TaskIssue").order_by(OutboundMailJob.created_at).all()
    assert len(issue_jobs) == 2
    assert "1300100257" in issue_jobs[0].body
    assert "1050600001" not in issue_jobs[0].body
    assert "1050600001" in issue_jobs[1].body
    assert "1300100257" not in issue_jobs[1].body

    ack = session.query(OutboundMailJob).filter_by(mail_type="SalesReceiptAck").one()
    assert tasks[0].task_no in ack.body
    assert tasks[1].task_no in ack.body


def test_imported_workflow_without_primary_contact_mapping_routes_to_internal_exception():
    session = make_session()
    import_workflow_document(
        session,
        file_path="/Users/kaimao/github/jm-sp-bot/docs/商务部邮件下单流程梳理.docx",
        raw_text=None,
        prefer_llm=False,
        auto_publish=True,
        actor="tester",
    )
    session.commit()
    mail = create_inbound_mail(
        session,
        from_address="sales@jimuyida.com",
        subject="生产订单需求 - 武汉仓出货硬件正常销售订单",
        body_text="\n".join(
            [
                "客户名称：测试客户",
                "产品：G100",
                "数量：20台",
                "期望交期：2026-07-01",
                "订单号：SO-WF-002",
                "物料详情描述：编码A1，规格标准版，20台",
                "物流发货方式：顺丰",
                "出货时间要求：2026-06-28",
                "客户收件信息：深圳南山区xx路",
                "交付要求：木箱加固",
                "附件：深圳积木与湖北积木的采购订单文档、海外渠道销售PI、特殊附作等",
            ]
        ),
    )

    task = create_task_from_mail(session, mail)
    session.commit()

    assert task is None
    case = session.query(ExceptionCase).filter_by(exception_type="RoutingMissing").order_by(ExceptionCase.created_at.desc()).first()
    assert case is not None
    detail = loads(case.detail, {})
    assert "生产部门邮箱未配置" in detail["message"]
    assert detail.get("unresolved_contacts") == []
    assert session.query(OutboundMailJob).filter_by(mail_type="RequirementSupplementRequest").count() == 0


def test_non_email_workflow_cc_contact_is_discarded_and_does_not_fail_initial_review():
    session = make_session()
    import_workflow_document(
        session,
        file_path=None,
        raw_text="""
流程一: 抄送动态角色流程
邮件收件人：zhangyan@jimuyida.com
邮件抄送人：销售直属领导
邮件主题：[抄送测试][{{task_no}}]
邮件内容模板：
流程类型：抄送动态角色流程
附件：采购订单
""".strip(),
        prefer_llm=False,
        auto_publish=True,
        actor="tester",
    )
    session.commit()
    mail = create_inbound_mail(
        session,
        from_address="sales@jimuyida.com",
        subject="生产订单需求 - 抄送动态角色流程",
        body_text="\n".join(
            [
                "客户名称：测试客户",
                "产品：G100",
                "数量：20台",
                "期望交期：2026-07-01",
                "订单号：SO-WF-CC-001",
                "附件：采购订单",
            ]
        ),
    )

    task = create_task_from_mail(session, mail)
    session.commit()

    assert task is not None
    assert as_list(task.target_mail_to_json) == ["zhangyan@jimuyida.com"]
    assert as_list(task.target_mail_cc_json) == []
    binding = session.query(RequirementWorkflowBinding).filter_by(requirement_id=task.requirement_id).one()
    assert as_list(binding.unresolved_contacts_json) == []
    assert session.query(ExceptionCase).filter_by(exception_type="ReviewNeedManual").count() == 0
    assert session.query(OutboundMailJob).filter_by(mail_type="RequirementSupplementRequest").count() == 0
