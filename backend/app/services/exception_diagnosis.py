from __future__ import annotations

from typing import Any

from sqlalchemy.orm import Session

from backend.app.models import AgentRunLog, AuditEvent, ExceptionCase, ProcessingJob, now_utc
from backend.app.services.jsonutil import dumps, loads
from backend.app.services.llm_fallback import active_model_config, model_ready, parse_json_object
from backend.app.services.model_provider import call_model, extract_chat_content


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
    run_log = AgentRunLog(
        agent_name="ExceptionDiagnosisAgent",
        task_type="ExceptionDiagnosis",
        related_object_type="ExceptionCase",
        related_object_id=case.id,
        input_json=dumps(exception_diagnosis_context(case, detail)),
        status="Running",
        started_at=now_utc(),
    )
    session.add(run_log)
    diagnosis = build_rule_based_diagnosis(case, detail)
    try:
        llm_diagnosis = diagnose_exception_with_llm(session, case, detail)
        if llm_diagnosis:
            diagnosis = llm_diagnosis
        run_log.status = "Succeeded"
    except Exception as exc:
        diagnosis["fallback_reason"] = f"LLM 诊断失败，已使用规则兜底：{exc}"
        run_log.status = "Fallback"
        run_log.error_message = str(exc)
    run_log.output_json = dumps(diagnosis)
    run_log.finished_at = now_utc()
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


def exception_diagnosis_context(case: ExceptionCase, detail: dict[str, Any]) -> dict[str, Any]:
    return {
        "exception_id": case.id,
        "exception_type": case.exception_type,
        "severity": case.severity,
        "status": case.status,
        "exception": detail.get("exception") if isinstance(detail.get("exception"), dict) else {},
        "order": detail.get("order") if isinstance(detail.get("order"), dict) else {},
        "validation": detail.get("validation") if isinstance(detail.get("validation"), dict) else {},
    }


def diagnose_exception_with_llm(session: Session, case: ExceptionCase, detail: dict[str, Any]) -> dict[str, Any] | None:
    config = active_model_config(session)
    if not model_ready(session, config):
        return None
    assert config is not None
    context = exception_diagnosis_context(case, detail)
    output = call_model(
        session,
        config,
        task_type="ExceptionDiagnosis",
        related_object_type="ExceptionCase",
        related_object_id=case.id,
        response_format={"type": "json_object"},
        messages=[
            {
                "role": "system",
                "content": (
                    "你是商务订单中台异常诊断 Agent。只返回 JSON，不要 Markdown。"
                    "不要暴露 SQL、堆栈、NullPointerException 等技术黑话。"
                    "所有结论必须来自输入 ContextPack。"
                    "如果你发现异常是由于收货地址不规范、城市名拼写错误、地址过短、脱敏、邮编格式错误或电话号码缺失/异常等收货信息问题引起的，"
                    "你必须分析并生成地址、联系人及电话的修正建议，并写入返回 JSON 的 address_correction 字段中。"
                ),
            },
            {
                "role": "user",
                "content": (
                    "请根据 ContextPack 输出如下格式的 JSON（注意：不要在外面包裹 ```json ``` 块）：\n"
                    "{\n"
                    "  \"diagnosis_type\": \"LLM_JSON\",\n"
                    "  \"summary\": \"业务化摘要\",\n"
                    "  \"root_causes\": [\"原因\"],\n"
                    "  \"recommended_actions\": [\"动作\"],\n"
                    "  \"suggested_owner\": \"责任角色\",\n"
                    "  \"confidence\": 0.95,\n"
                    "  \"address_correction\": {\n"
                    "    \"receipt_address\": \"修正后的详细收货地址（若不需要修正或不适用，则返回 null）\",\n"
                    "    \"receipt_contact\": \"修正后的收货人姓名（若不需要修正或不适用，则返回 null）\",\n"
                    "    \"receipt_phone\": \"修正后的联系电话（若不需要修正或不适用，则返回 null）\",\n"
                    "    \"reason\": \"地址修正的具体原因说明（若不需要修正，则返回 null）\"\n"
                    "  }\n"
                    "}\n"
                    f"ContextPack:\n{dumps(context)[:8000]}"
                ),
            },
        ],
    )
    data = parse_json_object(extract_chat_content(output))
    return normalize_llm_diagnosis(data)


def normalize_llm_diagnosis(data: dict[str, Any]) -> dict[str, Any] | None:
    if not data:
        return None
    summary = str(data.get("summary") or "").strip()
    if not summary:
        return None
    try:
        confidence = float(data.get("confidence", 0.75))
    except (TypeError, ValueError):
        confidence = 0.75
        
    corr = data.get("address_correction")
    address_correction = None
    if isinstance(corr, dict) and any(corr.get(k) for k in ("receipt_address", "receipt_contact", "receipt_phone")):
        address_correction = {
            "receipt_address": str(corr.get("receipt_address") or "").strip() or None,
            "receipt_contact": str(corr.get("receipt_contact") or "").strip() or None,
            "receipt_phone": str(corr.get("receipt_phone") or "").strip() or None,
            "reason": str(corr.get("reason") or "AI 地址智能修正").strip()
        }

    return {
        "diagnosis_type": "LLM_JSON",
        "summary": summary,
        "root_causes": dedupe([str(item) for item in data.get("root_causes", [])]) if isinstance(data.get("root_causes"), list) else [summary],
        "recommended_actions": dedupe([str(item) for item in data.get("recommended_actions", [])]) if isinstance(data.get("recommended_actions"), list) else ["查看异常上下文并人工确认处理方案"],
        "suggested_owner": str(data.get("suggested_owner") or "运营"),
        "confidence": max(0.0, min(confidence, 1.0)),
        "address_correction": address_correction,
        "generated_at": now_utc().isoformat(),
    }


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
        "address_correction": None,
        "generated_at": now_utc().isoformat(),
    }


def dedupe(values: list[str]) -> list[str]:
    result: list[str] = []
    for value in values:
        text = str(value or "").strip()
        if text and text not in result:
            result.append(text)
    return result
