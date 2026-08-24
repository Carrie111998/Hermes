"""CLI entry point for the Hermes eval harness.

Mirrors the `runner.py` convention of the sibling suites
(`evals/compaction/runner.py`, `evals/readtool/runner.py`): the data model and
the `run_benchmark` library live in `harness.py`, and this module is only the
command-line surface over them.

Usage:
    python -m evals.harness.runner --benchmark osworld2 --limit 5
"""

from __future__ import annotations

import argparse
from pathlib import Path

from .adapters import get_adapter, known_adapters
from .harness import run_benchmark


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(
        prog="python -m evals.harness.runner",
        description="Run an external benchmark via the Hermes harness.",
    )
    p.add_argument("--benchmark", required=True, help="Adapter name, e.g. osworld2")
    p.add_argument("--model", default="default", help="Model to use, e.g. mmx/M-7b")
    p.add_argument("--limit", type=int, default=None, help="Max tasks to run")
    p.add_argument(
        "--parallel",
        type=int,
        default=1,
        help="Reserved for parallel execution; not yet implemented (runs sequentially)",
    )
    p.add_argument("--output-dir", type=Path, default=None)
    args = p.parse_args(argv)

    adapter = get_adapter(args.benchmark)
    if adapter is None:
        registered = known_adapters()
        available = ", ".join(registered) if registered else "none"
        raise SystemExit(
            f"No adapter registered for {args.benchmark!r} (registered: {available})"
        )

    run = run_benchmark(
        adapter,
        model=args.model,
        limit=args.limit,
        parallel=args.parallel,
        output_dir=args.output_dir,
    )

    print(f"\n=== {run.benchmark} ({run.version}) on {run.model} ===")
    print(f"Tasks:       {run.summary['n_tasks']}")
    print(f"Mean score:  {run.summary['mean_score']:.3f}")
    print(f"p50 latency: {run.summary['p50_latency_s']:.1f}s")
    print(f"p90 latency: {run.summary['p90_latency_s']:.1f}s")
    print(f"Errors:      {run.summary['error_count']}")
    print(f"Total cost:  ${run.summary['total_cost_usd']:.4f}")
    print(f"Scorecard:   {run.scorecard_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
