#!/usr/bin/env python3
"""查询金蝶云星空：销售组织和仓库（用于确认主体-仓库映射）"""
import json, time, uuid
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

def req_login():
    return {"format": 1, "useragent": "jm-sp-bot", "rid": str(uuid.uuid4()),
            "parameters": [CONFIG["acct_id"], CONFIG["username"],
                           CONFIG["app_id"], CONFIG["app_sec"], CONFIG["lcid"]],
            "timestamp": str(int(time.time())), "v": "1.0"}

def req_query(form_id, fields, limit=100):
    return {"format": 1, "useragent": "jm-sp-bot", "rid": str(uuid.uuid4()),
            "parameters": [{"FormId": form_id, "FieldKeys": fields,
                            "FilterString": "", "OrderString": "",
                            "TopRowCount": 0, "StartRow": 0,
                            "Limit": limit, "SubSystemId": ""}],
            "timestamp": str(int(time.time())), "v": "1.0"}

def query(name, form_id, fields, limit=100):
    try:
        with httpx.Client(timeout=TIMEOUT) as c:
            r1 = c.post(LOGIN_URL, json=req_login())
            r1.raise_for_status()
            r2 = c.post(QUERY_URL, json=req_query(form_id, fields, limit))
            data = r2.json()
            if isinstance(data, list):
                print(f"\n{'='*60}")
                print(f"  {name} ({len(data)} 条)")
                print(f"{'='*60}")
                for row in data:
                    if isinstance(row, list) and len(row) >= 2:
                        vals = [str(v) if v is not None else '' for v in row]
                        print(f"  {' | '.join(vals)}")
            else:
                print(f"\n{name}: {json.dumps(data, ensure_ascii=False)[:300]}")
    except Exception as e:
        print(f"❌ {name}: {e}")

print("="*60)
print("  金蝶云星空查询")
print(f"  时间: {time.strftime('%Y-%m-%d %H:%M:%S')}")
print("="*60)

query("销售组织 (FSaleOrgId)", "ORG_Organizations", "FNumber,FName,FOrgFormID", 50)
query("仓库 (BD_STOCK)", "BD_STOCK", "FNumber,FName,FFullName", 50)
