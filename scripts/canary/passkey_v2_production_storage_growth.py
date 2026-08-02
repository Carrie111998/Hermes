#!/usr/bin/env python3
"""Passkey-v2 authorization for the exact production 50 -> 100 GiB plan."""

from __future__ import annotations

import copy
import re
from typing import Any, Mapping

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

from scripts.canary import passkey_v2_protocol as protocol
from scripts.canary import production_storage_growth_contract as contract


ACTION_SCHEMA = "muncho-passkey-v2-production-storage-growth-action.v1"
FACTS_SCHEMA = "muncho-passkey-v2-production-storage-growth-facts.v1"
AUTHORIZATION_BUNDLE_SCHEMA = (
    "muncho-passkey-v2-production-storage-growth-authorization.v1"
)
OWNER_DISCORD_USER_ID = "1279454038731264061"
ACTION_SCOPE = "production_write"
ACTION_STAGE = "grow"
ACTION_CASE_ID = "case:muncho-production-boot-storage-growth-50-to-100"
ACTION_TARGET_SYSTEM = (
    "gce:adventico-ai-platform/europe-west3-a/"
    "ai-platform-runtime-01/disk/ai-platform-runtime-01"
)
ACTION_SUMMARY = (
    "Grow the exact production boot disk ai-platform-runtime-01 from "
    "50 GiB to 100 GiB and expand its ext4 root filesystem online."
)
ACTION_RISK = (
    "The provider disk resize is irreversible; a wrong target or identity "
    "could affect production availability or storage cost."
)
ACTION_ROLLBACK = (
    "Disk shrink is forbidden and is not a rollback. After provider resize, "
    "recover forward under this same immutable plan by completing the exact "
    "online partition and ext4 filesystem growth."
)

_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_ACTION_FIELDS = frozenset({
    "schema",
    "operation",
    "growth_plan",
    "growth_plan_sha256",
    "authorization_nonce_sha256",
    "allowed_operations",
    "one_shot",
    "one_irreversible_provider_resize",
    "online_forward_recovery_only",
    "shrink_rollback_available",
    "caller_selected_commands_allowed",
    "caller_selected_paths_allowed",
    "caller_selected_targets_allowed",
    "generic_shell_fallback_allowed",
})
_BUNDLE_FIELDS = frozenset({
    "schema",
    "action_envelope",
    "challenge_record",
    "grant_record",
    "authorization_receipt",
    "bundle_sha256",
})


class ProductionStoragePasskeyError(RuntimeError):
    """Stable, secret-free owner authorization failure."""


def _is_sha(value: Any) -> bool:
    return isinstance(value, str) and _SHA256.fullmatch(value) is not None


def build_action_envelope(
    *,
    growth_plan: Mapping[str, Any],
    authorization_nonce_sha256: str,
    authority_manifest_sha256: str,
    authority_host_receipt_sha256: str,
    external_iam_receipt_sha256: str,
    prior_authoritative_receipt_sha256: str,
    prior_event_head_sha256: str,
    issued_at_unix: int,
) -> Mapping[str, Any]:
    plan = contract.validate_plan(growth_plan)
    if not _is_sha(authorization_nonce_sha256):
        raise ProductionStoragePasskeyError("production_storage_passkey_nonce_invalid")
    payload = {
        "schema": ACTION_SCHEMA,
        "operation": contract.OPERATION,
        "growth_plan": plan,
        "growth_plan_sha256": plan["plan_sha256"],
        "authorization_nonce_sha256": authorization_nonce_sha256,
        "allowed_operations": [contract.OPERATION],
        "one_shot": True,
        "one_irreversible_provider_resize": True,
        "online_forward_recovery_only": True,
        "shrink_rollback_available": False,
        "caller_selected_commands_allowed": False,
        "caller_selected_paths_allowed": False,
        "caller_selected_targets_allowed": False,
        "generic_shell_fallback_allowed": False,
    }
    transaction_id = protocol.sha256_json({
        "schema": "muncho-production-storage-growth-transaction.v1",
        "growth_plan_sha256": plan["plan_sha256"],
        "authorization_nonce_sha256": authorization_nonce_sha256,
    })
    request_id = protocol.sha256_json({
        "schema": "muncho-production-storage-growth-request.v1",
        "transaction_id": transaction_id,
        "issued_at_unix": issued_at_unix,
    })
    try:
        return protocol.build_action_envelope(
            request_id=request_id,
            requester_discord_user_id=OWNER_DISCORD_USER_ID,
            required_approver_discord_user_id=OWNER_DISCORD_USER_ID,
            scope=ACTION_SCOPE,
            case_id=ACTION_CASE_ID,
            target_system=ACTION_TARGET_SYSTEM,
            action_summary=ACTION_SUMMARY,
            risk=ACTION_RISK,
            rollback=ACTION_ROLLBACK,
            action_payload=payload,
            executor_release_sha=plan["release_revision"],
            executor_plan_sha256=plan["plan_sha256"],
            transaction_id=transaction_id,
            stage=ACTION_STAGE,
            webauthn_rp_id=protocol.PRODUCTION_RP_ID,
            webauthn_origin=protocol.PRODUCTION_ORIGIN,
            authority_release_sha=plan["release_revision"],
            authority_manifest_sha256=authority_manifest_sha256,
            authority_host_receipt_sha256=authority_host_receipt_sha256,
            source_preflight_sha256=plan["source_preflight_sha256"],
            live_projection_sha256=plan["plan_sha256"],
            external_iam_receipt_sha256=external_iam_receipt_sha256,
            prior_authoritative_receipt_sha256=prior_authoritative_receipt_sha256,
            prior_event_head_sha256=prior_event_head_sha256,
            issued_at_unix=issued_at_unix,
            approval_ttl_seconds=protocol.MAXIMUM_TTL_SECONDS,
        )
    except protocol.PasskeyV2ProtocolError:
        raise ProductionStoragePasskeyError(
            "production_storage_passkey_action_invalid"
        ) from None


def validate_action_envelope(
    value: Any,
    *,
    growth_plan: Mapping[str, Any] | None = None,
) -> Mapping[str, Any]:
    try:
        action = protocol.validate_action_envelope(value)
        protocol.require_production_webauthn_identity(action)
    except protocol.PasskeyV2ProtocolError:
        raise ProductionStoragePasskeyError(
            "production_storage_passkey_action_invalid"
        ) from None
    payload = action.get("action_payload")
    if not isinstance(payload, Mapping) or set(payload) != _ACTION_FIELDS:
        raise ProductionStoragePasskeyError("production_storage_passkey_action_invalid")
    try:
        plan = contract.validate_plan(payload["growth_plan"])
    except (KeyError, contract.ProductionStorageGrowthError):
        raise ProductionStoragePasskeyError(
            "production_storage_passkey_action_invalid"
        ) from None
    nonce = payload.get("authorization_nonce_sha256")
    transaction_id = protocol.sha256_json({
        "schema": "muncho-production-storage-growth-transaction.v1",
        "growth_plan_sha256": plan["plan_sha256"],
        "authorization_nonce_sha256": nonce,
    })
    request_id = protocol.sha256_json({
        "schema": "muncho-production-storage-growth-request.v1",
        "transaction_id": transaction_id,
        "issued_at_unix": action["issued_at_unix"],
    })
    if (
        payload.get("schema") != ACTION_SCHEMA
        or payload.get("operation") != contract.OPERATION
        or payload.get("growth_plan_sha256") != plan["plan_sha256"]
        or not _is_sha(nonce)
        or payload.get("allowed_operations") != [contract.OPERATION]
        or payload.get("one_shot") is not True
        or payload.get("one_irreversible_provider_resize") is not True
        or payload.get("online_forward_recovery_only") is not True
        or payload.get("shrink_rollback_available") is not False
        or any(
            payload.get(name) is not False
            for name in (
                "caller_selected_commands_allowed",
                "caller_selected_paths_allowed",
                "caller_selected_targets_allowed",
                "generic_shell_fallback_allowed",
            )
        )
        or action["scope"] != ACTION_SCOPE
        or action["case_id"] != ACTION_CASE_ID
        or action["target_system"] != ACTION_TARGET_SYSTEM
        or action["action_summary"] != ACTION_SUMMARY
        or action["risk"] != ACTION_RISK
        or action["rollback"] != ACTION_ROLLBACK
        or action["stage"] != ACTION_STAGE
        or action["requester_discord_user_id"] != OWNER_DISCORD_USER_ID
        or action["required_approver_discord_user_id"] != OWNER_DISCORD_USER_ID
        or action["executor_release_sha"] != plan["release_revision"]
        or action["authority_release_sha"] != plan["release_revision"]
        or action["executor_plan_sha256"] != plan["plan_sha256"]
        or action["source_preflight_sha256"] != plan["source_preflight_sha256"]
        or action["live_projection_sha256"] != plan["plan_sha256"]
        or action["transaction_id"] != transaction_id
        or action["request_id"] != request_id
    ):
        raise ProductionStoragePasskeyError("production_storage_passkey_action_invalid")
    if growth_plan is not None:
        expected = contract.validate_plan(growth_plan)
        if protocol.canonical_json_bytes(expected) != protocol.canonical_json_bytes(
            plan
        ):
            raise ProductionStoragePasskeyError(
                "production_storage_passkey_plan_binding_invalid"
            )
    return action


def mechanical_approval_facts(value: Any) -> Mapping[str, Any]:
    action = validate_action_envelope(value)
    plan = action["action_payload"]["growth_plan"]
    return {
        "schema": FACTS_SCHEMA,
        "project": contract.PROJECT,
        "zone": contract.ZONE,
        "instance_name": contract.INSTANCE_NAME,
        "instance_id": contract.INSTANCE_ID,
        "disk_name": contract.DISK_NAME,
        "disk_id": contract.DISK_ID,
        "disk_type": contract.DISK_TYPE,
        "authenticated_account": contract.AUTHENTICATED_ACCOUNT,
        "source_size_gb": contract.SOURCE_SIZE_GB,
        "target_size_gb": contract.TARGET_SIZE_GB,
        "growth_plan_sha256": plan["plan_sha256"],
        "source_preflight_sha256": plan["source_preflight_sha256"],
        "minimum_postflight_available_bytes": (
            contract.MINIMUM_POSTFLIGHT_AVAILABLE_BYTES
        ),
        "provider_request_id": plan["provider_request_id"],
        "single_use": True,
        "user_verification_required": True,
        "totp_available": False,
        "disk_shrink_available": False,
        "forward_recovery_required": True,
        "caller_selected_commands_allowed": False,
        "caller_selected_paths_allowed": False,
        "caller_selected_targets_allowed": False,
    }


def build_authorization_bundle(
    *,
    growth_plan: Mapping[str, Any],
    action_envelope: Mapping[str, Any],
    challenge_record: Mapping[str, Any],
    grant_record: Mapping[str, Any],
    authorization_receipt: Mapping[str, Any],
    receipt_public_key: Ed25519PublicKey,
) -> Mapping[str, Any]:
    plan = contract.validate_plan(growth_plan)
    unsigned = {
        "schema": AUTHORIZATION_BUNDLE_SCHEMA,
        "action_envelope": copy.deepcopy(dict(action_envelope)),
        "challenge_record": copy.deepcopy(dict(challenge_record)),
        "grant_record": copy.deepcopy(dict(grant_record)),
        "authorization_receipt": copy.deepcopy(dict(authorization_receipt)),
    }
    return validate_authorization_bundle(
        {**unsigned, "bundle_sha256": protocol.sha256_json(unsigned)},
        growth_plan=plan,
        receipt_public_key=receipt_public_key,
        now_unix=authorization_receipt.get("consumed_at_unix"),
        require_current=True,
    )


def validate_authorization_bundle(
    value: Any,
    *,
    growth_plan: Mapping[str, Any],
    receipt_public_key: Ed25519PublicKey,
    now_unix: int,
    require_current: bool,
) -> Mapping[str, Any]:
    if (
        not isinstance(value, Mapping)
        or set(value) != _BUNDLE_FIELDS
        or not isinstance(receipt_public_key, Ed25519PublicKey)
        or type(now_unix) is not int
        or now_unix <= 0
        or type(require_current) is not bool
    ):
        raise ProductionStoragePasskeyError(
            "production_storage_passkey_authorization_invalid"
        )
    bundle = copy.deepcopy(dict(value))
    unsigned = {name: item for name, item in bundle.items() if name != "bundle_sha256"}
    if bundle.get("schema") != AUTHORIZATION_BUNDLE_SCHEMA or bundle.get(
        "bundle_sha256"
    ) != protocol.sha256_json(unsigned):
        raise ProductionStoragePasskeyError(
            "production_storage_passkey_authorization_invalid"
        )
    try:
        action = validate_action_envelope(
            bundle["action_envelope"], growth_plan=growth_plan
        )
        challenge = protocol.validate_challenge_record(
            bundle["challenge_record"], envelope=action
        )
        grant = protocol.validate_passkey_grant(
            bundle["grant_record"], envelope=action, challenge=challenge
        )
        receipt = protocol.validate_authorization_receipt(
            bundle["authorization_receipt"],
            envelope=action,
            grant=grant,
            challenge=challenge,
            receipt_public_key=receipt_public_key,
        )
    except (
        KeyError,
        TypeError,
        protocol.PasskeyV2ProtocolError,
        ProductionStoragePasskeyError,
    ):
        raise ProductionStoragePasskeyError(
            "production_storage_passkey_authorization_invalid"
        ) from None
    plan = contract.validate_plan(growth_plan)
    runtime = receipt["runtime_binding"]
    if (
        grant["method"] != "passkey"
        or grant["single_use"] is not True
        or grant["user_verified"] is not True
        or receipt["outcome"] != "ALLOW"
        or receipt["mutation_authorized"] is not True
        or receipt["mutation_executed"] is not False
        or receipt["authorization_disposition"] != "authorized_once"
        or receipt["approval_method"] != "passkey"
        or receipt["approver_discord_user_id"] != OWNER_DISCORD_USER_ID
        or runtime["executor_release_sha"] != plan["release_revision"]
        or runtime["executor_plan_sha256"] != plan["plan_sha256"]
        or runtime["executor_binary_sha256"] != plan["executor_binary_sha256"]
        or runtime["mutation_wrapper_sha256"] != plan["mutation_wrapper_sha256"]
        or runtime["remote_transport_sha256"] != plan["remote_transport_sha256"]
        or receipt["prior_journal_head_sha256"] != protocol.GENESIS_JOURNAL_HEAD_SHA256
        or receipt["consumed_at_unix"] > now_unix
        or require_current
        and not receipt["consumed_at_unix"]
        <= now_unix
        < receipt["execution_window_expires_at_unix"]
    ):
        raise ProductionStoragePasskeyError(
            "production_storage_passkey_authorization_invalid"
        )
    return bundle


__all__ = [
    "ACTION_CASE_ID",
    "ACTION_SCHEMA",
    "ACTION_SCOPE",
    "ACTION_STAGE",
    "ACTION_TARGET_SYSTEM",
    "AUTHORIZATION_BUNDLE_SCHEMA",
    "FACTS_SCHEMA",
    "OWNER_DISCORD_USER_ID",
    "ProductionStoragePasskeyError",
    "build_action_envelope",
    "build_authorization_bundle",
    "mechanical_approval_facts",
    "validate_action_envelope",
    "validate_authorization_bundle",
]
