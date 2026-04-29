from __future__ import annotations

import email
import imaplib
import smtplib
from dataclasses import dataclass, field
from email.header import Header, decode_header
from email.message import EmailMessage
from email.policy import default
from email.utils import encode_rfc2231, formataddr, getaddresses
from html import unescape
import re

from sqlalchemy.orm import Session

from backend.app.models import AttachmentAsset, MailMessage, OutboundMailJob
from backend.app.services.attachment_parser import ParsedAttachment, parse_attachment
from backend.app.services.jsonutil import as_list, dumps
from backend.app.services.mail_throttle import mail_login_interval_seconds, reserve_mail_login, reserve_mail_send
from backend.app.services.parser import classify_mail, normalize_latest_reply
from backend.app.services.storage import save_attachment
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
    with imaplib.IMAP4_SSL(host, port) as imap:
        imap.login(username, password)
        imap.select("INBOX")
        _, search_data = imap.search(None, "UNSEEN")
        message_nums = search_data[0].split()[:limit]
        for message_num in message_nums:
            _, fetch_data = imap.fetch(message_num, "(RFC822)")
            if not fetch_data or not isinstance(fetch_data[0], tuple):
                continue
            incoming = parse_email_bytes(fetch_data[0][1])
            mail = store_incoming_email(session, incoming)
            imported += 1
            enqueue_job(session, "process_inbound_mail", {"mail_id": mail.id})
            queued += 1
        imap.logout()
    return {"imported": imported, "queued": queued}


def store_incoming_email(session: Session, incoming: IncomingEmail) -> MailMessage:
    dedupe_source = incoming.message_id or f"{incoming.from_address}|{incoming.subject}|{incoming.body_text[:80]}"
    mail = create_inbound_mail(
        session,
        from_address=incoming.from_address,
        subject=incoming.subject,
        body_text=incoming.body_text,
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


def mark_outbound_failure(session: Session, job: OutboundMailJob, error: str) -> None:
    job.status = "Failed"
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


def send_pending_smtp(session: Session, *, limit: int = 20) -> dict:
    host = get_config(session, "smtp_host", "smtp.exmail.qq.com")
    port = int(get_config(session, "smtp_port", "465"))
    username = get_config(session, "bot_email", "bot.market@jimuyida.com")
    password = get_config(session, "bot_email_password", "")
    display_name = normalize_bot_display_name(get_config(session, "bot_display_name", "商务部小J"))
    jobs = session.query(OutboundMailJob).filter_by(status="Pending").order_by(OutboundMailJob.created_at).limit(limit).all()
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
    jobs = (
        session.query(OutboundMailJob)
        .filter(OutboundMailJob.id.in_(job_ids), OutboundMailJob.status == "Pending")
        .order_by(OutboundMailJob.created_at)
        .all()
    )
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
    job_ids = [
        row.id
        for row in (
            session.query(OutboundMailJob)
            .filter(OutboundMailJob.mail_type.in_(AUTO_WORKFLOW_MAIL_TYPES), OutboundMailJob.status == "Pending")
            .order_by(OutboundMailJob.created_at)
            .limit(limit)
            .all()
        )
    ]
    return send_outbound_jobs_smtp(session, job_ids, include_generated_followups=True)


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
        return {"sent": 0, "failed": 0, "total": 0, "skipped": "bot is disabled"}
    if not jobs:
        return {"sent": 0, "failed": 0, "total": 0}
    if not password:
        raise RuntimeError(f"smtp password is not configured for {username}")

    sent = 0
    failed = 0
    total = 0
    send_attempts = 0
    sent_ids: set[str] = set()
    queue = list(jobs)
    interval_seconds = mail_login_interval_seconds(session)
    reserve_mail_login("smtp", username, interval_seconds=interval_seconds)
    with smtplib.SMTP_SSL(host, port) as smtp:
        smtp.login(username, password)
        index = 0
        while index < len(queue):
            if send_attempts >= 1:
                break
            job = queue[index]
            index += 1
            if job.id in sent_ids or job.status != "Pending":
                continue
            total += 1
            recipients = as_list(job.to_json) + as_list(job.cc_json)
            if not recipients:
                mark_outbound_failure(session, job, "missing recipients")
                failed += 1
                continue
            reserve_mail_send(username, interval_seconds=interval_seconds)
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
                mark_outbound_failure(session, job, str(exc))
                failed += 1
                continue
            job.status = "Sent"
            add_audit(session, "OutboundMailSent", "OutboundMailJob", job.id, {"recipients": recipients, "mail_type": job.mail_type})
            sent_ids.add(job.id)
            generated_followups = [
                enqueue_sales_reply_reissue_receipt(session, job),
                enqueue_requirement_supplement_receipt(session, job),
            ]
            session.flush()
            if include_generated_followups:
                for generated in generated_followups:
                    if generated is not None and generated.status == "Pending" and generated.id not in sent_ids:
                        queue.append(generated)
            sent += 1
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
    with smtplib.SMTP_SSL(host, port) as smtp:
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
