from __future__ import annotations
import logging
from typing import Optional
from pydantic import BaseModel, Field
from .base import BaseSkill, SkillResult
from .registry import SkillRegistry
from backend.app.services.mail_adapter import (
    sync_imap_mailbox, 
    send_pending_smtp, 
    send_pending_auto_workflow_mails_smtp
)

logger = logging.getLogger(__name__)

class MailSkillInput(BaseModel):
    limit: int = Field(default=20, description="Maximum number of emails to process")

@SkillRegistry.register
class ReceiveMailsSkill(BaseSkill):
    name = "receive_mails"
    description = "从配置的腾讯企业邮箱收取新邮件并存入系统进行分类和解析。"
    input_schema = MailSkillInput

    async def execute(self, limit: int = 20) -> SkillResult:
        try:
            result = sync_imap_mailbox(self.session, limit=limit)
            return SkillResult(
                success=True,
                message=f"Successfully imported {result.get('imported', 0)} mails.",
                data=result
            )
        except Exception as e:
            logger.exception("ReceiveMailsSkill failed")
            return SkillResult(success=False, message=str(e), error=str(e))

@SkillRegistry.register
class SendHighPriorityMailsSkill(BaseSkill):
    name = "send_high_priority_mails"
    description = "仅发送高优先级（优先级 < 30）的待发邮件，如收件回执、业务推进和任务单。"
    input_schema = MailSkillInput

    async def execute(self, limit: int = 20) -> SkillResult:
        try:
            from backend.app.services.mail_worker import _send_high_priority_mails
            result = _send_high_priority_mails(self.session, limit=limit)
            return SkillResult(
                success=True,
                message=f"Successfully sent {result.get('sent', 0)} high priority mails.",
                data=result
            )
        except Exception as e:
            logger.exception("SendHighPriorityMailsSkill failed")
            return SkillResult(success=False, message=str(e), error=str(e))

@SkillRegistry.register
class SendAutoWorkflowMailsSkill(BaseSkill):
    name = "send_auto_workflow_mails"
    description = "发送自动流转类邮件，通常为低优先级通知或周报。"
    input_schema = MailSkillInput

    async def execute(self, limit: int = 20) -> SkillResult:
        try:
            result = send_pending_auto_workflow_mails_smtp(self.session, limit=limit)
            return SkillResult(
                success=True,
                message=f"Successfully sent {result.get('sent', 0)} auto workflow mails.",
                data=result
            )
        except Exception as e:
            logger.exception("SendAutoWorkflowMailsSkill failed")
            return SkillResult(success=False, message=str(e), error=str(e))
