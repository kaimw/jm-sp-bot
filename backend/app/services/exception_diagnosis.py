from __future__ import annotations

from typing import Any

from sqlalchemy.orm import Session

from backend.app.models import AuditEvent, ExceptionCase, ProcessingJob, now_utc
from backend.app.services.jsonutil import dumps, loads


def enqueue_exception_diagnosis(session: Session, case: ExceptionCase, *, source: str = "system") -> ProcessingJob:
    payload = {"exception_id": case.id}
    existing = (
        session.query(ProcessingJob)
        .filter(
            ProcessingJob.job_type == "DIAGNOSE_EXCEPTION",
            ProcessingJob.payload_json == dumps(payload),
            ProcessingJob.status.in_(["Pending", "Running"]),
        )
        .first()
    )
    if existing is not None:
        return existing
    job = ProcessingJob(job_type="DIAGNOSE_EXCEPTION", payload_json=dumps(payload), status="Pending")
    session.add(job)
    session.add(
        AuditEvent(
            event_type="ExceptionDiagnosisQueued",
            actor=source,
            related_object_type="ExceptionCase",
            related_object_id=case.id,
            detail=dumps(payload),
            created_at=now_utc(),
        )
    )
    return job


def diagnose_exception_case(session: Session, exception_id: str, *, actor: str = "diagnosis-worker") -> dict[str, Any]:
    case = session.get(ExceptionCase, exception_id)
    if case is None:
        raise ValueError("exception not found")
    detail = loads(case.detail, {})
    diagnosis = build_rule_based_diagnosis(case, detail)
    detail["ai_diagnosis"] = diagnosis
    case.detail = dumps(detail)
    case.updated_at = now_utc()
    case.last_actor = actor
    session.add(
        AuditEvent(
            event_type="ExceptionDiagnosed",
            actor=actor,
            related_object_type="ExceptionCase",
            related_object_id=case.id,
            detail=dumps(diagnosis),
            created_at=now_utc(),
        )
    )
    return diagnosis


def build_rule_based_diagnosis(case: ExceptionCase, detail: dict[str, Any]) -> dict[str, Any]:
    exception = detail.get("exception") if isinstance(detail, dict) else {}
    validation = detail.get("validation") if isinstance(detail, dict) else {}
    failed_rules = validation.get("failed_rules", []) if isinstance(validation, dict) else []
    failed_codes = {str(item.get("rule_code")) for item in failed_rules if isinstance(item, dict)}
    summary = str((exception or {}).get("summary") or (exception or {}).get("likely_reason") or case.exception_type)

    root_causes: list[str] = []
    actions: list[str] = []
    owner = "运营"
    confidence = 0.78

    if case.exception_type in {"OMS_BLOCKED", "OMS_REQUIRED_FIELDS_MISSING"}:
        owner = "运维/OMS接口负责人"
        root_causes.append("OMS 下推参数、接口响应或幂等状态异常")
        actions.extend(["核对货主、仓库、店铺、物流方式和外部订单号", "用外部订单号在 OMS 反查是否已建单", "修复配置后从异常台重放 OMS 下推"])
        confidence = 0.86
    if case.exception_type.startswith("CRM_CHANGED") or case.exception_type.startswith("CRM_CANCELLED"):
        owner = "商务/订单运营"
        root_causes.append("CRM 订单在中台流程推进后发生编辑或撤销")
        actions.extend(["比对新旧 CRM 快照差异", "确认是否需要作废发货预览或拦截下游单据", "人工确认后重新同步或重放流程"])
        confidence = 0.82
    if "CUSTOMER_MAPPING" in failed_codes:
        owner = "商务/主数据维护人"
        root_causes.append("客户未完成一期客户主数据映射")
        actions.append("在 v2_customer_mapping_json 或正式客户主数据中维护客户编码")
    if "KNOWN_ACTIVE_SKU" in failed_codes:
        owner = "产品/主数据维护人"
        root_causes.append("SKU 未在主数据启用或 CRM 明细未映射 SKU")
        actions.append("维护 SKU 主数据或修正 CRM 商品明细")
    if "PHASE1_COMPLETE_PRE_REVIEW_FIELDS" in failed_codes:
        owner = "商务"
        root_causes.append("CRM 订单基础字段或关键附件不完整")
        actions.append("在 CRM 补齐收货信息、交期、审批状态和关键附件")
    if "LOCAL_INVENTORY_AVAILABLE" in failed_codes:
        owner = "仓储/供应链"
        root_causes.append("本地库存快照不足或缺失")
        actions.append("同步库存快照并确认可用库存")

    if not root_causes:
        root_causes.append(summary)
    if not actions:
        actions.extend((exception or {}).get("suggested_actions") or ["查看异常上下文并人工确认处理方案"])

    return {
        "diagnosis_type": "RULE_BASED_AI_COMPATIBLE",
        "summary": summary,
        "root_causes": dedupe(root_causes),
        "recommended_actions": dedupe(actions),
        "suggested_owner": owner,
        "confidence": confidence,
        "generated_at": now_utc().isoformat(),
    }


def dedupe(values: list[str]) -> list[str]:
    result: list[str] = []
    for value in values:
        text = str(value or "").strip()
        if text and text not in result:
            result.append(text)
    return result
