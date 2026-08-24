"""Reporter — renders the markdown scorecard and appends scoreboard.md rows.

The machine-readable JSON is written by `harness.run_benchmark`, not here.
"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path

from .harness import BenchmarkRun, safe_model_slug


SCOREBOARD_HEADER = """# Hermes Eval Scoreboard

Append-only leaderboard across all benchmarks and models. Oldest runs first.

| Date | Benchmark | Model | Score | n | p50 (s) | p90 (s) | Cost ($) | Trace correlation |
|------|-----------|-------|-------|---|---------|---------|----------|-------------------|
"""


def render_scorecard(run: BenchmarkRun) -> str:
    """Render a single BenchmarkRun as a markdown scorecard."""
    s = run.summary
    return f"""# {run.benchmark} ({run.version}) — {run.timestamp}

**Model:** `{run.model}`
**Tasks run:** {s.get('n_tasks', 0)} / total benchmark
**Mean score:** {s.get('mean_score', 0):.3f}
**p50 latency:** {s.get('p50_latency_s', 0):.1f}s
**p90 latency:** {s.get('p90_latency_s', 0):.1f}s
**Total cost:** ${s.get('total_cost_usd', 0):.4f}
**Total tokens:** {s.get('total_tokens', 0):,}
**Errors:** {s.get('error_count', 0)}

## Per-task results

| Task ID | Score | Latency (s) | Cost ($) | Tokens | Trace ID | Error |
|---------|-------|-------------|----------|--------|----------|-------|
""" + "\n".join(
        f"| `{t.task_id}` | {t.score:.3f} | {t.elapsed_seconds:.1f} | {t.cost_usd:.4f} | {t.tokens_used} | `{t.trace_id or '-'}` | {t.error or '-'} |"
        for t in run.task_results
    ) + f"\n\n---\n*Scorecard JSON: `{run.scorecard_path}`*\n"


def write_scorecard(run: BenchmarkRun, output_dir: Path) -> Path:
    """Write the per-run markdown scorecard. Overwrites on re-run.

    The machine-readable JSON is written separately by `run_benchmark`.
    """
    output_dir.mkdir(parents=True, exist_ok=True)
    md_path = output_dir / f"{run.benchmark}_{safe_model_slug(run.model)}_{run.timestamp}.md"
    md_path.write_text(render_scorecard(run), encoding="utf-8", newline="\n")
    return md_path


def append_to_scoreboard(run: BenchmarkRun, scoreboard_path: Path) -> None:
    """Append one row to scoreboard.md. Idempotent (no-op if row already exists)."""
    s = run.summary
    new_row = (
        f"| {datetime.fromisoformat(run.timestamp).strftime('%Y-%m-%d %H:%M')} "
        f"| {run.benchmark} ({run.version}) "
        f"| `{run.model}` "
        f"| {s.get('mean_score', 0):.3f} "
        f"| {s.get('n_tasks', 0)} "
        f"| {s.get('p50_latency_s', 0):.1f} "
        f"| {s.get('p90_latency_s', 0):.1f} "
        f"| {s.get('total_cost_usd', 0):.4f} "
        f"| per-task trace_ids in JSON |"
    )

    if not scoreboard_path.exists():
        scoreboard_path.parent.mkdir(parents=True, exist_ok=True)
        scoreboard_path.write_text(SCOREBOARD_HEADER, encoding="utf-8", newline="\n")

    content = scoreboard_path.read_text(encoding="utf-8")
    # Idempotency: skip if row already present
    if new_row in content:
        return
    scoreboard_path.write_text(content + new_row + "\n", encoding="utf-8", newline="\n")


def init_scoreboard(scoreboard_path: Path) -> None:
    """Create scoreboard.md with header if it doesn't exist."""
    if not scoreboard_path.exists():
        scoreboard_path.parent.mkdir(parents=True, exist_ok=True)
        scoreboard_path.write_text(SCOREBOARD_HEADER, encoding="utf-8", newline="\n")
