# 纷享销客 CRM 销售订单数据接入设计与可行路线

更新时间：2026-06-11

## 1. 目标与边界

根据《商务 AI Agent 系统开发需求规格说明书 v0.1》，一期订单源头应从人工邮件切换为 CRM 审批完成销售订单。中台需要从纷享销客 CRM 获取销售订单、客户、商品明细、合同/金额、附件、审批状态等数据，并保留 CRM 原始订单号、原始 ID、版本和原始 payload，用于后续预审、OMS 发货通知、金蝶 ERP 财务核验与异常闭环。

一期建议只做“渠道/大客户订单”的订单镜像与状态同步，不在 CRM 侧写入业务单据；如需回写中台状态，也应作为可选能力后置。

## 2. 当前可见 CRM 数据线索

从用户提供的 CRM 销售订单列表截图可见，销售订单对象 URL 为：

```text
https://www.fxiaoke.com/XV/UI/Home#crm/list/=/SalesOrderObj
```

列表字段至少包括：

| CRM 列表字段 | 中台目标字段 | 用途 |
| --- | --- | --- |
| 销售订单编号 | crm_order_no / external_order_no | 幂等、展示、跨系统关联 |
| 客户名称 | customer_name / crm_customer_id | 客户映射、权限、对账 |
| 商机2.0 | opportunity_name / opportunity_id | 合同/商机关联、销售上下文 |
| 生命状态 | crm_life_status | 判断有效、作废、取消等状态 |
| 下单日期 | order_date | 筛选、统计、同步游标 |
| 订单结算方式 | settlement_method / currency | 收款与财务口径 |
| 销售订单金额(元) | order_amount | 合同金额、回款核验 |
| 已回款金额(元) | received_amount | 财务进度展示，最终仍以 ERP 为准 |

左侧菜单还能看到“订单产品”“回款”“回款明细”“开票申请”等对象，说明销售订单明细、回款、发票可能是独立 CRM 对象或子对象。需要通过字段字典或对象详情确认真实 API name、关联字段和明细结构。

## 3. 本项目需要的数据清单

### 3.1 P0 必须拉取

| 数据对象 | 必要字段 |
| --- | --- |
| 订单头 | CRM 数据 ID、销售订单编号、审批/生命状态、下单日期、客户、销售人员、部门、币种/结算方式、订单金额、已回款金额、创建/更新时间 |
| 订单明细 | 明细 ID、关联订单 ID、商品/型号/SKU/料号、产品名称、规格、数量、单价、金额、特殊要求 |
| 客户 | CRM 客户 ID、客户名称、联系人、收货信息、渠道类型、客户状态 |
| 附件 | 合同、特殊要求文件、报关/包装文件等附件 ID、文件名、下载 URL、更新时间 |
| 审批/状态 | 审批完成时间、当前审批状态、作废/取消状态、更新时间 |

### 3.2 P1 建议拉取

| 数据对象 | 用途 |
| --- | --- |
| 商机/合同 | 订单与合同金额、机会阶段、归属销售匹配 |
| CRM 回款/回款明细 | 做销售侧过程展示，最终财务核验仍以金蝶 ERP 为准 |
| 开票申请 | 对比 ERP 发票状态，辅助异常提醒 |
| 变更记录 | 判断订单版本、幂等更新和异常追溯 |

## 4. 可行路线对比

### 路线 A：纷享销客官方 OpenAPI / 对象 API 轮询

由本项目后端作为客户端，使用纷享销客开放平台凭证调用 CRM 对象查询接口，按时间窗口拉取 `SalesOrderObj` 及相关明细对象。

优点：

- 最符合本项目“CRM 拉取/定时任务”的架构。
- 易做幂等、重试、审计、增量游标和失败补偿。
- 不依赖浏览器登录态，适合生产长期运行。

限制：

- 需要企业管理员开通开放平台/API 权限。
- 需要确认对象 API name、字段 API name、审批完成状态码、接口调用频率和费用。
- 附件下载可能需要额外权限或临时签名 URL。

建议用途：主路线。

### 路线 B：纷享销客自定义连接器 / 事件订阅推送到中台

纷享销客帮助中心说明，自定义连接器适合 CRM 与其他提供 API 的系统集成，可实现业务单据自动同步，并支持接口配置、增删查改、批量查询、单条查询、事件订阅和代理服务。参考：纷享销客帮助中心《自定义连接器配置指引》。

实现方式是本项目暴露标准接收接口，例如：

```text
POST /api/v1/integrations/crm/events
POST /api/v1/integrations/crm/orders/upsert
```

由纷享销客侧在销售订单审批完成或变更时，将订单主对象和明细对象推送到中台。

优点：

- 更接近实时。
- 不需要中台高频轮询。
- 对“审批完成即进入中台”的业务体验最好。

限制：

- 可能需要购买/开通自定义连接器产品。
- 配置依赖 CRM 管理员/实施顾问。
- 推送失败重放、字段变更、历史补数据仍建议保留轮询补偿。

建议用途：和路线 A 组合，作为正式生产的实时入口；轮询作为补偿。

### 路线 C：CRM 列表导出 / 文件导入过渡

从销售订单列表、订单产品、回款等页面人工导出 Excel，由本项目提供导入入口或定时读取固定目录。

优点：

- 启动最快，不依赖 API 开通。
- 适合第一周字段摸底、样本验证、规则验证。

限制：

- 不能实时同步。
- 容易漏导、重复导、字段格式漂移。
- 附件、审批日志、明细结构可能不完整。

建议用途：PoC 和 API 未开通前的临时兜底，不建议作为正式生产方案。

### 路线 D：CRM 报表/列表导出 + 邮件或固定目录自动导入

如果不走官方 OpenAPI，这是最推荐的免费路线。做法是在纷享销客里配置销售订单列表视图或自定义报表，由业务人员定期导出 Excel；如果 CRM 支持报表定时发送到邮箱，则直接发到本项目已接入的企业邮箱，由现有邮件/附件解析链路自动入库。

实现方式：

```text
CRM 自定义列表/报表 -> Excel 导出或报表邮件 -> 本项目附件解析/Excel 导入 -> CRM 订单镜像表
```

优点：

- 不需要官方 API 授权。
- 符合学习研究场景，账号风险低。
- 能复用项目现有企业邮箱、附件解析、Excel 导入能力。
- 字段稳定性通常好于页面 DOM 抓取，只要固定导出模板即可。

限制：

- 实时性较弱，取决于人工导出或报表发送频率。
- 需要业务侧维护固定视图/报表字段。
- 附件、审批流明细通常拿不到，只能先覆盖订单头和订单产品明细。

建议用途：非 OpenAPI 场景的主路线。

### 路线 E：本地浏览器 RPA 自动导出

通过 Playwright/Chrome 自动化复用人工登录后的浏览器会话，进入销售订单、订单产品、回款等页面，按固定筛选条件点击导出，然后把下载的 Excel 导入中台。

优点：

- 不需要 API 授权。
- 比抓页面表格稳定，因为最终仍以 CRM 官方导出的 Excel 为数据源。
- 可以实现“每天/每小时自动导出一次”的准自动同步。

限制：

- 依赖人工保持登录态，遇到验证码、MFA、登录过期需要人工处理。
- 不建议保存 CRM 密码，也不建议在服务器无人值守登录。
- 页面菜单和导出按钮变更会导致脚本失效。

建议用途：学习研究和内网电脑上的半自动同步；不建议直接作为严肃生产主链路。

### 路线 F：浏览器页面表格读取 / 页面内部接口复用

通过登录 CRM 页面，抓取页面网络接口或自动点击导出。

优点：

- 在官方 API 暂不可用时，可以快速证明页面上有哪些字段和数据。

限制：

- 依赖账号、验证码、登录态、页面结构和前端接口，稳定性与合规风险高。
- 不适合保存账号密码后长期无人值守运行。
- 页面内部接口可能不承诺兼容，升级后容易失效。

建议用途：只做只读技术侦察或短期救急，不作为一期主方案。

#### 2026-06-11 兜底链路验证结果

已用独立调试 Chrome 验证页面内部接口可复放，销售订单列表接口为：

```text
POST https://www.fxiaoke.com/FHH/EM1HNCRM/API/v1/object/SalesOrderObj/controller/List
```

核心请求参数：

```json
{
  "object_describe_api_name": "SalesOrderObj",
  "search_query_info": "{\"limit\":20,\"offset\":0,\"filters\":[]}"
}
```

实际请求还包含 `list_component`、按钮配置和汇总字段配置，当前复放脚本从抓包文件中复用完整 payload，只替换 `search_query_info.limit/offset`。

本次验证结果：

| 项目 | 结果 |
| --- | --- |
| 登录方式 | 独立 Chrome 临时 profile，人工/脚本登录态 |
| 抓包方式 | Chrome CDP，端口 9333 |
| 列表对象 | `SalesOrderObj` |
| 分页 | `limit=20`，`offset=0/20/40` |
| 拉取结果 | 共 53 条，三页分别 20/20/13 |
| 输出字段 | 订单 ID、订单编号、客户 ID/名称、商机 ID/名称、状态、下单日期、金额、回款、开票、附件文件名等 |
| 敏感处理 | 导出 JSON/CSV 不保存密码、token、附件 signedUrl；只保留附件文件名 |

已新增脚本：

```text
scripts/fxiaoke_cdp_probe.mjs
scripts/fxiaoke_replay_sales_orders.mjs
```

使用方式：

```bash
# 1. 启动独立调试 Chrome
open -na "Google Chrome" --args \
  --remote-debugging-address=127.0.0.1 \
  --remote-debugging-port=9333 \
  --user-data-dir=/private/tmp/fxiaoke-cdp-profile-9333 \
  --no-first-run \
  --no-default-browser-check \
  "https://www.fxiaoke.com/proj/page/loginv2?returnUrl=https%3A%2F%2Fwww.fxiaoke.com%2FXV%2FUI%2FHome%23crm%2Flist%2F%3D%2FSalesOrderObj"

# 2. 抓取页面请求，凭据通过 stdin 临时传入，不写入文件
printf '%s' '{"username":"<手机号>","password":"<密码>"}' \
  | node scripts/fxiaoke_cdp_probe.mjs

# 3. 复放列表接口并导出 JSON/CSV
node scripts/fxiaoke_replay_sales_orders.mjs \
  --probe=/private/tmp/fxiaoke-cdp-probe-xxxx.json
```

完整 probe 文件只适合临时分析，因其中可能包含 CRM 原始响应和附件临时链接，不建议长期保存。确认 List 请求后，应抽取最小配置：

```json
{
  "method": "POST",
  "url": "https://www.fxiaoke.com/FHH/EM1HNCRM/API/v1/object/SalesOrderObj/controller/List?...",
  "postData": "{...完整 List payload...}"
}
```

然后使用：

```bash
node scripts/fxiaoke_replay_sales_orders.mjs \
  --request=/private/tmp/fxiaoke-sales-order-list-request.json
```

当前结论：页面接口拉取销售订单头数据可行，可作为学习研究兜底采集方案。下一步需继续验证“订单产品”对象的列表接口，补齐订单明细。

## 5. 推荐总体方案

如果可以使用官方能力，建议采用“A + B + C”的分层路线：

1. 短期 PoC：先用 CRM 导出样本或管理员提供字段字典，完成字段映射、订单镜像表、规则引擎输入结构验证。
2. MVP 主链路：后端实现纷享销客 CRM Adapter，按更新时间或审批完成时间轮询拉取销售订单与明细。
3. 生产增强：开通自定义连接器或 webhook 事件订阅，审批完成实时推送；轮询任务继续作为补偿。
4. 风险兜底：保留 Excel 导入入口，供 API 故障或历史补数据使用。

如果本项目明确不使用官方 OpenAPI，推荐采用“D + E”的免费路线：

1. 固定 CRM 导出模板：销售订单、订单产品、回款/回款明细各一份，字段名和顺序保持稳定。
2. 优先走报表邮件：如果纷享销客支持定时发送报表到邮箱，则发送到本项目监听邮箱，复用现有邮件附件解析能力。
3. 没有报表邮件时，走本地浏览器 RPA 自动导出 Excel：用户首次手动登录，脚本只点击筛选和导出，不保存密码。
4. 中台侧只消费 Excel：所有后续解析、幂等、字段映射、规则校验都基于导出的文件完成，不直接依赖页面 DOM。
5. 页面表格抓取和内部接口复用只做侦察或临时救急，不作为默认同步方式。

免费路线的数据流建议：

```text
纷享销客销售订单/订单产品/回款导出 Excel
  -> 本项目 import job
  -> crm_orders / crm_order_items / crm_receipts 镜像表
  -> sales_orders 标准订单
  -> 规则引擎 / 发货通知 / ERP 对账
```

## 6. 数据模型建议

当前项目已有 `order_requirements` 等邮件订单需求表，不建议直接复用为 CRM 订单主表。CRM 订单是结构化源数据，应新增独立镜像表，再通过映射关系驱动现有生产任务流程或新中台订单流程。

建议新增：

| 表名 | 说明 |
| --- | --- |
| crm_orders | CRM 销售订单镜像，保留原始 ID、订单号、状态、金额、客户、销售、更新时间、raw_json |
| crm_order_items | CRM 订单产品/明细镜像，保留 SKU/型号/数量/金额、raw_json |
| crm_customers | CRM 客户镜像或客户映射缓存 |
| crm_attachments | CRM 附件元数据与本地对象存储引用 |
| sales_orders | 中台统一订单头，来源可为 CRM、邮件、手工补录 |
| sales_order_items | 中台统一订单明细 |
| integration_events | 已在需求文档中建议，用于接口 trace、幂等、重试、错误记录 |

幂等键建议：

```text
source_system = fxiaoke_crm
biz_key = SalesOrderObj:{crm_data_id}:{version_or_updated_at}:{crm_status}
```

如果 CRM 无版本号，则使用 `crm_data_id + updated_at + payload_hash`。

## 7. 后端模块设计

建议在现有金蝶适配器风格基础上新增：

```text
backend/app/services/crm/
  __init__.py
  fxiaoke_client.py        # 鉴权、对象查询、详情、附件
  sales_order_sync.py      # 增量同步、分页、游标、幂等
  mapper.py                # CRM 原始字段 -> 中台订单字段
  schemas.py               # CRM payload 内部结构
```

配置项存入 `system_configs`，敏感项标记 `is_secret=true`：

| key | 说明 |
| --- | --- |
| crm_enabled | 是否启用 CRM 同步 |
| crm_provider | fxiaoke |
| crm_api_base | API 基础地址 |
| crm_app_id / crm_app_secret | 开放平台应用凭证 |
| crm_tenant_id / corp_id | 企业/租户标识 |
| crm_sales_order_obj | 默认 SalesOrderObj |
| crm_order_item_obj | 订单产品对象 API name，待确认 |
| crm_sync_interval_seconds | 同步间隔 |
| crm_last_sync_at | 增量同步游标 |
| crm_status_approved_values | 审批完成/有效状态码映射 |

建议 API：

```text
POST /api/v1/jobs/crm-sync
GET  /api/v1/integrations/crm/orders
GET  /api/v1/integrations/crm/orders/{crm_order_id}
POST /api/v1/integrations/crm/events
POST /api/v1/integrations/crm/field-mapping/test
```

## 8. 字段映射初稿

| 中台字段 | CRM 来源字段候选 | 备注 |
| --- | --- | --- |
| crm_order_id | CRM 数据 ID / _id / dataId | 必须从 API 确认 |
| crm_order_no | 销售订单编号 | 截图可见，如 `20260520-006881` |
| customer_name | 客户名称 | 后续映射 customer_master |
| opportunity_name | 商机2.0 | 可选 |
| crm_status | 生命状态 / 审批状态 | 需确认“审批完成”字段不是仅列表生命状态 |
| order_date | 下单日期 | 截图可见 |
| settlement_method | 订单结算方式 | 截图可见 |
| currency | 订单结算方式中的币种或独立币种字段 | 需确认 |
| order_amount | 销售订单金额(元) | 截图可见 |
| received_amount | 已回款金额(元) | 截图可见；财务最终以 ERP 为准 |
| sku_code | 订单产品明细 SKU/料号 | 待确认 |
| product_name | 订单产品明细商品名称/型号 | 待确认 |
| quantity | 订单产品明细数量 | 待确认 |
| unit_price | 订单产品明细单价 | 待确认 |
| line_amount | 订单产品明细金额 | 待确认 |
| special_requirement | 备注、特殊要求、自定义字段 | 待业务确认 |

## 9. 非 OpenAPI 免费 PoC 验证步骤

### 第 1 天：固定导出模板

1. 在 CRM 中为“销售订单”创建固定列表视图，筛选条件建议为“下单日期 >= 最近 90 天”或“更新时间 >= 上次导出时间”。
2. 导出销售订单 Excel，确认包含订单编号、客户、商机、生命状态、下单日期、结算方式、订单金额、已回款金额、销售人员、部门、更新时间。
3. 导出“订单产品”Excel，确认能通过销售订单编号或订单数据 ID 关联回订单头。
4. 如需财务参考，再导出“回款/回款明细”Excel。

### 第 2 天：中台导入映射

1. 基于导出 Excel 建立字段映射配置，不在代码里写死中文列名。
2. 使用销售订单编号 + 更新时间 + 文件 hash 做幂等，重复导入不重复创建中台订单。
3. 将原始行保存到 `raw_json`，便于后续字段变化时追溯。
4. 对缺少订单明细、SKU、数量、金额的记录生成异常任务。

### 第 3 天：自动化导出增强

1. 优先验证 CRM 是否可把报表定时发送到邮箱；如果可以，直接接入现有邮件附件解析。
2. 如果不支持报表邮件，再用本地 Playwright/Chrome 脚本点击导出。
3. 脚本只使用人工登录后的会话，不保存账号密码；登录过期时提示人工重新登录。
4. 下载文件进入固定目录后，由中台 import job 自动处理。

## 10. 官方 API PoC 验证步骤

### 第 1 天：CRM 字段与权限摸底

1. 由 CRM 管理员确认是否已开通开放平台 API 或自定义连接器。
2. 导出/截图销售订单对象字段字典，至少覆盖 `SalesOrderObj`、订单产品、客户、附件、审批状态。
3. 确认“审批完成”的准确状态字段和值，不只依赖列表中的“生命状态=正常”。
4. 选取 5 条订单样本，覆盖已回款、未回款、多产品、零金额、特殊要求等情况。

### 第 2-3 天：API 可行性验证

1. 使用 Postman 或后端脚本完成鉴权。
2. 拉取最近 30 天销售订单列表，验证分页、筛选、更新时间、返回字段。
3. 拉取单条订单详情，确认明细、附件、审批字段是否完整。
4. 验证增量同步条件：更新时间、审批完成时间或事件回调。
5. 记录调用限制、错误码、token 有效期和附件下载方式。

### 第 4-5 天：项目内最小闭环

1. 新增 CRM Adapter 与镜像表。
2. 同步样本订单到中台。
3. 输出标准 `OrderHeader + OrderItem` JSON。
4. 接入现有初审规则或新规则引擎，完成必填、SKU、金额的基础校验。
5. 写入 `integration_events`，验证重复同步不会重复创建订单。

## 11. 风险与处理

| 风险 | 影响 | 建议 |
| --- | --- | --- |
| API/连接器未开通或收费 | 主链路延期 | 先用导出样本做字段与规则 PoC，同时推进开通 |
| 字段 API name 不明 | 无法开发稳定映射 | 必须拿字段字典；不要用页面中文名硬编码 |
| 审批状态口径不清 | 草稿/作废单误入中台 | 单独确认审批完成字段和值，并设置白名单 |
| 订单明细是子对象或独立对象 | 明细拉取复杂 | 在 PoC 中优先验证一对多关联字段 |
| 附件下载权限受限 | 合同/特殊要求缺失 | 先保存附件元数据，下载失败转异常 |
| 页面抓取不稳定 | 生产不可用 | 页面自动化只保留为人工导出兜底 |
| CRM 回款与 ERP 回款口径不一致 | 财务异常误判 | CRM 回款只做销售侧参考，ERP 作为核验准绳 |
| 导出模板被业务人员改动 | 导入失败或字段错位 | 使用模板版本、列名校验、导入前预检 |
| 手工导出漏单 | 订单不同步 | 使用更新时间窗口重叠导出，例如每次多拉前 3 天数据并靠幂等去重 |
| 浏览器登录过期 | 自动导出中断 | RPA 只做辅助，失败后通知人工；保留手工上传入口 |

## 12. 结论

该 CRM 页面展示的数据足以支撑一期“订单自动进入中台”的核心需求。如果不考虑官方授权和长期生产稳定性，免费的最佳路线不是直接抓页面，而是：

```text
固定 CRM 导出模板 / 报表邮件
+ 本项目 Excel 导入
+ 可选本地浏览器 RPA 自动下载
```

这条路线免费、可控、实现快，适合学习研究和内部 PoC。它牺牲的是实时性和一部分字段完整度，但能把订单头、订单产品、回款参考先跑起来。等后续确实要生产化，再平滑替换为官方 OpenAPI 或连接器。
