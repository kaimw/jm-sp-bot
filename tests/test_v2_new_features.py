from __future__ import annotations
import json
import os
import tempfile
import openpyxl
from decimal import Decimal
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
import pytest

from backend.app.database import Base
from backend.app.models import (
    CrmSalesOrder, MiddlePlatformOrder, ProductSPU, ProductSKU,
    ProductInventorySnapshot, InterEntityTransfer, MailReceiverConfig,
    OutboundMailJob, EntityMapping, CustomerEntityMapping
)
from backend.app.services.jsonutil import dumps, loads
from backend.app.services.bootstrap import seed_defaults, set_config
from backend.app.services.excel_import import import_inventory_excel
from backend.app.services.order_middle_platform import (
    upsert_middle_platform_order, run_validation_chain, handle_crm_snapshot_changed
)
from backend.app.services.mail_template_service import enqueue_delivery_notice_mail

def make_session():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(bind=engine)
    Session = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)
    session = Session()
    seed_defaults(session)
    session.commit()
    return session

def test_excel_import_scoped_deletion():
    session = make_session()
    
    # 1. Create a dummy SPU and SKU
    spu = ProductSPU(spu_id="13001", name="Product 13001")
    session.add(spu)
    session.flush()
    sku = ProductSKU(spu_uuid=spu.id, sku_id="13001", status="Active")
    session.add(sku)
    
    # 2. Seed initial snapshots with required warehouse_name
    session.add(ProductInventorySnapshot(
        material_code="13001", material_name="P1", warehouse_code="美西仓库",
        warehouse_name="美西仓库", qty=50.0, base_qty=50.0, status="Active"
    ))
    session.add(ProductInventorySnapshot(
        material_code="13001", material_name="P1", warehouse_code="英国仓库",
        warehouse_name="英国仓库", qty=80.0, base_qty=80.0, status="Active"
    ))
    session.commit()
    
    # 3. Create dummy Excel with ONLY US warehouse data
    headers = [
        "查询编号\nFNSKU/EAN", "sku", "仓库", "Material Code\n料号代码",
        "Chinese description of goods\n中文品名", "产品系列", "产品系列2",
        "Model\n型号", "库存数量", "库存预警", "在途数量", "待接收及转运数量"
    ]
    row_us = [
        "1111", "13001", "美西仓库", "13001",
        "三维扫描仪", "三维扫描仪", "-", "-", 
        42.0, "normal", 0.0, None
    ]
    
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.append(headers)
    ws.append(row_us)
    
    fd, path = tempfile.mkstemp(suffix=".xlsx")
    os.close(fd)
    try:
        wb.save(path)
        
        # 4. Import the spreadsheet
        res = import_inventory_excel(path, session)
        session.commit()
        
        assert res["ok"] is True
        
        # 5. Verify database snapshot results
        # US warehouse snapshot should be updated to 42.0
        snap_us = session.query(ProductInventorySnapshot).filter_by(warehouse_code="美西仓库").one()
        assert snap_us.qty == 42.0
        
        # UK warehouse snapshot should remain 80.0 (scoped deletion worked)
        snap_uk = session.query(ProductInventorySnapshot).filter_by(warehouse_code="英国仓库").one()
        assert snap_uk.qty == 80.0
        
    finally:
        if os.path.exists(path):
            os.remove(path)


def test_raw_inventory_snapshots_api_is_paginated():
    session = make_session()
    for i in range(25):
        session.add(ProductInventorySnapshot(
            material_code=f"MAT-{i:03d}",
            material_name=f"物料 {i:03d}",
            warehouse_code="WH",
            warehouse_name="WH",
            qty=float(i),
            base_qty=float(i),
            status="Active",
        ))
    session.commit()

    from backend.app.main import list_raw_inventory_snapshots

    page1 = list_raw_inventory_snapshots(page=1, page_size=10, session=session)
    page3 = list_raw_inventory_snapshots(page=3, page_size=10, session=session)

    assert len(page1["items"]) == 10
    assert page1["has_more"] is True
    assert page1["items"][0]["material_code"] == "MAT-000"
    assert len(page3["items"]) == 5
    assert page3["has_more"] is False

def test_q6_erp_change_rollback(monkeypatch):
    session = make_session()
    
    # 1. Use upsert_crm_sales_orders helper to setup the crm order and sub-items correctly
    crm_row = {
        "crm_order_id": "crm_so_q6",
        "crm_order_no": "SO-Q6-001",
        "customer_name": "Q6客户",
        "sales_user_name": "Alice",
        "sales_user_email": "alice@test.com",
        "owner_department": "Logistics",
        "order_date": "2026-06-25",
        "order_amount": "100.00",
        "received_amount": "0.00",
        "receivable_amount": "100.00",
        "receipt_contact": "TestContact",
        "receipt_phone": "18600000000",
        "receipt_address": "Road A, Shenzhen",
        "approval_status": "approved",
        "attachment_files": "盖章采购订单.pdf",
        "items": [{"sku_code": "SKU-3D-SCANNER-PRO", "quantity": 1, "unit_price": "100.00", "line_amount": "100.00"}],
    }
    from backend.app.services.crm_sync import upsert_crm_sales_orders
    upsert_crm_sales_orders(session, [crm_row])
    session.commit()
    
    crm = session.query(CrmSalesOrder).filter_by(crm_order_id="crm_so_q6").one()
    # Explicitly set crm fields as crm_sync only stores raw payload in raw_json
    crm.receipt_contact = "TestContact"
    crm.receipt_phone = "18600000000"
    crm.receipt_address = "Road A, Shenzhen"
    session.commit()
    
    order = upsert_middle_platform_order(session, crm)
    order.status = "ERP_SAVED"
    order.erp_bill_no = "ERP-SO-12345"
    session.commit()
    
    # 2. Mock KingdeeClient functions
    from backend.app.services.erp.kingdee_client import KingdeeClient
    query_calls = []
    unaudit_calls = []
    cancel_calls = []
    
    def mock_execute_bill_query(self, form_id, field_keys, filter_string, limit=1):
        query_calls.append((form_id, filter_string))
        return {
            "ok": True,
            "raw": [
                [10001, "ERP-SO-12345"]
            ]
        }
        
    def mock_un_audit_bill(self, form_id, bill_ids):
        unaudit_calls.append((form_id, bill_ids))
        return {"ok": True, "message": "Success"}
        
    def mock_cancel_bill(self, form_id, bill_ids):
        cancel_calls.append((form_id, bill_ids))
        return {"ok": True, "message": "Success"}
        
    monkeypatch.setattr(KingdeeClient, "execute_bill_query", mock_execute_bill_query)
    monkeypatch.setattr(KingdeeClient, "un_audit_bill", mock_un_audit_bill)
    monkeypatch.setattr(KingdeeClient, "cancel_bill", mock_cancel_bill)
    
    # Set correct Kingdee config keys in SystemConfig
    set_config(session, "erp_server_url", "http://fake-erp.com")
    set_config(session, "erp_acct_id", "fake-db")
    set_config(session, "erp_username", "fake-user")
    set_config(session, "erp_app_id", "fake-app")
    set_config(session, "erp_app_sec", "fake-secret")
    set_config(session, "erp_lcid", "2052")
    session.commit()
    
    # 3. Simulate CRM change when status is ERP_SAVED
    res = handle_crm_snapshot_changed(
        session,
        order,
        crm,
        new_payload_hash="new_hash",
        trace_id="test_trace",
    )
    
    session.commit()
    
    assert res.get("continue_processing") is True
    assert res.get("q6_erp_reverted") is True
    
    # Order should revert to IMPORTED
    assert order.status == "IMPORTED"
    
    # Kingdee Client should have queried and cancelled the bill
    assert len(query_calls) == 1
    assert query_calls[0] == ("SAL_SaleOrder", "FBillNo = 'ERP-SO-12345'")
    assert len(unaudit_calls) == 1
    assert unaudit_calls[0] == ("SAL_SaleOrder", [10001])
    assert len(cancel_calls) == 1
    assert cancel_calls[0] == ("SAL_SaleOrder", [10001])

def test_inventory_three_step_rule_inter_entity_transfer():
    session = make_session()
    
    # 1. Create SPU and SKU
    spu = ProductSPU(spu_id="SPU-SCAN", name="Scanner SPU")
    session.add(spu)
    session.flush()
    sku = ProductSKU(spu_uuid=spu.id, sku_id="SKU-SCAN-01", status="Active")
    session.add(sku)
    
    # 2. Setup Entity Warehouse Mappings
    session.query(EntityMapping).delete()
    session.flush()
    session.add(EntityMapping(entity_code="SZ", entity_name="Shenzhen", erp_org_id="ORG_SZ", warehouses_json=dumps([{"warehouse_code": "WH-SZ-01"}]), is_active=True))
    session.add(EntityMapping(entity_code="HK", entity_name="Hongkong", erp_org_id="ORG_HK", warehouses_json=dumps([{"warehouse_code": "WH-HK-01"}]), is_active=True))
    
    # 3. Seed inventory snapshots:
    session.add(ProductInventorySnapshot(
        material_code="SKU-SCAN-01", material_name="Scanner", warehouse_code="WH-SZ-01",
        warehouse_name="深圳仓", qty=0.0, base_qty=0.0, status="Active"
    ))
    session.add(ProductInventorySnapshot(
        material_code="SKU-SCAN-01", material_name="Scanner", warehouse_code="WH-HK-01",
        warehouse_name="香港仓", qty=10.0, base_qty=10.0, status="Active"
    ))
    session.commit()
    
    # 4. Create a CRM order for entity SZ using the helper to set up items
    crm_row = {
        "crm_order_id": "crm_so_three_step",
        "crm_order_no": "SO-STEP-001",
        "customer_name": "Step客户",
        "sales_user_name": "Alice",
        "sales_user_email": "alice@test.com",
        "owner_department": "Logistics",
        "order_date": "2026-06-25",
        "order_amount": "100.00",
        "received_amount": "0.00",
        "receivable_amount": "100.00",
        "receipt_contact": "TestContact",
        "receipt_phone": "18600000000",
        "receipt_address": "Road A, Shenzhen",
        "approval_status": "approved",
        "attachment_files": "盖章采购订单.pdf",
        "items": [{"sku_code": "SKU-SCAN-01", "quantity": 2, "unit_price": "50.00", "line_amount": "100.00"}],
    }
    from backend.app.services.crm_sync import upsert_crm_sales_orders
    upsert_crm_sales_orders(session, [crm_row])
    session.commit()
    
    crm = session.query(CrmSalesOrder).filter_by(crm_order_id="crm_so_three_step").one()
    crm.receipt_contact = "TestContact"
    crm.receipt_phone = "18600000000"
    crm.receipt_address = "Road A, Shenzhen"
    session.commit()
    
    order = upsert_middle_platform_order(session, crm)
    order.entity_code = "SZ"
    session.commit()
    
    # 5. Run validation chain
    results = run_validation_chain(session, order)
    
    step_result = next(r for r in results if r.rule_code == "INVENTORY_THREE_STEP")
    assert step_result.passed is True
    assert step_result.blocker_level.name == "LOW"
    assert "Step2 调货" in step_result.reason
    
    # 6. Verify InterEntityTransfer record in DB after explicit flush (since autoflush=False)
    session.flush()
    transfers = session.query(InterEntityTransfer).all()
    assert len(transfers) == 1
    transfer = transfers[0]
    assert transfer.source_entity == "SZ"
    assert transfer.target_entity == "HK"
    assert transfer.status == "Draft"
    assert "SKU-SCAN-01" in transfer.material_json

def test_enqueue_delivery_notice_mail_rendering():
    session = make_session()
    
    # 1. Create a dummy order
    crm_row = {
        "crm_order_id": "crm_so_mail",
        "crm_order_no": "SO-MAIL-01",
        "customer_name": "Mail客户",
        "sales_user_name": "Alice",
        "sales_user_email": "alice@test.com",
        "owner_department": "Logistics",
        "order_date": "2026-06-25",
        "order_amount": "100.00",
        "received_amount": "0.00",
        "receivable_amount": "100.00",
        "receipt_contact": "张三",
        "receipt_phone": "18600001111",
        "receipt_address": "湖北省武汉市东湖区1号",
        "approval_status": "approved",
        "attachment_files": "盖章采购订单.pdf",
        "items": [{"sku_code": "SKU-3D-SCANNER-PRO", "quantity": 1, "unit_price": "100.00", "line_amount": "100.00"}],
    }
    from backend.app.services.crm_sync import upsert_crm_sales_orders
    upsert_crm_sales_orders(session, [crm_row])
    session.commit()
    
    crm = session.query(CrmSalesOrder).filter_by(crm_order_id="crm_so_mail").one()
    crm.receipt_contact = "张三"
    crm.receipt_phone = "18600001111"
    crm.receipt_address = "湖北省武汉市东湖区1号"
    session.commit()
    
    order = upsert_middle_platform_order(session, crm)
    order.order_type = "SALES_ORDER"
    order.erp_bill_no = "ERP-SO-999"
    session.commit()
    
    # 2. Seed Mail Receiver Configs
    session.add(MailReceiverConfig(
        scene="domestic_delivery",
        to_json=dumps(["warehouse_manager@test.com"]),
        cc_json=dumps(["cc_sales@test.com"]),
        is_active=True
    ))
    session.commit()
    
    # 3. Call template service to queue mail
    job = enqueue_delivery_notice_mail(
        session,
        order,
        list(order.items),
        warehouse="武汉工厂仓"
    )
    session.commit()
    
    assert job is not None
    assert job.mail_type == "sales_delivery"
    assert "ERP-SO-999" in job.subject
    assert "张三" in job.body
    assert "18600001111" in job.body
    assert "warehouse_manager@test.com" in job.to_json
    assert "cc_sales@test.com" in job.cc_json
