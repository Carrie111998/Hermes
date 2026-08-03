# Task 1 correction report

## Scope

Commit 41f600df3 had two blocking review findings. This follow-up changes only:

- `hermes_cli/kanban_db.py`
- `tests/hermes_cli/test_kanban_db.py`
- `task-1-report.md`

No live Kanban state was mutated, no service was restarted, and no remote operation was performed.

## Finding 1: durable integration shortcut

### RED

Added `test_explicit_integration_verification_is_not_skipped_by_durable_state` first, then ran:

```text
scripts/run_tests.sh tests/hermes_cli/test_kanban_db.py -k 'explicit_integration_verification_is_not_skipped_by_durable_state or reconcile_keeps_prior_member_state_when_sibling_advances_tip' -v
```

The new regression failed on 41f600df3 as expected:

```text
assert len(verify_calls) == 1
E assert 0 == 1
```

After adding the distinct-source assertion, the same test also demonstrated the second part of the defect on 41f600df3:

```text
assert 'already_integrated' == 'verify_failed'
```

### GREEN

The implementation now uses the durable-state shortcut only for the ordinary reconcile fast path: no explicit `expected_source_sha`, `candidate_verify_fn`, or `before_apply_fn`. Explicit source identity, candidate verification, and ownership checks proceed through their existing verified path.

The focused command then passed:

```text
scripts/run_tests.sh tests/hermes_cli/test_kanban_db.py -k 'explicit_integration_verification_is_not_skipped_by_durable_state or reconcile_keeps_prior_member_state_when_sibling_advances_tip' -v
```

Result: 2 passed, 0 failed.

The regression proves that:

- a different expected source SHA is not suppressed by an older integration row;
- candidate verification still runs after a prior integration;
- ownership verification still runs after a prior integration.

## Finding 2: sibling reconciliation regression

`test_reconcile_keeps_prior_member_state_when_sibling_advances_tip` now runs reconciliation a second time after the sibling advances the epic tip. It proves:

- the second reconciliation integrates nothing;
- the earlier member's integration-event count is unchanged;
- the sibling's integration-event count is unchanged;
- the second pass records zero Git calls.

The focused command above passed this strengthened regression.

## Canonical verification

```text
scripts/run_tests.sh tests/hermes_cli/test_kanban_db.py
```

Result: 451 passed, 0 failed.

```text
git diff --check
```

Result: clean.

Terminal-epic handling and the existing PR #75 pin behavior remain covered by the canonical Kanban test file and passed unchanged.
