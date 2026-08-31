"""One-shot Host-owned recovery for deterministic pre-dispatch faults."""

from __future__ import annotations

import sys
from typing import Any, Callable


def retry_after_runtime_repair(
    operation: Callable[[], Any],
    *,
    phase: str,
    session_id: str,
    task_id: str,
    turn_id: str,
    project_root: str = "",
    error: BaseException | None = None,
    fault: dict[str, Any] | None = None,
) -> Any:
    """Run *operation*, asking the Host to repair one eligible failure once.

    The repair callback owns classification, isolation, verification and
    installation. Hermes only accepts an explicit RETRY directive, reloads
    the active profile plugin, and repeats the untouched operation once.
    """
    try:
        return operation()
    except Exception as caught:
        if error is not None:
            raise
        error = caught

    from hermes_cli.lifecycle import has_hook, invoke_required_hook

    if not has_hook("runtime_fault_repair"):
        raise error
    directive = invoke_required_hook(
        "runtime_fault_repair",
        phase=phase,
        session_id=session_id,
        task_id=task_id,
        turn_id=turn_id,
        project_root=project_root,
        error_type=type(error).__name__,
        error_message=str(error),
        fault=dict(fault or {}),
    )
    if not isinstance(directive, dict) or directive.get("action") != "RETRY":
        raise error

    from hermes_cli.plugins import discover_plugins

    prefixes = directive.get("module_prefixes", [])
    if not isinstance(prefixes, list) or any(
        not isinstance(prefix, str)
        or not prefix
        or prefix.startswith(("agent", "hermes_cli", "tools"))
        for prefix in prefixes
    ):
        raise error
    for prefix in prefixes:
        for name in tuple(sys.modules):
            if name == prefix or name.startswith(f"{prefix}."):
                del sys.modules[name]
    discover_plugins(force=True)
    return operation()


__all__ = ["retry_after_runtime_repair"]
