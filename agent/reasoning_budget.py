"""Per-turn reasoning-output budget accounting."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal, Mapping

from agent.model_metadata import estimate_tokens_rough


@dataclass(frozen=True)
class ReasoningBudgetConfig:
    """Configuration for reasoning-output warnings."""

    warn_after_tokens: int = 100_000
    context_ratio: float = 8.0
    ratio_min_tokens: int = 8_192
    nudge_next_turn: bool = False

    @classmethod
    def from_mapping(cls, values: Mapping[str, Any] | None) -> "ReasoningBudgetConfig":
        values = values if isinstance(values, Mapping) else {}
        defaults = cls()

        def _number(key: str, default: int | float, cast):
            try:
                return max(0, cast(values.get(key, default)))
            except (TypeError, ValueError):
                return default

        raw_nudge = values.get("reasoning_warn_nudge", defaults.nudge_next_turn)
        if isinstance(raw_nudge, str):
            nudge = raw_nudge.strip().lower() in {"1", "true", "yes", "on"}
        else:
            nudge = bool(raw_nudge)
        return cls(
            warn_after_tokens=_number(
                "reasoning_warn_after_tokens", defaults.warn_after_tokens, int
            ),
            context_ratio=_number(
                "reasoning_warn_context_ratio", defaults.context_ratio, float
            ),
            ratio_min_tokens=_number(
                "reasoning_warn_ratio_min_tokens", defaults.ratio_min_tokens, int
            ),
            nudge_next_turn=nudge,
        )


@dataclass(frozen=True)
class ReasoningBudgetWarning:
    """A newly crossed reasoning-budget threshold."""

    reason: Literal["absolute", "ratio"]
    reasoning_tokens: int
    prompt_tokens: int


class ReasoningBudgetTracker:
    """Estimate streamed reasoning tokens and emit at most once per turn."""

    _CHUNK_SIZE = 256

    def __init__(self, config: ReasoningBudgetConfig) -> None:
        self.config = config
        self._estimated_tokens = 0
        self._pending_text = ""
        self.prompt_tokens = 0
        self.warned = False

    @property
    def reasoning_tokens(self) -> int:
        return self._estimated_tokens + estimate_tokens_rough(self._pending_text)

    def set_prompt_tokens(self, prompt_tokens: int) -> None:
        self.prompt_tokens = max(0, int(prompt_tokens or 0))

    def reset_turn(self) -> None:
        self._estimated_tokens = 0
        self._pending_text = ""
        self.prompt_tokens = 0
        self.warned = False

    def add_delta(self, text: str) -> ReasoningBudgetWarning | None:
        if (
            self.config.warn_after_tokens <= 0
            or self.warned
            or not isinstance(text, str)
            or not text
        ):
            return None
        self._pending_text += text
        while len(self._pending_text) >= self._CHUNK_SIZE:
            chunk = self._pending_text[: self._CHUNK_SIZE]
            self._pending_text = self._pending_text[self._CHUNK_SIZE :]
            self._estimated_tokens += estimate_tokens_rough(chunk)

        tokens = self.reasoning_tokens
        if self.config.warn_after_tokens > 0 and tokens >= self.config.warn_after_tokens:
            self.warned = True
            return ReasoningBudgetWarning(
                reason="absolute",
                reasoning_tokens=tokens,
                prompt_tokens=self.prompt_tokens,
            )
        if (
            self.config.context_ratio > 0
            and self.prompt_tokens > 0
            and tokens >= self.config.ratio_min_tokens
            and tokens >= self.prompt_tokens * self.config.context_ratio
        ):
            self.warned = True
            return ReasoningBudgetWarning(
                reason="ratio",
                reasoning_tokens=tokens,
                prompt_tokens=self.prompt_tokens,
            )
        return None


_CONCLUSION_NUDGE = (
    "[System note: The previous model response used an unusually large "
    "reasoning budget. Conclude directly or make the planned tool call now; "
    "do not repeat prior analysis.]"
)


def _tracker_for(agent: Any) -> ReasoningBudgetTracker:
    tracker = getattr(agent, "_reasoning_budget_tracker", None)
    if isinstance(tracker, ReasoningBudgetTracker):
        return tracker
    try:
        from hermes_cli.config import load_config_readonly

        raw_config = load_config_readonly()
        agent_config = raw_config.get("agent", {}) if isinstance(raw_config, Mapping) else {}
    except Exception:
        agent_config = {}
    tracker = ReasoningBudgetTracker(ReasoningBudgetConfig.from_mapping(agent_config))
    agent._reasoning_budget_tracker = tracker
    return tracker


def set_reasoning_prompt_tokens(agent: Any, prompt_tokens: int) -> None:
    """Record the current request size for the ratio threshold."""
    _tracker_for(agent).set_prompt_tokens(prompt_tokens)


def track_reasoning_delta(agent: Any, text: str) -> str | None:
    """Account for a reasoning delta and return a new warning message, if any."""
    tracker = _tracker_for(agent)
    warning = tracker.add_delta(text)
    if warning is None:
        return None
    if tracker.config.nudge_next_turn:
        agent._pending_reasoning_budget_nudge = True
    context = ""
    if warning.prompt_tokens > 0:
        ratio = warning.reasoning_tokens / warning.prompt_tokens
        context = (
            f" ({ratio:.1f}x the current ~{warning.prompt_tokens:,}-token "
            "request context)"
        )
    return (
        "⚠️ Reasoning budget warning: the model has produced "
        f"~{warning.reasoning_tokens:,} reasoning tokens this turn{context}. "
        "Consider interrupting if it is not making progress."
    )


def begin_reasoning_budget_turn(agent: Any) -> str:
    """Reset turn accounting and consume any staged one-shot nudge."""
    pending_nudge = bool(getattr(agent, "_pending_reasoning_budget_nudge", False))
    agent._pending_reasoning_budget_nudge = False
    tracker = getattr(agent, "_reasoning_budget_tracker", None)
    if isinstance(tracker, ReasoningBudgetTracker):
        tracker.reset_turn()
    return _CONCLUSION_NUDGE if pending_nudge else ""
