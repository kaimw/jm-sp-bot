from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy.orm import Session

from backend.app.models import ProductSKU, ProductSPU, SystemConfig, now_utc
from backend.app.services.bootstrap import set_config
from backend.app.services.erp.kingdee_client import execute_bill_query_with_config, kingdee_config_from_session
from backend.app.services.jsonutil import dumps, loads


DEFAULT_MATERIAL_FORM_ID = "BD_MATERIAL"
DEFAULT_MATERIAL_FIELD_KEYS = "FNumber,FName,FSpecification,FMaterialGroup.FName,FForbidStatus"


def config_value(session: Session, key: str, fallback: str = "") -> str:
    row = session.get(SystemConfig, key)
    if row is None:
        return fallback
    return row.value or fallback


def config_bool(session: Session, key: str, default: bool = False) -> bool:
    value = config_value(session, key, str(default)).strip().lower()
    return value in {"1", "true", "yes", "on"}


def sync_erp_materials(session: Session, *, batch_size: int = 500, max_batches: int = 200) -> dict[str, Any]:
    if not config_bool(session, "erp_enabled", False):
        return {"ok": False, "skipped": "ERP 未启用", "created_spu": 0, "updated_spu": 0, "created_sku": 0, "updated_sku": 0, "total": 0}

    form_id = config_value(session, "erp_material_form_id", DEFAULT_MATERIAL_FORM_ID).strip() or DEFAULT_MATERIAL_FORM_ID
    field_keys = config_value(session, "erp_material_field_keys", DEFAULT_MATERIAL_FIELD_KEYS).strip() or DEFAULT_MATERIAL_FIELD_KEYS
    fields = [field.strip() for field in field_keys.split(",") if field.strip()]
    if "FNumber" not in fields or "FName" not in fields:
        raise RuntimeError("ERP 物料同步字段必须包含 FNumber,FName")

    config = kingdee_config_from_session(session)
    created_spu = updated_spu = created_sku = updated_sku = total = skipped_duplicates = 0
    start_row = 0
    last_query: dict[str, Any] | None = None
    seen_numbers: set[str] = set()

    for _ in range(max_batches):
        result = execute_bill_query_with_config(
            config,
            form_id=form_id,
            field_keys=field_keys,
            limit=batch_size,
            start_row=start_row,
        )
        last_query = result
        if not result.get("ok"):
            raise RuntimeError(result.get("message") or "ERP 物料查询失败")
        rows = result.get("items") or []
        if not rows:
            break
        for row in rows:
            material = material_from_query_row(fields, row)
            if not material["number"]:
                continue
            if material["number"] in seen_numbers:
                skipped_duplicates += 1
                continue
            seen_numbers.add(material["number"])
            spu_result = upsert_material_spu(session, material)
            sku_result = upsert_material_sku(session, material, spu_result["spu"])
            created_spu += 1 if spu_result["created"] else 0
            updated_spu += 0 if spu_result["created"] else 1
            created_sku += 1 if sku_result["created"] else 0
            updated_sku += 0 if sku_result["created"] else 1
            total += 1
        session.flush()
        if len(rows) < batch_size:
            break
        start_row += len(rows)

    synced_at = now_utc()
    set_config(session, "erp_material_last_sync_at", synced_at.isoformat(), is_secret=False)
    return {
        "ok": True,
        "form_id": form_id,
        "field_keys": field_keys,
        "total": total,
        "created_spu": created_spu,
        "updated_spu": updated_spu,
        "created_sku": created_sku,
        "updated_sku": updated_sku,
        "skipped_duplicates": skipped_duplicates,
        "last_sync_at": synced_at.isoformat(),
        "last_query_elapsed_ms": last_query.get("elapsed_ms") if last_query else None,
    }


def material_from_query_row(fields: list[str], row: Any) -> dict[str, Any]:
    values = row if isinstance(row, list) else [row]
    data = {fields[index]: values[index] if index < len(values) else None for index in range(len(fields))}
    return {
        "number": str(data.get("FNumber") or "").strip(),
        "name": str(data.get("FName") or "").strip(),
        "specification": str(data.get("FSpecification") or "").strip(),
        "category": str(data.get("FMaterialGroup.FName") or "").strip(),
        "forbid_status": str(data.get("FForbidStatus") or "").strip(),
        "raw": data,
    }


def material_status(forbid_status: str) -> str:
    text = str(forbid_status or "").strip().lower()
    if text in {"b", "forbid", "true", "1", "禁用", "停用"}:
        return "Inactive"
    return "Active"


def upsert_material_spu(session: Session, material: dict[str, Any]) -> dict[str, Any]:
    spu = session.query(ProductSPU).filter_by(spu_id=material["number"]).one_or_none()
    created = spu is None
    if spu is None:
        spu = ProductSPU(spu_id=material["number"], name=material["name"] or material["number"])
        session.add(spu)
        session.flush()
    spu.name = material["name"] or material["number"]
    if material.get("category"):
        spu.category = material["category"]
    spu.status = material_status(material.get("forbid_status", ""))
    info = loads(spu.extended_info_json, {})
    info["erp"] = {
        "source": "kingdee_k3cloud",
        "material_number": material["number"],
        "specification": material.get("specification", ""),
        "forbid_status": material.get("forbid_status", ""),
        "raw": material.get("raw", {}),
        "synced_at": now_utc().isoformat(),
    }
    spu.extended_info_json = dumps(info)
    spu.updated_at = now_utc()
    return {"spu": spu, "created": created}


def upsert_material_sku(session: Session, material: dict[str, Any], spu: ProductSPU) -> dict[str, Any]:
    sku = session.query(ProductSKU).filter_by(sku_id=material["number"]).one_or_none()
    created = sku is None
    if sku is None:
        sku = ProductSKU(spu_uuid=spu.id, sku_id=material["number"])
        session.add(sku)
    sku.spu_uuid = spu.id
    sku.model = material.get("specification") or sku.model
    sku.status = material_status(material.get("forbid_status", ""))
    attrs = loads(sku.attributes_json, {})
    attrs.update(
        {
            "erp_material_name": material.get("name", ""),
            "erp_specification": material.get("specification", ""),
            "erp_category": material.get("category", ""),
            "erp_forbid_status": material.get("forbid_status", ""),
            "erp_synced_at": now_utc().isoformat(),
        }
    )
    sku.attributes_json = dumps(attrs)
    supply = loads(sku.supply_info_json, {})
    supply["erp"] = {"source": "kingdee_k3cloud", "raw": material.get("raw", {})}
    sku.supply_info_json = dumps(supply)
    sku.updated_at = now_utc()
    return {"sku": sku, "created": created}


def erp_material_sync_due(session: Session, *, now: datetime | None = None) -> bool:
    if not config_bool(session, "erp_enabled", False) or not config_bool(session, "erp_material_sync_enabled", True):
        return False
    interval = int(config_value(session, "erp_material_sync_interval_seconds", "86400") or "86400")
    if interval <= 0:
        return False
    last_sync = config_value(session, "erp_material_last_sync_at", "").strip()
    if not last_sync:
        return True
    try:
        last_dt = datetime.fromisoformat(last_sync)
    except ValueError:
        return True
    return ((now or now_utc()) - last_dt).total_seconds() >= interval
