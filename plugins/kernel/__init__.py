"""kernel: MershLab's own audit-invariant plugin.

Observes every outgoing model call via Hermes's real pre_api_request /
post_api_request hooks (hermes_cli/plugins.py VALID_HOOKS) and records a
provenance-shaped, append-only log entry for each. See kernel.py's module
docstring for exactly what this checks, what it deliberately doesn't
check yet, and why it can detect but not block a call — that scoping is
load-bearing, not an implementation detail to skim.
"""
from __future__ import annotations

import logging
import sys

from hermes_constants import get_hermes_home

from .kernel import KernelEvent, append_event, check_continuity, content_hash

logger = logging.getLogger(__name__)


def _log_path() -> str:
    return str(get_hermes_home() / "kernel" / "events.jsonl")


def _on_pre_api_request(**kwargs) -> None:
    session_id = kwargs.get("session_id") or ""
    api_request_id = kwargs.get("api_request_id") or ""
    request_messages = kwargs.get("request_messages") or []
    message_count = kwargs.get("message_count", len(request_messages))
    log_path = _log_path()

    coverage_state, violation = check_continuity(session_id, message_count, log_path)

    event = KernelEvent(
        kind="api_request",
        session_id=session_id,
        api_request_id=api_request_id,
        model=kwargs.get("model") or "",
        provider=kwargs.get("provider") or "",
        message_count=message_count,
        request_hash=content_hash(request_messages),
        coverage_state=coverage_state,
    )
    _append(log_path, event)

    if violation is not None:
        violation_event = KernelEvent(
            kind="kernel_violation",
            session_id=session_id,
            api_request_id=api_request_id,
            model=kwargs.get("model") or "",
            provider=kwargs.get("provider") or "",
            message_count=message_count,
            coverage_state=coverage_state,
            detail=violation,
        )
        _append(log_path, violation_event)
        message = (
            f"kernel: message history shrank in session {session_id!r} "
            f"(api_request_id={api_request_id!r}): "
            f"{violation['prior_message_count']} -> {violation['current_message_count']} "
            f"since the previous call ({violation['prior_api_request_id']!r})"
        )
        logger.warning(message)
        print(f"\n\033[91m[kernel] {message}\033[0m\n", file=sys.stderr)


def _on_post_api_request(**kwargs) -> None:
    session_id = kwargs.get("session_id") or ""
    api_request_id = kwargs.get("api_request_id") or ""
    response = kwargs.get("response")

    event = KernelEvent(
        kind="api_response",
        session_id=session_id,
        api_request_id=api_request_id,
        model=kwargs.get("response_model") or kwargs.get("model") or "",
        provider=kwargs.get("provider") or "",
        message_count=kwargs.get("message_count", 0),
        response_hash=content_hash(response) if response is not None else "",
        coverage_state="unknown",
    )
    _append(_log_path(), event)


def _append(log_path: str, event: KernelEvent) -> None:
    try:
        append_event(log_path, event)
    except OSError as exc:
        logger.warning("kernel: failed to write audit log: %s", exc)


def register(ctx) -> None:
    """Register the kernel's hook callbacks. Called once by the plugin loader."""
    ctx.register_hook("pre_api_request", _on_pre_api_request)
    ctx.register_hook("post_api_request", _on_post_api_request)
