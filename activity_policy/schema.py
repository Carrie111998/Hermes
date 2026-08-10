from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Any, Mapping

EXECUTION_CLASSES = frozenset({"D0", "S1", "S2", "P3", "P4"})
QUALITY_FLOORS = frozenset({"deterministic", "bounded", "standard", "premium", "exceptional"})
ESCALATION_REASONS = frozenset({
    "quality_gate_failed", "conflicting_evidence", "low_source_confidence",
    "high_value_application", "executive_role", "security_sensitive",
    "architecture_cross_service", "test_failure_nonlocal", "provider_unavailable",
    "context_limit_pressure",
})

class PolicyError(ValueError):
    pass

@dataclass(frozen=True)
class BudgetPolicy:
    max_turns: int
    max_model_calls: int
    max_uncached_input_tokens: int
    max_cache_read_tokens: int
    max_cache_write_tokens: int
    max_output_tokens: int
    max_reasoning_tokens: int
    max_tool_calls: int
    wall_clock_seconds: int
    retries: int
    max_children: int
    max_child_depth: int
    max_recorded_provider_cost_usd: Decimal
    max_api_equivalent_cost_usd: Decimal

@dataclass(frozen=True)
class ActivityPolicy:
    activity_id: str
    policy_version: int
    owner: str
    aliases: tuple[str, ...]
    execution_class: str
    quality_floor: str
    preferred_models: tuple[str, ...]
    allowed_fallbacks: tuple[str, ...]
    reasoning_mode: str
    reasoning_effort: str
    budgets: BudgetPolicy
    tools_allow: tuple[str, ...]
    tools_deny: tuple[str, ...]
    required_artifacts: tuple[str, ...]
    required_validations: tuple[str, ...]
    escalation_on: tuple[str, ...]

    @classmethod
    def from_mapping(cls, activity_id: str, data: Mapping[str, Any]) -> "ActivityPolicy":
        if not isinstance(data, Mapping):
            raise PolicyError(f"policy {activity_id} must be a mapping")
        allowed = {"policy_version", "owner", "aliases", "execution_class", "quality_floor", "preferred_models", "allowed_fallbacks", "reasoning", "budgets", "tools", "outcome_contract", "escalation"}
        _exact_keys(data, allowed, f"policy {activity_id}", require_all=True)
        version = data.get("policy_version")
        if isinstance(version, bool) or not isinstance(version, int) or version <= 0:
            raise PolicyError("policy_version must be a positive integer")
        owner = data.get("owner")
        if not isinstance(owner, str) or not owner.strip():
            raise PolicyError("owner must be a non-blank string")
        execution_class = data.get("execution_class")
        if execution_class not in EXECUTION_CLASSES:
            raise PolicyError(f"unknown execution class: {execution_class!r}")
        quality_floor = data.get("quality_floor")
        if quality_floor not in QUALITY_FLOORS:
            raise PolicyError(f"unknown quality floor: {quality_floor!r}")
        aliases = _strings(data.get("aliases"), "aliases")
        preferred = _strings(data.get("preferred_models"), "preferred_models")
        fallbacks = _strings(data.get("allowed_fallbacks"), "allowed_fallbacks")
        reasoning = _mapping(data.get("reasoning"), "reasoning")
        _exact_keys(reasoning, {"mode", "effort"}, "reasoning", require_all=True)
        mode = _nonblank(reasoning.get("mode"), "reasoning mode")
        effort = _nonblank(reasoning.get("effort"), "reasoning effort")
        budget_data = _mapping(data.get("budgets"), "budgets")
        budget_ints = ("max_turns", "max_model_calls", "max_uncached_input_tokens", "max_cache_read_tokens", "max_cache_write_tokens", "max_output_tokens", "max_reasoning_tokens", "max_tool_calls", "wall_clock_seconds", "retries", "max_children", "max_child_depth")
        cost_keys = ("max_recorded_provider_cost_usd", "max_api_equivalent_cost_usd")
        _exact_keys(budget_data, set(budget_ints + cost_keys), "budgets", require_all=True)
        integers = {name: _nonnegative_int(budget_data[name], name) for name in budget_ints}
        costs = {name: _nonnegative_decimal(budget_data[name], name) for name in cost_keys}
        budgets = BudgetPolicy(**integers, **costs)
        tools = _mapping(data.get("tools"), "tools")
        _exact_keys(tools, {"allow", "deny"}, "tools", require_all=True)
        tools_allow = _strings(tools.get("allow"), "tools allow")
        tools_deny = _strings(tools.get("deny"), "tools deny")
        overlap = set(tools_allow) & set(tools_deny)
        if overlap:
            raise PolicyError(f"tool allow/deny overlap: {sorted(overlap)}")
        if execution_class != "D0" and not tools_allow:
            raise PolicyError("non-D0 activity requires a non-empty tool allowlist")
        outcome = _mapping(data.get("outcome_contract"), "outcome_contract")
        _exact_keys(outcome, {"required_artifacts", "required_validations"}, "outcome_contract", require_all=True)
        artifacts = _strings(outcome.get("required_artifacts"), "required_artifacts")
        validations = _strings(outcome.get("required_validations"), "required_validations")
        escalation = _mapping(data.get("escalation"), "escalation")
        _exact_keys(escalation, {"on"}, "escalation", require_all=True)
        escalation_on = _strings(escalation.get("on"), "escalation on")
        unknown = set(escalation_on) - ESCALATION_REASONS
        if unknown:
            raise PolicyError(f"unknown escalation reason: {sorted(unknown)}")
        if execution_class == "D0" and (quality_floor != "deterministic" or preferred or fallbacks or budgets.max_model_calls != 0):
            raise PolicyError("D0 requires deterministic quality, no model routes, and zero model calls")
        return cls(activity_id, version, owner.strip(), aliases, execution_class, quality_floor, preferred, fallbacks, mode, effort, budgets, tools_allow, tools_deny, artifacts, validations, escalation_on)

def _mapping(value: Any, label: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise PolicyError(f"{label} must be a mapping")
    return value

def _exact_keys(data: Mapping[str, Any], allowed: set[str], label: str, *, require_all: bool = False) -> None:
    unknown = set(data) - allowed
    if unknown:
        raise PolicyError(f"{label} has unknown keys: {sorted(unknown)}")
    missing = allowed - set(data)
    if require_all and missing:
        raise PolicyError(f"{label} missing keys: {sorted(missing)}")

def _strings(value: Any, label: str) -> tuple[str, ...]:
    if not isinstance(value, list) or any(not isinstance(item, str) or not item.strip() for item in value):
        raise PolicyError(f"{label} must be a list of non-blank strings")
    values = tuple(item.strip() for item in value)
    if len(set(values)) != len(values):
        raise PolicyError(f"{label} contains duplicates")
    return values

def _nonblank(value: Any, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise PolicyError(f"{label} must be a non-blank string")
    return value.strip()

def _nonnegative_int(value: Any, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise PolicyError(f"{label} must be a non-negative integer")
    return value

def _nonnegative_decimal(value: Any, label: str) -> Decimal:
    try:
        result = Decimal(str(value))
    except (InvalidOperation, ValueError):
        raise PolicyError(f"{label} must be a non-negative decimal") from None
    if not result.is_finite() or result < 0:
        raise PolicyError(f"{label} must be a non-negative decimal")
    return result
