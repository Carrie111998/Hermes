# Session Sidebar `notLoaded` Preserve-and-Recover Design

Date: 2026-07-26

Status: approved by the user after live diagnosis

## Objective

Restore continuous Claude/Hermes session delivery to the Codex sidebar without
deleting, replacing, or abandoning an already-created native Codex task.

The immediate incident is a failed Session Bridge row whose exact Codex task exists,
contains the authenticated registration marker, and has a completed registration
turn, but whose native thread status remains `notLoaded`. The current heartbeat
rejects that status as non-idle, exhausts the row's retry budget, and then blocks the
entire sidebar queue behind `sidebar_failed`.

## Confirmed live state

At diagnosis time:

- Session Bridge and both source watchers were healthy.
- The sidebar catalog had 670 visible sessions, 179 pending sessions, 6 retry rows,
  and 3 failed rows.
- One failed row was a global execution blocker:
  `sidebar-job:4d1adae2b78f3e0cd7697c21f5bd03851f5bf576132c153e38987e44ac101ba3`.
- Its source session was
  `claude:2a23388a-5d72-4e0d-bbad-5e655ef4c8a3`.
- Its preserved Codex task was
  `019f8bed-2a9f-7353-a841-551fc1c8b68e`.
- The failed error was `native_task_not_indexed` after five attempts.
- Reading that exact task returned the expected local cwd, the exact signed Session
  Bridge marker, and one completed registration turn, while the top-level task
  status remained `notLoaded`.

The deterministic sidebar executor already maps `notLoaded` to its internal idle
status. The mismatch is in the native heartbeat path, which requires a literal
`idle` status before rename and commit.

## Safety invariants

The repair must preserve the existing Session Bridge guarantees:

1. The catalog and sidebar queue remain authoritative.
2. The exact bound Codex task ID is never cleared or replaced.
3. A retry never creates a second native task.
4. A native task is accepted only when its identity and signed marker authenticate
   the expected source session.
5. Active, unreadable, errored, ambiguous, or mismatched tasks remain rejected.
6. Queue mutations are compare-and-swap guarded against stale operator input.
7. No direct mutation of Codex application state or databases is introduced.

## Considered approaches

### 1. Terminally resolve and abandon the failed task

This would unblock the queue but would leave the exact Claude session unrecovered
and would not prevent the next `notLoaded` registration from failing the same way.
It is rejected.

### 2. Ignore failed rows as global blockers

This would let later jobs run, but it would weaken a deliberate safety gate and
could hide a genuinely ambiguous native creation. It is rejected.

### 3. Preserve the bound task, authenticate quiescence, and explicitly retry

This is selected. The worker gains a narrow authenticated interpretation of
`notLoaded`, and the store gains an operator-only retry that preserves the exact
bound task and creation reservation. The current blocker is then requeued through
that path and completed in place.

## Selected design

### Authenticated `notLoaded` quiescence

The installed `session-sidebar-sync` skill may treat a native task with top-level
status `notLoaded` as quiescent only when all of the following are true:

- the returned task ID exactly matches the leased row's `codex_thread_id`;
- the host is local and the task's project/cwd identity matches the lease;
- the registration prompt contains the exact expected signed marker;
- at least one turn is returned;
- every returned turn is completed;
- no active turn, approval request, user-input request, or system error is present.

Literal `idle` remains accepted under the existing identity and marker checks.
`notLoaded` is not globally redefined as idle; it is accepted only by this
authenticated completed-registration predicate.

The worker must continue polling or fail closed when:

- the task is active or has an incomplete turn;
- the task cannot be read;
- the task reports `systemError`;
- the task ID, host, cwd/project, source identity, or signed marker differs;
- the response is ambiguous or lacks the registration turn.

Once quiescence is proven, the existing rename and commit flow runs against the
preserved task ID.

### Guarded bound-task retry

Add a distinct operator-only store operation for failed, already-bound sidebar
rows. It is intentionally separate from the existing unbound retry operation.

The operation requires exact caller-supplied values for:

- sidebar job ID;
- source session ID;
- Codex task ID;
- expected error code, restricted to `native_task_not_indexed`;
- an explicit exact-bound-task confirmation token.

Within one transaction, it must verify:

- the row is currently `sidebar_failed`;
- job ID, source session ID, Codex task ID, and expected error all match;
- there is no completion digest, visible timestamp, active lease, or terminal
  resolution;
- the create reservation still binds the same job to the same Codex task;
- no conflicting sibling row exists for the source session.

On success it transitions the row to `sidebar_retry`, clears the current error and
lease fields, resets the automatic-attempt counter for one reviewed recovery cycle,
and schedules the retry immediately. It preserves:

- `codex_thread_id`;
- idempotency key;
- signed marker inputs;
- create reservation;
- source/worktree identity.

Claiming that row must return the preserved Codex task as a recovered binding.
Neither the store nor the skill may enter native creation for that retry.

The CLI exposes this as `session-bridge sidebar-retry-bound` rather than broad SQL
access. It requires:

```text
--job-id <exact job ID>
--source-session-id <exact source ID>
--codex-thread-id <exact preserved task ID>
--expected-error-code native_task_not_indexed
--confirm PRESERVE_EXACT_BOUND_TASK
```

Its result reports only the job ID, preserved task ID, new state, and sanitized
status.

### Incident recovery sequence

1. Pause the three-minute sidebar heartbeat.
2. Install the authenticated `notLoaded` worker behavior and guarded retry command.
3. Requeue the exact failed row with all expected identifiers supplied.
4. Invoke the worker once.
5. Re-read the exact Codex task and verify its authenticated quiescent state.
6. Rename and commit the existing task in place.
7. Verify the failed blocker is gone and the next pending/retry row becomes
   actionable.
8. Run a bounded drain sample and confirm no duplicate task was created.
9. Resume the heartbeat only after the sample passes.

## Tests

### Skill contract tests

- accepts literal `idle` under the existing authenticated checks;
- accepts `notLoaded` only for an exact completed registration task;
- rejects `notLoaded` with no turns, an incomplete turn, a mismatched ID/cwd/marker,
  an approval/input request, or a system error;
- proves rename and commit target the preserved task ID;
- proves native creation is forbidden for the recovered bound row.

### Store and CLI tests

- requeues the exact failed bound `native_task_not_indexed` row;
- preserves the Codex task ID, reservation, idempotency key, and source identity;
- rejects stale job/source/task/error inputs;
- rejects unbound, completed, visible, leased, terminally resolved, or ambiguous
  rows;
- proves concurrent or repeated recovery attempts are compare-and-swap safe;
- proves the existing unbound retry behavior is unchanged;
- verifies CLI output is sanitized and the confirmation token is mandatory.

### End-to-end regression

Seed a failed bound row and a native task fixture with `notLoaded`, the exact signed
marker, and a completed registration turn. Recover the row, run one worker cycle,
and assert:

- the original task is renamed and committed;
- the row becomes visible/completed;
- no create call occurs;
- no second native task exists;
- the queue blocker clears and the next candidate can be leased.

## Rollout and rollback

Rollout uses the existing asset installation path and the guarded CLI; no manual
database edit is permitted. Before resuming automation, compare the live catalog,
failed-row count, blocker list, and native task IDs before and after recovery.

If validation fails:

- keep the heartbeat paused;
- leave the original task and binding intact;
- do not terminally resolve, delete, archive, or recreate the task;
- restore the prior installed skill asset;
- leave the row failed for diagnosis.

## Non-goals

This repair does not change transcript mirroring or proactively render a Claude Code
conversation inside its registration task. It restores reliable native task
registration and continuation for the exact source session. Transcript translation
and richer initial task presentation remain a separate product change.

## Acceptance criteria

- The exact existing Codex task is recovered without replacement.
- The blocking row completes and `sidebar_failed` no longer blocks execution.
- At least one subsequent queued session is delivered successfully.
- Duplicate-task count does not increase.
- Future authenticated completed-registration tasks with `notLoaded` converge
  without exhausting retries.
- Active, mismatched, unreadable, or ambiguous tasks continue to fail closed.
- The heartbeat is resumed only after live verification succeeds.
