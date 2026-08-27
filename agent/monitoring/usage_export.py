"""Coding-agent usage export (hermes.* metrics) to an OTLP collector.

Emits per-API-call token/cost usage so Hermes spend is attributable alongside
other coding agents in a shared observability backend (e.g. the Dubber Claude
Apps Gateway, whose ADOT sidecar promotes these to CloudWatch).

Design notes learned the hard way against a real awsemf collector:

* DELTA temporality is mandatory. The OTel SDK default is CUMULATIVE; the
  awsemf exporter treats a single cumulative datapoint as a delta baseline and
  emits NOTHING, silently, with no error surfaced by the gateway or collector.
* Metric names must match a collector-side selector. A name the collector does
  not know is written into the EMF record but never promoted to a metric.
* Attribute (dimension) sets must match what the collector declares, and
  CloudWatch SEARCH matches the EXACT full dimension schema, not a subset.

Fail-open by design: telemetry must never break an agent turn.
"""

from __future__ import annotations

import logging
import os
import threading
from typing import Any, Dict, Optional

logger = logging.getLogger(__name__)

_SCOPE = "hermes.agent.usage"

# Metric names. Native hermes.* rather than reusing another vendor's namespace
# so the two agents stay separable in every panel.
_M_TOKENS = "hermes.token.usage"
_M_COST = "hermes.cost.usage"
_M_SESSIONS = "hermes.session.count"
_M_ACTIVE = "hermes.active_time.total"

_lock = threading.Lock()
_state: Optional["_UsageExportState"] = None

# Set by whichever entrypoint registers the atexit drain, so a process that
# creates many agents/sessions registers it exactly once.
_atexit_registered = False


class _UsageExportState:
    __slots__ = ("provider", "tokens", "cost", "sessions", "active", "attrs_base")

    def __init__(self, provider, tokens, cost, sessions, active, attrs_base):
        self.provider = provider
        self.tokens = tokens
        self.cost = cost
        self.sessions = sessions
        self.active = active
        self.attrs_base = attrs_base


def _usage_config(config: Dict[str, Any]) -> Dict[str, Any]:
    mon = (config or {}).get("monitoring") or {}
    return mon.get("usage_export") or {}


def _otlp_config(config: Dict[str, Any]) -> Dict[str, Any]:
    mon = (config or {}).get("monitoring") or {}
    export = mon.get("export") or {}
    return export.get("otlp") or {}


def enabled(config: Dict[str, Any]) -> bool:
    usage = _usage_config(config)
    otlp = _otlp_config(config)
    return bool(usage.get("enabled") and otlp.get("endpoint"))


def _metric_endpoint(endpoint: str) -> str:
    """Normalise to the OTLP HTTP metrics path.

    A bare origin gets /v1/metrics appended. An endpoint that already ends in
    /v1/traces or /v1/metrics is rewritten rather than doubled: a doubled path
    404s and telemetry is dropped with no error surfaced.
    """
    ep = (endpoint or "").rstrip("/")
    for suffix in ("/v1/traces", "/v1/logs", "/v1/metrics"):
        if ep.endswith(suffix):
            ep = ep[: -len(suffix)]
            break
    return ep + "/v1/metrics"


def _resolve_headers(headers_env: Optional[Dict[str, str]]) -> Dict[str, str]:
    """Map header name -> ENV VAR NAME, resolved at export time.

    Never stores secret values in config.
    """
    resolved: Dict[str, str] = {}
    for header_name, env_name in (headers_env or {}).items():
        val = os.environ.get(str(env_name))
        if val:
            resolved[str(header_name)] = val
    return resolved


def _read_credential_file(path: str) -> Optional[str]:
    """Read a bearer credential out of a JSON file written by an auth flow.

    Returns None (not an exception) on any problem: a telemetry exporter must
    never be the reason an agent turn fails.
    """
    try:
        import json
        with open(os.path.expanduser(path), "r") as fh:
            data = json.load(fh)
    except Exception:
        return None
    for key in ("access_token", "token", "id_token"):
        val = data.get(key)
        if isinstance(val, str) and val:
            return val
    return None


class _RefreshingSession:
    """requests.Session wrapper that re-reads a credential file per request.

    The OTLP HTTP exporter resolves its headers ONCE at construction. Gateway
    session credentials are short-lived (8h is typical), so a long-running agent
    or gateway process would export happily until expiry and then fail every
    batch for the rest of its life — silently, since export errors are logged at
    debug and never surface to the user.

    Wrapping the session instead of the exporter keeps the refresh on the
    request path, which is the only place that can observe a rotated file.
    """

    def __init__(self, session, credential_file: str,
                 header: str = "Authorization", scheme: str = "Bearer"):
        self._session = session
        self._credential_file = credential_file
        self._header = header
        self._scheme = scheme

    def __getattr__(self, name):
        # Delegate everything the exporter touches (close, headers, mount, ...)
        return getattr(self._session, name)

    def post(self, *args, **kwargs):
        current = _read_credential_file(self._credential_file)
        if current:
            headers = dict(kwargs.get("headers") or {})
            headers[self._header] = f"{self._scheme} {current}"
            kwargs["headers"] = headers
        return self._session.post(*args, **kwargs)


def _require_sdk(*, auto_install: bool = True) -> Dict[str, Any]:
    if auto_install:
        try:
            from tools.lazy_deps import ensure as _lazy_ensure
            _lazy_ensure("export.otlp", prompt=False)
        except Exception:
            pass
    from opentelemetry.exporter.otlp.proto.http.metric_exporter import OTLPMetricExporter
    from opentelemetry.sdk.metrics import Counter, MeterProvider
    from opentelemetry.sdk.metrics.export import (
        AggregationTemporality,
        PeriodicExportingMetricReader,
    )
    from opentelemetry.sdk.resources import Resource
    return {
        "OTLPMetricExporter": OTLPMetricExporter,
        "Counter": Counter,
        "MeterProvider": MeterProvider,
        "AggregationTemporality": AggregationTemporality,
        "PeriodicExportingMetricReader": PeriodicExportingMetricReader,
        "Resource": Resource,
    }


def start(config: Dict[str, Any]) -> bool:
    """Start the usage exporter. Returns True if running. Never raises."""
    global _state
    if not enabled(config):
        return False
    with _lock:
        if _state is not None:
            return True
        try:
            sdk = _require_sdk()
        except Exception:
            logger.debug("usage export: OTLP SDK unavailable", exc_info=True)
            return False
        try:
            usage = _usage_config(config)
            otlp = _otlp_config(config)
            endpoint = _metric_endpoint(str(otlp.get("endpoint") or ""))
            headers = _resolve_headers(otlp.get("headers_env"))

            # A credential file beats a static env var for anything long-lived:
            # gateway sessions expire (8h typical) and the exporter would
            # otherwise keep posting a dead credential forever.
            session = None
            cred_file = str(usage.get("credential_file") or "").strip()
            if cred_file:
                try:
                    import requests
                    session = _RefreshingSession(requests.Session(), cred_file)
                except Exception:
                    logger.debug("usage export: refreshing session unavailable",
                                 exc_info=True)
                    session = None

            exporter_kwargs: Dict[str, Any] = {
                "endpoint": endpoint,
                "headers": headers or None,
                # MANDATORY — see module docstring.
                "preferred_temporality": {
                    sdk["Counter"]: sdk["AggregationTemporality"].DELTA
                },
            }
            if session is not None:
                exporter_kwargs["session"] = session
            exporter = sdk["OTLPMetricExporter"](**exporter_kwargs)
            interval_ms = max(5, int(usage.get("export_interval_seconds", 60))) * 1000
            reader = sdk["PeriodicExportingMetricReader"](
                exporter, export_interval_millis=interval_ms
            )
            resource = sdk["Resource"].create(
                {"service.name": str(usage.get("service_name") or "hermes-agent")}
            )
            provider = sdk["MeterProvider"](
                metric_readers=[reader], resource=resource
            )
            meter = provider.get_meter(_SCOPE)

            attrs_base: Dict[str, str] = {}
            email = str(usage.get("user_email") or "").strip()
            if email:
                attrs_base["user.email"] = email

            _state = _UsageExportState(
                provider=provider,
                tokens=meter.create_counter(_M_TOKENS, unit="tokens"),
                cost=meter.create_counter(_M_COST, unit="USD"),
                sessions=meter.create_counter(_M_SESSIONS),
                active=meter.create_counter(_M_ACTIVE, unit="s"),
                attrs_base=attrs_base,
            )
            logger.info(
                "usage export started: endpoint=%s interval=%ss", endpoint,
                usage.get("export_interval_seconds", 60),
            )
            return True
        except Exception:
            logger.warning("usage export failed to start", exc_info=True)
            _state = None
            return False


def record_api_call(
    *,
    model: str,
    input_tokens: int = 0,
    output_tokens: int = 0,
    cache_read_tokens: int = 0,
    cache_write_tokens: int = 0,
    cost_usd: Optional[float] = None,
    effort: Optional[str] = None,
) -> None:
    """Record one API call's usage. Fail-open; never raises into the agent."""
    st = _state
    if st is None:
        return
    try:
        base = dict(st.attrs_base)
        if model:
            base["model"] = str(model)
        if effort:
            base["effort"] = str(effort)

        # `type` mirrors the four token classes the dashboards break out.
        for type_name, value in (
            ("input", input_tokens),
            ("output", output_tokens),
            ("cacheRead", cache_read_tokens),
            ("cacheCreation", cache_write_tokens),
        ):
            if value:
                attrs = dict(base)
                attrs["type"] = type_name
                st.tokens.add(int(value), attrs)

        if cost_usd:
            st.cost.add(float(cost_usd), base)
    except Exception:
        logger.debug("usage export: record_api_call failed", exc_info=True)


def record_session_start(*, terminal_type: Optional[str] = None) -> None:
    st = _state
    if st is None:
        return
    try:
        attrs = dict(st.attrs_base)
        if terminal_type:
            attrs["terminal.type"] = str(terminal_type)
        st.sessions.add(1, attrs)
    except Exception:
        logger.debug("usage export: record_session_start failed", exc_info=True)


def record_active_time(seconds: float, *, terminal_type: Optional[str] = None) -> None:
    st = _state
    if st is None or not seconds:
        return
    try:
        attrs = dict(st.attrs_base)
        if terminal_type:
            attrs["terminal.type"] = str(terminal_type)
        st.active.add(float(seconds), attrs)
    except Exception:
        logger.debug("usage export: record_active_time failed", exc_info=True)


def flush(timeout_millis: int = 10000) -> bool:
    st = _state
    if st is None:
        return False
    try:
        return bool(st.provider.force_flush(timeout_millis=timeout_millis))
    except Exception:
        logger.debug("usage export: flush failed", exc_info=True)
        return False


def shutdown() -> None:
    global _state
    with _lock:
        st = _state
        _state = None
    if st is None:
        return
    try:
        st.provider.force_flush(timeout_millis=10000)
        st.provider.shutdown()
    except Exception:
        logger.debug("usage export: shutdown failed", exc_info=True)
