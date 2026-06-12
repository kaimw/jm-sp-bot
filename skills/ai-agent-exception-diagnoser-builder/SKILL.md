---
name: ai-agent-exception-diagnoser-builder
description: Build or modify AI exception diagnosis services, LLM prompts, JSON-mode parsing, ContextPack assembly, or agent fallback behavior. Use when adding model-assisted exception analysis for orders, validation, or workflow failures.
---

# AI Agent Exception Diagnoser Builder

## Goal
Provide reliable model-assisted exception diagnosis without blocking or destabilizing core business flow.

## Mandatory Constraints
- Before calling an LLM, explicitly assemble a compact, structured `ContextPack`.
- LLM calls must force JSON output through function calling or JSON mode.
- JSON parsing must be wrapped in `try-catch` with default fallback values.
- Model hallucination or malformed output must not block the main business thread.
- Agent service functions must be stateless.

## Workflow
1. Identify the exception source, required operator output, and existing model-provider path.
2. Build a minimal `ContextPack` with only fields needed for diagnosis.
3. Use the repository's OpenAI-compatible model provider and runtime config.
4. Request strict JSON and parse into a typed response object.
5. On model, timeout, or parse failure, return deterministic fallback diagnosis.
6. Add tests for valid JSON, malformed JSON, provider failure, and missing context.

## Implementation Checklist
- Do not persist hidden conversation state inside the agent service.
- Keep prompts concise and data-minimized.
- Do not leak secrets or raw credentials into `ContextPack`.
- Keep model failures observable through logs, audit events, or exception records.

## Validation
Run model-provider and exception-diagnosis tests plus the project baseline:

```bash
python3 -m compileall backend
python3 -m pytest
```
