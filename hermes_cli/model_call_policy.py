"""Fail-closed plugin policy for concrete model-provider attempts.

The agent loop and the auxiliary client both call this module at their final
provider callback.  Keeping resolution and payload shaping here gives policy
plugins one contract without making either execution loop own plugin-policy
semantics.
"""

from __future__ import annotations

import json
import logging
from typing import Any, Dict, Iterable, Mapping
from urllib.parse import urlsplit

logger = logging.getLogger(__name__)

PRE_TOOL_CALL_POLICY_FAILURE_MESSAGE = (
    "pre_tool_call policy could not be evaluated safely"
)
PRE_MODEL_CALL_POLICY_FAILURE_MESSAGE = (
    "Model request denied because a pre-model policy did not complete safely."
)

_ACTION_RANK = {"allow": 0, "pause": 1, "deny": 2}
_RESULT_FIELDS = frozenset({"action", "message"})


class ModelCallPolicyHalt(BaseException):
    """Internal control flow that bypasses provider retry/fallback handlers."""

    def __init__(self, action: str, message: str) -> None:
        self.action = action
        self.message = message
        super().__init__(message)


class ModelCallPolicyDenied(RuntimeError):
    """Public auxiliary-call error produced by a policy pause or denial."""

    def __init__(self, action: str, message: str) -> None:
        self.action = action
        self.message = message
        super().__init__(message)


def fail_closed_hook_result(hook_name: str) -> Dict[str, str]:
    """Return the hook-specific directive for an unsafe policy failure."""
    if hook_name == "pre_model_call_policy":
        return {
            "action": "deny",
            "message": PRE_MODEL_CALL_POLICY_FAILURE_MESSAGE,
        }
    return {
        "action": "block",
        "message": "pre_tool_call plugin callback timed out or is still running",
    }


def resolve_pre_model_call_policy(**payload: Any) -> Dict[str, str]:
    """Resolve all registered policy callbacks for one provider attempt.

    ``None`` is no opinion.  Every non-``None`` result is validated completely
    before precedence is applied, so a malformed low-priority result cannot be
    hidden by an earlier valid pause or denial.
    """
    try:
        from hermes_cli import lifecycle

        if not lifecycle.has_hook("pre_model_call_policy"):
            return {"action": "allow", "message": ""}
        results = lifecycle.invoke_hook("pre_model_call_policy", **payload)
    except Exception:
        logger.warning("pre_model_call_policy dispatch failed", exc_info=True)
        return fail_closed_hook_result("pre_model_call_policy")

    return _resolve_policy_results(results)


def _resolve_policy_results(results: Iterable[Any]) -> Dict[str, str]:
    validated: list[Dict[str, str]] = []
    for result in results:
        if result is None:
            continue
        if not isinstance(result, dict) or not set(result).issubset(_RESULT_FIELDS):
            return fail_closed_hook_result("pre_model_call_policy")

        raw_action = result.get("action")
        if not isinstance(raw_action, str):
            return fail_closed_hook_result("pre_model_call_policy")
        action = raw_action.strip().lower()
        if action not in _ACTION_RANK:
            return fail_closed_hook_result("pre_model_call_policy")

        raw_message = result.get("message")
        if raw_message is not None and not isinstance(raw_message, str):
            return fail_closed_hook_result("pre_model_call_policy")
        validated.append(
            {
                "action": action,
                "message": (raw_message or "").strip()[:500],
            }
        )

    decision = {"action": "allow", "message": ""}
    for result in validated:
        if _ACTION_RANK[result["action"]] > _ACTION_RANK[decision["action"]]:
            decision = result

    if decision["action"] != "allow" and not decision["message"]:
        decision["message"] = (
            "Model request paused by policy."
            if decision["action"] == "pause"
            else "Model request denied by policy."
        )
    return decision


def enforce_pre_model_call_policy(
    request: Mapping[str, Any],
    **context: Any,
) -> Dict[str, str]:
    """Authorize one final request or halt before provider I/O."""
    try:
        from hermes_cli import lifecycle

        has_policy = lifecycle.has_hook("pre_model_call_policy")
    except Exception:
        logger.warning("Unable to inspect pre_model_call_policy", exc_info=True)
        decision = fail_closed_hook_result("pre_model_call_policy")
        raise ModelCallPolicyHalt(
            decision["action"], decision["message"]
        ) from None

    if not has_policy:
        return {"action": "allow", "message": ""}

    payload = _policy_payload(request, **context)
    try:
        results = lifecycle.invoke_hook("pre_model_call_policy", **payload)
    except Exception:
        logger.warning("pre_model_call_policy dispatch failed", exc_info=True)
        decision = fail_closed_hook_result("pre_model_call_policy")
    else:
        decision = _resolve_policy_results(results)

    if decision["action"] != "allow":
        raise ModelCallPolicyHalt(
            decision["action"], decision["message"]
        ) from None
    return decision


def _policy_payload(
    request: Mapping[str, Any],
    **context: Any,
) -> Dict[str, Any]:
    messages = request.get("messages")
    if not isinstance(messages, list):
        messages = request.get("input")
    if not isinstance(messages, list):
        messages = []

    try:
        message_utf8_bytes = len(
            json.dumps(
                messages,
                ensure_ascii=False,
                separators=(",", ":"),
                default=str,
            ).encode("utf-8")
        )
    except Exception:
        message_utf8_bytes = -1

    approx_input_tokens = context.pop("approx_input_tokens", None)
    if not isinstance(approx_input_tokens, int) or isinstance(
        approx_input_tokens, bool
    ):
        approx_input_tokens = -1

    max_output_tokens = _request_output_cap(request)
    payload: Dict[str, Any] = {
        "policy_schema_version": 1,
        "call_kind": str(context.pop("call_kind", "conversation") or "conversation"),
        "task_id": str(context.pop("task_id", "") or ""),
        "mission_id": str(context.pop("mission_id", "") or ""),
        "auxiliary_task": str(context.pop("auxiliary_task", "") or ""),
        "profile": str(context.pop("profile", "") or ""),
        "session_id": str(context.pop("session_id", "") or ""),
        "turn_id": str(context.pop("turn_id", "") or ""),
        "api_request_id": str(context.pop("api_request_id", "") or ""),
        "call_seq": _non_negative_int(context.pop("call_seq", 0)),
        "request_attempt": _non_negative_int(
            context.pop("request_attempt", 0)
        ),
        "platform": str(context.pop("platform", "") or ""),
        "model": str(
            request.get("model") or context.pop("model", "") or ""
        ),
        "provider": str(context.pop("provider", "") or ""),
        "base_url_host": _base_url_host(context.pop("base_url", "")),
        "api_mode": str(context.pop("api_mode", "") or ""),
        "message_count": len(messages),
        "message_utf8_bytes": message_utf8_bytes,
        "approx_input_tokens": approx_input_tokens,
        "max_output_tokens": max_output_tokens,
        "middleware_trace": list(context.pop("middleware_trace", []) or []),
    }
    return payload


def _request_output_cap(request: Mapping[str, Any]) -> int:
    for key in ("max_output_tokens", "max_completion_tokens", "max_tokens"):
        value = request.get(key)
        if isinstance(value, int) and not isinstance(value, bool) and value >= 0:
            return value
    return -1


def _non_negative_int(value: Any) -> int:
    if isinstance(value, int) and not isinstance(value, bool) and value >= 0:
        return value
    return 0


def _base_url_host(value: Any) -> str:
    raw = str(value or "").strip()
    if not raw:
        return ""
    try:
        return str(urlsplit(raw).hostname or "")
    except Exception:
        return ""


def dispatch_pre_tool_call_fail_closed(
    *args: Any,
    **kwargs: Any,
) -> tuple[str | None, Dict[str, Any] | None]:
    """Dispatch the existing tool policy and block on dispatcher failure."""
    try:
        from hermes_cli.plugins import _dispatch_pre_tool_call_hooks

        return _dispatch_pre_tool_call_hooks(*args, **kwargs)
    except Exception:
        logger.warning("pre_tool_call policy dispatch failed closed", exc_info=True)
        return PRE_TOOL_CALL_POLICY_FAILURE_MESSAGE, None
