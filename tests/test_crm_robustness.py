"""CRM 鲁棒性优化集成测试

测试范围：
1. crm_order_summary 聚合查询优化（P0）
2. order_dashboard 缓存机制（P0）
3. _apply_db_query_timeout 超时保护（P0）
4. upsert savepoint 隔离回滚（P1）
5. _save_sync_run_failure 异常恢复（P1）
"""

from __future__ import annotations

import json
import time

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker

from backend.app.database import Base
from backend.app.models import CrmSalesOrder, CrmSyncRun, MiddlePlatformOrder, ExceptionCase, SystemConfig
from backend.app.services.bootstrap import seed_defaults, set_config
from backend.app.services.jsonutil import dumps, loads
from backend.app.services.crm_sync import (
    crm_order_summary,
    upsert_crm_sales_orders,
    _save_sync_run_failure,
    logger as crm_logger,
)
from backend.app.services.order_middle_platform import order_dashboard


# ── 测试 fixture ──

@pytest.fixture
def session():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(bind=engine)
    Session = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)
    s = Session()
    seed_defaults(s)
    s.commit()
    yield s
    s.close()


# ─────────────────────────────────────────────────
# 测试 1: crm_order_summary 聚合查询优化
# ─────────────────────────────────────────────────

def _make_crm_row(crm_order_id: str, crm_order_no: str, order_amount: str, received_amount: str, receivable_amount: str) -> dict:
    return {
        "crm_order_id": crm_order_id,
        "crm_order_no": crm_order_no,
        "customer_name": "测试客户",
        "customer_id": f"cust-{crm_order_id}",
        "approval_status": "Approved",
        "life_status": "Normal",
        "order_date": "2026-06-01",
        "settlement_method": "Credit",
        "order_amount": order_amount,
        "received_amount": received_amount,
        "receivable_amount": receivable_amount,
        "invoice_amount": "0",
        "product_amount": "0",
        "sales_user_name": "张三",
        "owner_department": "销售部",
        "receipt_contact": "李四",
        "receipt_phone": "13800138000",
        "receipt_address": "深圳市南山区科技园",
        "delivery_date": "2026-06-15",
        "remark": "",
        "attachments": [],
        "order_items": [],
    }


class TestCrmOrderSummary:
    """crm_order_summary 聚合查询鲁棒性测试"""

    def test_empty_database_returns_zero(self, session):
        """空数据库应返回 0 而非报错"""
        result = crm_order_summary(session)
        assert result["total"] == 0
        assert result["total_orders"] == 0
        assert result["total_order_amount"] == 0.0
        assert result["total_received_amount"] == 0.0
        assert result["total_receivable_amount"] == 0.0
        assert result["last_run"] is None
        assert result["pending_job"] is None

    def test_single_row_correctly_summed(self, session):
        """单行数据金额正确"""
        rows = [_make_crm_row("CRM001", "NO001", "1000.50", "500.00", "500.50")]
        upsert_crm_sales_orders(session, rows)
        result = crm_order_summary(session)
        assert result["total"] == 1
        assert result["total_order_amount"] == 1000.50
        assert result["total_received_amount"] == 500.00
        assert result["total_receivable_amount"] == 500.50

    def test_multiple_rows_correctly_summed(self, session):
        """多行数据金额累加正确"""
        rows = [
            _make_crm_row("CRM001", "NO001", "1000.50", "500.00", "500.50"),
            _make_crm_row("CRM002", "NO002", "2000.00", "1000.00", "1000.00"),
            _make_crm_row("CRM003", "NO003", "3500.75", "1500.25", "2000.50"),
        ]
        upsert_crm_sales_orders(session, rows)
        result = crm_order_summary(session)
        assert result["total"] == 3
        assert result["total_order_amount"] == 1000.50 + 2000.00 + 3500.75  # 6501.25
        assert result["total_received_amount"] == 500.00 + 1000.00 + 1500.25  # 3000.25
        assert result["total_receivable_amount"] == 500.50 + 1000.00 + 2000.50  # 3501.00

    def test_amount_with_commas_handled(self, session):
        """含千分位逗号的金额字段正确解析"""
        rows = [_make_crm_row("CRM001", "NO001", "1,000.50", "500.00", "500.50")]
        upsert_crm_sales_orders(session, rows)
        result = crm_order_summary(session)
        assert result["total_order_amount"] == 1000.50

    def test_null_amounts_handled(self, session):
        """NULL 金额不报错"""
        rows = [_make_crm_row("CRM001", "NO001", "", "", "")]
        upsert_crm_sales_orders(session, rows)
        result = crm_order_summary(session)
        assert result["total_order_amount"] == 0.0
        assert result["total_received_amount"] == 0.0
        assert result["total_receivable_amount"] == 0.0

    def test_mixed_amounts_handled(self, session):
        """混合 NULL/空/有效金额不报错"""
        rows = [
            _make_crm_row("CRM001", "NO001", "1000.50", "", "500.50"),
            _make_crm_row("CRM002", "NO002", "", "1000.00", ""),
            _make_crm_row("CRM003", "NO003", "3,500.75", "1,500.25", "2,000.50"),
        ]
        upsert_crm_sales_orders(session, rows)
        result = crm_order_summary(session)
        assert result["total"] == 3
        assert result["total_order_amount"] == 1000.50 + 3500.75  # 4501.25
        assert result["total_received_amount"] == 1000.00 + 1500.25  # 2500.25
        assert result["total_receivable_amount"] == 500.50 + 2000.50  # 2501.00

    def test_latest_run_is_populated(self, session):
        """有 sync_run 记录时 last_run 应返回（非 None）"""
        run = CrmSyncRun(source_system="fxiaoke", sync_type="sales_orders", status="Running", trigger="manual")
        session.add(run)
        session.commit()
        result = crm_order_summary(session)
        assert result["last_run"] is not None
        assert result["last_run"]["status"] == "Running"
        assert result["latest_run"]["status"] == "Running"

    def test_pending_job_detected(self, session):
        """有 Pending 的 ProcessingJob 时返回"""
        from backend.app.models import ProcessingJob
        job = ProcessingJob(job_type="sync_crm_sales_orders", status="Pending", payload_json="{}")
        session.add(job)
        session.commit()
        result = crm_order_summary(session)
        assert result["pending_job"] is not None
        assert result["pending_job"]["status"] == "Pending"


# ─────────────────────────────────────────────────
# 测试 2: order_dashboard 缓存机制
# ─────────────────────────────────────────────────

class TestOrderDashboardCache:
    """order_dashboard 30 秒 TTL 缓存测试"""

    def test_cache_hits_within_ttl(self, session):
        """30 秒内连续调用应命中缓存，结果一致"""
        # 清空缓存
        import backend.app.services.order_middle_platform as omp
        omp._dashboard_cache = {"result": None, "expires_at": 0.0}

        result1 = order_dashboard(session)
        assert result1["total_orders"] == 0
        assert result1["stp_rate"] == 0.0

        result2 = order_dashboard(session)
        # 缓存命中，与 result1 是同一个 dict
        assert result2 is result1

    def test_cache_expires_after_ttl(self, session):
        """TTL 过期后应返回新结果"""
        import backend.app.services.order_middle_platform as omp
        omp._dashboard_cache = {"result": None, "expires_at": 0.0}

        result1 = order_dashboard(session)
        assert result1["total_orders"] == 0

        # 把缓存过期时间拨到过去
        omp._dashboard_cache["expires_at"] = time.time() - 1.0

        result2 = order_dashboard(session)
        assert result2 is not result1

    def test_cache_reflects_new_data_after_expiry(self, session):
        """缓存过期后新写入的数据应在新结果中反映"""
        import backend.app.services.order_middle_platform as omp
        omp._dashboard_cache = {"result": None, "expires_at": 0.0}

        result1 = order_dashboard(session)
        assert result1["total_orders"] == 0

        # 添加一个 MiddlePlatformOrder
        import uuid
        import datetime
        from backend.app.models import MiddlePlatformOrder, new_id
        order = MiddlePlatformOrder(
            id=new_id(),
            order_no="MP-TEST-001",
            source_system="fxiaoke",
            crm_sales_order_id=new_id(),
            crm_order_id="CRM001",
            crm_order_no="NO001",
            payload_hash="hash1",
            status="VALIDATED",
            created_at=datetime.datetime.now(datetime.timezone.utc),
            updated_at=datetime.datetime.now(datetime.timezone.utc),
        )
        session.add(order)
        session.commit()

        # 强制过期缓存
        omp._dashboard_cache["expires_at"] = time.time() - 1.0

        result2 = order_dashboard(session)
        assert result2["total_orders"] == 1
        assert result2["status_counts"].get("VALIDATED") == 1


# ─────────────────────────────────────────────────
# 测试 3: upsert savepoint 隔离
# ─────────────────────────────────────────────────

class TestUpsertSavepoint:
    """upsert_crm_sales_orders 的 savepoint 隔离测试"""

    def test_normal_upsert_still_works(self, session):
        """正常 upsert 不受 savepoint 改造影响"""
        rows = [_make_crm_row("CRM001", "NO001", "1000.00", "500.00", "500.00")]
        result = upsert_crm_sales_orders(session, rows)
        assert result["created"] == 1
        assert result["total"] == 1
        assert result["row_errors"] == 0

        # 再次 upsert 同一行（用新 dict，因为 apply_order_row 会原地修改 dict）
        rows2 = [_make_crm_row("CRM001", "NO001", "1000.00", "500.00", "500.00")]
        result = upsert_crm_sales_orders(session, rows2)
        assert result["unchanged"] == 1

    def test_bad_row_isolated_from_good_rows(self, session):
        """坏行被 savepoint 隔离，不影响好行"""
        # 先来一个好行
        good_rows = [_make_crm_row("CRM001", "NO001", "1000.00", "500.00", "500.00")]
        result = upsert_crm_sales_orders(session, good_rows)
        assert result["created"] == 1

        # 制造混合行：正常行 + 空行(跳过) + 正常行
        rows = [
            _make_crm_row("CRM002", "NO002", "2000.00", "1000.00", "1000.00"),
            {},  # 无 crm_order_id/crm_order_no → 跳过
            _make_crm_row("CRM003", "NO003", "3000.00", "1500.00", "1500.00"),
        ]
        result = upsert_crm_sales_orders(session, rows)

        # 空 dict 被跳过，好行正常 upsert
        assert result["created"] == 2  # CRM002 + CRM003
        assert result["total"] == 2
        # 验证好行数据还在
        assert session.query(CrmSalesOrder).count() == 3  # CRM001 + CRM002 + CRM003

    def test_savepoint_rolls_back_bad_row_not_good_rows(self, session):
        """验证 savepoint 回滚不影响的已提交的好行"""
        # 先用好行
        rows = [_make_crm_row("CRM001", "NO001", "1000.00", "500.00", "500.00")]
        upsert_crm_sales_orders(session, rows)
        session.commit()

        # 导入一个 payload_hash 会失败的行（模拟）
        # 用 None crm_order_id + 空 dict 模拟数据异常
        row = _make_crm_row("CRM-BAD", "NO-BAD", "abc", "def", "ghi")  # 非数值金额
        result = upsert_crm_sales_orders(session, [row])

        # 好行不受影响
        good = session.query(CrmSalesOrder).filter(CrmSalesOrder.crm_order_id == "CRM001").first()
        assert good is not None
        assert good.crm_order_no == "NO001"


# ─────────────────────────────────────────────────
# 测试 4: _save_sync_run_failure 异常恢复
# ─────────────────────────────────────────────────

class TestSaveSyncRunFailure:
    """_save_sync_run_failure 异常恢复逻辑测试"""

    def test_saves_failure_for_existing_run(self, session):
        """已有 sync_run 时不返回 None"""
        run = CrmSyncRun(source_system="fxiaoke", sync_type="sales_orders", status="Running", trigger="manual")
        session.add(run)
        session.commit()
        run_id = run.id

        exc = RuntimeError("测试错误：CRM 浏览器不可用")
        result = _save_sync_run_failure(session, run_id, "manual", exc, retries=1)
        assert result is True

        # 验证 sync_run 已更新
        updated = session.get(CrmSyncRun, run_id)
        assert updated.status == "Failed"
        assert updated.error_message == "测试错误：CRM 浏览器不可用"

    def test_saves_failure_with_new_run_when_id_is_none(self, session):
        """sync_run 不存在时创建新 Failed run"""
        exc = RuntimeError("测试错误")
        result = _save_sync_run_failure(session, None, "manual", exc, retries=1)
        assert result is True

        # 应有新的 Failed sync_run
        failed = session.query(CrmSyncRun).filter(CrmSyncRun.status == "Failed").first()
        assert failed is not None
        assert failed.trigger == "manual"
        assert "测试错误" in (failed.error_message or "")

    def test_saves_failure_when_id_does_not_exist(self, session):
        """sync_run_id 无效时创建新 Failed run"""
        exc = RuntimeError("测试错误")
        result = _save_sync_run_failure(session, "nonexistent-id", "manual", exc, retries=1)
        assert result is True
        assert session.query(CrmSyncRun).filter(CrmSyncRun.status == "Failed").count() >= 1

    def test_detail_contains_error_type(self, session):
        """失败状态的 detail_json 应包含 error_type"""
        run = CrmSyncRun(source_system="fxiaoke", sync_type="sales_orders", status="Running", trigger="auto")
        session.add(run)
        session.commit()

        exc = RuntimeError("超时错误")
        _save_sync_run_failure(session, run.id, "auto", exc, retries=1)

        updated = session.get(CrmSyncRun, run.id)
        detail = loads(updated.detail_json, {})
        assert detail.get("error_type") == "RuntimeError"


# ─────────────────────────────────────────────────
# 测试 5: main.py 中 _apply_db_query_timeout
# ─────────────────────────────────────────────────

class TestDbQueryTimeout:
    """_apply_db_query_timeout 超时保护测试"""

    def test_sqlite_noop(self, session):
        """SQLite 上 _apply_db_query_timeout 不应报错"""
        from backend.app.main import _apply_db_query_timeout
        # SQLite 不支持 SET LOCAL，函数内应有 dialect.name 检查
        _apply_db_query_timeout(session, timeout_seconds=15)
        # 能正常执行后续查询
        result = session.execute(text("SELECT 1")).scalar()
        assert result == 1

    def test_fastapi_app_loads(self):
        """FastAPI app 模块可以正常加载"""
        from backend.app.main import app
        assert app is not None
        # 验证各 CRM 端点存在
        routes = [route.path for route in app.routes if hasattr(route, "path")]
        assert "/api/crm/orders" in routes
        assert "/api/crm/sync/summary" in routes
        assert "/api/crm/sync/run" in routes
        assert "/api/crm/sync/queue" in routes


# ─────────────────────────────────────────────────
# 测试 6: FastAPI 应用启动完整性
# ─────────────────────────────────────────────────

def test_all_crm_endpoints_load():
    """验证所有 CRM 路由端点注册成功"""
    from backend.app.main import app
    crm_routes = {
        route.path: getattr(route, "methods", None)
        for route in app.routes
        if hasattr(route, "path") and "/api/crm" in (route.path or "")
    }
    expected = {
        "/api/crm/orders",
        "/api/crm/orders/{order_id}",
        "/api/crm/orders/{order_id}/retry-detail-sync",
        "/api/crm/sync/summary",
        "/api/crm/sync/queue",
        "/api/crm/sync/run",
        "/api/crm/sync/orders/{crm_order_no}/force",
        "/api/crm/sync/test-connection",
    }
    for ep in expected:
        assert ep in crm_routes, f"缺少 CRM 端点: {ep}"
