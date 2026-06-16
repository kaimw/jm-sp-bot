from decimal import Decimal

from backend.app.services.rules import BlockerLevel, OrderContext, ValidationResult
from backend.app.services.rules.helpers import config_bool, inventory_available_quantity


class LocalInventoryAvailableRule:
    def get_rule_code(self) -> str:
        return "LOCAL_INVENTORY_AVAILABLE"

    def supports(self, context: OrderContext) -> bool:
        return config_bool(context.session, "oms_inventory_review_enabled", True) and bool(context.items)

    def validate(self, context: OrderContext) -> ValidationResult:
        missing_blocks = config_bool(context.session, "oms_inventory_missing_blocks", False)
        failures: list[str] = []
        unknown: list[str] = []
        for item in context.items:
            sku_code = str(item.sku_code or "").strip()
            if not sku_code:
                continue
            required = Decimal(str(item.quantity or 0))
            available = inventory_available_quantity(context.session, sku_code)
            if available is None:
                unknown.append(sku_code)
                continue
            if available < required:
                failures.append(f"{sku_code} 可用 {available} < 需求 {required}")
        if failures:
            return ValidationResult(self.get_rule_code(), False, BlockerLevel.CRITICAL, "库存可用量不足：" + "；".join(failures[:8]), failures)
        if unknown and missing_blocks:
            return ValidationResult(self.get_rule_code(), False, BlockerLevel.CRITICAL, "未找到库存快照：" + "、".join(unknown[:8]), unknown)
        if unknown:
            return ValidationResult(self.get_rule_code(), False, BlockerLevel.HIGH, "部分 SKU 暂无库存快照，已允许进入人工发货单确认：" + "、".join(unknown[:8]), unknown)
        return ValidationResult(self.get_rule_code(), True)
