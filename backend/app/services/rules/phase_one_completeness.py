import re

from backend.app.models import OrderAttachment
from backend.app.services.rules import BlockerLevel, OrderContext, ValidationResult
from backend.app.services.rules.helpers import config_bool, is_approved_status
from backend.app.services.address_quality import is_detailed_receipt_address
from backend.app.services.jsonutil import loads


PO_HINT = re.compile(r"(采购订单|采购单|客户\s*PO|\bPO\b|purchase\s+order)", re.IGNORECASE)
CONTRACT_HINT = re.compile(r"(合同|协议|contract|agreement)", re.IGNORECASE)
SIGNED_HINT = re.compile(r"(盖章|签章|回签|签字|签署|授权签字人|公章|印章|用印|signed|signature|stamp|seal)", re.IGNORECASE)


def attachment_evidence_text(context: OrderContext) -> str:
    rows = (
        context.session.query(OrderAttachment)
        .filter(
            OrderAttachment.source_system == context.crm_order.source_system,
            OrderAttachment.crm_order_id == context.crm_order.crm_order_id,
            OrderAttachment.payload_hash == context.crm_order.payload_hash,
        )
        .all()
    )
    parts: list[str] = []
    for row in rows:
        evidence = loads(row.evidence_json, {})
        raw = loads(row.raw_json, {})
        raw_dict = raw if isinstance(raw, dict) else {}
        raw_file_name = raw_dict.get("file_name") or (raw if isinstance(raw, str) else "")
        parts.extend([
            row.file_name or "",
            row.attachment_type or "",
            str(evidence.get("parsed_text") or ""),
            str(raw_file_name or ""),
            str(raw_dict.get("attachment_type") or raw_dict.get("type") or ""),
        ])
    return "；".join(part for part in parts if part)


def has_signed_po_or_contract(text: str) -> bool:
    if not text:
        return False
    has_signed = bool(SIGNED_HINT.search(text))
    if not has_signed:
        return False
    return bool(PO_HINT.search(text) or CONTRACT_HINT.search(text))


class PhaseOneCompletenessRule:
    def get_rule_code(self) -> str:
        return "PHASE1_COMPLETE_PRE_REVIEW_FIELDS"

    def supports(self, context: OrderContext) -> bool:
        return True

    def validate(self, context: OrderContext) -> ValidationResult:
        crm = context.crm_order
        missing: list[str] = []
        invalid: list[str] = []
        raw = loads(crm.raw_json, {})
        attachments = loads(crm.attachment_files_json, [])
        if not isinstance(attachments, list):
            attachments = []

        required = [
            ("sales_user_name", "销售负责人"),
            ("sales_user_email", "销售邮箱"),
            ("owner_department", "归属部门"),
            ("order_date", "订单日期"),
            ("settlement_method", "结算方式"),
            ("receipt_contact", "收货联系人"),
            ("receipt_phone", "收货联系电话"),
            ("receipt_address", "收货地址"),
        ]
        for field, label in required:
            if not str(getattr(crm, field, "") or "").strip():
                missing.append(f"{label}({field})")
        if str(crm.receipt_address or "").strip() and not is_detailed_receipt_address(crm.receipt_address):
            invalid.append(f"收货地址不是可邮寄详细地址：{crm.receipt_address}")

        if not str(context.order.currency or "").strip():
            missing.append("币种(currency)")
        if not attachments:
            missing.append("关键附件(attachment_files)")
        if not context.items:
            missing.append("订单商品明细(order_items)")

        approval_status = str(crm.approval_status or "").strip()
        if approval_status and not is_approved_status(context.session, approval_status):
            invalid.append(f"CRM 审批状态未通过：{approval_status}")

        attachment_names = "；".join(str(item) for item in attachments)
        if attachments and config_bool(context.session, "v2_review_require_key_attachment", True):
            attachment_text = f"{attachment_names}；{attachment_evidence_text(context)}"
            if not has_signed_po_or_contract(attachment_text):
                invalid.append("附件未识别到盖章/签字 PO 或盖章/签字合同")

        if raw.get("life_status") and str(raw.get("life_status")).lower() not in {"normal", "active", "正常"}:
            invalid.append(f"CRM 订单生命状态异常：{raw.get('life_status')}")

        if missing or invalid:
            parts = []
            if missing:
                parts.append("缺少字段：" + "、".join(missing))
            if invalid:
                parts.append("预审不通过：" + "；".join(invalid))
            return ValidationResult(
                self.get_rule_code(),
                False,
                BlockerLevel.CRITICAL,
                "；".join(parts),
                missing + invalid,
            )
        return ValidationResult(self.get_rule_code(), True)
