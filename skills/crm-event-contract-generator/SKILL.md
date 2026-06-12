---
name: crm-event-contract-generator
description: Generate or modify CRM order parsed event contracts, publishers, consumers, schemas, or tests. Use when work touches CrmOrderParsedEvent, CRM-to-order events, payload_hash idempotency, or microservice event communication.
---

# CRM Event Contract Generator

## Goal
Keep CRM order event integration asynchronous, schema-stable, and idempotent.

## Mandatory Constraints
- Microservice communication for this flow must not use synchronous service calls.
- Event payloads must strictly follow the `CrmOrderParsedEvent` JSON Schema.
- Generated consumer code must extract `payload_hash` as its first business step.
- Consumers must check `payload_hash` for replay/idempotency before side effects.
- Duplicate payloads must raise `DuplicateEventException` and terminate processing.

## Workflow
1. Locate the current event schema, models, consumers, and tests before editing.
2. Confirm the target event is `CrmOrderParsedEvent` or an explicitly versioned successor.
3. Keep producers and consumers connected through queue/event boundaries.
4. Validate required fields, event version, and `payload_hash` generation.
5. Add tests for valid event handling, schema rejection, and duplicate replay.

## Implementation Checklist
- Define or update structured schema types instead of ad hoc dict payloads.
- Preserve backward compatibility or document an explicit event version bump.
- Store or query the idempotency key before creating tasks, orders, jobs, or audit records.
- Make duplicate handling observable without mutating downstream state.

## Validation
Run the focused unit tests for event producers and consumers, then the project baseline:

```bash
python3 -m compileall backend
python3 -m pytest
```
