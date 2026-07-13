# 商务 AI Agent 订单中台 — 一期编码计划

> 版本：V1.0  
> 日期：2026-06-27  
> 依据：`商务AI_Agent_系统开发需求规格说明书_v0.2.docx`、`v2-order-middle-platform-design.md`  
> 启动方式：爸爸说"开始"后开始编码

---

## 总体顺序原则

1. **先底座后功能** — 数据模型 → 配置页 → 核心逻辑
2. **先核心链路后外围** — ERP制单为主轴，管理台配置为辅
3. **分期提交** — 每个阶段完成后可独立测试
4. **已完成的代码不重构** — 现有 `crm_sync.py`、`order_middle_platform.py` 等只新增不重写

---

## 阶段 0：基础设施（约 0.5 天）

### 0.1 数据模型扩展

| 文件 | 新增/修改 | 说明 | 关联FR |
|------|----------|------|--------|
| `models.py` | 新增 `OrderSequence` | 订单号序列表（year + last_seq） | FR-005 |
| `models.py` | 新增 `EntityMapping` | 主体-仓库映射配置（entity_code, warehouses_json） | FR-008 |
| `models.py` | 新增 `CustomerEntityMapping` | 客户-主体映射表（customer_name, entity_code, warehouse） | FR-009 |
| `models.py` | 新增 `InterEntityTransfer` | 跨主体调货记录 | FR-017 |
| `models.py` | 扩展 `ProductSKU.attributes_json` | 补充 `oms_en_name` 字段（已有，确认字段名） | FR-006 |
| `models.py` | 扩展 `MiddlePlatformOrder` | 新增 `entity_code`, `fulfillment_entity`, `inter_entity_ref` | FR-008/FR-017 |
| `models.py` | 扩展 `OrderStatus` | 新增 `ERP_PENDING`, `ERP_SAVING`, `ERP_SAVED`, `ERP_FAILED` | FR-012 |
| `models.py` | 扩展 `OrderEvent` | 新增6个ERP相关事件 + `DeliveryNoticeCreated` | FR-012 |

### 0.2 配置项初始化

| 文件 | 修改 | 说明 |
|------|------|------|
| `bootstrap.py` | 新增默认配置项 | `order_seq_year`, `erp_write_enabled`（已有）, `mail_receivers_json`（收件人配置） |

### 0.3 订单号生成器

| 文件 | 新增 | 说明 |
|------|------|------|
| `services/order_no_generator.py` | **新增** | `generate_middle_order_no(session) → str` 原子递增，预审通过后调用 |

---

## 阶段 1：金蝶制单集成（约 2 天）— 一期核心

### 1.1 ERP制单状态机集成

| 文件 | 修改 | 说明 |
|------|------|------|
| `order_middle_platform.py` | 修改 `process_crm_order_parsed_event` | 预审通过(`RulesPassed`)后，不直接到发货通知，改为进入 `ERP_PENDING` |
| `order_middle_platform.py` | 新增 `process_erp_billing` | 从 `ERP_PENDING` → `ERP_SAVING`，调用 KingdeeClient |
| `order_middle_platform.py` | 新增 `handle_erp_save_success` | Save→Submit→Audit 成功后 → `ERP_SAVED` |
| `order_middle_platform.py` | 新增 `handle_erp_failure` | 任一环节失败 → `ERP_FAILED` + ExceptionCase（含详细失败原因） |
| `order_middle_platform.py` | 新增 `retry_erp_billing` | `ExceptionResolvedAndReErp` → 重新进入 `ERP_PENDING` |

**Q6 变更处理：**
| 文件 | 修改 | 说明 |
|------|------|------|
| `order_middle_platform.py` | 新增 `handle_crm_changed_during_erp` | ERP制单中CRM变更→完成制单→若SAVED则UnAudit→Cancel→退回IMPORTED |

### 1.2 备货→武汉仓跳过ERP制单

| 文件 | 修改 | 说明 |
|------|------|------|
| `order_middle_platform.py` | 修改 `process_erp_billing` | 检查订单类型+发货仓，备货→武汉仓跳过制单直达发货通知 |

### 1.3 ERP 字段映射

| 文件 | 新增 | 说明 |
|------|------|------|
| `services/erp/sales_order_mapper.py` | **新增** | CRM/MiddlePlatformOrder → 金蝶Save JSON 的字段映射器 |
| `services/erp/kingdee_client.py` | 已有（接口已就绪） | 确认 `save_bill`, `submit_bill`, `audit_bill`, `cancel_bill`, `un_audit_bill` 接口可用 |

**字段映射表：**
```
CRM字段                   → 金蝶字段
客户编号(CustomerID)      → FCustomerID.FNumber
销售组织(OrgID)           → FSaleOrgId.FNumber（查客户-主体映射表）
部门(DeptID)              → FSaleDeptId.FNumber
物料(料号)               → FSaleOrderEntry[].FMaterialId.FNumber
数量                      → FSaleOrderEntry[].FQty
销售员                   → FSalerId.FNumber
销售类型                 → F_UXYO_Assistant.FNUMBER
单据日期                  → FDate
价格（备货）              → 产品价格表（按主体取）
备注(特殊需求摘要)        → FNote
```

### 1.4 新增API端点

| 方法 | 路径 | 说明 |
|------|------|------|
| POST | `/api/erp/test-write-permissions` | 已有，确认可用 |
| POST | `/api/erp/billing-status/{order_id}` | 查询ERP制单状态和失败原因 |

---

## 阶段 2：预审规则链更新（约 2 天）

### 2.1 订单类型自动识别（FR-003）

| 文件 | 修改/新增 | 说明 |
|------|----------|------|
| `services/rules/order_type_identifier.py` | **新增** | 根据金额+附件区分销售/备货，返回类型枚举 |
| `services/order_middle_platform.py` | 修改 | 预审前先调用类型识别，将结果存入 `MiddlePlatformOrder` |

### 2.2 商务审核前置条件（FR-004）

| 文件 | 修改/新增 | 说明 |
|------|----------|------|
| `services/rules/contract_approval_rule.py` | **新增** | 检查CRM `approval_status` = "Approved"，阻断时通知销售 |
| `services/rules/__init__.py` | 修改 | 注册新规则 |

### 2.3 库存三步判断（FR-007）— 替代 LocalInventoryAvailableRule

| 文件 | 修改/新增 | 说明 |
|------|----------|------|
| `services/rules/inventory_three_step_rule.py` | **新增** | Step1查主体仓库→Step2查其他仓库→Step3全缺通知销售 |
| `services/rules/local_inventory.py` | 标记废弃 | 被新规则替代 |
| `services/rules/__init__.py` | 修改 | 替换 `LocalInventoryAvailableRule` |

**Step 2 逻辑：** 其他仓库有货 → 生成调货通知邮件发送全部干系人 → **非阻断，正常通过**  ✅  
**Step 3 逻辑：** 全缺 → **阻断**，状态 `VALIDATION_BLOCKED`，邮件通知销售  ✅

### 2.4 备货订单规则差异

| 文件 | 修改 | 说明 |
|------|------|------|
| `services/rules/__init__.py` | 修改 `supports()` | 备货订单跳过金额/合同/附件规则 |
| `services/erp/sales_order_mapper.py` | 修改 | 备货取价走产品价格表（按主体） |

### 2.5 物料别名匹配完善（FR-006）

| 文件 | 修改 | 说明 |
|------|------|------|
| `services/products.py` | 完善别名匹配 | 低置信度进异常，LLM语义匹配通道已有 |
| `services/llm_fallback.py` | 完善 | 英文→中文翻译缓存逻辑 |

---

## 阶段 3：管理台配置页面（约 2.5 天）

### 3.1 库存Excel导入（FR-010）

| 文件 | 新增/修改 | 说明 |
|------|----------|------|
| `main.py` | 新增 `POST /api/inventory/upload` | Excel上传→解析→预览→确认 |
| `main.py` | 新增 `GET /api/inventory` | 按仓库/物料查询库存快照 |
| `services/inventory_service.py` | **新增** | 库存数据导入逻辑，覆盖该仓库旧数据 |
| `static/index.html` | 新增库存管理页面 | 下载模板、上传、预览、确认 |
| `static/app.js` | 新增前端逻辑 | |

### 3.2 主体-仓库映射页面（FR-008）

| 文件 | 新增/修改 | 说明 |
|------|----------|------|
| `main.py` | 新增 CRUD 接口 | 仓库→出货主体（下拉选择，商务人员维护） |
| `models.py` | 已有 EntityMapping | |
| `static/index.html` | 新增页面 | 表格+编辑弹窗 |

### 3.3 客户-主体映射页面（FR-009）

| 文件 | 新增/修改 | 说明 |
|------|----------|------|
| `main.py` | 新增 CRUD 接口 | 客户名称→关联主体+仓库 |
| `models.py` | 已有 CustomerEntityMapping | |

### 3.4 产品价格维护（FR-011）

| 文件 | 新增/修改 | 说明 |
|------|----------|------|
| `main.py` | 新增 CRUD 接口 | SKU内部成本价，支持按主体维度 |
| `services/products.py` | 扩展 | 价格查询增加 entity_code 参数 |
| `static/index.html` | 新增页面 | 价格表+批量导入 |

### 3.5 收件人配置（用于发货通知邮件）

| 文件 | 新增/修改 | 说明 |
|------|----------|------|
| `main.py` | 新增配置接口 | 按仓库/场景配置收件人邮箱 |
| `models.py` | 扩展 SystemConfig | 或新增独立表 |

### 3.6 LLM模型配置（FR-026）

| 文件 | 新增/修改 | 说明 |
|------|----------|------|
| `main.py` | 已有模型配置接口 | 确认 `ModelProviderConfig` 已可维护 |

---

## 阶段 4：发货通知邮件（约 2 天）

### 4.1 邮件模板引擎

| 文件 | 新增/修改 | 说明 |
|------|----------|------|
| `services/mail/templates/sales_delivery.py` | **新增** | 销售订单发货邮件模板 |
| `services/mail/templates/stock_replenishment.py` | **新增** | 备货订单发货邮件模板 |
| `services/mail_template_service.py` | **新增** | 模板选择+字段填充+附件转发 |

**模板选择规则：**
- 销售订单 → 发货邮件模板（含收货信息，物料表按需6列）
- 备货→海外仓 → 备货邮件模板（不含收货信息，固定5列）
- 备货→武汉仓 → 备货邮件模板，**不带「销售编号：XXX」**，**不走ERP制单**

### 4.2 邮件差异逻辑

| 场景 | 标题 | 收件人 | 发货仓标注 | 销售编号 | ERP制单 |
|------|------|--------|-----------|---------|---------|
| 国内仓发货 | `采购订单-JM-CGDD-{编号}({客户}){日期}【销售编号：{ERP单号}】` | 仓管(可配置) | 无标注 | 带 | ✅ |
| 海外仓发货 | `{客户}，销售订单{日期}-{序号}，【销售编号：{ERP单号}】` | 物流(可配置) | 加粗标红 | 带 | ✅ |
| 备货→海外仓 | `采购订单-JM-CGDD-{编号}({描述}){日期}【销售编号：{ERP单号}】` | 物流(可配置) | 加粗标红 | 带 | ✅ |
| 备货→武汉仓 | `采购订单-JM-CGDD-{编号}({描述}){日期}【备注：武汉仓备货】` | 仓管(可配置) | 无标注 | **不带** | ❌ |

### 4.3 特殊需求提取（FR-018）

| 文件 | 新增/修改 | 说明 |
|------|----------|------|
| `services/crm_attachment_extraction.py` | 扩展 | LLM提取附件/备注中的特殊需求（物流类/生产类/报关类） |
| `services/mail_template_service.py` | 修改 | 有特殊需求时在邮件正文中红色加粗标注 |
| `services/storage.py` | 扩展 | 附件转发（箱唛/标签/中文标签文件） |

### 4.4 邮件发送集成

| 文件 | 修改 | 说明 |
|------|------|------|
| `services/mail_worker.py` | 扩展 | `ERP_SAVED` 后触发发货通知邮件发送 |
| `services/outbound_mail.py` | 已有 | 复用现有SMTP发送能力 |

---

## 阶段 5：异常与通知（约 0.5 天）

### 5.1 库存异常通知

| 文件 | 新增/修改 | 说明 |
|------|----------|------|
| `services/exception_diagnosis.py` | 扩展 | 库存三步判断的异常通知：Step2通知全干系人，Step3通知销售 |
| `services/notification_service.py` | 扩展 | 全干系人通知（深圳商务+香港商务+双方财务+物流） |

### 5.2 ERP制单失败通知

| 文件 | 新增/修改 | 说明 |
|------|----------|------|
| `services/exception_diagnosis.py` | 扩展 | ERP_FAILED时记录详细失败原因并通知IT/商务 |

---

## 依赖关系图

```
阶段 0（数据模型+配置）
  ├──→ 阶段 1（ERP制单集成）—— 一期核心
  │     └──→ 阶段 2（预审规则更新）
  │           └──→ 阶段 4（发货通知邮件）
  │
  ├──→ 阶段 3（管理台页面）
  │     ├── 库存导入 → 预审规则 Step1
  │     ├── 主体映射 → 预审规则 Step1
  │     ├── 客户主体映射 → 备货订单/价格
  │     └── 价格维护 → 备货订单/ERP制单取价
  │
  └──→ 阶段 5（异常与通知）—— 贯穿所有阶段
```

---

## 预估工期

| 阶段 | 内容 | 预估天数 | 并行可能 |
|------|------|---------|---------|
| 0 | 数据模型扩展 + 配置项 | 0.5 | — |
| 1 | 金蝶制单集成（核心） | 2 | 与阶段2、3并行 |
| 2 | 预审规则更新 | 2 | 与阶段1、3并行 |
| 3 | 管理台页面 | 2.5 | 与阶段1、2并行 |
| 4 | 发货通知邮件 | 2 | 依赖阶段1（ERP_SAVED触发） |
| 5 | 异常与通知 | 0.5 | 可并行 |
| **合计** | | **约 6-7 天** | 阶段1、2、3并行，约**3-4 天** |

---

## 启动方式

爸爸在聊天中说 **"开始"**，我按阶段 0 → 1&2&3(并行) → 4 → 5 的顺序开始编码。

### 补充编码任务（基于最新讨论）

| 任务 | 说明 | 优先级 |
|------|------|--------|
| 仓库-主体映射管理页 | 仓库下拉 + 主体下拉，商务人员维护 | P0 |
| 物料例外表管理页 | 特殊物料指定出货主体，覆盖仓库映射 | P0 |
| ERP制单备注+VAT+收货人 | FEntryNote追加仓库+VAT；FReceiveContact填入收货人 | P0 |
| 库存导入记录列表 | 展示每次导入的文件名/仓库/行数/时间 | P0 |
| 库存走势查询 | 按物料+仓库维度查询历史变化 | P1 |
