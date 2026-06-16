from decimal import Decimal

from backend.app.services.rules import BlockerLevel, OrderContext, ValidationResult
from backend.app.services.rules.helpers import parse_decimal


class AmountConsistencyRule:
    def get_rule_code(self) -> str:
        return "AMOUNT_CONSISTENCY"

    def supports(self, context: OrderContext) -> bool:
        return True

    def validate(self, context: OrderContext) -> ValidationResult:
        crm = context.crm_order
        order_amount = parse_decimal(crm.order_amount)
        product_amount = parse_decimal(crm.product_amount)
        received_amount = parse_decimal(crm.received_amount)
        receivable_amount = parse_decimal(crm.receivable_amount)
        failures: list[str] = []

        if product_amount is None:
            failures.append("商品金额(product_amount)缺失")
        elif order_amount is not None and abs(order_amount - product_amount) > Decimal("0.01"):
            failures.append(f"订单金额 {order_amount} 与商品金额 {product_amount} 不一致")

        if received_amount is not None and receivable_amount is not None and order_amount is not None:
            if abs((received_amount + receivable_amount) - order_amount) > Decimal("0.01"):
                failures.append(f"已收+应收 {received_amount + receivable_amount} 与订单金额 {order_amount} 不一致")

        if failures:
            return ValidationResult(self.get_rule_code(), False, BlockerLevel.CRITICAL, "；".join(failures), failures)
        return ValidationResult(self.get_rule_code(), True)
