"""Final provider-boundary enforcement for source-bound LLM egress."""

from __future__ import annotations

import json
import math
from pathlib import Path
from types import MappingProxyType
from types import SimpleNamespace
from typing import Any, Callable, Mapping, Sequence

from agent.llm_egress_firewall import (
    AuthorizedEgress,
    LLMEgressFirewall,
    LiteralSegment,
    OutboundText,
    SanitizedSegment,
    SourceBoundSegment,
    SourceGrant,
    TypedOutboundRequest,
    DestinationClass,
    classify_destination,
    source_grant_digest,
    static_literal_sha256,
    validate_sanitized_text,
)
from agent.source_provenance import DEFAULT_POLICY_DIGEST, SourceProvenanceRegistry


# Timeout is a non-content SDK control. Header/query values remain in the
# authorized JSON body so credentials or other caller-controlled text cannot
# be appended after the firewall receipt is written.
_SDK_CONTROL_KEYS = frozenset({"timeout"})
_PROTOCOL_LITERAL_FIELDS = frozenset({"role", "type"})
_PROTOCOL_LITERAL_VALUES = frozenset({
    "assistant",
    "computer_call_output",
    "developer",
    "function_call",
    "function_call_output",
    "input_image",
    "input_text",
    "output_text",
    "reasoning",
    "system",
    "tool",
    "user",
})
_PROTECTED_REMOTE_PROVIDERS = frozenset({
    "anthropic",
    "openai-codex",
    "nous",
    "nous-portal",
    "nousresearch",
})


def provider_uses_egress_firewall(provider: Any) -> bool:
    """Return whether an exact configured provider owns a protected remote lane."""

    return str(provider or "").strip().lower() in _PROTECTED_REMOTE_PROVIDERS


def _read_grant_text(grant: SourceGrant) -> str | None:
    try:
        lines = Path(grant.canonical_path).read_bytes().splitlines(keepends=True)
        return b"".join(lines[grant.line_start - 1 : grant.line_end]).decode("utf-8")
    except (OSError, UnicodeDecodeError, ValueError, TypeError):
        return None


def _grant_texts(grants: Sequence[SourceGrant]) -> tuple[tuple[str, SourceGrant], ...]:
    unique: dict[str, SourceGrant] = {}
    for grant in grants:
        if not isinstance(grant, SourceGrant):
            continue
        text = _read_grant_text(grant)
        if text:
            unique.setdefault(text, grant)
    return tuple(sorted(unique.items(), key=lambda item: (-len(item[0]), item[0])))


def _approved_sanitized(text: str, *, cap: int) -> SanitizedSegment:
    # Admission is finalized by LLMEgressFirewall so every denial is reported
    # as its content-free EgressBlocked decision. Keep only the local type and
    # byte bound here; the firewall repeats secret/base64/path scans on the
    # rendered request immediately before dispatch.
    if not isinstance(text, str):
        raise TypeError("sanitized segment must be text")
    if cap <= 0 or len(text.encode("utf-8")) > cap:
        raise ValueError("sanitized segment exceeds byte cap")
    return SanitizedSegment(text)


def _segment_text(
    text: str,
    grant_texts: Sequence[tuple[str, SourceGrant]],
    used_grants: dict[str, SourceGrant],
    *,
    sanitized_cap: int,
) -> SanitizedSegment | SourceBoundSegment | OutboundText:
    matches: list[tuple[int, int, SourceGrant]] = []
    cursor = 0
    while cursor < len(text):
        chosen: tuple[int, int, SourceGrant] | None = None
        for granted_text, grant in grant_texts:
            start = text.find(granted_text, cursor)
            if start < 0:
                continue
            candidate = (start, start + len(granted_text), grant)
            if chosen is None or candidate[:2] < chosen[:2]:
                chosen = candidate
        if chosen is None:
            break
        matches.append(chosen)
        cursor = chosen[1]

    if not matches:
        return _approved_sanitized(text, cap=sanitized_cap)

    segments: list[SanitizedSegment | SourceBoundSegment] = []
    cursor = 0
    for start, end, grant in matches:
        if start > cursor:
            segments.append(_approved_sanitized(text[cursor:start], cap=sanitized_cap))
        digest = source_grant_digest(grant)
        segments.append(SourceBoundSegment(digest))
        used_grants[digest] = grant
        cursor = end
    if cursor < len(text):
        segments.append(_approved_sanitized(text[cursor:], cap=sanitized_cap))
    return segments[0] if len(segments) == 1 else OutboundText(tuple(segments))


def _typed_payload(
    value: Any,
    grant_texts: Sequence[tuple[str, SourceGrant]],
    used_grants: dict[str, SourceGrant],
    *,
    sanitized_cap: int,
    field_name: str | None = None,
) -> Any:
    if isinstance(value, str):
        if field_name in _PROTOCOL_LITERAL_FIELDS and value in _PROTOCOL_LITERAL_VALUES:
            return LiteralSegment(value)
        return _segment_text(
            value,
            grant_texts,
            used_grants,
            sanitized_cap=sanitized_cap,
        )
    if isinstance(value, Mapping):
        return {
            key: _typed_payload(
                item,
                grant_texts,
                used_grants,
                sanitized_cap=sanitized_cap,
                field_name=key,
            )
            for key, item in value.items()
        }
    if isinstance(value, (list, tuple)):
        return [
            _typed_payload(
                item,
                grant_texts,
                used_grants,
                sanitized_cap=sanitized_cap,
                field_name=field_name,
            )
            for item in value
        ]
    return value


def _structural_literal_hashes(value: Any) -> frozenset[str]:
    literals: set[str] = set()

    def visit(item: Any) -> None:
        if isinstance(item, Mapping):
            for key, child in item.items():
                if isinstance(key, str):
                    literals.add(key)
                    if (
                        key in _PROTOCOL_LITERAL_FIELDS
                        and isinstance(child, str)
                        and child in _PROTOCOL_LITERAL_VALUES
                    ):
                        literals.add(child)
                visit(child)
        elif isinstance(item, (list, tuple)):
            for child in item:
                visit(child)
        elif item is None or isinstance(item, (bool, int)):
            literals.add(json.dumps(item, ensure_ascii=True, separators=(",", ":")))
        elif isinstance(item, float) and math.isfinite(item):
            literals.add(
                json.dumps(
                    item, ensure_ascii=True, allow_nan=False, separators=(",", ":")
                )
            )

    visit(value)
    return frozenset(static_literal_sha256(literal) for literal in literals)


def _route_for_agent(agent: Any, route: Any | None) -> Any:
    if route is not None:
        return route
    provider = str(getattr(agent, "provider", "") or "")
    base_url = getattr(agent, "base_url", None)
    api_mode = getattr(agent, "api_mode", None)
    if provider == "openai-codex" and not base_url:
        base_url = "https://chatgpt.com/backend-api/codex"
        api_mode = api_mode or "codex_responses"
    return SimpleNamespace(
        provider=provider,
        model=str(getattr(agent, "model", "") or ""),
        base_url=base_url,
        api_mode=api_mode,
    )


def authorize_agent_sdk_kwargs(
    agent: Any,
    kwargs: Mapping[str, Any],
    *,
    route: Any | None = None,
    sdk_control_keys: Sequence[str] = _SDK_CONTROL_KEYS,
) -> tuple[dict[str, Any], AuthorizedEgress]:
    controls = {key: kwargs[key] for key in sdk_control_keys if key in kwargs}
    body = {key: value for key, value in kwargs.items() if key not in controls}
    session_id = str(getattr(agent, "session_id", "") or "")
    turn_id = str(getattr(agent, "_current_turn_id", "") or "")
    request_id = str(getattr(agent, "_current_api_request_id", "") or "")
    policy_digest = str(
        getattr(agent, "_llm_egress_policy_digest", "")
        or getattr(agent, "llm_egress_policy_digest", "")
        or DEFAULT_POLICY_DIGEST
    )
    registry = getattr(agent, "_source_provenance_registry", None)
    grants = (
        registry.grants_for_request(request_id)
        if isinstance(registry, SourceProvenanceRegistry)
        else ()
    )
    sanitized_segment_cap = int(
        getattr(agent, "_llm_egress_max_sanitized_segment_bytes", 32_768)
    )
    sanitized_aggregate_cap = int(
        getattr(agent, "_llm_egress_max_sanitized_bytes", 32_768)
    )
    used_grants: dict[str, SourceGrant] = {}
    typed_body = _typed_payload(
        body,
        _grant_texts(grants),
        used_grants,
        sanitized_cap=sanitized_segment_cap,
    )
    request = TypedOutboundRequest(
        payload=typed_body,
        session_id=session_id,
        turn_id=turn_id,
        request_id=request_id,
        policy_digest=policy_digest,
    )
    state_dir = Path(
        getattr(agent, "_llm_egress_state_dir", "")
        or Path.home() / ".hermes" / "egress"
    )
    max_serialized_bytes = int(
        getattr(agent, "_llm_egress_max_serialized_bytes", 262_144)
    )
    max_conservative_tokens = int(
        getattr(agent, "_llm_egress_max_conservative_tokens", 87_382)
    )
    firewall = LLMEgressFirewall(
        state_dir,
        policy_digest=policy_digest,
        max_serialized_bytes=max_serialized_bytes,
        max_conservative_tokens=max_conservative_tokens,
        max_granted_serialized_bytes=int(
            getattr(
                agent,
                "_llm_egress_max_granted_serialized_bytes",
                max_serialized_bytes,
            )
        ),
        max_granted_conservative_tokens=int(
            getattr(
                agent,
                "_llm_egress_max_granted_conservative_tokens",
                max_conservative_tokens,
            )
        ),
        max_sanitized_bytes=sanitized_aggregate_cap,
        max_sanitized_segment_bytes=sanitized_segment_cap,
        static_literal_hashes_by_policy={
            policy_digest: _structural_literal_hashes(body)
        },
    )
    authorization = firewall.authorize(
        request,
        _route_for_agent(agent, route),
        grants=tuple(used_grants.values()),
    )
    rebuilt = json.loads(authorization.payload_bytes)
    if not isinstance(rebuilt, dict):
        raise TypeError("authorized provider payload must be a JSON object")
    rebuilt.update(controls)
    return rebuilt, authorization


def dispatch_authorized_agent_request(
    agent: Any,
    kwargs: Mapping[str, Any],
    callback: Callable[[dict[str, Any]], Any],
    *,
    route: Any | None = None,
    sdk_control_keys: Sequence[str] = _SDK_CONTROL_KEYS,
) -> Any:
    resolved_route = _route_for_agent(agent, route)
    destination = classify_destination(
        str(getattr(resolved_route, "provider", "") or ""),
        getattr(resolved_route, "base_url", None),
        getattr(resolved_route, "api_mode", None),
    )
    if destination in {DestinationClass.LOCAL_PROCESS, DestinationClass.LOOPBACK}:
        return callback(dict(kwargs))
    authorized, receipt = authorize_agent_sdk_kwargs(
        agent,
        kwargs,
        route=resolved_route,
        sdk_control_keys=sdk_control_keys,
    )
    # Recreate the exact body digest immediately before the provider callback.
    # Only explicit non-content SDK controls are excluded; headers/query are
    # scanned and included in the firewall-authorized JSON body.
    wire_body = {
        key: value for key, value in authorized.items() if key not in sdk_control_keys
    }
    wire_bytes = json.dumps(
        wire_body,
        ensure_ascii=False,
        allow_nan=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    receipt.verify_payload(wire_bytes)
    return callback(MappingProxyType(authorized))
