# Kanban runtime reconciliation verification

Canonical design: `docs/rfcs/2026-09-kanban-runtime-reconciliation.md` at signed commit `b6431e71fae69815f71723244ff6dc622d482a05`.

## Task 1 - pinned worker PID registration

### Initial RED

Command:

    HERMES_PYTHON=/home/houminxi/code/hermes/hermes-agent/venv/bin/python scripts/run_tests.sh tests/hermes_cli/test_kanban_worker_registration.py -q

Result: exit 1, six failures. Each failed with `AttributeError: module 'hermes_cli.kanban_db' has no attribute 'register_worker_pid'`. This was the expected missing-interface failure before production implementation.

### GREEN

Command:

    HERMES_PYTHON=/home/houminxi/code/hermes/hermes-agent/venv/bin/python scripts/run_tests.sh tests/hermes_cli/test_kanban_worker_registration.py tests/hermes_cli/test_kanban_reclaim_claim_lock_guard.py tests/hermes_cli/test_kanban_worker_lifecycle_hooks.py tests/hermes_cli/test_kanban_parent_reopen_invalidation.py -q

Result: exit 0, `17 tests passed, 0 failed`.

### Claim-lock defect injection

Production mutation: temporarily removed both task and run claim-lock comparisons from `register_worker_pid` in `hermes_cli/kanban_db.py` while retaining the real run-ID checks and SQL writes.

Command:

    HERMES_PYTHON=/home/houminxi/code/hermes/hermes-agent/venv/bin/python scripts/run_tests.sh tests/hermes_cli/test_kanban_worker_registration.py -k test_register_worker_pid_rejects_wrong_claim_lock -q

Injected result: exit 1, `1 failed, 5 deselected`. The stale claim reached the write path; both pinned updates matched zero rows and the test observed `RuntimeError: worker PID registration lost its identity pin`. This proves the test rejects removal of the explicit pre-write claim identity checks.

Restored result: exit 0, `1 tests passed, 0 failed` using the identical command.

Source under test: `hermes_cli/kanban_db.py::register_worker_pid` and both `dispatch_once` spawn lanes. External stubs: dispatcher spawn functions return controlled PIDs in existing lifecycle tests; SQLite, claim creation, run creation, transactions, rows, and event persistence are real.

## Task 2 - controlled worker self-registration

### Initial RED

The prior run added worker bridge tests before production code. The first run of `tests/hermes_cli/test_kanban_worker_registration.py` failed because `tools.kanban_tools.register_current_worker_from_env` did not exist. The exact initial output did not survive the reclaimed worker process; this limitation is recorded rather than reconstructed.

### Startup-call defect injection

Production mutation: temporarily removed the `register_current_worker_from_env(source="worker_start")` block from the quiet single-query path in `cli.py`.

Command:

    HERMES_PYTHON=/home/houminxi/code/hermes/hermes-agent/venv/bin/python scripts/run_tests.sh tests/cli/test_single_query_session_finalize.py -k test_quiet_kanban_worker_registers_pid_before_credentials -q

Injected result: exit 1. The test failed with `StopIteration` because no `(kanban_register, worker_start)` call was observed. Restored result: exit 0, `1 tests passed, 0 failed` using the identical command.

### Real subprocess correction and GREEN

The first real-subprocess run exposed a test-harness defect: mutating `HERMES_HOME` inside the child occurs after collection-time imports may cache profile paths, so the child opened a different database and returned `None`. The corrected test passes the dispatcher's real `HERMES_KANBAN_DB` pin in the child environment before process start, matching `_spawn_worker` production behavior and avoiding a generated scratch script.

Command:

    HERMES_PYTHON=/home/houminxi/code/hermes/hermes-agent/venv/bin/python scripts/run_tests.sh tests/hermes_cli/test_kanban_worker_registration.py tests/cli/test_single_query_session_finalize.py tests/cron/test_cron_kanban_env_isolation.py -q

Result: exit 0, `36 tests passed, 0 failed`. The subprocess test launches the real interpreter, uses a real SQLite file and run/claim rows, prints its real PID, and verifies both task and run rows contain that PID. No process, PID, SQLite API, or registration function is mocked. The only patched boundaries are the quiet CLI integration test's fake credential/model lifecycle and the heartbeat timing clock.

### Static gates

Command:

    ruff check cli.py tools/kanban_tools.py tests/hermes_cli/test_kanban_worker_registration.py tests/cli/test_single_query_session_finalize.py
    git diff --check
    /home/houminxi/code/hermes/hermes-agent/venv/bin/python -m py_compile cli.py tools/kanban_tools.py tests/hermes_cli/test_kanban_worker_registration.py tests/cli/test_single_query_session_finalize.py

Result: exit 0, `All checks passed!`; `git diff --check` and `py_compile` were silent.

### Pre-commit review disposition

Code Forge job `5db9ede2-1152-4f64-b8f6-667fb3a2c15e` reviewed the staged five-file Task 2 diff with the `mimo-direct` backend. It reported one confirmed code finding: the quiet CLI guard checked only `HERMES_KANBAN_TASK` even though registration requires all four identity pins. The implementation now checks DB, task, run, and claim before importing the bridge. A runtime advisory also identified an unbounded subprocess wait; the real-process test now uses a 15-second timeout. Code Forge separately reported coverage/semgrep infrastructure limitations; those are tool-environment findings, not code dispositions.

### Fresh re-verification (this run, pre-commit)

Command:

    HERMES_PYTHON=/home/houminxi/code/hermes/hermes-agent/venv/bin/python scripts/run_tests.sh tests/hermes_cli/test_kanban_worker_registration.py tests/cli/test_single_query_session_finalize.py tests/cron/test_cron_kanban_env_isolation.py -q

Result: exit 0, `3 files, 36 tests passed, 0 failed (100% complete) in 2.1s`.

Static gates on the same five-file staged tree:

    ruff check cli.py tools/kanban_tools.py tests/hermes_cli/test_kanban_worker_registration.py tests/cli/test_single_query_session_finalize.py
    git diff --cached --check
    /home/houminxi/code/hermes/hermes-agent/venv/bin/python -m py_compile cli.py tools/kanban_tools.py tests/hermes_cli/test_kanban_worker_registration.py tests/cli/test_single_query_session_finalize.py

Result: ruff `All checks passed!`; `py_compile` and `git diff --cached --check` silent; no non-ASCII added lines in the staged Python/test/docs hunks.

## Task 3 - running worker identity diagnostics

Source under test: `hermes_cli/kanban_diagnostics.py::_rule_running_worker_identity`. Thresholds stay as internal constants `RUNNING_PID_GRACE_SECONDS = 30` and `RUNNING_FIRST_HEARTBEAT_GRACE_SECONDS = 120`. The `cfg` argument remains unused on purpose; no new config keys.

Contract: one root-cause diagnostic per state.

- Critical `running_worker_run_mismatch` when the current run is missing/ended, claim locks differ, or task/run PIDs differ, including one-sided emptiness. Pid-missing and heartbeat stay silent on a broken identity.
- Error `running_worker_pid_missing` only when both rows exist, both PID fields are empty, and age is past 30s. Detail/data name both unbound layers; they do not claim a populated layer is missing.
- Warning `running_worker_heartbeat_missing` only when task/run/claim/PID identity already matches.

The claim-time race finding is not a code defect: `claim_task()` writes the run row and `current_run_id` in one `write_txn`.

### Initial RED

Command:

    HERMES_PYTHON=/home/houminxi/code/hermes/hermes-agent/venv/bin/python scripts/run_tests.sh tests/hermes_cli/test_kanban_diagnostics.py tests/hermes_cli/test_kanban_review_surfaces.py -q

Result: exit 1, `2 files, 23 tests passed, 7 failed`. One-sided emptiness did not emit mismatch (`StopIteration` / `len == 0`); pid-missing data lacked `missing_layers`.

### GREEN then architect correction

First GREEN compared PIDs with `task_worker_pid != run_pid` but still stacked pid-missing/heartbeat on top of mismatch. Architect review of confirmed Forge finding `3271f746bbb7945c` required one diagnostic per state, not documented overlap.

After rewriting tests to demand a single kind, RED against the stacked implementation was exit 1, `17 passed, 4 failed` (`test_running_one_sided_*_past_grace_is_mismatch_only` and the two heartbeat-suppression tests). Production now returns after mismatch, emits pid-missing only for both-empty after 30s, and emits heartbeat only on a consistent bound identity.

Command:

    HERMES_PYTHON=/home/houminxi/code/hermes/hermes-agent/venv/bin/python scripts/run_tests.sh tests/hermes_cli/test_kanban_diagnostics.py tests/hermes_cli/test_kanban_review_surfaces.py -q

Result: exit 0, `2 files, 32 tests passed, 0 failed (100% complete) in 2.4s`.

### One-sided PID mismatch defect injection

Production mutation: restored the old both-nonempty compare:

    or (task_worker_pid is not None and run_pid is not None and task_worker_pid != run_pid)

Command:

    HERMES_PYTHON=/home/houminxi/code/hermes/hermes-agent/venv/bin/python scripts/run_tests.sh tests/hermes_cli/test_kanban_diagnostics.py tests/hermes_cli/test_kanban_review_surfaces.py -k 'test_running_run_mismatch_task_pid_without_run_pid or test_running_run_mismatch_run_pid_without_task_pid or test_running_one_sided_task_pid_past_grace_is_mismatch_only or test_running_one_sided_run_pid_past_grace_is_mismatch_only or test_cli_and_dashboard_receive_one_sided_run_pid_mismatch' -q

Injected result: exit 1, `0 tests passed, 5 failed`. One-sided cases emitted no identity diagnostic (`kinds == []` / `len(cli_diags) == 0`). Restored result: exit 0, `2 files, 32 tests passed, 0 failed`.

### Static gates

Command:

    ruff check hermes_cli/kanban_diagnostics.py tests/hermes_cli/test_kanban_diagnostics.py tests/hermes_cli/test_kanban_review_surfaces.py
    /home/houminxi/code/hermes/hermes-agent/venv/bin/python -m py_compile hermes_cli/kanban_diagnostics.py tests/hermes_cli/test_kanban_diagnostics.py tests/hermes_cli/test_kanban_review_surfaces.py
    git diff --check

Result: ruff `All checks passed!`; `py_compile` silent; `git diff --check` silent; no non-ASCII added lines. Python identifiers, diagnostic kinds, and JSON keys remain ASCII.

### Code Forge cycle 1 (job `b9ee4ca5-1570-4eb6-97a6-06a717d06ccc`)

CI mode, backend `mimo-direct`, 3 passes, wall 650s. Verdict FAIL: 1 confirmed, 2 uncertain, 1 dismissed.

- Confirmed `3271f746bbb7945c`: one-sided PID emptiness past 30s stacked mismatch and pid-missing. Architect required suppressing the lower-severity signals; the production early-return implements that. Clean count resets.
- Uncertain `8ad61e349be136db` / `97f8502669dbeaf0`: same cascade class; heartbeat is now suppressed unless identity already matches.
- Dismissed E2E_CHECK and runtime/coverage/semgrep notes are tool-environment advisories, not code findings.

Cycle 2 job `7c2e00d0-5deb-43f4-b196-74f54cda0a63` was stopped after the architect correction landed mid-review.

### Fresh re-verification (continuation after crash)

The prior coder run crashed (`pid 734261 not alive`) after staging the single-diagnostic identity rule. This continuation kept that staged tree, collapsed leftover blank lines in `tests/hermes_cli/test_kanban_diagnostics.py`, and re-ran the required gates on the live worktree.

Command:

    HERMES_PYTHON=/home/houminxi/code/hermes/hermes-agent/venv/bin/python scripts/run_tests.sh tests/hermes_cli/test_kanban_diagnostics.py tests/hermes_cli/test_kanban_review_surfaces.py -q

Result: exit 0, `2 files, 32 tests passed, 0 failed (100% complete) in 1.9s`.

One-sided PID mismatch defect injection (same production mutation as above: restore the both-nonempty compare) was re-run with `PYTHONDONTWRITEBYTECODE=1` after clearing `hermes_cli` bytecode for `kanban_diagnostics`. Injected result: exit 1, `0 tests passed, 5 failed`. One-sided cases emitted no identity diagnostic (`kinds == []` / `len(cli_diags) == 0`). Restored result: exit 0, `2 files, 32 tests passed, 0 failed`.

Static gates on the four-file Task 3 tree:

    ruff check hermes_cli/kanban_diagnostics.py tests/hermes_cli/test_kanban_diagnostics.py tests/hermes_cli/test_kanban_review_surfaces.py
    /home/houminxi/code/hermes/hermes-agent/venv/bin/python -m py_compile hermes_cli/kanban_diagnostics.py tests/hermes_cli/test_kanban_diagnostics.py tests/hermes_cli/test_kanban_review_surfaces.py
    git diff --check

Result: ruff `All checks passed!`; `py_compile` silent; `git diff --check` silent; no non-ASCII added lines. Python identifiers, diagnostic kinds, and JSON keys remain ASCII.

Operator direction for this coder run: do not restart long multi-round Forge loops. Cycle 1 confirmed code finding `3271f746bbb7945c` (stacked mismatch + pid-missing) is addressed by the early-return single-diagnostic rule; remaining clean rounds belong to the later independent Reviewer card, not this commit.
