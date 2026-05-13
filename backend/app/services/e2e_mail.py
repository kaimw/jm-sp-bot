from __future__ import annotations

import imaplib
import time
import uuid

from sqlalchemy.orm import Session

from backend.app.models import MailMessage, OutboundMailJob, ProductionDepartment, ProductionTask
from backend.app.services.jobs import run_pending_jobs
from backend.app.services.jsonutil import as_list, dumps
from backend.app.services.mail_adapter import parse_email_bytes, send_direct_smtp, send_outbound_jobs_smtp, sync_imap_mailbox
from backend.app.services.mail_throttle import mail_login_interval_seconds, reserve_mail_login
from backend.app.services.workflow import approve_task, get_config


def run_tencent_mail_e2e(session: Session) -> dict:
    host_imap = get_config(session, "imap_host", "imap.exmail.qq.com")
    port_imap = int(get_config(session, "imap_port", "993"))
    host_smtp = get_config(session, "smtp_host", "smtp.exmail.qq.com")
    port_smtp = int(get_config(session, "smtp_port", "465"))
    bot_email = require_config(session, "bot_email")
    require_config(session, "bot_email_password")
    sales_email = get_config(session, "e2e_sales_email", "bot.sales@jimuyida.com")
    sales_password = require_config(session, "e2e_sales_password")
    production_email = get_config(session, "e2e_production_email", "bot.production@jimuyida.com")
    production_password = require_config(session, "e2e_production_password")
    login_interval_seconds = mail_login_interval_seconds(session)

    configure_e2e_department(session, production_email)
    test_id = uuid.uuid4().hex[:8]
    subject = f"生产订单需求 - 端到端测试 - {test_id}"
    body = "\n".join(
        [
            f"客户名称：端到端测试客户-{test_id}",
            "物料：腾讯企业邮箱端到端测试展架",
            "数量：1套",
            "期望交期：2026-05-20",
            f"订单号：E2E-{test_id}",
        ]
    )
    steps: list[dict[str, str]] = []

    send_direct_smtp(
        host=host_smtp,
        port=port_smtp,
        username=sales_email,
        password=sales_password,
        display_name="销售测试账号",
        to_addresses=[bot_email],
        subject=subject,
        body=body,
    )
    steps.append({"name": "sales_smtp_send", "status": "ok", "detail": f"{sales_email} -> {bot_email}"})

    mail = wait_for_bot_inbound_mail(session, subject, sales_email, interval_seconds=login_interval_seconds)
    steps.append({"name": "bot_imap_sync", "status": "ok", "detail": mail.id})

    jobs_result = run_pending_jobs(session, limit=50)
    session.commit()
    mail = session.get(MailMessage, mail.id)
    if mail is None:
        raise RuntimeError("bot inbound mail was not persisted")
    task = session.get(ProductionTask, mail.related_task_id) if mail.related_task_id else None
    if task is None:
        raise RuntimeError("bot processed sales mail but did not create production task")
    steps.append({"name": "inbound_queue_process", "status": "ok", "detail": f"task={task.task_no}; completed={jobs_result['completed']}"})

    ack_job = find_ack_job(session, mail, subject)
    task_issue_job = approve_task(session, task.id, actor="e2e-mail-test")
    session.flush()
    steps.append({"name": "task_approved", "status": "ok", "detail": task_issue_job.subject})

    ack_send = send_outbound_jobs_smtp(session, [ack_job.id])
    if ack_send["failed"] or ack_send["sent"] != 1:
        raise RuntimeError(f"bot sales ack smtp send failed: {ack_send}")
    session.flush()
    steps.append({"name": "bot_sales_ack_smtp_send", "status": "ok", "detail": f"sent={ack_send['sent']}"})

    time.sleep(login_interval_seconds)
    task_send = send_outbound_jobs_smtp(session, [task_issue_job.id])
    if task_send["failed"] or task_send["sent"] != 1:
        raise RuntimeError(f"bot task smtp send failed: {task_send}")
    steps.append({"name": "bot_task_smtp_send", "status": "ok", "detail": f"sent={task_send['sent']}"})

    sales_ack = wait_for_imap_message(
        host_imap,
        port_imap,
        sales_email,
        sales_password,
        subject_fragment=test_id,
        from_address=bot_email,
        interval_seconds=login_interval_seconds,
    )
    steps.append({"name": "sales_imap_ack_received", "status": "ok", "detail": sales_ack["subject"]})

    production_task_mail = wait_for_imap_message(
        host_imap,
        port_imap,
        production_email,
        production_password,
        subject_fragment=task.task_no,
        from_address=bot_email,
        interval_seconds=login_interval_seconds,
    )
    steps.append({"name": "production_imap_task_received", "status": "ok", "detail": production_task_mail["subject"]})

    return {
        "ok": True,
        "test_id": test_id,
        "sales_email": sales_email,
        "production_email": production_email,
        "bot_email": bot_email,
        "mail_id": mail.id,
        "task_id": task.id,
        "task_no": task.task_no,
        "outbound_job_ids": [ack_job.id, task_issue_job.id],
        "steps": steps,
    }


def require_config(session: Session, key: str) -> str:
    value = get_config(session, key, "")
    if not value:
        raise RuntimeError(f"{key} is not configured")
    return value


def configure_e2e_department(session: Session, production_email: str) -> None:
    department = session.query(ProductionDepartment).filter_by(department_code="default").one_or_none()
    if department is None:
        department = ProductionDepartment(department_code="default", department_name="默认生产部门")
        session.add(department)
    department.department_name = "腾讯企业邮箱端到端测试生产部"
    department.mail_to_json = dumps([production_email])
    department.mail_cc_json = dumps([])
    department.status = "Active"
    session.flush()


def wait_for_bot_inbound_mail(session: Session, subject: str, sales_email: str, *, interval_seconds: int) -> MailMessage:
    last_sync = {"imported": 0, "queued": 0}
    for attempt in range(2):
        last_sync = sync_imap_mailbox(session, limit=50)
        session.flush()
        mail = (
            session.query(MailMessage)
            .filter(MailMessage.subject == subject, MailMessage.from_address == sales_email)
            .order_by(MailMessage.created_at.desc())
            .first()
        )
        if mail is not None:
            return mail
        if attempt == 0:
            time.sleep(interval_seconds)
    raise RuntimeError(f"bot mailbox did not receive sales test mail; last_sync={last_sync}")


def find_ack_job(session: Session, mail: MailMessage, subject: str) -> OutboundMailJob:
    ack_subject = subject if subject.lower().startswith("re:") else f"Re: {subject}"
    job = (
        session.query(OutboundMailJob)
        .filter_by(mail_type="SalesReceiptAck", subject=ack_subject, status="Pending")
        .order_by(OutboundMailJob.created_at.desc())
        .first()
    )
    if job is None or mail.from_address not in as_list(job.to_json):
        raise RuntimeError("sales receipt ack outbound job was not queued")
    return job


def wait_for_imap_message(
    host: str,
    port: int,
    username: str,
    password: str,
    *,
    subject_fragment: str,
    from_address: str,
    interval_seconds: int,
) -> dict:
    last_seen = ""
    for attempt in range(2):
        found = find_imap_message(
            host,
            port,
            username,
            password,
            subject_fragment=subject_fragment,
            from_address=from_address,
            interval_seconds=interval_seconds,
        )
        if found is not None:
            return found
        last_seen = subject_fragment
        if attempt == 0:
            time.sleep(interval_seconds)
    raise RuntimeError(f"{username} did not receive expected mail containing subject fragment: {last_seen}")


def find_imap_message(
    host: str,
    port: int,
    username: str,
    password: str,
    *,
    subject_fragment: str,
    from_address: str,
    interval_seconds: int,
    limit: int = 80,
) -> dict | None:
    reserve_mail_login("imap", username, interval_seconds=interval_seconds)
    with imaplib.IMAP4_SSL(host, port) as imap:
        imap.login(username, password)
        imap.select("INBOX")
        _, search_data = imap.search(None, "ALL")
        message_nums = search_data[0].split()[-limit:]
        for message_num in reversed(message_nums):
            _, fetch_data = imap.fetch(message_num, "(BODY.PEEK[])")
            if not fetch_data or not isinstance(fetch_data[0], tuple):
                continue
            incoming = parse_email_bytes(fetch_data[0][1])
            if subject_fragment in incoming.subject and incoming.from_address.lower() == from_address.lower():
                return {
                    "message_id": incoming.message_id,
                    "from_address": incoming.from_address,
                    "subject": incoming.subject,
                }
        imap.logout()
    return None
