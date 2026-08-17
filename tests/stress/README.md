# Stress / battle-test suite

Long-running tests that exercise the Kanban kernel under adversarial
conditions. **Not run by `scripts/run_tests.sh`** because they can
take 30+ seconds each and spawn real subprocesses.

## Running them

Two supported paths, and they run the same code — the pytest lane shells
out to the scripts.

**Directly** — what you want while iterating on one script:

```bash
./venv/bin/python tests/stress/test_concurrency.py
```

Every script exits 0 iff all of its checks passed, and prints a summary.

**Through pytest**, for gating:

```bash
pytest tests/stress -m stress
```

`tests/stress/test_stress_entrypoints.py` is the only module pytest
collects here; it runs each script as a subprocess and asserts the exit
code. The scripts themselves stay ignored (see `conftest.py`) because
none of them defines a module-level `test_*` function — collecting them
would import eight modules and find zero tests.

The `stress` marker is **deselected by default** via `addopts` in
`pyproject.toml`, so a normal `pytest` run never pays for them. The full
set takes roughly 25 minutes, dominated by `test_property_fuzzing`
(~530s) and `test_benchmarks` (~500s). Narrow it with `-k`:

```bash
pytest tests/stress -m stress -k property_fuzzing
```

Deadlines in these scripts are safety nets scaled by
`HERMES_TEST_TIMEOUT_SCALE` (see `tests/timeout_budget.py`); raise it on a
loaded host rather than editing the numbers.

### Platform notes

Some checks cannot run everywhere and skip cleanly rather than fail:

- `hermes_home_via_symlink` needs symlink creation, which Windows grants
  only to elevated processes or with Developer Mode on (`WinError 1314`).
- `test_subprocess_e2e` scenario B (crashed worker) is POSIX-only:
  `subprocess` `pass_fds` is rejected on Windows, there is no `sleep`
  binary, and the double-fork orphaning it relies on has no Windows
  equivalent.

## What's covered

- **test_concurrency.py** — 5 workers, 100 tasks, race-for-claim. Asserts
  no double-claims, no orphan runs, no SQLite errors escape retry.
- **test_concurrency_mixed.py** — 10 workers + 1 reclaimer, 500 tasks,
  random ops (claim/complete/block/unblock/archive). Same invariants
  under adversarial scheduling.
- **test_concurrency_reclaim_race.py** — TTL < work duration so the
  reclaimer intentionally yanks tasks mid-work; verifies the worker's
  late-complete is refused cleanly (CAS guard works).
- **test_subprocess_e2e.py** — dispatcher spawns real Python subprocess
  workers that heartbeat + complete via the CLI; crash detection
  against a real dead PID.
- **test_property_fuzzing.py** — 500 random operation sequences,
  ~40k operations total, 9 invariant checks after each step.
- **test_atypical_scenarios.py** — 28 scenarios covering atypical
  user inputs: unicode/emoji/RTL, 1 MB strings, SQL injection
  attempts, cycles, self-parents, wide fan-in/out, clock skew,
  HERMES_HOME with spaces/unicode/symlinks, 1000 runs on one
  task, idempotency-key race across processes, terminal-state
  resurrection attempts, dashboard REST with weird JSON.
- **test_benchmarks.py** — latency at 100/1k/10k tasks for dispatch,
  recompute_ready, list_tasks, build_worker_context, etc. Results saved
  to JSON for regression diffing.
