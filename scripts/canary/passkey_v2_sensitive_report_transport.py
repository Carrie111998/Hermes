#!/usr/bin/env python3
"""Cloud-to-owner-gate transport for one exact sensitive report request.

The trusted model selects the explicit operational operation and authors its
arguments.  This module does not inspect prose or infer sensitivity.  It
mechanically binds the Canonical Writer's signed capability to one deterministic
passkey request, exposes only the public approval URL, and returns the signed
single-use authorization bundle after the authenticated user approves it.
"""

from __future__ import annotations

import base64
import hashlib
import http.client
import json
import ssl
from dataclasses import dataclass
from typing import Any, Callable, Mapping

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

from scripts.canary import passkey_v2_protocol as protocol
from scripts.canary import passkey_v2_sensitive_report as sensitive


FRAME_SCHEMA = "muncho-sensitive-report-owner-gate-frame.v1"
RESPONSE_SCHEMA = "muncho-sensitive-report-owner-gate-response.v1"
PUBLIC_PATH = "/internal/sensitive-report"
PUBLIC_HOST = "auth.lomliev.com"
MAXIMUM_RESPONSE_BYTES = 512 * 1024
_OPERATIONS = frozenset({"create", "consume"})
_ENVELOPE_FIELDS = frozenset({"schema", "key_id", "payload", "signature_b64"})


class SensitiveReportTransportError(RuntimeError):
    """Stable, secret-free transport failure."""


def _fail(code: str) -> None:
    raise SensitiveReportTransportError(code)


def _envelope(value: Any) -> Mapping[str, Any]:
    if (
        not isinstance(value, Mapping)
        or set(value) != _ENVELOPE_FIELDS
        or value.get("schema") != "muncho-ed25519-envelope.v1"
        or not isinstance(value.get("key_id"), str)
        or not isinstance(value.get("payload"), Mapping)
        or not isinstance(value.get("signature_b64"), str)
    ):
        _fail("sensitive_report_transport_capability_invalid")
    try:
        signature = base64.b64decode(value["signature_b64"], validate=True)
    except (TypeError, ValueError):
        _fail("sensitive_report_transport_capability_invalid")
    if len(signature) != 64:
        _fail("sensitive_report_transport_capability_invalid")
    return dict(value)


def _capability_and_intent(
    capability_envelope: Any,
    intent_value: Any,
    *,
    writer_key_id: str,
    writer_public_key: Ed25519PublicKey,
    now_unix: int,
) -> tuple[Mapping[str, Any], Mapping[str, Any], Mapping[str, Any]]:
    envelope = _envelope(capability_envelope)
    try:
        pinned_key_id = protocol.sha256_bytes(
            writer_public_key.public_bytes_raw()
        )
        if envelope["key_id"] != writer_key_id or writer_key_id != pinned_key_id:
            raise ValueError
        writer_public_key.verify(
            base64.b64decode(envelope["signature_b64"], validate=True),
            protocol.canonical_json_bytes(envelope["payload"]),
        )
        capability = sensitive._capability(envelope["payload"])
        intent = sensitive._intent(intent_value)
    except Exception as exc:
        raise SensitiveReportTransportError(
            "sensitive_report_transport_capability_invalid"
        ) from exc
    if (
        intent["operation_id"] != sensitive.OPERATION_ID
        or capability["operation_id"] != intent["operation_id"]
        or capability["arguments_sha256"] != intent["arguments_sha256"]
        or capability["idempotency_key"] != intent["idempotency_key"]
        or not capability["issued_at_unix_ms"]
        <= now_unix * 1000
        < capability["expires_at_unix_ms"]
    ):
        _fail("sensitive_report_transport_operation_invalid")
    return envelope, capability, intent


def retrieval_token(capability_envelope: Any) -> bytes:
    envelope = _envelope(capability_envelope)
    return hashlib.sha256(
        b"muncho-sensitive-report-retrieval.v1\x00"
        + protocol.canonical_json_bytes(envelope)
    ).digest()


def deterministic_request_id(
    capability_envelope: Any, intent: Any
) -> str:
    token = retrieval_token(capability_envelope)
    digest = hashlib.sha256(
        b"muncho-sensitive-report-request.v1\x00"
        + token
        + protocol.canonical_json_bytes(sensitive._intent(intent))
    ).digest()
    return base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")


def build_frame(
    *,
    operation: str,
    capability_envelope: Mapping[str, Any],
    intent: Any,
    runtime_binding: Mapping[str, Any] | None = None,
) -> Mapping[str, Any]:
    if operation not in _OPERATIONS:
        _fail("sensitive_report_transport_operation_invalid")
    if (operation == "create") != (runtime_binding is None):
        _fail("sensitive_report_transport_runtime_invalid")
    unsigned = {
        "schema": FRAME_SCHEMA,
        "operation": operation,
        "capability_envelope": dict(capability_envelope),
        "operational_intent": sensitive._intent(intent),
        "request_id": deterministic_request_id(capability_envelope, intent),
        "runtime_binding": (
            None if runtime_binding is None else dict(runtime_binding)
        ),
    }
    return {
        **unsigned,
        "frame_sha256": hashlib.sha256(protocol.canonical_json_bytes(unsigned)).hexdigest(),
    }


def validate_frame(
    value: Any,
    *,
    writer_key_id: str,
    writer_public_key: Ed25519PublicKey,
    now_unix: int,
) -> tuple[Mapping[str, Any], Mapping[str, Any], Mapping[str, Any]]:
    fields = {
        "schema", "operation", "capability_envelope", "operational_intent",
        "request_id", "runtime_binding", "frame_sha256",
    }
    if not isinstance(value, Mapping) or set(value) != fields:
        _fail("sensitive_report_transport_frame_invalid")
    frame = dict(value)
    unsigned = {key: item for key, item in frame.items() if key != "frame_sha256"}
    if (
        frame.get("schema") != FRAME_SCHEMA
        or frame.get("operation") not in _OPERATIONS
        or frame.get("frame_sha256")
        != hashlib.sha256(protocol.canonical_json_bytes(unsigned)).hexdigest()
    ):
        _fail("sensitive_report_transport_frame_invalid")
    envelope, capability, intent = _capability_and_intent(
        frame["capability_envelope"],
        frame["operational_intent"],
        writer_key_id=writer_key_id,
        writer_public_key=writer_public_key,
        now_unix=now_unix,
    )
    if frame.get("request_id") != deterministic_request_id(
        envelope, intent
    ):
        _fail("sensitive_report_transport_request_binding_invalid")
    runtime = frame.get("runtime_binding")
    if frame["operation"] == "create":
        if runtime is not None:
            _fail("sensitive_report_transport_runtime_invalid")
    else:
        try:
            protocol.validate_runtime_binding(runtime)
        except Exception as exc:
            raise SensitiveReportTransportError(
                "sensitive_report_transport_runtime_invalid"
            ) from exc
    return frame, capability, intent


def build_runtime_binding(
    *,
    action_envelope: Mapping[str, Any],
    capability_envelope: Mapping[str, Any],
    intent: Any,
) -> Mapping[str, Any]:
    action = sensitive.validate_action_envelope(action_envelope)
    return protocol.build_runtime_binding(
        executor_release_sha=action["executor_release_sha"],
        executor_plan_sha256=sensitive.operational_command_sha256(intent),
        executor_binary_sha256=hashlib.sha256(
            protocol.canonical_json_bytes(sensitive._intent(intent))
        ).hexdigest(),
        mutation_wrapper_sha256=hashlib.sha256(
            protocol.canonical_json_bytes(dict(capability_envelope))
        ).hexdigest(),
        remote_transport_sha256=hashlib.sha256(
            FRAME_SCHEMA.encode("ascii")
        ).hexdigest(),
    )


def step_up_bundle(
    *,
    response: Mapping[str, Any],
    capability_envelope: Mapping[str, Any],
) -> Mapping[str, Any]:
    required = {
        "schema", "operation", "state", "request_id", "approval_url",
        "action_envelope", "challenge_record", "grant_record",
        "authorization_receipt",
    }
    if (
        not isinstance(response, Mapping)
        or set(response) != required
        or response.get("schema") != RESPONSE_SCHEMA
        or response.get("operation") != "consume"
        or response.get("state") != "authorized"
        or not isinstance(response.get("action_envelope"), Mapping)
        or not isinstance(response.get("challenge_record"), Mapping)
        or not isinstance(response.get("grant_record"), Mapping)
        or not isinstance(response.get("authorization_receipt"), Mapping)
    ):
        _fail("sensitive_report_transport_response_invalid")
    return {
        "schema": "muncho-sensitive-report-operational-step-up.v1",
        "retrieval_token_b64": base64.b64encode(
            retrieval_token(capability_envelope)
        ).decode("ascii"),
        "action_envelope": dict(response["action_envelope"]),
        "challenge_record": dict(response["challenge_record"]),
        "grant_record": dict(response["grant_record"]),
        "authorization_receipt": dict(response["authorization_receipt"]),
    }


@dataclass(frozen=True)
class SensitiveReportOwnerGateClient:
    requester: Callable[[bytes], tuple[int, bytes]] | None = None

    def _request(self, raw: bytes) -> tuple[int, bytes]:
        context = ssl.create_default_context()
        connection = http.client.HTTPSConnection(
            PUBLIC_HOST, 443, timeout=10, context=context
        )
        try:
            connection.request(
                "POST",
                PUBLIC_PATH,
                body=raw,
                headers={
                    "Content-Type": "application/json",
                    "Content-Length": str(len(raw)),
                    "X-Muncho-Relay": "sensitive-report-v1",
                },
            )
            response = connection.getresponse()
            body = response.read(MAXIMUM_RESPONSE_BYTES + 1)
            return response.status, body
        finally:
            connection.close()

    def call(self, frame: Mapping[str, Any]) -> Mapping[str, Any]:
        raw = protocol.canonical_json_bytes(frame)
        status, body = (self.requester or self._request)(raw)
        if status not in {200, 409} or not body or len(body) > MAXIMUM_RESPONSE_BYTES:
            _fail("sensitive_report_transport_unavailable")
        try:
            value = json.loads(body.decode("utf-8", errors="strict"))
        except (UnicodeError, ValueError, json.JSONDecodeError) as exc:
            raise SensitiveReportTransportError(
                "sensitive_report_transport_response_invalid"
            ) from exc
        if not isinstance(value, Mapping):
            _fail("sensitive_report_transport_response_invalid")
        return dict(value)


__all__ = [
    "FRAME_SCHEMA",
    "PUBLIC_HOST",
    "PUBLIC_PATH",
    "RESPONSE_SCHEMA",
    "SensitiveReportOwnerGateClient",
    "SensitiveReportTransportError",
    "build_frame",
    "build_runtime_binding",
    "deterministic_request_id",
    "retrieval_token",
    "step_up_bundle",
    "validate_frame",
]
