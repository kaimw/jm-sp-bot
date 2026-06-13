from __future__ import annotations

import email
import imaplib
import smtplib
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from email.header import Header, decode_header
from email.message import EmailMessage
from email.policy import default
from email.utils import encode_rfc2231, formataddr, getaddresses, parsedate_to_datetime
from html import unescape
import re

from sqlalchemy.orm import Session

from backend.app.config import settings
from backend.app.models import AttachmentAsset, MailMessage, OutboundMailJob
from backend.app.services.attachment_parser import ParsedAttachment, parse_attachment
from backend.app.services.jsonutil import as_list, dumps
from backend.app.services.mail_throttle import (
    mail_login_interval_seconds,
    reserve_mail_login,
    reserve_mail_send,
    reserve_mail_send_slot,
)
from backend.app.services.parser import classify_mail, normalize_latest_reply
from backend.app.services.storage import save_attachment
from backend.app.services.task_scheduler import RetryPolicy, next_retry_at
from backend.app.services.workflow import (
    add_audit,
    bot_enabled,
    create_inbound_mail,
    enqueue_job,
    enqueue_requirement_supplement_receipt,
    enqueue_sales_receipt_ack,
    enqueue_sales_reply_reissue_receipt,
    get_config,
    record_exception_case,
)

OUTBOUND_EMAIL_POLICY = default.clone(max_line_length=998)


@dataclass
class IncomingAttachment:
    file_name: str
    content_type: str
    content: bytes


@dataclass
class IncomingEmail:
    message_id: str
    from_address: str
    to_addresses: list[str]
    cc_addresses: list[str]
    subject: str
    body_text: str
    received_at: datetime | None = None
    attachments: list[IncomingAttachment] = field(default_factory=list)
    raw_bytes: bytes = b""


def decode_mime(value: str | None) -> str:
    if not value:
        return ""
    parts: list[str] = []
    for content, charset in decode_header(value):
        if isinstance(content, bytes):
            parts.append(decode_header_bytes(content, charset))
        else:
            parts.append(repair_mojibake_text(content))
    return "".join(parts)


def decode_header_bytes(content: bytes, charset: str | None) -> str:
    candidates = []
    if charset:
        candidates.append(str(charset).strip().lower())
    candidates.extend(["utf-8", "gb18030", "gbk", "big5", "latin-1"])
    seen: set[str] = set()
    fallback = content.decode("utf-8", errors="replace")
    for candidate in candidates:
        if not candidate or candidate in seen:
            continue
        seen.add(candidate)
        try:
            decoded = content.decode(candidate)
        except (LookupError, UnicodeDecodeError):
            continue
        if "\ufffd" not in decoded:
            return decoded
        fallback = decoded
    return fallback


def has_cjk(text: str) -> bool:
    return any("\u4e00" <= char <= "\u9fff" for char in text)


def repair_mojibake_text(value: str) -> str:
    text = str(value or "")
    if not text:
        return ""
    if any(0xDC80 <= ord(char) <= 0xDCFF for char in text):
        raw = text.encode("ascii", errors="surrogateescape")
        for target_encoding in ("utf-8", "gb18030", "gbk", "big5"):
            try:
                repaired = raw.decode(target_encoding)
            except UnicodeDecodeError:
                continue
            if repaired and "\ufffd" not in repaired:
                return repaired
    latin1_like = sum(1 for char in text if 0x80 <= ord(char) <= 0xFF)
    if latin1_like >= 2 or any(marker in text for marker in ("Ã", "Â", "�")):
        for source_encoding in ("latin-1", "cp1252"):
            try:
                raw = text.encode(source_encoding)
            except UnicodeEncodeError:
                continue
            for target_encoding in ("utf-8", "gb18030", "gbk", "big5"):
                try:
                    repaired = raw.decode(target_encoding)
                except UnicodeDecodeError:
                    continue
                if repaired != text and ("\ufffd" not in repaired) and (has_cjk(repaired) or target_encoding == "utf-8"):
                    return repaired
    return text


def decode_attachment_filename(part) -> str:
    filename = decode_mime(part.get_filename())
    if filename and "\ufffd" not in filename:
        return filename
    for header_name, param_name in (("Content-Disposition", "filename"), ("Content-Type", "name")):
        raw_header_value = next((value for name, value in part.raw_items() if name.lower() == header_name.lower()), "")
        matched = re.search(rf"{param_name}\*?\s*=\s*(?:\"([^\"]*)\"|([^;\s]+))", raw_header_value, flags=re.IGNORECASE)
        if not matched:
            continue
        raw_value = matched.group(1) if matched.group(1) is not None else matched.group(2)
        repaired = decode_mime(raw_value)
        if repaired and "\ufffd" not in repaired:
            return repaired
    for header_name, param_name in (("Content-Disposition", "filename"), ("Content-Type", "name")):
        value = part.get_param(param_name, header=header_name, unquote=True)
        fallback = decode_mime(value)
        if fallback:
            return fallback
    return filename or ""


def apply_attachment_filename_compatibility(part, file_name: str, content_type: str) -> None:
    encoded_word = Header(file_name, "utf-8", maxlinelen=1000).encode()
    rfc2231 = encode_rfc2231(file_name, "utf-8")
    part.replace_header("Content-Type", f'{content_type}; name="{encoded_word}"')
    part.replace_header("Content-Disposition", f'attachment; filename="{encoded_word}"; filename*={rfc2231}')


def strip_html(html: str) -> str:
    text = re.sub(r"(?is)<(script|style).*?>.*?</\1>", "", html)
    text = re.sub(r"(?s)<br\s*/?>", "\n", text)
    text = re.sub(r"(?s)</p\s*>", "\n", text)
    text = re.sub(r"(?s)<.*?>", " ", text)
    text = unescape(text)
    return re.sub(r"[ \t]+", " ", text).strip()


def normalize_bot_display_name(value: str) -> str:
    # Compatibility shim: keep old persisted config values but emit new sender name.
    if (value or "").strip() == "市场部小J":
        return "商务部小J"
    return (value or "").strip() or "商务部小J"


def parse_email_date(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        parsed = parsedate_to_datetime(value)
    except (TypeError, ValueError, IndexError, OverflowError):
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def backfill_mail_received_at(session: Session, *, limit: int = 1000) -> int:
    rows = (
        session.query(MailMessage, AttachmentAsset.storage_ref)
        .outerjoin(
            AttachmentAsset,
            (AttachmentAsset.mail_id == MailMessage.id)
            & (
                (AttachmentAsset.content_type == "message/rfc822")
                | (AttachmentAsset.file_name.ilike("%.eml"))
            ),
        )
        .filter(MailMessage.received_at.is_(None))
        .order_by(MailMessage.created_at.desc())
        .limit(limit)
        .all()
    )
    updated = 0
    for mail, storage_ref in rows:
        received_at = None
        if storage_ref:
            try:
                with open(storage_ref, "rb") as file:
                    msg = email.message_from_binary_file(file, policy=default)
                received_at = parse_email_date(msg.get("Date"))
            except OSError:
                received_at = None
        mail.received_at = received_at or mail.created_at
        updated += 1
    if updated:
        session.flush()
    return updated


def parse_email_bytes(raw: bytes) -> IncomingEmail:
    msg = email.message_from_bytes(raw, policy=default)
    subject = decode_mime(msg.get("Subject"))
    from_address = getaddresses([msg.get("From", "")])[0][1] if msg.get("From") else ""
    to_addresses = [addr for _, addr in getaddresses(msg.get_all("To", [])) if addr]
    cc_addresses = [addr for _, addr in getaddresses(msg.get_all("Cc", [])) if addr]
    message_id = msg.get("Message-ID", "")
    body_text = ""
    html_body = ""
    attachments: list[IncomingAttachment] = []

    for part in msg.walk():
        content_disposition = part.get_content_disposition()
        content_type = part.get_content_type()
        if part.is_multipart():
            continue
        if content_disposition == "attachment":
            file_name = decode_attachment_filename(part) or "attachment.bin"
            attachments.append(
                IncomingAttachment(
                    file_name=file_name,
                    content_type=content_type,
                    content=part.get_payload(decode=True) or b"",
                )
            )
            continue
        if content_type == "text/plain" and not body_text:
            body_text = part.get_content()
        elif content_type == "text/html" and not html_body:
            html_body = part.get_content()

    if not body_text and html_body:
        body_text = strip_html(html_body)
    body_text = normalize_latest_reply(body_text)
    return IncomingEmail(
        message_id=message_id,
        from_address=from_address,
        to_addresses=to_addresses,
        cc_addresses=cc_addresses,
        subject=subject,
        body_text=body_text,
        received_at=parse_email_date(msg.get("Date")),
        attachments=attachments,
        raw_bytes=raw,
    )


def sync_imap_mailbox(session: Session, *, limit: int = 20) -> dict:
    if not bot_enabled(session):
        return {"imported": 0, "queued": 0, "skipped": "bot is disabled"}

    host = get_config(session, "imap_host", "imap.exmail.qq.com")
    port = int(get_config(session, "imap_port", "993"))
    username = get_config(session, "bot_email", "bot.market@jimuyida.com")
    password = get_config(session, "bot_email_password", "")
    if not password:
        raise RuntimeError("bot_email_password is not configured")

    imported = 0
    queued = 0
    reserve_mail_login("imap", username, interval_seconds=mail_login_interval_seconds(session))
    with imaplib.IMAP4_SSL(host, port, timeout=settings.mail_imap_timeout_seconds) as imap:
        imap.login(username, password)
        imap.select("INBOX")
        status, search_data = imap.search(None, "UNSEEN")
        if status != "OK" or not search_data:
            return {"imported": 0, "queued": 0, "searched": 0}
        all_message_nums = search_data[0].split()
        # IMAP returns message sequence numbers in ascending order. Use the newest
        # unseen messages first so old unread mail cannot starve fresh orders.
        message_nums = list(reversed(all_message_nums[-limit:]))
        for message_num in message_nums:
            fetch_status, fetch_data = imap.fetch(message_num, "(RFC822)")
            if fetch_status != "OK":
                continue
            if not fetch_data or not isinstance(fetch_data[0], tuple):
                continue
            incoming = parse_email_bytes(fetch_data[0][1])
            mail = store_incoming_email(session, incoming)
            imported += 1
            enqueue_job(session, "process_inbound_mail", {"mail_id": mail.id})
            queued += 1
        imap.logout()
    return {"imported": imported, "queued": queued, "searched": len(all_message_nums)}


def store_incoming_email(session: Session, incoming: IncomingEmail) -> MailMessage:
    dedupe_source = incoming.message_id or f"{incoming.from_address}|{incoming.subject}|{incoming.body_text[:80]}"
    mail = create_inbound_mail(
        session,
        from_address=incoming.from_address,
        subject=incoming.subject,
        body_text=incoming.body_text,
        received_at=incoming.received_at,
        dedupe_key=f"imap:{dedupe_source}",
    )
    mail.to_json = dumps(incoming.to_addresses)
    mail.cc_json = dumps(incoming.cc_addresses)
    existing_assets = session.query(AttachmentAsset).filter_by(mail_id=mail.id).count()
    if existing_assets:
        enqueue_sales_receipt_ack(session, mail)
        return mail
    if incoming.raw_bytes:
        save_raw_email(session, mail.id, incoming.raw_bytes)
    for attachment in incoming.attachments:
        save_and_parse_attachment(session, mail.id, attachment.file_name, attachment.content_type, attachment.content)
    attachment_text = "\n".join(
        asset.extracted_text or ""
        for asset in session.query(AttachmentAsset).filter_by(mail_id=mail.id).all()
        if asset.extracted_text
    )
    if attachment_text:
        classification, confidence = classify_mail(mail.subject, f"{mail.body_text}\n{attachment_text}", mail.from_address)
        if confidence > mail.classification_confidence:
            mail.classification = classification
            mail.classification_confidence = confidence
    enqueue_sales_receipt_ack(session, mail)
    return mail


def save_raw_email(session: Session, mail_id: str, raw_bytes: bytes) -> AttachmentAsset:
    storage_ref, digest = save_attachment(f"{mail_id}.eml", raw_bytes)
    asset = AttachmentAsset(
        mail_id=mail_id,
        file_name=f"{mail_id}.eml",
        content_type="message/rfc822",
        file_size=len(raw_bytes),
        file_hash=digest,
        storage_ref=storage_ref,
        parse_status="Stored",
    )
    session.add(asset)
    session.flush()
    return asset


def save_and_parse_attachment(
    session: Session,
    mail_id: str,
    file_name: str,
    content_type: str,
    content: bytes,
    *,
    parent_attachment_id: str | None = None,
    archive_path: str | None = None,
    archive_depth: int | None = None,
) -> AttachmentAsset:
    storage_ref, digest = save_attachment(file_name, content)
    max_zip_bytes = int(get_config(session, "zip_max_bytes", "104857600"))
    max_zip_depth = int(get_config(session, "zip_max_depth", "1"))
    parsed = parse_attachment(file_name, content, max_zip_bytes=max_zip_bytes, max_depth=max_zip_depth, depth=archive_depth or 0)
    asset = AttachmentAsset(
        mail_id=mail_id,
        parent_attachment_id=parent_attachment_id,
        file_name=file_name,
        content_type=content_type,
        file_size=len(content),
        file_hash=digest,
        storage_ref=storage_ref,
        parse_status=parsed.status,
        extracted_text=parsed.text,
        parse_error=parsed.error,
        archive_path=archive_path or parsed.archive_path,
        archive_depth=archive_depth if archive_depth is not None else parsed.archive_depth,
    )
    session.add(asset)
    session.flush()
    if parsed.status == "Failed":
        record_attachment_parse_exception(session, asset, parsed.error or "attachment parse failed")
    for child in parsed.children:
        child_asset = AttachmentAsset(
                mail_id=mail_id,
                parent_attachment_id=asset.id,
                file_name=child.file_name,
                content_type="application/octet-stream",
                file_size=0,
                file_hash="",
                storage_ref=storage_ref,
                parse_status=child.status,
                extracted_text=child.text,
                parse_error=child.error,
                archive_path=child.archive_path,
                archive_depth=child.archive_depth,
        )
        session.add(child_asset)
        session.flush()
        if child.status == "Failed":
            record_attachment_parse_exception(session, child_asset, child.error or "attachment parse failed")
    return asset


def record_attachment_parse_exception(session: Session, asset: AttachmentAsset, error: str) -> None:
    detail = {
        "source_mail_id": asset.mail_id,
        "attachment_id": asset.id,
        "file_name": asset.file_name,
        "error": error[:1000],
    }
    record_exception_case(
        session,
        exception_type="AttachmentParseFailed",
        severity="Medium",
        detail=detail,
        source_mail_id=asset.mail_id,
    )
    add_audit(session, "AttachmentParseFailed", "AttachmentAsset", asset.id, detail)


# 最大自动重试次数，超出后标记 Failed 并进异常队列
OUTBOUND_MAX_AUTO_RETRIES = 5

# 邮件优先级常量（越小越高）
OUTBOUND_PRIORITY_RECEIPT = 10       # 收件回执、生产疑问回执
OUTBOUND_PRIORITY_BUSINESS = 20      # 业务推进（补充请求、疑问转发）
OUTBOUND_PRIORITY_TASK = 30          # 任务单
OUTBOUND_PRIORITY_NOTIFY = 40        # 通知类（默认）
OUTBOUND_PRIORITY_LOW = 60           # 低优先级（周报、告警）

MAIL_TYPE_PRIORITY: dict[str, int] = {
    "SalesReceiptAck": OUTBOUND_PRIORITY_RECEIPT,
    "ProductionQuestionReceipt": OUTBOUND_PRIORITY_RECEIPT,
    "ProductionConfirmationReceipt": OUTBOUND_PRIORITY_RECEIPT,
    "RequirementSupplementRequest": OUTBOUND_PRIORITY_BUSINESS,
    "ProductionQuestionForward": OUTBOUND_PRIORITY_BUSINESS,
    "SalesReplyTaskReissue": OUTBOUND_PRIORITY_BUSINESS,
    "RequirementSupplementTaskIssue": OUTBOUND_PRIORITY_BUSINESS,
    "TaskIssue": OUTBOUND_PRIORITY_TASK,
    "SalesReplyReissueReceipt": OUTBOUND_PRIORITY_TASK,
    "RequirementSupplementAcceptedReceipt": OUTBOUND_PRIORITY_TASK,
    "WeeklyReport": OUTBOUND_PRIORITY_LOW,
    "OutboundAlert": OUTBOUND_PRIORITY_LOW,
}


def _outbound_priority_for(mail_type: str) -> int:
    """根据邮件类型返回优先级，未知类型默认 NOTIFY(40)。"""
    return MAIL_TYPE_PRIORITY.get(mail_type, OUTBOUND_PRIORITY_NOTIFY)


def _next_retry_at(attempt_count: int) -> datetime:
    """指数退避：第 n 次失败后，等待 min(2^n × 60s, 3600s) 重试。"""
    return next_retry_at(attempt_count, RetryPolicy(base_delay_seconds=120, multiplier=2, max_delay_seconds=3600))


def mark_outbound_failure(session: Session, job: OutboundMailJob, error: str) -> None:
    """向后兼容：直接标记 Failed，不重试（用于收件人缺失等无法恢复的错误）。"""
    job.status = "Failed"
    job.last_error = error[:1000]
    _clear_outbound_lock(job)
    detail = {
        "outbound_job_id": job.id,
        "mail_type": job.mail_type,
        "subject": job.subject,
        "to": as_list(job.to_json),
        "cc": as_list(job.cc_json),
        "error": error[:1000],
    }
    record_exception_case(
        session,
        related_task_id=job.related_task_id,
        exception_type="OutboundMailSendFailed",
        severity="High",
        detail=detail,
    )
    add_audit(session, "OutboundMailSendFailed", "OutboundMailJob", job.id, detail)


def mark_outbound_retry_or_fail(session: Session, job: OutboundMailJob, error: str) -> None:
    """SMTP 发送失败时：指数退避自动重试；超过最大次数后转 Failed 并记录异常。"""
    job.attempt_count = (job.attempt_count or 0) + 1
    job.last_error = error[:1000]
    if job.attempt_count <= OUTBOUND_MAX_AUTO_RETRIES:
        # 保持 Pending 状态，设置下次可重试时间
        job.status = "Pending"
        job.next_retry_at = _next_retry_at(job.attempt_count)
        _clear_outbound_lock(job)
        add_audit(
            session,
            "OutboundMailRetryScheduled",
            "OutboundMailJob",
            job.id,
            {
                "attempt_count": job.attempt_count,
                "next_retry_at": job.next_retry_at.isoformat(),
                "error": error[:500],
            },
        )
    else:
        # 超过最大重试次数，标记失败并进异常队列
        job.status = "Failed"
        _clear_outbound_lock(job)
        detail = {
            "outbound_job_id": job.id,
            "mail_type": job.mail_type,
            "subject": job.subject,
            "to": as_list(job.to_json),
            "cc": as_list(job.cc_json),
            "attempt_count": job.attempt_count,
            "error": error[:1000],
        }
        record_exception_case(
            session,
            related_task_id=job.related_task_id,
            exception_type="OutboundMailSendFailed",
            severity="High",
            detail=detail,
        )
        add_audit(session, "OutboundMailSendFailed", "OutboundMailJob", job.id, detail)


def _clear_outbound_lock(job: OutboundMailJob) -> None:
    job.locked_by = None
    job.locked_until = None
    job.sending_started_at = None


def _release_claimed_jobs(session: Session, jobs: list[OutboundMailJob], *, next_retry_at: datetime | None = None) -> None:
    for job in jobs:
        if job.status == "Sending":
            job.status = "Pending"
            if next_retry_at is not None:
                job.next_retry_at = next_retry_at
            _clear_outbound_lock(job)
    session.commit()


def recover_stale_outbound_sending(session: Session) -> int:
    now = datetime.now(timezone.utc)
    stale_jobs = (
        session.query(OutboundMailJob)
        .filter(
            OutboundMailJob.status == "Sending",
            OutboundMailJob.locked_until.is_not(None),
            OutboundMailJob.locked_until < now,
        )
        .all()
    )
    for job in stale_jobs:
        job.status = "SendUnknown"
        job.last_error = "send lease expired before final SMTP result was recorded"
        _clear_outbound_lock(job)
        detail = {
            "outbound_job_id": job.id,
            "mail_type": job.mail_type,
            "subject": job.subject,
            "message": job.last_error,
        }
        record_exception_case(
            session,
            related_task_id=job.related_task_id,
            exception_type="OutboundMailSendUnknown",
            severity="High",
            detail=detail,
        )
        add_audit(session, "OutboundMailSendUnknown", "OutboundMailJob", job.id, detail)
    return len(stale_jobs)


def _due_outbound_filter(now: datetime):
    return (OutboundMailJob.next_retry_at.is_(None)) | (OutboundMailJob.next_retry_at <= now)


def claim_outbound_jobs(
    session: Session,
    *,
    limit: int,
    mail_types: set[str] | None = None,
    priority_lt: int | None = None,
    job_ids: list[str] | None = None,
    worker_id: str | None = None,
) -> list[OutboundMailJob]:
    recover_stale_outbound_sending(session)
    now = datetime.now(timezone.utc)
    worker = worker_id or f"outbound-{uuid.uuid4()}"
    query = session.query(OutboundMailJob).filter(
        OutboundMailJob.status == "Pending",
        _due_outbound_filter(now),
    )
    if mail_types is not None:
        query = query.filter(OutboundMailJob.mail_type.in_(mail_types))
    if priority_lt is not None:
        query = query.filter(OutboundMailJob.priority < priority_lt)
    if job_ids is not None:
        query = query.filter(OutboundMailJob.id.in_(job_ids))
    ids = [
        row.id
        for row in query.order_by(OutboundMailJob.priority, OutboundMailJob.created_at).limit(limit).all()
    ]
    if not ids:
        session.flush()
        return []
    locked_until = now + timedelta(seconds=settings.outbound_send_lease_seconds)
    claimed_count = (
        session.query(OutboundMailJob)
        .filter(OutboundMailJob.id.in_(ids), OutboundMailJob.status == "Pending", _due_outbound_filter(now))
        .update(
            {
                "status": "Sending",
                "locked_by": worker,
                "locked_until": locked_until,
                "sending_started_at": now,
            },
            synchronize_session=False,
        )
    )
    session.commit()
    if claimed_count == 0:
        return []
    session.expire_all()
    return (
        session.query(OutboundMailJob)
        .filter(OutboundMailJob.locked_by == worker, OutboundMailJob.status == "Sending")
        .order_by(OutboundMailJob.priority, OutboundMailJob.created_at)
        .all()
    )


def send_pending_smtp(session: Session, *, limit: int = 20) -> dict:
    host = get_config(session, "smtp_host", "smtp.exmail.qq.com")
    port = int(get_config(session, "smtp_port", "465"))
    username = get_config(session, "bot_email", "bot.market@jimuyida.com")
    password = get_config(session, "bot_email_password", "")
    display_name = normalize_bot_display_name(get_config(session, "bot_display_name", "商务部小J"))
    jobs = claim_outbound_jobs(session, limit=min(max(1, limit), 1))
    return send_outbound_jobs_with_account(
        session,
        jobs,
        host=host,
        port=port,
        username=username,
        password=password,
        display_name=display_name,
    )


def send_outbound_jobs_smtp(session: Session, job_ids: list[str], *, include_generated_followups: bool = False) -> dict:
    host = get_config(session, "smtp_host", "smtp.exmail.qq.com")
    port = int(get_config(session, "smtp_port", "465"))
    username = get_config(session, "bot_email", "bot.market@jimuyida.com")
    password = get_config(session, "bot_email_password", "")
    display_name = normalize_bot_display_name(get_config(session, "bot_display_name", "商务部小J"))
    jobs = claim_outbound_jobs(session, limit=min(max(1, len(job_ids)), 1), job_ids=job_ids)
    return send_outbound_jobs_with_account(
        session,
        jobs,
        host=host,
        port=port,
        username=username,
        password=password,
        display_name=display_name,
        include_generated_followups=include_generated_followups,
    )


AUTO_WORKFLOW_MAIL_TYPES = {
    "SalesReceiptAck",
    "RequirementSupplementRequest",
    "DuplicateSubmissionNotice",
    "RequirementSupplementTaskIssue",
    "RequirementSupplementAcceptedReceipt",
    "LogisticsTaskIssue",
    "LogisticsShipped",
    "LogisticsManualClosedSales",
    "LogisticsManualClosedLogistics",
    "TaskIssue",
    "ProductionQuestionForward",
    "ProductionQuestionReceipt",
    "ProductionPendingTasksQueryReply",
    "SalesDemandStatusQueryReply",
    "ProductionDemandStatusQueryReply",
    "ProductionConfirmationReceipt",
    "ProductionConfirmed",
    "ProductionRejected",
    "ClosedTaskReplyRejected",
    "SalesReplyNoOpenQuestion",
    "ProductionTerminateSalesNotice",
    "ProductionTerminateProductionNotice",
    "SalesDemandWithdrawn",
    "ProductionDemandWithdrawn",
    "SalesDemandWithdrawRejected",
    "TaskManualClosedSales",
    "TaskManualClosedProduction",
    "ConversationClosedMaxRounds",
    "SalesReplyTaskReissue",
    "SalesReplyReissueReceipt",
    "WeeklyReport",
    "OutboundAlert",
}


def send_pending_receipt_acks_smtp(session: Session, *, limit: int = 20) -> dict:
    job_ids = [
        row.id
        for row in (
            session.query(OutboundMailJob)
            .filter_by(mail_type="SalesReceiptAck", status="Pending")
            .order_by(OutboundMailJob.created_at)
            .limit(limit)
            .all()
        )
    ]
    return send_outbound_jobs_smtp(session, job_ids)


def send_pending_auto_workflow_mails_smtp(session: Session, *, limit: int = 20) -> dict:
    jobs = claim_outbound_jobs(session, limit=min(max(1, limit), 1), mail_types=AUTO_WORKFLOW_MAIL_TYPES)
    host = get_config(session, "smtp_host", "smtp.exmail.qq.com")
    port = int(get_config(session, "smtp_port", "465"))
    username = get_config(session, "bot_email", "bot.market@jimuyida.com")
    password = get_config(session, "bot_email_password", "")
    display_name = normalize_bot_display_name(get_config(session, "bot_display_name", "商务部小J"))
    return send_outbound_jobs_with_account(
        session,
        jobs,
        host=host,
        port=port,
        username=username,
        password=password,
        display_name=display_name,
        include_generated_followups=True,
    )


def send_pending_auto_sales_replies_smtp(session: Session, *, limit: int = 20) -> dict:
    return send_pending_auto_workflow_mails_smtp(session, limit=limit)


def send_outbound_jobs_with_account(
    session: Session,
    jobs: list[OutboundMailJob],
    *,
    host: str,
    port: int,
    username: str,
    password: str,
    display_name: str,
    include_generated_followups: bool = False,
) -> dict:
    display_name = normalize_bot_display_name(display_name)
    if not bot_enabled(session):
        _release_claimed_jobs(session, jobs)
        return {"sent": 0, "failed": 0, "total": 0, "skipped": "bot is disabled"}
    if not jobs:
        return {"sent": 0, "failed": 0, "total": 0}
    if not password:
        _release_claimed_jobs(session, jobs)
        raise RuntimeError(f"smtp password is not configured for {username}")

    # 最多发 jobs 列表的长度封（已由调用方 limit 参数截取）
    send_limit = len(jobs)
    sent = 0
    failed = 0
    total = 0
    send_attempts = 0
    sent_ids: set[str] = set()
    queue = list(jobs)
    interval_seconds = mail_login_interval_seconds(session)
    throttled_until = reserve_mail_send_slot(session, username, interval_seconds=interval_seconds)
    if throttled_until is not None:
        _release_claimed_jobs(session, jobs, next_retry_at=throttled_until)
        return {"sent": 0, "failed": 0, "total": 0, "throttled_until": throttled_until.isoformat()}

    try:
        reserve_mail_login("smtp", username, interval_seconds=interval_seconds)
    except RuntimeError:
        throttled_until = datetime.now(timezone.utc) + timedelta(seconds=interval_seconds)
        _release_claimed_jobs(session, jobs, next_retry_at=throttled_until)
        return {"sent": 0, "failed": 0, "total": 0, "throttled_until": throttled_until.isoformat()}
    session.commit()
    try:
        with smtplib.SMTP_SSL(host, port, timeout=settings.mail_smtp_timeout_seconds) as smtp:
            smtp.login(username, password)
            index = 0
            while index < len(queue) and send_attempts < send_limit:
                job = queue[index]
                index += 1
                if job.id in sent_ids or job.status not in {"Pending", "Sending"}:
                    continue
                total += 1
                recipients = as_list(job.to_json) + as_list(job.cc_json)
                if not recipients:
                    # 收件人缺失是不可恢复错误，直接 Failed
                    mark_outbound_failure(session, job, "missing recipients")
                    failed += 1
                    session.commit()
                    continue  # 跳过该封，不中断其他邮件
                send_attempts += 1
                try:
                    msg = EmailMessage(policy=OUTBOUND_EMAIL_POLICY)
                    msg["From"] = formataddr((display_name, username))
                    msg["To"] = ", ".join(as_list(job.to_json))
                    if as_list(job.cc_json):
                        msg["Cc"] = ", ".join(as_list(job.cc_json))
                    msg["Subject"] = job.subject
                    msg.set_content(job.body)
                    attach_original_order_files(session, msg, job)
                    smtp.send_message(msg, from_addr=username, to_addrs=recipients)
                except Exception as exc:
                    # SMTP 发送异常：指数退避重试，不中断队列中其他邮件
                    mark_outbound_retry_or_fail(session, job, str(exc))
                    failed += 1
                    session.commit()
                    continue  # 继续发下一封
                job.status = "Sent"
                job.sent_at = datetime.now(timezone.utc)
                _clear_outbound_lock(job)
                add_audit(session, "OutboundMailSent", "OutboundMailJob", job.id, {"recipients": recipients, "mail_type": job.mail_type})
                sent_ids.add(job.id)
                generated_followups = [
                    enqueue_sales_reply_reissue_receipt(session, job),
                    enqueue_requirement_supplement_receipt(session, job),
                ]
                session.flush()
                session.commit()
                if include_generated_followups:
                    for generated in generated_followups:
                        if generated is not None and generated.status == "Pending" and generated.id not in sent_ids:
                            queue.append(generated)
                sent += 1
    except Exception as exc:
        for job in jobs:
            if job.status == "Sending":
                total += 1
                mark_outbound_retry_or_fail(session, job, f"smtp connect/login failed: {exc}")
                failed += 1
        session.commit()
    return {"sent": sent, "failed": failed, "total": total}


def send_direct_smtp(
    *,
    host: str,
    port: int,
    username: str,
    password: str,
    display_name: str,
    to_addresses: list[str],
    cc_addresses: list[str] | None = None,
    subject: str,
    body: str,
) -> None:
    display_name = normalize_bot_display_name(display_name)
    if not password:
        raise RuntimeError(f"smtp password is not configured for {username}")
    recipients = to_addresses + (cc_addresses or [])
    if not recipients:
        raise RuntimeError("smtp recipients are not configured")
    msg = EmailMessage(policy=OUTBOUND_EMAIL_POLICY)
    msg["From"] = formataddr((display_name, username))
    msg["To"] = ", ".join(to_addresses)
    if cc_addresses:
        msg["Cc"] = ", ".join(cc_addresses)
    msg["Subject"] = subject
    msg.set_content(body)
    reserve_mail_login("smtp", username)
    reserve_mail_send(username)
    with smtplib.SMTP_SSL(host, port, timeout=settings.mail_smtp_timeout_seconds) as smtp:
        smtp.login(username, password)
        smtp.send_message(msg, from_addr=username, to_addrs=recipients)


def attach_original_order_files(session: Session, msg: EmailMessage, job: OutboundMailJob) -> None:
    if job.mail_type not in {"TaskIssue", "RequirementSupplementTaskIssue", "SalesReplyTaskReissue"} or not job.related_task_id:
        return
    from backend.app.models import ProductionTask
    from backend.app.services.storage import read_storage

    task = session.get(ProductionTask, job.related_task_id)
    if task is None:
        return
    rows = (
        session.query(AttachmentAsset)
        .filter(
            AttachmentAsset.mail_id == task.requirement.source_mail_id,
            AttachmentAsset.parent_attachment_id.is_(None),
            AttachmentAsset.content_type != "message/rfc822",
        )
        .all()
    )
    for asset in rows:
        try:
            maintype, subtype = (asset.content_type or "application/octet-stream").split("/", 1)
            msg.add_attachment(read_storage(asset.storage_ref), maintype=maintype, subtype=subtype, filename=asset.file_name)
            apply_attachment_filename_compatibility(msg.get_payload()[-1], asset.file_name, f"{maintype}/{subtype}")
        except Exception as exc:
            record_attachment_parse_exception(session, asset, f"attach outbound failed: {exc}")
