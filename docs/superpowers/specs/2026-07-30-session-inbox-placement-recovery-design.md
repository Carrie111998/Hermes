# Session Inbox Placement, Recovery, and Refresh Design

Date: 2026-07-30

Status: approved design, pending implementation plan

## Context

The cross-harness Session Bridge is discovering recent Claude Code sessions,
creating native Codex tasks, and rendering a bounded readable preview. Live
inspection on 2026-07-29 and 2026-07-30 proved that recent imports contain the
expected Continuation Brief and last five messages.

Those tasks are still absent from the project-scoped Codex sidebar. The newest
real imports have:

- `projectId: null`;
- a `cwd` under a transient Claude worktree or another unsaved path;
- a readable imported-session summary;
- a durable `mirrors` link from the Claude source session.

The two saved local Codex projects are:

- `C:\Users\diego\Developer\session-sidebar-broker`;
- `C:\Users\diego\.hermes`.

The earlier project-aware conversational broker selected a saved project before
calling the native Codex task tool. Continuous delivery now uses the
deterministic app-server executor instead. Its `thread/start` call passes the
source candidate cwd directly. The raw app-server `ThreadStartParams` contract
has a `cwd` field but no `projectId` field. Codex therefore persists these
threads outside every saved project even though the bridge counts them as
visible.

The acceptance canary did not expose this defect because its source cwd was
already `C:\Users\diego\.hermes`, so the canary received the `.hermes` project
identity while normal Claude worktree sessions did not.

There is a second freshness problem. A Claude source may continue for hours
after its one native mirror is created. The source cursor and `last_active`
advance, but the existing Codex task receives no new readable preview and its
`updatedAt` does not advance. Users therefore see an old task timestamp and an
old last-five snapshot even while the source session is active.

## Decision

Use the existing saved `.hermes` project as the canonical Session Inbox for
every Claude and Hermes import.

Project grouping and execution cwd are separate identities:

- the Codex task is created with the canonical local `.hermes` path so it is
  visible in the project-scoped sidebar;
- the exact source cwd and worktree snapshot remain authenticated bridge data;
- continuation keeps requiring every command and file operation to use the
  exact source cwd;
- the source cwd is included as a runtime workspace root when the native task is
  created or recovered.

Future imports use the inbox at creation time. Existing projectless imports are
preserved and recovered through an authenticated fork. Active, untouched
mirrors receive bounded signed refresh turns so their summary, last five
messages, and Codex recency follow source activity.

## Goals

- Make every new eligible Claude or Hermes import appear under `.hermes`.
- Preserve the authoritative source cwd and worktree identity.
- Recover existing eligible projectless imports without deleting or rewriting
  their original Codex tasks.
- Keep one canonical Codex mirror link per source session.
- Refresh bounded readable content for active, uncontinued mirrors.
- Preserve all existing no-blind-retry, exact-ID, and signed-marker guarantees.
- Make placement and freshness failures visible in health and status output.
- Recover safely after process or laptop crashes at every native mutation
  boundary.

## Non-goals

- Do not add transient Claude worktrees as saved Codex projects.
- Do not reintroduce the scheduled conversational sidebar worker.
- Do not edit the Codex database, rollout files, or desktop state directly.
- Do not delete, archive, or silently rewrite original projectless tasks.
- Do not refresh a task after substantive Codex work begins.
- Do not turn the readable preview into a full transcript migration.
- Do not replay every source change; refresh delivery is coalesced.
- Do not alter Claude-side native visibility or Claude Desktop registry logic.

## Considered Approaches

### 1. Fix future creation only

Create new imports in `.hermes` and leave existing projectless tasks untouched.

This is the smallest change, but it does not repair the already-imported
sessions the user cannot see. It is rejected as incomplete.

### 2. Canonical inbox plus preserve-and-recover migration

Create future imports in `.hermes`, fork eligible existing projectless tasks
into `.hermes`, preserve original task lineage, and add bounded refresh turns
for active sources.

This is the selected approach. It fixes future placement, repairs the current
inventory, preserves evidence, and avoids direct Codex state mutation.

### 3. Restore the project-aware conversational broker

The native Codex task tool can accept a saved `projectId`, but the scheduled
broker repeatedly interrupted unrelated user work and depended on a
conversational procedure for durable delivery.

This approach is rejected. The deterministic executor remains the owner of
continuous delivery.

## Identity Model

The implementation must represent source identity and sidebar placement
separately.

### Source identity

The existing `SidebarCandidate` remains authoritative for:

- source session ID;
- provider;
- bridge ID;
- exact source cwd;
- git root, branch, and HEAD;
- worktree ID;
- eligible activity timestamp.

The candidate cwd must not be overwritten with the inbox path.

### Placement identity

A separate immutable placement value carries:

- canonical inbox cwd;
- normalized local host identity;
- runtime workspace roots;
- placement generation.

For this deployment:

```text
inbox cwd = C:\Users\diego\.hermes
host = local
placement generation = 1
```

The inbox path is resolved through the profile-safe Hermes home mechanism, then
compared with the explicitly configured canonical path. A missing, relative,
non-canonical, or nonexistent inbox is a pre-dispatch failure.

### Verification identity

Native verification requires all of the following:

- exact returned or recovered thread ID;
- local host execution;
- native thread cwd equal to the canonical inbox cwd;
- exact authenticated source registration marker;
- registration metadata whose source cwd equals the candidate cwd;
- persisted history through a fresh normal app-server process;
- no active turn, approval request, user-input request, or system error.

A task is not considered sidebar-visible merely because a native thread ID
exists.

## Future Import Flow

1. Discover the source and capture its exact worktree snapshot.
2. Build the bounded readable registration preview from the source snapshot.
3. Resolve and validate the canonical `.hermes` inbox placement.
4. Claim and durably reserve exactly one native creation.
5. Call `thread/start` once with:
   - `cwd` set to the inbox cwd;
   - `runtimeWorkspaceRoots` containing the inbox cwd and exact source cwd;
   - the existing recovery key as `threadSource`.
6. Persist the returned thread ID before registration, rename, or commit.
7. Run the existing signed registration turn.
8. Read the exact task through a fresh normal app-server client.
9. Verify inbox placement, source identity, marker, persistence, and quiescence.
10. Rename and atomically commit the canonical mirror link.

Once native dispatch begins, every uncertain result remains ambiguous and never
authorizes another create.

## Existing Task Recovery

### Eligibility

An existing task is eligible for automatic recovery only when:

- its sidebar job is already `sidebar_visible`;
- it has an exact bound Codex thread ID;
- its current cwd is outside the canonical inbox;
- its signed source marker authenticates against the durable source and bridge;
- it contains only bridge-owned registration, hydration, refresh, and fixed
  acknowledgement turns;
- its canonical relation remains `mirrors`;
- it is quiescent and contains no substantive Codex work;
- no recovery is already committed or ambiguously reserved.

Tasks with substantive Codex work are retained and reported as manual recovery
items. They are never automatically forked or refreshed.

### Recovery state machine

Recovery uses a separate durable state machine so completed visibility jobs are
not reopened:

```text
recovery_pending
  -> recovery_leased
  -> recovery_reserved
  -> recovery_bound
  -> recovery_verified
  -> recovery_committed

retryable failures:
  recovery_leased -> recovery_retry

uncertain native fork:
  recovery_reserved -> recovery_ambiguous

identity or substantive-work conflict:
  any pre-commit state -> recovery_manual
```

Only one recovery may be leased at a time.

### Recovery procedure

1. Read and authenticate the original exact task.
2. Recheck that every turn is bridge-owned and quiescent.
3. Durably reserve a recovery key before native fork dispatch.
4. Call `thread/fork` once with:
   - the exact original thread ID;
   - `cwd` set to the inbox cwd;
   - runtime roots containing the inbox and exact source cwd;
   - the recovery key as `threadSource`.
5. Persist the returned recovery thread ID immediately.
6. Verify the fork through a fresh normal client:
   - inbox cwd;
   - exact signed source marker;
   - copied bridge-owned history;
   - local host;
   - quiescent persisted state.
7. Rename the recovered task to the existing exact bridge title.
8. Atomically move the canonical source link to the recovered thread.
9. Commit immutable lineage from original thread ID to recovered thread ID.

The original task remains intact and unarchived. It is no longer the canonical
mirror target after recovery commits, but its identity is retained permanently
in the recovery ledger.

### Recovery ambiguity

Before `thread/fork` is invoked, definite validation failures may retry.

After `thread/fork` is invoked:

- a timeout, disconnect, malformed response, or missing exact thread ID becomes
  `recovery_ambiguous`;
- no second fork is permitted;
- later reconciliation searches only by the durable recovery key and exact
  signed source marker;
- zero or multiple authenticated candidates require operator review;
- an exact authenticated candidate may be bound and verified without another
  fork.

If the source advances during recovery, placement recovery completes against
the authenticated original history first. The newest source snapshot is then
coalesced into one refresh job.

## Readable Refresh

### Eligibility

A visible canonical task may refresh only when:

- its relation is still `mirrors`;
- the task contains no substantive Codex work;
- the task is quiescent;
- the source cursor/hash advanced beyond the last delivered preview;
- the task is already in the canonical inbox;
- no unresolved recovery, continuation, or refresh ambiguity exists.

Once the relation becomes `continues`, automatic refresh stops permanently for
that task.

### Refresh payload

The existing preview builder produces:

- a bounded Continuation Brief;
- the latest five source messages;
- redacted repository/worktree state;
- source cursor and source hash;
- a deterministic content digest.

A signed refresh marker binds:

- source session ID;
- canonical Codex thread ID;
- source cursor and hash;
- preview digest;
- refresh generation.

The refresh prompt explicitly identifies itself as bridge maintenance, forbids
project work and `session_continue` during that turn, and requires the exact
`REFRESHED` acknowledgement.

### Refresh scheduling

- Coalesce pending source advances to the newest cursor/hash.
- Refresh at most once every 15 minutes during sustained source activity.
- Enqueue a final refresh after approximately 60 seconds of source inactivity.
- Enforce a global hourly refresh ceiling in addition to the per-source
  interval.
- Never send every intermediate snapshot.
- Prefer placement recovery over refresh when both are actionable.
- Prefer an already-reserved refresh reconciliation over a new refresh send.

### Refresh send state machine

```text
refresh_pending
  -> refresh_leased
  -> refresh_reserved
  -> refresh_sent
  -> refresh_verified
  -> refresh_committed

uncertain send:
  refresh_reserved -> refresh_ambiguous
```

The executor reserves immediately before the exact native send. After dispatch,
any uncertainty is reconciliation-only. The exact marker must appear in a
completed turn before the delivered cursor/hash is committed.

## Continuation Semantics

Sidebar placement never changes the authoritative source cwd.

The existing continuation path already:

- validates the stored source worktree snapshot;
- performs permission preflight on the exact source cwd;
- returns `exact_cwd`;
- renders the instruction that every command and file operation must pass that
  exact cwd.

Inbox-created and recovered tasks preserve that behavior. The source cwd is also
provided as a runtime workspace root so continuation does not inherit an inbox-
only permission boundary.

The first substantive user turn still calls `session_continue`. Bridge-owned
registration, hydration, recovery, and refresh turns do not count as
substantive user work.

## Failure Handling

Fixed failure classes include:

- `inbox_unavailable`: canonical inbox validation failed before dispatch;
- `placement_mismatch`: a created or recovered task is not rooted in the inbox;
- `recovery_original_unreadable`: the exact original task cannot be read;
- `recovery_identity_mismatch`: source, marker, cwd, or history identity failed;
- `recovery_substantive_work`: automatic recovery is unsafe;
- `recovery_native_rejected`: definite pre-dispatch native rejection;
- `recovery_ambiguous`: fork may have succeeded and requires reconciliation;
- `refresh_target_unreadable`: the exact canonical task cannot be read;
- `refresh_identity_mismatch`: source/task/cursor/digest authentication failed;
- `refresh_send_ambiguous`: refresh may have persisted and must not be resent;
- `native_task_not_indexed`: exact task is not yet persistently readable;
- `bridge_temporarily_unavailable`: durable store transition failed.

Raw exception text, source content, signed markers, and opaque lease tokens are
never persisted in public status or copied into failure output.

## Crash Safety and Concurrency

- Registration, recovery, and refresh share the existing process-wide native
  mutation lock.
- Each lane processes at most one leased item per executor call.
- Durable reservation occurs immediately before create, fork, or send.
- Returned native IDs are persisted before any further native operation.
- A crash after native dispatch resumes with reconciliation, not replacement.
- Recovery and refresh use latest-wins queueing but never overwrite an
  ambiguous reserved snapshot.
- Ambiguous exact-ID reconciliation always runs before a new native mutation.
- Among ordinary work, new registration has latency priority. Recovery and
  quiet refresh then alternate through a durable fair scheduler, followed by
  throttled active refresh.
- A continuously nonempty recovery backlog cannot block a newly eligible
  source, and a steady stream of new sources cannot permanently starve
  recovery.

## Observability

`sidebar-status` and health output add:

- canonical inbox cwd and placement generation;
- new imports verified in the inbox;
- projectless tasks awaiting recovery;
- recovered task count;
- manual recovery count and fixed reason codes;
- ambiguous recovery count;
- refresh pending, retry, ambiguous, and committed counts;
- last delivered source cursor/hash digest and refresh timestamp;
- placement recovery latency;
- source-advance-to-refresh latency;
- placement canary status.

The status surface does not expose source messages, raw paths beyond the
configured inbox, markers, or tokens.

A task with a readable marker but the wrong project placement is not counted as
successfully sidebar-visible for the new placement generation.

## Configuration

Behavioral settings live under the existing `session_bridge.sidebar` config:

- `inbox_cwd`;
- `placement_generation`;
- `recovery_enabled`;
- `recovery_backfill_days`;
- `refresh_enabled`;
- `refresh_active_interval_seconds`;
- `refresh_quiet_seconds`.
- `refresh_max_per_hour`.

Defaults for this installation:

```text
inbox_cwd = C:\Users\diego\.hermes
placement_generation = 1
recovery_backfill_days = 30
refresh_active_interval_seconds = 900
refresh_quiet_seconds = 60
refresh_max_per_hour = 20
```

No new environment variable is introduced.

## Testing

Tests assert behavior and invariants rather than source-code shape.

### Unit and store tests

- Source cwd and placement cwd remain separate.
- Inbox path validation fails before native dispatch.
- Recovery eligibility rejects substantive Codex work.
- Recovery reservation prevents a second fork after ambiguity.
- Exact recovery reconciliation binds at most one authenticated candidate.
- Canonical link rebinding and lineage commit are atomic.
- Refresh queueing coalesces to the newest source snapshot.
- Refresh ambiguity never authorizes a duplicate send.
- Continuation permanently disables automatic refresh.

### App-server integration tests

- `thread/start` with inbox cwd persists a task rooted in `.hermes`.
- Runtime workspace roots include the exact source cwd.
- A fresh normal client resumes the lean-runtime registration with normal
  capabilities.
- `thread/fork` preserves bridge-owned history and creates an inbox-rooted task.
- Original task history remains unchanged after recovery.
- Restart between reservation, binding, verification, and commit reconciles
  without duplicate native mutations.

### Production canaries

1. Create one disposable future-import canary and verify its `projectId` equals
   the saved `.hermes` project.
2. Recover one disposable projectless canary and verify:
   - the original still exists unchanged;
   - the recovered task is under `.hermes`;
   - the canonical link targets only the recovered task.
3. Recover the five newest eligible untouched Claude imports.
4. Verify titles, readable previews, last five messages, project IDs, and
   canonical links through native Codex task APIs.
5. Advance one active Claude canary and verify the existing Codex task receives
   one coalesced refresh and floats in recency.

## Rollout

1. Ship schema and status support with recovery and refresh disabled.
2. Run all focused tests and the real app-server canaries.
3. Enable inbox placement for new imports.
   If raw app-server creation at the inbox cwd does not receive the saved
   `.hermes` project identity, stop the rollout and do not create production
   imports through that path.
4. Observe a clean production soak with no projectless new tasks.
5. Enable recovery for the five newest eligible tasks.
6. Verify the five tasks manually through the Codex project sidebar.
7. Recover the remaining eligible imports within the 30-day window, one at a
   time with bounded throughput.
8. Enable quiet refresh, then active refresh.
9. Restart the bridge during controlled reserved states and prove exact
   reconciliation.
10. Run the full regression suite and a final production soak.

Rollback disables new recovery and refresh leases. Already-created recovered
tasks and immutable lineage remain preserved. New registration may temporarily
fall back to queueing, but it must not return to projectless creation.

## Acceptance Criteria

- Every new eligible import is listed with the saved `.hermes` project ID.
- No new import created after cutover has `projectId: null`.
- The five newest recovery canaries show the expected Continuation Brief and
  last five source messages.
- Every recovered source has one canonical Codex mirror target.
- Every original projectless task remains readable and unchanged.
- No task containing substantive Codex work is automatically forked.
- An ambiguous fork or refresh send produces no duplicate native mutation.
- Active uncontinued sources refresh within the configured interval.
- Quiet sources receive a final refresh after the configured settle period.
- Continued tasks receive no later automatic refresh.
- Registration, recovery, and refresh queues drain after restart.
- Focused, integration, and full regression tests pass.
- Production health reports zero placement mismatches and zero unresolved
  ambiguity before the work is declared complete.
