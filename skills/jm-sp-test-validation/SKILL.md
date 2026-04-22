---
name: jm-sp-test-validation
description: Validate JM production-order agent changes across FastAPI, SQLAlchemy workflow state, Tencent email adapters, attachment parsing, model-provider behavior, static admin UI, secrets safety, and release readiness. Use when users ask to test, verify, review, or produce a validation pass.
---

# JM-SP Test Validation

## Goal
Give a reproducible verdict on whether a change is safe for the MVP email workflow.

## Validation Workflow
1. Define scope from changed files.
2. Run baseline compile and tests.
3. Add targeted tests for changed workflow states, parsers, queues, or APIs.
4. Smoke test running endpoints when the server is started.
5. Check secrets and real-send risk.
6. Report findings before summaries when performing a review.

## Baseline Commands
Run:
```bash
python3 -m compileall backend
python3 -m pytest
```

When the server is running:
```bash
curl -s http://127.0.0.1:8000/health
curl -s http://127.0.0.1:8000/api/config
curl -s http://127.0.0.1:8000/api/jobs
curl -s http://127.0.0.1:8000/api/attachments
```

## Scenario Matrix
Cover the relevant subset:
- Order email creates a task draft.
- Missing fields create review exceptions.
- Approval creates a pending production task email.
- Production confirmation closes the task and applies default CC.
- Production rejection reopens the question loop and applies default CC.
- IMAP parser handles MIME headers, plain text, HTML fallback, recipients, and attachments.
- Attachment parser handles Word, Excel, ZIP, unsupported types, and parser failures.
- Queue job completes or records failure.
- Model provider builds OpenAI-compatible payloads and resolves runtime credentials.
- Admin UI escapes untrusted backend text.

## Secrets and Safety Checks
Search for real sensitive values or accidental placeholders before finalizing:
```bash
rg -n "password|api[_-]?key|secret|token|credential" .
```

Expected outcome:
- Real secrets must not appear in tracked files.
- Empty placeholders in `.env.example` are acceptable.
- Runtime config API must mask secret values in reads.

## Real Integration Checks
Only run real IMAP/SMTP/model calls when the user explicitly asks and runtime secrets are configured.

For real integration, verify:
- IMAP sync imports a limited number of messages.
- Sync creates queue jobs but does not auto-send.
- Queue processing creates expected task drafts.
- SMTP sends only selected pending outbound jobs.
- Failures record useful error messages without leaking secrets.

## Reporting Format
Use this order:
1. Findings, high to low severity
2. Open questions or assumptions
3. Pass/fail verdict
4. Commands run
5. Residual risks
