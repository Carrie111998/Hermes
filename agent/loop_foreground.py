"""Policy for explicit foreground Loop routing.

The policy is deliberately separate from prompt construction and provider
adapters.  It reads the canonical Hermes reasoning intent before any adapter
normalizes it for a wire protocol, so a provider's supported effort levels do
not change whether an eligible foreground turn is routed through Loop.
"""
from __future__ import annotations

from dataclasses import dataclass
import json
import os
import re
from typing import Any, Mapping


_REQUIRED_LOOP_TOOLS = frozenset(
    {"delegate_task", "kanban_show", "kanban_complete", "kanban_comment"}
)


@dataclass(frozen=True)
class ForegroundLoopDecision:
    """Pure decision record used by the conversation loop and tests."""

    route: bool
    reason: str


def _config_value(config: Mapping[str, Any] | None, key: str, default: Any) -> Any:
    loop = config.get("loop") if isinstance(config, Mapping) else None
    return loop.get(key, default) if isinstance(loop, Mapping) else default


def _is_opt_out(text: str) -> bool:
    normalized = text.lower()
    return bool(
        re.search(
            r"(?:^|\b)(?:no|skip|avoid|without|don't use|do not use)\s+(?:a\s+)?(?:durable\s+)?loop\b",
            normalized,
        )
        or "no-loop" in normalized
    )


def _is_clarification_dependency(text: str) -> bool:
    normalized = text.lower().strip()
    return bool(
        re.search(r"\b(?:clarify|clarification|need your input|needs? input|waiting on|blocked by)\b", normalized)
        or re.search(r"\bwhich\s+(?:option|one|approach)\b", normalized)
        or (normalized.endswith("?") and re.search(r"\b(?:should|can you confirm|what do you mean)\b", normalized))
    )


def _is_trivial_or_single_step(text: str) -> bool:
    normalized = text.lower().strip()
    has_multiple_actions = bool(
        re.search(
            r"\b(?:and|then|also|plus|verify|implement|fix|update|deploy|create|review)\b",
            normalized,
        )
    )
    if not normalized:
        return True
    if re.fullmatch(
        r"(?:hi|hello|hey|thanks|thank you|ok|okay|good morning|good night|yo|ping|status)\s*[!.?]*",
        normalized,
    ):
        return True
    if (
        re.search(r"\b(?:quick|just|simply|only)\b", normalized)
        and not has_multiple_actions
        and len(normalized.split()) <= 24
    ):
        return True
    if re.match(
        r"^(?:what is|who is|when is|where is|how much is|calculate|convert|translate|define|look up|search for|check the status of|read|open|show|list|find|run|execute|inspect)\b",
        normalized,
    ) and len(normalized.split()) <= 24:
        return not has_multiple_actions
    return False


def _is_internal_wake(agent: Any) -> bool:
    """Recognize boundary/completion turns without changing prompt bytes."""
    if any(
        os.environ.get(name)
        for name in ("HERMES_INTERNAL_WAKE", "HERMES_KANBAN_WAKE", "HERMES_WORKFLOW_WAKE")
    ):
        return True
    if bool(getattr(agent, "_internal_wake", False)):
        return True
    try:
        from gateway.session_context import get_current_workflow_id

        # Workflow context is bound for watcher-delivered completion/boundary
        # turns.  A root turn has no workflow identity until Loop creates one.
        return bool(get_current_workflow_id())
    except Exception:
        return False


def _is_delegated_child() -> bool:
    try:
        from agent.delegation_context import is_delegated_child_context

        return bool(is_delegated_child_context())
    except Exception:
        return False


def decide_foreground_loop_route(
    agent: Any,
    user_message: Any,
    *,
    config: Mapping[str, Any] | None = None,
) -> ForegroundLoopDecision:
    """Return whether this turn must begin with a Loop-root delegation.

    This is intentionally fail-closed for missing capability and recursion
    signals.  No prompt or conversation history is mutated by this helper.
    """
    text = user_message if isinstance(user_message, str) else str(user_message or "")
    if config is None:
        try:
            from hermes_cli.config import load_config_readonly

            config = load_config_readonly()
        except Exception:
            return ForegroundLoopDecision(False, "config_unavailable")

    if _config_value(config, "enabled", True) is False:
        return ForegroundLoopDecision(False, "loop_disabled")
    if str(_config_value(config, "foreground_routing", "ultra") or "").strip().lower() != "ultra":
        return ForegroundLoopDecision(False, "foreground_routing_disabled")

    reasoning = getattr(agent, "reasoning_config", None)
    if not isinstance(reasoning, Mapping) or reasoning.get("enabled") is False:
        return ForegroundLoopDecision(False, "reasoning_disabled")
    if str(reasoning.get("effort") or "").strip().lower() != "ultra":
        return ForegroundLoopDecision(False, "effort_not_ultra")

    if os.environ.get("HERMES_KANBAN_TASK"):
        return ForegroundLoopDecision(False, "dispatcher_worker")
    if _is_delegated_child():
        return ForegroundLoopDecision(False, "delegated_child")
    if _is_internal_wake(agent):
        return ForegroundLoopDecision(False, "internal_wake_or_active_workflow")
    if bool(getattr(agent, "_foreground_loop_routed", False)):
        return ForegroundLoopDecision(False, "already_routed")

    valid_tools = set(getattr(agent, "valid_tool_names", ()) or ())
    if not _REQUIRED_LOOP_TOOLS.issubset(valid_tools):
        return ForegroundLoopDecision(False, "loop_tools_unavailable")
    if _is_opt_out(text):
        return ForegroundLoopDecision(False, "request_opt_out")
    if _is_clarification_dependency(text):
        return ForegroundLoopDecision(False, "clarification_dependency")
    if _is_trivial_or_single_step(text):
        return ForegroundLoopDecision(False, "trivial_or_single_step")

    return ForegroundLoopDecision(True, "canonical_ultra_substantive_foreground_request")


def foreground_loop_tool_choice(agent: Any) -> dict[str, Any] | str | None:
    """Return the provider-native named tool choice for the first Loop round."""
    if getattr(agent, "api_mode", "chat_completions") == "anthropic_messages":
        # ``build_api_kwargs`` normally translates the OpenAI-style string
        # into this shape.  The foreground guard is applied after that builder
        # returns, so it must use the already-native Anthropic form here.
        return {"type": "tool", "name": "delegate_task"}
    if getattr(agent, "api_mode", "chat_completions") == "chat_completions":
        return {"type": "function", "function": {"name": "delegate_task"}}
    return None


def validate_loop_plan_arguments(arguments: Any) -> tuple[bool, str]:
    """Validate the minimum plan packet before accepting the first tool round."""
    def _context_is_complete(value: Any) -> bool:
        context = str(value or "").strip().lower()
        return bool(
            context
            and re.search(r"\b(?:boundary|boundaries|scope|out of scope)\b", context)
            and re.search(
                r"\b(?:acceptance criteria|acceptance|done when|verification|tests?)\b",
                context,
            )
        )

    if not isinstance(arguments, Mapping):
        return False, "delegate_task arguments must be an object"
    mode = str(arguments.get("mode") or "").strip().lower()
    if mode != "loop":
        return False, "the first foreground plan must call delegate_task with mode='loop'"
    if arguments.get("goal"):
        if not _context_is_complete(arguments.get("context")):
            return False, "the Loop plan must include context with boundaries and acceptance criteria"
        return True, ""
    tasks = arguments.get("tasks")
    if not isinstance(tasks, list) or not tasks:
        return False, "the Loop plan must include a goal or non-empty tasks list"
    for index, task in enumerate(tasks):
        if not isinstance(task, Mapping) or not str(task.get("goal") or task.get("title") or "").strip():
            return False, f"Loop plan task {index} is missing a goal/title"
        if not _context_is_complete(task.get("context") or arguments.get("context")):
            return False, "the Loop plan must include context with boundaries and acceptance criteria"
    return True, ""


def enforce_foreground_loop_plan(agent: Any, user_message: Any) -> tuple[bool, str]:
    """Create the one required foreground Loop boundary.

    The normal Hermes conversation loop enforces this boundary by requiring
    the first model round to call ``delegate_task(mode="loop")``.  The Codex
    app-server runtime does not pass through that model-tool loop, so it must
    use the same foreground tool directly before handing the turn to Codex.
    This keeps the policy model-agnostic and fail-closed without downgrading
    or silently bypassing the selected app-server runtime.
    """
    if getattr(agent, "_foreground_loop_plan_attempted", False):
        return False, "foreground Loop planning was already attempted for this turn"
    agent._foreground_loop_plan_attempted = True

    text = user_message if isinstance(user_message, str) else str(user_message or "")
    context = (
        "Foreground Loop planning boundary. The durable Loop owns task "
        "decomposition, dependency routing, and worker execution. Boundaries: "
        "the requested work only; do not create another Loop graph or recurse "
        "into delegation. Acceptance criteria: the requested outcome is "
        "verified and the originating foreground session receives the completed "
        "evidence for review."
    )
    try:
        from tools.delegate_tool import delegate_task

        raw_result = delegate_task(
            goal=text,
            context=context,
            mode="loop",
            parent_agent=agent,
        )
    except Exception:
        return False, "foreground Loop planning could not be invoked safely"

    try:
        result = raw_result if isinstance(raw_result, Mapping) else json.loads(raw_result)
    except (TypeError, ValueError):
        return False, "foreground Loop planning returned an invalid result"
    if not isinstance(result, Mapping):
        return False, "foreground Loop planning returned an invalid result"
    if result.get("status") != "dispatched" or result.get("mode") != "loop":
        return False, "foreground Loop planning was rejected before dispatch"
    if not result.get("workflow_id"):
        return False, "foreground Loop planning returned no workflow identity"

    agent._foreground_loop_routed = True
    return True, ""


__all__ = [
    "ForegroundLoopDecision",
    "decide_foreground_loop_route",
    "foreground_loop_tool_choice",
    "validate_loop_plan_arguments",
    "enforce_foreground_loop_plan",
]
