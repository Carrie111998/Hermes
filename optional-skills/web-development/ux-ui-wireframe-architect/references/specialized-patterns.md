# Specialized Wireframe Patterns

Load the sections that match the requested surface. Combine patterns only when
the same user task genuinely spans them.

## Mobile-first interfaces

### Start from priority, not compression

1. Define the one task that must remain obvious on the narrowest supported
   viewport.
2. Place orientation and critical status before secondary navigation.
3. Stack content in task order.
4. Defer secondary detail through disclosure or a separate screen.
5. Add wider layouts only when width enables useful simultaneity.

### Navigation

- Use a bottom navigation bar for three to five frequent peer destinations.
- Use a top app bar for title, back, and one or two contextual actions.
- Use drawers or "More" for infrequent destinations, not critical tasks.
- Preserve system back behavior and deep-link state.
- Do not hide the only path behind an unlabeled hamburger or gesture.

### Touch and gesture

- Prefer touch targets around 44 by 44 CSS pixels; never go below the applicable
  WCAG 2.2 minimum without a documented exception.
- Keep destructive actions away from frequent navigation.
- Provide alternatives to swipe, drag, pinch, long-press, and hover.
- State pressed, selected, disabled, loading, and focus behavior.

### Viewport, keyboard, and safe areas

- Account for notches, home indicators, browser chrome, and safe-area insets.
- Define sticky CTA behavior when the software keyboard opens.
- Keep the active field and its error visible above the keyboard.
- Avoid fixed-height layouts that fail with large text or landscape rotation.
- State whether headers, tabs, and action bars remain sticky while scrolling.

### Mobile data presentation

For wide desktop tables, choose one transformation deliberately:

- priority columns plus horizontal scroll;
- record cards with consistent label-value pairs;
- master list followed by a detail screen;
- grouped disclosure for secondary fields;
- comparison mode limited to selected records.

Do not squeeze every desktop column into a four-column mobile grid.

### Mobile state checks

Verify one-hand reach where relevant, interrupted sessions, offline/reconnect,
slow networks, permission prompts, camera/file picker return paths, and
orientation changes.

## B2B dashboards

### Identify the operating mode

Choose the primary mode:

- **Monitor:** scan health, trends, exceptions, and freshness.
- **Operate:** select records, take actions, and resolve queues.
- **Analyze:** compare dimensions, inspect causes, and export findings.
- **Configure:** change rules, roles, mappings, or integrations.

Dashboards often combine modes, but the dominant mode determines hierarchy.

### Context bar

Before KPIs or data, show any context that changes meaning:

- organization, tenant, workspace, project, environment, or account;
- date/time range and timezone;
- saved view or active filter set;
- data freshness and refresh state;
- current role or permission boundary when consequential.

Cross-tenant or production actions must make their scope unmistakable.

### KPI rules

Every KPI must answer a real question and expose:

- label and unit;
- value state: live, delayed, cached, estimated, or unavailable;
- comparison basis and period when a delta is shown;
- timestamp or freshness where material;
- drill-down destination or explicit non-interactive status;
- empty and error behavior.

Avoid decorative metrics, unlabeled sparklines, and ambiguous red/green deltas.

### Filter architecture

- Put frequent filters in the primary filter bar.
- Put advanced filters in a drawer or builder.
- Show active filter chips and result count.
- Provide clear-all without destroying a saved view unexpectedly.
- Distinguish search, filter, sort, group, and view selection.
- State whether filter changes apply immediately or require an Apply action.
- Support saved views when users repeat complex filter combinations.
- Decide whether filters are URL-shareable and whether they persist by user.

### Data table contract

Specify, where relevant:

- row identity and primary label;
- column priority, alignment, formatting, and truncation;
- sort state and sortable columns;
- selection rules across pages or filtered results;
- pagination, infinite scroll, or virtualization;
- column resize, reorder, pin, and visibility;
- row action, bulk action, and detail navigation;
- inline edit, save, cancel, conflict, and validation;
- empty, no-results, loading, stale, partial, and error states;
- keyboard navigation and screen-reader table semantics;
- mobile transformation.

Numeric columns align for comparison. Do not put several icon-only actions in
every row; use one clear primary affordance plus a labeled overflow menu when
needed.

### Bulk actions

- Keep the selection count visible.
- Define whether selection means current page, loaded rows, or all filtered
  results.
- Show affected scope before execution.
- Disable incompatible actions with an explanation.
- Confirm destructive, external, or high-volume effects.
- Report success, skipped records, partial failure, and retry options.
- Preserve the filter and selection context after recoverable failure.

### Drill-down and workspace continuity

Use a side panel when the user benefits from preserving list context; use a
full detail page when the object has deep navigation or long tasks. Keep object
identity, position, filter state, and a reliable return path.

### Roles, audit, and collaboration

- Hide or disable actions according to the confirmed permission model; do not
  imply authorization.
- Explain permission failures without revealing protected data.
- Surface actor, timestamp, scope, before/after values, and correlation context
  in audit views where available.
- Show ownership, assignment, comments, presence, locks, or conflict state only
  when the product supports them.

### Dashboard scale and performance

Specify progressive loading by region, request cancellation after filter
changes, stale-data handling, auto-refresh pause, large-data aggregation,
export lifecycle, long-running jobs, and reconnect behavior.

## Complex data-entry forms

### Choose the form structure

Use a single page when fields are limited, dependencies are visible, and the
user benefits from scanning the whole form. Use a stepper or wizard when:

- stages have distinct user goals;
- later questions depend on earlier answers;
- validation or review occurs by stage;
- users need saved progress;
- the task is long enough that chunking reduces cognitive load.

Do not create steps solely to reduce visible field count. Avoid tabs for a
strict sequence.

### Field grouping and order

- Group fields by user meaning and decision sequence.
- Put prerequisites before dependent fields.
- Keep labels persistent and examples close to inputs.
- Mark optional fields explicitly when most fields are required, or required
  fields explicitly when most are optional.
- Use sensible defaults without assuming consent or legal agreement.
- Keep destructive reset and cancel actions separate from save/continue.

### Control selection

- Text input: short unconstrained text.
- Text area: long free-form content.
- Radio: one visible choice from a small set.
- Checkbox: independent choice or explicit acknowledgement.
- Select/combobox: larger sets; support search when needed.
- Toggle: immediate binary setting, not form submission consent.
- Date/time: locale-aware input plus constraints and timezone context.
- File upload: file types, size, progress, cancel, retry, replacement, and
  security/privacy note.
- Repeating group: clear add/remove/reorder behavior, limits, and item identity.

Never use disabled inputs to communicate read-only values without explaining
why they cannot be edited.

### Validation timing

- Validate syntax after blur or when enough input exists.
- Validate cross-field and server rules at the earliest reliable point.
- Do not show errors before the user has interacted unless submission reveals
  them.
- Keep error text next to the field and include a page/step summary for long
  forms.
- Focus the first invalid field after submission and preserve every valid value.
- Distinguish warning, blocking error, and informational hint without relying
  on color.

### Conditional fields

- Reveal dependent fields immediately after the controlling choice when
  possible.
- Explain why the field appeared and whether it is required.
- Define whether hidden values are retained, cleared, or submitted.
- Prevent inaccessible focus from landing in hidden regions.
- Recalculate step completion and review summaries after conditions change.

### Drafts and autosave

Specify:

- what triggers save;
- local versus server persistence;
- visible saving/saved/failed/offline state;
- last-saved time;
- retry and conflict behavior;
- expiry and privacy of saved drafts;
- unsaved-change warning on navigation;
- whether another device or collaborator can modify the same draft.

Do not claim autosave if the backend does not support it.

### Multi-step flow

Each step should include:

- step title and purpose;
- progress that reflects meaningful stages, not false precision;
- back and continue behavior;
- save-and-exit behavior when supported;
- step-level errors;
- dependencies on earlier answers;
- final review before consequential submission.

Back must not discard valid information. Skipping must explain its consequence.
After submission, show a receipt or confirmation identifier when the domain
provides one and define what can happen next.

### High-risk and sensitive forms

For financial, legal, identity, medical, permission, or irreversible actions:

- explain why sensitive data is needed;
- minimize collection;
- mask secrets and identifiers;
- expose scope, fees, timing, recipients, and consequences before confirmation;
- require a clear acknowledgement when policy demands it;
- support re-authentication or strong confirmation when appropriate;
- provide a receipt, audit trail, cancellation window, or recovery path where
  the product supports it.

Never fabricate regulatory compliance or security guarantees in a wireframe.

### Form state matrix

At minimum consider:

| State | Required structural response |
|---|---|
| Pristine | labels, defaults, requirements, and next action |
| In progress | changed state, conditional fields, save status |
| Invalid field | field message and correction |
| Invalid step/page | summary plus focus routing |
| Submitting | duplicate prevention and progress |
| Success | confirmation, result, and next action |
| Failure | preserved data, reason, retry, support path |
| Offline | local behavior, limitation, reconnect |
| Conflict | compare/reload/overwrite decision if supported |
| Expired | re-authenticate without losing valid work where possible |

## Search and result surfaces

- Separate query input, active filters, sort, and result count.
- Preserve query and filters through detail navigation.
- Distinguish empty index from no matching results.
- Offer a recovery action for no results: clear filters, broaden query, or
  create a record when authorized.
- Define typo handling, recent searches, suggestions, and privacy only if the
  product supports them.

## Destructive and irreversible actions

For delete, revoke, transfer, publish, pay, submit, or permission changes:

1. name the exact object and scope;
2. state who or what is affected;
3. state reversibility, delay, fee, or external side effect;
4. require an appropriate confirmation proportional to risk;
5. prevent duplicate execution;
6. show completion, partial failure, and recovery/audit destination.

Do not make cancel and destructive confirmation visually or spatially
ambiguous.
