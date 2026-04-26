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

Docker 启动：

```bash
cp .env.example .env
docker compose up --build
```

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
