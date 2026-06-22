from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field
from typing import Any

from sqlalchemy.orm import Session

from backend.app.models import CrmSalesOrder, OrderAttachment
from backend.app.services.address_quality import is_detailed_receipt_address
from backend.app.services.attachment_parser import ATTACHMENT_TEXT_PARSER_VERSION, parse_attachment
from backend.app.services.crm_attachment_cache import local_storage_ref
from backend.app.services.jsonutil import dumps, loads
from backend.app.services.purchase_order_extraction import extract_purchase_order_fields
from backend.app.services.storage import read_storage
from backend.app.services.llm_fallback import active_model_config, llm_fallback_enabled, model_ready, parse_json_object, sensitive_llm_allowed
from backend.app.services.model_provider import call_model, extract_chat_content


PHONE_PATTERN = re.compile(r"(?<!\d)(?:1[3-9]\d{9}|0\d{2,3}[-\s]?\d{7,8}(?:[-\s]?\d{1,6})?)(?!\d)")
DATE_PATTERN = re.compile(r"(\d{4}[年./-]\d{1,2}(?:[月./-]\d{1,2}日?)?)")
CONTRACT_SIGNER_HINT = re.compile(r"(合同|签订|签署|签约|甲方|乙方|丙方|法定代表|授权代表|委托代理|经办人|盖章|开户|纳税|contract|signer|authorized representative|party a|party b)", re.IGNORECASE)
LOGISTICS_HINT = re.compile(r"(收货|收件|送货|配送|物流|邮寄|寄送|到货|交货|交付|receiver|recipient|ship|shipping|delivery|logistics|consignee)", re.IGNORECASE)
CONTACT_LABEL = re.compile(r"(?:(?:收货|收件|物流|配送|送货)?(?:联系人|收货人|收件人|联络人)|(?:receiver|recipient|consignee|shipping contact))[:：\s]*([^\s,，;；|/、]+)", re.IGNORECASE)
ADDRESS_LABEL = re.compile(r"(?:收货地址|收件地址|送货地址|配送地址|邮寄地址|寄送地址|设备交付地点及联系人|交付地点及联系人|交货地点及联系人|设备交付地点|交付地点|交货地点|送达地点|shipping\s+address|delivery\s+address|receiver\s+address|recipient\s+address)[:：\s]*(.+)", re.IGNORECASE)
DELIVERY_LABEL = re.compile(r"(?:交货日期|交付日期|期望交期|要求交期|到货日期|交货时间|交付时间|delivery date|expected delivery|ship date)[:：\s]*(.+)", re.IGNORECASE)
PHONE_LABEL = re.compile(r"(?:电话|手机|联系方式|联系电话|phone|mobile|tel)[:：\s]*(.+)", re.IGNORECASE)
BUYER_PARTY_HEADER = re.compile(r"^(?:甲方|采购方|买方|需方)(?:（[^）]*）)?[:：]\s*(.*)$")
SUPPLIER_PARTY_HEADER = re.compile(r"^(?:乙方|供方|供应商|卖方)(?:（[^）]*）)?[:：]")
CONTACT_STOP_WORDS = {"", ":", "：", "信息", "联系人", "收货人", "收件人", "客户", "地址"}
CONTACT_ADDRESS_HINT = re.compile(r"(省|市|区|县|路|街|大道|号|楼|室|园区|大厦|中心|基地|公司)")
CONTACT_NOISE_HINT = re.compile(r"(导致|无法|如期|交付|设备|承担|产生|费用|信息|配送|基础|收货人等|乙方|甲方|合同|协议|采购|订单|地址)")
ATTACHMENT_PARSER_VERSION = f"crm-{ATTACHMENT_TEXT_PARSER_VERSION}"
OMS_FIELD_EXTRACTOR_VERSION = "buyer-party-priority-v2"


@dataclass
class ExtractedOmsFields:
    receipt_contact: str = ""
    receipt_phone: str = ""
    receipt_address: str = ""
    delivery_date: str = ""
    confidence: int = 0
    source: str = "rule"
    evidence: list[dict[str, str]] = field(default_factory=list)
    validation_errors: list[str] = field(default_factory=list)
    manual_review_required: bool = False
    attachment_hash: str = ""
    parser_version: str = ATTACHMENT_PARSER_VERSION
    extractor_version: str = OMS_FIELD_EXTRACTOR_VERSION

    def missing_keys(self) -> list[str]:
        return [key for key in ("receipt_contact", "receipt_phone", "receipt_address", "delivery_date") if not getattr(self, key)]

    def as_dict(self) -> dict[str, Any]:
        return {
            "receipt_contact": self.receipt_contact,
            "receipt_phone": self.receipt_phone,
            "receipt_address": self.receipt_address,
            "delivery_date": self.delivery_date,
            "confidence": self.confidence,
            "source": self.source,
            "evidence": self.evidence,
            "validation_errors": self.validation_errors,
            "manual_review_required": self.manual_review_required,
            "attachment_hash": self.attachment_hash,
            "parser_version": self.parser_version,
            "extractor_version": self.extractor_version,
        }


def normalize_text(value: Any) -> str:
    return re.sub(r"[ \t\r\n]+", " ", str(value or "").replace("\u00a0", " ")).strip()


def attachment_text_signature(attachment_texts: list[tuple[OrderAttachment | None, str]]) -> str:
    digest = hashlib.sha256()
    for attachment, text in attachment_texts:
        clean_text = str(text or "").strip()
        if not clean_text:
            continue
        file_name = attachment.file_name if attachment else "inline"
        fingerprint = attachment.fingerprint if attachment else ""
        digest.update(str(file_name or "").encode("utf-8", errors="replace"))
        digest.update(b"\0")
        digest.update(str(fingerprint or "").encode("utf-8", errors="replace"))
        digest.update(b"\0")
        digest.update(clean_text.encode("utf-8", errors="replace"))
        digest.update(b"\0")
    return digest.hexdigest()


def extraction_result_from_dict(data: Any) -> ExtractedOmsFields | None:
    if not isinstance(data, dict):
        return None
    allowed = {field.name for field in ExtractedOmsFields.__dataclass_fields__.values()}
    values = {key: value for key, value in data.items() if key in allowed}
    try:
        return ExtractedOmsFields(**values)
    except TypeError:
        return None


def extraction_quality(result: ExtractedOmsFields | None) -> tuple[int, int, int, int]:
    if result is None:
        return (0, 0, 0, 0)
    has_contact = int(is_valid_receipt_contact(result.receipt_contact))
    has_phone = int(is_valid_receipt_phone(result.receipt_phone))
    has_address = int(is_detailed_receipt_address(result.receipt_address))
    complete = int(has_contact and has_phone and has_address and not result.manual_review_required)
    valid_count = has_contact + has_phone + has_address
    confidence = max(0, min(100, int(result.confidence or 0)))
    return (complete, valid_count, confidence, -int(result.manual_review_required))


def is_current_extraction(previous: dict[str, Any], attachment_hash: str) -> bool:
    return (
        str(previous.get("attachment_hash") or "") == attachment_hash
        and str(previous.get("parser_version") or "") == ATTACHMENT_PARSER_VERSION
        and str(previous.get("extractor_version") or "") == OMS_FIELD_EXTRACTOR_VERSION
    )


def normalize_date(value: str) -> str:
    text = normalize_text(value)
    match = DATE_PATTERN.search(text)
    if not match:
        return ""
    date = match.group(1).replace("年", "-").replace("月", "-").replace("日", "").replace("/", "-").replace(".", "-")
    parts = [part for part in date.split("-") if part]
    if len(parts) == 2:
        return f"{parts[0]}-{int(parts[1]):02d}"
    if len(parts) >= 3:
        return f"{parts[0]}-{int(parts[1]):02d}-{int(parts[2]):02d}"
    return ""


def clean_contact(value: str) -> str:
    text = normalize_text(value)
    text = PHONE_PATTERN.sub("", text)
    text = re.sub(r"^(收货人|收件人|联系人|物流联系人|配送联系人)[:：]?", "", text).strip()
    parts = [part.strip(" ：:;；,，.。()（）[]【】") for part in re.split(r"[,，;；|/、\s]+", text) if part.strip()]
    for part in reversed(parts or [text]):
        if is_valid_receipt_contact(part):
            return part[:64]
    return ""


def clean_address(value: str) -> str:
    text = normalize_text(value)
    text = PHONE_PATTERN.sub("", text)
    text = re.split(r"(?:收货人|收件人|联系人|电话|手机|联系方式|交期|交货日期|交付日期|receiver|recipient|phone|mobile|tel|delivery date)[:：]", text, maxsplit=1, flags=re.IGNORECASE)[0]
    candidates = [part.strip(" ：:;；,，.。") for part in re.split(r"[,，;；|]", text) if part.strip()]
    for candidate in candidates or [text]:
        if is_detailed_receipt_address(candidate):
            return candidate[:500]
    return text.strip(" ：:;；,，")[:500]


def is_valid_receipt_contact(value: str) -> bool:
    text = normalize_text(value).strip(" ：:;；,，.。()（）[]【】")
    if text in CONTACT_STOP_WORDS or len(text) < 2 or len(text) > 20:
        return False
    if re.fullmatch(r"[A-Za-z]{1,3}", text):
        return False
    if PHONE_PATTERN.search(text) or CONTACT_ADDRESS_HINT.search(text):
        return False
    if CONTACT_NOISE_HINT.search(text):
        return False
    return bool(re.search(r"[\u4e00-\u9fffA-Za-z]", text))


def normalize_phone(value: str) -> str:
    match = PHONE_PATTERN.search(normalize_text(value))
    if not match:
        return ""
    return re.sub(r"\s+", "", match.group(0))


def is_valid_receipt_phone(value: str) -> bool:
    phone = normalize_phone(value)
    return bool(phone and PHONE_PATTERN.fullmatch(phone))


def validate_extracted_fields(result: ExtractedOmsFields) -> list[str]:
    errors: list[str] = []
    if not is_valid_receipt_contact(result.receipt_contact):
        errors.append("收货联系人未通过校验")
    if not is_valid_receipt_phone(result.receipt_phone):
        errors.append("联系方式电话未通过校验")
    if not is_detailed_receipt_address(result.receipt_address):
        errors.append("收货地址不是可邮寄详细地址")
    return errors


def is_extracted_complete_and_valid(result: ExtractedOmsFields) -> bool:
    return not validate_extracted_fields(result)


def is_contract_signer_line(line: str) -> bool:
    return bool(CONTRACT_SIGNER_HINT.search(line)) and not bool(LOGISTICS_HINT.search(line))


def merge_field(target: ExtractedOmsFields, key: str, value: str, line: str, confidence: int) -> None:
    value = normalize_text(value)
    if not value or getattr(target, key):
        return
    if key == "receipt_address" and not is_detailed_receipt_address(value):
        return
    if key == "receipt_contact" and not is_valid_receipt_contact(value):
        return
    if key == "receipt_phone" and not is_valid_receipt_phone(value):
        return
    if key == "receipt_phone":
        value = normalize_phone(value)
    setattr(target, key, value)
    target.confidence = max(target.confidence, confidence)
    target.evidence.append({"field": key, "value": value, "text": line[:300]})


def extract_oms_fields_by_rule(text: str) -> ExtractedOmsFields:
    result = ExtractedOmsFields(source="rule")
    extract_priority_buyer_blocks(text, result)
    merge_purchase_order_buyer_fallback(text, result)
    for raw_line in text.splitlines():
        line = normalize_text(raw_line)
        if not line or is_contract_signer_line(line):
            continue

        phone = PHONE_PATTERN.search(line)
        if phone and (LOGISTICS_HINT.search(line) or result.receipt_contact or result.receipt_address):
            merge_field(result, "receipt_phone", phone.group(0), line, 75)

        contact = CONTACT_LABEL.search(line)
        if contact and not is_contract_signer_line(line):
            merge_field(result, "receipt_contact", clean_contact(contact.group(1)), line, 80)

        address = ADDRESS_LABEL.search(line)
        if address and LOGISTICS_HINT.search(line):
            merge_field(result, "receipt_address", clean_address(address.group(1)), line, 85)

        delivery = DELIVERY_LABEL.search(line)
        if delivery:
            merge_field(result, "delivery_date", normalize_date(delivery.group(1)), line, 75)

    result.confidence = result.confidence if not result.missing_keys() else min(result.confidence, 70)
    result.validation_errors = validate_extracted_fields(result)
    return result


def merge_purchase_order_buyer_fallback(text: str, result: ExtractedOmsFields) -> None:
    structured = extract_purchase_order_fields(text)
    buyer = structured.get("buyer") if isinstance(structured, dict) else {}
    if not isinstance(buyer, dict):
        return
    if not result.receipt_contact and buyer.get("contact"):
        merge_field(result, "receipt_contact", str(buyer.get("contact") or ""), "采购方/甲方结构化兜底", 88)
    if not result.receipt_phone and buyer.get("phone"):
        merge_field(result, "receipt_phone", str(buyer.get("phone") or ""), "采购方/甲方结构化兜底", 88)
    if not result.receipt_address and buyer.get("address"):
        merge_field(result, "receipt_address", str(buyer.get("address") or ""), "采购方/甲方结构化兜底", 88)


def extract_priority_buyer_blocks(text: str, result: ExtractedOmsFields) -> None:
    lines = [normalize_text(line) for line in text.splitlines() if normalize_text(line)]
    extract_contract_buyer_party_blocks(lines, result)
    for index, line in enumerate(lines):
        if not re.search(r"(采购方信息|甲方信息|买方信息|需方信息|购买方信息)", line):
            continue
        block = "\n".join(lines[index : index + 8])
        for block_line in block.splitlines():
            address = ADDRESS_LABEL.search(block_line) or re.search(r"地址[:：\s]*(.+)", block_line)
            if address:
                merge_field(result, "receipt_address", clean_address(address.group(1)), block_line, 92)
            contact = CONTACT_LABEL.search(block_line)
            if contact:
                merge_field(result, "receipt_contact", clean_contact(contact.group(1)), block_line, 92)
            phone = PHONE_PATTERN.search(block_line)
            if phone and re.search(r"(电话|手机|联系方式|联系电话)", block_line):
                merge_field(result, "receipt_phone", phone.group(0), block_line, 92)
    for line in lines:
        if not re.search(r"(交付地点及联系人|交货地点及联系人|设备交付地点及联系人)", line):
            continue
        address = ADDRESS_LABEL.search(line)
        if address:
            merge_field(result, "receipt_address", clean_address(address.group(1)), line, 90)
        phone = PHONE_PATTERN.search(line)
        if phone:
            merge_field(result, "receipt_phone", phone.group(0), line, 90)
        without_phone = PHONE_PATTERN.sub("", line)
        parts = [part.strip(" ：:;；,，.。") for part in re.split(r"[,，;；|]", without_phone) if part.strip()]
        for part in reversed(parts):
            if is_valid_receipt_contact(part):
                merge_field(result, "receipt_contact", part, line, 90)
                break


def extract_contract_buyer_party_blocks(lines: list[str], result: ExtractedOmsFields) -> None:
    for index, line in enumerate(lines):
        if not BUYER_PARTY_HEADER.search(line):
            continue
        block_lines = [line]
        for next_line in lines[index + 1 : index + 8]:
            if SUPPLIER_PARTY_HEADER.search(next_line):
                break
            block_lines.append(next_line)
        block = "\n".join(block_lines)
        for block_line in block_lines:
            address = re.search(r"地址[:：\s]*(.+)", block_line)
            if address:
                merge_field(result, "receipt_address", clean_address(address.group(1)), block_line, 94)
            contact = re.search(r"(?:联系人|联络人)[:：\s]*(.+)", block_line)
            if contact:
                merge_field(result, "receipt_contact", clean_contact(contact.group(1)), block_line, 94)
            phone = PHONE_LABEL.search(block_line) or PHONE_PATTERN.search(block_line)
            if phone:
                value = phone.group(1) if hasattr(phone, "lastindex") and phone.lastindex else phone.group(0)
                merge_field(result, "receipt_phone", value, block_line, 94)
        if all((result.receipt_contact, result.receipt_phone, result.receipt_address)):
            result.evidence.append({"field": "buyer_party_block", "value": "matched", "text": block[:300]})
            return


def extract_oms_fields_with_llm(session: Session, order: CrmSalesOrder, source_text: str, current: ExtractedOmsFields) -> ExtractedOmsFields:
    current.validation_errors = validate_extracted_fields(current)
    if not llm_fallback_enabled(session):
        return current
    config = active_model_config(session)
    if not model_ready(session, config):
        return current
    assert config is not None
    if not sensitive_llm_allowed(session, config, config_key="crm_attachment_llm_allow_external_sensitive"):
        current.evidence.append(
            {
                "field": "llm_privacy_blocked",
                "value": "crm_attachment_llm_allow_external_sensitive=false",
                "text": "CRM attachment text contains receiver/contact PII; external LLM fallback skipped",
            }
        )
        return current
    try:
        structured = extract_purchase_order_fields(source_text)
        output = call_model(
            session,
            config,
            task_type="CrmAttachmentOmsFieldExtraction",
            related_object_type="CrmSalesOrder",
            related_object_id=order.id,
            timeout_seconds=45,
            messages=[
                {
                    "role": "system",
                    "content": (
                        "你是CRM订单附件里的OMS发货字段抽取器。只返回JSON。"
                        "必须区分合同签订人/授权代表/甲乙方联系人 与 物流收货联系人/收件人。"
                        "在采购订单里，如果没有单独的物流收货信息，优先把采购方/甲方/买方/需方信息作为收货信息。"
                        "不要把供方/乙方/供应商/卖方的联系人和电话当作收货信息。"
                        "联系人必须是人名或明确收货联系人；电话必须和该联系人/采购方块同上下文；地址必须是可邮寄详细地址。"
                        "不确定返回空字符串。"
                    ),
                },
                {
                    "role": "user",
                    "content": (
                        f"CRM订单号：{order.crm_order_no}\n客户：{order.customer_name or ''}\n"
                        f"已规则识别：{dumps(current.as_dict())}\n"
                        f"采购订单结构化解析：{dumps(structured)}\n\n附件文本：\n{source_text[:8000]}\n\n"
                        "返回格式：{\"receipt_contact\":\"\",\"receipt_phone\":\"\",\"receipt_address\":\"\","
                        "\"delivery_date\":\"YYYY-MM-DD或空\",\"confidence\":0-100,\"reason\":\"\"}"
                    ),
                },
            ],
        )
    except Exception as exc:
        current.evidence.append({"field": "llm_error", "value": str(exc)[:300], "text": "LLM fallback failed"})
        return current
    data = parse_json_object(extract_chat_content(output))
    if not data:
        return current
    merged = ExtractedOmsFields(**current.as_dict())
    merged.source = "llm"
    merged.receipt_contact = ""
    merged.receipt_phone = ""
    merged.receipt_address = ""
    merged.delivery_date = ""
    merged.evidence = [item for item in merged.evidence if item.get("field") not in {"receipt_contact", "receipt_phone", "receipt_address", "delivery_date"}]
    for key in ("receipt_contact", "receipt_phone", "receipt_address"):
        value = normalize_text(data.get(key))
        if key == "receipt_contact":
            valid = is_valid_receipt_contact(value)
        elif key == "receipt_phone":
            value = normalize_phone(value)
            valid = is_valid_receipt_phone(value)
        else:
            valid = is_detailed_receipt_address(value)
        if value and valid:
            setattr(merged, key, value)
            merged.evidence.append({"field": key, "value": value, "text": "LLM fallback"})
    date = normalize_date(str(data.get("delivery_date") or ""))
    if date and not merged.delivery_date:
        merged.delivery_date = date
        merged.evidence.append({"field": "delivery_date", "value": date, "text": "LLM fallback"})
    try:
        merged.confidence = max(merged.confidence, min(100, max(0, int(data.get("confidence") or 0))))
    except (TypeError, ValueError):
        pass
    if data.get("reason"):
        merged.evidence.append({"field": "llm_reason", "value": normalize_text(data.get("reason")), "text": "LLM fallback"})
    merged.validation_errors = validate_extracted_fields(merged)
    merged.manual_review_required = bool(merged.validation_errors)
    return merged


def download_attachment_text(attachment: OrderAttachment, *, timeout_seconds: float = 20.0, max_bytes: int = 10 * 1024 * 1024) -> tuple[str, dict[str, Any]]:
    try:
        ref = local_storage_ref(attachment)
        if ref:
            content = read_storage(ref)[:max_bytes]
        else:
            return "", {"status": "Skipped", "reason": "missing local cached file"}
        parsed = parse_attachment(attachment.file_name, content, max_zip_bytes=max_bytes, max_depth=2)
        return parsed.text, {
            "status": parsed.status,
            "error": parsed.error,
            "text_length": len(parsed.text),
            **parsed.metadata,
        }
    except Exception as exc:
        return "", {"status": "Failed", "error": str(exc)[:1000]}


def enrich_order_from_attachment_text(session: Session, order: CrmSalesOrder, attachment_texts: list[tuple[OrderAttachment | None, str]]) -> ExtractedOmsFields:
    source_text = "\n\n".join(f"[{attachment.file_name if attachment else 'inline'}]\n{text}" for attachment, text in attachment_texts if text.strip())
    attachment_hash = attachment_text_signature(attachment_texts)
    raw = loads(order.raw_json, {})
    previous = extraction_result_from_dict(raw.get("oms_field_extraction"))
    rule_result = extract_oms_fields_by_rule(source_text)
    if validate_extracted_fields(rule_result):
        rule_result.validation_errors = ["联系人三要素已切换为 LLM 主提取，规则结果仅作上下文参考"]
        result = extract_oms_fields_with_llm(session, order, source_text, rule_result)
    else:
        result = rule_result
    result.attachment_hash = attachment_hash
    result.parser_version = ATTACHMENT_PARSER_VERSION
    result.extractor_version = OMS_FIELD_EXTRACTOR_VERSION
    result.validation_errors = validate_extracted_fields(result)
    result.manual_review_required = bool(result.validation_errors)
    if previous is not None and extraction_quality(previous) > extraction_quality(result):
        previous.evidence.append(
            {
                "field": "stale_lower_quality_extraction_skipped",
                "value": attachment_hash,
                "text": "New extraction result had lower quality than saved result and was not applied.",
            }
        )
        apply_extracted_fields(order, previous)
        raw["oms_field_extraction"] = previous.as_dict()
        order.raw_json = dumps(raw)
        return previous
    apply_extracted_fields(order, result)
    raw["oms_field_extraction"] = result.as_dict()
    for key in ("receipt_contact", "receipt_phone", "receipt_address", "delivery_date"):
        if getattr(order, key, None):
            raw[key] = getattr(order, key)
    order.raw_json = dumps(raw)
    return result


def apply_extracted_fields(order: CrmSalesOrder, extracted: ExtractedOmsFields) -> None:
    if extracted.receipt_contact and is_valid_receipt_contact(extracted.receipt_contact) and not is_valid_receipt_contact(order.receipt_contact):
        order.receipt_contact = extracted.receipt_contact
    if extracted.receipt_phone and is_valid_receipt_phone(extracted.receipt_phone) and not is_valid_receipt_phone(order.receipt_phone):
        order.receipt_phone = extracted.receipt_phone
    if extracted.receipt_address and not is_detailed_receipt_address(order.receipt_address):
        order.receipt_address = extracted.receipt_address
    if extracted.delivery_date and not normalize_text(order.delivery_date):
        order.delivery_date = extracted.delivery_date


def enrich_order_from_registered_attachments(session: Session, order: CrmSalesOrder) -> ExtractedOmsFields | None:
    raw = loads(order.raw_json, {})
    previous_extraction = raw.get("oms_field_extraction")
    missing = [
        key
        for key in ("receipt_contact", "receipt_phone", "receipt_address", "delivery_date")
        if not normalize_text(getattr(order, key, ""))
        or (key == "receipt_contact" and not is_valid_receipt_contact(getattr(order, key, "")))
        or (key == "receipt_phone" and not is_valid_receipt_phone(getattr(order, key, "")))
        or (key == "receipt_address" and not is_detailed_receipt_address(getattr(order, key, "")))
    ]
    attachments = (
        session.query(OrderAttachment)
        .filter(OrderAttachment.source_system == order.source_system, OrderAttachment.crm_order_id == order.crm_order_id, OrderAttachment.payload_hash == order.payload_hash)
        .order_by(OrderAttachment.created_at.asc())
        .all()
    )
    texts: list[tuple[OrderAttachment | None, str]] = []
    for attachment in attachments:
        evidence = loads(attachment.evidence_json, {})
        cached_text = str(evidence.get("parsed_text") or "").strip()
        download_parse = evidence.get("download_parse") if isinstance(evidence.get("download_parse"), dict) else {}
        cached_parser_version = str(evidence.get("parser_version") or download_parse.get("parser_version") or "")
        if cached_text and cached_parser_version == ATTACHMENT_TEXT_PARSER_VERSION:
            texts.append((attachment, cached_text))
            continue
        text, parse_info = download_attachment_text(attachment)
        evidence["download_parse"] = parse_info
        attachment.evidence_json = dumps(evidence)
        if text:
            attachment.parse_status = "Parsed"
            structured = extract_purchase_order_fields(text)
            evidence["parsed_text"] = text[:20000]
            evidence["parser_version"] = ATTACHMENT_TEXT_PARSER_VERSION
            evidence["ocr_engine"] = parse_info.get("ocr_engine")
            evidence["purchase_order_extraction"] = structured
            attachment.evidence_json = dumps(evidence)
            texts.append((attachment, text))
        elif attachment.parse_status == "Registered":
            attachment.parse_status = "ParseFailed" if parse_info.get("status") == "Failed" else attachment.parse_status
    if not texts:
        return None
    attachment_hash = attachment_text_signature(texts)
    if isinstance(previous_extraction, dict) and previous_extraction and is_current_extraction(previous_extraction, attachment_hash):
        return None
    if not missing:
        return None
    result = enrich_order_from_attachment_text(session, order, texts)
    for attachment, _text in texts:
        evidence = loads(attachment.evidence_json, {})
        evidence["oms_field_extraction"] = result.as_dict()
        attachment.evidence_json = dumps(evidence)
    return result
