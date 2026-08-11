"""Pure validation for canonical product-workflow terminal outcomes.

The database layer owns task/run transitions.  This module only interprets the
structured envelope that an ordinary Test or Review worker is allowed to use.
Serialized prompt-parameter markup is deliberately observed as a leak, never
parsed as lifecycle authority.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Literal, cast


TerminalVerdict = Literal[
    "passed",
    "approved",
    "changes_requested",
    "architecture_invalid",
]
OutcomeObservation = Literal["serialized_parameter_leak"]

_REWORK_ROUTES: dict[tuple[str, str], str] = {
    ("test", "changes_requested"): "development",
    ("review", "changes_requested"): "development",
    ("review", "architecture_invalid"): "architecture",
}
_POSITIVE_VERDICTS: dict[str, str] = {
    "test": "passed",
    "review": "approved",
}
_KNOWN_VERDICTS = frozenset(
    {"passed", "approved", "changes_requested", "architecture_invalid"}
)
_SERIALIZED_PARAMETER_RE = re.compile(
    r"<parameter\s+name=['\"]workflow_outcome['\"]\s*>"
)
_MISSING = object()


@dataclass(frozen=True)
class TerminalOutcome:
    """The only outcome shape ordinary Test/Review completion may consume."""

    verdict: TerminalVerdict
    target_step: str | None
    findings: tuple[str, ...]
    observations: tuple[OutcomeObservation, ...]


class OutcomeValidationError(ValueError):
    """A safe, bounded reason why a terminal envelope is not authoritative."""

    def __init__(self, code: str, *, qualifier: str | None = None):
        self.code = code
        self.qualifier = qualifier
        super().__init__(code)


class ProductOutcomeError(ValueError):
    """Typed ordinary-completion rejection with no worker-authored prose."""

    def __init__(
        self,
        task_id: str,
        run_id: int,
        phase: str,
        code: str,
        qualifier: str | None,
    ):
        self.task_id = task_id
        self.run_id = run_id
        self.phase = phase
        self.code = code
        self.qualifier = qualifier
        super().__init__(code)


def _has_serialized_parameter_marker(summary: str | None, result: str | None) -> bool:
    return any(
        isinstance(value, str) and _SERIALIZED_PARAMETER_RE.search(value)
        for value in (summary, result)
    )


def _invalid_shape() -> OutcomeValidationError:
    return OutcomeValidationError("invalid_shape")


def _validate_redundant_fields(
    phase: str,
    outcome: TerminalOutcome,
    metadata: Mapping[str, object],
) -> None:
    """Reject contradictory aliases without treating aliases as authority.

    Older workers sometimes repeated a verdict at the metadata root or under a
    role-specific provenance object.  Those values are advisory: a recognized
    value may agree with the canonical envelope, but it can never repair a
    missing envelope or override it.
    """

    aliases: list[Any] = []
    for key in (
        "verdict",
        "outcome",
        "run_outcome",
        "completion_outcome",
        "outcome_verdict",
        "reviewer_verdict",
        "tester_verdict",
        "reviewer_result",
        "tester_result",
    ):
        if key in metadata:
            aliases.append((key, metadata[key]))

    for source in (metadata, metadata.get("ai_provenance")):
        if not isinstance(source, Mapping):
            continue
        for role in ("reviewer", "tester", "verifier"):
            details = source.get(role)
            if isinstance(details, Mapping):
                for key in ("verdict", "result", "outcome"):
                    if key in details:
                        aliases.append((f"{role}.{key}", details[key]))

    canonical_verdict = outcome.verdict
    for name, value in aliases:
        if not isinstance(value, str):
            continue
        normalized = value.strip().lower()
        if normalized not in _KNOWN_VERDICTS:
            continue
        if name.startswith("reviewer_"):
            role = "reviewer"
        elif name.startswith("tester_"):
            role = "tester"
        else:
            role = name.split(".", 1)[0]
        role_is_current = (
            role == "reviewer" and phase == "review"
        ) or (role in {"tester", "verifier"} and phase == "test")
        generic_alias = "." not in name and not name.startswith(
            ("reviewer_", "tester_")
        )
        if (generic_alias or role_is_current) and normalized != canonical_verdict:
            raise OutcomeValidationError("contradictory")


def _validate_exact_shape(phase: str, canonical: object) -> TerminalOutcome:
    if not isinstance(canonical, Mapping):
        raise _invalid_shape()

    keys = set(canonical)
    verdict = canonical.get("verdict")
    if not isinstance(verdict, str) or verdict not in _KNOWN_VERDICTS:
        raise OutcomeValidationError("invalid_verdict")
    typed_verdict = cast(TerminalVerdict, verdict)

    positive = _POSITIVE_VERDICTS.get(phase)
    if typed_verdict in {"passed", "approved"}:
        if positive != typed_verdict or keys != {"verdict"}:
            raise OutcomeValidationError(
                "phase_mismatch" if positive != typed_verdict else "invalid_shape"
            )
        return TerminalOutcome(
            verdict=typed_verdict, target_step=None, findings=(), observations=()
        )

    if keys != {"verdict", "target_step", "findings"}:
        raise _invalid_shape()
    target_step = canonical.get("target_step")
    expected_target = _REWORK_ROUTES.get((phase, typed_verdict))
    if expected_target is None or target_step != expected_target:
        raise OutcomeValidationError("phase_mismatch")

    raw_findings = canonical.get("findings")
    if (
        not isinstance(raw_findings, list)
        or not raw_findings
        or not all(isinstance(item, str) and item.strip() for item in raw_findings)
    ):
        raise OutcomeValidationError("invalid_findings")
    findings = tuple(item.strip() for item in raw_findings)
    return TerminalOutcome(
        verdict=typed_verdict,
        target_step=expected_target,
        findings=findings,
        observations=(),
    )


def validate_terminal_outcome(
    *,
    task_id: str,
    run_id: int,
    phase: str,
    summary: str | None,
    result: str | None,
    metadata: Mapping[str, object] | None,
) -> TerminalOutcome:
    """Validate an ordinary Test/Review terminal envelope.

    ``task_id`` and ``run_id`` are part of the public seam so callers can bind
    the result to the active task/run before mutating anything.  The pure
    validator does not use worker-authored identifiers as authority.
    """

    del task_id, run_id
    marker = _has_serialized_parameter_marker(summary, result)
    canonical: object = (
        metadata.get("workflow_outcome", _MISSING)
        if isinstance(metadata, Mapping)
        else _MISSING
    )
    if canonical is _MISSING or canonical is None:
        raise OutcomeValidationError(
            "missing", qualifier="serialized_parameter" if marker else None
        )

    outcome = _validate_exact_shape(phase, canonical)
    if not isinstance(metadata, Mapping):
        # The canonical value could only have been found in a Mapping, but keep
        # this guard explicit for unusual Mapping implementations.
        raise _invalid_shape()
    _validate_redundant_fields(phase, outcome, metadata)
    if marker:
        return TerminalOutcome(
            verdict=outcome.verdict,
            target_step=outcome.target_step,
            findings=outcome.findings,
            observations=("serialized_parameter_leak",),
        )
    return outcome


__all__ = [
    "OutcomeValidationError",
    "ProductOutcomeError",
    "TerminalOutcome",
    "validate_terminal_outcome",
]
