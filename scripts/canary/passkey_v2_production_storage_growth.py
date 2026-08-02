#!/usr/bin/env python3
"""Passkey-v2 authorization for the exact production 50 -> 100 GiB plan."""

from __future__ import annotations

import base64
import copy
import hashlib
import re
import struct
from typing import Any, Mapping, Protocol

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

from scripts.canary import passkey_v2_protocol as protocol
from scripts.canary import production_storage_growth_contract as contract


ACTION_SCHEMA = "muncho-passkey-v2-production-storage-growth-action.v1"
FACTS_SCHEMA = "muncho-passkey-v2-production-storage-growth-facts.v1"
AUTHORIZATION_BUNDLE_SCHEMA = (
    "muncho-passkey-v2-production-storage-growth-authorization.v1"
)
REMOTE_FRAME_SCHEMA = "muncho-passkey-v2-owner-gate-frame.v1"
REMOTE_RESPONSE_SCHEMA = "muncho-passkey-v2-owner-gate-response.v1"
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
EXTERNAL_IAM_RECEIPT_SCHEMA = (
    "muncho-production-storage-growth-external-iam-receipt.v1"
)
EXTERNAL_IAM_SSHSIG_NAMESPACE = (
    "muncho-production-storage-growth-external-iam-v1"
)
EXTERNAL_IAM_PROJECT_NUMBER = "39589465056"
EXTERNAL_IAM_TTL_SECONDS = 300
EXTERNAL_IAM_MINIMUM_REMAINING_SECONDS = 90
EXTERNAL_IAM_PERMISSIONS = (
    "compute.disks.get",
    "compute.disks.resize",
    "compute.instances.get",
    "compute.instances.osAdminLogin",
    "iap.tunnelInstances.accessViaIAP",
)
# The signed receipt proves that these exact capabilities were granted and
# fresh at collection time.  It deliberately does not claim that the account
# has no additional IAM roles; production execution remains closed by the
# exact target, operation, release, runtime, and passkey bindings below.
# This is the already-provisioned and separately pinned owner operations key.
# The private key remains outside Python and outside the release.
EXTERNAL_IAM_OWNER_PUBLIC_KEY_ED25519_HEX = (
    "4f928fd117e2e62f1e52b0095d6ab5524370707f5e9b295efdc62479ce887e26"
)
EXTERNAL_IAM_OWNER_KEY_ID = (
    "d9229ecb5084f4a78c8887b07effc6259355ccceecbe9b4ad55994e070d674c1"
)

_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_SSHSIG_BEGIN = "-----BEGIN SSH SIGNATURE-----"
_SSHSIG_END = "-----END SSH SIGNATURE-----"
_MAX_SSHSIG_BYTES = 4096
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
_EXTERNAL_IAM_FIELDS = frozenset({
    "schema",
    "account",
    "owner_subject_sha256",
    "project",
    "project_number",
    "zone",
    "instance_name",
    "instance_id",
    "disk_name",
    "disk_id",
    "permissions",
    "authorization_snapshot_sha256",
    "instance_evidence_sha256",
    "disk_evidence_sha256",
    "collected_at_unix",
    "expires_at_unix",
    "owner_public_key_id",
    "receipt_sha256",
    "signature_sshsig",
})


class ProductionStoragePasskeyError(RuntimeError):
    """Stable, secret-free owner authorization failure."""


class DedicatedOwnerGateTransport(Protocol):
    """One fixed stdin-only owner-gate exchange."""

    def invoke_owner_gate(self, canonical_frame: bytes) -> bytes: ...


def _is_sha(value: Any) -> bool:
    return isinstance(value, str) and _SHA256.fullmatch(value) is not None


def external_iam_signature_payload(value: Mapping[str, Any]) -> bytes:
    """Return the one canonical SSHSIG message for an exact IAM receipt."""

    if not isinstance(value, Mapping):
        raise ProductionStoragePasskeyError(
            "production_storage_external_iam_invalid"
        )
    signed = {
        name: copy.deepcopy(item)
        for name, item in value.items()
        if name != "signature_sshsig"
    }
    return protocol.canonical_json_bytes(signed)


def _ssh_wire(value: bytes) -> bytes:
    return struct.pack(">I", len(value)) + value


def _read_ssh_wire(value: bytes, offset: int) -> tuple[bytes, int]:
    if offset < 0 or offset + 4 > len(value):
        raise ProductionStoragePasskeyError(
            "production_storage_external_iam_invalid"
        )
    length = struct.unpack(">I", value[offset : offset + 4])[0]
    start = offset + 4
    end = start + length
    if length > _MAX_SSHSIG_BYTES or end > len(value):
        raise ProductionStoragePasskeyError(
            "production_storage_external_iam_invalid"
        )
    return value[start:end], end


def _verify_external_iam_sshsig(
    signature: str,
    *,
    message: bytes,
    public_key_ed25519_hex: str,
) -> None:
    code = "production_storage_external_iam_invalid"
    if (
        not isinstance(signature, str)
        or not isinstance(message, bytes)
        or not message
        or len(message) > 64 * 1024
        or _SHA256.fullmatch(public_key_ed25519_hex or "") is None
    ):
        raise ProductionStoragePasskeyError(code)
    try:
        signature_bytes = signature.encode("ascii", errors="strict")
        namespace_bytes = EXTERNAL_IAM_SSHSIG_NAMESPACE.encode(
            "ascii", errors="strict"
        )
    except UnicodeError:
        raise ProductionStoragePasskeyError(code) from None
    if (
        len(signature_bytes) > _MAX_SSHSIG_BYTES
        or not signature.startswith(_SSHSIG_BEGIN + "\n")
        or not signature.endswith("\n" + _SSHSIG_END + "\n")
    ):
        raise ProductionStoragePasskeyError(code)
    lines = signature.splitlines()
    if (
        len(lines) < 3
        or lines[0] != _SSHSIG_BEGIN
        or lines[-1] != _SSHSIG_END
        or any(
            re.fullmatch(r"[A-Za-z0-9+/=]{1,70}", line) is None
            for line in lines[1:-1]
        )
        or any(len(line) != 70 for line in lines[1:-2])
    ):
        raise ProductionStoragePasskeyError(code)
    encoded = "".join(lines[1:-1])
    try:
        envelope = base64.b64decode(encoded, validate=True)
    except (UnicodeError, ValueError):
        raise ProductionStoragePasskeyError(code) from None
    if (
        base64.b64encode(envelope).decode("ascii") != encoded
        or not envelope.startswith(b"SSHSIG")
    ):
        raise ProductionStoragePasskeyError(code)
    offset = 6
    if (
        offset + 4 > len(envelope)
        or struct.unpack(">I", envelope[offset : offset + 4])[0] != 1
    ):
        raise ProductionStoragePasskeyError(code)
    offset += 4
    public_blob, offset = _read_ssh_wire(envelope, offset)
    observed_namespace, offset = _read_ssh_wire(envelope, offset)
    reserved, offset = _read_ssh_wire(envelope, offset)
    hash_algorithm, offset = _read_ssh_wire(envelope, offset)
    signature_blob, offset = _read_ssh_wire(envelope, offset)
    if offset != len(envelope):
        raise ProductionStoragePasskeyError(code)
    key_type, key_offset = _read_ssh_wire(public_blob, 0)
    public_bytes, key_offset = _read_ssh_wire(public_blob, key_offset)
    algorithm, signature_offset = _read_ssh_wire(signature_blob, 0)
    raw_signature, signature_offset = _read_ssh_wire(
        signature_blob, signature_offset
    )
    try:
        expected_public = bytes.fromhex(public_key_ed25519_hex)
    except ValueError:
        raise ProductionStoragePasskeyError(code) from None
    if (
        key_offset != len(public_blob)
        or signature_offset != len(signature_blob)
        or key_type != b"ssh-ed25519"
        or algorithm != b"ssh-ed25519"
        or public_bytes != expected_public
        or len(public_bytes) != 32
        or len(raw_signature) != 64
        or observed_namespace != namespace_bytes
        or reserved != b""
        or hash_algorithm != b"sha512"
    ):
        raise ProductionStoragePasskeyError(code)
    signed = (
        b"SSHSIG"
        + _ssh_wire(observed_namespace)
        + _ssh_wire(reserved)
        + _ssh_wire(hash_algorithm)
        + _ssh_wire(hashlib.sha512(message).digest())
    )
    try:
        Ed25519PublicKey.from_public_bytes(public_bytes).verify(
            raw_signature,
            signed,
        )
    except (InvalidSignature, ValueError):
        raise ProductionStoragePasskeyError(code) from None


def validate_external_iam_receipt(
    value: Any,
    *,
    now_unix: int,
    minimum_remaining_seconds: int = EXTERNAL_IAM_MINIMUM_REMAINING_SECONDS,
    expected_public_key_ed25519_hex: str = (
        EXTERNAL_IAM_OWNER_PUBLIC_KEY_ED25519_HEX
    ),
    expected_owner_key_id: str = EXTERNAL_IAM_OWNER_KEY_ID,
) -> Mapping[str, Any]:
    """Verify exact production authority, freshness, digest, and owner SSHSIG."""

    if (
        not isinstance(value, Mapping)
        or set(value) != _EXTERNAL_IAM_FIELDS
        or type(now_unix) is not int
        or now_unix <= 0
        or type(minimum_remaining_seconds) is not int
        or not 0 <= minimum_remaining_seconds <= EXTERNAL_IAM_TTL_SECONDS
        or not _is_sha(expected_public_key_ed25519_hex)
        or not _is_sha(expected_owner_key_id)
    ):
        raise ProductionStoragePasskeyError(
            "production_storage_external_iam_invalid"
        )
    receipt = copy.deepcopy(dict(value))
    unsigned = {
        name: item
        for name, item in receipt.items()
        if name not in {"receipt_sha256", "signature_sshsig"}
    }
    permissions = receipt.get("permissions")
    collected = receipt.get("collected_at_unix")
    expires = receipt.get("expires_at_unix")
    if (
        receipt.get("schema") != EXTERNAL_IAM_RECEIPT_SCHEMA
        or receipt.get("account") != contract.AUTHENTICATED_ACCOUNT
        or not _is_sha(receipt.get("owner_subject_sha256"))
        or receipt.get("project") != contract.PROJECT
        or receipt.get("project_number") != EXTERNAL_IAM_PROJECT_NUMBER
        or receipt.get("zone") != contract.ZONE
        or receipt.get("instance_name") != contract.INSTANCE_NAME
        or receipt.get("instance_id") != contract.INSTANCE_ID
        or receipt.get("disk_name") != contract.DISK_NAME
        or receipt.get("disk_id") != contract.DISK_ID
        or not isinstance(permissions, Mapping)
        or set(permissions) != set(EXTERNAL_IAM_PERMISSIONS)
        or any(permissions.get(name) != "GRANTED" for name in EXTERNAL_IAM_PERMISSIONS)
        or any(
            not _is_sha(receipt.get(name))
            for name in (
                "authorization_snapshot_sha256",
                "instance_evidence_sha256",
                "disk_evidence_sha256",
            )
        )
        or type(collected) is not int
        or type(expires) is not int
        or collected <= 0
        or expires != collected + EXTERNAL_IAM_TTL_SECONDS
        or not collected <= now_unix < expires
        or expires - now_unix < minimum_remaining_seconds
        or receipt.get("owner_public_key_id") != expected_owner_key_id
        or receipt.get("receipt_sha256") != protocol.sha256_json(unsigned)
        or not isinstance(receipt.get("signature_sshsig"), str)
    ):
        raise ProductionStoragePasskeyError(
            "production_storage_external_iam_invalid"
        )
    _verify_external_iam_sshsig(
        receipt["signature_sshsig"],
        message=external_iam_signature_payload(receipt),
        public_key_ed25519_hex=expected_public_key_ed25519_hex,
    )
    return receipt


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


class ProductionStoragePasskeyBoundary:
    """Owner-side request/consume route with no mutation callback."""

    def __init__(
        self,
        release_revision: str,
        transport: DedicatedOwnerGateTransport,
    ) -> None:
        if (
            not isinstance(release_revision, str)
            or re.fullmatch(r"[0-9a-f]{40}", release_revision) is None
            or not callable(getattr(transport, "invoke_owner_gate", None))
            or callable(getattr(transport, "run_local_compute_mutation", None))
        ):
            raise ProductionStoragePasskeyError(
                "production_storage_owner_gate_transport_invalid"
            )
        self.release_revision = release_revision
        self._transport = transport

    def _invoke(self, operation: str, document: Mapping[str, Any]) -> Mapping[str, Any]:
        if operation not in {
            "attest_production_storage_authority",
            "request_production_storage_growth",
            "consume_production_storage_growth",
        }:
            raise ProductionStoragePasskeyError(
                "production_storage_owner_gate_operation_invalid"
            )
        unsigned = {
            "schema": REMOTE_FRAME_SCHEMA,
            "operation": operation,
            "release_sha": self.release_revision,
            "document": copy.deepcopy(dict(document)),
        }
        frame = {**unsigned, "frame_sha256": protocol.sha256_json(unsigned)}
        raw = self._transport.invoke_owner_gate(protocol.canonical_json_bytes(frame))
        try:
            response = protocol.decode_canonical_json(raw)
        except protocol.PasskeyV2ProtocolError:
            raise ProductionStoragePasskeyError(
                "production_storage_owner_gate_response_invalid"
            ) from None
        response_unsigned = (
            {name: item for name, item in response.items() if name != "response_sha256"}
            if isinstance(response, Mapping)
            else {}
        )
        if (
            not isinstance(response, Mapping)
            or set(response)
            != {
                "schema",
                "operation",
                "release_sha",
                "ok",
                "document",
                "response_sha256",
            }
            or response.get("schema") != REMOTE_RESPONSE_SCHEMA
            or response.get("operation") != operation
            or response.get("release_sha") != self.release_revision
            or response.get("ok") is not True
            or not isinstance(response.get("document"), Mapping)
            or response.get("response_sha256")
            != protocol.sha256_json(response_unsigned)
        ):
            raise ProductionStoragePasskeyError(
                "production_storage_owner_gate_response_invalid"
            )
        return copy.deepcopy(dict(response["document"]))

    def attest_authority(self) -> Mapping[str, Any]:
        value = self._invoke("attest_production_storage_authority", {})
        unsigned = {
            name: item
            for name, item in value.items()
            if name != "attestation_sha256"
        } if isinstance(value, Mapping) else {}
        if (
            not isinstance(value, Mapping)
            or set(value)
            != {
                "schema", "release_sha", "receipt_public_key_ed25519_hex",
                "receipt_public_key_id", "portable_trust_bundle_sha256",
                "portable_trust_bundle",
                "authority_manifest_sha256", "authority_host_receipt_sha256",
                "root_owned_trust_bundle_validated",
                "rotation_requires_new_release_and_owner_install",
                "attestation_sha256",
            }
            or value.get("schema")
            != "muncho-production-storage-authority-key-attestation.v1"
            or value.get("release_sha") != self.release_revision
            or not _is_sha(value.get("receipt_public_key_ed25519_hex"))
            or not _is_sha(value.get("receipt_public_key_id"))
            or value.get("receipt_public_key_id")
            != hashlib.sha256(bytes.fromhex(
                value["receipt_public_key_ed25519_hex"]
            )).hexdigest()
            or any(
                not _is_sha(value.get(name))
                for name in (
                    "portable_trust_bundle_sha256",
                    "authority_manifest_sha256",
                    "authority_host_receipt_sha256",
                )
            )
            or not isinstance(value.get("portable_trust_bundle"), Mapping)
            or value["portable_trust_bundle"].get("trust_bundle_sha256")
            != value.get("portable_trust_bundle_sha256")
            or value.get("root_owned_trust_bundle_validated") is not True
            or value.get("rotation_requires_new_release_and_owner_install")
            is not True
            or value.get("attestation_sha256")
            != protocol.sha256_json(unsigned)
        ):
            raise ProductionStoragePasskeyError(
                "production_storage_authority_key_attestation_invalid"
            )
        return copy.deepcopy(dict(value))

    def request(
        self,
        *,
        growth_plan: Mapping[str, Any],
        authorization_nonce_sha256: str,
        external_iam_receipt: Mapping[str, Any],
        now_unix: int,
    ) -> Mapping[str, Any]:
        plan = contract.validate_plan(growth_plan)
        if not _is_sha(authorization_nonce_sha256):
            raise ProductionStoragePasskeyError(
                "production_storage_passkey_nonce_invalid"
            )
        iam = validate_external_iam_receipt(
            external_iam_receipt,
            now_unix=now_unix,
        )
        return self._invoke(
            "request_production_storage_growth",
            {
                "growth_plan": plan,
                "authorization_nonce_sha256": authorization_nonce_sha256,
                "external_iam_receipt": iam,
            },
        )

    def consume(
        self,
        *,
        growth_plan: Mapping[str, Any],
        request_id: str,
        consume_attempt_id: str,
        external_iam_receipt: Mapping[str, Any],
        now_unix: int,
    ) -> Mapping[str, Any]:
        plan = contract.validate_plan(growth_plan)
        if not _is_sha(request_id) or not _is_sha(consume_attempt_id):
            raise ProductionStoragePasskeyError(
                "production_storage_consume_attempt_invalid"
            )
        iam = validate_external_iam_receipt(
            external_iam_receipt,
            now_unix=now_unix,
            minimum_remaining_seconds=0,
        )
        result = self._invoke(
            "consume_production_storage_growth",
            {
                "growth_plan": plan,
                "request_id": request_id,
                "consume_attempt_id": consume_attempt_id,
                "external_iam_receipt": iam,
            },
        )
        if not isinstance(result.get("authorization_bundle"), Mapping):
            raise ProductionStoragePasskeyError(
                "production_storage_owner_gate_response_invalid"
            )
        return result


__all__ = [
    "ACTION_CASE_ID",
    "ACTION_SCHEMA",
    "ACTION_SCOPE",
    "ACTION_STAGE",
    "ACTION_TARGET_SYSTEM",
    "AUTHORIZATION_BUNDLE_SCHEMA",
    "EXTERNAL_IAM_MINIMUM_REMAINING_SECONDS",
    "EXTERNAL_IAM_OWNER_KEY_ID",
    "EXTERNAL_IAM_OWNER_PUBLIC_KEY_ED25519_HEX",
    "EXTERNAL_IAM_PERMISSIONS",
    "EXTERNAL_IAM_RECEIPT_SCHEMA",
    "EXTERNAL_IAM_SSHSIG_NAMESPACE",
    "FACTS_SCHEMA",
    "OWNER_DISCORD_USER_ID",
    "ProductionStoragePasskeyBoundary",
    "ProductionStoragePasskeyError",
    "build_action_envelope",
    "build_authorization_bundle",
    "external_iam_signature_payload",
    "mechanical_approval_facts",
    "validate_action_envelope",
    "validate_authorization_bundle",
    "validate_external_iam_receipt",
]
