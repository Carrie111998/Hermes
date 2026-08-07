#!/usr/bin/env python3
"""Exact passkey step-up boundary for trusted-team sensitive reports.

The trusted model chooses the sensitive-report operation and authors the SQL.
This module does not infer sensitivity or route prose.  It binds one signed
Canonical Writer capability to the authenticated Discord user, Canonical case,
database, normalized query hash, purpose hash, and one passkey credential.
"""

from __future__ import annotations

import hashlib
import re
import sqlite3
import threading
from pathlib import Path
from typing import Any, Mapping

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

from scripts.canary import passkey_v2_protocol as protocol


ACTION_SCHEMA = "muncho-sensitive-report-passkey-action.v1"
FACTS_SCHEMA = "muncho-sensitive-report-passkey-mechanical-facts.v1"
OPERATION_ID = "skyvision.db.query_sensitive"
SCOPE = protocol.SENSITIVE_REPORT_SCOPE
STAGE = "report"
DEFAULT_MAX_ROWS = 100
COMMAND_AUTHORIZATION_SCHEMA = "muncho-operational-edge-command-authorization.v1"
_PAYLOAD_FIELDS = frozenset({
    "schema",
    "operation_id",
    "authority_ref",
    "capability_sha256",
    "subject_discord_user_id",
    "scope",
    "case_id",
    "arguments_sha256",
    "idempotency_key",
    "database",
    "normalized_query_sha256",
    "purpose_sha256",
    "max_rows",
    "retrieval_token_sha256",
})
_IDEMPOTENCY = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/-]{0,239}$")
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_DISCORD_ID = re.compile(r"^[1-9][0-9]{16,21}$")
_CASE_ID = re.compile(r"^case:[A-Za-z0-9][A-Za-z0-9._:/-]{0,190}$")
_MAX_ARGUMENT_BYTES = 128 * 1024
_MAX_CAPABILITY_MILLISECONDS = 8 * 60 * 60 * 1000


class SensitiveReportPasskeyError(RuntimeError):
    """One stable, secret-free sensitive-report step-up failure."""


def _fail(code: str) -> None:
    raise SensitiveReportPasskeyError(code)


def _mapping(value: Any, *, code: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        converter = getattr(value, "to_mapping", None)
        value = converter() if callable(converter) else None
    if not isinstance(value, Mapping):
        _fail(code)
    return dict(value)


def _intent(value: Any) -> Mapping[str, Any]:
    item = _mapping(value, code="sensitive_report_intent_invalid")
    if (
        set(item) != {
        "operation_id", "arguments", "arguments_sha256", "idempotency_key"
        }
        or item.get("operation_id") != OPERATION_ID
        or not isinstance(item.get("arguments"), Mapping)
        or len(protocol.canonical_json_bytes(item["arguments"]))
        > _MAX_ARGUMENT_BYTES
        or not isinstance(item.get("arguments_sha256"), str)
        or _SHA256.fullmatch(item["arguments_sha256"]) is None
        or not isinstance(item.get("idempotency_key"), str)
        or _IDEMPOTENCY.fullmatch(item["idempotency_key"]) is None
    ):
        _fail("sensitive_report_intent_invalid")
    if protocol.sha256_json(dict(item["arguments"])) != item.get(
        "arguments_sha256"
    ):
        _fail("sensitive_report_intent_invalid")
    return item


def _capability(value: Any) -> Mapping[str, Any]:
    item = _mapping(value, code="sensitive_report_capability_invalid")
    if (
        set(item) != {
        "schema", "authority_kind", "authority_ref", "operation_id",
        "arguments_sha256", "idempotency_key", "issued_at_unix_ms",
        "expires_at_unix_ms", "subject_discord_user_id", "case_id",
        "operator_tier",
        }
        or item.get("schema") != "muncho-operational-edge-capability.v3"
        or item.get("authority_kind") != "canonical_plan"
        or not isinstance(item.get("authority_ref"), str)
        or not item["authority_ref"]
        or len(item["authority_ref"]) > 240
        or "\x00" in item["authority_ref"]
        or item.get("operation_id") != OPERATION_ID
        or not isinstance(item.get("arguments_sha256"), str)
        or _SHA256.fullmatch(item["arguments_sha256"]) is None
        or not isinstance(item.get("idempotency_key"), str)
        or _IDEMPOTENCY.fullmatch(item["idempotency_key"]) is None
        or type(item.get("issued_at_unix_ms")) is not int
        or type(item.get("expires_at_unix_ms")) is not int
        or item["issued_at_unix_ms"] < 1
        or item["expires_at_unix_ms"] <= item["issued_at_unix_ms"]
        or item["expires_at_unix_ms"] - item["issued_at_unix_ms"]
        > _MAX_CAPABILITY_MILLISECONDS
        or not isinstance(item.get("subject_discord_user_id"), str)
        or _DISCORD_ID.fullmatch(item["subject_discord_user_id"]) is None
        or not isinstance(item.get("case_id"), str)
        or _CASE_ID.fullmatch(item["case_id"]) is None
        or item.get("operator_tier") not in {"standard", "top", "owner"}
    ):
        _fail("sensitive_report_capability_invalid")
    return item


def operational_command_sha256(intent: Any) -> str:
    return protocol.sha256_json({
        "schema": COMMAND_AUTHORIZATION_SCHEMA,
        "intent": _intent(intent),
    })


def normalized_query_sha256(query: Any) -> str:
    if not isinstance(query, str):
        _fail("sensitive_report_query_invalid")
    normalized = query.strip().rstrip(";")
    if not normalized or len(normalized.encode("utf-8")) > 64 * 1024:
        _fail("sensitive_report_query_invalid")
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()


def _payload(
    *,
    capability: Any,
    intent: Any,
    retrieval_token: bytes,
) -> Mapping[str, Any]:
    capability_value = _capability(capability)
    intent_value = _intent(intent)
    if intent_value["operation_id"] != OPERATION_ID:
        _fail("sensitive_report_operation_invalid")
    arguments = dict(intent_value["arguments"])
    if (
        arguments.get("requester_id")
        != capability_value["subject_discord_user_id"]
        or arguments.get("case_id") != capability_value["case_id"]
    ):
        _fail("sensitive_report_identity_binding_invalid")
    database = arguments.get("db")
    purpose = arguments.get("purpose")
    max_rows = arguments.get("max_rows", DEFAULT_MAX_ROWS)
    if (
        database not in {"skyvisio_fp", "skyvisio_laravel", "skyvisio_wp64"}
        or not isinstance(purpose, str)
        or not purpose.strip()
        or purpose != purpose.strip()
        or len(purpose) > 2000
        or type(max_rows) is not int
        or not 1 <= max_rows <= 500
        or not isinstance(retrieval_token, bytes)
        or len(retrieval_token) != 32
    ):
        _fail("sensitive_report_arguments_invalid")
    return {
        "schema": ACTION_SCHEMA,
        "operation_id": OPERATION_ID,
        "authority_ref": capability_value["authority_ref"],
        "capability_sha256": protocol.sha256_json(capability_value),
        "subject_discord_user_id": capability_value["subject_discord_user_id"],
        "scope": SCOPE,
        "case_id": capability_value["case_id"],
        "arguments_sha256": intent_value["arguments_sha256"],
        "idempotency_key": intent_value["idempotency_key"],
        "database": database,
        "normalized_query_sha256": normalized_query_sha256(
            arguments.get("query")
        ),
        "purpose_sha256": hashlib.sha256(purpose.encode("utf-8")).hexdigest(),
        "max_rows": max_rows,
        "retrieval_token_sha256": hashlib.sha256(retrieval_token).hexdigest(),
    }


def build_action_envelope(
    *,
    capability: Any,
    intent: Any,
    retrieval_token: bytes,
    request_id: str,
    executor_release_sha: str,
    authority_release_sha: str,
    authority_manifest_sha256: str,
    authority_host_receipt_sha256: str,
    source_preflight_sha256: str,
    live_projection_sha256: str,
    prior_authoritative_receipt_sha256: str,
    prior_event_head_sha256: str,
    issued_at_unix: int,
    approval_ttl_seconds: int = 300,
) -> Mapping[str, Any]:
    capability_value = _capability(capability)
    intent_value = _intent(intent)
    if (
        capability_value["operation_id"] != intent_value["operation_id"]
        or capability_value["arguments_sha256"]
        != intent_value["arguments_sha256"]
        or capability_value["idempotency_key"]
        != intent_value["idempotency_key"]
        or not capability_value["issued_at_unix_ms"]
        <= issued_at_unix * 1000
        < capability_value["expires_at_unix_ms"]
    ):
        _fail("sensitive_report_capability_intent_mismatch")
    payload = _payload(
        capability=capability_value,
        intent=intent_value,
        retrieval_token=retrieval_token,
    )
    transaction_id = protocol.sha256_json({
        "schema": ACTION_SCHEMA,
        "operation_id": intent_value["operation_id"],
        "arguments_sha256": intent_value["arguments_sha256"],
        "idempotency_key": intent_value["idempotency_key"],
        "subject_discord_user_id": capability_value["subject_discord_user_id"],
        "case_id": capability_value["case_id"],
    })
    envelope = protocol.build_action_envelope(
        request_id=request_id,
        requester_discord_user_id=capability_value["subject_discord_user_id"],
        required_approver_discord_user_id=capability_value["subject_discord_user_id"],
        scope=SCOPE,
        case_id=capability_value["case_id"],
        target_system=f"skyvision-db:{payload['database']}",
        action_summary="Authorize this exact bounded sensitive report query.",
        risk="The report may contain personal or commercially sensitive data.",
        rollback="No database mutation is permitted; deny or expire before disclosure.",
        action_payload=payload,
        executor_release_sha=executor_release_sha,
        executor_plan_sha256=operational_command_sha256(intent_value),
        transaction_id=transaction_id,
        stage=STAGE,
        webauthn_rp_id=protocol.PRODUCTION_RP_ID,
        webauthn_origin=protocol.PRODUCTION_ORIGIN,
        authority_release_sha=authority_release_sha,
        authority_manifest_sha256=authority_manifest_sha256,
        authority_host_receipt_sha256=authority_host_receipt_sha256,
        source_preflight_sha256=source_preflight_sha256,
        live_projection_sha256=live_projection_sha256,
        external_iam_receipt_sha256=payload["capability_sha256"],
        prior_authoritative_receipt_sha256=prior_authoritative_receipt_sha256,
        prior_event_head_sha256=prior_event_head_sha256,
        issued_at_unix=issued_at_unix,
        approval_ttl_seconds=approval_ttl_seconds,
    )
    return validate_action_envelope(envelope)


def validate_action_envelope(value: Any) -> Mapping[str, Any]:
    try:
        action = protocol.validate_action_envelope(value)
    except protocol.PasskeyV2ProtocolError as exc:
        raise SensitiveReportPasskeyError(
            "sensitive_report_action_invalid"
        ) from exc
    payload = action.get("action_payload")
    if (
        action.get("scope") != SCOPE
        or action.get("stage") != STAGE
        or action.get("requester_discord_user_id")
        != action.get("required_approver_discord_user_id")
        or not isinstance(payload, Mapping)
        or set(payload) != _PAYLOAD_FIELDS
        or payload.get("schema") != ACTION_SCHEMA
        or payload.get("operation_id") != OPERATION_ID
        or payload.get("scope") != SCOPE
        or payload.get("subject_discord_user_id")
        != action.get("requester_discord_user_id")
        or payload.get("case_id") != action.get("case_id")
        or payload.get("capability_sha256")
        != action.get("external_iam_receipt_sha256")
        or action.get("target_system")
        != f"skyvision-db:{payload.get('database')}"
    ):
        _fail("sensitive_report_action_binding_invalid")
    for name in (
        "capability_sha256",
        "arguments_sha256",
        "normalized_query_sha256",
        "purpose_sha256",
        "retrieval_token_sha256",
    ):
        item = payload.get(name)
        if (
            not isinstance(item, str)
            or len(item) != 64
            or any(character not in "0123456789abcdef" for character in item)
        ):
            _fail("sensitive_report_action_digest_invalid")
    if (
        payload.get("database")
        not in {"skyvisio_fp", "skyvisio_laravel", "skyvisio_wp64"}
        or type(payload.get("max_rows")) is not int
        or not 1 <= payload["max_rows"] <= 500
    ):
        _fail("sensitive_report_action_arguments_invalid")
    return action


def mechanical_approval_facts(value: Any) -> Mapping[str, Any]:
    action = validate_action_envelope(value)
    payload = action["action_payload"]
    return {
        "schema": FACTS_SCHEMA,
        "authenticated_discord_user_id": payload["subject_discord_user_id"],
        "case_id": payload["case_id"],
        "database": payload["database"],
        "query_sha256": payload["normalized_query_sha256"],
        "purpose_sha256": payload["purpose_sha256"],
        "max_rows": payload["max_rows"],
        "expires_at_unix": action["expires_at_unix"],
    }


def require_retrieval_token(value: Any, token: bytes) -> Mapping[str, Any]:
    action = validate_action_envelope(value)
    if (
        not isinstance(token, bytes)
        or len(token) != 32
        or hashlib.sha256(token).hexdigest()
        != action["action_payload"]["retrieval_token_sha256"]
    ):
        _fail("sensitive_report_retrieval_token_invalid")
    return action


def validate_authorization_receipt(
    *,
    receipt: Any,
    envelope: Any,
    grant: Any,
    challenge: Any,
    receipt_public_key: Ed25519PublicKey,
    intent: Any,
    capability: Any,
    now_unix: int,
) -> Mapping[str, Any]:
    action = validate_action_envelope(envelope)
    expected_payload = action["action_payload"]
    intent_value = _intent(intent)
    capability_value = _capability(capability)
    if (
        expected_payload["arguments_sha256"] != intent_value["arguments_sha256"]
        or expected_payload["idempotency_key"] != intent_value["idempotency_key"]
        or expected_payload["capability_sha256"]
        != protocol.sha256_json(capability_value)
        or expected_payload["subject_discord_user_id"]
        != capability_value["subject_discord_user_id"]
        or expected_payload["case_id"] != capability_value["case_id"]
    ):
        _fail("sensitive_report_authorization_intent_mismatch")
    try:
        checked = protocol.validate_authorization_receipt(
            receipt,
            envelope=action,
            grant=grant,
            challenge=challenge,
            receipt_public_key=receipt_public_key,
        )
    except protocol.PasskeyV2ProtocolError as exc:
        raise SensitiveReportPasskeyError(
            "sensitive_report_authorization_invalid"
        ) from exc
    if (
        checked.get("scope") != SCOPE
        or checked.get("case_id") != capability_value["case_id"]
        or checked.get("approver_discord_user_id")
        != capability_value["subject_discord_user_id"]
        or checked.get("approval_method") != "passkey"
        or checked.get("mutation_authorized") is not True
        or checked.get("mutation_executed") is not False
        or not checked["consumed_at_unix"] <= now_unix
        < checked["execution_window_expires_at_unix"]
    ):
        _fail("sensitive_report_authorization_binding_invalid")
    return checked


class SensitiveReportAuthorizationJournal:
    """Append-only, replay-safe consumption of passkey authorization receipts."""

    def __init__(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        self._connection = sqlite3.connect(
            path, check_same_thread=False, timeout=5
        )
        self._connection.execute("PRAGMA journal_mode=WAL")
        self._connection.execute("PRAGMA synchronous=FULL")
        self._connection.execute(
            "CREATE TABLE IF NOT EXISTS sensitive_report_authorizations ("
            "receipt_sha256 TEXT PRIMARY KEY, intent_sha256 TEXT NOT NULL, "
            "idempotency_key TEXT NOT NULL UNIQUE, consumed_at_unix INTEGER NOT NULL)"
        )
        self._connection.execute(
            "CREATE TRIGGER IF NOT EXISTS sensitive_report_no_update "
            "BEFORE UPDATE ON sensitive_report_authorizations BEGIN "
            "SELECT RAISE(ABORT,'append_only'); END"
        )
        self._connection.execute(
            "CREATE TRIGGER IF NOT EXISTS sensitive_report_no_delete "
            "BEFORE DELETE ON sensitive_report_authorizations BEGIN "
            "SELECT RAISE(ABORT,'append_only'); END"
        )
        self._connection.commit()
        self._lock = threading.Lock()

    def consume_once(
        self,
        *,
        receipt_sha256: str,
        intent: Any,
        now_unix: int,
    ) -> str:
        intent_value = _intent(intent)
        intent_sha256 = hashlib.sha256(
            protocol.canonical_json_bytes(intent_value)
        ).hexdigest()
        with self._lock:
            try:
                self._connection.execute("BEGIN IMMEDIATE")
                existing = self._connection.execute(
                    "SELECT intent_sha256,idempotency_key FROM "
                    "sensitive_report_authorizations WHERE receipt_sha256=?",
                    (receipt_sha256,),
                ).fetchone()
                if existing is None:
                    conflict = self._connection.execute(
                        "SELECT receipt_sha256,intent_sha256 FROM "
                        "sensitive_report_authorizations WHERE idempotency_key=?",
                        (intent_value["idempotency_key"],),
                    ).fetchone()
                    if conflict is not None:
                        _fail("sensitive_report_idempotency_conflict")
                    self._connection.execute(
                        "INSERT INTO sensitive_report_authorizations VALUES (?,?,?,?)",
                        (
                            receipt_sha256,
                            intent_sha256,
                            intent_value["idempotency_key"],
                            now_unix,
                        ),
                    )
                    disposition = "consumed"
                elif existing != (
                    intent_sha256, intent_value["idempotency_key"]
                ):
                    _fail("sensitive_report_authorization_replay_forbidden")
                else:
                    disposition = "replayed_same_intent"
                self._connection.commit()
            except BaseException:
                self._connection.rollback()
                raise
        return disposition

    def close(self) -> None:
        self._connection.close()


__all__ = [
    "ACTION_SCHEMA",
    "FACTS_SCHEMA",
    "OPERATION_ID",
    "SCOPE",
    "SensitiveReportAuthorizationJournal",
    "SensitiveReportPasskeyError",
    "build_action_envelope",
    "mechanical_approval_facts",
    "normalized_query_sha256",
    "require_retrieval_token",
    "validate_action_envelope",
    "validate_authorization_receipt",
]
