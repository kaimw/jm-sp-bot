#!/usr/bin/env python3
"""
V2 订单中台 代码结构静态验证 v2
使用纯文本匹配方式（不依赖 AST），更鲁棒
"""

import os
import re
import sys

GREEN = "\033[92m"
RED = "\033[91m"
CYAN = "\033[96m"
YELLOW = "\033[93m"
BOLD = "\033[1m"
RESET = "\033[0m"

pass_count = [0]
fail_count = [0]

def check(desc, cond, detail=""):
    if cond:
        pass_count[0] += 1
        print(f"  {GREEN}✅{RESET} {desc}")
    else:
        fail_count[0] += 1
        print(f"  {RED}❌{RESET} {desc}")
        if detail:
            print(f"     {RED}{detail}{RESET}")

def info(msg):
    print(f"  {CYAN}ℹ{RESET} {msg}")

def header(t):
    print(f"\n{BOLD}{'─'*60}{RESET}")
    print(f"{BOLD}  {t}{RESET}")
    print(f"{BOLD}{'─'*60}{RESET}")

def grep_count(text, pattern):
    return len(re.findall(pattern, text))

def grep_exists(text, pattern):
    return bool(re.search(pattern, text))

PROJECT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

def read_file(path):
    with open(path) as f:
        return f.read()

def main():
    print(f"\n{BOLD}{CYAN}╔══════════════════════════════════════════════════════════════╗{RESET}")
    print(f"{BOLD}{CYAN}║   V2 订单中台 代码结构静态验证 (纯文本模式)                 ║{RESET}")
    print(f"{BOLD}{CYAN}╚══════════════════════════════════════════════════════════════╝{RESET}")

    models_src = read_file(os.path.join(PROJECT, "backend", "app", "models.py"))
    omp_src = read_file(os.path.join(PROJECT, "backend", "app", "services", "order_middle_platform.py"))
    main_src = read_file(os.path.join(PROJECT, "backend", "app", "main.py"))
    diag_src = read_file(os.path.join(PROJECT, "backend", "app", "services", "exception_diagnosis.py"))
    jobs_src = read_file(os.path.join(PROJECT, "backend", "app", "services", "jobs.py"))
    jk_src = read_file(os.path.join(PROJECT, "backend", "app", "services", "oms", "jackyun_client.py"))
    ts_src = read_file(os.path.join(PROJECT, "backend", "app", "services", "task_scheduler.py"))
    test_src = read_file(os.path.join(PROJECT, "tests", "test_order_middle_platform.py"))

    # ═══════════════════════════════════════════════════════════════
    # 1. 数据模型 — 对照设计文档 §7
    # ═══════════════════════════════════════════════════════════════
    header("1. 数据模型 (models.py) — 对照设计文档 §7")

    tables = [
        ("crm_sales_orders", "CRM 订单镜像"),
        ("crm_order_snapshots", "CRM 订单详情快照"),
        ("middle_platform_orders", "中台标准订单"),
        ("middle_platform_order_items", "中台订单明细"),
        ("order_attachments", "订单附件与证据"),
        ("delivery_notices", "发货通知单"),
        ("exception_cases", "异常任务"),
        ("processing_jobs", "异步任务总线"),
        ("integration_events", "集成事件"),
        ("audit_events", "审计日志"),
        ("agent_run_logs", "AI 运行记录"),
        ("model_call_logs", "大模型调用记录"),
        ("crm_order_items", "CRM 订单明细"),
        ("crm_sync_runs", "CRM 同步运行记录"),
        ("product_spus", "商品 SPU"),
        ("product_skus", "商品 SKU"),
        ("product_inventory_snapshots", "库存快照"),
        ("channel_pricings", "渠道定价"),
        ("promotion_rules", "促销规则"),
        ("users", "用户表"),
    ]
    for table, desc in tables:
        check(f"[{table}] — {desc}",
              grep_exists(models_src, rf"__tablename__\s*=\s*[\"']{table}[\"']"))

    # 关键字段检查
    mpo_section = re.search(r'class MiddlePlatformOrder.*?(?=^class \w|\Z)', models_src, re.DOTALL | re.MULTILINE)
    mpo_text = mpo_section.group(0) if mpo_section else ""

    mpo_fields = [
        "order_no", "source_system", "crm_order_id", "crm_order_no",
        "source_policy", "platform_order_no", "shop_code", "channel_code",
        "fulfillment_type", "customer_name", "sales_user_name",
        "currency", "order_amount", "status", "validation_summary_json",
        "version", "payload_hash",
    ]
    for f in mpo_fields:
        check(f"  MiddlePlatformOrder.{f}",
              grep_exists(mpo_text, rf'\b{f}\b.*=.*mapped_column'),
              detail=f"字段 '{f}' 未找到 mapped_column 定义" if not grep_exists(mpo_text, rf'\b{f}\b') else "")

    dn_section = re.search(r'class DeliveryNotice.*?(?=^class \w|\Z)', models_src, re.DOTALL | re.MULTILINE)
    dn_text = dn_section.group(0) if dn_section else ""
    dn_fields = [
        "notice_no", "order_id", "notice_version", "source_snapshot_hash",
        "status", "oms_idempotency_key", "oms_method", "oms_order_no",
        "warehouse_code", "shop_code", "logistic_code", "waybill_no",
        "retry_count", "max_retries", "next_retry_at", "last_error",
        "split_preview_json", "payload_json", "version",
        "print_status", "print_retry_count",
        "platform_fulfillment_status", "platform_fulfillment_error",
        "platform_fulfillment_retry_count",
    ]
    for f in dn_fields:
        check(f"  DeliveryNotice.{f}",
              grep_exists(dn_text, rf'\b{f}\b'),
              detail=f"字段 '{f}' 未找到")

    # 数据约束
    num_count = len(re.findall(r'Numeric\(15\s*,\s*2\)', models_src))
    ver_count = len(re.findall(r'version.*=.*mapped_column.*Integer.*default=0', models_src))
    uq_count = len(re.findall(r'UniqueConstraint', models_src))
    check("金额字段 Numeric(15,2)", num_count >= 3,
          f"共 {num_count} 处")
    check("乐观锁 version 字段", ver_count >= 3,
          f"共 {ver_count} 个")
    check("唯一约束 UniqueConstraint", uq_count >= 8,
          f"共 {uq_count} 个")

    # ═══════════════════════════════════════════════════════════════
    # 2. 状态机
    # ═══════════════════════════════════════════════════════════════
    header("2. 订单状态机 — 对照设计文档 §5")

    design_statuses = [
        "CRM_APPROVED", "IMPORTED", "VALIDATING", "VALIDATION_BLOCKED",
        "VALIDATED", "DELIVERY_NOTICE_READY", "OMS_PENDING", "OMS_RETRYING",
        "OMS_BLOCKED", "OMS_ACCEPTED", "PICKING", "SHIPPED",
        "FULFILLMENT_ARCHIVED", "SIGNED", "FINANCE_CHECKING",
        "FINANCE_EXCEPTION", "CLOSED", "CANCELLED",
    ]
    for s in design_statuses:
        check(f"  OrderStatus.{s}", grep_exists(omp_src, rf'{s}\s*=\s*"{s}"'))

    design_events = [
        "OrderSnapshotFetched", "CrmSnapshotChanged", "StartValidation",
        "RulesPassed", "RulesFailedCritical", "ExceptionResolvedAndRevalidate",
        "DeliveryNoticeCreated", "EnqueueOmsPush", "OmsPushSuccess",
        "FirstOmsPushFailed", "RetryTimerDueAndOmsSuccess",
        "RetryFailedButUnderMaxRetries", "RetryReachedMaxRetries",
        "ExceptionResolvedAndReplay", "OmsPickingStarted", "OmsShipped",
        "ArchivePhase1Fulfillment", "LogisticsSigned", "StartFinanceCheck",
        "FinanceCheckFailed", "FinanceCheckPassed", "CancelConfirmed",
    ]
    for e in design_events:
        # OrderEvent uses pattern: ORDER_SNAPSHOT_FETCHED = "OrderSnapshotFetched"
        # So we match the string VALUE, not the enum key
        check(f"  OrderEvent.{e}", grep_exists(omp_src, f'"{e}"'),
              f"未找到事件: {e}")

    check("STATE_TRANSITIONS 跃迁矩阵", grep_exists(omp_src, "STATE_TRANSITIONS"))
    check("IllegalStateTransition 异常", grep_exists(omp_src, "class IllegalStateTransition"))
    check("transition_order() 状态流转", grep_exists(omp_src, "def transition_order"))
    check("流转写入 AuditEvent", grep_exists(omp_src, "AuditEvent.*OrderStatusChanged") or
          grep_exists(omp_src, 'event_type="OrderStatusChanged"'))

    # Count STATE_TRANSITIONS entries by searching for tuple patterns
    transition_count = len(re.findall(r'\(OrderStatus\.\w+,\s*OrderEvent\.\w+\)', omp_src))
    check(f"跃迁规则数 (≥20)", transition_count >= 20, f"共 {transition_count} 条")

    # ═══════════════════════════════════════════════════════════════
    # 3. 预审规则引擎
    # ═══════════════════════════════════════════════════════════════
    header("3. 预审规则引擎 — 对照设计文档 §10")

    check("OrderValidationRule Protocol", grep_exists(omp_src, "class OrderValidationRule"))
    check("ValidationResult dataclass", grep_exists(omp_src, "class ValidationResult"))
    check("OrderContext dataclass", grep_exists(omp_src, "class OrderContext"))
    check("BlockerLevel Enum", grep_exists(omp_src, "class BlockerLevel"))

    rules = [
        "RequiredHeadFieldsRule", "PhaseOneCompletenessRule",
        "CustomerMappingRule", "PositiveAmountRule",
        "AmountConsistencyRule", "HasOrderItemsRule",
        "KnownSkuRule", "LocalInventoryAvailableRule",
    ]
    for r in rules:
        check(f"  [{r}]", grep_exists(omp_src, rf'class {r}'),
              f"规则 '{r}' 未找到类定义" if not grep_exists(omp_src, r) else "")

    check("DEFAULT_RULES 注册", grep_exists(omp_src, "DEFAULT_RULES"))
    check("run_validation_chain() 引擎", grep_exists(omp_src, "def run_validation_chain"))
    check("CRITICAL 阻断 break", grep_exists(omp_src, "BlockerLevel.CRITICAL") and grep_exists(omp_src, "break"))

    # ═══════════════════════════════════════════════════════════════
    # 4. CRM 事件契约
    # ═══════════════════════════════════════════════════════════════
    header("4. CRM 事件契约 — 对照设计文档 §9.4")

    check("crm_order_parsed_event()", grep_exists(omp_src, "def crm_order_parsed_event"))
    check("validate_crm_order_parsed_event()", grep_exists(omp_src, "def validate_crm_order_parsed_event"))
    check("process_crm_order_parsed_event()", grep_exists(omp_src, "def process_crm_order_parsed_event"))
    check("DuplicateEventException", grep_exists(omp_src, "class DuplicateEventException"))
    check("enqueue_crm_order_parsed_event()", grep_exists(omp_src, "def enqueue_crm_order_parsed_event"))
    check("事件 schema 含 crm_order_id, payload_hash, order_head",
          grep_exists(omp_src, '"crm_order_id"') and
          grep_exists(omp_src, '"payload_hash"') and
          grep_exists(omp_src, '"order_head"'))

    # ═══════════════════════════════════════════════════════════════
    # 5. OMS 履约补偿
    # ═══════════════════════════════════════════════════════════════
    header("5. OMS 履约补偿 — 对照设计文档 §11")

    check("Index退避算法 TaskScheduler", grep_exists(ts_src, "class TaskScheduler"))
    check("RetryPolicy dataclass", grep_exists(ts_src, "class RetryPolicy"))
    check("指数退避公式 base×multiplier^(count-1)", grep_exists(ts_src, r"multiplier\s*\*\*\s*max"))

    check("enqueue_oms_push()", grep_exists(omp_src, "def enqueue_oms_push"))
    check("process_oms_push_notice()", grep_exists(omp_src, "def process_oms_push_notice"))
    check("handle_oms_push_failure()", grep_exists(omp_src, "def handle_oms_push_failure"))
    check("lookup_existing_oms_order() 幂等冲突反查", grep_exists(omp_src, "def lookup_existing_oms_order"))
    check("stale_oms_push_reason() 过期跳过", grep_exists(omp_src, "def stale_oms_push_reason"))
    check("OMS_BLOCKED + 死信通知", grep_exists(omp_src, "enqueue_oms_blocked_notification"))
    check("poll_oms_status_updates()", grep_exists(omp_src, "def poll_oms_status_updates"))
    check("normalize_oms_fulfillment_status()", grep_exists(omp_src, "def normalize_oms_fulfillment_status"))

    # 面单打印
    check("process_oms_waybill_print()", grep_exists(omp_src, "def process_oms_waybill_print"))
    check("print_oms_waybill()", grep_exists(omp_src, "def print_oms_waybill"))
    check("save_waybill_outbound_proof() → OutboundProof", grep_exists(omp_src, "OutboundProof"))
    check("pg_try_advisory_xact_lock 分布式锁", grep_exists(omp_src, "pg_try_advisory_xact_lock"))
    check("enqueue_platform_fulfillment_sync()", grep_exists(omp_src, "def enqueue_platform_fulfillment_sync"))

    # OMS 重试配置
    check("oms_retry_base_delay_seconds 可配置", grep_exists(omp_src, "oms_retry_base_delay_seconds"))
    check("oms_retry_multiplier 可配置", grep_exists(omp_src, "oms_retry_multiplier"))

    # ═══════════════════════════════════════════════════════════════
    # 6. CRM 变更接管
    # ═══════════════════════════════════════════════════════════════
    header("6. CRM 变更 / 撤销接管 — 对照设计文档 §5.2.1")

    check("handle_crm_snapshot_changed()", grep_exists(omp_src, "def handle_crm_snapshot_changed"))
    check("handle_crm_cancel_confirmed()", grep_exists(omp_src, "def handle_crm_cancel_confirmed"))
    check("is_crm_order_cancelled()", grep_exists(omp_src, "def is_crm_order_cancelled"))
    check("expire_delivery_notices()", grep_exists(omp_src, "def expire_delivery_notices"))
    check("cancel_oms_push_jobs()", grep_exists(omp_src, "def cancel_oms_push_jobs"))

    high_risk = [
        "CRM_CHANGED_AFTER_OMS_ACCEPTED", "CRM_CHANGED_DURING_PICKING",
        "CRM_CHANGED_AFTER_SHIPPED", "CRM_CANCELLED_AFTER_OMS_ACCEPTED",
        "CRM_CHANGED_BEFORE_OMS_PUSH", "CRM_CANCELLED_BEFORE_OMS_PUSH",
        "CRM_CANCELLED_DURING_OMS_PENDING", "CRM_CHANGED_DURING_OMS_RETRY",
    ]
    for ex in high_risk:
        check(f"  异常类型 [{ex}]", grep_exists(omp_src, f'"{ex}"'))

    check("CancelConfirmed → CANCELLED 状态跃迁", grep_exists(omp_src, "CancelConfirmed"))

    # ═══════════════════════════════════════════════════════════════
    # 7. AI 诊断中枢
    # ═══════════════════════════════════════════════════════════════
    header("7. AI 诊断中枢 — 对照设计文档 §12")

    check("enqueue_exception_diagnosis()", grep_exists(diag_src, "def enqueue_exception_diagnosis"))
    check("diagnose_exception_case()", grep_exists(diag_src, "def diagnose_exception_case"))
    check("build_rule_based_diagnosis() 规则兜底", grep_exists(diag_src, "def build_rule_based_diagnosis"))
    check("diagnose_exception_with_llm() LLM 诊断", grep_exists(diag_src, "def diagnose_exception_with_llm"))
    check("exception_diagnosis_context() ContextPack", grep_exists(diag_src, "def exception_diagnosis_context"))
    check("normalize_llm_diagnosis() 反序列化", grep_exists(diag_src, "def normalize_llm_diagnosis"))
    check("AgentRunLog 写入", grep_exists(diag_src, "AgentRunLog"))
    check("JSON Mode (json_object)", grep_exists(diag_src, '"json_object"'))
    check("LLM 失败 Fallback 兜底", grep_exists(diag_src, '"Fallback"'))
    check("诊断反馈 (accepted/modified/rejected)", grep_exists(main_src, '"accepted"') and grep_exists(main_src, '"modified"') and grep_exists(main_src, '"rejected"'))

    # ═══════════════════════════════════════════════════════════════
    # 8. ContextPack
    # ═══════════════════════════════════════════════════════════════
    header("8. ContextPack 组装 — 对照设计文档 §12.2")

    check("build_context_pack()", grep_exists(omp_src, "def build_context_pack"))
    check("含 exception 区块", grep_exists(omp_src, '"exception"'))
    check("含 order 区块", grep_exists(omp_src, '"order"'))
    check("含 validation 区块", grep_exists(omp_src, '"validation"'))
    check("exception_policy() 策略映射", grep_exists(omp_src, "def exception_policy"))
    check("suggested_actions() 动作建议", grep_exists(omp_src, "def suggested_actions"))
    check("context_type = V2_ORDER_EXCEPTION", grep_exists(omp_src, "V2_ORDER_EXCEPTION"))

    # ═══════════════════════════════════════════════════════════════
    # 9. 电商渠道
    # ═══════════════════════════════════════════════════════════════
    header("9. 电商渠道订单接入 — 对照设计文档 §21")

    check("apportioned_order_item_payloads() 促销分摊", grep_exists(omp_src, "def apportioned_order_item_payloads"))
    check("尾差倒挤校准 (last_line_correction)", grep_exists(omp_src, "proportional_with_last_line_correction"))
    check("standard_sku_code_for_item() 渠道SKU映射", grep_exists(omp_src, "def standard_sku_code_for_item"))
    check("is_platform_fulfilled_order() FBA判定", grep_exists(omp_src, "def is_platform_fulfilled_order"))
    check("archive_platform_fulfilled_order() FBA归档", grep_exists(omp_src, "def archive_platform_fulfilled_order"))
    check("ChannelPricing 渠道定价表", grep_exists(models_src, "class ChannelPricing"))
    check("PromotionRule 促销规则表", grep_exists(models_src, "class PromotionRule"))

    # ═══════════════════════════════════════════════════════════════
    # 10. 集成事件 & 幂等
    # ═══════════════════════════════════════════════════════════════
    header("10. 集成事件 & 幂等")

    check("record_integration_event()", grep_exists(omp_src, "def record_integration_event"))
    check("uq_integration_event_hash 唯一索引", grep_exists(models_src, "uq_integration_event_hash"))
    check("payload_fingerprint() SHA256", grep_exists(omp_src, "def payload_fingerprint"))

    # ═══════════════════════════════════════════════════════════════
    # 11. API 端点
    # ═══════════════════════════════════════════════════════════════
    header("11. API 端点 (main.py)")

    api_routes = [
        ("订单大盘", "@app.get", "/api/v2/order-dashboard"),
        ("订单列表", "@app.get", "/api/v2/orders"),
        ("订单详情", "@app.get", "/api/v2/orders/{order_id}"),
        ("CRM入队V2", "@app.post", "/api/crm/orders/{order_id}/queue-v2"),
        ("CRM处理V2", "@app.post", "/api/crm/orders/{order_id}/process-v2"),
        ("发货通知确认", "@app.post", "/api/v2/delivery-notices/{notice_id}/confirm"),
        ("OMS重放", "@app.post", "/api/v2/delivery-notices/{notice_id}/replay-oms"),
        ("OMS状态同步", "@app.post", "/api/v2/delivery-notices/{notice_id}/sync-oms-status"),
        ("OMS状态轮询", "@app.post", "/api/v2/oms/status-poll"),
        ("异常列表", "@app.get", "/api/exceptions"),
        ("异常BFF", "@app.get", "/api/exceptions/{exception_id}/context"),
        ("异常诊断", "@app.post", "/api/exceptions/{exception_id}/diagnose"),
        ("异常诊断SSE", "@app.get", "/api/exceptions/{exception_id}/diagnose-stream"),
        ("异常反馈", "@app.post", "/api/exceptions/{exception_id}/diagnosis-feedback"),
        ("异常解决", "@app.post", "/api/exceptions/{exception_id}/resolve"),
        ("异常分派", "@app.post", "/api/exceptions/{exception_id}/assign"),
        ("异常重开", "@app.post", "/api/exceptions/{exception_id}/reopen"),
        ("全局跑马灯", "@app.get", "/api/global-exception-ticker"),
    ]
    for desc, method, route in api_routes:
        route_escaped = re.escape(route)
        found = grep_exists(main_src, route_escaped)
        check(f"  {desc}", found, f"未找到: {method} {route}")

    # ═══════════════════════════════════════════════════════════════
    # 12. Job 调度
    # ═══════════════════════════════════════════════════════════════
    header("12. ProcessingJob 调度中心 (jobs.py)")

    job_types = [
        "sync_crm_sales_orders", "CRM_ORDER_PARSED", "OMS_PUSH_NOTICE",
        "OMS_STATUS_SYNC", "OMS_STATUS_POLL", "OMS_WAYBILL_PRINT",
        "PLATFORM_FULFILLMENT_SYNC", "DIAGNOSE_EXCEPTION",
    ]
    for jt in job_types:
        check(f"  job_type: {jt}", grep_exists(jobs_src, f'"{jt}"'))

    check("run_pending_jobs() 统一调度", grep_exists(jobs_src, "def run_pending_jobs"))
    check("recover_stale_processing_jobs()", grep_exists(jobs_src, "def recover_stale_processing_jobs"))
    check("分布式锁 locked_by + locked_until", grep_exists(jobs_src, "locked_by") and grep_exists(jobs_src, "locked_until"))
    check("乐观锁 version 更新", grep_exists(jobs_src, "version") and grep_exists(jobs_src, "attempt_count"))

    # ═══════════════════════════════════════════════════════════════
    # 13. 测试覆盖
    # ═══════════════════════════════════════════════════════════════
    header("13. 测试覆盖 (test_order_middle_platform.py)")

    test_funcs = re.findall(r'def (test_\w+)', test_src)
    check(f"测试函数数量 (≥20)", len(test_funcs) >= 20, f"共 {len(test_funcs)} 个")
    check("全部用 SQLite 内存库", grep_exists(test_src, "sqlite:///:memory:"))
    check("全部用 Mock OMS", grep_exists(test_src, "FakeJackyunClient"))
    info(f"  测试函数列表 ({len(test_funcs)} 个):")
    for tf in test_funcs:
        info(f"    - {tf}")

    # 验证测试覆盖的关键场景
    test_scenarios = [
        ("正向流程", "test_crm_order_event_builds_delivery_preview"),
        ("发货配置缺失阻断", "test_delivery_confirmation_blocks_when_oms_required_config"),
        ("FBA 平台履约归档", "test_platform_fulfilled_order_archives"),
        ("电商促销金额分摊", "test_ecommerce_order_amount_apportionment"),
        ("渠道 SKU 映射", "test_channel_shop_sku_maps_to_standard_sku"),
        ("渠道 SKU 未映射阻断", "test_missing_channel_shop_sku_mapping"),
        ("OMS 幂等冲突反查", "test_oms_idempotency_conflict"),
        ("OMS 死信重放需证据", "test_oms_blocked_replay_requires_repair"),
        ("OMS 状态回写拣货+发货", "test_oms_status_sync_advances_to_picking"),
        ("跳过拣货直接发货", "test_oms_status_sync_job_can_skip"),
        ("面单打印+运单保存", "test_oms_waybill_print_job_saves"),
        ("运单回传平台幂等", "test_waybill_print_syncs_tracking_to_platform"),
        ("平台回传失败阻断", "test_platform_tracking_sync_failure_blocks"),
        ("面单打印失败阻断", "test_oms_waybill_print_failure_blocks"),
        ("OMS状态轮询", "test_oms_status_poll_job_queries"),
        ("过期快照跳过OMS", "test_oms_push_skips_when_source_snapshot_hash"),
        ("过期版本跳过OMS", "test_oms_push_skips_when_notice_version"),
        ("CRM详情快照+附件同步", "test_crm_sync_records_detail_snapshots"),
        ("CRM详情合并且可下载", "test_crm_sync_merges_order_detail_fields"),
        ("CRM详情重试", "test_retry_crm_order_detail_sync"),
        ("CRM附件提取收货人（区分签约人）", "test_crm_attachment_extraction_fills_oms_receiver"),
        ("CRM附件覆盖粗糙地址", "test_crm_attachment_extraction_overwrites_coarse"),
        ("CRM附件LLM兜底提取", "test_crm_attachment_extraction_uses_llm"),
        ("CRM范围外订单忽略", "test_crm_sync_ignores_out_of_scope"),
        ("CRM范围配置可动态调整", "test_crm_phase1_scope_config_can_be_updated"),
        ("CRM变更后预览作废", "test_crm_change_after_delivery_preview_blocks"),
        ("CRM未推前取消", "test_crm_cancel_before_oms_push_cancels"),
        ("OMS已接受后CRM变更不自动改单", "test_crm_change_after_oms_accepted_creates_high_risk"),
        ("OMS待推期间CRM取消", "test_crm_cancel_during_oms_pending_cancels"),
        ("OMS重试期间CRM变更", "test_crm_change_during_oms_retry"),
        ("预审阻断+ContextPack+通知", "test_validation_blocked_creates_context_pack"),
        ("库存不足阻断", "test_inventory_rule_blocks_when_available"),
        ("字段缺失阻断+通知", "test_phase_one_missing_fields_interrupts"),
        ("客户映射失败+通知", "test_customer_mapping_failure_interrupts"),
        ("异常诊断规则兜底", "test_validation_exception_queues_and_writes_diagnosis"),
        ("异常诊断LLM增强", "test_exception_diagnosis_uses_llm_json"),
        ("异常诊断LLM失败兜底", "test_exception_diagnosis_falls_back"),
        ("异常BFF上下文+反馈", "test_exception_context_bff_returns_related_order"),
        ("非法状态跃迁拒绝", "test_illegal_state_transition_is_rejected"),
    ]
    for desc, prefix in test_scenarios:
        found = any(tf.startswith(prefix) for tf in test_funcs)
        check(f"  {desc}", found, f"未找到测试函数前缀: {prefix}")

    # ═══════════════════════════════════════════════════════════════
    # 14. CRM CDP 抓取脚本
    # ═══════════════════════════════════════════════════════════════
    header("14. CRM 爬取脚本")

    cdp_dir = os.path.join(PROJECT, "scripts")
    for script in ["fxiaoke_replay_sales_orders.mjs", "fxiaoke_cdp_probe.mjs",
                    "fxiaoke_capture_detail_request.mjs", "fxiaoke_download_attachment.mjs",
                    "fxiaoke_integration_smoke.mjs"]:
        check(f"  {script}", os.path.exists(os.path.join(cdp_dir, script)))

    # ═══════════════════════════════════════════════════════════════
    # 15. OMS 客户端
    # ═══════════════════════════════════════════════════════════════
    header("15. OMS/WMS 客户端 (jackyun_client.py)")

    check("create_delivery_order()", grep_exists(jk_src, "def create_delivery_order"))
    check("query_delivery_orders()", grep_exists(jk_src, "def query_delivery_orders"))
    check("print_delivery_label()", grep_exists(jk_src, "def print_delivery_label"))
    check("JackyunConfig dataclass", grep_exists(jk_src, "class JackyunConfig"))

    # ═══════════════════════════════════════════════════════════════
    # 总结
    # ═══════════════════════════════════════════════════════════════
    header("══════════════════════ 验证总结 ══════════════════════")
    total = pass_count[0] + fail_count[0]
    print(f"\n  {BOLD}总计: {total} 项检查{RESET}")
    print(f"  {GREEN}通过: {pass_count[0]}{RESET}")
    print(f"  {RED}失败: {fail_count[0]}{RESET}")
    if total > 0:
        rate = (pass_count[0] / total) * 100
        color = GREEN if rate >= 90 else YELLOW if rate >= 70 else RED
        print(f"  {color}通过率: {rate:.1f}%{RESET}")

    print(f"\n  ℹ 这是代码结构层面的静态验证（纯文本匹配，不依赖数据库/外部系统）。")
    print(f"  ℹ 运行时端到端模拟测试: python3 scripts/v2_e2e_simulation.py")
    print(f"  ℹ 已有 pytest 测试: pytest tests/test_order_middle_platform.py -v")

    if fail_count[0] > 0:
        sys.exit(1)

if __name__ == "__main__":
    main()
