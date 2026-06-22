---
name: jm-sp-feature-development
description: Implement backend, workflow, queue, parser, model-provider, and admin UI changes for the JM production-order agent. Use when users ask to code MVP features, wire FastAPI endpoints, update SQLAlchemy models, modify the static admin console, or extend the email/task workflow.
---

# JM-SP Feature Development

## Goal
Ship a working, scoped change that preserves email safety, task idempotency, and runtime configurability.

## Delivery Workflow
1. Identify the smallest business slice that satisfies the request.
2. Search existing models, services, endpoints, and tests before editing.
3. Keep business logic in `backend/app/services/`; keep `main.py` as API orchestration.
4. Prefer additive database changes and avoid destructive changes to local data.
5. Update admin UI only when the feature needs operator control or visibility.
6. Add or update focused tests.
7. Run compile and test checks.

## Codebase Map
- API entry: `backend/app/main.py`
- Schemas: `backend/app/schemas.py`
- Database setup: `backend/app/database.py`
- Models: `backend/app/models.py`
- Defaults/config bootstrap: `backend/app/services/bootstrap.py`
- Workflow: `backend/app/services/workflow.py`
- Mail integration: `backend/app/services/mail_adapter.py`
- Attachments: `backend/app/services/attachment_parser.py`, `backend/app/services/storage.py`
- Model calls: `backend/app/services/model_provider.py`
- Queue jobs: `backend/app/services/jobs.py`
- Static admin UI: `backend/app/static/index.html`, `app.js`, `styles.css`
- Tests: `tests/test_workflow.py`

Use:
```bash
rg -n "<symbol or business term>" backend tests docs
```

## Common Edit Patterns
### Display time
- Persist database timestamps in UTC.
- Every user-facing displayed time in the system must be rendered as Beijing time (`Asia/Shanghai`), including admin pages, flow cards, queue/audit pages, dashboards, emails, diagnostics, exports, and generated notification text.
- Do not insert raw `created_at`, `updated_at`, `sent_at`, `synced_at`, `started_at`, `finished_at`, or other timestamp strings into visible UI or outbound text. Route them through the shared frontend `formatTime()` helper or backend Beijing-time helper.
- Backend API timestamps may stay UTC/ISO for contracts, but must include timezone information or be parsed by the frontend as UTC before display.

### Add an API endpoint
- Add request/response schema in `schemas.py` when structured input is needed.
- Implement orchestration in `main.py`.
- Keep reusable behavior in a service module.
- Commit DB changes only after all service operations succeed.

### Add workflow behavior
- Update `workflow.py`.
- Preserve idempotency keys for outbound jobs.
- Add audit events for state transitions.
- Keep CC rules explicit and testable.

### Add integration behavior
- Keep real IMAP/SMTP/model calls behind explicit endpoints or operator actions.
- Read secrets from runtime config or environment, never from tracked source defaults.
- In tests, use synthetic MIME/model payloads instead of network calls.

### Add attachment behavior
- Route file type logic through `attachment_parser.py`.
- Enforce ZIP size/depth limits.
- Store parse status, parse error, text preview, and archive path metadata.

### Update admin UI
- Reflect backend state with existing REST APIs.
- Escape untrusted text before inserting into HTML.
- Keep controls practical for operators: sync, run queue, send pending, configure runtime.

## Safety Rules
- Do not write real `bot_email_password` or model API keys to tracked files.
- Do not auto-send real email during tests or local validation.
- Do not execute attachment content.
- Do not broaden cleanup rules for effective emails without explicit product approval.
- Do not silently change default recipients or CC behavior.

## Validation Minimum
Run:
```bash
python3 -m compileall backend
python3 -m pytest
```

For service smoke checks when the app is running:
```bash
curl -s http://127.0.0.1:8000/health
curl -s http://127.0.0.1:8000/api/config
```

## Final Output Checklist
- State changed files by area.
- List tested commands.
- Identify any behavior requiring runtime secrets or real mailbox access.
- Call out residual risks, especially real email delivery or model connectivity.
