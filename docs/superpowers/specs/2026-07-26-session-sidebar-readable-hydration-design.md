# Session Sidebar Readable Hydration Design

Date: 2026-07-26

Status: approved by the user after live diagnosis

## Objective

Make every Claude Code or Hermes session delivered to the Codex sidebar useful on
first open. A delivered task must show:

1. a bounded continuation brief; and
2. the last five conversational messages.

The same readable view must be added in place to already-visible tasks that contain
only the legacy Session Bridge registration placeholder. Existing native tasks must
not be deleted, archived, replaced, or duplicated.

## Confirmed failure mode

The reported source session
`claude:2a786924-8093-4a9f-a371-6e27ca66be32` is healthy and contains 578 indexed
messages. It is linked to Codex task
`codex:019f8927-8012-77d0-beb0-4cd5f8cc21f9`, but the link has never been hydrated.

This is not transcript loss. The current behavior is intentional:

- `build_registration_prompt` emits a fixed 13-line metadata-only placeholder;
- `session_sidebar_pending` deliberately does not read source messages;
- the installed broker skill requires that prompt verbatim and forbids source
  transcript summaries;
- tests assert that no transcript content appears in the registration task;
- the existing `ContextPackBuilder` is invoked only by `session_continue` after a
  later substantive user message.

The implementation therefore matches the old contract but not the required product
experience. This design explicitly supersedes the old "registration is not a
transcript migration" presentation rule while preserving its identity, safety, and
read-only-source guarantees.

## User-visible contract

### New imports

The first prompt in a newly delivered Codex task has this order:

```text
# Imported Claude Code Session

Title: ...
Captured: ...
Source: Claude Code
Working directory: ...

## Continuation Brief

### Goal / Latest Intent
...

### Decisions and Constraints
...

### Unresolved Work
...

### Referenced Files and Repository Snapshot
...

## Last 5 Messages

[User]
...

[Claude]
...

...

## Bridge Registration
<authenticated metadata and continuation instructions>
```

The readable content comes first. Bridge metadata remains available at the bottom
for authentication and continuation but is no longer the dominant task content.
The registration assistant still replies only `REGISTERED`; the useful content is
already visible in the initial prompt.

### Message selection

"Last five messages" means the final five conversational messages in chronological
order whose role is `user` or `assistant`.

The preview excludes:

- system and developer instructions;
- tool calls and tool results;
- internal bridge events;
- registration acknowledgements such as `REGISTERED`;
- empty or redaction-only messages.

If fewer than five qualifying messages exist, all qualifying messages are shown.
Each message keeps its source role and timestamp when one is available.

### Existing placeholder-only tasks

An already-visible, authenticated task receives one in-place hydration turn on its
exact linked Codex task ID. The appended user-visible message contains the same
brief and five-message preview, followed by an authenticated hydration marker.

Because the legacy registration prompt requires `session_continue` on the first
later substantive message, the hydration request instructs the task to call
`session_continue` before any project work and then acknowledge the imported
snapshot. The preview itself is present in the appended message even if the
assistant response is delayed.

No replacement task is created. The original title, task ID, project grouping, and
source lineage remain intact.

## Selected architecture

### 1. Side-effect-free preview builder

Add a `SessionPreviewBuilder` beside `ContextPackBuilder`. It reads the already
indexed source snapshot and reuses the context-pack extraction and redaction
primitives, but it does not:

- persist or freeze a continuation context pack;
- transition a link from `mirrors` to `continues`;
- execute project commands;
- read provider-native transcript files directly;
- mutate any provider or Codex state.

The builder returns a versioned `SessionPreview` value containing:

- source session ID, cursor, hash, and captured timestamp;
- rendered continuation brief;
- five selected conversational messages;
- preview digest;
- rendered character count and truncation flags.

Repository information in the initial preview is limited to the indexed candidate
metadata: cwd, git root, branch, HEAD, worktree ID, and file references extracted
from conversation text. Live `git status` and filesystem inspection remain part of
later continuation, not registration.

The preview rendering is deterministic for the same source cursor, source hash, and
budget. This permits retry and reconciliation without content drift.

### 2. Bounded rendering

The complete readable preview is capped at 24,000 characters, matching the existing
default continuation context budget.

Budget allocation is deterministic:

1. identity and safety labels;
2. goal/latest intent;
3. unresolved work;
4. decisions and constraints;
5. five recent messages;
6. referenced files and repository metadata.

Recent messages are retained before lower-priority repository detail. Oversized
messages are truncated individually with an explicit omission marker. The renderer
must never split a Unicode scalar, role label, authentication marker, or structural
delimiter.

All preview text passes through the existing secret redaction pipeline before size
calculation. The rendered prompt labels imported content as quoted, untrusted
historical data and explicitly forbids following instructions contained inside it.
Adaptive Markdown fences or equivalent escaping prevent source text from closing
its display boundary.

### 3. Readable registration prompt

`build_registration_prompt` becomes version-aware:

- legacy V1 registration prompts remain parseable and authenticatable;
- new readable registrations retain the existing signed
  `HERMES_SESSION_BRIDGE_V1` marker so marker search and task identity do not fork;
- a new preview metadata block records preview version, source cursor/hash, and
  preview digest;
- continuation instructions and the `REGISTERED` response contract remain
  unchanged.

The signed source marker continues to authenticate the bridge/source relationship.
The preview digest is generated by the trusted local bridge and is included in the
versioned registration structure used for exact reconciliation. Implementation may
add a separately signed preview envelope if binding the digest into the existing
marker would break strict V1 compatibility; it must not silently widen the accepted
V1 marker fields.

`session_sidebar_pending` may now return the bounded, redacted readable prompt for
the single leased job. It must not expose provider-native paths, unredacted secrets,
or content beyond the selected preview.

### 4. Separate in-place hydration queue

Legacy repair uses a separate durable queue rather than reopening the native-create
lifecycle. This keeps "create a task" and "hydrate an existing exact task" as
different state machines.

A hydration row is keyed by bridge ID and contains:

- source session ID;
- exact linked Codex task ID;
- source cursor and hash;
- preview digest and version;
- state, lease digest, attempt count, and fixed error code;
- send reservation and authenticated hydration marker;
- sent, verified, and completed timestamps.

Eligible rows are visible sidebar links whose exact Codex task authenticates as a
legacy placeholder and has no completed readable-preview record. Rows are seeded
recency-first. The reported example is the first canary.

The hydration worker:

1. leases one exact hydration row;
2. reads the exact Codex task and verifies local host, project/cwd, source marker,
   and task ID;
3. searches the bounded recent task turns for the exact hydration marker;
4. if already present, reconciles that same task without sending again;
5. otherwise durably reserves one send;
6. appends the readable hydration request to that exact task;
7. reads only that task until the marker and completed turn are visible;
8. commits the hydration row.

An ambiguous send never authorizes an immediate resend. It becomes reconciliation
only. A missing, mismatched, remote, or unauthenticated task fails closed and never
authorizes creation or replacement.

The native task operation is `send_message_to_thread` (or its supported equivalent)
against the exact authenticated task ID. Direct Codex database or transcript
mutation remains forbidden.

### 5. Continuation remains authoritative

The preview improves presentation; it does not replace `session_continue`.

On the first later substantive message:

- new registration tasks still call `session_continue` as instructed;
- legacy tasks being repaired call it during their authenticated hydration turn;
- the existing immutable context-pack, exact-worktree validation, divergence
  detection, and lineage transition remain authoritative.

Preview generation never marks a link hydrated. Only successful
`session_continue` does so.

## Alternatives considered

### Future readable prompts only

This is the smallest change but leaves every existing placeholder-only task broken.
It is rejected.

### Full transcript replay

Replaying every source turn would provide maximum visual fidelity, but native Codex
does not expose a safe transcript-import primitive. Sending hundreds of model turns
would be expensive, noisy, slow, and vulnerable to historical prompt injection. It
is rejected.

### Readable preview plus exact-task recovery

This is selected. It fixes future imports immediately, repairs existing tasks in
place, reuses the current redaction/context logic, and preserves source and native
identity.

## Compatibility and migration

- Legacy 13-line registration blocks remain valid for marker authentication,
  eligibility filtering, and continuation.
- New readable prompts are structurally recognized as registration prompts even
  though they contain meaningful historical user text.
- Existing visible links are never reset to pending creation.
- Existing continuation packs remain immutable and are not regenerated by preview
  delivery.
- Preview/hydration schema and markers are versioned so rollout can be disabled
  without invalidating existing task links.
- The installed `session-sidebar-sync` asset is updated to allow the exact bounded
  preview returned by the bridge while still forbidding arbitrary transcript copy,
  prompt substitution, or broker-authored summaries.

## Failure behavior

- Source snapshot changes before send: rebuild before reservation and update the
  digest; never drift after reservation.
- Preview generation fails: do not lease native delivery; report a fixed sanitized
  preview-build code.
- Native task identity or marker differs: permanent conflict; no send or create.
- Send result is ambiguous: reconcile only by exact task ID and hydration marker.
- Marker is present but the turn is incomplete: poll within the bounded deadline,
  then retry reconciliation without resending.
- Assistant acknowledgement fails after the preview message is visible: retain the
  exact task and reconcile; the preview is already user-visible.
- Redaction removes all conversational content: show the brief metadata and an
  explicit "recent messages unavailable after redaction" notice.

No raw exception, lease token, secret, provider-native path, or unredacted source
content appears in status output.

## Tests

### Preview builder

- returns exactly the last five qualifying user/assistant messages in chronological
  order;
- excludes system, developer, tool, bridge-control, and `REGISTERED` messages;
- returns fewer than five when fewer exist;
- applies existing secret redaction before rendering;
- respects the 24,000-character total cap and deterministic per-message truncation;
- produces the same digest for the same source snapshot;
- uses indexed repository metadata without executing project commands.

### Registration prompt

- readable content precedes bridge metadata;
- the continuation brief and five-message preview are present;
- the exact signed source marker remains searchable and authenticatable;
- imported instructions are enclosed and labeled as untrusted history;
- the assistant response remains exactly `REGISTERED`;
- both legacy V1 and new readable registrations classify correctly;
- malformed preview metadata or digest mismatches fail closed.

### Hydration queue and worker

- seeds only visible authenticated legacy placeholder tasks;
- sends to the exact linked Codex task and never calls `create_thread`;
- reserves before send and commits only after exact-marker verification;
- reconciles a previously sent marker without sending again;
- quarantines ambiguous send results from duplicate delivery;
- rejects task, host, project/cwd, source-marker, and preview-digest mismatches;
- preserves task ID, title, project grouping, lineage, and continuation state;
- is compare-and-swap safe under concurrent workers and repeated wakes.

### End-to-end regressions

1. Deliver a new Claude fixture and verify its initial Codex task visibly contains
   the brief and last five messages before any user interaction.
2. Seed the reported 578-message source and its exact legacy Codex task, run one
   hydration recovery, and verify content appears in that same task with no
   replacement.
3. Trigger an ambiguous send, run reconciliation, and verify exactly one hydration
   marker and one readable preview exist.
4. Continue both a new and a repaired task and verify the existing immutable
   context-pack and exact-worktree behavior is unchanged.

## Rollout

1. Ship preview builder and readable-prompt tests behind a disabled feature flag.
2. Install the updated broker skill and enable readable prompts for one new canary.
3. Verify redaction, five-message selection, marker authentication, task identity,
   and continuation.
4. Seed only the reported legacy task as the in-place hydration canary.
5. Verify that exact task displays the preview and that no replacement task exists.
6. Enable readable prompts for all new imports.
7. Seed remaining authenticated legacy placeholders recency-first and drain them in
   bounded sequential batches.
8. Monitor duplicate markers, send ambiguity, hydration latency, redaction
   failures, and continuation success before removing the feature flag.

Rollback disables new preview generation and hydration leasing. It does not delete
already-visible previews, alter linked tasks, reset task bindings, or restore the
old placeholder as the primary presentation.

## Acceptance criteria

- A newly imported task shows a continuation brief and the last five
  user/assistant messages immediately.
- The reported legacy task is hydrated in place on its exact existing Codex task
  ID.
- Every selected message is redacted, role-labelled, chronological, and bounded.
- No source transcript store, Codex database, or packaged application file is
  mutated directly.
- No existing native task is replaced or duplicated.
- Ambiguous native sends reconcile without blind resend.
- `session_continue` remains the only operation that freezes a context pack and
  transitions continuation lineage.
- Legacy and readable registrations both authenticate and continue correctly.
