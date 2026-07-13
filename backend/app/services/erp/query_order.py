import sys, json
from backend.app.database import SessionLocal
from backend.app.services.erp.kingdee_client import KingdeeClient, kingdee_config_from_session

def main():
    session = SessionLocal()
    try:
        config = kingdee_config_from_session(session)
        client = KingdeeClient(config)
        
        # Test Delete with {"Ids": "190779"}
        payload = {
            "CreateOrgId": 0,
            "Numbers": [],
            "Ids": "190779",
            "SelectedPostId": 0
        }
        params = [json.dumps(payload)]
        
        print("Sending Delete for 190779...")
        res = client._write_operation(
            endpoint_path="Kingdee.BOS.WebApi.ServicesStub.DynamicFormService.Delete.common.kdsvc",
            form_id="SAL_SaleOrder",
            params=params,
            label="Delete"
        )
        print("Delete Response 190779:")
        print(json.dumps(res, ensure_ascii=False, indent=2))
        
        # Test Delete with {"Ids": "190780"}
        payload2 = {
            "CreateOrgId": 0,
            "Numbers": [],
            "Ids": "190780",
            "SelectedPostId": 0
        }
        params2 = [json.dumps(payload2)]
        
        print("Sending Delete for 190780...")
        res2 = client._write_operation(
            endpoint_path="Kingdee.BOS.WebApi.ServicesStub.DynamicFormService.Delete.common.kdsvc",
            form_id="SAL_SaleOrder",
            params=params2,
            label="Delete"
        )
        print("Delete Response 190780:")
        print(json.dumps(res2, ensure_ascii=False, indent=2))
        
    finally:
        session.close()

if __name__ == "__main__":
    main()
