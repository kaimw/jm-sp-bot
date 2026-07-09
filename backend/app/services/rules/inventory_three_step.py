"""库存三步判断规则（替代 LocalInventoryAvailableRule）

根据设计文档 §16.2（Q5 决策）：
  Step 1：查主体关联仓库库存 → 够→通过；不够→Step 2
  Step 2：查其他主体仓库库存 → 有其他→非阻断+调货通知；全缺→Step 3
  Step 3：全缺→阻断，通知销售重新提交

该规则作为预审规则链的最后一步。
"""

from decimal import Decimal
from typing import Any

from sqlalchemy.orm import Session

from backend.app.models import (
    CustomerEntityMapping,
    EntityMapping,
    InterEntityTransfer,
    ProductInventorySnapshot,
)
from backend.app.services.rules import BlockerLevel, OrderContext, ValidationResult
from backend.app.services.rules.helpers import config_bool, config_value


class InventoryThreeStepRule:
    """库存三步判断规则 — 替代 LocalInventoryAvailableRule"""

    def get_rule_code(self) -> str:
        return "INVENTORY_THREE_STEP"

    def supports(self, context: OrderContext) -> bool:
        return config_bool(context.session, "inventory_three_step_enabled", True) and bool(context.items)

    def validate(self, context: OrderContext) -> ValidationResult:
        session = context.session
        order = context.order

        # 确定主体编码
        entity_code = _resolve_entity_code(session, order)

        # Step 1：查主体关联仓库的库存
        warehouses = _entity_warehouses(session, entity_code)
        step1_failures = _check_inventory(session, context.items, warehouses)

        if not step1_failures:
            return ValidationResult(self.get_rule_code(), True,
                                    reason=f"Step1 通过：主体 {entity_code} 仓库 {warehouses} 库存充足")

        # Step 2：查其他主体仓库的库存
        all_failures = list(step1_failures)
        other_warehouses = _other_warehouses(session, entity_code)
        step2_failures = _check_inventory(session, context.items, other_warehouses)

        if step2_failures:
            # Step 3：全缺 → 阻断
            return ValidationResult(
                self.get_rule_code(), False, BlockerLevel.CRITICAL,
                f"Step3 阻断：所有仓库均缺货\n"
                f"缺货物料：{'；'.join(step2_failures)}",
                step2_failures,
            )

        # Step 2 有其他仓库有货 → 非阻断，创建调货记录+返回调货信息
        _create_inter_entity_transfer(session, context, entity_code, other_warehouses, step1_failures)
        replenish_hint = _build_replenish_hint(session, context.items, entity_code, other_warehouses)
        return ValidationResult(
            self.get_rule_code(), True, BlockerLevel.LOW,
            f"Step2 调货：主体 {entity_code} 仓库缺货，其他仓库有库存，已发送调货通知\n{replenish_hint}",
            step1_failures,
        )


def _resolve_entity_code(session: Session, order) -> str:
    """解析订单主体编码"""
    entity_code = order.entity_code or "SZ"
    if order.order_type == "STOCK_REPLENISHMENT" and order.customer_name:
        cust_map = (
            session.query(CustomerEntityMapping)
            .filter(CustomerEntityMapping.customer_name == order.customer_name, CustomerEntityMapping.is_active == True)
            .first()
        )
        if cust_map:
            entity_code = cust_map.entity_code
    return entity_code


def _entity_warehouses(session: Session, entity_code: str) -> list[str]:
    """获取主体关联的仓库列表"""
    mapping = session.query(EntityMapping).filter(EntityMapping.entity_code == entity_code, EntityMapping.is_active == True).first()
    if mapping and mapping.warehouses_json:
        import json
        try:
            warehouses = json.loads(mapping.warehouses_json)
            if isinstance(warehouses, list):
                return [w.get("warehouse_code", "") for w in warehouses if isinstance(w, dict) and w.get("warehouse_code")]
            return warehouses if isinstance(warehouses, list) else []
        except (json.JSONDecodeError, TypeError):
            pass
    # 若数据库中完全没有配置任何 EntityMapping，则向下兼容，返回当前所有存在库存快照的仓库
    total_mappings_count = session.query(EntityMapping).filter(EntityMapping.is_active == True).count()
    if total_mappings_count == 0:
        wh_codes = [r[0] for r in session.query(ProductInventorySnapshot.warehouse_code).distinct().all() if r[0]]
        return wh_codes
    return []


def _other_warehouses(session: Session, entity_code: str) -> list[str]:
    """获取其他主体的仓库列表"""
    mappings = session.query(EntityMapping).filter(EntityMapping.entity_code != entity_code, EntityMapping.is_active == True).all()
    result: list[str] = []
    import json
    for mapping in mappings:
        if mapping.warehouses_json:
            try:
                whs = json.loads(mapping.warehouses_json)
                if isinstance(whs, list):
                    result.extend([w.get("warehouse_code", "") for w in whs if isinstance(w, dict) and w.get("warehouse_code")])
            except (json.JSONDecodeError, TypeError):
                pass
    return list(set(result))


def _check_inventory(session: Session, items, warehouses: list[str]) -> list[str]:
    """检查指定仓库列表中物料的库存是否满足需求，返回缺货物料列表"""
    if not warehouses:
        return [f"仓库列表为空"]

    failures: list[str] = []
    for item in items:
        sku_code = str(item.sku_code or "").strip()
        if not sku_code:
            continue
        required = Decimal(str(item.quantity or 0))
        total_available = Decimal("0")
        found_any = False
        for wh in warehouses:
            snapshot = (
                session.query(ProductInventorySnapshot)
                .filter(
                    ProductInventorySnapshot.material_code == sku_code,
                    ProductInventorySnapshot.warehouse_code == wh,
                    ProductInventorySnapshot.status == "Active",
                )
                .first()
            )
            if snapshot is not None:
                found_any = True
                total_available += Decimal(str(snapshot.qty or 0))
        if not found_any:
            failures.append(f"{sku_code} 无库存数据")
        elif total_available < required:
            failures.append(f"{sku_code} 需求 {required}，可用 {total_available}，缺 {required - total_available}")
    return failures


def _build_replenish_hint(session: Session, items, entity_code: str, other_warehouses: list[str]) -> str:
    """生成调货提示信息（邮件内容用）"""
    hints: list[str] = []
    for item in items:
        sku_code = str(item.sku_code or "").strip()
        if not sku_code:
            continue
        for wh in other_warehouses:
            snapshot = (
                session.query(ProductInventorySnapshot)
                .filter(
                    ProductInventorySnapshot.material_code == sku_code,
                    ProductInventorySnapshot.warehouse_code == wh,
                    ProductInventorySnapshot.status == "Active",
                )
                .first()
            )
            if snapshot is not None and snapshot.qty > 0:
                hints.append(f"{sku_code} → {wh}({snapshot.qty}台)")
    return "可调货：" + "；".join(hints[:6]) if hints else ""


def _create_inter_entity_transfer(
    session: Session,
    context: OrderContext,
    entity_code: str,
    other_warehouses: list[str],
    shortages: list[str],
) -> None:
    """Step 2 判定通过时，创建跨主体调货记录"""
    if not context.order or not context.crm_order:
        return
    order = context.order
    crm_order = context.crm_order
    try:
        transfer = InterEntityTransfer(
            source_entity=entity_code,
            target_entity=_find_target_entity(session, other_warehouses, entity_code),
            crm_order_id=crm_order.crm_order_id or "",
            order_id=order.id,
            material_json=__import__("json").dumps(shortages, ensure_ascii=False),
            status="Draft",
            notified=False,
        )
        session.add(transfer)
    except Exception:
        pass  # 记录失败不影响预审流程


def _find_target_entity(session: Session, other_warehouses: list[str], exclude_entity: str) -> str:
    """根据其他仓库列表反查主体编码"""
    if not other_warehouses:
        return "SZ"
    wh = other_warehouses[0]
    mapping = (
        session.query(EntityMapping)
        .filter(
            EntityMapping.warehouses_json.like(f"%{wh}%"),
            EntityMapping.entity_code != exclude_entity,
            EntityMapping.is_active == True,
        )
        .first()
    )
    return mapping.entity_code if mapping else "SZ"
