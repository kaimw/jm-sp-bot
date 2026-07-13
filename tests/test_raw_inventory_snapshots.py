from backend.app.models import ProductInventorySnapshot, ProductSPU
from backend.app.main import list_raw_inventory_snapshots
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from backend.app.database import Base

def make_session():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(bind=engine)
    Session = sessionmaker(bind=engine)
    return Session()

def test_list_raw_inventory_snapshots_finished_only():
    session = make_session()

    # 1. Setup finished product classification rules in system configs
    # Finished category is '成品'
    from backend.app.services.erp.business_queries import FINISHED_CATEGORY
    
    # 2. Add SPUs: one finished (成品) and one non-finished
    spu_finished = ProductSPU(spu_id="SPU-FINISHED", name="Finished Item", category="成品")
    spu_raw = ProductSPU(spu_id="SPU-RAW", name="Raw Material", category="原材料")
    session.add(spu_finished)
    session.add(spu_raw)
    session.commit()

    # 3. Add inventory snapshots
    snap_finished = ProductInventorySnapshot(
        material_code="SPU-FINISHED",
        material_name="Finished Item",
        warehouse_code="WH01",
        warehouse_name="Wuhan Warehouse",
        qty=10
    )
    snap_raw = ProductInventorySnapshot(
        material_code="SPU-RAW",
        material_name="Raw Material",
        warehouse_code="WH01",
        warehouse_name="Wuhan Warehouse",
        qty=20
    )
    session.add(snap_finished)
    session.add(snap_raw)
    session.commit()

    # Test query without inventory_scope (returns all snapshots)
    res_all = list_raw_inventory_snapshots(session=session)
    assert len(res_all["items"]) == 2
    assert res_all["items"][0]["warehouse_name"] == "Wuhan Warehouse"

    # Test query with inventory_scope="finished" (returns only finished item)
    res_finished = list_raw_inventory_snapshots(inventory_scope="finished", session=session)
    assert len(res_finished["items"]) == 1
    assert res_finished["items"][0]["material_code"] == "SPU-FINISHED"

    # Test query with inventory_scope="non_finished" (returns only raw material item)
    res_non_finished = list_raw_inventory_snapshots(inventory_scope="non_finished", session=session)
    assert len(res_non_finished["items"]) == 1
    assert res_non_finished["items"][0]["material_code"] == "SPU-RAW"

    # Add a zero qty (out of stock) snapshot
    snap_zero = ProductInventorySnapshot(
        material_code="SPU-ZERO",
        material_name="Zero Stock Item",
        warehouse_code="WH01",
        warehouse_name="Wuhan Warehouse",
        qty=0
    )
    session.add(snap_zero)
    session.commit()

    # Test stock_status="in_stock" (returns snapshots with qty > 0: finished and raw)
    res_in_stock = list_raw_inventory_snapshots(stock_status="in_stock", session=session)
    assert len(res_in_stock["items"]) == 2
    assert all(item["qty"] > 0 for item in res_in_stock["items"])

    # Test stock_status="out_of_stock" (returns snapshots with qty <= 0: zero stock item)
    res_out_of_stock = list_raw_inventory_snapshots(stock_status="out_of_stock", session=session)
    assert len(res_out_of_stock["items"]) == 1
    assert res_out_of_stock["items"][0]["material_code"] == "SPU-ZERO"

def test_get_kingdee_organizations():
    from backend.app.main import get_kingdee_organizations
    session = make_session()
    res = get_kingdee_organizations(session=session)
    assert "items" in res
    assert len(res["items"]) == 12
    assert res["items"][0]["entity_code"] == "SZ"
    assert res["items"][0]["erp_org_id"] == "100"

def test_outbound_mail_job_recipient_merging():
    from backend.app.models import OutboundMailJob, MailReceiverConfig
    import json
    session = make_session()

    config = MailReceiverConfig(
        scene="ProductionRejected",
        to_json=json.dumps(["configured_to@example.com"]),
        cc_json=json.dumps(["configured_cc@example.com"]),
        is_active=True
    )
    session.add(config)
    session.commit()

    job = OutboundMailJob(
        mail_type="ProductionRejected",
        to_json=json.dumps(["original_to@example.com"]),
        cc_json=json.dumps(["original_cc@example.com"]),
        subject="Test Status Notice",
        body="Body content",
        idempotency_key="test-key-12345",
        status="Pending"
    )
    session.add(job)
    session.commit()

    retrieved = session.get(OutboundMailJob, job.id)
    retrieved_to = json.loads(retrieved.to_json)
    retrieved_cc = json.loads(retrieved.cc_json)

    assert "original_to@example.com" in retrieved_to
    assert "configured_to@example.com" in retrieved_to
    assert "original_cc@example.com" in retrieved_cc
    assert "configured_cc@example.com" in retrieved_cc

