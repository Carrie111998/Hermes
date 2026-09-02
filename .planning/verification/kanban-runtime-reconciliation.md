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
