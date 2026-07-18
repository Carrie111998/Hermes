from __future__ import annotations

import base64
from collections.abc import Sequence
from dataclasses import dataclass
from enum import StrEnum
import hashlib
import hmac
import json
import re
from typing import Any


class Provider(StrEnum):
    CLAUDE = "claude"
    CODEX = "codex"
    HERMES = "hermes"


class OriginKind(StrEnum):
    NATIVE = "native"
    BRIDGE_PLACEHOLDER = "bridge_placeholder"
    BRIDGE_CONTINUATION = "bridge_continuation"


class Relation(StrEnum):
    MIRRORS = "mirrors"
    CONTINUES = "continues"
    FORKS = "forks"


class MirrorJobState(StrEnum):
    QUEUED = "queued"
    RUNNING = "running"
    RETRY = "retry"
    SUCCEEDED = "succeeded"
    MANUAL_FAILURE = "manual_failure"


class SidebarJobState(StrEnum):
    PENDING = "sidebar_pending"
    LEASED = "sidebar_leased"
    VISIBLE = "sidebar_visible"
    RETRY = "sidebar_retry"
    FAILED = "sidebar_failed"


@dataclass(frozen=True)
class ProjectedMessage:
    native_event_id: str
    ordinal: int
    role: str
    content: str | None
    timestamp: float
    tool_name: str | None = None
    tool_calls: list[dict[str, Any]] | None = None
    tool_call_id: str | None = None
    reasoning: str | None = None


@dataclass(frozen=True)
class SessionProjection:
    provider: Provider
    native_id: str
    title: str | None
    cwd: str | None
    started_at: float
    last_active: float
    messages: Sequence[ProjectedMessage]
    native_path: str | None = None
    native_status: str = "active"
    native_cursor: str | None = None
    native_hash: str | None = None
    parser_version: int = 1
    origin_kind: OriginKind = OriginKind.NATIVE
    origin_bridge_id: str | None = None
    git_branch: str | None = None


@dataclass(frozen=True)
class BridgeMarkerPayload:
    bridge_id: str
    source_session_id: str
    target_provider: Provider
    policy_generation: int


@dataclass(frozen=True)
class UpsertResult:
    session_id: str
    inserted_messages: int
    rebuilt: bool
    first_seen: bool


@dataclass(frozen=True)
class SessionLink:
    id: str
    from_session_id: str
    to_session_id: str
    relation: Relation
    bridge_id: str
    source_cursor: str | None
    source_hash: str | None
    created_at: float


@dataclass(frozen=True)
class ContextPack:
    id: str
    bridge_id: str
    source_session_id: str
    target_session_id: str | None
    source_cursor: str
    source_hash: str
    budget_chars: int
    payload: str
    created_at: float
    immutable_at: float | None = None


class InvalidBridgeMarker(ValueError):
    """Raised when a bridge marker cannot be authenticated or decoded."""


_MARKER_PREFIX = "HERMES_SESSION_BRIDGE_V1"
_MARKER_FIELDS = {
    "bridge_id",
    "policy_generation",
    "source_session_id",
    "target_provider",
}
_BASE64URL_RE = re.compile(r"^[A-Za-z0-9_-]+$")


def canonical_session_id(provider: Provider | str, native_id: str) -> str:
    try:
        normalized_provider = Provider(provider)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"unknown session provider: {provider!r}") from exc

    if not isinstance(native_id, str) or not native_id.strip():
        raise ValueError("native session ID must not be empty")

    normalized_native_id = native_id.strip()
    if normalized_provider is Provider.HERMES:
        if normalized_native_id.startswith(("claude:", "codex:")):
            raise ValueError(
                "Hermes session ID uses a reserved external-provider prefix"
            )
        return normalized_native_id
    return f"{normalized_provider.value}:{normalized_native_id}"


def stable_message_key(message: ProjectedMessage) -> str:
    if (
        not isinstance(message.native_event_id, str)
        or not message.native_event_id.strip()
    ):
        raise ValueError("native event ID must not be empty")
    if (
        not isinstance(message.ordinal, int)
        or isinstance(message.ordinal, bool)
        or message.ordinal < 0
    ):
        raise ValueError("message ordinal must be a non-negative integer")

    identity = (
        f"{len(message.native_event_id)}:{message.native_event_id}:{message.ordinal}"
    )
    return hashlib.sha256(identity.encode("utf-8")).hexdigest()


def encode_bridge_marker(payload: BridgeMarkerPayload, secret: bytes) -> str:
    secret_bytes = _validated_secret(secret)
    try:
        target_provider = Provider(payload.target_provider)
        if target_provider not in (Provider.CLAUDE, Provider.CODEX):
            raise InvalidBridgeMarker("bridge marker target must be Claude or Codex")
        if not isinstance(payload.bridge_id, str):
            raise InvalidBridgeMarker("bridge marker bridge_id must be a string")
        if not isinstance(payload.source_session_id, str):
            raise InvalidBridgeMarker(
                "bridge marker source_session_id must be a string"
            )
        if not isinstance(payload.policy_generation, int) or isinstance(
            payload.policy_generation, bool
        ):
            raise InvalidBridgeMarker(
                "bridge marker policy_generation must be an integer"
            )

        body = json.dumps(
            {
                "bridge_id": payload.bridge_id,
                "policy_generation": payload.policy_generation,
                "source_session_id": payload.source_session_id,
                "target_provider": target_provider.value,
            },
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        encoded_body = _encode_base64url(body)
        signature = hmac.new(
            secret_bytes, encoded_body.encode("ascii"), hashlib.sha256
        ).digest()
        return f"{_MARKER_PREFIX}:{encoded_body}.{_encode_base64url(signature)}"
    except InvalidBridgeMarker:
        raise
    except (AttributeError, TypeError, ValueError) as exc:
        raise InvalidBridgeMarker("invalid bridge marker payload") from exc


def decode_bridge_marker(marker: str, secret: bytes) -> BridgeMarkerPayload:
    secret_bytes = _validated_secret(secret)
    try:
        if not isinstance(marker, str) or marker.count(":") != 1:
            raise InvalidBridgeMarker("malformed bridge marker")
        prefix, encoded_and_signature = marker.split(":", 1)
        if prefix != _MARKER_PREFIX or encoded_and_signature.count(".") != 1:
            raise InvalidBridgeMarker("malformed bridge marker")
        encoded_body, encoded_signature = encoded_and_signature.split(".", 1)
        if not encoded_body or not encoded_signature:
            raise InvalidBridgeMarker("malformed bridge marker")

        expected_signature = _encode_base64url(
            hmac.new(
                secret_bytes, encoded_body.encode("ascii"), hashlib.sha256
            ).digest()
        )
        if not hmac.compare_digest(encoded_signature, expected_signature):
            raise InvalidBridgeMarker("bridge marker signature mismatch")

        body = _decode_base64url(encoded_body)
        if _encode_base64url(body) != encoded_body:
            raise InvalidBridgeMarker("noncanonical bridge marker body encoding")
        decoded = json.loads(body.decode("utf-8"))
        if not isinstance(decoded, dict) or set(decoded) != _MARKER_FIELDS:
            raise InvalidBridgeMarker("malformed bridge marker payload")

        bridge_id = decoded["bridge_id"]
        policy_generation = decoded["policy_generation"]
        source_session_id = decoded["source_session_id"]
        target_provider_value = decoded["target_provider"]
        if not isinstance(bridge_id, str) or not isinstance(source_session_id, str):
            raise InvalidBridgeMarker("malformed bridge marker payload")
        if not isinstance(policy_generation, int) or isinstance(
            policy_generation, bool
        ):
            raise InvalidBridgeMarker("malformed bridge marker payload")
        if not isinstance(target_provider_value, str):
            raise InvalidBridgeMarker("malformed bridge marker payload")

        try:
            target_provider = Provider(target_provider_value)
        except ValueError as exc:
            raise InvalidBridgeMarker("unknown bridge marker target provider") from exc
        if target_provider not in (Provider.CLAUDE, Provider.CODEX):
            raise InvalidBridgeMarker("bridge marker target must be Claude or Codex")

        canonical_body = json.dumps(
            {
                "bridge_id": bridge_id,
                "policy_generation": policy_generation,
                "source_session_id": source_session_id,
                "target_provider": target_provider.value,
            },
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        if body != canonical_body:
            raise InvalidBridgeMarker("noncanonical bridge marker payload")

        return BridgeMarkerPayload(
            bridge_id=bridge_id,
            source_session_id=source_session_id,
            target_provider=target_provider,
            policy_generation=policy_generation,
        )
    except InvalidBridgeMarker:
        raise
    except (UnicodeError, ValueError, TypeError, KeyError, json.JSONDecodeError) as exc:
        raise InvalidBridgeMarker("malformed bridge marker") from exc


def _validated_secret(secret: bytes) -> bytes:
    if not isinstance(secret, bytes) or not secret:
        raise InvalidBridgeMarker("bridge marker secret must not be empty")
    return secret


def _encode_base64url(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).rstrip(b"=").decode("ascii")


def _decode_base64url(value: str) -> bytes:
    if not isinstance(value, str) or not _BASE64URL_RE.fullmatch(value):
        raise InvalidBridgeMarker("malformed base64url data")
    padded = value + "=" * (-len(value) % 4)
    try:
        return base64.b64decode(padded, altchars=b"-_", validate=True)
    except (ValueError, TypeError) as exc:
        raise InvalidBridgeMarker("malformed base64url data") from exc
