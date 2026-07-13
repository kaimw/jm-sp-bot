from __future__ import annotations

from typing import Any


DOMESTIC_SETTLEMENT_METHOD = "人民币结算"

_COUNTRY_KEYS = (
    "country_region",
    "country",
    "region",
    "国家地区",
    "国家",
    "地区",
)

_SIGNAL_KEYS = (
    *_COUNTRY_KEYS,
    "business_type",
    "业务类型",
    "customer_source",
    "客户来源",
    "channel",
    "channel_code",
    "platform",
    "owner_department",
    "department",
    "receipt_address",
    "customer_name",
)

_DOMESTIC_COUNTRY_VALUES = {"中国", "中华人民共和国", "china", "cn", "prc", "中国大陆", "大陆"}
_OVERSEAS_TOKENS = (
    "海外",
    "国外",
    "跨境",
    "外贸",
    "北美",
    "欧洲",
    "美国",
    "加拿大",
    "日本",
    "韩国",
    "德国",
    "法国",
    "英国",
    "澳大利亚",
    "东南亚",
    "海外美元",
    "usd",
    "eur",
    "jpy",
    "hkd",
    "llc",
    "inc",
    "ltd.",
)


def _text(value: Any) -> str:
    return str(value or "").strip()


def _value_from_keys(payload: dict[str, Any], keys: tuple[str, ...]) -> str:
    for key in keys:
        value = _text(payload.get(key))
        if value:
            return value
    return ""


def is_overseas_order_payload(payload: dict[str, Any] | None) -> bool:
    if not isinstance(payload, dict):
        return False
    country = _value_from_keys(payload, _COUNTRY_KEYS)
    if country:
        normalized = country.strip().lower()
        if normalized in _DOMESTIC_COUNTRY_VALUES:
            return False
        return True
    signal_text = " ".join(_text(payload.get(key)) for key in _SIGNAL_KEYS if _text(payload.get(key))).lower()
    return any(token in signal_text for token in _OVERSEAS_TOKENS)
