from __future__ import annotations

import html
import re
from dataclasses import dataclass


@dataclass(frozen=True)
class ExtractedRequirement:
    customer_name: str | None
    salesperson_name: str | None
    salesperson_email: str | None
    product_summary: str | None
    quantity_text: str | None
    expected_delivery_date: str | None
    external_order_no: str | None
    missing_fields: list[str]
    risk_flags: list[str]


FIELD_PATTERNS = {
    "customer_name": r"(?:客户|客户名称)[:：]\s*(?P<value>.+)",
    "product_summary": r"(?:物料|物料名称|物料规格|物料/规格|产品|产品名称|产品规格|产品/规格|商品|商品名称|商品规格|商品/规格|规格|品名)[:：]\s*(?P<value>[^\n]+?)(?=\s*(?:数量|交期)[:：]|$)",
    "quantity_text": r"(?:数量|生产数量)[:：]\s*(?P<value>.+)",
    "expected_delivery_date": r"(?:交期|期望交期|交付日期)[:：]\s*(?P<value>.+)",
    "external_order_no": r"(?:订单号|订单编号)[:：]\s*(?P<value>.+)",
}

RISK_KEYWORDS = ["客户未确认", "价格待定", "先生产后补合同", "特急", "加急"]
BOUNCE_KEYWORDS = ["delivery status notification", "undelivered mail", "退信", "投递失败", "邮件投递失败"]
QUOTE_CUT_MARKERS = [
    "\n-----Original Message-----",
    "\n------------------ Original",
    "\n发件人:",
    "\nFrom:",
    "\n在 ",
    "\n原始邮件",
]
ORDER_REQUEST_KEYWORDS = ["生产订单", "生产需求", "订单需求", "下单", "物料", "产品", "商品", "请排产", "排产"]
NATURAL_ORDER_PATTERN = re.compile(
    r"(?P<customer>[\u4e00-\u9fffA-Za-z0-9（）()·._-]{2,40}?)(?:需要|要|需|订购|采购|购买|下单)"
    r"(?P<item>[^，。,；;\n]+)",
    flags=re.IGNORECASE,
)
QUANTITY_PATTERN = re.compile(r"(?P<value>\d+(?:\.\d+)?\s*(?:台|套|件|个|pcs?|PCS|箱|批|组|只|条|张|米))")
DATE_PATTERN = re.compile(r"(?P<value>\d{4}[-/.年]\d{1,2}(?:[-/.月]\d{1,2}(?:日)?|月(?:底|前|完成)?|[-/.]\d{1,2})?)")
ORDER_NO_PATTERN = re.compile(r"(?:订单号|订单编号|编号)[:：\s]*(?P<value>[A-Za-z0-9][A-Za-z0-9._/-]{2,})", flags=re.IGNORECASE)


def normalize_latest_reply(text: str) -> str:
    normalized = html.unescape(text).replace("\xa0", " ").replace("\r\n", "\n").replace("\r", "\n")
    for marker in QUOTE_CUT_MARKERS:
        index = normalized.find(marker)
        if index > 0:
            normalized = normalized[:index]
    normalized = re.sub(r"\n>.*", "", normalized)
    return normalized.strip()


def classify_mail(subject: str, body: str, from_address: str = "") -> tuple[str, int]:
    body = normalize_latest_reply(body)
    text = f"{subject}\n{body}".lower()
    if any(word in text for word in BOUNCE_KEYWORDS) or "mailer-daemon" in from_address.lower():
        return "BounceOrAutoReply", 94
    if any(word in text for word in ["取消订单", "订单取消", "取消生产", "取消任务", "撤回需求", "撤回任务", "撤回订单"]):
        return "OrderCancelRequest", 92
    if any(word in text for word in ["变更", "更改", "修改订单"]):
        return "OrderChangeRequest", 88
    if any(word in text for word in ["确认排产", "安排生产", "可以生产", "已排产", "同意排产", "同意生产", "同意安排生产"]):
        return "ProductionScheduleConfirmation", 90
    if any(word in text for word in ["答复", "回复如下", "确认如下", "补充如下", "补充确认"]):
        return "SalesClarificationReply", 82
    if any(word in text for word in ["驳回", "疑问", "请确认", "信息不足"]):
        return "ProductionQuestion", 86
    if any(word.lower() in text for word in ORDER_REQUEST_KEYWORDS):
        return "SalesOrderRequirement", 91
    if looks_like_natural_order(text):
        return "SalesOrderRequirement", 88
    return "NonTarget", 70


def _match(pattern: str, body: str) -> str | None:
    match = re.search(pattern, body, flags=re.IGNORECASE | re.MULTILINE)
    if not match:
        return None
    return match.group("value").strip()


def looks_like_natural_order(text: str) -> bool:
    if not NATURAL_ORDER_PATTERN.search(text):
        return False
    return QUANTITY_PATTERN.search(text) is not None or any(keyword in text for keyword in ["排产", "生产", "交付", "交期"])


def _clean_value(value: str | None) -> str | None:
    if value is None:
        return None
    cleaned = value.strip(" \t\r\n，。,；;：:")
    return cleaned or None


def _extract_natural_order_values(text: str) -> dict[str, str | None]:
    values: dict[str, str | None] = {
        "customer_name": None,
        "product_summary": None,
        "quantity_text": None,
        "expected_delivery_date": None,
        "external_order_no": None,
    }
    match = NATURAL_ORDER_PATTERN.search(text)
    if match:
        values["customer_name"] = _clean_value(match.group("customer"))
        item_text = match.group("item")
        quantity = QUANTITY_PATTERN.search(item_text)
        if quantity:
            values["quantity_text"] = _clean_value(quantity.group("value"))
            product_text = item_text[: quantity.start()]
        else:
            product_text = item_text
        product_text = re.split(r"(?:请)?排产|生产|交付|交期|到货", product_text, maxsplit=1)[0]
        values["product_summary"] = _clean_value(product_text)

    if values["quantity_text"] is None:
        quantity = QUANTITY_PATTERN.search(text)
        if quantity:
            values["quantity_text"] = _clean_value(quantity.group("value"))
    date = DATE_PATTERN.search(text)
    if date:
        values["expected_delivery_date"] = _clean_value(date.group("value"))
    order_no = ORDER_NO_PATTERN.search(text)
    if order_no:
        values["external_order_no"] = _clean_value(order_no.group("value"))
    return values


def extract_requirement(subject: str, body: str, from_address: str) -> ExtractedRequirement:
    body = normalize_latest_reply(body)
    source_text = f"{subject}\n{body}"
    # 第一优先：规则提取
    values = {field: _match(pattern, body) for field, pattern in FIELD_PATTERNS.items()}

    # 仅当存在未命中字段时，再用自然语言提取兜底，且只补全仍为空的字段
    missing_rule_fields = [field for field, val in values.items() if not val]
    if missing_rule_fields:
        natural_values = _extract_natural_order_values(body)  # 只在正文匹配，避免主题行被误识别
        for field in missing_rule_fields:
            if natural_values.get(field):
                values[field] = natural_values[field]
    missing = [
        label
        for label, value in [
            ("客户名称", values["customer_name"]),
            ("物料/规格", values["product_summary"]),
            ("数量", values["quantity_text"]),
            ("期望交期", values["expected_delivery_date"]),
        ]
        if not value
    ]
    risk_flags = [keyword for keyword in RISK_KEYWORDS if keyword in body or keyword in subject]
    if values["expected_delivery_date"] and not re.search(r"\d{4}[-/.年]\d{1,2}(?:[-/.月]\d{1,2}|月)?", values["expected_delivery_date"]):
        risk_flags.append("交期格式无法校验")
    if values["quantity_text"] and not re.search(r"\d+", values["quantity_text"]):
        risk_flags.append("数量格式无法校验")
    name = from_address.split("@", 1)[0] if from_address else None
    return ExtractedRequirement(
        customer_name=values["customer_name"],
        salesperson_name=name,
        salesperson_email=from_address or None,
        product_summary=values["product_summary"],
        quantity_text=values["quantity_text"],
        expected_delivery_date=values["expected_delivery_date"],
        external_order_no=values["external_order_no"],
        missing_fields=missing,
        risk_flags=risk_flags,
    )
