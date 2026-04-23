from __future__ import annotations

import io
import zipfile
from datetime import timedelta
from email.message import EmailMessage

import pytest
from docx import Document
from openpyxl import Workbook
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from backend.app.database import Base
from backend.app.models import (
    AttachmentAsset,
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
    SystemConfig,
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
    store_incoming_email,
)
from backend.app.services.mail_throttle import clamp_mail_interval_seconds, reset_mail_login_throttle
from backend.app.services.model_provider import build_openai_chat_payload, extract_chat_content, resolve_api_key
from backend.app.services.operations import cleanup_preview, execute_cleanup, weekly_report_csv
from backend.app.services.pdf import simple_pdf
from backend.app.services.workflow import (
    apply_exception_requirement_patch,
    approve_task,
    create_inbound_mail,
    create_task_from_mail,
    enqueue_weekly_report,
    record_exception_case,
    record_production_feedback,
    retry_outbound_mail,
    set_weekly_report_recipients,
    weekly_report_recipients,
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
    model = session.query(ModelProviderConfig).one()
    assert model.title == "Dify deepseekV3"
    assert model.credential_ref == "env:MODEL_API_KEY"


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
    assert "排队处理中" in ack.body
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


def test_send_selected_smtp_only_sends_requested_jobs(monkeypatch):
    session = make_session()
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


def test_pending_auto_workflow_sender_includes_task_issues_and_questions(monkeypatch):
    session = make_session()
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
    case = session.query(ExceptionCase).filter_by(exception_type="OrderCancelManualReview").one()

    assert cancel_result is None
    assert task.status == "CancelReview"
    assert task.manual_takeover is True
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
