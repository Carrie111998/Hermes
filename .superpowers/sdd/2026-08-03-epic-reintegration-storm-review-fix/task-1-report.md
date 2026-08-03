# Task 1 report: preserve reworked-story integration

Branch: `fix/epic-reintegration-storm`
Base preserved: `4117ba7fd`

## Correction

The ordinary reconciliation fast path now treats a durable integration row as
current only when `epic_story_integrations.integrated_at >= tasks.completed_at`
for the story. Explicit source/candidate/ownership verification paths remain
outside this fast path. The existing no-Git unchanged path is preserved.

The regression test uses a real temporary Git repository: it integrates a
story, adds `rework.txt` to the same story branch, records a later completion,
then verifies one reconciliation integrates the rework and a subsequent
unchanged reconciliation performs no Git work and emits no duplicate event.

## TDD evidence

RED command:

```text
scripts/run_tests.sh tests/hermes_cli/test_kanban_db.py -k test_reconcile_reintegrates_story_after_later_completion -v
```

Expected/current failure before the production correction:

```text
FAILED ...::test_reconcile_reintegrates_story_after_later_completion
AssertionError: assert [] == ['t_ab93d345']
```

The exact task id is generated per isolated run; the failure was that
reconciliation returned no integrated story against the uncorrected
story-identity-only fast path.

GREEN focused command:

```text
scripts/run_tests.sh tests/hermes_cli/test_kanban_db.py -k test_reconcile_reintegrates_story_after_later_completion -v
```

Result: 1 test passed, 0 failed.

GREEN regression subset after correcting the shared completed-task fixture:

```text
scripts/run_tests.sh tests/hermes_cli/test_kanban_db.py -k 'integrate_story_to_epic_idempotent_second_call_is_noop or reconcile_ten_times_does_not_reprocess_integrated_story or reconcile_keeps_prior_member_state_when_sibling_advances_tip or integration_state_survives_member_event_gc_and_restart or new_active_epic_member_integrates_once_via_reconcile or reconcile_reintegrates_story_after_later_completion'
```

Result: 6 tests passed, 0 failed.

GREEN relevant Kanban suites:

```text
scripts/run_tests.sh tests/hermes_cli/test_kanban_db.py tests/hermes_cli/test_kanban_epics.py tests/hermes_cli/test_kanban_epic_branch_materialization.py tests/hermes_cli/test_kanban_release_evidence.py tests/hermes_cli/test_kanban_release_cli.py
```

Result: 535 tests passed, 0 failed.

## Final evidence

```text
git diff --check
```

Result: clean; no output.

Commit:

```text
2d6fc2e618803f8a2143f6d28ce23415fc7a082b
```

Changed files in the correction commit:

- `hermes_cli/kanban_db.py`
- `tests/hermes_cli/test_kanban_db.py`
- The correction commit changes exactly `hermes_cli/kanban_db.py` and
  `tests/hermes_cli/test_kanban_db.py`.

The report is committed separately because embedding a report containing a
commit's own SHA would make that SHA self-invalidating.
