"""V2 预审规则引擎 — 策略模式 + 责任链

所有规则实现 OrderValidationRule Protocol，通过 DEFAULT_RULES 列表注册到责任链引擎。
新增校验逻辑只需新建文件实现 Protocol 并注册即可。
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Any, Protocol

from sqlalchemy.orm import Session

from backend.app.models import CrmSalesOrder, MiddlePlatformOrder, MiddlePlatformOrderItem


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
