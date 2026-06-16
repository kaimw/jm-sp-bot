from backend.app.services.rules import BlockerLevel, OrderContext, ValidationResult


class RequiredHeadFieldsRule:
    required_fields = ("crm_order_id", "crm_order_no", "customer_name")

    def get_rule_code(self) -> str:
        return "REQUIRED_HEAD_FIELDS"

    def supports(self, context: OrderContext) -> bool:
        return True

    def validate(self, context: OrderContext) -> ValidationResult:
        missing = [field for field in self.required_fields if not getattr(context.order, field, None)]
        if missing:
            return ValidationResult(
                self.get_rule_code(),
                False,
                BlockerLevel.CRITICAL,
                f"订单头字段缺失：{', '.join(missing)}",
                missing,
            )
        return ValidationResult(self.get_rule_code(), True)
