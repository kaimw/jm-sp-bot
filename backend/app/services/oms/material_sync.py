from __future__ import annotations
import logging
from datetime import datetime
from typing import Any
from sqlalchemy.orm import Session
from backend.app.models import ProductSKU, ProductSPU, SystemConfig, AuditEvent, now_utc
from backend.app.services.bootstrap import set_config
from backend.app.services.oms.jackyun_client import jackyun_client_from_session
from backend.app.services.jsonutil import dumps, loads

logger = logging.getLogger(__name__)

def config_value(session: Session, key: str, fallback: str = "") -> str:
    row = session.get(SystemConfig, key)
    if row is None:
        return fallback
    return row.value or fallback

def config_bool(session: Session, key: str, default: bool = False) -> bool:
    value = config_value(session, key, str(default)).strip().lower()
    return value in {"1", "true", "yes", "on"}

def extract_goods_rows(data_block: Any) -> list[dict[str, Any]]:
    if isinstance(data_block, list):
        return [row for row in data_block if isinstance(row, dict)]
    if not isinstance(data_block, dict):
        return []
    for key in ("rows", "goods", "list"):
        rows = data_block.get(key)
        if isinstance(rows, list):
            return [row for row in rows if isinstance(row, dict)]
    nested = data_block.get("data")
    if isinstance(nested, (dict, list)):
        rows = extract_goods_rows(nested)
        if rows:
            return rows
    for val in data_block.values():
        if isinstance(val, list) and val:
            return [row for row in val if isinstance(row, dict)]
    return []

def clean_oms_value(value: Any) -> str:
    text = str(value or "").strip()
    return "" if text.lower() == "none" else text

def oms_material_dedupe_key(item: dict[str, Any]) -> str:
    material_code = oms_material_code(item)
    return f"material:{material_code}" if material_code else ""

def oms_material_code(item: dict[str, Any]) -> str:
    sku_code = clean_oms_value(item.get("skuNo") or item.get("skuCode") or item.get("sku_code"))
    if sku_code:
        return sku_code
    goods_code = clean_oms_value(item.get("goodsNo") or item.get("goodsCode") or item.get("goods_code"))
    return goods_code if goods_code.isdigit() else ""

def oms_sku_cursor(item: dict[str, Any]) -> tuple[int, str] | None:
    value = clean_oms_value(item.get("skuId") or item.get("sku_id"))
    if not value:
        return None
    try:
        return int(value), value
    except ValueError:
        logger.warning("OMS goodslist 返回了非数字 skuId，无法继续 maxSkuId 游标同步: %s", value)
        return None

def oms_flag_enabled(item: dict[str, Any], key: str) -> bool:
    value = item.get(key)
    return value is True or str(value).strip() == "1"

def is_syncable_oms_material(item: dict[str, Any]) -> bool:
    if not oms_material_code(item):
        return False
    if oms_flag_enabled(item, "isDelete"):
        return False
    if oms_flag_enabled(item, "isBlockup") or oms_flag_enabled(item, "skuIsBlockup"):
        return False
    if oms_flag_enabled(item, "isPackageGood"):
        return False
    return True

def sync_oms_materials(session: Session, *, batch_size: int = 200, max_batches: int = 10) -> dict[str, Any]:
    # 如果 mock 模式开启
    if config_bool(session, "oms_mock_success", True):
        mock_items = [
            {
                "goodsCode": "SPU-3D-SCANNER",
                "goodsName": "3D Scanner",
                "enName": "3D Scanner Pro",
                "skuCode": "SKU-3D-SCANNER-PRO",
                "skuName": "Pro",
                "brandName": "Creality",
                "categoryName": "成品",
                "status": "Active",
                "alias": "扫描仪, 3D扫描仪专业版, 旗舰版扫描仪"
            },
            {
                "goodsCode": "SPU-3D-SCANNER",
                "goodsName": "3D Scanner",
                "enName": "3D Scanner Lite",
                "skuCode": "SKU-3D-SCANNER-LITE",
                "skuName": "Lite",
                "brandName": "Creality",
                "categoryName": "成品",
                "status": "Active",
                "alias": "扫描仪, 3D扫描仪轻量版, 青春版扫描仪"
            },
            {
                "goodsCode": "MAT-001",
                "goodsName": "物料001",
                "skuCode": "MAT-001",
                "skuName": "M01",
                "brandName": "Creality",
                "categoryName": "成品",
                "status": "Active",
                "alias": "标准测试物料"
            },
            {
                "goodsCode": "MAT-JOB",
                "goodsName": "队列物料",
                "skuCode": "MAT-JOB",
                "skuName": "MJ",
                "brandName": "Creality",
                "categoryName": "成品",
                "status": "Active",
                "alias": "定时同步测试物料"
            },
            {
                "goodsCode": "MAT-SEARCH",
                "goodsName": "查询物料",
                "skuCode": "MAT-SEARCH",
                "skuName": "MS",
                "brandName": "Creality",
                "categoryName": "同步分类",
                "status": "Active",
                "alias": "别名匹配测试物料"
            },
            {
                "goodsCode": "MAT-STOCK",
                "goodsName": "库存物料",
                "skuCode": "MAT-STOCK",
                "skuName": "MSK",
                "brandName": "Creality",
                "categoryName": "成品",
                "status": "Active",
                "alias": "库存快照对照物料"
            },
            {
                "goodsCode": "MAT-ZERO",
                "goodsName": "零库存物料",
                "skuCode": "MAT-ZERO",
                "skuName": "MZ",
                "brandName": "Creality",
                "categoryName": "成品",
                "status": "Active",
                "alias": "无库存展示物料"
            }
        ]
        created_spu = updated_spu = created_sku = updated_sku = total = 0
        for item in mock_items:
            spu_res = upsert_oms_spu(session, item)
            sku_res = upsert_oms_sku(session, item, spu_res["spu"])
            created_spu += 1 if spu_res["created"] else 0
            updated_spu += 0 if spu_res["created"] else 1
            created_sku += 1 if sku_res["created"] else 0
            updated_sku += 0 if sku_res["created"] else 1
            total += 1
        session.flush()
        synced_at = now_utc()
        set_config(session, "oms_material_last_sync_at", synced_at.isoformat(), is_secret=False)
        session.add(AuditEvent(
            event_type="OmsMaterialSynced",
            related_object_type="SystemConfig",
            related_object_id="master-data",
            detail=dumps({"source": "mock", "total": total, "created_spus": created_spu, "created_skus": created_sku})
        ))
        return {
            "ok": True,
            "source": "mock",
            "total": total,
            "created_spu": created_spu,
            "updated_spu": updated_spu,
            "created_sku": created_sku,
            "updated_sku": updated_sku,
            "last_sync_at": synced_at.isoformat(),
        }

    # 真实同步模式：erp.storage.goodslist 按 maxSkuId 游标同步，首次 maxSkuId=0。
    if not config_bool(session, "oms_enabled", False):
        return {"ok": False, "skipped": "OMS 未启用且未处于 Mock 模式", "total": 0}

    client = jackyun_client_from_session(session)
    created_spu = updated_spu = created_sku = updated_sku = total = 0
    debug_info: dict[str, Any] = {}
    seen_sku_ids: set[str] = set()
    max_sku_id = "0"
    batch_count = 0

    for batch_index in range(max_batches):
        result = client.search_goods({"pageIndex": 0, "pageSize": batch_size, "maxSkuId": max_sku_id})
        if not result.get("ok"):
            raise RuntimeError(result.get("message") or "OMS 货品查询接口失败")

        data_block = result.get("data") or {}
        rows = extract_goods_rows(data_block)

        if batch_index == 0:
            debug_info["pagination"] = "maxSkuId"
            debug_info["page_size"] = batch_size
            debug_info["first_row_keys"] = list(rows[0].keys()) if rows else []

        if not rows:
            break

        batch_count += 1
        next_cursor: str | None = None
        for row in rows:
            cursor = oms_sku_cursor(row)
            if cursor:
                next_cursor = cursor[1]
            dedupe_key = oms_material_dedupe_key(row)
            if dedupe_key and dedupe_key in seen_sku_ids:
                continue  # 已在本批次同步中处理过
            if not is_syncable_oms_material(row):
                continue
            spu_res = upsert_oms_spu(session, row)
            if spu_res.get("skipped"):
                continue
            sku_res = upsert_oms_sku(session, row, spu_res["spu"])
            created_spu += 1 if spu_res["created"] else 0
            updated_spu += 0 if spu_res["created"] else 1
            created_sku += 1 if sku_res["created"] else 0
            updated_sku += 0 if sku_res["created"] else 1
            total += 1
            if dedupe_key:
                seen_sku_ids.add(dedupe_key)

        # 每页结束立即 flush，确保下一页的 upsert 能查到已有数据
        session.flush()

        if next_cursor is None or next_cursor == max_sku_id:
            break
        max_sku_id = next_cursor
        # 本页数据不足一页 → 已是最后一页
        if len(rows) < batch_size:
            break

    synced_at = now_utc()
    set_config(session, "oms_material_last_sync_at", synced_at.isoformat(), is_secret=False)
    session.add(AuditEvent(
        event_type="OmsMaterialSynced",
        related_object_type="SystemConfig",
        related_object_id="master-data",
        detail=dumps({"source": "jackyun", "total": total, "created_spus": created_spu, "created_skus": created_sku})
    ))
    return {
        "ok": True,
        "source": "jackyun",
        "total": total,
        "created_spu": created_spu,
        "updated_spu": updated_spu,
        "created_sku": created_sku,
        "updated_sku": updated_sku,
        "last_sync_at": synced_at.isoformat(),
        "_debug": {**debug_info, "batches": batch_count, "last_max_sku_id": max_sku_id},
    }


def upsert_oms_spu(session: Session, item: dict[str, Any]) -> dict[str, Any]:
    # goodslist API 字段: goodsNo=货品编码, goodsName=中文名, goodsNameEn=英文名
    goods_code = clean_oms_value(item.get("goodsNo") or item.get("goodsCode") or item.get("goods_code"))
    goods_name = str(item.get("goodsName") or item.get("goods_name") or "").strip()
    if not goods_code:
        goods_code = clean_oms_value(item.get("skuNo") or item.get("skuCode") or item.get("sku_code"))
    if not goods_code:
        return {"spu": None, "created": False, "skipped": True, "reason": "no goods_code"}
    if not goods_name:
        goods_name = goods_code

    spu = session.query(ProductSPU).filter_by(spu_id=goods_code).one_or_none()
    created = spu is None
    if spu is None:
        spu = ProductSPU(spu_id=goods_code, name=goods_name)
        session.add(spu)
        session.flush()

    spu.name = goods_name
    spu.brand = str(item.get("brandName") or item.get("brand_name") or spu.brand or "").strip() or None
    # goodslist 返回的都是成品货品，统一设为"成品"以确保在物料列表中可见
    # cateName（如"三维扫描仪-CRS1"）存入 extended_info 供参考
    spu.category = "成品"
    spu.status = "Active"

    # 提取别名
    raw_aliases = []
    alias_keys = [
        "goodsAlias", "alias", "aliasName", "alias_name", "goods_alias",
        "subName", "sub_name", "shortName", "short_name", "mnemonicCode",
        "mnemonic_code", "searchName", "search_name", "goodsSpec", "goods_spec",
    ]
    for key in alias_keys:
        val = item.get(key)
        if val and str(val).strip() and str(val).strip().lower() != "none":
            raw_aliases.append(str(val).strip())

    # 提取英文名（加入别名以便搜索）
    en_name_keys = [
        "goodsNameEn", "enName", "englishName", "goodsEnName",
        "nameEn", "goods_name_en", "en_name", "english_name", "name_en",
    ]
    en_name = ""
    for key in en_name_keys:
        val = item.get(key)
        if val and str(val).strip() and str(val).strip().lower() != "none":
            s = str(val).strip()
            if not en_name:
                en_name = s
            raw_aliases.append(s)

    from backend.app.services.products import normalize_product_review_aliases
    new_aliases = normalize_product_review_aliases(raw_aliases)

    info = loads(spu.extended_info_json, {})
    if not isinstance(info, dict):
        info = {}

    existing_aliases = info.get("review_aliases") or []
    if not isinstance(existing_aliases, list):
        existing_aliases = [existing_aliases]

    merged = list(existing_aliases)
    for alias in new_aliases:
        if alias not in merged:
            merged.append(alias)

    info["review_aliases"] = merged

    # 存储 OMS 的英文名称等扩展信息
    oms_info = {
        "source": "jackyun",
        "goods_code": goods_code,
        "synced_at": now_utc().isoformat(),
    }
    for key in en_name_keys:
        val = item.get(key)
        if val and str(val).strip():
            oms_info["en_name"] = str(val).strip()
            break
    # 捕获完整的原始数据以供调试
    oms_info["raw"] = item
    info["oms"] = oms_info
    spu.extended_info_json = dumps(info)
    spu.updated_at = now_utc()
    return {"spu": spu, "created": created}

def upsert_oms_sku(session: Session, item: dict[str, Any], spu: ProductSPU) -> dict[str, Any]:
    if spu is None:
        return {"sku": None, "created": False, "skipped": True}
    # skuId/goodsNo 只用于 OMS 游标和溯源；本系统物料编码必须来自 OMS SKU 编码字段。
    sku_code = oms_material_code(item)
    sku_name = str(item.get("skuName") or item.get("sku_name") or
                   item.get("goodsName") or item.get("goods_name") or
                   spu.name or "").strip()
    if not sku_code:
        return {"sku": None, "created": False, "skipped": True}

    sku = session.query(ProductSKU).filter_by(sku_id=sku_code).one_or_none()
    created = sku is None
    if sku is None:
        sku = ProductSKU(spu_uuid=spu.id, sku_id=sku_code)
        session.add(sku)

    sku.spu_uuid = spu.id
    sku.model = sku_name or sku.model
    sku.status = "Active"

    attrs = loads(sku.attributes_json, {})
    attrs.update({
        "oms_sku_id": clean_oms_value(item.get("skuId") or item.get("sku_id")),
        "oms_sku_no": clean_oms_value(item.get("skuNo") or item.get("skuCode") or item.get("sku_code")),
        "oms_sku_barcode": clean_oms_value(item.get("skuBarcode") or item.get("barcode")),
        "oms_goods_name": item.get("goodsName") or item.get("goods_name", spu.name),
        "oms_sku_name": sku_name,
        "oms_brand_name": item.get("brandName") or item.get("brand_name", ""),
        "oms_category_name": item.get("cateName") or item.get("categoryName") or item.get("category_name", ""),
        "oms_synced_at": now_utc().isoformat()
    })
    # 捕获英文名
    for key in ["goodsNameEn", "enName", "englishName", "skuEnName", "nameEn",
                "en_name", "english_name", "sku_en_name", "name_en", "goods_name_en"]:
        val = item.get(key)
        if val and str(val).strip() and str(val).strip().lower() != "none":
            attrs["oms_en_name"] = str(val).strip()
            break
    sku.attributes_json = dumps(attrs)
    
    supply = loads(sku.supply_info_json, {})
    supply["oms"] = {"source": "jackyun", "raw": item}
    sku.supply_info_json = dumps(supply)
    sku.updated_at = now_utc()
    deactivate_legacy_generated_sku(session, item, canonical_sku=sku)
    return {"sku": sku, "created": created}

def deactivate_legacy_generated_sku(session: Session, item: dict[str, Any], *, canonical_sku: ProductSKU) -> None:
    oms_sku_id = clean_oms_value(item.get("skuId") or item.get("sku_id"))
    if not oms_sku_id or oms_sku_id == canonical_sku.sku_id:
        return
    legacy = session.query(ProductSKU).filter_by(sku_id=oms_sku_id).one_or_none()
    if legacy is None or legacy.id == canonical_sku.id:
        return
    supply = loads(legacy.supply_info_json, {})
    raw = supply.get("oms", {}).get("raw") if isinstance(supply, dict) else None
    if not isinstance(raw, dict):
        return
    raw_material_code = oms_material_code(raw)
    current_material_code = oms_material_code(item)
    if raw_material_code != current_material_code:
        return
    legacy.status = "Inactive"
    attrs = loads(legacy.attributes_json, {})
    attrs["oms_replaced_by_sku_id"] = canonical_sku.sku_id
    attrs["oms_deactivated_reason"] = "legacy_generated_from_oms_sku_id"
    attrs["oms_deactivated_at"] = now_utc().isoformat()
    legacy.attributes_json = dumps(attrs)
    legacy.updated_at = now_utc()

def oms_material_sync_due(session: Session, *, now: datetime | None = None) -> bool:
    if not config_bool(session, "oms_enabled", False) and not config_bool(session, "oms_mock_success", False):
        return False
    if not config_bool(session, "oms_material_sync_enabled", True):
        return False
    interval = int(config_value(session, "oms_material_sync_interval_seconds", "86400") or "86400")
    if interval <= 0:
        return False
    last_sync = config_value(session, "oms_material_last_sync_at", "").strip()
    if not last_sync:
        return True
    try:
        last_dt = datetime.fromisoformat(last_sync)
    except ValueError:
        return True
    return ((now or now_utc()) - last_dt).total_seconds() >= interval
