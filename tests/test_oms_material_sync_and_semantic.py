from __future__ import annotations
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
import pytest

from backend.app.database import Base
from backend.app.models import ProductSKU, ProductSPU, SystemConfig
from backend.app.services.bootstrap import set_config
from backend.app.services.oms.material_sync import sync_oms_materials, oms_material_sync_due
from backend.app.services.products import get_skus, semantic_match_skus


def make_session():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(bind=engine)
    Session = sessionmaker(bind=engine)
    return Session()


def test_oms_material_sync_mock(monkeypatch):
    session = make_session()
    set_config(session, "oms_mock_success", "true")
    session.commit()

    result = sync_oms_materials(session)
    assert result["ok"] is True
    assert result["source"] == "mock"
    assert result["total"] > 0

    scanner_spu = session.query(ProductSPU).filter_by(spu_id="SPU-3D-SCANNER").one()
    assert scanner_spu.name == "3D Scanner"

    scanner_sku = session.query(ProductSKU).filter_by(sku_id="SKU-3D-SCANNER-PRO").one()
    assert scanner_sku.model == "Pro"
    assert scanner_sku.spu_uuid == scanner_spu.id


def test_oms_material_sync_uses_goodslist_max_sku_id_cursor(monkeypatch):
    session = make_session()
    set_config(session, "oms_mock_success", "false")
    set_config(session, "oms_enabled", "true")
    session.commit()

    calls = []

    class FakeClient:
        def search_goods(self, payload):
            calls.append(payload)
            max_sku_id = str(payload.get("maxSkuId"))
            rows_by_cursor = {
                "0": [
                    {"goodsNo": "G-1", "goodsName": "Goods 1", "skuNo": "S-100", "skuId": "100", "skuName": "A"},
                    {"goodsNo": "G-1", "goodsName": "Goods 1", "skuNo": "S-101", "skuId": "101", "skuName": "B"},
                ],
                "101": [
                    {"goodsNo": "G-2", "goodsName": "Goods 2", "skuNo": "S-102", "skuId": "102", "skuName": "C"},
                ],
            }
            return {"ok": True, "data": {"data": {"goods": rows_by_cursor.get(max_sku_id, [])}}}

    monkeypatch.setattr(
        "backend.app.services.oms.material_sync.jackyun_client_from_session",
        lambda _session: FakeClient(),
    )

    result = sync_oms_materials(session, batch_size=2, max_batches=10)

    assert result["ok"] is True
    assert result["total"] == 3
    assert [call["maxSkuId"] for call in calls] == ["0", "101"]
    assert all(call["pageIndex"] == 0 for call in calls)
    assert session.query(ProductSKU).count() == 3
    assert {sku.sku_id for sku in session.query(ProductSKU).all()} == {"S-100", "S-101", "S-102"}


def test_oms_material_sync_skips_rows_without_oms_sku_code(monkeypatch):
    session = make_session()
    set_config(session, "oms_mock_success", "false")
    set_config(session, "oms_enabled", "true")
    session.commit()

    class FakeClient:
        def search_goods(self, payload):
            return {
                "ok": True,
                "data": {
                    "data": {
                        "goods": [
                            {"goodsNo": "k1c+seal", "goodsName": "Combo Goods", "skuNo": "", "skuId": "200", "skuName": "A"},
                            {"goodsNo": "1021500010", "goodsName": "Numeric Material", "skuNo": "", "skuId": "201", "skuName": "B"},
                        ]
                    }
                },
            }

    monkeypatch.setattr(
        "backend.app.services.oms.material_sync.jackyun_client_from_session",
        lambda _session: FakeClient(),
    )

    result = sync_oms_materials(session, batch_size=200, max_batches=1)

    assert result["total"] == 1
    assert {sku.sku_id for sku in session.query(ProductSKU).all()} == {"1021500010"}


def test_oms_material_sync_skips_blocked_and_package_goods(monkeypatch):
    session = make_session()
    set_config(session, "oms_mock_success", "false")
    set_config(session, "oms_enabled", "true")
    session.commit()

    class FakeClient:
        def search_goods(self, payload):
            return {
                "ok": True,
                "data": {
                    "data": {
                        "goods": [
                            {"goodsNo": "G-A", "goodsName": "Goods A", "skuNo": "S-A", "skuId": "300", "skuName": "A"},
                            {"goodsNo": "G-B", "goodsName": "Goods B", "skuNo": "S-B", "skuId": "301", "skuName": "B", "isPackageGood": 1},
                            {"goodsNo": "G-C", "goodsName": "Goods C", "skuNo": "S-C", "skuId": "302", "skuName": "C", "isBlockup": 1, "skuIsBlockup": 1},
                        ]
                    }
                },
            }

    monkeypatch.setattr(
        "backend.app.services.oms.material_sync.jackyun_client_from_session",
        lambda _session: FakeClient(),
    )

    result = sync_oms_materials(session, batch_size=200, max_batches=1)

    assert result["total"] == 1
    assert {sku.sku_id for sku in session.query(ProductSKU).all()} == {"S-A"}


def test_oms_material_sync_due():
    session = make_session()
    
    assert oms_material_sync_due(session) is False

    set_config(session, "oms_mock_success", "true")
    set_config(session, "oms_material_sync_enabled", "true")
    set_config(session, "oms_material_sync_interval_seconds", "60")
    session.commit()

    assert oms_material_sync_due(session) is True

    import datetime
    from backend.app.models import now_utc
    set_config(session, "oms_material_last_sync_at", now_utc().isoformat())
    session.commit()

    assert oms_material_sync_due(session) is False


def test_get_skus_fallback(monkeypatch):
    session = make_session()
    spu = ProductSPU(spu_id="SPU-A", name="Standard Product A", category="成品", status="Active")
    session.add(spu)
    session.flush()
    sku = ProductSKU(spu_uuid=spu.id, sku_id="SKU-A-PRO", model="Pro Version", status="Active")
    session.add(sku)
    session.commit()

    items, total = get_skus(session, query="Standard", crm_semantic=False)
    assert total == 1
    assert items[0].sku_id == "SKU-A-PRO"

    items, total = get_skus(session, query="SKU-A-PRO", crm_semantic=False)
    assert total == 1
