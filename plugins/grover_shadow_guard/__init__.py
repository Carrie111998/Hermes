"""Mechanical external-effect guard for the clean Grover shadow runtime."""

from __future__ import annotations

import os
from collections.abc import Callable
from functools import partial
from typing import Any

_BLOCK_MESSAGE = "grover-shadow is mechanically external-effect-free"
_EXECUTION_ERROR = {"error": "grover-shadow external effects disabled"}


def _is_shadow_runtime() -> bool:
    return (
        os.environ.get("HERMES_PROFILE", "").strip().lower() == "grover-shadow"
        or os.environ.get("GROVER_RUNTIME_ROLE", "").strip().lower() == "shadow"
    )


def _on_pre_tool_call(
    tool_name: str = "",
    args: Any = None,
    *,
    _force_shadow: bool = False,
    **_: Any,
) -> dict[str, str] | None:
    """Block every tool at the policy boundary in the shadow runtime."""

    del tool_name, args
    if not (_force_shadow or _is_shadow_runtime()):
        return None
    return {"action": "block", "message": _BLOCK_MESSAGE}


def _on_llm_execution(
    request: dict[str, Any],
    next_call: Callable[[dict[str, Any]], Any],
    *,
    _force_shadow: bool = False,
    **_: Any,
) -> Any:
    """Stop provider execution before ``next_call`` in shadow mode."""

    if _force_shadow or _is_shadow_runtime():
        return dict(_EXECUTION_ERROR)
    return next_call(request)


def _on_tool_execution(
    tool_name: str,
    args: dict[str, Any],
    next_call: Callable[[dict[str, Any]], Any],
    *,
    _force_shadow: bool = False,
    **_: Any,
) -> Any:
    """Stop tool execution before ``next_call`` in shadow mode."""

    del tool_name
    if _force_shadow or _is_shadow_runtime():
        return dict(_EXECUTION_ERROR)
    return next_call(args)


def register(context: Any) -> None:
    """Register both the policy hook and behavior-changing execution guards."""

    profile_name = getattr(context, "profile_name", None)
    if not isinstance(profile_name, str) or profile_name.casefold() != "grover-shadow":
        raise RuntimeError("grover-shadow-guard may only be enabled for grover-shadow")

    context.register_hook(
        "pre_tool_call",
        partial(_on_pre_tool_call, _force_shadow=True),
    )
    context.register_middleware(
        "llm_execution",
        partial(_on_llm_execution, _force_shadow=True),
    )
    context.register_middleware(
        "tool_execution",
        partial(_on_tool_execution, _force_shadow=True),
    )
