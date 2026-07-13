# 商务生产任务单智能体 MVP → V2 订单中台

本仓库当前实现第一版 MVP 闭环：后台配置、腾讯企业邮箱 IMAP/SMTP 适配、真实邮件入库队列、原始 EML 留存、Word/Excel/PDF/ZIP 附件解析、外部 OpenAI 兼容模型 Provider、订单任务单工作流、字段证据、缺字段补充、变更/取消分流、模板管理、生产反馈抄送策略、周报导出/入队、清理/备份和本地管理台。

V2 已从"邮件驱动的任务单工作台"升级为"以 CRM 审批完成订单为源头的商务 AI Agent 订单中台"。详见 [docs/v2-order-middle-platform-design.md](docs/v2-order-middle-platform-design.md) 和 [docs/v2-architecture-and-agent-spec.md](docs/v2-architecture-and-agent-spec.md)。

---

## 本地运行

要求 **Python ≥ 3.10**（`pyproject.toml` 中声明 `requires-python = ">=3.10"`）。项目根目录已配置 `.python-version`（pyenv），进入目录后 pyenv 会自动切换到 3.10。

推荐使用虚拟环境：

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -e ".[test]"
```

如果直接使用系统 pip（不推荐）：

```bash
python3 -m pip install -e ".[test]"
```

启动服务：

```bash
uvicorn backend.app.main:app --reload --host 0.0.0.0 --port 8000
```

或通过 python3 模块方式：

```bash
python3 -m uvicorn backend.app.main:app --reload --host 0.0.0.0 --port 8000
```

打开：

```text
http://127.0.0.1:8000
```

默认登录账号为 `admin` / `admin`，可通过 `ADMIN_USERNAME`、`ADMIN_PASSWORD`、`AUTH_SECRET` 和 `AUTH_SESSION_SECONDS` 覆盖。
管理台"模拟订单"默认使用测试销售信箱 `bot.sales@jimuyida.com`。

默认数据库为 `sqlite:///data/app.db`。生产试运行可通过 `DATABASE_URL` 切换到 PostgreSQL，例如：

```bash
DATABASE_URL=postgresql+psycopg://jm_sp_bot:change-me@127.0.0.1:5432/jm_sp_bot
```

兼容 `postgres://` 和 `postgresql://` 写法，运行时会统一转为 SQLAlchemy 2 推荐的 `postgresql+psycopg://`。

SQLite 内测库迁移到目标数据库可先 dry-run：

```bash
python scripts/migrate_sqlite_to_database.py \
  --source sqlite:///data/app.db \
  --target postgresql+psycopg://jm_sp_bot:change-me@127.0.0.1:5432/jm_sp_bot
```

确认表行数后再执行：

```bash
python scripts/migrate_sqlite_to_database.py \
  --source sqlite:///data/app.db \
  --target postgresql+psycopg://jm_sp_bot:change-me@127.0.0.1:5432/jm_sp_bot \
  --execute
```

Docker 启动：

```bash
cp .env.example .env
docker compose up --build
```

## 生产环境部署

### 🚀 运维部署关键步骤（TL;DR 一键部署指南）

请运维同事参考以下流程快速拉起生产服务：

1. **拉取最新代码**：
   ```bash
   git clone https://github.com/kaimw/jm-sp-bot.git
   cd jm-sp-bot
   git checkout main
   ```

2. **配置生产环境变量**：
   ```bash
   cp .env.production.example .env.production
   # 编辑 .env.production，必须修改以下核心变量：
   # - CONFIG_ENCRYPTION_KEY (必须通过 openssl rand -base64 32 生成，用于中台数据库密钥字段加密)
   # - ADMIN_PASSWORD (管理台管理员登录密码)
   # - MODEL_API_KEY (AI 异常诊断大语言模型 API Key)
   # - BOT_EMAIL_PASSWORD (收发邮件机器人的腾讯企业邮箱授权码)
   ```

3. **使用 Docker Compose 启动容器**：
   ```bash
   docker compose -f docker-compose.prod.yml --env-file .env.production up --build -d
   ```

4. **服务连通性检查**：
   ```bash
   curl http://127.0.0.1:8000/health
   # 正常运行时应返回: {"status":"ok"}
   ```

---

### 部署前必读

以下操作必须在首次部署到生产环境前完成。**跳过任何一项都可能导致安全漏洞或功能不可用。**

### 第一步：环境变量

```bash
cp .env.production.example .env.production
```

编辑 `.env.production`，至少修改以下变量：

| 变量 | 说明 | 如何生成 |
|------|------|----------|
| `POSTGRES_PASSWORD` | 数据库密码 | `openssl rand -base64 24` |
| `ADMIN_PASSWORD` | 管理员登录密码 | 设置一个强密码，不少于 12 位 |
| `AUTH_SECRET` | 会话签名密钥 | `openssl rand -base64 32` |
| `CONFIG_ENCRYPTION_KEY` | 敏感配置加密密钥（见下文） | `openssl rand -base64 32` |
| `MODEL_API_KEY` | 大模型 API Key | 从模型服务商获取 |
| `BOT_EMAIL_PASSWORD` | 企业邮箱密码/授权码 | 从企业邮箱管理后台获取 |

### 第二步：CONFIG_ENCRYPTION_KEY（重要）

**背景**：系统运行期间，用户通过管理台配置的 OMS AppKey/Secret、邮箱密码、模型 API Key 等敏感配置，存入数据库前会用 `CONFIG_ENCRYPTION_KEY` 做 Fernet 对称加密（AES-128-CBC + HMAC-SHA256），数据库里只存密文 `enc:...`。每次读取时自动解密。

**如果未设置**：系统会打印警告并使用一个硬编码的 fallback key。这个 fallback key 已写死在仓库中，**绝对不能用于生产环境**——任何拥有源码的人都能解密你的数据库敏感字段。

**生成方法**：

```bash
# 生成一个强随机密钥
openssl rand -base64 32
```

**设置方法**：在 `.env.production` 中添加：

```bash
CONFIG_ENCRYPTION_KEY=你生成的随机字符串
```

如果是 `docker-compose.prod.yml` 部署，需要在 `jm-sp-bot` 服务的 `environment` 中加入：

```yaml
CONFIG_ENCRYPTION_KEY: ${CONFIG_ENCRYPTION_KEY}
```

**⚠️ 警告**：

1. 密钥一旦设置就不要更改。更换密钥会导致所有已加密的数据库字段无法解密。
2. 不要将 `.env.production` 或密钥提交到 Git。
3. 请将密钥安全备份（如 1Password、密钥管理服务），丢失密钥等同于丢失数据库中所有加密字段。

### 第三步：数据库

生产环境必须使用 PostgreSQL（SQLite 仅用于本地开发和测试）。`docker-compose.prod.yml` 已内置 PostgreSQL 16 容器。

启动：

```bash
docker compose -f docker-compose.prod.yml --env-file .env.production up --build -d
```

### 第四步：V2 订单中台核心配置

V2 订单中台的完整链路（CRM 同步 → 中台建单 → 预审 → 发货通知 → OMS 下推 → 状态追踪 → 面单打印）需要通过管理台 `/api/config` 或数据库 `system_configs` 表完成以下配置。

**4.1 CRM（纷享销客）接入**

| 配置项 | 说明 | 备注 |
|--------|------|------|
| `crm_sync_enabled` | CRM 自动同步开关 | 生产环境设为 `true` |
| `v2_crm_phase1_scope_enabled` | 一期业务范围过滤开关 | 设为 `true` 后只有匹配范围的订单才会进入中台 |
| `v2_crm_phase1_scope_json` | 一期纳入范围配置（JSON） | 指定 `approved_values`、`include_owner_departments` 等 |
| `v2_crm_phase1_scope_json` | 示例 | `{"approved_values":["approved","审批通过"],"include_owner_departments":["商务一部"]}` |

**4.2 OMS/WMS（吉客云）接入**

| 配置项 | 说明 | 备注 |
|--------|------|------|
| `oms_enabled` | OMS 真实下推开关 | 首次部署先设为 `false`，沙箱验证通过后再开启 |
| `oms_mock_success` | OMS Mock 模式 | 首次部署保持 `true`，与 OMS 侧联调通过后改为 `false` |
| `oms_jackyun_gateway_url` | 吉客云网关地址 | 默认 `https://open.jackyun.com/open/openapi/do` |
| `oms_jackyun_app_key` | 吉客云 AppKey | **需加密存储**，通过管理台配置后自动加密 |
| `oms_jackyun_app_secret` | 吉客云 Secret | **需加密存储**，通过管理台配置后自动加密 |
| `oms_owner_code` | 货主 CODE | 需与 OMS 侧确认 |
| `oms_warehouse_code` | 默认发货仓 CODE | 需与仓库侧确认 |
| `oms_shop_code` | 默认店铺 CODE | 需与 OMS 侧确认 |
| `oms_logistic_code` | 默认物流方式编码 | 如顺丰 `SF` |
| `oms_max_retries` | OMS 下推最大重试次数 | 默认 3 |
| `oms_retry_base_delay_seconds` | 重试基础延迟（秒） | 默认 60 |
| `oms_retry_multiplier` | 重试延迟乘数 | 默认 3 |
| `oms_auto_confirm_delivery_notice` | 发货通知自动确认 | 建议初期设为 `false`（人工确认），稳定后对低风险订单开启 |

**4.3 通知与异常处理**

| 配置项 | 说明 | 备注 |
|--------|------|------|
| `v2_validation_failure_to_json` | 预审失败通知收件人列表 | JSON 数组，如 `["商务主管@example.com","运营@example.com"]` |
| `v2_oms_blocked_to_json` | OMS 阻塞通知收件人列表 | JSON 数组 |
| `v2_customer_mapping_json` | 客户主数据映射 | JSON 对象，如 `{"CRM客户ID":{"customer_code":"OMS客户编码","name":"客户名称"}}` |
| `ceo_email` | 兜底通知邮箱 | 未配置特定通知人时使用 |
| `ops_cc_email` | 运维抄送邮箱 | 未配置特定通知人时使用 |

**4.4 LLM / AI 诊断**

| 配置项 | 说明 | 备注 |
|--------|------|------|
| `model_api_key` | 大模型 API Key | **需加密存储**，通过管理台配置 |
| `MODEL_API_BASE` | 模型 API 地址 | 环境变量或管理台配置 |

### 第五步：首次部署验证

部署完成后，按顺序验证以下内容：

1. **健康检查**：`curl http://服务器IP:8000/health` 返回 `{"status":"ok"}`
2. **登录管理台**：浏览器打开 `http://服务器IP:8000`，用 `ADMIN_USERNAME`/`ADMIN_PASSWORD` 登录
3. **检查数据库**：确认 `system_configs` 表中有加密敏感配置（`is_secret=true` 的条目 value 以 `enc:` 开头）
4. **运行静态验证**：`python3 scripts/v2_static_verification.py`（纯代码检查，不依赖外部系统）
5. **运行 E2E 模拟测试**：`python3 scripts/v2_e2e_simulation.py`（SQLite 内存库 + 全 Mock，验证完整链路）
6. **运行 pytest**：`python3 -m pytest tests/ -v`

### 迁移内测数据到生产库

```bash
python scripts/migrate_sqlite_to_database.py \
  --source sqlite:///data/app.db \
  --target postgresql+psycopg://jm_sp_bot:密码@127.0.0.1:5432/jm_sp_bot \
  --execute --truncate-target
```

### 生产 Compose 试运行

```bash
cp .env.production.example .env.production
# 修改 .env.production 中的数据库密码、管理员密码、AUTH_SECRET、CONFIG_ENCRYPTION_KEY、邮箱和模型密钥
docker compose -f docker-compose.prod.yml --env-file .env.production up --build -d
```

生产 Compose 会启动应用和 PostgreSQL，应用健康检查访问 `/health`。

---

## 自维护 MVP

管理台"自维护"页面支持收集系统上下文、生成诊断、生成白名单配置修复草案、生成代码修复草案、生成代码交接包、运行白名单验证、查看维护时间线、查看维护会话详情、查看维护动作详情、回填补丁执行记录、记录补丁人工复核结论，以及归档已处理维护会话。会话列表默认隐藏归档记录，可通过"显示已归档会话"切换查看历史，并支持按状态、风险等级筛选和分页浏览；维护动作列表支持按动作类型和状态筛选，并支持分页浏览。配置修复必须输入管理员密码确认后才会写入运行期配置；代码修复草案不会在业务进程中直接改代码。

外部维护 runner 可读取最新代码修复草案、生成交接包、导出 Markdown 报告，并运行白名单验证命令：

```bash
python scripts/maintenance_runner.py list
python scripts/maintenance_runner.py handoff --action-id <maintenance_action_id>
python scripts/maintenance_runner.py export --action-id <maintenance_action_id>
python scripts/maintenance_runner.py validate --action-id <maintenance_action_id> --command "node --check backend/app/static/app.js"
python scripts/maintenance_runner.py complete --action-id <maintenance_action_id> --summary "补丁已完成，等待人工复核" --changed-file backend/app/main.py --test "python3 -m pytest"
python scripts/maintenance_runner.py review --action-id <maintenance_action_id> --decision ReviewAccepted --note "人工复核通过"
```

`handoff` 会在 `data/maintenance` 下生成 Markdown 和 JSON 交接包，包含修复草案、上下文、安全边界、推荐执行流程和 runner 命令；管理台也可以对代码修复动作一键生成交接包，并直接展开查看交接包内容。`validate` 会执行固定白名单命令，管理台也提供同等的"运行验证"入口。`complete` 用于外部维护者回填补丁摘要、改动文件、测试结果和残余风险，管理台也提供同等的"回填执行"入口；`review` 用于记录人工接受、驳回或继续修改结论，形成代码补丁闭环。runner 仅允许执行固定验证命令：`python3 -m compileall backend scripts`、`python3 -m pytest` 和 `node --check backend/app/static/app.js`。执行结果会写回维护动作并记录审计事件。

---

## Agent + Skill 架构演进

下一阶段系统将从固定邮件工作流演进为"商务部数字分身"：商务人员通过技能实验室用自然语言创建、修改、停用和归档 Skill，Agent 在订单排产链路中按权限、证据和成本预算调用技能。详细方案见 [docs/agent-skill-architecture-design.md](docs/agent-skill-architecture-design.md)。

关键原则：

1. Skill 默认先生成草稿，经过静态校验、模拟测试和审批后才发布。
2. 动态 Skill 不直接发送邮件，只能创建草稿或建议动作，由策略层和现有外发队列执行。
3. 系统运行优先用规则、结构化查询、缓存和模板，只有必要时才调用 LLM。
4. 每次 Agent 或 Skill 动作都记录证据、权限判断、模型调用和成本信息。

---

## V2 订单中台

V2 将系统从"邮件驱动的任务单工作台"升级为"以 CRM 审批完成订单为源头的商务 AI Agent 订单中台"。

### 架构文档

- [v2-order-middle-platform-design.md](docs/v2-order-middle-platform-design.md) — V2 改造总设计母版（状态机、数据模型、事件契约、规则引擎、OMS 补偿、AI 诊断、前端交互）
- [v2-architecture-and-agent-spec.md](docs/v2-architecture-and-agent-spec.md) — 架构与开发规格说明书（面向 AI 编码助手增强版）
- [v2-code-completion-review-v2.md](docs/v2-code-completion-review-v2.md) — 代码完成率 Review（含一期试运行建议）

### 核心链路

```
CRM 审批订单 → 中台同步 → 中台建单(MP-前缀)
  → 8条预审规则责任链 → 通过/阻断
  → 发货通知草稿 + 拆单预览
  → 人工/自动确认 → OMS 下推(吉客云 wms.order.create)
  → 指数退避重试(60s/180s/540s)
  → OMS 状态回写(拣货→发货→归档)
  → 跨境面单打印(wms-cross.delivery.print)
  → 运单号回传平台履约
  → 异常自动进入 AI 诊断(LLM + 规则兜底)
```

### 测试

```bash
# 全流程端到端模拟（SQLite 内存库 + 全 Mock，不碰外部系统）
python3 scripts/v2_e2e_simulation.py

# 代码结构静态验证（纯标准库，对照设计文档逐项检查）
python3 scripts/v2_static_verification.py

# 39 个单元测试
pytest tests/test_order_middle_platform.py -v
```

---

## 当前边界

1. 不在代码或文档中保存邮箱密码、模型 API Key。运行期敏感配置通过管理台/API 配置，自动用 `CONFIG_ENCRYPTION_KEY` 做 Fernet 加密后存入数据库。
2. 运行期可通过管理台/API 配置腾讯企业邮箱账号密码、IMAP/SMTP、模型 API Base/API Key、生产部门邮箱和任务单模板。
3. 附件解析当前覆盖 `.docx`、`.xlsx`、`.pdf`、`.zip`、`.txt`、`.csv`；ZIP 默认最大 100MB、最多解压 1 层。
4. MVP 支持低风险生产任务单自动下达：初审通过、字段完整、路由唯一且无风险命中时，系统自动生成并发送生产任务单；订单取消、已排产后变更、风险词命中、路由缺失、低质量答复均进入人工处理。
5. V2 一期订单源头强制唯一（CRM），不接受邮件/聊天/Excel 作为订单主数据入口。邮件只保留为通知、催办、沟通留痕渠道。

---

## 关键接口

- `POST /api/auth/login`：前台登录，默认账号 `admin`、密码 `admin`。
- `POST /api/auth/logout`：退出并清理会话 Cookie。
- `GET /api/auth/me`：检查当前会话状态。
- `POST /api/mailbox/sync`：从腾讯企业邮箱 IMAP 同步未读邮件并写入入库队列。
- 销售订单、销售补充、订单变更和订单取消邮件入库后，会自动生成 `SalesReceiptAck` 回执外发任务，通知发件人邮件已收到并排队处理中。
- 规则分类结果为 `NonTarget` 时，可启用当前模型配置进行 LLM 兜底分类和订单字段抽取；规则和 LLM 都判定为 `NonTarget` 时，邮件会进入异常队列，并尽量归类到当前订单沟通会话。
- 系统启动后默认运行自动邮件 worker，每 60 秒同步 IMAP、处理入库队列，并自动发送销售收件回执、初审未通过/待补充回复、低风险首次生产任务单、生产疑问转销售邮件、生产疑问收件回执、销售答复后的更新版任务单、更新版任务单发送成功回执和最大轮次关闭通知；周报仍需手动生成入队。
- `POST /api/mailbox/auto-run-once`：立即运行一次自动邮件 worker，便于排查真实邮箱链路。
- 后台运行期配置支持 `llm_fallback_enabled` 和 `conversation_max_rounds`，用于控制 LLM 兜底和单次订单沟通会话最大往返轮数。
- `GET/PUT /api/initial-review/rules`：查看或更新销售订单初审规则，支持启停、必填字段和字段/全文自定义规则；未通过初审会回复销售并进入异常队列。
- `POST /api/jobs/run-pending`：执行待处理入库任务，生成任务单草稿。
- `GET /api/mails`、`GET /api/mails/{mail_id}`：查看邮件入库、分类、正文和附件。
- `POST /api/outbound-mails/send-pending`：通过 SMTP 发送待发邮件，单封失败会进入异常队列。
- `POST /api/outbound-mails/{job_id}/retry`：将失败外发邮件重新加入待发送队列。
- `GET/PUT /api/reports/weekly/recipients`：查看或更新周报收件人。
- `POST /api/reports/weekly/enqueue`：生成周报邮件并加入外发队列。
- `GET /api/reports/weekly/export.pdf`：导出带公司抬头/页眉页脚/签章占位的 PDF 周报。
- `GET /api/reports/weekly/export.csv`：导出周报明细 CSV。
- `POST /api/tasks/{task_id}/production-question`：记录生产疑问并生成转销售邮件。
- `POST /api/tasks/{task_id}/sales-reply`：记录销售答复并生成新版任务单草稿。
- `GET /api/tasks/{task_id}/questions`：查看生产疑问和销售答复记录。
- `GET /api/tasks/{task_id}/evidence`：查看订单字段来源证据。
- `GET /api/exceptions`：查看待处理异常队列。
- `POST /api/exceptions/{exception_id}/resolve`：关闭已人工处理的异常。
- `POST /api/exceptions/{exception_id}/diagnose`：触发 AI 异常诊断（LLM + 规则兜底）。
- `GET /api/exceptions/{exception_id}/diagnose-stream`：SSE 流式 AI 诊断。
- `POST /api/exceptions/{exception_id}/apply-requirement-patch`：补齐订单字段并恢复生成任务单草稿。
- `POST /api/cleanup/preview`：预览过期非目标邮件和临时记录清理范围。
- `POST /api/cleanup/run`：执行清理，默认不清理有效订单邮件。
- `POST /api/backups/run`：生成 SQLite 数据和附件目录 ZIP 备份。
- `GET /api/audit-events`：查看关键动作审计日志。

### V2 订单中台接口

- `GET /api/v2/order-dashboard`：Agent 运行大盘（订单总数、状态分布、STP 直通率、异常积压）
- `GET /api/v2/orders`：订单管理列表（支持搜索、状态筛选、分页、权限过滤）
- `GET /api/v2/orders/{order_id}`：订单详情（含明细、发货通知、预审结果、权限掩码）
- `POST /api/crm/orders/{order_id}/queue-v2`：CRM 订单入队 V2 处理
- `POST /api/crm/orders/{order_id}/process-v2`：CRM 订单同步处理 V2
- `POST /api/v2/delivery-notices/{notice_id}/confirm`：确认发货通知并下推 OMS
- `POST /api/v2/delivery-notices/{notice_id}/replay-oms`：OMS 死信重放（需修复证据）
- `POST /api/v2/delivery-notices/{notice_id}/sync-oms-status`：手工同步 OMS 执行状态
- `POST /api/v2/oms/status-poll`：批量轮询 OMS 状态
- `GET /api/exceptions/{exception_id}/context`：异常 BFF 聚合上下文（订单、快照、附件、诊断、审计时间线）
- `POST /api/exceptions/{exception_id}/diagnosis-feedback`：AI 诊断反馈（采纳/修改/驳回）
- `POST /api/exceptions/{exception_id}/assign`：异常分派责任人
- `POST /api/exceptions/{exception_id}/reopen`：重新打开已关闭异常
- `GET /api/global-exception-ticker`：全局异常跑马灯（Critical/High 优先级）

### 配置管理接口

- `PUT /api/config/mail`：运行期更新邮箱、抄送、ZIP 和保留策略配置。
- `PUT /api/config/crm`：运行期更新 CRM 同步和一期范围配置。
- `PUT /api/config/oms`：运行期更新 OMS/WMS 接入配置（AppKey/Secret 自动加密存储）。
- `PUT /api/config/erp`：运行期更新 ERP 只读查询配置。
- `PUT /api/model-providers/active`：运行期更新 OpenAI 兼容模型配置。
- `POST /api/model-providers/test`：测试模型 Provider 连通性。
- `POST /api/model-providers/chat`：使用当前模型配置发起一次对话测试。
- `POST /api/e2e/tencent-mail/run`：使用 `bot.sales@jimuyida.com` 和 `bot.production@jimuyida.com` 运行真实腾讯企业邮箱 IMAP/SMTP 端到端测试；两个测试账号密码通过管理台运行期配置保存，不在代码中保存。

---

## 测试

```bash
# 确保已激活虚拟环境
source .venv/bin/activate

python3 -m pytest
```

本地浏览器端回归使用 Playwright：

```bash
npm install
npm run playwright:install
npm run test:e2e
```

默认会复用 `http://127.0.0.1:8000` 上已经运行的服务；未运行时会自动启动 `uvicorn`。可通过 `E2E_BASE_URL`、`E2E_ADMIN_USERNAME`、`E2E_ADMIN_PASSWORD` 覆盖测试目标和登录账号。
如果当前环境不允许 Playwright 子进程绑定端口，先手动启动后端，再运行：

```bash
npm run test:e2e:reuse
```

### V2 订单中台测试

```bash
# 确保已激活虚拟环境
source .venv/bin/activate

# 全流程端到端模拟（5 个业务场景，SQLite 内存库 + 全 Mock，不碰外部系统）
python3 scripts/v2_e2e_simulation.py

# 代码结构静态验证（270 项检查，纯标准库，对照设计文档逐项验证）
python3 scripts/v2_static_verification.py

# V2 单元测试（39 个用例，覆盖状态机、规则引擎、OMS 补偿、CRM 变更接管、AI 诊断）
pytest tests/test_order_middle_platform.py -v
pytest tests/test_order_middle_platform_lock_and_dlq.py -v
```
