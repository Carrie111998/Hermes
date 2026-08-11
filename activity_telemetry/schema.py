from dataclasses import dataclass
from decimal import Decimal

OUTCOMES = frozenset({
    "succeeded",
    "no_work",
    "blocked",
    "partial",
    "failed",
    "budget_exhausted",
    "unknown",
})


@dataclass(frozen=True)
class OutcomeLayers:
    process: str = "unknown"
    protocol: str = "unknown"
    artifact: str = "unknown"
    domain: str = "unknown"
    delivery: str = "unknown"

    def __post_init__(self) -> None:
        for value in (
            self.process,
            self.protocol,
            self.artifact,
            self.domain,
            self.delivery,
        ):
            if value not in OUTCOMES:
                raise ValueError(f"invalid outcome: {value}")


@dataclass(frozen=True)
class LogicalActivityStart:
    run_id: str
    correlation_id: str
    activity_id: str
    policy_version: int
    trigger_source: str
    profile: str
    effective_hermes_home: str
    requested_provider: str | None = None
    requested_model: str | None = None
    session_id: str | None = None
    parent_run_id: str | None = None
    child_depth: int = 0

    def __post_init__(self) -> None:
        for name in (
            "run_id",
            "correlation_id",
            "activity_id",
            "trigger_source",
            "profile",
            "effective_hermes_home",
        ):
            value = getattr(self, name)
            if not isinstance(value, str) or not value.strip():
                raise ValueError(f"{name} must be non-empty")
        if not isinstance(self.policy_version, int) or isinstance(self.policy_version, bool) or self.policy_version <= 0:
            raise ValueError("policy_version must be a positive integer")
        if not isinstance(self.child_depth, int) or isinstance(self.child_depth, bool) or self.child_depth < 0:
            raise ValueError("child_depth must be a non-negative integer")
        for name in ("requested_provider", "requested_model", "session_id", "parent_run_id"):
            value = getattr(self, name)
            if value is not None and (not isinstance(value, str) or not value.strip()):
                raise ValueError(f"{name} must be non-empty when provided")


@dataclass(frozen=True)
class ServedRoute:
    provider: str
    model: str

    def __post_init__(self) -> None:
        for name in ("provider", "model"):
            value = getattr(self, name)
            if not isinstance(value, str) or not value.strip():
                raise ValueError(f"{name} must be non-empty")


@dataclass(frozen=True)
class RouteUsageDelta:
    turns: int = 0
    model_calls: int = 0
    tool_calls: int = 0
    retries: int = 0
    uncached_input_tokens: int = 0
    cache_read_tokens: int = 0
    cache_write_tokens: int = 0
    output_tokens: int = 0
    reasoning_tokens: int = 0
    recorded_provider_cost_usd: Decimal | None = None
    api_equivalent_cost_usd: Decimal | None = None

    def __post_init__(self) -> None:
        for name in (
            "turns",
            "model_calls",
            "tool_calls",
            "retries",
            "uncached_input_tokens",
            "cache_read_tokens",
            "cache_write_tokens",
            "output_tokens",
            "reasoning_tokens",
        ):
            value = getattr(self, name)
            if not isinstance(value, int) or isinstance(value, bool) or value < 0:
                raise ValueError(f"{name} must be a non-negative integer")
        for name in ("recorded_provider_cost_usd", "api_equivalent_cost_usd"):
            value = getattr(self, name)
            if value is not None and (not isinstance(value, Decimal) or not value.is_finite() or value < 0):
                raise ValueError(f"{name} must be a non-negative finite Decimal")


def derive_final_outcome(layers: OutcomeLayers) -> str:
    values = (
        layers.process,
        layers.protocol,
        layers.artifact,
        layers.domain,
        layers.delivery,
    )
    for terminal in ("failed", "budget_exhausted", "blocked", "partial"):
        if terminal in values:
            return terminal
    if layers.process == "no_work":
        return "no_work"
    if all(value == "succeeded" for value in values):
        return "succeeded"
    return "unknown"
