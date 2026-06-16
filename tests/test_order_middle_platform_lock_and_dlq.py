from __future__ import annotations

import hashlib
import logging
from unittest.mock import MagicMock, patch
import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from backend.app.database import Base
from backend.app.models import CrmSalesOrder, ExceptionCase, MiddlePlatformOrder, DeliveryNotice, ProcessingJob, AuditEvent
from backend.app.services.bootstrap import seed_defaults, set_config
from backend.app.services.jobs import run_pending_jobs, run_platform_sync_async
from backend.app.services.crm_sync import upsert_crm_sales_orders
from backend.app.services.order_middle_platform import (
    process_oms_waybill_print,
    handle_platform_fulfillment_sync_failure,
    OrderStatus
)
from backend.app.services.jsonutil import dumps, loads
from backend.app.main import apply_exception_address_correction


def make_test_session():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(bind=engine)
    Session = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)
    session = Session()
    seed_defaults(session)
    session.commit()
    return session


def seed_active_sku(session, sku_id: str = "SKU-3D-SCANNER-PRO") -> None:
    from backend.app.models import ProductSPU, ProductSKU
    spu = ProductSPU(spu_id="SPU-3D-SCANNER", name="3D Scanner")
    session.add(spu)
    session.flush()
    session.add(ProductSKU(spu_uuid=spu.id, sku_id=sku_id, status="Active"))
    session.commit()


def seed_inventory(session, sku_id: str = "SKU-3D-SCANNER-PRO", quantity: int = 100) -> None:
    from backend.app.models import ProductInventorySnapshot
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


def test_periodic_crm_sync_job_runs_even_when_same_payload_completed(monkeypatch):
    import backend.app.services.jobs as jobs_service

    session = make_test_session()
    payload = dumps({"source": "auto"})
    session.add(ProcessingJob(job_type="sync_crm_sales_orders", payload_json=payload, status="Completed"))
    pending = ProcessingJob(job_type="sync_crm_sales_orders", payload_json=payload, status="Pending")
    session.add(pending)
    session.commit()
    calls = []

    def fake_sync(_session, trigger="job"):
        calls.append(trigger)
        return {"total": 0}

    monkeypatch.setattr(jobs_service, "run_crm_sales_order_sync", fake_sync)

    result = run_pending_jobs(session)

    assert result["completed"] == 1
    assert calls == ["auto"]
    assert session.get(ProcessingJob, pending.id).error_message is None


def test_periodic_oms_status_poll_job_runs_even_when_same_payload_completed(monkeypatch):
    import backend.app.services.jobs as jobs_service

    session = make_test_session()
    payload = dumps({"limit": 50, "source": "scheduled"})
    session.add(ProcessingJob(job_type="OMS_STATUS_POLL", payload_json=payload, status="Completed"))
    pending = ProcessingJob(job_type="OMS_STATUS_POLL", payload_json=payload, status="Pending")
    session.add(pending)
    session.commit()
    calls = []

    def fake_poll(_session, limit=50):
        calls.append(limit)
        return {"checked": 0}

    monkeypatch.setattr(jobs_service, "poll_oms_status_updates", fake_poll)

    result = run_pending_jobs(session)

    assert result["completed"] == 1
    assert calls == [50]
    assert session.get(ProcessingJob, pending.id).error_message is None


def valid_crm_order_row(**overrides):
    row = {
        "crm_order_id": "crm_addr_01",
        "crm_order_no": "SO-ADDR-01",
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
        "receipt_address": "北京市",
        "delivery_date": "2026-06-30",
        "attachment_files": "采购订单.pdf; 合同盖章版.pdf",
        "items": [{"sku_code": "SKU-3D-SCANNER-PRO", "quantity": 50, "unit_price": "2500", "line_amount": "125000"}],
    }
    row.update(overrides)
    return row


def test_process_oms_waybill_print_postgres_lock_success():
    session = make_test_session()
    
    # Setup test order and notice
    crm_order = CrmSalesOrder(
        crm_order_id="crm_lock_01",
        crm_order_no="SO-LOCK-01",
        payload_hash="abc",
        receipt_address="湖北省武汉市东湖高新区",
        raw_json="{}"
    )
    session.add(crm_order)
    session.flush()

    order = MiddlePlatformOrder(
        order_no="OMP-LOCK-01",
        source_system="fxiaoke",
        crm_sales_order_id=crm_order.id,
        crm_order_id=crm_order.crm_order_id,
        crm_order_no=crm_order.crm_order_no,
        payload_hash="abc",
        status=OrderStatus.PICKING.value
    )
    session.add(order)
    session.flush()

    notice = DeliveryNotice(
        notice_no="DN-LOCK-01",
        order_id=order.id,
        oms_idempotency_key="ik-01",
        status="Confirmed"
    )
    session.add(notice)
    session.commit()

    # Mock postgres dialect and lock acquisition success
    mock_result = MagicMock()
    mock_result.scalar.return_value = True

    orig_execute = session.execute
    def mock_execute(statement, *args, **kwargs):
        if "pg_try_advisory_xact_lock" in str(statement):
            return mock_result
        return orig_execute(statement, *args, **kwargs)

    # Patch session.bind.dialect.name using patch.object
    with patch.object(session.bind.dialect, "name", "postgresql"), \
         patch.object(session, "execute", side_effect=mock_execute) as mock_exec:
        
        # Calling process_oms_waybill_print.
        # We mock print_oms_waybill to avoid calling real API
        with patch("backend.app.services.order_middle_platform.print_oms_waybill") as mock_print:
            mock_print.return_value = {
                "ok": True,
                "data": {"waybillNo": "WB-LOCK-123", "printData": "Base64PDF"}
            }
            
            res = process_oms_waybill_print(session, {"notice_id": notice.id})
            
            # Assert execute was called for try lock
            mock_exec.assert_called()
            assert res["waybill_no"] == "WB-LOCK-123"


def test_process_oms_waybill_print_postgres_lock_failure():
    session = make_test_session()
    
    # Setup test order and notice
    crm_order = CrmSalesOrder(
        crm_order_id="crm_lock_02",
        crm_order_no="SO-LOCK-02",
        payload_hash="abc",
        receipt_address="湖北省武汉市东湖高新区",
        raw_json="{}"
    )
    session.add(crm_order)
    session.flush()

    order = MiddlePlatformOrder(
        order_no="OMP-LOCK-02",
        source_system="fxiaoke",
        crm_sales_order_id=crm_order.id,
        crm_order_id=crm_order.crm_order_id,
        crm_order_no=crm_order.crm_order_no,
        payload_hash="abc",
        status=OrderStatus.PICKING.value
    )
    session.add(order)
    session.flush()

    notice = DeliveryNotice(
        notice_no="DN-LOCK-02",
        order_id=order.id,
        oms_idempotency_key="ik-02",
        status="Confirmed"
    )
    session.add(notice)
    session.commit()

    # Mock postgres dialect and lock acquisition failure
    mock_result = MagicMock()
    mock_result.scalar.return_value = False

    orig_execute = session.execute
    def mock_execute(statement, *args, **kwargs):
        if "pg_try_advisory_xact_lock" in str(statement):
            return mock_result
        return orig_execute(statement, *args, **kwargs)

    # Patch session.bind.dialect.name using patch.object
    with patch.object(session.bind.dialect, "name", "postgresql"), \
         patch.object(session, "execute", side_effect=mock_execute), \
         pytest.raises(RuntimeError, match="Could not acquire advisory lock"):
        
        process_oms_waybill_print(session, {"notice_id": notice.id})


def test_async_platform_sync_decoupling():
    session = make_test_session()
    
    # Create a PLATFORM_FULFILLMENT_SYNC job
    job = ProcessingJob(
        job_type="PLATFORM_FULFILLMENT_SYNC",
        payload_json=dumps({"notice_id": "dummy", "order_id": "dummy"}),
        status="Pending"
    )
    session.add(job)
    session.commit()

    # Verify that run_pending_jobs spawns a thread and returns immediately
    with patch("threading.Thread") as mock_thread:
        res = run_pending_jobs(session)
        assert mock_thread.called
        # The job isn't completed synchronously because the thread executes it
        assert res["completed"] == 0


def test_platform_fulfillment_dlq_alert():
    session = make_test_session()
    
    crm_order = CrmSalesOrder(
        crm_order_id="crm_dlq_01",
        crm_order_no="SO-DLQ-01",
        payload_hash="abc",
        receipt_address="湖北省武汉市东湖高新区",
        raw_json="{}"
    )
    session.add(crm_order)
    session.flush()

    order = MiddlePlatformOrder(
        order_no="OMP-DLQ-01",
        source_system="fxiaoke",
        crm_sales_order_id=crm_order.id,
        crm_order_id=crm_order.crm_order_id,
        crm_order_no=crm_order.crm_order_no,
        payload_hash="abc",
        status=OrderStatus.PICKING.value
    )
    session.add(order)
    session.flush()

    notice = DeliveryNotice(
        notice_no="DN-DLQ-01",
        order_id=order.id,
        oms_idempotency_key="ik-dlq",
        status="Confirmed",
        platform_fulfillment_retry_count=5  # Exceeding retry count
    )
    session.add(notice)
    session.commit()

    # Set retry limits
    set_config(session, "platform_fulfillment_sync_max_retries", "3")
    session.commit()

    # Verify logging of [DLQ_ALERT]
    with patch("logging.getLogger") as mock_get_logger:
        mock_logger = MagicMock()
        mock_get_logger.return_value = mock_logger
        
        handle_platform_fulfillment_sync_failure(session, notice, order, "Platform API down")
        
        # Assert logger.error was called with [DLQ_ALERT]
        mock_logger.error.assert_called()
        call_args = mock_logger.error.call_args[0][0]
        assert "[DLQ_ALERT]" in call_args


def test_apply_address_correction_success():
    session = make_test_session()
    
    # 1. Setup Active SKU, stock & required config
    seed_active_sku(session)
    seed_inventory(session, quantity=100)
    seed_oms_required_config(session)
    
    # 2. Setup a CRM order and middle order blocked by validation due to coarse address
    row = valid_crm_order_row(receipt_address="北京市")
    result = upsert_crm_sales_orders(session, [row])
    session.commit()
    assert result["queued_events"] == 1
    
    # Run parsing & validation chain, which will fail because "北京市" is not a detailed address
    run_pending_jobs(session)
    
    # Verify order is VALIDATION_BLOCKED
    order = session.query(MiddlePlatformOrder).filter_by(crm_order_no="SO-ADDR-01").one()
    crm_order = order.crm_order
    assert order.status == OrderStatus.VALIDATION_BLOCKED.value
    
    # Get created exception case
    case = session.query(ExceptionCase).filter_by(exception_type="VALIDATION_BLOCKED").one()
    
    # Inject AI diagnosis address_correction
    correction = {
        "receipt_address": "湖北省武汉市东湖高新区光谷一路 100 号",
        "receipt_contact": "李四",
        "receipt_phone": "18611112222",
        "reason": "AI 地址智能修正建议"
    }
    detail = loads(case.detail, {})
    detail["ai_diagnosis"] = {
        "address_correction": correction
    }
    case.detail = dumps(detail)
    session.commit()

    # Run address correction endpoint logic
    res = apply_exception_address_correction(case.id, session=session)
    
    assert res["success"] is True
    assert res["order_status"] in {OrderStatus.DELIVERY_NOTICE_READY.value, OrderStatus.OMS_ACCEPTED.value}
    
    # Verify DB state is updated
    session.refresh(crm_order)
    assert crm_order.receipt_address == "湖北省武汉市东湖高新区光谷一路 100 号"
    assert crm_order.receipt_contact == "李四"
    assert crm_order.receipt_phone == "18611112222"
    
    # Verify hash has changed
    assert order.payload_hash == crm_order.payload_hash
    
    # Verify ExceptionCase resolved
    session.refresh(case)
    assert case.status == "Resolved"
