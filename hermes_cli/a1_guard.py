"""Mandatory A1 resolver / payload-capture guard for model dispatch.

The guard is intentionally independent from plugin middleware.  Middleware may
observe or transform requests, but A1 evidence capture is the fail-closed
boundary before a provider call is attempted.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Literal
from urllib.parse import urlparse

Decision = Literal["allow", "deny", "quarantine", "fail_closed"]

FRONTIER_MARKERS = (
    "anthropic",
    "frontier",
    "gemini",
    "headroom",
    "litellm",
    "openai",
    "openrouter",
)
LOCAL_PROVIDER_MARKERS = (
    "local",
    "local-ollama",
    "ollama",
)
LOCAL_HOSTS = {
    "localhost",
    "localhost:11434",
    "127.0.0.1",
    "127.0.0.1:11434",
    "::1",
}


@dataclass(frozen=True)
class A1Decision:
    correlation_id: str
    api_request_id: str
    session_id: str
    surface: str
    profile: str
    classification: str
    requested_provider: str
    requested_model: str
    canonical_provider: str
    canonical_model: str
    canonical_api_mode: str
    canonical_base_url_host: str
    provider_source: str
    policy_version: str
    config_hash: str
    decision: Decision
    rule_id: str
    denial_reason: str | None = None


@dataclass(frozen=True)
class A1PayloadCapture:
    correlation_id: str
    api_request_id: str
    payload_shape: str
    message_count: int
    tool_count: int
    payload_digest: str
    redaction_class: str
    request_overrides_digest: str
    middleware_trace_digest: str
    dispatch_allowed: bool


@dataclass(frozen=True)
class A1GuardResult:
    decision: A1Decision
    capture: A1PayloadCapture


class A1DispatchDenied(Exception):
    """Raised before provider dispatch when A1 policy denies or capture fails."""

    def __init__(self, rule_id: str, reason: str) -> None:
        super().__init__(f"{rule_id}: {reason}")
        self.rule_id = rule_id
        self.reason = reason


def guard_model_dispatch(
    *,
    api_kwargs: dict[str, Any],
    runtime_context: dict[str, Any],
    middleware_trace: list[dict[str, Any]] | None = None,
    evidence_sink: str | Path | None,
) -> A1GuardResult:
    """Capture and enforce the mandatory A1 model-dispatch envelope.

    The provider call may proceed only after resolver and payload-capture events
    are durably written.  Denials also append a dispatch_result event with
    provider_call_attempted=false before raising ``A1DispatchDenied``.
    """
    sink = _require_sink(evidence_sink)
    trace = middleware_trace or []
    decision = _build_decision(api_kwargs=api_kwargs, runtime_context=runtime_context)
    capture = _build_capture(
        api_kwargs=api_kwargs,
        runtime_context=runtime_context,
        middleware_trace=trace,
        dispatch_allowed=decision.decision == "allow",
    )

    try:
        _append_event(sink, "resolver_decision", asdict(decision))
        _append_event(sink, "payload_capture", asdict(capture))
        if decision.decision != "allow":
            _append_event(
                sink,
                "dispatch_result",
                {
                    "correlation_id": decision.correlation_id,
                    "api_request_id": decision.api_request_id,
                    "decision": decision.decision,
                    "rule_id": decision.rule_id,
                    "denial_reason": decision.denial_reason,
                    "provider_call_attempted": False,
                    "provider_call_completed": False,
                },
            )
    except A1DispatchDenied:
        raise
    except Exception as exc:  # pragma: no cover - filesystem failures are platform-specific
        raise A1DispatchDenied("a1.guard.capture-failed", str(exc)) from exc

    if decision.decision != "allow":
        raise A1DispatchDenied(decision.rule_id, decision.denial_reason or "A1 dispatch denied")

    return A1GuardResult(decision=decision, capture=capture)


def guarded_model_dispatch(
    *,
    api_kwargs: dict[str, Any],
    next_call: Any,
    runtime_context: dict[str, Any],
    middleware_trace: list[dict[str, Any]] | None = None,
    evidence_sink: str | Path | None,
) -> Any:
    """Run a provider call only after the mandatory A1 guard allows it."""
    result = guard_model_dispatch(
        api_kwargs=api_kwargs,
        runtime_context=runtime_context,
        middleware_trace=middleware_trace,
        evidence_sink=evidence_sink,
    )
    try:
        response = next_call(api_kwargs)
    except Exception as exc:
        record_dispatch_result(
            evidence_sink=evidence_sink,
            api_request_id=result.decision.api_request_id,
            correlation_id=result.decision.correlation_id,
            provider_call_attempted=True,
            provider_call_completed=False,
            error_type=type(exc).__name__,
        )
        raise

    record_dispatch_result(
        evidence_sink=evidence_sink,
        api_request_id=result.decision.api_request_id,
        correlation_id=result.decision.correlation_id,
        provider_call_attempted=True,
        provider_call_completed=True,
    )
    return response



def record_dispatch_result(
    *,
    evidence_sink: str | Path | None,
    api_request_id: str,
    correlation_id: str,
    provider_call_attempted: bool,
    provider_call_completed: bool,
    fallback_activated: bool = False,
    error_type: str | None = None,
    denial_reason: str | None = None,
) -> None:
    """Append the correlated provider dispatch result event."""
    sink = _require_sink(evidence_sink)
    _append_event(
        sink,
        "dispatch_result",
        {
            "correlation_id": correlation_id,
            "api_request_id": api_request_id,
            "decision": "allow" if provider_call_attempted else "deny",
            "rule_id": "a1.dispatch.completed" if provider_call_completed else "a1.dispatch.not-completed",
            "denial_reason": denial_reason,
            "provider_call_attempted": provider_call_attempted,
            "provider_call_completed": provider_call_completed,
            "fallback_activated": fallback_activated,
            "error_type": error_type,
        },
    )


def _build_decision(*, api_kwargs: dict[str, Any], runtime_context: dict[str, Any]) -> A1Decision:
    classification = str(runtime_context.get("classification") or "").strip().upper()
    canonical_provider = str(runtime_context.get("canonical_provider") or runtime_context.get("provider") or "")
    requested_provider = str(runtime_context.get("requested_provider") or canonical_provider)
    canonical_base_url = str(runtime_context.get("canonical_base_url") or runtime_context.get("base_url") or "")
    canonical_host = _base_url_host(canonical_base_url)

    rule_id = "a1.dispatch.allow"
    decision: Decision = "allow"
    denial_reason: str | None = None

    if not classification:
        if _is_non_local_route(canonical_provider, canonical_host):
            decision = "deny"
            rule_id = "a1.guard.missing-classification"
            denial_reason = "Unknown classification inherits confidential handling for non-local routes"
    elif _is_confidential(classification) and _is_non_local_route(canonical_provider, canonical_host):
        decision = "deny"
        rule_id = "a1.c2.frontier-deny"
        denial_reason = "C2/local-only payload cannot dispatch to frontier or proxy-backed route"

    allowed_hosts = runtime_context.get("allowed_base_url_hosts") or []
    if decision == "allow" and allowed_hosts and canonical_host not in set(map(str, allowed_hosts)):
        decision = "deny"
        rule_id = "a1.route.unexpected-base-url"
        denial_reason = f"Resolved base URL host {canonical_host!r} is not in the allowed route set"

    requested_model = str(runtime_context.get("requested_model") or api_kwargs.get("model") or "")
    canonical_model = str(runtime_context.get("canonical_model") or api_kwargs.get("model") or "")
    return A1Decision(
        correlation_id=str(runtime_context.get("correlation_id") or runtime_context.get("api_request_id") or ""),
        api_request_id=str(runtime_context.get("api_request_id") or ""),
        session_id=str(runtime_context.get("session_id") or ""),
        surface=str(runtime_context.get("surface") or runtime_context.get("platform") or ""),
        profile=str(runtime_context.get("profile") or ""),
        classification=classification or "UNKNOWN",
        requested_provider=requested_provider,
        requested_model=requested_model,
        canonical_provider=canonical_provider,
        canonical_model=canonical_model,
        canonical_api_mode=str(runtime_context.get("canonical_api_mode") or runtime_context.get("api_mode") or ""),
        canonical_base_url_host=canonical_host,
        provider_source=str(runtime_context.get("provider_source") or runtime_context.get("source") or ""),
        policy_version=str(runtime_context.get("policy_version") or "a1.model-dispatch.v1"),
        config_hash=str(runtime_context.get("config_hash") or ""),
        decision=decision,
        rule_id=rule_id,
        denial_reason=denial_reason,
    )


def _build_capture(
    *,
    api_kwargs: dict[str, Any],
    runtime_context: dict[str, Any],
    middleware_trace: list[dict[str, Any]],
    dispatch_allowed: bool,
) -> A1PayloadCapture:
    messages = api_kwargs.get("messages")
    input_payload = api_kwargs.get("input")
    message_count = len(messages) if isinstance(messages, list) else 0
    if not message_count and isinstance(input_payload, list):
        message_count = len(input_payload)
    tools = api_kwargs.get("tools")
    tool_count = len(tools) if isinstance(tools, list) else 0
    payload_shape = "messages" if isinstance(messages, list) else "input" if input_payload is not None else "unknown"
    overrides = {
        key: value
        for key, value in api_kwargs.items()
        if key not in {"messages", "input", "tools"}
    }
    return A1PayloadCapture(
        correlation_id=str(runtime_context.get("correlation_id") or runtime_context.get("api_request_id") or ""),
        api_request_id=str(runtime_context.get("api_request_id") or ""),
        payload_shape=payload_shape,
        message_count=message_count,
        tool_count=tool_count,
        payload_digest=_digest(api_kwargs),
        redaction_class="digest-only",
        request_overrides_digest=_digest(overrides),
        middleware_trace_digest=_digest(middleware_trace),
        dispatch_allowed=dispatch_allowed,
    )


def _require_sink(evidence_sink: str | Path | None) -> Path:
    if evidence_sink is None:
        raise A1DispatchDenied(
            "a1.guard.capture-failed",
            "A1 evidence sink is required before model dispatch",
        )
    return Path(evidence_sink)


def _append_event(path: Path, event_type: str, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    event = {
        "event_type": event_type,
        "timestamp": datetime.now(timezone.utc).isoformat(),
        **payload,
    }
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(event, sort_keys=True, separators=(",", ":")) + "\n")


def _digest(value: Any) -> str:
    data = json.dumps(value, sort_keys=True, default=str, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(data).hexdigest()


def _base_url_host(base_url: str) -> str:
    if not base_url:
        return ""
    parsed = urlparse(base_url)
    if parsed.netloc:
        return parsed.netloc.lower()
    return parsed.path.split("/", 1)[0].lower()


def _is_confidential(classification: str) -> bool:
    value = classification.upper()
    return value.startswith("C2") or value.startswith("C3") or "LOCAL_ONLY" in value or "CONFIDENTIAL" in value


def _is_non_local_route(provider: str, host: str) -> bool:
    provider_l = provider.lower()
    if any(marker in provider_l for marker in FRONTIER_MARKERS):
        return True
    if any(marker in provider_l for marker in LOCAL_PROVIDER_MARKERS):
        return False
    if host in LOCAL_HOSTS or host.startswith("localhost:") or host.startswith("127.0.0.1:"):
        return False
    return True
