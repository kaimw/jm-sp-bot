from __future__ import annotations

import re
from typing import Any


# ─── 粗糙地址：仅包含城市/大区域名，不视为详细收货地址 ───
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

# 英文粗糙地址：纯国家名、占位符、测试值
_COARSE_ENGLISH_PATTERNS = re.compile(
    r"^\s*(USA|US|U\.?S\.?A?|United\s+States|"
    r"UK|U\.?K\.?|United\s+Kingdom|GB|"
    r"CA|Canada|"
    r"AU|Australia|"
    r"DE|Germany|"
    r"FR|France|"
    r"JP|Japan|"
    r"N/?A|N\.A\.|None|"
    r"TBD|TBC|TBA|"
    r"FBA(\s*(FC|Warehouse|Fulfillment|Center))?|"
    r"Amazon\s*(FC|Fulfillment|Warehouse)?"
    r")\s*$",
    re.IGNORECASE,
)


# ─── 中文地址识别 ───
_DETAIL_ADDRESS_HINT = re.compile(
    r"(\d|路|街|大道|巷|弄|号|栋|幢|楼|层|室|座|园区|产业园|科技园|大厦|中心|基地|厂|院|苑|广场|小区|仓|库)"
)


# ─── 英文地址识别 ───

# 门牌号 + 街道名 + 街道类型后缀：如 "123 Main St" / "333 7th Ave" / "10 Downing Street"
_EN_STREET_PATTERN = re.compile(
    r"\b\d+\s+\w+(\s+\w+)*\s+"
    r"(St(reet)?|Ave(nue)?|Rd|Road|Blvd|Boulevard|Ln|Lane|Dr(ive)?|"
    r"Ct|Court|Pl(ace)?|Way|Pkwy|Parkway|Hwy|Highway|Cir(cle)?|"
    r"Sq(uare)?|Ter(race)?|Trl|Trail|Plz|Plaza|Row|Walk|"
    r"Cres(cent)?|Gate|Mews|Mall|Gdns|Gardens|Grn|Green|"
    r"Loop|Espl|Esplanade|Fwy|Freeway|Tpke|Turnpike|"
    r"Alley|Bnd|Bend|Brg|Bridge|Brk|Brook|Byu|Bayou|"
    r"Crk|Creek|Cswy|Causeway|Cv|Cove|Expy|Expressway|"
    r"Fld|Field|Flt|Flat|Gln|Glen|Hbr|Harbor|Hvn|Haven|"
    r"Is|Island|Jct|Junction|Knl|Knoll|Ldg|Lodge|"
    r"Mdw|Meadow|Mtn|Mountain|Orch|Orchard|Pne|Pine|"
    r"Rdg|Ridge|Shr|Shoal|Spg|Spring|Strm|Stream|"
    r"Vly|Valley|Vw|View|Xing|Crossing)\b",
    re.IGNORECASE,
)

# 单元标识 + 数字：Suite 400, Apt 3B, Floor 5, Building A, Room 101
_EN_UNIT_PATTERN = re.compile(
    r"\b(Suite|Ste|Floor|Fl|Unit|Apt|Apartment|Bldg|Building|Room|Rm)\b\s*[#]?\s*\w+",
    re.IGNORECASE,
)

# 城市, 州 邮编（美国式）：Seattle, WA 98101 / New York, NY 10001-1234
_EN_CITY_STATE_ZIP = re.compile(
    r"[A-Za-z]+(\s+[A-Za-z]+)*,\s*[A-Z]{2}\s+\d{5}(-\d{4})?"
)

# 英国邮编：SW1A 2AA / EC1V 9LB
_EN_UK_POSTCODE = re.compile(
    r"\b[A-Z]{1,2}\d[A-Z\d]?\s*\d[A-Z]{2}\b"
)

# 加拿大邮编：K1A 0B1 / M5V 2T6
_EN_CA_POSTCODE = re.compile(
    r"\b[A-Z]\d[A-Z]\s*\d[A-Z]\d\b"
)

# 通用邮编：如日本 150-0002、德国 10115、澳大利亚 2000
_EN_GENERIC_POSTCODE = re.compile(
    r"\b\d{3,6}([-\s]\d{3,4})?\b"
)


def normalize_address_text(value: Any) -> str:
    return re.sub(r"[ \t\r\n]+", " ", str(value or "").replace(" ", " ")).strip(" ：:;；,，")


def is_coarse_english_address(text: str) -> bool:
    """判断是否为粗糙英文地址（纯国家名、占位符、测试值）。"""
    return bool(_COARSE_ENGLISH_PATTERNS.match(text.strip()))


def is_detailed_english_address(text: str) -> bool:
    """判断是否为详细的英文收货地址。"""
    # 门牌号 + 街道名 + 街道类型（最可靠）
    if _EN_STREET_PATTERN.search(text):
        return True

    # 单元标识 + 数字（Suite/Floor/Apt/Building + number）
    if _EN_UNIT_PATTERN.search(text):
        return True

    # 城市, 州 邮编（美国式）
    if _EN_CITY_STATE_ZIP.search(text):
        return True

    # 英国邮编格式
    if _EN_UK_POSTCODE.search(text):
        return True

    # 加拿大邮编格式
    if _EN_CA_POSTCODE.search(text):
        return True

    # 有街道指示词 + 逗号分隔的城市/州标记
    has_street = bool(re.search(
        r"\b(St\b|Street|Ave\b|Avenue|Rd\b|Road|Lane|Blvd|Boulevard|Drive|Pkwy|Highway|Suite|Floor|Apt|Building)\b",
        text, re.IGNORECASE,
    ))
    has_comma_location = bool(re.search(r",\s*[A-Z]{2}\b", text))
    if has_street and has_comma_location:
        return True

    return False


def is_detailed_receipt_address(value: Any) -> bool:
    """
    判断是否为可邮寄的详细收货地址。

    支持中文地址（街道号、路名、园区等）和英文地址（门牌号+街道名、
    城市+州+邮编、Suite/Apt 等单元标识）。
    """
    text = normalize_address_text(value)
    if not text:
        return False

    # ── 粗糙地址过滤 ──
    # 中文粗糙地址（纯城市名）
    compact = re.sub(r"[\s+省市区县/、,，\-]+", "", text)
    if text in COARSE_ADDRESS_VALUES or compact in COARSE_ADDRESS_VALUES:
        return False

    # 英文粗糙地址（国家名、占位符、测试值）
    if is_coarse_english_address(text):
        return False

    # ── 中文详细地址 ──
    if len(compact) >= 8 and _DETAIL_ADDRESS_HINT.search(text):
        return True

    # ── 英文详细地址 ──
    if is_detailed_english_address(text):
        return True

    return False
