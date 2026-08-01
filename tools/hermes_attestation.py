"""Hermes client attestations for protected MCP prepare/execute rails."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal
import base64
import hashlib
import hmac
import json
import math
import os
import re
import secrets
import time
from typing import Any, Mapping


DIRECT_THREAD_SENTINEL = "__hermes_direct_no_thread__"
ATTESTATION_META_KEY = "dps.hermes.attestation"
_ENV_NAME_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_KID_RE = re.compile(r"^[A-Za-z0-9._-]{1,80}$")
_HEX64_RE = re.compile(r"^[0-9a-f]{64}$")
_MAX_PROPOSAL_TOKEN_BYTES = 48_000
_MAX_PROPOSAL_PAYLOAD_BYTES = 48_000
_JS_SAFE_INTEGER_MAX = 2**53 - 1
_INTERNAL_ARG_FIELDS = {
    "_meta",
    "__dps_hermes_attestation",
    "dps_hermes_attestation",
    "hermes_attestation",
}


class HermesAttestationError(ValueError):
    """Fail-closed attestation/config/proposal error with a non-secret reason."""


@dataclass(frozen=True)
class HermesAttestationPolicy:
    enabled: bool
    kid_env: str = ""
    key_env: str = ""
    oauth_client_id_env: str = ""
    operator_subject_env: str = ""
    ttl_seconds: int = 300
    direct_thread_sentinel: str = DIRECT_THREAD_SENTINEL

    @classmethod
    def from_mapping(cls, value: Any) -> "HermesAttestationPolicy":
        if not isinstance(value, Mapping) or value.get("enabled") is not True:
            return cls(enabled=False)
        ttl = value.get("ttl_seconds", 300)
        try:
            ttl_int = int(ttl)
        except (TypeError, ValueError):
            ttl_int = 300
        ttl_int = min(max(ttl_int, 1), 300)
        sentinel = str(value.get("direct_thread_sentinel") or DIRECT_THREAD_SENTINEL).strip()
        key_env = _env_ref(value.get("key_env"), "key_env")
        if not key_env.upper().endswith("_KEY"):
            raise HermesAttestationError("attestation key_env must end with _KEY")
        return cls(
            enabled=True,
            kid_env=_env_ref(value.get("kid_env"), "kid_env"),
            key_env=key_env,
            oauth_client_id_env=_env_ref(value.get("oauth_client_id_env"), "oauth_client_id_env"),
            operator_subject_env=_env_ref(value.get("operator_subject_env"), "operator_subject_env"),
            ttl_seconds=ttl_int,
            direct_thread_sentinel=sentinel or DIRECT_THREAD_SENTINEL,
        )

    def resolve(self) -> tuple[str, str, str, str]:
        if not self.enabled:
            raise HermesAttestationError("attestation disabled")
        kid = _env_value(self.kid_env, "kid")
        key = _env_value(self.key_env, "key")
        oauth_client_id = _env_value(self.oauth_client_id_env, "oauth_client_id")
        operator_subject = _env_value(self.operator_subject_env, "operator_subject")
        if not _KID_RE.match(kid):
            raise HermesAttestationError("attestation kid is invalid")
        if len(key.encode("utf-8")) < 32:
            raise HermesAttestationError("attestation key is weak")
        return kid, key, oauth_client_id, operator_subject


def _env_ref(value: Any, label: str) -> str:
    text = str(value or "").strip()
    if text.startswith("${") and text.endswith("}"):
        text = text[2:-1].strip()
        if text.startswith("env:"):
            text = text[4:].strip()
    if not text or not _ENV_NAME_RE.match(text):
        raise HermesAttestationError(f"attestation {label} must name an env var")
    return text


def _env_value(name: str, label: str) -> str:
    value = os.environ.get(name, "")
    if not isinstance(value, str) or not value.strip():
        raise HermesAttestationError(f"attestation {label} env is missing")
    return value.strip()


def _b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).decode("ascii").rstrip("=")


def _b64url_decode(value: str) -> bytes:
    text = str(value or "")
    padding = "=" * (-len(text) % 4)
    return base64.urlsafe_b64decode((text + padding).encode("ascii"))


def sha256_hex(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def strip_internal_hermes_fields(value: Any) -> Any:
    if isinstance(value, str):
        return value
    if not isinstance(value, Mapping):
        return value
    return {key: item for key, item in value.items() if key not in _INTERNAL_ARG_FIELDS}


def canonical_hermes_tool_args_digest(value: Any) -> str:
    if isinstance(value, str):
        return sha256_hex(value)
    return sha256_hex(canonical_hermes_json(strip_internal_hermes_fields(value)))


def canonical_proposal_digest(proposal: Mapping[str, Any]) -> str:
    return sha256_hex(canonical_hermes_json(proposal))


def canonical_hermes_json(value: Any) -> str:
    return _render_json(value)


def _render_json(value: Any) -> str:
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, str):
        return json.dumps(value, ensure_ascii=False, separators=(",", ":"))
    if isinstance(value, int) and not isinstance(value, bool):
        if value < -_JS_SAFE_INTEGER_MAX or value > _JS_SAFE_INTEGER_MAX:
            raise HermesAttestationError("hermes_json safe integer unsupported")
        return str(value)
    if isinstance(value, float):
        if not math.isfinite(value):
            raise HermesAttestationError("hermes_json_number_unsupported")
        if value == 0:
            return "0"
        return _render_float(value)
    if isinstance(value, Decimal):
        if not value.is_finite():
            raise HermesAttestationError("hermes_json_number_unsupported")
        if value == 0:
            return "0"
        return format(value.normalize(), "f").rstrip("0").rstrip(".")
    if isinstance(value, (list, tuple)):
        return "[" + ",".join(_render_json(item) for item in value) + "]"
    if isinstance(value, Mapping):
        parts = []
        for key in sorted(value.keys(), key=lambda item: str(item)):
            if not isinstance(key, str) or not key:
                raise HermesAttestationError("hermes_json_key_unsupported")
            parts.append(f"{_render_json(key)}:{_render_json(value[key])}")
        return "{" + ",".join(parts) + "}"
    raise HermesAttestationError("hermes_json_value_unsupported")


def _render_float(value: float) -> str:
    text = repr(value)
    if "e" not in text and "E" not in text:
        return text
    mantissa, exponent_text = re.split("[eE]", text)
    exponent = int(exponent_text)
    if -6 <= exponent < 21:
        fixed = format(value, f".{max(0, 18)}f").rstrip("0").rstrip(".")
        return fixed or "0"
    mantissa = mantissa.rstrip("0").rstrip(".")
    sign = "+" if exponent >= 0 else "-"
    return f"{mantissa}e{sign}{abs(exponent)}"


def normalize_thread_id(thread_id: str, sentinel: str = DIRECT_THREAD_SENTINEL) -> str:
    value = str(thread_id or "").strip()
    return value or sentinel


def require_attestable_context(
    ctx: Mapping[str, str],
    policy: HermesAttestationPolicy,
) -> dict[str, str]:
    platform = str(ctx.get("platform") or "").strip()
    user_id = str(ctx.get("user_id") or "").strip()
    chat_id = str(ctx.get("chat_id") or "").strip()
    thread_id = normalize_thread_id(str(ctx.get("thread_id") or ""), policy.direct_thread_sentinel)
    session_id = str(ctx.get("session_id") or "").strip()
    session_key = str(ctx.get("session_key") or "").strip()
    message_id = str(ctx.get("message_id") or "").strip()
    if not all((platform, user_id, chat_id, thread_id, session_id, session_key, message_id)):
        raise HermesAttestationError("attestation context is incomplete")
    if platform != "telegram":
        raise HermesAttestationError("attestation requires Telegram context")
    return {
        "platform": platform,
        "platform_user_id": user_id,
        "chat_id": chat_id,
        "thread_id": thread_id,
        "session_id": session_id,
        "session_key": session_key,
        "originating_user_message_id": message_id,
    }


def sign_attestation(
    *,
    policy: HermesAttestationPolicy,
    phase: str,
    tool_name: str,
    args: Mapping[str, Any],
    ctx: Mapping[str, str],
    approval: Mapping[str, str] | None = None,
    proposal: Mapping[str, str] | None = None,
    now_seconds: int | None = None,
) -> str:
    kid, key, oauth_client_id, operator_subject = policy.resolve()
    trusted = require_attestable_context(ctx, policy)
    iat = int(now_seconds if now_seconds is not None else time.time())
    payload: dict[str, Any] = {
        "v": 1,
        "kid": kid,
        "phase": phase,
        "tool_name": tool_name,
        "oauth_client_id": oauth_client_id,
        "operator_subject": operator_subject,
        "platform": trusted["platform"],
        "platform_user_id": trusted["platform_user_id"],
        "chat_id": trusted["chat_id"],
        "thread_id": trusted["thread_id"],
        "session_id": trusted["session_id"],
        "originating_user_message_id": trusted["originating_user_message_id"],
        "iat": iat,
        "exp": iat + policy.ttl_seconds,
        "nonce": secrets.token_urlsafe(24),
        "tool_args_digest": canonical_hermes_tool_args_digest(args),
    }
    if approval:
        payload.update({
            "approval_request_id": _required(approval, "approval_request_id"),
            "approval_event_id": _required(approval, "approval_event_id"),
            "approved_at": _required(approval, "approved_at"),
        })
    if proposal:
        payload.update({
            "proposal_token_digest": _required(proposal, "proposal_token_digest"),
            "proposal_digest": _required(proposal, "proposal_digest"),
            "proposal_payload_digest": _required(proposal, "proposal_payload_digest"),
        })
    canonical = canonical_hermes_json(payload)
    signature = hmac.new(key.encode("utf-8"), canonical.encode("utf-8"), hashlib.sha256).digest()
    return f"hmac-sha256-v1.{_b64url(canonical.encode('utf-8'))}.{_b64url(signature)}"


def _required(value: Mapping[str, str], key: str) -> str:
    item = str(value.get(key) or "").strip()
    if not item:
        raise HermesAttestationError(f"attestation {key} missing")
    return item


def attestation_meta(token: str) -> dict[str, str]:
    if not token:
        raise HermesAttestationError("attestation token missing")
    return {ATTESTATION_META_KEY: token}


def decode_proposal_token_metadata(token: str) -> dict[str, str]:
    token_text = str(token or "")
    if not token_text or len(token_text.encode("utf-8")) > _MAX_PROPOSAL_TOKEN_BYTES:
        raise HermesAttestationError("proposal token missing or oversized")
    first_segment = token_text.split(".", 1)[0]
    try:
        decoded = _b64url_decode(first_segment)
    except Exception as exc:
        raise HermesAttestationError("proposal token payload is malformed") from exc
    if len(decoded) > _MAX_PROPOSAL_PAYLOAD_BYTES:
        raise HermesAttestationError("proposal token payload is oversized")
    try:
        proposal = json.loads(decoded.decode("utf-8"))
    except Exception as exc:
        raise HermesAttestationError("proposal token payload is malformed") from exc
    if not isinstance(proposal, dict) or proposal.get("v") != 1:
        raise HermesAttestationError("proposal token version is invalid")
    binding = proposal.get("hermes_binding")
    if not isinstance(binding, dict) or binding.get("v") != 1:
        raise HermesAttestationError("proposal token hermes binding is missing")
    payload_hash = proposal.get("payload_hash")
    if not isinstance(payload_hash, str) or not _HEX64_RE.match(payload_hash):
        raise HermesAttestationError("proposal token payload_hash is invalid")
    return {
        "proposal_token_digest": canonical_hermes_tool_args_digest(token_text),
        "proposal_digest": canonical_proposal_digest(proposal),
        "proposal_payload_digest": payload_hash,
    }


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")
