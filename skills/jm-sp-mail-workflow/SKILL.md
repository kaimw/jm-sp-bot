---
name: jm-sp-mail-workflow
description: Work on Tencent enterprise email IMAP/SMTP ingestion, outbound mail queues, production-task email generation, Word/Excel/ZIP attachment parsing, and model-assisted email understanding for the JM production-order agent. Use for 邮箱接入, 邮件入库, 邮件发送, 附件解析, 队列处理, or model extraction tasks.
---

# JM-SP Mail Workflow

## Goal
Maintain a reliable and auditable email-to-production-task loop without leaking secrets or sending unintended mail.

## Primary Flow
1. `POST /api/mailbox/sync` calls IMAP sync in `mail_adapter.py`.
2. Raw MIME is parsed into sender, recipients, subject, text body, and attachments.
3. `MailMessage` is stored with a dedupe key.
4. Attachments are saved and parsed into `AttachmentAsset`.
5. A `ProcessingJob` with `process_inbound_mail` is queued.
6. `POST /api/jobs/run-pending` creates an order requirement and task draft when classification passes.
7. Operator approves task draft.
8. `OutboundMailJob` is created for production department.
9. `POST /api/outbound-mails/send-pending` sends pending mail through SMTP.
10. Production feedback records confirmation or rejection and creates the next outbound job.

## Key Files
- `backend/app/services/mail_adapter.py`
- `backend/app/services/attachment_parser.py`
- `backend/app/services/jobs.py`
- `backend/app/services/workflow.py`
- `backend/app/services/model_provider.py`
- `backend/app/models.py`
- `tests/test_workflow.py`

## Tencent Mail Rules
- Defaults: IMAP `imap.exmail.qq.com:993`, SMTP `smtp.exmail.qq.com:465`.
- Credentials are runtime config keys, especially `bot_email_password`, or environment variables.
- Never hardcode or print real credentials in tests, logs, docs, or final answers.
- Real sending should happen only through explicit operator action or an explicitly requested automation.

## Attachment Rules
- Supported MVP parsing: `.docx`, `.xlsx`, `.zip`, `.txt`, `.csv`.
- `.zip` defaults: max 100MB, max extraction depth 1.
- Preserve unsupported files as skipped metadata rather than failing the whole email.
- Never execute macros, scripts, binaries, or ZIP contents.
- Keep parser failures per attachment and let the mail continue when safe.

## Model Provider Rules
- Default provider is OpenAI-compatible.
- API base, model name, and key must be runtime configurable.
- Use `config:model_api_key` or `env:MODEL_API_KEY`; never tracked plaintext.
- Log model calls through `ModelCallLog`, including success/failure and latency.
- Keep deterministic heuristic extraction as fallback for MVP.

## Business Rules to Preserve
- Production confirmed mail default CC: CEO, sales originator, `jinlei@jimuyida.com`.
- Production rejected mail default CC: `jinlei@jimuyida.com`.
- Sales originator defaults to inbound requirement sender.
- Production department recipients are admin configured.
- Task templates are admin configurable and versioned.

## Validation Checklist
Use synthetic emails and attachments first:
- MIME with plain text body.
- MIME with `.docx` attachment.
- MIME with `.xlsx` attachment.
- ZIP containing supported child files.
- Duplicate Message-ID does not create duplicate inbound work.
- Queue job changes from Pending to Completed or Failed.
- SMTP tests do not send real mail unless explicitly intended.

Run:
```bash
python3 -m pytest
```

## Red Flags
Stop and clarify if:
- The request would mark all unseen mailbox messages as processed without a recovery path.
- The request sends real emails automatically on inbound sync.
- Attachment parsing needs unsupported formats such as legacy `.doc` or macro-enabled Office files.
- The model is expected to be the only classifier without deterministic fallback.

