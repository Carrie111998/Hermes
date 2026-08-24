"""Smoke test for evals/harness/ — verifies imports, the protocol surface, the
grader primitives, and one end-to-end `run_benchmark` pass against a stub
adapter. No external benchmark is contacted and nothing is written outside a
temporary directory. Catches obvious regressions in the harness framework
before the first real adapter is wired up.

Run: python -m evals.harness.smoke_test
"""

from __future__ import annotations

import sys
import tempfile
from pathlib import Path

# Ensure project root is on path
ROOT = Path(__file__).resolve().parent.parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def test_imports():
    from evals.harness import (
        AgentRun,
        BenchmarkAdapter,
        BenchmarkRun,
        GraderResult,
        Task,
        TaskResult,
        run_benchmark,
    )
    assert Task is not None
    assert AgentRun is not None
    assert GraderResult is not None
    assert TaskResult is not None
    assert BenchmarkRun is not None
    assert callable(run_benchmark)
    print("[OK] Public API imports")


def test_grader_primitives():
    from evals.harness.grader import exact_match, regex_match, trajectory_check, grade

    # exact_match
    r = exact_match("  Hello  ", {"expected": "hello", "task_id": "t1"})
    assert r.score == 1.0, f"exact_match case-insensitive: {r}"
    r = exact_match("hello world", {"expected": "hello", "task_id": "t1"})
    assert r.score == 0.0, f"exact_match mismatch: {r}"
    print("[OK] exact_match")

    # regex_match
    r = regex_match("phone: 555-1234", {"pattern": r"\d{3}-\d{4}", "task_id": "t2"})
    assert r.score == 1.0, f"regex_match hit: {r}"
    r = regex_match("no digits here", {"pattern": r"\d+", "task_id": "t2"})
    assert r.score == 0.0, f"regex_match miss: {r}"
    print("[OK] regex_match")

    # trajectory_check
    traj = [
        {"tool": "web_search"},
        {"tool": "web_extract"},
        {"tool": "terminal"},
    ]
    r = trajectory_check(traj, {
        "must_call": ["web_search"],
        "must_not_call": ["bash_exec"],
        "max_tool_calls": 5,
        "task_id": "t3",
    })
    assert r.score == 1.0, f"trajectory_check all-pass: {r}"
    print("[OK] trajectory_check all-pass")

    r = trajectory_check(traj, {
        "must_call": ["bash_exec"],  # missing
        "task_id": "t4",
    })
    assert r.score == 0.0, f"trajectory_check all-fail: {r}"
    print("[OK] trajectory_check all-fail")

    # dispatch via grade()
    r = grade({"grader": "exact_match", "expected": "x", "task_id": "t5"}, "X")
    assert r.score == 1.0
    print("[OK] grade() dispatcher")


def test_reporter():
    from evals.harness.reporter import init_scoreboard, render_scorecard
    from evals.harness.harness import BenchmarkRun, TaskResult

    # Write into a temp dir, never the package's own results/ directory.
    with tempfile.TemporaryDirectory() as td:
        sb = Path(td) / "scoreboard.md"
        init_scoreboard(sb)
        assert sb.exists(), "init_scoreboard did not create the scoreboard"

    run = BenchmarkRun(
        benchmark="smoke",
        version="v0",
        model="test",
        timestamp="2026-08-23_220000",
        task_results=[
            TaskResult(task_id="t1", score=1.0, elapsed_seconds=1.0,
                       cost_usd=0.001, tokens_used=100, trace_id="abc"),
            TaskResult(task_id="t2", score=0.5, elapsed_seconds=2.0,
                       cost_usd=0.002, tokens_used=200, trace_id="def"),
        ],
    )
    md = render_scorecard(run)
    assert "smoke" in md
    assert "t1" in md
    assert "abc" in md
    print("[OK] render_scorecard")


def test_adapter_registry():
    from evals.harness.adapters import known_adapters, get_adapter

    # No adapters registered yet (stubs don't auto-register)
    adapters = known_adapters()
    print(f"[OK] adapter registry (registered: {adapters})")

    # OSWorld 2.0 stub is importable
    from evals.harness.adapters.osworld2 import OSWorld2Adapter
    adapter = OSWorld2Adapter()
    assert adapter.name == "osworld2"
    assert adapter.task_count == 108
    print(f"[OK] OSWorld2Adapter stub ({adapter.name} {adapter.version})")


def test_run_benchmark_end_to_end():
    """Exercise load → setup → run → grade → report against a stub adapter.

    Guards three regressions that have each happened once already: the reporter
    being defined but never called, a single failing task aborting the whole run,
    and a model id containing '/' breaking the output filenames.
    """
    from evals.harness import run_benchmark
    from evals.harness.harness import AgentRun, GraderResult, Task

    class _StubAdapter:
        name = "smoke"
        version = "v0"
        task_count = 3

        def load_tasks(self, limit=None):
            tasks = [
                Task(id="ok", prompt="p"),
                Task(id="boom", prompt="p"),         # fails BEFORE the agent runs
                Task(id="ungradeable", prompt="p"),  # fails AFTER the agent has spent
            ]
            return tasks[:limit] if limit else tasks

        def setup_environment(self, task):
            if task.id == "boom":
                raise RuntimeError("intentional setup failure")
            return "handle"

        def run_agent(self, task, env_handle, model="default"):
            return AgentRun(task_id=task.id, model=model, elapsed_seconds=1.0,
                            tokens_used=10, cost_usd=0.01, trace_id="t1")

        def grade(self, task, run):
            if task.id == "ungradeable":
                raise RuntimeError("intentional grader failure")
            return GraderResult(task_id=task.id, score=1.0)

    with tempfile.TemporaryDirectory() as td:
        out = Path(td)
        run = run_benchmark(_StubAdapter(), model="mmx/M-7b", output_dir=out)

        assert run.summary["n_tasks"] == 3, run.summary
        assert run.summary["error_count"] == 2, "failing tasks must be recorded, not fatal"
        assert run.scorecard_path.exists(), "JSON scorecard missing"

        # A grader failure must not discard spend the agent already incurred.
        ungradeable = next(t for t in run.task_results if t.task_id == "ungradeable")
        assert ungradeable.cost_usd == 0.01, f"grader failure zeroed real spend: {ungradeable}"
        assert ungradeable.tokens_used == 10, f"grader failure zeroed real tokens: {ungradeable}"
        assert ungradeable.trace_id == "t1", f"grader failure dropped the trace: {ungradeable}"
        assert run.summary["total_cost_usd"] == 0.02, run.summary

        # A setup failure legitimately has no agent run to account for.
        boom = next(t for t in run.task_results if t.task_id == "boom")
        assert boom.cost_usd == 0.0 and boom.trace_id is None, boom

        cards = [p for p in out.iterdir() if p.suffix == ".md" and p.name != "scoreboard.md"]
        assert cards, "write_scorecard was never called"
        assert (out / "scoreboard.md").exists(), "append_to_scoreboard was never called"
    print("[OK] run_benchmark end-to-end (report wired, failures isolated, spend preserved)")


if __name__ == "__main__":
    test_imports()
    test_grader_primitives()
    test_reporter()
    test_adapter_registry()
    test_run_benchmark_end_to_end()
    print("\n=== ALL SMOKE TESTS PASSED ===")
