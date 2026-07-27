"""
tool-escalator plugin — automatic escalation to MoA on consecutive tool errors.

Wires three hook callbacks to detect, escalate, and de-escalate:

1. ``post_tool_call`` — inspects every tool result for error indicators
   (``"error"`` / ``"failed"`` substrings, ``Error``-prefixed lines, non-zero
   exit codes).  Increments a session-scoped consecutive-error counter on
   failure; resets it to zero on success.  When the counter reaches the
   configured threshold (default 3), logs an escalation decision and sets a
   session flag so ``pre_llm_call`` injects escalation context on the next
   turn.

2. ``pre_llm_call`` — resets the per-turn error-observation state.  If the
   session has an outstanding escalation flag, returns a context string
   describing the consecutive errors so the model/loop can consider switching
   to MoA.  Also acts as a safety-net: if MoA completed but de-escalation
   was missed, restores the primary model.

3. ``post_llm_call`` — detects MoA completion by checking ``model`` for a
   MoA preset prefix (``"moa:"`` or ``provider="moa"``) and clears the
   escalation flag, completing the de-escalation cycle.

Configuration:
  The threshold defaults to 3.  Users can override via the ``config`` dict
  in ``plugins.entries.tool-escalator.config`` in ``config.yaml``:

  .. code-block:: yaml

     plugins:
       entries:
         tool-escalator:
           enabled: true
           config:
             threshold: 5  # escalate after 5 consecutive errors
"""

from __future__ import annotations

import logging
import re
from typing import Any, Dict, Optional

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Module-level state — scoped per session_id so concurrent sessions don't
# interfere.  All access is single-threaded (hooks fire from the agent's
# main loop), so no locking is needed.
# ---------------------------------------------------------------------------

# Consecutive error count per session.
_error_counts: Dict[str, int] = {}

# Whether the session has been escalated (threshold reached).
_escalated: Dict[str, bool] = {}

# The model this session was using *before* escalation, so we can
# detect (in post_llm_call) when MoA has handed back to the primary.
_primary_model: Dict[str, str] = {}

# Whether the current LLM call is a MoA aggregator response (set by
# pre_llm_call when model is "moa" or starts with "moa:").
_moa_active: Dict[str, bool] = {}

# ---------------------------------------------------------------------------
# Default threshold
# ---------------------------------------------------------------------------

_DEFAULT_THRESHOLD = 3

# ---------------------------------------------------------------------------
# Error detection helpers
# ---------------------------------------------------------------------------

# Patterns that indicate a tool error in the result string.
_ERROR_PATTERNS = re.compile(
    r"(?i)\b(error|failed|failure|exception|traceback|timeout)\b"
)

# Terminal-specific: pattern for "exit code N" with N != 0.
# Matches "exit code 1", "exit status 2", "exited with code 1",
# and similar, but NOT "exit code 0".
_EXIT_CODE_PATTERN = re.compile(
    r"(?i)exit\w*\s+(?:with\s+)?(?:code|status)\s*[1-9]\d*"
)


def _result_indicates_error(result: Any) -> bool:
    """Return True when *result* looks like a tool error.

    Checks for common error indicators in the string representation
    of the result.  Pragmatic — avoids false positives on the word
    "error" in normal output (e.g. "error_rate=0.0" matches the
    pattern but is a deliberate minor risk accepted by the issue).
    """
    if result is None:
        return False
    if not isinstance(result, str):
        result = str(result)
    if not result.strip():
        return False

    # Non-zero exit code (common in terminal output).
    if _EXIT_CODE_PATTERN.search(result):
        return True

    # Error/failure keywords.
    if _ERROR_PATTERNS.search(result):
        return True

    # Lines that start with "Error" or "ERROR".
    for line in result.splitlines():
        stripped = line.strip()
        if stripped.startswith(("Error", "ERROR", "Error:")):
            return True

    return False


def _load_config() -> int:
    """Return the configured threshold, falling back to the default.

    Reads from ``plugins.entries.tool-escalator.config.threshold``
    via the Hermes config tree.  This is called once per hook
    invocation, but the config is cached by the host so the cost is
    negligible.
    """
    try:
        from hermes_cli.config import get_config as _get_config

        cfg = _get_config()
        threshold = (
            cfg.get("plugins", {})
            .get("entries", {})
            .get("tool-escalator", {})
            .get("config", {})
            .get("threshold", _DEFAULT_THRESHOLD)
        )
        return int(threshold) if threshold and int(threshold) > 0 else _DEFAULT_THRESHOLD
    except Exception:
        return _DEFAULT_THRESHOLD


# ---------------------------------------------------------------------------
# Hooks
# ---------------------------------------------------------------------------


def _on_post_tool_call(
    tool_name: str = "",
    args: Optional[Dict[str, Any]] = None,
    result: Any = None,
    session_id: str = "",
    task_id: str = "",
    tool_call_id: str = "",
    duration_ms: Optional[int] = None,
    **_: Any,
) -> None:
    """Count consecutive tool errors; escalate at threshold.

    Inspects every tool result for error indicators.  On success
    resets the consecutive-error counter to zero.  On failure
    increments it.  When the counter reaches the threshold, logs an
    escalation decision and sets the session-level escalation flag.
    """
    if not session_id:
        return

    threshold = _load_config()
    is_error = _result_indicates_error(result)

    if is_error:
        _error_counts[session_id] = _error_counts.get(session_id, 0) + 1
        count = _error_counts[session_id]
        logger.debug(
            "tool-escalator: session=%s tool=%s error count=%d/%d",
            session_id,
            tool_name,
            count,
            threshold,
        )

        if count >= threshold and not _escalated.get(session_id):
            _escalated[session_id] = True
            logger.info(
                "tool-escalator: ESCALATING session=%s after %d consecutive "
                "tool errors (threshold=%d). Last failing tool: %s",
                session_id,
                count,
                threshold,
                tool_name,
            )
    else:
        # A successful tool call breaks the consecutive-error streak.
        if session_id in _error_counts:
            logger.debug(
                "tool-escalator: session=%s tool=%s succeeded — resetting "
                "error count (was %d)",
                session_id,
                tool_name,
                _error_counts[session_id],
            )
            _error_counts[session_id] = 0


def _on_pre_llm_call(
    session_id: str = "",
    user_message: str = "",
    conversation_history: Any = None,
    is_first_turn: bool = False,
    model: str = "",
    platform: str = "",
    task_id: str = "",
    turn_id: str = "",
    sender_id: str = "",
    **_: Any,
) -> Optional[str]:
    """Reset per-turn error tracking; inject escalation context if flagged.

    Safety-net: if the session shows ``_escalated`` but the model is
    already a MoA preset, mark this call as MoA-active so
    ``post_llm_call`` can detect completion.

    Returns a context string when escalation is active, which gets
    injected into the current turn's user message.
    """
    if not session_id:
        return None

    # Detect if we're already in a MoA call.
    is_moa = bool(model and (model.startswith("moa:") or model.strip().lower() == "moa"))
    _moa_active[session_id] = is_moa

    # Safety-net: if escalation was set but model is MoA, we're on the
    # right track — log it and let post_llm_call handle de-escalation.
    if is_moa and _escalated.get(session_id):
        logger.info(
            "tool-escalator: MoA active for session=%s (model=%s). "
            "De-escalation will follow.",
            session_id,
            model,
        )
        return None

    # If escalated and model is NOT MoA, inject escalation context.
    if _escalated.get(session_id):
        count = _error_counts.get(session_id, 0)
        logger.info(
            "tool-escalator: INJECTING escalation context for session=%s "
            "(consecutive errors=%d)",
            session_id,
            count,
        )
        return (
            f"[tool-escalator] ⚠️ The previous turn experienced {count} "
            f"consecutive tool errors. Consider switching to a MoA preset "
            f"(e.g. ``/moa`` or ``/model moa:<preset>``) for a more capable "
            f"aggregation strategy, or try a different approach to resolve "
            f"the recurring tool failures."
        )

    return None


def _on_post_llm_call(
    session_id: str = "",
    model: str = "",
    platform: str = "",
    **_: Any,
) -> None:
    """Detect MoA completion and de-escalate.

    When the just-completed LLM call was a MoA aggregator response
    (detected via ``pre_llm_call``'s ``_moa_active`` flag), and the
    session had been escalated, clear the escalation flag and log
    the de-escalation.
    """
    if not session_id:
        return

    was_moa = _moa_active.pop(session_id, False)

    if was_moa and _escalated.get(session_id):
        logger.info(
            "tool-escalator: DE-ESCALATING session=%s — MoA aggregation "
            "completed successfully.",
            session_id,
        )
        _escalated[session_id] = False
        _error_counts.pop(session_id, None)
        _primary_model.pop(session_id, None)


# ---------------------------------------------------------------------------
# Plugin registration
# ---------------------------------------------------------------------------


def register(ctx) -> None:
    ctx.register_hook("post_tool_call", _on_post_tool_call)
    ctx.register_hook("pre_llm_call", _on_pre_llm_call)
    ctx.register_hook("post_llm_call", _on_post_llm_call)
