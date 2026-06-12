# 商务 AI Agent 系统架构与开发规格说明书 (V2.0)

**版本：** V2.0 (面向 AI 辅助编程增强版)

**基础工程：** `jm-sp-bot` (基于现有 SQLAlchemy + FastAPI 底座)

**目标读者：** 资深架构师、全栈研发工程师、AI 编码助手 (Codex/Cursor/Copilot)

---

## 1. 架构演进目标与核心原则

V2 版本将系统从“邮件驱动的任务单工作台（MVP）”升级为“以 CRM 订单为源头、Agent 异常驱动的商务订单中台”。

**1.1 核心原则 (AI 编码约束原则)：**

* **控制边界：** 彻底干掉邮件下单，一期订单源头强制唯一（纷享销客 CRM）。
* **架构克制：** 当前仅金蝶云星空 ERP 具备只读接入能力；一期重点补齐 `CRM（源头） -> 中台（预审） -> OMS（履约）`，ERP 只做查询与核验，不做回写。
* **防幻觉编码：** AI 在生成代码时，必须严格遵守本文档定义的**强类型契约、状态机矩阵与设计模式**，严禁使用“面条式” `if-else` 和魔法字符串。

**1.2 当前接入缺口（生成代码前必须校验）：**

| 接入项 | 当前状态 | 编码约束 |
| --- | --- | --- |
| 金蝶云星空 ERP | 已有只读接入 | 只允许读取财务事实用于核验，不得生成 ERP 写入、推单或改单逻辑 |
| 纷享销客 CRM | 缺少生产接入 | 必须新增/完善 CRM 订单、明细、客户、附件、销售人员和销售邮箱同步能力 |
| OMS/WMS | 缺少生产接入 | 必须新增/完善发货单创建、拆单预览、状态回写、失败码、幂等键和重试能力 |
| 流程节点干系人邮箱 | 缺少完整接入 | 商务、生产、财务、审批人、异常责任人等邮箱来自组织/配置/主数据；销售邮箱必须从 CRM 订单销售人员信息获取 |

---

## 2. 数据模型与强类型约束

在现有 `models.py` 的基础上进行升维，严格限制 AI 生成 DDL 与 ORM 时的自由度。

* **高精度数值：** 所有金额字段（如 `amount`, `unit_price`）必须指定为 `DECIMAL(15,2)`。
* **状态字段强枚举：** 状态字段必须在代码级使用强类型 `Enum`，并映射为数据库 `VARCHAR`。
* **分布式幂等索引：** `CrmSalesOrder` 及未来接入的事件表，强制添加组合唯一索引 `UNIQUE KEY uk_order_hash (crm_order_id, payload_hash)`。在应用层捕获 `DuplicateKeyException` 以实现无锁幂等。
* **乐观锁并发控制：** 核心表（如 `orders`, `ProductionTask`）新增 `version (INT)` 字段，用于防范重试与并发篡改。

---

## 3. 核心状态机流转矩阵 (State Machine)

禁止在散落的 Service 方法中直接使用 `UPDATE` 修改状态。所有订单的主状态变更必须遵循以下跃迁矩阵，越级跃迁需抛出 `IllegalStateException`：

* `[IMPORTED]` + `[Event: 触发预审]` -> `[VALIDATING]`
* `[VALIDATING]` + `[Event: 规则全票通过]` -> `[VALIDATED]`
* `[VALIDATING]` + `[Event: 规则命中_CRITICAL]` -> `[VALIDATION_BLOCKED]` (挂起待人工/Agent介入)
* `[VALIDATED]` + `[Event: 首次下推OMS失败]` -> `[OMS_RETRYING]`
* `[OMS_RETRYING]` + `[Event: 重试达到MaxRetries]` -> `[OMS_BLOCKED]` (触发死信诊断)
* `[OMS_RETRYING]` + `[Event: 异步下推OMS成功]` -> `[OMS_ACCEPTED]`

---

## 4. 微服务间异步通信与事件契约

解耦现有的数据抓取与订单处理逻辑。抓取模块清洗完 CRM 数据后，必须通过事件总线（复用升格后的 `ProcessingJob` 或 MQ）下发标准 JSON 契约。

**`CrmOrderParsedEvent` 标准 Schema (强制约定)：**

```json
{
  "trace_id": "req-9876-abc-123",
  "event_type": "CRM_ORDER_PARSED",
  "source_system": "FXIAOKE",
  "data": {
    "crm_order_id": "crm_obj_001",
    "payload_hash": "a1b2c3d4e5f6...", 
    "order_head": { "customer_name": "亚马逊北美渠道", "amount": 125000.00, "currency": "RMB" },
    "order_items": [ { "sku_code": "SKU-3D-SCANNER-PRO", "quantity": 50 } ]
  }
}

```

---

## 5. 预审规则引擎架构范式 (Validation Engine)

废弃臃肿的单体校验函数，必须采用 **策略模式 (Strategy) + 责任链 (Chain of Responsibility)**。

* **基准接口 (`OrderValidationRule`)：** 任何新增规则均须实现该接口，包含 `getRuleCode()`, `supports(orderContext)`, `validate(orderContext)` 三个方法。
* **标准返回 (`ValidationResult`)：** 校验结果必须包含 `passed (bool)`, `blockerLevel (NONE|LOW|HIGH|CRITICAL)`, `reason`, `evidenceRefs`。
* **熔断机制：** 引擎遍历 Rule 列表，若遇到 `blockerLevel == CRITICAL`，立即中断链条并生成挂起的异常记录 `ExceptionCase`。

---

## 6. OMS 履约弹性补偿与死信接管

对于下游发货接口调用，严禁编写无延迟的 `while(true)` 或硬编码 `for` 循环。

* **指数退避策略：** 计算公式为 $T_{wait} = BaseDelay \times Multiplier^{(retry\_count - 1)} \pm Jitter$。（默认配置：基数 60秒，乘数 3，最大重试 3 次）。
* **分布式锁：** 执行重试 Worker 必须使用订单号作为唯一 Key 加锁。
* **死信触发 (DLQ)：** 重试耗尽后，变更状态为 `OMS_BLOCKED`，并将完整的底层 Error Stack 保存至 `ExceptionCase` 供 AI 诊断。

---

## 7. AI Agent 诊断中枢与提示词工程

利用大模型解读死信队列或系统异常栈，将技术黑话转化为业务人话。

* **上下文隔离 (`ContextPack`)：** 严禁直接将大对象或上万行日志丢给 LLM。必须在代码层组装清洗后的 `ContextPack`（含异常明细、订单快照、主数据匹配结果）。
* **JSON Mode 强制约束：** API 调用必须开启结构化 JSON 输出。
* **System Prompt 模板 (内嵌核心代码)：**
* 职责：系统技术支持专家。
* 输入：`{{CONTEXT_PACK_JSON}}`
* 约束：消除技术黑话，精准归因，推荐最合适的责任人（如：商务/IT/财务）。
* 输出格式：包含 `summary`, `risk_level`, `likely_reason`, `suggested_actions` 的 JSON。



---

## 8. 面向 Agent 的前端 UI/UX 中枢 (React + AntD)

前端由“表单驱动的增删改查”重构为“异常驱动的 Agent 控制台”。

* **布局：** 全局左中侧主控视图 + 常驻右侧 `CopilotDrawer` (AI 对话与诊断面板)。
* **API BFF 与流式通信：** 针对异常工作台构建高聚合端点（一次性返回 ContextPack）。对于复杂的 AI 追问，前端通过 **SSE (Server-Sent Events)** 接收流式返回（`agent_thinking`, `agent_typing`）。
* **局部覆写：** 原始单据（如抓取数据）默认全量只读。只有 AI 诊断出的 `missing_fields` 或 `risk_flags` 才渲染为可编辑状态或带有动态 `Intent Button` 的快捷操作卡片。

---

## 9. 从现有 `jm-sp-bot` 的演进重构路径 (Migration Strategy)

在现有代码库上操刀时，分为三个核心重构阶段：

1. **解耦上帝类：** 剥离 `workflow.py` 中的 `evaluate_initial_review` 过程式代码，重构为规则引擎框架存放至 `app/services/rules/` 目录。
2. **补齐 CRM 源头接入：** 改造 `crm_sync.py` 前必须先确认 CRM 生产接入方式；在 `upsert_crm_sales_orders` 保存成功后，立即向 `ProcessingJob` 写入一条 `CRM_ORDER_PARSED` 异步任务，将流量切向新中台。销售邮箱随 CRM 订单销售人员信息同步，缺失时生成数据缺失异常。
3. **补齐 OMS 履约接入：** 新增 OMS/WMS Adapter，明确创建发货单、拆单预览、状态回写、失败码和幂等键后，再启用 OMS 推送与重试 Worker。
4. **接入流程节点邮箱：** 建立商务、生产、财务、审批人、异常责任人等邮箱来源；销售邮箱不单独配置，统一从 CRM 订单信息获取。
5. **保留 ERP 只读边界：** 复用金蝶云星空 ERP 只读适配器做回款、发票、销售出库核验，不生成 ERP 写入逻辑。
6. **激活异常表：** 编写 AI 后台任务，轮询读取 `ExceptionCase` 中待处理的数据，调用 LLM 进行诊断补全，通过 Dify 等流程分发给企微/前端界面。
