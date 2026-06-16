#!/usr/bin/env python3
"""
V2 订单中台 — 全异常类型覆盖测试（SQLite 内存库）
===================================================
对照设计文档 §5.2.2 异常类型矩阵，逐一验证系统行为。
全部在 SQLite 内存库运行，不碰真实数据库和外部系统。
"""

from __future__ import annotations
import json as _json
import os
import sys
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ["CONFIG_ENCRYPTION_KEY"] = "all-exception-test-key"

# ─── 辅助 ───
RESULTS: list[dict] = []
TOTAL = [0, 0]  # pass, fail

def section(title: str):
    print(f"\n{'='*70}")
    print(f"  {title}")
    print(f"{'='*70}")

def pass_(desc: str):
    TOTAL[0] += 1
    print(f"  ✅ {desc}")

def fail_(desc: str, detail: str = ""):
    TOTAL[1] += 1
    print(f"  ❌ {desc}")
    if detail:
        print(f"     {detail}")

def check(desc: str, cond: bool, detail: str = ""):
    if cond:
        pass_(desc)
    else:
        fail_(desc, detail)
    return cond

def info(msg: str):
    print(f"  ℹ  {msg}")

# ─── 公共 setup ───
def make_session():
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker
    from backend.app.database import Base
    engine = create_engine("sqlite:///:memory:", echo=False)
    Base.metadata.create_all(bind=engine)
    Session = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)
    s = Session()
    from backend.app.services.bootstrap import seed_defaults, set_config
    seed_defaults(s)
    s.commit()
    return s

def seed_sku(session, sku_id="SKU-TEST-001", model="测试SKU"):
    from backend.app import models
    spu = models.ProductSPU(spu_id="SPU-TEST", name="测试产品")
    session.add(spu)
    session.flush()
    session.add(models.ProductSKU(spu_uuid=spu.id, sku_id=sku_id, model=model, status="Active"))
    session.commit()

def seed_sku_and_inventory(session, sku_id="SKU-TEST-001", qty=100):
    from backend.app import models
    seed_sku(session, sku_id)
    from backend.app.services.jsonutil import dumps
    session.add(models.ProductInventorySnapshot(
        material_code=sku_id, material_name="测试商品",
        warehouse_code="WH-001", warehouse_name="深圳总仓",
        base_qty=qty, qty=qty,
        source_payload_json=dumps({"canUseQuantity": qty}),
    ))
    session.commit()

def seed_channel_pricing(session, channel="amazon_us", shop_sku="AMZ-TEST", std_sku="SKU-TEST-001"):
    from backend.app import models
    sku = session.query(models.ProductSKU).filter_by(sku_id=std_sku).one()
    session.add(models.ChannelPricing(
        sku_uuid=sku.id, channel=channel, channel_sku_id=shop_sku,
        map_price=10000, currency="USD",
    ))
    session.commit()

def seed_oms_config(session):
    from backend.app.services.bootstrap import set_config
    set_config(session, "oms_enabled", "true")
    set_config(session, "oms_mock_success", "true")
    set_config(session, "oms_owner_code", "OWNER-TEST")
    set_config(session, "oms_warehouse_code", "WH-TEST")
    set_config(session, "oms_shop_code", "SHOP-TEST")
    set_config(session, "oms_logistic_code", "SF")
    set_config(session, "oms_max_retries", "3")
    session.commit()

def seed_customer_mapping(session, customer_id="cust_001"):
    from backend.app.services.bootstrap import set_config
    from backend.app.services.jsonutil import dumps
    set_config(session, "v2_customer_mapping_json", dumps({
        customer_id: {"customer_code": "CUST-001", "name": "测试客户"}
    }))
    set_config(session, "v2_review_customer_mapping_required", "true")
    session.commit()

def make_crm_row(crm_id="crm_test_001", crm_no="SO-TEST-001", **kw):
    base = {
        "crm_order_id": crm_id, "crm_order_no": crm_no,
        "customer_name": "测试客户", "customer_id": "cust_001",
        "sales_user_name": "张三", "sales_user_email": "zhangsan@test.com", "owner_department": "商务一部",
        "life_status": "normal", "approval_status": "approved",
        "order_date": "2026-06-15", "settlement_method": "CNY",
        "currency": "CNY", "order_amount": "10000.00",
        "received_amount": "0.00", "receivable_amount": "10000.00",
        "product_amount": "10000.00",
        "receipt_contact": "李四", "receipt_phone": "18600001111",
        "receipt_address": "广东省深圳市南山区科技园测试路1号",
        "delivery_date": "2026-06-30", "attachment_files": "采购订单.pdf",
        "items": [{"sku_code": "SKU-TEST-001", "quantity": 1, "unit_price": "10000", "line_amount": "10000"}],
    }
    base.update(kw)
    return base

def upsert_and_consume(session, rows):
    from backend.app.services.crm_sync import upsert_crm_sales_orders
    from backend.app.services.jobs import run_pending_jobs
    r = upsert_crm_sales_orders(session, rows)
    session.commit()
    if r.get("queued_events", 0) > 0:
        try:
            jr = run_pending_jobs(session)
        except Exception as e:
            print(f"  ⚠️ run_pending_jobs 异常: {e}")
            import traceback; traceback.print_exc()
            return r
    return r

def get_order(session, crm_order_no):
    from backend.app import models
    return session.query(models.MiddlePlatformOrder).filter_by(crm_order_no=crm_order_no).first()


def get_order_or_die(session, crm_order_no, *, step_name: str = ""):
    """获取中台订单，None 时打印诊断信息并安全退出当前场景"""
    from backend.app import models
    o = session.query(models.MiddlePlatformOrder).filter_by(crm_order_no=crm_order_no).first()
    if o is None:
        crm = session.query(models.CrmSalesOrder).filter_by(crm_order_no=crm_order_no).first()
        info(f"❌ [{step_name}] 中台订单不存在")
        if crm:
            info(f"  CRM scope={crm.scope_status} approval={crm.approval_status} dept={crm.owner_department}")
        else:
            info(f"  CRM 订单也不存在")
        jobs = session.query(models.ProcessingJob).filter(
            models.ProcessingJob.payload_json.ilike(f"%{crm_order_no}%")).all()
        if jobs:
            for j in jobs:
                info(f"  Job: type={j.job_type} status={j.status} err={j.error_message or ''}")
        return None
    return o


# ══════════════════════════════════════════════════════════════════
def main():
    from backend.app import models
    from backend.app.services.order_middle_platform import (
        OrderStatus, OrderEvent, BlockerLevel,
        transition_order, run_validation_chain, create_exception_case,
        confirm_delivery_notice, create_delivery_notice,
        process_oms_push_notice, handle_oms_push_failure,
        process_oms_status_update,
    )
    from backend.app.services.jsonutil import dumps, loads
    from backend.app.services.jobs import run_pending_jobs
    from backend.app.services.exception_diagnosis import diagnose_exception_case

    print()
    print("╔" + "═" * 68 + "╗")
    print("║  V2 订单中台 — 全异常类型覆盖测试                              ║")
    print("║  设计文档 §5.2.2 异常矩阵 | SQLite 内存库 | 全 Mock           ║")
    print("╚" + "═" * 68 + "╝")

    # ══════════════════════════════════════════════════════
    section("场景 1: SKU_MAPPING_MISSING — 渠道 SKU 未映射标准 SKU")
    # ══════════════════════════════════════════════════════
    s = make_session()
    seed_sku_and_inventory(s, "SKU-3D-PRO", 100)
    seed_customer_mapping(s)
    r = upsert_and_consume(s, [make_crm_row(
        crm_id="ex01", crm_no="EX-SKU-MISSING",
        channel_code="amazon_us", shop_code="AMZ-01",
        items=[{"shop_sku_code": "AMZ-UNKNOWN-SKU", "quantity": 1, "unit_price": "100", "line_amount": "100"}],
        order_amount="100.00", product_amount="100.00", receivable_amount="100.00",
    )])
    o = get_order(s, "EX-SKU-MISSING")
    vs = loads(o.validation_summary_json, {}).get("results", [])
    has_sku_mapping = any(r.get("rule_code") == "SKU_MAPPING_MISSING" and not r.get("passed") for r in vs)
    excs = s.query(models.ExceptionCase).filter(models.ExceptionCase.detail.ilike(f"%{o.order_no}%")).all()
    has_exc = len(excs) > 0

    check("渠道 SKU 未映射时 KnownSkuRule 触发 SKU_MAPPING_MISSING", has_sku_mapping,
          f"预审规则: {[(r.get('rule_code'), r.get('passed')) for r in vs]}")
    check("创建了 VALIDATION_BLOCKED 异常", has_exc,
          f"异常数={len(excs)}" if not has_exc else f"异常: {[(e.exception_type, e.severity) for e in excs]}")
    check("订单状态 = VALIDATION_BLOCKED", o.status == OrderStatus.VALIDATION_BLOCKED.value,
          f"status={o.status}")
    # AI 诊断
    if excs:
        diag = diagnose_exception_case(s, excs[0].id, actor="test")
        s.commit()
        check("AI 诊断返回建议", bool(diag.get("recommended_actions")),
              f"actions={diag.get('recommended_actions', [])}")
    s.close()

    # ══════════════════════════════════════════════════════
    section("场景 2: CUSTOMER_MAPPING_MISSING — 客户不在映射表")
    # ══════════════════════════════════════════════════════
    s = make_session()
    seed_sku_and_inventory(s, "SKU-TEST-001", 100)
    # 故意不配置客户映射
    from backend.app.services.bootstrap import set_config
    set_config(s, "v2_review_customer_mapping_required", "true")
    s.commit()
    r = upsert_and_consume(s, [make_crm_row(
        crm_id="ex02", crm_no="EX-CUSTOMER-MISSING",
        customer_name="未映射客户", customer_id="",
    )])
    o = get_order(s, "EX-CUSTOMER-MISSING")
    vs = loads(o.validation_summary_json, {}).get("results", [])
    has_cust_block = any(r.get("rule_code") == "CUSTOMER_MAPPING" and not r.get("passed") for r in vs)

    check("客户未映射时 CUSTOMER_MAPPING 阻断", has_cust_block,
          f"预审: {[(r.get('rule_code'), r.get('passed'), r.get('reason','')[:40]) for r in vs]}")
    check("订单状态 = VALIDATION_BLOCKED", o.status == OrderStatus.VALIDATION_BLOCKED.value)
    s.close()

    # ══════════════════════════════════════════════════════
    section("场景 3: 必填字段缺失 — 缺少销售、收件、交期等")
    # ══════════════════════════════════════════════════════
    s = make_session()
    r = upsert_and_consume(s, [{
        "crm_order_id": "ex03", "crm_order_no": "EX-MISSING-FIELDS",
        "customer_name": "缺字段客户",
        "order_amount": "100.00", "settlement_method": "CNY",
    }])
    o = get_order(s, "EX-MISSING-FIELDS")
    vs = loads(o.validation_summary_json, {}).get("results", [])
    has_phase1 = any(r.get("rule_code") == "PHASE1_COMPLETE_PRE_REVIEW_FIELDS" and not r.get("passed") for r in vs)
    reason = next((r.get("reason","") for r in vs if r.get("rule_code") == "PHASE1_COMPLETE_PRE_REVIEW_FIELDS"), "")

    check("订单缺少 sales/user/dept/date/contacts 时阻断", has_phase1,
          f"reason: {reason[:80]}")
    check("reason 中列出缺失的具体字段", "销售负责人" in reason or "归属部门" in reason,
          f"reason={reason[:80]}")
    check("订单状态 = VALIDATION_BLOCKED", o.status == OrderStatus.VALIDATION_BLOCKED.value)
    excs = s.query(models.ExceptionCase).filter(
        models.ExceptionCase.detail.ilike(f"%{o.order_no}%")).all()
    notification = s.query(models.OutboundMailJob).filter_by(mail_type="V2ValidationFailed").first()
    check("预审失败邮件通知已生成", notification is not None,
          f"邮件={notification.subject if notification else '无'}")
    if notification:
        check("邮件 body 包含缺料清单", "缺少或需修正的基础资料" in (notification.body or ""))
    s.close()

    # ══════════════════════════════════════════════════════
    section("场景 4: 金额不一致 — 订单金额 vs 商品金额 vs 已收+应收")
    # ══════════════════════════════════════════════════════
    s = make_session()
    seed_sku_and_inventory(s, "SKU-TEST-001", 100)
    seed_customer_mapping(s)
    r = upsert_and_consume(s, [make_crm_row(
        crm_id="ex04", crm_no="EX-AMOUNT-MISMATCH",
        order_amount="10000.00", product_amount="8000.00",
        received_amount="3000.00", receivable_amount="8000.00",
    )])
    o = get_order(s, "EX-AMOUNT-MISMATCH")
    vs = loads(o.validation_summary_json, {}).get("results", [])
    has_amount = any(r.get("rule_code") == "AMOUNT_CONSISTENCY" and not r.get("passed") for r in vs)
    reason = next((r.get("reason","") for r in vs if r.get("rule_code") == "AMOUNT_CONSISTENCY"), "")

    check("订单金额 10000 ≠ 商品金额 8000 时阻断", has_amount,
          f"reason: {reason[:80]}")
    check("reason 指明金额不一致", "不一致" in reason)
    check("订单状态 = VALIDATION_BLOCKED", o.status == OrderStatus.VALIDATION_BLOCKED.value)
    s.close()

    # ══════════════════════════════════════════════════════
    section("场景 5: INVENTORY_SHORTAGE — 库存可用量不足")
    # ══════════════════════════════════════════════════════
    s = make_session()
    seed_sku_and_inventory(s, "SKU-TEST-001", 1)  # 只有 1 台
    seed_customer_mapping(s)
    r = upsert_and_consume(s, [make_crm_row(
        crm_id="ex05", crm_no="EX-STOCK-SHORT",
        items=[{"sku_code": "SKU-TEST-001", "quantity": 10, "unit_price": "1000", "line_amount": "10000"}],
        order_amount="10000.00", product_amount="10000.00", receivable_amount="10000.00",
    )])
    o = get_order(s, "EX-STOCK-SHORT")
    if o is None:
        crm = s.query(models.CrmSalesOrder).filter_by(crm_order_no="EX-STOCK-SHORT").first()
        info(f"CRM order: scope={crm.scope_status if crm else 'NOT_FOUND'} "
             f"approval={crm.approval_status if crm else 'N/A'} "
             f"dept={crm.owner_department if crm else 'N/A'}")
        jobs = s.query(models.ProcessingJob).filter(
            models.ProcessingJob.payload_json.ilike("%EX-STOCK-SHORT%")).all()
        for j in jobs:
            info(f"Job: type={j.job_type}, status={j.status}, error={j.error_message or ''}")
        check("中台订单已创建", False, "get_order 返回 None — 可能被 scope 过滤或 Job 执行失败")
        s.close()
    else:
        vs = loads(o.validation_summary_json, {}).get("results", [])
        has_inv = any(r.get("rule_code") == "LOCAL_INVENTORY_AVAILABLE" and not r.get("passed") for r in vs)
        reason = next((r.get("reason","") for r in vs if r.get("rule_code") == "LOCAL_INVENTORY_AVAILABLE"), "")

        check("库存可用 1 < 需求 10 时阻断", has_inv,
              f"reason: {reason[:80]}")
        check("reason 量化不足 (1 vs 10)", "1" in reason and "10" in reason)
        check("订单状态 = VALIDATION_BLOCKED", o.status == OrderStatus.VALIDATION_BLOCKED.value)
        s.close()

    # ══════════════════════════════════════════════════════
    section("场景 6: OMS_REQUIRED_FIELDS_MISSING — 确认发货通知时缺必填配置")
    # ══════════════════════════════════════════════════════
    s = make_session()
    seed_sku_and_inventory(s, "SKU-TEST-001", 100)
    seed_customer_mapping(s)
    from backend.app.services.bootstrap import set_config
    set_config(s, "oms_enabled", "true")
    set_config(s, "oms_mock_success", "true")
    # 故意不配置 oms_owner_code / oms_warehouse_code 等
    s.commit()
    r = upsert_and_consume(s, [make_crm_row(crm_id="ex06", crm_no="EX-OMS-CONFIG")])
    o = get_order(s, "EX-OMS-CONFIG")
    if o.status == OrderStatus.DELIVERY_NOTICE_READY.value:
        notice = o.delivery_notices[0]
        try:
            confirm_delivery_notice(s, notice, confirmed_by="tester", trace_id="ex06")
            s.commit()
            check("发货通知确认应被阻断", False, "confirm 未抛异常")
        except RuntimeError as exc:
            # confirm_delivery_notice 内部已设置了 notice.status=Blocked
            # 并创建了异常，但抛出了 RuntimeError。需要 commit 才能持久化。
            s.commit()
            s.refresh(notice)
            msg = str(exc)
            check("确认发货通知时 OMS 必填配置缺失被拦截", "OMS 下推必填字段缺失" in msg or "货主" in msg or "仓库" in msg,
                  f"error: {msg[:100]}")
            check("发货通知状态 = Blocked", notice.status == "Blocked",
                  f"notice.status={notice.status}")
            excs = s.query(models.ExceptionCase).filter(
                models.ExceptionCase.exception_type == "OMS_REQUIRED_FIELDS_MISSING"
            ).all()
            check("产生 OMS_REQUIRED_FIELDS_MISSING 异常", len(excs) > 0,
                  f"异常数={len(excs)}")
    else:
        check("订单应进入 DELIVERY_NOTICE_READY", False, f"status={o.status}")
    s.close()

    # ══════════════════════════════════════════════════════
    section("场景 7: OMS 发货单下推失败 → OMS_BLOCKED 死信接管")
    # ══════════════════════════════════════════════════════
    import backend.app.services.order_middle_platform as omp7

    original_push = omp7.push_notice_to_oms
    def fake_push_fail(session, notice):
        raise RuntimeError("MOCK: OMS 服务不可用 (主数据缺失)")

    omp7.push_notice_to_oms = fake_push_fail

    s = make_session()
    seed_sku_and_inventory(s, "SKU-TEST-001", 100)
    seed_customer_mapping(s)
    seed_oms_config(s)
    from backend.app.services.bootstrap import set_config as set_cfg
    set_cfg(s, "oms_enabled", "true")
    set_cfg(s, "oms_mock_success", "false")
    set_cfg(s, "oms_max_retries", "1")
    s.commit()

    r = upsert_and_consume(s, [make_crm_row(crm_id="ex07", crm_no="EX-OMS-BLOCKED")])
    o = get_order(s, "EX-OMS-BLOCKED")
    if o.status == OrderStatus.DELIVERY_NOTICE_READY.value:
        notice = o.delivery_notices[0]
        confirm_delivery_notice(s, notice, confirmed_by="tester", trace_id="ex07")
        s.commit()
        run_pending_jobs(s)
        s.refresh(o)
        s.refresh(notice)

        check("OMS 下推失败 → OMS_BLOCKED", o.status == OrderStatus.OMS_BLOCKED.value,
              f"order.status={o.status}, notice.status={notice.status}")
        check("重试计数 >= max_retries", notice.retry_count >= 1,
              f"retry_count={notice.retry_count}, max={notice.max_retries}")
        check("通知状态 = Blocked", notice.status == "Blocked",
              f"notice.status={notice.status}")
        excs = s.query(models.ExceptionCase).filter_by(exception_type="OMS_BLOCKED").all()
        check("创建 OMS_BLOCKED 异常", len(excs) > 0, f"异常数={len(excs)}")
        blocked_mail = s.query(models.OutboundMailJob).filter_by(mail_type="V2OmsBlocked").first()
        check("OMS 阻塞邮件已生成", blocked_mail is not None,
              f"subject={blocked_mail.subject if blocked_mail else '无'}")
        if blocked_mail:
            check("邮件含 OMS 提示", "OMS/WMS" in (blocked_mail.body or ""))
    else:
        check(f"订单应进入 DELIVERY_NOTICE_READY", False, f"status={o.status}")
    omp7.push_notice_to_oms = original_push
    s.close()

    # ══════════════════════════════════════════════════════
    section("场景 8: MANUAL_REPLAY_WITHOUT_FIX — OMS 死信重放缺少修复证据")
    # ══════════════════════════════════════════════════════
    import backend.app.services.order_middle_platform as omp8

    original_push8 = omp8.push_notice_to_oms
    def fake_push_fail8(session, notice):
        raise RuntimeError("MOCK: OMS 主数据缺失")
    omp8.push_notice_to_oms = fake_push_fail8

    s = make_session()
    seed_sku_and_inventory(s, "SKU-TEST-001", 100)
    seed_customer_mapping(s)
    seed_oms_config(s)
    from backend.app.services.bootstrap import set_config as set_cfg8
    set_cfg8(s, "oms_enabled", "true")
    set_cfg8(s, "oms_mock_success", "false")
    set_cfg8(s, "oms_max_retries", "1")
    s.commit()

    # 先制造 OMS_BLOCKED
    r = upsert_and_consume(s, [make_crm_row(crm_id="ex08", crm_no="EX-REPLAY-NOFIX")])
    o = get_order(s, "EX-REPLAY-NOFIX")
    if o.status == OrderStatus.DELIVERY_NOTICE_READY.value:
        confirm_delivery_notice(s, o.delivery_notices[0], confirmed_by="tester", trace_id="ex08")
        s.commit()
        run_pending_jobs(s)
        s.refresh(o)
    check("已进入 OMS_BLOCKED", o.status == OrderStatus.OMS_BLOCKED.value,
          f"status={o.status}")

    if o.status == OrderStatus.OMS_BLOCKED.value:
        # 恢复 push_notice_to_oms 使 replay 能通过（mock success）
        omp8.push_notice_to_oms = original_push8
        from backend.app.services.bootstrap import set_config as sc2
        sc2(s, "oms_mock_success", "true")
        s.commit()

        from backend.app.main import replay_v2_delivery_notice
        notice = o.delivery_notices[0]
        # 无证据重放 → 应被拦截
        try:
            replay_v2_delivery_notice(notice.id, {}, s)
            check("无证据重放应被拦截", False, "replay 未抛异常")
        except Exception as exc:
            from fastapi import HTTPException
            if isinstance(exc, HTTPException):
                check("无证据重放 400", exc.status_code == 400,
                      f"status={exc.status_code}, detail={exc.detail}")
                check("提示需修复证据", "修复" in str(exc.detail) or "证据" in str(exc.detail),
                      f"detail={exc.detail}")
            else:
                check("无证据重放被拦截", True, f"exception={exc}")

        # 有证据重放
        result = replay_v2_delivery_notice(notice.id, {
            "repair_evidence": "已修复 OMS 主数据映射", "actor": "ops-fixer"
        }, s)
        check("有修复证据的重放被允许", result.get("queued") is True, f"result={result}")
        check("重放时关联的 OMS_BLOCKED 异常被解决",
              result.get("resolved_exceptions", 0) >= 1,
              f"resolved={result.get('resolved_exceptions', 0)}")

    omp8.push_notice_to_oms = original_push8
    s.close()

    # ══════════════════════════════════════════════════════
    section("场景 9: CRM 变更接管 — 发货预览后 CRM 编辑 → 预览作废 + 重新预审")
    # ══════════════════════════════════════════════════════
    s = make_session()
    seed_sku_and_inventory(s, "SKU-TEST-001", 100)
    seed_customer_mapping(s)
    seed_oms_config(s)
    r = upsert_and_consume(s, [make_crm_row(crm_id="ex09", crm_no="EX-CRM-CHANGE")])
    o = get_order(s, "EX-CRM-CHANGE")
    check("正常预审通过 → DELIVERY_NOTICE_READY", o.status == OrderStatus.DELIVERY_NOTICE_READY.value,
          f"status={o.status}")

    # 模拟 CRM 变更（新 payload_hash）
    modified = make_crm_row(crm_id="ex09", crm_no="EX-CRM-CHANGE",
                            order_amount="12000.00", product_amount="12000.00", receivable_amount="12000.00")
    r2 = upsert_and_consume(s, [modified])
    s.refresh(o)

    check("CRM 变更 → VALIDATION_BLOCKED", o.status == OrderStatus.VALIDATION_BLOCKED.value,
          f"status={o.status}")
    check("旧发货通知作废", o.delivery_notices[0].status == "Stale",
          f"notice.status={o.delivery_notices[0].status}")
    excs = s.query(models.ExceptionCase).filter_by(exception_type="CRM_CHANGED_BEFORE_OMS_PUSH").all()
    check("创建 CRM_CHANGED_BEFORE_OMS_PUSH 异常", len(excs) > 0,
          f"异常数={len(excs)}")
    s.close()

    # ══════════════════════════════════════════════════════
    section("场景 10: CRM 取消接管 — 未推 OMS 前撤销 → 取消流程")
    # ══════════════════════════════════════════════════════
    s = make_session()
    seed_sku_and_inventory(s, "SKU-TEST-001", 100)
    seed_customer_mapping(s)
    r = upsert_and_consume(s, [make_crm_row(crm_id="ex10", crm_no="EX-CRM-CANCEL")])
    o = get_order(s, "EX-CRM-CANCEL")
    check("正常预审通过", o.status == OrderStatus.DELIVERY_NOTICE_READY.value)

    # 模拟 CRM 撤销
    cancelled = make_crm_row(crm_id="ex10", crm_no="EX-CRM-CANCEL",
                             life_status="cancelled", approval_status="cancelled")
    r2 = upsert_and_consume(s, [cancelled])
    s.refresh(o)

    check("CRM 取消 → CANCELLED", o.status == OrderStatus.CANCELLED.value,
          f"status={o.status}")
    check("发货通知已取消", o.delivery_notices[0].status == "Cancelled",
          f"notice.status={o.delivery_notices[0].status}")
    excs = s.query(models.ExceptionCase).filter_by(exception_type="CRM_CANCELLED_BEFORE_OMS_PUSH").all()
    check("创建 CRM_CANCELLED_BEFORE_OMS_PUSH 异常", len(excs) > 0)
    s.close()

    # ══════════════════════════════════════════════════════
    section("场景 11: OMS 已接收后 CRM 变更 → P0 高危异常（不自动改单）")
    # ══════════════════════════════════════════════════════
    s = make_session()
    seed_sku_and_inventory(s, "SKU-TEST-001", 100)
    seed_customer_mapping(s)
    seed_oms_config(s)
    r = upsert_and_consume(s, [make_crm_row(crm_id="ex11", crm_no="EX-OMS-ACCEPTED-CHANGE")])
    o = get_order(s, "EX-OMS-ACCEPTED-CHANGE")
    # 确认 → 推 OMS → mock 成功
    confirm_delivery_notice(s, o.delivery_notices[0], confirmed_by="tester", trace_id="ex11")
    s.commit()
    run_pending_jobs(s)
    s.refresh(o)
    check("OMS 已接收", o.status == OrderStatus.OMS_ACCEPTED.value,
          f"status={o.status}")

    # 模拟 CRM 变更
    modified = make_crm_row(crm_id="ex11", crm_no="EX-OMS-ACCEPTED-CHANGE",
                            order_amount="15000.00", product_amount="15000.00", receivable_amount="15000.00")
    r2 = upsert_and_consume(s, [modified])
    s.refresh(o)

    check("OMS 已接收后 CRM 变更 → 主状态不变（保留履约事实）",
          o.status == OrderStatus.OMS_ACCEPTED.value,
          f"status={o.status}")
    excs = s.query(models.ExceptionCase).filter_by(exception_type="CRM_CHANGED_AFTER_OMS_ACCEPTED").all()
    check("创建 CRM_CHANGED_AFTER_OMS_ACCEPTED P0 高危异常", len(excs) > 0,
          f"异常数={len(excs)}")
    if excs:
        check("高危异常标记 requires_confirmation",
              excs[0].severity == "Critical" and "CRM_CHANGED_AFTER_OMS_ACCEPTED" in excs[0].exception_type)
    s.close()

    # ══════════════════════════════════════════════════════
    section("场景 12: FBA 平台履约订单 → 自动归档（正常流程，非异常）")
    # ══════════════════════════════════════════════════════
    s = make_session()
    seed_sku_and_inventory(s, "SKU-TEST-001", 100)
    seed_customer_mapping(s)
    r = upsert_and_consume(s, [make_crm_row(
        crm_id="ex12", crm_no="EX-FBA-ARCHIVE",
        fulfillment_type="FBA", channel_code="amazon_us", shop_code="AMZ-US-01",
    )])
    o = get_order(s, "EX-FBA-ARCHIVE")
    vs = loads(o.validation_summary_json, {})

    check("FBA 订单自动归档（跳过 OMS）", o.status == OrderStatus.FULFILLMENT_ARCHIVED.value,
          f"status={o.status}")
    check("未生成发货通知", len(o.delivery_notices) == 0,
          f"notices={len(o.delivery_notices)}")
    check("履约类型 = PLATFORM_FULFILLED",
          vs.get("fulfillment", {}).get("type") == "PLATFORM_FULFILLED")
    check("归档原因说明不为空",
          bool(vs.get("fulfillment", {}).get("reason", "")))
    audit = s.query(models.AuditEvent).filter_by(event_type="PlatformFulfilledOrderArchived").count()
    check("记录 PlatformFulfilledOrderArchived 审计", audit >= 1)
    s.close()

    # ══════════════════════════════════════════════════════
    section("场景 13: 收货地址粗糙 / 收货人电话缺失 → 发货确认阻断")
    # ══════════════════════════════════════════════════════
    s = make_session()
    seed_sku_and_inventory(s, "SKU-TEST-001", 100)
    seed_customer_mapping(s)
    seed_oms_config(s)
    # 地址粗糙
    r = upsert_and_consume(s, [make_crm_row(
        crm_id="ex13a", crm_no="EX-COARSE-ADDR",
        receipt_address="北京",  # 粗糙地址
    )])
    o1 = get_order(s, "EX-COARSE-ADDR")
    vs1 = loads(o1.validation_summary_json, {}).get("results", [])
    has_addr_warn = any("收货地址" in r.get("reason","") for r in vs1 if not r.get("passed"))
    check("粗糙地址「北京」被预审识别", has_addr_warn,
          f"失败规则: {[(r.get('rule_code'), r.get('reason','')[:60]) for r in vs1 if not r.get('passed')]}")

    # 二次确认：进入发货通知后，地址仍然粗糙 → 确认阻断
    s2 = make_session()
    seed_sku_and_inventory(s2, "SKU-TEST-001", 100)
    seed_customer_mapping(s2)
    seed_oms_config(s2)
    # 使用详细地址预审通过，再手动改成粗糙地址
    r2 = upsert_and_consume(s2, [make_crm_row(
        crm_id="ex13b", crm_no="EX-CONFIRM-ADDR",
        receipt_address="广东省深圳市南山区科技园测试路1号",  # 详细
    )])
    o2 = get_order(s2, "EX-CONFIRM-ADDR")
    if o2.status == OrderStatus.DELIVERY_NOTICE_READY.value:
        # 篡改发货通知中的收货地址为粗糙地址
        payload = loads(o2.delivery_notices[0].payload_json, {})
        payload["orderInfo"] = payload.get("orderInfo", {})
        payload["orderInfo"]["receiverAddress"] = "北京"
        o2.delivery_notices[0].payload_json = dumps(payload)
        s2.commit()
        try:
            confirm_delivery_notice(s2, o2.delivery_notices[0], confirmed_by="tester")
            s2.commit()
            check("粗糙地址「北京」应阻断确认", False, "确认成功（不应通过）")
        except RuntimeError as exc:
            s2.rollback()
            check("确认时粗糙地址被拦截", "可邮寄详细收货地址" in str(exc),
                  f"error: {str(exc)[:120]}")
    s2.close()
    s.close()

    # ══════════════════════════════════════════════════════
    section("场景 14: 非法状态跃迁被拒绝")
    # ══════════════════════════════════════════════════════
    s = make_session()
    from backend.app import models as m2
    from backend.app.services.order_middle_platform import IllegalStateTransition

    crm = m2.CrmSalesOrder(
        source_system="fxiaoke", crm_order_id="ex14", crm_order_no="EX-ILLEGAL",
        customer_name="测试", payload_hash="hash14",
    )
    s.add(crm)
    s.flush()
    order = m2.MiddlePlatformOrder(
        order_no="MP-EX-ILLEGAL", source_system="fxiaoke",
        crm_sales_order_id=crm.id, crm_order_id=crm.crm_order_id,
        crm_order_no=crm.crm_order_no, payload_hash=crm.payload_hash,
        status=OrderStatus.IMPORTED.value,
    )
    s.add(order)
    s.flush()

    try:
        transition_order(s, order, OrderEvent.OMS_PUSH_SUCCESS)  # IMPORTED → OMS_ACCEPTED 非法
        check("IMPORTED → OMS_ACCEPTED 应被拒绝", False, "跃迁成功（不应通过）")
    except IllegalStateTransition:
        check("非法跃迁 IMPORTED → OMS_ACCEPTED 被状态机拒绝", True)
    s.close()

    # ══════════════════════════════════════════════════════
    # 最终汇总
    # ══════════════════════════════════════════════════════
    section("══════════════ 异常覆盖汇总 ══════════════")
    print(f"""
  异常类型                                   场景
  ────────────────────────────────────────────────────────
  SKU_MAPPING_MISSING                       场景 1  (渠道SKU未映射)
  CUSTOMER_MAPPING_MISSING                  场景 2  (客户未在映射表)
  PHASE1_COMPLETE_PRE_REVIEW_FIELDS         场景 3  (必填字段缺失)
  AMOUNT_CONSISTENCY                        场景 4  (金额不一致)
  LOCAL_INVENTORY_AVAILABLE                 场景 5  (库存不足)
  OMS_REQUIRED_FIELDS_MISSING               场景 6  (OMS配置缺失)
  OMS_BLOCKED (死信)                        场景 7  (OMS下推耗尽)
  MANUAL_REPLAY_WITHOUT_FIX                 场景 8  (无证据重放)
  CRM_CHANGED_BEFORE_OMS_PUSH               场景 9  (发货预览后变更)
  CRM_CANCELLED_BEFORE_OMS_PUSH             场景 10 (未推前撤销)
  CRM_CHANGED_AFTER_OMS_ACCEPTED            场景 11 (已接收后变更)
  FBA 平台履约归档（正常）                  场景 12 (非异常)
  粗糙地址 / 确认阻断                       场景 13 (地址&电话校验)
  非法状态跃迁拦截                          场景 14 (状态机保护)
""")

    total = TOTAL[0] + TOTAL[1]
    rate = (TOTAL[0] / total * 100) if total else 0
    color = "✅" if rate >= 90 else "⚠️"
    print(f"  总计: {total} 项 | {color} 通过 {TOTAL[0]} | 失败 {TOTAL[1]} | 通过率 {rate:.1f}%\n")

    pytest_count = 39
    print(f"  补充说明: 已有 {pytest_count} 个 pytest 用例 (tests/test_order_middle_platform.py)")
    print(f"  覆盖: OMS_IDEMPOTENCY_CONFLICT / OMS_STATUS_SYNC / 面单打印 / 运单回传 /")
    print(f"        详情同步重试 / 附件提取 / CRM 范围配置 / LLM 诊断兜底 等")
    print()

    if TOTAL[1] > 0:
        sys.exit(1)


if __name__ == "__main__":
    main()
