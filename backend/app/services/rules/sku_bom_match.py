"""BOM 标准库校验规则 — 检查 CRM SKU 是否在 BOM 标准库中"""

from backend.app.models import ProductSKU
from backend.app.services.rules import BlockerLevel, OrderContext, ValidationResult


class SkuBomMatchRule:
    """检查 CRM 订单的 SKU 是否匹配标准 BOM 库中的型号。

    与 KnownSkuRule 的区别：
    - KnownSkuRule：检查 SKU 编码是否在 product_skus 表中启用
    - SkuBomMatchRule：检查 CRM 录入的商品名称/型号是否可通过 BOM 别名反查标准 SKU
    """

    def get_rule_code(self) -> str:
        return "RULE_SKU_BOM_MATCH"

    def supports(self, context: OrderContext) -> bool:
        return bool(context.items)

    def validate(self, context: OrderContext) -> ValidationResult:
        unmatched = []
        for item in context.items:
            sku_code = str(item.sku_code or "").strip()
            product_name = str(item.product_name or "").strip()
            if sku_code:
                # 有 SKU 编码，由 KnownSkuRule 校验
                continue
            if not product_name:
                unmatched.append(item.id or "未知行")
                continue
            # 尝试通过商品名模糊匹配 BOM 库
            found = (
                context.session.query(ProductSKU.sku_id)
                .filter(ProductSKU.model.ilike(f"%{product_name}%"), ProductSKU.status == "Active")
                .first()
            )
            if not found:
                # 尝试 SPU 名称匹配
                from backend.app.models import ProductSPU
                found_spu = (
                    context.session.query(ProductSPU.spu_id)
                    .filter(ProductSPU.name.ilike(f"%{product_name}%"), ProductSPU.status == "Active")
                    .first()
                )
                if not found_spu:
                    unmatched.append(product_name)

        if unmatched:
            return ValidationResult(
                self.get_rule_code(),
                False,
                BlockerLevel.HIGH,
                f"CRM 录入的商品名称未匹配到标准 BOM 库：{', '.join(unmatched[:5])}",
                unmatched,
            )
        return ValidationResult(self.get_rule_code(), True)
