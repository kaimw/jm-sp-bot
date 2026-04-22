---
name: jm-sp-doc-writing
description: Write or update PRD, technical solution, MVP task lists, database/workflow designs, operational runbooks, and release notes for the JM production-order agent. Use when users ask for PRD 文档, 技术方案, 数据库设计, 工作流设计, 周报/运行说明, or review-ready Chinese documentation.
---

# JM-SP Doc Writing

## Goal
Create concise, review-ready documentation that matches the implemented system and the approved production-order email workflow.

## Evidence-First Workflow
1. Read existing docs and code before writing new claims.
2. Preserve confirmed business decisions unless the user changes them.
3. Separate current MVP from reserved future integrations.
4. Include exact workflow states, data entities, APIs, and operational constraints.
5. Add diagrams when they reduce ambiguity.
6. Verify no real secrets are included.

## Recommended Sources
- `docs/production-order-agent-prd-review-v0.2.md`
- `docs/technical-solution.md`
- `docs/database-workflow-design.md`
- `docs/mvp-task-list.md`
- `README.md`
- `backend/app/models.py`
- `backend/app/services/workflow.py`
- `backend/app/services/mail_adapter.py`
- `backend/app/main.py`

Use:
```bash
rg -n "<term>" docs README.md backend
```

## Required Content for Technical Docs
Include:
- Background and goals
- Scope in and scope out
- Actor and email workflow
- Runtime configuration and secret handling
- Data model summary
- Queue and state transitions
- Attachment parsing policy
- Model provider policy
- Admin UI/API operations
- Acceptance criteria
- Risks and mitigations
- Open questions

## Mermaid Guidance
Use Mermaid for workflow, sequence, ER, and state diagrams when documenting:
- Sales -> bot/business -> production loop
- Production rejection and reissue loop
- IMAP inbound queue processing
- SMTP outbound queue sending
- Database entity relationships

Keep labels concrete and Chinese when the document is Chinese.

## Business Facts to Preserve
- Enterprise email: Tencent IMAP/SMTP.
- Default bot email: `bot.market@jimuyida.com`.
- Default display name: `市场部小J`.
- Default signature: `积木易搭AI机器人`.
- CEO email default: `dingyong@jimuyida.com`.
- Ops CC default: `jinlei@jimuyida.com`.
- Model default: title `Dify deepseekV3`, provider `openai`, model `DeepSeek-V3`, API base `http://192.168.10.55:5000/v1`.
- Do not document actual mailbox password or model API key.
- ZIP defaults: 100MB and 1 extraction level.
- DingTalk and WeCom are reserved integrations, not MVP hard requirements.

## Final Checklist
- Claims match current code or are clearly marked as planned.
- No plaintext real secrets.
- MVP and future scope are separated.
- APIs and data entities are named consistently.
- Open questions are actionable.

