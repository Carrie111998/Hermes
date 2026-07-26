# Pre-CS-01 WIP Debt

## What is quarantined

Seven tests describe lifecycle behavior whose matching production half is not
present in the current checkout:

1. `tests/gateway/test_telegram_restart_resume_policy.py::test_telegram_can_continue_interrupted_task_after_restart`
   expects Telegram's optional `resume_interrupted_tasks` setting to make
   restart recovery continue unfinished work instead of asking what to do next.
2. `tests/run_agent/test_post_task_review_lifecycle.py::test_terminal_gate_rejects_active_todo`
   expects post-task review to wait while a todo remains active.
3. `tests/run_agent/test_post_task_review_lifecycle.py::test_terminal_gate_accepts_completed_task`
   expects post-task review to proceed after all terminal conditions are met.
4. `tests/run_agent/test_post_task_review_lifecycle.py::test_terminal_gate_rejects_active_process_and_delegation`
   expects post-task review to wait for active processes and delegations.
5. `tests/run_agent/test_post_task_review_lifecycle.py::test_review_completion_is_claimed_once`
   expects an atomic exactly-once completion claim.
6. `tests/run_agent/test_post_task_review_lifecycle.py::test_spawn_starts_once_for_duplicate_completion`
   expects duplicate completion IDs to start at most one review.
7. `tests/run_agent/test_post_task_review_lifecycle.py::test_bounded_review_wait_refuses_active_turn`
   expects the bounded review wait to refuse an active foreground turn.

Pytest marks exactly these seven node IDs as non-strict expected failures from
the repository-level `tests/conftest.py`. No test file listed above is edited.

## Why this happened

On 2026-07-23, `git stash push --include-untracked` preserved a paired WIP
change across two stash objects. The untracked tests were preserved at
`f9f0eccf7`, while their matching tracked implementation was preserved in the
working-tree stash at `stash@{0}`, ref `85cc4ddbe`. The current checkout
contains the tests byte-for-byte but not their production half.

CS-17 established that both artifacts predate CS-01. This is dormant WIP debt,
not a regression introduced by CS-01 through CS-16.

## Where the implementation lives

The matching implementation is parked in:

```text
stash@{0}
85cc4ddbef56b8eb926af52cbf7679b93331fc27
```

It includes intended lifecycle work in `agent/background_review.py`,
`run_agent.py`, `agent/codex_runtime.py`, `agent/turn_finalizer.py`, and
`plugins/platforms/telegram/adapter.py`.

## Warning

Do **not** run `git stash pop stash@{0}` or `git stash apply stash@{0}`.
That stash also contains broad, unrelated July 23 changes. Applying it
wholesale would inject unreviewed drift into the runtime checkout.

## Recovery procedure

After the Monday Tihna cutover:

1. Inspect the intended lifecycle portions without applying the stash:

   ```text
   git stash show -p stash@{0} -- agent/background_review.py plugins/platforms/telegram/adapter.py
   ```

   If the local Git version rejects a path-limited `stash show`, use the
   equivalent read-only comparison:

   ```text
   git diff 8208fc527 85cc4ddbe -- agent/background_review.py plugins/platforms/telegram/adapter.py
   ```

2. Create a separately scoped repair. Manually reimplement or extract only the
   intended restart/review lifecycle behavior; do not apply the stash.
3. Write dedicated behavioral tests for terminal gating, exactly-once review
   claims, bounded idle waiting, duplicate suppression, and Telegram restart
   recovery.
4. Run the adjacent background-review, gateway, restart, and established
   regression suites.
5. Remove each quarantine marker only after its production contract passes.
6. Do not restore any other stashed file without independent review.

## Impact assessment

None of the quarantined tests exercises Tihna, `BusinessLane`, the lane
harness, lane doctor, dry-run, routing doctrine, cost gating, approvals, or
publishing. Monday's Tihna cutover is unblocked.

Owner: unassigned
Next review: after Monday Tihna cutover completes
