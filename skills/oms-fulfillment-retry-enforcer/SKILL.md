---
name: oms-fulfillment-retry-enforcer
description: Implement or review OMS fulfillment push, retry scheduling, dead-letter handling, distributed locking, or optimistic locking. Use when changing order downstream submission, retry queues, or failure recovery.
---

# OMS Fulfillment Retry Enforcer

## Goal
Make OMS fulfillment retries configurable, collision-safe, and recoverable.

## Mandatory Constraints
- Failed downstream pushes must not retry immediately through hard-coded logic.
- Retry scheduling must use externally configurable exponential backoff with jitter.
- Retry ownership must be protected by an `order_no` distributed lock or database optimistic lock.
- After max retries, failures must be persisted as dead-letter records for later handling.

## Workflow
1. Locate the OMS push service, queue job model, retry config, and failure records.
2. Confirm retry timing is read from runtime config or environment-backed settings.
3. Add lock acquisition before each retry attempt that can mutate downstream state.
4. Record attempt count, next retry time, final failure reason, and dead-letter metadata.
5. Test transient failure, lock contention, eventual success, and max-retry dead letter.

## Implementation Checklist
- Keep retry math centralized and unit-tested.
- Add jitter in a bounded, deterministic-testable way.
- Avoid duplicate downstream pushes for the same `order_no`.
- Make dead-letter records queryable by operators or follow-up jobs.

## Validation
Run retry-focused tests and the project baseline:

```bash
python3 -m compileall backend
python3 -m pytest
```
