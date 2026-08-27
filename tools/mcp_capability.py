"""Process-local capabilities for privileged portable MCP workflow calls.

The signing key never leaves the Hermes gateway process.  Portable MCP
servers receive only the signed envelope and must pass it unchanged to a
trusted in-process verifier before any native action is dispatched.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import math
import re
import secrets
import time
from typing import Any, Mapping


CAPABILITY_META_KEY = "com.hermes/capability"
CAPABILITY_VERSION = 1
CAPABILITY_TTL_SECONDS = 300
MAX_CAPABILITY_TTL_SECONDS = 300
MAX_SAFE_INTEGER = 9_007_199_254_740_991
MAX_CAPABILITY_ARGUMENT_BYTES = 64 * 1024

_DIGEST_RE = re.compile(r"^sha256:[0-9a-f]{64}$")
_HEX_RE = re.compile(r"^[0-9a-f]{64}$")
_NONCE_RE = re.compile(r"^[0-9a-f]{32}$")
_SIGNING_KEY = secrets.token_bytes(32)
_CLAIM_FIELDS = frozenset({
    "version",
    "audience",
    "binding",
    "package_digest",
    "platform",
    "profile",
    "chat_id",
    "session_id",
    "message_id",
    "tool_call_id",
    "workflow",
    "arguments_sha256",
    "issued_at",
    "expires_at",
    "nonce",
})


class McpCapabilityError(PermissionError):
    """A portable MCP capability was absent, malformed, or unauthorized."""


def _canonical_json(value: Any) -> str:
    def _reject_constant(_value: str) -> None:
        raise ValueError("non-finite JSON number")

    # Round-trip through the JSON data model so custom mappings, tuples, and
    # non-finite numbers cannot gain implementation-dependent signatures.
    encoded = json.dumps(
        value,
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    parsed = json.loads(encoded, parse_constant=_reject_constant)
    return json.dumps(
        parsed,
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def canonical_arguments_digest(arguments: Any) -> str:
    """Return the canonical SHA-256 digest for exact model arguments."""

    if not isinstance(arguments, Mapping):
        raise McpCapabilityError("MCP workflow arguments must be an object")
    try:
        canonical = _canonical_json(dict(arguments))
    except (TypeError, ValueError, OverflowError) as exc:
        raise McpCapabilityError("MCP workflow arguments are not strict JSON") from exc
    encoded = canonical.encode("utf-8")
    if len(encoded) > MAX_CAPABILITY_ARGUMENT_BYTES:
        raise McpCapabilityError("MCP workflow arguments exceed the capability bound")
    return hashlib.sha256(encoded).hexdigest()


def _bounded_identity(value: Any, *, maximum: int = 256) -> str:
    if (
        not isinstance(value, str)
        or not value
        or value != value.strip()
        or len(value.encode("utf-8")) > maximum
        or any(ord(char) < 0x20 or ord(char) == 0x7F for char in value)
    ):
        raise McpCapabilityError("MCP capability identity is malformed")
    return value


def _session_claims(session: Mapping[str, Any]) -> dict[str, str]:
    required = {
        "platform",
        "profile",
        "chat_id",
        "session_id",
        "message_id",
        "tool_call_id",
    }
    if not isinstance(session, Mapping) or not required <= set(session):
        raise McpCapabilityError("The exact active Hermes turn is required")
    return {key: _bounded_identity(session.get(key)) for key in sorted(required)}


def _signature(claims: Mapping[str, Any]) -> str:
    encoded = _canonical_json(dict(claims)).encode("utf-8")
    return hmac.new(_SIGNING_KEY, encoded, hashlib.sha256).hexdigest()


def mint_mcp_capability(
    *,
    audience: str,
    binding: str,
    package_digest: str,
    workflow: str,
    arguments: Mapping[str, Any],
    session: Mapping[str, Any],
    now: int | None = None,
    ttl_seconds: int = CAPABILITY_TTL_SECONDS,
) -> dict[str, Any]:
    """Mint one capability for one exact logical public workflow call."""

    audience = _bounded_identity(audience)
    binding = _bounded_identity(binding)
    workflow = _bounded_identity(workflow, maximum=128)
    if (
        not isinstance(package_digest, str)
        or _DIGEST_RE.fullmatch(package_digest) is None
    ):
        raise McpCapabilityError("Portable package digest is malformed")
    if (
        type(ttl_seconds) is not int
        or not 1 <= ttl_seconds <= MAX_CAPABILITY_TTL_SECONDS
    ):
        raise McpCapabilityError("MCP capability lifetime is invalid")
    issued_at = int(time.time()) if now is None else now
    if (
        type(issued_at) is not int
        or not 0 < issued_at <= MAX_SAFE_INTEGER - ttl_seconds
    ):
        raise McpCapabilityError("MCP capability timestamp is invalid")

    claims: dict[str, Any] = {
        "version": CAPABILITY_VERSION,
        "audience": audience,
        "binding": binding,
        "package_digest": package_digest,
        **_session_claims(session),
        "workflow": workflow,
        "arguments_sha256": canonical_arguments_digest(arguments),
        "issued_at": issued_at,
        "expires_at": issued_at + ttl_seconds,
        "nonce": secrets.token_hex(16),
    }
    return {"claims": claims, "signature": _signature(claims)}


def verify_mcp_capability(
    capability: Any,
    *,
    expected_audience: str,
    expected_workflow: str,
    expected_arguments: Mapping[str, Any],
    now: int | None = None,
) -> dict[str, Any]:
    """Verify a capability and return a detached claims dictionary.

    Active-turn and replay checks are deliberately left to the native bridge,
    which owns that live state.  Every structural and signed binding check is
    performed here before those checks run.
    """

    if not isinstance(capability, dict) or set(capability) != {"claims", "signature"}:
        raise McpCapabilityError("MCP capability is malformed")
    claims = capability.get("claims")
    signature = capability.get("signature")
    if not isinstance(claims, dict) or set(claims) != _CLAIM_FIELDS:
        raise McpCapabilityError("MCP capability claims are malformed")
    if not isinstance(signature, str) or _HEX_RE.fullmatch(signature) is None:
        raise McpCapabilityError("MCP capability signature is malformed")
    try:
        expected_signature = _signature(claims)
    except (TypeError, ValueError, OverflowError) as exc:
        raise McpCapabilityError("MCP capability claims are malformed") from exc
    if not hmac.compare_digest(signature, expected_signature):
        raise McpCapabilityError("MCP capability signature is invalid")

    if (
        type(claims.get("version")) is not int
        or claims["version"] != CAPABILITY_VERSION
    ):
        raise McpCapabilityError("MCP capability version is invalid")
    for key in (
        "audience",
        "binding",
        "platform",
        "profile",
        "chat_id",
        "session_id",
        "message_id",
        "tool_call_id",
    ):
        _bounded_identity(claims.get(key))
    _bounded_identity(claims.get("workflow"), maximum=128)
    if _DIGEST_RE.fullmatch(str(claims.get("package_digest", ""))) is None:
        raise McpCapabilityError("MCP capability package digest is malformed")
    if _HEX_RE.fullmatch(str(claims.get("arguments_sha256", ""))) is None:
        raise McpCapabilityError("MCP capability arguments digest is malformed")
    if _NONCE_RE.fullmatch(str(claims.get("nonce", ""))) is None:
        raise McpCapabilityError("MCP capability nonce is malformed")

    issued_at = claims.get("issued_at")
    expires_at = claims.get("expires_at")
    if (
        type(issued_at) is not int
        or type(expires_at) is not int
        or not 0 < issued_at < expires_at <= MAX_SAFE_INTEGER
        or expires_at - issued_at > MAX_CAPABILITY_TTL_SECONDS
    ):
        raise McpCapabilityError("MCP capability lifetime is malformed")
    current = int(time.time()) if now is None else now
    if type(current) is not int or current < issued_at or current >= expires_at:
        raise McpCapabilityError("MCP capability is not currently valid")

    if not hmac.compare_digest(
        claims["audience"], _bounded_identity(expected_audience)
    ):
        raise McpCapabilityError("MCP capability audience is invalid")
    if not hmac.compare_digest(
        claims["workflow"], _bounded_identity(expected_workflow, maximum=128)
    ):
        raise McpCapabilityError("MCP capability workflow is invalid")
    expected_digest = canonical_arguments_digest(expected_arguments)
    if not hmac.compare_digest(claims["arguments_sha256"], expected_digest):
        raise McpCapabilityError("MCP capability arguments are invalid")
    return dict(claims)
