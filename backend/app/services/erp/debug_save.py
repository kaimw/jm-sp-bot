import sys, json
from backend.app.database import SessionLocal
from backend.app.models import MiddlePlatformOrder
from backend.app.services.erp.kingdee_client import KingdeeClient, kingdee_config_from_session
from backend.app.services.erp.sales_order_mapper import build_sales_order_model

def main():
    session = SessionLocal()
    try:
        order_id = "2f53729d-ee16-4c46-8f74-bd113382967a"
        order = session.get(MiddlePlatformOrder, order_id)
        if not order:
            print("Order not found in DB!")
            return
            
        config = kingdee_config_from_session(session)
        client = KingdeeClient(config)
        
        # Build model
        bill_model = build_sales_order_model(session, order, order.items)
        
        # Manually fix fields in the model for testing
        # 1. Change FCustomerID to FCustId
        if "FCustomerID" in bill_model:
            bill_model["FCustId"] = bill_model.pop("FCustomerID")
        else:
            bill_model["FCustId"] = {"FNumber": "100JM000009"}
            
        # 2. Set FSaleDeptId to 100BM004.01 (商务部\\商务组) to match the salesperson's department!
        bill_model["FSaleDeptId"] = {"FNumber": "100BM004.01"}
        
        # 3. Set FSalerId to 00007_100GW000012_1 (宋勤红)
        bill_model["FSalerId"] = {"FNumber": "00007_100GW000012_1"}
        
        # 4. Set F_UXYO_Assistant to 001
        bill_model["F_UXYO_Assistant"] = {"FNUMBER": "001"}
        
        # 5. Set FPrice to 5999.0 in entry
        if "FSaleOrderEntry" in bill_model and len(bill_model["FSaleOrderEntry"]) > 0:
            bill_model["FSaleOrderEntry"][0]["FPrice"] = 5999.0
            
        # ───【测试：将 FBillNo 设为空字符串，让金蝶自动分配其内部编码规则下的单号】───
        bill_model["FBillNo"] = ""
        
        print("Modified model for test:")
        print(json.dumps(bill_model, ensure_ascii=False, indent=2))
        
        # Call _write_operation directly
        save_res = client._write_operation(
            endpoint_path="Kingdee.BOS.WebApi.ServicesStub.DynamicFormService.Save.common.kdsvc",
            form_id="SAL_SaleOrder",
            params=[{
                "NeedUpDateFields": [],
                "NeedReturnFields": ["FBillNo", "FDate"],
                "IsDeleteEntry": "true",
                "SubSystemId": "",
                "IsVerifyBaseDataField": "false",
                "IsEntryBatchFill": "true",
                "ValidateBehavior": "Volume",
                "Model": bill_model,
            }],
            label="Save"
        )
        print("Save Response Raw:")
        print(json.dumps(save_res, ensure_ascii=False, indent=2))
            
    finally:
        session.close()

if __name__ == "__main__":
    main()
