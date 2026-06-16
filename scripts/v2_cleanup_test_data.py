#!/usr/bin/env python3
"""
清理 V2 真实订单测试产生的数据
"""
from __future__ import annotations
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("CONFIG_ENCRYPTION_KEY", "cleanup-key")

from backend.app import models
from backend.app.config import settings as _settings
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

TEST_MARKER = "🤖 机器人测试订单"

def main():
    db_url = os.environ.get("DATABASE_URL", "")
    if not db_url:
        db_path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data", "app.db")
        db_url = f"sqlite:///{db_path}"

    engine = create_engine(db_url)
    Session = sessionmaker(bind=engine)
    session = Session()

    print()
    print(f"数据库: {db_url}")
    print(f"正在查找包含「{TEST_MARKER}」标记的测试数据...")
    print()

    # CRM 订单 remark
    crm_orders = session.query(models.CrmSalesOrder).filter(
        models.CrmSalesOrder.remark.ilike(f"%{TEST_MARKER}%")
    ).all()
    if crm_orders:
        print(f"  CRM 订单 ({len(crm_orders)}):")
        for o in crm_orders:
            print(f"    {o.crm_order_no} | {o.customer_name} | remark={o.remark[:60]}")

    # 中台订单
    mid_orders = session.query(models.MiddlePlatformOrder).filter(
        models.MiddlePlatformOrder.crm_order_no == "20260520-006881"
    ).all()
    if mid_orders:
        print(f"  中台订单 ({len(mid_orders)}):")
        for o in mid_orders:
            notices = session.query(models.DeliveryNotice).filter_by(order_id=o.id).all()
            print(f"    {o.order_no} | status={o.status} | notices={len(notices)}")

    # 异常任务（关联中台订单）
    exceptions = session.query(models.ExceptionCase).filter(
        models.ExceptionCase.detail.ilike(f"%MP-20260520-006881%")
    ).all()
    if exceptions:
        print(f"  异常任务 ({len(exceptions)}):")
        for e in exceptions:
            print(f"    {e.id[:12]}... | {e.exception_type} | {e.severity} | {e.status}")

    # 审计事件
    audit_count = session.query(models.AuditEvent).filter(
        models.AuditEvent.detail.ilike("%real-order-test%")
    ).count()
    if audit_count:
        print(f"  审计事件 : {audit_count} 条")

    if not (crm_orders or mid_orders or exceptions):
        print("  未找到测试数据，无需清理。")
        session.close()
        return

    print()
    resp = input("确认删除以上数据？输入 yes 确认: ").strip()
    if resp.lower() != "yes":
        print("已取消。")
        session.close()
        return

    total = 0

    # 删发货通知
    for o in mid_orders:
        for dn in session.query(models.DeliveryNotice).filter_by(order_id=o.id).all():
            session.delete(dn)
            total += 1

    # 删中台订单明细
    if mid_orders:
        item_count = session.query(models.MiddlePlatformOrderItem).filter(
            models.MiddlePlatformOrderItem.order_id.in_([o.id for o in mid_orders])
        ).delete(synchronize_session=False)
        total += item_count

    # 删中台订单
    for o in mid_orders:
        session.delete(o)
        total += 1

    # 删异常
    for e in exceptions:
        session.delete(e)
        total += 1

    # 删审计事件
    deleted_audit = session.query(models.AuditEvent).filter(
        models.AuditEvent.detail.ilike("%real-order-test%")
    ).delete(synchronize_session=False)
    total += deleted_audit

    # 恢复 CRM remark
    for o in crm_orders:
        if o.remark:
            o.remark = o.remark.replace(f"{TEST_MARKER} ", "").replace(TEST_MARKER, "")
            total += 1

    # 删除相关 IntegrationEvent
    ie_count = session.query(models.IntegrationEvent).filter(
        models.IntegrationEvent.biz_key == "20260520-006881"
    ).delete(synchronize_session=False)
    total += ie_count

    # 删除相关 ProcessingJob
    pj_count = session.query(models.ProcessingJob).filter(
        models.ProcessingJob.payload_json.ilike("%20260520-006881%"),
        models.ProcessingJob.payload_json.ilike("%real-order-test%"),
    ).delete(synchronize_session=False)
    total += pj_count

    session.commit()
    print(f"\n  ✅ 已清理 {total} 条测试记录。")
    session.close()


if __name__ == "__main__":
    main()
