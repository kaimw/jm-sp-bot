"""V2 预审规则引擎 — 策略模式 + 责任链

所有规则实现 OrderValidationRule Protocol，通过 DEFAULT_RULES 列表注册到责任链引擎。
新增校验逻辑只需新建文件实现 Protocol 并注册即可。
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Any, Protocol

from sqlalchemy.orm import Session

from backend.app.models import SystemConfig
from backend.app.models import CrmSalesOrder, MiddlePlatformOrder, MiddlePlatformOrderItem
from backend.app.services.jsonutil import loads


class BlockerLevel(str, Enum):
    NONE = "NONE"
    LOW = "LOW"
    HIGH = "HIGH"
    CRITICAL = "CRITICAL"


@dataclass
class ValidationResult:
    rule_code: str
    passed: bool
    blocker_level: BlockerLevel = BlockerLevel.NONE
    reason: str = ""
    evidence_refs: list[str] | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "rule_code": self.rule_code,
            "passed": self.passed,
            "blockerLevel": self.blocker_level.value,
            "reason": self.reason,
            "evidenceRefs": self.evidence_refs or [],
        }


@dataclass
class OrderContext:
    order: MiddlePlatformOrder
    crm_order: CrmSalesOrder
    items: list[MiddlePlatformOrderItem]
    session: Session


class OrderValidationRule(Protocol):
    def get_rule_code(self) -> str:
        ...

    def supports(self, context: OrderContext) -> bool:
        ...

    def validate(self, context: OrderContext) -> ValidationResult:
        ...


# ── 规则注册表 ──
from .required_head_fields import RequiredHeadFieldsRule
from .phase_one_completeness import PhaseOneCompletenessRule
from .customer_mapping import CustomerMappingRule
from .positive_amount import PositiveAmountRule
from .amount_consistency import AmountConsistencyRule
from .has_order_items import HasOrderItemsRule
from .known_sku import KnownSkuRule
from .sku_bom_match import SkuBomMatchRule
from .contract_amount import ContractAmountConsistencyRule
from .attachment_product_consistency import AttachmentProductConsistencyRule
from .local_inventory import LocalInventoryAvailableRule

DEFAULT_RULES: list[OrderValidationRule] = [
    RequiredHeadFieldsRule(),
    PhaseOneCompletenessRule(),
    CustomerMappingRule(),
    PositiveAmountRule(),
    AmountConsistencyRule(),
    HasOrderItemsRule(),
    KnownSkuRule(),
    SkuBomMatchRule(),
    ContractAmountConsistencyRule(),
    AttachmentProductConsistencyRule(),
    LocalInventoryAvailableRule(),
]

V2_REVIEW_RULE_STATES_KEY = "v2_review_rule_states_json"

RULE_METADATA: dict[str, dict[str, str]] = {
    "REQUIRED_HEAD_FIELDS": {
        "name": "订单头基础字段",
        "description": "检查中台订单头基础字段是否存在，例如订单号、来源系统、CRM 订单 ID 和客户名称。",
        "default_blocker_level": "CRITICAL",
    },
    "PHASE1_COMPLETE_PRE_REVIEW_FIELDS": {
        "name": "一期完整性预审",
        "description": "检查销售、归属部门、订单日期、结算方式、收货三要素、币种、附件和商品明细等一期必备信息。",
        "default_blocker_level": "CRITICAL",
    },
    "CUSTOMER_MAPPING": {
        "name": "客户主数据映射",
        "description": "检查 CRM 客户是否能映射或查询到 OMS 客户主数据。",
        "default_blocker_level": "CRITICAL",
    },
    "POSITIVE_ORDER_AMOUNT": {
        "name": "订单金额有效性",
        "description": "检查订单金额存在且大于 0。",
        "default_blocker_level": "CRITICAL",
    },
    "AMOUNT_CONSISTENCY": {
        "name": "金额一致性",
        "description": "检查订单金额、商品金额、已收和应收金额之间是否一致。",
        "default_blocker_level": "CRITICAL",
    },
    "HAS_ORDER_ITEMS": {
        "name": "订单商品明细",
        "description": "检查订单明细存在，且数量合法。",
        "default_blocker_level": "CRITICAL",
    },
    "KNOWN_ACTIVE_SKU": {
        "name": "SKU 主数据启用",
        "description": "检查订单 SKU 是否存在且在商品主数据中启用；缺 SKU 时要求人工映射。",
        "default_blocker_level": "HIGH",
    },
    "RULE_SKU_BOM_MATCH": {
        "name": "SKU/BOM 匹配",
        "description": "检查 CRM 商品名称或型号是否能匹配标准 SKU/BOM 主数据。",
        "default_blocker_level": "HIGH",
    },
    "RULE_CONTRACT_AMOUNT_CONSISTENCY": {
        "name": "合同金额一致性",
        "description": "检查附件或合同中识别出的金额与 CRM 订单金额是否一致。",
        "default_blocker_level": "HIGH",
    },
    "ATTACHMENT_PRODUCT_CONSISTENCY": {
        "name": "附件商品一致性",
        "description": "检查 CRM 订单产品与附件解析出的产品、数量、金额是否一致。",
        "default_blocker_level": "CRITICAL",
    },
    "LOCAL_INVENTORY_AVAILABLE": {
        "name": "本地库存可用量",
        "description": "检查本地库存快照是否满足订单发货数量。",
        "default_blocker_level": "HIGH",
    },
}


def review_rule_states(session: Session) -> dict[str, dict[str, Any]]:
    row = session.get(SystemConfig, V2_REVIEW_RULE_STATES_KEY)
    parsed = loads(row.value if row is not None else "{}", {})
    return parsed if isinstance(parsed, dict) else {}


def is_review_rule_enabled(session: Session, rule_code: str) -> bool:
    states = review_rule_states(session)
    state = states.get(rule_code)
    if isinstance(state, dict) and "enabled" in state:
        return bool(state.get("enabled"))
    return True


def review_rule_config(session: Session) -> dict[str, Any]:
    states = review_rule_states(session)
    rules: list[dict[str, Any]] = []
    for index, rule in enumerate(DEFAULT_RULES, start=1):
        code = rule.get_rule_code()
        meta = RULE_METADATA.get(code, {})
        state = states.get(code) if isinstance(states.get(code), dict) else {}
        enabled = bool(state.get("enabled", True))
        rules.append(
            {
                "id": f"v2:{code}",
                "code": code,
                "name": meta.get("name") or code,
                "description": meta.get("description") or "",
                "default_blocker_level": meta.get("default_blocker_level") or "",
                "enabled": enabled,
                "order": index,
                "is_v2_review_rule": True,
            }
        )
    return {"rules": rules}


def register_rule(rule: OrderValidationRule, *, before: str | None = None, after: str | None = None):
    """向责任链动态注册一条规则，可指定插入位置"""
    code = rule.get_rule_code()
    # 去重
    global DEFAULT_RULES
    DEFAULT_RULES = [r for r in DEFAULT_RULES if r.get_rule_code() != code]
    if after:
        for i, r in enumerate(DEFAULT_RULES):
            if r.get_rule_code() == after:
                DEFAULT_RULES.insert(i + 1, rule)
                return
    if before:
        for i, r in enumerate(DEFAULT_RULES):
            if r.get_rule_code() == before:
                DEFAULT_RULES.insert(i, rule)
                return
    DEFAULT_RULES.append(rule)


def remove_rule(rule_code: str):
    """从责任链中移除一条规则"""
    global DEFAULT_RULES
    DEFAULT_RULES = [r for r in DEFAULT_RULES if r.get_rule_code() != rule_code]
