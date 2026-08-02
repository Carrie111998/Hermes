#!/usr/bin/python3
"""Root-owned state machine for the one production storage-growth transaction.

This program is deliberately standalone and Python 3.9 compatible.  The
installer copies this exact file to one root-owned executable.  It accepts no
arguments, paths, commands, cloud credentials, network endpoints, or mutation
requests.  Its only authority is to attest and advance the fixed local
hash-chained execution journal while holding the fixed execution lock.
"""

from __future__ import annotations

import base64
try:
    import fcntl
except ImportError:  # pragma: no cover - helper is POSIX-only
    fcntl = None
import hashlib
import json
import os
import stat
import sys
import tempfile
import time


STATE_ROOT = "/var/lib/muncho-production-storage-growth"
INSTALLATION_RECEIPT = STATE_ROOT + "/.installation.json"
LOCK_PATH = STATE_ROOT + "/.execution.lock"
INSTALLED_HELPER = "/usr/local/lib/muncho/production-storage-growth-state-helper"
FRAME_SCHEMA = "muncho-production-storage-growth-state-helper-frame.v1"
RESPONSE_SCHEMA = "muncho-production-storage-growth-state-helper-response.v1"
JOURNAL_SCHEMA = "muncho-production-storage-growth-journal.v1"
EVENT_SCHEMA = "muncho-production-storage-growth-event.v1"
MAXIMUM_FRAME_BYTES = 2 * 1024 * 1024
GENESIS_HEAD = hashlib.sha256(
    b"muncho-passkey-v2-authoritative-journal-genesis-v1"
).hexdigest()

JOURNAL_FIELDS = frozenset({
    "schema", "state", "plan_sha256", "authorization_bundle_sha256",
    "authorization_receipt_sha256", "provider_request_id",
    "idempotency_key_sha256", "started_at_unix", "completed_at_unix",
    "final_observation", "prior_journal_head_sha256", "transition_event",
    "journal_sha256",
})
INSTALLATION_FIELDS = frozenset({
    "schema", "release_sha", "state_root", "installer_sha256",
    "sealed_artifact_binding", "sealed_artifact_binding_sha256",
    "installed_at_unix", "state_root_device", "state_root_inode",
    "state_helper_path", "state_helper_sha256",
    "state_helper_sudoers_path", "state_helper_sudoers_sha256",
    "authorized_client_uid", "authorized_client_gid",
    "authority_key_attestation", "authority_key_attestation_sha256",
    "installation_receipt_sha256",
})

PLAN_FIELDS = frozenset({
    "schema", "operation", "release_revision", "source_preflight",
    "source_preflight_sha256", "preflight_max_age_seconds", "project", "zone",
    "instance_name", "instance_id", "instance_self_link", "disk_name", "disk_id",
    "disk_self_link", "disk_type", "boot_device_name", "boot_id",
    "authenticated_account", "source_size_gb", "target_size_gb",
    "minimum_postflight_filesystem_bytes", "minimum_postflight_available_bytes",
    "provider_request_id", "idempotency_key_sha256", "executor_binary_sha256",
    "mutation_wrapper_sha256", "read_only_collector_sha256", "remote_transport_sha256",
    "owner_cli_sha256", "owner_route_sha256", "production_cutover_transport_sha256",
    "installer_sha256", "state_helper_sha256", "runtime_artifact_attestation",
    "runtime_artifact_attestation_sha256", "maximum_provider_resize_operations",
    "online_partition_filesystem_growth_only", "stop_allowed", "start_allowed",
    "reboot_allowed", "snapshot_allowed", "delete_allowed", "replacement_allowed",
    "shrink_allowed", "rollback_by_shrink_allowed", "forward_recovery_required",
    "caller_selected_commands_allowed", "caller_selected_paths_allowed",
    "caller_selected_targets_allowed", "generic_shell_fallback_allowed", "plan_sha256",
})
STATIC_PLAN = {
    "schema": "muncho-production-storage-growth-plan.v1",
    "operation": "grow_exact_production_boot_disk_50_to_100.v1",
    "preflight_max_age_seconds": 300,
    "project": "adventico-ai-platform",
    "zone": "europe-west3-a",
    "instance_name": "ai-platform-runtime-01",
    "instance_id": "1094477181810932795",
    "instance_self_link": "https://www.googleapis.com/compute/v1/projects/adventico-ai-platform/zones/europe-west3-a/instances/ai-platform-runtime-01",
    "disk_name": "ai-platform-runtime-01",
    "disk_id": "8330339521755118650",
    "disk_self_link": "https://www.googleapis.com/compute/v1/projects/adventico-ai-platform/zones/europe-west3-a/disks/ai-platform-runtime-01",
    "disk_type": "pd-balanced",
    "authenticated_account": "lomliev@adventico.com",
    "source_size_gb": 50,
    "target_size_gb": 100,
    "minimum_postflight_filesystem_bytes": 104000000000,
    "minimum_postflight_available_bytes": 5368709120,
    "maximum_provider_resize_operations": 1,
    "online_partition_filesystem_growth_only": True,
    "stop_allowed": False,
    "start_allowed": False,
    "reboot_allowed": False,
    "snapshot_allowed": False,
    "delete_allowed": False,
    "replacement_allowed": False,
    "shrink_allowed": False,
    "rollback_by_shrink_allowed": False,
    "forward_recovery_required": True,
    "caller_selected_commands_allowed": False,
    "caller_selected_paths_allowed": False,
    "caller_selected_targets_allowed": False,
    "generic_shell_fallback_allowed": False,
}
ARTIFACT_RELATIVES = {
    "plan_builder": "scripts/canary/production_storage_growth_owner_cli.py",
    "owner_cli": "scripts/canary/production_storage_growth_owner_cli.py",
    "owner_route": "scripts/canary/full_canary_owner_launcher.py",
    "executor": "scripts/canary/production_storage_growth_executor.py",
    "adapter": "scripts/canary/production_storage_growth_adapter.py",
    "production_cutover": "scripts/canary/production_cutover_owner_launcher.py",
    "guest": "scripts/canary/production_storage_growth_guest.py",
    "installer": "scripts/canary/production_storage_growth_installer.py",
    "state_helper": "scripts/canary/production_storage_growth_state_helper.py",
}
ACTION_ENVELOPE_FIELDS = frozenset({
    "schema", "canonicalization", "request_id",
    "requester_discord_user_id", "required_approver_discord_user_id",
    "scope", "case_id", "target_system", "action_summary", "risk",
    "rollback", "action_payload", "action_payload_sha256",
    "executor_release_sha", "executor_plan_sha256", "transaction_id",
    "stage", "webauthn_rp_id", "webauthn_origin",
    "authority_release_sha", "authority_manifest_sha256",
    "authority_host_receipt_sha256", "source_preflight_sha256",
    "live_projection_sha256", "external_iam_receipt_sha256",
    "prior_authoritative_receipt_sha256", "prior_event_head_sha256",
    "execution_window_seconds", "issued_at_unix", "expires_at_unix",
    "approval_ttl_seconds", "envelope_sha256",
})
ACTION_PAYLOAD_FIELDS = frozenset({
    "schema", "operation", "growth_plan", "growth_plan_sha256",
    "authorization_nonce_sha256", "allowed_operations", "one_shot",
    "one_irreversible_provider_resize", "online_forward_recovery_only",
    "shrink_rollback_available", "caller_selected_commands_allowed",
    "caller_selected_paths_allowed", "caller_selected_targets_allowed",
    "generic_shell_fallback_allowed",
})
CHALLENGE_FIELDS = frozenset({
    "schema", "canonicalization", "state", "request_id",
    "action_envelope_sha256", "challenge_id", "challenge_b64url",
    "challenge_sha256", "rp_id", "origin", "created_at_unix",
    "expires_at_unix", "challenge_record_sha256",
})
GRANT_FIELDS = frozenset({
    "schema", "canonicalization", "state", "method", "single_use",
    "request_id", "action_envelope_sha256", "challenge_id",
    "challenge_record_sha256", "grant_id", "approver_discord_user_id",
    "credential_id_sha256", "credential_record_sha256",
    "credential_migration_receipt_sha256",
    "assertion_verification_sha256", "credential_sign_count",
    "credential_backed_up", "user_verified", "rp_id", "origin",
    "granted_at_unix", "expires_at_unix", "grant_sha256",
})
RUNTIME_FIELDS = frozenset({
    "executor_release_sha", "executor_plan_sha256",
    "executor_binary_sha256", "mutation_wrapper_sha256",
    "remote_transport_sha256", "runtime_binding_sha256",
})
RECEIPT_FIELDS = frozenset({
    "schema", "canonicalization", "outcome", "mutation_authorized",
    "mutation_executed", "authorization_disposition",
    "consume_attempt_id", "request_id", "action_envelope_sha256",
    "action_payload_sha256", "scope", "case_id", "target_system",
    "transaction_id", "stage", "grant_id", "grant_sha256",
    "approver_discord_user_id", "credential_id_sha256",
    "credential_record_sha256", "credential_migration_receipt_sha256",
    "assertion_verification_sha256", "credential_sign_count",
    "credential_backed_up", "approval_method", "challenge_id",
    "challenge_record_sha256", "granted_at_unix",
    "grant_expires_at_unix", "consumed_at_unix",
    "execution_window_seconds", "execution_window_expires_at_unix",
    "authority_release_sha", "authority_manifest_sha256",
    "authority_host_receipt_sha256", "source_preflight_sha256",
    "live_projection_sha256", "external_iam_receipt_sha256",
    "prior_authoritative_receipt_sha256", "prior_event_head_sha256",
    "runtime_binding", "prior_journal_head_sha256",
    "receipt_public_key_id", "signature_ed25519_b64url",
    "receipt_sha256",
})


class StateHelperError(RuntimeError):
    pass


def canonical_bytes(value):
    try:
        return json.dumps(
            value, ensure_ascii=False, allow_nan=False, sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8", errors="strict")
    except (TypeError, ValueError, UnicodeError):
        raise StateHelperError("production_storage_state_helper_frame_invalid")


def sha256_json(value):
    return hashlib.sha256(canonical_bytes(value)).hexdigest()


def _pairs(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise StateHelperError("production_storage_state_helper_frame_invalid")
        result[key] = value
    return result


def decode(raw):
    if not isinstance(raw, bytes) or not raw or len(raw) > MAXIMUM_FRAME_BYTES:
        raise StateHelperError("production_storage_state_helper_frame_invalid")
    try:
        value = json.loads(
            raw.decode("utf-8", errors="strict"), object_pairs_hook=_pairs,
            parse_float=lambda _value: (_ for _ in ()).throw(ValueError()),
            parse_constant=lambda _value: (_ for _ in ()).throw(ValueError()),
        )
    except (ValueError, UnicodeError):
        raise StateHelperError("production_storage_state_helper_frame_invalid")
    if canonical_bytes(value) != raw:
        raise StateHelperError("production_storage_state_helper_frame_invalid")
    return value


def _sha(value):
    return (
        isinstance(value, str) and len(value) == 64
        and all(character in "0123456789abcdef" for character in value)
    )


def _sha40(value):
    return (
        isinstance(value, str) and len(value) == 40
        and all(character in "0123456789abcdef" for character in value)
    )


def _node(path, directory, mode, uid=0, gid=0):
    try:
        info = os.lstat(path)
    except OSError:
        raise StateHelperError("production_storage_state_storage_invalid")
    kind = stat.S_ISDIR if directory else stat.S_ISREG
    if (
        not kind(info.st_mode) or stat.S_IMODE(info.st_mode) != mode
        or info.st_uid != uid or info.st_gid != gid
        or (not directory and info.st_nlink != 1)
    ):
        raise StateHelperError("production_storage_state_storage_invalid")
    return info


def _read_json(path, maximum=MAXIMUM_FRAME_BYTES):
    try:
        raw = open(path, "rb").read(maximum + 1)
    except OSError:
        raise StateHelperError("production_storage_state_storage_invalid")
    if not raw.endswith(b"\n") or raw.endswith(b"\n\n") or len(raw) > maximum:
        raise StateHelperError("production_storage_state_storage_invalid")
    return decode(raw[:-1])


def _write_atomic(path, value, state_root=STATE_ROOT, uid=0, gid=0):
    payload = canonical_bytes(value) + b"\n"
    descriptor = None
    temporary = None
    try:
        descriptor, temporary = tempfile.mkstemp(prefix=".state.", dir=state_root)
        os.fchmod(descriptor, 0o600)
        os.fchown(descriptor, uid, gid)
        offset = 0
        while offset < len(payload):
            count = os.write(descriptor, payload[offset:])
            if count <= 0:
                raise OSError("short write")
            offset += count
        os.fsync(descriptor)
        os.close(descriptor)
        descriptor = None
        os.replace(temporary, path)
        temporary = None
        parent = os.open(state_root, os.O_RDONLY)
        try:
            os.fsync(parent)
        finally:
            os.close(parent)
    except OSError:
        if descriptor is not None:
            try:
                os.close(descriptor)
            except OSError:
                pass
        if temporary is not None:
            try:
                os.unlink(temporary)
            except OSError:
                pass
        raise StateHelperError("production_storage_state_storage_invalid")
    _node(path, False, 0o600, uid, gid)


def _append_events(path, events, state_root=STATE_ROOT, uid=0, gid=0):
    payload = b"".join(canonical_bytes(item) + b"\n" for item in events)
    descriptor = None
    temporary = None
    try:
        descriptor, temporary = tempfile.mkstemp(prefix=".events.", dir=state_root)
        os.fchmod(descriptor, 0o600)
        os.fchown(descriptor, uid, gid)
        offset = 0
        while offset < len(payload):
            count = os.write(descriptor, payload[offset:])
            if count <= 0:
                raise OSError("short write")
            offset += count
        os.fsync(descriptor)
        os.close(descriptor)
        descriptor = None
        os.replace(temporary, path)
        temporary = None
        parent = os.open(state_root, os.O_RDONLY)
        try:
            os.fsync(parent)
        finally:
            os.close(parent)
    except OSError:
        if descriptor is not None:
            try:
                os.close(descriptor)
            except OSError:
                pass
        if temporary is not None:
            try:
                os.unlink(temporary)
            except OSError:
                pass
        raise StateHelperError("production_storage_event_log_write_failed")
    _node(path, False, 0o600, uid, gid)


# Small, direct RFC 8032 verifier.  It exists here so the root helper can run
# under Apple's root-owned /usr/bin/python3 without importing user-controlled
# site-packages.
_Q = 2**255 - 19
_L = 2**252 + 27742317777372353535851937790883648493
_D = (-121665 * pow(121666, _Q - 2, _Q)) % _Q
_I = pow(2, (_Q - 1) // 4, _Q)


def _xrecover(y):
    xx = (y * y - 1) * pow(_D * y * y + 1, _Q - 2, _Q) % _Q
    x = pow(xx, (_Q + 3) // 8, _Q)
    if (x * x - xx) % _Q:
        x = x * _I % _Q
    if (x * x - xx) % _Q:
        raise ValueError("invalid point")
    return x


def _decode_point(raw):
    if not isinstance(raw, bytes) or len(raw) != 32:
        raise ValueError("invalid point")
    value = int.from_bytes(raw, "little")
    sign = value >> 255
    y = value & ((1 << 255) - 1)
    if y >= _Q:
        raise ValueError("invalid point")
    x = _xrecover(y)
    if (x & 1) != sign:
        x = _Q - x
    if (-x * x + y * y - 1 - _D * x * x * y * y) % _Q:
        raise ValueError("invalid point")
    return (x, y, 1, x * y % _Q)


def _add(p, q):
    a = (p[1] - p[0]) * (q[1] - q[0]) % _Q
    b = (p[1] + p[0]) * (q[1] + q[0]) % _Q
    c = 2 * p[3] * q[3] * _D % _Q
    d = 2 * p[2] * q[2] % _Q
    e, f, g, h = b - a, d - c, d + c, b + a
    return (e * f % _Q, g * h % _Q, f * g % _Q, e * h % _Q)


def _scalar(point, scalar):
    result = (0, 1, 1, 0)
    current = point
    while scalar:
        if scalar & 1:
            result = _add(result, current)
        current = _add(current, current)
        scalar >>= 1
    return result


def _equal(p, q):
    return (
        (p[0] * q[2] - q[0] * p[2]) % _Q == 0
        and (p[1] * q[2] - q[1] * p[2]) % _Q == 0
    )


_BY = 4 * pow(5, _Q - 2, _Q) % _Q
_BX = _xrecover(_BY)
if _BX & 1:
    _BX = _Q - _BX
_B = (_BX, _BY, 1, 0)
_B = (_B[0], _B[1], 1, _B[0] * _B[1] % _Q)


def verify_ed25519(public_key, signature, message):
    try:
        if len(public_key) != 32 or len(signature) != 64:
            return False
        rpoint = _decode_point(signature[:32])
        apoint = _decode_point(public_key)
        scalar = int.from_bytes(signature[32:], "little")
        if scalar >= _L:
            return False
        challenge = int.from_bytes(
            hashlib.sha512(signature[:32] + public_key + message).digest(), "little"
        ) % _L
        return _equal(_scalar(_B, scalar), _add(rpoint, _scalar(apoint, challenge)))
    except (ValueError, TypeError):
        return False


def validate_plan(plan, release_sha, helper_sha):
    if not isinstance(plan, dict) or set(plan) != PLAN_FIELDS:
        raise StateHelperError("production_storage_plan_invalid")
    if any(plan.get(name) != value for name, value in STATIC_PLAN.items()):
        raise StateHelperError("production_storage_plan_invalid")
    if plan.get("release_revision") != release_sha or not _sha40(release_sha):
        raise StateHelperError("production_storage_plan_invalid")
    unsigned = dict(plan)
    observed_sha = unsigned.pop("plan_sha256", None)
    if observed_sha != sha256_json(unsigned):
        raise StateHelperError("production_storage_plan_invalid")
    source = plan.get("source_preflight")
    if not isinstance(source, dict):
        raise StateHelperError("production_storage_plan_invalid")
    source_unsigned = dict(source)
    source_sha = source_unsigned.pop("observation_sha256", None)
    if source_sha != sha256_json(source_unsigned) or plan.get("source_preflight_sha256") != source_sha:
        raise StateHelperError("production_storage_plan_invalid")
    artifacts = plan.get("runtime_artifact_attestation")
    if not isinstance(artifacts, dict):
        raise StateHelperError("production_storage_plan_invalid")
    artifact_unsigned = dict(artifacts)
    artifact_sha = artifact_unsigned.pop("attestation_sha256", None)
    entries = artifacts.get("artifacts")
    if (
        artifact_sha != sha256_json(artifact_unsigned)
        or plan.get("runtime_artifact_attestation_sha256") != artifact_sha
        or artifacts.get("release_revision") != release_sha
        or not isinstance(entries, dict) or set(entries) != set(ARTIFACT_RELATIVES)
    ):
        raise StateHelperError("production_storage_plan_invalid")
    for name, relative in ARTIFACT_RELATIVES.items():
        entry = entries.get(name)
        if (
            not isinstance(entry, dict)
            or set(entry) != {"release_relative", "sha256", "size"}
            or entry.get("release_relative") != relative
            or not _sha(entry.get("sha256"))
            or type(entry.get("size")) is not int or not 0 < entry["size"] <= 8 * 1024 * 1024
        ):
            raise StateHelperError("production_storage_plan_invalid")
    bindings = {
        "executor_binary_sha256": "executor",
        "mutation_wrapper_sha256": "guest",
        "read_only_collector_sha256": "guest",
        "remote_transport_sha256": "adapter",
        "owner_cli_sha256": "owner_cli",
        "owner_route_sha256": "owner_route",
        "production_cutover_transport_sha256": "production_cutover",
        "installer_sha256": "installer",
        "state_helper_sha256": "state_helper",
    }
    if any(plan.get(field) != entries[name]["sha256"] for field, name in bindings.items()):
        raise StateHelperError("production_storage_plan_invalid")
    if plan.get("state_helper_sha256") != helper_sha:
        raise StateHelperError("production_storage_plan_invalid")
    return dict(plan)


def _b64url(value):
    if (
        not isinstance(value, str) or "=" in value
        or not value
        or any(
            character not in (
                "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz"
                "0123456789-_"
            )
            for character in value
        )
    ):
        raise StateHelperError("production_storage_authorization_invalid")
    try:
        decoded = base64.urlsafe_b64decode(
            value + "=" * ((4 - len(value) % 4) % 4)
        )
    except (ValueError, TypeError):
        raise StateHelperError("production_storage_authorization_invalid")
    if base64.urlsafe_b64encode(decoded).rstrip(b"=").decode("ascii") != value:
        raise StateHelperError("production_storage_authorization_invalid")
    return decoded


def validate_authorization(
    bundle, plan, public_key_hex, now_unix, require_current=True
):
    if (
        not isinstance(bundle, dict)
        or set(bundle) != {"schema", "action_envelope", "challenge_record", "grant_record", "authorization_receipt", "bundle_sha256"}
        or bundle.get("schema") != "muncho-passkey-v2-production-storage-growth-authorization.v1"
    ):
        raise StateHelperError("production_storage_authorization_invalid")
    bundle_unsigned = dict(bundle)
    if bundle_unsigned.pop("bundle_sha256", None) != sha256_json(bundle_unsigned):
        raise StateHelperError("production_storage_authorization_invalid")
    action = bundle.get("action_envelope")
    challenge = bundle.get("challenge_record")
    grant = bundle.get("grant_record")
    receipt = bundle.get("authorization_receipt")
    if (
        not all(
            isinstance(value, dict)
            for value in (action, challenge, grant, receipt)
        )
        or set(action) != ACTION_ENVELOPE_FIELDS
        or set(challenge) != CHALLENGE_FIELDS
        or set(grant) != GRANT_FIELDS
        or set(receipt) != RECEIPT_FIELDS
        or action.get("schema") != "muncho-dangerous-action-envelope.v2"
        or challenge.get("schema")
        != "muncho-dangerous-action-passkey-challenge.v2"
        or grant.get("schema")
        != "muncho-dangerous-action-passkey-grant.v2"
        or receipt.get("schema")
        != "muncho-dangerous-action-passkey-authorization-receipt.v2"
    ):
        raise StateHelperError("production_storage_authorization_invalid")
    action_unsigned = dict(action)
    action_sha = action_unsigned.pop("envelope_sha256", None)
    payload = action.get("action_payload")
    if (
        action_sha != sha256_json(action_unsigned)
        or not isinstance(payload, dict)
        or set(payload) != ACTION_PAYLOAD_FIELDS
        or payload.get("schema")
        != "muncho-passkey-v2-production-storage-growth-action.v1"
    ):
        raise StateHelperError("production_storage_authorization_invalid")
    payload_unsigned = dict(payload)
    if (
        payload.get("growth_plan") != plan
        or payload.get("operation") != STATIC_PLAN["operation"]
        or payload.get("growth_plan_sha256") != plan["plan_sha256"]
        or payload.get("allowed_operations") != [STATIC_PLAN["operation"]]
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
        or action.get("executor_plan_sha256") != plan["plan_sha256"]
    ):
        raise StateHelperError("production_storage_authorization_invalid")
    if action.get("action_payload_sha256") != sha256_json(payload_unsigned):
        raise StateHelperError("production_storage_authorization_invalid")
    if (
        action.get("executor_release_sha") != plan["release_revision"]
        or action.get("authority_release_sha") != plan["release_revision"]
        or action.get("scope") != "production_write"
        or action.get("case_id")
        != "case:muncho-production-boot-storage-growth-50-to-100"
        or action.get("stage") != "grow"
        or action.get("target_system") != "gce:adventico-ai-platform/europe-west3-a/ai-platform-runtime-01/disk/ai-platform-runtime-01"
        or action.get("requester_discord_user_id") != "1279454038731264061"
        or action.get("required_approver_discord_user_id") != "1279454038731264061"
        or action.get("webauthn_rp_id") != "lomliev.com"
        or action.get("webauthn_origin") != "https://auth.lomliev.com"
    ):
        raise StateHelperError("production_storage_authorization_invalid")
    grant_unsigned = dict(grant)
    grant_sha = grant_unsigned.pop("grant_sha256", None)
    challenge_unsigned = dict(challenge)
    challenge_sha = challenge_unsigned.pop("challenge_record_sha256", None)
    if (
        grant_sha != sha256_json(grant_unsigned)
        or challenge_sha != sha256_json(challenge_unsigned)
        or any(
            item.get("canonicalization") != "muncho-json-utf8-v1"
            for item in (action, challenge, grant, receipt)
        )
        or challenge.get("rp_id") != "lomliev.com"
        or challenge.get("origin") != "https://auth.lomliev.com"
        or grant.get("rp_id") != "lomliev.com"
        or grant.get("origin") != "https://auth.lomliev.com"
        or grant.get("method") != "passkey"
        or grant.get("single_use") is not True
        or grant.get("user_verified") is not True
        or grant.get("approver_discord_user_id") != "1279454038731264061"
        or grant.get("challenge_record_sha256") != challenge_sha
        or challenge.get("action_envelope_sha256") != action_sha
    ):
        raise StateHelperError("production_storage_authorization_invalid")
    receipt_signed = dict(receipt)
    receipt_sha = receipt_signed.pop("receipt_sha256", None)
    signature_text = receipt_signed.pop("signature_ed25519_b64url", None)
    try:
        public_key = bytes.fromhex(public_key_hex)
    except (ValueError, TypeError):
        raise StateHelperError("production_storage_authorization_invalid")
    signature = _b64url(signature_text)
    runtime = receipt_signed.get("runtime_binding")
    if (
        len(public_key) != 32 or len(signature) != 64
        or receipt.get("receipt_public_key_id") != hashlib.sha256(public_key).hexdigest()
        or not verify_ed25519(public_key, signature, canonical_bytes(receipt_signed))
        or receipt_sha != sha256_json({**receipt_signed, "signature_ed25519_b64url": signature_text})
        or not isinstance(runtime, dict)
        or set(runtime) != RUNTIME_FIELDS
        or runtime.get("runtime_binding_sha256")
        != sha256_json({
            key: value for key, value in runtime.items()
            if key != "runtime_binding_sha256"
        })
        or runtime.get("executor_release_sha") != plan["release_revision"]
        or runtime.get("executor_plan_sha256") != plan["plan_sha256"]
        or runtime.get("executor_binary_sha256") != plan["executor_binary_sha256"]
        or runtime.get("mutation_wrapper_sha256") != plan["mutation_wrapper_sha256"]
        or runtime.get("remote_transport_sha256") != plan["remote_transport_sha256"]
        or receipt.get("outcome") != "ALLOW"
        or receipt.get("mutation_authorized") is not True
        or receipt.get("mutation_executed") is not False
        or receipt.get("authorization_disposition") != "authorized_once"
        or receipt.get("approval_method") != "passkey"
        or receipt.get("approver_discord_user_id") != "1279454038731264061"
        or receipt.get("action_envelope_sha256") != action_sha
        or receipt.get("action_payload_sha256") != action["action_payload_sha256"]
        or receipt.get("grant_sha256") != grant_sha
        or receipt.get("challenge_record_sha256") != challenge_sha
        or type(receipt.get("consumed_at_unix")) is not int
        or type(receipt.get("execution_window_expires_at_unix")) is not int
        or receipt["consumed_at_unix"] > now_unix
        or receipt["execution_window_expires_at_unix"]
        <= receipt["consumed_at_unix"]
        or require_current
        and not receipt["consumed_at_unix"]
        <= now_unix < receipt["execution_window_expires_at_unix"]
        or not _sha(receipt.get("prior_journal_head_sha256"))
    ):
        raise StateHelperError("production_storage_authorization_invalid")
    return dict(bundle)


def validate_observation_for_plan(observation, plan):
    if not isinstance(observation, dict):
        raise StateHelperError("production_storage_observation_invalid")
    unsigned = dict(observation)
    observation_sha = unsigned.pop("observation_sha256", None)
    guest = observation.get("guest")
    disk = observation.get("disk")
    instance = observation.get("instance")
    if (
        observation_sha != sha256_json(unsigned)
        or not isinstance(guest, dict) or not isinstance(disk, dict)
        or not isinstance(instance, dict)
        or instance.get("id") != plan["instance_id"]
        or instance.get("name") != plan["instance_name"]
        or disk.get("id") != plan["disk_id"]
        or disk.get("name") != plan["disk_name"]
        or guest.get("boot_id") != plan["boot_id"]
    ):
        raise StateHelperError("production_storage_observation_invalid")
    if disk.get("size_gb") == plan["source_size_gb"]:
        source_facts = dict(observation)
        expected_facts = dict(plan["source_preflight"])
        for facts in (source_facts, expected_facts):
            facts.pop("collected_at_unix", None)
            facts.pop("observation_sha256", None)
            facts["guest"] = dict(facts["guest"])
            facts["guest"].pop("available_bytes", None)
        if source_facts != expected_facts:
            raise StateHelperError("production_storage_observation_invalid")
        state = "source"
    elif (
        disk.get("size_gb") == plan["target_size_gb"]
        and guest.get("filesystem_size_bytes", 0)
        >= plan["minimum_postflight_filesystem_bytes"]
        and guest.get("available_bytes", 0)
        >= plan["minimum_postflight_available_bytes"]
    ):
        state = "target"
    elif disk.get("size_gb") == plan["target_size_gb"]:
        state = "partial"
    else:
        raise StateHelperError("production_storage_observation_invalid")
    return state, observation_sha


def _event(events, plan, receipt, kind, state, observation_sha, failure_code, now_unix):
    prior = events[-1]["event_head_sha256"] if events else receipt["prior_journal_head_sha256"]
    unsigned = {
        "schema": EVENT_SCHEMA,
        "sequence": len(events) + 1,
        "event_kind": kind,
        "plan_sha256": plan["plan_sha256"],
        "authorization_receipt_sha256": receipt["receipt_sha256"],
        "journal_state": state,
        "observation_sha256": observation_sha,
        "failure_code": failure_code,
        "occurred_at_unix": now_unix,
        "prior_event_head_sha256": prior,
    }
    return {**unsigned, "event_head_sha256": sha256_json(unsigned)}


def validate_recovery_events(
    events, journal, plan, receipt, allow_missing_completion=False
):
    fields = {
        "schema", "sequence", "event_kind", "plan_sha256",
        "authorization_receipt_sha256", "journal_state",
        "observation_sha256", "failure_code", "occurred_at_unix",
        "prior_event_head_sha256", "event_head_sha256",
    }
    if not isinstance(events, list) or not events:
        raise StateHelperError("production_storage_event_log_invalid")
    prior = receipt["prior_journal_head_sha256"]
    prior_time = 0
    completed_count = 0
    for index, event in enumerate(events, start=1):
        unsigned = dict(event) if isinstance(event, dict) else {}
        head = unsigned.pop("event_head_sha256", None)
        kind = event.get("event_kind") if isinstance(event, dict) else None
        if (
            not isinstance(event, dict) or set(event) != fields
            or event.get("schema") != EVENT_SCHEMA
            or event.get("sequence") != index
            or event.get("plan_sha256") != plan["plan_sha256"]
            or event.get("authorization_receipt_sha256")
            != receipt["receipt_sha256"]
            or event.get("prior_event_head_sha256") != prior
            or head != sha256_json(unsigned)
            or type(event.get("occurred_at_unix")) is not int
            or event["occurred_at_unix"] < prior_time
        ):
            raise StateHelperError("production_storage_event_log_invalid")
        if index == 1:
            legal = (
                kind == "execution_started"
                and event.get("journal_state") == "started"
                and event.get("failure_code") is None
                and _sha(event.get("observation_sha256"))
            )
        elif kind == "execution_failed":
            legal = (
                completed_count == 0
                and event.get("journal_state") == "started"
                and isinstance(event.get("failure_code"), str)
                and event["failure_code"].startswith("production_storage_")
                and (
                    event.get("observation_sha256") is None
                    or _sha(event.get("observation_sha256"))
                )
            )
        elif kind == "execution_completed":
            completed_count += 1
            legal = (
                completed_count == 1
                and index == len(events)
                and event.get("journal_state") == "completed"
                and event.get("failure_code") is None
                and journal.get("final_observation", {}).get(
                    "observation_sha256"
                ) == event.get("observation_sha256")
            )
        else:
            legal = False
        if not legal:
            raise StateHelperError("production_storage_event_log_invalid")
        prior, prior_time = head, event["occurred_at_unix"]
    completed_invalid = (
        journal.get("state") == "completed"
        and completed_count != 1
        and not (allow_missing_completion and completed_count == 0)
    )
    started_invalid = (
        journal.get("state") == "started" and completed_count != 0
    )
    if completed_invalid or started_invalid:
        raise StateHelperError("production_storage_event_log_invalid")
    return events


def validate_journal(journal, plan, bundle):
    receipt = bundle["authorization_receipt"]
    unsigned = (
        {key: value for key, value in journal.items() if key != "journal_sha256"}
        if isinstance(journal, dict) else {}
    )
    transition = journal.get("transition_event") if isinstance(journal, dict) else None
    state = journal.get("state") if isinstance(journal, dict) else None
    if (
        not isinstance(journal, dict)
        or set(journal) != JOURNAL_FIELDS
        or journal.get("schema") != JOURNAL_SCHEMA
        or state not in {"started", "completed"}
        or journal.get("plan_sha256") != plan["plan_sha256"]
        or journal.get("authorization_receipt_sha256") != receipt["receipt_sha256"]
        or journal.get("authorization_bundle_sha256") != bundle["bundle_sha256"]
        or journal.get("provider_request_id") != plan["provider_request_id"]
        or journal.get("idempotency_key_sha256") != plan["idempotency_key_sha256"]
        or journal.get("prior_journal_head_sha256")
        != receipt["prior_journal_head_sha256"]
        or type(journal.get("started_at_unix")) is not int
        or journal["started_at_unix"] <= 0
        or journal.get("journal_sha256") != sha256_json(unsigned)
        or not isinstance(transition, dict)
    ):
        raise StateHelperError("production_storage_journal_invalid")
    if state == "started":
        if (
            journal.get("completed_at_unix") is not None
            or journal.get("final_observation") is not None
            or transition.get("event_kind") != "execution_started"
            or transition.get("sequence") != 1
            or transition.get("journal_state") != "started"
            or transition.get("occurred_at_unix") != journal["started_at_unix"]
        ):
            raise StateHelperError("production_storage_journal_invalid")
    else:
        final = journal.get("final_observation")
        completed = journal.get("completed_at_unix")
        if (
            type(completed) is not int
            or completed < journal["started_at_unix"]
            or not isinstance(final, dict)
            or transition.get("event_kind") != "execution_completed"
            or transition.get("journal_state") != "completed"
            or transition.get("occurred_at_unix") != completed
            or transition.get("observation_sha256")
            != final.get("observation_sha256")
        ):
            raise StateHelperError("production_storage_journal_invalid")
    return journal


class RootStateMachine:
    def __init__(
        self, state_root=STATE_ROOT, helper_path=INSTALLED_HELPER,
        now=lambda: int(time.time()), expected_uid=0, expected_gid=0,
    ):
        self.state_root = state_root
        self.helper_path = helper_path
        self.now = now
        self.expected_uid = expected_uid
        self.expected_gid = expected_gid
        self.release_sha = None
        self.helper_sha = None
        self.receipt_public_key_hex = None
        self.plan = None
        self.bundle = None
        self.journal = None
        self.events = []
        self.lock_fd = None

    def open(self):
        if (
            fcntl is None or not hasattr(os, "geteuid")
            or os.geteuid() != self.expected_uid
        ):
            raise StateHelperError("production_storage_state_helper_privilege_invalid")
        lock_path = (
            LOCK_PATH if self.state_root == STATE_ROOT
            else self.state_root + "/.execution.lock"
        )
        flags = os.O_RDWR
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        descriptor = None
        try:
            descriptor = os.open(lock_path, flags)
            lock_info = os.fstat(descriptor)
            lock_path_info = os.lstat(lock_path)
            if (
                not stat.S_ISREG(lock_info.st_mode)
                or lock_info.st_uid != self.expected_uid
                or lock_info.st_gid != self.expected_gid
                or stat.S_IMODE(lock_info.st_mode) != 0o600
                or lock_info.st_nlink != 1
                or lock_path_info.st_dev != lock_info.st_dev
                or lock_path_info.st_ino != lock_info.st_ino
            ):
                raise OSError("invalid execution lock")
            fcntl.flock(descriptor, fcntl.LOCK_EX)
            lock_path_info = os.lstat(lock_path)
            if (
                lock_path_info.st_dev != lock_info.st_dev
                or lock_path_info.st_ino != lock_info.st_ino
            ):
                raise OSError("execution lock identity changed")
        except OSError:
            if descriptor is not None:
                try:
                    os.close(descriptor)
                except OSError:
                    pass
            raise StateHelperError(
                "production_storage_state_storage_invalid"
            ) from None
        self.lock_fd = descriptor
        try:
            root_info = _node(
                self.state_root, True, 0o700,
                self.expected_uid, self.expected_gid,
            )
            receipt_path = (
                INSTALLATION_RECEIPT if self.state_root == STATE_ROOT
                else self.state_root + "/.installation.json"
            )
            _node(
                receipt_path, False, 0o600,
                self.expected_uid, self.expected_gid,
            )
            _node(
                self.helper_path, False, 0o555,
                self.expected_uid, self.expected_gid,
            )
            receipt = _read_json(receipt_path)
            try:
                with open(self.helper_path, "rb") as handle:
                    helper_raw = handle.read(MAXIMUM_FRAME_BYTES + 1)
            except OSError:
                raise StateHelperError(
                    "production_storage_state_helper_installation_invalid"
                )
            if len(helper_raw) > MAXIMUM_FRAME_BYTES:
                raise StateHelperError(
                    "production_storage_state_helper_installation_invalid"
                )
            self.helper_sha = hashlib.sha256(helper_raw).hexdigest()
            receipt_unsigned = {
                key: value for key, value in receipt.items()
                if key != "installation_receipt_sha256"
            } if isinstance(receipt, dict) else {}
            if (
                not isinstance(receipt, dict)
                or set(receipt) != INSTALLATION_FIELDS
                or receipt.get("schema") != "muncho-production-storage-growth-owner-installation.v2"
                or receipt.get("state_helper_sha256") != self.helper_sha
                or receipt.get("state_helper_path") != INSTALLED_HELPER
                or not _sha40(receipt.get("release_sha"))
                or receipt.get("state_root") != STATE_ROOT
                or receipt.get("state_root_device") != root_info.st_dev
                or receipt.get("state_root_inode") != root_info.st_ino
                or receipt.get("installation_receipt_sha256")
                != sha256_json(receipt_unsigned)
            ):
                raise StateHelperError("production_storage_state_helper_installation_invalid")
            self.release_sha = receipt["release_sha"]
            try:
                sudo_uid = int(os.environ["SUDO_UID"])
                sudo_gid = int(os.environ["SUDO_GID"])
            except (KeyError, ValueError, TypeError):
                raise StateHelperError(
                    "production_storage_state_helper_invoker_invalid"
                )
            if (
                sudo_uid != receipt.get("authorized_client_uid")
                or sudo_gid != receipt.get("authorized_client_gid")
            ):
                raise StateHelperError(
                    "production_storage_state_helper_invoker_invalid"
                )
            authority = receipt.get("authority_key_attestation")
            try:
                authority_key_hex = authority.get(
                    "receipt_public_key_ed25519_hex", ""
                ) if isinstance(authority, dict) else ""
                authority_key = bytes.fromhex(authority_key_hex)
            except (TypeError, ValueError):
                raise StateHelperError(
                    "production_storage_state_helper_installation_invalid"
                )
            authority_unsigned = (
                {
                    key: value for key, value in authority.items()
                    if key != "attestation_sha256"
                }
                if isinstance(authority, dict) else {}
            )
            if (
                not isinstance(authority, dict)
                or authority.get("release_sha") != self.release_sha
                or authority.get("attestation_sha256")
                != receipt.get("authority_key_attestation_sha256")
                or authority.get("attestation_sha256")
                != sha256_json(authority_unsigned)
                or len(authority_key) != 32
                or authority.get("receipt_public_key_id")
                != hashlib.sha256(authority_key).hexdigest()
                or authority.get("root_owned_trust_bundle_validated") is not True
                or authority.get("rotation_requires_new_release_and_owner_install")
                is not True
            ):
                raise StateHelperError(
                    "production_storage_state_helper_installation_invalid"
                )
            self.receipt_public_key_hex = authority_key_hex
            return {
                "release_sha": self.release_sha,
                "state_helper_sha256": self.helper_sha,
                "installation_receipt_sha256": receipt[
                    "installation_receipt_sha256"
                ],
                "lock_acquired": True,
            }
        except StateHelperError:
            self.close()
            raise
        except (OSError, ValueError, TypeError):
            self.close()
            raise StateHelperError(
                "production_storage_state_helper_installation_invalid"
            ) from None

    def close(self):
        if self.lock_fd is not None:
            try:
                try:
                    fcntl.flock(self.lock_fd, fcntl.LOCK_UN)
                except OSError:
                    pass
            finally:
                try:
                    os.close(self.lock_fd)
                except OSError:
                    pass
                self.lock_fd = None

    def attest(self, document):
        if set(document) != {"release_sha", "growth_plan"} or document.get("release_sha") != self.release_sha:
            raise StateHelperError("production_storage_state_helper_frame_invalid")
        self.plan = validate_plan(document["growth_plan"], self.release_sha, self.helper_sha)
        return {"release_sha": self.release_sha, "plan_sha256": self.plan["plan_sha256"], "lock_acquired": True}

    def _paths(self):
        if self.plan is None:
            raise StateHelperError("production_storage_state_helper_sequence_invalid")
        base = self.state_root + "/" + self.plan["plan_sha256"]
        return base + ".json", base + ".events.jsonl"

    def _read_persisted_events(self, event_path):
        _node(
            event_path, False, 0o600,
            self.expected_uid, self.expected_gid,
        )
        try:
            with open(event_path, "rb") as handle:
                raw = handle.read(MAXIMUM_FRAME_BYTES + 1)
        except OSError:
            raise StateHelperError(
                "production_storage_event_log_invalid"
            ) from None
        if (
            not raw.endswith(b"\n") or raw.endswith(b"\n\n")
            or len(raw) > MAXIMUM_FRAME_BYTES
        ):
            raise StateHelperError("production_storage_event_log_invalid")
        return [decode(line) for line in raw[:-1].split(b"\n")]

    def begin(self, document):
        if set(document) != {"authorization_bundle", "initial_observation"}:
            raise StateHelperError("production_storage_state_helper_frame_invalid")
        if self.plan is None:
            raise StateHelperError("production_storage_state_helper_sequence_invalid")
        observed_state, observation_sha = validate_observation_for_plan(
            document["initial_observation"], self.plan
        )
        journal_path, event_path = self._paths()
        recovery = os.path.exists(journal_path)
        bundle = validate_authorization(
            document["authorization_bundle"], self.plan,
            self.receipt_public_key_hex, self.now(),
            require_current=not recovery,
        )
        self.bundle = bundle
        receipt = bundle["authorization_receipt"]
        if recovery:
            _node(
                journal_path, False, 0o600,
                self.expected_uid, self.expected_gid,
            )
            journal = _read_json(journal_path)
            validate_journal(journal, self.plan, bundle)
            if not receipt["consumed_at_unix"] <= journal[
                "started_at_unix"
            ] < receipt["execution_window_expires_at_unix"]:
                raise StateHelperError("production_storage_journal_invalid")
            events = []
            if not os.path.exists(event_path):
                if journal["state"] != "started":
                    raise StateHelperError("production_storage_event_log_invalid")
                events = [journal["transition_event"]]
                validate_recovery_events(
                    events, journal, self.plan, receipt
                )
                _append_events(
                    event_path, events, self.state_root,
                    self.expected_uid, self.expected_gid,
                )
            else:
                _node(
                    event_path, False, 0o600,
                    self.expected_uid, self.expected_gid,
                )
                raw = open(event_path, "rb").read(MAXIMUM_FRAME_BYTES + 1)
                if (
                    not raw.endswith(b"\n") or raw.endswith(b"\n\n")
                    or len(raw) > MAXIMUM_FRAME_BYTES
                ):
                    raise StateHelperError("production_storage_event_log_invalid")
                events = [decode(line) for line in raw[:-1].split(b"\n")]
                if (
                    journal["state"] == "completed"
                    and events[-1].get("event_kind") != "execution_completed"
                ):
                    validate_recovery_events(
                        events, journal, self.plan, receipt,
                        allow_missing_completion=True,
                    )
                    transition = journal["transition_event"]
                    if (
                        transition.get("sequence") != len(events) + 1
                        or transition.get("prior_event_head_sha256")
                        != events[-1].get("event_head_sha256")
                    ):
                        raise StateHelperError(
                            "production_storage_event_log_invalid"
                        )
                    events.append(transition)
                    _append_events(
                        event_path, events, self.state_root,
                        self.expected_uid, self.expected_gid,
                    )
                validate_recovery_events(
                    events, journal, self.plan, receipt
                )
            if journal["transition_event"] != (
                events[0] if journal["state"] == "started" else events[-1]
            ):
                raise StateHelperError("production_storage_event_log_invalid")
            self.journal, self.events = journal, events
            return {"journal": journal, "events": events, "recovered": True}
        now_unix = self.now()
        if observed_state != "source":
            raise StateHelperError("production_storage_unowned_partial_state")
        event = _event([], self.plan, receipt, "execution_started", "started", observation_sha, None, now_unix)
        unsigned = {
            "schema": JOURNAL_SCHEMA,
            "state": "started",
            "plan_sha256": self.plan["plan_sha256"],
            "authorization_bundle_sha256": bundle["bundle_sha256"],
            "authorization_receipt_sha256": receipt["receipt_sha256"],
            "provider_request_id": self.plan["provider_request_id"],
            "idempotency_key_sha256": self.plan["idempotency_key_sha256"],
            "started_at_unix": now_unix,
            "completed_at_unix": None,
            "final_observation": None,
            "prior_journal_head_sha256": receipt["prior_journal_head_sha256"],
            "transition_event": event,
        }
        journal = {**unsigned, "journal_sha256": sha256_json(unsigned)}
        _write_atomic(
            journal_path, journal, self.state_root,
            self.expected_uid, self.expected_gid,
        )
        _append_events(
            event_path, [event], self.state_root,
            self.expected_uid, self.expected_gid,
        )
        self.journal, self.events = journal, [event]
        return {"journal": journal, "events": [event], "recovered": False}

    def failure(self, document):
        if set(document) != {"observation_sha256", "failure_code"} or self.journal is None or self.bundle is None:
            raise StateHelperError("production_storage_state_helper_sequence_invalid")
        failure = document.get("failure_code")
        if (
            document.get("observation_sha256") is not None and not _sha(document["observation_sha256"])
            or not isinstance(failure, str) or not failure.startswith("production_storage_") or len(failure) > 96
            or self.journal["state"] != "started"
        ):
            raise StateHelperError("production_storage_state_helper_frame_invalid")
        event = _event(self.events, self.plan, self.bundle["authorization_receipt"], "execution_failed", "started", document["observation_sha256"], failure, self.now())
        self.events.append(event)
        _append_events(
            self._paths()[1], self.events, self.state_root,
            self.expected_uid, self.expected_gid,
        )
        return {"journal": self.journal, "event": event}

    def complete(self, document):
        if set(document) != {"final_observation"} or self.journal is None or self.bundle is None:
            raise StateHelperError("production_storage_state_helper_sequence_invalid")
        observation = document.get("final_observation")
        observed_state, observation_sha = validate_observation_for_plan(
            observation, self.plan
        )
        if observed_state != "target":
            raise StateHelperError("production_storage_observation_invalid")
        journal_path, event_path = self._paths()
        _node(
            journal_path, False, 0o600,
            self.expected_uid, self.expected_gid,
        )
        persisted = validate_journal(
            _read_json(journal_path), self.plan, self.bundle
        )
        persisted_events = self._read_persisted_events(event_path)
        if persisted["state"] == "completed":
            if persisted.get("final_observation") != observation:
                raise StateHelperError(
                    "production_storage_state_helper_sequence_invalid"
                )
            missing_completion = (
                persisted_events[-1].get("event_kind")
                != "execution_completed"
            )
            validate_recovery_events(
                persisted_events,
                persisted,
                self.plan,
                self.bundle["authorization_receipt"],
                allow_missing_completion=missing_completion,
            )
            transition = persisted["transition_event"]
            if missing_completion:
                if (
                    transition.get("sequence") != len(persisted_events) + 1
                    or transition.get("prior_event_head_sha256")
                    != persisted_events[-1].get("event_head_sha256")
                ):
                    raise StateHelperError(
                        "production_storage_event_log_invalid"
                    )
            elif transition != persisted_events[-1]:
                raise StateHelperError(
                    "production_storage_event_log_invalid"
                )
            self.journal = persisted
            self.events = persisted_events
            return {"journal": persisted, "event": transition}
        if persisted != self.journal:
            raise StateHelperError("production_storage_journal_invalid")
        validate_recovery_events(
            persisted_events,
            persisted,
            self.plan,
            self.bundle["authorization_receipt"],
        )
        self.events = persisted_events
        completed = self.now()
        unsigned = dict(persisted)
        unsigned.pop("journal_sha256", None)
        event = _event(self.events, self.plan, self.bundle["authorization_receipt"], "execution_completed", "completed", observation_sha, None, completed)
        unsigned.update({
            "state": "completed",
            "completed_at_unix": completed,
            "final_observation": observation,
            "transition_event": event,
        })
        journal = {**unsigned, "journal_sha256": sha256_json(unsigned)}
        _write_atomic(
            journal_path, journal, self.state_root,
            self.expected_uid, self.expected_gid,
        )
        self.events.append(event)
        _append_events(
            event_path, self.events, self.state_root,
            self.expected_uid, self.expected_gid,
        )
        self.journal = journal
        return {"journal": journal, "event": event}


def _emit(operation, ok, document):
    unsigned = {"schema": RESPONSE_SCHEMA, "operation": operation, "ok": ok, "document": document}
    sys.stdout.buffer.write(canonical_bytes({**unsigned, "response_sha256": sha256_json(unsigned)}) + b"\n")
    sys.stdout.buffer.flush()


def main():
    if len(sys.argv) != 1:
        return 2
    machine = RootStateMachine()
    try:
        _emit("ready", True, machine.open())
        while True:
            raw = sys.stdin.buffer.readline(MAXIMUM_FRAME_BYTES + 2)
            if not raw:
                return 0
            if not raw.endswith(b"\n") or len(raw) > MAXIMUM_FRAME_BYTES + 1:
                raise StateHelperError("production_storage_state_helper_frame_invalid")
            frame = decode(raw[:-1])
            unsigned = {key: value for key, value in frame.items() if key != "frame_sha256"}
            operation = frame.get("operation") if isinstance(frame, dict) else None
            if (
                not isinstance(frame, dict)
                or set(frame) != {"schema", "operation", "document", "frame_sha256"}
                or frame.get("schema") != FRAME_SCHEMA
                or operation not in {"attest", "begin-or-recover", "append-failure", "complete"}
                or not isinstance(frame.get("document"), dict)
                or frame.get("frame_sha256") != sha256_json(unsigned)
            ):
                raise StateHelperError("production_storage_state_helper_frame_invalid")
            result = {
                "attest": machine.attest,
                "begin-or-recover": machine.begin,
                "append-failure": machine.failure,
                "complete": machine.complete,
            }[operation](frame["document"])
            _emit(operation, True, result)
    except StateHelperError as error:
        code = error.args[0] if len(error.args) == 1 else "production_storage_state_helper_failed"
        _emit("failure", False, {"error_code": code})
        return 2
    except Exception:
        _emit("failure", False, {
            "error_code": "production_storage_state_helper_failed"
        })
        return 2
    finally:
        machine.close()


if __name__ == "__main__":
    raise SystemExit(main())
