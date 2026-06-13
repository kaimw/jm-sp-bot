# JM-SP Admin Design System

## Purpose
This document is the design contract for the JM-SP admin console. Use it before changing `backend/app/static/index.html`, `backend/app/static/styles.css`, `backend/app/static/app.js`, or any future React/Ant Design replacement.

The console is an internal operations tool for order middle-platform work: CRM intake, pre-review, fulfillment, exception takeover, notifications, master data, integrations, and audit. It should feel dense, calm, scannable, and reliable.

## Product Principles
- Put operator work first: show status, blockers, owner, next action, and evidence before decorative content.
- Favor read-only records plus explicit actions over broad CRUD surfaces.
- Preserve the left navigation mental model: overview, order fulfillment, rules/master data, integrations/ops, organization config.
- Make AI output visually distinct from source business data.
- Treat exception and fulfillment pages as high-risk workflows. They need clear confirmation, audit feedback, and reversible paths.
- Keep changes consistent with the current static frontend unless a migration is explicitly requested.

## Visual Language
- Use a restrained enterprise palette with neutral backgrounds and a single strong action color.
- Current tokens in `styles.css` are the source of truth:
  - `--bg` page background
  - `--surface` primary panels
  - `--surface-soft` secondary panels
  - `--ink` primary text
  - `--muted` secondary text
  - `--line` and `--line-strong` borders
  - `--accent` and `--accent-strong` primary actions
  - `--warn` warnings
  - `--danger` destructive or blocking states
- Avoid marketing-style hero sections, large ornamental cards, gradient blobs, and decorative illustrations.
- Border radius should stay compact, normally 7-8px.
- Shadows should be subtle and reserved for active nav, popovers, modals, and elevated overlays.

## Layout
- Keep the sidebar fixed on desktop and grouped by operator task.
- Keep the topbar sticky and concise: page title, subtitle, global controls.
- Page sections should be full-width bands or unframed layouts. Do not nest cards inside cards.
- Use tables and compact lists for repeated business records.
- Use detail panes, drawers, or clearly separated sections for single-record review.
- Filters belong above the affected table and should be one scan line when possible.
- Pagination belongs directly below its table.

## Component Patterns
### Tables
- First column: business identity, such as order number, customer, SKU, mail subject, or exception title.
- Middle columns: status, owner, timestamps, key metrics.
- Last column: compact actions.
- Each table needs an empty state and error state.
- Long text should wrap predictably or be truncated with a title/secondary detail path.

### Status
- Use plain language labels, not internal enum names alone.
- Critical blockers should use danger styling and appear before secondary metadata.
- Warning states should use `--warn`, not danger.
- Success and normal completed states should be quieter than pending work.

### Forms
- Avoid full CRUD forms unless the workflow truly requires broad editing.
- Prefer inline filters, targeted patch forms, and confirmation dialogs for risky actions.
- Mark destructive actions visually and require explicit confirmation.
- Never expose secret values after save; show masked placeholders.

### AI Content
- Put AI diagnosis, suggested actions, and extracted reasoning inside a visually distinct container.
- Label AI content as suggestion or diagnosis, not truth.
- Pair each suggested action with evidence and the expected business effect.
- Do not allow one-click irreversible actions from AI output without operator confirmation.

### Notifications
- Use the existing notice trail and toast patterns for short feedback.
- Use inline alert-style blocks for persistent errors, blockers, and integration failures.
- Error copy should say what failed and what the operator can try next.

## Responsive Rules
- Desktop is the primary workflow surface, but every page must remain usable on narrow screens.
- Controls must not overlap or overflow their parent.
- Buttons and links should maintain at least a 44px touch target on mobile.
- Use CSS grid/flex wrapping for filter bars and action groups.
- Do not scale font sizes directly with viewport width.

## Page Guidance
### Agent Overview
- Show the automation chain and current waterline.
- Keep workflow cards navigational and compact.
- Prioritize queue counts, readiness gaps, and pending exceptions.

### CRM Orders
- Optimize for finding new orders, duplicates, changed orders, and blocked pre-review.
- Detail views should show source evidence, parsed fields, and downstream readiness.

### Exceptions
- This is a takeover cockpit.
- Show severity, blocker reason, related order/mail, owner, suggested next action, and audit history.
- AI diagnosis can help, but source evidence and manual action must remain primary.

### Fulfillment / Logistics
- Show inventory, OMS push status, retry state, and dead-letter state clearly.
- Retry and re-push actions must reveal lock/retry implications before execution.

### Skill Lab
- Separate draft, validation, approval, enabled, disabled, and archived states.
- Show generated skill capabilities and safety boundaries before activation.

### Integration Settings
- Group CRM, OMS, mailbox, model provider, and runtime config separately.
- Test connection actions should show scoped results without leaking credentials.

## Implementation Workflow
1. Read this file and the current page code before editing UI.
2. Identify the operator task, primary data object, and risky actions.
3. Keep the first change narrow and consistent with existing HTML/CSS/JS patterns.
4. Reuse existing classes and tokens before adding new ones.
5. Add or update responsive styles with the same change.
6. Validate with `node --check backend/app/static/app.js` for JS changes.
7. When practical, open `http://127.0.0.1:8000` and inspect desktop plus mobile widths.

## Migration Note
If the admin console later moves to React/Ant Design, preserve these product principles and page patterns. Use Ant Design tables, descriptions, alerts, drawers, modals, segmented controls, and TanStack Query while keeping operator density and risk controls intact.
