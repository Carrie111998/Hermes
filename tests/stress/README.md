# Stress / battle-test suite

Long-running scripts that exercise the Kanban kernel under adversarial
conditions. They are **not run by `scripts/run_tests.sh`** because they can
take 30+ seconds each and spawn real subprocesses.

These files are standalone `__main__` programs, not pytest test modules.
Running `pytest tests/stress/` intentionally collects zero tests. Run the
scripts directly, or use `.github/workflows/stress-nightly.yml`.

Run manually from the repository root:

```bash
source .venv/bin/activate
python tests/stress/test_concurrency.py
python tests/stress/test_subprocess_e2e.py
python tests/stress/test_property_fuzzing.py
python tests/stress/test_benchmarks.py
```

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
