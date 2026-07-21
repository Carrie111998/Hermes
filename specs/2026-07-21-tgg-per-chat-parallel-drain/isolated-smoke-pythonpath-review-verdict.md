# TGG isolated-smoke PYTHONPATH fix — cross-provider review verdict

Reviewed: `def336b60f8303c11056ae6707b83e85abfc60c2` ("fix(tgg): verify isolated smoke with app import path") against its parent `b6ef21641d3e4a14039f2b5d6fb08d2744a492d1` (repo: hermes-pcl). 1 file, +2/-1: `deploy/tgg/christopher/scripts/verify_runtime.sh`.

Context: the deployed runtime on `b6ef21641` is healthy, but full deploy verification (`verify_runtime.sh`, non-`--quick` mode) failed because it invokes `run_isolated_smoke.py` by absolute path (`"$APP_ROOT/.venv/bin/python" "$DEPLOY_ROOT/scripts/run_isolated_smoke.py"`) and the script could not import `gateway`. This commit prefixes only that invocation with `PYTHONPATH="$APP_ROOT${PYTHONPATH:+:$PYTHONPATH}"`.

## Verdict: CLEAR

### 1. Fix resolves the exact import failure — CLEAR
`run_isolated_smoke.py` has no top-level `gateway` import; it late-binds `from gateway import durable_jsonl_consumer as consumer` (line 219) inside `main()`, reached only after slot/home setup — which is why `--help` alone (parses before that line executes) does not surface the crash either way. Absolute-path invocation sets `sys.path[0]` to the script's own directory (`$DEPLOY_ROOT/scripts/`), not `$APP_ROOT`, so `gateway` (a real subpackage at `$APP_ROOT/gateway/__init__.py`) is not importable without help. Reproduced directly: a minimal `from gateway import durable_jsonl_consumer as consumer` script invoked by absolute path from a `cwd` outside `$APP_ROOT` raises `ModuleNotFoundError: No module named 'gateway'` with `PYTHONPATH` unset, and imports cleanly (`<module 'gateway.durable_jsonl_consumer' from '$APP_ROOT/gateway/durable_jsonl_consumer.py'>`) with `PYTHONPATH=$APP_ROOT` — matching this diff's mechanism exactly.

### 2. Shell semantics — both unset and pre-set PYTHONPATH cases correct — CLEAR
`PYTHONPATH="$APP_ROOT${PYTHONPATH:+:$PYTHONPATH}"` is the standard prepend-if-set idiom. `$PYTHONPATH` here resolves against the shell's inherited environment (the script never sets/exports `PYTHONPATH` earlier — confirmed via grep, only reference is this line). Verified both branches by direct evaluation: unset → expands to `$APP_ROOT` alone; pre-set to `/tmp/other` → expands to `$APP_ROOT:/tmp/other` (correct prepend, existing path preserved). The value is scoped via `env` as a one-shot prefix to the single `runuser -u pclaw -- env HERMES_HOME=... PYTHONPATH=... "$APP_ROOT/.venv/bin/python" ...` invocation — it does not leak into the script's own shell environment or any other command in `verify_runtime.sh`.

### 3. bash syntax, manifest, and invocation checks pass — CLEAR
`bash -n deploy/tgg/christopher/scripts/verify_runtime.sh` → OK. `pa-agent.hermes.manifest.json` parses as valid JSON (untouched by this diff, confirming no drift). Local invocation: `PYTHONPATH="$PWD" .venv/bin/python deploy/tgg/christopher/scripts/run_isolated_smoke.py --help` (aliased via `python3` locally, no venv difference relevant to import resolution) exits 0 and prints the full argument list. Note: `--help` passing is not on its own proof the gateway import path is fixed (argparse exits before reaching the deferred import in both the broken and fixed cases) — the decisive evidence is the isolated `gateway` import reproduction in § 1, run separately with and without `PYTHONPATH` from a `cwd` outside `$APP_ROOT`.

### 4. Diff scope and safety — CLEAR
`git diff b6ef21641 def336b60 --stat` touches exactly one file, `deploy/tgg/christopher/scripts/verify_runtime.sh`, +2/-1: only the `PYTHONPATH=` line insertion and its trailing continuation reflow. No other argument (`--app-root`, `--live-home`, `--test-root`, `--slot-file`, `--report`), no `runuser`/`HERMES_HOME` semantics, no demo-pause or processing-gate logic, and no secret handling changed. No live-host access was made or required for this review; all checks ran locally against the repo working tree.

No correctness issues found in the reviewed diff.
