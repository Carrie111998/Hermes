"""Hermes observability primitives (Phase A of ADR-0020).

Exports:
    get_tracer(name)      -> opentelemetry.trace.Tracer
    shutdown()            -> flush pending spans before process exit
    ensure_initialized()  -> idempotent; reads HERMES .env if needed
    stamp_langfuse_attributes(span, session_id=, user_id=, tags=)
                          -> best-effort Langfuse grouping attrs (SR-535)
"""

from .otel_tracing import (
    ensure_initialized,
    get_tracer,
    shutdown,
    stamp_langfuse_attributes,
)

__all__ = [
    "ensure_initialized",
    "get_tracer",
    "shutdown",
    "stamp_langfuse_attributes",
]
