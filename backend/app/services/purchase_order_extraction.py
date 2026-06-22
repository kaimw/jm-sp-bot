from __future__ import annotations

import re
from decimal import Decimal, InvalidOperation
from typing import Any

from backend.app.services.address_quality import is_detailed_receipt_address


PHONE_PATTERN = re.compile(r"(?<!\d)(?:1[3-9]\d{9}|0\d{2,3}[-\s]?\d{7,8}(?:[-\s]?\d{1,6})?)(?!\d)")


def extract_purchase_order_fields(text: str) -> dict[str, Any]:
    source = str(text or "")
    buyer = extract_buyer_party(source)
    items = extract_purchase_table_items(source)
    missing_fields = purchase_order_missing_fields(buyer, items)
    return {
        "purchase_order_no": extract_purchase_order_no(source),
        "buyer": buyer,
        "items": items,
        "confidence": extraction_confidence(buyer, items),
        "missing_fields": missing_fields,
        "manual_review_required": bool(missing_fields),
    }


def purchase_order_missing_fields(buyer: dict[str, str], items: list[dict[str, Any]]) -> list[str]:
    missing: list[str] = []
    if not buyer.get("contact"):
        missing.append("buyer.contact")
    if not buyer.get("phone"):
        missing.append("buyer.phone")
    if not buyer.get("address") or buyer.get("address_quality") == "not_detailed":
        missing.append("buyer.address")
    if not items:
        missing.append("items")
    return missing


def extract_purchase_order_no(text: str) -> str:
    for line in [normalize_line(line) for line in str(text or "").splitlines() if normalize_line(line)]:
        match = re.search(r"(?:采购单号|采购订单号|订单编号|订单号|PO\s*No\.?)[:：]?\s*([A-Za-z0-9][A-Za-z0-9_\-./]+)", line, re.IGNORECASE)
        if match:
            return clean_value(match.group(1))
    return ""


def extract_buyer_party(text: str) -> dict[str, str]:
    lines = [normalize_line(line) for line in str(text or "").splitlines() if normalize_line(line)]
    buyer = {"name": "", "address": "", "contact": "", "phone": "", "source": ""}
    for source, block_lines in buyer_candidate_blocks(lines):
        merge_buyer_from_block(buyer, block_lines, source)
        if buyer.get("contact") and buyer.get("phone") and buyer.get("address"):
            break
    if not buyer["source"]:
        buyer["source"] = "unmatched"
    buyer.pop("source", None)
    if buyer["address"] and not is_detailed_receipt_address(buyer["address"]):
        buyer["address_quality"] = "not_detailed"
    return buyer


def buyer_candidate_blocks(lines: list[str]) -> list[tuple[str, list[str]]]:
    blocks: list[tuple[str, list[str]]] = []
    for index, line in enumerate(lines):
        if re.search(r"(采购方信息|甲方信息|买方信息|需方信息|购买方信息)", line):
            blocks.append(("buyer_party_marker", lines[index : index + 14]))
        elif re.search(r"^(采购方|甲方|买方|需方)(?:（[^）]*）)?[:：]", line):
            blocks.append(("buyer_party_line", lines[index : index + 14]))
        elif re.search(r"(收货信息|收件信息|物流信息|配送信息|交货地点|交付地点|送货地址|收货地址)", line):
            blocks.append(("logistics_marker", lines[index : index + 10]))
    if not blocks:
        for index, line in enumerate(lines):
            if re.search(r"(地址|联系人|联系方式|联系电话|电话|手机)", line):
                window = lines[max(0, index - 3) : index + 6]
                if not is_supplier_context(window):
                    blocks.append(("nearby_contact_window", window))
    return blocks


def merge_buyer_from_block(buyer: dict[str, str], block_lines: list[str], source: str) -> None:
    name_candidates: list[str] = []
    address_candidates: list[str] = []
    contact_candidates: list[str] = []
    phone_candidates: list[str] = []
    pending_phone = False
    for line in block_lines:
        name = re.search(r"(?:采购方名称|甲方名称|买方名称|需方名称|采购方(?:（[^）]*）)?|甲方|买方|需方)[:：]\s*(.+)", line)
        if name:
            name_candidates.append(clean_party_value(name.group(1)))
        address = re.search(r"地址[:：]\s*(.+)", line)
        if address:
            address_candidates.append(clean_party_value(address.group(1)))
        contact = re.search(r"(?:联系人|联络人)[:：]\s*(.+)", line)
        if contact:
            value = clean_party_value(PHONE_PATTERN.sub("", contact.group(1)))
            if value:
                contact_candidates.append(value)
        labeled_phone = re.search(r"(?:电话|手机|联系方式|联系电话)[:：]\s*(.*)", line)
        if labeled_phone:
            value = labeled_phone.group(1)
            match = PHONE_PATTERN.search(value)
            if match:
                phone_candidates.append(re.sub(r"\s+", "", match.group(0)))
                pending_phone = False
            else:
                pending_phone = True
            continue
        if pending_phone:
            match = PHONE_PATTERN.search(line)
            if match:
                phone_candidates.append(re.sub(r"\s+", "", match.group(0)))
                pending_phone = False
                continue
        match = PHONE_PATTERN.search(line)
        if match:
            phone_candidates.append(re.sub(r"\s+", "", match.group(0)))
    prefer_second = has_two_party_interleaved_block(block_lines)
    if not buyer.get("name"):
        buyer["name"] = choose_buyer_candidate(name_candidates, prefer_second=False)
    if not buyer.get("address"):
        buyer["address"] = choose_buyer_candidate(address_candidates, prefer_second=prefer_second)
    if not buyer.get("contact"):
        buyer["contact"] = choose_buyer_candidate(contact_candidates, prefer_second=False)
    if not buyer.get("phone"):
        buyer["phone"] = choose_buyer_candidate(phone_candidates, prefer_second=False)
    if any((address_candidates, contact_candidates, phone_candidates)) and not buyer.get("source"):
        buyer["source"] = source


def is_supplier_context(lines: list[str]) -> bool:
    text = "\n".join(lines)
    return bool(re.search(r"供方信息|供方名称|乙方信息|乙方名称|供应商|卖方", text)) and not bool(
        re.search(r"采购方|甲方|买方|需方|收货|收件|交货|交付|送货", text)
    )


def has_two_party_interleaved_block(lines: list[str]) -> bool:
    text = "\n".join(lines)
    return bool(
        re.search(r"采购方信息|甲方信息|买方信息|需方信息", text)
        and re.search(r"供方信息|供方名称|乙方信息|乙方名称|供应商信息|供应商名称|卖方信息|卖方名称", text)
    )


def choose_buyer_candidate(candidates: list[str], *, prefer_second: bool) -> str:
    values = [candidate for candidate in candidates if candidate]
    if not values:
        return ""
    if prefer_second and len(values) >= 2:
        return values[1]
    return values[0]


def extract_purchase_table_items(text: str) -> list[dict[str, Any]]:
    lines = [normalize_line(line) for line in str(text or "").splitlines() if normalize_line(line)]
    items: list[dict[str, Any]] = []
    header: list[str] | None = None
    header_raw: list[str] | None = None
    for line in lines:
        if "|" not in line:
            continue
        cells = [clean_value(cell) for cell in line.split("|")]
        normalized = [normalize_key(cell) for cell in cells]
        if is_purchase_table_header(normalized):
            header = normalized
            header_raw = cells
            continue
        if not header or len(cells) < 4:
            inferred = infer_purchase_item_from_cells(cells)
            if inferred:
                items.append(inferred)
            continue
        item = row_to_purchase_item(header, cells, header_raw or [])
        if item:
            items.append(item)
        elif in_purchase_table_context(lines, line):
            inferred = infer_purchase_item_from_cells(cells)
            if inferred:
                items.append(inferred)
    if not items:
        items = extract_vertical_purchase_table_items(lines)
    return items


def is_purchase_table_header(cells: list[str]) -> bool:
    joined = "".join(cells)
    has_quantity = any("数量" in cell or "套数" in cell or "件数" in cell for cell in cells)
    has_product = any(re.search(r"产品|品名|货物|商品|设备|名称", cell) for cell in cells)
    has_money = any(re.search(r"单价|总价|总金额|金额|小计|含税|不含税", cell) for cell in cells)
    return has_quantity and has_product and has_money


def row_to_purchase_item(header: list[str], cells: list[str], header_raw: list[str] | None = None) -> dict[str, Any] | None:
    def at(*names: str) -> str:
        for name in names:
            index = next((idx for idx, cell in enumerate(header) if name in cell), -1)
            if 0 <= index < len(cells):
                return clean_value(cells[index])
        return ""

    row_no = at("序号")
    product_name = at("产品名称", "品名", "商品名称", "货物名称", "设备名称", "设备")
    specification = at("规格型号", "主要规格", "详细配置", "型号", "规格", "配置")
    quantity = at("数量", "套数", "件数")
    unit = at("单位")
    quantity, inferred_unit = split_quantity_unit(quantity)
    if not unit:
        unit = inferred_unit
    unit_price = at("单价")
    line_amount = at("总金额", "明细总价", "含税总价", "不含税总价", "总价", "金额", "小计")
    header_delivery = delivery_from_header(header_raw or [])
    delivery = at("交期", "交货", "交付")
    if is_order_total_text(delivery):
        delivery = header_delivery
    elif not delivery:
        delivery = header_delivery
    if is_order_total_row(row_no, product_name, specification):
        return None
    if not product_name and not specification:
        return None
    if not parse_decimal(quantity):
        return None
    return {
        "row_no": row_no,
        "product_name": product_name,
        "specification": specification,
        "quantity": decimal_text(quantity),
        "unit": unit,
        "unit_price": decimal_text(unit_price),
        "line_amount": decimal_text(line_amount),
        "delivery": delivery,
        "source": "purchase_table",
    }


def infer_purchase_item_from_cells(cells: list[str]) -> dict[str, Any] | None:
    clean_cells = [clean_value(cell) for cell in cells if clean_value(cell)]
    if len(clean_cells) < 4 or is_order_total_row(*clean_cells):
        return None
    row_no = clean_cells[0] if re.fullmatch(r"\d+", clean_cells[0]) else ""
    candidate_cells = clean_cells[1:] if row_no else clean_cells
    money_values = [cell for cell in candidate_cells if parse_decimal(cell) is not None and re.search(r"[¥￥元]|^\d+(?:\.\d+)?$", cell)]
    quantity_index = next((idx for idx, cell in enumerate(candidate_cells) if is_quantity_cell(cell)), -1)
    if quantity_index < 0 or not money_values:
        return None
    product_index = next((idx for idx, cell in enumerate(candidate_cells) if idx != quantity_index and looks_like_product_text(cell)), -1)
    if product_index < 0:
        return None
    spec_index = next((idx for idx, cell in enumerate(candidate_cells) if idx not in {product_index, quantity_index} and looks_like_spec_text(cell)), -1)
    quantity, unit = split_quantity_unit(candidate_cells[quantity_index])
    numeric_money = [cell for cell in money_values if cell != candidate_cells[quantity_index]]
    unit_price = numeric_money[0] if numeric_money else ""
    line_amount = numeric_money[-1] if numeric_money else ""
    return {
        "row_no": row_no,
        "product_name": candidate_cells[product_index],
        "specification": candidate_cells[spec_index] if spec_index >= 0 else "",
        "quantity": decimal_text(quantity),
        "unit": unit,
        "unit_price": decimal_text(unit_price),
        "line_amount": decimal_text(line_amount),
        "delivery": "",
        "source": "purchase_table_inferred",
    }


def extract_vertical_purchase_table_items(lines: list[str]) -> list[dict[str, Any]]:
    for index in range(len(lines)):
        if not is_table_header_label(lines[index]):
            continue
        header_lines: list[str] = []
        for line in lines[index : index + 10]:
            if header_lines and re.fullmatch(r"\d+", clean_value(line)):
                break
            if is_table_header_label(line):
                header_lines.append(line)
                continue
            if header_lines and len(header_lines) >= 4:
                break
        normalized = [normalize_key(line) for line in header_lines]
        if not is_purchase_table_header(normalized):
            continue
        value_start = index + len(header_lines)
        values = lines[value_start : value_start + len(header_lines)]
        if len(values) < 4:
            continue
        header = normalized
        item = row_to_purchase_item(header, values, header_lines)
        if item:
            item["source"] = "purchase_table_vertical"
            return [item]
    return []


def in_purchase_table_context(lines: list[str], current_line: str) -> bool:
    try:
        index = lines.index(current_line)
    except ValueError:
        return False
    context = "\n".join(lines[max(0, index - 6) : index + 2])
    return bool(re.search(r"供货内容|价格和数量|产品参数|采购明细|货物清单|产品清单|订单产品|采购以下产品", context))


def is_quantity_cell(value: str) -> bool:
    quantity, _unit = split_quantity_unit(value)
    return parse_decimal(quantity) is not None and bool(re.fullmatch(r"\d+(?:\.\d+)?(?:台|套|个|件|pcs|PCS)?", clean_value(value)))


def looks_like_product_text(value: str) -> bool:
    text = clean_value(value)
    return bool(text and not parse_decimal(text) and not re.search(r"税率|总价|总金额|合计|小计|人民币|大写|小写", text))


def looks_like_spec_text(value: str) -> bool:
    text = clean_value(value)
    return bool(text and not parse_decimal(text) and re.search(r"[A-Za-z0-9]|基础款|升级款|标准版|旗舰版|配置|规格|型号", text))


def is_table_header_label(value: str) -> bool:
    text = normalize_key(value)
    return bool(re.search(r"序号|产品名称|品名|货物名称|商品名称|设备名称|规格型号|主要规格|详细配置|数量|套数|单位|单价|总价|总金额|金额|含税|不含税|交期|交货|交付", text))


def delivery_from_header(cells: list[str]) -> str:
    for cell in cells:
        if "交期" not in cell and "交货" not in cell and "交付" not in cell:
            continue
        text = re.sub(r"^(?:交期|交货|交付)\s*[:：]?", "", clean_value(cell)).strip()
        return text if text and text not in {"交期", "交货", "交付"} else ""
    return ""


def is_order_total_text(value: Any) -> bool:
    text = str(value or "")
    return bool(re.search(r"订单总金额|大写|小写|伍万元|人民币", text))


def is_order_total_row(*values: Any) -> bool:
    text = "".join(str(value or "") for value in values)
    return bool(re.search(r"订单总金额|合计|小计", text))


def extraction_confidence(buyer: dict[str, str], items: list[dict[str, Any]]) -> int:
    score = 0
    if buyer.get("contact"):
        score += 20
    if buyer.get("phone"):
        score += 20
    if buyer.get("address") and buyer.get("address_quality") != "not_detailed":
        score += 25
    if items:
        score += 25
    if any(item.get("quantity") and item.get("line_amount") for item in items):
        score += 10
    return min(100, score)


def parse_decimal(value: Any) -> Decimal | None:
    text = re.sub(r"[,，￥¥元\s]", "", str(value or ""))
    if not text:
        return None
    match = re.search(r"-?\d+(?:\.\d+)?", text)
    if not match:
        return None
    try:
        return Decimal(match.group(0))
    except (InvalidOperation, ValueError):
        return None


def split_quantity_unit(value: Any) -> tuple[str, str]:
    text = clean_value(value)
    match = re.match(r"^(-?\d+(?:\.\d+)?)([\u4e00-\u9fffA-Za-z]+)$", text)
    if not match:
        return text, ""
    return match.group(1), match.group(2)


def decimal_text(value: Any) -> str:
    number = parse_decimal(value)
    if number is None:
        return ""
    text = format(number.normalize(), "f")
    return text.rstrip("0").rstrip(".") if "." in text else text


def normalize_line(value: str) -> str:
    return re.sub(r"[ \t\r\n]+", " ", str(value or "").replace("\u00a0", " ")).strip()


def clean_value(value: Any) -> str:
    return normalize_line(str(value or "").strip(" ：:;；,，.。"))


def clean_party_value(value: Any) -> str:
    text = clean_value(value)
    text = re.split(
        r"(?:采购单号|采购日期|采购方信息|采购方名称|供方信息|供方名称|供方(?:（[^）]*）)?|乙方(?:（[^）]*）)?|地址|联系人|联络人|电话|手机|联系方式|联系电话)[:：]?",
        text,
        maxsplit=1,
    )[0]
    return clean_value(text)


def normalize_key(value: str) -> str:
    return re.sub(r"[\s（）()：:;；,，.。]+", "", str(value or ""))
