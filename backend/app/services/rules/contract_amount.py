"""合同金额附件证据校验规则 — 从附件 evidence_json 中提取合同金额并与 CRM 订单金额比对"""

from decimal import Decimal

from sqlalchemy import or_
from sqlalchemy.orm import Session

from backend.app.models import OrderAttachment
from backend.app.services.rules import BlockerLevel, OrderContext, ValidationResult
from backend.app.services.rules.helpers import parse_decimal
from backend.app.services.jsonutil import loads


class ContractAmountConsistencyRule:
    """检查订单附件中合同/盖章件/客户PO 的金额与 CRM 订单金额是否一致。

    证据来源：order_attachments.evidence_json 中提取的金额字段
    """

    def get_rule_code(self) -> str:
        return "RULE_CONTRACT_AMOUNT_CONSISTENCY"

    def supports(self, context: OrderContext) -> bool:
        # 备货订单跳过合同金额一致性检查
        if context.order.order_type == "STOCK_REPLENISHMENT":
            return False
        return bool(context.order.order_amount) and context.order.order_amount > 0

    def validate(self, context: OrderContext) -> ValidationResult:
        order_amount = parse_decimal(context.order.order_amount)
        if order_amount is None or order_amount == Decimal("0"):
            return ValidationResult(self.get_rule_code(), True)

        attachments = _load_attachments(context.session, context.crm_order.crm_order_id, context.order.payload_hash)
        if not attachments:
            return ValidationResult(self.get_rule_code(), True, evidence_refs=["未找到当前快照的附件记录，跳过合同金额校验"])

        mismatches = []
        evidence_refs = []
        for att in attachments:
            evidence = loads(att.evidence_json, {})
            # 从附件证据中提取金额
            contract_amount = _extract_amount(evidence)
            if contract_amount is None:
                continue
            ref = f"附件 {att.file_name}: 合同金额 {contract_amount} vs CRM 订单金额 {order_amount}"
            evidence_refs.append(ref)
            if abs(contract_amount - order_amount) > Decimal("0.02"):
                mismatches.append(ref)

        if not evidence_refs:
            # 无金额证据可比较，放行（不阻断）
            return ValidationResult(
                self.get_rule_code(),
                True,
                evidence_refs=["附件中未提取到金额信息，跳过合同金额比对"],
            )

        if mismatches:
            return ValidationResult(
                self.get_rule_code(),
                False,
                BlockerLevel.HIGH,
                "合同金额与CRM订单金额不一致：" + "；".join(mismatches[:3]),
                mismatches,
            )
        return ValidationResult(self.get_rule_code(), True, evidence_refs=evidence_refs)


def _load_attachments(session: Session, crm_order_id: str, payload_hash: str) -> list[OrderAttachment]:
    return (
        session.query(OrderAttachment)
        .filter(
            OrderAttachment.crm_order_id == crm_order_id,
            OrderAttachment.payload_hash == payload_hash,
            or_(
                OrderAttachment.attachment_type.in_(("Contract", "StampedContract", "CustomerPO")),
                OrderAttachment.attachment_type.is_(None),
                OrderAttachment.attachment_type == "",
            ),
        )
        .order_by(OrderAttachment.created_at)
        .all()
    )


def _extract_amount(evidence: dict) -> Decimal | None:
    """从附件 evidence_json 中提取合同金额。支持多种路径格式。"""
    for key in ("contract_amount", "total_amount", "order_total", "金额", "总金额", "合同金额"):
        val = evidence.get(key)
        amt = parse_decimal(val)
        if amt is not None:
            return amt
    # 从嵌套结构中提取
    for container in (evidence.get("extracted"), evidence.get("fields"), evidence.get("items")):
        if isinstance(container, dict):
            for key, val in container.items():
                amt = parse_decimal(val)
                if amt is not None and amt > 0:
                    return amt
    return None
