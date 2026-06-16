# V2 商务 AI Agent 订单中台 — 代码完成率 Review

**日期：** 2026-06-15
**Review 基准：** `docs/v2-order-middle-platform-design.md` (v0.2)、`docs/v2-architecture-and-agent-spec.md` (V2.0)、`20260609105729-积木易搭丨毛凯预定的会议-逐字稿文本-1.docx`
**覆盖范围：** `backend/app/models.py`、`backend/app/services/order_middle_platform.py`、`backend/app/services/exception_diagnosis.py`、`backend/app/services/task_scheduler.py`、`backend/app/services/crm_sync.py`、`backend/app/services/oms/jackyun_client.py`、`backend/app/main.py`、`scripts/*.mjs`

---

## 总体完成度评估：~60%

后端核心引擎和状态机实现较为完整（约 80%），但前端 React Agent 控制台、独立规则引擎目录、BFF 聚合端点等关键模块尚未启动（约 0-20%）。以下按设计文档的 15 个实施阶段逐项分析。

---

## 一、阶段 A：订单中台底座与数据契约 — 完成度 85%

### 已完成 ✅

1. **CRM 订单镜像表 `crm_sales_orders`**：已实现，含 `payload_hash`、`scope_status`、`sync_status`、唯一索引 `uq_crm_order_hash` 和 `uq_crm_order_source_id`。金额字段虽用了 `String(64)`，但从爬取兼容角度可接受，代码中解析时通过 `parse_decimal()` 转为 `Decimal`。

2. **CRM 详情快照 `crm_order_snapshots`**：已实现，含 `is_latest`、`parse_status`、`raw_json`、唯一索引 `uq_crm_snapshot_hash`。

3. **中台标准订单 `middle_platform_orders`**：已实现，含 `order_no`（MP- 前缀）、`source_policy` (`CRM_ONLY`)、`platform_order_no`、`shop_code`、`channel_code`、`fulfillment_type`、`status`、`version` 乐观锁、`validation_summary_json`。金额用 `Numeric(15,2)`，符合设计约束。

4. **中台订单明细 `middle_platform_order_items`**：已实现，含 `sku_code`、`shop_sku_code`、`channel_code`。金额用 `Numeric(15,2)`。

5. **订单附件表 `order_attachments`**：已实现，含 `attachment_type`、`evidence_json`、`fingerprint`、`parse_status`。

6. **CRM 明细表 `crm_order_items`**：已实现，含 `unique constraint uq_crm_order_item_source_id`。

7. **订单状态机 State Machine**：完整实现了 `OrderStatus` 强类型 Enum（18 个状态）、`OrderEvent` Enum（20+ 事件）、`STATE_TRANSITIONS` 跃迁矩阵字典。`transition_order()` 函数严格执行矩阵校验，越级跃迁抛出 `IllegalStateTransition`。每次跃迁写入 `audit_events` 包含 `from_status`、`to_status`、`event`、`operator_type`、`trace_id`。

8. **两段式同步**：CRM 列表同步 (`sync_crm_sales_orders`) → 详情同步任务 (`sync_crm_order_detail`) 流程已实现。

9. **`CrmOrderParsedEvent` 事件**：定义了标准 JSON Schema（含 `trace_id`、`event_type`、`source_system`、`data.crm_order_id`、`data.payload_hash`、`data.order_head`、`data.order_items`）。Validator 检查字段完整性。消费端幂等通过 `DuplicateEventException` 实现。

10. **`IntegrationEvent` 表与幂等**：`uq_integration_event_hash` 唯一索引。`record_integration_event()` 函数自动比对 `event_type + biz_key + payload_hash`，重复事件只更新状态。

### 部分完成 ⚠️

1. **`crm_sales_orders` CRM 销售邮箱字段缺失**：设计文档明确要求 `sales_user_email` 必须从 CRM 订单销售人员信息获取。当前 `CrmSalesOrder` 模型没有此字段，只在 `sales_user_name` 处有值。销售邮箱缺失时系统不会生成异常。

2. **`crm_order_snapshots` 缺少设计文档中部分字段**：设计文档要求 `approval_status`、`lifecycle_status`、`normalized_json`。当前模型虽有 `raw_json` 和 `parse_status`，但缺少显式的 `approval_status`、`lifecycle_status` 和 `normalized_json` 字段（不过 `raw_json` 包含了这些数据）。

3. **订单号生成**：当前使用 `MP-{crm_order_no}` 截断方案，设计文档未明确格式要求，但建议更稳定的生成策略。

### 未完成 ❌

1. **CRM 详情附件同步未完全自动化**：设计文档 9.3 要求："附件解析 PDF、Word、Excel、图片 OCR 进入解析队列"。当前 `crm_attachment_cache.py` 和 `crm_attachment_extraction.py` 存在基础实现，但 OCR 和 Word/Excel 解析队列尚未观察到。

2. **`DELIVERY_NOTICE_READY` 状态流转有缺失**：设计文档要求从 `VALIDATED` 可以跳过 OMS 直接 `ArchivePhase1Fulfillment`（平台履约订单），当前 `STATE_TRANSITIONS` 中有此规则但标记警告。

---

## 二、阶段 B：预审规则引擎与异常闭环 — 完成度 75%

### 已完成 ✅

1. **策略模式 + 责任链**：定义了 `OrderValidationRule` Protocol（含 `get_rule_code`、`supports`、`validate`）、`ValidationResult` dataclass（含 `rule_code`、`passed`、`blocker_level`、`reason`、`evidence_refs`）、`OrderContext` 上下文类。

2. **8 条默认规则**：
   - `RequiredHeadFieldsRule` — 订单头必填字段
   - `PhaseOneCompletenessRule` — 一期完整字段校验（含审批状态、收货地址、附件类型识别）
   - `CustomerMappingRule` — 客户主数据映射
   - `PositiveAmountRule` — 金额大于 0
   - `AmountConsistencyRule` — 金额一致性（商品金额 vs 订单金额 vs 已收+应收）
   - `HasOrderItemsRule` — 明细存在且数量合法
   - `KnownSkuRule` — SKU 在主数据中启用
   - `LocalInventoryAvailableRule` — 库存可用量校验

3. **CRITICAL 阻断立即中断**：引擎遍历规则列表，`blockerLevel=CRITICAL` 立即停止，写异常任务。

4. **`run_validation_chain()`** 引擎函数。

### 部分完成 ⚠️

1. **规则文件未拆分到独立目录**：设计文档明确要求规则拆分到 `services/rules/` 或 `services/validation/rules/`。当前所有规则类都在 `order_middle_platform.py` 内部（同一个 2791 行的文件），这违反了单一职责原则。

2. **规则启停和参数未完全配置化**：部分规则有 `config_bool` 控制（如 `v2_review_customer_mapping_required`），但规则注册本身仍是硬编码的 `DEFAULT_RULES` 列表，不支持动态注册或配置表启停。

3. **缺少 BOM 型号校验规则**：设计文档 10.3 以 `SkuBomMatchRule` 为示例，当前只有 `KnownSkuRule` 检查 SKU 是否在 ProductSKU 表中存在，没有检查 BOM 标准库。

4. **缺少合同/盖章件附件金额校验**：设计文档 4.1/6.4 要求附件证据参与预审（"合同金额一致性"规则），当前规则未实现从附件 `evidence_json` 中提取金额进行比对。

5. **异常任务缺少 `exception_type` 枚举映射**：当前 `create_exception_case()` 使用字符串 `exception_type`，但没有统一的 `ExceptionType` Enum。代码中散落了 `"VALIDATION_BLOCKED"`、`"CRM_CHANGED_BEFORE_OMS_PUSH"` 等魔法字符串。

### 未完成 ❌

1. **规则注册表/IoC 容器注入**：设计文档要求 "所有 Rule 注册到 IoC 容器或规则注册表中"，当前用硬编码列表模拟。

2. **历史订单模拟测试**：设计文档 6.4 要求预审中心支持"历史订单模拟测试"，未实现。

---

## 三、阶段 C：发货通知与 OMS 弹性补偿 — 完成度 80%

### 已完成 ✅

1. **发货通知 `delivery_notices` 表**：已实现，含 `notice_no`、`oms_idempotency_key`、`oms_method`、`oms_order_no`、`warehouse_code`、`shop_code`、`logistic_code`、`waybill_no`、`retry_count`、`max_retries`、`next_retry_at`、`last_error`、`split_preview_json`、`payload_json`、`version` 乐观锁。

2. **拆单预览**：`build_delivery_split_preview()` 生成预览 JSON，支持单仓默认策略，含库存对比和配置警告。

3. **OMS Adapter (`jackyun_client.py`)**：完整实现了吉客云 API 签名/验签，支持 `wms.order.create` 创建发货单、`wms.order.query-info.page` 状态查询、`wms-cross.delivery.print` 跨境面单打印。

4. **OMS 发货单创建与重试**：`push_notice_to_oms()` → 成功则设置 `oms_order_no`；失败则 `handle_oms_push_failure()` 进入指数退避。幂等冲突时自动调用 `lookup_existing_oms_order()` 反查。

5. **指数退避 + 随机抖动**：`task_scheduler.py` 实现了 `RetryPolicy` dataclass（`base_delay_seconds`、`multiplier`、`max_delay_seconds`、`jitter_seconds`）和 `TaskScheduler` 类。`calculate_next_retry_at()` 函数接入配置参数 `oms_retry_base_delay_seconds`（默认 60s）、`oms_retry_multiplier`（默认 3）、jitter ±5s。

6. **OMS 死信队列**：重试达到 `max_retries` 后 → `transition_order(OMS_BLOCKED)` → 写 `ExceptionCase` → `enqueue_oms_blocked_notification()` 发送邮件。

7. **OMS 状态轮询**：`poll_oms_status_updates()` 批量查询 OMS 状态并更新中台流程 (`picking` → `PICKING`、`shipped` → `SHIPPED` → `FULFILLMENT_ARCHIVED`)。

8. **跨境面单打印**：`wms-cross.delivery.print` 集成完成，含运单号提取、面单 PDF 保存为 `OutboundProof` 附件、指数退避重试、死信异常。PostgreSQL 下使用 `pg_try_advisory_xact_lock` 分布式锁防止并发重复打印。

9. **平台履约回传**：`PLATFORM_FULFILLMENT_SYNC` job 类型，当前使用 mock 模式。

### 部分完成 ⚠️

1. **OMS 下推必填字段校验不完整**：`validate_delivery_notice_for_oms()` 检查了 ownerCode、warehouseCode、shopCode、logisticCode、erporderNo、SKU、数量、收货信息。但设计文档表 9.1.1 要求检查的 `source_snapshot_hash`、`notice_version` 在 job 过期检查中体现，但不在发货单确认时校验。

2. **拆单逻辑过于简化**：当前只有 `single_warehouse_default` 策略，只生成一组。设计文档 6.5 要求按仓库、物流方式、SKU 可发数量、特殊发货要求生成候选发货单组，多组拆单能力未实现。

3. **OMS 产品接口字段映射未使用设计文档约定的字段名**：设计文档 11.1.1 指定了 `erporderNo`、`warehouseCode`、`ownerCode`、`shopCode`、`logisticCode` 等精确映射。当前 `build_jackyun_delivery_payload()` 使用了这些字段名，符合要求。

### 未完成 ❌

1. **无 `while(true)` / 无限循环**：已通过 `ProcessingJob` + `next_retry_at` 调度机制避免，符合设计约束。

---

## 四、阶段 D：AI 诊断中枢 — 完成度 75%

### 已完成 ✅

1. **`exception_diagnosis.py` 异常诊断模块**：完整实现了 `enqueue_exception_diagnosis()` 入队、`diagnose_exception_case()` 执行诊断、`build_rule_based_diagnosis()` 规则兜底（15+ 异常类型覆盖）、`diagnose_exception_with_llm()` LLM 增强。

2. **`ContextPack` 组装**：`build_context_pack()` 函数构造包含 `exception`（type、severity、summary、risk_level、responsible_role、can_auto_retry、freeze_order_flow、suggested_actions、evidence_refs）、`order`（order_no、status、crm 信息、amount、currency）、`validation`（failed_rules、missing_materials、evidence_summary）的上下文包。

3. **System Prompt + JSON Mode**：LLM 诊断使用 `response_format: {"type": "json_object"}`，System Prompt 包含角色设定和约束指令。输出定义了标准 JSON Schema（summary、root_causes、recommended_actions、suggested_owner、confidence、address_correction）。

4. **强类型反序列化**：`normalize_llm_diagnosis()` 解析 LLM 输出，含地址修正字段结构校验。

5. **兜底容错**：`diagnose_exception_case()` 先调用 LLM，失败则 fallback 到 `build_rule_based_diagnosis()`，写入 `AgentRunLog`。

6. **`agent_run_logs` / `model_call_logs` 留痕**：`AgentRunLog` 记录 `input_json`、`output_json`、`status`、`error_message`、`started_at`、`finished_at`。

7. **人工反馈闭环**：`/api/exceptions/{id}/diagnosis-feedback` 端点支持 `accepted/modified/rejected` 三种反馈。

### 部分完成 ⚠️

1. **AI 诊断输出未完全遵循设计文档格式**：设计文档 12.4 定义的标准输出含 `suggested_owner_role`（枚举值中有 "BOM工程师"），但当前 `build_rule_based_diagnosis()` 使用 "运维/OMS接口负责人"、"商务/订单运营" 等更偏实际的中文角色名，两者不完全一致。

2. **System Prompt 未完全模板化**：设计文档 12.3 定义了完整的 Agent System Prompt 模板，当前代码中的 Prompt 是简化的内联版本。缺少对 `{{CONTEXT_PACK_JSON}}` 占位符的显式模板引擎。

### 未完成 ❌

1. **Dify/MCP 编排层接入**：设计文档 12.5 提到"可选引入 Dify 作为 AI 诊断编排层"，当前未接入。

---

## 五、阶段 E：Agent 控制台前端 — 完成度 20%

### 已完成 ✅

1. **后端 API 端点就绪**：
   - `GET /api/v2/order-dashboard` — Agent 运行大盘
   - `GET /api/v2/orders` — 订单列表（支持分页/状态筛选/搜索/权限过滤）
   - `GET /api/v2/orders/{order_id}` — 订单详情（含 items、delivery_notices、validation_summary）
   - `POST /api/exceptions/{id}/diagnose` — 异常诊断
   - `GET /api/exceptions/{id}/diagnose-stream` — SSE 流式诊断
   - `POST /api/v2/delivery-notices/{id}/confirm` — 确认发货通知
   - `POST /api/v2/delivery-notices/{id}/replay-oms` — OMS 重放
   - `POST /api/v2/oms/status-poll` — OMS 状态轮询
   - `GET /api/global-exception-ticker` — 全局异常跑马灯
   - `GET /api/exceptions` — 异常列表（支持状态/类型/严重级别筛选）

2. **`order_dashboard()` 统计函数**：计算 total_orders、status_counts、STP 直通率、open_exceptions、oms_retrying、oms_blocked。

### 未完成 ❌

1. **React + Ant Design 前端项目未启动**：设计文档 13.1 要求 React 18+、Ant Design v5、React Query、SSE。整个 `frontend/` 目录不存在，没有任何 `.tsx`/`.jsx` 文件。

2. **`AgentConsoleLayout` 未实现**：设计要求全局常驻 `CopilotDrawer` 与 `GlobalExceptionTicker`。

3. **高阶业务组件均未实现**：`SmartExceptionCard`、`StreamDiagnosisPanel`、`LineageDiffViewer`、`CopilotDrawer`。`GlobalExceptionTicker` 只有后端数据接口，没有前端 UI。

4. **BFF 聚合端点不完整**：设计文档 13.3 要求 `GET /api/v2/agent-views/exception-desk/{exception_id}` 一次性返回 `exception_case`、`context_pack`、`order_snapshot`、`delivery_notice`、`validation_results`、`master_data_refs`、`suggested_actions`、`audit_timeline`。当前 `GET /api/exceptions/{id}/context` 端点存在但返回结构不同，需确认是否满足 BFF 要求。

5. **"异常驱动"的交互模式未实现**：设计要求"异常详情页默认只读 + 局部修复"、"不出现传统全量表单"，前端缺失使得这些交互约束无法落地。

---

## 六、阶段 F：下一期物流轨迹与签收 — 完成度 10%

当前模型和状态机已预留 `OrderStatus.SIGNED`、`DeliveryNotice.waybill_no` 和跨境面单打印能力 (`OMS_WAYBILL_PRINT`)。但签收状态同步、物流轨迹 API 接入、轨迹节点展示均未实现。按设计文档这是下一期内容，当前完成度合理。

---

## 七、阶段 G：下一期 ERP 财务核验 — 完成度 5%

金蝶云星空 ERP 只读客户端 (`kingdee_client.py`) 已实现，ERP 物料同步 (`material_sync.py`) 和库存快照同步已有。但回款/发票/销售出库核验、财务对账页、ERP 单据核验视图均未实现。按设计文档这是下一期内容，当前完成度合理。

---

## 八、架构演进重构 — 完成度 60%

### 已完成 ✅

1. **`workflow.py` 未完全解耦但新链路已独立**：`order_middle_platform.py` 作为全新的入口文件，不再依赖 `create_task_from_mail`。CRM 数据驱动的新流程已独立运作。但老的 `evaluate_initial_review`（`initial_review.py`）和 `review_order_products` 仍在原位。

2. **`ProcessingJob` 升格为事件总线**：已支持多种 job_type：
   - `sync_crm_sales_orders` — CRM 列表同步
   - `sync_crm_order_detail` — CRM 详情同步
   - `CRM_ORDER_PARSED` — CRM 解析完成事件
   - `OMS_PUSH_NOTICE` — OMS 发货单下推
   - `OMS_WAYBILL_PRINT` — 跨境面单打印
   - `OMS_STATUS_POLL` — OMS 状态轮询
   - `PLATFORM_FULFILLMENT_SYNC` — 平台履约回传
   - `DIAGNOSE_EXCEPTION` — AI 异常诊断

3. **`OutboundMailJob` 指数退避算法已抽象为独立 `TaskScheduler`**：`task_scheduler.py` 的 `RetryPolicy` 可同时在邮件发送和 OMS 重试中复用。

4. **`ExceptionCase` AI 诊断中枢已激活**：`exception_diagnosis.py` 包含完整的诊断 Worker 逻辑，从 `ExceptionCase.detail` 读取 ContextPack 并调用 LLM。

5. **CRM 数据层平滑切换**：`crm_sync.py` 中 `upsert_crm_sales_orders` 检测到新建/更新时自动 `enqueue_crm_order_parsed_event()`。

### 未完成 ❌

1. **`CrmToOrderRequirementMapper`（ACL Mapper）未显式实现**：设计文档要求 "将 `CrmSalesOrder` 转换为中台通用 `OrderRequirement`"。当前 `upsert_middle_platform_order()` 直接从 CRM 字段赋值，缺少中间 ACL 模型。老系统的 `OrderRequirement` 表（`order_requirements`）仍绑定 `MailMessage`。

2. **`Spike 顺序` 中部分步骤未完成**：
   - Spike 2（源头切换）：已基本完成。
   - Spike 4（OMS 补偿）：已基本完成。
   - Spike 5（前端桥接 BFF）：API 端点存在但未完全实现 BFF 聚合。

---

## 九、网商渠道订单接入 — 完成度 55%

### 已完成 ✅

1. **促销金额分摊算法**：`apportioned_order_item_payloads()` 完整实现了按比例分摊优惠和运费，最后一行倒挤校准精度。代码中使用了设计文档 21.2 的数学公式。

2. **平台履约判定**：`is_platform_fulfilled_order()` / `is_platform_fulfilled_raw()` 支持检测 FBA、平台自送、亚马逊物流等标识。`fulfillment_type=PLATFORM_FULFILLED` 订单自动 `ArchivePhase1Fulfillment` 跳过 OMS。

3. **渠道 SKU 自动映射**：`standard_sku_code_for_item()` 通过 `ChannelPricing.channel_sku_id` 查找标准 SKU。找不到时返回原始 shop_sku_code，后续 `KnownSkuRule` 会阻断。

4. **跨境面单打印 API 集成**：吉客云 `wms-cross.delivery.print` API 对接完成，AppKey `17563412`、Secret `ee7ead1fec2c4e26af24a0a7499ab103` 已在 `system_configs` 中通过加密存储。面单 PDF 保存为 OSS/S3 附件，运单号回传平台履约。

### 未完成 ❌

1. **多仓库路由**：设计文档 21.3.2 要求根据买家邮编地理分区自动推荐仓库编码（如德区→DE海外仓、北美→US美东仓、其他→深圳总仓）。当前只支持单一 `oms_warehouse_code` 配置。

2. **真实电商平台 API 履约回传**：当前 `push_platform_fulfillment()` 是 mock 模式（`platform_fulfillment_mock_success=True`），尚未对接 Shopify Fulfillment API 等真实平台。

---

## 十、权限与审计 — 完成度 70%

### 已完成 ✅

1. **角色权限**：`User.role` 支持 `admin`、`business_owner`、`business_operator`、`auditor`、`it_ops`。`serialize_middle_order()` 中 `should_mask_financials()` 对 `business_operator` 权限掩码金额字段。

2. **审计事件**：`audit_events` 表记录了 20+ 种事件类型，包括 `OrderStatusChanged`、`CrmSnapshotFetched`、`ValidationRuleFailed`、`ExceptionCreated`、`ExceptionResolved`、`OmsPushAttempted`、`OmsPushBlocked`、`AiDiagnosisGenerated`、`NotificationSent` 等。`transition_order()` 每次状态变更都写入审计。

3. **高危异常二次确认**：`validate_high_risk_exception_resolution()` 对 `CRM_CHANGED_AFTER_OMS_ACCEPTED` 等 P0 异常要求二次确认 + 处理备注 + 责任人身份。`MANUAL_REPLAY_WITHOUT_FIX` 异常阻止无证据重放。

### 部分完成 ⚠️

1. **BFF 权限裁剪**：当前 BFF 未完全实现，权限裁剪依赖端点内联逻辑。

2. **审计事件命名与设计文档部分对齐**：设计文档 14 节定义了 `CRM_SNAPSHOT_FETCHED`、`AI_ACTION_APPROVED`、`NOTIFICATION_SENT` 等事件。当前 `audit_events` 使用了更细粒度的命名（如 `OmsPushJobsCancelled`、`PlatformFulfillmentSyncBlocked`），语义更清晰但未完全对齐。

---

## 十一、测试覆盖 — 完成度 40%

### 已完成 ✅

1. **`test_order_middle_platform.py`**：存在完整测试文件，覆盖了状态机流转、CRM 解析事件处理、OMS 状态更新、发货通知确认等核心流程。

2. **`test_order_middle_platform_lock_and_dlq.py`**：覆盖了乐观锁和死信队列场景。

### 未完成 ❌

1. **缺少规则引擎单元测试**：8 条默认规则没有独立的规则级单元测试。

2. **缺少 OMS 重试集成测试**：指数退避和死信队列的重试逻辑缺少端到端验证。

3. **缺少前端 E2E 测试**：Playwright 配置存在，但测试文件 `tests/e2e/outbound.spec.ts` 仍面向老系统。

---

## 十二、关键差距总结

| 维度 | 完成度 | 关键缺失 |
|------|--------|----------|
| 数据模型 | 85% | CRM 销售邮箱字段、ACL Mapper 显式化 |
| 状态机 | 90% | 跃迁矩阵完整，乐观锁到位 |
| CRM 同步 | 80% | 附件 OCR 解析队列未完整自动化 |
| 预审规则引擎 | 75% | 规则未拆分到 `rules/` 目录，BOM 匹配规则缺失 |
| OMS 履约补偿 | 80% | 多仓路由拆分未实现，真实平台履约回传未对接 |
| AI 诊断 | 75% | Dify 编排层未接入，System Prompt 模板化不足 |
| **前端 React 控制台** | **20%** | **整个前端未启动，无任何 .tsx 文件** |
| 电商渠道接入 | 55% | 多仓路由、真实平台 API 履约回传 |
| 权限与审计 | 70% | BFF 权限裁剪不完整 |
| 测试 | 40% | 规则引擎/OMS 重试单元测试缺失 |
| **总体** | **≈60%** | 后端引擎 ~80%，前端 ~20%，测试 ~40% |

---

## 十三、优先级建议

### P0（阻塞上线）

1. **启动 React + Ant Design 前端项目**：没有前端，所有后端工作无法验收。建议优先实现 Agent 运行大盘、订单管理列表/详情、异常干预台三个 P0 页面。

2. **实现 BFF 聚合端点** `GET /api/v2/agent-views/exception-desk/{exception_id}`：前端异常干预台的核心数据源。

3. **补齐 `SmartExceptionCard` + `StreamDiagnosisPanel` 组件**：异常驱动的 Agent 交互模式的核心组件。

### P1（核心功能完整性）

4. **拆分规则引擎到独立目录** `services/rules/`：将 8 条规则类从 `order_middle_platform.py` 抽出，每个规则独立文件，支持动态注册。

5. **添加 CRM 销售邮箱字段**到 `CrmSalesOrder`，缺失时生成 `CRM_DATA_MISSING` 异常。

6. **实现多仓路由和按仓库/物流方式拆单**：当前单一仓库方案无法支撑生产级多仓业务。

7. **补充规则引擎单元测试和 OMS 重试集成测试**。

### P2（体验与运维）

8. **实现附件 OCR/Word/Excel 解析队列**：完善合同金额一致性等附件证据校验规则。

9. **System Prompt 模板引擎化**：将 Prompt 外置为可配置模板，便于运维调整。

10. **对接真实平台履约 API**：将 mock 模式替换为 Shopify/Amazon/其他平台的 Fulfillment API。

### 后续阶段（设计文档已列为下一期）

11. 物流轨迹与签收（阶段 F）
12. ERP 财务核验（阶段 G）
13. Dify/MCP AI 诊断编排层

---

## 十四、特别提醒

1. **`order_middle_platform.py` 文件过长**（2791 行）：建议拆分为 `state_machine.py`、`rules.py`、`delivery.py`、`oms_push.py`、`dashboard.py` 等模块。

2. **`workflow.py` 解耦尚未完成**：老的邮件驱动流程仍在运行，与 V2 新链路并存。后续需要制定迁移计划，逐步将 `OrderRequirement` 从 `MailMessage` 绑定切换到 `CrmSalesOrder` 绑定。

3. **配置项命名不统一**：部分配置使用 `v2_` 前缀（如 `v2_review_require_key_attachment`），部分使用 `oms_` 前缀（如 `oms_owner_code`），部分无前缀（如 `ceo_email`）。建议制定统一的配置命名规范。

4. **会议决议需要确认的待定项**（设计文档 19 节中的 OI-001 ~ OI-015）：这些决策直接影响编码优先级。建议先与业务方确认 OI-001（一期业务范围）、OI-004（OMS 接口字段映射）、OI-013（前端独立工程）三个核心问题。
