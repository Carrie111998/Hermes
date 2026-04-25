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
from typing import Optional

_LOCK = threading.Lock()
_INITIALIZED = False
_PROVIDER = None  # TracerProvider | None


def _load_env_once() -> None:
    """Best-effort: if LANGFUSE_* not in os.environ, read ~/.hermes/.env."""
    if os.environ.get("LANGFUSE_HOST") and os.environ.get("LANGFUSE_PUBLIC_KEY"):
        return
    env_path = Path.home() / ".hermes" / ".env"
    if not env_path.exists():
        return
    for line in env_path.read_text(encoding="utf-8").splitlines():
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
        provider.add_span_processor(BatchSpanProcessor(exporter))
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
