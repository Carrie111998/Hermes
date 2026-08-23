"""OpenTelemetry tracing helper for Hermes agents / subscribers / crons.

Phase A of ADR-0020: every agent invocation, subscriber.handle(), and cron
execution gets a top-level span. Spans are exported via OTLP HTTP to the
self-hosted Langfuse at http://localhost:3050/api/public/otel.

## Usage

    from obs import get_tracer
    tracer = get_tracer("hermes.scout")
    with tracer.start_as_current_span("scout.scan") as span:
        span.set_attribute("profile", "scout")
        span.set_attribute("source", "linkedin")
        ...

## Env vars (read from ~/.hermes/.env via dotenv on first call, then os.environ)

    LANGFUSE_HOST            http://localhost:3050
    LANGFUSE_PUBLIC_KEY      pk-lf-...
    LANGFUSE_SECRET_KEY      sk-lf-...
    HERMES_OTEL_DISABLE=1    (optional) hard-disable export; spans become no-ops

## Infra-span head sampling (SR-535 / ADR-0029 decision 2)

Infrastructure spans (default: names starting with ``subscriber.handle``) are
head-sampled at the export boundary so the self-hosted Langfuse stops
ingesting ~33k noise traces/day. Error spans are ALWAYS exported regardless
of the ratio. LLM/agent/cron spans (non-matching names) are never sampled.

    HERMES_OTEL_SUBSCRIBER_SAMPLE     keep ratio for infra spans, 0.0-1.0.
                                      Default 0.02 (~1:50). 1.0 disables
                                      sampling; 0.0 keeps only error spans.
    HERMES_OTEL_INFRA_SPAN_PREFIXES   comma-separated span-name prefixes
                                      counted as infra. Default
                                      "subscriber.handle" (matches the
                                      SR-534 retention sweep's definition).

## Why HTTP/protobuf and not gRPC

Langfuse's OTLP endpoint only speaks HTTP. gRPC is 4318 territory; Langfuse
serves both signals (spans, logs) on /api/public/otel via HTTP.
"""

from __future__ import annotations

import atexit
import base64
import os
import threading
from pathlib import Path

_LOCK = threading.Lock()
_INITIALIZED = False
_PROVIDER = None  # TracerProvider | None

# SR-535 defaults: infra spans head-sampled at ~1:50; prefix list matches
# scripts/langfuse_retention_sweep.py's --infra-name-prefix default so the
# source-side and retention-side definitions of "infrastructure" agree.
_INFRA_SAMPLE_DEFAULT = 0.02
_INFRA_PREFIXES_DEFAULT = "subscriber.handle"


def _infra_sampling_config() -> "tuple[tuple[str, ...], float]":
    """Read (prefixes, ratio) for infra-span sampling from the environment.

    Bad/missing values fall back to defaults; ratio is clamped to [0, 1].
    """
    raw_prefixes = os.environ.get(
        "HERMES_OTEL_INFRA_SPAN_PREFIXES", _INFRA_PREFIXES_DEFAULT
    )
    prefixes = tuple(p.strip() for p in raw_prefixes.split(",") if p.strip())
    try:
        ratio = float(
            os.environ.get("HERMES_OTEL_SUBSCRIBER_SAMPLE", _INFRA_SAMPLE_DEFAULT)
        )
    except (TypeError, ValueError):
        ratio = _INFRA_SAMPLE_DEFAULT
    ratio = min(max(ratio, 0.0), 1.0)
    return prefixes, ratio


def _span_is_error(span) -> bool:
    """True if the ended span recorded an error status or an exception event.

    Duck-typed (no OTel imports) so it works on ReadableSpan and on test
    fakes alike. opentelemetry Status.is_ok is True for UNSET and OK.
    """
    try:
        status = getattr(span, "status", None)
        if status is not None and not status.is_ok:
            return True
        for ev in getattr(span, "events", None) or ():
            if getattr(ev, "name", "") == "exception":
                return True
    except Exception:
        pass
    return False


class _InfraSamplingSpanProcessor:
    """Head-samples infrastructure spans at the export boundary (SR-535).

    Wraps the real export processor (BatchSpanProcessor). Spans whose name
    starts with an infra prefix are forwarded only for a deterministic
    trace-id ratio (same lower-64-bit scheme as OTel's TraceIdRatioBased,
    so a whole trace is kept or dropped coherently) — EXCEPT spans that
    recorded an error/exception, which are always forwarded. All other
    spans pass through untouched.

    Duck-typed rather than subclassing sdk.trace.SpanProcessor so this
    module stays importable in envs without opentelemetry installed.
    """

    _TRACE_ID_MASK = (1 << 64) - 1

    def __init__(self, wrapped, prefixes: "tuple[str, ...]", ratio: float):
        self._wrapped = wrapped
        self._prefixes = prefixes
        self._bound = round(min(max(ratio, 0.0), 1.0) * (1 << 64))

    def _should_export(self, span) -> bool:
        name = getattr(span, "name", "") or ""
        if not name.startswith(self._prefixes):
            return True
        if _span_is_error(span):
            return True
        try:
            trace_id = span.context.trace_id
        except Exception:
            return True  # fail-open: never lose a span we can't inspect
        return (trace_id & self._TRACE_ID_MASK) < self._bound

    # --- SpanProcessor interface (delegation) ---
    def on_start(self, span, parent_context=None):
        self._wrapped.on_start(span, parent_context)

    def _on_ending(self, span):
        # Newer OTel SDKs (>=1.34) call a private _on_ending hook on every
        # registered processor while the span is still mutable. Delegate if
        # the wrapped processor has it; older SDKs never call this.
        hook = getattr(self._wrapped, "_on_ending", None)
        if hook is not None:
            hook(span)

    def on_end(self, span):
        if self._should_export(span):
            self._wrapped.on_end(span)

    def shutdown(self):
        self._wrapped.shutdown()

    def force_flush(self, timeout_millis: int = 30000):
        return self._wrapped.force_flush(timeout_millis)


def _wrap_with_infra_sampling(processor):
    """Wrap the export processor with SR-535 infra sampling if configured.

    Returns the processor unchanged when sampling is effectively off
    (ratio >= 1.0 or no prefixes configured).
    """
    prefixes, ratio = _infra_sampling_config()
    if not prefixes or ratio >= 1.0:
        return processor
    return _InfraSamplingSpanProcessor(processor, prefixes, ratio)


# ---------------------------------------------------------------------------
# Langfuse-semantic span attributes (SR-535 remainder / ADR-0029)
#
# Langfuse's OTLP ingest maps these OTel span attributes onto trace-level
# fields (langfuse.com/docs/opentelemetry/get-started, verified 2026-07-08
# against the self-hosted v3 line):
#     langfuse.session.id  -> trace sessionId  (groups traces as a session)
#     langfuse.user.id     -> trace userId
#     langfuse.trace.tags  -> trace tags       (expects string[])
# They may be set on any span in the trace. We stamp them at span creation
# on the emitters whose spans survive the infra sampler above, so the lean
# trace stream stays groupable/filterable in the Langfuse UI.
# ---------------------------------------------------------------------------
LANGFUSE_SESSION_ID_ATTR = "langfuse.session.id"
LANGFUSE_USER_ID_ATTR = "langfuse.user.id"
LANGFUSE_TAGS_ATTR = "langfuse.trace.tags"


def stamp_langfuse_attributes(span, *, session_id=None, user_id=None, tags=None) -> None:
    """Best-effort: set Langfuse grouping attributes on ``span``.

    Never raises — safe for real OTel spans and for the emitters' no-op
    span fakes alike. Falsy values are skipped; ``tags`` is coerced to a
    list of non-empty strings (Langfuse expects ``string[]``).
    """
    try:
        if session_id:
            span.set_attribute(LANGFUSE_SESSION_ID_ATTR, str(session_id))
        if user_id:
            span.set_attribute(LANGFUSE_USER_ID_ATTR, str(user_id))
        if tags:
            clean = [str(t) for t in tags if t is not None and str(t)]
            if clean:
                span.set_attribute(LANGFUSE_TAGS_ATTR, clean)
    except Exception:
        pass


def _load_env_once() -> None:
    """Best-effort: if LANGFUSE_* not in os.environ, read the Hermes ``.env``.

    Resolves the file via :func:`hermes_constants.get_hermes_home` (override →
    ``HERMES_HOME`` env var → platform default) rather than hardcoding
    ``Path.home() / ".hermes"``. The hardcoded path leaked every key of the
    developer's *real* home ``.env`` into ``os.environ`` even when
    ``HERMES_HOME`` pointed at a profile or a test tempdir — inside pytest
    that re-injects credentials *after* the hermetic-environment fixture has
    blanked them, flipping ambient-credential gates (e.g.
    ``check_web_api_key``) and making tool-visibility tests pass or fail on
    the developer's machine instead of the code. Never overwrites a value
    already present in ``os.environ``.
    """
    if os.environ.get("LANGFUSE_HOST") and os.environ.get("LANGFUSE_PUBLIC_KEY"):
        return
    try:
        from hermes_constants import get_hermes_home

        env_path = get_hermes_home() / ".env"
    except Exception:  # noqa: BLE001 — obs must load without the app too
        env_path = Path.home() / ".hermes" / ".env"
    if not env_path.exists():
        return
    try:
        lines = env_path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return
    for line in lines:
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, _, v = line.partition("=")
        k, v = k.strip(), v.strip()
        if k and k not in os.environ:
            os.environ[k] = v


def ensure_initialized(service_name: str = "hermes") -> None:
    """Idempotent. Safe to call from every agent entrypoint."""
    global _INITIALIZED, _PROVIDER
    if _INITIALIZED:
        return
    with _LOCK:
        if _INITIALIZED:
            return

        if os.environ.get("HERMES_OTEL_DISABLE") == "1":
            _INITIALIZED = True
            return

        _load_env_once()

        host = os.environ.get("LANGFUSE_HOST", "http://localhost:3050").rstrip("/")
        pk = os.environ.get("LANGFUSE_PUBLIC_KEY")
        sk = os.environ.get("LANGFUSE_SECRET_KEY")
        if not (pk and sk):
            # Langfuse not configured -> no-op provider (spans still work via API, just not exported).
            _INITIALIZED = True
            return

        # Imports guarded so obs/ can be imported even in envs without OTel installed.
        from opentelemetry import trace
        from opentelemetry.exporter.otlp.proto.http.trace_exporter import (
            OTLPSpanExporter,
        )
        from opentelemetry.sdk.resources import Resource
        from opentelemetry.sdk.trace import TracerProvider
        from opentelemetry.sdk.trace.export import BatchSpanProcessor

        auth = base64.b64encode(f"{pk}:{sk}".encode("utf-8")).decode("ascii")
        exporter = OTLPSpanExporter(
            endpoint=f"{host}/api/public/otel/v1/traces",
            headers={"Authorization": f"Basic {auth}"},
        )
        resource = Resource.create(
            {
                "service.name": service_name,
                "service.namespace": "hermes",
                "deployment.environment": os.environ.get(
                    "HERMES_DEPLOY_ENV", "laptop"
                ),
            }
        )
        provider = TracerProvider(resource=resource)
        # SR-535: infra spans (subscriber.handle:*) are head-sampled at the
        # export boundary; error spans always survive. See module docstring.
        provider.add_span_processor(
            _wrap_with_infra_sampling(BatchSpanProcessor(exporter))
        )
        trace.set_tracer_provider(provider)
        _PROVIDER = provider
        _INITIALIZED = True
        atexit.register(shutdown)


def get_tracer(name: str):
    """Return an OTel tracer; initializes provider on first call."""
    ensure_initialized(service_name=name)
    from opentelemetry import trace

    return trace.get_tracer(name)


def shutdown(timeout_ms: int = 3000) -> None:
    """Flush + shutdown. Called at process exit; safe to call manually."""
    global _PROVIDER
    if _PROVIDER is None:
        return
    try:
        _PROVIDER.shutdown()
    except Exception:
        pass
    _PROVIDER = None
