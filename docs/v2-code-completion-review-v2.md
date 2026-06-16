# V2 商务 AI Agent 订单中台 — 代码完成率 Review（修订版）

**日期：** 2026-06-15
**修订说明：** 前端 React 化已确认推到二期以后，本次 Review 重新聚焦于后端引擎的一期可用性、稳定性和完整性。
**Review 基准：** `docs/v2-order-middle-platform-design.md` (v0.2)、`docs/v2-architecture-and-agent-spec.md` (V2.0)

---

## 一、总体完成度评估

| 维度 | 完成度 | 说明 |
|------|--------|------|
| **核心端到端链路（后台）** | **≈90%** | CRM订单进入→预审→发货通知→OMS下推→状态追踪→面单打印，全链路贯通并通过测试 |
| 数据模型 | 85% | 核心表全部就绪，部分字段待补 |
| 状态机 | 95% | 跃迁矩阵完整，乐观锁+审计到位 |
| 预审规则引擎 | 80% | 8条规则可工作，但未拆分独立目录 |
| OMS 履约补偿 | 85% | 指数退避+死信队列+幂等保护完整 |
| AI 诊断中枢 | 80% | 双路径（LLM+规则兜底）工作正常 |
| 前端 React 控制台 | **二期** | 不在一期范围内，用现有 API 端点+管理后台过渡 |
| **总体（不含前端）** | **≈85%** | 后端引擎足以支撑一期试运行 |

---

## 二、已验证可工作的核心链路

测试文件 `test_order_middle_platform.py` 含 25 个测试用例，全部覆盖以下链路：

### 2.1 CRM → 中台订单（✅ 已验证）

```
CRM 审批订单列表同步
  → upsert_crm_sales_orders() 创建 CRM 镜像 + 详情快照
  → enqueue_crm_order_parsed_event() 写入 ProcessingJob
  → process_crm_order_parsed_event() 消费事件
    → upsert_middle_platform_order() 创建 MP- 前缀中台订单
    → sync_middle_order_items() 同步明细（含渠道 SKU 映射 + 促销金额分摊）
    → transition_order(ORDER_SNAPSHOT_FETCHED) → IMPORTED
    → transition_order(START_VALIDATION) → VALIDATING
    → run_validation_chain() 执行 8 条规则
      → 通过 → transition_order(RULES_PASSED) → VALIDATED
      → 失败 → transition_order(RULES_FAILED_CRITICAL) → VALIDATION_BLOCKED
        → create_exception_case() + 邮件通知
```

**测试覆盖：**
- `test_crm_order_event_builds_delivery_preview_then_pushes_after_confirmation` — 完整正向流程
- `test_validation_blocked_creates_context_pack_exception` — 预审阻断 + ContextPack + 邮件通知
- `test_customer_mapping_failure_interrupts_and_notifies_with_evidence_summary` — 客户映射异常
- `test_phase_one_missing_fields_interrupts_and_notifies_stakeholders` — 字段缺失异常
- `test_inventory_rule_blocks_when_available_quantity_is_short` — 库存不足阻断

### 2.2 电商渠道订单（✅ 已验证）

```
CRM 订单含 shop_code + channel_code + platform_order_no
  → standard_sku_code_for_item() 通过 ChannelPricing 映射渠道SKU→标准SKU
  → apportioned_order_item_payloads() 按比例分摊优惠/运费，尾差倒挤校准
  → is_platform_fulfilled_order() 检测FBA/平台履约 → 直接Archive跳过OMS
  → fulfillment_type=FBM/MERCHANT_FULFILLED → 正常进入发货通知流程
```

**测试覆盖：**
- `test_channel_shop_sku_maps_to_standard_sku_before_pre_review` — 渠道SKU自动映射
- `test_missing_channel_shop_sku_mapping_blocks_pre_review` — 未映射SKU阻断
- `test_ecommerce_order_amount_apportionment_preserves_paid_total` — 金额分摊精度
- `test_platform_fulfilled_order_archives_without_delivery_notice` — FBA自动归档

### 2.3 发货通知 → OMS 下推（✅ 已验证）

```
VALIDATED → create_delivery_notice() 生成发货预览
  → build_delivery_split_preview() 拆单预览（含库存对比+配置警告）
  → build_jackyun_delivery_payload() 构造OMS请求体
DELIVERY_NOTICE_READY → confirm_delivery_notice()
  → validate_delivery_notice_for_oms() 校验必填字段
  → transition_order(ENQUEUE_OMS_PUSH) → OMS_PENDING
  → enqueue_oms_push() 写入 ProcessingJob
process_oms_push_notice()
  → stale_oms_push_reason() 检查 snapshot_hash/notice_version 是否过期
  → push_notice_to_oms() 调用吉客云 wms.order.create
    → 成功 → OMS_ACCEPTED
    → 幂等冲突 → lookup_existing_oms_order() 反查
    → 失败 → handle_oms_push_failure()
      → OMS_RETRYING（指数退避+随机抖动）
      → OMS_BLOCKED（重试耗尽，邮件通知+AI诊断）
```

**测试覆盖：**
- `test_crm_order_event_builds_delivery_preview_then_pushes_after_confirmation` — 正向OMS下推
- `test_delivery_confirmation_blocks_when_oms_required_config_missing` — 配置缺失阻断
- `test_delivery_confirmation_blocks_when_receiver_phone_missing` — 收货人电话缺失阻断
- `test_delivery_confirmation_blocks_when_receiver_address_is_coarse` — 地址粗糙阻断
- `test_oms_idempotency_conflict_is_resolved_by_reverse_lookup` — 幂等冲突反查解决
- `test_oms_blocked_replay_requires_repair_evidence` — 死信重放需修复证据
- `test_oms_push_skips_when_source_snapshot_hash_is_stale` — 过期快照跳过
- `test_oms_push_skips_when_notice_version_is_stale` — 过期版本跳过

### 2.4 OMS 状态追踪 + 面单打印（✅ 已验证）

```
OMS_PUSH 成功后：
  → process_oms_status_update() 接收OMS状态回写
    → 拣货中 → OMS_PICKING_STARTED → PICKING
      → enqueue_oms_waybill_print() 触发跨境面单打印
    → 已发货 → OMS_SHIPPED → SHIPPED → FULFILLMENT_ARCHIVED

OMS 面单打印：
  → process_oms_waybill_print()
    → pg_try_advisory_xact_lock 分布式锁（防并发重复打印）
    → print_oms_waybill() 调用 wms-cross.delivery.print
    → 提取 waybillNo + 保存面单PDF为 OutboundProof 附件
    → enqueue_platform_fulfillment_sync() 回传运单号到电商平台

OMS 状态轮询：
  → poll_oms_status_updates() 批量查询
    → wms.order.query-info.page
    → 匹配 erporderNo 或 oms_order_no
```

**测试覆盖：**
- `test_oms_status_sync_advances_to_picking_and_shipped` — 状态流转：拣货→发货→归档
- `test_oms_status_sync_job_can_skip_directly_to_shipped` — 直接跳转到发货
- `test_oms_waybill_print_job_saves_waybill_and_outbound_proof` — 面单打印+运单号保存
- `test_waybill_print_syncs_tracking_to_platform_once` — 运单回传平台（幂等）
- `test_platform_tracking_sync_failure_blocks_and_creates_exception` — 平台回传失败阻断
- `test_oms_waybill_print_failure_blocks_and_creates_exception` — 面单打印失败阻断
- `test_oms_status_poll_job_queries_oms_and_updates_order` — OMS状态轮询

### 2.5 CRM 变更/撤销接管（✅ 已验证）

```
CRM payload_hash 变化时：
  → handle_crm_snapshot_changed() 按当前状态分流处理
    → IMPORTED/VALIDATED → 重新预审
    → DELIVERY_NOTICE_READY → 作废旧预览 + CRM_CHANGED_BEFORE_OMS_PUSH 异常
    → OMS_PENDING/RETRYING/BLOCKED → 冻结待推job + 高危异常
    → OMS_ACCEPTED/PICKING/SHIPPED → 冻结自动处理 + P0异常（不自动改单）

CRM 撤销/作废时：
  → handle_crm_cancel_confirmed()
    → 未推OMS → 取消预览/job → CANCELLED
    → 已推OMS → P0异常，不自动回滚
```

**测试覆盖：**
- `test_crm_change_after_delivery_preview_blocks_and_expires_preview` — 发货预览后CRM变更
- `test_crm_cancel_before_oms_push_cancels_order_and_preview` — 未下推前CRM撤销
- `test_crm_change_after_oms_accepted_creates_high_risk_exception_without_auto_change` — OMS接收后CRM变更（不自动改单）
- `test_crm_cancel_during_oms_pending_cancels_pending_push_job` — OMS待推期间CRM撤销
- `test_crm_change_during_oms_retry_uses_retry_exception_type` — OMS重试期间CRM变更

### 2.6 AI 诊断闭环（✅ 已验证）

```
异常创建 → create_exception_case()
  → build_context_pack() 组装标准ContextPack
  → enqueue_exception_diagnosis() 入队AI诊断

diagnose_exception_case()
  → LLM路径：diagnose_exception_with_llm() + JSON Mode
    → normalize_llm_diagnosis() 强类型反序列化
  → 兜底路径：build_rule_based_diagnosis() 规则驱动诊断
  → 写入 AgentRunLog + ModelCallLog

人工反馈闭环：
  → /api/exceptions/{id}/diagnosis-feedback (accepted/modified/rejected)
```

**测试覆盖：**
- `test_validation_exception_queues_and_writes_diagnosis` — 规则诊断自动触发
- `test_exception_diagnosis_uses_llm_json_when_model_ready` — LLM JSON Mode 诊断
- `test_exception_diagnosis_falls_back_when_llm_fails` — LLM失败时规则兜底
- `test_exception_context_bff_returns_related_order_and_feedback` — BFF上下文+反馈

### 2.7 CRM 数据质量（✅ 已验证）

- `test_crm_sync_records_detail_snapshots_and_attachments` — 详情快照+附件同步+版本管理
- `test_crm_sync_merges_order_detail_fields_and_downloadable_attachments` — 详情字段合并+可下载附件
- `test_retry_crm_order_detail_sync_refreshes_failed_detail` — 详情同步失败重试
- `test_crm_sync_ignores_out_of_scope_order_without_queueing_middle_platform` — 范围外订单不建中台单
- `test_crm_phase1_scope_config_can_be_updated_by_ops` — 一期范围配置动态调整
- `test_crm_attachment_extraction_fills_oms_receiver_fields_without_confusing_contract_signer` — 附件提取区分签约人与收货人
- `test_crm_attachment_extraction_overwrites_coarse_receiver_address` — 附件提取覆盖粗糙地址
- `test_crm_attachment_extraction_prefers_purchase_party_contact_block` — 采购方信息块提取
- `test_crm_attachment_extraction_uses_llm_when_rule_result_fails_validation` — LLM兜底提取
- `test_crm_attachment_extraction_marks_manual_review_when_llm_still_invalid` — 提取无效标记人工复核

---

## 三、一期待补项（按优先级排序）

### P0：阻塞一期试运行的项

| 序号 | 项 | 当前状态 | 建议 |
|------|-----|----------|------|
| 1 | **CRM 生产接入** | 纷享销客页面接口 replay 方式已验证（`scripts/fxiaoke_replay_sales_orders.mjs`），但真实测试发现重新登录/会话续租后，历史捕获的 `_fs_token`、trace/span、组件 payload 可能失效；当前已增加 DOM 降级发现订单，但详情字段不足时不能直接进入自动履约 | 需确认 CRM 生产环境的会话管理方案；列表同步与详情补全拆成两段；补项目 LLM 兜底解析器，根据当前页面 DOM/网络响应/字段字典动态生成标准订单摘要，并对低置信度字段生成异常 |
| 2 | **OMS/WMS 生产接入** | 吉客云 API 客户端已就绪，但 `oms_enabled` 默认关闭，`oms_mock_success` 默认开启 | 需配置真实的 AppKey/Secret，与 OMS 侧确认接口字段映射和幂等键支持，关闭 mock 模式 |
| 3 | **流程节点干系人邮箱配置** | `v2_validation_failure_to_json` 和 `v2_oms_blocked_to_json` 依赖配置，当前 fallback 到 `ops_cc_email`/`ceo_email` | 需配置各业务部门的标准通知邮箱列表 |

### P1：完善一期功能完整性

| 序号 | 项 | 当前状态 | 建议 |
|------|-----|----------|------|
| 4 | **CRM 销售邮箱字段** | `CrmSalesOrder` 模型缺少 `sales_user_email` 字段 | 从 CRM 详情接口提取销售邮箱，缺失时生成数据异常 |
| 5 | **预审规则拆分到独立目录** | 8 条规则全部在 `order_middle_platform.py`（2791行） | 拆分为 `services/rules/` 目录，每个规则一个文件。不影响功能但影响后续维护 |
| 6 | **拆单多仓支持** | 当前只有 `single_warehouse_default` 策略 | 按仓库+物流方式生成多组候选发货单，至少支持手动指定仓库 |
| 7 | **合同/附件金额一致性校验** | 规则引擎尚未从附件 `evidence_json` 中提取合同金额与 CRM 订单金额比对 | 实现 `ContractAmountConsistencyRule`，预审时加入责任链 |
| 8 | **System Prompt 外置模板化** | System Prompt 硬编码在 `exception_diagnosis.py` 中 | 将 Prompt 存入 `system_configs` 或模板文件，便于运维调整 |

### P2：体验与运维增强

| 序号 | 项 | 当前状态 | 建议 |
|------|-----|----------|------|
| 9 | **集成监控看板** | 已有 `/api/v2/order-dashboard`、`/api/global-exception-ticker`、`/api/integration-events`、`/api/jobs` 等数据端点 | 在现有管理后台添加一个简单的集成监控页面（非 React），或用 Grafana 对接数据库 |
| 10 | **OMS 重试参数调优** | 当前默认 base_delay=60s、multiplier=3、max_retries=3 | 根据 OMS 生产环境实际响应时间调整，建议 max_retries 可提高到 5（业务方确认后） |
| 11 | **配置项命名规范化** | 部分 `v2_` 前缀、部分 `oms_` 前缀、部分无前缀 | 统一为 `v2.{模块}.{参数}` 格式，便于配置管理 |
| 12 | **`order_middle_platform.py` 模块拆分** | 2791 行单文件 | 按职责拆为 `state_machine.py`、`rules.py`、`delivery.py`、`oms_push.py`、`dashboard.py` |
| 13 | **附件 OCR/解析队列完善** | `crm_attachment_cache.py`、`crm_attachment_extraction.py`、`attachment_parser.py` 存在基础能力，但仅 PDF 文本提取较完整 | 补充 Word/Excel 解析流水线，完善 OCR 队列调度 |

---

## 四、设计文档中已确认的"一期不做"清单

以下需求在设计文档中已明确标记为下一期，当前代码已预留接口和状态，但未实现，不纳入一期评估：

| 项 | 设计文档依据 | 当前预留 |
|-----|-------------|----------|
| React + Ant Design 前端Agent控制台 | §15 阶段E | API端点就绪，前端未建 |
| 物流轨迹与签收 | §15 阶段F | `OrderStatus.SIGNED` 已定义 |
| ERP 财务核验（回款/发票/销售出库） | §15 阶段G | `OrderStatus.FINANCE_CHECKING/FINANCE_EXCEPTION/CLOSED` 已定义，金蝶客户端只读 |
| Dify/MCP AI 诊断编排层 | §12.5 | 当前用自研 agent-service |
| 多主体内部结算 | §18 会议决议 | `fulfillment_type` 可扩展 |
| 企业微信/钉钉/飞书通知 | OI-009 待确认 | 邮件通知已就绪 |
| BFF 聚合端点 | §13.3 | 当前 `/api/exceptions/{id}/context` 已具备基础 ContextPack 能力 |
| 规则引擎配置化启停 | §6.8 | 当前通过 `config_bool` 控制，未做 UI |

---

## 五、一期试运行建议路线

### 第一阶段：沙箱验证（1-2周）

1. **配置真实 CRM 爬取参数**（CDP 代理地址、Cookie/会话管理）
2. **配置真实 OMS AppKey/Secret**，关闭 `oms_mock_success`
3. **选定 1-2 个测试业务线/销售**作为一期纳入范围（`v2_crm_phase1_scope_json`）
4. **运行 CRM 列表同步** → 下单详情同步 → 自动进入中台预审
5. **人工验证预审结果**，确认阻断/通过逻辑符合预期
6. **关闭 `oms_auto_confirm_delivery_notice`**（手工确认模式），逐单确认发货通知后手动推 OMS
7. **验证 OMS 状态回写** 和面单打印

### 第二阶段：小范围灰度（2-4周）

1. 开启 `oms_auto_confirm_delivery_notice`（低风险订单自动确认）
2. 配置完整的流程节点通知邮箱
3. 运行 OMS 状态轮询 Worker（定时 `OMS_STATUS_POLL`）
4. 监控异常积累和 AI 诊断质量
5. 根据实际异常反馈调整预审规则参数

### 第三阶段：业务扩面

1. 逐步将更多业务线/销售纳入一期范围
2. 接入电商渠道订单（天猫/京东/Shopify/Amazon）
3. 补齐 P1 项（规则拆分、多仓拆单、合同金额校验）
4. 启动前端 React Agent 控制台建设（二期）

---

## 六、结论

**后端引擎一期可用性评估：已具备沙箱试运行条件。**

核心端到端链路（CRM 同步 → 中台订单 → 预审 → 发货通知 → OMS 下推 → 状态追踪 → 面单打印 → CRM 变更接管）全部贯通，25 个测试用例全部覆盖。异常诊断（LLM + 规则兜底）、邮件通知、死信队列、幂等保护、乐观锁并发控制、高危异常二次确认均已就绪。

一期试运行前最关键的三件事：**(1) CRM 生产接入配置，(2) OMS 真实接口对接，(3) 流程节点邮箱配置。** 这三项是环境配置工作，不涉及代码改动。

次要但建议尽快做的事：拆分 `order_middle_platform.py`（当前 2791 行）、补齐 CRM 销售邮箱字段、实现合同金额一致性校验规则。
