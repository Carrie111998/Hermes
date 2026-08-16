"""Configurable budget constants for tool result persistence.

Per-tool resolution: pinned > config overrides > registry > default.
"""

import re
from dataclasses import dataclass, field, replace
from typing import Dict

# Strict whitelist for explicit persist-threshold strings: optional sign
# followed by digits only. "20.5", "1e4", "" etc. are rejected up front so the
# only strings that reach int() are guaranteed whole numbers.
_INT_STR_PATTERN = re.compile(r"^[+-]?\d+$")

# Tools whose thresholds must never be overridden.
# read_file=inf prevents infinite persist->read->persist loops.
PINNED_THRESHOLDS: Dict[str, float] = {
    "read_file": float("inf"),
}

# Defaults matching the current hardcoded values in tool_result_storage.py.
# Kept here as the single source of truth; tool_result_storage.py imports these.
DEFAULT_RESULT_SIZE_CHARS: int = 100_000
DEFAULT_TURN_BUDGET_CHARS: int = 200_000
DEFAULT_PREVIEW_SIZE_CHARS: int = 1_500


@dataclass(frozen=True)
class BudgetConfig:
    """Immutable budget constants for the 3-layer tool result persistence system.

    Layer 2 (per-result): resolve_threshold(tool_name) -> threshold in chars.
    Layer 3 (per-turn):   turn_budget -> aggregate char budget across all tool
                          results in a single assistant turn.
    Preview:              preview_size -> inline snippet size after persistence.
    """

    default_result_size: int = DEFAULT_RESULT_SIZE_CHARS
    turn_budget: int = DEFAULT_TURN_BUDGET_CHARS
    preview_size: int = DEFAULT_PREVIEW_SIZE_CHARS
    tool_overrides: Dict[str, int] = field(default_factory=dict)

    def resolve_threshold(self, tool_name: str) -> int | float:
        """Resolve the persistence threshold for a tool.

        Priority: pinned -> tool_overrides -> registry per-tool -> default.

        The registry per-tool value is capped at ``default_result_size`` so a
        context-scaled budget (small model) actually constrains tools that
        register a large fixed ``max_result_size_chars`` (web/terminal/x_search
        all register 100K). For the default budget this is a no-op because both
        equal 100K; for a scaled-down budget it prevents a per-tool registry
        value from re-inflating the cap past the model's window (#23767).
        """
        if tool_name in PINNED_THRESHOLDS:
            return PINNED_THRESHOLDS[tool_name]
        if tool_name in self.tool_overrides:
            return self.tool_overrides[tool_name]
        from tools.registry import registry
        registry_value = registry.get_max_result_size(tool_name, default=self.default_result_size)
        if registry_value == float("inf"):
            return registry_value
        return min(registry_value, self.default_result_size)


# Default config -- matches current hardcoded behavior exactly.
DEFAULT_BUDGET = BudgetConfig()


# Token<->char conversion used when scaling the budget to a model's context
# window. Deliberately conservative (a smaller divisor = more chars per token =
# a larger char budget) would UNDER-protect small models, so we use the same
# rough 4-chars-per-token ratio the estimator uses (agent/model_metadata.py).
_CHARS_PER_TOKEN: int = 4

# Fraction of a model's context window we allow a SINGLE tool result to occupy
# before persisting/truncating it, and the fraction the WHOLE turn's tool
# output may occupy. Tool output is not the only thing in the window (system
# prompt, tool schemas, conversation history, the model's own reply all
# compete), so these stay well under 1.0.
_PER_RESULT_WINDOW_FRACTION: float = 0.15
_PER_TURN_WINDOW_FRACTION: float = 0.30

# Floor so even a tiny-but-admitted model still gets a usable preview/result
# rather than a 0-char budget.
_MIN_RESULT_SIZE_CHARS: int = 8_000
_MIN_TURN_BUDGET_CHARS: int = 16_000


def budget_for_context_window(context_length: int | None) -> BudgetConfig:
    """Return a BudgetConfig scaled to the active model's context window.

    The fixed defaults (100K result / 200K turn chars) are correct for large
    (200K+ token) models but blind to small ones: on a 65K-token model a single
    tool result persisted at the 100K-char threshold, or a 200K-char turn
    budget (~50K tokens), can by itself approach or exceed the whole window and
    force an oversized request (#23767).

    Scaling keeps large models byte-identical to today (the proportional value
    is clamped to the existing defaults as a CAP) while shrinking the budget for
    small models proportionally to their window, floored so a usable preview
    always survives.
    """
    if not context_length or context_length <= 0:
        return DEFAULT_BUDGET

    window_chars = context_length * _CHARS_PER_TOKEN
    per_result = int(window_chars * _PER_RESULT_WINDOW_FRACTION)
    per_turn = int(window_chars * _PER_TURN_WINDOW_FRACTION)

    # Clamp: never exceed the historical defaults (so large models are
    # unchanged), never drop below the floor (so tiny models stay usable).
    per_result = max(_MIN_RESULT_SIZE_CHARS, min(per_result, DEFAULT_RESULT_SIZE_CHARS))
    per_turn = max(_MIN_TURN_BUDGET_CHARS, min(per_turn, DEFAULT_TURN_BUDGET_CHARS))

    return BudgetConfig(
        default_result_size=per_result,
        turn_budget=per_turn,
        preview_size=DEFAULT_PREVIEW_SIZE_CHARS,
    )


def normalize_persist_threshold(value) -> int | None:
    """Validate and normalize the explicit persist-threshold config value.

    Single source of truth for ``tools.tool_result_persist_threshold_chars``:
    ``agent_init`` (config parsing), ``budget_with_persist_threshold`` (factory)
    and ``_budget_for_agent`` (programmatic access) all funnel through here so
    the accept/reject rules cannot drift between layers.

    Strict type whitelist -- only non-``bool`` ``int`` and whole-number
    strings are accepted:

    - ``None`` (unset) -> ``None``
    - ``bool`` -> ``None`` -- bool is an int subclass, so ``int(True) == 1``
      would silently persist almost every tool result; booleans are rejected
      even when set programmatically (plugins/tests), not just via YAML
    - other numeric types (``float``, ``Decimal``, ``Fraction``) -> ``None``
      -- ``int(1.5) == 1``, ``int(Decimal("1.5")) == 1`` and
      ``int(Fraction(3, 2)) == 1`` all silently truncate a fractional value
      into a near-universal persist threshold; only ints and whole-number
      strings are accepted
    - ``bytes`` and arbitrary objects -> ``None`` -- no coercion
    - positive ``int`` -> itself; whole-number ``str`` -> parsed ``int``
    - non-positive values (0/negative) -> ``None`` ("unset")
    """
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, int):
        n = value
    elif isinstance(value, str):
        if not _INT_STR_PATTERN.match(value.strip()):
            return None
        try:
            n = int(value)
        except ValueError:
            return None
    else:
        return None
    return n if n > 0 else None


def budget_with_persist_threshold(
    threshold_chars: int, context_length: int | None = None
) -> BudgetConfig:
    """Return a BudgetConfig with an explicit per-result persistence cap.

    User-configured ``tools.tool_result_persist_threshold_chars`` path: the
    explicit value overrides ONLY the per-result cap (``default_result_size``);
    the turn budget and preview size keep the context-scaled values from
    ``budget_for_context_window`` (falling back to the historical defaults when
    ``context_length`` is unknown), so a small model keeps its small-window
    turn-budget protection instead of being reset to the 200K default.

    Non-positive, ``None``, boolean, float/Decimal/Fraction, ``bytes`` or
    unconvertible values are treated as "unset" (via
    ``normalize_persist_threshold``) and return the context-scaled budget
    unchanged (a 0/negative value must NOT clamp to 1, which would
    persist almost every tool result). ``resolve_threshold`` still applies
    ``PINNED_THRESHOLDS`` (read_file stays exempt) and the per-tool registry
    cap, so a smaller explicit value always wins while a larger one is bounded
    by what the tool itself registers.
    """
    base = budget_for_context_window(context_length)
    normalized = normalize_persist_threshold(threshold_chars)
    if normalized is None:
        return base
    return replace(base, default_result_size=normalized)
