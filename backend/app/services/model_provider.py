from __future__ import annotations

import os
import time
from typing import Any

import httpx
from sqlalchemy.orm import Session

from backend.app.models import ModelCallLog, ModelProviderConfig, SystemConfig
from backend.app.services.jsonutil import dumps


def build_openai_chat_payload(model_name: str, messages: list[dict[str, str]], *, temperature: float = 0) -> dict[str, Any]:
    return {"model": model_name, "messages": messages, "temperature": temperature}


def extract_chat_content(output: dict[str, Any]) -> str:
    choices = output.get("choices") if isinstance(output, dict) else None
    if not isinstance(choices, list) or not choices:
        return ""
    first = choices[0] if isinstance(choices[0], dict) else {}
    message = first.get("message") if isinstance(first.get("message"), dict) else {}
    content = message.get("content")
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for item in content:
            if isinstance(item, dict) and isinstance(item.get("text"), str):
                parts.append(item["text"])
        return "".join(parts)
    return ""


def resolve_api_key(session: Session, config: ModelProviderConfig) -> str:
    if config.credential_ref and config.credential_ref.startswith("env:"):
        return os.getenv(config.credential_ref.removeprefix("env:"), "")
    if config.credential_ref and config.credential_ref.startswith("config:"):
        row = session.get(SystemConfig, config.credential_ref.removeprefix("config:"))
        return row.value if row is not None else ""
    if config.credential_ref and config.credential_ref.startswith("secret:"):
        row = session.get(SystemConfig, config.credential_ref.removeprefix("secret:"))
        return row.value if row is not None else ""
    return os.getenv("MODEL_API_KEY", "")


def call_model(
    session: Session,
    config: ModelProviderConfig,
    *,
    task_type: str,
    messages: list[dict[str, str]],
    related_object_type: str | None = None,
    related_object_id: str | None = None,
) -> dict[str, Any]:
    api_key = resolve_api_key(session, config)
    started = time.perf_counter()
    payload = build_openai_chat_payload(config.model_name, messages)
    status = "Success"
    output: dict[str, Any] | None = None
    error: str | None = None
    try:
        if not api_key:
            raise RuntimeError("model api key is not configured")
        with httpx.Client(timeout=30) as client:
            response = client.post(
                f"{config.api_base.rstrip('/')}/chat/completions",
                headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
                json=payload,
            )
            response.raise_for_status()
            output = response.json()
            return output
    except Exception as exc:
        status = "Failed"
        error = str(exc)
        raise
    finally:
        latency_ms = int((time.perf_counter() - started) * 1000)
        session.add(
            ModelCallLog(
                provider_config_id=config.id,
                task_type=task_type,
                related_object_type=related_object_type,
                related_object_id=related_object_id,
                input_summary=dumps({"message_count": len(messages), "model": config.model_name}),
                output_json=dumps(output) if output is not None else None,
                latency_ms=latency_ms,
                status=status,
                error_message=error,
            )
        )
