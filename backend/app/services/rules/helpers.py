"""规则引擎辅助函数 — 从 order_middle_platform.py 提取的共享工具"""

from __future__ import annotations

from decimal import Decimal, InvalidOperation
from typing import Any

from sqlalchemy.orm import Session

from backend.app.models import ProductInventorySnapshot, SystemConfig
from backend.app.services.jsonutil import loads


def config_value(session: Session, key: str, default: str = "") -> str:
    row = session.get(SystemConfig, key)
    if row is None or row.value is None:
        return default
    return str(row.value)


def config_bool(session: Session, key: str, default: bool = False) -> bool:
    value = config_value(session, key, "")
    if value == "":
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def config_list(session: Session, key: str, default: list[str] | None = None) -> list[str]:
    raw = config_value(session, key, "")
    if raw:
        parsed = loads(raw, None)
        if isinstance(parsed, list):
            return [str(item).strip() for item in parsed if str(item).strip()]
        return [item.strip() for item in raw.split(",") if item.strip()]
    return list(default or [])


def config_int(session: Session, key: str, default: int) -> int:
    try:
        return int(config_value(session, key, str(default)))
    except (TypeError, ValueError):
        return default


def config_dict(session: Session, key: str, default: dict[str, Any] | None = None) -> dict[str, Any]:
    raw = config_value(session, key, "")
    if raw:
        parsed = loads(raw, None)
        if isinstance(parsed, dict):
            return parsed
    return dict(default or {})


def parse_decimal(value: Any) -> Decimal | None:
    text = str(value or "").strip().replace(",", "")
    if not text:
        return None
    try:
        return Decimal(text).quantize(Decimal("0.01"))
    except (InvalidOperation, ValueError):
        return None


def is_approved_status(session: Session, value: str) -> bool:
    allowed = config_list(
        session,
        "v2_review_crm_approved_values",
        ["approved", "审批通过", "已审批", "已通过", "complete", "completed", "passed"],
    )
    normalized = value.strip().lower()
    return normalized in {item.strip().lower() for item in allowed}


def inventory_available_quantity(session: Session, sku_code: str) -> Decimal | None:
    rows = (
        session.query(ProductInventorySnapshot)
        .filter(ProductInventorySnapshot.material_code == sku_code, ProductInventorySnapshot.status == "Active")
        .all()
    )
    if not rows:
        return None
    total = Decimal("0")
    for row in rows:
        source = loads(row.source_payload_json, {})
        raw_available = (
            source.get("canUseQuantity")
            or source.get("availableQuantity")
            or source.get("available_quantity")
            or source.get("qty")
            or row.qty
        )
        total += parse_decimal(raw_available) or Decimal("0")
    return total
