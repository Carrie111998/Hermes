"""Small JSON-friendly models for Fusion artifact tests."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field, is_dataclass
from pathlib import Path
from typing import Any, Mapping, Sequence


def _jsonable(value: Any) -> Any:
    if isinstance(value, Path):
        return str(value)
    if is_dataclass(value):
        return {k: _jsonable(v) for k, v in asdict(value).items()}
    if isinstance(value, Mapping):
        return {str(k): _jsonable(v) for k, v in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [_jsonable(v) for v in value]
    return value


@dataclass(slots=True)
class JsonModel:
    def to_dict(self) -> dict[str, Any]:
        return _jsonable(self)


@dataclass(slots=True)
class FusionRequest(JsonModel):
    mode: str
    task: str
    participants: int = 3
    roster: str = "planning"
    timeout_seconds: int | None = None
    repo_path: str | None = None
    output_root: str | None = None
    min_successful_participants: int = 2
    allow_single_participant: bool = False
    model_specs: list[str] = field(default_factory=list)
    min_distinct_models: int = 2
    allow_homogeneous_models: bool = False
    debate_rounds: int = 5
    convergence_rounds: int = 5
    reasoning_effort: str | None = None
    spike_worktrees: bool = True


@dataclass(slots=True)
class FusionParticipantSpec(JsonModel):
    slug: str
    role: str
    focus: str
    model: str | None = None
    provider: str | None = None
    api_mode: str | None = None
    reasoning_effort: str | None = None
    model_slug: str | None = None
    requested_provider: str | None = None
    requested_model: str | None = None

    @property
    def runtime_label(self) -> str:
        provider = (
            self.provider or self.requested_provider or "inherit"
        ).strip() or "inherit"
        model = (self.model or self.requested_model or "inherit").strip() or "inherit"
        return f"{provider}:{model}"


@dataclass(slots=True)
class FusionParticipantResult(JsonModel):
    spec: FusionParticipantSpec
    status: str
    output: str = ""
    error: str | None = None
    duration_seconds: float = 0.0
    api_calls: int = 0
    model: str | None = None
    provider: str | None = None
    phase: str = "draft"
    metadata: dict[str, Any] = field(default_factory=dict)
    output_hash: str | None = None

    @property
    def ok(self) -> bool:
        return self.status == "completed" and bool(self.output.strip())

    @property
    def failure_reason(self) -> str:
        return (
            self.error
            or self.metadata.get("reason")
            or self.metadata.get("fail_fast_reason")
            or "no usable output"
        )


def count_successful(results: Sequence[FusionParticipantResult]) -> int:
    return sum(1 for result in results if result.ok)


@dataclass(slots=True)
class FusionConvergenceVote(JsonModel):
    participant: str
    candidate_id: str
    approved: bool
    material_dissent: list[str] = field(default_factory=list)
    required_changes: list[str] = field(default_factory=list)
    unsupported_claims: list[str] = field(default_factory=list)
    confidence: str = ""
    summary: str = ""


@dataclass(slots=True)
class FusionCandidate(JsonModel):
    id: str
    round_index: int
    content: str
    source_phases: list[str] = field(default_factory=list)


@dataclass(slots=True)
class FusionConsensusItem(JsonModel):
    axis: str
    agreed: bool
    summary: str
    participants: list[str]


@dataclass(slots=True)
class FusionConflict(JsonModel):
    axis: str
    summary: str
    claims: dict[str, str]
    participants: list[str]
    material: bool = True


@dataclass(slots=True)
class FusionFinding(JsonModel):
    axis: str
    participant: str
    claim: str
    support: str = ""
    confidence: str = ""


@dataclass(slots=True)
class FusionVerificationReport(JsonModel):
    matrix: dict[str, dict[str, str]] = field(default_factory=dict)
    consensus_items: list[FusionConsensusItem] = field(default_factory=list)
    conflicts: list[FusionConflict] = field(default_factory=list)
    unsupported_claims: list[FusionFinding] = field(default_factory=list)
    successful_participants: list[str] = field(default_factory=list)
    total_participants: int = 0
    candidate_id: str | None = None
    votes: list[FusionConvergenceVote] = field(default_factory=list)
    approved_participants: list[str] = field(default_factory=list)
    rejected_participants: list[str] = field(default_factory=list)
    model_diversity: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class FusionOperatorDecision(JsonModel):
    summary: str
    fork_options: list[str]
    conflicts: list[FusionConflict] = field(default_factory=list)


@dataclass(slots=True)
class FusionResult(JsonModel):
    status: str
    request: FusionRequest
    run_dir: str
    participants: list[FusionParticipantResult] = field(default_factory=list)
    phases: dict[str, list[FusionParticipantResult]] = field(default_factory=dict)
    candidates: list[FusionCandidate] = field(default_factory=list)
    votes: list[FusionConvergenceVote] = field(default_factory=list)
    routing: dict[str, Any] = field(default_factory=dict)
    brief: dict[str, Any] = field(default_factory=dict)
    coverage: dict[str, Any] = field(default_factory=dict)
    decision: str = ""
    verification: FusionVerificationReport | None = None
    operator_decision: FusionOperatorDecision | None = None
    artifacts: dict[str, str] = field(default_factory=dict)
    error: str | None = None

