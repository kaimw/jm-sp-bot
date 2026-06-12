from __future__ import annotations

import shutil
from types import SimpleNamespace

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from backend.app.database import Base
from backend.app.models import CrmOrderSnapshot, CrmSalesOrder, ExceptionCase, MiddlePlatformOrder, OrderAttachment, OutboundMailJob, ProcessingJob, ProductInventorySnapshot, ProductSKU, ProductSPU
from backend.app.services.attachment_parser import parse_attachment
from backend.app.services.bootstrap import seed_defaults, set_config
from backend.app.services.crm_sync import upsert_crm_sales_orders
from backend.app.services.crm_attachment_extraction import enrich_order_from_attachment_text
from backend.app.services import crm_attachment_extraction
from backend.app.services.jobs import run_pending_jobs
from backend.app.services.order_middle_platform import (
    IllegalStateTransition,
    OrderEvent,
    OrderStatus,
    confirm_delivery_notice,
    crm_order_parsed_event,
    process_crm_order_parsed_event,
    process_oms_status_update,
    transition_order,
)
from backend.app.services.jsonutil import dumps, loads
from backend.app.main import serialize_crm_order_with_flow


def make_session():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(bind=engine)
    Session = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)
    session = Session()
    seed_defaults(session)
    session.commit()
    return session


def seed_active_sku(session, sku_id: str = "SKU-3D-SCANNER-PRO") -> None:
    spu = ProductSPU(spu_id="SPU-3D-SCANNER", name="3D Scanner")
    session.add(spu)
    session.flush()
    session.add(ProductSKU(spu_uuid=spu.id, sku_id=sku_id, status="Active"))
    session.commit()


def seed_inventory(session, sku_id: str = "SKU-3D-SCANNER-PRO", quantity: int = 100) -> None:
    session.add(
        ProductInventorySnapshot(
            material_code=sku_id,
            material_name="3D Scanner",
            warehouse_code="A1",
            warehouse_name="武汉工厂仓",
            base_qty=quantity,
            qty=quantity,
            source_payload_json=dumps({"canUseQuantity": quantity}),
        )
    )
    session.commit()


def seed_oms_required_config(session) -> None:
    set_config(session, "oms_owner_code", "OWNER-001")
    set_config(session, "oms_warehouse_code", "WH-001")
    set_config(session, "oms_shop_code", "SHOP-001")
    set_config(session, "oms_logistic_code", "SF")
    session.commit()


def valid_crm_order_row(**overrides):
    row = {
        "crm_order_id": "crm_obj_001",
        "crm_order_no": "SO-001",
        "customer_name": "亚马逊北美渠道",
        "sales_user_name": "Alice",
        "owner_department": "商务一部",
        "life_status": "normal",
        "approval_status": "approved",
        "order_date": "2026-06-12",
        "order_amount": "125000.00",
        "received_amount": "25000.00",
        "receivable_amount": "100000.00",
        "product_amount": "125000.00",
        "settlement_method": "option1",
        "receipt_contact": "张三",
        "receipt_phone": "18600001111",
        "receipt_address": "湖北省武汉市东湖高新区测试路 1 号",
        "delivery_date": "2026-06-30",
        "attachment_files": "采购订单.pdf; 合同盖章版.pdf",
        "items": [{"sku_code": "SKU-3D-SCANNER-PRO", "quantity": 50, "unit_price": "2500", "line_amount": "125000"}],
    }
    row.update(overrides)
    return row


def create_delivery_ready_order(session):
    seed_oms_required_config(session)
    seed_active_sku(session)
    seed_inventory(session, quantity=100)
    result = upsert_crm_sales_orders(session, [valid_crm_order_row()])
    session.commit()
    assert result["queued_events"] == 1
    run_pending_jobs(session)
    return session.query(MiddlePlatformOrder).one()


def create_delivery_ready_order_without_oms_config(session):
    seed_active_sku(session)
    seed_inventory(session, quantity=100)
    result = upsert_crm_sales_orders(session, [valid_crm_order_row()])
    session.commit()
    assert result["queued_events"] == 1
    run_pending_jobs(session)
    return session.query(MiddlePlatformOrder).one()


def test_crm_order_event_builds_delivery_preview_then_pushes_after_confirmation():
    session = make_session()
    order = create_delivery_ready_order(session)
    assert order.status == OrderStatus.DELIVERY_NOTICE_READY.value
    assert order.delivery_notices[0].status == "Previewed"
    assert order.delivery_notices[0].confirmed_at is None

    confirm_delivery_notice(session, order.delivery_notices[0], confirmed_by="tester")
    session.commit()
    second = run_pending_jobs(session)

    assert second["completed"] == 1
    session.refresh(order)
    assert order.status == OrderStatus.OMS_ACCEPTED.value
    assert order.delivery_notices[0].status == "Accepted"
    assert order.delivery_notices[0].confirmed_by == "tester"


def test_delivery_confirmation_blocks_when_oms_required_config_missing():
    session = make_session()
    order = create_delivery_ready_order_without_oms_config(session)

    with pytest.raises(RuntimeError, match="OMS 下推必填字段缺失"):
        confirm_delivery_notice(session, order.delivery_notices[0], confirmed_by="tester")
    session.commit()

    session.refresh(order)
    assert order.status == OrderStatus.DELIVERY_NOTICE_READY.value
    assert order.delivery_notices[0].status == "Blocked"
    case = session.query(ExceptionCase).one()
    assert case.exception_type == "OMS_REQUIRED_FIELDS_MISSING"
    assert "货主CODE" in case.detail


def test_oms_idempotency_conflict_is_resolved_by_reverse_lookup(monkeypatch):
    import backend.app.services.order_middle_platform as omp

    class FakeJackyunClient:
        def create_delivery_order(self, payload, *, method="wms.order.create"):
            return {"ok": False, "message": "订单已存在，重复请求", "raw": {"code": "DUPLICATE"}}

        def query_delivery_orders(self, payload):
            return {
                "ok": True,
                "data": {"rows": [{"erporderNo": payload["erporderNo"], "orderNo": "OMS-EXISTING-001"}]},
                "raw": {"rows": [{"erporderNo": payload["erporderNo"], "orderNo": "OMS-EXISTING-001"}]},
            }

    session = make_session()
    set_config(session, "oms_enabled", "true")
    set_config(session, "oms_mock_success", "false")
    session.commit()
    monkeypatch.setattr(omp, "jackyun_client_from_session", lambda _session: FakeJackyunClient())
    order = create_delivery_ready_order(session)

    confirm_delivery_notice(session, order.delivery_notices[0], confirmed_by="tester")
    session.commit()
    run_pending_jobs(session)

    session.refresh(order)
    assert order.status == OrderStatus.OMS_ACCEPTED.value
    assert order.delivery_notices[0].oms_order_no == "OMS-EXISTING-001"
    assert order.delivery_notices[0].status == "Accepted"


def test_oms_status_sync_advances_to_picking_and_shipped():
    session = make_session()
    order = create_delivery_ready_order(session)
    confirm_delivery_notice(session, order.delivery_notices[0], confirmed_by="tester")
    session.commit()
    run_pending_jobs(session)
    session.refresh(order)
    assert order.status == OrderStatus.OMS_ACCEPTED.value

    result = process_oms_status_update(session, {"notice_id": order.delivery_notices[0].id, "oms_status": "拣货中"})
    session.commit()
    session.refresh(order)
    assert result["normalized_status"] == "picking"
    assert order.status == OrderStatus.PICKING.value
    assert order.delivery_notices[0].status == "Picking"

    result = process_oms_status_update(session, {"notice_id": order.delivery_notices[0].id, "oms_status": "已发货", "raw": {"carrier": "SF"}})
    session.commit()
    session.refresh(order)
    assert result["normalized_status"] == "shipped"
    assert order.status == OrderStatus.SHIPPED.value
    assert order.delivery_notices[0].status == "Shipped"


def test_oms_status_sync_job_can_skip_directly_to_shipped():
    session = make_session()
    order = create_delivery_ready_order(session)
    confirm_delivery_notice(session, order.delivery_notices[0], confirmed_by="tester")
    session.commit()
    run_pending_jobs(session)
    session.refresh(order)

    session.add(
        ProcessingJob(
            job_type="OMS_STATUS_SYNC",
            payload_json=dumps({"notice_id": order.delivery_notices[0].id, "oms_status": "shipped"}),
            status="Pending",
        )
    )
    session.commit()
    result = run_pending_jobs(session)

    assert result["completed"] == 1
    session.refresh(order)
    assert order.status == OrderStatus.SHIPPED.value
    assert order.delivery_notices[0].status == "Shipped"


def test_crm_order_detail_includes_middle_platform_flow():
    session = make_session()
    order = create_delivery_ready_order(session)
    crm = session.query(CrmSalesOrder).filter_by(crm_order_id=order.crm_order_id).one()

    detail = serialize_crm_order_with_flow(session, crm)

    assert detail["flow"]["middle_order"]["order_no"] == order.order_no
    assert detail["snapshots"][0]["payload_hash"] == crm.payload_hash
    assert detail["flow"]["crm_snapshots"][0]["is_latest"] is True
    assert detail["flow"]["middle_order"]["status"] == OrderStatus.DELIVERY_NOTICE_READY.value
    assert [step["key"] for step in detail["flow"]["steps"]] == ["crm", "imported", "validation", "notice", "oms", "fulfillment"]
    assert detail["flow"]["steps"][2]["status"] == "done"
    assert detail["flow"]["steps"][3]["status"] == "done"


def test_crm_sync_records_detail_snapshots_and_attachments():
    session = make_session()
    result = upsert_crm_sales_orders(session, [valid_crm_order_row(attachments=[{"file_name": "客户PO.pdf", "file_id": "file-001", "type": "PO"}])])
    session.commit()

    assert result["queued_events"] == 1
    crm = session.query(CrmSalesOrder).filter_by(crm_order_id="crm_obj_001").one()
    snapshots = session.query(CrmOrderSnapshot).filter_by(crm_order_id="crm_obj_001").order_by(CrmOrderSnapshot.version).all()
    attachments = session.query(OrderAttachment).filter_by(crm_order_id="crm_obj_001", payload_hash=crm.payload_hash).all()
    assert [snapshot.version for snapshot in snapshots] == [1]
    assert snapshots[0].is_latest is True
    assert crm.latest_snapshot_id == snapshots[0].id
    assert {attachment.file_name for attachment in attachments} == {"客户PO.pdf", "采购订单.pdf", "合同盖章版.pdf"}

    upsert_crm_sales_orders(session, [valid_crm_order_row(order_amount="126000.00", product_amount="126000.00", receivable_amount="101000.00")])
    session.commit()

    snapshots = session.query(CrmOrderSnapshot).filter_by(crm_order_id="crm_obj_001").order_by(CrmOrderSnapshot.version).all()
    assert [snapshot.version for snapshot in snapshots] == [1, 2]
    assert [snapshot.is_latest for snapshot in snapshots] == [False, True]


def test_crm_sync_merges_order_detail_fields_and_downloadable_attachments():
    session = make_session()
    result = upsert_crm_sales_orders(session, [
        valid_crm_order_row(
            sales_user_name="",
            detail_sync_status="Synced",
            detail_raw={"Value": {"data": {"id": "crm_obj_001"}}},
            sales_user_id="owner-001",
            owner_department="商务一部",
            receipt_contact="李四",
            receipt_phone="18600002222",
            receipt_address="深圳市南山区测试路 2 号",
            delivery_date="2026-07-01",
            remark="详情接口备注",
            attachments=[{"file_name": "客户PO.pdf", "file_url": "https://example.test/po.pdf", "file_id": "file-001"}],
        )
    ])
    session.commit()

    assert result["queued_events"] == 1
    crm = session.query(CrmSalesOrder).filter_by(crm_order_id="crm_obj_001").one()
    attachment = session.query(OrderAttachment).filter_by(crm_order_id="crm_obj_001", file_url="https://example.test/po.pdf").one()
    detail = serialize_crm_order_with_flow(session, crm)
    assert crm.receipt_contact == "李四"
    assert crm.receipt_phone == "18600002222"
    assert crm.receipt_address == "深圳市南山区测试路 2 号"
    assert crm.delivery_date == "2026-07-01"
    assert attachment.file_url == "https://example.test/po.pdf"
    assert detail["crm_detail_status"] == "detail_available"
    assert any(item["has_download"] for item in detail["attachments"])


def test_crm_attachment_extraction_fills_oms_receiver_fields_without_confusing_contract_signer():
    session = make_session()
    crm = CrmSalesOrder(
        crm_order_id="crm_obj_extract",
        crm_order_no="SO-EXTRACT",
        customer_name="附件客户",
        sales_user_name="Alice",
        owner_department="商务一部",
        life_status="normal",
        approval_status="approved",
        order_date="2026-06-12",
        settlement_method="CNY",
        currency="CNY",
        order_amount="100.00",
        product_amount="100.00",
        received_amount="0.00",
        receivable_amount="100.00",
        attachment_files_json=dumps(["合同.pdf"]),
        payload_hash="hash-extract",
        raw_json=dumps({}),
    )
    session.add(crm)
    session.flush()

    result = enrich_order_from_attachment_text(
        session,
        crm,
        [
            (
                None,
                "\n".join(
                    [
                        "甲方授权代表/合同签订人：王签约 13900000000",
                        "收货信息：收货人：赵物流 电话：18612345678",
                        "收货地址：广东省深圳市南山区科技园测试路 8 号",
                        "交货日期：2026-07-05",
                    ]
                ),
            )
        ],
    )

    assert result.receipt_contact == "赵物流"
    assert crm.receipt_contact == "赵物流"
    assert crm.receipt_phone == "18612345678"
    assert crm.receipt_address == "广东省深圳市南山区科技园测试路 8 号"
    assert crm.delivery_date == "2026-07-05"
    assert "王签约" not in crm.receipt_contact


def test_crm_attachment_extraction_overwrites_coarse_receiver_address():
    session = make_session()
    crm = CrmSalesOrder(
        crm_order_id="crm_obj_coarse_address",
        crm_order_no="SO-COARSE",
        payload_hash="hash-coarse",
        receipt_address="北京",
        raw_json=dumps({}),
    )
    session.add(crm)
    session.flush()

    result = enrich_order_from_attachment_text(
        session,
        crm,
        [
            (
                None,
                "\n".join(
                    [
                        "收货人：林杨",
                        "联系方式：13911704967",
                        "收货地址：北京市海淀区中关村南大街 1 号 2 号楼 301 室",
                    ]
                ),
            )
        ],
    )

    assert result.receipt_address == "北京市海淀区中关村南大街 1 号 2 号楼 301 室"
    assert crm.receipt_address == "北京市海淀区中关村南大街 1 号 2 号楼 301 室"


def test_crm_attachment_extraction_handles_delivery_place_contact_line():
    session = make_session()
    crm = CrmSalesOrder(crm_order_id="crm_obj_place_contact", crm_order_no="SO-PLACE", payload_hash="hash-place", raw_json=dumps({}))
    session.add(crm)
    session.flush()

    result = enrich_order_from_attachment_text(
        session,
        crm,
        [(None, "设备交付地点及联系人：陕西省西安市雁塔区沣惠南路摩尔中心C座11楼道通科技，康江，18809185717。")],
    )

    assert result.receipt_address == "陕西省西安市雁塔区沣惠南路摩尔中心C座11楼道通科技"
    assert result.receipt_contact == "康江"
    assert result.receipt_phone == "18809185717"


def test_crm_attachment_extraction_prefers_purchase_party_contact_block():
    session = make_session()
    crm = CrmSalesOrder(crm_order_id="crm_obj_purchase_party", crm_order_no="SO-PURCHASE", payload_hash="hash-purchase", raw_json=dumps({}))
    session.add(crm)
    session.flush()

    result = enrich_order_from_attachment_text(
        session,
        crm,
        [
            (
                None,
                "\n".join(
                    [
                        "采购订单",
                        "采购方信息",
                        "采购方名称：北京南得空间信息技术有限公司",
                        "地址：北京市丰台区西三环南路 14 号院 1 号楼 11 层 1114 室",
                        "联系人：陈亮",
                        "电话：18612795555",
                        "供方信息",
                        "联系人：",
                        "电话：0755-23910066",
                    ]
                ),
            )
        ],
    )

    assert result.receipt_contact == "陈亮"
    assert result.receipt_phone == "18612795555"
    assert result.receipt_address == "北京市丰台区西三环南路 14 号院 1 号楼 11 层 1114 室"
    assert result.manual_review_required is False


def test_crm_attachment_extraction_uses_llm_when_rule_result_fails_validation(monkeypatch):
    session = make_session()
    crm = CrmSalesOrder(crm_order_id="crm_obj_llm", crm_order_no="SO-LLM", payload_hash="hash-llm", raw_json=dumps({}))
    session.add(crm)
    session.flush()

    monkeypatch.setattr(crm_attachment_extraction, "llm_fallback_enabled", lambda session: True)
    monkeypatch.setattr(crm_attachment_extraction, "active_model_config", lambda session: SimpleNamespace(id="model"))
    monkeypatch.setattr(crm_attachment_extraction, "model_ready", lambda session, config: True)
    monkeypatch.setattr(
        crm_attachment_extraction,
        "call_model",
        lambda *args, **kwargs: {
            "choices": [
                {
                    "message": {
                        "content": dumps(
                            {
                                "receipt_contact": "陈亮",
                                "receipt_phone": "18612795555",
                                "receipt_address": "北京市丰台区西三环南路14号院1号楼11层1114室",
                                "delivery_date": "",
                                "confidence": 93,
                                "reason": "采购方信息块更符合收货信息",
                            }
                        )
                    }
                }
            ]
        },
    )

    result = enrich_order_from_attachment_text(
        session,
        crm,
        [(None, "采购方信息\n地址：北京市丰台区西三环南路14号院1号楼11层1114室\n联系人：\n电话：0755-23910066")],
    )

    assert result.receipt_contact == "陈亮"
    assert result.receipt_phone == "18612795555"
    assert result.manual_review_required is False


def test_crm_attachment_extraction_marks_manual_review_when_llm_still_invalid(monkeypatch):
    session = make_session()
    crm = CrmSalesOrder(crm_order_id="crm_obj_manual", crm_order_no="SO-MANUAL", payload_hash="hash-manual", raw_json=dumps({}))
    session.add(crm)
    session.flush()

    monkeypatch.setattr(crm_attachment_extraction, "llm_fallback_enabled", lambda session: True)
    monkeypatch.setattr(crm_attachment_extraction, "active_model_config", lambda session: SimpleNamespace(id="model"))
    monkeypatch.setattr(crm_attachment_extraction, "model_ready", lambda session, config: True)
    monkeypatch.setattr(
        crm_attachment_extraction,
        "call_model",
        lambda *args, **kwargs: {"choices": [{"message": {"content": dumps({"receipt_contact": ":", "receipt_phone": "0755", "receipt_address": "北京", "confidence": 50})}}]},
    )

    result = enrich_order_from_attachment_text(session, crm, [(None, "联系人：\n电话：0755\n地址：北京")])

    assert result.manual_review_required is True
    assert result.validation_errors
    assert not crm.receipt_contact
    assert not crm.receipt_phone


def test_crm_attachment_extraction_applies_valid_fields_even_when_one_required_field_fails():
    session = make_session()
    crm = CrmSalesOrder(crm_order_id="crm_obj_partial", crm_order_no="SO-PARTIAL", payload_hash="hash-partial", raw_json=dumps({}))
    session.add(crm)
    session.flush()

    result = enrich_order_from_attachment_text(
        session,
        crm,
        [
            (
                None,
                "\n".join(
                    [
                        "采购方信息",
                        "地址：北京市丰台区西三环南路14号院1号楼11层1114室",
                        "联系人：陈亮",
                    ]
                ),
            )
        ],
    )

    assert result.manual_review_required is True
    assert "联系方式电话未通过校验" in result.validation_errors
    assert crm.receipt_contact == "陈亮"
    assert crm.receipt_address == "北京市丰台区西三环南路14号院1号楼11层1114室"
    assert not crm.receipt_phone


def test_crm_order_detail_dedupes_current_payload_attachments():
    session = make_session()
    crm = CrmSalesOrder(
        crm_order_id="crm_obj_dedupe",
        crm_order_no="SO-DEDUPE",
        customer_name="去重客户",
        sales_user_name="Alice",
        owner_department="商务一部",
        life_status="normal",
        approval_status="approved",
        order_date="2026-06-12",
        settlement_method="CNY",
        currency="CNY",
        order_amount="100.00",
        product_amount="100.00",
        received_amount="0.00",
        receivable_amount="100.00",
        receipt_contact="张三",
        receipt_phone="18600005555",
        receipt_address="深圳市测试路 1 号",
        attachment_files_json=dumps(["采购合同.pdf", "采购合同.pdf"]),
        payload_hash="hash-current",
        raw_json=dumps({}),
    )
    session.add(crm)
    session.flush()
    for index, payload_hash in enumerate(["hash-old", "hash-current", "hash-current"], start=1):
        session.add(
            OrderAttachment(
                crm_sales_order_id=crm.id,
                crm_order_id=crm.crm_order_id,
                crm_order_no=crm.crm_order_no,
                payload_hash=payload_hash,
                file_name="采购合同.pdf",
                file_url=f"https://example.test/{index}.pdf",
                fingerprint=f"fp-{index}",
                evidence_json=dumps({}),
                raw_json=dumps({}),
            )
        )
    session.add(
        OrderAttachment(
            crm_sales_order_id=crm.id,
            crm_order_id=crm.crm_order_id,
            crm_order_no=crm.crm_order_no,
            payload_hash="hash-current",
            file_name="采购合同.pdf",
            file_url=None,
            fingerprint="fp-name-only",
            evidence_json=dumps({}),
            raw_json=dumps({}),
        )
    )
    session.commit()

    detail = serialize_crm_order_with_flow(session, crm)

    assert [item["file_name"] for item in detail["attachments"]] == ["采购合同.pdf"]
    assert detail["attachments"][0]["file_url"] in {"https://example.test/2.pdf", "https://example.test/3.pdf"}
    assert detail["attachments"][0]["has_download"] is True
    assert detail["attachments"][0]["download_url"].startswith("/api/crm/order-attachments/")


def test_image_ocr_text_can_feed_oms_receiver_extraction():
    if not shutil.which("tesseract"):
        pytest.skip("tesseract not installed")
    from PIL import Image, ImageDraw, ImageFont
    import io

    image = Image.new("RGB", (1200, 360), "white")
    draw = ImageDraw.Draw(image)
    font = ImageFont.truetype("/System/Library/Fonts/Supplemental/Arial.ttf", 34)
    draw.text((30, 30), "Contract signer: Wrong Person 13900000000", fill="black", font=font)
    draw.text((30, 100), "Receiver: Zhao Logistics", fill="black", font=font)
    draw.text((30, 150), "Phone: 18612345678", fill="black", font=font)
    draw.text((30, 210), "Shipping address: Shenzhen Test Road 8", fill="black", font=font)
    draw.text((30, 270), "Delivery date: 2026-07-05", fill="black", font=font)
    buffer = io.BytesIO()
    image.save(buffer, format="PNG")

    parsed = parse_attachment("scan.png", buffer.getvalue(), max_zip_bytes=1024 * 1024, max_depth=1)
    session = make_session()
    crm = CrmSalesOrder(crm_order_id="crm_obj_ocr", crm_order_no="SO-OCR", payload_hash="hash-ocr", raw_json=dumps({}))
    session.add(crm)
    session.flush()

    result = enrich_order_from_attachment_text(session, crm, [(None, parsed.text)])

    assert parsed.status == "Parsed"
    assert result.receipt_phone == "18612345678"
    assert result.delivery_date == "2026-07-05"
    assert "Wrong" not in (result.receipt_contact or "")


def test_delivery_confirmation_blocks_when_receiver_phone_missing():
    session = make_session()
    order = create_delivery_ready_order(session)
    order.crm_order.receipt_phone = None
    raw = loads(order.crm_order.raw_json, {})
    raw.pop("receipt_phone", None)
    order.crm_order.raw_json = dumps(raw)
    order.delivery_notices[0].payload_json = dumps(
        {
            **loads(order.delivery_notices[0].payload_json, {}),
            "orderInfo": {
                "receiverName": order.crm_order.receipt_contact,
                "receiverAddress": order.crm_order.receipt_address,
                "buyerMemo": "缺电话",
            },
        }
    )
    session.commit()

    with pytest.raises(RuntimeError, match="联系方式电话"):
        confirm_delivery_notice(session, order.delivery_notices[0], confirmed_by="tester")


def test_delivery_confirmation_blocks_when_receiver_address_is_coarse():
    session = make_session()
    order = create_delivery_ready_order(session)
    payload = loads(order.delivery_notices[0].payload_json, {})
    payload["orderInfo"]["receiverAddress"] = "北京"
    order.delivery_notices[0].payload_json = dumps(payload)
    session.commit()

    with pytest.raises(RuntimeError, match="可邮寄详细收货地址"):
        confirm_delivery_notice(session, order.delivery_notices[0], confirmed_by="tester")


def test_crm_sync_ignores_out_of_scope_order_without_queueing_middle_platform():
    session = make_session()
    result = upsert_crm_sales_orders(session, [valid_crm_order_row(crm_order_id="crm_obj_draft", crm_order_no="SO-DRAFT", approval_status="draft")])
    session.commit()

    assert result["ignored"] == 1
    assert result["queued_events"] == 0
    crm = session.query(CrmSalesOrder).filter_by(crm_order_id="crm_obj_draft").one()
    assert crm.scope_status == "Ignored"
    assert "approval_status_not_in_phase1_scope" in (crm.scope_ignore_reason or "")
    assert session.query(CrmOrderSnapshot).filter_by(crm_order_id="crm_obj_draft").count() == 1
    assert session.query(ProcessingJob).filter_by(job_type="CRM_ORDER_PARSED").count() == 0


def test_crm_change_after_delivery_preview_blocks_and_expires_preview():
    session = make_session()
    order = create_delivery_ready_order(session)
    old_hash = order.payload_hash

    result = upsert_crm_sales_orders(session, [valid_crm_order_row(order_amount="126000.00", product_amount="126000.00", receivable_amount="101000.00")])
    session.commit()

    assert result["updated"] == 1
    run_pending_jobs(session)

    session.refresh(order)
    assert order.payload_hash == old_hash
    assert order.status == OrderStatus.VALIDATION_BLOCKED.value
    assert order.delivery_notices[0].status == "Stale"
    case = session.query(ExceptionCase).one()
    assert case.exception_type == "CRM_CHANGED_BEFORE_OMS_PUSH"


def test_crm_cancel_before_oms_push_cancels_order_and_preview():
    session = make_session()
    order = create_delivery_ready_order(session)

    result = upsert_crm_sales_orders(session, [valid_crm_order_row(life_status="cancelled", approval_status="cancelled")])
    session.commit()

    assert result["updated"] == 1
    run_pending_jobs(session)

    session.refresh(order)
    assert order.status == OrderStatus.CANCELLED.value
    assert order.delivery_notices[0].status == "Cancelled"
    case = session.query(ExceptionCase).one()
    assert case.exception_type == "CRM_CANCELLED_BEFORE_OMS_PUSH"


def test_crm_change_after_oms_accepted_creates_high_risk_exception_without_auto_change():
    session = make_session()
    order = create_delivery_ready_order(session)
    confirm_delivery_notice(session, order.delivery_notices[0], confirmed_by="tester")
    session.commit()
    run_pending_jobs(session)
    session.refresh(order)
    assert order.status == OrderStatus.OMS_ACCEPTED.value

    result = upsert_crm_sales_orders(session, [valid_crm_order_row(order_amount="126000.00", product_amount="126000.00", receivable_amount="101000.00")])
    session.commit()

    assert result["updated"] == 1
    run_pending_jobs(session)

    session.refresh(order)
    assert order.status == OrderStatus.OMS_ACCEPTED.value
    assert order.delivery_notices[0].status == "Accepted"
    case = session.query(ExceptionCase).one()
    assert case.exception_type == "CRM_CHANGED_AFTER_OMS_ACCEPTED"


def test_crm_cancel_during_oms_pending_cancels_pending_push_job():
    session = make_session()
    order = create_delivery_ready_order(session)
    confirm_delivery_notice(session, order.delivery_notices[0], confirmed_by="tester")
    session.commit()
    push_job = session.query(ProcessingJob).filter_by(job_type="OMS_PUSH_NOTICE").one()
    assert push_job.status == "Pending"

    result = upsert_crm_sales_orders(session, [valid_crm_order_row(life_status="cancelled", approval_status="cancelled")])
    session.commit()

    assert result["updated"] == 1
    crm_order = session.query(CrmSalesOrder).filter_by(crm_order_id="crm_obj_001").one()
    process_crm_order_parsed_event(session, crm_order_parsed_event(crm_order))
    session.commit()

    session.refresh(order)
    session.refresh(push_job)
    assert order.status == OrderStatus.CANCELLED.value
    assert push_job.status == "Cancelled"
    assert order.delivery_notices[0].status == "Cancelled"


def test_validation_blocked_creates_context_pack_exception():
    session = make_session()
    crm = CrmSalesOrder(
        source_system="fxiaoke",
        crm_order_id="crm_obj_002",
        crm_order_no="SO-002",
        customer_name="缺 SKU 客户",
        sales_user_name="Alice",
        owner_department="商务一部",
        life_status="normal",
        approval_status="approved",
        order_date="2026-06-12",
        settlement_method="CNY",
        order_amount="100.00",
        product_amount="100.00",
        received_amount="0.00",
        receivable_amount="100.00",
        currency="CNY",
        receipt_contact="李四",
        receipt_phone="18600003333",
        receipt_address="上海市测试路 2 号",
        delivery_date="2026-06-25",
        attachment_files_json=dumps(["采购订单.pdf"]),
        payload_hash="hash-002",
        raw_json=dumps({"items": [{"sku_code": "UNKNOWN-SKU", "quantity": 1}]}),
    )
    session.add(crm)
    session.commit()

    payload = {
        "trace_id": "test-trace",
        "event_type": "CRM_ORDER_PARSED",
        "source_system": "FXIAOKE",
        "data": {
            "crm_sales_order_id": crm.id,
            "crm_order_id": crm.crm_order_id,
            "payload_hash": crm.payload_hash,
            "order_head": {"crm_order_no": crm.crm_order_no, "customer_name": crm.customer_name, "amount": 100.0, "currency": "CNY"},
            "order_items": [{"sku_code": "UNKNOWN-SKU", "quantity": 1}],
        },
    }
    result = process_crm_order_parsed_event(session, payload)
    session.commit()

    assert result["validation_passed"] is False
    order = session.query(MiddlePlatformOrder).one()
    assert order.status == OrderStatus.VALIDATION_BLOCKED.value
    detail = loads(session.query(ExceptionCase).one().detail, {})
    assert detail["context_type"] == "V2_ORDER_EXCEPTION"
    assert detail["order"]["order_no"] == order.order_no
    mail = session.query(OutboundMailJob).one()
    assert mail.mail_type == "V2ValidationFailed"
    assert "预审未通过" in mail.subject


def test_inventory_rule_blocks_when_available_quantity_is_short():
    session = make_session()
    seed_active_sku(session)
    session.add(
        ProductInventorySnapshot(
            material_code="SKU-3D-SCANNER-PRO",
            material_name="3D Scanner",
            warehouse_code="A1",
            warehouse_name="武汉工厂仓",
            base_qty=1,
            qty=1,
            source_payload_json=dumps({"canUseQuantity": 1}),
        )
    )
    session.commit()
    result = upsert_crm_sales_orders(
        session,
        [
            {
                "crm_order_id": "crm_obj_stock_short",
                "crm_order_no": "SO-STOCK-SHORT",
                "customer_name": "库存不足客户",
                "sales_user_name": "Alice",
                "owner_department": "商务一部",
                "life_status": "normal",
                "approval_status": "approved",
                "order_date": "2026-06-12",
                "order_amount": "200.00",
                "product_amount": "200.00",
                "received_amount": "0.00",
                "receivable_amount": "200.00",
                "settlement_method": "CNY",
                "receipt_contact": "王五",
                "receipt_phone": "18600004444",
                "receipt_address": "广东省深圳市南山区测试路 3 号 501 室",
                "delivery_date": "2026-06-28",
                "attachment_files": "采购订单.pdf",
                "items": [{"sku_code": "SKU-3D-SCANNER-PRO", "quantity": 2, "unit_price": "100", "line_amount": "200"}],
            }
        ],
    )
    session.commit()

    assert result["queued_events"] == 1
    run_pending_jobs(session)

    order = session.query(MiddlePlatformOrder).one()
    assert order.status == OrderStatus.VALIDATION_BLOCKED.value
    detail = loads(session.query(ExceptionCase).one().detail, {})
    assert detail["validation"]["failed_rules"][0]["rule_code"] == "LOCAL_INVENTORY_AVAILABLE"


def test_phase_one_missing_fields_interrupts_and_notifies_stakeholders():
    session = make_session()
    result = upsert_crm_sales_orders(
        session,
        [
            {
                "crm_order_id": "crm_obj_missing",
                "crm_order_no": "SO-MISSING",
                "customer_name": "字段缺失客户",
                "order_amount": "100.00",
                "settlement_method": "option1",
            }
        ],
    )
    session.commit()

    assert result["queued_events"] == 1
    run_pending_jobs(session)

    order = session.query(MiddlePlatformOrder).one()
    assert order.status == OrderStatus.VALIDATION_BLOCKED.value
    detail = loads(session.query(ExceptionCase).one().detail, {})
    failed = detail["validation"]["failed_rules"][0]
    assert failed["rule_code"] == "PHASE1_COMPLETE_PRE_REVIEW_FIELDS"
    assert "销售负责人" in failed["reason"]
    mail = session.query(OutboundMailJob).one()
    assert mail.mail_type == "V2ValidationFailed"
    assert "暂不会生成发货通知或下推 OMS" in mail.body
    assert "缺少或需修正的基础资料：" in mail.body
    assert "证据来源：" in mail.body


def test_customer_mapping_failure_interrupts_and_notifies_with_evidence_summary():
    session = make_session()
    seed_active_sku(session)
    seed_inventory(session, quantity=100)
    result = upsert_crm_sales_orders(session, [valid_crm_order_row(crm_order_id="crm_obj_unknown_customer", crm_order_no="SO-UNKNOWN-CUSTOMER", customer_name="未映射客户")])
    session.commit()

    assert result["queued_events"] == 1
    run_pending_jobs(session)

    order = session.query(MiddlePlatformOrder).one()
    assert order.status == OrderStatus.VALIDATION_BLOCKED.value
    detail = loads(session.query(ExceptionCase).one().detail, {})
    failed_rules = detail["validation"]["failed_rules"]
    assert any(rule["rule_code"] == "CUSTOMER_MAPPING" for rule in failed_rules)
    assert "客户资料/客户主数据映射" in detail["validation"]["missing_materials"]
    assert any("采购订单.pdf" in item for item in detail["validation"]["evidence_summary"])
    mail = session.query(OutboundMailJob).one()
    assert mail.mail_type == "V2ValidationFailed"
    assert "客户资料/客户主数据映射" in mail.body
    assert "附件：采购订单.pdf" in mail.body
    assert "CUSTOMER_MAPPING" in mail.body


def test_validation_exception_queues_and_writes_diagnosis():
    session = make_session()
    seed_active_sku(session)
    seed_inventory(session, quantity=100)
    upsert_crm_sales_orders(session, [valid_crm_order_row(crm_order_id="crm_obj_diag", crm_order_no="SO-DIAG", customer_name="未映射客户")])
    session.commit()

    first = run_pending_jobs(session)
    assert first["completed"] == 1
    case = session.query(ExceptionCase).one()
    diagnosis_job = session.query(ProcessingJob).filter_by(job_type="DIAGNOSE_EXCEPTION").one()
    assert diagnosis_job.status == "Pending"

    second = run_pending_jobs(session)
    assert second["completed"] == 1
    session.refresh(case)
    detail = loads(case.detail, {})
    diagnosis = detail["ai_diagnosis"]
    assert diagnosis["diagnosis_type"] == "RULE_BASED_AI_COMPATIBLE"
    assert diagnosis["suggested_owner"] == "商务/主数据维护人"
    assert any("客户" in item for item in diagnosis["root_causes"])
    assert session.query(ProcessingJob).filter_by(id=diagnosis_job.id).one().status == "Completed"


def test_illegal_state_transition_is_rejected():
    session = make_session()
    crm = CrmSalesOrder(
        source_system="fxiaoke",
        crm_order_id="crm_obj_003",
        crm_order_no="SO-003",
        customer_name="测试客户",
        payload_hash="hash-003",
    )
    session.add(crm)
    session.flush()
    order = MiddlePlatformOrder(
        order_no="MP-SO-003",
        source_system="fxiaoke",
        crm_sales_order_id=crm.id,
        crm_order_id=crm.crm_order_id,
        crm_order_no=crm.crm_order_no,
        payload_hash=crm.payload_hash,
        status=OrderStatus.IMPORTED.value,
    )
    session.add(order)
    session.flush()

    with pytest.raises(IllegalStateTransition):
        transition_order(session, order, OrderEvent.OMS_PUSH_SUCCESS)
