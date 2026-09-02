"""Configurable budget constants for tool result persistence.

Per-tool resolution: pinned > config overrides > registry > default.
"""

from dataclasses import dataclass, field
from typing import Dict

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

# Tighter default per-result threshold for MCP tools (name prefix ``mcp_``).
#
# MCP servers routinely return un-paginated 20-50K-char payloads (tool
# discovery catalogs, batched executions) that sail under the generic 100K
# threshold and silently bloat context — in agentic evals this measurably
# ballooned per-turn reasoning time on long conversations. Competitor
# harnesses cap harder (OpenCode 50KB, pi 50KB, Claude Code 30K chars,
# Codex ~10K tokens); 50K chars keeps parity with the strictest general-
# purpose caps while spillover (unlike truncation) preserves the full
# payload on disk. Overridable via ``tool_output.mcp_result_size_chars``
# in config.yaml.
DEFAULT_MCP_RESULT_SIZE_CHARS: int = 50_000

# Tool-name prefix that identifies MCP-served tools (same prefix the
# untrusted-content wrapper keys on in agent/tool_dispatch_helpers.py).
MCP_TOOL_PREFIX: str = "mcp_"


def _configured_budget_block() -> dict:
    """Return the spillover config, preferring the recognized ``tool_output`` key.

    ``tool_budget`` was the short-lived original spelling. Keep it as a
    read-only compatibility fallback so existing installations do not silently
    lose their MCP threshold after upgrading.
    """
    try:
        from hermes_cli.config import load_config_readonly

        data = load_config_readonly()
        if not isinstance(data, dict):
            return {}
        primary = data.get("tool_output")
        if isinstance(primary, dict):
            return primary
        legacy = data.get("tool_budget")
        return legacy if isinstance(legacy, dict) else {}
    except Exception:
        return {}


def _configured_mcp_result_size() -> int:
    """Read ``tool_output.mcp_result_size_chars`` from active config."""
    try:
        raw = _configured_budget_block().get("mcp_result_size_chars")
        if raw is not None and not isinstance(raw, bool):
            value = int(raw)
            if value > 0:
                return value
    except Exception:
        pass
    return DEFAULT_MCP_RESULT_SIZE_CHARS


def _configured_tool_overrides() -> Dict[str, int]:
    """Read positive per-tool spill thresholds from ``tool_output``.

    Tool handlers and providers do not always keep their documented result
    shape bounded. A named override lets an operator spill a known-chatty tool
    before the generic 100K threshold without shrinking every tool or the
    model's context window. Invalid entries are ignored fail-closed.
    """
    try:
        raw = _configured_budget_block().get("tool_overrides")
        if not isinstance(raw, dict):
            return {}
        overrides: Dict[str, int] = {}
        for name, threshold in raw.items():
            if not isinstance(name, str) or not name.strip():
                continue
            if isinstance(threshold, bool):
                continue
            try:
                value = int(threshold)
            except (TypeError, ValueError):
                continue
            if value > 0:
                overrides[name.strip()] = value
        return overrides
    except Exception:
        return {}


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
    mcp_result_size: int = DEFAULT_MCP_RESULT_SIZE_CHARS
    tool_overrides: Dict[str, int] = field(default_factory=dict)

    def resolve_threshold(self, tool_name: str) -> int | float:
        """Resolve the persistence threshold for a tool.

        Priority: pinned -> tool_overrides -> mcp_ prefix -> registry
        per-tool -> default.

        MCP tools (``mcp_`` prefix) get a tighter default threshold
        (``mcp_result_size``, 50K chars) because MCP servers return
        un-paginated payloads with no per-tool registry entry to constrain
        them. The value is additionally capped at ``default_result_size``
        so a context-scaled budget for a small model still constrains MCP
        results the same way it constrains registry values.

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
        if tool_name.startswith(MCP_TOOL_PREFIX):
            return min(self.mcp_result_size, self.default_result_size)
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
    mcp_result_size = _configured_mcp_result_size()
    tool_overrides = _configured_tool_overrides()

    if not context_length or context_length <= 0:
        if (
            mcp_result_size == DEFAULT_MCP_RESULT_SIZE_CHARS
            and not tool_overrides
        ):
            return DEFAULT_BUDGET
        return BudgetConfig(
            mcp_result_size=mcp_result_size,
            tool_overrides=tool_overrides,
        )

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
        mcp_result_size=mcp_result_size,
        tool_overrides={
            name: min(threshold, per_result)
            for name, threshold in tool_overrides.items()
        },
    )
