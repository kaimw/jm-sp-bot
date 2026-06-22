from __future__ import annotations
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
import pytest
from fastapi import HTTPException

from backend.app.database import Base
from backend.app.models import ProductSKU, ProductSPU
from backend.app.services.bootstrap import set_config
from backend.app.services.oms.material_sync import query_oms_realtime_stock
from backend.app.main import get_sku_realtime_stock_api


def make_session():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(bind=engine)
    Session = sessionmaker(bind=engine)
    return Session()


def test_query_oms_realtime_stock_mock():
    session = make_session()
    # 启用 mock 模式
    set_config(session, "oms_mock_success", "true")
    session.commit()

    # 创建测试 SKU
    spu = ProductSPU(spu_id="SPU-3D", name="Scanner SPU")
    session.add(spu)
    session.flush()
    sku = ProductSKU(spu_uuid=spu.id, sku_id="SKU-PRO-01", status="Active")
    session.add(sku)
    session.commit()

    stocks = query_oms_realtime_stock(session, "SKU-PRO-01")
    assert len(stocks) == 2
    assert stocks[0]["warehouse_code"] == "WH-SZ"
    assert stocks[0]["quantity"] == 80
    assert stocks[0]["usable_quantity"] == 70

    # 测试 LITE
    stocks_lite = query_oms_realtime_stock(session, "SKU-LITE-02")
    assert len(stocks_lite) == 2
    assert stocks_lite[0]["warehouse_code"] == "WH-SZ"
    assert stocks_lite[0]["quantity"] == 40
    assert stocks_lite[1]["warehouse_code"] == "WH-GZ"


def test_query_oms_realtime_stock_real_api(monkeypatch):
    session = make_session()
    set_config(session, "oms_mock_success", "false")
    set_config(session, "oms_enabled", "true")
    session.commit()

    class FakeClient:
        def query_sku_stock(self, payload):
            assert "skuCode" not in payload
            assert "skuNo" not in payload
            assert payload["goodsNo"] == "SKU-REAL-01"
            assert payload["cols"] == "skuCode,skuNo,warehouseCode,warehouseName,quantity,usableQuantity,currentQuantity,canUseQuantity"
            assert payload["warehouseCode"] in {"A1", "A2", "B1", "B2", "B3", "B5", "C1", "C2", "C3", "E1"}
            if payload["warehouseCode"] == "A1":
                return {
                    "ok": True,
                    "data": {
                        "data": [
                            {
                                "warehouseCode": "WH-MOCK-A",
                                "warehouseName": "Mock Warehouse A",
                                "currentQuantity": 15,
                                "canUseQuantity": 10
                            }
                        ]
                    }
                }
            return {"ok": True, "data": {"data": []}}

    monkeypatch.setattr(
        "backend.app.services.oms.material_sync.jackyun_client_from_session",
        lambda _session: FakeClient()
    )

    stocks = query_oms_realtime_stock(session, "SKU-REAL-01")
    assert len(stocks) == 1
    assert stocks[0]["warehouse_code"] == "WH-MOCK-A"
    assert stocks[0]["warehouse_name"] == "Mock Warehouse A"
    assert stocks[0]["quantity"] == 15
    assert stocks[0]["usable_quantity"] == 10


def test_get_sku_realtime_stock_api_route():
    session = make_session()
    set_config(session, "oms_mock_success", "true")
    session.commit()

    spu = ProductSPU(spu_id="SPU-TEST", name="SPU Name")
    session.add(spu)
    session.flush()
    sku = ProductSKU(spu_uuid=spu.id, sku_id="SKU-PRO-MOCK", status="Active")
    session.add(sku)
    session.commit()

    # 调用 API 路由函数
    res = get_sku_realtime_stock_api(sku_id="SKU-PRO-MOCK", session=session)
    assert res["ok"] is True
    assert res["sku_id"] == "SKU-PRO-MOCK"
    assert len(res["stocks"]) == 2

    # 测试不存在的 SKU 会报错 404
    with pytest.raises(HTTPException) as exc:
        get_sku_realtime_stock_api(sku_id="SKU-NOT-EXISTS", session=session)
    assert exc.value.status_code == 404
