from __future__ import annotations
import tempfile
import os
import openpyxl
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
import pytest
from fastapi import HTTPException

from backend.app.database import Base
from backend.app.models import ProductSKU, ProductSPU, ProductInventorySnapshot
from backend.app.services.excel_import import import_inventory_excel
from backend.app.main import get_sku_realtime_stock_api

def make_session():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(bind=engine)
    Session = sessionmaker(bind=engine)
    return Session()

def test_import_inventory_excel():
    session = make_session()
    
    # Enable OMS and disable mock
    from backend.app.services.bootstrap import set_config
    set_config(session, "oms_mock_success", "false")
    set_config(session, "oms_enabled", "true")
    session.commit()
    
    # 1. Create a dummy SPU and SKU
    spu = ProductSPU(spu_id="1300100008", name="Scanner SPU")
    session.add(spu)
    session.flush()
    sku = ProductSKU(spu_uuid=spu.id, sku_id="1300100008", status="Active")
    session.add(sku)
    session.commit()
    
    # 2. Write a mock Excel file
    headers = [
        "查询编号\nFNSKU/EAN", "sku", "仓库", "Material Code\n料号代码",
        "Chinese description of goods\n中文品名", "产品系列", "产品系列2",
        "Model\n型号", "库存数量", "库存预警", "在途数量", "待接收及转运数量"
    ]
    row1 = [
        "6975925240203", "1300100008", "美西仓库", "1300100008",
        "三维扫描仪CR-Scan Lizard升级款", "三维扫描仪", "-", "-", 
        42.0, "≥1 Year", 96.0, None
    ]
    row2 = [
        "6975925240210", "1300100008", "德国仓库", "1300100008",
        "三维扫描仪CR-Scan Lizard升级款", "三维扫描仪", "-", "-", 
        15.0, "正常", 0.0, None
    ]
    
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "海外库存总表"
    ws.append(headers)
    ws.append(row1)
    ws.append(row2)
    
    fd, path = tempfile.mkstemp(suffix=".xlsx")
    os.close(fd)
    try:
        wb.save(path)
        
        # 3. Call import function
        res = import_inventory_excel(path, session)
        session.commit()
        
        assert res["ok"] is True
        assert res["imported_count"] == 2
        
        # Verify database snapshots
        snaps = session.query(ProductInventorySnapshot).all()
        assert len(snaps) == 2
        
        snap_us = session.query(ProductInventorySnapshot).filter_by(warehouse_code="美西仓库").first()
        assert snap_us is not None
        assert snap_us.material_code == "1300100008"
        assert snap_us.base_qty == 42.0
        
        # 4. Verify merging logic in realtime-stock API
        class FakeClient:
            def query_sku_stock(self, payload):
                # Mock OMS query returns Wuhan倉 (A1) and Gucang-Meixi (C1)
                if payload["warehouseCode"] == "A1":
                    return {
                        "ok": True,
                        "data": {
                            "data": [
                                {
                                    "warehouseCode": "A1",
                                    "warehouseName": "武汉工厂仓",
                                    "currentQuantity": 159,
                                    "canUseQuantity": 159
                                }
                            ]
                        }
                    }
                elif payload["warehouseCode"] == "C1":
                    return {
                        "ok": True,
                        "data": {
                            "data": [
                                {
                                    "warehouseCode": "C1",
                                    "warehouseName": "谷仓-美西",
                                    "currentQuantity": 0,
                                    "canUseQuantity": 0
                                }
                            ]
                        }
                    }
                return {"ok": True, "data": {"data": []}}
                
        # Patch jackyun client
        import backend.app.services.oms.material_sync as ms
        original_client = ms.jackyun_client_from_session
        ms.jackyun_client_from_session = lambda _s: FakeClient()
        
        try:
            # Call API route function
            api_res = get_sku_realtime_stock_api(sku_id="1300100008", session=session)
            assert api_res["ok"] is True
            stocks = api_res["stocks"]
            
            # Wuhan factories (no excel stock)
            wh = next(item for item in stocks if item["warehouse_code"] == "A1")
            assert wh["quantity"] == 159
            assert wh["excel_qty"] is None
            
            # Gucang-Meixi (should match Excel's "美西仓库" because of mapping C1 -> 美西)
            c1 = next(item for item in stocks if item["warehouse_code"] == "C1")
            assert c1["quantity"] == 0
            assert c1["excel_qty"] == 42
            
            # The unmatched German warehouse Excel snap should be appended to stocks
            de = next(item for item in stocks if item["warehouse_code"] == "德国仓库")
            assert de["quantity"] is None
            assert de["excel_qty"] == 15
            
        finally:
            ms.jackyun_client_from_session = original_client
            
    finally:
        if os.path.exists(path):
            os.remove(path)
