"""Fail-closed model routing for Kanban worker tasks.

The router consumes only explicit, already-structured task metadata.  It must
never inspect task prose (title/body), infer intent from text, or consult
configuration.  A caller may use :meth:`RouteDecision.to_event_payload` for a
safe audit payload; it deliberately contains only fixed vocabulary and the
selected model/provider.

This module is intentionally stdlib-only so it can sit on the Kanban spawn
boundary without importing the agent runtime.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any


ROUTE_VERSION = "v1"
BASE_PROVIDER = "openai-codex"
LUNA_MODEL = "gpt-5.6-luna"
TERRA_MODEL = "gpt-5.6-terra"
SOL_MODEL = "gpt-5.6-sol"
ALLOWED_TARGET_MODELS = frozenset({LUNA_MODEL, TERRA_MODEL, SOL_MODEL})

ROUTING_METADATA_KEYS = frozenset(
    {
        "enabled",
        "risk_level",
        "external_send_requested",
        "cross_file",
        "tool_count",
        "multi_step_verification",
        "luna_insufficiency",
        "high_value",
        "terra_insufficient",
        "deep_reasoning_required",
    }
)
_ROUTING_BOOL_KEYS = ROUTING_METADATA_KEYS - {"risk_level", "tool_count"}
_RISK_LEVELS = frozenset({"low", "medium", "high"})


def validate_routing_metadata(
    metadata: Mapping[str, object] | None,
) -> dict[str, object] | None:
    """Validate and copy the task-level routing metadata contract.

    The field is optional, so ``None`` remains ``None``.  A supplied mapping
    may contain any subset of the allowlisted keys; omission is distinct from
    an enabled route and therefore remains fail-closed at decision time.
    Validation is deliberately strict at the persistence boundary: unknown
    keys, integer/bool confusion, and stringified booleans are rejected.
    """

    if metadata is None:
        return None
    if not isinstance(metadata, Mapping):
        raise ValueError("routing_metadata must be an object")

    unknown = set(metadata) - ROUTING_METADATA_KEYS
    if unknown:
        names = ", ".join(sorted(str(key) for key in unknown))
        raise ValueError(f"routing_metadata has unknown key(s): {names}")

    validated = dict(metadata)
    for key in _ROUTING_BOOL_KEYS:
        if key in validated and type(validated[key]) is not bool:
            raise ValueError(f"routing_metadata.{key} must be a boolean")

    if "risk_level" in validated:
        risk_level = validated["risk_level"]
        if type(risk_level) is not str or risk_level not in _RISK_LEVELS:
            raise ValueError(
                "routing_metadata.risk_level must be one of: low, medium, high"
            )

    if "tool_count" in validated:
        tool_count = validated["tool_count"]
        if type(tool_count) is not int or tool_count < 0:
            raise ValueError(
                "routing_metadata.tool_count must be a non-negative integer"
            )

    return validated


@dataclass(frozen=True)
class RouteDecision:
    """The complete, prompt-free result of one routing decision.

    ``selected_*`` is the effective route when the decision is safe to use.
    It is ``None`` for a missing/disabled/unsupported/bypassed metadata input;
    a valid Luna baseline is retained for a valid task that is deliberately
    not escalated.  ``routed`` means an ephemeral child argv should receive a
    model/provider addition.  ``bypass`` means the router must not add one.
    """

    selected_provider: str | None
    selected_model: str | None
    routed: bool
    rule: str
    reason_codes: tuple[str, ...]
    explicit_pin: bool
    bypass: bool
    route_version: str = ROUTE_VERSION

    @property
    def provider(self) -> str | None:
        """Short alias for callers that use provider/model terminology."""

        return self.selected_provider

    @property
    def model(self) -> str | None:
        """Short alias for callers that use provider/model terminology."""

        return self.selected_model

    def to_event_payload(self) -> dict[str, Any]:
        """Return the allowlisted payload for a ``model_routed`` event.

        No input metadata is copied into this result.  In particular, task
        prompt/title/body values and secret-like values cannot cross this
        boundary through an event payload.
        """

        return {
            "selected_provider": self.selected_provider,
            "selected_model": self.selected_model,
            "routed": self.routed,
            "rule": self.rule,
            "reason_codes": list(self.reason_codes),
            "explicit_pin": self.explicit_pin,
            "bypass": self.bypass,
            "route_version": self.route_version,
        }


def _is_true(metadata: Mapping[str, object], key: str) -> bool:
    """Accept only a typed boolean true value, failing closed otherwise."""

    return metadata.get(key) is True


def _has_value(metadata: Mapping[str, object], key: str) -> bool:
    """Whether an override field contains an explicit value.

    ``None`` and an empty string are the unset representation used by the
    existing task model.  Any other value is treated as a pin, including a
    malformed value, so malformed explicit pins cannot be auto-routed.
    """

    value = metadata.get(key)
    if value is None:
        return False
    return not (isinstance(value, str) and not value.strip())


def _has_explicit_pin(metadata: Mapping[str, object]) -> bool:
    """Detect only explicit pin metadata; never infer a pin from task text."""

    if _is_true(metadata, "explicit_pin"):
        return True
    for key in (
        "model_override",
        "provider_override",
        "task_model_override",
        "task_provider_override",
    ):
        if _has_value(metadata, key):
            return True
    return False


def _bypass(
    reason_codes: tuple[str, ...],
    *,
    explicit_pin: bool = False,
    selected_provider: str | None = None,
    selected_model: str | None = None,
) -> RouteDecision:
    return RouteDecision(
        selected_provider=selected_provider,
        selected_model=selected_model,
        routed=False,
        rule="bypass",
        reason_codes=reason_codes,
        explicit_pin=explicit_pin,
        bypass=True,
    )


def route_task_model(metadata: Mapping[str, object] | None) -> RouteDecision:
    """Choose a safe ephemeral Kanban worker model from explicit metadata.

    The baseline must be ``openai-codex/gpt-5.6-luna`` and ``enabled`` must be
    the typed boolean ``True``.  Explicit task model/provider overrides always
    bypass this router.  Only low-risk, non-external tasks may be escalated:

    * Terra: cross-file work, at least three tools, multi-step verification,
      or an explicit Luna insufficiency marker.
    * Sol: high-value work plus Terra insufficiency or deep-reasoning marker;
      this check has precedence over Terra.

    All other cases remain on Luna (or bypass entirely when the baseline is
    absent/invalid).  No field other than the explicit metadata mapping is
    consulted.
    """

    if not isinstance(metadata, Mapping) or not metadata:
        return _bypass(("missing_metadata",))

    explicit_pin = _has_explicit_pin(metadata)
    if explicit_pin:
        return _bypass(("explicit_pin",), explicit_pin=True)

    if not _is_true(metadata, "enabled"):
        return _bypass(("disabled",))

    provider = metadata.get("provider")
    model = metadata.get("model")
    if provider != BASE_PROVIDER or model != LUNA_MODEL:
        return _bypass(("unsupported_baseline",))

    # A valid baseline is retained for all non-escalated decisions.  It is
    # still ``bypass=True`` because the child argv must not be augmented.
    luna = {
        "selected_provider": BASE_PROVIDER,
        "selected_model": LUNA_MODEL,
    }
    risk_level = metadata.get("risk_level")
    if risk_level != "low":
        return _bypass(("risk_not_low",), **luna)
    if _is_true(metadata, "external_send_requested"):
        return _bypass(("external_send_requested",), **luna)

    terra_reasons: list[str] = []
    if _is_true(metadata, "cross_file"):
        terra_reasons.append("cross_file")
    tool_count = metadata.get("tool_count")
    if isinstance(tool_count, int) and not isinstance(tool_count, bool) and tool_count >= 3:
        terra_reasons.append("tool_count_gte_3")
    if _is_true(metadata, "multi_step_verification"):
        terra_reasons.append("multi_step_verification")
    if _is_true(metadata, "luna_insufficiency"):
        terra_reasons.append("luna_insufficiency")

    terra_insufficient = _is_true(metadata, "terra_insufficient")
    deep_reasoning = _is_true(metadata, "deep_reasoning_required")
    if _is_true(metadata, "high_value") and (terra_insufficient or deep_reasoning):
        sol_reasons = ["high_value"]
        if terra_insufficient:
            sol_reasons.append("terra_insufficient")
        if deep_reasoning:
            sol_reasons.append("deep_reasoning_required")
        return RouteDecision(
            selected_provider=BASE_PROVIDER,
            selected_model=SOL_MODEL,
            routed=True,
            rule="sol",
            reason_codes=tuple(sol_reasons),
            explicit_pin=False,
            bypass=False,
        )

    if terra_reasons:
        return RouteDecision(
            selected_provider=BASE_PROVIDER,
            selected_model=TERRA_MODEL,
            routed=True,
            rule="terra",
            reason_codes=tuple(terra_reasons),
            explicit_pin=False,
            bypass=False,
        )

    return _bypass(("luna_default",), **luna)


__all__ = [
    "ALLOWED_TARGET_MODELS",
    "BASE_PROVIDER",
    "LUNA_MODEL",
    "ROUTE_VERSION",
    "ROUTING_METADATA_KEYS",
    "SOL_MODEL",
    "TERRA_MODEL",
    "RouteDecision",
    "route_task_model",
    "validate_routing_metadata",
]
