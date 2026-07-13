"""附件商品信息一致性校验 — 预审阶段比较 CRM 订单产品与附件解析文本。"""

from __future__ import annotations

import re
from decimal import Decimal
from typing import Any

from sqlalchemy.orm import Session

from backend.app.models import OrderAttachment
from backend.app.services.jsonutil import loads
from backend.app.services.rules import BlockerLevel, OrderContext, ValidationResult
from backend.app.services.rules.helpers import parse_decimal


GENERIC_PRODUCT_TOKENS = {
    "产品",
    "商品",
    "设备",
    "硬件",
    "软件",
    "服务",
    "国内",
    "海外",
    "标准",
    "定制",
    "采购",
}


class AttachmentProductConsistencyRule:
    def get_rule_code(self) -> str:
        return "ATTACHMENT_PRODUCT_CONSISTENCY"

    def supports(self, context: OrderContext) -> bool:
        # 备货订单跳过附件一致性检查
        if context.order.order_type == "STOCK_REPLENISHMENT":
            return False
        return bool(context.items)

    def validate(self, context: OrderContext) -> ValidationResult:
        attachments = _load_attachments(context.session, context.crm_order.crm_order_id, context.order.payload_hash)
        if not attachments:
            return ValidationResult(self.get_rule_code(), True, evidence_refs=["未找到当前快照附件，商品一致性校验由关键附件规则兜底"])

        evidence_text = _attachment_text(attachments)
        if not evidence_text:
            return ValidationResult(self.get_rule_code(), True, evidence_refs=["附件尚未解析出文本，跳过商品一致性校验"])

        normalized_text = _normalize(evidence_text)
        table_values = _extract_po_table_values(evidence_text)
        failures: list[str] = []
        evidence_refs: list[str] = []
        for item in context.items:
            raw = loads(item.raw_json, {})
            product_name = str(item.product_name or raw.get("product_name") or raw.get("name") or "").strip()
            sku_code = str(item.sku_code or raw.get("sku_code") or "").strip()
            label = product_name or sku_code or f"明细 {item.id}"
            refs = []

            product_tokens = _meaningful_product_tokens(product_name)
            candidate_texts = _candidate_texts_for_item(evidence_text, product_tokens, sku_code)
            if product_tokens:
                if not any(_normalize(token) in normalized_text for token in product_tokens):
                    failures.append(f"{label}：附件未出现可匹配的商品名称/关键词")
                refs.append(f"商品关键词={','.join(product_tokens[:5])}")
            if sku_code:
                refs.append(f"SKU={sku_code}")

            quantity = parse_decimal(item.quantity or raw.get("quantity") or raw.get("qty"))
            if quantity is not None and quantity > Decimal("0"):
                quantity_values = _extract_labeled_decimals(candidate_texts, [r"数量", r"qty", r"quantity"])
                quantity_values = _merge_decimals(quantity_values, table_values.get("quantity", []))
                if not quantity_values:
                    failures.append(f"{label}：附件未识别到可比对的数量，CRM={quantity.normalize()}")
                elif not _decimal_exists(quantity, quantity_values, tolerance=Decimal("0")):
                    failures.append(f"{label}：附件数量与 CRM 不一致，CRM={quantity.normalize()}，附件={_format_decimals(quantity_values)}")
                refs.append(f"数量={quantity.normalize()}")

            unit_price = parse_decimal(item.unit_price or raw.get("unit_price") or raw.get("price"))
            if unit_price is not None and unit_price > Decimal("0"):
                unit_price_values = _extract_labeled_decimals(candidate_texts, [r"单价", r"销售单价", r"unit\s*price", r"price"])
                derived_unit_price_values = []
                if quantity is not None and quantity > Decimal("0"):
                    derived_unit_price_values = [value / quantity for value in table_values.get("line_amount", []) if value > 0]
                unit_price_values = _merge_decimals(
                    unit_price_values,
                    table_values.get("unit_price", []),
                    table_values.get("implied_unit_price", []),
                    derived_unit_price_values,
                )
                if not unit_price_values:
                    failures.append(f"{label}：附件未识别到可比对的单价，CRM={unit_price}")
                elif not _decimal_exists(unit_price, unit_price_values):
                    failures.append(f"{label}：附件单价与 CRM 不一致，CRM={unit_price}，附件={_format_decimals(unit_price_values)}")
                refs.append(f"单价={unit_price}")

            line_amount = parse_decimal(item.line_amount or raw.get("line_amount") or raw.get("amount"))
            if line_amount is not None and line_amount > Decimal("0"):
                line_amount_values = _extract_labeled_decimals(candidate_texts, [r"明细总价", r"总价", r"小计", r"合计", r"金额", r"amount", r"total"])
                line_amount_values = _merge_decimals(line_amount_values, table_values.get("line_amount", []))
                if not line_amount_values:
                    failures.append(f"{label}：附件未识别到可比对的明细总价，CRM={line_amount}")
                elif not _decimal_exists(line_amount, line_amount_values):
                    failures.append(f"{label}：附件明细总价与 CRM 不一致，CRM={line_amount}，附件={_format_decimals(line_amount_values)}")
                refs.append(f"明细总价={line_amount}")

            if refs:
                evidence_refs.append(f"{label}：" + "，".join(refs))

        if failures:
            return ValidationResult(
                self.get_rule_code(),
                False,
                BlockerLevel.CRITICAL,
                "CRM 订单产品与附件解析内容不一致：" + "；".join(failures),
                failures + evidence_refs,
            )
        return ValidationResult(self.get_rule_code(), True, evidence_refs=evidence_refs[:8])


def _load_attachments(session: Session, crm_order_id: str, payload_hash: str) -> list[OrderAttachment]:
    return (
        session.query(OrderAttachment)
        .filter(OrderAttachment.crm_order_id == crm_order_id, OrderAttachment.payload_hash == payload_hash)
        .order_by(OrderAttachment.created_at)
        .all()
    )


def _attachment_text(attachments: list[OrderAttachment]) -> str:
    parts: list[str] = []
    for attachment in attachments:
        evidence = loads(attachment.evidence_json, {})
        text = str(evidence.get("parsed_text") or "").strip()
        if text:
            parts.append(f"[{attachment.file_name}]\n{text}")
    return "\n\n".join(parts)


def _normalize(value: Any) -> str:
    return re.sub(r"[\s,，.。;；:：()（）\[\]【】_\-—/\\]+", "", str(value or "").lower())


def _meaningful_product_tokens(product_name: str) -> list[str]:
    raw_tokens = re.split(r"[\s,，;；/\\()（）【】\[\]_\-—+]+", product_name)
    tokens: list[str] = []
    for token in raw_tokens + [product_name]:
        text = _normalize(token)
        if len(text) < 2 or text in GENERIC_PRODUCT_TOKENS:
            continue
        if text not in tokens:
            tokens.append(text)
    return tokens


def _extract_amounts(text: str) -> list[Decimal]:
    amounts: list[Decimal] = []
    for match in re.finditer(r"(?<!\d)(?:¥|￥|CNY|RMB|人民币)?\s*([0-9]{1,3}(?:,[0-9]{3})+(?:\.\d{1,2})?|[0-9]+(?:\.\d{1,2})?)(?!\d)", text, re.IGNORECASE):
        value = parse_decimal(match.group(1))
        if value is not None:
            amounts.append(value)
    return amounts


def _candidate_texts_for_item(evidence_text: str, product_tokens: list[str], sku_code: str) -> list[str]:
    normalized_sku = _normalize(sku_code)
    normalized_tokens = [_normalize(token) for token in product_tokens if _normalize(token)]
    if normalized_sku:
        normalized_tokens.append(normalized_sku)
    lines = [line.strip() for line in re.split(r"[\r\n]+", evidence_text) if line.strip()]
    candidates: list[str] = []
    for index, line in enumerate(lines):
        normalized_line = _normalize(line)
        if not normalized_tokens or not any(token in normalized_line for token in normalized_tokens):
            continue
        window = "\n".join(lines[max(0, index - 1): index + 2])
        if window not in candidates:
            candidates.append(window)
    return candidates or [evidence_text]


def _extract_labeled_decimals(candidates: list[str], label_patterns: list[str]) -> list[Decimal]:
    values: list[Decimal] = []
    seen: set[str] = set()
    label = "|".join(f"(?:{pattern})" for pattern in label_patterns)
    money = r"(?:¥|￥|CNY|RMB|人民币)?\s*([0-9]{1,3}(?:,[0-9]{3})+(?:\.\d{1,2})?|[0-9]+(?:\.\d{1,2})?)"
    patterns = [
        re.compile(rf"(?:{label})\s*[:：]?\s*{money}", re.IGNORECASE),
        re.compile(rf"(?:{label})[^\d¥￥]{{0,12}}{money}", re.IGNORECASE),
    ]
    for text in candidates:
        for pattern in patterns:
            for match in pattern.finditer(text):
                value = parse_decimal(match.group(1))
                if value is None:
                    continue
                key = str(value)
                if key in seen:
                    continue
                seen.add(key)
                values.append(value)
    return values


def _decimal_exists(expected: Decimal, values: list[Decimal], *, tolerance: Decimal = Decimal("0.02")) -> bool:
    return any(abs(value - expected) <= tolerance for value in values)


def _format_decimals(values: list[Decimal]) -> str:
    return ",".join(str(value.normalize()) for value in values[:6])


def _merge_decimals(*groups: list[Decimal]) -> list[Decimal]:
    values: list[Decimal] = []
    seen: set[str] = set()
    for group in groups:
        for value in group:
            key = str(value)
            if key in seen:
                continue
            seen.add(key)
            values.append(value)
    return values


def _extract_po_table_values(text: str) -> dict[str, list[Decimal]]:
    """Extract values from common PO table rows.

    Handles OCR text where headers and row cells are separated by spaces/newlines:
    产品名称 ... 数量 单位 单价（未含税） 总金额（含税）
    三维扫描仪 ... 1 台 ¥44,247.79 ¥50,000.00
    """
    compact = re.sub(r"\s+", " ", text)
    money = r"(?:¥|￥|CNY|RMB|人民币)?\s*([0-9]{1,3}(?:,[0-9]{3})+(?:\.\d{1,2})?|[0-9]+(?:\.\d{1,2})?)"
    row_pattern = re.compile(
        rf"(?<![\d.])([1-9]\d{{0,5}}(?:\.\d+)?)\s*(?:台|套|件|个|pcs?|PCS|unit|Unit)\s*{money}\s*{money}",
        re.IGNORECASE,
    )
    quantity: list[Decimal] = []
    unit_price: list[Decimal] = []
    line_amount: list[Decimal] = []
    implied_unit_price: list[Decimal] = []
    for match in row_pattern.finditer(compact):
        qty = parse_decimal(match.group(1))
        unit = parse_decimal(match.group(2))
        total = parse_decimal(match.group(3))
        if qty is None or qty <= 0:
            continue
        quantity.append(qty)
        if unit is not None:
            unit_price.append(unit)
        if total is not None:
            line_amount.append(total)
            implied_unit_price.append(total / qty)

    for row in _extract_pipe_table_rows(text):
        qty = row.get("quantity")
        unit = row.get("unit_price")
        total = row.get("line_amount")
        if qty is None or qty <= 0:
            continue
        quantity.append(qty)
        if unit is not None:
            unit_price.append(unit)
        if total is not None:
            line_amount.append(total)
            implied_unit_price.append(total / qty)

    total_patterns = [
        re.compile(rf"订单总金额(?:（含税）)?\s*[:：]?[^\d¥￥]{{0,120}}{money}", re.IGNORECASE),
        re.compile(rf"总金额(?:（含税）)?[^\d¥￥]{{0,120}}{money}", re.IGNORECASE),
    ]
    for pattern in total_patterns:
        for match in pattern.finditer(compact):
            value = parse_decimal(match.group(1))
            if value is not None:
                line_amount.append(value)

    return {
        "quantity": _merge_decimals(quantity),
        "unit_price": _merge_decimals(unit_price),
        "line_amount": _merge_decimals(line_amount),
        "implied_unit_price": _merge_decimals(implied_unit_price),
    }


def _extract_pipe_table_rows(text: str) -> list[dict[str, Decimal | None]]:
    lines = [line.strip() for line in re.split(r"[\r\n]+", text) if "|" in line]
    header: list[str] | None = None
    results: list[dict[str, Decimal | None]] = []
    for line in lines:
        cells = [cell.strip() for cell in line.split("|")]
        normalized_cells = [_normalize(cell) for cell in cells]
        if "数量" in normalized_cells and any("单价" in cell for cell in normalized_cells) and any("金额" in cell for cell in normalized_cells):
            header = normalized_cells
            continue
        if not header or len(cells) < len(header):
            continue
        quantity_index = header.index("数量") if "数量" in header else -1
        unit_price_index = next((index for index, cell in enumerate(header) if "单价" in cell), -1)
        line_amount_index = next((index for index, cell in enumerate(header) if "总金额" in cell or "明细总价" in cell or cell == "金额"), -1)
        if quantity_index < 0:
            continue
        results.append(
            {
                "quantity": _parse_table_decimal(cells[quantity_index]),
                "unit_price": _parse_table_decimal(cells[unit_price_index]) if unit_price_index >= 0 and unit_price_index < len(cells) else None,
                "line_amount": _parse_table_decimal(cells[line_amount_index]) if line_amount_index >= 0 and line_amount_index < len(cells) else None,
            }
        )
    return results


def _parse_table_decimal(value: Any) -> Decimal | None:
    match = re.search(r"(?:¥|￥|CNY|RMB|人民币)?\s*([0-9]{1,3}(?:,[0-9]{3})+(?:\.\d{1,2})?|[0-9]+(?:\.\d{1,2})?)", str(value or ""), re.IGNORECASE)
    if not match:
        return None
    return parse_decimal(match.group(1))
