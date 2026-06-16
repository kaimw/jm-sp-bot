from __future__ import annotations

import pytest
import hashlib
from fastapi import HTTPException
from fastapi.responses import RedirectResponse
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from backend.app.database import Base
from backend.app.models import User, CrmSalesOrder, OrderAttachment, MiddlePlatformOrder, ExceptionCase
from backend.app.services.bootstrap import seed_defaults, set_config
from backend.app.services.auth import hash_password, verify_password, should_mask_financials
from backend.app.services.crm_sync import upsert_crm_sales_orders
from backend.app.services.jobs import run_pending_jobs
from backend.app.services.jsonutil import dumps, loads
from backend.app.main import (
    require_role,
    serialize_crm_order,
    serialize_order_attachment,
    download_crm_order_attachment,
    apply_exception_address_correction,
)
from backend.app.services.order_middle_platform import (
    serialize_middle_order,
    list_middle_orders,
)


def seed_active_sku(session, sku_id: str = "SKU-3D-SCANNER-PRO") -> None:
    from backend.app.models import ProductSPU, ProductSKU
    spu = ProductSPU(spu_id="SPU-3D-SCANNER", name="3D Scanner")
    session.add(spu)
    session.flush()
    session.add(ProductSKU(spu_uuid=spu.id, sku_id=sku_id, status="Active"))
    session.commit()


def seed_inventory(session, sku_id: str = "SKU-3D-SCANNER-PRO", quantity: int = 100) -> None:
    from backend.app.models import ProductInventorySnapshot
    from backend.app.services.jsonutil import dumps
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


def make_session():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(bind=engine)
    Session = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)
    session = Session()
    seed_defaults(session)
    session.commit()
    return session


def test_rbac_seeded_users():
    session = make_session()
    users = session.query(User).all()
    # Check that we seeded 6 default users
    assert len(users) == 6
    
    admin = session.query(User).filter_by(username="admin").one()
    assert admin.role == "admin"
    assert verify_password("admin", admin.password_hash)
    
    owner = session.query(User).filter_by(username="owner").one()
    assert owner.role == "business_owner"
    assert verify_password("owner123", owner.password_hash)
    
    operator = session.query(User).filter_by(username="operator").one()
    assert operator.role == "business_operator"
    assert operator.department == "Sales"
    assert verify_password("operator123", operator.password_hash)
    
    it_ops = session.query(User).filter_by(username="it_ops").one()
    assert it_ops.role == "it_ops"
    assert verify_password("itops123", it_ops.password_hash)


def test_rbac_require_role_decorator():
    session = make_session()
    admin = session.query(User).filter_by(username="admin").one()
    owner = session.query(User).filter_by(username="owner").one()
    operator = session.query(User).filter_by(username="operator").one()
    it_ops = session.query(User).filter_by(username="it_ops").one()
    
    # admin bypasses any require_role checks
    dep_owner = require_role(["business_owner"])
    assert dep_owner(admin) == admin
    assert dep_owner(owner) == owner
    
    # operator should fail to access business_owner endpoint
    with pytest.raises(HTTPException) as exc:
        dep_owner(operator)
    assert exc.value.status_code == 403
    
    # it_ops checks
    dep_it_ops = require_role(["admin", "it_ops"])
    assert dep_it_ops(admin) == admin
    assert dep_it_ops(it_ops) == it_ops
    with pytest.raises(HTTPException) as exc:
        dep_it_ops(owner)
    assert exc.value.status_code == 403


def test_rbac_should_mask_financials_logic():
    session = make_session()
    admin = session.query(User).filter_by(username="admin").one()
    owner = session.query(User).filter_by(username="owner").one()
    auditor = session.query(User).filter_by(username="auditor").one()
    operator = session.query(User).filter_by(username="operator").one()
    operator_other = session.query(User).filter_by(username="operator_other").one()
    it_ops = session.query(User).filter_by(username="it_ops").one()
    
    # admin, business_owner, auditor: should NOT mask
    assert not should_mask_financials(admin, "operator", "Sales")
    assert not should_mask_financials(owner, "operator", "Sales")
    assert not should_mask_financials(auditor, "operator", "Sales")
    
    # it_ops: should always mask
    assert should_mask_financials(it_ops, "operator", "Sales")
    
    # operator (same department Sales): should NOT mask
    assert not should_mask_financials(operator, "operator", "Sales")
    # operator (same department different salesman): should NOT mask because same department
    assert not should_mask_financials(operator, "operator_other", "Sales")
    
    # operator (different department Logistics vs Sales): should mask
    assert should_mask_financials(operator, "operator_other", "Logistics")
    
    # None or non-User: should NOT mask (backward compatibility / default)
    assert not should_mask_financials(None, "operator", "Sales")
    assert not should_mask_financials("invalid_user_type_mock", "operator", "Sales")


def test_rbac_crm_order_serialization_masking():
    session = make_session()
    seed_active_sku(session)
    
    row = {
        "crm_order_id": "crm_so_001",
        "crm_order_no": "SO-001",
        "customer_name": "Test Customer",
        "sales_user_name": "operator",
        "sales_user_email": "operator@jimuyida.com",
        "owner_department": "Sales",
        "order_amount": "100.50",
        "received_amount": "0.00",
        "receivable_amount": "100.50",
        "product_amount": "100.50",
        "settlement_method": "option1",
        "receipt_contact": "张三",
        "receipt_phone": "18600001111",
        "receipt_address": "北京市",
        "items": [{"sku_code": "SKU-3D-SCANNER-PRO", "quantity": 2, "unit_price": "50.25", "line_amount": "100.50"}],
    }
    upsert_crm_sales_orders(session, [row])
    session.commit()
    
    crm_order = session.query(CrmSalesOrder).filter_by(crm_order_id="crm_so_001").one()
    admin = session.query(User).filter_by(username="admin").one()
    it_ops = session.query(User).filter_by(username="it_ops").one()
    operator_other = session.query(User).filter_by(username="operator_other").one()
    
    # admin sees raw values
    data_admin = serialize_crm_order(crm_order, current_user=admin)
    assert float(data_admin["order_amount"]) == 100.5
    
    # it_ops sees masked values
    data_it = serialize_crm_order(crm_order, current_user=it_ops)
    assert data_it["order_amount"] == "***"
    
    # operator_other (different dept) sees masked values
    data_other = serialize_crm_order(crm_order, current_user=operator_other)
    assert data_other["order_amount"] == "***"


def test_rbac_attachment_permissions():
    session = make_session()
    seed_active_sku(session)
    
    row = {
        "crm_order_id": "crm_so_002",
        "crm_order_no": "SO-002",
        "customer_name": "Test Customer",
        "sales_user_name": "operator",
        "sales_user_email": "operator@jimuyida.com",
        "owner_department": "Sales",
        "order_amount": "100.50",
        "received_amount": "0.00",
        "receivable_amount": "100.50",
        "product_amount": "100.50",
        "settlement_method": "option1",
        "receipt_contact": "张三",
        "receipt_phone": "18600001111",
        "receipt_address": "北京市",
        "items": [{"sku_code": "SKU-3D-SCANNER-PRO", "quantity": 2, "unit_price": "50.25", "line_amount": "100.50"}],
    }
    upsert_crm_sales_orders(session, [row])
    session.commit()
    
    crm_order = session.query(CrmSalesOrder).filter_by(crm_order_id="crm_so_002").one()
    
    attachment = OrderAttachment(
        crm_sales_order_id=crm_order.id,
        crm_order_id=crm_order.crm_order_id,
        crm_order_no=crm_order.crm_order_no,
        file_name="invoice.pdf",
        attachment_type="Invoice",
        file_url="http://example.com/invoice.pdf",
        payload_hash="dummy_hash",
        fingerprint="dummy_fp",
    )
    session.add(attachment)
    session.commit()
    
    admin = session.query(User).filter_by(username="admin").one()
    it_ops = session.query(User).filter_by(username="it_ops").one()
    auditor = session.query(User).filter_by(username="auditor").one()
    
    # admin & auditor can serialize and see download link
    res_admin = serialize_order_attachment(attachment, current_user=admin)
    assert res_admin["download_url"] != ""
    assert res_admin["file_url"] != ""
    
    res_aud = serialize_order_attachment(attachment, current_user=auditor)
    assert res_aud["download_url"] != ""
    assert res_aud["file_url"] != ""
    
    # it_ops gets masked urls
    res_it = serialize_order_attachment(attachment, current_user=it_ops)
    assert res_it["download_url"] == ""
    assert res_it["file_url"] == ""
    
    # download_crm_order_attachment endpoint blocks unauthorized roles
    # admin & auditor can download (returns RedirectResponse because file_url is set, NOT 403)
    res_adm = download_crm_order_attachment(attachment.id, session=session, current_user=admin)
    assert isinstance(res_adm, RedirectResponse)
    
    res_aud_download = download_crm_order_attachment(attachment.id, session=session, current_user=auditor)
    assert isinstance(res_aud_download, RedirectResponse)
    
    # it_ops gets 403 Permission Denied
    with pytest.raises(HTTPException) as exc:
        download_crm_order_attachment(attachment.id, session=session, current_user=it_ops)
    assert exc.value.status_code == 403


def test_rbac_apply_address_correction_permissions():
    session = make_session()
    seed_active_sku(session)
    seed_inventory(session, quantity=100)
    seed_oms_required_config(session)
    
    row = {
        "crm_order_id": "crm_so_003",
        "crm_order_no": "SO-003",
        "customer_name": "亚马逊北美渠道",
        "sales_user_name": "operator",
        "sales_user_email": "operator@jimuyida.com",
        "owner_department": "Sales",
        "life_status": "normal",
        "approval_status": "approved",
        "order_date": "2026-06-12",
        "order_amount": "100.50",
        "received_amount": "0.00",
        "receivable_amount": "100.50",
        "product_amount": "100.50",
        "settlement_method": "option1",
        "receipt_contact": "张三",
        "receipt_phone": "18600001111",
        "receipt_address": "北京市",
        "delivery_date": "2026-06-30",
        "attachment_files": "采购订单.pdf; 合同盖章版.pdf",
        "items": [{"sku_code": "SKU-3D-SCANNER-PRO", "quantity": 2, "unit_price": "50.25", "line_amount": "100.50"}],
    }
    upsert_crm_sales_orders(session, [row])
    session.commit()
    
    # Run parsing and validation chain to produce the VALIDATION_BLOCKED case
    run_pending_jobs(session)
    
    case = session.query(ExceptionCase).filter_by(exception_type="VALIDATION_BLOCKED").one()
    
    # Inject AI diagnosis address correction suggestion
    detail = loads(case.detail, {})
    correction = {
        "receipt_address": "湖北省武汉市东湖高新区光谷一路 100 号",
        "receipt_contact": "张三",
        "receipt_phone": "13800001111",
        "reason": "AI修正"
    }
    detail["ai_diagnosis"] = {
        "address_correction": correction
    }
    case.detail = dumps(detail)
    session.commit()
    
    admin = session.query(User).filter_by(username="admin").one()
    operator_other = session.query(User).filter_by(username="operator_other").one()
    
    # operator_other from Logistics department should be blocked on address correction
    with pytest.raises(HTTPException) as exc:
        apply_exception_address_correction(case.id, session=session, current_user=operator_other)
    assert exc.value.status_code == 403
    
    # admin should be allowed
    res = apply_exception_address_correction(case.id, session=session, current_user=admin)
    assert res["success"] is True
