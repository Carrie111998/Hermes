# Hermes Eval Harness — Generic Benchmark Runner Framework

## Purpose

`evals/harness/` is a generic, reusable evaluation framework for running
external agent benchmarks (OSWorld 2.0, Tau²-Bench, ProgramBench, SABER,
SWE-MeM, etc.) against the Hermes Agent runtime.

It exists because every external benchmark has the same plumbing:
**load tasks → set up environment → run agent → grade output → report score**.
Building that plumbing once per benchmark is wasted work; this harness
encapsulates the shape so each new benchmark only needs a thin adapter.

## Architecture (matches existing evals/ convention)

```
evals/harness/
├── README.md          ← this file
├── runner.py          ← CLI entry point (`python -m evals.harness.runner`)
├── harness.py         ← data model + run_benchmark(): takes a BenchmarkAdapter, runs N tasks
├── grader.py          ← scoring primitives: exact_match, regex_match, llm_judge, trajectory_check
├── reporter.py        ← renders the markdown scorecard + appends scoreboard rows
├── smoke_test.py      ← framework self-check (`python -m evals.harness.smoke_test`)
├── adapters/          ← thin per-benchmark adapters (each implements the BenchmarkAdapter protocol)
│   ├── __init__.py    ← adapter registry
│   └── osworld2.py    ← skeleton only, not implemented
└── results/           ← run output, gitignored except .gitignore and scoreboard.md;
                         one JSON + one MD per run, named
                         <bench>_<model>_<YYYY-MM-DD_HHMMSS>.*
```

## The BenchmarkAdapter protocol

A benchmark adapter is any Python module exposing:

```python
class BenchmarkAdapter:
    name: str                # e.g. "osworld2"
    version: str             # e.g. "v2.0 (2026-06)"
    task_count: int          # total tasks in the benchmark
    
    def load_tasks(self, limit: int | None = None) -> list[Task]:
        """Return list of Task dataclasses with: id, prompt, env_setup, grader."""
        ...
    
    def setup_environment(self, task: Task) -> str:
        """Provision the runtime (docker container, browser session, mock service).
        Returns an environment handle that run_agent() will receive."""
        ...
    
    def run_agent(self, task: Task, env_handle: str, model: str = "default") -> AgentRun:
        """Run Hermes Agent on the task within env_handle. Returns AgentRun with:
        - trajectory (list of tool calls + responses)
        - final_output
        - elapsed_seconds
        - tokens_used
        - cost_usd
        - trace_id (for cross-referencing with observability backend)"""
        ...
    
    def grade(self, task: Task, run: AgentRun) -> GraderResult:
        """Score the run. Returns GraderResult with score (0-1), reason, sub-scores."""
        ...
```

## What lives in `harness.py`

```python
def run_benchmark(adapter: BenchmarkAdapter, *, model: str = "default",
                  limit: int | None = None, parallel: int = 1,
                  output_dir: Path | None = None) -> BenchmarkRun:
    """End-to-end: load → setup → run → grade → report."""
    ...
```

Key behaviors:
- **Non-clobbering re-runs:** each run is written under a fresh timestamp, so a
  re-run never overwrites a previous scorecard
- **Fault-isolated tasks:** a task that raises is recorded with score 0.0 and its
  exception text; the remaining tasks still run and still get reported
- **Trace correlation:** every AgentRun carries the OTel `trace_id` so results
  can be joined with the observability backend (see future OTel instrumentation)
- **Cost accounting:** `cost_usd` and `tokens_used` are whatever the adapter
  reports on each `AgentRun` and are summed into the run summary. There is no
  pricing table in the harness — an adapter that does not populate these fields
  reports 0.0.

Not yet implemented (accepted but inert): `--parallel` — tasks run sequentially.
Resuming a partially-completed run is also not supported; an interrupted run
restarts from the first task. Run outputs are keyed to second resolution, so two
runs of the same benchmark/model started within the same second overwrite each
other.

## What lives in `grader.py`

Every primitive takes `(output, config)` and returns a `GraderResult`.
`grade(config, output, trajectory=None)` dispatches on `config["grader"]` — pass `trajectory`
whenever the grader is `trajectory_check`, or it scores against an empty trajectory and
silently returns 0.0.

Implemented:
1. `exact_match(output, config)` — string equality, post-trim, case-insensitive
2. `regex_match(output, config)` — output matches `config["pattern"]`
3. `trajectory_check(trajectory, config)` — assert trajectory satisfies invariants
   (`must_call`, `must_not_call`, `max_tool_calls`)

Not yet implemented (registered in the dispatch table, raises `NotImplementedError`
if selected):
4. `llm_judge(output, config)` — model-as-judge scoring against a rubric; needs the
   judge-model integration wired up first

Each benchmark adapter picks the grader(s) it needs per task.

## What lives in `reporter.py`

Given a `BenchmarkRun`:
- `write_scorecard()` renders a per-run markdown summary at `results/<bench>_<model>_<ts>.md`
- `append_to_scoreboard()` appends a row to `results/scoreboard.md` with: date,
  bench, model, score, n, p50/p90 latency, cost — idempotent, so replaying the
  same run does not duplicate the row

`run_benchmark()` writes the machine-readable JSON itself and calls both of the
above; `runner.py` prints the console summary. A model id containing `/` (e.g.
`mmx/M-7b`) is slugified for filenames via `safe_model_slug()`.

## Adapter implementations (lives in `adapters/`)

Currently **0** adapters implemented. First planned:
- `adapters/osworld2.py` — OSWorld 2.0 (108 long-horizon computer-use tasks)
- `adapters/tau2.py` — Tau²-Bench (customer-service multi-turn)
- `adapters/programbench.py` — ProgramBench (architecture from binary)

Adding an adapter = writing one Python module implementing the protocol above.

## How to run

```bash
# Run just OSWorld 2.0 with 5 tasks
python -m evals.harness.runner --benchmark osworld2 --limit 5

# Run against a specific model
python -m evals.harness.runner --benchmark osworld2 --model mmx/M-7b --limit 10

# Verify the framework itself without running a benchmark
python -m evals.harness.smoke_test
```

`--benchmark` is required and takes one adapter at a time. Since zero adapters
are registered today, every `--benchmark` value currently exits with
`No adapter registered for ... (registered: none)` — that is the expected
behavior until the first adapter calls `register()`.

## Status (2026-08-23)

- ✅ Harness framework scaffolded
- ✅ Protocol + grader primitives + reporter defined
- ⏳ Zero adapters implemented yet
- ⏳ Zero real runs

The first real adapter (OSWorld 2.0) needs:
- Docker VM provisioning for computer-use tasks
- Screenshot capture + diff grader
- 108 task definitions (or subset with limit=10 for baseline)
- Per-task environment setup script
- Estimated work: 2-4 hours for a baseline of 10 tasks

## See also

- `evals/browser_use/` — closest existing precedent (component eval, not external benchmark)
- `evals/compaction/` — has `SCORECARD-2026-08-15.md` precedent for the scorecard format
- The 2026-08-23 reliability-benchmark gap analysis that motivated this scaffold
  (OSWorld 2.0 / Tau²-Bench / ProgramBench vs. the current Hermes eval surface)
