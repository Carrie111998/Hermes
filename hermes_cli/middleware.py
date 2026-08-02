"""Hermes middleware notification helpers.

The trusted model is the sole semantic authority for model requests and tool
calls.  Plugin middleware therefore observes immutable snapshots; it cannot
rewrite requests or arguments, wrap/skip execution, or replace results.  The
legacy names remain available so existing observer plugins keep receiving
events without retaining behavioral authority.
"""

from __future__ import annotations

import logging
from copy import deepcopy
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List

logger = logging.getLogger(__name__)

OBSERVER_SCHEMA_VERSION = "hermes.observer.v1"
MIDDLEWARE_SCHEMA_VERSION = "hermes.middleware.v2.notification-only"

TOOL_REQUEST_MIDDLEWARE = "tool_request"
TOOL_EXECUTION_MIDDLEWARE = "tool_execution"
LLM_REQUEST_MIDDLEWARE = "llm_request"
LLM_EXECUTION_MIDDLEWARE = "llm_execution"

# Back-compat aliases for older PoC branches that used API terminology.
API_REQUEST_MIDDLEWARE = LLM_REQUEST_MIDDLEWARE
API_EXECUTION_MIDDLEWARE = LLM_EXECUTION_MIDDLEWARE

VALID_MIDDLEWARE: set[str] = {
    TOOL_REQUEST_MIDDLEWARE,
    TOOL_EXECUTION_MIDDLEWARE,
    LLM_REQUEST_MIDDLEWARE,
    LLM_EXECUTION_MIDDLEWARE,
}


@dataclass
class RequestMiddlewareResult:
    """Compatibility result for a notification-only request event."""

    payload: Any
    original_payload: Any
    changed: bool = False
    trace: List[Dict[str, Any]] = field(default_factory=list)


def observer_payload(**kwargs: Any) -> Dict[str, Any]:
    kwargs.setdefault("telemetry_schema_version", OBSERVER_SCHEMA_VERSION)
    return kwargs


def middleware_payload(**kwargs: Any) -> Dict[str, Any]:
    kwargs.setdefault("telemetry_schema_version", OBSERVER_SCHEMA_VERSION)
    kwargs.setdefault("middleware_schema_version", MIDDLEWARE_SCHEMA_VERSION)
    return kwargs


def _safe_copy(payload: Any) -> Any:
    """Return a detached observer snapshot.

    A shallow-copy fallback would leak nested live objects to plugins, so a
    value that cannot be copied is not deliverable as an observer event.  The
    caller catches this and skips notification while the model/tool operation
    continues unchanged.
    """
    return deepcopy(payload)


def apply_llm_request_middleware(
    request: Dict[str, Any],
    **context: Any,
) -> RequestMiddlewareResult:
    """Notify middleware of an LLM request without granting mutation rights."""
    if not _has_middleware(LLM_REQUEST_MIDDLEWARE):
        return RequestMiddlewareResult(
            payload=request,
            original_payload=request,
            changed=False,
            trace=[],
        )

    try:
        original_request = _safe_copy(request)
        _invoke_middleware(
            LLM_REQUEST_MIDDLEWARE,
            phase="before",
            request=original_request,
            original_request=original_request,
            **context,
        )
    except Exception as exc:
        logger.warning("LLM request observer snapshot failed: %s", exc)
        original_request = request

    return RequestMiddlewareResult(
        payload=request,
        original_payload=original_request,
        changed=False,
        trace=[],
    )


def apply_tool_request_middleware(
    tool_name: str,
    args: Dict[str, Any],
    **context: Any,
) -> RequestMiddlewareResult:
    """Notify middleware of model-authored tool arguments without rewriting."""
    # Retained as a compatibility kwarg only.  It can no longer activate a
    # second semantic-authority path through request intercepts.
    context.pop("skip_relay", None)

    if not _has_middleware(TOOL_REQUEST_MIDDLEWARE):
        return RequestMiddlewareResult(
            payload=args,
            original_payload=args,
            changed=False,
            trace=[],
        )

    try:
        original_args = _safe_copy(args)
        _invoke_middleware(
            TOOL_REQUEST_MIDDLEWARE,
            phase="before",
            tool_name=tool_name,
            args=original_args,
            original_args=original_args,
            **context,
        )
    except Exception as exc:
        logger.warning("Tool request observer snapshot failed: %s", exc)
        original_args = args

    return RequestMiddlewareResult(
        payload=args,
        original_payload=original_args,
        changed=False,
        trace=[],
    )


def apply_api_request_middleware(
    request: Dict[str, Any],
    **context: Any,
) -> RequestMiddlewareResult:
    """Compatibility wrapper for older ``api_request`` naming."""
    return apply_llm_request_middleware(request, **context)


def run_llm_execution_middleware(
    request: Dict[str, Any],
    next_call: Callable[[Dict[str, Any]], Any],
    **context: Any,
) -> Any:
    """Execute the provider exactly once and emit detached before/after events."""
    return _run_observed_execution(
        LLM_EXECUTION_MIDDLEWARE,
        next_call,
        request=request,
        original_request=context.pop("original_request", request),
        **context,
    )


def run_tool_execution_middleware(
    tool_name: str,
    args: Dict[str, Any],
    next_call: Callable[[Dict[str, Any]], Any],
    **context: Any,
) -> Any:
    """Execute the tool exactly once and emit detached before/after events."""
    return _run_observed_execution(
        TOOL_EXECUTION_MIDDLEWARE,
        next_call,
        tool_name=tool_name,
        args=args,
        original_args=context.pop("original_args", args),
        **context,
    )


def run_api_execution_middleware(
    request: Dict[str, Any],
    next_call: Callable[[Dict[str, Any]], Any],
    **context: Any,
) -> Any:
    """Compatibility wrapper for older ``api_execution`` naming."""
    return run_llm_execution_middleware(request, next_call, **context)


def _invoke_middleware(kind: str, **kwargs: Any) -> List[Any]:
    from hermes_cli.plugins import invoke_middleware

    return invoke_middleware(kind, **middleware_payload(**kwargs))


def _has_middleware(kind: str) -> bool:
    from hermes_cli.plugins import has_middleware

    return has_middleware(kind)


def _run_observed_execution(
    kind: str,
    terminal_call: Callable[[Any], Any],
    **kwargs: Any,
) -> Any:
    payload_key = "request" if "request" in kwargs else "args"
    payload = kwargs[payload_key]

    if _has_middleware(kind):
        _invoke_middleware(kind, phase="before", **kwargs)

    try:
        result = terminal_call(payload)
    except BaseException as exc:
        if _has_middleware(kind):
            _invoke_middleware(
                kind,
                phase="after",
                outcome="error",
                error_type=type(exc).__name__,
                error_message=str(exc),
                **kwargs,
            )
        raise

    if _has_middleware(kind):
        _invoke_middleware(
            kind,
            phase="after",
            outcome="ok",
            result=result,
            **kwargs,
        )
    return result
