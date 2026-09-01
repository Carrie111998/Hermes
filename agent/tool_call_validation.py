"""Normalize and validate model-emitted tool-call protocol fields."""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Iterable


@dataclass(frozen=True)
class ToolNameValidation:
    """Name-validation facts that let the caller preserve its retry policy."""

    invalid_names: list[str]
    mixed_invalid_batch: bool


@dataclass(frozen=True)
class ToolArgumentValidation:
    """Argument-validation facts, after in-place protocol normalization."""

    invalid_arguments: list[tuple[str, str]]
    truncated: bool


def validate_tool_call_names(agent: Any, tool_calls: Iterable[Any]) -> ToolNameValidation:
    """Deduplicate ids, repair names, then report unknown-name batch shape."""
    calls = list(tool_calls)
    agent._uniquify_tool_call_ids(calls)
    for tool_call in calls:
        if tool_call.function.name not in agent.valid_tool_names:
            repaired = agent._repair_tool_call(tool_call.function.name)
            if repaired:
                print(
                    f"{agent.log_prefix}🔧 Auto-repaired tool name: "
                    f"'{tool_call.function.name}' -> '{repaired}'"
                )
                tool_call.function.name = repaired
    invalid_names = [
        tool_call.function.name
        for tool_call in calls
        if tool_call.function.name not in agent.valid_tool_names
    ]
    mixed = bool(invalid_names) and any(
        tool_call.function.name in agent.valid_tool_names for tool_call in calls
    )
    return ToolNameValidation(invalid_names, mixed)


def normalize_and_validate_tool_arguments(
    tool_calls: Iterable[Any],
    *,
    valid_tool_names: set[str],
    mixed_invalid_batch: bool,
) -> ToolArgumentValidation:
    """Coerce common provider quirks and report JSON errors without repairing."""
    calls = list(tool_calls)
    invalid_arguments = []
    for tool_call in calls:
        arguments = tool_call.function.arguments
        if isinstance(arguments, (dict, list)):
            tool_call.function.arguments = json.dumps(arguments)
            continue
        if arguments is not None and not isinstance(arguments, str):
            tool_call.function.arguments = str(arguments)
            arguments = tool_call.function.arguments
        if not arguments or not arguments.strip():
            tool_call.function.arguments = "{}"
            continue
        try:
            json.loads(arguments)
        except json.JSONDecodeError as exc:
            if mixed_invalid_batch and tool_call.function.name not in valid_tool_names:
                continue
            invalid_arguments.append((tool_call.function.name, str(exc)))
    invalid_names = {name for name, _ in invalid_arguments}
    truncated = any(
        not (tool_call.function.arguments or "").rstrip().endswith(("}", "]"))
        for tool_call in calls
        if tool_call.function.name in invalid_names
    )
    return ToolArgumentValidation(invalid_arguments, truncated)
