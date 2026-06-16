from backend.app.services.rules import BlockerLevel, OrderContext, ValidationResult
from backend.app.services.rules.helpers import config_bool, config_dict
from backend.app.services.customer_mapping import resolve_customer_mapping_from_oms


class CustomerMappingRule:
    def get_rule_code(self) -> str:
        return "CUSTOMER_MAPPING"

    def supports(self, context: OrderContext) -> bool:
        return config_bool(context.session, "v2_review_customer_mapping_required", True)

    def validate(self, context: OrderContext) -> ValidationResult:
        crm = context.crm_order
        customer_id = str(crm.customer_id or "").strip()
        customer_name = str(crm.customer_name or context.order.customer_name or "").strip()
        if not customer_name:
            return ValidationResult(self.get_rule_code(), False, BlockerLevel.CRITICAL, "客户名称缺失，无法完成客户主数据映射。", ["CRM.customer_name"])
        mapping = config_dict(context.session, "v2_customer_mapping_json", {})
        candidates = [customer_id, customer_name]
        for key in candidates:
            if key and key in mapping:
                mapped = mapping.get(key) or {}
                customer_code = str(mapped.get("customer_code") or mapped.get("code") or key).strip()
                if customer_code:
                    return ValidationResult(self.get_rule_code(), True, evidence_refs=[f"客户映射：{customer_name}->{customer_code}"])
        resolved = resolve_customer_mapping_from_oms(context.session, customer_name=customer_name, crm_customer_code=customer_id)
        if resolved.get("found"):
            customer = resolved.get("customer") or {}
            customer_code = str(customer.get("customer_code") or "").strip()
            return ValidationResult(self.get_rule_code(), True, evidence_refs=[f"OMS 客户查询命中：{customer_name}->{customer_code}"])
        query_detail = resolved.get("detail") or {}
        query_status = str(query_detail.get("status") or "NotFound")
        return ValidationResult(
            self.get_rule_code(),
            False,
            BlockerLevel.CRITICAL,
            f"客户未在映射表维护，且 OMS 查询未命中：{customer_name}",
            [f"CRM.customer_name={customer_name}", "配置项 v2_customer_mapping_json", f"OMS.customer.query={query_status}"],
        )
