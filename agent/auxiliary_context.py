"""Per-call privacy context shared by auxiliary provider adapters.

This module is intentionally dependency-light so provider-specific adapters can
honor the auxiliary client's privacy boundary without importing the client
router (which would create import cycles).
"""

import contextvars
from typing import Any, Dict, Optional


AUXILIARY_CALL_CONTEXT: contextvars.ContextVar[Optional[Dict[str, Any]]] = (
    contextvars.ContextVar("auxiliary_relay_call", default=None)
)


def sensitive_content_active() -> bool:
    """Return whether the active auxiliary call carries sensitive content."""
    context = AUXILIARY_CALL_CONTEXT.get()
    return bool(context and context.get("sensitive_content"))


def exception_log_detail(exc: BaseException) -> str:
    """Keep provider/request details out of logs for sensitive calls."""
    if sensitive_content_active():
        return f"<{type(exc).__name__}: details redacted>"
    return str(exc)


def exception_log_traceback() -> bool:
    """Suppress traceback rendering when it could echo sensitive content."""
    return not sensitive_content_active()


def content_log_detail(value: Any) -> str:
    """Redact provider-supplied content while preserving ordinary diagnostics."""
    if sensitive_content_active():
        return "<sensitive content: details redacted>"
    return str(value)
