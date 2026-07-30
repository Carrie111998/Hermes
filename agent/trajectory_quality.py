"""Trajectory quality routing — pure reducer and one-way policy ladder.

This module is intentionally side-effect free: it tracks per-turn tool
observations and returns decisions. Runtime code (run_agent.py) owns
whether those decisions become status notices, durable records, or a
controlled turn halt.

When ``TrajectoryQualityConfig.enabled`` is ``False`` (the default),
``observe`` returns ``None`` immediately — zero overhead.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Mapping


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class TrajectoryQualityConfig:
    """Thresholds for trajectory quality routing.

    Disabled by default. When enabled, the controller observes tool
    results and emits escalation decisions on a one-way ladder:
    continue -> recommend_stronger_model -> recommend_clean_restart -> stop.
    """

    enabled: bool = False
    execute_stop: bool = True
    execute_model_switch: bool = False  # unsupported in slice 1; must be ignored
    allow_deescalate_on_progress: bool = False
    persist_decisions: bool = True
    retention_days: int = 30
    max_decisions_per_session: int = 200
    identical_failure: int = 2
    same_tool_failure: int = 4
    failed_verification: int = 2
    stagnation_window: int = 8
    hysteresis_progress_needed: int = 2
    stronger_provider: str | None = None
    stronger_model: str | None = None

    @classmethod
    def from_mapping(cls, data: Mapping[str, Any] | None) -> "TrajectoryQualityConfig":
        """Build config from the ``trajectory_quality_routing`` config section."""
        if not isinstance(data, Mapping):
            return cls()

        thresholds = data.get("thresholds")
        if not isinstance(thresholds, Mapping):
            thresholds = {}

        stronger = data.get("stronger_model")
        if not isinstance(stronger, Mapping):
            stronger = {}

        defaults = cls()
        return cls(
            enabled=_as_bool(data.get("enabled"), defaults.enabled),
            execute_stop=_as_bool(data.get("execute_stop"), defaults.execute_stop),
            execute_model_switch=_as_bool(
                data.get("execute_model_switch"), defaults.execute_model_switch
            ),
            allow_deescalate_on_progress=_as_bool(
                data.get("allow_deescalate_on_progress"),
                defaults.allow_deescalate_on_progress,
            ),
            persist_decisions=_as_bool(
                data.get("persist_decisions"), defaults.persist_decisions
            ),
            retention_days=_positive_int(
                data.get("retention_days"), defaults.retention_days
            ),
            max_decisions_per_session=_positive_int(
                data.get("max_decisions_per_session"),
                defaults.max_decisions_per_session,
            ),
            identical_failure=_positive_int(
                thresholds.get("identical_failure", data.get("identical_failure")),
                defaults.identical_failure,
            ),
            same_tool_failure=_positive_int(
                thresholds.get("same_tool_failure", data.get("same_tool_failure")),
                defaults.same_tool_failure,
            ),
            failed_verification=_positive_int(
                thresholds.get("failed_verification", data.get("failed_verification")),
                defaults.failed_verification,
            ),
            stagnation_window=_positive_int(
                thresholds.get("stagnation_window", data.get("stagnation_window")),
                defaults.stagnation_window,
            ),
            hysteresis_progress_needed=_positive_int(
                data.get("hysteresis_progress_needed"),
                defaults.hysteresis_progress_needed,
            ),
            stronger_provider=_opt_str(stronger.get("provider")),
            stronger_model=_opt_str(stronger.get("model")),
        )


# ---------------------------------------------------------------------------
# Events / state / decisions
# ---------------------------------------------------------------------------

_PROGRESS_KINDS = {"file_mutation_landed", "verification_passed"}


@dataclass(frozen=True)
class TrajectoryObservation:
    """A single structured tool-result observation (no raw content)."""

    tool_name: str
    args_hash: str
    result_hash: str | None
    failed: bool
    progress_kind: str = "none"
    verification_status: str | None = None
    api_call_count: int = 0
    session_id: str = ""
    model: str = ""
    provider: str = ""

    @property
    def is_progress(self) -> bool:
        return self.progress_kind in _PROGRESS_KINDS


@dataclass(frozen=True)
class TrajectoryQualityDecision:
    """A routing decision emitted by the controller."""

    action: str  # continue | recommend_stronger_model | recommend_clean_restart | stop
    reason_code: str
    level_before: str
    level_after: str
    tool_name: str
    args_hash: str
    result_hash: str | None
    count: int
    explain: str
    model: str = ""
    provider: str = ""
    recommended_model: str | None = None
    recommended_provider: str | None = None
    decision_id: str = ""


# Level ordering for monotonic escalation.
_LEVEL_ORDER: dict[str, int] = {
    "continue": 0,
    "recommend_stronger_model": 1,
    "recommend_clean_restart": 2,
    "stop": 3,
}


def _level_max(a: str, b: str) -> str:
    return a if _LEVEL_ORDER.get(a, 0) >= _LEVEL_ORDER.get(b, 0) else b


def _deescalate_one(level: str) -> str:
    """Lower the level by one step (used only when de-escalation is enabled)."""
    order = _LEVEL_ORDER.get(level, 0)
    for name, val in _LEVEL_ORDER.items():
        if val == order - 1:
            return name
    return "continue"


# ---------------------------------------------------------------------------
# Controller
# ---------------------------------------------------------------------------


class TrajectoryQualityController:
    """Per-turn controller for trajectory quality routing.

    Pure: no I/O, no agent runtime. Feed it ``TrajectoryObservation`` events
    and it returns an optional ``TrajectoryQualityDecision`` when the policy
    ladder escalates.
    """

    def __init__(self, config: TrajectoryQualityConfig | None = None):
        self.config = config or TrajectoryQualityConfig()
        self.reset_for_turn()

    def reset_for_turn(self) -> None:
        self._level: str = "continue"
        self._exact_failure_counts: dict[tuple[str, str], int] = {}
        self._same_tool_failure_counts: dict[str, int] = {}
        self._consecutive_no_progress: int = 0
        self._failed_verification_streak: int = 0
        self._turn_saw_failure: bool = False
        self._turn_saw_verification_failed: bool = False
        self._observation_count: int = 0
        self._reason_codes_fired: set[str] = set()
        self._last_emitted_key: tuple[str, str, str, str] | None = None
        self._progress_since_level: int = 0

    @property
    def level(self) -> str:
        return self._level

    def observe(self, obs: TrajectoryObservation) -> TrajectoryQualityDecision | None:
        if not self.config.enabled:
            return None

        self._observation_count += 1
        level_before = self._level

        # ---- Update per-signature counters ----
        key = (obs.tool_name, obs.args_hash)
        exact = 0
        same = 0

        if obs.failed:
            exact = self._exact_failure_counts.get(key, 0) + 1
            self._exact_failure_counts[key] = exact
            same = self._same_tool_failure_counts.get(obs.tool_name, 0) + 1
            self._same_tool_failure_counts[obs.tool_name] = same
            self._turn_saw_failure = True
        else:
            # Success clears the exact-failure counter for this signature.
            self._exact_failure_counts.pop(key, None)
            if obs.is_progress:
                self._same_tool_failure_counts.pop(obs.tool_name, None)

        # ---- Verification streak tracking ----
        if obs.progress_kind == "verification_failed":
            self._failed_verification_streak += 1
            self._turn_saw_verification_failed = True
        elif obs.progress_kind == "verification_passed" or (
            obs.is_progress and obs.progress_kind != "verification_failed"
        ):
            self._failed_verification_streak = 0

        # ---- Stagnation tracking ----
        if obs.is_progress:
            self._consecutive_no_progress = 0
            self._progress_since_level += 1
        else:
            self._consecutive_no_progress += 1

        # ---- Evaluate triggers ----
        target_reason: str | None = None
        target_level = level_before
        count = 0

        # Two-identical-failure circuit breaker.
        if obs.failed and exact >= self.config.identical_failure:
            target_reason = "two_identical_failures"
            target_level = _level_max(target_level, "recommend_stronger_model")
            count = exact

        # Same-tool failure streak.
        if obs.failed and same >= self.config.same_tool_failure:
            if target_reason is None:
                target_reason = "same_tool_failure_streak"
                count = same
            target_level = _level_max(target_level, "recommend_stronger_model")

        # Failed-verification streak.
        if self._failed_verification_streak >= self.config.failed_verification:
            if target_reason is None:
                target_reason = "failed_verification_streak"
                count = self._failed_verification_streak
            target_level = _level_max(target_level, "recommend_stronger_model")

        # Stagnation: no verified progress for N observations, after a prior
        # failure or verification_failed in the turn.
        if (
            self._consecutive_no_progress >= self.config.stagnation_window
            and (self._turn_saw_failure or self._turn_saw_verification_failed)
        ):
            if target_reason is None:
                target_reason = "stagnation_no_progress"
                count = self._consecutive_no_progress
            target_level = _level_max(target_level, "recommend_clean_restart")

        # Compounding: a new reason while already >= level 1 pushes to level 2.
        if target_reason is not None and level_before != "continue":
            if target_reason not in self._reason_codes_fired:
                target_level = _level_max(target_level, "recommend_clean_restart")

        # Any trigger while already at level 2 pushes to stop.
        if target_reason is not None and level_before == "recommend_clean_restart":
            target_level = _level_max(target_level, "stop")

        # ---- De-escalation (optional, off by default) ----
        if (
            self.config.allow_deescalate_on_progress
            and obs.is_progress
            and self._level != "continue"
            and self._progress_since_level >= self.config.hysteresis_progress_needed
        ):
            self._level = _deescalate_one(self._level)
            self._progress_since_level = 0
            # A de-escalation is not an emitted decision — it silently lowers.
            return None

        if target_reason is None:
            return None

        # Monotonic: never lower the level via triggers.
        new_level = _level_max(self._level, target_level)
        if new_level == self._level and target_reason in self._reason_codes_fired:
            # Same reason, same level — suppress duplicate.
            return None

        self._level = new_level
        self._reason_codes_fired.add(target_reason)
        self._progress_since_level = 0

        decision = self._build_decision(
            obs=obs,
            reason_code=target_reason,
            level_before=level_before,
            level_after=new_level,
            count=count,
        )

        # Hysteresis: suppress duplicate (action, reason, tool, args_hash).
        emit_key = (
            decision.action,
            decision.reason_code,
            decision.tool_name,
            decision.args_hash,
        )
        if emit_key == self._last_emitted_key:
            return None
        self._last_emitted_key = emit_key
        return decision

    def _build_decision(
        self,
        *,
        obs: TrajectoryObservation,
        reason_code: str,
        level_before: str,
        level_after: str,
        count: int,
    ) -> TrajectoryQualityDecision:
        explain = _explain(
            reason_code=reason_code,
            tool_name=obs.tool_name,
            count=count,
            config=self.config,
        )
        return TrajectoryQualityDecision(
            action=level_after,
            reason_code=reason_code,
            level_before=level_before,
            level_after=level_after,
            tool_name=obs.tool_name,
            args_hash=obs.args_hash,
            result_hash=obs.result_hash,
            count=count,
            explain=explain,
            model=obs.model,
            provider=obs.provider,
            recommended_model=self.config.stronger_model,
            recommended_provider=self.config.stronger_provider,
        )


# ---------------------------------------------------------------------------
# Explain helper
# ---------------------------------------------------------------------------


def _explain(
    *,
    reason_code: str,
    tool_name: str,
    count: int,
    config: TrajectoryQualityConfig,
) -> str:
    """Build a short operator-facing sentence with no secrets."""
    if reason_code == "two_identical_failures":
        base = f"{tool_name} failed {count}x with identical args_hash"
    elif reason_code == "same_tool_failure_streak":
        base = f"{tool_name} failed {count}x this turn"
    elif reason_code == "failed_verification_streak":
        base = f"{count} consecutive verification failures after edits"
    elif reason_code == "stagnation_no_progress":
        base = f"no verified progress in {count} tool observations"
    else:
        base = f"{tool_name} quality signal ({reason_code}, count={count})"

    model_hint = ""
    if config.stronger_model:
        model_hint = f". Try /model {config.stronger_model} or start a clean session"
    else:
        model_hint = ". Try /model for a stronger model or start a clean session"

    action_word = {
        "recommend_stronger_model": "recommend stronger model",
        "recommend_clean_restart": "recommend clean session restart",
        "stop": "trajectory quality stop",
    }.get(reason_code, reason_code)
    # The action is the level_after, not the reason_code.
    return base


# ---------------------------------------------------------------------------
# Event builder (pure helper)
# ---------------------------------------------------------------------------


def build_observation(
    *,
    tool_name: str,
    args: Mapping[str, Any] | None,
    result: str | None,
    failed: bool,
    verification_status: str | None = None,
    api_call_count: int = 0,
    session_id: str = "",
    model: str = "",
    provider: str = "",
) -> TrajectoryObservation:
    """Build a ``TrajectoryObservation`` from raw tool call data.

    Reuses the canonical hashing from ``tool_guardrails`` so hashes are
    consistent with the loop guardrail subsystem. Never stores raw args
    or results — only their hashes.

    ``progress_kind`` is derived from the result:
    - ``file_mutation_landed`` when a file mutation result proves success
    - ``verification_failed`` / ``verification_passed`` when the caller
      passes ``verification_status``
    - ``none`` otherwise
    """
    from agent.tool_guardrails import ToolCallSignature, _result_hash
    from agent.tool_result_classification import file_mutation_result_landed

    signature = ToolCallSignature.from_call(tool_name, args or {})
    r_hash = _result_hash(result)

    progress_kind = "none"
    if not failed and file_mutation_result_landed(tool_name, result):
        progress_kind = "file_mutation_landed"

    if verification_status == "failed":
        progress_kind = "verification_failed"
    elif verification_status == "passed":
        progress_kind = "verification_passed"

    return TrajectoryObservation(
        tool_name=tool_name,
        args_hash=signature.args_hash,
        result_hash=r_hash,
        failed=failed,
        progress_kind=progress_kind,
        verification_status=verification_status,
        api_call_count=api_call_count,
        session_id=session_id,
        model=model,
        provider=provider,
    )


# ---------------------------------------------------------------------------
# Config parsing helpers (private)
# ---------------------------------------------------------------------------


def _as_bool(value: Any, default: bool) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    if isinstance(value, str):
        lowered = value.strip().lower()
        if lowered in {"1", "true", "yes", "on", "enabled"}:
            return True
        if lowered in {"0", "false", "no", "off", "disabled"}:
            return False
    return default


def _positive_int(value: Any, default: int) -> int:
    if value is None:
        return default
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return default
    return parsed if parsed >= 1 else default


def _opt_str(value: Any) -> str | None:
    if value is None:
        return None
    s = str(value).strip()
    return s or None
