"""Canonical byte preparation and application-wire sealing."""

from __future__ import annotations

import hashlib
import json
import math
import re
from collections.abc import Mapping, Sequence
from typing import Any

from .types import ContractError, DraftIntent, PreparedIntent, PublicationKind, TrustedIntentPolicy

WIRE_PREFIX = b"HERMES-KANBAN-WIRE\0v1\0"
_MARKER_RE = re.compile(r"^[A-Za-z0-9_.:-]{1,128}$")


def sha256_hex(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _validate_json(value: Any, *, depth: int = 0) -> None:
    if depth > 16:
        raise ContractError("canonical manifest nesting exceeds 16")
    if value is None or isinstance(value, (str, bool, int)):
        return
    if isinstance(value, float):
        if not math.isfinite(value):
            raise ContractError("non-finite numbers are forbidden")
        # Decimal spelling is language/runtime-sensitive.  V1 deliberately
        # forbids floats rather than pretending cross-language canonicality.
        raise ContractError("floats are forbidden in canonical V1 manifests")
    if isinstance(value, Mapping):
        for key, item in value.items():
            if not isinstance(key, str):
                raise ContractError("canonical object keys must be strings")
            _validate_json(item, depth=depth + 1)
        return
    if isinstance(value, Sequence) and not isinstance(value, (bytes, bytearray, memoryview)):
        for item in value:
            _validate_json(item, depth=depth + 1)
        return
    raise ContractError(f"unsupported canonical JSON value: {type(value).__name__}")


def canonical_json_bytes(value: Any) -> bytes:
    """Encode exact UTF-8 JSON without Unicode normalization.

    Sort order, separators, and ASCII handling are fixed.  Unicode code-point
    sequences are preserved: composed and combining forms intentionally hash
    differently, as do newline variants and every one-byte payload change.
    """

    _validate_json(value)
    return json.dumps(
        value,
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def deterministic_marker(kind: PublicationKind, intent_id: str) -> str:
    if not _MARKER_RE.fullmatch(intent_id):
        raise ContractError("intent_id is unsafe for a deterministic marker")
    return f"<!-- hermes-kanban:{kind.value}:{intent_id} -->"


def _github_payload_with_marker(
    kind: PublicationKind,
    payload: Mapping[str, Any],
    marker: str,
) -> dict[str, Any]:
    allowed = {"title", "body"} if kind is PublicationKind.GITHUB_ISSUE_CREATE else {"body"}
    unknown = set(payload) - allowed
    if unknown:
        raise ContractError(f"unsupported GitHub payload fields: {sorted(unknown)}")
    body = payload.get("body")
    if not isinstance(body, str):
        raise ContractError("GitHub body must be a string")
    if marker in body:
        marked_body = body
    else:
        marked_body = f"{body.rstrip()}\n\n{marker}\n"
    result: dict[str, Any] = {"body": marked_body}
    if kind is PublicationKind.GITHUB_ISSUE_CREATE:
        title = payload.get("title")
        if not isinstance(title, str) or not title.strip():
            raise ContractError("GitHub issue title is required")
        result["title"] = title
    return result


def prepare_intent(
    *,
    intent_id: str,
    draft: DraftIntent,
    policy: TrustedIntentPolicy,
) -> PreparedIntent:
    marker = deterministic_marker(draft.kind, intent_id)
    payload: Mapping[str, Any]
    if draft.kind in {
        PublicationKind.GITHUB_ISSUE_CREATE,
        PublicationKind.GITHUB_ISSUE_COMMENT_CREATE,
    }:
        payload = _github_payload_with_marker(draft.kind, draft.payload, marker)
    else:
        payload = dict(draft.payload)
        if "marker" in payload and payload["marker"] != marker:
            raise ContractError("worker supplied a conflicting marker")
        payload = {**payload, "marker": marker}

    request_body = canonical_json_bytes(dict(payload))
    request_body_sha = sha256_hex(request_body)
    if draft.kind in {
        PublicationKind.GITHUB_ISSUE_CREATE,
        PublicationKind.GITHUB_ISSUE_COMMENT_CREATE,
    }:
        application_headers = {
            "Accept": "application/vnd.github+json",
            "Authorization": "Bearer <publisher-principal-credential>",
            "Content-Type": "application/json; charset=utf-8",
            "X-GitHub-Api-Version": "2022-11-28",
        }
    else:
        application_headers = {
            "Content-Type": "application/json; charset=utf-8",
            "X-Hermes-Kanban-Signature": "sha256=<controller-derived-signature>",
            "X-Hermes-Kanban-Wire": "<wire-sha256>",
        }
    manifest = {
        "schema": "hermes.kanban.publication-intent.v1",
        "intent_id": intent_id,
        "kind": draft.kind.value,
        "required": policy.required,
        "publisher_principal": policy.publisher_principal,
        "adapter_version": policy.adapter_version,
        "target": dict(policy.target),
        "payload": dict(payload),
        "marker": marker,
        "request_body_sha256": request_body_sha,
        "request_body_length": len(request_body),
        "application_headers": application_headers,
    }
    prepared = canonical_json_bytes(manifest)
    wire_sha = sha256_hex(WIRE_PREFIX + prepared)
    return PreparedIntent(
        intent_id=intent_id,
        kind=draft.kind,
        required=policy.required,
        publisher_principal=policy.publisher_principal,
        adapter_version=policy.adapter_version,
        target=dict(policy.target),
        payload=dict(payload),
        application_headers=application_headers,
        marker=marker,
        prepared_bytes=prepared,
        request_body_bytes=request_body,
        request_body_sha256=request_body_sha,
        wire_sha256=wire_sha,
    )
