from __future__ import annotations

import hashlib
from typing import Any

from sqlalchemy.orm import Session

from backend.app.models import AuditEvent, OutboundMailJob, SystemConfig
from backend.app.services.bootstrap import set_config
from backend.app.services.jsonutil import dumps, loads
from backend.app.services.oms.jackyun_client import JackyunConfigError, jackyun_config_from_session, JackyunClient

DEFAULT_CUSTOMER_QUERY_METHODS = ["crm.customer.list.customized", "crm.customer.list"]


def config_value(session: Session, key: str, default: str = "") -> str:
    row = session.get(SystemConfig, key)
    if row is None or row.value is None:
        return default
    return str(row.value)


def config_dict(session: Session, key: str) -> dict[str, Any]:
    value = loads(config_value(session, key, "{}"), {})
    return value if isinstance(value, dict) else {}


def normalize_customer_name(value: Any) -> str:
    return " ".join(str(value or "").strip().split()).lower()


def first_text(data: dict[str, Any], *keys: str) -> str:
    for key in keys:
        value = str(data.get(key) or "").strip()
        if value:
            return value
    return ""


def extract_customer_rows(payload: Any) -> list[dict[str, Any]]:
    if isinstance(payload, list):
        return [item for item in payload if isinstance(item, dict)]
    if not isinstance(payload, dict):
        return []
    for key in ("customers", "items", "list", "rows", "dataList", "records"):
        value = payload.get(key)
        if isinstance(value, list):
            return [item for item in value if isinstance(item, dict)]
    for key in ("data", "result", "response"):
        nested = payload.get(key)
        rows = extract_customer_rows(nested)
        if rows:
            return rows
    return []


def normalize_oms_customer(row: dict[str, Any]) -> dict[str, str]:
    return {
        "customer_name": first_text(row, "customer_name", "customerName", "name", "nickname", "客户名称", "buyerName", "shopName"),
        "customer_code": first_text(row, "customer_code", "customerCode", "code", "客户编码", "customer_id", "customerId", "id", "buyerCode", "shopCode"),
    }


def find_customer_in_oms_response(response: dict[str, Any], customer_name: str) -> dict[str, str] | None:
    target = normalize_customer_name(customer_name)
    if not target:
        return None
    for row in extract_customer_rows(response):
        customer = normalize_oms_customer(row)
        if normalize_customer_name(customer["customer_name"]) == target and customer["customer_code"]:
            return customer
    return None


def customer_query_methods(session: Session) -> list[str]:
    raw = config_value(session, "oms_customer_query_method", "").strip()
    if not raw:
        return list(DEFAULT_CUSTOMER_QUERY_METHODS)
    parsed = loads(raw, None)
    if isinstance(parsed, list):
        methods = [str(item).strip() for item in parsed if str(item).strip()]
    else:
        methods = [item.strip() for item in raw.replace("\n", ",").split(",") if item.strip()]
    return methods or list(DEFAULT_CUSTOMER_QUERY_METHODS)


def customer_query_payload(session: Session, customer_name: str, method: str) -> dict[str, Any]:
    payload = loads(config_value(session, "oms_customer_query_payload_json", "{}"), {})
    if not isinstance(payload, dict):
        payload = {}
    payload = dict(payload)
    if method == "crm.customer.list.customized":
        payload.setdefault("pageSize", 50)
        payload.setdefault("hasTotal", 1)
        payload.setdefault("nickname", customer_name)
    elif method == "crm.customer.list":
        payload.setdefault("pageIndex", 0)
        payload.setdefault("pageSize", 50)
        payload.setdefault("hasTotal", 1)
        payload.setdefault("nickname", customer_name)
    else:
        payload.setdefault("pageNo", 1)
        payload.setdefault("pageSize", 20)
        payload.setdefault("customerName", customer_name)
    return payload


def query_oms_customer(session: Session, customer_name: str) -> tuple[dict[str, str] | None, dict[str, Any]]:
    try:
        client = JackyunClient(jackyun_config_from_session(session))
    except JackyunConfigError as exc:
        return None, {"status": "Failed", "reason": str(exc), "methods": customer_query_methods(session)}

    attempts: list[dict[str, Any]] = []
    for method in customer_query_methods(session):
        payload = customer_query_payload(session, customer_name, method)
        try:
            response = client.query_customers(method, payload)
        except Exception as exc:
            attempts.append({"status": "Failed", "reason": str(exc), "method": method, "payload": payload})
            continue
        customer = find_customer_in_oms_response(response, customer_name)
        attempt = {"status": "Found" if customer else "NotFound", "method": method, "payload": payload, "response": response}
        attempts.append(attempt)
        if customer is not None:
            return customer, {**attempt, "attempts": attempts}
    status = "Failed" if attempts and all(item.get("status") == "Failed" for item in attempts) else "NotFound"
    method_text = ",".join(item.get("method", "") for item in attempts if item.get("method"))
    return None, {"status": status, "method": method_text, "attempts": attempts}


def upsert_customer_mapping(session: Session, customer_name: str, customer_code: str, *, crm_customer_code: str = "", source: str = "oms_query") -> None:
    mapping = config_dict(session, "v2_customer_mapping_json")
    mapping[customer_name] = {
        "customer_code": customer_code,
        "customer_name": customer_name,
        "crm_customer_code": crm_customer_code,
        "mapping_source": source,
    }
    set_config(session, "v2_customer_mapping_json", dumps(mapping), is_secret=False)


def enqueue_oms_customer_missing_notification(session: Session, customer_name: str, query_detail: dict[str, Any], *, crm_customer_code: str = "") -> OutboundMailJob | None:
    admin_email = config_value(session, "oms_admin_email", "").strip()
    if not admin_email:
        session.add(AuditEvent(event_type="OmsCustomerMissingNotificationSkipped", related_object_type="SystemConfig", related_object_id="oms_admin_email", detail=dumps({"reason": "missing_oms_admin_email", "customer_name": customer_name})))
        return None
    digest_source = f"{customer_name}|{crm_customer_code}|{query_detail.get('status')}|{query_detail.get('method')}"
    idempotency_key = f"oms-customer-missing:{hashlib.sha256(digest_source.encode('utf-8')).hexdigest()}"
    existing = session.query(OutboundMailJob).filter_by(idempotency_key=idempotency_key).one_or_none()
    if existing is not None:
        return existing
    body = "\n".join(
        [
            "OMS 管理员好，",
            "",
            "CRM 订单预审发现客户未在中台映射表中维护，系统已尝试调用 OMS 客户查询接口，但未查询到对应客户。",
            "",
            f"客户名称：{customer_name}",
            f"CRM 客户编码：{crm_customer_code or '-'}",
            f"OMS 查询状态：{query_detail.get('status') or '-'}",
            f"OMS 查询接口：{query_detail.get('method') or '-'}",
            f"失败/未命中原因：{query_detail.get('reason') or 'OMS 未返回同名客户'}",
            "",
            "请在 OMS 创建/维护该客户，或修正 CRM/OMS 客户名称后重新同步订单。",
        ]
    )
    job = OutboundMailJob(
        mail_type="OmsCustomerMissing",
        to_json=dumps([admin_email]),
        cc_json=dumps([]),
        subject=f"[OMS客户缺失][{customer_name}] 请维护客户主数据",
        body=body,
        idempotency_key=idempotency_key,
        status="Pending",
        priority=15,
    )
    session.add(job)
    session.add(AuditEvent(event_type="OmsCustomerMissingNotificationQueued", related_object_type="SystemConfig", related_object_id="customer-mapping", detail=dumps({"to": [admin_email], "customer_name": customer_name, "query_status": query_detail.get("status")})))
    return job


def resolve_customer_mapping_from_oms(session: Session, *, customer_name: str, crm_customer_code: str = "") -> dict[str, Any]:
    customer, detail = query_oms_customer(session, customer_name)
    if customer is not None:
        upsert_customer_mapping(session, customer_name, customer["customer_code"], crm_customer_code=crm_customer_code, source="oms_query")
        session.add(AuditEvent(event_type="CustomerMappingResolvedFromOms", related_object_type="SystemConfig", related_object_id="customer-mapping", detail=dumps({"customer_name": customer_name, "customer_code": customer["customer_code"], "crm_customer_code": crm_customer_code})))
        return {"found": True, "customer": customer, "detail": detail}
    enqueue_oms_customer_missing_notification(session, customer_name, detail, crm_customer_code=crm_customer_code)
    return {"found": False, "customer": None, "detail": detail}
