# Session Sidebar Backfill Exclusions Design

## Purpose

Make Session Bridge sidebar backfill preview the same filesystem and worktree
validation that apply performs, and durably exclude historical sessions whose
recorded source working directory no longer exists. A historical exclusion is
an expected terminal outcome, not a delivery failure.

This change preserves the existing safety boundary: Session Bridge never
invents a cwd, silently switches a continuation to another checkout, or creates
a native Codex task whose exact source location cannot be proven.

## Problem

The current dry-run stops after eligibility, identity, title, cwd-field, and
existing-job checks. It reports a candidate as queueable before calling
`capture_worktree_snapshot`. Apply performs that snapshot immediately before
enqueue.

That difference caused a guarded rollout batch to preview seven queueable
Claude sessions but queue only two. Apply then classified sessions referencing
deleted Claude worktrees as failures. The permanent broker was correctly
paused, but the preview had provided an inaccurate safety signal.

Simply ignoring missing paths is insufficient. Backfill starts from the newest
candidate on every bounded invocation. Without durable exclusion state, the
same historical records are repeatedly examined and can consume the bounded
query or examination budget before older valid sessions are reached.

## Decisions

1. Dry-run and apply share one candidate preflight path.
2. A missing cwd field or a recorded cwd that no longer resolves to an existing
   directory is a fixed historical exclusion: `source_cwd_missing`.
3. Apply persists exclusions in an additive table separate from delivery jobs.
4. Dry-run remains side-effect free and reports the exclusions it would persist.
5. Persisted exclusions are omitted from future candidate pages at the store
   query boundary so they do not consume registration budgets.
6. `capture_worktree_snapshot` must distinguish a nonexistent or non-directory
   path from permission and other I/O failures. Source identity mismatch,
   malformed source records, permission failures, and unexpected exceptions
   remain failures. They are never converted to exclusions.
7. Existing pending, leased, retry, visible, or failed delivery jobs take
   precedence over exclusion discovery. No existing job is rewritten.
8. The two valid pending rollout jobs remain pending across deployment and are
   delivered through the existing one-job native broker after verification.

## Alternatives considered

### Dynamic skip without persistence

This is the smallest code change, but every backfill invocation would rediscover
the same deleted worktrees. A dense run of exclusions could prevent the bounded
scanner from reaching older valid sessions. This approach is rejected.

### Create a native task with blocked continuation

This maximizes sidebar visibility, but knowingly creates tasks that cannot meet
the exact-cwd continuation contract. It would also turn historical noise into
permanent native tasks. This approach is rejected for the approved rollout.

### Reuse the delivery job table with an excluded state

This would mix pre-delivery eligibility decisions with broker delivery state,
expand every delivery-state invariant, and risk treating exclusions as failed
or actionable jobs. A separate exclusion ledger keeps the broker state machine
unchanged and is preferred.

## Storage

Add an additive `session_sidebar_exclusions` table through the next Session
Bridge schema migration.

Each row contains:

- `source_session_id` as the primary key;
- `provider` (`claude` or `hermes`);
- fixed `reason_code` (`source_cwd_missing` for this change);
- `excluded_at` and `updated_at` finite timestamps;
- a deterministic source identity digest suitable for conflict detection.

The table stores no transcript, registration prompt, lease token, native Codex
task ID, or raw exception text.

Recording the same source, provider, reason, and digest is idempotent. A
conflicting provider, reason, or digest fails closed and leaves the existing row
unchanged.

`list_sidebar_candidates` excludes sources already present in the ledger. Store
lookups and counts expose exclusions separately from `session_sidebar_jobs`.

## Candidate preflight

The coordinator uses one preflight sequence for dry-run, apply, and continuous
registration:

1. Validate the source shape and duplicate-free page identity.
2. Apply meaningful-session eligibility.
3. Verify the canonical provider-native source ID.
4. Skip an existing delivery job without reading its filesystem.
5. Build the deterministic title and candidate metadata.
6. Require a non-empty recorded cwd.
7. Run `capture_worktree_snapshot` for the recorded cwd.
8. If indexed Git metadata exists, require the current snapshot to resolve to a
   Git root; otherwise return `source_identity_mismatch` as a failure.
9. Replace candidate cwd and Git fields with the canonical snapshot.
10. In dry-run, count the candidate as queueable without writing.
11. In apply or continuous mode, enqueue the candidate with the snapshot.

If step 6 or 7 produces `source_cwd_missing` because the field is absent, the
path does not exist, or a path component is not a directory, preflight returns
an exclusion result. Dry-run increments exclusion counters only. Apply and
continuous mode first record the durable exclusion and then increment the
counters. If the exclusion write conflicts or fails, the candidate is counted
as failed instead.

`PermissionError` and other filesystem `OSError` cases use a fixed non-exclusion
failure code and retain degraded behavior. They must not be persisted in the
exclusion ledger.

No other exception is interpreted as an exclusion.

## Summary and CLI contract

Extend `SidebarRegistrationSummary` with:

- `excluded`: total expected exclusions encountered in this invocation;
- `excluded_by_reason`: fixed-code counts, initially
  `{ "source_cwd_missing": N }` when nonzero.

Existing fields retain their meaning:

- `queued` or `would_queue` counts valid candidates only;
- `failed` counts unknown, malformed, conflicting, or infrastructure failures;
- `by_provider` counts queueable or queued candidates only;
- `examined` counts every raw candidate inspected.

The CLI exits successfully when `failed == 0`, even if `excluded > 0`.
Exclusions appear in JSON output and do not add a recent error code. Any nonzero
`failed` value retains the degraded exit behavior.

Sidebar status adds `sidebar_excluded` to its counts without including it in
pending age, delivery latency, broker health, or retry/failure gates.

## Concurrency and races

Filesystem state can change between dry-run and apply. Apply therefore repeats
the same preflight rather than trusting preview output. If a directory vanishes
after preview, apply records the fixed exclusion. If identity changes instead,
apply fails closed.

Exclusion insertion and conflicting-identity detection occur in one database
transaction. Candidate listing omits only committed exclusion rows. Concurrent
identical exclusions converge idempotently.

An existing delivery job always wins over a newly discovered exclusion. This
prevents a temporary path disappearance from rewriting a pending or visible
job.

## Testing

Unit and integration coverage must prove:

- dry-run calls the real worktree preflight and excludes a missing directory;
- dry-run writes neither a job nor an exclusion;
- dry-run and apply return matching queueable and exclusion counts for stable
  input;
- apply persists `source_cwd_missing` and does not create a delivery job;
- repeated apply is idempotent and the persisted exclusion disappears from
  later candidate pages;
- a run of more than the examination budget in persisted exclusions cannot
  starve an older valid candidate;
- an existing pending or visible job is not converted to an exclusion;
- source identity mismatch remains a failure and degraded CLI exit;
- permission and non-missing I/O errors remain failures and are not persisted;
- exclusion-only CLI output exits successfully;
- status reports exclusions separately without degrading broker health;
- the existing registration, delivery, retry, reconciliation, and continuation
  suites remain green.

Tests use temporary repositories and deleted temporary directories. They never
read or write the live `~/.hermes` state.

## Rollout

1. Land the migration, store ledger, shared preflight, summary fields, CLI
   behavior, and tests with the permanent broker still paused.
2. Restart Session Bridge and verify provider health.
3. Run a 30-day limit-10 dry-run. Review exclusions as fixed
   `source_cwd_missing` only.
4. Run one bounded apply. It may persist exclusions and queue at most ten valid
   sessions.
5. Verify the two pre-existing pending jobs remain intact and no duplicate
   source, bridge, idempotency, or native task identities appear.
6. Resume the permanent one-job broker and drain the current batch.
7. Continue bounded backfill only when pending, leased, retry, and failed counts
   are zero.
8. When no queueable candidates remain, run the existing 30-minute clean soak,
   enable continuous registration, and verify an empty broker cycle.

## Rollback

- Pause the permanent broker.
- Disable continuous registration.
- Leave existing native Codex tasks, delivery jobs, and exclusion audit rows
  intact.
- Do not delete or rewrite provider sessions.
- Reverting the coordinator to ignore the exclusion table is safe because the
  table is additive, but a rollback must not drop audit data automatically.

## Acceptance criteria

- Dry-run and apply agree on queueable and excluded candidates for stable input.
- Deleted or absent historical cwd records are durable, non-degraded
  exclusions.
- Persisted exclusions cannot consume future bounded scan budgets.
- Unknown identity or infrastructure problems still stop rollout.
- The two currently pending valid jobs are delivered without replacement or
  duplication after the broker resumes.
- The remaining 30-day meaningful-session backfill can progress to exhaustion.
- Continuous registration is enabled only after the established clean soak.
