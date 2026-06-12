---
name: react-antd-agent-ui-generator
description: Build or modify React Ant Design operator UI for AI-assisted order workflows, exception diagnosis, suggested actions, read-only detail views, or TanStack Query data loading.
---

# React AntD Agent UI Generator

## Goal
Create operator UI that surfaces order and AI diagnosis state clearly while keeping actions intent-driven and data fetching predictable.

## Mandatory Constraints
- Do not generate full CRUD forms by default.
- Prefer Ant Design `<Descriptions>` and read-only detail views for business records.
- Operation buttons must be intent buttons generated from AI `suggested_actions`.
- Network requests and state updates must use React Query / TanStack Query.
- Do not overuse `useEffect` for fetching or derived server state.
- Exception messages should use Ant Design `Alert`.
- AI analysis must be visually distinct from ordinary data through background, border, or equivalent container treatment.

## Workflow
1. Inspect existing frontend stack, routing, API helpers, and styling conventions.
2. Model server reads and mutations with TanStack Query hooks.
3. Render core order facts in `<Descriptions>` or compact read-only components.
4. Render AI diagnosis in a visually distinct container.
5. Generate action buttons from `suggested_actions`, with loading and disabled states.
6. Add UI tests or browser smoke checks for the changed view when practical.

## Implementation Checklist
- Keep forms limited to explicit operator input that cannot be inferred.
- Show exception state through `Alert` before secondary details.
- Ensure suggested action labels, intent IDs, and mutation payloads are traceable.
- Keep responsive layouts readable on desktop and narrow screens.

## Validation
Run frontend checks available in the repository and the backend baseline when API contracts changed:

```bash
npm test
python3 -m compileall backend
python3 -m pytest
```
