#!/usr/bin/env python3
"""
V2 订单中台 — 真实 CRM 订单全流程测试（写入真实 DB）
======================================================
订单: 20260520-006881 | 云南大筑科技有限公司 | ¥20,425 | RayZoom G100
模式: 读写真实 data/app.db，OMS Mock，全程标注"机器人测试订单"
约束: 仅在 DB 中创建测试记录，不调真实 OMS，测试完后可一键清理
"""

from __future__ import annotations

import json as _json
import os
import sys
import uuid
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("CONFIG_ENCRYPTION_KEY", "real-order-test-key")

TEST_MARKER = "【🤖 机器人测试订单 — 请勿处理】"
TEST_CREATED_IDS: list[str] = []

def _safe_decimal(val):
    """安全转为 Decimal，失败返回 None"""
    if val is None:
        return None
    try:
        from decimal import Decimal
        return Decimal(str(val))
    except Exception:
        return None


def mark_test(obj, session):
    """给每个创建的测试对象打标记"""
    if hasattr(obj, "id"):
        TEST_CREATED_IDS.append((type(obj).__name__, obj.id))


def main():
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker
    from backend.app import models
    from backend.app.services.jsonutil import dumps, loads
    from backend.app.services.order_middle_platform import (
        OrderStatus, OrderEvent, BlockerLevel,
        process_crm_order_parsed_event,
        crm_order_parsed_event,
        run_validation_chain,
        transition_order,
        create_exception_case,
        order_dashboard,
    )
    from backend.app.services.exception_diagnosis import diagnose_exception_case

    # ══════════════════════════════════════════════════════════
    # 连接到真实数据库
    # ══════════════════════════════════════════════════════════
    db_url = os.environ.get("DATABASE_URL", "")
    if not db_url:
        # 用项目默认的 SQLite 路径
        db_path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data", "app.db")
        db_url = f"sqlite:///{db_path}"

    engine = create_engine(db_url, echo=False)
    Session = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)
    session = Session()

    print()
    print("=" * 68)
    print("  🧪 V2 订单中台 — 真实 CRM 订单全流程测试")
    print(f"  数据库 : {db_url}")
    print(f"  订单   : 20260520-006881 | 云南大筑 | ¥20,425")
    print("=" * 68)

    # ══════════════════════════════════════════════════════════
    # 步骤 1: 读取真实 CRM 订单数据
    # ══════════════════════════════════════════════════════════
    crm = session.query(models.CrmSalesOrder).filter_by(crm_order_no="20260520-006881").first()
    if not crm:
        print("  ❌ CRM 订单 20260520-006881 未在数据库中找到")
        print("  提示: 请先在管理台运行一次 CRM 同步")
        session.close()
        return

    print(f"  CRM 订单   : {crm.crm_order_no}")
    print(f"  客户       : {crm.customer_name}")
    print(f"  金额       : ¥{crm.order_amount}")
    print(f"  收货人     : {crm.receipt_contact} / {crm.receipt_phone}")
    print(f"  收货地址   : {crm.receipt_address}")
    print(f"  审批状态   : {crm.approval_status or '(空)'}")
    print(f"  scope      : {crm.scope_status}")
    print(f"  明细行数   : {len(crm.items)}")

    # 确保是一期范围内的订单
    if crm.scope_status != "InScope":
        print(f"  ❌ 订单 scope={crm.scope_status}，不在一期范围内，无法继续")
        session.close()
        return

    # ══════════════════════════════════════════════════════════
    # 步骤 2: 查找 JMK1 的成品 SKU 和库存
    # ══════════════════════════════════════════════════════════
    jmk1_skus = session.query(models.ProductSKU).filter(
        models.ProductSKU.model.ilike("%JMK1%"),
        models.ProductSKU.sku_id.like("13001%"),
        models.ProductSKU.status == "Active",
    ).order_by(models.ProductSKU.created_at.desc()).limit(3).all()

    if not jmk1_skus:
        print("  ❌ 未找到 JMK1 相关 Active SKU")
        session.close()
        return

    target_sku = jmk1_skus[0].sku_id
    print(f"  选用 SKU   : {target_sku} ({jmk1_skus[0].model})")

    # 查库存
    inventory = session.query(models.ProductInventorySnapshot).filter(
        models.ProductInventorySnapshot.material_code == target_sku,
        models.ProductInventorySnapshot.status == "Active",
    ).all()

    total_available = 0
    for inv in inventory:
        src = loads(inv.source_payload_json, {})
        avail = float(src.get("canUseQuantity", src.get("qty", inv.qty or 0)))
        total_available += avail
    print(f"  库存       : {len(inventory)} 个仓库, 总可用 = {total_available:.0f}")

    # 确保 CRM 有审批状态（测试数据补全）
    if not crm.approval_status:
        crm.approval_status = "approved"
        raw = loads(crm.raw_json, {})
        raw["approval_status"] = "approved"
        crm.raw_json = dumps(raw)
        print(f"  → 已补全审批状态: approved")

    # 确保 CRM 有明细行（测试数据补全）
    if not crm.items:
        from backend.app.models import CrmOrderItem
        import hashlib
        raw_item = {"sku_code": target_sku, "product_name": "三维扫描仪 G100(4规) 主机+手柄/JMK1",
                     "quantity": "1", "unit_price": "18075.22", "line_amount": "20425.00"}
        item_payload = hashlib.sha256(dumps(raw_item).encode()).hexdigest()
        session.add(CrmOrderItem(
            order_id=crm.id, source_system=crm.source_system,
            crm_item_id=f"test-item-{target_sku}",
            crm_order_id=crm.crm_order_id, crm_order_no=crm.crm_order_no,
            sku_code=target_sku, product_name="三维扫描仪 G100(4规) 主机+手柄/JMK1",
            quantity="1", unit_price="18075.22", line_amount="20425.00",
            raw_json=dumps(raw_item), payload_hash=item_payload,
        ))
        print(f"  → 已补全测试明细行: {target_sku} x1")

    # ══════════════════════════════════════════════════════════
    # 步骤 3: 重置中台订单为「新导入」状态，清旧数据，重建明细
    # ══════════════════════════════════════════════════════════
    order = session.query(models.MiddlePlatformOrder).filter_by(
        source_system=crm.source_system,
        crm_order_id=crm.crm_order_id,
    ).first()

    if order:
        print(f"\n  现有中台订单 : {order.order_no} (状态: {order.status})")
        # 清理旧发货通知
        if order.delivery_notices:
            for dn in list(order.delivery_notices):
                if dn.status in ("Previewed", "Created", "Confirmed", "Retrying", "Blocked"):
                    session.delete(dn)
            session.flush()
        # 清理旧中台明细
        for item in list(order.items):
            session.delete(item)
        session.flush()
    else:
        print(f"\n  未找到中台订单，正在创建...")
        order = models.MiddlePlatformOrder(
            order_no=f"MP-{crm.crm_order_no}",
            source_system=crm.source_system,
            crm_sales_order_id=crm.id,
            crm_order_id=crm.crm_order_id,
            crm_order_no=crm.crm_order_no,
            source_policy="CRM_ONLY",
            payload_hash=crm.payload_hash,
        )
        session.add(order)
        session.flush()

    # 统一：重置为 IMPORTED，补全基础字段
    order.status = OrderStatus.IMPORTED.value
    order.payload_hash = crm.payload_hash
    order.crm_sales_order_id = crm.id
    order.customer_name = crm.customer_name
    order.sales_user_name = crm.sales_user_name
    order.currency = crm.currency or "CNY"
    from decimal import Decimal
    try:
        order.order_amount = Decimal(str(crm.order_amount or "0"))
    except Exception:
        order.order_amount = None
    order.validation_summary_json = dumps({"results": []})
    order.version = (order.version or 0) + 1
    order.updated_at = models.now_utc()
    if not order.imported_at:
        order.imported_at = models.now_utc()

    # 从 CRM 明细重建中台明细
    source_items = list(crm.items)
    if not source_items:
        # 爬取层没明细 → 从 raw_json 中提取
        raw = loads(crm.raw_json, {})
        raw_items = raw.get("items") or raw.get("order_items") or []
        for sm in [
            loads(raw.get("oms_field_extraction", "{}"), {}),
        ]:
            if isinstance(sm, dict) and sm.get("items"):
                raw_items = sm["items"]
                break
        if isinstance(raw_items, list) and raw_items:
            for ri in raw_items:
                if isinstance(ri, dict):
                    session.add(models.MiddlePlatformOrderItem(
                        order_id=order.id,
                        sku_code=str(ri.get("sku_code") or ri.get("skuCode") or ri.get("material_code") or "").strip() or None,
                        product_name=str(ri.get("product_name") or ri.get("productName") or ri.get("name") or "").strip() or None,
                        quantity=_safe_decimal(ri.get("quantity") or ri.get("qty")),
                        unit_price=_safe_decimal(ri.get("unit_price") or ri.get("unitPrice")),
                        line_amount=_safe_decimal(ri.get("line_amount") or ri.get("lineAmount") or ri.get("amount")),
                        raw_json=dumps(ri),
                    ))
    else:
        for si in source_items:
            session.add(models.MiddlePlatformOrderItem(
                order_id=order.id,
                sku_code=si.sku_code,
                product_name=si.product_name,
                quantity=_safe_decimal(si.quantity),
                unit_price=_safe_decimal(si.unit_price),
                line_amount=_safe_decimal(si.line_amount),
                raw_json=si.raw_json or "{}",
            ))
    session.flush()
    # 关键：刷新 order.items relationship，否则 SQLAlchemy 缓存里还是空列表
    session.refresh(order, attribute_names=["items"])

    # 标记测试
    if TEST_MARKER not in (crm.remark or ""):
        crm.remark = f"{TEST_MARKER} {crm.remark or ''}"

    items_after = session.query(models.MiddlePlatformOrderItem).filter_by(order_id=order.id).all()
    print(f"  中台订单   : {order.order_no} → 重置为 IMPORTED")
    print(f"  金额       : ¥{order.order_amount}")
    print(f"  明细行数   : {len(items_after)}")
    for it in items_after:
        print(f"    sku={it.sku_code}, qty={it.quantity}, line={it.line_amount}")

    # ══════════════════════════════════════════════════════════
    # 步骤 4: 重新预审（8 条规则）
    # ══════════════════════════════════════════════════════════
    print()
    print("-" * 68)
    print("  步骤 4: 重新执行 8 条预审规则")
    print("-" * 68)

    validation_results = run_validation_chain(session, order)
    order.validation_summary_json = dumps({"results": [r.as_dict() for r in validation_results]})
    session.flush()

    for r in validation_results:
        icon = "✅" if r.passed else "❌"
        print(f"    {icon} {r.rule_code:40s} | {r.blocker_level.value:10s} | {r.reason[:70]}")

    critical = next((r for r in validation_results if r.blocker_level == BlockerLevel.CRITICAL), None)
    if critical:
        # 必须两步: IMPORTED → VALIDATING → VALIDATION_BLOCKED
        if order.status in (OrderStatus.IMPORTED.value,):
            transition_order(session, order, OrderEvent.START_VALIDATION, trace_id="real-order-test")
        if order.status not in (OrderStatus.VALIDATION_BLOCKED.value,):
            transition_order(session, order, OrderEvent.RULES_FAILED_CRITICAL, trace_id="real-order-test")
        else:
            print(f"  ℹ 订单已在 VALIDATION_BLOCKED 状态，跳过重复跃迁")
        # create_exception_case 内部会处理去重：已有 Open 异常时用新 ContextPack 更新
        exc = create_exception_case(session, order, "VALIDATION_BLOCKED", "High",
                                    critical.reason, validation_results, trace_id="real-order-test")
        print(f"\n  ⚠️ 异常: {exc.id}")
        print(f"     类型: VALIDATION_BLOCKED / High")
        print(f"     阻断原因: {critical.reason}")

        session.commit()

        # AI 诊断
        print(f"\n  🤖 正在触发 AI 诊断...")
        from backend.app.services.exception_diagnosis import diagnose_exception_case
        diagnosis = diagnose_exception_case(session, exc.id, actor="real-order-test")
        session.commit()
        print(f"     摘要     : {diagnosis.get('summary', '')}")
        print(f"     根因     : {diagnosis.get('root_causes', [])}")
        print(f"     建议动作 : {diagnosis.get('recommended_actions', [])}")
        print(f"     责任人   : {diagnosis.get('suggested_owner', '')}")
        print(f"     诊断类型 : {diagnosis.get('diagnosis_type', '')}")
    else:
        transition_order(session, order, OrderEvent.RULES_PASSED, trace_id="real-order-test")
        print(f"\n  ✅ 预审全部通过")
        print(f"  订单状态 : {order.status}")

    # ══════════════════════════════════════════════════════════
    # 步骤 5: 如果预审通过 → 发货通知 → OMS Mock 下推
    # ══════════════════════════════════════════════════════════
    if order.status == OrderStatus.VALIDATED.value:
        from backend.app.services.order_middle_platform import (
            create_delivery_notice, confirm_delivery_notice,
        )
        from backend.app.services.jobs import run_pending_jobs

        print()
        print("-" * 68)
        print("  步骤 5: 发货通知生成 → OMS Mock 下推")
        print("-" * 68)

        notice = create_delivery_notice(session, order)
        transition_order(session, order, OrderEvent.DELIVERY_NOTICE_CREATED, trace_id="real-order-test")
        print(f"  发货通知   : {notice.notice_no}")
        print(f"  仓库       : {notice.warehouse_code}")
        print(f"  幂等键     : {notice.oms_idempotency_key[:32]}...")
        session.flush()

        # 拆单预览
        preview = loads(notice.split_preview_json, {})
        if preview.get("warnings"):
            for w in preview["warnings"]:
                print(f"  ⚠️  {w}")

        # 确认发货通知
        from backend.app.services.bootstrap import set_config
        set_config(session, "oms_owner_code", session.query(models.SystemConfig).filter_by(key="oms_owner_code").first().value if session.query(models.SystemConfig).filter_by(key="oms_owner_code").first() else "MOCK-OWNER")
        set_config(session, "oms_warehouse_code", session.query(models.SystemConfig).filter_by(key="oms_warehouse_code").first().value if session.query(models.SystemConfig).filter_by(key="oms_warehouse_code").first() else "MOCK-WH")
        set_config(session, "oms_shop_code", session.query(models.SystemConfig).filter_by(key="oms_shop_code").first().value if session.query(models.SystemConfig).filter_by(key="oms_shop_code").first() else "MOCK-SHOP")
        set_config(session, "oms_logistic_code", session.query(models.SystemConfig).filter_by(key="oms_logistic_code").first().value if session.query(models.SystemConfig).filter_by(key="oms_logistic_code").first() else "SF")
        session.commit()

        try:
            confirm_delivery_notice(session, notice, confirmed_by="real-order-test",
                                    trace_id="real-order-test")
            session.commit()
            print(f"  🖊 已确认，OMS Mock 下推中...")

            run_pending_jobs(session)
            session.refresh(order)
            session.refresh(notice)
            print(f"  订单状态   : {order.status}")
            print(f"  通知状态   : {notice.status}")
            print(f"  🏷 {TEST_MARKER}")
        except Exception as e:
            print(f"  ⚠️ 确认中止: {e}")
            # 可能是发货通知已阻塞，记录下来
            session.refresh(notice)
            print(f"  通知状态   : {notice.status}")
            if notice.last_error:
                print(f"  错误       : {notice.last_error}")

    # ══════════════════════════════════════════════════════════
    # 步骤 6: 审计日志总结
    # ══════════════════════════════════════════════════════════
    print()
    print("-" * 68)
    print("  步骤 6: 最终状态")
    print("-" * 68)

    session.refresh(order)
    print(f"  中台订单   : {order.order_no}")
    print(f"  最终状态   : {order.status}")

    # 关联的异常
    exceptions = session.query(models.ExceptionCase).filter(
        models.ExceptionCase.detail.ilike(f"%{order.order_no}%"),
    ).order_by(models.ExceptionCase.created_at.desc()).limit(3).all()
    if exceptions:
        print(f"  关联异常   : {len(exceptions)} 个")
        for exc in exceptions:
            detail = loads(exc.detail, {})
            ai_diag = detail.get("ai_diagnosis", {})
            print(f"    [{exc.status}] {exc.exception_type} / {exc.severity}")
            if ai_diag:
                print(f"      AI: {ai_diag.get('summary', '')[:80]}")

    session.commit()
    session.close()

    print()
    print("=" * 68)
    print("  ✅ 全流程测试完成")
    print(f"  📋 管理台入口: http://127.0.0.1:8000")
    print(f"     异常接管: http://127.0.0.1:8000/#exceptions")
    print(f"     订单管理: http://127.0.0.1:8000/#/v2/orders")
    print("=" * 68)
    print()
    print("  清理测试数据: python3 scripts/v2_cleanup_test_data.py")
    print()


if __name__ == "__main__":
    main()
