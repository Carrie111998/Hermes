"""CLI/gateway adapter for the `/fusion` slash command."""

from __future__ import annotations

import shlex
from typing import Any

from agent.fusion.models import FusionRequest, FusionResult
from agent.fusion.orchestrator import run_fusion

FUSION_MODES = {"plan", "review", "findings", "recommend"}
USAGE = (
    "Usage: /fusion [plan|review|findings|recommend] <task> "
    "[--participants N] [--roster name] [--models provider:model,...] "
    "[--reasoning xhigh] [--debate-rounds N] [--convergence-rounds N] "
    "[--allow-homogeneous] [--timeout S] [--repo PATH]"
)


def _split_models(value: str) -> list[str]:
    return [item.strip() for item in (value or "").split(",") if item.strip()]


def parse_fusion_command(command: str, *, default_repo_path: str | None = None) -> FusionRequest:
    try:
        tokens = shlex.split((command or "").strip())
    except ValueError as exc:
        raise ValueError(f"Could not parse /fusion arguments: {exc}") from exc
    if tokens and tokens[0].lstrip("/").lower() == "fusion":
        tokens = tokens[1:]
    if not tokens or tokens[0] in {"--help", "-h", "help"}:
        raise ValueError(USAGE)

    mode = "plan"
    if tokens and tokens[0].lower() in FUSION_MODES:
        mode = tokens.pop(0).lower()

    participants = 3
    roster = "planning"
    timeout = 300
    repo_path = default_repo_path
    model_specs: list[str] = []
    min_distinct_models = 2
    allow_homogeneous = False
    debate_rounds = 5
    convergence_rounds = 5
    reasoning_effort: str | None = None
    task_parts: list[str] = []
    idx = 0
    while idx < len(tokens):
        token = tokens[idx]
        if token in {"--participants", "-p"}:
            idx += 1
            if idx >= len(tokens):
                raise ValueError("--participants requires a number")
            participants = int(tokens[idx])
        elif token == "--roster":
            idx += 1
            if idx >= len(tokens):
                raise ValueError("--roster requires a name")
            roster = tokens[idx]
        elif token == "--models":
            idx += 1
            if idx >= len(tokens):
                raise ValueError("--models requires provider:model,...")
            model_specs = _split_models(tokens[idx])
        elif token == "--min-distinct-models":
            idx += 1
            if idx >= len(tokens):
                raise ValueError("--min-distinct-models requires a number")
            min_distinct_models = int(tokens[idx])
        elif token == "--allow-homogeneous":
            allow_homogeneous = True
        elif token == "--debate-rounds":
            idx += 1
            if idx >= len(tokens):
                raise ValueError("--debate-rounds requires a number")
            debate_rounds = int(tokens[idx])
        elif token == "--convergence-rounds":
            idx += 1
            if idx >= len(tokens):
                raise ValueError("--convergence-rounds requires a number")
            convergence_rounds = int(tokens[idx])
        elif token == "--reasoning":
            idx += 1
            if idx >= len(tokens):
                raise ValueError("--reasoning requires an effort level")
            reasoning_effort = tokens[idx]
        elif token == "--timeout":
            idx += 1
            if idx >= len(tokens):
                raise ValueError("--timeout requires seconds")
            timeout = int(tokens[idx])
        elif token == "--repo":
            idx += 1
            if idx >= len(tokens):
                raise ValueError("--repo requires a path")
            repo_path = tokens[idx]
        elif token.startswith("--"):
            raise ValueError(f"Unknown /fusion option: {token}")
        else:
            task_parts.append(token)
        idx += 1

    task = " ".join(task_parts).strip()
    if not task:
        raise ValueError(USAGE)
    return FusionRequest(
        mode=mode,
        task=task,
        participants=participants,
        roster=roster,
        timeout_seconds=timeout,
        repo_path=repo_path,
        model_specs=model_specs,
        min_distinct_models=min_distinct_models,
        allow_homogeneous_models=allow_homogeneous,
        debate_rounds=debate_rounds,
        convergence_rounds=convergence_rounds,
        reasoning_effort=reasoning_effort,
    )


def render_fusion_result(result: FusionResult) -> str:
    lines = [f"Fusion status: `{result.status}`", f"Artifacts: `{result.run_dir}`"]
    if result.decision:
        lines.append(f"Decision: `{result.decision}`")
    if result.routing:
        lines.append(
            f"Route: `{result.routing.get('task_kind', 'unknown')}` "
            f"locate_required=`{result.routing.get('locate_required', False)}`"
        )
    if result.coverage:
        requested = result.coverage.get("requested") or result.coverage.get("total")
        successful = result.coverage.get("draft_successful") or result.coverage.get("successful")
        degraded = " degraded" if result.coverage.get("degraded") else ""
        lines.append(f"Coverage: {successful}/{requested}{degraded}")
    if result.model_diversity:
        distinct = result.model_diversity.get("distinct_count")
        required = result.model_diversity.get("required_distinct_models")
        lines.append(f"Model diversity: {distinct}/{required} distinct provider:model pairs")
        for participant in result.model_diversity.get("participants", [])[:8]:
            lines.append(
                f"- {participant.get('slug')}: `{participant.get('provider')}:{participant.get('model')}` "
                f"reasoning=`{participant.get('reasoning_effort') or 'inherit'}`"
            )
    successful = len([p for p in result.participants if p.ok])
    if result.participants and not result.coverage:
        lines.append(f"Coverage: {successful}/{len(result.participants)} draft participants")
    if result.phases:
        phase_bits = [f"{phase}={len([p for p in items if p.ok])}/{len(items)}" for phase, items in result.phases.items()]
        lines.append("Phases: " + ", ".join(phase_bits))
    if result.spikes:
        available = len([spike for spike in result.spikes if spike.available])
        cleaned = len([spike for spike in result.spikes if spike.cleanup_ok])
        lines.append(f"Spikes: {available}/{len(result.spikes)} worktrees available, cleanup {cleaned}/{len(result.spikes)}")
    if result.status == "converged":
        final_path = result.artifacts.get("synthesis:final_plan") or "synthesis/final_plan.md"
        lines.append(f"Final artifact emitted: `{final_path}`")
    elif result.status == "operator_decision":
        lines.append("No final plan was emitted: unresolved material disagreement requires operator decision.")
        if result.operator_decision:
            lines.append("Fork options:")
            lines.extend(f"- {option}" for option in result.operator_decision.fork_options[:6])
    elif result.status == "model_diversity_error":
        lines.append("No participant execution happened: Fusion could not resolve a heterogeneous model roster.")
        if result.error:
            lines.append(f"Error: {result.error}")
    elif result.status == "write_leak":
        lines.append("⚠️ Write leak detected: tracked repo state changed during participant workflow. No final plan was emitted.")
    elif result.error:
        lines.append(f"Error: {result.error}")
    return "\n".join(lines)


def run_fusion_command(
    command: str,
    *,
    cli: Any = None,
    parent_agent: Any = None,
    default_repo_path: str | None = None,
) -> str:
    try:
        request = parse_fusion_command(command, default_repo_path=default_repo_path)
    except ValueError as exc:
        return str(exc)
    if parent_agent is None and cli is not None:
        parent_agent = getattr(cli, "agent", None)
    result = run_fusion(request, parent_agent=parent_agent)
    return render_fusion_result(result)


def handle_fusion_command(command: str, *, cli: Any = None) -> None:
    try:
        output = run_fusion_command(command, cli=cli)
    except Exception as exc:
        output = str(exc)
    if cli is not None and hasattr(cli, "_console_print"):
        cli._console_print(output)
    else:
        print(output)
