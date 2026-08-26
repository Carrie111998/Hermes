# Universal Wireframe Rules

Use this reference for every wireframe. These rules consolidate widely used
human-interface principles, usability heuristics, responsive behavior, and
accessibility constraints into structural decisions. They are not a claim to
contain every rule from every design system.

## Conflict order

When rules conflict, resolve them in this order:

1. law, safety, privacy, security, and accessibility;
2. confirmed business rules and user permissions;
3. primary user goal and critical task completion;
4. platform conventions and an existing product design system;
5. the defaults in this skill.

Document the trade-off instead of silently averaging incompatible rules.

## Low-fidelity system

### Palette

Use only:

| Token | Value | Structural use |
|---|---|---|
| white | `#FFFFFF` | page and open space |
| black | `#000000` | strongest text or outline |
| gray-50 | `#F3F4F6` | subtle region fill |
| gray-200 | `#E5E7EB` | separators and disabled fill |
| gray-400 | `#9CA3AF` | placeholders and secondary text |
| gray-600 | `#4B5563` | body text and controls |

Do not infer success, warning, error, priority, selection, or disabled state
from hue. Pair states with text, icon labels, border style, shape, or position.

### Media and icons

- Images: `[X]` or `[Image Placeholder: purpose, ratio]`.
- Icons: `[Icon: accessible name]`; never use an unlabeled decorative glyph as
  the only meaning carrier.
- Charts: `[Chart Placeholder: question answered]`, followed by fallback value
  or summary text.
- Video/audio: name the media, duration if known, transcript/caption access,
  and playback controls.
- Avatars/logos: use labeled placeholders; do not synthesize identity assets.

### Typography

Use two semantic levels only:

- **Header/Title:** page, section, group, or item heading.
- **Body/Placeholder:** labels, values, descriptions, hints, metadata, and
  feedback.

Hierarchy may also use spacing, alignment, indentation, weight labels, and
container boundaries. Do not design a branded type scale in a wireframe.

### Grid and spacing

- Mobile: four columns.
- Tablet: eight columns.
- Desktop: twelve columns.
- Use a consistent spacing rhythm; name relative size (`xs`, `sm`, `md`, `lg`)
  if exact pixels are not an implementation requirement.
- Align related edges. Keep unrelated regions visually separated.
- Prefer one main reading axis. Use split panes only when simultaneous context
  materially helps the task.
- State maximum content width or fluid behavior when it changes scanning.

## User-centered framing

### Match the real task

Define the user's desired outcome, current context, frequency, expertise,
constraints, and consequence of failure. Optimize frequent and critical paths
without making rare recovery paths impossible.

### Recognition over recall

- Keep choices, status, context, and valid next actions visible.
- Preserve user-entered values and previous selections where appropriate.
- Use concrete labels instead of relying on memory, unexplained icons, or
  hidden gestures.
- Show examples and formatting requirements beside the relevant control.

### Consistency and standards

- Reuse established labels, locations, control behavior, and terminology.
- Keep the same action in the same relative place across a flow.
- Follow platform conventions unless a documented user need justifies a
  deviation.
- Do not use one component shape for incompatible meanings.

### Affordance and mapping

- Controls should look actionable even in grayscale.
- Place effects near their causes and feedback near the action that produced it.
- Map spatial controls to spatial results where possible.
- Distinguish navigation, selection, editing, submission, and destructive
  actions structurally and textually.

### Choice and cognitive load

- Present only choices required for the current decision.
- Group related options, choose sensible defaults, and defer advanced settings.
- Break large tasks into meaningful stages, not arbitrary screen counts.
- Avoid simultaneous primary CTAs competing for attention.
- Use progressive disclosure without hiding prerequisites or consequences.

### Target size and proximity

- Make frequent and important targets easy to acquire.
- Target at least 24 by 24 CSS pixels for WCAG 2.2 target-size minimum where
  exceptions do not apply; prefer about 44 by 44 for touch controls.
- Provide spacing between adjacent destructive or opposite actions.
- Keep labels with the controls they describe.

## Information architecture

### Region priority

Arrange content by:

1. orientation: where am I and what object/context is active;
2. primary task: what should I do next;
3. decision support: what information is needed to act;
4. secondary actions and navigation;
5. metadata, help, and advanced controls.

Do not make every block a card. Use cards only when items are independently
selectable, comparable, movable, or meaningfully grouped.

### Labeling

- Use the user's domain language, not implementation jargon.
- Begin action labels with clear verbs.
- Name destinations with nouns.
- Avoid vague labels such as "Submit", "Continue", or "Manage" when a more
  specific result can be stated.
- Keep the same concept named consistently across screens.

### Navigation

- Show current location and available escape routes.
- Keep top-level destinations stable.
- Use tabs only for peer views of the same context, not sequential steps.
- Use breadcrumbs for hierarchy, not history.
- Preserve deep links and back behavior when the flow requires them.
- Do not rely on hover for essential navigation or explanation.

## Interaction and feedback

### Visibility of system status

For any action with noticeable delay, specify:

- immediate acknowledgement;
- loading or progress representation;
- whether the user can leave, cancel, retry, or continue elsewhere;
- success confirmation;
- failure consequence and recovery.

Use skeletons for known geometry, spinners for indeterminate compact work, and
progress indicators for measurable multi-step work. Never show fake progress.

### Error prevention and recovery

- Prevent invalid choices when the rule is known and explain why.
- Validate formatting locally without waiting for final submission when useful.
- Confirm actions that are destructive, costly, irreversible, or affect others.
- State the object, consequence, scope, and reversibility in confirmations.
- Prefer undo for reversible operations.
- Preserve valid data after an error.
- Give a specific recovery action, not only an error code.

### Control and freedom

- Provide cancel/back/close behavior without silently discarding work.
- Warn before losing unsaved changes.
- Support undo or version history when the domain warrants it.
- Do not trap focus or navigation.
- Distinguish temporary dismissal from permanent opt-out.

## State model

Consider the following state families for each region or screen:

| Family | Questions |
|---|---|
| Initial | First use, returning use, defaults, restored draft? |
| Loading | Whole page, region, row, or action? Blocking or background? |
| Data | Empty, populated, no results, partial, stale, very large? |
| Input | Pristine, focused, changed, valid, invalid, disabled, read-only? |
| Submission | Ready, submitting, duplicate, success, partial success, failed? |
| Access | Signed out, expired, insufficient role, tenant mismatch? |
| Connectivity | Offline, reconnecting, timed out, conflicting update? |
| Risk | Destructive, financial, legal, permission-changing, irreversible? |

Only render states that can occur, but do not omit them merely because product
requirements are silent. Mark unresolved states as product decisions.

## Responsive behavior

Design content priority before breakpoints. For each region, state whether it:

- stays fixed;
- wraps;
- stacks;
- collapses into disclosure;
- moves to a drawer, sheet, or separate screen;
- becomes horizontally scrollable;
- hides only after an explicit priority decision.

Do not solve mobile by shrinking text or controls. Preserve task order, labels,
focus order, and critical status. Test at narrow widths, zoom, long labels, and
large text rather than only named device sizes.

## Accessibility target

Use WCAG 2.2 AA as the default behavioral target, while noting that a wireframe
alone cannot certify compliance.

### Structure

- One clear page title and logical heading order.
- Landmarks and regions with meaningful names.
- Reading order, visual order, and DOM/focus order agree.
- Repeated navigation and actions remain predictable.

### Keyboard and focus

- Every action is keyboard reachable.
- Focus is visible and moves predictably.
- Dialog focus enters, remains contained while open, and returns to the trigger.
- Escape behavior is stated where dismissing is safe.
- Skip links or equivalent shortcuts exist for repeated dense navigation.

### Forms and instructions

- Every control has a persistent label.
- Required/optional status is textual.
- Instructions appear before they are needed.
- Errors identify the field, reason, and correction.
- Error summaries link or move focus to invalid fields on long forms.
- Do not rely on placeholder text as the label.

### Nonvisual understanding

- Status is announced or otherwise available to assistive technology.
- Tables have headers and captions or context.
- Charts provide a textual summary and underlying values when needed.
- Icon-only controls have accessible names.
- Drag operations have a non-drag alternative.

### Timing and motion

- Avoid time limits; when unavoidable, warn and allow extension.
- Do not require precise or path-based gestures without an alternative.
- Animation and auto-updating regions need pause/reduce-motion behavior when
  relevant, even if motion is not drawn in the wireframe.

## Content, localization, and trust

### Content resilience

- Use representative labels and data shapes, not lorem ipsum.
- Allow text expansion of roughly 30–50 percent for localization.
- Support multiline labels and values without overlap.
- Avoid direction-dependent language such as "click the item on the right."
- Account for right-to-left layout if the target locale requires it.
- Specify date, time, currency, units, and number formatting assumptions.

### Privacy and security

- Minimize collection and reveal why sensitive data is needed.
- Mask secrets by default and provide deliberate reveal/copy behavior.
- Avoid exposing personal data in shared screens, notifications, exports, and
  URLs.
- Make tenant/workspace/account context visible before consequential actions.
- Show session expiry, re-authentication, and permission failure recovery.
- Do not imply access that the current role does not have.

### Honest system behavior

- Distinguish live, cached, estimated, delayed, and unavailable data.
- Label preview-only or disconnected capabilities.
- Never fabricate metrics, records, success states, integrations, or audit
  history to fill a wireframe.
- Make irreversible and external side effects explicit before confirmation.

## Performance and scale

Even a low-fidelity design should state behavior for:

- slow initial load and progressive region loading;
- large result sets, pagination, infinite scroll, or virtualization;
- optimistic updates and rollback;
- stale data and refresh timestamps;
- concurrent edits and version conflict;
- interrupted uploads or long-running operations;
- low bandwidth and offline conditions when relevant.

Perceived speed must not misrepresent actual completion.

## Heuristic review

Before delivery, review the wireframe against these questions:

1. Is system status visible?
2. Does language match the user's world?
3. Can the user cancel, undo, or recover?
4. Are patterns consistent?
5. Are errors prevented where practical?
6. Is recognition favored over recall?
7. Are expert shortcuts possible without confusing new users?
8. Is every region necessary?
9. Are errors specific and actionable?
10. Is help contextual and discoverable?
11. Is the primary task accessible by keyboard and touch?
12. Does the structure remain coherent with no color, imagery, or ideal data?
