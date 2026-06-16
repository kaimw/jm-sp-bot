"""OMS 多仓库路由推荐。

根据收件地址、渠道、邮编自动推荐发货仓库编码。
优先级: 邮编精确匹配 > 渠道默认 > 全局默认
"""

from __future__ import annotations

from sqlalchemy.orm import Session

from backend.app.services.rules.helpers import config_dict, config_value


def warehouse_routing(
    session: Session,
    *,
    receipt_address: str = "",
    channel_code: str = "",
    shop_code: str = "",
) -> str | None:
    """
    根据收件信息和渠道推荐发货仓库编码。
    返回 None 表示未找到推荐仓库。
    """
    rules = config_dict(session, "oms_warehouse_routing_json", {})

    # 1. 邮编精确匹配
    postcode = _extract_postcode(receipt_address)
    if postcode:
        for key, wh_code in rules.items():
            if key == postcode or key == f"POST:{postcode}":
                return str(wh_code)

    # 2. 渠道默认
    if channel_code:
        channel_key = f"CHANNEL:{channel_code.lower()}"
        if channel_key in rules:
            return str(rules[channel_key])
    if shop_code:
        shop_key = f"SHOP:{shop_code.lower()}"
        if shop_key in rules:
            return str(rules[shop_key])

    # 3. 地理关键字
    address_lower = receipt_address.lower()
    region_map = {
        "us_west": ["wa", "oregon", "california", "ca ", "nv ", "seattle", "los angeles", "san francisco", "portland"],
        "us_east": ["ny ", "new york", "nj ", "fl ", "miami", "boston", "atlanta"],
        "eu_de": ["deutschland", "germany", "berlin", "münchen", "frankfurt"],
        "eu_uk": ["uk", "london", "manchester", "birmingham"],
        "cn_sz": ["深圳", "东莞", "惠州"],
        "cn_bj": ["北京", "海淀", "朝阳"],
        "cn_sh": ["上海", "浦东", "徐汇", "虹桥"],
    }
    for region, keywords in region_map.items():
        if any(kw in address_lower for kw in keywords):
            region_key = f"REGION:{region}"
            if region_key in rules:
                return str(rules[region_key])

    # 4. 全局默认
    default = config_value(session, "oms_warehouse_code", "").strip()
    return default or None


def _extract_postcode(text: str) -> str:
    import re
    # 中国邮编: 6位
    m = re.search(r"\b(\d{6})\b", text)
    if m:
        return m.group(1)
    # 美国邮编: 5位或5+4
    m = re.search(r"\b(\d{5})(?:-\d{4})?\b", text)
    if m:
        return m.group(1)
    # 英国邮编: SW1A 2AA
    m = re.search(r"\b([A-Z]{1,2}\d[A-Z\d]?\s*\d[A-Z]{2})\b", text, re.IGNORECASE)
    if m:
        return m.group(1).upper()
    return ""
