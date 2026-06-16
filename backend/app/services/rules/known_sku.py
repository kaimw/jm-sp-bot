from backend.app.models import ProductSKU
from backend.app.services.rules import BlockerLevel, OrderContext, ValidationResult


class KnownSkuRule:
    def get_rule_code(self) -> str:
        return "KNOWN_ACTIVE_SKU"

    def supports(self, context: OrderContext) -> bool:
        return bool(context.items)

    def validate(self, context: OrderContext) -> ValidationResult:
        sku_codes = sorted({str(item.sku_code or "").strip() for item in context.items if str(item.sku_code or "").strip()})
        if not sku_codes:
            return ValidationResult(self.get_rule_code(), False, BlockerLevel.HIGH, "订单明细未提供 SKU 编码，需人工映射。")
        known = {
            row[0]
            for row in context.session.query(ProductSKU.sku_id)
            .filter(ProductSKU.sku_id.in_(sku_codes), ProductSKU.status == "Active")
            .all()
        }
        missing = [sku for sku in sku_codes if sku not in known]
        if missing:
            unmapped_shop_skus = [item.shop_sku_code for item in context.items if item.sku_code in missing and item.shop_sku_code]
            if unmapped_shop_skus:
                return ValidationResult("SKU_MAPPING_MISSING", False, BlockerLevel.CRITICAL, f"CRM 渠道 SKU 未匹配中台标准 SKU：{', '.join(unmapped_shop_skus[:10])}", unmapped_shop_skus)
            return ValidationResult(self.get_rule_code(), False, BlockerLevel.CRITICAL, f"SKU 未在主数据启用：{', '.join(missing[:10])}", missing)
        return ValidationResult(self.get_rule_code(), True)
