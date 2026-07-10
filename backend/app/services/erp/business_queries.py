from __future__ import annotations

import re
from collections import Counter
from typing import Any

from sqlalchemy import false, func, or_
from sqlalchemy.orm import Session

from backend.app.models import ProductInventorySnapshot, ProductSKU, ProductSPU, SystemConfig, now_utc
from backend.app.services.bootstrap import set_config
from backend.app.services.erp.kingdee_client import execute_bill_query_from_config, execute_bill_query_with_config, kingdee_config_from_session
from backend.app.services.jsonutil import dumps, loads


ERP_INVENTORY_FORM_ID = "STK_Inventory"
ERP_INVENTORY_FIELD_KEYS = "FMaterialId.FNumber,FMaterialId.FName,FStockId.FNumber,FStockId.FName,FBaseQty,FQty"
INVENTORY_CLASSIFICATION_RULES_CONFIG_KEY = "inventory_classification_rules_json"
NON_COUNTABLE_CATEGORY_KEYWORDS = ("耗材", "软件", "办公设备")
NON_COUNTABLE_SPEC_PATTERNS = (r"(?<![A-Za-z0-9])\d+(?:\.\d+)?\s*(?:ml|毫升|升)(?![A-Za-z0-9])",)
NON_COUNTABLE_SPEC_PATTERN = re.compile(NON_COUNTABLE_SPEC_PATTERNS[0], re.IGNORECASE)
COUNTABLE_CATEGORY_KEYWORDS = (
    "成品",
    "pcb",
    "pcb板",
    "软板",
    "pcba",
    "包装盒",
    "包装袋",
    "纸箱",
    "标签",
    "贴纸",
    "说明书",
    "镜头",
    "泡棉",
    "内衬",
    "塑胶件",
    "钣金件",
    "机加件",
    "压铸件",
    "紧固件",
    "连接器",
    "芯片",
    "二极管",
    "光电器件",
    "led",
    "电阻",
    "电容",
    "电感",
    "晶体管",
    "开关",
    "电池",
    "晶振",
    "磁铁",
    "模块",
    "仪器仪表",
    "工业相机",
    "支架",
    "三脚架",
    "云台",
    "标定板",
    "掩膜片",
    "匀光板",
    "工程箱",
    "航空箱",
    "布包",
    "布袋",
    "货架",
    "周转箱",
    "机械设备",
    "办公设备",
    "外购成品",
    "服务",
    "雕像",
    "玩偶",
    "u盘",
    "tf卡",
)
COUNTABLE_NAME_KEYWORDS = (
    "包装盒",
    "包装箱",
    "纸箱",
    "标签",
    "标贴",
    "贴纸",
    "说明书",
    "镜头",
    "相机",
    "泡棉",
    "内衬",
    "标定板",
    "匀光板",
    "面板",
    "支架",
    "固定板",
    "固定环",
    "转换环",
    "保护盖",
    "保护套",
    "包装袋",
    "pcba",
    "芯片",
    "二极管",
    "led",
    "电源适配器",
    "风扇",
    "电机",
    "舵机",
    "电脑",
    "主机",
    "工作站",
    "机柜",
    "机器狗",
    "灭火器",
    "装订机",
    "碎纸机",
    "打印机",
    "窗帘",
    "一体屏",
    "眼镜",
    "内存条",
    "喷枪",
    "胶枪",
    "胶嘴",
    "混合管",
    "混合头",
)
LENGTH_CATEGORY_KEYWORDS = ("线材", "缠绕膜")
LENGTH_NAME_KEYWORDS = ("线缆", "线束", "端子线", "排线", "导线", "护套线", "电源线", "数据线", "卷材", "缠绕膜")
LENGTH_SPEC_PATTERNS = (r"(按米|每米|(?:米|m)\s*/\s*卷|(?:米|m)\s*每卷)",)
LENGTH_SPEC_PATTERN = re.compile(LENGTH_SPEC_PATTERNS[0], re.IGNORECASE)
WEIGHT_CATEGORY_KEYWORDS = ("粉末", "胶水", "油墨", "液体", "锡丝", "润滑脂", "润滑剂", "显像剂", "增强剂", "干燥剂")
WEIGHT_NAME_KEYWORDS = ("胶水", "硅脂", "润滑脂", "润滑剂", "锡丝", "锡线", "干燥剂", "喷雾胶", "密封胶", "热熔胶", "ab胶")
WEIGHT_SPEC_PATTERNS = (
    r"(?<![A-Za-z0-9])(?:净重|重量|容量|规格|每(?:瓶|桶|盒|包|支)|\b)(?:[:：]?\s*)\d+(?:\.\d+)?\s*(?:kg|KG|ml|ML|千克|公斤|克|毫升|升)\b",
)
WEIGHT_SPEC_PATTERN = re.compile(WEIGHT_SPEC_PATTERNS[0], re.IGNORECASE)
INVENTORY_MEASURE_TYPES = {"countable", "length", "weight"}
INVENTORY_OBJECT_SCOPES = {"finished", "non_finished"}
FINISHED_CATEGORY = "成品"
MATERIAL_MIDDLE_TYPE_RAW_KEYS = (
    "material_middle_type",
    "middle_type",
    "FMaterialMiddleGroup.FName",
    "FMaterialCategory.FName",
    "FMaterialSubGroup.FName",
)
MATERIAL_NAME_SUFFIXES = (
    "左侧板",
    "右侧板",
    "保护罩",
    "保护盖",
    "固定支架",
    "固定座盖板",
    "固定座",
    "外上壳",
    "外下壳",
    "装饰件",
    "装饰圈",
    "扫描头",
    "扫描盖",
    "连接器盖",
    "电池架",
    "亚克力板",
    "导光柱",
    "硅胶垫",
    "密封环",
    "密封圈",
    "橡胶圈",
    "胶圈",
    "面板",
    "底座",
    "底壳",
    "上壳",
    "下壳",
    "左壳",
    "右壳",
    "前盖",
    "后盖",
    "左盖",
    "右盖",
    "顶盖",
    "底盖",
    "面盖",
    "盖板",
    "转盘",
    "盘面",
    "按键组",
    "按键",
    "支架",
    "把手",
    "手柄",
)


def default_inventory_classification_rules() -> dict[str, Any]:
    return {
        "version": 1,
        "finished_categories": [FINISHED_CATEGORY],
        "countable_category_keywords": list(COUNTABLE_CATEGORY_KEYWORDS),
        "countable_name_keywords": list(COUNTABLE_NAME_KEYWORDS),
        "length_category_keywords": list(LENGTH_CATEGORY_KEYWORDS),
        "length_name_keywords": list(LENGTH_NAME_KEYWORDS),
        "length_spec_patterns": list(LENGTH_SPEC_PATTERNS),
        "weight_category_keywords": list(WEIGHT_CATEGORY_KEYWORDS),
        "weight_name_keywords": list(WEIGHT_NAME_KEYWORDS),
        "weight_spec_patterns": list(WEIGHT_SPEC_PATTERNS),
        "other_category_keywords": list(NON_COUNTABLE_CATEGORY_KEYWORDS),
        "other_spec_patterns": list(NON_COUNTABLE_SPEC_PATTERNS),
    }


def normalize_inventory_classification_rules(raw: dict[str, Any] | None = None) -> dict[str, Any]:
    rules = default_inventory_classification_rules()
    if not isinstance(raw, dict):
        return rules
    if isinstance(raw.get("version"), int):
        rules["version"] = raw["version"]
    for key, value in raw.items():
        if key not in rules or key == "version":
            continue
        if isinstance(value, list):
            cleaned = [str(item).strip() for item in value if str(item or "").strip()]
            rules[key] = cleaned
    return rules


def inventory_classification_rules(session: Session | None = None) -> dict[str, Any]:
    if session is None:
        return default_inventory_classification_rules()
    row = session.get(SystemConfig, INVENTORY_CLASSIFICATION_RULES_CONFIG_KEY)
    if row is None or not str(row.value or "").strip():
        return default_inventory_classification_rules()
    return normalize_inventory_classification_rules(loads(row.value, {}))


def save_inventory_classification_rules(session: Session, rules: dict[str, Any]) -> dict[str, Any]:
    normalized = normalize_inventory_classification_rules(rules)
    set_config(session, INVENTORY_CLASSIFICATION_RULES_CONFIG_KEY, dumps(normalized), is_secret=False)
    return normalized


def search_materials(session: Session, *, q: str = "", limit: int = 20, include_erp: bool = False) -> dict[str, Any]:
    limit = max(1, min(int(limit or 20), 100))
    query = session.query(ProductSPU)
    text = (q or "").strip()
    if text:
        pattern = f"%{text}%"
        query = query.filter(or_(ProductSPU.spu_id.ilike(pattern), ProductSPU.name.ilike(pattern), ProductSPU.category.ilike(pattern)))
    rows = query.order_by(ProductSPU.updated_at.desc()).limit(limit).all()
    items = [serialize_material_from_spu(row) for row in rows]
    result: dict[str, Any] = {"ok": True, "source": "local", "items": items, "total": len(items)}
    if include_erp and text:
        erp_result = execute_bill_query_from_config(
            session,
            form_id="BD_MATERIAL",
            field_keys="FNumber,FName,FSpecification,FMaterialGroup.FName,FForbidStatus",
            filter_string=f"FNumber like '%{escape_filter_value(text)}%' or FName like '%{escape_filter_value(text)}%'",
            limit=limit,
        )
        result["erp"] = erp_result
    return result


def query_inventory(
    session: Session,
    *,
    material_code: str = "",
    warehouse_code: str = "",
    limit: int = 50,
) -> dict[str, Any]:
    filters: list[str] = []
    if material_code.strip():
        filters.append(f"FMaterialId.FNumber = '{escape_filter_value(material_code.strip())}'")
    if warehouse_code.strip():
        filters.append(f"FStockId.FNumber = '{escape_filter_value(warehouse_code.strip())}'")
    filter_string = " and ".join(filters)
    erp_result = execute_bill_query_from_config(
        session,
        form_id=ERP_INVENTORY_FORM_ID,
        field_keys=ERP_INVENTORY_FIELD_KEYS,
        filter_string=filter_string,
        limit=max(1, min(int(limit or 50), 200)),
    )
    items = [inventory_item_from_row(row) for row in erp_result.get("items", [])]
    return {
        "ok": erp_result.get("ok", False),
        "message": erp_result.get("message", ""),
        "source": "erp",
        "form_id": ERP_INVENTORY_FORM_ID,
        "field_keys": ERP_INVENTORY_FIELD_KEYS,
        "filter_string": filter_string,
        "items": items,
        "total": len(items),
        "elapsed_ms": erp_result.get("elapsed_ms"),
        "error_type": erp_result.get("error_type", ""),
    }


def sync_inventory_snapshots(session: Session, *, batch_size: int = 500, max_batches: int = 200) -> dict[str, Any]:
    config = kingdee_config_from_session(session)
    start_row = 0
    synced_at = now_utc()
    aggregated: dict[tuple[str, str], dict[str, Any]] = {}

    # 仅同步国内仓库存（排除 106 开头的海外仓）
    filter_string = "FStockId.FNumber NOT LIKE '106%'"

    for _ in range(max_batches):
        result = execute_bill_query_with_config(
            config,
            form_id=ERP_INVENTORY_FORM_ID,
            field_keys=ERP_INVENTORY_FIELD_KEYS,
            filter_string=filter_string,
            limit=batch_size,
            start_row=start_row,
        )
        if not result.get("ok"):
            raise RuntimeError(result.get("message") or "ERP 库存查询失败")
        rows = result.get("items") or []
        if not rows:
            break
        for row in rows:
            item = inventory_item_from_row(row)
            material_code = str(item["material_code"] or "").strip()
            warehouse_code = str(item["warehouse_code"] or "").strip()
            if not material_code or not warehouse_code:
                continue
            key = (material_code, warehouse_code)
            current = aggregated.setdefault(
                key,
                {
                    **item,
                    "base_qty": 0.0,
                    "qty": 0.0,
                    "raw_rows": [],
                },
            )
            current["base_qty"] += item["base_qty"]
            current["qty"] += item["qty"]
            current["raw_rows"].append(row)
        if len(rows) < batch_size:
            break
        start_row += len(rows)

    created = updated = 0
    for (material_code, warehouse_code), item in aggregated.items():
        snapshot = (
            session.query(ProductInventorySnapshot)
            .filter_by(material_code=material_code, warehouse_code=warehouse_code)
            .one_or_none()
        )
        if snapshot is None:
            snapshot = ProductInventorySnapshot(material_code=material_code, warehouse_code=warehouse_code)
            session.add(snapshot)
            created += 1
        else:
            updated += 1
        snapshot.material_name = str(item.get("material_name") or material_code)
        snapshot.warehouse_name = str(item.get("warehouse_name") or warehouse_code)
        snapshot.base_qty = float(item.get("base_qty") or 0)
        snapshot.qty = float(item.get("qty") or 0)
        snapshot.source_payload_json = dumps({"rows": item.get("raw_rows", [])})
        snapshot.status = "Active"
        snapshot.synced_at = synced_at
        snapshot.updated_at = synced_at

    set_config(session, "erp_inventory_last_sync_at", synced_at.isoformat(), is_secret=False)
    session.flush()
    return {
        "ok": True,
        "total": len(aggregated),
        "created": created,
        "updated": updated,
        "last_sync_at": synced_at.isoformat(),
    }


def list_inventory_snapshots(
    session: Session,
    *,
    q: str = "",
    material_code: str = "",
    warehouse_code: str = "",
    low_stock_only: bool = False,
    countable_only: bool = True,
    measure_type: str = "",
    inventory_scope: str = "",
    threshold: float = 1,
    page: int = 1,
    page_size: int = 20,
) -> dict[str, Any]:
    query = session.query(ProductInventorySnapshot).outerjoin(
        ProductSPU,
        ProductSPU.spu_id == ProductInventorySnapshot.material_code,
    )
    query = apply_inventory_object_scope_filter(query, inventory_scope=inventory_scope)
    query = apply_inventory_scope_filter(query, countable_only=countable_only, measure_type=measure_type)
    if q.strip():
        pattern = f"%{q.strip()}%"
        query = query.filter(
            or_(
                ProductInventorySnapshot.material_code.ilike(pattern),
                ProductInventorySnapshot.material_name.ilike(pattern),
                ProductInventorySnapshot.warehouse_code.ilike(pattern),
                ProductInventorySnapshot.warehouse_name.ilike(pattern),
                ProductSPU.category.ilike(pattern),
                ProductSPU.product_type.ilike(pattern),
                ProductSPU.name.ilike(pattern),
            )
        )
    if material_code.strip():
        query = query.filter(ProductInventorySnapshot.material_code.ilike(f"%{material_code.strip()}%"))
    if warehouse_code.strip():
        query = query.filter(ProductInventorySnapshot.warehouse_code.ilike(f"%{warehouse_code.strip()}%"))
    if low_stock_only:
        query = query.filter(ProductInventorySnapshot.base_qty <= threshold)

    total = query.order_by(None).count()
    total_pages = max(1, (total + page_size - 1) // page_size)
    page = max(1, min(page, total_pages))
    rows = (
        query.order_by(ProductInventorySnapshot.base_qty.asc(), ProductInventorySnapshot.material_code.asc())
        .offset((page - 1) * page_size)
        .limit(page_size)
        .all()
    )
    summary = inventory_summary(
        session,
        q=q,
        material_code=material_code,
        warehouse_code=warehouse_code,
        low_stock_only=low_stock_only,
        threshold=threshold,
        countable_only=countable_only,
        measure_type=measure_type,
        inventory_scope=inventory_scope,
    )
    return {
        "items": [serialize_inventory_snapshot(row, threshold=threshold) for row in rows],
        "page": page,
        "page_size": page_size,
        "total": total,
        "total_pages": total_pages,
        "summary": summary,
    }


def list_inventory_type_summary(
    session: Session,
    *,
    q: str = "",
    warehouse_code: str = "",
    low_stock_only: bool = False,
    countable_only: bool = True,
    measure_type: str = "",
    inventory_scope: str = "",
    threshold: float = 1,
    page: int = 1,
    page_size: int = 20,
) -> dict[str, Any]:
    query = session.query(ProductInventorySnapshot, ProductSPU).join(
        ProductSPU,
        ProductSPU.spu_id == ProductInventorySnapshot.material_code,
    )
    query = apply_inventory_object_scope_filter(query, inventory_scope=inventory_scope)
    query = apply_inventory_scope_filter(query, countable_only=countable_only, measure_type=measure_type)
    if q.strip():
        pattern = f"%{q.strip()}%"
        query = query.filter(
            or_(
                ProductSPU.category.ilike(pattern),
                ProductSPU.product_type.ilike(pattern),
                ProductSPU.name.ilike(pattern),
                ProductInventorySnapshot.warehouse_code.ilike(pattern),
                ProductInventorySnapshot.warehouse_name.ilike(pattern),
            )
        )
    if warehouse_code.strip():
        pattern = f"%{warehouse_code.strip()}%"
        query = query.filter(or_(ProductInventorySnapshot.warehouse_code.ilike(pattern), ProductInventorySnapshot.warehouse_name.ilike(pattern)))
    if low_stock_only:
        query = query.filter(ProductInventorySnapshot.base_qty <= threshold)

    buckets: dict[tuple[str, str], dict[str, Any]] = {}
    for snapshot, spu in query.all():
        category = (spu.category or "未分类").strip() or "未分类"
        middle_type = material_middle_type(spu)
        bucket_key = (category, middle_type)
        bucket = buckets.setdefault(
            bucket_key,
            {
                "material_type": middle_type,
                "parent_category": category,
                "material_codes": set(),
                "warehouse_codes": set(),
                "sample_materials": [],
                "base_qty": 0.0,
                "qty": 0.0,
                "zero_stock_count": 0,
                "low_stock_count": 0,
                "row_count": 0,
                "latest_synced_at": None,
            },
        )
        bucket["material_codes"].add(snapshot.material_code)
        bucket["warehouse_codes"].add(snapshot.warehouse_code)
        if len(bucket["sample_materials"]) < 3 and snapshot.material_name not in bucket["sample_materials"]:
            bucket["sample_materials"].append(snapshot.material_name)
        bucket["base_qty"] += float(snapshot.base_qty or 0)
        bucket["qty"] += float(snapshot.qty or 0)
        bucket["row_count"] += 1
        if snapshot.base_qty <= 0:
            bucket["zero_stock_count"] += 1
        if snapshot.base_qty <= threshold:
            bucket["low_stock_count"] += 1
        if bucket["latest_synced_at"] is None or snapshot.synced_at > bucket["latest_synced_at"]:
            bucket["latest_synced_at"] = snapshot.synced_at

    items = [serialize_inventory_type_bucket(bucket, threshold=threshold) for bucket in buckets.values()]
    items.sort(key=lambda row: (-row["low_stock_count"], row["parent_category"], row["material_type"]))
    total = len(items)
    total_pages = max(1, (total + page_size - 1) // page_size)
    page = max(1, min(page, total_pages))
    page_items = items[(page - 1) * page_size : page * page_size]
    return {
        "items": page_items,
        "page": page,
        "page_size": page_size,
        "total": total,
        "total_pages": total_pages,
        "summary": inventory_summary(
            session,
            q=q,
            warehouse_code=warehouse_code,
            low_stock_only=low_stock_only,
            threshold=threshold,
            countable_only=countable_only,
            measure_type=measure_type,
            inventory_scope=inventory_scope,
        ),
    }


def list_inventory_type_items(
    session: Session,
    *,
    material_type: str,
    parent_category: str = "",
    q: str = "",
    warehouse_code: str = "",
    stock_status: str = "",
    low_stock_only: bool = False,
    countable_only: bool = True,
    measure_type: str = "",
    inventory_scope: str = "",
    threshold: float = 1,
    page: int = 1,
    page_size: int = 100,
) -> dict[str, Any]:
    target_type = str(material_type or "").strip()
    target_category = str(parent_category or "").strip()
    query = session.query(ProductInventorySnapshot, ProductSPU).join(
        ProductSPU,
        ProductSPU.spu_id == ProductInventorySnapshot.material_code,
    )
    query = apply_inventory_object_scope_filter(query, inventory_scope=inventory_scope)
    query = apply_inventory_scope_filter(query, countable_only=countable_only, measure_type=measure_type)
    if target_category:
        query = query.filter(ProductSPU.category == target_category)
    if q.strip():
        pattern = f"%{q.strip()}%"
        query = query.filter(
            or_(
                ProductInventorySnapshot.material_code.ilike(pattern),
                ProductInventorySnapshot.material_name.ilike(pattern),
                ProductInventorySnapshot.warehouse_code.ilike(pattern),
                ProductInventorySnapshot.warehouse_name.ilike(pattern),
                ProductSPU.name.ilike(pattern),
            )
        )
    if warehouse_code.strip():
        pattern = f"%{warehouse_code.strip()}%"
        query = query.filter(or_(ProductInventorySnapshot.warehouse_code.ilike(pattern), ProductInventorySnapshot.warehouse_name.ilike(pattern)))
    status_filter = str(stock_status or "").strip().lower()
    if status_filter == "zero":
        query = query.filter(ProductInventorySnapshot.base_qty <= 0)
    elif status_filter == "low" or low_stock_only:
        query = query.filter(ProductInventorySnapshot.base_qty <= threshold)

    matched: list[tuple[ProductInventorySnapshot, ProductSPU]] = []
    for snapshot, spu in query.all():
        if material_middle_type(spu) == target_type:
            matched.append((snapshot, spu))

    matched.sort(key=lambda pair: (-float(pair[0].base_qty or 0), pair[0].material_code, pair[0].warehouse_name, pair[0].warehouse_code))
    total = len(matched)
    total_pages = max(1, (total + page_size - 1) // page_size)
    page = max(1, min(page, total_pages))
    page_items = matched[(page - 1) * page_size : page * page_size]
    total_base_qty = sum(float(snapshot.base_qty or 0) for snapshot, _ in matched)
    total_qty = sum(float(snapshot.qty or 0) for snapshot, _ in matched)
    material_codes = {snapshot.material_code for snapshot, _ in matched}
    warehouse_codes = {snapshot.warehouse_code for snapshot, _ in matched}
    zero_count = sum(1 for snapshot, _ in matched if snapshot.base_qty <= 0)
    low_count = sum(1 for snapshot, _ in matched if snapshot.base_qty <= threshold)
    return {
        "material_type": target_type,
        "parent_category": target_category,
        "items": [
            {
                **serialize_inventory_snapshot(snapshot, threshold=threshold),
                "parent_category": (spu.category or "未分类").strip() or "未分类",
                "material_type": material_middle_type(spu),
            }
            for snapshot, spu in page_items
        ],
        "summary": {
            "inventory_row_count": total,
            "material_count": len(material_codes),
            "warehouse_count": len(warehouse_codes),
            "base_qty": round(total_base_qty, 3),
            "qty": round(total_qty, 3),
            "zero_stock_count": zero_count,
            "low_stock_count": low_count,
        },
        "page": page,
        "page_size": page_size,
        "total": total,
        "total_pages": total_pages,
    }


def inventory_summary(
    session: Session,
    *,
    q: str = "",
    material_code: str = "",
    warehouse_code: str = "",
    low_stock_only: bool = False,
    threshold: float = 1,
    countable_only: bool = True,
    measure_type: str = "",
    inventory_scope: str = "",
) -> dict[str, Any]:
    base_query = session.query(ProductInventorySnapshot).outerjoin(
        ProductSPU,
        ProductSPU.spu_id == ProductInventorySnapshot.material_code,
    )
    base_query = apply_inventory_object_scope_filter(base_query, inventory_scope=inventory_scope)
    base_query = apply_inventory_scope_filter(base_query, countable_only=countable_only, measure_type=measure_type)
    if q.strip():
        pattern = f"%{q.strip()}%"
        base_query = base_query.filter(
            or_(
                ProductInventorySnapshot.material_code.ilike(pattern),
                ProductInventorySnapshot.material_name.ilike(pattern),
                ProductInventorySnapshot.warehouse_code.ilike(pattern),
                ProductInventorySnapshot.warehouse_name.ilike(pattern),
                ProductSPU.category.ilike(pattern),
                ProductSPU.product_type.ilike(pattern),
                ProductSPU.name.ilike(pattern),
            )
        )
    if material_code.strip():
        base_query = base_query.filter(ProductInventorySnapshot.material_code.ilike(f"%{material_code.strip()}%"))
    if warehouse_code.strip():
        pattern = f"%{warehouse_code.strip()}%"
        base_query = base_query.filter(or_(ProductInventorySnapshot.warehouse_code.ilike(pattern), ProductInventorySnapshot.warehouse_name.ilike(pattern)))
    if low_stock_only:
        base_query = base_query.filter(ProductInventorySnapshot.base_qty <= threshold)
    total_rows = base_query.count()
    zero_stock = base_query.filter(ProductInventorySnapshot.base_qty <= 0).count()
    low_stock = base_query.filter(ProductInventorySnapshot.base_qty <= threshold).count()
    total_base_qty = base_query.with_entities(func.coalesce(func.sum(ProductInventorySnapshot.base_qty), 0)).scalar() or 0
    warehouses = (
        base_query.with_entities(
            ProductInventorySnapshot.warehouse_code,
            ProductInventorySnapshot.warehouse_name,
            func.count(ProductInventorySnapshot.id),
            func.coalesce(func.sum(ProductInventorySnapshot.base_qty), 0),
        )
        .group_by(ProductInventorySnapshot.warehouse_code, ProductInventorySnapshot.warehouse_name)
        .order_by(func.count(ProductInventorySnapshot.id).desc())
        .limit(10)
        .all()
    )
    return {
        "total_rows": total_rows,
        "zero_stock_count": zero_stock,
        "low_stock_count": low_stock,
        "total_base_qty": round(float(total_base_qty), 3),
        "countable_only": countable_only,
        "measure_type": normalize_inventory_measure_type(measure_type, countable_only=countable_only),
        "inventory_scope": normalize_inventory_object_scope(inventory_scope),
        "warehouses": [
            {"warehouse_code": row[0], "warehouse_name": row[1], "material_count": row[2], "base_qty": round(float(row[3] or 0), 3)}
            for row in warehouses
        ],
    }


def list_inventory_warehouses(session: Session, *, q: str = "", limit: int = 30) -> dict[str, Any]:
    limit = max(1, min(int(limit or 30), 100))
    query = session.query(
        ProductInventorySnapshot.warehouse_code,
        ProductInventorySnapshot.warehouse_name,
        func.count(ProductInventorySnapshot.id).label("inventory_row_count"),
        func.coalesce(func.sum(ProductInventorySnapshot.base_qty), 0).label("base_qty"),
        func.max(ProductInventorySnapshot.synced_at).label("synced_at"),
    )
    text = str(q or "").strip()
    if text:
        pattern = f"%{text}%"
        query = query.filter(or_(ProductInventorySnapshot.warehouse_code.ilike(pattern), ProductInventorySnapshot.warehouse_name.ilike(pattern)))
    rows = (
        query.group_by(ProductInventorySnapshot.warehouse_code, ProductInventorySnapshot.warehouse_name)
        .order_by(func.count(ProductInventorySnapshot.id).desc(), ProductInventorySnapshot.warehouse_code.asc())
        .limit(limit)
        .all()
    )
    items = [
        {
            "warehouse_code": row[0],
            "warehouse_name": row[1],
            "inventory_row_count": row[2],
            "base_qty": round(float(row[3] or 0), 3),
            "synced_at": row[4].isoformat() if row[4] else "",
            "label": f"{row[0]} · {row[1]}" if row[1] and row[1] != row[0] else str(row[0]),
        }
        for row in rows
    ]
    return {"items": items, "total": len(items), "q": text}


def inventory_classification_diagnostics(session: Session, *, sample_limit: int = 20) -> dict[str, Any]:
    rules = inventory_classification_rules(session)
    finished_categories = set(rules.get("finished_categories") or [FINISHED_CATEGORY])
    rows = (
        session.query(ProductInventorySnapshot, ProductSPU)
        .join(ProductSPU, ProductSPU.spu_id == ProductInventorySnapshot.material_code)
        .all()
    )
    scope_counts: Counter[str] = Counter()
    measure_counts: dict[str, Counter[str]] = {"finished": Counter(), "non_finished": Counter()}
    reason_counts: Counter[str] = Counter()
    suspicious: list[dict[str, Any]] = []
    suspicious_terms = ("pcb", "pcb板", "镜头", "包装盒", "包装箱", "标签", "贴纸", "说明书", "晶振", "配重铁")

    for snapshot, spu in rows:
        scope = "finished" if str(spu.category or "").strip() in finished_categories else "non_finished"
        scope_counts[scope] += 1
        result = classify_inventory_material(spu.category, spu.extended_info_json, name=spu.name, rules=rules)
        measure_type = result["measure_type"]
        measure_counts[scope][measure_type] += 1
        reason_counts[f"{measure_type}:{result['reason']}"] += 1
        haystack = f"{spu.name} {spu.category or ''}".lower()
        if (
            scope == "non_finished"
            and measure_type in {"length", "weight", "other"}
            and any(term.lower() in haystack for term in suspicious_terms)
            and len(suspicious) < sample_limit
        ):
            suspicious.append(
                {
                    "material_code": snapshot.material_code,
                    "material_name": spu.name,
                    "category": spu.category,
                    "measure_type": measure_type,
                    "reason": result["reason"],
                    "matched": result["matched"],
                }
            )

    return {
        "rules": rules,
        "summary": {
            "total_inventory_rows": len(rows),
            "scope_counts": dict(scope_counts),
            "measure_counts": {key: dict(value) for key, value in measure_counts.items()},
            "reason_counts": dict(reason_counts),
            "suspicious_sample_count": len(suspicious),
        },
        "suspicious_samples": suspicious,
    }


def serialize_inventory_type_bucket(bucket: dict[str, Any], *, threshold: float = 1) -> dict[str, Any]:
    base_qty = round(float(bucket["base_qty"] or 0), 3)
    if bucket["zero_stock_count"] > 0:
        alert_level = "zero"
    elif bucket["low_stock_count"] > 0:
        alert_level = "low"
    else:
        alert_level = "ok"
    return {
        "material_type": bucket["material_type"],
        "parent_category": bucket["parent_category"],
        "material_count": len(bucket["material_codes"]),
        "warehouse_count": len(bucket["warehouse_codes"]),
        "inventory_row_count": bucket["row_count"],
        "sample_materials": bucket["sample_materials"],
        "base_qty": base_qty,
        "qty": round(float(bucket["qty"] or 0), 3),
        "zero_stock_count": bucket["zero_stock_count"],
        "low_stock_count": bucket["low_stock_count"],
        "alert_level": alert_level,
        "synced_at": bucket["latest_synced_at"].isoformat() if bucket["latest_synced_at"] else "",
    }


def material_middle_type(spu: ProductSPU) -> str:
    if spu.product_type and spu.product_type.strip():
        return spu.product_type.strip()
    erp = loads(spu.extended_info_json, {}).get("erp", {})
    for key in MATERIAL_MIDDLE_TYPE_RAW_KEYS:
        value = erp.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    raw = erp.get("raw", {})
    if isinstance(raw, dict):
        for key in MATERIAL_MIDDLE_TYPE_RAW_KEYS:
            value = raw.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()
    return derive_material_middle_type(spu.name, spu.category, spu.spu_id)


def derive_material_middle_type(name: str, category: str | None = None, code: str | None = None) -> str:
    text = normalize_material_name(name)
    if not text:
        return (category or code or "未分类").strip() or "未分类"

    for delimiter in ("-", "－", "—", "_", "/"):
        if delimiter in text:
            prefix = text.split(delimiter, 1)[0].strip()
            if delimiter in {"-", "－", "—"} and re.fullmatch(r"[A-Za-z0-9+.\s]+", prefix):
                continue
            if len(prefix) >= 2:
                return prefix

    latin_match = re.match(r"^[A-Za-z][A-Za-z0-9+.\-]*(?:\s+[A-Za-z][A-Za-z0-9+.\-]*)*", text)
    if latin_match:
        prefix = latin_match.group(0).strip()
        if len(prefix) >= 2 and len(prefix) < len(text):
            return prefix

    for suffix in sorted(MATERIAL_NAME_SUFFIXES, key=len, reverse=True):
        if text.endswith(suffix):
            prefix = text[: -len(suffix)].strip()
            if len(prefix) >= 2:
                return prefix
    return text


def normalize_material_name(name: str) -> str:
    text = str(name or "").strip()
    text = re.sub(r"[（(]\s*(停用|禁用|作废)\s*[）)]", "", text)
    text = re.sub(r"\s+", " ", text)
    return text.strip()


def serialize_inventory_snapshot(row: ProductInventorySnapshot, *, threshold: float = 1) -> dict[str, Any]:
    import json
    import datetime
    if row.base_qty <= 0:
        alert_level = "zero"
    elif row.base_qty <= threshold:
        alert_level = "low"
    else:
        alert_level = "ok"

    in_transit_qty = 0.0
    warning_status = "正常"
    model = ""
    english_name = ""
    
    if row.source_payload_json:
        try:
            payload = json.loads(row.source_payload_json)
            in_transit_qty = payload.get("in_transit_qty") or 0.0
            warning_status = payload.get("warning_status") or "正常"
            model = payload.get("model") or ""
            english_name = payload.get("english_name") or ""
        except Exception:
            pass

    # Try to load English Name from SPU/SKU master data
    from sqlalchemy.orm import object_session
    from backend.app.models import ProductSKU, ProductSPU
    session = object_session(row)
    if session:
        try:
            sku = session.query(ProductSKU).filter(ProductSKU.sku_id == row.material_code).first()
            if sku:
                attrs = json.loads(sku.attributes_json) if sku.attributes_json else {}
                db_en = attrs.get("oms_en_name") or ""
                if db_en:
                    english_name = db_en
            if not english_name:
                spu = session.query(ProductSPU).filter(ProductSPU.spu_id == row.material_code).first()
                if spu and spu.name_en:
                    english_name = spu.name_en
        except Exception:
            pass

    synced_at_dt = row.synced_at
    if synced_at_dt.tzinfo is None:
        synced_at_dt = synced_at_dt.replace(tzinfo=datetime.timezone.utc)
    updated_at_dt = row.updated_at
    if updated_at_dt.tzinfo is None:
        updated_at_dt = updated_at_dt.replace(tzinfo=datetime.timezone.utc)

    return {
        "id": row.id,
        "material_code": row.material_code,
        "material_name": row.material_name,
        "english_name": english_name,
        "warehouse_code": row.warehouse_code,
        "warehouse_name": row.warehouse_name,
        "base_qty": row.base_qty,
        "qty": row.qty,
        "in_transit_qty": in_transit_qty,
        "warning_status": warning_status,
        "model": model,
        "alert_level": alert_level,
        "synced_at": synced_at_dt.isoformat(),
        "updated_at": updated_at_dt.isoformat(),
    }


def apply_countable_inventory_filter(query):
    excluded_codes = non_countable_material_codes(query.session)
    if not excluded_codes:
        return query
    return query.filter(ProductInventorySnapshot.material_code.not_in(excluded_codes))


def apply_inventory_scope_filter(query, *, countable_only: bool = True, measure_type: str = ""):
    normalized_type = normalize_inventory_measure_type(measure_type, countable_only=countable_only)
    if not normalized_type:
        return query
    codes = material_codes_by_measure_type(query.session, normalized_type)
    if not codes:
        return query.filter(false())
    return query.filter(ProductInventorySnapshot.material_code.in_(codes))


def apply_inventory_object_scope_filter(query, *, inventory_scope: str = ""):
    normalized_scope = normalize_inventory_object_scope(inventory_scope)
    if not normalized_scope:
        return query
    codes = material_codes_by_inventory_object_scope(query.session, normalized_scope)
    if not codes:
        return query.filter(false())
    return query.filter(ProductInventorySnapshot.material_code.in_(codes))


def normalize_inventory_measure_type(measure_type: str = "", *, countable_only: bool = True) -> str:
    value = str(measure_type or "").strip().lower()
    if value in INVENTORY_MEASURE_TYPES:
        return value
    if countable_only:
        return "countable"
    return ""


def normalize_inventory_object_scope(inventory_scope: str = "") -> str:
    value = str(inventory_scope or "").strip().lower()
    return value if value in INVENTORY_OBJECT_SCOPES else ""


def material_codes_by_measure_type(session: Session, measure_type: str) -> list[str]:
    normalized_type = normalize_inventory_measure_type(measure_type)
    rules = inventory_classification_rules(session)
    rows = session.query(ProductSPU.spu_id, ProductSPU.name, ProductSPU.category, ProductSPU.extended_info_json).all()
    codes: list[str] = []
    for code, name, category, info_json in rows:
        if inventory_material_measure_type(category, info_json, name=name, rules=rules) == normalized_type:
            codes.append(code)
    return codes


def material_codes_by_inventory_object_scope(session: Session, inventory_scope: str) -> list[str]:
    normalized_scope = normalize_inventory_object_scope(inventory_scope)
    rules = inventory_classification_rules(session)
    finished_categories = set(rules.get("finished_categories") or [FINISHED_CATEGORY])
    rows = session.query(ProductSPU.spu_id, ProductSPU.category).all()
    codes: list[str] = []
    for code, category in rows:
        is_finished = str(category or "").strip() in finished_categories
        if normalized_scope == "finished" and is_finished:
            codes.append(code)
        elif normalized_scope == "non_finished" and not is_finished:
            codes.append(code)
    return codes


def non_countable_material_codes(session: Session) -> list[str]:
    rules = inventory_classification_rules(session)
    rows = session.query(ProductSPU.spu_id, ProductSPU.category, ProductSPU.extended_info_json).all()
    codes: list[str] = []
    for code, category, info_json in rows:
        erp = loads(info_json, {}).get("erp", {})
        spec = str(erp.get("specification") or "")
        category_text = str(category or "")
        if is_non_countable_material(category_text, spec, rules=rules):
            codes.append(code)
    return codes


def inventory_material_measure_type(
    category: str | None,
    info_json: str,
    *,
    name: str | None = None,
    rules: dict[str, Any] | None = None,
) -> str:
    return classify_inventory_material(category, info_json, name=name, rules=rules)["measure_type"]


def classify_inventory_material(
    category: str | None,
    info_json: str,
    *,
    name: str | None = None,
    rules: dict[str, Any] | None = None,
) -> dict[str, Any]:
    active_rules = normalize_inventory_classification_rules(rules)
    erp = loads(info_json, {}).get("erp", {})
    spec = str(erp.get("specification") or "")
    category_text = str(category or "")
    name_text = str(name or erp.get("name") or "")
    matched = match_keyword(category_text, active_rules.get("countable_category_keywords", []))
    if matched:
        return {"measure_type": "countable", "reason": "countable_category_keyword", "matched": matched}
    matched = match_keyword(name_text, active_rules.get("countable_name_keywords", []))
    if matched:
        return {"measure_type": "countable", "reason": "countable_name_keyword", "matched": matched}
    matched = match_keyword(category_text, active_rules.get("length_category_keywords", []))
    if matched:
        return {"measure_type": "length", "reason": "length_category_keyword", "matched": matched}
    matched = match_keyword(name_text, active_rules.get("length_name_keywords", []))
    if matched:
        return {"measure_type": "length", "reason": "length_name_keyword", "matched": matched}
    matched = match_pattern(spec, active_rules.get("length_spec_patterns", []))
    if matched:
        return {"measure_type": "length", "reason": "length_spec_pattern", "matched": matched}
    matched = match_keyword(category_text, active_rules.get("weight_category_keywords", []))
    if matched:
        return {"measure_type": "weight", "reason": "weight_category_keyword", "matched": matched}
    matched = match_keyword(name_text, active_rules.get("weight_name_keywords", []))
    if matched:
        return {"measure_type": "weight", "reason": "weight_name_keyword", "matched": matched}
    matched = match_pattern(spec, active_rules.get("weight_spec_patterns", []))
    if matched:
        return {"measure_type": "weight", "reason": "weight_spec_pattern", "matched": matched}
    matched = match_keyword(category_text, active_rules.get("other_category_keywords", []))
    if matched:
        return {"measure_type": "other", "reason": "other_category_keyword", "matched": matched}
    matched = match_pattern(spec, active_rules.get("other_spec_patterns", []))
    if matched:
        return {"measure_type": "other", "reason": "other_spec_pattern", "matched": matched}
    return {"measure_type": "countable", "reason": "default_countable", "matched": ""}


def match_keyword(text: str, keywords: list[str] | tuple[str, ...]) -> str:
    lowered = str(text or "").lower()
    for keyword in keywords:
        normalized = str(keyword or "").strip().lower()
        if normalized and normalized in lowered:
            return str(keyword)
    return ""


def match_pattern(text: str, patterns: list[str] | tuple[str, ...]) -> str:
    source = str(text or "")
    for pattern in patterns:
        raw = str(pattern or "").strip()
        if not raw:
            continue
        try:
            if re.search(raw, source, re.IGNORECASE):
                return raw
        except re.error:
            continue
    return ""


def is_always_countable_material(category: str, name: str = "", *, rules: dict[str, Any] | None = None) -> bool:
    active_rules = normalize_inventory_classification_rules(rules)
    category_text = str(category or "").lower()
    name_text = str(name or "").lower()
    return any(keyword.lower() in category_text for keyword in active_rules["countable_category_keywords"]) or any(
        keyword.lower() in name_text for keyword in active_rules["countable_name_keywords"]
    )


def is_length_material(category: str, specification: str, name: str = "", *, rules: dict[str, Any] | None = None) -> bool:
    active_rules = normalize_inventory_classification_rules(rules)
    category_text = str(category or "").lower()
    name_text = str(name or "").lower()
    return (
        any(keyword.lower() in category_text for keyword in active_rules["length_category_keywords"])
        or any(keyword.lower() in name_text for keyword in active_rules["length_name_keywords"])
        or bool(match_pattern(specification, active_rules["length_spec_patterns"]))
    )


def is_weight_material(category: str, specification: str, name: str = "", *, rules: dict[str, Any] | None = None) -> bool:
    active_rules = normalize_inventory_classification_rules(rules)
    category_text = str(category or "").lower()
    name_text = str(name or "").lower()
    has_weight_hint = any(keyword.lower() in category_text for keyword in active_rules["weight_category_keywords"]) or any(
        keyword.lower() in name_text for keyword in active_rules["weight_name_keywords"]
    )
    return has_weight_hint or bool(match_pattern(specification, active_rules["weight_spec_patterns"]))


def is_non_countable_material(category: str, specification: str, *, rules: dict[str, Any] | None = None) -> bool:
    active_rules = normalize_inventory_classification_rules(rules)
    category_text = str(category or "").lower()
    if any(keyword.lower() in category_text for keyword in active_rules["other_category_keywords"]):
        return True
    return bool(match_pattern(specification, active_rules["other_spec_patterns"]))


def serialize_material_from_spu(spu: ProductSPU) -> dict[str, Any]:
    erp = loads(spu.extended_info_json, {}).get("erp", {})
    sku = spu.skus[0] if spu.skus else None
    sku_attrs = loads(sku.attributes_json, {}) if isinstance(sku, ProductSKU) else {}
    return {
        "material_code": spu.spu_id,
        "material_name": spu.name,
        "category": spu.category,
        "status": spu.status,
        "specification": erp.get("specification") or sku_attrs.get("erp_specification") or "",
        "sku_id": sku.sku_id if sku else "",
        "updated_at": spu.updated_at.isoformat(),
        "erp_synced_at": erp.get("synced_at") or sku_attrs.get("erp_synced_at") or "",
    }


def inventory_item_from_row(row: Any) -> dict[str, Any]:
    values = row if isinstance(row, list) else [row]
    return {
        "material_code": value_at(values, 0),
        "material_name": value_at(values, 1),
        "warehouse_code": value_at(values, 2),
        "warehouse_name": value_at(values, 3),
        "base_qty": numeric_value(value_at(values, 4)),
        "qty": numeric_value(value_at(values, 5)),
    }


def value_at(values: list[Any], index: int) -> Any:
    return values[index] if index < len(values) else None


def numeric_value(value: Any) -> float:
    try:
        return float(value or 0)
    except (TypeError, ValueError):
        return 0.0


def escape_filter_value(value: str) -> str:
    return str(value or "").replace("'", "''")
