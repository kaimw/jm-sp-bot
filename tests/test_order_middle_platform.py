from __future__ import annotations

import io
import json
from types import SimpleNamespace

import pytest
from fastapi import HTTPException
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from backend.app.database import Base
from backend.app.models import AgentRunLog, AuditEvent, ChannelPricing, CrmOrderItem, CrmOrderSnapshot, CrmSalesOrder, DeliveryNotice, ExceptionCase, IntegrationEvent, MiddlePlatformOrder, MiddlePlatformOrderItem, ModelCallLog, ModelProviderConfig, OrderAttachment, OutboundMailJob, ProcessingJob, ProductInventorySnapshot, ProductSKU, ProductSPU, SystemConfig
from backend.app.services.attachment_parser import parse_attachment
from backend.app.services.bootstrap import seed_defaults, set_config
from backend.app.services.crm_sync import CrmSyncBusyError, config_value as crm_sync_config_value, ensure_request_file, preflight_crm_cdp_browser, retry_crm_order_detail_sync, sync_crm_products_as_skus, sync_customer_mapping_from_masters, upsert_crm_sales_orders
from backend.app.services.crypto import encrypt_value
from backend.app.services.customer_mapping import enqueue_oms_customer_missing_notification, find_customer_in_oms_response, query_oms_customer, upsert_customer_mapping
from backend.app.services.crm_attachment_extraction import enrich_order_from_attachment_text
from backend.app.services import crm_attachment_extraction
from backend.app.services.jobs import run_pending_jobs
from backend.app.services.order_middle_platform import (
    IllegalStateTransition,
    OrderEvent,
    OrderStatus,
    confirm_delivery_notice,
    crm_order_parsed_event,
    process_crm_order_parsed_event,
    process_oms_status_update,
    run_validation_chain,
    transition_order,
    upsert_middle_platform_order,
)
from backend.app.services.jsonutil import dumps, loads
from backend.app.services.rules import DEFAULT_RULES, review_rule_config
from backend.app.main import delete_crm_order, exception_context, exception_diagnosis_feedback, list_agent_run_logs, list_model_call_logs, replay_v2_delivery_notice, serialize_crm_order, serialize_crm_order_with_flow, update_crm_config, update_oms_config
from backend.app.schemas import CrmRuntimeConfigUpdate, OmsRuntimeConfigUpdate


def make_session():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(bind=engine)
    Session = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)
    session = Session()
    seed_defaults(session)
    session.commit()
    return session


def seed_active_sku(session, sku_id: str = "SKU-3D-SCANNER-PRO") -> None:
    spu = ProductSPU(spu_id="SPU-3D-SCANNER", name="3D Scanner")
    session.add(spu)
    session.flush()
    session.add(ProductSKU(spu_uuid=spu.id, sku_id=sku_id, status="Active"))
    session.commit()


def seed_inventory(session, sku_id: str = "SKU-3D-SCANNER-PRO", quantity: int = 100) -> None:
    session.add(
        ProductInventorySnapshot(
            material_code=sku_id,
            material_name="3D Scanner",
            warehouse_code="WH01",
            warehouse_name="武汉工厂仓",
            base_qty=quantity,
            qty=quantity,
            source_payload_json=dumps({"canUseQuantity": quantity}),
        )
    )
    session.commit()


def seed_oms_required_config(session) -> None:
    set_config(session, "oms_owner_code", "OWNER-001")
    set_config(session, "oms_warehouse_code", "WH-001")
    set_config(session, "oms_shop_code", "SHOP-001")
    set_config(session, "oms_logistic_code", "SF")
    session.commit()


def complete_crm_order_required_fields(session, crm_order_id: str = "crm_obj_001") -> CrmSalesOrder:
    crm = session.query(CrmSalesOrder).filter_by(crm_order_id=crm_order_id).one()
    crm.receipt_contact = "张三"
    crm.receipt_phone = "18600001111"
    crm.receipt_address = "湖北省武汉市东湖高新区测试路 1 号"
    session.commit()
    return crm


def add_crm_order_item(session, crm: CrmSalesOrder, **overrides) -> CrmOrderItem:
    payload = {
        "product_name": "3D Scanner",
        "quantity": "1",
        "unit_price": "100.00",
        "line_amount": "100.00",
        **overrides,
    }
    item = CrmOrderItem(
        order_id=crm.id,
        source_system=crm.source_system,
        crm_item_id=payload.get("crm_item_id") or f"{crm.crm_order_id}:1",
        crm_order_id=crm.crm_order_id,
        crm_order_no=crm.crm_order_no,
        sku_code=payload.get("sku_code"),
        product_name=payload.get("product_name"),
        specification=payload.get("specification"),
        quantity=payload.get("quantity"),
        unit_price=payload.get("unit_price"),
        line_amount=payload.get("line_amount"),
        payload_hash=crm.payload_hash,
        raw_json=dumps({key: value for key, value in payload.items() if value is not None}),
    )
    session.add(item)
    return item


def valid_crm_order_row(**overrides):
    row = {
        "crm_order_id": "crm_obj_001",
        "crm_order_no": "SO-001",
        "customer_name": "亚马逊北美渠道",
        "sales_user_name": "Alice",
        "sales_user_email": "alice@jimuyida.com",
        "owner_department": "商务一部",
        "life_status": "normal",
        "approval_status": "approved",
        "order_date": "2026-06-12",
        "order_amount": "125000.00",
        "received_amount": "25000.00",
        "receivable_amount": "100000.00",
        "product_amount": "125000.00",
        "settlement_method": "option1",
        "receipt_contact": "张三",
        "receipt_phone": "18600001111",
        "receipt_address": "湖北省武汉市东湖高新区测试路 1 号",
        "delivery_date": "2026-06-30",
        "attachment_files": "采购订单.pdf; 合同盖章版.pdf",
        "order_items": [{"sku_code": "SKU-3D-SCANNER-PRO", "quantity": 50, "unit_price": "2500", "line_amount": "125000"}],
    }
    row.update(overrides)
    return row


def create_delivery_ready_order(session):
    seed_oms_required_config(session)
    seed_active_sku(session)
    seed_inventory(session, quantity=100)
    result = upsert_crm_sales_orders(session, [valid_crm_order_row()])
    session.commit()
    assert result["queued_events"] == 1
    complete_crm_order_required_fields(session)
    run_pending_jobs(session)
    return session.query(MiddlePlatformOrder).one()


def create_delivery_ready_order_without_oms_config(session):
    seed_active_sku(session)
    seed_inventory(session, quantity=100)
    result = upsert_crm_sales_orders(session, [valid_crm_order_row()])
    session.commit()
    assert result["queued_events"] == 1
    complete_crm_order_required_fields(session)
    run_pending_jobs(session)
    return session.query(MiddlePlatformOrder).one()


def test_crm_order_event_builds_delivery_preview_then_pushes_after_confirmation():
    session = make_session()
    order = create_delivery_ready_order(session)
    assert order.status == OrderStatus.DELIVERY_NOTICE_READY.value
    assert order.delivery_notices[0].status == "Previewed"
    assert order.delivery_notices[0].source_snapshot_hash == order.payload_hash
    assert order.delivery_notices[0].notice_version == 1
    assert order.delivery_notices[0].confirmed_at is None

    confirm_delivery_notice(session, order.delivery_notices[0], confirmed_by="tester")
    session.commit()
    second = run_pending_jobs(session)

    assert second["completed"] == 1
    session.refresh(order)
    assert order.status == OrderStatus.OMS_ACCEPTED.value
    assert order.delivery_notices[0].status == "Accepted"
    assert order.delivery_notices[0].confirmed_by == "tester"


def test_delete_crm_order_clears_local_workflow_for_resync():
    session = make_session()
    order = create_delivery_ready_order(session)
    crm = session.query(CrmSalesOrder).filter_by(crm_order_id=order.crm_order_id).one()
    session.add(
        ProcessingJob(
            job_type="OMS_PUSH_NOTICE",
            payload_json=dumps({"order_id": order.id, "notice_id": order.delivery_notices[0].id}),
            status="Pending",
        )
    )
    session.add(
        ExceptionCase(
            exception_type="VALIDATION_BLOCKED",
            severity="High",
            detail=dumps({"order_no": order.order_no, "crm_order_no": crm.crm_order_no}),
        )
    )
    session.commit()

    result = delete_crm_order(crm.id, session=session, current_user=SimpleNamespace(username="admin"))

    assert result["ok"] is True
    assert result["crm_order_no"] == crm.crm_order_no
    assert session.query(CrmSalesOrder).filter_by(crm_order_id=crm.crm_order_id).count() == 0
    assert session.query(CrmOrderItem).filter_by(crm_order_id=crm.crm_order_id).count() == 0
    assert session.query(CrmOrderSnapshot).filter_by(crm_order_id=crm.crm_order_id).count() == 0
    assert session.query(OrderAttachment).filter_by(crm_order_id=crm.crm_order_id).count() == 0
    assert session.query(MiddlePlatformOrder).filter_by(crm_order_id=crm.crm_order_id).count() == 0
    assert session.query(MiddlePlatformOrderItem).count() == 0
    assert session.query(DeliveryNotice).count() == 0
    assert session.query(ProcessingJob).filter(ProcessingJob.payload_json.contains(crm.crm_order_id)).count() == 0
    assert session.query(IntegrationEvent).filter_by(biz_key=crm.crm_order_id).count() == 0
    assert session.query(ExceptionCase).count() == 0
    audit = session.query(AuditEvent).filter_by(event_type="CrmOrderLocalDeleted").one()
    assert audit.related_object_id == crm.crm_order_id


def test_delivery_confirmation_blocks_when_oms_required_config_missing():
    session = make_session()
    order = create_delivery_ready_order_without_oms_config(session)

    with pytest.raises(RuntimeError, match="OMS 下推必填字段缺失"):
        confirm_delivery_notice(session, order.delivery_notices[0], confirmed_by="tester")
    session.commit()

    session.refresh(order)
    assert order.status == OrderStatus.DELIVERY_NOTICE_READY.value
    assert order.delivery_notices[0].status == "Blocked"
    case = session.query(ExceptionCase).one()
    assert case.exception_type == "OMS_REQUIRED_FIELDS_MISSING"
    assert "货主CODE" in case.detail


def test_platform_fulfilled_order_archives_without_delivery_notice():
    session = make_session()
    seed_active_sku(session)
    seed_inventory(session, quantity=100)
    result = upsert_crm_sales_orders(session, [valid_crm_order_row(fulfillment_type="FBA")])
    session.commit()
    assert result["queued_events"] == 1
    complete_crm_order_required_fields(session)

    job_result = run_pending_jobs(session)

    order = session.query(MiddlePlatformOrder).one()
    summary = loads(order.validation_summary_json, {})
    assert job_result["completed"] == 1
    assert order.status == OrderStatus.FULFILLMENT_ARCHIVED.value
    assert order.delivery_notices == []
    assert summary["fulfillment"]["type"] == "PLATFORM_FULFILLED"
    assert session.query(AuditEvent).filter_by(event_type="PlatformFulfilledOrderArchived").count() == 1


def test_ecommerce_order_amount_apportionment_preserves_paid_total():
    session = make_session()
    seed_active_sku(session, "SKU-3D-SCANNER-PRO")
    spu = session.query(ProductSPU).filter_by(spu_id="SPU-3D-SCANNER").one()
    session.add(ProductSKU(spu_uuid=spu.id, sku_id="SKU-3D-SCANNER-LITE", status="Active"))
    session.commit()
    seed_inventory(session, "SKU-3D-SCANNER-PRO", quantity=100)
    seed_inventory(session, "SKU-3D-SCANNER-LITE", quantity=100)
    row = valid_crm_order_row(
        order_amount="380.00",
        product_amount="380.00",
        received_amount="25.00",
        receivable_amount="355.00",
        total_discount="40.00",
        shipping_fee="20.00",
        total_paid_amount="380.00",
        order_items=[
            {"sku_code": "SKU-3D-SCANNER-PRO", "quantity": 1, "unit_price": "100.00", "line_amount": "100.00"},
            {"sku_code": "SKU-3D-SCANNER-LITE", "quantity": 1, "unit_price": "300.00", "line_amount": "300.00"},
        ],
    )
    result = upsert_crm_sales_orders(session, [row])
    session.commit()
    assert result["queued_events"] == 1
    complete_crm_order_required_fields(session)

    run_pending_jobs(session)

    order = session.query(MiddlePlatformOrder).one()
    amounts = [str(item.line_amount) for item in sorted(order.items, key=lambda item: item.sku_code or "")]
    apportionment = [loads(item.raw_json, {}).get("apportionment", {}) for item in order.items]
    assert order.status == OrderStatus.DELIVERY_NOTICE_READY.value
    assert amounts == ["285.00", "95.00"]
    assert sum(item.line_amount for item in order.items) == order.order_amount
    assert {item.get("method") for item in apportionment} == {"proportional_with_last_line_correction"}


def test_channel_shop_sku_maps_to_standard_sku_before_pre_review():
    session = make_session()
    seed_active_sku(session, "SKU-3D-SCANNER-PRO")
    seed_inventory(session, "SKU-3D-SCANNER-PRO", quantity=100)
    sku = session.query(ProductSKU).filter_by(sku_id="SKU-3D-SCANNER-PRO").one()
    session.add(ChannelPricing(sku_uuid=sku.id, channel="amazon_us", channel_sku_id="AMZ-SCANNER-PRO", map_price=10000, currency="USD"))
    session.commit()
    row = valid_crm_order_row(
        channel_code="amazon_us",
        shop_code="AMZ-US-01",
        platform_order_no="AMZ-ORDER-001",
        order_items=[{"shop_sku_code": "AMZ-SCANNER-PRO", "quantity": 50, "unit_price": "2500", "line_amount": "125000"}],
    )
    result = upsert_crm_sales_orders(session, [row])
    session.commit()
    assert result["queued_events"] == 1
    complete_crm_order_required_fields(session)

    run_pending_jobs(session)

    order = session.query(MiddlePlatformOrder).one()
    item = order.items[0]
    raw = loads(item.raw_json, {})
    assert order.status == OrderStatus.DELIVERY_NOTICE_READY.value
    assert order.source_policy == "CRM_ONLY"
    assert order.channel_code == "amazon_us"
    assert order.shop_code == "AMZ-US-01"
    assert order.platform_order_no == "AMZ-ORDER-001"
    assert item.shop_sku_code == "AMZ-SCANNER-PRO"
    assert item.sku_code == "SKU-3D-SCANNER-PRO"
    assert raw["sku_mapping"]["source"] == "channel_pricing"


def test_missing_channel_shop_sku_mapping_blocks_pre_review():
    session = make_session()
    row = valid_crm_order_row(
        channel_code="amazon_us",
        shop_code="AMZ-US-01",
        order_items=[{"shop_sku_code": "UNKNOWN-AMZ-SKU", "quantity": 1, "unit_price": "100", "line_amount": "100"}],
        order_amount="100.00",
        product_amount="100.00",
        received_amount="20.00",
        receivable_amount="80.00",
    )
    result = upsert_crm_sales_orders(session, [row])
    session.commit()
    assert result["queued_events"] == 1

    run_pending_jobs(session)

    order = session.query(MiddlePlatformOrder).one()
    failed_rules = loads(order.validation_summary_json, {}).get("results", [])
    assert order.status == OrderStatus.VALIDATION_BLOCKED.value
    assert any(rule["rule_code"] == "SKU_MAPPING_MISSING" for rule in failed_rules)
    assert session.query(ExceptionCase).filter_by(exception_type="VALIDATION_BLOCKED").count() == 1


def test_oms_idempotency_conflict_is_resolved_by_reverse_lookup(monkeypatch):
    import backend.app.services.order_middle_platform as omp

    class FakeJackyunClient:
        def create_delivery_order(self, payload, *, method="wms.order.create"):
            return {"ok": False, "message": "订单已存在，重复请求", "raw": {"code": "DUPLICATE"}}

        def query_delivery_orders(self, payload):
            return {
                "ok": True,
                "data": {"rows": [{"erporderNo": payload["erporderNo"], "orderNo": "OMS-EXISTING-001"}]},
                "raw": {"rows": [{"erporderNo": payload["erporderNo"], "orderNo": "OMS-EXISTING-001"}]},
            }

    session = make_session()
    set_config(session, "oms_enabled", "true")
    set_config(session, "oms_mock_success", "false")
    session.commit()
    monkeypatch.setattr(omp, "jackyun_client_from_session", lambda _session: FakeJackyunClient())
    order = create_delivery_ready_order(session)

    confirm_delivery_notice(session, order.delivery_notices[0], confirmed_by="tester")
    session.commit()
    run_pending_jobs(session)

    session.refresh(order)
    assert order.status == OrderStatus.OMS_ACCEPTED.value
    assert order.delivery_notices[0].oms_order_no == "OMS-EXISTING-001"
    assert order.delivery_notices[0].status == "Accepted"


def test_oms_blocked_replay_requires_repair_evidence(monkeypatch):
    import backend.app.services.order_middle_platform as omp

    class FailingJackyunClient:
        def create_delivery_order(self, payload, *, method="wms.order.create"):
            return {"ok": False, "message": "OMS 主数据缺失", "raw": {"code": "MASTER_DATA_MISSING"}}

        def query_delivery_orders(self, payload):
            return {"ok": True, "data": {"rows": []}, "raw": {}}

    session = make_session()
    set_config(session, "oms_enabled", "true")
    set_config(session, "oms_mock_success", "false")
    set_config(session, "oms_max_retries", "1")
    session.commit()
    monkeypatch.setattr(omp, "jackyun_client_from_session", lambda _session: FailingJackyunClient())

    order = create_delivery_ready_order(session)
    notice = order.delivery_notices[0]
    confirm_delivery_notice(session, notice, confirmed_by="tester")
    session.commit()
    run_pending_jobs(session)
    session.refresh(order)
    session.refresh(notice)
    assert order.status == OrderStatus.OMS_BLOCKED.value
    assert notice.status == "Blocked"
    blocked_mail = session.query(OutboundMailJob).filter_by(mail_type="V2OmsBlocked").one()
    assert "OMS/WMS 发货单下推重试已达到上限" in blocked_mail.body
    assert notice.notice_no in blocked_mail.body
    assert session.query(AuditEvent).filter_by(event_type="OmsBlockedNotificationQueued").count() == 1
    oms_case = session.query(ExceptionCase).filter_by(exception_type="OMS_BLOCKED").one()
    context = exception_context(oms_case.id, session)
    assert context["oms_replay"]["ready"] is True
    assert context["oms_replay"]["notice_id"] == notice.id
    assert context["oms_replay"]["evidence_required"] is True

    with pytest.raises(HTTPException, match="修复证据") as exc_info:
        replay_v2_delivery_notice(notice.id, {}, session)
    assert exc_info.value.status_code == 400
    assert session.query(AuditEvent).filter_by(event_type="ManualReplayWithoutFixBlocked").count() == 1
    assert session.query(ExceptionCase).filter_by(exception_type="MANUAL_REPLAY_WITHOUT_FIX").count() == 1

    result = replay_v2_delivery_notice(notice.id, {"repair_evidence": "已补齐 OMS 货主主数据", "actor": "ops"}, session)

    session.refresh(order)
    session.refresh(notice)
    assert result["queued"] is True
    assert result["repair_evidence_recorded"] is True
    assert result["resolved_exceptions"] == 2
    assert order.status == OrderStatus.OMS_PENDING.value
    assert notice.status == "Confirmed"
    assert notice.confirmed_by == "ops"
    assert session.query(AuditEvent).filter_by(event_type="OmsReplayRepairEvidenceRecorded").count() == 1
    assert session.query(ExceptionCase).filter(ExceptionCase.status == "Open").count() == 0
    assert session.query(AuditEvent).filter_by(event_type="ExceptionResolvedForOmsReplay").count() == 2
    oms_event = session.query(IntegrationEvent).filter_by(event_type="OMS_PUSH_NOTICE", biz_key=notice.notice_no).order_by(IntegrationEvent.updated_at.desc()).first()
    assert oms_event is not None
    assert oms_event.status in {"Dead", "Pending"}
    for case in session.query(ExceptionCase).all():
        evidence = loads(case.resolution_evidence_json, {})
        assert evidence["type"] == "OMS_REPLAY"
        assert evidence["repair_evidence"] == "已补齐 OMS 货主主数据"
        assert evidence["notice_id"] == notice.id


def test_oms_status_sync_advances_to_picking_and_shipped():
    session = make_session()
    order = create_delivery_ready_order(session)
    confirm_delivery_notice(session, order.delivery_notices[0], confirmed_by="tester")
    session.commit()
    run_pending_jobs(session)
    session.refresh(order)
    assert order.status == OrderStatus.OMS_ACCEPTED.value

    result = process_oms_status_update(session, {"notice_id": order.delivery_notices[0].id, "oms_status": "拣货中"})
    session.commit()
    session.refresh(order)
    assert result["normalized_status"] == "picking"
    assert order.status == OrderStatus.PICKING.value
    assert order.delivery_notices[0].status == "Picking"

    result = process_oms_status_update(session, {"notice_id": order.delivery_notices[0].id, "oms_status": "已发货", "raw": {"carrier": "SF"}})
    session.commit()
    session.refresh(order)
    assert result["normalized_status"] == "shipped"
    assert order.status == OrderStatus.FULFILLMENT_ARCHIVED.value
    assert order.delivery_notices[0].status == "Shipped"


def test_oms_status_sync_job_can_skip_directly_to_shipped():
    session = make_session()
    order = create_delivery_ready_order(session)
    confirm_delivery_notice(session, order.delivery_notices[0], confirmed_by="tester")
    session.commit()
    run_pending_jobs(session)
    session.refresh(order)

    session.add(
        ProcessingJob(
            job_type="OMS_STATUS_SYNC",
            payload_json=dumps({"notice_id": order.delivery_notices[0].id, "oms_status": "shipped"}),
            status="Pending",
        )
    )
    session.commit()
    result = run_pending_jobs(session)

    assert result["completed"] == 1
    session.refresh(order)
    assert order.status == OrderStatus.FULFILLMENT_ARCHIVED.value
    assert order.delivery_notices[0].status == "Shipped"


def test_oms_waybill_print_job_saves_waybill_and_outbound_proof(monkeypatch):
    import backend.app.services.order_middle_platform as omp

    class FakeJackyunClient:
        def create_delivery_order(self, payload, *, method="wms.order.create"):
            return {"ok": True, "data": {"orderNo": "OMS-WAYBILL-001"}, "raw": {"orderNo": "OMS-WAYBILL-001"}}

        def print_delivery_label(self, payload):
            assert payload["deliveryNo"] == "OMS-WAYBILL-001"
            return {"ok": True, "data": {"waybillNo": "TRACK-001", "printData": "JVBERi0xLjQ="}, "raw": {}}

    session = make_session()
    set_config(session, "oms_enabled", "true")
    set_config(session, "oms_mock_success", "false")
    session.commit()
    monkeypatch.setattr(omp, "jackyun_client_from_session", lambda _session: FakeJackyunClient())
    order = create_delivery_ready_order(session)
    confirm_delivery_notice(session, order.delivery_notices[0], confirmed_by="tester")
    session.commit()
    run_pending_jobs(session)
    session.refresh(order)

    process_oms_status_update(session, {"notice_id": order.delivery_notices[0].id, "oms_status": "拣货中"})
    session.commit()
    result = run_pending_jobs(session)

    session.refresh(order)
    notice = order.delivery_notices[0]
    proof = session.query(OrderAttachment).filter_by(attachment_type="OutboundProof").one()
    assert result["completed"] == 1
    assert notice.waybill_no == "TRACK-001"
    assert notice.print_status == "Printed"
    assert proof.source_file_id == notice.id
    assert loads(proof.evidence_json)["waybill_no"] == "TRACK-001"


def test_waybill_print_syncs_tracking_to_platform_once(monkeypatch):
    import backend.app.services.order_middle_platform as omp

    class FakeJackyunClient:
        def create_delivery_order(self, payload, *, method="wms.order.create"):
            return {"ok": True, "data": {"orderNo": "OMS-PLATFORM-001"}, "raw": {"orderNo": "OMS-PLATFORM-001"}}

        def print_delivery_label(self, payload):
            return {"ok": True, "data": {"waybillNo": "TRACK-PLATFORM-001", "printData": ""}, "raw": {}}

    calls = []

    def fake_push_platform_fulfillment(session, order, notice):
        calls.append({"platform_order_no": order.platform_order_no, "waybill_no": notice.waybill_no})
        return {"ok": True, "external_id": "FULFILLMENT-001"}

    session = make_session()
    set_config(session, "oms_enabled", "true")
    set_config(session, "oms_mock_success", "false")
    set_config(session, "platform_fulfillment_mock_success", "false")
    set_config(session, "platform_fulfillment_sync_async", "false")
    session.commit()
    monkeypatch.setattr(omp, "jackyun_client_from_session", lambda _session: FakeJackyunClient())
    monkeypatch.setattr(omp, "push_platform_fulfillment", fake_push_platform_fulfillment)
    seed_oms_required_config(session)
    seed_active_sku(session)
    seed_inventory(session, quantity=100)
    result = upsert_crm_sales_orders(
        session,
        [
            valid_crm_order_row(
                platform_order_no="SHOPIFY-ORDER-001",
                shop_code="SHOPIFY-US",
                channel_code="shopify",
                fulfillment_type="FBM",
            )
        ],
    )
    session.commit()
    assert result["queued_events"] == 1
    complete_crm_order_required_fields(session)
    run_pending_jobs(session)
    order = session.query(MiddlePlatformOrder).one()
    notice = order.delivery_notices[0]
    confirm_delivery_notice(session, notice, confirmed_by="tester")
    session.commit()
    run_pending_jobs(session)
    process_oms_status_update(session, {"notice_id": notice.id, "oms_status": "拣货中"})
    session.commit()

    print_result = run_pending_jobs(session)
    sync_result = run_pending_jobs(session)

    session.refresh(notice)
    assert print_result["completed"] == 1
    assert sync_result["completed"] == 1
    assert notice.waybill_no == "TRACK-PLATFORM-001"
    assert notice.platform_fulfillment_status == "Synced"
    assert notice.platform_fulfillment_synced_waybill_no == "TRACK-PLATFORM-001"
    assert calls == [{"platform_order_no": "SHOPIFY-ORDER-001", "waybill_no": "TRACK-PLATFORM-001"}]
    assert session.query(AuditEvent).filter_by(event_type="PlatformFulfillmentSynced").count() == 1
    assert session.query(IntegrationEvent).filter_by(event_type="OMS_WAYBILL_PRINT", biz_key=notice.notice_no, status="Succeeded").count() == 1
    assert session.query(IntegrationEvent).filter_by(event_type="PLATFORM_FULFILLMENT_SYNC", biz_key="SHOPIFY-ORDER-001", status="Succeeded").count() == 1

    skipped = omp.process_platform_fulfillment_sync(session, {"notice_id": notice.id})
    assert skipped["skipped"] is True
    assert skipped["reason"] == "already_synced"
    assert len(calls) == 1


def test_platform_tracking_sync_failure_blocks_and_creates_exception(monkeypatch):
    import backend.app.services.order_middle_platform as omp

    class FakeJackyunClient:
        def create_delivery_order(self, payload, *, method="wms.order.create"):
            return {"ok": True, "data": {"orderNo": "OMS-PLATFORM-FAIL"}, "raw": {"orderNo": "OMS-PLATFORM-FAIL"}}

        def print_delivery_label(self, payload):
            return {"ok": True, "data": {"waybillNo": "TRACK-PLATFORM-FAIL", "printData": ""}, "raw": {}}

    def fake_push_platform_fulfillment(session, order, notice):
        raise RuntimeError("Shopify Fulfillment API timeout")

    session = make_session()
    set_config(session, "oms_enabled", "true")
    set_config(session, "oms_mock_success", "false")
    set_config(session, "platform_fulfillment_mock_success", "false")
    set_config(session, "platform_fulfillment_sync_max_retries", "1")
    set_config(session, "platform_fulfillment_sync_async", "false")
    session.commit()
    monkeypatch.setattr(omp, "jackyun_client_from_session", lambda _session: FakeJackyunClient())
    monkeypatch.setattr(omp, "push_platform_fulfillment", fake_push_platform_fulfillment)
    seed_oms_required_config(session)
    seed_active_sku(session)
    seed_inventory(session, quantity=100)
    upsert_crm_sales_orders(
        session,
        [
            valid_crm_order_row(
                crm_order_id="crm_obj_platform_fail",
                crm_order_no="SO-PLATFORM-FAIL",
                platform_order_no="SHOPIFY-ORDER-FAIL",
                shop_code="SHOPIFY-US",
                channel_code="shopify",
                fulfillment_type="FBM",
            )
        ],
    )
    session.commit()
    complete_crm_order_required_fields(session, "crm_obj_platform_fail")
    run_pending_jobs(session)
    order = session.query(MiddlePlatformOrder).one()
    notice = order.delivery_notices[0]
    confirm_delivery_notice(session, notice, confirmed_by="tester")
    session.commit()
    run_pending_jobs(session)
    process_oms_status_update(session, {"notice_id": notice.id, "oms_status": "拣货中"})
    session.commit()
    run_pending_jobs(session)

    result = run_pending_jobs(session)

    session.refresh(notice)
    assert result["completed"] == 1
    assert notice.platform_fulfillment_status == "Blocked"
    assert notice.platform_fulfillment_retry_count == 1
    assert "timeout" in (notice.platform_fulfillment_error or "")
    assert session.query(ExceptionCase).filter_by(exception_type="OMS_STATUS_CONFLICT").count() == 1
    assert session.query(AuditEvent).filter_by(event_type="PlatformFulfillmentSyncBlocked").count() == 1
    assert session.query(IntegrationEvent).filter_by(event_type="PLATFORM_FULFILLMENT_SYNC", biz_key="SHOPIFY-ORDER-FAIL", status="Dead").count() == 1


def test_oms_waybill_print_failure_blocks_and_creates_exception(monkeypatch):
    import backend.app.services.order_middle_platform as omp

    class FakeJackyunClient:
        def create_delivery_order(self, payload, *, method="wms.order.create"):
            return {"ok": True, "data": {"orderNo": "OMS-WAYBILL-FAIL"}, "raw": {"orderNo": "OMS-WAYBILL-FAIL"}}

        def print_delivery_label(self, payload):
            return {"ok": False, "message": "面单服务超时", "raw": {"code": "TIMEOUT"}}

    session = make_session()
    set_config(session, "oms_enabled", "true")
    set_config(session, "oms_mock_success", "false")
    set_config(session, "oms_waybill_print_max_retries", "1")
    session.commit()
    monkeypatch.setattr(omp, "jackyun_client_from_session", lambda _session: FakeJackyunClient())
    order = create_delivery_ready_order(session)
    confirm_delivery_notice(session, order.delivery_notices[0], confirmed_by="tester")
    session.commit()
    run_pending_jobs(session)
    session.refresh(order)

    process_oms_status_update(session, {"notice_id": order.delivery_notices[0].id, "oms_status": "拣货中"})
    session.commit()
    result = run_pending_jobs(session)

    session.refresh(order)
    assert result["completed"] == 1
    assert order.delivery_notices[0].print_status == "Blocked"
    assert order.delivery_notices[0].print_retry_count == 1
    assert session.query(ExceptionCase).filter_by(exception_type="OMS_STATUS_CONFLICT").count() == 1


def test_oms_status_poll_job_queries_oms_and_updates_order(monkeypatch):
    import backend.app.services.order_middle_platform as omp

    class FakeJackyunClient:
        def create_delivery_order(self, payload, *, method="wms.order.create"):
            return {"ok": True, "data": {"orderNo": "OMS-POLL-001"}, "raw": {"orderNo": "OMS-POLL-001"}}

        def query_delivery_orders(self, payload):
            return {
                "ok": True,
                "data": {
                    "rows": [
                        {
                            "erporderNo": payload["erporderNo"],
                            "orderNo": "OMS-POLL-001",
                            "deliveryStatus": "已发货",
                        }
                    ]
                },
                "raw": {},
            }

    session = make_session()
    set_config(session, "oms_enabled", "true")
    set_config(session, "oms_mock_success", "false")
    session.commit()
    monkeypatch.setattr(omp, "jackyun_client_from_session", lambda _session: FakeJackyunClient())
    order = create_delivery_ready_order(session)
    notice = order.delivery_notices[0]
    confirm_delivery_notice(session, notice, confirmed_by="tester")
    session.commit()
    run_pending_jobs(session)
    session.refresh(order)
    notice.oms_order_no = "OMS-POLL-001"
    session.commit()

    session.add(ProcessingJob(job_type="OMS_STATUS_POLL", payload_json=dumps({"limit": 10}), status="Pending"))
    session.commit()
    result = run_pending_jobs(session)

    assert result["completed"] == 1
    session.refresh(order)
    assert order.status == OrderStatus.FULFILLMENT_ARCHIVED.value
    assert order.delivery_notices[0].status == "Shipped"


def test_oms_push_skips_when_source_snapshot_hash_is_stale():
    session = make_session()
    order = create_delivery_ready_order(session)
    notice = order.delivery_notices[0]
    confirm_delivery_notice(session, notice, confirmed_by="tester")
    session.commit()
    job = session.query(ProcessingJob).filter_by(job_type="OMS_PUSH_NOTICE").one()
    payload = loads(job.payload_json, {})
    assert payload["source_snapshot_hash"] == order.payload_hash
    assert payload["notice_version"] == notice.notice_version
    assert payload["notice_lock_version"] == notice.version

    order.payload_hash = "newer-crm-payload-hash"
    session.commit()
    result = run_pending_jobs(session)

    session.refresh(order)
    session.refresh(notice)
    assert result["completed"] == 1
    assert order.status == OrderStatus.OMS_PENDING.value
    assert notice.status == "Confirmed"
    event = session.query(AuditEvent).filter_by(event_type="OmsPushSkipped").one()
    detail = loads(event.detail, {})
    assert detail["skipped_reason"] == "stale_payload_hash"


def test_oms_push_skips_when_notice_version_is_stale():
    session = make_session()
    order = create_delivery_ready_order(session)
    notice = order.delivery_notices[0]
    confirm_delivery_notice(session, notice, confirmed_by="tester")
    session.commit()
    job = session.query(ProcessingJob).filter_by(job_type="OMS_PUSH_NOTICE").one()
    payload = loads(job.payload_json, {})
    assert payload["notice_version"] == notice.notice_version

    notice.notice_version += 1
    session.commit()
    result = run_pending_jobs(session)

    session.refresh(order)
    assert result["completed"] == 1
    assert order.status == OrderStatus.OMS_PENDING.value
    event = session.query(AuditEvent).filter_by(event_type="OmsPushSkipped").one()
    detail = loads(event.detail, {})
    assert detail["skipped_reason"] == "stale_notice_version"


def test_crm_order_detail_includes_middle_platform_flow():
    session = make_session()
    order = create_delivery_ready_order(session)
    crm = session.query(CrmSalesOrder).filter_by(crm_order_id=order.crm_order_id).one()

    detail = serialize_crm_order_with_flow(session, crm)

    assert detail["flow"]["middle_order"]["order_no"] == order.order_no
    assert detail["snapshots"][0]["payload_hash"] == crm.payload_hash
    assert detail["flow"]["crm_snapshots"][0]["is_latest"] is True
    assert detail["flow"]["middle_order"]["status"] == OrderStatus.DELIVERY_NOTICE_READY.value
    assert [step["key"] for step in detail["flow"]["steps"]] == ["crm", "imported", "validation", "notice", "oms", "fulfillment"]
    assert detail["flow"]["steps"][2]["status"] == "done"
    assert detail["flow"]["steps"][3]["status"] == "done"


def test_crm_order_serialization_includes_contact_extraction_confidence():
    crm = CrmSalesOrder(
        crm_order_id="crm_obj_confidence",
        crm_order_no="SO-CONFIDENCE",
        customer_name="附件客户",
        payload_hash="hash-confidence",
        raw_json=dumps(
            {
                "oms_field_extraction": {
                    "confidence": 87,
                    "source": "llm",
                    "manual_review_required": True,
                    "validation_errors": ["收货地址缺少门牌号"],
                }
            }
        ),
    )

    data = serialize_crm_order(crm)

    assert data["contact_extraction_confidence"] == 87
    assert data["contact_extraction_source"] == "llm"
    assert data["contact_extraction_manual_review_required"] is True
    assert data["contact_extraction_validation_errors"] == ["收货地址缺少门牌号"]


def test_crm_sync_records_detail_snapshots_and_attachments():
    session = make_session()
    result = upsert_crm_sales_orders(session, [valid_crm_order_row(attachments=[{"file_name": "客户PO.pdf", "file_id": "file-001", "type": "PO"}])])
    session.commit()

    assert result["queued_events"] == 1
    crm = session.query(CrmSalesOrder).filter_by(crm_order_id="crm_obj_001").one()
    snapshots = session.query(CrmOrderSnapshot).filter_by(crm_order_id="crm_obj_001").order_by(CrmOrderSnapshot.version).all()
    attachments = session.query(OrderAttachment).filter_by(crm_order_id="crm_obj_001", payload_hash=crm.payload_hash).all()
    assert [snapshot.version for snapshot in snapshots] == [1]
    assert snapshots[0].is_latest is True
    assert crm.latest_snapshot_id == snapshots[0].id
    assert {attachment.file_name for attachment in attachments} == {"客户PO.pdf"}

    upsert_crm_sales_orders(session, [valid_crm_order_row(order_amount="126000.00", product_amount="126000.00", receivable_amount="101000.00")])
    session.commit()

    snapshots = session.query(CrmOrderSnapshot).filter_by(crm_order_id="crm_obj_001").order_by(CrmOrderSnapshot.version).all()
    assert [snapshot.version for snapshot in snapshots] == [1, 2]
    assert [snapshot.is_latest for snapshot in snapshots] == [False, True]


def test_crm_sync_ignores_detail_contact_fields_and_keeps_downloadable_attachments():
    session = make_session()
    result = upsert_crm_sales_orders(session, [
        valid_crm_order_row(
            sales_user_name="",
            detail_sync_status="Synced",
            detail_raw={"Value": {"data": {"id": "crm_obj_001"}}},
            sales_user_id="owner-001",
            owner_department="商务一部",
            receipt_contact="李四",
            receipt_phone="18600002222",
            receipt_address="深圳市南山区测试路 2 号",
            delivery_date="2026-07-01",
            remark="详情接口备注",
            attachments=[{"file_name": "客户PO.pdf", "file_url": "https://example.test/po.pdf", "file_id": "file-001"}],
        )
    ])
    session.commit()

    assert result["queued_events"] == 1
    crm = session.query(CrmSalesOrder).filter_by(crm_order_id="crm_obj_001").one()
    attachment = session.query(OrderAttachment).filter_by(crm_order_id="crm_obj_001", file_url="https://example.test/po.pdf").one()
    detail = serialize_crm_order_with_flow(session, crm)
    assert crm.receipt_contact in (None, "")
    assert crm.receipt_phone in (None, "")
    assert crm.receipt_address in (None, "")
    assert crm.delivery_date in (None, "")
    assert attachment.file_url == "https://example.test/po.pdf"
    assert detail["crm_detail_status"] == "detail_available"
    assert any(item["has_download"] for item in detail["attachments"])


def test_crm_sync_preserves_attachment_extracted_contact_fields():
    session = make_session()
    upsert_crm_sales_orders(session, [valid_crm_order_row(receipt_contact="", receipt_phone="", receipt_address="", delivery_date="")])
    crm = session.query(CrmSalesOrder).filter_by(crm_order_id="crm_obj_001").one()
    crm.receipt_contact = "附件联系人"
    crm.receipt_phone = "18612345678"
    crm.receipt_address = "广东省深圳市南山区科技园测试路 8 号"
    crm.delivery_date = "2026-07-05"
    session.commit()

    upsert_crm_sales_orders(
        session,
        [
            valid_crm_order_row(
                receipt_contact="CRM联系人",
                receipt_phone="19900000000",
                receipt_address="南山区",
                delivery_date="2026-08-01",
            )
        ],
    )
    session.commit()

    session.refresh(crm)
    raw = loads(crm.raw_json, {})
    assert crm.receipt_contact == "附件联系人"
    assert crm.receipt_phone == "18612345678"
    assert crm.receipt_address == "广东省深圳市南山区科技园测试路 8 号"
    assert crm.delivery_date == "2026-07-05"
    assert raw["receipt_contact"] == "附件联系人"
    assert raw["receipt_address"] == "广东省深圳市南山区科技园测试路 8 号"


def test_crm_sync_maps_detail_owner_and_owner_main_department_aliases():
    session = make_session()
    result = upsert_crm_sales_orders(
        session,
        [
            valid_crm_order_row(
                sales_user_name="",
                owner_department="",
                sales_user_email="",
                owner_name="张负责人",
                owner_main_department="商务一部",
                owner_email="owner@example.com",
            )
        ],
    )
    session.commit()

    assert result["queued_events"] == 1
    crm = session.query(CrmSalesOrder).filter_by(crm_order_id="crm_obj_001").one()
    assert crm.sales_user_name == "张负责人"
    assert crm.owner_department == "商务一部"
    assert crm.sales_user_email == "owner@example.com"


def test_retry_crm_order_detail_sync_refreshes_failed_detail(monkeypatch):
    import backend.app.services.crm_sync as crm_sync

    session = make_session()
    result = upsert_crm_sales_orders(
        session,
        [
            valid_crm_order_row(
                sales_user_name="",
                detail_sync_status="Failed",
                detail_sync_error="HTTP 500",
                receipt_contact="",
                receipt_address="",
                attachments=[],
            )
        ],
    )
    session.commit()
    assert result["queued_events"] == 1
    crm = session.query(CrmSalesOrder).filter_by(crm_order_id="crm_obj_001").one()
    assert serialize_crm_order_with_flow(session, crm)["crm_detail_status"] == "detail_failed"

    def fake_fetch(_session, order):
        return (
            valid_crm_order_row(
                detail_sync_status="Synced",
                detail_raw={"Value": {"data": {"id": order.crm_order_id}}},
                sales_user_name="Alice",
                receipt_contact="王五",
                receipt_phone="18600003333",
                receipt_address="广州市天河区详情路 3 号",
                attachments=[{"file_name": "盖章合同.pdf", "file_url": "https://example.test/contract.pdf", "file_id": "file-contract"}],
            ),
            {"mode": "test"},
        )

    monkeypatch.setattr(crm_sync, "fetch_single_order_detail_via_replay", fake_fetch)
    retry = retry_crm_order_detail_sync(session, crm)
    session.commit()

    session.refresh(crm)
    detail = serialize_crm_order_with_flow(session, crm)
    assert retry["ok"] is True
    assert crm.receipt_contact in (None, "")
    assert crm.receipt_phone in (None, "")
    assert detail["crm_detail_status"] == "detail_available"
    assert any(item["file_name"] == "盖章合同.pdf" and item["has_download"] for item in detail["attachments"])
    assert session.query(AuditEvent).filter_by(event_type="CrmOrderDetailRetrySucceeded", related_object_id=crm.id).count() == 1


def test_crm_sync_lock_reports_busy_and_recovers_stale_lock():
    import backend.app.services.crm_sync as crm_sync

    session = make_session()
    assert crm_sync.CRM_SYNC_LOCK.acquire(blocking=False) is True
    crm_sync.CRM_SYNC_LOCK_STATE.clear()
    crm_sync.CRM_SYNC_LOCK_STATE.update({"operation": "CRM 销售订单同步", "acquired_at": crm_sync.time.time(), "lease_seconds": 180})
    try:
        with pytest.raises(CrmSyncBusyError) as exc:
            with crm_sync.crm_sync_lock(session, "CRM 订单详情同步"):
                pass
        assert "当前有 CRM 同步任务正在进行，请稍后重试" in str(exc.value)

        crm_sync.CRM_SYNC_LOCK_STATE["acquired_at"] = crm_sync.time.time() - 1000
        crm_sync.CRM_SYNC_LOCK_STATE["lease_seconds"] = 180
        with crm_sync.crm_sync_lock(session, "CRM 订单详情同步") as state:
            assert state["stale_lock_recovered"] is True
    finally:
        crm_sync.CRM_SYNC_LOCK_STATE.clear()
        try:
            crm_sync.CRM_SYNC_LOCK.release()
        except RuntimeError:
            pass


def test_crm_attachment_extraction_fills_oms_receiver_fields_without_confusing_contract_signer():
    session = make_session()
    crm = CrmSalesOrder(
        crm_order_id="crm_obj_extract",
        crm_order_no="SO-EXTRACT",
        customer_name="附件客户",
        sales_user_name="Alice",
        sales_user_email="alice@jimuyida.com",
        owner_department="商务一部",
        life_status="normal",
        approval_status="approved",
        order_date="2026-06-12",
        settlement_method="CNY",
        currency="CNY",
        order_amount="100.00",
        product_amount="100.00",
        received_amount="0.00",
        receivable_amount="100.00",
        attachment_files_json=dumps(["合同.pdf"]),
        payload_hash="hash-extract",
        raw_json=dumps({}),
    )
    session.add(crm)
    session.flush()

    result = enrich_order_from_attachment_text(
        session,
        crm,
        [
            (
                None,
                "\n".join(
                    [
                        "甲方授权代表/合同签订人：王签约 13900000000",
                        "收货信息：收货人：赵物流 电话：18612345678",
                        "收货地址：广东省深圳市南山区科技园测试路 8 号",
                        "交货日期：2026-07-05",
                    ]
                ),
            )
        ],
    )

    assert result.receipt_contact == "赵物流"
    assert crm.receipt_contact == "赵物流"
    assert crm.receipt_phone == "18612345678"
    assert crm.receipt_address == "广东省深圳市南山区科技园测试路 8 号"
    assert crm.delivery_date == "2026-07-05"
    assert "王签约" not in crm.receipt_contact


def test_crm_attachment_extraction_overwrites_coarse_receiver_address():
    session = make_session()
    crm = CrmSalesOrder(
        crm_order_id="crm_obj_coarse_address",
        crm_order_no="SO-COARSE",
        payload_hash="hash-coarse",
        receipt_address="北京",
        raw_json=dumps({}),
    )
    session.add(crm)
    session.flush()

    result = enrich_order_from_attachment_text(
        session,
        crm,
        [
            (
                None,
                "\n".join(
                    [
                        "收货人：林杨",
                        "联系方式：13911704967",
                        "收货地址：北京市海淀区中关村南大街 1 号 2 号楼 301 室",
                    ]
                ),
            )
        ],
    )

    assert result.receipt_address == "北京市海淀区中关村南大街 1 号 2 号楼 301 室"
    assert crm.receipt_address == "北京市海淀区中关村南大街 1 号 2 号楼 301 室"


def test_crm_attachment_extraction_reads_contract_buyer_party_block_without_llm(monkeypatch):
    session = make_session()
    crm = CrmSalesOrder(
        crm_order_id="crm_obj_buyer_party",
        crm_order_no="SO-BUYER-PARTY",
        payload_hash="hash-buyer-party",
        receipt_address="南山区",
        raw_json=dumps({}),
    )
    session.add(crm)
    session.flush()
    monkeypatch.setattr(crm_attachment_extraction, "call_model", lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("LLM should not be called")))

    result = enrich_order_from_attachment_text(
        session,
        crm,
        [
            (
                None,
                "\n".join(
                    [
                        "甲方：测试公司广州遇见小面",
                        "地址：广州市白云区北京西路724号",
                        "联系人：刘莉莉",
                        "联系方式：18888009988",
                        "乙方：深圳积木易搭科技技术有限公司",
                        "地址：深圳市南山区深南大道9996号李宁中心21A",
                        "联系人：毛总",
                        "联系方式：0755-23910066",
                    ]
                ),
            )
        ],
    )

    assert result.receipt_contact == "刘莉莉"
    assert result.receipt_phone == "18888009988"
    assert result.receipt_address == "广州市白云区北京西路724号"
    assert result.manual_review_required is False
    assert crm.receipt_address == "广州市白云区北京西路724号"


def test_crm_attachment_extraction_handles_delivery_place_contact_line():
    session = make_session()
    crm = CrmSalesOrder(crm_order_id="crm_obj_place_contact", crm_order_no="SO-PLACE", payload_hash="hash-place", raw_json=dumps({}))
    session.add(crm)
    session.flush()

    result = enrich_order_from_attachment_text(
        session,
        crm,
        [(None, "设备交付地点及联系人：陕西省西安市雁塔区沣惠南路摩尔中心C座11楼道通科技，康江，18809185717。")],
    )

    assert result.receipt_address == "陕西省西安市雁塔区沣惠南路摩尔中心C座11楼道通科技"
    assert result.receipt_contact == "康江"
    assert result.receipt_phone == "18809185717"


def test_crm_attachment_extraction_prefers_purchase_party_contact_block():
    session = make_session()
    crm = CrmSalesOrder(crm_order_id="crm_obj_purchase_party", crm_order_no="SO-PURCHASE", payload_hash="hash-purchase", raw_json=dumps({}))
    session.add(crm)
    session.flush()

    result = enrich_order_from_attachment_text(
        session,
        crm,
        [
            (
                None,
                "\n".join(
                    [
                        "采购订单",
                        "采购方信息",
                        "采购方名称：北京南得空间信息技术有限公司",
                        "地址：北京市丰台区西三环南路 14 号院 1 号楼 11 层 1114 室",
                        "联系人：陈亮",
                        "电话：18612795555",
                        "供方信息",
                        "联系人：",
                        "电话：0755-23910066",
                    ]
                ),
            )
        ],
    )

    assert result.receipt_contact == "陈亮"
    assert result.receipt_phone == "18612795555"
    assert result.receipt_address == "北京市丰台区西三环南路 14 号院 1 号楼 11 层 1114 室"
    assert result.manual_review_required is False


def test_crm_attachment_extraction_uses_llm_when_rule_result_fails_validation(monkeypatch):
    session = make_session()
    crm = CrmSalesOrder(crm_order_id="crm_obj_llm", crm_order_no="SO-LLM", payload_hash="hash-llm", raw_json=dumps({}))
    session.add(crm)
    session.flush()

    monkeypatch.setattr(crm_attachment_extraction, "llm_fallback_enabled", lambda session: True)
    monkeypatch.setattr(crm_attachment_extraction, "active_model_config", lambda session: SimpleNamespace(id="model"))
    monkeypatch.setattr(crm_attachment_extraction, "model_ready", lambda session, config: True)
    monkeypatch.setattr(crm_attachment_extraction, "sensitive_llm_allowed", lambda session, config, config_key: True)
    monkeypatch.setattr(
        crm_attachment_extraction,
        "call_model",
        lambda *args, **kwargs: {
            "choices": [
                {
                    "message": {
                        "content": dumps(
                            {
                                "receipt_contact": "陈亮",
                                "receipt_phone": "18612795555",
                                "receipt_address": "北京市丰台区西三环南路14号院1号楼11层1114室",
                                "delivery_date": "",
                                "confidence": 93,
                                "reason": "采购方信息块更符合收货信息",
                            }
                        )
                    }
                }
            ]
        },
    )

    result = enrich_order_from_attachment_text(
        session,
        crm,
        [(None, "采购方信息\n地址：北京市丰台区西三环南路14号院1号楼11层1114室\n联系人：\n电话：0755-23910066")],
    )

    assert result.receipt_contact == "陈亮"
    assert result.receipt_phone == "18612795555"
    assert result.manual_review_required is False


def test_crm_attachment_extraction_blocks_external_sensitive_llm_by_default(monkeypatch):
    session = make_session()
    crm = CrmSalesOrder(crm_order_id="crm_obj_llm_privacy", crm_order_no="SO-LLM-PRIVACY", payload_hash="hash-privacy", raw_json=dumps({}))
    session.add(crm)
    session.flush()

    monkeypatch.setattr(crm_attachment_extraction, "llm_fallback_enabled", lambda session: True)
    monkeypatch.setattr(crm_attachment_extraction, "active_model_config", lambda session: SimpleNamespace(id="model", api_base="https://api.example.com"))
    monkeypatch.setattr(crm_attachment_extraction, "model_ready", lambda session, config: True)
    monkeypatch.setattr(crm_attachment_extraction, "call_model", lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("external LLM should not receive PII")))

    result = enrich_order_from_attachment_text(session, crm, [(None, "联系人：\n电话：0755\n地址：北京")])

    assert result.manual_review_required is True
    assert any(item.get("field") == "llm_privacy_blocked" for item in result.evidence)


def test_crm_attachment_extraction_marks_manual_review_when_llm_still_invalid(monkeypatch):
    session = make_session()
    crm = CrmSalesOrder(crm_order_id="crm_obj_manual", crm_order_no="SO-MANUAL", payload_hash="hash-manual", raw_json=dumps({}))
    session.add(crm)
    session.flush()

    monkeypatch.setattr(crm_attachment_extraction, "llm_fallback_enabled", lambda session: True)
    monkeypatch.setattr(crm_attachment_extraction, "active_model_config", lambda session: SimpleNamespace(id="model"))
    monkeypatch.setattr(crm_attachment_extraction, "model_ready", lambda session, config: True)
    monkeypatch.setattr(crm_attachment_extraction, "sensitive_llm_allowed", lambda session, config, config_key: True)
    monkeypatch.setattr(
        crm_attachment_extraction,
        "call_model",
        lambda *args, **kwargs: {"choices": [{"message": {"content": dumps({"receipt_contact": ":", "receipt_phone": "0755", "receipt_address": "北京", "confidence": 50})}}]},
    )

    result = enrich_order_from_attachment_text(session, crm, [(None, "联系人：\n电话：0755\n地址：北京")])

    assert result.manual_review_required is True
    assert result.validation_errors
    assert not crm.receipt_contact
    assert not crm.receipt_phone


def test_crm_attachment_extraction_applies_valid_fields_even_when_one_required_field_fails():
    session = make_session()
    crm = CrmSalesOrder(crm_order_id="crm_obj_partial", crm_order_no="SO-PARTIAL", payload_hash="hash-partial", raw_json=dumps({}))
    session.add(crm)
    session.flush()

    result = enrich_order_from_attachment_text(
        session,
        crm,
        [
            (
                None,
                "\n".join(
                    [
                        "采购方信息",
                        "地址：北京市丰台区西三环南路14号院1号楼11层1114室",
                        "联系人：陈亮",
                    ]
                ),
            )
        ],
    )

    assert result.manual_review_required is True
    assert "联系方式电话未通过校验" in result.validation_errors
    assert crm.receipt_contact == "陈亮"
    assert crm.receipt_address == "北京市丰台区西三环南路14号院1号楼11层1114室"
    assert not crm.receipt_phone


def test_crm_attachment_extraction_keeps_higher_quality_saved_result():
    session = make_session()
    crm = CrmSalesOrder(
        crm_order_id="crm_obj_quality_guard",
        crm_order_no="SO-QUALITY-GUARD",
        payload_hash="hash-quality-guard",
        receipt_contact="陈亮",
        receipt_phone="18612795555",
        receipt_address="北京市丰台区西三环南路14号院1号楼11层1114室",
        raw_json=dumps(
            {
                "oms_field_extraction": {
                    "receipt_contact": "陈亮",
                    "receipt_phone": "18612795555",
                    "receipt_address": "北京市丰台区西三环南路14号院1号楼11层1114室",
                    "confidence": 95,
                    "source": "llm",
                    "manual_review_required": False,
                }
            }
        ),
    )
    session.add(crm)
    session.flush()

    result = enrich_order_from_attachment_text(session, crm, [(None, "采购方信息\n地址：北京\n联系人：\n电话：0755-23910066")])

    assert result.receipt_contact == "陈亮"
    assert result.receipt_phone == "18612795555"
    assert result.receipt_address == "北京市丰台区西三环南路14号院1号楼11层1114室"
    assert loads(crm.raw_json, {})["oms_field_extraction"]["receipt_phone"] == "18612795555"
    assert any(item.get("field") == "stale_lower_quality_extraction_skipped" for item in result.evidence)


def test_crm_order_detail_dedupes_current_payload_attachments():
    session = make_session()
    crm = CrmSalesOrder(
        crm_order_id="crm_obj_dedupe",
        crm_order_no="SO-DEDUPE",
        customer_name="去重客户",
        sales_user_name="Alice",
        sales_user_email="alice@jimuyida.com",
        owner_department="商务一部",
        life_status="normal",
        approval_status="approved",
        order_date="2026-06-12",
        settlement_method="CNY",
        currency="CNY",
        order_amount="100.00",
        product_amount="100.00",
        received_amount="0.00",
        receivable_amount="100.00",
        receipt_contact="张三",
        receipt_phone="18600005555",
        receipt_address="深圳市测试路 1 号",
        attachment_files_json=dumps(["采购合同.pdf", "采购合同.pdf"]),
        payload_hash="hash-current",
        raw_json=dumps({}),
    )
    session.add(crm)
    session.flush()
    for index, payload_hash in enumerate(["hash-old", "hash-current", "hash-current"], start=1):
        session.add(
            OrderAttachment(
                crm_sales_order_id=crm.id,
                crm_order_id=crm.crm_order_id,
                crm_order_no=crm.crm_order_no,
                payload_hash=payload_hash,
                file_name="采购合同.pdf",
                file_url=f"https://example.test/{index}.pdf",
                fingerprint=f"fp-{index}",
                evidence_json=dumps({}),
                raw_json=dumps({}),
            )
        )
    session.add(
        OrderAttachment(
            crm_sales_order_id=crm.id,
            crm_order_id=crm.crm_order_id,
            crm_order_no=crm.crm_order_no,
            payload_hash="hash-current",
            file_name="采购合同.pdf",
            file_url=None,
            fingerprint="fp-name-only",
            evidence_json=dumps({}),
            raw_json=dumps({}),
        )
    )
    session.commit()

    detail = serialize_crm_order_with_flow(session, crm)

    assert [item["file_name"] for item in detail["attachments"]] == ["采购合同.pdf"]
    assert detail["attachments"][0]["file_url"] in {"https://example.test/2.pdf", "https://example.test/3.pdf"}
    assert detail["attachments"][0]["has_download"] is True
    assert detail["attachments"][0]["download_url"].startswith("/api/crm/order-attachments/")


def test_image_ocr_text_uses_paddle_path_for_receiver_extraction(monkeypatch):
    from PIL import Image, ImageDraw, ImageFont
    import io

    import backend.app.services.attachment_parser as attachment_parser

    image = Image.new("RGB", (1200, 360), "white")
    draw = ImageDraw.Draw(image)
    font = ImageFont.truetype("/System/Library/Fonts/Supplemental/Arial.ttf", 34)
    draw.text((30, 30), "Contract signer: Wrong Person 13900000000", fill="black", font=font)
    draw.text((30, 100), "Receiver: Zhao Logistics", fill="black", font=font)
    draw.text((30, 150), "Phone: 18612345678", fill="black", font=font)
    draw.text((30, 210), "Shipping address: Shenzhen Test Road 8", fill="black", font=font)
    draw.text((30, 270), "Delivery date: 2026-07-05", fill="black", font=font)
    buffer = io.BytesIO()
    image.save(buffer, format="PNG")
    monkeypatch.setenv("ATTACHMENT_OCR_USE_WORKER", "false")
    monkeypatch.setattr(attachment_parser, "configured_ocr_engines", lambda: ["paddle"])
    monkeypatch.setattr(attachment_parser, "run_paddleocr", lambda *_args, **_kwargs: "Receiver: Zhao Logistics\nPhone: 18612345678")

    parsed = parse_attachment("scan.png", buffer.getvalue(), max_zip_bytes=1024 * 1024, max_depth=1)

    assert parsed.status == "Parsed"
    assert parsed.metadata["ocr_engine"] == "paddleocr"
    assert "18612345678" in parsed.text


def test_image_ocr_worker_accepts_output_when_worker_exits_abnormally(monkeypatch):
    from types import SimpleNamespace

    import backend.app.services.attachment_parser as attachment_parser

    def fake_run(command, **_kwargs):
        output_path = command[command.index("--output") + 1]
        with open(output_path, "w", encoding="utf-8") as handle:
            handle.write('{"ok": true, "text": "采购单号：RS-DL-001", "metadata": {"ocr_engine": "paddle_structure"}}')
        return SimpleNamespace(returncode=133, stdout="", stderr="libc++abi: terminating")

    monkeypatch.delenv("ATTACHMENT_OCR_IN_WORKER", raising=False)
    monkeypatch.setenv("ATTACHMENT_OCR_USE_WORKER", "true")
    monkeypatch.setattr(attachment_parser.subprocess, "run", fake_run)

    text, metadata = attachment_parser.parse_image_text_with_worker(b"not-an-image", ".png")

    assert text == "采购单号：RS-DL-001"
    assert metadata["ocr_engine"] == "paddle_structure"
    assert metadata["ocr_worker"] == "subprocess"
    assert metadata["ocr_worker_exit_code"] == "133"


def test_paddle_structure_table_html_converts_to_pipe_rows():
    from backend.app.services.attachment_parser import html_table_to_pipe_rows, paddle_structure_lines

    rows = html_table_to_pipe_rows(
        "<table><tr><td>序号</td><td>产品名称</td><td>数量</td></tr>"
        "<tr><td>1</td><td>三维扫描仪 Whale 基础款</td><td>1</td></tr></table>"
    )
    assert rows == ["序号 | 产品名称 | 数量", "1 | 三维扫描仪 Whale 基础款 | 1"]

    lines = paddle_structure_lines(
        [{"type": "table", "res": {"html": "<table><tr><td>规格型号</td><td>总金额（含税）</td></tr><tr><td>Whale 基础款</td><td>¥50,000.00</td></tr></table>"}}]
    )
    assert "规格型号 | 总金额（含税）" in lines
    assert "Whale 基础款 | ¥50,000.00" in lines

    nested_lines = paddle_structure_lines(
        [{"res": [{"table_result": "<html><body><table><tr><td>数量</td><td>单位</td></tr><tr><td>1</td><td>台</td></tr></table></body></html>"}]}]
    )
    assert "数量 | 单位" in nested_lines
    assert "1 | 台" in nested_lines


def test_purchase_order_extraction_reads_buyer_and_table_items():
    from backend.app.services.purchase_order_extraction import extract_purchase_order_fields

    result = extract_purchase_order_fields(
        "采购订单\n"
        "采购单号：RS-DL-20260615000-00\n"
        "采购方信息\n"
        "采购方名称：武汉尺子科技有限公司\n"
        "地址：湖北省武汉市测试区测试街888号，测试中心大楼8层\n"
        "联系人：测试先生\n"
        "电话：18000000000\n"
        "供方信息\n"
        "供方名称：武汉睿数信息技术有限公司\n"
        "联系人：刘寒砚\n"
        "电话：0755-23910066\n"
        "序号 | 产品名称 | 规格型号 | 数量 | 单位 | 单价（未含税） | 总金额（含税） | 交期\n"
        "1 | 三维扫描仪 Whale 基础款 | Whale 基础款 | 1 | 台 | ¥44,247.79 | ¥50,000.00 | 收到全额货款后发货\n"
    )

    assert result["purchase_order_no"] == "RS-DL-20260615000-00"
    assert result["buyer"]["name"] == "武汉尺子科技有限公司"
    assert result["buyer"]["contact"] == "测试先生"
    assert result["buyer"]["phone"] == "18000000000"
    assert result["buyer"]["address"] == "湖北省武汉市测试区测试街888号，测试中心大楼8层"
    assert result["items"] == [
        {
            "row_no": "1",
            "product_name": "三维扫描仪 Whale 基础款",
            "specification": "Whale 基础款",
            "quantity": "1",
            "unit": "台",
            "unit_price": "44247.79",
            "line_amount": "50000",
            "delivery": "收到全额货款后发货",
            "source": "purchase_table",
        }
    ]


def test_purchase_order_extraction_reads_interleaved_buyer_column_and_html_table():
    from backend.app.services.purchase_order_extraction import extract_purchase_order_fields

    result = extract_purchase_order_fields(
        "采购订单\n"
        "采购单号：RS-DL-20260615000-00\n"
        "供方信息\n"
        "采购方信息\n"
        "供方名称：武汉睿数信息技术有限公司\n"
        "采购方名称：武汉尺子科技有限公司\n"
        "地址：武汉东湖新技术开发区长城园路8号光谷精工科技园B\n"
        "地址：湖北省武汉市测试区测试街888号，测试中心大楼8层\n"
        "座401号\n"
        "联系人：测试先生\n"
        "联系人：刘寒砚\n"
        "电话：\n"
        "18000000000\n"
        "电话：0755-23910066\n"
        "序号 | 产品名称 | 规格型号 | 数量 | 单位 | 单价（未含税） | 总金额（含税） | 交期\n"
        "1 | 三维扫描仪Whale 基础款 | Whale 基础款 | 1 | 台 | ￥44,247.79 | ¥50,000.00 | 收到全额货款后发货\n"
    )

    assert result["purchase_order_no"] == "RS-DL-20260615000-00"
    assert result["buyer"]["name"] == "武汉尺子科技有限公司"
    assert result["buyer"]["address"] == "湖北省武汉市测试区测试街888号，测试中心大楼8层"
    assert result["buyer"]["contact"] == "测试先生"
    assert result["buyer"]["phone"] == "18000000000"
    assert result["items"][0]["quantity"] == "1"
    assert result["items"][0]["line_amount"] == "50000"

    shifted = extract_purchase_order_fields(
        "序号 | 产品名称 | 规格型号 | 数量 | 单位 | 单价（未含税） | 总金额（含税） | 交期 乙方于收到甲方全额货款后按照双方协商确定的时间发货。\n"
        "1 | 三维扫描仪Whale 基础款 | Whale 基础款 | 1 | 台 | ￥44,247.79 | ¥50,000.00 | 人民币 伍万元整 （大写） ¥50,000.00(小写）\n"
    )
    assert shifted["items"][0]["delivery"] == "乙方于收到甲方全额货款后按照双方协商确定的时间发货"


def test_purchase_order_extraction_reads_sales_contract_goods_table():
    from backend.app.services.purchase_order_extraction import extract_purchase_order_fields

    result = extract_purchase_order_fields(
        "甲方：测试公司广州遇见小面\n"
        "地址：广州市白云区北京西路724号\n"
        "联系人：刘莉莉\n"
        "联系方式：18888009988\n"
        "序号 | 货物名称 | 主要规格/详细配置 | 数量 | 不含税单价（元） | 不含税总价（元）\n"
        "1 | 三维扫描仪 | Seal基础款(4规) | 1台 | 5220 | ¥6000\n"
        "税率 | 税率 | 税率 | 税率 | 税率 | 13%\n"
        "含税总价（元） | 含税总价（元） | 含税总价（元） | 含税总价（元） | 含税总价（元） | ¥6000\n"
    )

    assert result["buyer"]["contact"] == "刘莉莉"
    assert result["buyer"]["phone"] == "18888009988"
    assert result["items"] == [
        {
            "row_no": "1",
            "product_name": "三维扫描仪",
            "specification": "Seal基础款(4规)",
            "quantity": "1",
            "unit": "台",
            "unit_price": "5220",
            "line_amount": "6000",
            "delivery": "",
            "source": "purchase_table",
        }
    ]


def test_purchase_order_extraction_uses_fuzzy_table_layers():
    from backend.app.services.purchase_order_extraction import extract_purchase_order_fields

    fuzzy_header = extract_purchase_order_fields(
        "一、供货内容、价格和数量\n"
        "项目 | 设备 | 配置 | 件数 | 成交价 | 小计\n"
        "1 | 三维扫描仪 | Seal基础款 | 1台 | 5220 | ¥6000\n"
    )
    assert fuzzy_header["items"][0]["product_name"] == "三维扫描仪"
    assert fuzzy_header["items"][0]["specification"] == "Seal基础款"
    assert fuzzy_header["items"][0]["quantity"] == "1"
    assert fuzzy_header["items"][0]["unit"] == "台"
    assert fuzzy_header["items"][0]["source"] == "purchase_table"

    no_header = extract_purchase_order_fields(
        "供货内容、价格和数量\n"
        "1 | 三维扫描仪 | Seal基础款 | 1台 | 5220 | ¥6000\n"
    )
    assert no_header["items"][0]["source"] == "purchase_table_inferred"
    assert no_header["items"][0]["line_amount"] == "6000"

    vertical = extract_purchase_order_fields(
        "产品参数\n"
        "序号\n"
        "货物名称\n"
        "主要规格/详细配置\n"
        "数量\n"
        "不含税单价（元）\n"
        "不含税总价（元）\n"
        "1\n"
        "三维扫描仪\n"
        "Seal基础款(4规)\n"
        "1台\n"
        "5220\n"
        "¥6000\n"
    )
    assert vertical["items"][0]["source"] == "purchase_table_vertical"
    assert vertical["items"][0]["product_name"] == "三维扫描仪"
    assert vertical["items"][0]["quantity"] == "1"


def test_purchase_order_extraction_uses_contact_nearby_fallback():
    from backend.app.services.purchase_order_extraction import extract_purchase_order_fields

    result = extract_purchase_order_fields(
        "设备交付地点及联系人\n"
        "地址：广东省深圳市南山区科技园测试路8号A座12层\n"
        "联络人：赵物流\n"
        "联系电话：18612345678\n"
        "序号 | 货物名称 | 数量 | 不含税总价（元）\n"
        "1 | 三维扫描仪 | 1台 | ¥6000\n"
    )

    assert result["buyer"]["address"] == "广东省深圳市南山区科技园测试路8号A座12层"
    assert result["buyer"]["contact"] == "赵物流"
    assert result["buyer"]["phone"] == "18612345678"


def test_crm_attachment_rule_uses_purchase_order_contact_fallback():
    result = crm_attachment_extraction.extract_oms_fields_by_rule(
        "采购订单\n"
        "交付地点及联系人\n"
        "地址：广东省深圳市南山区科技园测试路8号A座12层\n"
        "联络人：赵物流\n"
        "联系电话：18612345678\n"
    )

    assert result.receipt_address == "广东省深圳市南山区科技园测试路8号A座12层"
    assert result.receipt_contact == "赵物流"
    assert result.receipt_phone == "18612345678"
    assert result.manual_review_required is False


def test_docx_textbox_content_is_included_in_attachment_text():
    import io
    import zipfile

    from docx import Document

    document = Document()
    document.add_paragraph("采购订单")
    buffer = io.BytesIO()
    document.save(buffer)

    source = io.BytesIO(buffer.getvalue())
    target = io.BytesIO()
    textbox_xml = """
    <w:p>
      <w:r>
        <w:pict>
          <v:shape xmlns:v="urn:schemas-microsoft-com:vml">
            <v:textbox>
              <w:txbxContent>
                <w:p><w:r><w:t>地址：北京市二村新泰商贸楼10-11号铺面</w:t></w:r></w:p>
                <w:p><w:r><w:t>联系人：王豆</w:t></w:r></w:p>
                <w:p><w:r><w:t>电话：15013539200</w:t></w:r></w:p>
              </w:txbxContent>
            </v:textbox>
          </v:shape>
        </w:pict>
      </w:r>
    </w:p>
    """
    with zipfile.ZipFile(source) as zin, zipfile.ZipFile(target, "w") as zout:
        for item in zin.infolist():
            data = zin.read(item.filename)
            if item.filename == "word/document.xml":
                text = data.decode("utf-8")
                data = text.replace("</w:body>", f"{textbox_xml}</w:body>").encode("utf-8")
            zout.writestr(item, data)

    parsed = parse_attachment("采购订单.docx", target.getvalue(), max_zip_bytes=1024 * 1024, max_depth=1)

    assert parsed.status == "Parsed"
    assert "北京市二村新泰商贸楼10-11号铺面" in parsed.text
    assert "王豆" in parsed.text
    assert "15013539200" in parsed.text


def test_delivery_confirmation_blocks_when_receiver_phone_missing():
    session = make_session()
    order = create_delivery_ready_order(session)
    order.crm_order.receipt_phone = None
    raw = loads(order.crm_order.raw_json, {})
    raw.pop("receipt_phone", None)
    order.crm_order.raw_json = dumps(raw)
    order.delivery_notices[0].payload_json = dumps(
        {
            **loads(order.delivery_notices[0].payload_json, {}),
            "orderInfo": {
                "receiverName": order.crm_order.receipt_contact,
                "receiverAddress": order.crm_order.receipt_address,
                "buyerMemo": "缺电话",
            },
        }
    )
    session.commit()

    with pytest.raises(RuntimeError, match="联系方式电话"):
        confirm_delivery_notice(session, order.delivery_notices[0], confirmed_by="tester")


def test_delivery_confirmation_blocks_when_receiver_address_is_coarse():
    session = make_session()
    order = create_delivery_ready_order(session)
    payload = loads(order.delivery_notices[0].payload_json, {})
    payload["orderInfo"]["receiverAddress"] = "北京"
    order.delivery_notices[0].payload_json = dumps(payload)
    session.commit()

    with pytest.raises(RuntimeError, match="可邮寄详细收货地址"):
        confirm_delivery_notice(session, order.delivery_notices[0], confirmed_by="tester")


def test_crm_sync_ignores_out_of_scope_order_without_queueing_middle_platform():
    session = make_session()
    result = upsert_crm_sales_orders(session, [valid_crm_order_row(crm_order_id="crm_obj_draft", crm_order_no="SO-DRAFT", approval_status="draft")])
    session.commit()

    assert result["ignored"] == 1
    assert result["queued_events"] == 0
    crm = session.query(CrmSalesOrder).filter_by(crm_order_id="crm_obj_draft").one()
    assert crm.scope_status == "Ignored"
    assert "approval_status_not_in_phase1_scope" in (crm.scope_ignore_reason or "")
    assert session.query(CrmOrderSnapshot).filter_by(crm_order_id="crm_obj_draft").count() == 1
    assert session.query(ProcessingJob).filter_by(job_type="CRM_ORDER_PARSED").count() == 0


def test_crm_sync_ignores_under_review_life_status_even_without_approval_status():
    session = make_session()
    result = upsert_crm_sales_orders(
        session,
        [
            valid_crm_order_row(
                crm_order_id="crm_obj_under_review",
                crm_order_no="SO-UNDER-REVIEW",
                life_status="under_review",
                approval_status="",
            )
        ],
    )
    session.commit()

    assert result["ignored"] == 1
    assert result["queued_events"] == 0
    crm = session.query(CrmSalesOrder).filter_by(crm_order_id="crm_obj_under_review").one()
    assert crm.scope_status == "Ignored"
    assert "life_status_not_in_phase1_scope" in (crm.scope_ignore_reason or "")
    assert session.query(ProcessingJob).filter_by(job_type="CRM_ORDER_PARSED").count() == 0


def test_crm_sync_skips_orders_before_configured_min_order_date():
    session = make_session()
    set_config(session, "crm_sync_min_order_date", "2026-06-15")

    result = upsert_crm_sales_orders(
        session,
        [valid_crm_order_row(crm_order_id="crm_obj_old", crm_order_no="SO-OLD", order_date="2026-01-14")],
    )
    session.commit()

    assert result["ignored"] == 1
    assert result["queued_events"] == 0
    assert session.query(CrmSalesOrder).filter_by(crm_order_id="crm_obj_old").count() == 0
    assert session.query(CrmOrderSnapshot).filter_by(crm_order_id="crm_obj_old").count() == 0
    assert session.query(ProcessingJob).filter_by(job_type="CRM_ORDER_PARSED").count() == 0


def test_crm_sync_config_value_decrypts_secret_password():
    session = make_session()
    set_config(session, "crm_password", encrypt_value("crm-secret"), is_secret=True)

    assert crm_sync_config_value(session, "crm_password") == "crm-secret"


def test_crm_sync_recreates_missing_request_file_from_saved_json(tmp_path):
    missing_path = tmp_path / "fxiaoke-sales-order-list-request.json"
    request_path, written_path = ensure_request_file(
        configured_path=str(missing_path),
        request_json='{"method":"POST","url":"https://example.test/List"}',
        fallback_prefix="fxiaoke-list-request",
    )

    assert request_path == str(missing_path)
    assert written_path == missing_path
    assert missing_path.read_text(encoding="utf-8") == '{"method":"POST","url":"https://example.test/List"}'


def test_crm_cdp_preflight_fails_fast_on_login_page(monkeypatch):
    class FakeResponse(io.BytesIO):
        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, traceback):
            return False

    def fake_urlopen(url, timeout):
        assert url == "http://127.0.0.1:9334/json/list"
        assert timeout == 3
        return FakeResponse(json.dumps([{"title": "CRM登录系统 - 纷享销客CRM", "url": "https://www.fxiaoke.com/proj/page/loginv2"}]).encode())

    monkeypatch.setattr("backend.app.services.crm_sync.urllib.request.urlopen", fake_urlopen)
    monkeypatch.setattr("backend.app.services.crm_sync._cdp_cleanup_extra_pages", lambda cdp_url: {"pages": [], "kept": None, "closed": 0, "is_login_page": True})
    monkeypatch.setattr("backend.app.services.crm_sync._remaining_login_cooldown_seconds", lambda: 42)

    with pytest.raises(RuntimeError, match="CRM 自动登录仍在风控冷却期"):
        preflight_crm_cdp_browser("http://127.0.0.1:9334")


def test_crm_cdp_preflight_allows_login_page_when_auto_login_available(monkeypatch):
    class FakeResponse(io.BytesIO):
        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, traceback):
            return False

    def fake_urlopen(url, timeout):
        return FakeResponse(json.dumps([{"title": "CRM登录系统 - 纷享销客CRM", "url": "https://www.fxiaoke.com/proj/page/loginv2"}]).encode())

    monkeypatch.setattr("backend.app.services.crm_sync.urllib.request.urlopen", fake_urlopen)
    monkeypatch.setattr("backend.app.services.crm_sync._cdp_cleanup_extra_pages", lambda cdp_url: {"pages": [], "kept": None, "closed": 0, "is_login_page": True})
    monkeypatch.setattr("backend.app.services.crm_sync._remaining_login_cooldown_seconds", lambda: 0)

    result = preflight_crm_cdp_browser("http://127.0.0.1:9334", allow_login_page=True)

    assert result["login_page_count"] == 1
    assert result["logged_in_page_count"] == 0
    assert result["login_page_blocked"] is True
    assert result["login_page_allowed_for_auto_login"] is True


def test_crm_cdp_preflight_returns_page_summary(monkeypatch):
    class FakeResponse(io.BytesIO):
        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, traceback):
            return False

    def fake_urlopen(url, timeout):
        return FakeResponse(json.dumps([{"title": "销售订单-纷享销客", "url": "https://www.fxiaoke.com/XV/UI/Home#crm/list/=/SalesOrderObj"}]).encode())

    monkeypatch.setattr("backend.app.services.crm_sync.urllib.request.urlopen", fake_urlopen)

    result = preflight_crm_cdp_browser("http://127.0.0.1:9334")

    assert result["page_count"] == 1
    assert result["login_page_count"] == 0
    assert result["titles"] == ["销售订单-纷享销客"]


def test_crm_sync_backfills_settlement_method_from_order_items():
    session = make_session()
    result = upsert_crm_sales_orders(
        session,
        [
            valid_crm_order_row(
                settlement_method="",
                order_items=[
                    {
                        "product_name": "Moose",
                        "settlement_method": "人民币结算CNY",
                        "quantity": "1",
                        "unit_price": "5999.00",
                        "line_amount": "5999.00",
                    }
                ],
            )
        ],
    )
    session.commit()

    crm = session.query(CrmSalesOrder).filter_by(crm_order_id="crm_obj_001").one()
    assert result["queued_events"] == 1
    assert crm.settlement_method == "人民币结算CNY"
    assert crm.currency == "CNY"


def test_crm_detail_empty_dom_fallback_preserves_existing_raw_items_and_attachments():
    session = make_session()
    upsert_crm_sales_orders(
        session,
        [
            valid_crm_order_row(
                crm_order_id="crm_obj_degraded_detail",
                crm_order_no="SO-DEGRADED-DETAIL",
                order_items=[{"product_name": "Whale—overseas", "quantity": 1, "unit_price": "50000", "line_amount": "50000"}],
                attachments=[{"file_name": "客户PO.pdf", "file_url": "https://example.test/po.pdf"}],
                attachment_files="客户PO.pdf",
            )
        ],
    )
    session.commit()

    upsert_crm_sales_orders(
        session,
        [
            {
                "crm_order_id": "crm_obj_degraded_detail",
                "crm_order_no": "SO-DEGRADED-DETAIL",
                "customer_name": "武汉尺子科技有限公司",
                "sales_user_name": "毛总",
                "owner_department": "项目部",
                "order_amount": "50000.00",
                "detail_source": "DomDetailFallback",
                "order_items": [],
                "attachments": [],
                "attachment_files": "",
            }
        ],
    )
    session.commit()

    crm = session.query(CrmSalesOrder).filter_by(crm_order_id="crm_obj_degraded_detail").one()
    raw = loads(crm.raw_json, {})
    assert raw["order_items"][0]["product_name"] == "Whale—overseas"
    assert raw["attachments"][0]["file_name"] == "客户PO.pdf"
    assert loads(crm.attachment_files_json, []) == ["客户PO.pdf"]


def test_crm_attachment_sync_reuses_previous_parsed_signed_po_for_new_payload():
    session = make_session()
    result = upsert_crm_sales_orders(
        session,
        [
            valid_crm_order_row(
                crm_order_id="crm_obj_reuse_attachment_evidence",
                crm_order_no="SO-REUSE-ATTACHMENT-EVIDENCE",
                attachments=[{"file_name": "客户PO.pdf", "file_id": "file-001"}],
                attachment_files="客户PO.pdf",
            )
        ],
    )
    session.commit()
    assert result["queued_events"] == 1
    first = session.query(OrderAttachment).one()
    first.parse_status = "Parsed"
    first.evidence_json = dumps({"source": "crm_order_detail", "payload_hash": first.payload_hash, "parsed_text": "采购订单\n授权签字人：张三\n订单总金额 ¥125000"})
    session.commit()

    upsert_crm_sales_orders(
        session,
        [
            valid_crm_order_row(
                crm_order_id="crm_obj_reuse_attachment_evidence",
                crm_order_no="SO-REUSE-ATTACHMENT-EVIDENCE",
                attachments=[{"file_name": "客户PO.pdf", "file_id": "file-001"}],
                attachment_files="客户PO.pdf",
                remark="CRM 备注变化生成新快照",
            )
        ],
    )
    session.commit()

    latest = session.query(OrderAttachment).order_by(OrderAttachment.created_at.desc()).first()
    evidence = loads(latest.evidence_json, {})
    assert latest.id != first.id
    assert latest.parse_status == "Parsed"
    assert "采购订单" in evidence["parsed_text"]
    assert evidence["reused_from_attachment_id"] == first.id


def test_crm_product_sync_clears_existing_skus_and_rebuilds_from_crm_products():
    session = make_session()
    seed_active_sku(session, "OLD-SKU")
    session.commit()

    result = sync_crm_products_as_skus(
        session,
        [
            {"crm_product_id": "crm_prod_g500_single", "product_name": "睿数国内—空间扫描仪G500(单目)", "sku_code": "G500-SINGLE", "model": "G500 单目"},
            {"crm_product_id": "crm_prod_g500_dual", "product_name": "睿数国内—空间扫描仪G500(双目)", "sku_code": "G500-DUAL", "model": "G500 双目"},
        ],
        clear_existing=True,
    )
    session.commit()

    assert result["created_skus"] == 2
    assert session.query(ProductSKU).filter_by(sku_id="OLD-SKU").count() == 0
    assert session.query(ProductSKU).filter_by(sku_id="G500-SINGLE", status="Active").count() == 1
    assert session.query(ProductSPU).filter(ProductSPU.name.ilike("%空间扫描仪G500%")).count() == 2


def test_customer_mapping_sync_matches_crm_and_oms_by_customer_name():
    session = make_session()

    result = sync_customer_mapping_from_masters(
        session,
        [
            {"customer_name": "武汉尺子科技有限公司", "customer_code": "CRM-CUST-001"},
            {"customer_name": "未匹配客户", "customer_code": "CRM-CUST-002"},
        ],
        [{"customer_name": "武汉尺子科技有限公司", "customer_code": "OMS-CUST-8899"}],
    )
    session.commit()

    mapping = loads(session.get(SystemConfig, "v2_customer_mapping_json").value, {})
    unmatched = loads(session.get(SystemConfig, "v2_customer_mapping_unmatched_json").value, [])
    assert result["matched_count"] == 1
    assert mapping["武汉尺子科技有限公司"]["customer_code"] == "OMS-CUST-8899"
    assert mapping["武汉尺子科技有限公司"]["crm_customer_code"] == "CRM-CUST-001"
    assert unmatched[0]["customer_name"] == "未匹配客户"


def test_customer_mapping_queries_oms_when_static_mapping_missing(monkeypatch):
    import backend.app.services.customer_mapping as customer_mapping_service

    session = make_session()
    seed_active_sku(session)
    seed_inventory(session, quantity=100)
    set_config(session, "v2_customer_mapping_json", dumps({}))

    def fake_query(_session, customer_name):
        return {"customer_name": customer_name, "customer_code": "OMS-CUST-WH-CZ"}, {"status": "Found", "method": "mock.customer.query"}

    monkeypatch.setattr(customer_mapping_service, "query_oms_customer", fake_query)

    result = upsert_crm_sales_orders(
        session,
        [valid_crm_order_row(crm_order_id="crm_obj_oms_customer_found", crm_order_no="SO-OMS-CUSTOMER-FOUND", customer_name="武汉尺子科技有限公司")],
    )
    session.commit()
    assert result["queued_events"] == 1
    complete_crm_order_required_fields(session, "crm_obj_oms_customer_found")
    run_pending_jobs(session)

    mapping = loads(session.get(SystemConfig, "v2_customer_mapping_json").value, {})
    order = session.query(MiddlePlatformOrder).one()
    assert mapping["武汉尺子科技有限公司"]["customer_code"] == "OMS-CUST-WH-CZ"
    assert order.status != OrderStatus.VALIDATION_BLOCKED.value
    assert session.query(OutboundMailJob).filter_by(mail_type="OmsCustomerMissing").count() == 0


def test_customer_mapping_queries_customized_then_legacy_customer_list(monkeypatch):
    import backend.app.services.customer_mapping as customer_mapping_service

    session = make_session()
    set_config(session, "oms_jackyun_app_key", "app-key")
    set_config(session, "oms_jackyun_app_secret", "app-secret", is_secret=True)
    set_config(session, "oms_customer_query_method", "crm.customer.list.customized,crm.customer.list")
    calls = []

    class FakeClient:
        def __init__(self, _config):
            pass

        def query_customers(self, method, payload):
            calls.append((method, payload))
            if method == "crm.customer.list.customized":
                return {"ok": True, "data": {"customers": [{"nickname": "其他客户", "customerCode": "OMS-OTHER"}]}}
            return {"ok": True, "data": {"customers": [{"nickname": "武汉尺子科技有限公司", "customerCode": "OMS-CUST-WH-CZ"}]}}

    monkeypatch.setattr(customer_mapping_service, "JackyunClient", FakeClient)

    customer, detail = query_oms_customer(session, "武汉尺子科技有限公司")

    assert customer == {"customer_name": "武汉尺子科技有限公司", "customer_code": "OMS-CUST-WH-CZ"}
    assert detail["status"] == "Found"
    assert detail["method"] == "crm.customer.list"
    assert [method for method, _payload in calls] == ["crm.customer.list.customized", "crm.customer.list"]
    assert calls[0][1]["nickname"] == "武汉尺子科技有限公司"
    assert calls[1][1]["nickname"] == "武汉尺子科技有限公司"


def test_customer_mapping_notifies_oms_admin_when_oms_customer_missing(monkeypatch):
    import backend.app.services.customer_mapping as customer_mapping_service

    session = make_session()
    seed_active_sku(session)
    seed_inventory(session, quantity=100)
    set_config(session, "v2_customer_mapping_json", dumps({}))
    set_config(session, "oms_admin_email", "oms-admin@example.com")

    def fake_query(_session, _customer_name):
        return None, {"status": "NotFound", "method": "mock.customer.query"}

    monkeypatch.setattr(customer_mapping_service, "query_oms_customer", fake_query)

    result = upsert_crm_sales_orders(
        session,
        [valid_crm_order_row(crm_order_id="crm_obj_oms_customer_missing", crm_order_no="SO-OMS-CUSTOMER-MISSING", customer_name="OMS不存在客户")],
    )
    session.commit()
    assert result["queued_events"] == 1
    run_pending_jobs(session)

    order = session.query(MiddlePlatformOrder).one()
    notice = session.query(OutboundMailJob).filter_by(mail_type="OmsCustomerMissing").one()
    assert order.status == OrderStatus.VALIDATION_BLOCKED.value
    assert loads(notice.to_json, []) == ["oms-admin@example.com"]
    assert "OMS不存在客户" in notice.body


def test_oms_config_requires_admin_email():
    session = make_session()

    with pytest.raises(HTTPException) as exc:
        update_oms_config(OmsRuntimeConfigUpdate(oms_enabled=True), session=session)

    assert exc.value.status_code == 400
    assert "OMS 管理员邮箱为必填项" in exc.value.detail


def test_oms_config_accepts_customer_query_options():
    session = make_session()

    update_oms_config(
        OmsRuntimeConfigUpdate(
            oms_admin_email="oms-admin@example.com",
            oms_customer_query_method="mock.customer.query",
            oms_customer_query_payload_json=dumps({"pageNo": 1, "pageSize": 20}),
        ),
        session=session,
    )

    assert session.get(SystemConfig, "oms_admin_email").value == "oms-admin@example.com"
    assert session.get(SystemConfig, "oms_customer_query_method").value == "mock.customer.query"


def test_crm_phase1_scope_config_can_be_updated_by_ops():
    session = make_session()
    set_config(session, "crm_system_owner_email", "crm-owner@example.com")
    update_crm_config(
        CrmRuntimeConfigUpdate(
            v2_crm_phase1_scope_enabled=True,
            v2_crm_phase1_scope_json=dumps(
                {
                    "approved_values": ["approved"],
                    "cancelled_values": ["cancelled"],
                    "include_owner_departments": ["商务一部"],
                    "include_settlement_methods": [],
                    "include_customer_names": [],
                }
            ),
        ),
        session=session,
    )

    blocked = upsert_crm_sales_orders(
        session,
        [valid_crm_order_row(crm_order_id="crm_scope_blocked", crm_order_no="SO-SCOPE-BLOCKED", owner_department="商务二部")],
    )
    allowed = upsert_crm_sales_orders(
        session,
        [valid_crm_order_row(crm_order_id="crm_scope_allowed", crm_order_no="SO-SCOPE-ALLOWED", owner_department="商务一部")],
    )
    session.commit()

    assert blocked["ignored"] == 1
    assert allowed["queued_events"] == 1
    assert session.query(CrmSalesOrder).filter_by(crm_order_id="crm_scope_blocked").one().scope_status == "Ignored"
    assert session.query(CrmSalesOrder).filter_by(crm_order_id="crm_scope_allowed").one().scope_status == "InScope"


def test_crm_phase1_scope_config_rejects_invalid_json():
    session = make_session()
    set_config(session, "crm_system_owner_email", "crm-owner@example.com")

    with pytest.raises(HTTPException) as exc:
        update_crm_config(CrmRuntimeConfigUpdate(v2_crm_phase1_scope_json="[]"), session=session)

    assert exc.value.status_code == 400
    assert "一期纳入范围配置" in exc.value.detail


def test_crm_config_updates_system_owner_email():
    session = make_session()

    update_crm_config(CrmRuntimeConfigUpdate(crm_system_owner_email="crm-owner@example.com"), session=session)

    assert session.get(SystemConfig, "crm_system_owner_email").value == "crm-owner@example.com"


def test_crm_config_updates_browser_ops_options():
    session = make_session()

    update_crm_config(
        CrmRuntimeConfigUpdate(
            crm_system_owner_email="crm-owner@example.com",
            crm_cdp_browser_mode="headed",
            crm_cdp_port=9444,
            crm_cdp_user_data_dir="/private/tmp/fxiaoke-cdp-profile-9444",
            crm_chrome_bin="/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
        ),
        session=session,
    )

    assert session.get(SystemConfig, "crm_cdp_browser_mode").value == "headed"
    assert session.get(SystemConfig, "crm_cdp_port").value == "9444"
    assert session.get(SystemConfig, "crm_cdp_url").value == "http://127.0.0.1:9444"
    assert session.get(SystemConfig, "crm_cdp_user_data_dir").value == "/private/tmp/fxiaoke-cdp-profile-9444"
    assert session.get(SystemConfig, "crm_chrome_bin").value.endswith("Google Chrome")


def test_crm_browser_default_start_ignores_saved_headed_mode(monkeypatch):
    import backend.app.main as main_app

    class FakeProcess:
        pid = 12345
        returncode = None

        def poll(self):
            return None

    calls = []

    def fake_popen(command, **kwargs):
        calls.append(command)
        return FakeProcess()

    session = make_session()
    set_config(session, "crm_cdp_browser_mode", "headed")
    monkeypatch.setattr(main_app, "_crm_browser_process", None)
    monkeypatch.setattr(main_app, "_crm_browser_meta", {})
    monkeypatch.setattr(main_app, "crm_external_browser_pids", lambda *args, **kwargs: [])
    monkeypatch.setattr(main_app, "crm_cdp_version", lambda *args, **kwargs: {})
    monkeypatch.setattr(main_app.subprocess, "Popen", fake_popen)

    result = main_app.start_crm_browser_process(session)

    assert result["mode"] == "headless"
    assert "--headed" not in calls[0]


def test_crm_browser_manual_login_explicitly_uses_headed(monkeypatch):
    import backend.app.main as main_app

    class FakeProcess:
        pid = 12346
        returncode = None

        def poll(self):
            return None

    calls = []

    def fake_popen(command, **kwargs):
        calls.append(command)
        return FakeProcess()

    session = make_session()
    monkeypatch.setattr(main_app, "_crm_browser_process", None)
    monkeypatch.setattr(main_app, "_crm_browser_meta", {})
    monkeypatch.setattr(main_app, "crm_external_browser_pids", lambda *args, **kwargs: [])
    monkeypatch.setattr(main_app, "crm_cdp_version", lambda *args, **kwargs: {})
    monkeypatch.setattr(main_app.subprocess, "Popen", fake_popen)

    result = main_app.start_crm_browser_process(session, requested_mode="headed")

    assert result["mode"] == "headed"
    assert "--headed" in calls[0]


def test_crm_config_rejects_invalid_system_owner_email():
    session = make_session()

    with pytest.raises(HTTPException) as exc:
        update_crm_config(CrmRuntimeConfigUpdate(crm_system_owner_email="crm-owner"), session=session)

    assert exc.value.status_code == 400
    assert "CRM 系统负责人邮箱" in exc.value.detail


def test_crm_config_rejects_invalid_browser_ops_options():
    session = make_session()
    set_config(session, "crm_system_owner_email", "crm-owner@example.com")

    with pytest.raises(HTTPException) as mode_exc:
        update_crm_config(CrmRuntimeConfigUpdate(crm_cdp_browser_mode="stealth"), session=session)
    with pytest.raises(HTTPException) as port_exc:
        update_crm_config(CrmRuntimeConfigUpdate(crm_cdp_port=80), session=session)

    assert mode_exc.value.status_code == 400
    assert "浏览器模式" in mode_exc.value.detail
    assert port_exc.value.status_code == 400
    assert "CDP 端口" in port_exc.value.detail


def test_crm_config_requires_system_owner_email():
    session = make_session()

    with pytest.raises(HTTPException) as exc:
        update_crm_config(CrmRuntimeConfigUpdate(crm_sync_interval_seconds=3600), session=session)

    assert exc.value.status_code == 400
    assert "CRM 系统负责人邮箱为必填项" in exc.value.detail


def test_crm_change_after_delivery_preview_blocks_and_expires_preview():
    session = make_session()
    order = create_delivery_ready_order(session)
    old_hash = order.payload_hash

    result = upsert_crm_sales_orders(session, [valid_crm_order_row(order_amount="126000.00", product_amount="126000.00", receivable_amount="101000.00")])
    session.commit()

    assert result["updated"] == 1
    run_pending_jobs(session)

    session.refresh(order)
    assert order.payload_hash == old_hash
    assert order.status == OrderStatus.VALIDATION_BLOCKED.value
    assert order.delivery_notices[0].status == "Stale"
    case = session.query(ExceptionCase).one()
    assert case.exception_type == "CRM_CHANGED_BEFORE_OMS_PUSH"


def test_crm_cancel_before_oms_push_cancels_order_and_preview():
    session = make_session()
    order = create_delivery_ready_order(session)

    result = upsert_crm_sales_orders(session, [valid_crm_order_row(life_status="cancelled", approval_status="cancelled")])
    session.commit()

    assert result["updated"] == 1
    run_pending_jobs(session)

    session.refresh(order)
    assert order.status == OrderStatus.CANCELLED.value
    assert order.delivery_notices[0].status == "Cancelled"
    case = session.query(ExceptionCase).one()
    assert case.exception_type == "CRM_CANCELLED_BEFORE_OMS_PUSH"


def test_crm_change_after_oms_accepted_creates_high_risk_exception_without_auto_change():
    session = make_session()
    order = create_delivery_ready_order(session)
    confirm_delivery_notice(session, order.delivery_notices[0], confirmed_by="tester")
    session.commit()
    run_pending_jobs(session)
    session.refresh(order)
    assert order.status == OrderStatus.OMS_ACCEPTED.value

    result = upsert_crm_sales_orders(session, [valid_crm_order_row(order_amount="126000.00", product_amount="126000.00", receivable_amount="101000.00")])
    session.commit()

    assert result["updated"] == 1
    run_pending_jobs(session)

    session.refresh(order)
    assert order.status == OrderStatus.OMS_ACCEPTED.value
    assert order.delivery_notices[0].status == "Accepted"
    case = session.query(ExceptionCase).one()
    assert case.exception_type == "CRM_CHANGED_AFTER_OMS_ACCEPTED"
    crm_order = session.query(CrmSalesOrder).filter_by(crm_order_id="crm_obj_001").one()
    detail = serialize_crm_order_with_flow(session, crm_order)
    alert = detail["flow"]["risk_alert"]
    assert alert["exception_type"] == "CRM_CHANGED_AFTER_OMS_ACCEPTED"
    assert alert["oms_status"] == "Accepted"
    assert "查看 OMS 单据状态" in alert["next_actions"]
    diff = detail["flow"]["snapshot_diff"]
    assert diff["from_version"] == 1
    assert diff["to_version"] == 2
    assert any(change["field"] == "amount" and "126000.00" in change["new_value"] for change in diff["changes"])
    context = exception_context(case.id, session)
    assert any(change["field"] == "amount" for change in context["snapshot_diff"]["changes"])


def test_crm_cancel_during_oms_pending_cancels_pending_push_job():
    session = make_session()
    order = create_delivery_ready_order(session)
    confirm_delivery_notice(session, order.delivery_notices[0], confirmed_by="tester")
    session.commit()
    push_job = session.query(ProcessingJob).filter_by(job_type="OMS_PUSH_NOTICE").one()
    assert push_job.status == "Pending"

    result = upsert_crm_sales_orders(session, [valid_crm_order_row(life_status="cancelled", approval_status="cancelled")])
    session.commit()

    assert result["updated"] == 1
    crm_order = session.query(CrmSalesOrder).filter_by(crm_order_id="crm_obj_001").one()
    process_crm_order_parsed_event(session, crm_order_parsed_event(crm_order))
    session.commit()

    session.refresh(order)
    session.refresh(push_job)
    assert order.status == OrderStatus.CANCELLED.value
    assert push_job.status == "Cancelled"
    assert order.delivery_notices[0].status == "Cancelled"
    case = session.query(ExceptionCase).one()
    assert case.exception_type == "CRM_CANCELLED_DURING_OMS_PENDING"
    detail = loads(case.detail, {})
    assert detail["exception"]["freeze_order_flow"] is True
    assert detail["exception"]["responsible_role"] == "商务/物流"


def test_crm_order_items_are_synced_from_order_products_only():
    session = make_session()
    upsert_crm_sales_orders(
        session,
        [
            valid_crm_order_row(
                crm_order_id="crm_obj_raw_items",
                crm_order_no="SO-RAW-ITEMS",
                order_items=[
                    {
                        "sku_code": "SKU-J6M",
                        "product_name": "MagicScan SC-J6M",
                        "specification": "J6M 标准版",
                        "quantity": "1",
                        "unit_price": "40000.000",
                        "line_amount": "40000.000",
                    }
                ],
            )
        ],
    )
    session.commit()
    crm_order = session.query(CrmSalesOrder).filter_by(crm_order_id="crm_obj_raw_items").one()
    item = session.query(CrmOrderItem).filter_by(order_id=crm_order.id).one()

    event = crm_order_parsed_event(crm_order)

    assert item.product_name == "MagicScan SC-J6M"
    assert item.specification == "J6M 标准版"
    assert event["data"]["order_items"] == [
        {
            "sku_code": "SKU-J6M",
            "product_name": "MagicScan SC-J6M",
            "specification": "J6M 标准版",
            "quantity": "1",
            "unit_price": "40000.000",
            "line_amount": "40000.000",
            "raw": {
                "sku_code": "SKU-J6M",
                "product_name": "MagicScan SC-J6M",
                "specification": "J6M 标准版",
                "quantity": "1",
                "unit_price": "40000.000",
                "line_amount": "40000.000",
            },
        }
    ]


def test_crm_order_parsed_event_does_not_fallback_to_raw_items_when_relation_empty():
    session = make_session()
    crm = CrmSalesOrder(
        source_system="fxiaoke",
        crm_order_id="crm_obj_legacy_items",
        crm_order_no="SO-LEGACY-ITEMS",
        payload_hash="hash-legacy-items",
        raw_json=dumps({"items": [{"product_name": "硬件设备—空间扫描仪", "quantity": "1"}]}),
    )
    session.add(crm)
    session.commit()

    event = crm_order_parsed_event(crm)

    assert event["data"]["order_items"] == []


def test_crm_change_during_oms_retry_uses_retry_exception_type(monkeypatch):
    import backend.app.services.order_middle_platform as omp

    class FailingJackyunClient:
        def create_delivery_order(self, payload, *, method="wms.order.create"):
            return {"ok": False, "message": "OMS gateway timeout", "raw": {"code": "TIMEOUT"}}

        def query_delivery_orders(self, payload):
            return {"ok": True, "data": {"rows": []}, "raw": {}}

    session = make_session()
    set_config(session, "oms_enabled", "true")
    set_config(session, "oms_mock_success", "false")
    set_config(session, "oms_max_retries", "3")
    session.commit()
    monkeypatch.setattr(omp, "jackyun_client_from_session", lambda _session: FailingJackyunClient())
    order = create_delivery_ready_order(session)
    confirm_delivery_notice(session, order.delivery_notices[0], confirmed_by="tester")
    session.commit()
    run_pending_jobs(session)
    session.refresh(order)
    assert order.status == OrderStatus.OMS_RETRYING.value

    result = upsert_crm_sales_orders(session, [valid_crm_order_row(order_amount="126000.00", product_amount="126000.00", receivable_amount="101000.00")])
    session.commit()
    assert result["updated"] == 1
    run_pending_jobs(session)

    session.refresh(order)
    case = session.query(ExceptionCase).filter_by(exception_type="CRM_CHANGED_DURING_OMS_RETRY").one()
    detail = loads(case.detail, {})
    assert order.status == OrderStatus.VALIDATION_BLOCKED.value
    assert order.delivery_notices[0].status == "Stale"
    assert detail["exception"]["responsible_role"] == "商务主管/物流/IT"


def test_validation_blocked_creates_context_pack_exception():
    session = make_session()
    crm = CrmSalesOrder(
        source_system="fxiaoke",
        crm_order_id="crm_obj_002",
        crm_order_no="SO-002",
        customer_name="缺 SKU 客户",
        sales_user_name="Alice",
        sales_user_email="alice@jimuyida.com",
        owner_department="商务一部",
        life_status="normal",
        approval_status="approved",
        order_date="2026-06-12",
        settlement_method="CNY",
        order_amount="100.00",
        product_amount="100.00",
        received_amount="0.00",
        receivable_amount="100.00",
        currency="CNY",
        receipt_contact="李四",
        receipt_phone="18600003333",
        receipt_address="上海市测试路 2 号",
        delivery_date="2026-06-25",
        attachment_files_json=dumps(["盖章采购订单.pdf"]),
        payload_hash="hash-002",
        raw_json=dumps({"items": [{"sku_code": "UNKNOWN-SKU", "quantity": 1}]}),
    )
    session.add(crm)
    session.flush()
    add_crm_order_item(
        session,
        crm,
        sku_code="UNKNOWN-SKU",
        product_name="未知 SKU 产品",
        quantity="1",
        unit_price="100.00",
        line_amount="100.00",
    )
    session.commit()

    payload = {
        "trace_id": "test-trace",
        "event_type": "CRM_ORDER_PARSED",
        "source_system": "FXIAOKE",
        "data": {
            "crm_sales_order_id": crm.id,
            "crm_order_id": crm.crm_order_id,
            "payload_hash": crm.payload_hash,
            "order_head": {"crm_order_no": crm.crm_order_no, "customer_name": crm.customer_name, "amount": 100.0, "currency": "CNY"},
            "order_items": [{"sku_code": "UNKNOWN-SKU", "quantity": 1}],
        },
    }
    result = process_crm_order_parsed_event(session, payload)
    session.commit()

    assert result["validation_passed"] is False
    order = session.query(MiddlePlatformOrder).one()
    assert order.status == OrderStatus.VALIDATION_BLOCKED.value
    detail = loads(session.query(ExceptionCase).one().detail, {})
    assert detail["context_type"] == "V2_ORDER_EXCEPTION"
    assert detail["order"]["order_no"] == order.order_no
    mail = session.query(OutboundMailJob).one()
    assert mail.mail_type == "V2ValidationFailed"
    assert "预审未通过" in mail.subject


def test_middle_order_items_prefer_crm_order_items_over_legacy_items():
    session = make_session()
    crm = CrmSalesOrder(
        source_system="fxiaoke",
        crm_order_id="crm-order-items-priority",
        crm_order_no="20260616-007196",
        customer_name="测试公司广州遇见小面",
        life_status="normal",
        approval_status="approved",
        order_date="2026-06-16",
        settlement_method="人民币结算CNY",
        order_amount="6000.00",
        product_amount="6000.00",
        currency="CNY",
        receipt_contact="刘莉莉",
        receipt_phone="18888009988",
        receipt_address="广州市白云区北京西路724号",
        attachment_files_json=dumps([]),
        payload_hash="hash-order-items-priority",
        raw_json=dumps(
            {
                "items": [{"product_name": "硬件设备—Seal", "quantity": "1", "unit_price": "6000.00", "line_amount": "6000.00"}],
                "order_items": [{"product_name": "seal扫描仪", "quantity": "1", "unit_price": "6000.000", "line_amount": "6000.000"}],
            }
        ),
    )
    session.add(crm)
    session.flush()
    session.add(
        CrmOrderItem(
            order_id=crm.id,
            source_system=crm.source_system,
            crm_item_id="crm-order-items-priority:1",
            crm_order_id=crm.crm_order_id,
            crm_order_no=crm.crm_order_no,
            product_name="seal扫描仪",
            quantity="1",
            unit_price="6000.000",
            line_amount="6000.000",
            payload_hash=crm.payload_hash,
            raw_json=dumps({"product_name": "seal扫描仪", "quantity": "1", "unit_price": "6000.000", "line_amount": "6000.000"}),
        )
    )
    session.commit()

    order = upsert_middle_platform_order(session, crm)

    assert [item.product_name for item in order.items] == ["seal扫描仪"]


def test_middle_order_items_map_sku_by_product_name_semantic_match():
    session = make_session()
    spu = ProductSPU(spu_id="SPU-MOOSE", name="Moose 扫描仪", name_en="Moose Scanner", category="成品", status="Active")
    session.add(spu)
    session.flush()
    session.add(ProductSKU(spu_uuid=spu.id, sku_id="SKU-MOOSE-001", status="Active"))
    crm = CrmSalesOrder(
        source_system="fxiaoke",
        crm_order_id="crm-product-name-sku",
        crm_order_no="SO-PRODUCT-NAME-SKU",
        customer_name="产品名客户",
        payload_hash="hash-product-name-sku",
        raw_json=dumps(
            {
                "order_items": [
                    {"product_name": "Moose 扫描仪", "quantity": "1", "unit_price": "5999.00", "line_amount": "5999.00"}
                ]
            }
        ),
    )
    session.add(crm)
    session.flush()
    session.add(
        CrmOrderItem(
            order_id=crm.id,
            source_system=crm.source_system,
            crm_item_id="crm-product-name-sku:1",
            crm_order_id=crm.crm_order_id,
            crm_order_no=crm.crm_order_no,
            product_name="Moose 扫描仪",
            quantity="1",
            unit_price="5999.00",
            line_amount="5999.00",
            payload_hash=crm.payload_hash,
            raw_json=dumps({"product_name": "Moose 扫描仪", "quantity": "1", "unit_price": "5999.00", "line_amount": "5999.00"}),
        )
    )
    session.commit()

    order = upsert_middle_platform_order(session, crm)

    assert order.items[0].sku_code == "SKU-MOOSE-001"
    mapping = loads(order.items[0].raw_json, {})["sku_mapping"]
    assert mapping["source"] == "product_name_semantic"
    assert mapping["matched"] is True
    assert mapping["confidence"] >= 80


def test_middle_order_items_keep_manual_review_when_product_name_match_low_confidence():
    session = make_session()
    spu = ProductSPU(spu_id="SPU-SEAL", name="Seal 扫描仪", name_en="Seal Scanner", category="成品", status="Active")
    session.add(spu)
    session.flush()
    session.add(ProductSKU(spu_uuid=spu.id, sku_id="SKU-SEAL-001", status="Active"))
    seed_inventory(session, "SPU-SEAL", quantity=10)
    crm = CrmSalesOrder(
        source_system="fxiaoke",
        crm_order_id="crm-product-name-low-confidence",
        crm_order_no="SO-PRODUCT-NAME-LOW",
        customer_name="产品名客户",
        payload_hash="hash-product-name-low",
        raw_json=dumps(
            {
                "order_items": [
                    {"product_name": "完全未知产品", "quantity": "1", "unit_price": "5999.00", "line_amount": "5999.00"}
                ]
            }
        ),
    )
    session.add(crm)
    session.flush()
    add_crm_order_item(
        session,
        crm,
        product_name="完全未知产品",
        quantity="1",
        unit_price="5999.00",
        line_amount="5999.00",
    )
    session.commit()

    order = upsert_middle_platform_order(session, crm)
    results = run_validation_chain(session, order)
    known_sku = next(item for item in results if item.rule_code == "KNOWN_ACTIVE_SKU")

    assert order.items[0].sku_code in (None, "")
    assert known_sku.passed is False
    assert "需人工" in known_sku.reason


def test_pre_review_blocks_when_product_name_sku_match_requires_manual_confirmation():
    session = make_session()
    set_config(session, "v2_review_customer_mapping_required", "false")
    for index in range(2):
        spu = ProductSPU(spu_id=f"SPU-MOOSE-{index}", name=f"Moose 扫描仪 {index}", category="成品", status="Active")
        session.add(spu)
        session.flush()
        session.add(ProductSKU(spu_uuid=spu.id, sku_id=f"SKU-MOOSE-{index}", status="Active"))
    crm = CrmSalesOrder(
        source_system="fxiaoke",
        crm_order_id="crm-product-name-ambiguous",
        crm_order_no="SO-PRODUCT-NAME-AMBIGUOUS",
        customer_name="产品名客户",
        sales_user_name="Alice",
        sales_user_email="alice@jimuyida.com",
        owner_department="商务一部",
        life_status="normal",
        approval_status="approved",
        order_date="2026-06-16",
        settlement_method="人民币结算CNY",
        order_amount="5999.00",
        product_amount="5999.00",
        received_amount="0.00",
        receivable_amount="5999.00",
        currency="CNY",
        receipt_contact="刘莉莉",
        receipt_phone="18888009988",
        receipt_address="广州市白云区北京西路724号",
        delivery_date="2026-06-30",
        attachment_files_json=dumps(["盖章采购订单.pdf"]),
        payload_hash="hash-product-name-ambiguous",
        raw_json=dumps(
            {
                "life_status": "normal",
                "order_items": [
                    {"product_name": "硬件设备—Moose", "quantity": "1", "unit_price": "5999.00", "line_amount": "5999.00"}
                ],
            }
        ),
    )
    session.add(crm)
    session.flush()
    add_crm_order_item(
        session,
        crm,
        product_name="硬件设备—Moose",
        quantity="1",
        unit_price="5999.00",
        line_amount="5999.00",
    )
    session.commit()

    result = process_crm_order_parsed_event(session, crm_order_parsed_event(crm))
    session.commit()

    order = session.query(MiddlePlatformOrder).one()
    summary = loads(order.validation_summary_json, {})
    failed_codes = [item["rule_code"] for item in summary.get("results", []) if not item.get("passed")]
    assert result["validation_passed"] is False
    assert order.status == OrderStatus.VALIDATION_BLOCKED.value
    assert "KNOWN_ACTIVE_SKU" in failed_codes
    assert order.delivery_notices == []
    mail = session.query(OutboundMailJob).filter_by(mail_type="V2ValidationFailed").one()
    assert "商品/SKU 匹配问题" in mail.body
    assert "CRM 商品：硬件设备—Moose" in mail.body
    assert "当前值：候选 SKU 过多，无法自动选择" in mail.body
    assert "可能匹配项（按相似度排序）：" in mail.body
    assert "SKU-MOOSE-0｜Moose 扫描仪 0｜相似度" in mail.body
    assert "SKU-MOOSE-1｜Moose 扫描仪 1｜相似度" in mail.body


def test_force_revalidate_resets_delivery_ready_order_and_blocks_failed_review():
    session = make_session()
    set_config(session, "v2_review_customer_mapping_required", "false")
    for index in range(2):
        spu = ProductSPU(spu_id=f"SPU-MOOSE-REVALIDATE-{index}", name=f"Moose 扫描仪 {index}", category="成品", status="Active")
        session.add(spu)
        session.flush()
        session.add(ProductSKU(spu_uuid=spu.id, sku_id=f"SKU-MOOSE-REVALIDATE-{index}", status="Active"))
    crm = CrmSalesOrder(
        source_system="fxiaoke",
        crm_order_id="crm-force-revalidate-ambiguous",
        crm_order_no="SO-FORCE-REVALIDATE-AMBIGUOUS",
        customer_name="产品名客户",
        sales_user_name="Alice",
        sales_user_email="alice@jimuyida.com",
        owner_department="商务一部",
        life_status="normal",
        approval_status="approved",
        order_date="2026-06-16",
        settlement_method="人民币结算CNY",
        order_amount="125000.00",
        product_amount="125000.00",
        received_amount="0.00",
        receivable_amount="125000.00",
        currency="CNY",
        receipt_contact="刘莉莉",
        receipt_phone="18888009988",
        receipt_address="广州市白云区北京西路724号",
        delivery_date="2026-06-30",
        attachment_files_json=dumps(["盖章采购订单.pdf"]),
        payload_hash="hash-force-revalidate-ambiguous",
        raw_json=dumps(
            {
                "life_status": "normal",
                "order_items": [
                    {"product_name": "硬件设备—Moose", "quantity": "1", "unit_price": "125000.00", "line_amount": "125000.00"}
                ],
            }
        ),
    )
    session.add(crm)
    session.flush()
    add_crm_order_item(
        session,
        crm,
        product_name="硬件设备—Moose",
        quantity="1",
        unit_price="125000.00",
        line_amount="125000.00",
    )
    order = MiddlePlatformOrder(
        order_no="MP-SO-FORCE-REVALIDATE-AMBIGUOUS",
        source_system=crm.source_system,
        crm_sales_order_id=crm.id,
        crm_order_id=crm.crm_order_id,
        crm_order_no=crm.crm_order_no,
        payload_hash=crm.payload_hash,
        customer_name=crm.customer_name,
        sales_user_name=crm.sales_user_name,
        currency=crm.currency,
        order_amount=crm.order_amount,
        status=OrderStatus.DELIVERY_NOTICE_READY.value,
    )
    session.add(order)
    session.flush()
    notice = DeliveryNotice(
        notice_no="DN-SO-FORCE-REVALIDATE-AMBIGUOUS",
        order_id=order.id,
        source_snapshot_hash=order.payload_hash,
        status="Previewed",
        oms_idempotency_key="force-revalidate-ambiguous",
    )
    session.add(notice)
    session.flush()
    notice_id = notice.id
    session.commit()

    event = crm_order_parsed_event(crm, trace_id="force-revalidate-ambiguous-sku")
    event["force_revalidate"] = True
    result = process_crm_order_parsed_event(session, event)
    session.commit()

    session.refresh(order)
    failed_codes = [
        item["rule_code"]
        for item in loads(order.validation_summary_json, {}).get("results", [])
        if not item.get("passed")
    ]
    assert result["validation_passed"] is False
    assert order.status == OrderStatus.VALIDATION_BLOCKED.value
    assert "KNOWN_ACTIVE_SKU" in failed_codes
    assert session.get(DeliveryNotice, notice_id).status == "Stale"
    flow = serialize_crm_order_with_flow(session, crm)["flow"]
    notice_step = next(step for step in flow["steps"] if step["key"] == "notice")
    oms_step = next(step for step in flow["steps"] if step["key"] == "oms")
    assert notice_step["status"] == "pending"
    assert notice_step["description"] == "尚未生成发货通知"
    assert "time" not in notice_step or notice_step["time"] is None
    assert oms_step["status"] == "pending"


def test_inventory_rule_blocks_when_available_quantity_is_short():
    session = make_session()
    seed_active_sku(session)
    session.add(
        ProductInventorySnapshot(
            material_code="SKU-3D-SCANNER-PRO",
            material_name="3D Scanner",
            warehouse_code="WH01",
            warehouse_name="武汉工厂仓",
            base_qty=1,
            qty=1,
            source_payload_json=dumps({"canUseQuantity": 1}),
        )
    )
    session.commit()
    result = upsert_crm_sales_orders(
        session,
        [
            {
                "crm_order_id": "crm_obj_stock_short",
                "crm_order_no": "SO-STOCK-SHORT",
                "customer_name": "库存不足客户",
                "sales_user_name": "Alice",
                "sales_user_email": "alice@jimuyida.com",
                "owner_department": "商务一部",
                "life_status": "normal",
                "approval_status": "approved",
                "order_date": "2026-06-12",
                "order_amount": "200.00",
                "product_amount": "200.00",
                "received_amount": "0.00",
                "receivable_amount": "200.00",
                "settlement_method": "CNY",
                "receipt_contact": "王五",
                "receipt_phone": "18600004444",
                "receipt_address": "广东省深圳市南山区测试路 3 号 501 室",
                "delivery_date": "2026-06-28",
                "attachment_files": "盖章采购订单.pdf",
                "items": [{"sku_code": "SKU-3D-SCANNER-PRO", "quantity": 2, "unit_price": "100", "line_amount": "200"}],
            }
        ],
    )
    session.commit()

    assert result["queued_events"] == 1
    complete_crm_order_required_fields(session, "crm_obj_stock_short")
    run_pending_jobs(session)

    order = session.query(MiddlePlatformOrder).one()
    assert order.status == OrderStatus.VALIDATION_BLOCKED.value
    detail = loads(session.query(ExceptionCase).one().detail, {})
    assert detail["validation"]["failed_rules"][0]["rule_code"] == "INVENTORY_THREE_STEP"


def test_phase_one_missing_fields_interrupts_and_notifies_stakeholders():
    session = make_session()
    result = upsert_crm_sales_orders(
        session,
        [
            {
                "crm_order_id": "crm_obj_missing",
                "crm_order_no": "SO-MISSING",
                "customer_name": "字段缺失客户",
                "order_amount": "100.00",
                "settlement_method": "option1",
            }
        ],
    )
    session.commit()

    assert result["queued_events"] == 1
    run_pending_jobs(session)

    order = session.query(MiddlePlatformOrder).one()
    assert order.status == OrderStatus.VALIDATION_BLOCKED.value
    detail = loads(session.query(ExceptionCase).one().detail, {})
    failed = detail["validation"]["failed_rules"][0]
    exception = detail["exception"]
    assert failed["rule_code"] == "PHASE1_COMPLETE_PRE_REVIEW_FIELDS"
    assert "销售负责人" in failed["reason"]
    assert exception["source_system"] == "CRM"
    assert exception["responsible_role"] == "商务/销售"
    assert exception["can_auto_retry"] is False
    assert exception["freeze_order_flow"] is True
    assert exception["evidence_refs"]
    mail = session.query(OutboundMailJob).one()
    assert mail.mail_type == "V2ValidationFailed"
    assert "暂不会生成发货通知或下推 OMS" in mail.body
    assert "预审时间：" in mail.body
    assert "（北京时间）" in mail.body
    assert "缺少或需修正的基础资料：" in mail.body
    assert "证据来源：" in mail.body


def test_phase_one_completeness_does_not_require_approval_status():
    session = make_session()
    result = upsert_crm_sales_orders(
        session,
        [
            valid_crm_order_row(
                approval_status="",
                settlement_method="CNY",
                receipt_contact="赵物流",
                receipt_phone="18612345678",
                receipt_address="广东省深圳市南山区科技园测试路 8 号",
                order_items=[{"sku_code": "SKU-3D-SCANNER-PRO", "quantity": 1, "unit_price": "100.00", "line_amount": "100.00"}],
            )
        ],
    )
    session.commit()

    assert result["queued_events"] == 1
    crm = session.query(CrmSalesOrder).filter_by(crm_order_id="crm_obj_001").one()
    crm.receipt_contact = "赵物流"
    crm.receipt_phone = "18612345678"
    crm.receipt_address = "广东省深圳市南山区科技园测试路 8 号"
    session.commit()
    order = upsert_middle_platform_order(session, crm)
    from backend.app.services.rules import OrderContext
    from backend.app.services.rules.phase_one_completeness import PhaseOneCompletenessRule

    result = PhaseOneCompletenessRule().validate(OrderContext(order=order, crm_order=crm, items=list(order.items), session=session))

    assert result.passed is True


def test_domestic_order_defaults_settlement_method_to_cny_for_phase_one_review():
    session = make_session()
    upsert_crm_sales_orders(
        session,
        [
            valid_crm_order_row(
                customer_name="测试公司北京百度",
                country_region="中国",
                settlement_method="",
                receipt_contact="赵物流",
                receipt_phone="18612345678",
                receipt_address="广东省深圳市南山区科技园测试路 8 号",
                attachment_files="盖章采购订单.pdf",
                order_items=[{"sku_code": "SKU-3D-SCANNER-PRO", "quantity": 1, "unit_price": "100.00", "line_amount": "100.00"}],
            )
        ],
    )
    session.commit()
    crm = session.query(CrmSalesOrder).filter_by(crm_order_id="crm_obj_001").one()
    crm.receipt_contact = "赵物流"
    crm.receipt_phone = "18612345678"
    crm.receipt_address = "广东省深圳市南山区科技园测试路 8 号"
    session.commit()
    order = upsert_middle_platform_order(session, crm)
    from backend.app.services.rules import OrderContext
    from backend.app.services.rules.phase_one_completeness import PhaseOneCompletenessRule

    result = PhaseOneCompletenessRule().validate(OrderContext(order=order, crm_order=crm, items=list(order.items), session=session))

    assert crm.settlement_method == "人民币结算"
    assert crm.currency == "CNY"
    assert order.currency == "CNY"
    assert result.passed is True


def test_overseas_order_requires_explicit_settlement_method_for_phase_one_review():
    session = make_session()
    upsert_crm_sales_orders(
        session,
        [
            valid_crm_order_row(
                customer_name="InovaMetrics LLC",
                country_region="美国",
                settlement_method="",
                receipt_contact="John",
                receipt_phone="18000000000",
                receipt_address="美国 CA 94016 Market Street 100",
                attachment_files="盖章采购订单.pdf",
                order_items=[{"sku_code": "SKU-3D-SCANNER-PRO", "quantity": 1, "unit_price": "100.00", "line_amount": "100.00"}],
            )
        ],
    )
    session.commit()
    crm = session.query(CrmSalesOrder).filter_by(crm_order_id="crm_obj_001").one()
    crm.receipt_contact = "John"
    crm.receipt_phone = "18000000000"
    crm.receipt_address = "美国 CA 94016 Market Street 100"
    session.commit()
    order = upsert_middle_platform_order(session, crm)
    from backend.app.services.rules import OrderContext
    from backend.app.services.rules.phase_one_completeness import PhaseOneCompletenessRule

    result = PhaseOneCompletenessRule().validate(OrderContext(order=order, crm_order=crm, items=list(order.items), session=session))

    assert crm.settlement_method in (None, "")
    assert result.passed is False
    assert "结算方式" in result.reason


def test_v2_review_rule_config_lists_registered_rules_only():
    session = make_session()

    config = review_rule_config(session)

    assert [rule["code"] for rule in config["rules"]] == [rule.get_rule_code() for rule in DEFAULT_RULES]
    assert "WAREHOUSE_ROUTING" not in {rule["code"] for rule in config["rules"]}
    assert all(rule["enabled"] is True for rule in config["rules"])


def test_disabled_v2_review_rule_is_skipped_by_validation_chain():
    session = make_session()
    result = upsert_crm_sales_orders(
        session,
        [
            valid_crm_order_row(
                customer_id="",
                customer_name="未维护客户",
                receipt_contact="",
                receipt_phone="",
                receipt_address="",
            )
        ],
    )
    session.commit()
    crm = session.query(CrmSalesOrder).filter_by(crm_order_id="crm_obj_001").one()
    crm.receipt_contact = "赵物流"
    crm.receipt_phone = "18612345678"
    crm.receipt_address = "广东省深圳市南山区科技园测试路 8 号"
    session.commit()
    order = upsert_middle_platform_order(session, crm)

    enabled_results = run_validation_chain(session, order)
    set_config(session, "v2_review_rule_states_json", dumps({"CUSTOMER_MAPPING": {"enabled": False}}), is_secret=False)
    session.commit()
    disabled_results = run_validation_chain(session, order)

    assert result["queued_events"] == 1
    assert any(item.rule_code == "CUSTOMER_MAPPING" for item in enabled_results)
    assert all(item.rule_code != "CUSTOMER_MAPPING" for item in disabled_results)


def test_validation_failure_notification_prefers_crm_owner_email():
    session = make_session()
    set_config(session, "v2_validation_failure_to_json", dumps(["stakeholder@example.com"]))
    set_config(session, "v2_validation_failure_cc_json", dumps(["ops@example.com"]))
    result = upsert_crm_sales_orders(
        session,
        [
            {
                "crm_order_id": "crm_obj_owner_email",
                "crm_order_no": "SO-OWNER-EMAIL",
                "customer_name": "负责人邮箱客户",
                "sales_user_name": "张负责人",
                "sales_user_email": "owner@example.com",
                "owner_department": "商务一部",
                "order_amount": "100.00",
                "settlement_method": "option1",
            }
        ],
    )
    session.commit()

    assert result["queued_events"] == 1
    run_pending_jobs(session)

    mail = session.query(OutboundMailJob).filter_by(mail_type="V2ValidationFailed").one()
    assert loads(mail.to_json, []) == ["owner@example.com"]
    assert loads(mail.cc_json, []) == []


def test_validation_failure_notification_falls_back_to_crm_system_owner_email():
    session = make_session()
    set_config(session, "crm_system_owner_email", "crm-owner@example.com")
    result = upsert_crm_sales_orders(
        session,
        [
            {
                "crm_order_id": "crm_obj_system_owner_email",
                "crm_order_no": "SO-SYSTEM-OWNER-EMAIL",
                "customer_name": "CRM系统负责人兜底客户",
                "sales_user_name": "张负责人",
                "owner_department": "商务一部",
                "order_amount": "100.00",
                "settlement_method": "option1",
            }
        ],
    )
    session.commit()

    assert result["queued_events"] == 1
    run_pending_jobs(session)

    mail = session.query(OutboundMailJob).filter_by(mail_type="V2ValidationFailed").one()
    assert loads(mail.to_json, []) == ["crm-owner@example.com"]


def test_validation_failure_notification_dedupes_same_failure_across_payload_changes():
    session = make_session()

    result = upsert_crm_sales_orders(
        session,
        [
            {
                "crm_order_id": "crm_obj_dedupe_notice",
                "crm_order_no": "SO-DEDUPE-NOTICE",
                "customer_name": "字段缺失客户",
                "order_amount": "100.00",
                "settlement_method": "option1",
            }
        ],
    )
    session.commit()
    assert result["queued_events"] == 1
    run_pending_jobs(session)

    changed = upsert_crm_sales_orders(
        session,
        [
            {
                "crm_order_id": "crm_obj_dedupe_notice",
                "crm_order_no": "SO-DEDUPE-NOTICE",
                "customer_name": "字段缺失客户",
                "order_amount": "100.00",
                "settlement_method": "option1",
                "remark": "补充了不影响阻断原因的备注",
            }
        ],
    )
    session.commit()
    assert changed["queued_events"] == 1
    run_pending_jobs(session)

    mails = session.query(OutboundMailJob).filter_by(mail_type="V2ValidationFailed").all()
    assert len(mails) == 1


def test_validation_failure_notification_cancels_stale_pending_for_same_order():
    session = make_session()

    result = upsert_crm_sales_orders(
        session,
        [
            {
                "crm_order_id": "crm_obj_supersede_notice",
                "crm_order_no": "SO-SUPERSEDE-NOTICE",
                "customer_name": "字段缺失客户",
                "sales_user_email": "owner@example.com",
                "order_amount": "100.00",
                "settlement_method": "option1",
            }
        ],
    )
    session.commit()
    assert result["queued_events"] == 1
    run_pending_jobs(session)
    first = session.query(OutboundMailJob).filter_by(mail_type="V2ValidationFailed").one()
    assert first.status == "Pending"

    changed = upsert_crm_sales_orders(
        session,
        [
            {
                "crm_order_id": "crm_obj_supersede_notice",
                "crm_order_no": "SO-SUPERSEDE-NOTICE",
                "customer_name": "字段缺失客户",
                "sales_user_email": "owner@example.com",
                "order_amount": "100.00",
                "settlement_method": "option1",
                "items": [{"product_name": "未映射商品", "quantity": 1, "unit_price": "100", "line_amount": "100"}],
            }
        ],
    )
    session.commit()
    assert changed["queued_events"] == 1
    run_pending_jobs(session)

    mails = session.query(OutboundMailJob).filter_by(mail_type="V2ValidationFailed").order_by(OutboundMailJob.created_at).all()
    assert [mail.status for mail in mails] == ["Cancelled", "Pending"]
    assert mails[0].last_error == "superseded by newer validation failure notification"
    assert "商品/SKU 匹配问题" in mails[1].body
    assert "CRM 商品：未映射商品" in mails[1].body
    assert "KNOWN_ACTIVE_SKU" not in mails[1].body


def test_validation_failure_notification_requeues_cancelled_same_digest():
    session = make_session()

    result = upsert_crm_sales_orders(
        session,
        [
            {
                "crm_order_id": "crm_obj_requeue_notice",
                "crm_order_no": "SO-REQUEUE-NOTICE",
                "customer_name": "字段缺失客户",
                "sales_user_email": "owner@example.com",
                "order_amount": "100.00",
                "settlement_method": "option1",
            }
        ],
    )
    session.commit()
    assert result["queued_events"] == 1
    run_pending_jobs(session)

    mail = session.query(OutboundMailJob).filter_by(mail_type="V2ValidationFailed").one()
    mail.status = "Cancelled"
    mail.last_error = "queue cleared by ops"
    session.commit()

    unchanged = upsert_crm_sales_orders(
        session,
        [
            {
                "crm_order_id": "crm_obj_requeue_notice",
                "crm_order_no": "SO-REQUEUE-NOTICE",
                "customer_name": "字段缺失客户",
                "sales_user_email": "owner@example.com",
                "order_amount": "100.00",
                "settlement_method": "option1",
            }
        ],
    )
    session.commit()
    assert unchanged["queued_events"] == 0
    crm = session.query(CrmSalesOrder).filter_by(crm_order_id="crm_obj_requeue_notice").one()
    event = crm_order_parsed_event(crm, trace_id="requeue-cancelled-notice")
    event["force_revalidate"] = True
    process_crm_order_parsed_event(session, event)
    session.commit()

    session.refresh(mail)
    assert mail.status == "Pending"
    assert mail.last_error is None
    assert mail.attempt_count == 0
    assert "字段缺失客户" in mail.body
    assert session.query(OutboundMailJob).filter_by(mail_type="V2ValidationFailed").count() == 1
    assert session.query(AuditEvent).filter_by(event_type="ValidationFailureNotificationRequeued").count() == 1


def test_validation_failure_notification_resolves_exception_on_sent(monkeypatch):
    class FakeSMTP:
        def __init__(self, host, port, **kwargs):
            pass
        def __enter__(self):
            return self
        def __exit__(self, exc_type, exc, traceback):
            return False
        def login(self, username, password):
            pass
        def send_message(self, msg, from_addr, to_addrs):
            pass

    monkeypatch.setattr("backend.app.services.mail_adapter.smtplib.SMTP_SSL", FakeSMTP)

    from backend.app.services.mail_adapter import send_pending_smtp
    session = make_session()
    set_config(session, "bot_email_password", "runtime-secret", is_secret=True)

    result = upsert_crm_sales_orders(
        session,
        [
            {
                "crm_order_id": "crm_obj_resolve_exception",
                "crm_order_no": "SO-RESOLVE-EXCEPTION",
                "customer_name": "字段缺失客户",
                "order_amount": "100.00",
                "settlement_method": "option1",
            }
        ],
    )
    session.commit()
    assert result["queued_events"] == 1
    run_pending_jobs(session)

    # Verify we have an Open exception case and a Pending mail job
    from backend.app.models import ExceptionCase, OutboundMailJob, AuditEvent
    case = session.query(ExceptionCase).filter_by(exception_type="VALIDATION_BLOCKED").one()
    assert case.status == "Open"

    mail = session.query(OutboundMailJob).filter_by(mail_type="V2ValidationFailed").one()
    assert mail.status == "Pending"

    # Send pending mail job
    send_pending_smtp(session)
    session.commit()

    # Verify that the mail job status becomes Sent, and ExceptionCase status becomes Resolved
    session.refresh(mail)
    session.refresh(case)
    assert mail.status == "Sent"
    assert case.status == "Resolved"
    assert case.resolution_note == "预审不通过且已成功发送通知邮件，自动标记为已解决"

    # Verify that an AuditEvent was created
    audit = session.query(AuditEvent).filter_by(event_type="ExceptionResolved", related_object_id=case.id).one()
    assert "预审不通过且已成功发送通知邮件" in audit.detail


def test_registered_crm_attachments_enrich_receiver_fields_before_pre_review(monkeypatch):
    import backend.app.services.order_middle_platform as omp

    session = make_session()
    seed_active_sku(session)
    seed_inventory(session, quantity=100)
    result = upsert_crm_sales_orders(
        session,
        [
            valid_crm_order_row(
                receipt_contact="",
                receipt_phone="",
                receipt_address="",
                attachments=[{"file_name": "客户PO.pdf", "file_url": "https://example.test/po.pdf", "file_id": "file-001"}],
            )
        ],
    )
    session.commit()

    def fake_enrich(_session, crm_order):
        crm_order.receipt_contact = "赵物流"
        crm_order.receipt_phone = "18612345678"
        crm_order.receipt_address = "广东省深圳市南山区科技园测试路 8 号"
        raw = loads(crm_order.raw_json, {})
        raw["oms_field_extraction"] = {"source": "attachment", "confidence": 91}
        crm_order.raw_json = dumps(raw)
        return SimpleNamespace(as_dict=lambda: raw["oms_field_extraction"])

    monkeypatch.setattr(omp, "enrich_order_from_registered_attachments", fake_enrich)

    assert result["queued_events"] == 1
    run_pending_jobs(session)

    crm = session.query(CrmSalesOrder).filter_by(crm_order_id="crm_obj_001").one()
    order = session.query(MiddlePlatformOrder).one()
    assert crm.receipt_contact == "赵物流"
    assert crm.receipt_phone == "18612345678"
    assert crm.receipt_address == "广东省深圳市南山区科技园测试路 8 号"
    assert order.status == OrderStatus.DELIVERY_NOTICE_READY.value
    assert session.query(AuditEvent).filter_by(event_type="CrmAttachmentOmsFieldsExtracted").count() == 1


def test_registered_crm_attachments_extracts_contact_only_once(monkeypatch):
    session = make_session()
    crm = CrmSalesOrder(
        crm_order_id="crm_obj_extract_once",
        crm_order_no="SO-EXTRACT-ONCE",
        payload_hash="hash-extract-once",
        raw_json=dumps({}),
    )
    session.add(crm)
    session.flush()
    attachment = OrderAttachment(
        crm_sales_order_id=crm.id,
        source_system=crm.source_system,
        crm_order_id=crm.crm_order_id,
        crm_order_no=crm.crm_order_no,
        payload_hash=crm.payload_hash,
        file_name="客户PO.pdf",
        fingerprint="fingerprint-extract-once",
        evidence_json=dumps({"parsed_text": "收货人：张三 电话：18600001111 地址：湖北省武汉市东湖高新区测试路 1 号"}),
    )
    session.add(
        attachment
    )
    signature = crm_attachment_extraction.attachment_text_signature([(attachment, "收货人：张三 电话：18600001111 地址：湖北省武汉市东湖高新区测试路 1 号")])
    crm.raw_json = dumps(
        {
            "oms_field_extraction": {
                "source": "llm",
                "manual_review_required": True,
                "attachment_hash": signature,
                "parser_version": crm_attachment_extraction.ATTACHMENT_PARSER_VERSION,
                "extractor_version": crm_attachment_extraction.OMS_FIELD_EXTRACTOR_VERSION,
            }
        }
    )
    session.commit()
    monkeypatch.setattr(crm_attachment_extraction, "extract_oms_fields_by_rule", lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("should not extract twice")))

    result = crm_attachment_extraction.enrich_order_from_registered_attachments(session, crm)

    assert result is None
    assert crm.receipt_contact in (None, "")
    assert crm.receipt_phone in (None, "")


def test_pre_review_parses_registered_attachments_even_when_receiver_fields_complete(monkeypatch):
    session = make_session()
    seed_active_sku(session)
    seed_inventory(session, quantity=100)
    result = upsert_crm_sales_orders(
        session,
        [
            valid_crm_order_row(
                crm_order_id="crm_obj_parse_attachment_for_review",
                crm_order_no="SO-PARSE-ATTACHMENT-FOR-REVIEW",
                order_amount="50000.00",
                product_amount="50000.00",
                received_amount="0.00",
                receivable_amount="50000.00",
                order_items=[{"sku_code": "SKU-3D-SCANNER-PRO", "product_name": "空间扫描仪", "quantity": 1, "unit_price": "50000", "line_amount": "50000"}],
                attachment_files="客户PO.pdf",
            )
        ],
    )
    session.commit()

    def fake_download(attachment):
        return (
            "采购订单\n收货人：赵物流\n电话：18612345678\n"
            "收货地址：广东省深圳市南山区科技园测试路 8 号\n"
            "授权签字人：张三\n采购明细：空间扫描仪 数量 1 单价 ¥50,000.00 明细总价 ¥50,000.00",
            {"status": "Parsed", "text_length": 120},
        )

    monkeypatch.setattr(crm_attachment_extraction, "download_attachment_text", fake_download)

    assert result["queued_events"] == 1
    run_pending_jobs(session)

    attachment = session.query(OrderAttachment).one()
    evidence = loads(attachment.evidence_json, {})
    assert attachment.parse_status == "Parsed"
    assert "空间扫描仪" in evidence["parsed_text"]
    assert session.query(MiddlePlatformOrder).one().status == OrderStatus.DELIVERY_NOTICE_READY.value


def test_attachment_product_consistency_blocks_and_notifies_sales_owner(monkeypatch):
    session = make_session()
    seed_active_sku(session)
    seed_inventory(session, quantity=100)
    result = upsert_crm_sales_orders(
        session,
        [
            valid_crm_order_row(
                crm_order_id="crm_obj_attachment_product_mismatch",
                crm_order_no="SO-ATTACHMENT-PRODUCT-MISMATCH",
                sales_user_email="owner@example.com",
                order_amount="50000.00",
                product_amount="50000.00",
                received_amount="0.00",
                receivable_amount="50000.00",
                order_items=[{"sku_code": "SKU-3D-SCANNER-PRO", "product_name": "空间扫描仪", "quantity": 1, "unit_price": "50000", "line_amount": "50000"}],
                attachment_files="客户PO.pdf",
            )
        ],
    )
    session.commit()

    def fake_download(attachment):
        return "采购订单\n授权签字人：张三\n采购明细：便携式打印机 数量 1 单价 ¥40,000.00 明细总价 ¥40,000.00", {"status": "Parsed", "text_length": 60}

    monkeypatch.setattr(crm_attachment_extraction, "download_attachment_text", fake_download)

    assert result["queued_events"] == 1
    run_pending_jobs(session)

    order = session.query(MiddlePlatformOrder).one()
    assert order.status == OrderStatus.VALIDATION_BLOCKED.value
    detail = loads(session.query(ExceptionCase).one().detail, {})
    failed_codes = [rule["rule_code"] for rule in detail["validation"]["failed_rules"]]
    assert "ATTACHMENT_PRODUCT_CONSISTENCY" in failed_codes
    mail = session.query(OutboundMailJob).filter_by(mail_type="V2ValidationFailed").one()
    assert loads(mail.to_json, []) == ["owner@example.com"]
    assert loads(mail.cc_json, []) == []
    assert "CRM 订单产品与附件解析内容不一致" in mail.body
    assert "| 商品 | 对比项 | CRM | 附件 | 结论 |" in mail.body
    assert "| 空间扫描仪 | 商品名称/关键词 | - | 未匹配 | 附件未匹配到 CRM 商品关键词 |" in mail.body


def test_attachment_product_consistency_reads_po_table_amounts(monkeypatch):
    session = make_session()
    seed_active_sku(session)
    seed_inventory(session, quantity=100)
    result = upsert_crm_sales_orders(
        session,
        [
            valid_crm_order_row(
                crm_order_id="crm_obj_po_table_amounts",
                crm_order_no="SO-PO-TABLE-AMOUNTS",
                sales_user_email="owner@example.com",
                order_amount="50000.00",
                product_amount="50000.00",
                received_amount="0.00",
                receivable_amount="50000.00",
                order_items=[{"sku_code": "SKU-3D-SCANNER-PRO", "product_name": "硬件设备—空间扫描仪", "quantity": 1, "unit_price": "50000", "line_amount": "50000"}],
                attachment_files="客户PO.png",
            )
        ],
    )
    session.commit()

    def fake_download(attachment):
        return (
            "采购订单\n"
            "序号 产品名称 规格型号 数量 单位 单价（未含税） 总金额（含税） 交期\n"
            "1 三维扫描仪 Whale 基础款 Whale 基础款 1 台 ¥44,247.79 ¥50,000.00 乙方收到甲方全额货款后发货。\n"
            "订单总金额（含税）：人民币 伍万元整（大写） ¥50,000.00（小写）\n"
            "授权签字人：测试"
        ), {"status": "Parsed", "text_length": 180}

    monkeypatch.setattr(crm_attachment_extraction, "download_attachment_text", fake_download)

    assert result["queued_events"] == 1
    run_pending_jobs(session)

    detail = loads(session.query(ExceptionCase).one().detail, {})
    failed = next(rule for rule in detail["validation"]["failed_rules"] if rule["rule_code"] == "ATTACHMENT_PRODUCT_CONSISTENCY")
    assert "附件未出现可匹配的商品名称/关键词" in failed["reason"]
    assert "附件未识别到可比对的数量" not in failed["reason"]
    assert "附件未识别到可比对的单价" not in failed["reason"]
    assert "附件未识别到可比对的明细总价" not in failed["reason"]
    assert "附件单价与 CRM 不一致" not in failed["reason"]
    mail = session.query(OutboundMailJob).filter_by(mail_type="V2ValidationFailed").one()
    assert "| 硬件设备—空间扫描仪 | 商品名称/关键词 | - | 未匹配 | 附件未匹配到 CRM 商品关键词 |" in mail.body


def test_attachment_product_consistency_reads_pipe_po_table_without_unit_column():
    from backend.app.services.rules.attachment_product_consistency import _extract_po_table_values

    values = _extract_po_table_values(
        "采购订单\n"
        "序号 | 品名 | 品牌 | 规格型号 | 数量 | 单价（含税） | 总金额（含税）\n"
        "1 | 三维扫描仪 | 积木易搭 | Moose 升级款 | 1 | ¥ 5999 | ¥ 5999\n"
        "订单总金额（含税） | 人民币伍仟玖佰玖拾玖圆整（大写）¥ 5999.00（小写）\n"
    )

    assert [str(value.normalize()) for value in values["quantity"]] == ["1"]
    assert [str(value.normalize()) for value in values["unit_price"]] == ["5999"]
    assert "5999" in [str(value.normalize()) for value in values["line_amount"]]


def test_attachment_product_consistency_uses_order_total_when_table_ocr_degrades(monkeypatch):
    session = make_session()
    seed_active_sku(session)
    seed_inventory(session, quantity=100)
    result = upsert_crm_sales_orders(
        session,
        [
            valid_crm_order_row(
                crm_order_id="crm_obj_po_ocr_degraded",
                crm_order_no="SO-PO-OCR-DEGRADED",
                sales_user_email="owner@example.com",
                order_amount="50000.00",
                product_amount="50000.00",
                received_amount="0.00",
                receivable_amount="50000.00",
                order_items=[{"sku_code": "SKU-3D-SCANNER-PRO", "product_name": "硬件设备—空间扫描仪", "quantity": 1, "unit_price": "50000", "line_amount": "50000"}],
                attachment_files="客户PO.png",
            )
        ],
    )
    session.commit()

    def fake_download(attachment):
        return (
            "采购订单\n"
            "[es | sane | sweronss [meat |e [tio Cex | 总金额《全各\n"
            "js [amar [war [+ [+ | sus [mn |r\n"
            "订单总金额 (AB : | AR 伍万元整 (大写) ¥50,000.00 (小写)\n"
            "授权签字人 : 测试"
        ), {"status": "Parsed", "text_length": 120}

    monkeypatch.setattr(crm_attachment_extraction, "download_attachment_text", fake_download)

    assert result["queued_events"] == 1
    run_pending_jobs(session)

    detail = loads(session.query(ExceptionCase).one().detail, {})
    failed = next(rule for rule in detail["validation"]["failed_rules"] if rule["rule_code"] == "ATTACHMENT_PRODUCT_CONSISTENCY")
    assert "附件未出现可匹配的商品名称/关键词" in failed["reason"]
    assert "附件未识别到可比对的数量" in failed["reason"]
    assert "附件未识别到可比对的单价" not in failed["reason"]
    assert "附件未识别到可比对的明细总价" not in failed["reason"]
    assert "附件单价与 CRM 不一致" not in failed["reason"]


def test_attachment_product_consistency_blocks_when_price_quantity_or_total_mismatch(monkeypatch):
    session = make_session()
    seed_active_sku(session)
    seed_inventory(session, quantity=100)
    result = upsert_crm_sales_orders(
        session,
        [
            valid_crm_order_row(
                crm_order_id="crm_obj_attachment_amount_mismatch",
                crm_order_no="SO-ATTACHMENT-AMOUNT-MISMATCH",
                sales_user_email="owner@example.com",
                order_amount="50000.00",
                product_amount="50000.00",
                received_amount="0.00",
                receivable_amount="50000.00",
                order_items=[{"sku_code": "SKU-3D-SCANNER-PRO", "product_name": "空间扫描仪", "quantity": 1, "unit_price": "50000", "line_amount": "50000"}],
                attachment_files="客户PO.pdf",
            )
        ],
    )
    session.commit()

    def fake_download(attachment):
        return "采购订单\n授权签字人：张三\n采购明细：空间扫描仪 数量 2 单价 ¥25,000.00 明细总价 ¥50,000.00", {"status": "Parsed", "text_length": 62}

    monkeypatch.setattr(crm_attachment_extraction, "download_attachment_text", fake_download)

    assert result["queued_events"] == 1
    run_pending_jobs(session)

    order = session.query(MiddlePlatformOrder).one()
    assert order.status == OrderStatus.VALIDATION_BLOCKED.value
    detail = loads(session.query(ExceptionCase).one().detail, {})
    failed = next(rule for rule in detail["validation"]["failed_rules"] if rule["rule_code"] == "ATTACHMENT_PRODUCT_CONSISTENCY")
    assert "附件数量与 CRM 不一致" in failed["reason"]
    assert "附件单价与 CRM 不一致" in failed["reason"]
    mail = session.query(OutboundMailJob).filter_by(mail_type="V2ValidationFailed").one()
    assert loads(mail.to_json, []) == ["owner@example.com"]


def test_customer_mapping_failure_interrupts_and_notifies_with_evidence_summary():
    session = make_session()
    seed_active_sku(session)
    seed_inventory(session, quantity=100)
    result = upsert_crm_sales_orders(
        session,
        [
            valid_crm_order_row(
                crm_order_id="crm_obj_unknown_customer",
                crm_order_no="SO-UNKNOWN-CUSTOMER",
                customer_name="未映射客户",
                order_items=[{"product_name": "未映射商品", "quantity": 1, "unit_price": "100", "line_amount": "100"}],
            )
        ],
    )
    session.commit()

    assert result["queued_events"] == 1
    run_pending_jobs(session)

    order = session.query(MiddlePlatformOrder).one()
    assert order.status == OrderStatus.VALIDATION_BLOCKED.value
    detail = loads(session.query(ExceptionCase).one().detail, {})
    failed_rules = detail["validation"]["failed_rules"]
    assert any(rule["rule_code"] == "CUSTOMER_MAPPING" for rule in failed_rules)
    assert any(rule["rule_code"] == "KNOWN_ACTIVE_SKU" for rule in failed_rules)
    assert "客户资料/客户主数据映射" in detail["validation"]["missing_materials"]
    assert "商品明细、SKU 主数据或数量" in detail["validation"]["missing_materials"]
    assert any("采购订单.pdf" in item for item in detail["validation"]["evidence_summary"])
    mail = session.query(OutboundMailJob).one()
    assert mail.mail_type == "V2ValidationFailed"
    assert "客户资料/客户主数据映射" in mail.body
    assert "商品明细、SKU 主数据或数量" in mail.body
    assert "附件：采购订单.pdf" in mail.body
    assert "客户映射" in mail.body
    assert "商品/SKU 匹配问题" in mail.body
    assert "当前值：未映射客户" in mail.body
    assert "CUSTOMER_MAPPING" not in mail.body
    assert "KNOWN_ACTIVE_SKU" not in mail.body


def test_validation_exception_queues_and_writes_diagnosis():
    session = make_session()
    seed_active_sku(session)
    seed_inventory(session, quantity=100)
    upsert_crm_sales_orders(session, [valid_crm_order_row(crm_order_id="crm_obj_diag", crm_order_no="SO-DIAG", customer_name="未映射客户")])
    session.commit()
    complete_crm_order_required_fields(session, "crm_obj_diag")

    first = run_pending_jobs(session)
    assert first["completed"] == 1
    case = session.query(ExceptionCase).one()
    diagnosis_job = session.query(ProcessingJob).filter_by(job_type="DIAGNOSE_EXCEPTION").one()
    assert diagnosis_job.status == "Pending"

    second = run_pending_jobs(session)
    assert second["completed"] == 1
    session.refresh(case)
    detail = loads(case.detail, {})
    diagnosis = detail["ai_diagnosis"]
    assert diagnosis["diagnosis_type"] == "RULE_BASED_AI_COMPATIBLE"
    assert diagnosis["suggested_owner"] == "商务/主数据维护人"
    assert any("客户" in item for item in diagnosis["root_causes"])
    assert session.query(ProcessingJob).filter_by(id=diagnosis_job.id).one().status == "Completed"
    run_log = session.query(AgentRunLog).filter_by(related_object_id=case.id).one()
    assert run_log.agent_name == "ExceptionDiagnosisAgent"
    assert run_log.status == "Succeeded"
    assert "CUSTOMER_MAPPING" in (run_log.input_json or "")
    assert "商务/主数据维护人" in (run_log.output_json or "")


def test_exception_diagnosis_uses_llm_json_when_model_ready(monkeypatch):
    import backend.app.services.exception_diagnosis as diagnosis_service

    session = make_session()
    model = session.query(ModelProviderConfig).one()
    model.credential_ref = "config:model_api_key"
    set_config(session, "model_api_key", "runtime-secret", is_secret=True)
    case = ExceptionCase(
        exception_type="OMS_BLOCKED",
        severity="Critical",
        detail=dumps({"exception": {"summary": "OMS 主数据缺失"}, "order": {"order_no": "MP-LLM"}}),
        status="Open",
    )
    session.add(case)
    session.commit()
    seen = {}

    def fake_call_model(_session, _config, **kwargs):
        seen["response_format"] = kwargs.get("response_format")
        return {
            "choices": [
                {
                    "message": {
                        "content": dumps(
                            {
                                "summary": "OMS 下推因主数据缺失阻塞",
                                "root_causes": ["OMS 货主或仓库主数据未维护"],
                                "recommended_actions": ["维护 OMS 主数据后从异常台重放"],
                                "suggested_owner": "IT 运维/物流",
                                "confidence": 0.91,
                            }
                        )
                    }
                }
            ]
        }

    monkeypatch.setattr(diagnosis_service, "call_model", fake_call_model)
    result = diagnosis_service.diagnose_exception_case(session, case.id, actor="tester")
    session.commit()

    assert seen["response_format"] == {"type": "json_object"}
    assert result["diagnosis_type"] == "LLM_JSON"
    assert result["summary"] == "OMS 下推因主数据缺失阻塞"
    run_log = session.query(AgentRunLog).filter_by(related_object_id=case.id).one()
    assert run_log.status == "Succeeded"
    assert "LLM_JSON" in (run_log.output_json or "")


def test_agent_and_model_logs_are_queryable_for_ops():
    session = make_session()
    model = session.query(ModelProviderConfig).one()
    case = ExceptionCase(
        exception_type="OMS_BLOCKED",
        severity="High",
        detail=dumps({"exception": {"summary": "OMS 返回仓库未配置"}}),
        status="Open",
    )
    session.add(case)
    session.flush()
    session.add(
        AgentRunLog(
            agent_name="ExceptionDiagnosisAgent",
            task_type="ExceptionDiagnosis",
            related_object_type="ExceptionCase",
            related_object_id=case.id,
            input_json=dumps({"exception_type": case.exception_type}),
            output_json=dumps({"summary": "仓库主数据缺失"}),
            status="Succeeded",
        )
    )
    session.add(
        ModelCallLog(
            provider_config_id=model.id,
            task_type="ExceptionDiagnosis",
            related_object_type="ExceptionCase",
            related_object_id=case.id,
            input_summary=dumps({"prompt": "diagnose exception"}),
            output_json=dumps({"summary": "仓库主数据缺失"}),
            latency_ms=128,
            status="Succeeded",
        )
    )
    session.commit()

    agent_logs = list_agent_run_logs(agent_name="ExceptionDiagnosisAgent", page=1, page_size=10, session=session)
    model_logs = list_model_call_logs(task_type="ExceptionDiagnosis", page=1, page_size=10, session=session)

    assert agent_logs["total"] == 1
    assert agent_logs["items"][0]["related_object_id"] == case.id
    assert agent_logs["items"][0]["input"]["exception_type"] == "OMS_BLOCKED"
    assert "ExceptionDiagnosisAgent" in agent_logs["agent_name_options"]
    assert model_logs["total"] == 1
    assert model_logs["items"][0]["latency_ms"] == 128
    assert model_logs["items"][0]["output"]["summary"] == "仓库主数据缺失"
    assert "Succeeded" in model_logs["status_options"]


def test_exception_diagnosis_falls_back_when_llm_fails(monkeypatch):
    import backend.app.services.exception_diagnosis as diagnosis_service

    session = make_session()
    model = session.query(ModelProviderConfig).one()
    model.credential_ref = "config:model_api_key"
    set_config(session, "model_api_key", "runtime-secret", is_secret=True)
    case = ExceptionCase(
        exception_type="OMS_BLOCKED",
        severity="Critical",
        detail=dumps({"exception": {"summary": "OMS 主数据缺失"}}),
        status="Open",
    )
    session.add(case)
    session.commit()

    def fake_call_model(*_args, **_kwargs):
        raise RuntimeError("model unavailable")

    monkeypatch.setattr(diagnosis_service, "call_model", fake_call_model)
    result = diagnosis_service.diagnose_exception_case(session, case.id, actor="tester")
    session.commit()

    assert result["diagnosis_type"] == "RULE_BASED_AI_COMPATIBLE"
    assert "LLM 诊断失败" in result["fallback_reason"]
    run_log = session.query(AgentRunLog).filter_by(related_object_id=case.id).one()
    assert run_log.status == "Fallback"
    assert "model unavailable" in (run_log.error_message or "")


def test_exception_context_bff_returns_related_order_and_feedback():
    session = make_session()
    seed_active_sku(session)
    seed_inventory(session, quantity=100)
    upsert_crm_sales_orders(session, [valid_crm_order_row(crm_order_id="crm_obj_ctx", crm_order_no="SO-CTX", customer_name="未映射客户")])
    session.commit()
    run_pending_jobs(session)
    run_pending_jobs(session)
    case = session.query(ExceptionCase).one()

    context = exception_context(case.id, session)

    assert context["exception"]["id"] == case.id
    assert context["middle_order"]["crm_order_no"] == "SO-CTX"
    assert context["crm_order"]["crm_order_no"] == "SO-CTX"
    assert context["crm_snapshots"]
    assert context["order_attachments"]
    assert any(job["job_type"] == "DIAGNOSE_EXCEPTION" for job in context["processing_jobs"])
    assert context["diagnosis"]["diagnosis_type"] == "RULE_BASED_AI_COMPATIBLE"
    assert context["next_actions"]

    request = SimpleNamespace(state=SimpleNamespace(username="manager"))
    result = exception_diagnosis_feedback(case.id, {"feedback": "accepted", "note": "按建议处理"}, request, session)
    feedback = result["feedback"]
    assert feedback["feedback"] == "accepted"
    assert feedback["actor"] == "manager"
    assert loads(session.get(ExceptionCase, case.id).detail)["ai_feedback"][0]["note"] == "按建议处理"
    assert session.query(AuditEvent).filter_by(event_type="ExceptionDiagnosisFeedbackRecorded", related_object_id=case.id).count() == 1


def test_illegal_state_transition_is_rejected():
    session = make_session()
    crm = CrmSalesOrder(
        source_system="fxiaoke",
        crm_order_id="crm_obj_003",
        crm_order_no="SO-003",
        customer_name="测试客户",
        payload_hash="hash-003",
    )
    session.add(crm)
    session.flush()
    order = MiddlePlatformOrder(
        order_no="MP-SO-003",
        source_system="fxiaoke",
        crm_sales_order_id=crm.id,
        crm_order_id=crm.crm_order_id,
        crm_order_no=crm.crm_order_no,
        payload_hash=crm.payload_hash,
        status=OrderStatus.IMPORTED.value,
    )
    session.add(order)
    session.flush()

    with pytest.raises(IllegalStateTransition):
        transition_order(session, order, OrderEvent.OMS_PUSH_SUCCESS)
