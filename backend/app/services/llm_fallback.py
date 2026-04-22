from __future__ import annotations

import json
import re
from dataclasses import dataclass, field

from sqlalchemy.orm import Session

from backend.app.models import MailMessage, ModelProviderConfig, SystemConfig
from backend.app.services.model_provider import call_model, extract_chat_content, resolve_api_key


CLASSIFICATIONS = {
    "SalesOrderRequirement",
    "SalesClarificationReply",
    "ProductionQuestion",
    "ProductionScheduleConfirmation",
    "OrderChangeRequest",
    "OrderCancelRequest",
    "BounceOrAutoReply",
    "NonTarget",
}


@dataclass(frozen=True)
class LLMMailClassification:
    classification: str
    confidence: int
    reason: str = ""
    extracted_requirement: dict[str, str] = field(default_factory=dict)


def llm_fallback_enabled(session: Session) -> bool:
    row = session.get(SystemConfig, "llm_fallback_enabled")
    value = row.value if row is not None else "true"
    return value.lower() in {"1", "true", "yes", "on"}


def active_model_config(session: Session) -> ModelProviderConfig | None:
    return session.query(ModelProviderConfig).filter_by(status="Active").first()


def model_ready(session: Session, config: ModelProviderConfig | None) -> bool:
    return bool(config is not None and resolve_api_key(session, config))


def parse_json_object(text: str) -> dict:
    clean = text.strip()
    if clean.startswith("```"):
        clean = re.sub(r"^```(?:json)?\s*", "", clean)
        clean = re.sub(r"\s*```$", "", clean)
    try:
        data = json.loads(clean)
        return data if isinstance(data, dict) else {}
    except json.JSONDecodeError:
        match = re.search(r"\{.*\}", clean, flags=re.DOTALL)
        if not match:
            return {}
        try:
            data = json.loads(match.group(0))
            return data if isinstance(data, dict) else {}
        except json.JSONDecodeError:
            return {}


def classify_mail_with_llm(session: Session, mail: MailMessage, source_text: str) -> LLMMailClassification | None:
    if not llm_fallback_enabled(session):
        return None
    config = active_model_config(session)
    if not model_ready(session, config):
        return None
    assert config is not None
    allowed = ", ".join(sorted(CLASSIFICATIONS))
    output = call_model(
        session,
        config,
        task_type="MailClassificationFallback",
        related_object_type="MailMessage",
        related_object_id=mail.id,
        messages=[
            {
                "role": "system",
                "content": (
                    "你是商务生产任务单邮件分类器。只返回 JSON，不要解释。"
                    f"classification 必须是以下之一：{allowed}。"
                    "如果是销售发起的生产订单需求，返回 SalesOrderRequirement。"
                    "如果是生产部对已下达任务单提出问题，返回 ProductionQuestion。"
                    "如果是销售补充生产部问题的答复，返回 SalesClarificationReply。"
                    "如果确实与订单沟通无关，返回 NonTarget。"
                ),
            },
            {
                "role": "user",
                "content": (
                    f"发件人：{mail.from_address}\n"
                    f"主题：{mail.subject}\n"
                    f"正文和附件文本：\n{source_text[:6000]}\n\n"
                    "返回格式："
                    "{\"classification\":\"...\",\"confidence\":0-100,\"reason\":\"...\","
                    "\"extracted_requirement\":{\"customer_name\":\"\",\"product_summary\":\"\","
                    "\"quantity_text\":\"\",\"expected_delivery_date\":\"\",\"external_order_no\":\"\"}}"
                ),
            },
        ],
    )
    data = parse_json_object(extract_chat_content(output))
    classification = str(data.get("classification") or "NonTarget")
    if classification not in CLASSIFICATIONS:
        classification = "NonTarget"
    confidence = int(data.get("confidence") or 75)
    confidence = max(0, min(confidence, 100))
    extracted = data.get("extracted_requirement")
    return LLMMailClassification(
        classification=classification,
        confidence=confidence,
        reason=str(data.get("reason") or ""),
        extracted_requirement=extracted if isinstance(extracted, dict) else {},
    )


def extract_requirement_with_llm(session: Session, mail: MailMessage, source_text: str) -> dict[str, str]:
    if not llm_fallback_enabled(session):
        return {}
    config = active_model_config(session)
    if not model_ready(session, config):
        return {}
    assert config is not None
    output = call_model(
        session,
        config,
        task_type="RequirementExtractionFallback",
        related_object_type="MailMessage",
        related_object_id=mail.id,
        messages=[
            {
                "role": "system",
                "content": (
                    "你是商务生产任务单字段抽取器。只返回 JSON，不要解释。"
                    "无法确定的字段返回空字符串。"
                ),
            },
            {
                "role": "user",
                "content": (
                    f"发件人：{mail.from_address}\n"
                    f"主题：{mail.subject}\n"
                    f"正文和附件文本：\n{source_text[:6000]}\n\n"
                    "返回格式：{\"customer_name\":\"\",\"product_summary\":\"\","
                    "\"quantity_text\":\"\",\"expected_delivery_date\":\"\",\"external_order_no\":\"\"}"
                ),
            },
        ],
    )
    data = parse_json_object(extract_chat_content(output))
    return {key: str(value).strip() for key, value in data.items() if isinstance(value, str) and value.strip()}
