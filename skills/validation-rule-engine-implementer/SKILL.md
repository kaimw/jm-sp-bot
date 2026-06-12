---
name: validation-rule-engine-implementer
description: Implement or modify order validation logic, validation rules, blocker levels, or exception-case persistence. Use when adding business checks that produce ValidationResult or affect CRITICAL blockers.
---

# Validation Rule Engine Implementer

## Goal
Add validation behavior through explicit rule strategies that return standard validation results and preserve exception handling.

## Mandatory Constraints
- New validation logic must not be implemented as flat `if-else` chains.
- Each rule must implement the `OrderValidationRule` interface.
- Rule output must be a standard `ValidationResult` object.
- When `blockerLevel == CRITICAL`, configure circuit-break behavior.
- Critical exceptions must be written to `ExceptionCase` records.

## Workflow
1. Find the existing validation interface, rule registry, result model, and tests.
2. Add one concrete strategy class per business rule.
3. Register the rule through the existing rule engine or local registry pattern.
4. Keep rule ordering explicit and testable.
5. Cover PASS, WARNING/BLOCKING, and CRITICAL behavior in tests.

## Implementation Checklist
- Prefer small rule classes with one reason to change.
- Keep `ValidationResult` construction consistent across all rules.
- Include enough context in `ExceptionCase` for operators to diagnose the failed order.
- Make CRITICAL circuit-break behavior deterministic and visible to callers.

## Validation
Run focused validation tests and the project baseline:

```bash
python3 -m compileall backend
python3 -m pytest
```
