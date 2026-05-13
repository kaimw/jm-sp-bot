import json
import math
from typing import Dict, Any, List
import pandas as pd
from sqlalchemy import select
from sqlalchemy.orm import Session

from backend.app.models import ProductSPU, ProductSKU, ChannelPricing

def _clean_val(v):
    if pd.isna(v):
        return None
    if isinstance(v, float) and math.isnan(v):
        return None
    return v

def _parse_price(v):
    v = _clean_val(v)
    if v is None:
        return None
    try:
        return int(float(v) * 100)
    except ValueError:
        return None

def _parse_date(v):
    v = _clean_val(v)
    if v is None:
        return None
    try:
        return pd.to_datetime(v).to_pydatetime()
    except Exception:
        return None


def _first_val(row, *names):
    for name in names:
        value = _clean_val(row.get(name))
        if value is not None:
            return value
    return None

def preview_excel_import(file_path: str, session: Session) -> Dict[str, Any]:
    """Parse the Excel file and return a preview of changes (New, Conflict)."""
    xls = pd.ExcelFile(file_path)
    
    result = {
        "spu": {"new": [], "conflict": []},
        "sku": {"new": [], "conflict": []},
        "pricing": {"new": [], "conflict": []}
    }
    
    # 1. Parse SPU
    spu_sheet = next((name for name in ["SPU_物料基础信息", "SPU_产品基础信息"] if name in xls.sheet_names), None)
    if spu_sheet:
        df_spu = pd.read_excel(xls, spu_sheet)
        for _, row in df_spu.iterrows():
            spu_id = _clean_val(row.get("SPU_ID"))
            if not spu_id:
                continue
            
            item = {
                "spu_id": str(spu_id),
                "name": str(_first_val(row, "物料名称(中文)", "产品名称(中文)", "商品名称(中文)") or ""),
                "name_en": _first_val(row, "物料名称(英文)", "产品名称(英文)", "商品名称(英文)"),
                "product_line": _first_val(row, "物料线", "产品线", "商品线"),
                "product_type": _first_val(row, "物料类型", "产品类型", "商品类型"),
                "brand": _clean_val(row.get("品牌")),
                "positioning": _first_val(row, "物料定位", "产品定位", "商品定位"),
                "launch_time": _clean_val(row.get("上市时间")),
                "lifecycle": _clean_val(row.get("生命周期")),
                "core_selling_points": _clean_val(row.get("核心卖点")),
                "use_cases": _clean_val(row.get("应用场景")),
                "target_users": _clean_val(row.get("目标用户")),
            }
            
            existing = session.execute(select(ProductSPU).where(ProductSPU.spu_id == str(spu_id))).scalars().first()
            if existing:
                result["spu"]["conflict"].append(item)
            else:
                result["spu"]["new"].append(item)

    # 2. Parse SKU
    if "SKU_销售单元" in xls.sheet_names:
        df_sku = pd.read_excel(xls, "SKU_销售单元")
        for _, row in df_sku.iterrows():
            sku_id = _clean_val(row.get("SKU_ID"))
            spu_id = _clean_val(row.get("SPU_ID"))
            if not sku_id or not spu_id:
                continue
            
            item = {
                "sku_id": str(sku_id),
                "spu_id": str(spu_id),
                "model": _clean_val(row.get("型号")),
                "version": _clean_val(row.get("版本")),
                "package_contents": _clean_val(row.get("套装内容")),
                "barcode": str(_clean_val(row.get("条码"))) if _clean_val(row.get("条码")) else None,
                "weight": _clean_val(row.get("重量")),
                "dimensions": _clean_val(row.get("尺寸")),
                "color": _clean_val(row.get("颜色")),
                "cost_price": _parse_price(row.get("成本价")),
                "msrp": _parse_price(row.get("建议零售价")),
            }
            
            existing = session.execute(select(ProductSKU).where(ProductSKU.sku_id == str(sku_id))).scalars().first()
            if existing:
                result["sku"]["conflict"].append(item)
            else:
                result["sku"]["new"].append(item)

    # 3. Parse Pricing
    if "渠道映射_增强" in xls.sheet_names:
        df_pricing = pd.read_excel(xls, "渠道映射_增强")
        for _, row in df_pricing.iterrows():
            sku_id = _clean_val(row.get("SKU_ID"))
            channel = _clean_val(row.get("渠道名称"))
            if not sku_id or not channel:
                continue
            
            item = {
                "sku_id": str(sku_id),
                "channel": str(channel),
                "channel_sku_id": _clean_val(row.get("渠道SKU_ID")),
                "listing_id": _clean_val(row.get("Listing_ID")),
                "status": _clean_val(row.get("上架状态")),
                "tier_a_price": _parse_price(row.get("日常售价")),
                "tier_b_price": _parse_price(row.get("一级促销价（如大促）")),
                "tier_c_price": _parse_price(row.get("二级促销价（如日常折扣）")),
                "map_price": _parse_price(row.get("最低限价")),
                "max_price": _parse_price(row.get("最高限价")),
                "promo_start_time": _parse_date(row.get("促销开始时间")),
                "promo_end_time": _parse_date(row.get("促销结束时间")),
                "currency": _clean_val(row.get("货币")) or "CNY",
                "stock_quantity": _clean_val(row.get("渠道库存")),
                "manager": _clean_val(row.get("渠道负责人")),
            }
            
            sku_db = session.execute(select(ProductSKU).where(ProductSKU.sku_id == str(sku_id))).scalars().first()
            sku_uuid = sku_db.id if sku_db else None
            
            if sku_uuid:
                existing = session.execute(select(ChannelPricing).where(ChannelPricing.sku_uuid == sku_uuid, ChannelPricing.channel == str(channel))).scalars().first()
                if existing:
                    result["pricing"]["conflict"].append(item)
                else:
                    result["pricing"]["new"].append(item)
            else:
                # Treat as new, but it will require the SKU to be imported first
                result["pricing"]["new"].append(item)

    return result


def confirm_excel_import(data: Dict[str, Any], session: Session) -> Dict[str, int]:
    """Execute the import logic using the accepted preview data."""
    counts = {"spu": 0, "sku": 0, "pricing": 0}
    
    # 1. SPU
    for spu_data in data.get("spu", []):
        spu_id = spu_data["spu_id"]
        existing = session.execute(select(ProductSPU).where(ProductSPU.spu_id == spu_id)).scalars().first()
        
        extended_info = {
            "core_selling_points": spu_data.get("core_selling_points"),
            "use_cases": spu_data.get("use_cases"),
            "target_users": spu_data.get("target_users"),
        }
        
        if existing:
            existing.name = spu_data.get("name", existing.name)
            existing.name_en = spu_data.get("name_en", existing.name_en)
            existing.product_line = spu_data.get("product_line", existing.product_line)
            existing.product_type = spu_data.get("product_type", existing.product_type)
            existing.brand = spu_data.get("brand", existing.brand)
            existing.positioning = spu_data.get("positioning", existing.positioning)
            existing.launch_time = str(spu_data.get("launch_time")) if spu_data.get("launch_time") else existing.launch_time
            existing.lifecycle = spu_data.get("lifecycle", existing.lifecycle)
            existing.extended_info_json = json.dumps(extended_info)
        else:
            new_spu = ProductSPU(
                spu_id=spu_id,
                name=spu_data.get("name", spu_id),
                name_en=spu_data.get("name_en"),
                product_line=spu_data.get("product_line"),
                product_type=spu_data.get("product_type"),
                brand=spu_data.get("brand"),
                positioning=spu_data.get("positioning"),
                launch_time=str(spu_data.get("launch_time")) if spu_data.get("launch_time") else None,
                lifecycle=spu_data.get("lifecycle"),
                extended_info_json=json.dumps(extended_info)
            )
            session.add(new_spu)
        counts["spu"] += 1
    
    session.flush()

    # 2. SKU
    for sku_data in data.get("sku", []):
        sku_id = sku_data["sku_id"]
        spu_id = sku_data["spu_id"]
        
        # find spu
        spu = session.execute(select(ProductSPU).where(ProductSPU.spu_id == spu_id)).scalars().first()
        if not spu:
            continue
            
        existing = session.execute(select(ProductSKU).where(ProductSKU.sku_id == sku_id)).scalars().first()
        
        attributes = {
            "weight": sku_data.get("weight"),
            "dimensions": sku_data.get("dimensions"),
            "color": sku_data.get("color"),
            "package_contents": sku_data.get("package_contents")
        }
        
        if existing:
            existing.model = sku_data.get("model", existing.model)
            existing.version = sku_data.get("version", existing.version)
            existing.barcode = str(sku_data.get("barcode")) if sku_data.get("barcode") else existing.barcode
            existing.cost_price = sku_data.get("cost_price", existing.cost_price)
            existing.msrp = sku_data.get("msrp", existing.msrp)
            existing.attributes_json = json.dumps(attributes)
        else:
            new_sku = ProductSKU(
                spu_uuid=spu.id,
                sku_id=sku_id,
                model=sku_data.get("model"),
                version=sku_data.get("version"),
                barcode=str(sku_data.get("barcode")) if sku_data.get("barcode") else None,
                cost_price=sku_data.get("cost_price"),
                msrp=sku_data.get("msrp"),
                attributes_json=json.dumps(attributes)
            )
            session.add(new_sku)
        counts["sku"] += 1
    
    session.flush()

    # 3. Pricing
    for pricing_data in data.get("pricing", []):
        sku_id = pricing_data["sku_id"]
        channel = pricing_data["channel"]
        
        sku = session.execute(select(ProductSKU).where(ProductSKU.sku_id == sku_id)).scalars().first()
        if not sku:
            continue
            
        existing = session.execute(select(ChannelPricing).where(
            ChannelPricing.sku_uuid == sku.id,
            ChannelPricing.channel == channel
        )).scalars().first()
        
        if existing:
            existing.channel_sku_id = pricing_data.get("channel_sku_id", existing.channel_sku_id)
            existing.listing_id = pricing_data.get("listing_id", existing.listing_id)
            existing.status = pricing_data.get("status", existing.status)
            existing.tier_a_price = pricing_data.get("tier_a_price", existing.tier_a_price)
            existing.tier_b_price = pricing_data.get("tier_b_price", existing.tier_b_price)
            existing.tier_c_price = pricing_data.get("tier_c_price", existing.tier_c_price)
            existing.map_price = pricing_data.get("map_price", existing.map_price)
            existing.max_price = pricing_data.get("max_price", existing.max_price)
            if "promo_start_time" in pricing_data:
                existing.promo_start_time = pricing_data["promo_start_time"]
            if "promo_end_time" in pricing_data:
                existing.promo_end_time = pricing_data["promo_end_time"]
            existing.currency = pricing_data.get("currency", existing.currency)
            existing.stock_quantity = pricing_data.get("stock_quantity", existing.stock_quantity)
            existing.manager = pricing_data.get("manager", existing.manager)
        else:
            new_pricing = ChannelPricing(
                sku_uuid=sku.id,
                channel=channel,
                channel_sku_id=pricing_data.get("channel_sku_id"),
                listing_id=pricing_data.get("listing_id"),
                status=pricing_data.get("status"),
                tier_a_price=pricing_data.get("tier_a_price"),
                tier_b_price=pricing_data.get("tier_b_price"),
                tier_c_price=pricing_data.get("tier_c_price"),
                map_price=pricing_data.get("map_price"),
                max_price=pricing_data.get("max_price"),
                promo_start_time=pricing_data.get("promo_start_time"),
                promo_end_time=pricing_data.get("promo_end_time"),
                currency=pricing_data.get("currency") or "CNY",
                stock_quantity=pricing_data.get("stock_quantity"),
                manager=pricing_data.get("manager")
            )
            session.add(new_pricing)
        counts["pricing"] += 1
        
    return counts
