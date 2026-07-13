from decimal import Decimal

from backend.app.services.rules import BlockerLevel, OrderContext, ValidationResult


class PositiveAmountRule:
    def get_rule_code(self) -> str:
        return "POSITIVE_ORDER_AMOUNT"

    def supports(self, context: OrderContext) -> bool:
        # 备货订单跳过金额检查
        return context.order.order_type != "STOCK_REPLENISHMENT"

    def validate(self, context: OrderContext) -> ValidationResult:
        if context.order.order_amount is None:
            return ValidationResult(self.get_rule_code(), False, BlockerLevel.CRITICAL, "订单金额为空，需人工确认 CRM 金额。")
        if Decimal(str(context.order.order_amount)) <= Decimal("0"):
            return ValidationResult(self.get_rule_code(), False, BlockerLevel.CRITICAL, "订单金额必须大于 0。")
        return ValidationResult(self.get_rule_code(), True)
