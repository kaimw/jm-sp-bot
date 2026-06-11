from __future__ import annotations

import time
import uuid
from dataclasses import dataclass
from typing import Any
from urllib.parse import urljoin

import httpx
from sqlalchemy.orm import Session

from backend.app.models import SystemConfig


KINGDEE_LOGIN_PATH = "Kingdee.BOS.WebApi.ServicesStub.AuthService.LoginByAppSecret.common.kdsvc"
KINGDEE_BILL_QUERY_PATH = "Kingdee.BOS.WebApi.ServicesStub.DynamicFormService.ExecuteBillQuery.common.kdsvc"


class KingdeeConfigError(ValueError):
    pass


@dataclass(frozen=True)
class KingdeeConnectionConfig:
    server_url: str
    acct_id: str
    username: str
    app_id: str
    app_sec: str
    lcid: int = 2052


def normalize_kingdee_server_url(value: str) -> str:
    text = (value or "").strip()
    if not text:
        return ""
    if "html5/index.aspx" in text.lower():
        text = text[: text.lower().index("html5/index.aspx")]
    if not text.endswith("/"):
        text = f"{text}/"
    return text


def kingdee_config_from_session(session: Session) -> KingdeeConnectionConfig:
    def value(key: str) -> str:
        row = session.get(SystemConfig, key)
        return row.value.strip() if row is not None and row.value is not None else ""

    server_url = normalize_kingdee_server_url(value("erp_server_url"))
    raw_lcid = value("erp_lcid") or "2052"
    try:
        lcid = int(raw_lcid)
    except ValueError as exc:
        raise KingdeeConfigError("ERP LCID 必须是数字") from exc
    config = KingdeeConnectionConfig(
        server_url=server_url,
        acct_id=value("erp_acct_id"),
        username=value("erp_username"),
        app_id=value("erp_app_id"),
        app_sec=value("erp_app_sec"),
        lcid=lcid,
    )
    missing = [
        label
        for label, field_value in [
            ("ServerUrl", config.server_url),
            ("AcctID", config.acct_id),
            ("用户名称", config.username),
            ("AppID", config.app_id),
            ("AppSec", config.app_sec),
        ]
        if not field_value
    ]
    if missing:
        raise KingdeeConfigError(f"ERP 配置不完整：缺少 {'、'.join(missing)}")
    return config


class KingdeeClient:
    def __init__(self, config: KingdeeConnectionConfig, *, timeout_seconds: float = 15.0) -> None:
        self.config = config
        self.timeout_seconds = timeout_seconds

    @property
    def login_url(self) -> str:
        return urljoin(self.config.server_url, KINGDEE_LOGIN_PATH)

    def build_login_payload(self) -> dict[str, Any]:
        return {
            "format": 1,
            "useragent": "jm-sp-bot",
            "rid": str(uuid.uuid4()),
            "parameters": [
                self.config.acct_id,
                self.config.username,
                self.config.app_id,
                self.config.app_sec,
                self.config.lcid,
            ],
            "timestamp": str(int(time.time())),
            "v": "1.0",
        }

    def build_bill_query_payload(
        self,
        *,
        form_id: str,
        field_keys: str,
        filter_string: str = "",
        order_string: str = "",
        limit: int = 20,
        start_row: int = 0,
    ) -> dict[str, Any]:
        return {
            "format": 1,
            "useragent": "jm-sp-bot",
            "rid": str(uuid.uuid4()),
            "parameters": [
                {
                    "FormId": form_id,
                    "FieldKeys": field_keys,
                    "FilterString": filter_string,
                    "OrderString": order_string,
                    "TopRowCount": 0,
                    "StartRow": start_row,
                    "Limit": limit,
                    "SubSystemId": "",
                }
            ],
            "timestamp": str(int(time.time())),
            "v": "1.0",
        }

    def test_connection(self) -> dict[str, Any]:
        started = time.time()
        try:
            with httpx.Client(timeout=self.timeout_seconds) as client:
                response = client.post(self.login_url, json=self.build_login_payload())
                response.raise_for_status()
                payload = response.json()
        except httpx.HTTPStatusError as exc:
            return {
                "ok": False,
                "endpoint": self.login_url,
                "error_type": "HTTPStatusError",
                "message": f"金蝶登录接口返回 HTTP {exc.response.status_code}",
                "elapsed_ms": round((time.time() - started) * 1000),
            }
        except Exception as exc:
            return {
                "ok": False,
                "endpoint": self.login_url,
                "error_type": exc.__class__.__name__,
                "message": str(exc),
                "elapsed_ms": round((time.time() - started) * 1000),
            }

        return {
            "ok": is_login_success(payload),
            "endpoint": self.login_url,
            "error_type": "" if is_login_success(payload) else "KingdeeAuthFailed",
            "message": login_message(payload),
            "elapsed_ms": round((time.time() - started) * 1000),
            "context": login_context_summary(payload),
        }

    def execute_bill_query(
        self,
        *,
        form_id: str,
        field_keys: str,
        filter_string: str = "",
        order_string: str = "",
        limit: int = 20,
        start_row: int = 0,
    ) -> dict[str, Any]:
        started = time.time()
        endpoint = urljoin(self.config.server_url, KINGDEE_BILL_QUERY_PATH)
        try:
            with httpx.Client(timeout=self.timeout_seconds) as client:
                login_response = client.post(self.login_url, json=self.build_login_payload())
                login_response.raise_for_status()
                login_payload = login_response.json()
                if not is_login_success(login_payload):
                    return {
                        "ok": False,
                        "endpoint": endpoint,
                        "error_type": "KingdeeAuthFailed",
                        "message": login_message(login_payload),
                        "elapsed_ms": round((time.time() - started) * 1000),
                        "items": [],
                        "raw": login_payload,
                    }
                query_response = client.post(
                    endpoint,
                    json=self.build_bill_query_payload(
                        form_id=form_id,
                        field_keys=field_keys,
                        filter_string=filter_string,
                        order_string=order_string,
                        limit=limit,
                        start_row=start_row,
                    ),
                )
                query_response.raise_for_status()
                payload = query_response.json()
        except httpx.HTTPStatusError as exc:
            return {
                "ok": False,
                "endpoint": endpoint,
                "error_type": "HTTPStatusError",
                "message": f"金蝶查询接口返回 HTTP {exc.response.status_code}",
                "elapsed_ms": round((time.time() - started) * 1000),
                "items": [],
            }
        except Exception as exc:
            return {
                "ok": False,
                "endpoint": endpoint,
                "error_type": exc.__class__.__name__,
                "message": str(exc),
                "elapsed_ms": round((time.time() - started) * 1000),
                "items": [],
            }

        return {
            "ok": is_query_success(payload),
            "endpoint": endpoint,
            "error_type": "" if is_query_success(payload) else "KingdeeQueryFailed",
            "message": query_message(payload),
            "elapsed_ms": round((time.time() - started) * 1000),
            "items": normalize_query_rows(payload),
            "raw": payload,
        }


def execute_bill_query_with_config(
    config: KingdeeConnectionConfig,
    *,
    form_id: str,
    field_keys: str,
    filter_string: str = "",
    order_string: str = "",
    limit: int = 20,
    start_row: int = 0,
) -> dict[str, Any]:
    return KingdeeClient(config).execute_bill_query(
        form_id=form_id,
        field_keys=field_keys,
        filter_string=filter_string,
        order_string=order_string,
        limit=limit,
        start_row=start_row,
    )


def is_login_success(payload: dict[str, Any]) -> bool:
    result_type = payload.get("LoginResultType")
    if result_type in (1, "1", True):
        return True
    result = payload.get("Result")
    if isinstance(result, dict):
        response_status = result.get("ResponseStatus")
        if isinstance(response_status, dict) and response_status.get("IsSuccess") is True:
            return True
    return False


def login_message(payload: dict[str, Any]) -> str:
    for key in ("Message", "KDSVCSessionId"):
        value = payload.get(key)
        if isinstance(value, str) and value:
            return value if key == "Message" else "连接成功"
    result = payload.get("Result")
    if isinstance(result, dict):
        response_status = result.get("ResponseStatus")
        if isinstance(response_status, dict):
            errors = response_status.get("Errors")
            if isinstance(errors, list) and errors:
                messages = [str(item.get("Message") or item.get("FieldName") or item) for item in errors if isinstance(item, dict)]
                if messages:
                    return "；".join(messages)
            if response_status.get("IsSuccess") is True:
                return "连接成功"
    return "连接失败"


def login_context_summary(payload: dict[str, Any]) -> dict[str, Any]:
    context = payload.get("Context")
    if not isinstance(context, dict):
        return {}
    summary: dict[str, Any] = {}
    for source, target in [
        ("UserName", "user_name"),
        ("UserId", "user_id"),
        ("DataCenterName", "data_center_name"),
        ("CurrentOrganizationInfo", "current_org"),
    ]:
        if source in context:
            summary[target] = context[source]
    return summary


def test_kingdee_connection_from_config(session: Session) -> dict[str, Any]:
    try:
        config = kingdee_config_from_session(session)
    except KingdeeConfigError as exc:
        return {"ok": False, "endpoint": "", "error_type": "KingdeeConfigError", "message": str(exc), "elapsed_ms": 0, "context": {}}
    return KingdeeClient(config).test_connection()


def execute_bill_query_from_config(
    session: Session,
    *,
    form_id: str,
    field_keys: str,
    filter_string: str = "",
    order_string: str = "",
    limit: int = 20,
    start_row: int = 0,
) -> dict[str, Any]:
    try:
        config = kingdee_config_from_session(session)
    except KingdeeConfigError as exc:
        return {"ok": False, "endpoint": "", "error_type": "KingdeeConfigError", "message": str(exc), "elapsed_ms": 0, "items": []}
    return KingdeeClient(config).execute_bill_query(
        form_id=form_id,
        field_keys=field_keys,
        filter_string=filter_string,
        order_string=order_string,
        limit=limit,
        start_row=start_row,
    )


def is_query_success(payload: Any) -> bool:
    if isinstance(payload, list):
        embedded_status = embedded_response_status(payload)
        if embedded_status is not None:
            return embedded_status.get("IsSuccess") is True
        return True
    if isinstance(payload, dict):
        response_status = payload.get("ResponseStatus")
        if isinstance(response_status, dict):
            return response_status.get("IsSuccess") is True
        result = payload.get("Result")
        if isinstance(result, dict):
            nested_status = result.get("ResponseStatus")
            if isinstance(nested_status, dict):
                return nested_status.get("IsSuccess") is True
    return False


def query_message(payload: Any) -> str:
    if isinstance(payload, list):
        embedded_status = embedded_response_status(payload)
        if embedded_status is not None:
            return response_status_message(embedded_status) or "查询失败"
        return "查询成功"
    if isinstance(payload, dict):
        message = payload.get("Message")
        if isinstance(message, str) and message:
            return message
        for status in [payload.get("ResponseStatus"), payload.get("Result", {}).get("ResponseStatus") if isinstance(payload.get("Result"), dict) else None]:
            if not isinstance(status, dict):
                continue
            status_message = response_status_message(status)
            if status_message:
                return status_message
            if status.get("IsSuccess") is True:
                return "查询成功"
    return "查询失败"


def normalize_query_rows(payload: Any) -> list[Any]:
    if not is_query_success(payload):
        return []
    if isinstance(payload, list):
        return payload
    if isinstance(payload, dict):
        result = payload.get("Result")
        if isinstance(result, list):
            return result
        rows = payload.get("Rows")
        if isinstance(rows, list):
            return rows
    return []


def embedded_response_status(payload: list[Any]) -> dict[str, Any] | None:
    if len(payload) != 1:
        return None
    first = payload[0]
    if isinstance(first, list) and len(first) == 1:
        first = first[0]
    if not isinstance(first, dict):
        return None
    result = first.get("Result")
    if isinstance(result, dict):
        status = result.get("ResponseStatus")
        if isinstance(status, dict):
            return status
    status = first.get("ResponseStatus")
    if isinstance(status, dict):
        return status
    return None


def response_status_message(status: dict[str, Any]) -> str:
    errors = status.get("Errors")
    if isinstance(errors, list) and errors:
        messages = [str(item.get("Message") or item) for item in errors if isinstance(item, dict)]
        if messages:
            return "；".join(messages)
    return ""
