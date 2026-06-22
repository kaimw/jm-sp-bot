from __future__ import annotations

import hashlib
import json
import time
from dataclasses import dataclass
from typing import Any

import httpx
from sqlalchemy.orm import Session

from backend.app.models import SystemConfig
from backend.app.services.crypto import decrypt_value


DEFAULT_JACKYUN_GATEWAY = "https://open.jackyun.com/open/openapi/do"


class JackyunConfigError(ValueError):
    pass


@dataclass(frozen=True)
class JackyunConfig:
    gateway_url: str
    app_key: str
    app_secret: str
    version: str = "1.0"
    content_type: str = "json"
    timeout_seconds: float = 20.0


def config_value(session: Session, key: str, default: str = "") -> str:
    row = session.get(SystemConfig, key)
    if row is None or row.value is None:
        return default
    if row.is_secret:
        return decrypt_value(str(row.value))
    return str(row.value)


def config_bool(session: Session, key: str, default: bool = False) -> bool:
    value = config_value(session, key, "")
    if value == "":
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def jackyun_config_from_session(session: Session) -> JackyunConfig:
    gateway_url = config_value(session, "oms_jackyun_gateway_url", DEFAULT_JACKYUN_GATEWAY).strip() or DEFAULT_JACKYUN_GATEWAY
    app_key = config_value(session, "oms_jackyun_app_key", "").strip()
    app_secret = config_value(session, "oms_jackyun_app_secret", "").strip()
    version = config_value(session, "oms_jackyun_version", "1.0").strip() or "1.0"
    content_type = config_value(session, "oms_jackyun_content_type", "json").strip() or "json"
    try:
        timeout_seconds = float(config_value(session, "oms_jackyun_timeout_seconds", "20") or "20")
    except ValueError:
        timeout_seconds = 20.0
    missing = []
    if not app_key:
        missing.append("AppKey")
    if not app_secret:
        missing.append("AppSecret")
    if missing:
        raise JackyunConfigError(f"吉客云 OpenAPI 配置不完整：缺少 {'、'.join(missing)}")
    return JackyunConfig(
        gateway_url=gateway_url,
        app_key=app_key,
        app_secret=app_secret,
        version=version,
        content_type=content_type,
        timeout_seconds=max(3.0, timeout_seconds),
    )


class JackyunClient:
    def __init__(self, config: JackyunConfig) -> None:
        self.config = config

    def build_common_params(self, method: str, bizcontent: dict[str, Any], *, timestamp: str | None = None) -> dict[str, str]:
        bizcontent_json = json.dumps(bizcontent, ensure_ascii=False, separators=(",", ":"), sort_keys=False)
        params = {
            "method": method,
            "appkey": self.config.app_key,
            "version": self.config.version,
            "contenttype": self.config.content_type,
            "timestamp": timestamp or time.strftime("%Y-%m-%d %H:%M:%S"),
            "bizcontent": bizcontent_json,
        }
        params["sign"] = sign_params(params, self.config.app_secret)
        return params

    def call_api(self, method: str, bizcontent: dict[str, Any]) -> dict[str, Any]:
        params = self.build_common_params(method, bizcontent)
        with httpx.Client(timeout=self.config.timeout_seconds) as client:
            response = client.post(self.config.gateway_url, data=params)
            response.raise_for_status()
            payload = response.json()
        return normalize_response(payload)

    def create_delivery_order(self, payload: dict[str, Any], *, method: str = "wms.order.create") -> dict[str, Any]:
        return self.call_api(method, payload)

    def query_delivery_orders(self, payload: dict[str, Any]) -> dict[str, Any]:
        return self.call_api("wms.order.query-info.page", payload)

    def print_delivery_label(self, payload: dict[str, Any]) -> dict[str, Any]:
        return self.call_api("wms-cross.delivery.print", payload)

    def search_skus(self, payload: dict[str, Any]) -> dict[str, Any]:
        return self.call_api("erp-goods.goods.sku.search", payload)

    def search_goods(self, payload: dict[str, Any]) -> dict[str, Any]:
        """调用 erp.storage.goodslist 分页查询货品信息（含英文名称、别名等完整字段）"""
        return self.call_api("erp.storage.goodslist", payload)

    def query_sku_stock(self, payload: dict[str, Any]) -> dict[str, Any]:
        return self.call_api("erp-stock.stock.skulist", payload)

    def query_customers(self, method: str, payload: dict[str, Any]) -> dict[str, Any]:
        return self.call_api(method, payload)


def sign_params(params: dict[str, str], app_secret: str) -> str:
    parts = []
    for key in sorted(params):
        if key.lower() in {"sign", "contextid"}:
            continue
        value = params[key]
        if value is None:
            continue
        parts.append(f"{key}{value}")
    raw = f"{app_secret}{''.join(parts)}{app_secret}".lower()
    return hashlib.md5(raw.encode("utf-8")).hexdigest()


def normalize_response(payload: Any) -> dict[str, Any]:
    if not isinstance(payload, dict):
        return {"ok": False, "code": "", "message": "吉客云返回格式不是 JSON 对象", "raw": payload}
    code = payload.get("code")
    sub_code = payload.get("subCode") or payload.get("sub_code")
    message = str(payload.get("msg") or payload.get("message") or payload.get("errorMsg") or "")
    ok = code in {200, "200"} or (code in {0, "0"} and not sub_code)
    return {
        "ok": ok,
        "code": code,
        "sub_code": sub_code,
        "message": message,
        "data": payload.get("data") or payload.get("result") or {},
        "raw": payload,
    }


def jackyun_client_from_session(session: Session) -> JackyunClient:
    return JackyunClient(jackyun_config_from_session(session))
