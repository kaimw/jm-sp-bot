---
name: jm-sp-feature-design
description: Design new capabilities for the JM production-order agent, including PRD refinement, MVP slicing, email workflow scope, API shape, database/workflow impact, and rollout plans. Use when users ask for 需求分析, 技术方案, MVP 拆分, workflow design, or compare implementation options before coding.
---

# JM-SP Feature Design

## Goal
Produce an implementation-ready design for this production-order email agent with clear scope, business workflow impact, and validation criteria.

## Workflow
1. Restate the target outcome in one sentence.
2. Identify which business actor or loop is affected: sales, bot/business, production, finance, CEO, sales director.
3. Locate current evidence in code and docs before proposing changes.
4. Separate MVP behavior from reserved enterprise behavior.
5. Define API, data model, queue, and UI/admin impact.
6. Write acceptance criteria and rollout/rollback rules.

## Codebase Map
Start discovery with:
- `docs/production-order-agent-prd-review-v0.2.md` for reviewed business requirements.
- `docs/technical-solution.md` for architecture decisions.
- `docs/database-workflow-design.md` for entities and workflow.
- `docs/mvp-task-list.md` for delivery slicing.
- `backend/app/models.py` for database contracts.
- `backend/app/services/workflow.py` for task lifecycle and CC rules.
- `backend/app/services/mail_adapter.py` for Tencent IMAP/SMTP.
- `backend/app/services/attachment_parser.py` for Word/Excel/ZIP parsing.
- `backend/app/services/model_provider.py` for external model calls.
- `backend/app/main.py` for API surface.
- `backend/app/static/` for local admin UI.

Use:
```bash
rg -n "<topic keyword>" docs backend tests
```

## Domain Constraints
- Never put real mailbox passwords or model API keys in code, docs, tests, or committed examples.
- Tencent enterprise email is the default integration path: IMAP for inbound, SMTP for outbound.
- Default bot identity is `bot.market@jimuyida.com`, display name `市场部小J`, signature `积木易搭AI机器人`.
- Production department recipients and task template are admin configurable.
- Sales originator defaults to the original inbound requirement email sender.
- ZIP handling must enforce max size and max depth, and must never execute attachment contents.
- Model Provider must remain OpenAI-compatible and runtime configurable.
- Effective emails are retained permanently unless cleanup tooling explicitly targets eligible temporary or non-target data.

## Output Contract
Include:
- Problem statement
- Scope in and scope out
- Current-state evidence
- Proposed workflow and state changes
- API and data model impact
- Queue/background processing impact
- Security and secret-handling rules
- MVP acceptance criteria
- Validation plan
- Risks, mitigations, and rollback path

## Option Evaluation Criteria
Compare options by:
- Business correctness for the email loop
- Risk of sending wrong email or wrong CC
- Data retention and auditability
- Manual takeover path
- Implementation complexity
- Operability in local and enterprise deployment
- Rollback simplicity

## Red Flags
Escalate before coding if:
- A change may send real email automatically without an explicit operator action.
- A design stores plaintext secrets in tracked files.
- A parser change expands attachment handling without size/depth controls.
- A workflow change bypasses audit events or idempotency.
- The requirement needs DingTalk or WeCom production integration; MVP currently only reserves interfaces.
