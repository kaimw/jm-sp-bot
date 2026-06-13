from __future__ import annotations

import shutil
from types import SimpleNamespace

import pytest
from fastapi import HTTPException
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from backend.app.database import Base
from backend.app.models import AgentRunLog, AuditEvent, ChannelPricing, CrmOrderSnapshot, CrmSalesOrder, ExceptionCase, IntegrationEvent, MiddlePlatformOrder, ModelCallLog, ModelProviderConfig, OrderAttachment, OutboundMailJob, ProcessingJob, ProductInventorySnapshot, ProductSKU, ProductSPU
from backend.app.services.attachment_parser import parse_attachment
from backend.app.services.bootstrap import seed_defaults, set_config
from backend.app.services.crm_sync import retry_crm_order_detail_sync, upsert_crm_sales_orders
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
    transition_order,
)
from backend.app.services.jsonutil import dumps, loads
from backend.app.main import exception_context, exception_diagnosis_feedback, list_agent_run_logs, list_model_call_logs, replay_v2_delivery_notice, serialize_crm_order, serialize_crm_order_with_flow, update_crm_config
from backend.app.schemas import CrmRuntimeConfigUpdate


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
            warehouse_code="A1",
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


def valid_crm_order_row(**overrides):
    row = {
        "crm_order_id": "crm_obj_001",
        "crm_order_no": "SO-001",
        "customer_name": "亚马逊北美渠道",
        "sales_user_name": "Alice",
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
        "items": [{"sku_code": "SKU-3D-SCANNER-PRO", "quantity": 50, "unit_price": "2500", "line_amount": "125000"}],
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
    run_pending_jobs(session)
    return session.query(MiddlePlatformOrder).one()


def create_delivery_ready_order_without_oms_config(session):
    seed_active_sku(session)
    seed_inventory(session, quantity=100)
    result = upsert_crm_sales_orders(session, [valid_crm_order_row()])
    session.commit()
    assert result["queued_events"] == 1
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
        items=[
            {"sku_code": "SKU-3D-SCANNER-PRO", "quantity": 1, "unit_price": "100.00", "line_amount": "100.00"},
            {"sku_code": "SKU-3D-SCANNER-LITE", "quantity": 1, "unit_price": "300.00", "line_amount": "300.00"},
        ],
    )
    result = upsert_crm_sales_orders(session, [row])
    session.commit()
    assert result["queued_events"] == 1

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
        items=[{"shop_sku_code": "AMZ-SCANNER-PRO", "quantity": 50, "unit_price": "2500", "line_amount": "125000"}],
    )
    result = upsert_crm_sales_orders(session, [row])
    session.commit()
    assert result["queued_events"] == 1

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
        items=[{"shop_sku_code": "UNKNOWN-AMZ-SKU", "quantity": 1, "unit_price": "100", "line_amount": "100"}],
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


def test_crm_sync_merges_order_detail_fields_and_downloadable_attachments():
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
    assert crm.receipt_contact == "李四"
    assert crm.receipt_phone == "18600002222"
    assert crm.receipt_address == "深圳市南山区测试路 2 号"
    assert crm.delivery_date == "2026-07-01"
    assert attachment.file_url == "https://example.test/po.pdf"
    assert detail["crm_detail_status"] == "detail_available"
    assert any(item["has_download"] for item in detail["attachments"])


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
    assert crm.receipt_contact == "王五"
    assert crm.receipt_phone == "18600003333"
    assert detail["crm_detail_status"] == "detail_available"
    assert any(item["file_name"] == "盖章合同.pdf" and item["has_download"] for item in detail["attachments"])
    assert session.query(AuditEvent).filter_by(event_type="CrmOrderDetailRetrySucceeded", related_object_id=crm.id).count() == 1


def test_crm_attachment_extraction_fills_oms_receiver_fields_without_confusing_contract_signer():
    session = make_session()
    crm = CrmSalesOrder(
        crm_order_id="crm_obj_extract",
        crm_order_no="SO-EXTRACT",
        customer_name="附件客户",
        sales_user_name="Alice",
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


def test_crm_attachment_extraction_marks_manual_review_when_llm_still_invalid(monkeypatch):
    session = make_session()
    crm = CrmSalesOrder(crm_order_id="crm_obj_manual", crm_order_no="SO-MANUAL", payload_hash="hash-manual", raw_json=dumps({}))
    session.add(crm)
    session.flush()

    monkeypatch.setattr(crm_attachment_extraction, "llm_fallback_enabled", lambda session: True)
    monkeypatch.setattr(crm_attachment_extraction, "active_model_config", lambda session: SimpleNamespace(id="model"))
    monkeypatch.setattr(crm_attachment_extraction, "model_ready", lambda session, config: True)
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


def test_crm_order_detail_dedupes_current_payload_attachments():
    session = make_session()
    crm = CrmSalesOrder(
        crm_order_id="crm_obj_dedupe",
        crm_order_no="SO-DEDUPE",
        customer_name="去重客户",
        sales_user_name="Alice",
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


def test_image_ocr_text_can_feed_llm_receiver_extraction():
    if not shutil.which("tesseract"):
        pytest.skip("tesseract not installed")
    from PIL import Image, ImageDraw, ImageFont
    import io

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

    parsed = parse_attachment("scan.png", buffer.getvalue(), max_zip_bytes=1024 * 1024, max_depth=1)

    assert parsed.status == "Parsed"
    assert "18612345678" in parsed.text


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


def test_crm_phase1_scope_config_can_be_updated_by_ops():
    session = make_session()
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

    with pytest.raises(HTTPException) as exc:
        update_crm_config(CrmRuntimeConfigUpdate(v2_crm_phase1_scope_json="[]"), session=session)

    assert exc.value.status_code == 400
    assert "一期纳入范围配置" in exc.value.detail


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
        attachment_files_json=dumps(["采购订单.pdf"]),
        payload_hash="hash-002",
        raw_json=dumps({"items": [{"sku_code": "UNKNOWN-SKU", "quantity": 1}]}),
    )
    session.add(crm)
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


def test_inventory_rule_blocks_when_available_quantity_is_short():
    session = make_session()
    seed_active_sku(session)
    session.add(
        ProductInventorySnapshot(
            material_code="SKU-3D-SCANNER-PRO",
            material_name="3D Scanner",
            warehouse_code="A1",
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
                "attachment_files": "采购订单.pdf",
                "items": [{"sku_code": "SKU-3D-SCANNER-PRO", "quantity": 2, "unit_price": "100", "line_amount": "200"}],
            }
        ],
    )
    session.commit()

    assert result["queued_events"] == 1
    run_pending_jobs(session)

    order = session.query(MiddlePlatformOrder).one()
    assert order.status == OrderStatus.VALIDATION_BLOCKED.value
    detail = loads(session.query(ExceptionCase).one().detail, {})
    assert detail["validation"]["failed_rules"][0]["rule_code"] == "LOCAL_INVENTORY_AVAILABLE"


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
    assert "缺少或需修正的基础资料：" in mail.body
    assert "证据来源：" in mail.body


def test_customer_mapping_failure_interrupts_and_notifies_with_evidence_summary():
    session = make_session()
    seed_active_sku(session)
    seed_inventory(session, quantity=100)
    result = upsert_crm_sales_orders(session, [valid_crm_order_row(crm_order_id="crm_obj_unknown_customer", crm_order_no="SO-UNKNOWN-CUSTOMER", customer_name="未映射客户")])
    session.commit()

    assert result["queued_events"] == 1
    run_pending_jobs(session)

    order = session.query(MiddlePlatformOrder).one()
    assert order.status == OrderStatus.VALIDATION_BLOCKED.value
    detail = loads(session.query(ExceptionCase).one().detail, {})
    failed_rules = detail["validation"]["failed_rules"]
    assert any(rule["rule_code"] == "CUSTOMER_MAPPING" for rule in failed_rules)
    assert "客户资料/客户主数据映射" in detail["validation"]["missing_materials"]
    assert any("采购订单.pdf" in item for item in detail["validation"]["evidence_summary"])
    mail = session.query(OutboundMailJob).one()
    assert mail.mail_type == "V2ValidationFailed"
    assert "客户资料/客户主数据映射" in mail.body
    assert "附件：采购订单.pdf" in mail.body
    assert "CUSTOMER_MAPPING" in mail.body


def test_validation_exception_queues_and_writes_diagnosis():
    session = make_session()
    seed_active_sku(session)
    seed_inventory(session, quantity=100)
    upsert_crm_sales_orders(session, [valid_crm_order_row(crm_order_id="crm_obj_diag", crm_order_no="SO-DIAG", customer_name="未映射客户")])
    session.commit()

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
