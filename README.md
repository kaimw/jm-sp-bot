# 商务生产任务单智能体 MVP

本仓库当前实现第一版 MVP 闭环：后台配置、腾讯企业邮箱 IMAP/SMTP 适配、真实邮件入库队列、原始 EML 留存、Word/Excel/PDF/ZIP 附件解析、外部 OpenAI 兼容模型 Provider、订单任务单工作流、字段证据、缺字段补充、变更/取消分流、模板管理、生产反馈抄送策略、周报导出/入队、清理/备份和本地管理台。

## 本地运行

```bash
python3 -m pip install -e ".[test]"
```

```bash
python3 -m uvicorn backend.app.main:app --reload --host 0.0.0.0 --port 8000
```

打开：

```text
http://127.0.0.1:8000
```

默认登录账号为 `admin` / `admin`，可通过 `ADMIN_USERNAME`、`ADMIN_PASSWORD`、`AUTH_SECRET` 和 `AUTH_SESSION_SECONDS` 覆盖。
管理台“模拟订单”默认使用测试销售信箱 `bot.sales@jimuyida.com`。

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

生产 Compose 试运行：

```bash
cp .env.production.example .env.production
# 修改 .env.production 中的数据库密码、管理员密码、AUTH_SECRET、邮箱和模型密钥
docker compose -f docker-compose.prod.yml --env-file .env.production up --build -d
```

生产 Compose 会启动应用和 PostgreSQL，应用健康检查访问 `/health`。迁移内测 SQLite 数据到生产库时，先用上面的迁移脚本 dry-run，再加 `--execute` 写入；目标库已有数据时需要显式传 `--truncate-target`。

## 自维护 MVP

管理台“自维护”页面支持收集系统上下文、生成诊断、生成白名单配置修复草案、生成代码修复草案、生成代码交接包、运行白名单验证、查看维护时间线、查看维护会话详情、查看维护动作详情、回填补丁执行记录、记录补丁人工复核结论，以及归档已处理维护会话。会话列表默认隐藏归档记录，可通过“显示已归档会话”切换查看历史，并支持按状态、风险等级筛选和分页浏览；维护动作列表支持按动作类型和状态筛选，并支持分页浏览。配置修复必须输入管理员密码确认后才会写入运行期配置；代码修复草案不会在业务进程中直接改代码。

外部维护 runner 可读取最新代码修复草案、生成交接包、导出 Markdown 报告，并运行白名单验证命令：

```bash
python scripts/maintenance_runner.py list
python scripts/maintenance_runner.py handoff --action-id <maintenance_action_id>
python scripts/maintenance_runner.py export --action-id <maintenance_action_id>
python scripts/maintenance_runner.py validate --action-id <maintenance_action_id> --command "node --check backend/app/static/app.js"
python scripts/maintenance_runner.py complete --action-id <maintenance_action_id> --summary "补丁已完成，等待人工复核" --changed-file backend/app/main.py --test "python3 -m pytest"
python scripts/maintenance_runner.py review --action-id <maintenance_action_id> --decision ReviewAccepted --note "人工复核通过"
```

`handoff` 会在 `data/maintenance` 下生成 Markdown 和 JSON 交接包，包含修复草案、上下文、安全边界、推荐执行流程和 runner 命令；管理台也可以对代码修复动作一键生成交接包，并直接展开查看交接包内容。`validate` 会执行固定白名单命令，管理台也提供同等的“运行验证”入口。`complete` 用于外部维护者回填补丁摘要、改动文件、测试结果和残余风险，管理台也提供同等的“回填执行”入口；`review` 用于记录人工接受、驳回或继续修改结论，形成代码补丁闭环。runner 仅允许执行固定验证命令：`python3 -m compileall backend scripts`、`python3 -m pytest` 和 `node --check backend/app/static/app.js`。执行结果会写回维护动作并记录审计事件。

## 当前边界

1. 不在代码或文档中保存邮箱密码、模型 API Key。
2. 运行期可通过管理台/API 配置腾讯企业邮箱账号密码、IMAP/SMTP、模型 API Base/API Key、生产部门邮箱和任务单模板。
3. 附件解析当前覆盖 `.docx`、`.xlsx`、`.pdf`、`.zip`、`.txt`、`.csv`；ZIP 默认最大 100MB、最多解压 1 层。
4. MVP 保持“系统生成、商务确认发送”的边界；订单取消、已排产后变更、风险词命中、路由缺失、低质量答复均进入人工处理。

## 关键接口

- `POST /api/auth/login`：前台登录，默认账号 `admin`、密码 `admin`。
- `POST /api/auth/logout`：退出并清理会话 Cookie。
- `GET /api/auth/me`：检查当前会话状态。
- `POST /api/mailbox/sync`：从腾讯企业邮箱 IMAP 同步未读邮件并写入入库队列。
- 销售订单、销售补充、订单变更和订单取消邮件入库后，会自动生成 `SalesReceiptAck` 回执外发任务，通知发件人邮件已收到并排队处理中。
- 规则分类结果为 `NonTarget` 时，可启用当前模型配置进行 LLM 兜底分类和订单字段抽取；规则和 LLM 都判定为 `NonTarget` 时，邮件会进入异常队列，并尽量归类到当前订单沟通会话。
- 系统启动后默认运行自动邮件 worker，每 60 秒同步 IMAP、处理入库队列，并自动发送销售收件回执、初审未通过/待补充回复、生产疑问转销售邮件、生产疑问收件回执、销售答复后的更新版任务单、更新版任务单发送成功回执和最大轮次关闭通知；不会自动发送首次生产任务单、周报或其它待发邮件。
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
- `POST /api/exceptions/{exception_id}/apply-requirement-patch`：补齐订单字段并恢复生成任务单草稿。
- `POST /api/cleanup/preview`：预览过期非目标邮件和临时记录清理范围。
- `POST /api/cleanup/run`：执行清理，默认不清理有效订单邮件。
- `POST /api/backups/run`：生成 SQLite 数据和附件目录 ZIP 备份。
- `GET /api/audit-events`：查看关键动作审计日志。
- `PUT /api/config/mail`：运行期更新邮箱、抄送、ZIP 和保留策略配置。
- `PUT /api/model-providers/active`：运行期更新 OpenAI 兼容模型配置。
- `POST /api/model-providers/test`：测试模型 Provider 连通性。
- `POST /api/model-providers/chat`：使用当前模型配置发起一次对话测试。
- `POST /api/e2e/tencent-mail/run`：使用 `bot.sales@jimuyida.com` 和 `bot.production@jimuyida.com` 运行真实腾讯企业邮箱 IMAP/SMTP 端到端测试；两个测试账号密码通过管理台运行期配置保存，不在代码中保存。

## 测试

```bash
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
