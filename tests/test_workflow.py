from __future__ import annotations

import io
import zipfile
from contextlib import nullcontext
from datetime import timedelta
from email.message import EmailMessage

import httpx
import pytest
from docx import Document
from openpyxl import Workbook
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from backend.app.database import Base
from backend.app.models import (
    AuditEvent,
    AttachmentAsset,
    BackupJob,
    ExceptionCase,
    ExtractionEvidence,
    MailMessage,
    ModelProviderConfig,
    OrderRequirement,
    OutboundMailJob,
    ProcessingJob,
    ProductionDepartment,
    ProductionTask,
    ProductionTaskVersion,
    QuestionAndReply,
    RequirementWorkflowBinding,
    SystemConfig,
    WorkflowImportJob,
    WorkflowVersion,
)
from backend.app.services.attachment_parser import parse_attachment
from backend.app.services.auth import create_session_token, parse_session_token
from backend.app.services.bootstrap import seed_defaults, set_config
from backend.app.services.jobs import run_pending_jobs
from backend.app.services.jsonutil import dumps, loads, as_list
from backend.app.services.mail_adapter import (
    parse_email_bytes,
    send_outbound_jobs_smtp,
    send_pending_auto_workflow_mails_smtp,
    send_pending_receipt_acks_smtp,
    send_pending_smtp,
    sync_imap_mailbox,
    store_incoming_email,
)
from backend.app.services.mail_throttle import clamp_mail_interval_seconds, reset_mail_login_throttle
from backend.app.services.mail_worker import run_mail_auto_worker_once
from backend.app.services.initial_review import initial_review_config, remember_deleted_workflow_review_rules
from backend.app.services.model_provider import build_openai_chat_payload, call_model, extract_chat_content, resolve_api_key
from backend.app.services.operations import cleanup_preview, execute_cleanup, weekly_report_csv
from backend.app.services.pdf import simple_pdf
from backend.app.services.workflow import (
    apply_exception_requirement_patch,
    approve_task,
    create_inbound_mail,
    create_task_from_mail,
    enqueue_weekly_report,
    force_close_task_manual,
    record_exception_case,
    record_production_feedback,
    record_production_question,
    retry_outbound_mail,
    set_weekly_report_recipients,
    weekly_report_recipients,
)
from backend.app.services.workflow_rules import (
    deactivate_workflow_version,
    delete_workflow_version,
    chat_generate_workflow_rule,
    import_structured_workflow_rules,
    import_workflow_document,
    list_workflow_rules,
    match_workflow_for_mail,
    save_workflow_version_rules,
)
from backend.app.models import now_utc


@pytest.fixture(autouse=True)
def reset_mail_throttle_between_tests():
    reset_mail_login_throttle()


def make_session():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(bind=engine)
    Session = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)
    session = Session()
    seed_defaults(session)
    session.commit()
    return session


def configure_department(session):
    department = session.query(ProductionDepartment).filter_by(department_code="default").one()
    department.mail_to_json = dumps(["production@jimuyida.com"])
    session.commit()


def create_valid_task(session, order_no="SO-001"):
    mail = create_inbound_mail(
        session,
        from_address="sales@jimuyida.com",
        subject=f"生产订单需求 - 测试客户 - {order_no}",
        body_text="\n".join(
            [
                "客户名称：测试客户",
                "产品：积木展示架 A1",
                "数量：120 套",
                "期望交期：2026-05-20",
                f"订单号：{order_no}",
            ]
        ),
    )
    task = create_task_from_mail(session, mail)
    session.commit()
    assert task is not None
    return task


def test_seed_defaults_omit_plaintext_secrets():
    session = make_session()
    assert session.get(SystemConfig, "bot_email").value == "bot.market@jimuyida.com"
    assert session.get(SystemConfig, "mail_auto_worker_interval_seconds").value == "60"
    assert session.get(SystemConfig, "mail_rate_limit_interval_seconds").value == "60"
    assert session.get(SystemConfig, "bot_enabled").value == "false"
    model = session.query(ModelProviderConfig).one()
    assert model.title == "Dify deepseekV3"
    assert model.credential_ref == "env:MODEL_API_KEY"


def test_system_enable_requires_model_bot_and_department_config():
    from backend.app.main import config, update_mail_config
    from backend.app.schemas import MailRuntimeConfigUpdate

    session = make_session()

    readiness = config(session)["startup_readiness"]
    assert readiness["ready"] is False
    assert "Dify API Key" in readiness["missing"]
    assert "bot邮箱密码" in readiness["missing"]
    assert "生产部门邮箱" in readiness["missing"]

    with pytest.raises(Exception) as exc:
        update_mail_config(MailRuntimeConfigUpdate(bot_enabled=True), session)

    assert exc.value.status_code == 400
    assert "Dify API Key" in exc.value.detail
    assert "bot邮箱密码" in exc.value.detail
    assert "生产部门邮箱" in exc.value.detail
    assert session.get(SystemConfig, "bot_enabled").value == "false"

    configure_department(session)
    model = session.query(ModelProviderConfig).one()
    set_config(session, "model_api_key", "runtime-secret", is_secret=True)
    model.credential_ref = "config:model_api_key"
    session.commit()

    result = update_mail_config(MailRuntimeConfigUpdate(bot_email_password="mail-secret", bot_enabled=True), session)

    assert result["startup_readiness"]["ready"] is True
    assert session.get(SystemConfig, "bot_enabled").value == "True"


def test_mail_rate_limit_interval_is_clamped_to_one_minute():
    assert clamp_mail_interval_seconds(30) == 60
    assert clamp_mail_interval_seconds("45") == 60
    assert clamp_mail_interval_seconds(120) == 120


def test_auth_token_roundtrip_and_tamper_detection():
    token = create_session_token("admin")
    assert parse_session_token(token) == "admin"
    assert parse_session_token(token + "x") is None


def test_order_to_task_approval_flow():
    session = make_session()
    configure_department(session)
    task = create_valid_task(session)

    assert task.status == "TaskIssued"
    job = approve_task(session, task.id, actor="tester")
    session.commit()

    assert job.mail_type == "TaskIssue"
    assert as_list(job.to_json) == ["production@jimuyida.com"]
    assert task.status == "TaskIssued"


def test_production_feedback_default_cc_rules():
    session = make_session()
    configure_department(session)
    task = create_valid_task(session)
    approve_task(session, task.id, actor="tester")
    session.commit()

    confirmed = record_production_feedback(session, task.id, "confirmed", "已确认排产")
    assert as_list(confirmed.cc_json) == [
        "dingyong@jimuyida.com",
        "sales@jimuyida.com",
        "jinlei@jimuyida.com",
    ]

    rejected_task = create_valid_task(session, order_no="SO-002")
    approve_task(session, rejected_task.id, actor="tester")
    rejected = record_production_feedback(session, rejected_task.id, "rejected", "资料不完整")
    assert as_list(rejected.cc_json) == ["jinlei@jimuyida.com"]


def test_production_natural_question_reply_is_routed_and_receipted():
    session = make_session()
    configure_department(session)
    task = create_valid_task(session)
    approve_task(session, task.id, actor="tester")
    session.commit()
    mail = create_inbound_mail(
        session,
        from_address="production@jimuyida.com",
        subject=f"Re: [生产任务单][{task.task_no}][测试客户][G100][V1]",
        body_text="没有写明哪个版本的G100，国内还是海外版？",
    )
    session.add(ProcessingJob(job_type="process_inbound_mail", payload_json=dumps({"mail_id": mail.id}), status="Pending"))
    session.commit()

    result = run_pending_jobs(session)
    session.commit()

    forward = session.query(OutboundMailJob).filter_by(mail_type="ProductionQuestionForward").one()
    receipt = session.query(OutboundMailJob).filter_by(mail_type="ProductionQuestionReceipt").one()
    assert result["completed"] == 1
    assert mail.classification == "ProductionQuestion"
    assert mail.related_task_id == task.id
    assert as_list(forward.to_json) == ["sales@jimuyida.com"]
    assert "没有写明哪个版本" in forward.body
    assert as_list(receipt.to_json) == ["production@jimuyida.com"]
    assert "已转发销售人员补充确认" in receipt.body


def test_production_email_can_query_pending_confirmation_tasks():
    session = make_session()
    configure_department(session)
    task = create_valid_task(session, order_no="SO-PENDING-QUERY")
    session.commit()
    mail = create_inbound_mail(
        session,
        from_address="production@jimuyida.com",
        subject="查询待确认任务",
        body_text="请查询当前待确认生产任务。",
    )

    result = process_mail_direct(session, mail)
    session.commit()

    reply = session.query(OutboundMailJob).filter_by(mail_type="ProductionPendingTasksQueryReply").one()
    assert result == reply
    assert mail.classification == "ProductionPendingTaskQuery"
    assert as_list(reply.to_json) == ["production@jimuyida.com"]
    assert task.task_no in reply.body
    assert "如需确认指定任务" in reply.body


def test_sales_email_can_query_own_demand_status_with_llm(monkeypatch):
    session = make_session()
    configure_department(session)
    own_task = create_valid_task(session, order_no="SO-SALES-QUERY")
    other_mail = create_inbound_mail(
        session,
        from_address="other.sales@jimuyida.com",
        subject="生产订单需求 - 其他客户",
        body_text="\n".join(
            [
                "客户名称：其他客户",
                "产品：G200",
                "数量：10 套",
                "期望交期：2026-06-01",
                "订单号：SO-OTHER-QUERY",
            ]
        ),
    )
    create_task_from_mail(session, other_mail)
    session.commit()
    query_mail = create_inbound_mail(
        session,
        from_address="sales@jimuyida.com",
        subject="查询需求状态",
        body_text="请查询我提交过的需求状态及统计。",
    )
    captured = {}

    def fake_call_model(session, config, *, task_type, messages, related_object_type=None, related_object_id=None):
        captured["task_type"] = task_type
        captured["prompt"] = messages[-1]["content"]
        return {"choices": [{"message": {"content": "销售同事好，您当前有 1 条生产任务，均已下达生产。"}}]}

    monkeypatch.setattr("backend.app.services.workflow.call_model", fake_call_model)

    result = process_mail_direct(session, query_mail)
    session.commit()

    reply = session.query(OutboundMailJob).filter_by(mail_type="SalesDemandStatusQueryReply").one()
    assert result == reply
    assert query_mail.classification == "SalesDemandStatusQuery"
    assert as_list(reply.to_json) == ["sales@jimuyida.com"]
    assert "销售同事好" in reply.body
    assert own_task.task_no in captured["prompt"]
    assert "SO-OTHER-QUERY" not in captured["prompt"]
    assert captured["task_type"] == "MailStatusQueryReply"


def test_production_email_can_query_accepted_demand_status_with_llm(monkeypatch):
    session = make_session()
    configure_department(session)
    task = create_valid_task(session, order_no="SO-PROD-STATUS")
    session.commit()
    query_mail = create_inbound_mail(
        session,
        from_address="production@jimuyida.com",
        subject="查询受理需求统计",
        body_text="请查询生产侧受理需求的状态和统计。",
    )
    captured = {}

    def fake_call_model(session, config, *, task_type, messages, related_object_type=None, related_object_id=None):
        captured["prompt"] = messages[-1]["content"]
        return {"choices": [{"message": {"content": "生产部同事好，当前受理任务 1 条，待确认 1 条。"}}]}

    monkeypatch.setattr("backend.app.services.workflow.call_model", fake_call_model)

    result = process_mail_direct(session, query_mail)
    session.commit()

    reply = session.query(OutboundMailJob).filter_by(mail_type="ProductionDemandStatusQueryReply").one()
    assert result == reply
    assert query_mail.classification == "ProductionDemandStatusQuery"
    assert as_list(reply.to_json) == ["production@jimuyida.com"]
    assert "生产部同事好" in reply.body
    assert task.task_no in captured["prompt"]


def test_production_email_can_confirm_specified_task():
    session = make_session()
    configure_department(session)
    task = create_valid_task(session, order_no="SO-PROD-CONFIRM")
    session.commit()
    mail = create_inbound_mail(
        session,
        from_address="production@jimuyida.com",
        subject="确认排产",
        body_text=f"确认排产 {task.task_no}",
    )

    result = process_mail_direct(session, mail)
    session.commit()

    confirmed = session.query(OutboundMailJob).filter_by(mail_type="ProductionConfirmed", related_task_id=task.id).one()
    receipt = session.query(OutboundMailJob).filter_by(mail_type="ProductionConfirmationReceipt", related_task_id=task.id).one()
    assert result == confirmed
    assert mail.classification == "ProductionScheduleConfirmation"
    assert mail.related_task_id == task.id
    assert task.status == "Closed"
    assert task.closed_reason == "ScheduledConfirmed"
    assert as_list(receipt.to_json) == ["production@jimuyida.com"]


def test_production_email_can_confirm_current_task_by_reply_subject():
    session = make_session()
    configure_department(session)
    task = create_valid_task(session, order_no="SO-PROD-REPLY-CONFIRM")
    session.commit()
    mail = create_inbound_mail(
        session,
        from_address="production@jimuyida.com",
        subject=f"Re: [生产任务单][{task.task_no}][测试客户][G100][V1]",
        body_text="确认",
    )

    process_mail_direct(session, mail)
    session.commit()

    assert mail.related_task_id == task.id
    assert mail.classification == "ProductionScheduleConfirmation"
    assert task.status == "Closed"
    assert session.query(OutboundMailJob).filter_by(mail_type="ProductionConfirmed", related_task_id=task.id).count() == 1


def test_production_reply_agree_schedule_confirms_current_task():
    session = make_session()
    configure_department(session)
    task = create_valid_task(session, order_no="SO-PROD-AGREE-SCHEDULE")
    session.commit()
    mail = create_inbound_mail(
        session,
        from_address="production@jimuyida.com",
        subject=f"Re: [生产任务单][{task.task_no}][江西大学][G200][V1]",
        body_text="收到任务单，同意排产",
    )

    process_mail_direct(session, mail)
    session.commit()

    assert mail.related_task_id == task.id
    assert mail.classification == "ProductionScheduleConfirmation"
    assert task.status == "Closed"
    assert task.closed_reason == "ScheduledConfirmed"
    assert session.query(OutboundMailJob).filter_by(mail_type="ProductionConfirmed", related_task_id=task.id).count() == 1


def test_conversation_closes_when_max_rounds_reached():
    session = make_session()
    configure_department(session)
    set_config(session, "conversation_max_rounds", "1")
    task = create_valid_task(session, order_no="SO-MAX-ROUND")
    approve_task(session, task.id, actor="tester")
    session.add(
        QuestionAndReply(
            task_id=task.id,
            question_text="第一轮疑问",
            reply_text="第一轮答复",
            status="Answered",
        )
    )
    session.commit()
    mail = create_inbound_mail(
        session,
        from_address="production@jimuyida.com",
        subject=f"Re: [生产任务单][{task.task_no}]",
        body_text="没有写明包装方式？",
    )
    session.add(ProcessingJob(job_type="process_inbound_mail", payload_json=dumps({"mail_id": mail.id}), status="Pending"))
    session.commit()

    result = run_pending_jobs(session)
    session.commit()

    close_job = session.query(OutboundMailJob).filter_by(mail_type="ConversationClosedMaxRounds").one()
    case = session.query(ExceptionCase).filter_by(exception_type="ConversationMaxRounds").one()
    assert result["completed"] == 1
    assert task.status == "Closed"
    assert task.closed_reason == "ConversationMaxRounds"
    assert as_list(close_job.to_json) == ["sales@jimuyida.com", "production@jimuyida.com"]
    assert "请销售重新发起完整的订单需求邮件" in close_job.body
    assert case.related_task_id == task.id


def test_workflow_conversation_policy_overrides_global_max_rounds():
    session = make_session()
    configure_department(session)
    set_config(session, "conversation_max_rounds", "3")
    set_config(session, "workflow_contact_map_json", dumps({"张燕": "production@jimuyida.com"}), is_secret=False)
    import_structured_workflow_rules(
        session,
        rules=[
            {
                "workflow_name": "轮次限制流程",
                "routing": {"to_names": ["张燕"], "cc_names": []},
                "match": {"any_keywords": ["轮次限制流程", "轮次限制"], "order_type": "normal_sales"},
                "subject_template": "[轮次限制][{{task_no}}]",
                "body_template": "流程类型：轮次限制流程",
                "required_fields": ["customer_name", "product_summary", "quantity_text", "expected_delivery_date"],
                "required_attachments": [],
                "review_rules": [],
                "conversation_policy": {
                    "max_question_rounds": 1,
                    "on_exceeded": "close_task",
                    "message": "本流程最多允许1轮询问答疑，已达到上限。",
                },
            }
        ],
        actor="tester",
        auto_publish=True,
        source_asset_ref="workflow-policy-test",
    )
    mail = create_inbound_mail(
        session,
        from_address="sales@jimuyida.com",
        subject="生产订单需求 - 轮次限制流程",
        body_text="\n".join(
            [
                "客户名称：轮次客户",
                "产品：轮次产品",
                "数量：10台",
                "期望交期：2026-10-20",
                "订单号：SO-WF-MAX-ROUND",
            ]
        ),
    )
    task = create_task_from_mail(session, mail)
    assert task is not None
    approve_task(session, task.id, actor="tester")
    session.add(
        QuestionAndReply(
            task_id=task.id,
            question_text="第一轮疑问",
            reply_text="第一轮答复",
            status="Answered",
        )
    )
    production_mail = create_inbound_mail(
        session,
        from_address="production@jimuyida.com",
        subject=f"Re: [轮次限制][{task.task_no}]",
        body_text="请再确认包装方式？",
    )
    session.commit()

    close_job = record_production_question(session, task.id, production_mail.body_text, source_mail=production_mail)
    session.commit()

    assert close_job.mail_type == "ConversationClosedMaxRounds"
    assert task.status == "Closed"
    assert "本流程最多允许1轮询问答疑" in close_job.body


def make_docx_bytes(text: str) -> bytes:
    document = Document()
    document.add_paragraph(text)
    buffer = io.BytesIO()
    document.save(buffer)
    return buffer.getvalue()


def make_xlsx_bytes(rows: list[list[str]]) -> bytes:
    workbook = Workbook()
    sheet = workbook.active
    sheet.title = "订单"
    for row in rows:
        sheet.append(row)
    buffer = io.BytesIO()
    workbook.save(buffer)
    workbook.close()
    return buffer.getvalue()


def test_word_excel_zip_attachment_parser():
    docx_bytes = make_docx_bytes("客户名称：附件客户")
    xlsx_bytes = make_xlsx_bytes([["产品", "数量"], ["积木展架", "80套"]])
    pdf_bytes = simple_pdf("PDF订单", ["客户名称：PDF客户", "产品：PDF展架"])
    zip_buffer = io.BytesIO()
    with zipfile.ZipFile(zip_buffer, "w") as archive:
        archive.writestr("需求.docx", docx_bytes)
        archive.writestr("订单.xlsx", xlsx_bytes)
        archive.writestr("订单.pdf", pdf_bytes)

    docx = parse_attachment("需求.docx", docx_bytes, max_zip_bytes=1024 * 1024, max_depth=1)
    xlsx = parse_attachment("订单.xlsx", xlsx_bytes, max_zip_bytes=1024 * 1024, max_depth=1)
    pdf = parse_attachment("订单.pdf", pdf_bytes, max_zip_bytes=1024 * 1024, max_depth=1)
    zipped = parse_attachment("资料.zip", zip_buffer.getvalue(), max_zip_bytes=1024 * 1024, max_depth=1)

    assert docx.status == "Parsed"
    assert "附件客户" in docx.text
    assert xlsx.status == "Parsed"
    assert "积木展架 | 80套" in xlsx.text
    assert pdf.status == "Parsed"
    assert "PDF客户" in pdf.text
    assert zipped.status == "Parsed"
    assert len(zipped.children) == 3
    assert "附件客户" in zipped.text
    assert "积木展架 | 80套" in zipped.text
    assert "PDF展架" in zipped.text


def test_email_store_and_processing_queue_creates_task():
    session = make_session()
    configure_department(session)

    message = EmailMessage()
    message["From"] = "销售 <sales@jimuyida.com>"
    message["To"] = "bot.market@jimuyida.com"
    message["Subject"] = "生产订单需求 - 邮箱入库"
    message["Message-ID"] = "<mail-queue-test@jimuyida.com>"
    message.set_content(
        "\n".join(
            [
                "客户名称：邮箱客户",
                "产品：快闪展台",
                "数量：32套",
                "期望交期：2026-06-01",
                "订单号：SO-MAIL-001",
            ]
        )
    )
    message.add_attachment(
        make_docx_bytes("附件补充：表面处理为哑光。"),
        maintype="application",
        subtype="vnd.openxmlformats-officedocument.wordprocessingml.document",
        filename="补充说明.docx",
    )

    incoming = parse_email_bytes(message.as_bytes())
    mail = store_incoming_email(session, incoming)
    assert session.query(OutboundMailJob).filter_by(mail_type="SalesReceiptAck").count() == 0
    session.add(ProcessingJob(job_type="process_inbound_mail", payload_json=dumps({"mail_id": mail.id}), status="Pending"))
    session.commit()

    result = run_pending_jobs(session)
    session.commit()
    assets = session.query(AttachmentAsset).filter_by(mail_id=mail.id).all()
    ack = session.query(OutboundMailJob).filter_by(mail_type="SalesReceiptAck").one()

    assert result["completed"] == 1
    assert result["failed"] == 0
    parsed_assets = [asset for asset in assets if asset.parse_status == "Parsed"]
    raw_assets = [asset for asset in assets if asset.content_type == "message/rfc822"]
    assert len(parsed_assets) == 1
    assert len(raw_assets) == 1
    assert "表面处理" in (parsed_assets[0].extracted_text or "")
    assert mail.related_task_id is not None
    assert as_list(ack.to_json) == ["sales@jimuyida.com"]
    task = session.get(ProductionTask, mail.related_task_id)
    assert task is not None
    assert f"任务号：{task.task_no}" in ack.body
    assert "邮箱入库" in ack.subject

    duplicate = store_incoming_email(session, incoming)
    assert duplicate.id == mail.id
    assert session.query(OutboundMailJob).filter_by(mail_type="SalesReceiptAck").count() == 1


def test_config_backed_model_provider_key_and_payload():
    session = make_session()
    model = session.query(ModelProviderConfig).one()
    set_config(session, "model_api_key", "runtime-secret", is_secret=True)
    model.credential_ref = "config:model_api_key"
    session.commit()

    payload = build_openai_chat_payload(model.model_name, [{"role": "user", "content": "ping"}])

    assert resolve_api_key(session, model) == "runtime-secret"
    assert payload["model"] == "DeepSeek-V3"
    assert payload["messages"][0]["content"] == "ping"


def test_model_provider_extracts_chat_content():
    output = {"choices": [{"message": {"content": "配置可用"}}]}
    assert extract_chat_content(output) == "配置可用"
    assert extract_chat_content({"choices": []}) == ""


def test_model_provider_streaming_collects_sse_chunks(monkeypatch):
    session = make_session()
    model = session.query(ModelProviderConfig).one()
    set_config(session, "model_api_key", "runtime-secret", is_secret=True)
    model.credential_ref = "config:model_api_key"
    session.commit()

    class FakeStreamResponse:
        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def raise_for_status(self):
            return None

        def iter_lines(self):
            yield 'data: {"choices":[{"delta":{"content":"流程"}}]}'
            yield 'data: {"choices":[{"delta":{"content":"导入"}}]}'
            yield "data: [DONE]"

    class FakeClient:
        def __init__(self, *args, **kwargs):
            self.timeout = kwargs.get("timeout")

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def stream(self, method, url, *, headers=None, json=None):
            assert method == "POST"
            assert url.endswith("/chat/completions")
            assert json is not None and json.get("stream") is True
            return FakeStreamResponse()

    monkeypatch.setattr("backend.app.services.model_provider.httpx.Client", FakeClient)

    output = call_model(
        session,
        model,
        task_type="WorkflowImportParse",
        messages=[{"role": "user", "content": "ping"}],
        stream=True,
    )

    assert extract_chat_content(output) == "流程导入"


def test_llm_fallback_can_classify_and_extract_natural_sales_order(monkeypatch):
    session = make_session()
    configure_department(session)
    model = session.query(ModelProviderConfig).one()
    set_config(session, "model_api_key", "runtime-secret", is_secret=True)
    model.credential_ref = "config:model_api_key"
    session.commit()

    def fake_call_model(session, config, *, task_type, messages, related_object_type=None, related_object_id=None):
        if task_type == "MailClassificationFallback":
            content = dumps({"classification": "SalesOrderRequirement", "confidence": 93, "reason": "自然语言订单需求"})
        else:
            content = dumps(
                {
                    "customer_name": "武汉大学",
                    "product_summary": "G100",
                    "quantity_text": "50套",
                    "expected_delivery_date": "2026-10-20",
                    "external_order_no": "SO-NL-001",
                }
            )
        return {"choices": [{"message": {"content": content}}]}

    monkeypatch.setattr("backend.app.services.llm_fallback.call_model", fake_call_model)
    mail = create_inbound_mail(
        session,
        from_address="sales@jimuyida.com",
        subject="请处理",
        body_text="武汉大学那边的新项目是G100五十套，2026-10-20前交付，编号SO-NL-001。",
    )
    assert mail.classification == "NonTarget"
    session.add(ProcessingJob(job_type="process_inbound_mail", payload_json=dumps({"mail_id": mail.id}), status="Pending"))
    session.commit()

    result = run_pending_jobs(session)
    session.commit()

    ack = session.query(OutboundMailJob).filter_by(mail_type="SalesReceiptAck").one()
    version = session.query(ProductionTaskVersion).one()
    assert result["completed"] == 1
    assert mail.classification == "SalesOrderRequirement"
    assert version.task.requirement.customer_name == "武汉大学"
    assert version.task.requirement.product_summary == "G100"
    assert as_list(ack.to_json) == ["sales@jimuyida.com"]


def test_llm_fallback_non_target_is_recorded_as_conversation_exception(monkeypatch):
    session = make_session()
    model = session.query(ModelProviderConfig).one()
    set_config(session, "model_api_key", "runtime-secret", is_secret=True)
    model.credential_ref = "config:model_api_key"
    session.commit()

    def fake_call_model(session, config, *, task_type, messages, related_object_type=None, related_object_id=None):
        return {"choices": [{"message": {"content": dumps({"classification": "NonTarget", "confidence": 91, "reason": "与订单沟通无关"})}}]}

    monkeypatch.setattr("backend.app.services.llm_fallback.call_model", fake_call_model)
    mail = create_inbound_mail(session, from_address="someone@example.com", subject="午餐", body_text="今天吃什么？")

    process_mail_direct(session, mail)
    session.commit()

    case = session.query(ExceptionCase).filter_by(exception_type="NonTarget").one()
    detail = loads(case.detail, {})
    assert detail["rule_classification"] == "NonTarget"
    assert detail["llm_classification"] == "NonTarget"
    assert detail["llm_reason"] == "与订单沟通无关"


def test_source_mail_exceptions_are_merged_into_one_record():
    session = make_session()
    mail = create_inbound_mail(session, from_address="sales@jimuyida.com", subject="异常合并", body_text="测试")

    record_exception_case(
        session,
        exception_type="ReviewNeedManual",
        severity="Medium",
        detail={"source_mail_id": mail.id, "missing_fields": ["期望交期"]},
        source_mail_id=mail.id,
    )
    record_exception_case(
        session,
        exception_type="AttachmentParseFailed",
        severity="High",
        detail={"source_mail_id": mail.id, "attachment_id": "att-1", "error": "解析失败"},
        source_mail_id=mail.id,
    )
    session.commit()

    case = session.query(ExceptionCase).one()
    detail = loads(case.detail, {})
    assert case.exception_type == "MailExceptions"
    assert case.severity == "High"
    assert detail["source_mail_id"] == mail.id
    assert detail["missing_fields"] == ["期望交期"]
    assert detail["exception_types"] == ["AttachmentParseFailed", "ReviewNeedManual"]
    assert len(detail["exceptions"]) == 2


def test_production_question_sales_reply_reissue_flow(monkeypatch):
    session = make_session()
    configure_department(session)
    set_config(session, "bot_enabled", "true", is_secret=False)
    set_config(session, "bot_email_password", "runtime-secret", is_secret=True)
    task = create_valid_task(session, order_no="SO-QUESTION-001")
    original_issue = approve_task(session, task.id, actor="tester")
    session.commit()

    production_message = EmailMessage()
    production_message["From"] = "生产部 <production@jimuyida.com>"
    production_message["To"] = "bot.market@jimuyida.com"
    production_message["Subject"] = f"生产疑问 - {task.task_no}"
    production_message["Message-ID"] = "<production-question@jimuyida.com>"
    production_message.set_content("请确认表面处理和最终交期，当前信息不足。")
    production_mail = store_incoming_email(session, parse_email_bytes(production_message.as_bytes()))
    session.add(ProcessingJob(job_type="process_inbound_mail", payload_json=dumps({"mail_id": production_mail.id}), status="Pending"))
    session.commit()

    question_result = run_pending_jobs(session)
    session.commit()
    question = session.query(QuestionAndReply).filter_by(task_id=task.id).one()
    forward = session.query(OutboundMailJob).filter_by(related_task_id=task.id, mail_type="ProductionQuestionForward").one()

    assert question_result["completed"] == 1
    assert task.status == "ProductionQuestioned"
    assert question.status == "AwaitingSalesReply"
    assert as_list(forward.to_json) == ["sales@jimuyida.com"]
    assert "请确认表面处理" in forward.body

    sales_message = EmailMessage()
    sales_message["From"] = "销售 <sales@jimuyida.com>"
    sales_message["To"] = "bot.market@jimuyida.com"
    sales_message["Subject"] = f"答复生产疑问 - {task.task_no}"
    sales_message["Message-ID"] = "<sales-reply@jimuyida.com>"
    sales_message.set_content(
        "\n".join(
            [
                "答复如下：",
                "产品：积木展示架 A1 哑光版",
                "期望交期：2026-05-22",
            ]
        )
    )
    sales_mail = store_incoming_email(session, parse_email_bytes(sales_message.as_bytes()))
    session.add(ProcessingJob(job_type="process_inbound_mail", payload_json=dumps({"mail_id": sales_mail.id}), status="Pending"))
    session.commit()

    reply_result = run_pending_jobs(session)
    session.commit()
    version = session.query(ProductionTaskVersion).filter_by(task_id=task.id, version_no=2).one()

    assert reply_result["completed"] == 1
    assert question.status == "Answered"
    assert task.status == "Reissued"
    assert task.current_version_no == 2
    assert task.requirement.product_summary == "积木展示架 A1 哑光版"
    assert task.requirement.expected_delivery_date == "2026-05-22"
    assert "销售补充答复" in version.body

    reissue_job = session.query(OutboundMailJob).filter_by(related_task_id=task.id, mail_type="SalesReplyTaskReissue").one()
    assert as_list(reissue_job.to_json) == ["production@jimuyida.com"]
    assert "积木展示架 A1 哑光版" in reissue_job.body
    assert original_issue.status == "Pending"

    class FakeSMTP:
        sent_subjects = []

        def __init__(self, host, port):
            pass

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, traceback):
            return False

        def login(self, username, password):
            assert password == "runtime-secret"

        def send_message(self, msg, from_addr, to_addrs):
            self.sent_subjects.append(msg["Subject"])

    monkeypatch.setattr("backend.app.services.mail_adapter.smtplib.SMTP_SSL", FakeSMTP)
    send_result = send_outbound_jobs_smtp(session, [reissue_job.id])
    session.commit()

    sales_receipt = session.query(OutboundMailJob).filter_by(mail_type="SalesReplyReissueReceipt").one()
    sales_reply_ack = (
        session.query(OutboundMailJob)
        .filter_by(mail_type="SalesReceiptAck", subject=f"Re: 答复生产疑问 - {task.task_no}")
        .one()
    )
    assert send_result == {"sent": 1, "failed": 0, "total": 1}
    assert reissue_job.status == "Sent"
    assert sales_reply_ack.status == "Pending"
    assert sales_receipt.status == "Pending"
    assert FakeSMTP.sent_subjects == [reissue_job.subject]
    assert original_issue.status == "Pending"
    assert "[已重新下达]" in sales_receipt.subject
    assert "已更新生产任务单并成功重新发送给生产部" in sales_receipt.body


def test_sales_reply_after_conversation_closed_is_rejected_without_reissue():
    session = make_session()
    configure_department(session)
    task = create_valid_task(session, order_no="SO-CLOSED-REPLY-001")
    task.status = "Closed"
    task.closed_reason = "ConversationMaxRounds"
    task.requirement.status = "Closed"
    task.current_version_no = 1
    session.commit()

    reply = create_inbound_mail(
        session,
        from_address="sales@jimuyida.com",
        subject=f"答复生产疑问 - {task.task_no}",
        body_text="答复如下：产品：关闭后不应重发",
    )
    result = process_mail_direct(session, reply)
    session.commit()

    reject = session.query(OutboundMailJob).filter_by(mail_type="ClosedTaskReplyRejected", related_task_id=task.id).one()
    assert result == reject
    assert task.status == "Closed"
    assert task.current_version_no == 1
    assert session.query(OutboundMailJob).filter_by(mail_type="SalesReplyTaskReissue", related_task_id=task.id).count() == 0
    assert "已关闭" in reject.body


def test_sales_reply_without_open_question_does_not_reissue_task():
    session = make_session()
    configure_department(session)
    task = create_valid_task(session, order_no="SO-NO-OPEN-QUESTION")
    task.status = "TaskIssued"
    task.current_version_no = 1
    session.commit()

    reply = create_inbound_mail(
        session,
        from_address="sales@jimuyida.com",
        subject=f"答复生产疑问 - {task.task_no}",
        body_text="答复如下：产品：没有待答复疑问时不应重发",
    )
    result = process_mail_direct(session, reply)
    session.commit()

    notice = session.query(OutboundMailJob).filter_by(mail_type="SalesReplyNoOpenQuestion", related_task_id=task.id).one()
    case = session.query(ExceptionCase).filter_by(exception_type="SalesReplyWithoutOpenQuestion", related_task_id=task.id).one()
    assert result == notice
    assert case.related_task_id == task.id
    assert task.status == "TaskIssued"
    assert task.current_version_no == 1
    assert session.query(ProductionTaskVersion).filter_by(task_id=task.id, version_no=2).count() == 0
    assert session.query(OutboundMailJob).filter_by(mail_type="SalesReplyTaskReissue", related_task_id=task.id).count() == 0


def test_exception_patch_can_recover_missing_fields_to_task_draft():
    session = make_session()
    configure_department(session)
    mail = create_inbound_mail(
        session,
        from_address="sales@jimuyida.com",
        subject="生产订单需求 - 缺字段客户",
        body_text="\n".join(
            [
                "客户名称：缺字段客户",
                "产品：移动展示墙",
                "期望交期：2026-06-20",
                "订单号：SO-MISSING-001",
            ]
        ),
    )
    task = create_task_from_mail(session, mail)
    session.commit()
    case = session.query(ExceptionCase).filter_by(exception_type="ReviewNeedManual").one()

    assert task is None
    assert case.status == "Open"

    recovered = apply_exception_requirement_patch(session, case.id, {"quantity_text": "48套"})
    session.commit()

    assert recovered is not None
    assert recovered.status == "TaskIssued"
    assert recovered.requirement.quantity_text == "48套"
    assert case.status == "Resolved"


def test_weekly_report_enqueue_uses_configured_recipients_and_is_idempotent():
    session = make_session()
    configure_department(session)
    task = create_valid_task(session, order_no="SO-REPORT-001")
    approve_task(session, task.id, actor="tester")
    set_weekly_report_recipients(
        session,
        ["finance@jimuyida.com", "sales-director@jimuyida.com"],
        ["dingyong@jimuyida.com"],
    )
    session.commit()

    first = enqueue_weekly_report(session)
    second = enqueue_weekly_report(session)
    session.commit()

    recipients = weekly_report_recipients(session)

    assert first.id == second.id
    assert first.mail_type == "WeeklyReport"
    assert as_list(first.to_json) == ["finance@jimuyida.com", "sales-director@jimuyida.com"]
    assert as_list(first.cc_json) == ["dingyong@jimuyida.com"]
    assert "一、任务统计" in first.body
    assert "本次上报周期：本周" in first.body
    assert "生成时间：" in first.body
    assert "统计周期：" in first.body
    assert "北京时间" in first.body
    assert "二、已确认产品订单统计（分产品）" in first.body
    assert "三、未确认产品订单统计（分产品）" in first.body
    assert "四、销售 Top10 统计（需求总数和已确认总数）" in first.body
    assert "待处理异常" not in first.body
    assert "待发送邮件" not in first.body
    assert recipients["to"] == ["finance@jimuyida.com", "sales-director@jimuyida.com"]


def test_manual_weekly_report_enqueue_creates_new_outbound_each_click():
    session = make_session()
    set_weekly_report_recipients(session, ["finance@jimuyida.com"], ["dingyong@jimuyida.com"])
    session.commit()

    first = enqueue_weekly_report(session, force_new=True)
    second = enqueue_weekly_report(session, force_new=True)
    session.commit()

    assert first.id != second.id
    assert first.status == "Pending"
    assert second.status == "Pending"
    assert session.query(OutboundMailJob).filter_by(mail_type="WeeklyReport").count() == 2
    assert "本次上报周期：本周" in first.body
    assert "北京时间" in first.body
    assert "发送失败邮件" not in first.body
    assert "变更/取消待确认" not in first.body
    assert "风险/异常摘要" not in first.body


def test_smtp_send_marks_success_failure_and_retry(monkeypatch):
    session = make_session()
    set_config(session, "bot_enabled", "true", is_secret=False)
    set_config(session, "bot_email_password", "runtime-secret", is_secret=True)
    ok = OutboundMailJob(
        mail_type="Manual",
        to_json=dumps(["ok@jimuyida.com"]),
        cc_json=dumps([]),
        subject="OK",
        body="hello",
        idempotency_key="smtp-ok",
        status="Pending",
    )
    missing_recipient = OutboundMailJob(
        mail_type="Manual",
        to_json=dumps([]),
        cc_json=dumps([]),
        subject="NO-RECIPIENT",
        body="hello",
        idempotency_key="smtp-no-recipient",
        status="Pending",
    )
    send_failure = OutboundMailJob(
        mail_type="Manual",
        to_json=dumps(["fail@jimuyida.com"]),
        cc_json=dumps([]),
        subject="FAIL",
        body="hello",
        idempotency_key="smtp-fail",
        status="Pending",
    )
    session.add_all([ok, missing_recipient, send_failure])
    session.commit()

    class FakeSMTP:
        sent_messages = []

        def __init__(self, host, port):
            self.host = host
            self.port = port

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, traceback):
            return False

        def login(self, username, password):
            assert username == "bot.market@jimuyida.com"
            assert password == "runtime-secret"

        def send_message(self, msg, from_addr, to_addrs):
            if msg["Subject"] == "FAIL":
                raise RuntimeError("smtp send failed")
            self.sent_messages.append((msg["Subject"], from_addr, to_addrs))

    monkeypatch.setattr("backend.app.services.mail_adapter.smtplib.SMTP_SSL", FakeSMTP)

    result = send_pending_smtp(session, limit=10)
    session.commit()

    assert result == {"sent": 1, "failed": 0, "total": 1}
    assert ok.status == "Sent"
    assert missing_recipient.status == "Pending"
    assert send_failure.status == "Pending"
    assert FakeSMTP.sent_messages == [("OK", "bot.market@jimuyida.com", ["ok@jimuyida.com"])]
    assert session.query(ExceptionCase).filter_by(exception_type="OutboundMailSendFailed").count() == 0

    reset_mail_login_throttle()
    result = send_pending_smtp(session, limit=10)
    session.commit()

    assert result == {"sent": 0, "failed": 2, "total": 2}
    assert missing_recipient.status == "Failed"
    assert send_failure.status == "Failed"
    assert FakeSMTP.sent_messages == [("OK", "bot.market@jimuyida.com", ["ok@jimuyida.com"])]
    assert session.query(ExceptionCase).filter_by(exception_type="OutboundMailSendFailed").count() == 2

    retried = retry_outbound_mail(session, missing_recipient.id)
    session.commit()

    assert retried.status == "Pending"


def test_smtp_send_skips_when_bot_disabled():
    session = make_session()
    set_config(session, "bot_enabled", "false", is_secret=False)
    set_config(session, "bot_email_password", "runtime-secret", is_secret=True)
    pending = OutboundMailJob(
        mail_type="Manual",
        to_json=dumps(["sales@jimuyida.com"]),
        cc_json=dumps([]),
        subject="BOT DISABLED",
        body="hello",
        idempotency_key="smtp-bot-disabled",
        status="Pending",
    )
    session.add(pending)
    session.commit()

    result = send_pending_smtp(session, limit=10)
    session.commit()

    assert result == {"sent": 0, "failed": 0, "total": 0, "skipped": "bot is disabled"}
    assert pending.status == "Pending"


def test_cancel_pending_outbound_marks_only_matching_pending_jobs():
    from backend.app.main import cancel_pending_outbound
    from backend.app.schemas import OutboundBulkCancelRequest

    class Request:
        class State:
            username = "tester"

        state = State()

    session = make_session()
    matched = OutboundMailJob(
        mail_type="WeeklyReport",
        to_json=dumps(["finance@jimuyida.com"]),
        cc_json=dumps([]),
        subject="周报-Pending",
        body="hello",
        idempotency_key="cancel-matched",
        status="Pending",
    )
    other_pending = OutboundMailJob(
        mail_type="TaskIssue",
        to_json=dumps(["production@jimuyida.com"]),
        cc_json=dumps([]),
        subject="任务-Pending",
        body="hello",
        idempotency_key="cancel-other",
        status="Pending",
    )
    sent = OutboundMailJob(
        mail_type="WeeklyReport",
        to_json=dumps(["finance@jimuyida.com"]),
        cc_json=dumps([]),
        subject="周报-Sent",
        body="hello",
        idempotency_key="cancel-sent",
        status="Sent",
    )
    session.add_all([matched, other_pending, sent])
    session.commit()

    result = cancel_pending_outbound(
        OutboundBulkCancelRequest(mail_type="WeeklyReport"),
        Request(),
        session,
    )

    assert result["cancelled"] == 1
    assert matched.status == "Cancelled"
    assert other_pending.status == "Pending"
    assert sent.status == "Sent"
    audit = session.query(AuditEvent).filter_by(event_type="OutboundMailCancelled", related_object_id=matched.id).one()
    assert audit.actor == "tester"


def test_clear_tasks_requires_admin_password_and_removes_task_list():
    from backend.app.main import clear_tasks
    from backend.app.schemas import TaskClearRequest

    class Request:
        class State:
            username = "admin"

        state = State()

    session = make_session()
    configure_department(session)
    task = create_valid_task(session, order_no="SO-CLEAR")
    version = session.query(ProductionTaskVersion).filter_by(task_id=task.id).first()
    assert version is not None
    source_mail = session.get(MailMessage, task.requirement.source_mail_id)
    source_mail.related_task_id = task.id
    question = QuestionAndReply(
        task_id=task.id,
        question_text="请补充包装要求",
        status="AwaitingSalesReply",
    )
    outbound = OutboundMailJob(
        related_task_id=task.id,
        related_version_id=version.id,
        mail_type="TaskIssue",
        to_json=dumps(["production@jimuyida.com"]),
        cc_json=dumps([]),
        subject="任务单",
        body="hello",
        idempotency_key="clear-task-outbound",
        status="Pending",
    )
    case = ExceptionCase(
        related_task_id=task.id,
        exception_type="ManualReview",
        severity="Medium",
        detail="测试异常",
        status="Open",
    )
    session.add_all([source_mail, question, outbound, case])
    session.commit()
    task_id = task.id
    requirement_id = task.requirement_id
    source_mail_id = source_mail.id
    outbound_id = outbound.id
    case_id = case.id

    with pytest.raises(Exception) as exc:
        clear_tasks(TaskClearRequest(admin_password="wrong"), Request(), session)
    assert exc.value.status_code == 403
    assert session.query(ProductionTask).count() == 1

    result = clear_tasks(TaskClearRequest(admin_password="admin"), Request(), session)
    session.expire_all()

    assert result["cleared"] == 1
    assert session.query(ProductionTask).count() == 0
    assert session.query(ProductionTaskVersion).filter_by(task_id=task_id).count() == 0
    assert session.query(QuestionAndReply).filter_by(task_id=task_id).count() == 0
    assert session.query(OrderRequirement).filter_by(id=requirement_id).count() == 0
    assert session.query(RequirementWorkflowBinding).filter_by(requirement_id=requirement_id).count() == 0
    assert session.query(ExtractionEvidence).filter_by(requirement_id=requirement_id).count() == 0
    assert session.get(MailMessage, source_mail_id).related_task_id is None
    assert session.get(OutboundMailJob, outbound_id).related_task_id is None
    assert session.get(OutboundMailJob, outbound_id).related_version_id is None
    assert session.get(ExceptionCase, case_id).related_task_id is None
    audit = session.query(AuditEvent).filter_by(event_type="TaskListCleared").one()
    assert audit.actor == "admin"


def test_clear_exception_and_ops_lists_require_admin_password():
    from backend.app.main import clear_attachments, clear_audit_events, clear_backups, clear_exceptions, clear_jobs
    from backend.app.schemas import AdminPasswordRequest

    class Request:
        class State:
            username = "admin"

        state = State()

    session = make_session()
    mail = create_inbound_mail(
        session,
        from_address="sales@jimuyida.com",
        subject="附件测试",
        body_text="测试正文",
    )
    attachment = AttachmentAsset(
        mail_id=mail.id,
        file_name="order.docx",
        file_size=12,
        file_hash="hash-clear",
        storage_ref="data/attachments/order.docx",
        parse_status="Completed",
    )
    session.add(attachment)
    session.flush()
    session.add_all(
        [
            ProcessingJob(job_type="process_inbound_mail", payload_json=dumps({"mail_id": mail.id}), status="Pending"),
            ExceptionCase(exception_type="ReviewNeedManual", severity="Medium", detail="缺少字段", status="Open"),
            BackupJob(backup_type="Manual", status="Completed", storage_ref="data/backups/test.zip", manifest_json=dumps({})),
            ExtractionEvidence(
                requirement_id="not-used-in-this-test",
                field_name="customer_name",
                field_value="测试客户",
                source_type="attachment",
                source_attachment_id=attachment.id,
                evidence_text="客户名称：测试客户",
                confidence=90,
            ),
        ]
    )
    session.commit()

    with pytest.raises(Exception) as exc:
        clear_exceptions(AdminPasswordRequest(admin_password="wrong"), Request(), session)
    assert exc.value.status_code == 403
    assert session.query(ExceptionCase).count() == 1

    assert clear_exceptions(AdminPasswordRequest(admin_password="admin"), Request(), session)["cleared"] == 1
    assert clear_jobs(AdminPasswordRequest(admin_password="admin"), Request(), session)["cleared"] == 1
    attachment_result = clear_attachments(AdminPasswordRequest(admin_password="admin"), Request(), session)
    assert attachment_result["cleared"] == 1
    assert attachment_result["evidence_links_cleared"] == 1
    assert clear_backups(AdminPasswordRequest(admin_password="admin"), Request(), session)["cleared"] == 1
    assert session.query(ExceptionCase).count() == 0
    assert session.query(ProcessingJob).count() == 0
    assert session.query(AttachmentAsset).count() == 0
    assert session.query(BackupJob).count() == 0
    assert session.query(ExtractionEvidence).filter(ExtractionEvidence.source_attachment_id.isnot(None)).count() == 0
    assert session.query(AuditEvent).filter(AuditEvent.event_type.in_(["ExceptionsCleared", "ProcessingJobsCleared", "AttachmentsCleared", "BackupsCleared"])).count() == 4

    audit_count = session.query(AuditEvent).count()
    audit_result = clear_audit_events(AdminPasswordRequest(admin_password="admin"), session)
    assert audit_result["cleared"] == audit_count
    assert session.query(AuditEvent).count() == 0


def test_sync_imap_mailbox_skips_when_bot_disabled():
    session = make_session()
    set_config(session, "bot_enabled", "false", is_secret=False)
    result = sync_imap_mailbox(session, limit=10)
    assert result == {"imported": 0, "queued": 0, "skipped": "bot is disabled"}


def test_mail_auto_worker_skips_when_bot_disabled(monkeypatch):
    session = make_session()
    set_config(session, "bot_enabled", "false", is_secret=False)
    session.add(ProcessingJob(job_type="process_inbound_mail", payload_json=dumps({"mail_id": "demo"}), status="Pending"))
    session.commit()

    monkeypatch.setattr("backend.app.services.mail_worker.SessionLocal", lambda: nullcontext(session))
    result = run_mail_auto_worker_once()

    assert result["enabled"] is False
    assert result["synced"]["skipped"] == "bot is disabled"
    assert result["processed"]["skipped"] == "bot is disabled"
    assert session.query(ProcessingJob).filter_by(status="Pending").count() == 1


def test_send_selected_smtp_only_sends_requested_jobs(monkeypatch):
    session = make_session()
    set_config(session, "bot_enabled", "true", is_secret=False)
    set_config(session, "bot_email_password", "runtime-secret", is_secret=True)
    selected = OutboundMailJob(
        mail_type="Manual",
        to_json=dumps(["selected@jimuyida.com"]),
        cc_json=dumps([]),
        subject="SELECTED",
        body="hello",
        idempotency_key="smtp-selected",
        status="Pending",
    )
    skipped = OutboundMailJob(
        mail_type="Manual",
        to_json=dumps(["skipped@jimuyida.com"]),
        cc_json=dumps([]),
        subject="SKIPPED",
        body="hello",
        idempotency_key="smtp-skipped",
        status="Pending",
    )
    session.add_all([selected, skipped])
    session.commit()

    class FakeSMTP:
        sent_subjects = []

        def __init__(self, host, port):
            pass

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, traceback):
            return False

        def login(self, username, password):
            assert password == "runtime-secret"

        def send_message(self, msg, from_addr, to_addrs):
            self.sent_subjects.append(msg["Subject"])

    monkeypatch.setattr("backend.app.services.mail_adapter.smtplib.SMTP_SSL", FakeSMTP)

    result = send_outbound_jobs_smtp(session, [selected.id])
    session.commit()

    assert result == {"sent": 1, "failed": 0, "total": 1}
    assert selected.status == "Sent"
    assert skipped.status == "Pending"
    assert FakeSMTP.sent_subjects == ["SELECTED"]


def test_pending_receipt_ack_sender_does_not_send_task_issues(monkeypatch):
    session = make_session()
    set_config(session, "bot_enabled", "true", is_secret=False)
    set_config(session, "bot_email_password", "runtime-secret", is_secret=True)
    ack = OutboundMailJob(
        mail_type="SalesReceiptAck",
        to_json=dumps(["sales@jimuyida.com"]),
        cc_json=dumps([]),
        subject="Re: 生产订单需求",
        body="已收到",
        idempotency_key="ack-only",
        status="Pending",
    )
    task_issue = OutboundMailJob(
        mail_type="TaskIssue",
        to_json=dumps(["production@jimuyida.com"]),
        cc_json=dumps([]),
        subject="生产任务单",
        body="任务单",
        idempotency_key="task-not-auto",
        status="Pending",
    )
    session.add_all([ack, task_issue])
    session.commit()

    class FakeSMTP:
        sent_subjects = []

        def __init__(self, host, port):
            pass

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, traceback):
            return False

        def login(self, username, password):
            assert password == "runtime-secret"

        def send_message(self, msg, from_addr, to_addrs):
            self.sent_subjects.append(msg["Subject"])

    monkeypatch.setattr("backend.app.services.mail_adapter.smtplib.SMTP_SSL", FakeSMTP)

    result = send_pending_receipt_acks_smtp(session, limit=10)
    session.commit()

    assert result == {"sent": 1, "failed": 0, "total": 1}
    assert ack.status == "Sent"
    assert task_issue.status == "Pending"
    assert FakeSMTP.sent_subjects == ["Re: 生产订单需求"]


def test_attachment_text_can_create_task_and_evidence():
    session = make_session()
    configure_department(session)

    message = EmailMessage()
    message["From"] = "销售 <sales@jimuyida.com>"
    message["To"] = "bot.market@jimuyida.com"
    message["Subject"] = "生产订单需求 - 附件订单"
    message["Message-ID"] = "<attachment-only-order@jimuyida.com>"
    message.set_content("订单信息请看附件。")
    message.add_attachment(
        make_docx_bytes(
            "\n".join(
                [
                    "客户名称：附件字段客户",
                    "产品：附件展台",
                    "数量：66套",
                    "期望交期：2026-07-01",
                    "订单号：SO-ATTACH-001",
                ]
            )
        ),
        maintype="application",
        subtype="vnd.openxmlformats-officedocument.wordprocessingml.document",
        filename="订单需求.docx",
    )

    mail = store_incoming_email(session, parse_email_bytes(message.as_bytes()))
    assert session.query(OutboundMailJob).filter_by(mail_type="SalesReceiptAck").count() == 0
    session.add(ProcessingJob(job_type="process_inbound_mail", payload_json=dumps({"mail_id": mail.id}), status="Pending"))
    session.commit()

    result = run_pending_jobs(session)
    session.commit()
    ack = session.query(OutboundMailJob).filter_by(mail_type="SalesReceiptAck").one()
    evidence = session.query(ExtractionEvidence).filter_by(field_name="customer_name").one()

    assert result["completed"] == 1
    assert mail.related_task_id is not None
    assert evidence.source_type == "Attachment"
    assert evidence.field_value == "附件字段客户"
    assert as_list(ack.to_json) == ["sales@jimuyida.com"]


def test_missing_fields_enqueue_supplement_request():
    session = make_session()
    configure_department(session)
    mail = create_inbound_mail(
        session,
        from_address="sales@jimuyida.com",
        subject="生产订单需求 - 缺数量",
        body_text="\n".join(
            [
                "客户名称：缺数量客户",
                "产品：展示柜",
                "期望交期：2026-07-20",
                "订单号：SO-MISS-QTY",
            ]
        ),
    )

    task = create_task_from_mail(session, mail)
    session.commit()
    supplement = session.query(OutboundMailJob).filter_by(mail_type="RequirementSupplementRequest").one()

    assert task is None
    assert "数量" in supplement.body
    assert as_list(supplement.to_json) == ["sales@jimuyida.com"]


def test_short_natural_sales_order_triggers_review_rejection_without_ack():
    session = make_session()
    configure_department(session)
    message = EmailMessage()
    message["From"] = "销售 <sales@jimuyida.com>"
    message["To"] = "bot.market@jimuyida.com"
    message["Subject"] = "会触发初审规则的邮件"
    message["Message-ID"] = "<natural-order-review@jimuyida.com>"
    message.set_content("武汉大学需要G100,10台，请排产")

    mail = store_incoming_email(session, parse_email_bytes(message.as_bytes()))
    session.add(ProcessingJob(job_type="process_inbound_mail", payload_json=dumps({"mail_id": mail.id}), status="Pending"))
    session.commit()

    result = run_pending_jobs(session)
    session.commit()

    supplement = session.query(OutboundMailJob).filter_by(mail_type="RequirementSupplementRequest").one()
    case = session.query(ExceptionCase).filter_by(exception_type="ReviewNeedManual").one()
    detail = loads(case.detail, {})
    assert result["completed"] == 1
    assert mail.classification == "SalesOrderRequirement"
    assert "期望交期" in supplement.body
    assert "期望交期" in detail["missing_fields"]
    assert session.query(OutboundMailJob).filter_by(mail_type="SalesReceiptAck").count() == 0
    assert as_list(supplement.to_json) == ["sales@jimuyida.com"]


def test_sales_reply_to_initial_review_supplement_creates_task_and_receipt_after_send(monkeypatch):
    session = make_session()
    configure_department(session)
    set_config(session, "bot_enabled", "true", is_secret=False)
    set_config(session, "bot_email_password", "runtime-secret", is_secret=True)
    original = create_inbound_mail(
        session,
        from_address="bot.sales@jimuyida.com",
        subject="常州大学-Seal-2000台",
        body_text="常州大学需要Seal 2000台，请排产",
    )
    task = create_task_from_mail(session, original)
    session.commit()
    requirement = session.query(OrderRequirement).filter_by(source_mail_id=original.id).one()
    supplement = session.query(OutboundMailJob).filter_by(mail_type="RequirementSupplementRequest").one()
    supplement.status = "Sent"
    session.commit()

    reply_message = EmailMessage()
    reply_message["From"] = "sales <bot.sales@jimuyida.com>"
    reply_message["To"] = "bot.market@jimuyida.com"
    reply_message["Subject"] = f"Re:{supplement.subject}"
    reply_message["Message-ID"] = "<requirement-supplement-reply@jimuyida.com>"
    reply_message.set_content(
        "\n".join(
            [
                "2027年1月完成",
                "",
                "------------------ Original ------------------",
                supplement.body,
            ]
        )
    )
    reply_mail = store_incoming_email(session, parse_email_bytes(reply_message.as_bytes()))
    session.add(ProcessingJob(job_type="process_inbound_mail", payload_json=dumps({"mail_id": reply_mail.id}), status="Pending"))
    session.commit()

    result = run_pending_jobs(session)
    session.commit()
    created_task = session.query(ProductionTask).filter_by(requirement_id=requirement.id).one()
    issue_job = session.query(OutboundMailJob).filter_by(related_task_id=created_task.id, mail_type="RequirementSupplementTaskIssue").one()

    assert task is None
    assert result["completed"] == 1
    assert requirement.expected_delivery_date == "2027年1月完成"
    assert requirement.status == "TaskCreated"
    assert created_task.status == "TaskIssued"
    assert reply_mail.related_task_id == created_task.id
    assert as_list(issue_job.to_json) == ["production@jimuyida.com"]
    assert session.query(OutboundMailJob).filter_by(mail_type="RequirementSupplementAcceptedReceipt").count() == 0

    class FakeSMTP:
        sent_subjects = []

        def __init__(self, host, port):
            pass

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, traceback):
            return False

        def login(self, username, password):
            assert password == "runtime-secret"

        def send_message(self, msg, from_addr, to_addrs):
            self.sent_subjects.append(msg["Subject"])

    monkeypatch.setattr("backend.app.services.mail_adapter.smtplib.SMTP_SSL", FakeSMTP)
    send_result = send_outbound_jobs_smtp(session, [issue_job.id])
    session.commit()

    receipt = session.query(OutboundMailJob).filter_by(mail_type="RequirementSupplementAcceptedReceipt").one()
    assert send_result == {"sent": 1, "failed": 0, "total": 1}
    assert issue_job.status == "Sent"
    assert receipt.status == "Pending"
    assert "[已下达生产]" in receipt.subject
    assert "补充的订单信息已处理" in receipt.body
    assert FakeSMTP.sent_subjects == [issue_job.subject]


def test_pending_non_target_mail_is_reclassified_by_updated_rules():
    session = make_session()
    configure_department(session)
    mail = create_inbound_mail(
        session,
        from_address="sales@jimuyida.com",
        subject="会触发初审规则的邮件",
        body_text="武汉大学需要G100,10台，请排产",
    )
    mail.classification = "NonTarget"
    mail.classification_confidence = 70
    session.add(ProcessingJob(job_type="process_inbound_mail", payload_json=dumps({"mail_id": mail.id}), status="Pending"))
    session.commit()

    result = run_pending_jobs(session)
    session.commit()

    supplement = session.query(OutboundMailJob).filter_by(mail_type="RequirementSupplementRequest").one()
    assert result["completed"] == 1
    assert mail.classification == "SalesOrderRequirement"
    assert "武汉大学" in supplement.body


def test_duplicate_processing_does_not_duplicate_review_rejection():
    session = make_session()
    configure_department(session)
    mail = create_inbound_mail(
        session,
        from_address="sales@jimuyida.com",
        subject="会触发初审规则的邮件",
        body_text="武汉大学需要G100,10台，请排产",
    )
    session.add(ProcessingJob(job_type="process_inbound_mail", payload_json=dumps({"mail_id": mail.id}), status="Pending"))
    session.commit()
    run_pending_jobs(session)
    session.commit()

    session.add(ProcessingJob(job_type="process_inbound_mail", payload_json=dumps({"mail_id": mail.id}), status="Pending"))
    session.commit()
    run_pending_jobs(session)
    session.commit()

    assert session.query(OrderRequirement).filter_by(source_mail_id=mail.id).count() == 1
    assert session.query(OutboundMailJob).filter_by(mail_type="RequirementSupplementRequest").count() == 1
    assert session.query(ExceptionCase).filter_by(exception_type="ReviewNeedManual").count() == 1


def test_legacy_duplicate_requirements_do_not_duplicate_review_rejection():
    session = make_session()
    configure_department(session)
    mail = create_inbound_mail(
        session,
        from_address="sales@jimuyida.com",
        subject="会触发初审规则的邮件",
        body_text="武汉大学需要G100,10台，请排产",
    )
    task = create_task_from_mail(session, mail)
    session.commit()
    first_requirement = session.query(OrderRequirement).filter_by(source_mail_id=mail.id).one()

    session.add(
        OrderRequirement(
            source_mail_id=mail.id,
            internal_order_no="REQ-DUPLICATE-LEGACY",
            customer_name=first_requirement.customer_name,
            salesperson_email=first_requirement.salesperson_email,
            product_summary=first_requirement.product_summary,
            quantity_text=first_requirement.quantity_text,
            missing_fields_json=first_requirement.missing_fields_json,
            risk_flags_json="[]",
            status="ReviewFailed",
        )
    )
    session.commit()

    reprocessed = create_task_from_mail(session, mail)
    session.commit()

    assert task is None
    assert reprocessed is None
    assert session.query(OrderRequirement).filter_by(source_mail_id=mail.id).count() == 2
    assert session.query(OutboundMailJob).filter_by(mail_type="RequirementSupplementRequest").count() == 1


def test_duplicate_processing_jobs_only_execute_one_business_flow():
    session = make_session()
    configure_department(session)
    mail = create_inbound_mail(
        session,
        from_address="sales@jimuyida.com",
        subject="生产订单需求 - 重复队列",
        body_text="\n".join(
            [
                "客户名称：重复队列客户",
                "产品：G100",
                "数量：10台",
                "期望交期：2026-10-20",
            ]
        ),
    )
    payload = dumps({"mail_id": mail.id})
    session.add_all(
        [
            ProcessingJob(job_type="process_inbound_mail", payload_json=payload, status="Pending"),
            ProcessingJob(job_type="process_inbound_mail", payload_json=payload, status="Pending"),
        ]
    )
    session.commit()

    result = run_pending_jobs(session)
    session.commit()

    skipped = session.query(ProcessingJob).filter(ProcessingJob.error_message.like("Skipped duplicate%")).one()
    assert result == {"completed": 2, "failed": 0, "total": 2}
    assert skipped.status == "Completed"
    assert session.query(OrderRequirement).filter_by(source_mail_id=mail.id).count() == 1
    assert session.query(ProductionTask).count() == 1
    assert session.query(OutboundMailJob).filter_by(mail_type="TaskIssue").count() == 1


def test_custom_initial_review_rule_rejects_sales_order():
    session = make_session()
    configure_department(session)
    set_config(
        session,
        "initial_review_rules_json",
        dumps(
            [
                {
                    "id": "no-rush",
                    "name": "加急订单人工确认",
                    "field": "source_text",
                    "operator": "not_contains",
                    "value": "加急",
                    "message": "加急订单需要商务人工确认后再下达生产。",
                    "enabled": True,
                }
            ]
        ),
    )
    session.commit()
    mail = create_inbound_mail(
        session,
        from_address="sales@jimuyida.com",
        subject="生产订单需求 - 加急客户",
        body_text="\n".join(
            [
                "客户名称：加急客户",
                "产品：展示柜",
                "数量：50套",
                "期望交期：2026-07-20",
                "订单号：SO-RUSH",
                "备注：加急",
            ]
        ),
    )

    task = create_task_from_mail(session, mail)
    session.commit()

    supplement = session.query(OutboundMailJob).filter_by(mail_type="RequirementSupplementRequest").one()
    case = session.query(ExceptionCase).filter_by(exception_type="ReviewNeedManual").one()
    assert task is None
    assert "加急订单需要商务人工确认" in supplement.body
    assert "review_failures" in case.detail
    assert session.query(ProductionTaskVersion).count() == 0


def test_initial_review_config_includes_readonly_builtin_rules():
    session = make_session()
    set_config(
        session,
        "initial_review_rules_json",
        dumps(
            [
                {
                    "id": "custom-rule",
                    "name": "自定义规则",
                    "field": "source_text",
                    "operator": "contains",
                    "value": "采购订单",
                    "message": "缺少采购订单",
                    "enabled": True,
                }
            ]
        ),
    )
    session.commit()

    config = initial_review_config(session)
    rules = config["rules"]

    assert [rule["id"] for rule in rules[:3]] == [
        "builtin-required-core-fields",
        "builtin-parser-risk-flags",
        "builtin-duplicate-submission",
    ]
    assert all(rule["read_only"] is True and rule["is_builtin"] is True for rule in rules[:3])
    assert rules[-1]["id"] == "custom-rule"


def test_initial_review_config_removes_duplicate_custom_rules():
    session = make_session()
    set_config(
        session,
        "initial_review_rules_json",
        dumps(
            [
                {
                    "id": "rule-1",
                    "name": "采购订单校验",
                    "field": "source_text",
                    "operator": "contains",
                    "value": "采购订单",
                    "message": "缺少采购订单",
                    "enabled": True,
                },
                {
                    "id": "rule-2",
                    "name": "重复采购订单校验",
                    "field": "source_text",
                    "operator": "contains",
                    "value": " 采购 订单 ",
                    "message": "重复项应被清理",
                    "enabled": False,
                },
                {
                    "id": "rule-3",
                    "name": "特批编码校验",
                    "field": "source_text",
                    "operator": "contains",
                    "value": "特批编码",
                    "message": "缺少特批编码",
                    "enabled": True,
                },
            ]
        ),
        is_secret=False,
    )
    session.commit()

    config = initial_review_config(session, include_workflow_rules=True)
    session.commit()

    custom_rules = [rule for rule in config["rules"] if not rule.get("is_builtin")]
    assert [rule["id"] for rule in custom_rules] == ["rule-1", "rule-3"]
    persisted_rules = loads(session.get(SystemConfig, "initial_review_rules_json").value, [])
    assert [rule["id"] for rule in persisted_rules] == ["rule-1", "rule-3"]


def test_pending_auto_workflow_sender_includes_task_issues_and_questions(monkeypatch):
    session = make_session()
    set_config(session, "bot_enabled", "true", is_secret=False)
    set_config(session, "bot_email_password", "runtime-secret", is_secret=True)
    ack = OutboundMailJob(
        mail_type="SalesReceiptAck",
        to_json=dumps(["sales@jimuyida.com"]),
        cc_json=dumps([]),
        subject="Re: 生产订单需求",
        body="已收到",
        idempotency_key="ack-auto",
        status="Pending",
    )
    review = OutboundMailJob(
        mail_type="RequirementSupplementRequest",
        to_json=dumps(["sales@jimuyida.com"]),
        cc_json=dumps([]),
        subject="[订单信息待补充] 请补充生产任务单信息",
        body="初审未通过",
        idempotency_key="review-auto",
        status="Pending",
    )
    question_forward = OutboundMailJob(
        mail_type="ProductionQuestionForward",
        to_json=dumps(["sales@jimuyida.com"]),
        cc_json=dumps([]),
        subject="[生产疑问] 请补充确认",
        body="生产疑问",
        idempotency_key="question-forward-auto",
        status="Pending",
    )
    question_receipt = OutboundMailJob(
        mail_type="ProductionQuestionReceipt",
        to_json=dumps(["production@jimuyida.com"]),
        cc_json=dumps([]),
        subject="Re: [生产任务单]",
        body="已转发销售",
        idempotency_key="question-receipt-auto",
        status="Pending",
    )
    task_issue = OutboundMailJob(
        mail_type="TaskIssue",
        to_json=dumps(["production@jimuyida.com"]),
        cc_json=dumps([]),
        subject="生产任务单",
        body="任务单",
        idempotency_key="task-manual",
        status="Pending",
    )
    weekly_report = OutboundMailJob(
        mail_type="WeeklyReport",
        to_json=dumps(["dingyong@jimuyida.com"]),
        cc_json=dumps(["jinlei@jimuyida.com"]),
        subject="[商务生产任务单周报][2026-W17]",
        body="周报",
        idempotency_key="weekly-report-auto",
        status="Pending",
    )
    production_confirmed = OutboundMailJob(
        mail_type="ProductionConfirmed",
        to_json=dumps(["sales@jimuyida.com"]),
        cc_json=dumps(["dingyong@jimuyida.com", "jinlei@jimuyida.com"]),
        subject="[生产确认] 已确认排产",
        body="已确认",
        idempotency_key="production-confirmed-auto",
        status="Pending",
    )
    confirmation_receipt = OutboundMailJob(
        mail_type="ProductionConfirmationReceipt",
        to_json=dumps(["production@jimuyida.com"]),
        cc_json=dumps([]),
        subject="Re: [生产确认] 已记录",
        body="已记录",
        idempotency_key="production-confirmation-receipt-auto",
        status="Pending",
    )
    session.add_all([ack, review, question_forward, question_receipt, task_issue, weekly_report, production_confirmed, confirmation_receipt])
    session.commit()

    class FakeSMTP:
        sent_subjects = []

        def __init__(self, host, port):
            pass

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, traceback):
            return False

        def login(self, username, password):
            assert password == "runtime-secret"

        def send_message(self, msg, from_addr, to_addrs):
            self.sent_subjects.append(msg["Subject"])

    monkeypatch.setattr("backend.app.services.mail_adapter.smtplib.SMTP_SSL", FakeSMTP)

    result = send_pending_auto_workflow_mails_smtp(session, limit=10)
    session.commit()

    assert result == {"sent": 1, "failed": 0, "total": 1}
    assert ack.status == "Sent"
    assert review.status == "Pending"
    assert question_forward.status == "Pending"
    assert question_receipt.status == "Pending"
    assert task_issue.status == "Pending"
    assert weekly_report.status == "Pending"
    assert production_confirmed.status == "Pending"
    assert confirmation_receipt.status == "Pending"
    assert FakeSMTP.sent_subjects == ["Re: 生产订单需求"]


def test_pending_auto_workflow_sender_includes_production_rejected(monkeypatch):
    session = make_session()
    set_config(session, "bot_enabled", "true", is_secret=False)
    set_config(session, "bot_email_password", "runtime-secret", is_secret=True)
    rejected = OutboundMailJob(
        mail_type="ProductionRejected",
        to_json=dumps(["sales@jimuyida.com"]),
        cc_json=dumps(["jinlei@jimuyida.com"]),
        subject="[生产驳回][PT-20260422-0001] 需补充确认",
        body="生产部驳回",
        idempotency_key="production-rejected-auto",
        status="Pending",
    )
    session.add(rejected)
    session.commit()

    class FakeSMTP:
        sent_subjects = []

        def __init__(self, host, port):
            pass

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, traceback):
            return False

        def login(self, username, password):
            assert password == "runtime-secret"

        def send_message(self, msg, from_addr, to_addrs):
            self.sent_subjects.append(msg["Subject"])

    monkeypatch.setattr("backend.app.services.mail_adapter.smtplib.SMTP_SSL", FakeSMTP)

    result = send_pending_auto_workflow_mails_smtp(session, limit=10)
    session.commit()

    assert result == {"sent": 1, "failed": 0, "total": 1}
    assert rejected.status == "Sent"
    assert FakeSMTP.sent_subjects == [rejected.subject]


def test_order_change_and_cancel_are_routed_to_correct_flow():
    session = make_session()
    configure_department(session)
    task = create_valid_task(session, order_no="SO-CHANGE-001")
    approve_task(session, task.id, actor="tester")
    session.commit()

    change_mail = create_inbound_mail(
        session,
        from_address="sales@jimuyida.com",
        subject=f"订单变更 - {task.task_no}",
        body_text="\n".join(
            [
                "订单号：SO-CHANGE-001",
                "产品：积木展示架 B2",
                "数量：150套",
                "期望交期：2026-05-25",
            ]
        ),
    )
    change_result = process_mail_direct(session, change_mail)
    session.commit()

    assert change_result is not None
    assert task.status == "ReissueDrafted"
    assert task.current_version_no == 2
    assert task.requirement.product_summary == "积木展示架 B2"

    cancel_mail = create_inbound_mail(
        session,
        from_address="sales@jimuyida.com",
        subject=f"取消订单 - {task.task_no}",
        body_text="订单取消，请暂停处理。",
    )
    cancel_result = process_mail_direct(session, cancel_mail)
    session.commit()
    sales_notice = session.query(OutboundMailJob).filter_by(mail_type="SalesDemandWithdrawn", related_task_id=task.id).one()
    production_notice = session.query(OutboundMailJob).filter_by(mail_type="ProductionDemandWithdrawn", related_task_id=task.id).one()

    assert cancel_result is None
    assert task.status == "Closed"
    assert task.closed_reason == "WithdrawnBySales"
    assert task.manual_takeover is False
    assert as_list(sales_notice.to_json) == ["sales@jimuyida.com"]
    assert as_list(production_notice.to_json) == ["production@jimuyida.com"]
    assert session.query(ExceptionCase).filter_by(exception_type="OrderCancelManualReview").count() == 0


def test_manual_force_close_task_sends_sales_and_production_notice():
    session = make_session()
    configure_department(session)
    task = create_valid_task(session, order_no="SO-MANUAL-CLOSE-001")
    jobs = force_close_task_manual(session, task.id, reason="商务人工终止", actor="tester")
    session.commit()

    sales_notice = session.query(OutboundMailJob).filter_by(mail_type="TaskManualClosedSales", related_task_id=task.id).one()
    production_notice = session.query(OutboundMailJob).filter_by(mail_type="TaskManualClosedProduction", related_task_id=task.id).one()

    assert len(jobs) == 2
    assert task.status == "Closed"
    assert task.closed_reason == "ManualForceClosed"
    assert task.manual_takeover is True
    assert task.requirement.status == "Closed"
    assert as_list(sales_notice.to_json) == ["sales@jimuyida.com"]
    assert as_list(production_notice.to_json) == ["production@jimuyida.com"]
    assert "商务人工终止" in sales_notice.body
    assert "商务人工终止" in production_notice.body


def test_manual_force_close_closed_task_raises():
    session = make_session()
    configure_department(session)
    task = create_valid_task(session, order_no="SO-MANUAL-CLOSE-002")
    record_production_feedback(session, task.id, "confirmed", "已确认排产")
    session.commit()

    with pytest.raises(ValueError, match="already closed"):
        force_close_task_manual(session, task.id, reason="再次关闭", actor="tester")


def test_production_termination_uses_dedicated_notice_types():
    session = make_session()
    configure_department(session)
    task = create_valid_task(session, order_no="SO-PRODUCTION-TERMINATE")
    session.commit()

    terminate_mail = create_inbound_mail(
        session,
        from_address="production@jimuyida.com",
        subject=f"终止生产 - {task.task_no}",
        body_text=f"生产侧终止生产，请停止该任务 {task.task_no}。",
    )
    result = process_mail_direct(session, terminate_mail)
    session.commit()

    sales_notice = session.query(OutboundMailJob).filter_by(mail_type="ProductionTerminateSalesNotice", related_task_id=task.id).one()
    production_notice = session.query(OutboundMailJob).filter_by(mail_type="ProductionTerminateProductionNotice", related_task_id=task.id).one()
    assert result == [sales_notice, production_notice]
    assert terminate_mail.classification == "ProductionTerminateRequest"
    assert task.status == "Closed"
    assert task.closed_reason == "ProductionTerminated"
    assert as_list(sales_notice.to_json) == ["sales@jimuyida.com"]
    assert as_list(production_notice.to_json) == ["production@jimuyida.com"]
    assert "生产侧已终止" in sales_notice.body
    assert session.query(OutboundMailJob).filter_by(mail_type="SalesDemandWithdrawn", related_task_id=task.id).count() == 0
    assert session.query(OutboundMailJob).filter_by(mail_type="ProductionDemandWithdrawn", related_task_id=task.id).count() == 0


def test_duplicate_sales_requirement_within_24h_sends_no_repeat_notice():
    session = make_session()
    configure_department(session)
    first_mail = create_inbound_mail(
        session,
        from_address="sales@jimuyida.com",
        subject="生产订单需求 - 重复提交1",
        body_text="\n".join(
            [
                "客户名称：重复客户",
                "产品：重复展台",
                "数量：10套",
                "期望交期：2026-08-20",
                "订单号：SO-REPEAT-001",
            ]
        ),
    )
    first_task = create_task_from_mail(session, first_mail)
    assert first_task is not None
    session.commit()

    duplicate_mail = create_inbound_mail(
        session,
        from_address="sales@jimuyida.com",
        subject="生产订单需求 - 重复提交2",
        body_text="\n".join(
            [
                "客户名称：重复客户",
                "产品：重复展台",
                "数量：10套",
                "期望交期：2026-08-20",
                "订单号：SO-REPEAT-001",
            ]
        ),
    )
    duplicate_task = create_task_from_mail(session, duplicate_mail)
    session.commit()

    duplicate_notice = session.query(OutboundMailJob).filter_by(mail_type="DuplicateSubmissionNotice").one()
    assert duplicate_task is None
    assert session.query(ProductionTask).count() == 1
    assert "请勿重复提交" in duplicate_notice.subject
    assert f"已受理任务号：{first_task.task_no}" in duplicate_notice.body
    assert as_list(duplicate_notice.to_json) == ["sales@jimuyida.com"]


def test_sales_cancel_after_production_confirmed_is_rejected():
    session = make_session()
    configure_department(session)
    task = create_valid_task(session, order_no="SO-CANCEL-AFTER-CONFIRMED")
    record_production_feedback(session, task.id, "confirmed", "确认排产")
    session.commit()

    cancel_mail = create_inbound_mail(
        session,
        from_address="sales@jimuyida.com",
        subject=f"撤回需求 - {task.task_no}",
        body_text=f"撤回需求 {task.task_no}",
    )
    result = process_mail_direct(session, cancel_mail)
    session.commit()

    reject_notice = session.query(OutboundMailJob).filter_by(mail_type="SalesDemandWithdrawRejected", related_task_id=task.id).one()
    case = session.query(ExceptionCase).filter_by(exception_type="OrderCancelAfterProductionConfirmed").one()
    assert result is None
    assert task.status == "Closed"
    assert task.closed_reason == "ScheduledConfirmed"
    assert "生产已确认排单" in reject_notice.body
    assert case.related_task_id == task.id


def process_mail_direct(session, mail):
    from backend.app.services.workflow import process_inbound_mail

    return process_inbound_mail(session, mail)


def test_cleanup_preview_execute_and_weekly_csv():
    session = make_session()
    mail = create_inbound_mail(
        session,
        from_address="newsletter@example.com",
        subject="普通通知",
        body_text="这不是订单。",
    )
    mail.classification = "NonTarget"
    mail.created_at = now_utc() - timedelta(days=40)
    session.commit()

    preview = cleanup_preview(session)
    session.commit()
    result = execute_cleanup(session, preview["cleanup_job_id"])
    session.commit()
    csv_text = weekly_report_csv(session)

    assert preview["mail_count"] == 1
    assert result["mail_count"] == 1
    assert session.get(MailMessage, mail.id) is None
    assert "section,period,product,salesperson" in csv_text
    assert "任务统计" in csv_text


def test_workflow_import_creates_published_versions():
    session = make_session()
    result = import_workflow_document(
        session,
        file_path="/Users/kaimao/github/jm-sp-bot/docs/商务部邮件下单流程梳理.docx",
        raw_text=None,
        prefer_llm=False,
        auto_publish=True,
        actor="tester",
    )
    session.commit()

    assert result["validation_errors"] == []
    assert len(result["created_versions"]) >= 5
    assert session.query(WorkflowImportJob).count() == 1
    assert session.query(WorkflowVersion).filter_by(status="Active").count() >= 5


def test_workflow_import_accepts_uploaded_text_content():
    session = make_session()
    raw_text = """
流程一: 上传流程
邮件收件人：张燕
邮件抄送人：销售直属领导
邮件主题：[上传][{{task_no}}]
邮件内容模板：
流程类型：上传流程
附件：采购订单
""".strip()

    result = import_workflow_document(
        session,
        file_path=None,
        raw_text=None,
        file_name="workflow-rules.txt",
        file_content=raw_text.encode("utf-8"),
        prefer_llm=False,
        auto_publish=False,
        actor="tester",
    )
    session.commit()

    assert result["validation_errors"] == []
    assert result["file_name"] == "workflow-rules.txt"
    assert result["source_asset_ref"] == "uploaded:workflow-rules.txt"
    version = session.get(WorkflowVersion, result["created_versions"][0]["id"])
    assert version is not None
    assert loads(version.compiled_rules_json, {})["workflow_name"] == "上传流程"


def test_workflow_import_same_doc_is_idempotent_on_versions():
    session = make_session()
    first = import_workflow_document(
        session,
        file_path="/Users/kaimao/github/jm-sp-bot/docs/商务部邮件下单流程梳理.docx",
        raw_text=None,
        prefer_llm=False,
        auto_publish=True,
        actor="tester",
    )
    session.commit()
    before_versions = session.query(WorkflowVersion).count()
    second = import_workflow_document(
        session,
        file_path="/Users/kaimao/github/jm-sp-bot/docs/商务部邮件下单流程梳理.docx",
        raw_text=None,
        prefer_llm=False,
        auto_publish=True,
        actor="tester",
    )
    session.commit()
    after_versions = session.query(WorkflowVersion).count()

    assert len(first["created_versions"]) >= 5
    assert second["validation_errors"] == []
    assert len(second["created_versions"]) == 0
    assert before_versions == after_versions


def test_workflow_list_includes_builtin_default_order_flow():
    session = make_session()
    configure_department(session)

    rows = list_workflow_rules(session, only_active=False)
    builtin = next((row for row in rows if row.get("is_builtin")), None)

    assert builtin is not None
    assert builtin["workflow_code"] == "builtin_default_order_flow"
    assert builtin["status"] == "BuiltIn"
    assert builtin["editable"] is False
    assert "production@jimuyida.com" in (builtin["rules"].get("routing", {}).get("to_names") or [])
    assert builtin["rules"]["subject_template"]
    assert builtin["rules"]["body_template"]


def test_workflow_import_falls_back_when_llm_timeout(monkeypatch):
    session = make_session()
    raw_text = """
流程一: 常规销售流程
邮件收件人：张燕
邮件抄送人：销售直属领导
邮件主题：[常规][{{task_no}}]
邮件内容模板：
流程类型：常规销售
附件：采购订单
""".strip()

    def raise_timeout(*args, **kwargs):
        raise httpx.ReadTimeout("timed out")

    monkeypatch.setattr("backend.app.services.workflow_rules.call_model", raise_timeout)
    monkeypatch.setattr("backend.app.services.workflow_rules.resolve_api_key", lambda *args, **kwargs: "mock-key")

    result = import_workflow_document(
        session,
        file_path=None,
        raw_text=raw_text,
        prefer_llm=True,
        auto_publish=True,
        actor="tester",
    )
    session.commit()

    assert result["validation_errors"] == []
    assert result["llm_used"] is False
    assert len(result["created_versions"]) == 1
    version = session.get(WorkflowVersion, result["created_versions"][0]["id"])
    assert version is not None
    rules = loads(version.compiled_rules_json, {})
    assert rules["workflow_name"] == "常规销售流程"
    assert rules["routing"]["to_names"] == ["张燕"]


def test_workflow_import_backfills_task_template_variables_when_missing():
    session = make_session()
    raw_text = """
流程一: 静态模板流程
邮件收件人：张燕
邮件主题：静态主题
邮件内容模板：
张主管，你好！
现有销售订单需要备货出货。
物料详情描述：【含物料编码、物料名称、规格型号、数量】
附件：采购订单
""".strip()

    result = import_workflow_document(
        session,
        file_path=None,
        raw_text=raw_text,
        prefer_llm=False,
        auto_publish=False,
        actor="tester",
    )
    session.commit()

    version = session.get(WorkflowVersion, result["created_versions"][0]["id"])
    rules = loads(version.compiled_rules_json, {})
    assert "{{task_no}}" in rules["subject_template"]
    assert "{{customer_name}}" in rules["subject_template"]
    for token in [
        "{{task_no}}",
        "{{version_no}}",
        "{{customer_name}}",
        "{{product_summary}}",
        "{{quantity_text}}",
        "{{expected_delivery_date}}",
        "{{workflow_name}}",
    ]:
        assert token in rules["body_template"]
    assert "原流程邮件模板" in rules["body_template"]
    assert "张主管，你好" in rules["body_template"]


def test_workflow_chat_generate_returns_normalized_rule(monkeypatch):
    session = make_session()

    def fake_call_model(*args, **kwargs):
        return {
            "choices": [
                {
                    "message": {
                        "content": dumps(
                            {
                                "assistant_reply": "流程信息齐全，已生成草稿。",
                                "ready": True,
                                "workflow_rule": {
                                    "workflow_name": "样机赠送流程",
                                    "match": {
                                        "any_keywords": ["样机赠送", "赠送"],
                                        "warehouse": "wuhan",
                                        "order_type": "sample_gift",
                                    },
                                    "routing": {"to_names": ["洪丹"], "cc_names": ["销售直属领导"]},
                                    "subject_template": "[样机赠送][{{task_no}}]",
                                    "body_template": "流程类型：样机赠送",
                                    "required_fields": ["customer_name", "product_summary", "quantity_text", "expected_delivery_date"],
                                    "required_attachments": ["审批截图"],
                                    "review_rules": [
                                        {
                                            "id": "gift-approval",
                                            "name": "审批截图校验",
                                            "field": "source_text",
                                            "operator": "contains",
                                            "value": "审批截图",
                                            "message": "缺少审批截图说明",
                                            "enabled": True,
                                        }
                                    ],
                                },
                            }
                        )
                    }
                }
            ]
        }

    monkeypatch.setattr("backend.app.services.workflow_rules._active_model", lambda _session: object())
    monkeypatch.setattr("backend.app.services.workflow_rules.call_model", fake_call_model)

    result = chat_generate_workflow_rule(
        session,
        messages=[{"role": "user", "content": "新增样机赠送流程，收件人洪丹。"}],
        current_rule=None,
    )

    assert result["ready"] is True
    assert result["validation_errors"] == []
    assert result["reply"] == "流程信息齐全，已生成草稿。"
    assert "自动生成该流程对应规则" in result["notification"]
    assert result["compiled_rule"]["workflow_name"] == "样机赠送流程"
    assert result["compiled_rule"]["routing"]["to_names"] == ["洪丹"]
    assert len(result["compiled_rule"]["review_rules"]) == 1


def test_workflow_chat_generate_guides_user_when_definition_incomplete(monkeypatch):
    session = make_session()

    def fake_call_model(*args, **kwargs):
        return {
            "choices": [
                {
                    "message": {
                        "content": dumps(
                            {
                                "assistant_reply": "先记录到流程草稿。",
                                "ready": True,
                                "workflow_rule": {
                                    "workflow_name": "新流程",
                                    "routing": {"to_names": [], "cc_names": []},
                                    "match": {"any_keywords": ["流程"], "order_type": "normal_sales"},
                                    "required_fields": ["customer_name", "product_summary", "quantity_text", "expected_delivery_date"],
                                },
                            }
                        )
                    }
                }
            ]
        }

    monkeypatch.setattr("backend.app.services.workflow_rules._active_model", lambda _session: object())
    monkeypatch.setattr("backend.app.services.workflow_rules.call_model", fake_call_model)

    result = chat_generate_workflow_rule(
        session,
        messages=[{"role": "user", "content": "先建一个新流程"}],
        current_rule=None,
    )

    assert result["ready"] is False
    assert result["compiled_rule"] is not None
    assert "主送给谁" in result["next_question"]
    assert result["pending_questions"]
    assert result["notification"] == ""


def test_workflow_chat_generate_backfills_name_from_user_turn_when_rule_missing(monkeypatch):
    session = make_session()

    def fake_call_model(*args, **kwargs):
        return {
            "choices": [
                {
                    "message": {
                        "content": dumps(
                            {
                                "assistant_reply": "流程名称已确认。",
                                "ready": False,
                                "workflow_rule": None,
                            }
                        )
                    }
                }
            ]
        }

    monkeypatch.setattr("backend.app.services.workflow_rules._active_model", lambda _session: object())
    monkeypatch.setattr("backend.app.services.workflow_rules.call_model", fake_call_model)

    result = chat_generate_workflow_rule(
        session,
        messages=[
            {"role": "assistant", "content": "请先告诉我这个新流程的名称。"},
            {"role": "user", "content": "新流程的名称就是“测试流程”"},
        ],
        current_rule=None,
    )

    assert result["ready"] is False
    assert result["compiled_rule"] is not None
    assert result["compiled_rule"]["workflow_name"] == "测试流程"
    assert result["next_question"].startswith("该流程邮件主送给谁")
    assert result["pending_questions"]
    assert "名称" not in result["pending_questions"][0]


def test_workflow_chat_generate_detects_existing_flow_for_edit(monkeypatch):
    session = make_session()
    import_result = import_structured_workflow_rules(
        session,
        rules=[
            {
                "workflow_code": "transfer_flow",
                "workflow_name": "新机调拨流程",
                "routing": {"to_names": ["张燕"], "cc_names": ["销售直属领导"]},
                "match": {"any_keywords": ["新机调拨"], "order_type": "transfer"},
                "subject_template": "[新机调拨][{{task_no}}]",
                "body_template": "流程类型：新机调拨",
                "required_fields": ["customer_name", "product_summary", "quantity_text", "expected_delivery_date"],
                "required_attachments": [],
                "review_rules": [],
            }
        ],
        actor="tester",
        auto_publish=False,
        source_asset_ref="workflow-chat",
    )
    session.commit()
    version_id = import_result["created_versions"][0]["id"]

    def fake_call_model(*args, **kwargs):
        messages = kwargs.get("messages") or []
        assert any("当前任务是编辑已有流程" in item.get("content", "") for item in messages)
        return {
            "choices": [
                {
                    "message": {
                        "content": dumps(
                            {
                                "assistant_reply": "已在原流程上增加必填字段。",
                                "ready": True,
                                "workflow_rule": {
                                    "workflow_name": "新增的错误流程名",
                                    "required_fields": [
                                        "customer_name",
                                        "product_summary",
                                        "quantity_text",
                                        "expected_delivery_date",
                                        "initiator",
                                        "expected_time",
                                    ],
                                },
                            }
                        )
                    }
                }
            ]
        }

    monkeypatch.setattr("backend.app.services.workflow_rules._active_model", lambda _session: object())
    monkeypatch.setattr("backend.app.services.workflow_rules.call_model", fake_call_model)

    result = chat_generate_workflow_rule(
        session,
        messages=[{"role": "user", "content": "我要重新编辑 新机调拨流程，增加必填字段包括发起人和期望时间"}],
        current_rule=None,
    )

    assert result["edit_version_id"] == version_id
    assert result["edit_workflow_name"] == "新机调拨流程"
    assert result["compiled_rule"]["workflow_code"] == "transfer_flow"
    assert result["compiled_rule"]["workflow_name"] == "新机调拨流程"
    assert "initiator" in result["compiled_rule"]["required_fields"]


def test_import_structured_workflow_rules_creates_draft_version():
    session = make_session()
    result = import_structured_workflow_rules(
        session,
        rules=[
            {
                "workflow_name": "对话生成流程",
                "routing": {"to_names": ["张燕"], "cc_names": ["销售直属领导"]},
                "match": {"any_keywords": ["对话流程"], "order_type": "normal_sales"},
                "subject_template": "[对话流程][{{task_no}}]",
                "body_template": "流程类型：对话流程",
                "required_fields": ["customer_name", "product_summary", "quantity_text", "expected_delivery_date"],
                "required_attachments": ["采购订单"],
                "review_rules": [
                    {
                        "id": "chat-rule-1",
                        "name": "采购订单校验",
                        "field": "source_text",
                        "operator": "contains",
                        "value": "采购订单",
                        "message": "缺少采购订单信息",
                        "enabled": True,
                    }
                ],
            }
        ],
        actor="tester",
        auto_publish=False,
        source_asset_ref="workflow-chat",
    )
    session.commit()

    assert result["validation_errors"] == []
    assert len(result["created_versions"]) == 1
    version = session.get(WorkflowVersion, result["created_versions"][0]["id"])
    assert version is not None
    assert version.status == "Draft"
    assert version.source_asset_ref == "workflow-chat"


def test_save_workflow_version_rules_can_activate_after_manual_edit():
    session = make_session()
    raw_text = """
流程一: 常规销售流程
邮件收件人：张燕
邮件抄送人：销售直属领导
邮件主题：[常规][{{task_no}}]
邮件内容模板：
流程类型：常规销售
附件：采购订单
""".strip()
    result = import_workflow_document(
        session,
        file_path=None,
        raw_text=raw_text,
        prefer_llm=False,
        auto_publish=False,
        actor="tester",
    )
    session.commit()

    draft_id = result["created_versions"][0]["id"]
    draft = session.get(WorkflowVersion, draft_id)
    assert draft is not None
    assert draft.status == "Draft"
    rules = loads(draft.compiled_rules_json, {})
    rules["review_rules"] = [
        {
            "id": "manual-review-1",
            "name": "特批编码校验",
            "field": "source_text",
            "operator": "contains",
            "value": "特批编码",
            "message": "邮件缺少特批编码信息",
            "enabled": True,
        }
    ]
    rules["routing"] = {"to_names": ["洪丹"], "cc_names": ["销售直属领导", "商务负责人"]}

    saved = save_workflow_version_rules(
        session,
        draft_id,
        compiled_rules=rules,
        actor="tester",
        activate=True,
    )
    session.commit()

    assert saved.status == "Active"
    saved_rules = loads(saved.compiled_rules_json, {})
    assert len(saved_rules.get("review_rules", [])) == 1
    assert saved_rules["review_rules"][0]["name"] == "特批编码校验"
    assert saved_rules["routing"]["to_names"] == ["洪丹"]
    assert saved_rules["routing"]["cc_names"] == ["销售直属领导", "商务负责人"]


def test_edit_active_workflow_requires_deactivate_first():
    session = make_session()
    raw_text = """
流程一: 常规销售流程
邮件收件人：张燕
邮件抄送人：销售直属领导
邮件主题：[常规][{{task_no}}]
邮件内容模板：
流程类型：常规销售
附件：采购订单
""".strip()
    result = import_workflow_document(
        session,
        file_path=None,
        raw_text=raw_text,
        prefer_llm=False,
        auto_publish=True,
        actor="tester",
    )
    session.commit()

    version_id = result["created_versions"][0]["id"]
    active = session.get(WorkflowVersion, version_id)
    assert active is not None
    assert active.status == "Active"

    rules = loads(active.compiled_rules_json, {})
    rules["subject_template"] = "[更新][{{task_no}}]"
    with pytest.raises(ValueError, match="deactivated before edit"):
        save_workflow_version_rules(
            session,
            version_id,
            compiled_rules=rules,
            actor="tester",
            activate=False,
        )


def test_workflow_version_can_be_deactivated_then_updated_in_place_and_deleted():
    session = make_session()
    raw_text = """
流程一: 常规销售流程
邮件收件人：张燕
邮件抄送人：销售直属领导
邮件主题：[常规][{{task_no}}]
邮件内容模板：
流程类型：常规销售
附件：采购订单
""".strip()
    result = import_workflow_document(
        session,
        file_path=None,
        raw_text=raw_text,
        prefer_llm=False,
        auto_publish=True,
        actor="tester",
    )
    session.commit()

    version_id = result["created_versions"][0]["id"]
    archived = deactivate_workflow_version(session, version_id)
    session.commit()
    assert archived.status == "Archived"

    rules = loads(archived.compiled_rules_json, {})
    rules["subject_template"] = "[停用后编辑][{{task_no}}]"
    draft = save_workflow_version_rules(
        session,
        version_id,
        compiled_rules=rules,
        actor="tester",
        activate=False,
    )
    session.commit()
    assert draft.id == version_id
    assert draft.status == "Draft"
    assert session.query(WorkflowVersion).count() == 1

    delete_workflow_version(session, version_id)
    session.commit()
    assert session.get(WorkflowVersion, version_id) is None


def test_delete_active_workflow_requires_deactivate_first():
    session = make_session()
    raw_text = """
流程一: 常规销售流程
邮件收件人：张燕
邮件抄送人：销售直属领导
邮件主题：[常规][{{task_no}}]
邮件内容模板：
流程类型：常规销售
附件：采购订单
""".strip()
    result = import_workflow_document(
        session,
        file_path=None,
        raw_text=raw_text,
        prefer_llm=False,
        auto_publish=True,
        actor="tester",
    )
    session.commit()

    version_id = result["created_versions"][0]["id"]
    with pytest.raises(ValueError, match="deactivated before delete"):
        delete_workflow_version(session, version_id)


def test_import_workflow_document_rejects_duplicate_workflow_name():
    session = make_session()
    first_text = """
流程一: 新机调拨
邮件收件人：张燕
邮件主题：[新机调拨][{{task_no}}]
邮件内容模板：
流程类型：新机调拨
""".strip()
    second_text = """
流程一: 新机 调拨
邮件收件人：张燕
邮件主题：[新机调拨更新][{{task_no}}]
邮件内容模板：
流程类型：新机调拨更新
""".strip()

    first = import_workflow_document(
        session,
        file_path=None,
        raw_text=first_text,
        prefer_llm=False,
        auto_publish=False,
        actor="tester",
    )
    session.commit()

    second = import_workflow_document(
        session,
        file_path=None,
        raw_text=second_text,
        prefer_llm=False,
        auto_publish=False,
        actor="tester",
    )
    session.commit()

    assert first["created_versions"]
    assert second["created_versions"] == []
    assert any("流程已存在" in message for message in second["validation_errors"])
    assert session.query(WorkflowVersion).count() == 1


def test_import_workflow_document_rejects_duplicate_names_in_same_batch():
    session = make_session()
    raw_text = """
流程一: 重复流程
邮件收件人：张燕
邮件主题：[重复A][{{task_no}}]
邮件内容模板：
流程类型：重复A

流程二: 重复流程
邮件收件人：洪丹
邮件主题：[重复B][{{task_no}}]
邮件内容模板：
流程类型：重复B
""".strip()

    result = import_workflow_document(
        session,
        file_path=None,
        raw_text=raw_text,
        prefer_llm=False,
        auto_publish=False,
        actor="tester",
    )
    session.commit()

    assert len(result["created_versions"]) == 1
    assert any("本次导入的其他流程名称重复" in message for message in result["validation_errors"])


def test_workflow_specific_review_rules_block_and_allow_after_fix():
    session = make_session()
    set_config(
        session,
        "workflow_contact_map_json",
        dumps(
            {
                "张燕": "zhangyan@jimuyida.com",
                "销售直属领导": "sales.lead@jimuyida.com",
            }
        ),
        is_secret=False,
    )
    raw_text = """
流程一: 常规销售流程
邮件收件人：张燕
邮件抄送人：销售直属领导
邮件主题：[常规][{{task_no}}]
邮件内容模板：
流程类型：常规销售
附件：采购订单
""".strip()
    import_result = import_workflow_document(
        session,
        file_path=None,
        raw_text=raw_text,
        prefer_llm=False,
        auto_publish=True,
        actor="tester",
    )
    session.commit()
    version_id = import_result["created_versions"][0]["id"]
    active_version = session.get(WorkflowVersion, version_id)
    assert active_version is not None
    custom_rules = loads(active_version.compiled_rules_json, {})
    custom_rules["review_rules"] = [
        {
            "id": "special-code",
            "name": "特批编码校验",
            "field": "source_text",
            "operator": "contains",
            "value": "特批编码",
            "message": "邮件缺少特批编码信息",
            "enabled": True,
        }
    ]
    deactivate_workflow_version(session, version_id)
    save_workflow_version_rules(session, version_id, compiled_rules=custom_rules, actor="tester", activate=True)
    session.commit()

    blocked = create_inbound_mail(
        session,
        from_address="sales@jimuyida.com",
        subject="生产订单需求 - 常规销售流程",
        body_text="\n".join(
            [
                "客户名称：流程客户A",
                "产品：产品A",
                "数量：20台",
                "期望交期：2026-10-20",
                "订单号：SO-WF-REVIEW-001",
                "附件：采购订单",
            ]
        ),
    )
    blocked_task = create_task_from_mail(session, blocked)
    session.commit()

    assert blocked_task is None
    blocked_case = session.query(ExceptionCase).filter_by(exception_type="ReviewNeedManual").order_by(ExceptionCase.created_at.desc()).first()
    assert blocked_case is not None
    blocked_detail = loads(blocked_case.detail, {})
    assert any("特批编码校验" in str(item.get("rule_name", "")) for item in blocked_detail.get("review_failures", []))

    passed = create_inbound_mail(
        session,
        from_address="sales@jimuyida.com",
        subject="生产订单需求 - 常规销售流程",
        body_text="\n".join(
            [
                "客户名称：流程客户B",
                "产品：产品B",
                "数量：22台",
                "期望交期：2026-10-22",
                "订单号：SO-WF-REVIEW-002",
                "附件：采购订单",
                "特批编码：SP-7788",
            ]
        ),
    )
    passed_task = create_task_from_mail(session, passed)
    session.commit()

    assert passed_task is not None
    assert as_list(passed_task.target_mail_to_json) == ["zhangyan@jimuyida.com"]


def test_workflow_match_prefers_llm_selected_flow_when_multiple_active(monkeypatch):
    session = make_session()
    raw_text = """
流程一: 样机借用 下单
邮件收件人：张燕
邮件抄送人：销售直属领导
邮件主题：[样机借用][{{task_no}}]
邮件内容模板：
流程类型：样机借用
附件：样机借用审批截图

流程二: 常规销售 下单
邮件收件人：洪丹
邮件抄送人：销售直属领导
邮件主题：[常规销售][{{task_no}}]
邮件内容模板：
流程类型：常规销售
附件：采购订单
""".strip()
    import_workflow_document(
        session,
        file_path=None,
        raw_text=raw_text,
        prefer_llm=False,
        auto_publish=True,
        actor="tester",
    )
    session.commit()

    versions = session.query(WorkflowVersion).filter_by(status="Active").all()
    code_by_name = {}
    for version in versions:
        rule = loads(version.compiled_rules_json, {})
        code_by_name[str(rule.get("workflow_name"))] = str(rule.get("workflow_code"))
    sample_code = code_by_name["样机借用 下单"]

    def fake_call_model(*args, **kwargs):
        return {
            "choices": [
                {
                    "message": {
                        "content": dumps(
                            {
                                "workflow_code": sample_code,
                                "confidence": 89,
                                "reason": "邮件提到样机借用审批截图，优先走样机借用流程。",
                            }
                        )
                    }
                }
            ]
        }

    monkeypatch.setattr("backend.app.services.workflow_rules._active_model", lambda _session: object())
    monkeypatch.setattr("backend.app.services.workflow_rules.call_model", fake_call_model)

    mail = create_inbound_mail(
        session,
        from_address="sales@jimuyida.com",
        subject="生产订单需求 - 需要样机借用",
        body_text="客户名称：测试客户\n产品：G100\n数量：10台\n期望交期：2026-08-01\n样机借用审批截图：已上传",
    )
    match = match_workflow_for_mail(session, mail, mail.body_text)

    assert match is not None
    assert match.rule["workflow_code"] == sample_code
    assert match.confidence == 89
    assert any("LLM判定" in reason for reason in match.reasons)


def test_supplement_reply_uses_full_context_for_workflow_required_fields():
    session = make_session()
    set_config(
        session,
        "workflow_contact_map_json",
        dumps(
            {
                "张燕": "zhangyan@jimuyida.com",
                "洪丹": "hongdan@jimuyida.com",
                "销售直属领导": "sales.lead@jimuyida.com",
            }
        ),
        is_secret=False,
    )
    raw_text = """
流程一: 样机借用 下单
邮件收件人：张燕
邮件抄送人：销售直属领导
邮件主题：[样机借用][{{task_no}}]
邮件内容模板：
流程类型：样机借用
附件：样机借用审批截图

流程二: 常规销售 下单
邮件收件人：洪丹
邮件抄送人：销售直属领导
邮件主题：[常规销售][{{task_no}}]
邮件内容模板：
流程类型：常规销售
附件：采购订单
""".strip()
    import_workflow_document(
        session,
        file_path=None,
        raw_text=raw_text,
        prefer_llm=False,
        auto_publish=True,
        actor="tester",
    )
    session.commit()

    original = create_inbound_mail(
        session,
        from_address="sales@jimuyida.com",
        subject="生产订单需求 - 样机借用 下单",
        body_text="\n".join(
            [
                "客户名称：样机客户",
                "产品：样机机型X",
                "数量：5台",
                "订单号：SO-SAMPLE-CTX-001",
                "样机借用审批截图：已附图",
            ]
        ),
    )
    task = create_task_from_mail(session, original)
    session.commit()
    assert task is None

    reply = create_inbound_mail(
        session,
        from_address="sales@jimuyida.com",
        subject="Re: 订单信息待补充",
        body_text="期望交期：2026-09-30",
    )
    created = process_mail_direct(session, reply)
    session.commit()

    assert created is not None
    assert as_list(created.target_mail_to_json) == ["zhangyan@jimuyida.com"]
    binding = session.query(RequirementWorkflowBinding).filter_by(requirement_id=created.requirement_id).one()
    assert binding.workflow_name == "样机借用 下单"
    assert "样机借用审批截图" not in as_list(binding.missing_fields_json)


def test_workflow_review_rules_are_not_mixed_with_global_initial_review_rules():
    session = make_session()
    set_config(
        session,
        "initial_review_rules_json",
        dumps(
            [
                {
                    "id": "global-blocker",
                    "name": "全局阻断规则",
                    "field": "source_text",
                    "operator": "contains",
                    "value": "永远不会出现",
                    "message": "命中全局阻断规则",
                    "enabled": True,
                }
            ]
        ),
        is_secret=False,
    )
    set_config(
        session,
        "workflow_contact_map_json",
        dumps(
            {
                "张燕": "zhangyan@jimuyida.com",
                "销售直属领导": "sales.lead@jimuyida.com",
            }
        ),
        is_secret=False,
    )
    raw_text = """
流程一: 常规销售流程
邮件收件人：张燕
邮件抄送人：销售直属领导
邮件主题：[常规][{{task_no}}]
邮件内容模板：
流程类型：常规销售
附件：采购订单
""".strip()
    import_workflow_document(
        session,
        file_path=None,
        raw_text=raw_text,
        prefer_llm=False,
        auto_publish=True,
        actor="tester",
    )
    session.commit()

    mail = create_inbound_mail(
        session,
        from_address="sales@jimuyida.com",
        subject="生产订单需求 - 常规销售流程",
        body_text="\n".join(
            [
                "客户名称：流程客户",
                "产品：常规产品A",
                "数量：30台",
                "期望交期：2026-10-10",
                "订单号：SO-WF-NO-GLOBAL-001",
                "附件：采购订单",
            ]
        ),
    )
    task = create_task_from_mail(session, mail)
    session.commit()

    assert task is not None
    assert as_list(task.target_mail_to_json) == ["zhangyan@jimuyida.com"]
    latest_review_case = (
        session.query(ExceptionCase)
        .filter_by(exception_type="ReviewNeedManual")
        .order_by(ExceptionCase.created_at.desc())
        .first()
    )
    if latest_review_case is not None:
        detail = loads(latest_review_case.detail, {})
        assert not any("全局阻断规则" in str(item.get("rule_name", "")) for item in detail.get("review_failures", []))


def test_initial_review_config_syncs_workflow_review_rules_as_custom_rules():
    session = make_session()
    set_config(
        session,
        "workflow_contact_map_json",
        dumps({"张燕": "zhangyan@jimuyida.com"}),
        is_secret=False,
    )
    import_structured_workflow_rules(
        session,
        rules=[
            {
                "workflow_code": "custom_review_flow",
                "workflow_name": "带规则流程",
                "match": {"any_keywords": ["带规则流程"]},
                "routing": {"to_names": ["张燕"]},
                "subject_template": "[带规则][{{task_no}}]",
                "body_template": "流程类型：{{workflow_name}}",
                "required_fields": [],
                "required_attachments": [],
                "review_rules": [
                    {
                        "id": "workflow-special-code",
                        "name": "特批编码校验",
                        "field": "source_text",
                        "operator": "contains",
                        "value": "特批编码",
                        "message": "邮件缺少特批编码信息",
                        "enabled": True,
                    }
                ],
            }
        ],
        actor="tester",
        auto_publish=True,
    )
    session.commit()

    display_config = initial_review_config(session, include_workflow_rules=True)
    execution_config = initial_review_config(session)

    workflow_rule = next(rule for rule in display_config["rules"] if rule.get("name") == "特批编码校验")
    assert workflow_rule.get("read_only") is not True
    assert workflow_rule.get("is_builtin") is not True
    assert workflow_rule["is_workflow_rule"] is True
    assert workflow_rule["workflow_name"] == "带规则流程"
    assert workflow_rule["id"].startswith("workflow:")
    assert any(rule.get("name") == "特批编码校验" for rule in execution_config["rules"])


def test_deleted_workflow_review_rule_is_not_restored_by_sync():
    session = make_session()
    import_structured_workflow_rules(
        session,
        rules=[
            {
                "workflow_code": "deletable_review_flow",
                "workflow_name": "可删除规则流程",
                "match": {"any_keywords": ["可删除规则流程"]},
                "routing": {"to_names": ["张燕"]},
                "subject_template": "[可删除][{{task_no}}]",
                "body_template": "流程类型：{{workflow_name}}",
                "required_fields": [],
                "required_attachments": [],
                "review_rules": [
                    {
                        "id": "deletable-code",
                        "name": "可删除规则",
                        "field": "source_text",
                        "operator": "contains",
                        "value": "特批编码",
                        "message": "邮件缺少特批编码信息",
                        "enabled": True,
                    }
                ],
            }
        ],
        actor="tester",
        auto_publish=True,
    )
    session.commit()

    first_config = initial_review_config(session, include_workflow_rules=True)
    workflow_rule_id = next(rule["id"] for rule in first_config["rules"] if rule.get("name") == "可删除规则")
    remember_deleted_workflow_review_rules(
        session,
        {str(rule.get("id")) for rule in first_config["rules"] if rule.get("id") != workflow_rule_id},
    )
    set_config(
        session,
        "initial_review_rules_json",
        dumps([rule for rule in first_config["rules"] if rule.get("id") != workflow_rule_id and not rule.get("is_builtin")]),
        is_secret=False,
    )
    session.commit()

    second_config = initial_review_config(session, include_workflow_rules=True)
    assert not any(rule.get("id") == workflow_rule_id for rule in second_config["rules"])


WORKFLOW_CONTACT_MAP = {
    "张燕": "zhangyan@jimuyida.com",
    "单涛": "dantao@jimuyida.com",
    "丁总": "dingyong@jimuyida.com",
    "金总": "jinzong@jimuyida.com",
    "罗总": "luozong@jimuyida.com",
    "张杏": "zhangxing@jimuyida.com",
    "洪丹": "hongdan@jimuyida.com",
    "曾鲜艳": "zengxianyan@jimuyida.com",
    "余烁": "yushuo@jimuyida.com",
    "袁辉": "yuanhui@jimuyida.com",
    "包亚敏": "baoyamin@jimuyida.com",
    "张洁仪": "zhangjieyi@jimuyida.com",
    "邢惠玲": "xinghuiling@jimuyida.com",
    "宋勤红": "songqinhong@jimuyida.com",
    "蒋文俊": "jiangwenjun@jimuyida.com",
    "张文鹏": "zhangwenpeng@jimuyida.com",
    "吴婉真": "wuwanzhen@jimuyida.com",
    "徐升": "xusheng@jimuyida.com",
    "销售直属领导": "sales.lead@jimuyida.com",
}


WORKFLOW_CASES = [
    {
        "name": "武汉仓出货硬件正常销售订单/样机赠送/电商平台/海外电商、渠道备货",
        "subject": "生产订单需求 - 武汉仓出货硬件正常销售订单",
        "expected_to": "zhangyan@jimuyida.com",
        "missing_label": "物流发货方式",
        "lines": [
            "客户名称：流程客户A",
            "产品：武汉仓标准设备A",
            "数量：20台",
            "期望交期：2026-07-01",
            "订单号：SO-WF-MATRIX-001",
            "物料详情描述：编码A1，规格标准版，20台",
            "物流发货方式：顺丰",
            "出货时间要求：2026-06-28",
            "客户收件信息：深圳南山区xx路",
            "交付要求：木箱加固",
            "附件：深圳积木与湖北积木的采购订单文档、海外渠道销售PI、特殊附作等",
        ],
    },
    {
        "name": "武汉仓出货硬件独立站补单/假期订单补单",
        "subject": "生产订单需求 - 武汉仓出货硬件独立站补单",
        "expected_to": "zhangyan@jimuyida.com",
        "missing_label": "物料详情描述",
        "lines": [
            "客户名称：流程客户B",
            "产品：独立站补单设备B",
            "数量：3台",
            "期望交期：2026-07-02",
            "订单号：SO-WF-MATRIX-002",
            "物料详情描述：编码B1，假期补单，3台",
            "附件：深圳积木与湖北积木的采购订单文档、海外渠道销售PI、特殊附作等",
        ],
    },
    {
        "name": "武汉仓出货硬件销售样机借用",
        "subject": "生产订单需求 - 武汉仓出货硬件销售样机借用",
        "expected_to": "zhangyan@jimuyida.com",
        "missing_label": "样机借用审批截图",
        "lines": [
            "客户名称：流程客户C",
            "产品：武汉仓样机C",
            "数量：1台",
            "期望交期：2026-07-03",
            "订单号：SO-WF-MATRIX-003",
            "物料详情描述：编码C1，样机，1台",
            "借用时间：2026-07-03至2026-07-20",
            "物流发货方式：顺丰",
            "出货时间要求：2026-07-03",
            "客户收件信息：广州天河区xx路",
            "样机借用审批截图：已上传",
            "附件：深圳积木与湖北积木的采购订单文档",
        ],
    },
    {
        "name": "海外仓出货硬件销售订单/样机赠送",
        "subject": "生产订单需求 - 海外仓出货硬件销售订单",
        "expected_to": "dantao@jimuyida.com",
        "missing_label": "出货仓/借货仓",
        "lines": [
            "客户名称：流程客户D",
            "产品：海外仓设备D",
            "数量：8台",
            "期望交期：2026-07-04",
            "订单号：SO-WF-MATRIX-004",
            "物料详情描述：编码D1，海外仓设备，8台",
            "物流发货方式：DHL",
            "出货仓：美国仓",
            "客户收件信息：海外客户地址",
            "交付要求：按PI发货",
            "附件：海外渠道销售PI、特殊附作等",
        ],
    },
    {
        "name": "海外仓出货硬件销售样机借用",
        "subject": "生产订单需求 - 海外仓出货硬件销售样机借用",
        "expected_to": "dantao@jimuyida.com",
        "missing_label": "归还时间",
        "lines": [
            "客户名称：流程客户E",
            "产品：海外仓样机E",
            "数量：1台",
            "期望交期：2026-07-05",
            "订单号：SO-WF-MATRIX-005",
            "物料详情描述：编码E1，海外样机，1台",
            "归还时间：2026-08-05",
            "出货仓：德国仓",
            "客户收件信息：海外样机地址",
            "样机借用审批截图：已上传",
        ],
    },
]


def prepare_imported_workflow_session():
    session = make_session()
    set_config(session, "workflow_contact_map_json", dumps(WORKFLOW_CONTACT_MAP), is_secret=False)
    import_workflow_document(
        session,
        file_path="/Users/kaimao/github/jm-sp-bot/docs/商务部邮件下单流程梳理.docx",
        raw_text=None,
        prefer_llm=False,
        auto_publish=True,
        actor="tester",
    )
    session.commit()
    return session


@pytest.mark.parametrize("case", WORKFLOW_CASES, ids=[item["name"] for item in WORKFLOW_CASES])
def test_imported_business_workflow_cases_pass_initial_review_and_route(case):
    session = prepare_imported_workflow_session()
    mail = create_inbound_mail(
        session,
        from_address="bot.sales@jimuyida.com",
        subject=case["subject"],
        body_text="\n".join(case["lines"]),
    )

    task = create_task_from_mail(session, mail)
    session.commit()

    assert task is not None
    assert as_list(task.target_mail_to_json)[0] == case["expected_to"]
    binding = session.query(RequirementWorkflowBinding).filter_by(requirement_id=task.requirement_id).one()
    assert binding.workflow_name == case["name"]
    assert as_list(binding.missing_fields_json) == []


@pytest.mark.parametrize("case", WORKFLOW_CASES, ids=[item["name"] for item in WORKFLOW_CASES])
def test_imported_business_workflow_cases_fail_initial_review_when_required_field_missing(case):
    session = prepare_imported_workflow_session()
    missing_label = case["missing_label"]
    lines = [line for line in case["lines"] if not line.startswith(f"{missing_label.split('/')[0]}：")]
    mail = create_inbound_mail(
        session,
        from_address="bot.sales@jimuyida.com",
        subject=f"{case['subject']} - 缺字段",
        body_text="\n".join(lines),
    )

    task = create_task_from_mail(session, mail)
    session.commit()

    assert task is None
    case_row = (
        session.query(ExceptionCase)
        .filter_by(exception_type="ReviewNeedManual")
        .order_by(ExceptionCase.created_at.desc())
        .first()
    )
    assert case_row is not None
    detail = loads(case_row.detail, {})
    assert any(missing_label in item.get("message", "") for item in detail.get("review_failures", []))


def test_imported_workflow_routes_task_after_contact_mapping():
    session = make_session()
    set_config(
        session,
        "workflow_contact_map_json",
        dumps(
            {
                "张燕": "zhangyan@jimuyida.com",
                "丁总": "dingyong@jimuyida.com",
                "金总": "jinzong@jimuyida.com",
                "罗总": "luozong@jimuyida.com",
                "张杏": "zhangxing@jimuyida.com",
                "洪丹": "hongdan@jimuyida.com",
                "曾鲜艳": "zengxianyan@jimuyida.com",
                "余烁": "yushuo@jimuyida.com",
                "单涛": "dantao@jimuyida.com",
                "袁辉": "yuanhui@jimuyida.com",
                "包亚敏": "baoyamin@jimuyida.com",
                "张洁仪": "zhangjieyi@jimuyida.com",
                "邢惠玲": "xinghuiling@jimuyida.com",
                "宋勤红": "songqinhong@jimuyida.com",
                "销售直属领导": "sales.lead@jimuyida.com",
            }
        ),
        is_secret=False,
    )
    import_workflow_document(
        session,
        file_path="/Users/kaimao/github/jm-sp-bot/docs/商务部邮件下单流程梳理.docx",
        raw_text=None,
        prefer_llm=False,
        auto_publish=True,
        actor="tester",
    )
    session.commit()

    mail = create_inbound_mail(
        session,
        from_address="sales@jimuyida.com",
        subject="生产订单需求 - 武汉仓出货硬件正常销售订单",
        body_text="\n".join(
            [
                "客户名称：测试客户",
                "产品：G100",
                "数量：20台",
                "期望交期：2026-07-01",
                "订单号：SO-WF-001",
                "物料详情描述：编码A1，规格标准版，20台",
                "物流发货方式：顺丰",
                "出货时间要求：2026-06-28",
                "客户收件信息：深圳南山区xx路",
                "交付要求：木箱加固",
                "附件：深圳积木与湖北积木的采购订单文档、海外渠道销售PI、特殊附作等",
            ]
        ),
    )

    task = create_task_from_mail(session, mail)
    session.commit()

    assert task is not None
    assert as_list(task.target_mail_to_json) == ["zhangyan@jimuyida.com"]
    binding = session.query(RequirementWorkflowBinding).filter_by(requirement_id=task.requirement_id).one()
    assert binding.workflow_code
    assert "物流发货方式" not in as_list(binding.missing_fields_json)


def test_imported_workflow_without_contact_mapping_fails_review():
    session = make_session()
    import_workflow_document(
        session,
        file_path="/Users/kaimao/github/jm-sp-bot/docs/商务部邮件下单流程梳理.docx",
        raw_text=None,
        prefer_llm=False,
        auto_publish=True,
        actor="tester",
    )
    session.commit()
    mail = create_inbound_mail(
        session,
        from_address="sales@jimuyida.com",
        subject="生产订单需求 - 武汉仓出货硬件正常销售订单",
        body_text="\n".join(
            [
                "客户名称：测试客户",
                "产品：G100",
                "数量：20台",
                "期望交期：2026-07-01",
                "订单号：SO-WF-002",
                "物料详情描述：编码A1，规格标准版，20台",
                "物流发货方式：顺丰",
                "出货时间要求：2026-06-28",
                "客户收件信息：深圳南山区xx路",
                "交付要求：木箱加固",
                "附件：深圳积木与湖北积木的采购订单文档、海外渠道销售PI、特殊附作等",
            ]
        ),
    )

    task = create_task_from_mail(session, mail)
    session.commit()

    assert task is None
    case = session.query(ExceptionCase).filter_by(exception_type="ReviewNeedManual").order_by(ExceptionCase.created_at.desc()).first()
    assert case is not None
    detail = loads(case.detail, {})
    assert any("流程收件人未映射邮箱" in item.get("message", "") for item in detail.get("review_failures", []))
