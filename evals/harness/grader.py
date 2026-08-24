"""Scoring primitives for the Hermes eval harness.

Three implemented composable graders (`exact_match`, `regex_match`,
`trajectory_check`) plus `llm_judge`, which is registered in `GRADERS` but
raises `NotImplementedError` until the judge-model integration lands. Each
benchmark picks the grader(s) it needs per task in `task.grader_config`.

Pattern: each grader is a callable `GraderFn = Callable[[Any, dict], GraderResult]`
that takes (output, config) and returns a GraderResult.
"""

from __future__ import annotations

import re
from typing import Any, Callable
from .harness import GraderResult


# ---------- Grader primitives ----------------------------------------------

def exact_match(output: Any, config: dict) -> GraderResult:
    """String equality, post-trim and case-normalized.

    Config:
        expected: str — the expected output
        case_sensitive: bool (default False)
        strip: bool (default True)
    """
    expected = config["expected"]
    case_sensitive = config.get("case_sensitive", False)
    strip = config.get("strip", True)

    actual = str(output) if output is not None else ""
    if strip:
        actual = actual.strip()
        expected = expected.strip()
    if not case_sensitive:
        actual = actual.lower()
        expected = expected.lower()

    return GraderResult(
        task_id=config.get("task_id", "?"),
        score=1.0 if actual == expected else 0.0,
        reason=f"exact_match: {'OK' if actual == expected else 'MISMATCH'}",
    )


def regex_match(output: Any, config: dict) -> GraderResult:
    """Output matches a regex (search, not fullmatch by default).

    Config:
        pattern: str — the regex pattern
        fullmatch: bool (default False)
    """
    pattern = config["pattern"]
    fullmatch = config.get("fullmatch", False)
    actual = str(output) if output is not None else ""

    if fullmatch:
        match = re.fullmatch(pattern, actual)
    else:
        match = re.search(pattern, actual)

    return GraderResult(
        task_id=config.get("task_id", "?"),
        score=1.0 if match else 0.0,
        reason=f"regex_match: {'HIT' if match else 'MISS'} ({pattern!r})",
    )


def llm_judge(output: Any, config: dict) -> GraderResult:
    """Use a model-as-judge to score output against a rubric.

    Config:
        rubric: str — what to evaluate (e.g. 'Did the agent correctly identify
              the user's intent?')
        model: str — judge model (default 'judge')
        scale: '0-1' | '0-10' (default '0-1')
    """
    # Real implementation would call the judge model via the standard
    # hermes chat-completion endpoint. Until then this raises rather than
    # scoring, so a missing implementation can never be mistaken for a 0.0.
    raise NotImplementedError(
        "llm_judge requires the judge model integration; see adapters/* for "
        "an example. Implement in a follow-up commit."
    )


def trajectory_check(trajectory: list[dict], config: dict) -> GraderResult:
    """Assert trajectory satisfies invariants.

    Config:
        must_call: list[str] — tool names that must appear in trajectory
        must_not_call: list[str] — tool names that must NOT appear
        max_tool_calls: int
        must_call_in_order: list[str] — tool calls must appear in this order
    """
    tool_calls = [step.get("tool") for step in trajectory if step.get("tool")]
    sub_scores: dict[str, float] = {}

    must_call = config.get("must_call", [])
    must_not_call = config.get("must_not_call", [])
    max_tool_calls = config.get("max_tool_calls")
    must_call_in_order = config.get("must_call_in_order", [])

    # must_call
    if must_call:
        missing = [t for t in must_call if t not in tool_calls]
        sub_scores["must_call"] = 1.0 - (len(missing) / len(must_call))
    # must_not_call
    if must_not_call:
        violations = [t for t in must_not_call if t in tool_calls]
        sub_scores["must_not_call"] = 1.0 if not violations else 0.0
    # max_tool_calls
    if max_tool_calls is not None:
        sub_scores["max_tool_calls"] = 1.0 if len(tool_calls) <= max_tool_calls else 0.0
    # must_call_in_order
    if must_call_in_order:
        order_ok = True
        last_idx = -1
        for tool in must_call_in_order:
            try:
                idx = tool_calls.index(tool, last_idx + 1)
                last_idx = idx
            except ValueError:
                order_ok = False
                break
        sub_scores["must_call_in_order"] = 1.0 if order_ok else 0.0

    score = sum(sub_scores.values()) / len(sub_scores) if sub_scores else 1.0
    return GraderResult(
        task_id=config.get("task_id", "?"),
        score=score,
        reason=f"trajectory_check: {sub_scores}",
        sub_scores=sub_scores,
    )


# ---------- Registry -------------------------------------------------------

GRADERS: dict[str, Callable] = {
    "exact_match": exact_match,
    "regex_match": regex_match,
    "llm_judge": llm_judge,
    "trajectory_check": trajectory_check,
}


def grade(task_config: dict, output: Any, trajectory: list[dict] | None = None) -> GraderResult:
    """Dispatch to the right grader based on `task_config['grader']`."""
    grader_name = task_config["grader"]
    grader_fn = GRADERS.get(grader_name)
    if grader_fn is None:
        raise ValueError(f"Unknown grader: {grader_name!r}. Known: {list(GRADERS)}")

    if grader_name == "trajectory_check":
        return grader_fn(trajectory or [], task_config)
    return grader_fn(output, task_config)
