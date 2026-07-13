#!/usr/bin/env python3
"""
金蝶云星空 Open API 写入权限测试（第二步：配齐必填字段）

发现 4 个必填字段：
  FCustId         — 客户
  FSaleDeptId     — 销售部门
  FSalerId        — 销售员
  F_UXYO_Assistant — 销售类型

先查这几个基础资料的编码，再制单 → 提交 → 审核 → 反审核 → 作废 → 删除
"""
import json, sys, time, uuid
import httpx

CONFIG = {
    "server_url": "http://vpn.3dyunzhan.com:17080/k3cloud/",
    "acct_id": "67d3f92ed0716e",
    "username": "机器人测试",
    "app_id": "345031_724v7zttQpn+3X1Gw37A3zwLyuXbSDMv",
    "app_sec": "f7538caeefc343c3973851e1333cb97f",
    "lcid": 2052,
}

BASE = CONFIG["server_url"].rstrip("/") + "/"
TIMEOUT = 30

LOGIN_URL = BASE + "Kingdee.BOS.WebApi.ServicesStub.AuthService.LoginByAppSecret.common.kdsvc"
QUERY_URL = BASE + "Kingdee.BOS.WebApi.ServicesStub.DynamicFormService.ExecuteBillQuery.common.kdsvc"
SAVE_URL = BASE + "Kingdee.BOS.WebApi.ServicesStub.DynamicFormService.Save.common.kdsvc"
SUBMIT_URL = BASE + "Kingdee.BOS.WebApi.ServicesStub.DynamicFormService.Submit.common.kdsvc"
AUDIT_URL = BASE + "Kingdee.BOS.WebApi.ServicesStub.DynamicFormService.Audit.common.kdsvc"
UNAUDIT_URL = BASE + "Kingdee.BOS.WebApi.ServicesStub.DynamicFormService.UnAudit.common.kdsvc"
CANCEL_URL = BASE + "Kingdee.BOS.WebApi.ServicesStub.DynamicFormService.Cancel.common.kdsvc"
DELETE_URL = BASE + "Kingdee.BOS.WebApi.ServicesStub.DynamicFormService.Delete.common.kdsvc"

SALES_ORDER_FORMID = "SAL_SaleOrder"

bill_id = None
bill_no = None
ok_count = 0
fail_count = 0

def is_ok(data):
    if not isinstance(data, dict):
        return False
    if data.get("LoginResultType") in (1, "1", True):
        return True
    if data.get("IsSuccess") is True:
        return True
    result = data.get("Result")
    if isinstance(result, dict):
        if result.get("Id") is not None:
            return True
        rs = result.get("ResponseStatus")
        if isinstance(rs, dict) and rs.get("IsSuccess") is True:
            return True
    rs = data.get("ResponseStatus")
    if isinstance(rs, dict) and rs.get("IsSuccess") is True:
        return True
    return False


def is_list_success(data):
    """ExecuteBillQuery 成功时返回 list，不是 dict"""
    return isinstance(data, list)

def call(client, name, url, payload):
    global ok_count, fail_count
    start = time.time()
    try:
        resp = client.post(url, json=payload, timeout=TIMEOUT)
        data = resp.json()
        elapsed = int((time.time() - start) * 1000)
    except Exception as e:
        elapsed = int((time.time() - start) * 1000)
        print(f"\n{'─'*60}")
        print(f"▶ {name}")
        print(f"  ❌ 异常 ({elapsed}ms): {e}")
        fail_count += 1
        return None, False
    ok = is_ok(data) or is_list_success(data)
    ds = json.dumps(data, ensure_ascii=False, indent=2)
    if len(ds) > 600:
        ds = ds[:600] + "\n  ...(截断)"
    print(f"\n{'─'*60}")
    print(f"▶ {name}  {'✅' if ok else '❌'} ({elapsed}ms)")
    print(f"  {ds}")
    if ok:
        ok_count += 1
    else:
        fail_count += 1
    return data, ok

def req_login():
    return {"format": 1, "useragent": "jm-sp-bot-test", "rid": str(uuid.uuid4()),
            "parameters": [CONFIG["acct_id"], CONFIG["username"],
                           CONFIG["app_id"], CONFIG["app_sec"], CONFIG["lcid"]],
            "timestamp": str(int(time.time())), "v": "1.0"}

def req_query(form_id, field_keys, filt=""):
    return {"format": 1, "useragent": "jm-sp-bot-test", "rid": str(uuid.uuid4()),
            "parameters": [{"FormId": form_id, "FieldKeys": field_keys,
                            "FilterString": filt, "OrderString": "",
                            "TopRowCount": 0, "StartRow": 0, "Limit": 10, "SubSystemId": ""}],
            "timestamp": str(int(time.time())), "v": "1.0"}

def req_save(model_data):
    return {"format": 1, "useragent": "jm-sp-bot-test", "rid": str(uuid.uuid4()),
            "parameters": [SALES_ORDER_FORMID, json.dumps(model_data)],
            "timestamp": str(int(time.time())), "v": "1.0"}

def req_operate(bill_ids):
    return {"format": 1, "useragent": "jm-sp-bot-test", "rid": str(uuid.uuid4()),
            "parameters": [SALES_ORDER_FORMID, json.dumps(bill_ids)],
            "timestamp": str(int(time.time())), "v": "1.0"}


print("=" * 60)
print("  金蝶销售订单制单 — 必填字段查询与写入测试")
print(f"  时间: {time.strftime('%Y-%m-%d %H:%M:%S')}")
print("=" * 60)

with httpx.Client(timeout=TIMEOUT) as client:
    # ── 登录 ──
    d, ok = call(client, "登录", LOGIN_URL, req_login())
    if not ok:
        sys.exit(1)

    # ── 查询 4 个必填字段的基础资料 ──
    print(f"\n{'='*60}")
    print("  🔍 查询必填字段基础资料")
    print(f"{'='*60}")

    # 存储查到的值
    org = "100"
    customer = ""
    dept = ""
    saler = ""
    sale_type = ""
    mat = ""

    # 1. 销售组织
    d, ok = call(client, "查询销售组织", QUERY_URL, req_query("ORG_Organizations", "FNumber,FName", ""))
    if is_list_success(d) and len(d) > 0 and isinstance(d[0], list) and len(d[0]) > 0 and isinstance(d[0][0], str):
        org = str(d[0][0])
        print(f"  → 销售组织: {org} ({d[0][1] if len(d[0]) > 1 else ''})")

    # 2. 客户
    d, ok = call(client, "查询客户", QUERY_URL, req_query("BD_Customer", "FNumber,FName", ""))
    if is_list_success(d) and len(d) > 0 and isinstance(d[0], list) and len(d[0]) > 0 and isinstance(d[0][0], str):
        customer = str(d[0][0])
        print(f"  → 客户: {customer} ({d[0][1] if len(d[0]) > 1 else ''})")

    # 3. 部门
    d, ok = call(client, "查询部门", QUERY_URL, req_query("BD_Department", "FNumber,FName", ""))
    if is_list_success(d) and len(d) > 0 and isinstance(d[0], list) and len(d[0]) > 0 and isinstance(d[0][0], str):
        dept = str(d[0][0])
        print(f"  → 部门: {dept} ({d[0][1] if len(d[0]) > 1 else ''})")

    # 4. 销售员（金蝶员工表是 BD_Employee，备用 EMP_Employee）
    for fid in ["BD_Employee", "EMP_Employee"]:
        if not saler:
            d, _ = call(client, f"查询员工({fid})", QUERY_URL, req_query(fid, "FNumber,FName", ""))
            if is_list_success(d) and len(d) > 0 and isinstance(d[0], list) and len(d[0]) > 0 and isinstance(d[0][0], str):
                saler = str(d[0][0])
                print(f"  → 销售员: {saler} ({d[0][1] if len(d[0]) > 1 else ''})")

    # 5. 销售类型
    for fid in ["SAL_SaleType", "SAL_SaleTypeNew"]:
        if not sale_type:
            d, _ = call(client, f"查询销售类型({fid})", QUERY_URL, req_query(fid, "FNumber,FName", ""))
            if is_list_success(d) and len(d) > 0 and isinstance(d[0], list) and len(d[0]) > 0 and isinstance(d[0][0], str):
                sale_type = str(d[0][0])
                print(f"  → 销售类型: {sale_type} ({d[0][1] if len(d[0]) > 1 else ''})")

    # 6. 物料
    d, ok = call(client, "查询物料", QUERY_URL, req_query("BD_MATERIAL", "FNumber,FName", ""))
    if is_list_success(d) and len(d) > 0 and isinstance(d[0], list) and len(d[0]) > 0 and isinstance(d[0][0], str):
        mat = str(d[0][0])
        print(f"  → 物料: {mat} ({d[0][1] if len(d[0]) > 1 else ''})")

    print(f"\n  📋 准备数据：")
    print(f"     销售组织: {org}")
    print(f"     客户:     {customer}")
    print(f"     部门:     {dept}")
    print(f"     销售员:   {saler}")
    print(f"     销售类型: {sale_type}")
    print(f"     物料:     {mat}")

    # ── 制单 ──
    ts = str(int(time.time()))
    model_body = {
        "FBillTypeID": {"FNUMBER": "XSDD01_SYS"},
        "FBillNo": f"TEST-WRITE-{ts}",
        "FDate": time.strftime("%Y-%m-%d"),
        "FSaleOrgId": {"FNumber": org},
        "FCustomerID": {"FNumber": customer},
        "FSaleDeptId": {"FNumber": dept},
        "FNote": "JM-SP-BOT 写入权限测试",
    }
    if saler:
        model_body["FSalerId"] = {"FNumber": saler}
    if sale_type:
        model_body["F_UXYO_Assistant"] = {"FNUMBER": sale_type}
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

    ds = json.dumps(model["Model"], ensure_ascii=False, indent=2)
    print(f"\n{'='*60}")
    print(f"  📝 准备制单数据:")
    print(f"  {ds[:800]}")
    print(f"{'='*60}")

    d, ok = call(client, "① 制单 Save", SAVE_URL, req_save(model))

    if ok:
        r = d.get("Result", {})
        if isinstance(r, dict):
            bill_id = r.get("Id")
            bill_no = r.get("Number")
        if bill_id:
            print(f"\n  📄 单据内码={bill_id}, 编号={bill_no}")

            # ── 提交 ──
            d, ok = call(client, "② 提交 Submit", SUBMIT_URL, req_operate([bill_id]))

            if ok:
                # ── 审核 ──
                d, ok = call(client, "③ 审核 Audit", AUDIT_URL, req_operate([bill_id]))

                if ok:
                    # ── 反审核 ──
                    call(client, "④ 反审核 UnAudit", UNAUDIT_URL, req_operate([bill_id]))

            # ── 清理 ──
            d, ok = call(client, "⑤ 删除 Delete", DELETE_URL, req_operate([bill_id]))
            if not ok:
                call(client, "⑤b 作废 Cancel", CANCEL_URL, req_operate([bill_id]))
                call(client, "⑤c 再次删除 Delete", DELETE_URL, req_operate([bill_id]))
    else:
        print(f"\n  ⚠️ 制单失败，请检查必填字段的值是否正确")

print(f"\n{'='*60}")
print(f"  测试汇总")
print(f"  ✅ 成功: {ok_count}")
print(f"  ❌ 失败: {fail_count}")
if bill_no:
    print(f"  📄 单据编号: {bill_no}")
print(f"{'='*60}")
