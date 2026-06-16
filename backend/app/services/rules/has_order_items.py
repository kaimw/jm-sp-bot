from decimal import Decimal

from backend.app.services.rules import BlockerLevel, OrderContext, ValidationResult


class HasOrderItemsRule:
    def get_rule_code(self) -> str:
        return "HAS_ORDER_ITEMS"

    def supports(self, context: OrderContext) -> bool:
        return True

    def validate(self, context: OrderContext) -> ValidationResult:
        if not context.items:
            return ValidationResult(self.get_rule_code(), False, BlockerLevel.CRITICAL, "CRM 订单未解析到任何明细行。")
        missing_qty = [item.sku_code or item.product_name or item.id for item in context.items if item.quantity is None or Decimal(str(item.quantity)) <= 0]
        if missing_qty:
            return ValidationResult(self.get_rule_code(), False, BlockerLevel.CRITICAL, f"订单明细数量缺失或不合法：{', '.join(missing_qty[:5])}")
        return ValidationResult(self.get_rule_code(), True)
