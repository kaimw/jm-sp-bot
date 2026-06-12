from __future__ import annotations

import re
from typing import Any


COARSE_ADDRESS_VALUES = {
    "北京",
    "北京市",
    "上海",
    "上海市",
    "天津",
    "天津市",
    "重庆",
    "重庆市",
}

DETAIL_ADDRESS_HINT = re.compile(
    r"(\d|路|街|大道|巷|弄|号|栋|幢|楼|层|室|座|园区|产业园|科技园|大厦|中心|基地|厂|院|苑|广场|小区|仓|库)"
)


def normalize_address_text(value: Any) -> str:
    return re.sub(r"[ \t\r\n]+", " ", str(value or "").replace("\u00a0", " ")).strip(" ：:;；,，")


def is_detailed_receipt_address(value: Any) -> bool:
    text = normalize_address_text(value)
    if not text:
        return False
    compact = re.sub(r"[\s+省市区县/、,，-]+", "", text)
    if text in COARSE_ADDRESS_VALUES or compact in COARSE_ADDRESS_VALUES:
        return False
    if len(compact) < 8:
        return False
    return bool(DETAIL_ADDRESS_HINT.search(text))
