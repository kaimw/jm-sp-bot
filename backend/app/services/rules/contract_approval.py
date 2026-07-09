"""商务审核前置条件规则

检查 CRM 订单的生命状态（life_status）是否为"正常"/"normal"。
纷享销客中生命状态为"正常"表示订单已生效，等同于审批通过。
仅适用于销售订单，备货订单跳过。
"""

from backend.app.services.rules import BlockerLevel, OrderContext, ValidationResult


STOCK_REPLENISHMENT = "STOCK_REPLENISHMENT"
LIFE_STATUS_APPROVED = {"normal", "正常"}


class ContractApprovalRule:
    def get_rule_code(self) -> str:
        return "CONTRACT_APPROVAL"

    def supports(self, context: OrderContext) -> bool:
        return context.order.order_type != STOCK_REPLENISHMENT

    def validate(self, context: OrderContext) -> ValidationResult:
        crm_order = context.crm_order
        if crm_order is None:
            return ValidationResult(
                self.get_rule_code(), False, BlockerLevel.CRITICAL,
                "未找到 CRM 订单数据，无法检查订单生效状态",
            )

        life_status = (crm_order.life_status or "").strip().lower()
        if not life_status:
            return ValidationResult(
                self.get_rule_code(), False, BlockerLevel.CRITICAL,
                "CRM 订单缺少生命状态字段，请确认订单是否已生效",
            )

        if life_status not in {"normal", "正常"}:
            return ValidationResult(
                self.get_rule_code(), False, BlockerLevel.CRITICAL,
                f"订单生命状态异常：当前为「{crm_order.life_status}」，需要为「正常」",
            )

        return ValidationResult(self.get_rule_code(), True, reason=f"订单已生效（生命状态：{crm_order.life_status}）")
