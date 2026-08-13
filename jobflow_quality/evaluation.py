"""The release gate: does a candidate route earn the right to run in production?

The premium-routing plan's gate reads *"No premium route is changed until
workload evaluation and shadow outputs pass. Cost is a tie-breaker only after
quality floors."* This is that gate, and it fails closed. Three ways to not
pass, and only one of them is "the candidate was wrong":

* **The set cannot judge.** An evaluation set that is all easy negatives scores
  any competent candidate near-perfectly. A headline accuracy computed over it
  is not evidence, so an unbalanced set never passes regardless of the score.
* **The candidate did not answer.** A missing prediction is scored as wrong,
  never skipped. Treating silence as agreement is how a candidate that crashed
  on half the set walks through the gate with 100%.
* **The candidate excluded a human-approved job.** See below.

The errors are not symmetric and the gate refuses to average them. Advancing a
job that should have been excluded costs one model call — reported, not fatal.
Excluding a job a person approved destroys a real opportunity and leaves no
artifact to notice it happened. Precision on those items is therefore required
to be perfect: one false exclude fails the gate outright, whatever the
aggregate accuracy says.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any

from .golden_set import Difficulty, GoldenItem, Label, summarize


@dataclass(frozen=True)
class Prediction:
    """One candidate verdict. ``job_id`` must match a golden item to count."""

    job_id: str
    label: Label


@dataclass(frozen=True)
class EvaluationResult:
    passed: bool
    total: int
    correct: int
    false_excludes: tuple[str, ...] = ()
    false_advances: tuple[str, ...] = ()
    unpredicted: tuple[str, ...] = ()
    accuracy_by_difficulty: dict[str, float] = field(default_factory=dict)
    accuracy_by_source: dict[str, float] = field(default_factory=dict)
    reasons: tuple[str, ...] = ()


def evaluate(
    items: Sequence[GoldenItem],
    predictions: Sequence[Prediction],
) -> EvaluationResult:
    """Score a candidate against the golden set and decide whether it may ship.

    Raises only on a caller error the result could not be trusted through —
    two predictions for one job means the caller cannot say what it predicted,
    and silently keeping either one would invent a verdict.
    """
    seen: dict[str, Label] = {}
    for prediction in predictions:
        if prediction.job_id in seen:
            raise ValueError(f"duplicate prediction for {prediction.job_id}")
        seen[prediction.job_id] = prediction.label

    reasons: list[str] = []
    false_excludes: list[str] = []
    false_advances: list[str] = []
    unpredicted: list[str] = []
    per_difficulty: dict[str, list[bool]] = {}
    # Broken out so a candidate scored against labels it produced itself cannot
    # hide behind an aggregate. hard_filter graded on deterministic_exclusion
    # agrees with itself by construction.
    per_source: dict[str, list[bool]] = {}

    for item in items:
        predicted = seen.get(item.job_id)
        # Absent is wrong, not absent. Scoring only what the candidate answered
        # rewards a candidate that answered only the easy ones.
        correct = predicted is item.label
        per_difficulty.setdefault(item.difficulty.value, []).append(correct)
        per_source.setdefault(item.source.value, []).append(correct)

        if predicted is None:
            unpredicted.append(item.job_id)
        elif not correct:
            if item.label is Label.ADVANCE:
                false_excludes.append(item.job_id)
            else:
                false_advances.append(item.job_id)

    correct_count = sum(sum(1 for ok in results if ok) for results in per_difficulty.values())

    if not items:
        reasons.append("empty evaluation set: nothing to judge")
    elif not summarize(tuple(items))["balanced"]:
        reasons.append(
            "set balance: one difficulty dominates, so accuracy over it is not evidence"
        )

    if unpredicted:
        reasons.append(
            f"coverage: {len(unpredicted)} of {len(items)} items got no prediction"
        )

    if false_excludes:
        # The plan's "precision=100% on protected positives", stated as the
        # rule it actually is rather than a threshold to be tuned down later.
        reasons.append(
            f"protected positives: {len(false_excludes)} human-approved job(s) excluded"
        )

    return EvaluationResult(
        passed=not reasons,
        total=len(items),
        correct=correct_count,
        false_excludes=tuple(sorted(false_excludes)),
        false_advances=tuple(sorted(false_advances)),
        unpredicted=tuple(sorted(unpredicted)),
        accuracy_by_difficulty={
            name: (sum(1 for ok in results if ok) / len(results)) if results else 0.0
            for name, results in sorted(per_difficulty.items())
        },
        accuracy_by_source={
            name: (sum(1 for ok in results if ok) / len(results)) if results else 0.0
            for name, results in sorted(per_source.items())
        },
        reasons=tuple(reasons),
    )


def as_dict(result: EvaluationResult) -> dict[str, Any]:
    """Report shape for an operator or a CI step."""
    return {
        "passed": result.passed,
        "total": result.total,
        "correct": result.correct,
        "accuracy_by_difficulty": result.accuracy_by_difficulty,
        "accuracy_by_source": result.accuracy_by_source,
        "false_excludes": list(result.false_excludes),
        "false_advances": list(result.false_advances),
        "unpredicted": list(result.unpredicted),
        "reasons": list(result.reasons),
    }


__all__ = ["Prediction", "EvaluationResult", "evaluate", "as_dict", "Difficulty"]
