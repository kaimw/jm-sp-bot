from backend.app.models import ProductSKU
from backend.app.services.rules import BlockerLevel, OrderContext, ValidationResult
from backend.app.services.jsonutil import dumps, loads


class KnownSkuRule:
    def get_rule_code(self) -> str:
        return "KNOWN_ACTIVE_SKU"

    def supports(self, context: OrderContext) -> bool:
        return bool(context.items)

    def validate(self, context: OrderContext) -> ValidationResult:
        sku_codes = sorted({str(item.sku_code or "").strip() for item in context.items if str(item.sku_code or "").strip()})
        if not sku_codes:
            evidence = []
            for item in context.items:
                raw = loads(item.raw_json, {})
                mapping = raw.get("sku_mapping") if isinstance(raw, dict) else None
                if isinstance(mapping, dict) and mapping.get("source") == "product_name_semantic":
                    reason = str(mapping.get("reason") or "unknown")
                    product_name = str(mapping.get("product_name") or item.product_name or "").strip()
                    candidates = mapping.get("candidates") if isinstance(mapping.get("candidates"), list) else []
                    evidence.append(
                        "SKU_MATCH_JSON:"
                        + dumps(
                            {
                                "product_name": product_name,
                                "reason": reason,
                                "candidates": candidates[:5],
                            }
                        )
                    )
                    top = candidates[0] if candidates and isinstance(candidates[0], dict) else {}
                    if reason == "low_confidence":
                        evidence.append(
                            f"{product_name}：语义检索置信度过低，候选 {top.get('sku_id') or '-'}({top.get('confidence') or 0})"
                        )
                    elif reason == "ambiguous":
                        evidence.append(f"{product_name}：语义检索存在多个同置信候选，需人工确认")
                    else:
                        evidence.append(f"{product_name}：语义检索未命中标准物料")
            if evidence:
                return ValidationResult(self.get_rule_code(), False, BlockerLevel.HIGH, "订单产品未能自动匹配标准 SKU，需人工确认：" + "；".join(evidence[:5]), evidence)
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
