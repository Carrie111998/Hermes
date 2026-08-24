"""Generic Hermes eval harness — runs an external benchmark adapter against the
Hermes Agent runtime and produces a scorecard.

Usage:
    from evals.harness import run_benchmark
    from evals.harness.adapters.osworld2 import OSWorld2Adapter

    adapter = OSWorld2Adapter()
    run = run_benchmark(adapter, model="mmx/M-7b", limit=10)
    print(run.scorecard_path)

The command-line surface lives in `runner.py`.
"""

from __future__ import annotations

import json
import re
import time
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import Any, Protocol


# ---------- Public data shapes ----------------------------------------------

@dataclass
class Task:
    id: str
    prompt: str
    env_setup: dict[str, Any] = field(default_factory=dict)
    grader_config: dict[str, Any] = field(default_factory=dict)
    expected_output: Any = None
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass
class AgentRun:
    task_id: str
    model: str
    trajectory: list[dict[str, Any]] = field(default_factory=list)
    final_output: Any = None
    elapsed_seconds: float = 0.0
    tokens_used: int = 0
    cost_usd: float = 0.0
    trace_id: str | None = None
    error: str | None = None


@dataclass
class GraderResult:
    task_id: str
    score: float  # 0.0 - 1.0
    reason: str = ""
    sub_scores: dict[str, float] = field(default_factory=dict)


@dataclass
class TaskResult:
    task_id: str
    score: float
    elapsed_seconds: float
    cost_usd: float
    tokens_used: int
    trace_id: str | None
    error: str | None = None


@dataclass
class BenchmarkRun:
    benchmark: str
    version: str
    model: str
    timestamp: str
    task_results: list[TaskResult] = field(default_factory=list)
    summary: dict[str, Any] = field(default_factory=dict)
    scorecard_path: Path | None = None

    @property
    def mean_score(self) -> float:
        if not self.task_results:
            return 0.0
        return sum(t.score for t in self.task_results) / len(self.task_results)


# ---------- Adapter protocol -----------------------------------------------

class BenchmarkAdapter(Protocol):
    name: str
    version: str
    task_count: int

    def load_tasks(self, limit: int | None = None) -> list[Task]: ...
    def setup_environment(self, task: Task) -> str: ...
    def run_agent(self, task: Task, env_handle: str, model: str = "default") -> AgentRun: ...
    def grade(self, task: Task, run: AgentRun) -> GraderResult: ...


# ---------- Runner ----------------------------------------------------------

def safe_model_slug(model: str) -> str:
    """Make a model id safe to embed in a filename.

    Model ids routinely carry a provider prefix (``mmx/M-7b``); interpolating
    one straight into a path silently creates a nested directory that was never
    created, and the write fails *after* the whole benchmark has been run.
    """
    return re.sub(r"[^\w.-]", "-", model)


def run_benchmark(
    adapter: BenchmarkAdapter,
    *,
    model: str = "default",
    limit: int | None = None,
    parallel: int = 1,
    output_dir: Path | None = None,
) -> BenchmarkRun:
    """End-to-end: load → setup → run → grade → report.

    Each run is written under its start timestamp, at second resolution — two
    runs of the same benchmark and model started within the same second
    overwrite each other. Resuming a partially-completed run is NOT yet
    supported; an interrupted run restarts from the first task.

    ``parallel`` is accepted for forward compatibility but not yet honoured;
    tasks are executed sequentially.

    A task that raises is recorded with score 0.0 and its exception text, and
    the run continues — one bad task does not discard the whole benchmark.
    """
    if output_dir is None:
        output_dir = Path(__file__).parent / "results"
    output_dir.mkdir(parents=True, exist_ok=True)

    tasks = adapter.load_tasks(limit=limit)
    ts = time.strftime("%Y-%m-%d_%H%M%S")
    run = BenchmarkRun(
        benchmark=adapter.name,
        version=adapter.version,
        model=model,
        timestamp=ts,
    )

    for task in tasks:
        started = time.monotonic()
        agent_run: AgentRun | None = None
        try:
            env_handle = adapter.setup_environment(task)
            agent_run = adapter.run_agent(task, env_handle, model=model)
            grader = adapter.grade(task, agent_run)
        except Exception as exc:
            # A single failing task is recorded, not fatal: aborting here would
            # throw away every task already run and graded.
            #
            # Preserve whatever the agent actually spent before the failure. If
            # run_agent() completed and only grade() raised, the tokens, cost and
            # trace are real and must still be accounted for -- zeroing them here
            # would silently under-report paid work in the run summary.
            run.task_results.append(TaskResult(
                task_id=task.id,
                score=0.0,
                elapsed_seconds=(
                    agent_run.elapsed_seconds if agent_run else time.monotonic() - started
                ),
                cost_usd=agent_run.cost_usd if agent_run else 0.0,
                tokens_used=agent_run.tokens_used if agent_run else 0,
                trace_id=agent_run.trace_id if agent_run else None,
                error=f"{type(exc).__name__}: {exc}",
            ))
            continue
        run.task_results.append(TaskResult(
            task_id=task.id,
            score=grader.score,
            elapsed_seconds=agent_run.elapsed_seconds,
            cost_usd=agent_run.cost_usd,
            tokens_used=agent_run.tokens_used,
            trace_id=agent_run.trace_id,
            error=agent_run.error,
        ))

    # Summary stats
    n = len(run.task_results)
    run.summary = {
        "n_tasks": n,
        "mean_score": run.mean_score,
        "p50_latency_s": _percentile([t.elapsed_seconds for t in run.task_results], 50),
        "p90_latency_s": _percentile([t.elapsed_seconds for t in run.task_results], 90),
        "total_cost_usd": sum(t.cost_usd for t in run.task_results),
        "total_tokens": sum(t.tokens_used for t in run.task_results),
        "error_count": sum(1 for t in run.task_results if t.error),
    }

    # Persist. scorecard_path is assigned before rendering so the markdown
    # scorecard can cite the JSON path.
    scorecard_json = output_dir / f"{adapter.name}_{safe_model_slug(model)}_{ts}.json"
    scorecard_json.write_text(
        json.dumps(asdict(run), indent=2, default=str), encoding="utf-8", newline="\n"
    )
    run.scorecard_path = scorecard_json

    # Report. Imported here rather than at module scope because reporter.py
    # imports from this module.
    from .reporter import append_to_scoreboard, write_scorecard

    write_scorecard(run, output_dir)
    append_to_scoreboard(run, output_dir / "scoreboard.md")

    return run


def _percentile(values: list[float], p: int) -> float:
    if not values:
        return 0.0
    s = sorted(values)
    idx = int(len(s) * p / 100)
    return s[min(idx, len(s) - 1)]
