from __future__ import annotations

import json
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

# ── 写入操作 API 路径 ──
KINGDEE_SAVE_PATH = "Kingdee.BOS.WebApi.ServicesStub.DynamicFormService.Save.common.kdsvc"
KINGDEE_SUBMIT_PATH = "Kingdee.BOS.WebApi.ServicesStub.DynamicFormService.Submit.common.kdsvc"
KINGDEE_AUDIT_PATH = "Kingdee.BOS.WebApi.ServicesStub.DynamicFormService.Audit.common.kdsvc"
KINGDEE_UNAUDIT_PATH = "Kingdee.BOS.WebApi.ServicesStub.DynamicFormService.UnAudit.common.kdsvc"
KINGDEE_CANCEL_PATH = "Kingdee.BOS.WebApi.ServicesStub.DynamicFormService.Cancel.common.kdsvc"
KINGDEE_DELETE_PATH = "Kingdee.BOS.WebApi.ServicesStub.DynamicFormService.Delete.common.kdsvc"
KINGDEE_VIEW_PATH = "Kingdee.BOS.WebApi.ServicesStub.DynamicFormService.View.common.kdsvc"

# 常用单据 FormId
FORMID_SALES_ORDER = "SAL_SaleOrder"      # 销售订单
FORMID_PURCHASE_ORDER = "PUR_PurchaseOrder"  # 采购订单


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

    # ──────────────────────────────────────────────
    # 写入操作：制单 / 提交 / 审核 / 反审核 / 作废 / 删除
    # ──────────────────────────────────────────────

    def _write_operation(
        self,
        *,
        endpoint_path: str,
        form_id: str,
        params: list[Any],
        label: str = "",
    ) -> dict[str, Any]:
        """写入操作通用方法：先登录获取会话，再用同一客户端调用写入 API"""
        started = time.time()
        endpoint = urljoin(self.config.server_url, endpoint_path)
        try:
            with httpx.Client(timeout=self.timeout_seconds) as client:
                # Step 1: 登录（httpx.Client 自动保存 Cookie）
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
                        "result": None,
                    }

                # Step 2: 执行写入操作（复用同一个 client，Cookie 自动携带）
                payload = {
                    "format": 1,
                    "useragent": "jm-sp-bot",
                    "rid": str(uuid.uuid4()),
                    "parameters": [form_id] + params,
                    "timestamp": str(int(time.time())),
                    "v": "1.0",
                }
                response = client.post(endpoint, json=payload)
                response.raise_for_status()
                data = response.json()
        except httpx.HTTPStatusError as exc:
            return {
                "ok": False,
                "endpoint": endpoint,
                "error_type": "HTTPStatusError",
                "message": f"金蝶 {label} 接口返回 HTTP {exc.response.status_code}",
                "status_code": exc.response.status_code,
                "elapsed_ms": round((time.time() - started) * 1000),
                "result": None,
            }
        except Exception as exc:
            return {
                "ok": False,
                "endpoint": endpoint,
                "error_type": exc.__class__.__name__,
                "message": str(exc),
                "elapsed_ms": round((time.time() - started) * 1000),
                "result": None,
            }
        return {
            "ok": is_write_success(data),
            "endpoint": endpoint,
            "error_type": "" if is_write_success(data) else "KingdeeWriteFailed",
            "message": write_result_message(data) or (f"{label}成功" if is_write_success(data) else f"{label}失败"),
            "elapsed_ms": round((time.time() - started) * 1000),
            "result": data.get("Result") if isinstance(data, dict) else None,
            "raw": data,
        }

    def save_bill(self, *, form_id: str, model: dict[str, Any], need_return_fields: list[str] | None = None) -> dict[str, Any]:
        """制单/修改：创建新单据或修改已有单据（传单据内码即修改）"""
        params = [json.dumps({
            "NeedUpDateFields": [],
            "NeedReturnFields": need_return_fields or ["FBillNo"],
            "IsDeleteEntry": "true",
            "Model": model,
        })]
        return self._write_operation(endpoint_path=KINGDEE_SAVE_PATH, form_id=form_id, params=params, label="制单")

    def submit_bill(self, *, form_id: str, bill_ids: list[str | int]) -> dict[str, Any]:
        payload = {"CreateOrgId": 0, "Numbers": [], "Ids": ",".join(str(x) for x in bill_ids), "SelectedPostId": 0}
        return self._write_operation(endpoint_path=KINGDEE_SUBMIT_PATH, form_id=form_id, params=[json.dumps(payload)], label="提交")

    def audit_bill(self, *, form_id: str, bill_ids: list[str | int]) -> dict[str, Any]:
        payload = {"CreateOrgId": 0, "Numbers": [], "Ids": ",".join(str(x) for x in bill_ids), "SelectedPostId": 0}
        return self._write_operation(endpoint_path=KINGDEE_AUDIT_PATH, form_id=form_id, params=[json.dumps(payload)], label="审核")

    def un_audit_bill(self, *, form_id: str, bill_ids: list[str | int]) -> dict[str, Any]:
        payload = {"CreateOrgId": 0, "Numbers": [], "Ids": ",".join(str(x) for x in bill_ids), "SelectedPostId": 0}
        return self._write_operation(endpoint_path=KINGDEE_UNAUDIT_PATH, form_id=form_id, params=[json.dumps(payload)], label="反审核")

    def cancel_bill(self, *, form_id: str, bill_ids: list[str | int]) -> dict[str, Any]:
        payload = {"CreateOrgId": 0, "Numbers": [], "Ids": ",".join(str(x) for x in bill_ids), "SelectedPostId": 0}
        return self._write_operation(endpoint_path=KINGDEE_CANCEL_PATH, form_id=form_id, params=[json.dumps(payload)], label="作废")

    def delete_bill(self, *, form_id: str, bill_ids: list[str | int]) -> dict[str, Any]:
        payload = {"CreateOrgId": 0, "Numbers": [], "Ids": ",".join(str(x) for x in bill_ids), "SelectedPostId": 0}
        return self._write_operation(endpoint_path=KINGDEE_DELETE_PATH, form_id=form_id, params=[json.dumps(payload)], label="删除")

    def view_bill(self, *, form_id: str, bill_id: str | int) -> dict[str, Any]:
        params = [json.dumps({"Id": str(bill_id)})]
        return self._write_operation(endpoint_path=KINGDEE_VIEW_PATH, form_id=form_id, params=params, label="查看")

    def _query_first_code(self, *, form_id: str, field_keys: str, name_label: str = "") -> tuple[str, str]:
        """查询基础资料，返回 (编码, 名称)。失败返回 ("", "")"""
        r = self.execute_bill_query(form_id=form_id, field_keys=field_keys)
        if not r.get("ok"):
            return ("", "")
        items = normalize_query_rows(r.get("raw"))
        if not items or not isinstance(items, list) or not isinstance(items[0], list) or not isinstance(items[0][0], str):
            return ("", "")
        name = str(items[0][1]) if len(items[0]) > 1 and isinstance(items[0][1], str) else ""
        return (items[0][0], name)

    def test_write_permissions(self) -> dict[str, Any]:
        """一站式测试所有写入权限（Save→Submit→Audit→UnAudit→Cancel→Delete），测试完自动清理

        先用查询接口取基础资料编码，再用完整字段制单，确保不走"会话已丢失"弯路。
        """
        import datetime as _dt
        results: dict[str, Any] = {}

        # ── 用同一个 Client 完成全部操作（Cookie 自动维持会话）──
        with httpx.Client(timeout=self.timeout_seconds) as client:
            # 先登录
            login_resp = client.post(self.login_url, json=self.build_login_payload())
            login_resp.raise_for_status()
            login_payload = login_resp.json()
            login_ok = is_login_success(login_payload)

            if not login_ok:
                err = {"ok": False, "message": login_message(login_payload)}
                return {"ok": False, "message": "登录失败", "login": err, "results": results}

            # 钩子：将当前 client 注入到查询方法中
            q = lambda form_id, fields: self._query_first_code_using_client(
                client, form_id=form_id, field_keys=fields
            )

            org, _ = q("ORG_Organizations", "FNumber,FName")
            cust, _ = q("BD_Customer", "FNumber,FName")
            dept, _ = q("BD_Department", "FNumber,FName")

            # 销售员：BD_Employee → EMP_Employee 两级回退
            saler, _ = q("BD_Employee", "FNumber,FName")
            if not saler:
                saler, _ = q("EMP_Employee", "FNumber,FName")

            # 销售类型：SAL_SaleType → SAL_SaleTypeNew
            stype, _ = q("SAL_SaleType", "FNumber,FName")
            if not stype:
                stype, _ = q("SAL_SaleTypeNew", "FNumber,FName")

            mat, mat_name = q("BD_MATERIAL", "FNumber,FName")

            # ── 构建 Save body ──
            bill_no = f"PERM-TEST-{int(time.time())}"
            model_body: dict[str, Any] = {
                "FBillTypeID": {"FNUMBER": "XSDD01_SYS"},
                "FBillNo": bill_no,
                "FDate": _dt.date.today().isoformat(),
                "FNote": "JM-SP-BOT 写入权限测试 - 自动删除",
            }
            if org:
                model_body["FSaleOrgId"] = {"FNumber": org}
            if cust:
                model_body["FCustomerID"] = {"FNumber": cust}
            if dept:
                model_body["FSaleDeptId"] = {"FNumber": dept}
            if saler:
                model_body["FSalerId"] = {"FNumber": saler}
            if stype:
                model_body["F_UXYO_Assistant"] = {"FNUMBER": stype}
            if mat:
                model_body["FSaleOrderEntry"] = [{
                    "FMaterialId": {"FNumber": mat},
                    "FQty": 1,
                    "FEntryNote": "测试条目",
                }]

            model = {
                "NeedUpDateFields": [],
                "NeedReturnFields": ["FBillNo", "FDate"],
                "IsDeleteEntry": "true",
                "Model": model_body,
            }

            # ── Save ──
            save_payload = {
                "format": 1, "useragent": "jm-sp-bot", "rid": str(uuid.uuid4()),
                "parameters": [FORMID_SALES_ORDER, json.dumps(model)],
                "timestamp": str(int(time.time())), "v": "1.0",
            }
            save_resp = client.post(urljoin(self.config.server_url, KINGDEE_SAVE_PATH), json=save_payload)
            save_data = save_resp.json()
            save_ok = is_write_success(save_data)
            results["save"] = {"ok": save_ok, "message": write_result_message(save_data) if not save_ok else "制单成功"}

            bill_id = None
            if save_ok:
                sr = save_data.get("Result")
                if isinstance(sr, dict):
                    bill_id = sr.get("Id")
                    bill_no = sr.get("Number") or bill_no

            if not bill_id:
                return {
                    "ok": False, "bill_no": bill_no, "bill_id": None,
                    "message": "制单失败，请检查金蝶基础资料编码是否正确",
                    "master_data": {"org": org, "customer": cust, "dept": dept, "saler": saler, "sale_type": stype, "material": mat},
                    "results": results,
                }

            # ── Submit ──
            sub_payload = {
                "format": 1, "useragent": "jm-sp-bot", "rid": str(uuid.uuid4()),
                "parameters": [FORMID_SALES_ORDER, json.dumps([bill_id])],
                "timestamp": str(int(time.time())), "v": "1.0",
            }
            sub_resp = client.post(urljoin(self.config.server_url, KINGDEE_SUBMIT_PATH), json=sub_payload)
            sub_data = sub_resp.json()
            sub_ok = is_write_success(sub_data)
            results["submit"] = {"ok": sub_ok, "message": write_result_message(sub_data) if not sub_ok else "提交成功"}

            if sub_ok:
                # ── Audit ──
                aud_payload = {
                    "format": 1, "useragent": "jm-sp-bot", "rid": str(uuid.uuid4()),
                    "parameters": [FORMID_SALES_ORDER, json.dumps([bill_id])],
                    "timestamp": str(int(time.time())), "v": "1.0",
                }
                aud_resp = client.post(urljoin(self.config.server_url, KINGDEE_AUDIT_PATH), json=aud_payload)
                aud_data = aud_resp.json()
                aud_ok = is_write_success(aud_data)
                results["audit"] = {"ok": aud_ok, "message": write_result_message(aud_data) if not aud_ok else "审核成功"}

                if aud_ok:
                    # ── UnAudit ──
                    unaud_payload = {
                        "format": 1, "useragent": "jm-sp-bot", "rid": str(uuid.uuid4()),
                        "parameters": [FORMID_SALES_ORDER, json.dumps([bill_id])],
                        "timestamp": str(int(time.time())), "v": "1.0",
                    }
                    unaud_resp = client.post(urljoin(self.config.server_url, KINGDEE_UNAUDIT_PATH), json=unaud_payload)
                    unaud_data = unaud_resp.json()
                    unaud_ok = is_write_success(unaud_data)
                    results["un_audit"] = {"ok": unaud_ok, "message": write_result_message(unaud_data) if not unaud_ok else "反审核成功"}
                else:
                    results["un_audit"] = {"ok": False, "skipped": True}
            else:
                results["audit"] = {"ok": False, "skipped": True}
                results["un_audit"] = {"ok": False, "skipped": True}

            # ── Delete ──
            del_payload = {
                "format": 1, "useragent": "jm-sp-bot", "rid": str(uuid.uuid4()),
                "parameters": [FORMID_SALES_ORDER, json.dumps([bill_id])],
                "timestamp": str(int(time.time())), "v": "1.0",
            }
            del_resp = client.post(urljoin(self.config.server_url, KINGDEE_DELETE_PATH), json=del_payload)
            del_data = del_resp.json()
            del_ok = is_write_success(del_data)
            results["delete"] = {"ok": del_ok, "message": write_result_message(del_data) if not del_ok else "删除成功"}

            if not del_ok:
                can_payload = {
                    "format": 1, "useragent": "jm-sp-bot", "rid": str(uuid.uuid4()),
                    "parameters": [FORMID_SALES_ORDER, json.dumps([bill_id])],
                    "timestamp": str(int(time.time())), "v": "1.0",
                }
                can_resp = client.post(urljoin(self.config.server_url, KINGDEE_CANCEL_PATH), json=can_payload)
                can_data = can_resp.json()
                can_ok = is_write_success(can_data)
                results["cancel"] = {"ok": can_ok, "message": write_result_message(can_data) if not can_ok else "作废成功"}

                del2_resp = client.post(urljoin(self.config.server_url, KINGDEE_DELETE_PATH), json=del_payload)
                del2_data = del2_resp.json()
                del2_ok = is_write_success(del2_data)
                results["delete_retry"] = {"ok": del2_ok, "message": write_result_message(del2_data) if not del2_ok else "删除成功"}

            all_ops = ["save", "submit", "audit", "un_audit", "cancel", "delete"]
            success = sum(1 for op in all_ops if results.get(op, {}).get("ok"))
            total = sum(1 for op in all_ops if op in results and not results[op].get("skipped"))

        return {
            "ok": bool(success), "bill_no": bill_no, "bill_id": bill_id,
            "total_operations": total, "success_count": success,
            "write_enabled": success >= 2,
            "summary": f"测试单据 {bill_no}：{success}/{total} 个写入操作成功" if total else "未执行写入操作",
            "results": results,
        }

    def _query_first_code_using_client(self, client: httpx.Client, *, form_id: str, field_keys: str) -> tuple[str, str]:
        """使用外部传入的 httpx.Client 查询基础资料，返回 (编码, 名称)"""
        try:
            payload = self.build_bill_query_payload(form_id=form_id, field_keys=field_keys)
            resp = client.post(urljoin(self.config.server_url, KINGDEE_BILL_QUERY_PATH), json=payload)
            data = resp.json()
            if not isinstance(data, list) or len(data) < 1 or not isinstance(data[0], list) or not isinstance(data[0][0], str):
                return ("", "")
            name = str(data[0][1]) if len(data[0]) > 1 and isinstance(data[0][1], str) else ""
            return (data[0][0], name)
        except Exception:
            return ("", "")


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


# ── 写入操作结果判断 ──

def is_write_success(data: Any) -> bool:
    """判断写入操作（Save/Submit/Audit/Cancel/Delete/View）是否成功"""
    if not isinstance(data, dict):
        return False
    if data.get("IsSuccess") is True:
        return True
    result = data.get("Result")
    if isinstance(result, dict):
        # Save 成功返回 {"Id": ..., "Number": ...}
        # 必须确保 Id 不是 None 且不是空字符串，因为失败时 Kingdee 会返回 "Id": ""
        if result.get("Id") is not None and str(result.get("Id")).strip() != "":
            return True
        rs = result.get("ResponseStatus")
        if isinstance(rs, dict) and rs.get("IsSuccess") is True:
            return True
    # Submit/Audit/Cancel 成功返回 {"Result": {"ResponseStatus": {"IsSuccess": true}}}
    response_status = data.get("ResponseStatus")
    if isinstance(response_status, dict) and response_status.get("IsSuccess") is True:
        return True
    return False


def write_result_message(data: Any) -> str:
    """从写入操作的返回中提取错误消息"""
    if not isinstance(data, dict):
        return ""
    # 优先取最外层的 Message
    msg = data.get("Message")
    if isinstance(msg, str) and msg:
        return msg
    # 从 Result.ResponseStatus.Errors 中提取
    result = data.get("Result")
    if isinstance(result, dict):
        rs = result.get("ResponseStatus")
        if isinstance(rs, dict):
            errors = rs.get("Errors")
            if isinstance(errors, list) and errors:
                messages = [str(e.get("Message") or e.get("FieldName") or e) for e in errors if isinstance(e, dict)]
                if messages:
                    return "；".join(messages)
    # 从外层 ResponseStatus.Errors 提取
    rs = data.get("ResponseStatus")
    if isinstance(rs, dict):
        errors = rs.get("Errors")
        if isinstance(errors, list) and errors:
            messages = [str(e.get("Message") or e.get("FieldName") or e) for e in errors if isinstance(e, dict)]
            if messages:
                return "；".join(messages)
    return ""


def test_kingdee_write_permissions_from_config(session: Session) -> dict[str, Any]:
    """从数据库配置读取金蝶连接信息，一站式测试所有写入权限"""
    try:
        config = kingdee_config_from_session(session)
    except KingdeeConfigError as exc:
        return {"ok": False, "message": str(exc), "results": {}}
    return KingdeeClient(config).test_write_permissions()
