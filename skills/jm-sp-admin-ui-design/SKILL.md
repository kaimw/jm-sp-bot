---
name: jm-sp-admin-ui-design
description: Design or improve the JM-SP admin console UI, including static HTML/CSS/JS pages, future React/AntD screens, operator workflows, responsive behavior, tables, filters, exception handling, AI diagnosis panels, and visual consistency. Use before changing backend/app/static or planning admin-console redesigns.
---

# JM-SP Admin UI Design

## Goal
Keep the JM-SP admin console consistent, dense, operator-focused, and safe for high-risk order workflows.

## Required Context
Before designing or editing admin UI, read:

```text
DESIGN.md
```

For implementation, also inspect the relevant current files:

```text
backend/app/static/index.html
backend/app/static/styles.css
backend/app/static/app.js
```

## Workflow
1. Identify the operator task, affected page, primary object, and risky actions.
2. Preserve the current navigation grouping and static frontend conventions unless migration is requested.
3. Prefer compact tables, filters, detail panes, alerts, and read-only data views.
4. Make AI diagnosis or suggested actions visually distinct from source business data.
5. Add responsive handling for any new layout or control group.
6. Validate JavaScript and visually inspect changed screens when practical.

## Design Constraints
- Do not create marketing-style landing pages for admin work.
- Do not add decorative hero sections, gradient blobs, or nested card stacks.
- Do not replace dense operational views with oversized cards unless the data is genuinely summary-only.
- Do not expose saved secrets; use masked placeholders.
- Destructive or irreversible actions need explicit confirmation and clear feedback.
- Exception takeover, fulfillment retry, OMS push, mailbox sending, and skill activation are high-risk flows.

## Preferred Patterns
- Tables for repeated records.
- Read-only detail sections for business facts.
- Inline filter bars above tables.
- Pagination directly below tables.
- Alert-style blocks for blockers and integration failures.
- Distinct bordered or tinted containers for AI diagnosis and suggested actions.
- Compact action buttons grouped near the affected record.

## Validation
For static frontend changes, run:

```bash
node --check backend/app/static/app.js
```

If backend contracts changed too, also run:

```bash
python3 -m compileall backend
python3 -m pytest
```
