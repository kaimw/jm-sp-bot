from __future__ import annotations

import json
import os
import time
from typing import Any

import httpx
from sqlalchemy.orm import Session

from backend.app.models import ModelCallLog, ModelProviderConfig, SystemConfig
from backend.app.services.jsonutil import dumps

DEFAULT_MODEL_TIMEOUT_SECONDS = 90.0
DEFAULT_MODEL_STREAM_READ_TIMEOUT_SECONDS = 30.0


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


def _resolve_timeout_seconds(timeout_seconds: float | None) -> float:
    if timeout_seconds is None:
        return DEFAULT_MODEL_TIMEOUT_SECONDS
    try:
        value = float(timeout_seconds)
    except (TypeError, ValueError):
        return DEFAULT_MODEL_TIMEOUT_SECONDS
    if value <= 0:
        return DEFAULT_MODEL_TIMEOUT_SECONDS
    return value


def _content_to_text(content: Any) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for item in content:
            if isinstance(item, str):
                parts.append(item)
                continue
            if isinstance(item, dict):
                text = item.get("text")
                if isinstance(text, str):
                    parts.append(text)
                    continue
                nested = item.get("content")
                if isinstance(nested, str):
                    parts.append(nested)
        return "".join(parts)
    return ""


def _extract_stream_chunk_text(chunk: dict[str, Any]) -> str:
    choices = chunk.get("choices")
    if not isinstance(choices, list):
        return ""
    parts: list[str] = []
    for choice in choices:
        if not isinstance(choice, dict):
            continue
        delta = choice.get("delta")
        if isinstance(delta, dict):
            text = _content_to_text(delta.get("content"))
            if text:
                parts.append(text)
        message = choice.get("message")
        if isinstance(message, dict):
            text = _content_to_text(message.get("content"))
            if text:
                parts.append(text)
        text = _content_to_text(choice.get("text"))
        if text:
            parts.append(text)
    return "".join(parts)


def call_model(
    session: Session,
    config: ModelProviderConfig,
    *,
    task_type: str,
    messages: list[dict[str, str]],
    related_object_type: str | None = None,
    related_object_id: str | None = None,
    stream: bool = False,
    timeout_seconds: float | None = None,
) -> dict[str, Any]:
    api_key = resolve_api_key(session, config)
    started = time.perf_counter()
    payload = build_openai_chat_payload(config.model_name, messages)
    request_timeout = _resolve_timeout_seconds(timeout_seconds)
    status = "Success"
    output: dict[str, Any] | None = None
    error: str | None = None
    try:
        if not api_key:
            raise RuntimeError("model api key is not configured")
        endpoint = f"{config.api_base.rstrip('/')}/chat/completions"
        headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}
        if stream:
            payload["stream"] = True
            stream_timeout = httpx.Timeout(
                connect=min(10.0, request_timeout),
                write=min(30.0, request_timeout),
                read=min(DEFAULT_MODEL_STREAM_READ_TIMEOUT_SECONDS, request_timeout),
                pool=min(30.0, request_timeout),
            )
            with httpx.Client(timeout=stream_timeout) as client:
                with client.stream("POST", endpoint, headers=headers, json=payload) as response:
                    response.raise_for_status()
                    parts: list[str] = []
                    for raw_line in response.iter_lines():
                        line = raw_line.decode("utf-8", errors="ignore") if isinstance(raw_line, bytes) else raw_line
                        if not isinstance(line, str):
                            continue
                        line = line.strip()
                        if not line:
                            continue
                        if line.startswith("data:"):
                            line = line[5:].strip()
                        if not line or line == "[DONE]":
                            continue
                        try:
                            chunk = json.loads(line)
                        except json.JSONDecodeError:
                            continue
                        if not isinstance(chunk, dict):
                            continue
                        text = _extract_stream_chunk_text(chunk)
                        if text:
                            parts.append(text)
                    output = {"choices": [{"message": {"content": "".join(parts)}}]}
                    return output
        with httpx.Client(timeout=request_timeout) as client:
            response = client.post(endpoint, headers=headers, json=payload)
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
