# Codex Native Sidebar Broker Design

Date: 2026-07-15

Status: approved in conversation; pending written-spec review

## Summary

Hermes Session Bridge already indexes Claude Code, Codex, and Hermes sessions and
can create signed Codex placeholders through a separate `codex app-server`
process. Those externally created Codex threads are persisted and searchable,
but Codex Desktop does not reliably load them into its left sidebar.

This design replaces external app-server creation as the Codex sidebar delivery
path. A Codex-owned broker task runs every minute, leases eligible Claude Code
and Hermes sessions from Session Bridge, and creates their sidebar entries with
Codex's native task-creation capability. The bridge remains authoritative for
eligibility, lineage, hydration, retries, and duplicate prevention.

The result must satisfy one visible acceptance criterion: every eligible source
session appears somewhere in Codex's left sidebar within one minute. It may be
grouped under its matching saved project or under the `.hermes` Session Inbox.

## User-approved requirements

1. Source providers are Claude Code and Hermes.
2. A source session is eligible only when it contains at least one meaningful
   user request.
3. Eligible sessions active in the last 30 days are backfilled.
4. Newly eligible sessions are delivered continuously.
5. A new eligible session appears in Codex's left sidebar within one minute.
6. The sidebar section is not prescribed. Projects, Tasks, or another native
   sidebar group are acceptable.
7. Continuing the Codex task must preserve the exact original working
   directory or worktree, Git branch, and access to uncommitted files.
8. Native Codex sidebar visibility, not Ctrl+G searchability, is the acceptance
   signal.
9. Existing Hermes unified-catalog search remains available and authoritative.
10. Automatic external app-server placeholder creation remains disabled.

## Current-state diagnosis

### What works

- Session Bridge indexes Claude Code and Codex through provider adapters and
  projects Hermes sessions into one catalog.
- Hermes Desktop exposes that catalog.
- Signed bridge markers, lineage, context packs, continuation, divergence, and
  durable mirror jobs are implemented.
- A bridge-created Codex thread is persisted and discoverable with Ctrl+G.

### What fails

- The running Codex Desktop app owns one private app-server connection.
- Session Bridge starts a separate app-server process.
- `thread/start` emits `thread/started` to the connected bridge client, not to
  the already-running Desktop client.
- Codex Desktop groups local tasks under saved projects and hydrates a recent
  subset. A source cwd in a hidden Claude worktree may have no saved project
  group.
- Setting `threadSource: "user"` is semantically correct but insufficient to
  make an externally created thread appear live in the sidebar.

### Supported product surface

The current Codex manual documents these relevant capabilities:

- Import from another agent can bring recent chats and projects into native
  Codex tasks, but it is an interactive import flow rather than a continuous
  provider API.
- Scheduled work from a task supports minute-based follow-up loops while the
  desktop app is running.
- Codex's native task-management surface can create, rename, inspect, and
  manage desktop-owned tasks.
- App-server is supported for rich custom clients, but its notifications are
  scoped to the active transport connection.

## Selected architecture

### Components

#### 1. Session Bridge eligibility and delivery coordinator

Session Bridge continues to own provider indexing and durable cross-harness
state. It gains a distinct delivery mode for Codex Desktop sidebar tasks.

Responsibilities:

- Classify source sessions as eligible or ineligible.
- Queue one sidebar-delivery job per canonical source session.
- Lease bounded batches to the Codex broker.
- Validate lease ownership and expiry.
- Persist the resulting Codex task identity.
- Reconcile ambiguous outcomes without creating duplicates.
- Expose delivery state in MCP and the Hermes catalog.

The coordinator never invokes `codex app-server thread/start` for this delivery
mode.

#### 2. Session sidebar MCP contract

Session Bridge exposes three narrow broker operations:

- `session_sidebar_pending(limit)` leases and returns eligible jobs.
- `session_sidebar_commit(lease_token, codex_thread_id)` records successful
  native task creation after verifying the source and signed marker binding.
- `session_sidebar_fail(lease_token, error_code)` releases or advances the job
  according to retry policy.

The existing `session_continue` operation remains responsible for context-pack
hydration on the first substantive Codex turn.

The tool schemas expose only the information required by the broker. Raw
transcripts, provider secrets, native source paths outside the selected session,
and internal exception text are not returned.

#### 3. Codex sidebar-sync skill

A personal Codex skill named `session-sidebar-sync` defines the deterministic
broker procedure. It is invoked explicitly by the scheduled broker prompt.

The skill:

1. Requests a bounded pending batch.
2. Lists saved Codex projects.
3. Chooses the destination project for each source.
4. Creates a native Codex task.
5. Renames the task with a provider prefix and source title.
6. Commits the native task ID to Session Bridge.
7. Reports only actionable failures.

The skill contains no provider parser or persistence logic. Those remain in
Session Bridge so the broker procedure stays small and reviewable.

#### 4. Codex-owned scheduled broker task

A dedicated Codex task runs the sidebar-sync skill every minute through a task
heartbeat. It is project-scoped to the saved `.hermes` project and uses local
execution.

The broker task is an execution host, not a source of truth. If it is paused,
closed, rate-limited, or unavailable, durable jobs remain queued in Session
Bridge and are delivered later.

The scheduled prompt instructs the broker to:

- Invoke `$session-sidebar-sync`.
- Exit quietly when no jobs are pending.
- Avoid project work and transcript summarization.
- Never create a task without a valid lease.
- Never fall back to external app-server creation.

### Destination selection

For each source session:

1. Canonicalize and validate the exact source cwd.
2. If the exact cwd is a saved Codex project, create the task there.
3. Otherwise, if the source cwd equals a saved stable repository root, use that
   project.
4. Otherwise, create the sidebar task under the saved `.hermes` project, which
   acts as the Session Inbox.

Grouping under `.hermes` does not change the operational working directory.
The registration prompt and hydration contract preserve the exact source cwd.

### Native task title

Titles use a deterministic provider prefix:

- `[Claude] <source title>`
- `[Hermes] <source title>`

Titles are redacted and bounded before delivery. If the provider has no usable
title, Session Bridge derives one from the first meaningful user request using
the existing deterministic title rules. The bridge ID is never placed in the
visible title.

## Meaningful-session eligibility

Eligibility is deterministic and provider-neutral.

A source session qualifies when all of the following are true:

- Provider is Claude Code or Hermes.
- Origin is native rather than a bridge placeholder or continuation target.
- The session was active within the last 30 days or became active after
  continuous delivery was enabled.
- At least one user-authored event remains meaningful after normalization.
- No succeeded sidebar-delivery link already exists for the canonical source.

Normalization removes or ignores:

- Whitespace-only content.
- Tool calls and tool results.
- System and developer instructions.
- Signed bridge registration blocks.
- Registration acknowledgements such as `READY`.
- Session-control commands with no user request, including resume, clear,
  help, quit, and equivalent provider controls.
- Acknowledgement-only content such as `ok` or `yes` when it is the session's
  only user content.
- Subagent-only and automation-only runs.

After those exclusions, a message is meaningful when its normalized text has at
least three Unicode letters or digits and is not an exact case-insensitive match
for the fixed acknowledgement/control set. This keeps short requests such as
`fix it` eligible while rejecting a session whose only content is `ok` or
`yes`. The classifier must not make an LLM call or use a mutable vocabulary
snapshot as its primary test.

## Durable state and idempotency

### Delivery identity

Each job uses an immutable idempotency key:

`codex-sidebar:<canonical-source-session-id>:v1`

Only one active or succeeded job may exist for that key.

### Delivery states

- `sidebar_pending`: queued and available for lease.
- `sidebar_leased`: temporarily owned by one broker run.
- `sidebar_visible`: native task creation committed successfully.
- `sidebar_retry`: a retryable failure occurred.
- `sidebar_failed`: retry budget exhausted or invariant failed.

Sidebar delivery uses a new additive `session_sidebar_jobs` table rather than
overloading provider mirror jobs. At minimum it stores the idempotency key,
canonical source ID, bridge ID, state, lease digest and expiry, attempt count,
next-attempt time, fixed error code, native Codex task ID, and audit timestamps.
Existing mirror-job rows and invariants remain unchanged.

### Lease rules

- Leasing is atomic and bounded.
- A lease contains a high-entropy opaque token and expiry timestamp.
- Commit and fail operations require the exact token.
- Expired leases return to retryable state.
- A worker cannot commit another worker's lease.
- Repeated commit with the same source and Codex task ID is idempotent.
- Commit with a different Codex task ID after success fails closed.

### Ambiguous native creation

The broker includes the signed marker and canonical source ID in the native
task's initial prompt. If creation succeeds but commit fails, reconciliation
searches native Codex tasks for that authenticated marker before allowing
another creation attempt.

No duplicate task is created until the previous outcome is proven absent.

## Registration and continuation contract

### Registration prompt

The initial native task prompt contains:

- A fixed statement that this is a Hermes Session Bridge placeholder.
- Signed bridge marker.
- Canonical source session ID.
- Exact source cwd and worktree metadata.
- Expected Git branch and snapshot identity when available.
- Instruction to call `session_continue` before answering the first
  substantive user message.
- Instruction not to perform project work during registration.

It does not contain the raw source transcript.

### Exact working-directory preservation

Before returning a context pack, `session_continue` revalidates:

- The source cwd is still an existing directory.
- The canonical path still resolves to the recorded location.
- The worktree identity still matches when Git metadata is available.
- The branch and HEAD are reported accurately.
- The current permission mode permits operations in that directory.

All file and command operations in the continued task must pass the exact source
cwd as their working directory. Grouping the task under `.hermes` is a sidebar
organization choice only.

If the source directory disappeared, moved, or no longer represents the same
worktree, hydration returns a visible blocking warning. The task remains in the
sidebar, but the agent must not silently switch to the repository root or a
newly created worktree.

### Permission preflight

The live rollout must prove that a task grouped under `.hermes` can operate in
the exact external source directory under Diego's configured Codex permission
mode. If the task lacks access, rollout stops before backfill. The design does
not weaken global permissions automatically or mutate security settings.

## Scheduling and throughput

- Heartbeat interval: one minute.
- Continuous batch limit: five jobs per heartbeat.
- Manual backfill batch limit: ten jobs per invocation.
- Lease duration: five minutes.
- Retry budget: five failed attempts, with retry delays of 1, 2, 4, 8, and 15
  minutes plus bounded jitter.
- An empty batch produces no user-facing message.
- Initial 30-day backfill uses explicit bounded batches after both provider
  canaries pass.
- Backfill and continuous delivery share the same idempotency and lease path.
- A broker run that reaches its time budget commits completed items and safely
  releases the remainder.

Configuration is stored in `config.yaml`, not `.env`. Credentials remain in the
existing secret stores.

## Failure handling

### Retryable failures

- Codex task-creation tool temporarily unavailable.
- Desktop app offline or task heartbeat delayed.
- Transient Session Bridge or SQLite lock error.
- Native rename failure after a task ID is known.
- Temporary project-list lookup failure.

Retry uses bounded exponential backoff with jitter and durable attempt counts.

### Manual failures

- Authenticated marker conflict.
- Source identity mismatch.
- Commit attempts to replace an already linked Codex task.
- Malformed or provider-misrouted source ID.
- Missing exact cwd at first hydration.
- Permission preflight failure for an inbox-grouped task.
- Retry budget exhausted.

Manual failures expose fixed error codes, not exception strings or secrets.

### Provider and broker isolation

A Claude parser failure does not block Hermes delivery. A Hermes catalog failure
does not invalidate already indexed Claude jobs. A Codex broker outage queues
both providers without losing source indexing.

## Hermes catalog presentation

Hermes Desktop adds sidebar-delivery status to each eligible source session:

- Pending
- Visible in Codex
- Retrying
- Failed

The public API uses an allowlist of fields and fixed error codes. It does not
expose lease tokens, signed marker payloads, native transcript paths, internal
exceptions, or arbitrary persisted state.

## Security and privacy

- Provider-native transcript stores remain read-only.
- Codex SQLite, global UI state, and packaged application files are never
  modified directly.
- Native task creation occurs only inside the Codex-owned broker task.
- Every task is bound to one source by an authenticated signed marker.
- Reverse mirrors, continuations, and existing bridge placeholders are excluded
  from eligibility.
- Registration prompts contain only minimal redacted metadata.
- Context packs keep the existing deterministic redaction and size limits.
- Lease tokens are opaque, short-lived, and never logged in full.
- Automatic archive or delete propagation remains prohibited.

## Observability

Session Bridge health and status report:

- Eligible source count by provider.
- Pending, leased, retrying, visible, and failed counts.
- Oldest pending age.
- Last successful broker heartbeat.
- Last committed Codex task ID, redacted where appropriate.
- Fixed recent failure codes.
- Delivery latency percentiles from eligibility to commit.

The laptop monitor alerts when the broker heartbeat is stale or when the oldest
pending job exceeds the one-minute service objective by a configured grace
period. It does not page on a legitimately empty queue.

## Testing strategy

### Unit tests

- Meaningful Claude user request is eligible.
- Meaningful Hermes user request is eligible.
- Empty, acknowledgement-only, control-only, automation-only, subagent, and
  bridge-origin sessions are ineligible.
- The 30-day boundary is deterministic.
- One canonical source produces one idempotency key.
- Lease acquisition, expiry, ownership, retry, and commit invariants hold.
- Repeated exact commit is idempotent.
- Conflicting task identity fails closed.
- Provider title prefixing and redaction are deterministic.

### Integration tests

- A fake native creator receives the signed registration prompt and returns a
  task ID that is durably linked.
- A post-create commit failure reconciles the exact authenticated task instead
  of creating a duplicate.
- Saved-project selection and `.hermes` inbox fallback are deterministic.
- Claude and Hermes queues remain isolated when one provider fails.
- Context hydration preserves the exact source cwd and detects a removed or
  replaced worktree.
- No external app-server creation occurs for sidebar-delivery jobs.

### Live canaries

1. Manually invoke the broker skill in the current Codex task before enabling
   scheduling.
2. Deliver one meaningful Claude session.
3. Confirm the native task appears in the left sidebar without Ctrl+G.
4. Continue it and prove commands run in the exact Claude cwd/worktree.
5. Deliver one meaningful Hermes session.
6. Confirm the native task appears in the left sidebar.
7. Continue it and prove commands run in the exact Hermes cwd.
8. Confirm both links and context packs in the Hermes catalog.
9. Trigger one additional leased canary from the scheduled broker task and
   prove that scheduled execution exposes native task creation and renaming.

User-visible sidebar confirmation is a mandatory live gate because app-server
searchability alone does not prove the product requirement.

## Rollout

1. Land storage, eligibility, MCP, skill, and tests with scheduling disabled.
2. Restart Session Bridge and verify all memory and session MCP health checks.
3. Run the broker skill manually with no pending work.
4. Run one Claude canary and one Hermes canary.
5. Verify exact cwd permission and continuation behavior.
6. Run a scheduled native-creation canary. If the scheduled execution surface
   does not expose native task creation and renaming, stop without backfill.
7. Run a bounded recent backfill sample.
8. Verify sidebar count, titles, duplicate absence, and catalog links.
9. Complete the remaining 30-day backfill in bounded batches.
10. Enable the one-minute task heartbeat.
11. Observe for at least 30 minutes with both harnesses open.
12. Enable continuous eligibility enqueue only after the soak passes.

## Rollback

- Pause or delete the Codex broker heartbeat.
- Disable sidebar-delivery enqueue in Session Bridge config.
- Leave existing native Codex tasks and source sessions intact.
- Preserve catalog links and delivery audit state for diagnosis.
- Do not delete or archive created tasks automatically.
- External app-server automatic creation remains off throughout rollback.

## Acceptance criteria

The feature is complete only when all of the following are true:

- Every eligible Claude Code and Hermes session from the 30-day window has one
  and only one native Codex sidebar task.
- A newly eligible session appears within one minute while Codex Desktop and
  Session Bridge are running.
- Sidebar visibility is confirmed directly; Ctrl+G-only visibility fails the
  criterion.
- Continuing each provider's canary uses the exact recorded source cwd or
  worktree and preserves access to its branch and uncommitted files.
- Empty and non-meaningful sessions do not create sidebar noise.
- Broker restart, lease expiry, and ambiguous native creation do not create
  duplicates.
- Pausing the broker causes durable queueing and later recovery.
- No direct Codex state or package mutation is introduced.
- Automatic external placeholder creation remains disabled.

## Explicit non-goals

- Making Codex Desktop load externally created app-server threads.
- Patching Codex's renderer, package, SQLite database, or global state.
- Copying complete source transcripts into registration prompts.
- Automatically merging divergent Claude, Hermes, and Codex branches.
- Propagating archive or delete actions across harnesses.
- Changing global Codex security settings.
- Guaranteeing delivery while Codex Desktop is closed.

## References

- Existing cross-harness design:
  `docs/superpowers/specs/2026-07-13-cross-harness-session-bridge-design.md`
  in the Hermes operations repository.
- Existing implementation plan:
  `docs/superpowers/plans/2026-07-13-cross-harness-session-bridge.md`.
- Codex manual sections: Import from another agent; Scheduled tasks; Projects,
  chats, and tasks; Codex App Server.
- GBrain: `systems/cross-harness-session-bridge`.
- MemPalace corrected sidebar investigation:
  `drawer_hermes_cross-harness-session-bridge-implementation-2026-07-13_8ae896d1f61d7198f357f102`.
