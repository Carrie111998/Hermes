"""Hermes observability primitives (Phase A of ADR-0020).

Exports:
    get_tracer(name)      -> opentelemetry.trace.Tracer
    shutdown()            -> flush pending spans before process exit
    ensure_initialized()  -> idempotent; reads HERMES .env if needed
"""

from .otel_tracing import ensure_initialized, get_tracer, shutdown

__all__ = ["ensure_initialized", "get_tracer", "shutdown"]
