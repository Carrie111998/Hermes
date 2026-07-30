#!/usr/bin/env python3
"""Exact passkey-v2 contract for the dual upstream-sync rail activation.

The contract exposes one closed production action.  It cannot select a
command, path, unit, cron job, target, or fallback at runtime.  The action
binds the complete activation plan, and its authorization bundle carries the
single-use WebAuthn grant and signed consumption receipt produced by the
existing owner-gate authority.
"""

from __future__ import annotations

import copy
import re
from typing import Any, Mapping, Protocol

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

from scripts.canary import passkey_v2_protocol as protocol
from scripts.canary import passkey_v2_storage_growth as storage


UPSTREAM_SYNC_ACTION_SCHEMA = (
    "muncho-passkey-v2-dual-upstream-sync-action.v1"
)
UPSTREAM_SYNC_FACTS_SCHEMA = (
    "muncho-passkey-v2-dual-upstream-sync-facts.v1"
)
ACTIVATION_PLAN_SCHEMA = "muncho-dual-upstream-sync-activation-plan.v1"
AUTHORIZATION_BUNDLE_SCHEMA = (
    "muncho-dual-upstream-sync-owner-authorization.v1"
)

PRODUCTION_PROJECT = "adventico-ai-platform"
PRODUCTION_ZONE = "europe-west3-a"
PRODUCTION_VM_NAME = "ai-platform-runtime-01"
PRODUCTION_VM_INSTANCE_ID = "1094477181810932795"
OWNER_DISCORD_USER_ID = storage.OWNER_DISCORD_USER_ID
ACTION_SCOPE = "production_write"
ACTION_STAGE = "activate"
ACTION_CASE_ID = "case:muncho-dual-upstream-sync-rail-activation"
ACTION_TARGET_SYSTEM = (
    "gce:adventico-ai-platform/europe-west3-a/"
    "ai-platform-runtime-01/upstream-sync-rail"
)
ACTION_SUMMARY = (
    "Activate the exact digest-bound Muncho and SkyAI upstream-sync timers, "
    "then retire the exact legacy Hermes cron and collector timer."
)
ACTION_RISK = (
    "This changes four exact systemd unit files and retires one exact legacy "
    "scheduler record only after both replacement timers are proven active."
)
ACTION_ROLLBACK = (
    "Before either replacement timer becomes active, remove only unit files "
    "proven absent at preflight. After activation starts, recover forward "
    "under the same immutable plan without moving an open sync candidate."
)

OPERATION = "activate_dual_upstream_sync_rail.v1"
ALLOWED_OPERATIONS = (OPERATION,)
LEGACY_CRON_JOB_ID = "06ef64d72891"
LEGACY_COLLECTOR_TIMER_UNIT = (
    "muncho-cron-06ef64d72891.timer"
)
UNIT_NAMES = (
    "muncho-dual-upstream-sync.service",
    "muncho-dual-upstream-sync.timer",
    "muncho-dual-upstream-sync-report.service",
    "muncho-dual-upstream-sync-report.timer",
)
TIMER_NAMES = (
    "muncho-dual-upstream-sync.timer",
    "muncho-dual-upstream-sync-report.timer",
)
LEGACY_TIMER_FRAGMENT_PATH = (
    "/etc/systemd/system/muncho-cron-06ef64d72891.timer"
)

_SHA40 = re.compile(r"^[0-9a-f]{40}$")
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_LEGACY_TIMER_PRESTATES = frozenset(
    {
        "absent",
        "disabled_inactive",
        "enabled_inactive",
        "enabled_active",
    }
)
_PLAN_FIELDS = frozenset(
    {
        "schema",
        "operation",
        "production_project",
        "production_zone",
        "production_vm_name",
        "production_vm_instance_id",
        "release_revision",
        "sender_revision",
        "package_manifest_sha256",
        "activation_runtime_sha256",
        "first_catch_up_receipt_sha256",
        "candidate_upstream_sha",
        "fork_main_after_sha",
        "unit_digests",
        "timer_units",
        "legacy_cron_job_id",
        "legacy_cron_source_definition_sha256",
        "legacy_cron_retired_definition_sha256",
        "legacy_collector_timer_unit",
        "legacy_collector_timer_prestate",
        "legacy_collector_timer_fragment_path",
        "legacy_collector_timer_fragment_sha256",
        "new_candidate_may_replace_open_candidate",
        "later_upstream_is_tail_drift",
        "auto_merge_or_deploy_enabled",
        "retire_legacy_only_after_new_timers_active",
        "secret_material_recorded",
        "activation_plan_sha256",
    }
)
_ACTION_FIELDS = frozenset(
    {
        "schema",
        "operation",
        "activation_plan",
        "activation_plan_sha256",
        "authorization_nonce_sha256",
        "allowed_operations",
        "one_shot",
        "caller_selected_commands_allowed",
        "caller_selected_paths_allowed",
        "caller_selected_targets_allowed",
        "generic_shell_fallback_allowed",
    }
)
_BUNDLE_FIELDS = frozenset(
    {
        "schema",
        "action_envelope",
        "challenge_record",
        "grant_record",
        "authorization_receipt",
        "bundle_sha256",
    }
)


class UpstreamSyncPasskeyError(RuntimeError):
    """Stable, secret-free upstream-sync owner-gate contract failure."""


class DedicatedOwnerGateTransport(Protocol):
    """Narrow remote intake transport without a local mutation callback."""

    def invoke_owner_gate(self, canonical_frame: bytes) -> bytes: ...


def _is_sha(value: Any) -> bool:
    return isinstance(value, str) and _SHA256.fullmatch(value) is not None


def validate_activation_plan(value: Any) -> Mapping[str, Any]:
    if not isinstance(value, Mapping) or set(value) != _PLAN_FIELDS:
        raise UpstreamSyncPasskeyError(
            "upstream_sync_passkey_plan_invalid"
        )
    plan = copy.deepcopy(dict(value))
    unsigned = {
        name: item
        for name, item in plan.items()
        if name != "activation_plan_sha256"
    }
    unit_digests = plan.get("unit_digests")
    prestate = plan.get("legacy_collector_timer_prestate")
    if (
        plan.get("schema") != ACTIVATION_PLAN_SCHEMA
        or plan.get("operation") != OPERATION
        or plan.get("production_project") != PRODUCTION_PROJECT
        or plan.get("production_zone") != PRODUCTION_ZONE
        or plan.get("production_vm_name") != PRODUCTION_VM_NAME
        or plan.get("production_vm_instance_id")
        != PRODUCTION_VM_INSTANCE_ID
        or any(
            not isinstance(plan.get(name), str)
            or _SHA40.fullmatch(plan[name]) is None
            for name in (
                "release_revision",
                "sender_revision",
                "candidate_upstream_sha",
                "fork_main_after_sha",
            )
        )
        or any(
            not _is_sha(plan.get(name))
            for name in (
                "package_manifest_sha256",
                "activation_runtime_sha256",
                "first_catch_up_receipt_sha256",
                "legacy_cron_source_definition_sha256",
                "legacy_cron_retired_definition_sha256",
            )
        )
        or not isinstance(unit_digests, Mapping)
        or set(unit_digests) != set(UNIT_NAMES)
        or any(not _is_sha(item) for item in unit_digests.values())
        or plan.get("timer_units") != list(TIMER_NAMES)
        or plan.get("legacy_cron_job_id") != LEGACY_CRON_JOB_ID
        or plan.get("legacy_collector_timer_unit")
        != LEGACY_COLLECTOR_TIMER_UNIT
        or prestate not in _LEGACY_TIMER_PRESTATES
        or prestate == "absent"
        and (
            plan.get("legacy_collector_timer_fragment_path") is not None
            or plan.get("legacy_collector_timer_fragment_sha256") is not None
        )
        or prestate != "absent"
        and (
            plan.get("legacy_collector_timer_fragment_path")
            != LEGACY_TIMER_FRAGMENT_PATH
            or not _is_sha(
                plan.get("legacy_collector_timer_fragment_sha256")
            )
        )
        or plan.get("new_candidate_may_replace_open_candidate") is not False
        or plan.get("later_upstream_is_tail_drift") is not True
        or plan.get("auto_merge_or_deploy_enabled") is not False
        or plan.get("retire_legacy_only_after_new_timers_active") is not True
        or plan.get("secret_material_recorded") is not False
        or plan.get("activation_plan_sha256")
        != protocol.sha256_json(unsigned)
    ):
        raise UpstreamSyncPasskeyError(
            "upstream_sync_passkey_plan_invalid"
        )
    return plan


def build_activation_plan(
    *,
    release_revision: str,
    sender_revision: str,
    package_manifest_sha256: str,
    activation_runtime_sha256: str,
    first_catch_up_receipt_sha256: str,
    candidate_upstream_sha: str,
    fork_main_after_sha: str,
    unit_digests: Mapping[str, str],
    legacy_cron_source_definition_sha256: str,
    legacy_cron_retired_definition_sha256: str,
    legacy_collector_timer_prestate: str,
    legacy_collector_timer_fragment_path: str | None,
    legacy_collector_timer_fragment_sha256: str | None,
) -> Mapping[str, Any]:
    unsigned = {
        "schema": ACTIVATION_PLAN_SCHEMA,
        "operation": OPERATION,
        "production_project": PRODUCTION_PROJECT,
        "production_zone": PRODUCTION_ZONE,
        "production_vm_name": PRODUCTION_VM_NAME,
        "production_vm_instance_id": PRODUCTION_VM_INSTANCE_ID,
        "release_revision": release_revision,
        "sender_revision": sender_revision,
        "package_manifest_sha256": package_manifest_sha256,
        "activation_runtime_sha256": activation_runtime_sha256,
        "first_catch_up_receipt_sha256": first_catch_up_receipt_sha256,
        "candidate_upstream_sha": candidate_upstream_sha,
        "fork_main_after_sha": fork_main_after_sha,
        "unit_digests": dict(unit_digests),
        "timer_units": list(TIMER_NAMES),
        "legacy_cron_job_id": LEGACY_CRON_JOB_ID,
        "legacy_cron_source_definition_sha256": (
            legacy_cron_source_definition_sha256
        ),
        "legacy_cron_retired_definition_sha256": (
            legacy_cron_retired_definition_sha256
        ),
        "legacy_collector_timer_unit": LEGACY_COLLECTOR_TIMER_UNIT,
        "legacy_collector_timer_prestate": (
            legacy_collector_timer_prestate
        ),
        "legacy_collector_timer_fragment_path": (
            legacy_collector_timer_fragment_path
        ),
        "legacy_collector_timer_fragment_sha256": (
            legacy_collector_timer_fragment_sha256
        ),
        "new_candidate_may_replace_open_candidate": False,
        "later_upstream_is_tail_drift": True,
        "auto_merge_or_deploy_enabled": False,
        "retire_legacy_only_after_new_timers_active": True,
        "secret_material_recorded": False,
    }
    return validate_activation_plan(
        {
            **unsigned,
            "activation_plan_sha256": protocol.sha256_json(unsigned),
        }
    )


def build_upstream_sync_action_envelope(
    *,
    activation_plan: Mapping[str, Any],
    authorization_nonce_sha256: str,
    authority_manifest_sha256: str,
    authority_host_receipt_sha256: str,
    external_iam_receipt_sha256: str,
    prior_authoritative_receipt_sha256: str,
    prior_event_head_sha256: str,
    issued_at_unix: int,
) -> Mapping[str, Any]:
    plan = validate_activation_plan(activation_plan)
    if not _is_sha(authorization_nonce_sha256):
        raise UpstreamSyncPasskeyError(
            "upstream_sync_passkey_nonce_invalid"
        )
    payload = {
        "schema": UPSTREAM_SYNC_ACTION_SCHEMA,
        "operation": OPERATION,
        "activation_plan": plan,
        "activation_plan_sha256": plan["activation_plan_sha256"],
        "authorization_nonce_sha256": authorization_nonce_sha256,
        "allowed_operations": list(ALLOWED_OPERATIONS),
        "one_shot": True,
        "caller_selected_commands_allowed": False,
        "caller_selected_paths_allowed": False,
        "caller_selected_targets_allowed": False,
        "generic_shell_fallback_allowed": False,
    }
    transaction_id = protocol.sha256_json(
        {
            "schema": "muncho-dual-upstream-sync-transaction.v1",
            "activation_plan_sha256": plan["activation_plan_sha256"],
            "authorization_nonce_sha256": authorization_nonce_sha256,
        }
    )
    request_id = protocol.sha256_json(
        {
            "schema": "muncho-dual-upstream-sync-request.v1",
            "transaction_id": transaction_id,
            "issued_at_unix": issued_at_unix,
        }
    )
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
            executor_plan_sha256=plan["activation_plan_sha256"],
            transaction_id=transaction_id,
            stage=ACTION_STAGE,
            webauthn_rp_id=protocol.PRODUCTION_RP_ID,
            webauthn_origin=protocol.PRODUCTION_ORIGIN,
            authority_release_sha=plan["release_revision"],
            authority_manifest_sha256=authority_manifest_sha256,
            authority_host_receipt_sha256=authority_host_receipt_sha256,
            source_preflight_sha256=(
                plan["first_catch_up_receipt_sha256"]
            ),
            live_projection_sha256=plan["package_manifest_sha256"],
            external_iam_receipt_sha256=external_iam_receipt_sha256,
            prior_authoritative_receipt_sha256=(
                prior_authoritative_receipt_sha256
            ),
            prior_event_head_sha256=prior_event_head_sha256,
            issued_at_unix=issued_at_unix,
            approval_ttl_seconds=protocol.MAXIMUM_TTL_SECONDS,
        )
    except protocol.PasskeyV2ProtocolError:
        raise UpstreamSyncPasskeyError(
            "upstream_sync_passkey_action_invalid"
        ) from None


def validate_upstream_sync_action_envelope(
    envelope: Any,
    *,
    activation_plan: Mapping[str, Any] | None = None,
) -> Mapping[str, Any]:
    try:
        action = protocol.validate_action_envelope(envelope)
        protocol.require_production_webauthn_identity(action)
    except protocol.PasskeyV2ProtocolError:
        raise UpstreamSyncPasskeyError(
            "upstream_sync_passkey_action_invalid"
        ) from None
    payload = action["action_payload"]
    if not isinstance(payload, Mapping) or set(payload) != _ACTION_FIELDS:
        raise UpstreamSyncPasskeyError(
            "upstream_sync_passkey_action_invalid"
        )
    try:
        plan = validate_activation_plan(payload["activation_plan"])
    except (KeyError, UpstreamSyncPasskeyError):
        raise UpstreamSyncPasskeyError(
            "upstream_sync_passkey_action_invalid"
        ) from None
    nonce = payload.get("authorization_nonce_sha256")
    expected_transaction = protocol.sha256_json(
        {
            "schema": "muncho-dual-upstream-sync-transaction.v1",
            "activation_plan_sha256": plan["activation_plan_sha256"],
            "authorization_nonce_sha256": nonce,
        }
    )
    expected_request = protocol.sha256_json(
        {
            "schema": "muncho-dual-upstream-sync-request.v1",
            "transaction_id": expected_transaction,
            "issued_at_unix": action["issued_at_unix"],
        }
    )
    if (
        payload.get("schema") != UPSTREAM_SYNC_ACTION_SCHEMA
        or payload.get("operation") != OPERATION
        or payload.get("activation_plan_sha256")
        != plan["activation_plan_sha256"]
        or not _is_sha(nonce)
        or payload.get("allowed_operations") != list(ALLOWED_OPERATIONS)
        or payload.get("one_shot") is not True
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
        or action["required_approver_discord_user_id"]
        != OWNER_DISCORD_USER_ID
        or action["executor_release_sha"] != plan["release_revision"]
        or action["authority_release_sha"] != plan["release_revision"]
        or action["executor_plan_sha256"]
        != plan["activation_plan_sha256"]
        or action["source_preflight_sha256"]
        != plan["first_catch_up_receipt_sha256"]
        or action["live_projection_sha256"]
        != plan["package_manifest_sha256"]
        or action["transaction_id"] != expected_transaction
        or action["request_id"] != expected_request
    ):
        raise UpstreamSyncPasskeyError(
            "upstream_sync_passkey_action_invalid"
        )
    if activation_plan is not None:
        expected = validate_activation_plan(activation_plan)
        if protocol.canonical_json_bytes(expected) != (
            protocol.canonical_json_bytes(plan)
        ):
            raise UpstreamSyncPasskeyError(
                "upstream_sync_passkey_plan_binding_invalid"
            )
    return action


def mechanical_approval_facts(envelope: Any) -> Mapping[str, Any]:
    action = validate_upstream_sync_action_envelope(envelope)
    plan = action["action_payload"]["activation_plan"]
    return {
        "schema": UPSTREAM_SYNC_FACTS_SCHEMA,
        "production_project": PRODUCTION_PROJECT,
        "production_zone": PRODUCTION_ZONE,
        "production_vm_name": PRODUCTION_VM_NAME,
        "production_vm_instance_id": PRODUCTION_VM_INSTANCE_ID,
        "release_revision": plan["release_revision"],
        "package_manifest_sha256": plan["package_manifest_sha256"],
        "activation_plan_sha256": plan["activation_plan_sha256"],
        "first_catch_up_receipt_sha256": (
            plan["first_catch_up_receipt_sha256"]
        ),
        "candidate_upstream_sha": plan["candidate_upstream_sha"],
        "legacy_cron_job_id": LEGACY_CRON_JOB_ID,
        "legacy_collector_timer_unit": LEGACY_COLLECTOR_TIMER_UNIT,
        "exact_timer_units": list(TIMER_NAMES),
        "exact_allowed_operations": list(ALLOWED_OPERATIONS),
        "single_use": True,
        "user_verification_required": True,
        "totp_available": False,
        "caller_selected_commands_allowed": False,
        "caller_selected_paths_allowed": False,
        "caller_selected_targets_allowed": False,
    }


def build_authorization_bundle(
    *,
    activation_plan: Mapping[str, Any],
    action_envelope: Mapping[str, Any],
    challenge_record: Mapping[str, Any],
    grant_record: Mapping[str, Any],
    authorization_receipt: Mapping[str, Any],
    receipt_public_key: Ed25519PublicKey,
) -> Mapping[str, Any]:
    plan = validate_activation_plan(activation_plan)
    unsigned = {
        "schema": AUTHORIZATION_BUNDLE_SCHEMA,
        "action_envelope": copy.deepcopy(dict(action_envelope)),
        "challenge_record": copy.deepcopy(dict(challenge_record)),
        "grant_record": copy.deepcopy(dict(grant_record)),
        "authorization_receipt": copy.deepcopy(
            dict(authorization_receipt)
        ),
    }
    value = {
        **unsigned,
        "bundle_sha256": protocol.sha256_json(unsigned),
    }
    return validate_authorization_bundle(
        value,
        activation_plan=plan,
        receipt_public_key=receipt_public_key,
        now_unix=authorization_receipt.get("consumed_at_unix"),
    )


def validate_authorization_bundle(
    value: Any,
    *,
    activation_plan: Mapping[str, Any],
    receipt_public_key: Ed25519PublicKey,
    now_unix: int,
) -> Mapping[str, Any]:
    if (
        not isinstance(value, Mapping)
        or set(value) != _BUNDLE_FIELDS
        or not isinstance(receipt_public_key, Ed25519PublicKey)
        or type(now_unix) is not int
        or now_unix <= 0
    ):
        raise UpstreamSyncPasskeyError(
            "upstream_sync_passkey_authorization_invalid"
        )
    bundle = copy.deepcopy(dict(value))
    unsigned = {
        name: item
        for name, item in bundle.items()
        if name != "bundle_sha256"
    }
    if (
        bundle.get("schema") != AUTHORIZATION_BUNDLE_SCHEMA
        or bundle.get("bundle_sha256") != protocol.sha256_json(unsigned)
    ):
        raise UpstreamSyncPasskeyError(
            "upstream_sync_passkey_authorization_invalid"
        )
    try:
        action = validate_upstream_sync_action_envelope(
            bundle["action_envelope"],
            activation_plan=activation_plan,
        )
        challenge = protocol.validate_challenge_record(
            bundle["challenge_record"],
            envelope=action,
        )
        grant = protocol.validate_passkey_grant(
            bundle["grant_record"],
            envelope=action,
            challenge=challenge,
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
        UpstreamSyncPasskeyError,
    ):
        raise UpstreamSyncPasskeyError(
            "upstream_sync_passkey_authorization_invalid"
        ) from None
    if (
        grant["method"] != "passkey"
        or grant["single_use"] is not True
        or grant["user_verified"] is not True
        or receipt["outcome"] != "ALLOW"
        or receipt["mutation_authorized"] is not True
        or receipt["mutation_executed"] is not False
        or receipt["authorization_disposition"] != "authorized_once"
        or receipt["approval_method"] != "passkey"
        or receipt["approver_discord_user_id"]
        != OWNER_DISCORD_USER_ID
        or not receipt["consumed_at_unix"] <= now_unix
        < receipt["execution_window_expires_at_unix"]
    ):
        raise UpstreamSyncPasskeyError(
            "upstream_sync_passkey_authorization_invalid"
        )
    return bundle


class UpstreamSyncPasskeyBoundary:
    """Owner-side exchange with the two fixed upstream-sync intake actions."""

    def __init__(
        self,
        authority_release_sha: str,
        transport: DedicatedOwnerGateTransport,
    ) -> None:
        if (
            _SHA40.fullmatch(authority_release_sha or "") is None
            or not callable(getattr(transport, "invoke_owner_gate", None))
            or callable(getattr(transport, "run_local_compute_mutation", None))
        ):
            raise UpstreamSyncPasskeyError(
                "upstream_sync_owner_gate_transport_invalid"
            )
        self.authority_release_sha = authority_release_sha
        self._transport = transport

    def _invoke(
        self,
        operation: str,
        document: Mapping[str, Any],
    ) -> Mapping[str, Any]:
        if operation not in {
            "request_upstream_sync_activation",
            "consume_upstream_sync_activation",
        }:
            raise UpstreamSyncPasskeyError(
                "upstream_sync_owner_gate_operation_invalid"
            )
        unsigned = {
            "schema": storage.REMOTE_FRAME_SCHEMA,
            "operation": operation,
            "release_sha": self.authority_release_sha,
            "document": copy.deepcopy(dict(document)),
        }
        frame = {
            **unsigned,
            "frame_sha256": protocol.sha256_json(unsigned),
        }
        raw = self._transport.invoke_owner_gate(
            protocol.canonical_json_bytes(frame)
        )
        try:
            response = protocol.decode_canonical_json(raw)
        except protocol.PasskeyV2ProtocolError:
            raise UpstreamSyncPasskeyError(
                "upstream_sync_owner_gate_response_invalid"
            ) from None
        response_unsigned = (
            {
                name: item
                for name, item in response.items()
                if name != "response_sha256"
            }
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
            or response.get("schema") != storage.REMOTE_RESPONSE_SCHEMA
            or response.get("operation") != operation
            or response.get("release_sha") != self.authority_release_sha
            or response.get("ok") is not True
            or not isinstance(response.get("document"), Mapping)
            or response.get("response_sha256")
            != protocol.sha256_json(response_unsigned)
        ):
            raise UpstreamSyncPasskeyError(
                "upstream_sync_owner_gate_response_invalid"
            )
        return copy.deepcopy(dict(response["document"]))

    def request(
        self,
        *,
        activation_plan: Mapping[str, Any],
        authorization_nonce_sha256: str,
    ) -> Mapping[str, Any]:
        plan = validate_activation_plan(activation_plan)
        if not _is_sha(authorization_nonce_sha256):
            raise UpstreamSyncPasskeyError(
                "upstream_sync_passkey_nonce_invalid"
            )
        return self._invoke(
            "request_upstream_sync_activation",
            {
                "activation_plan": plan,
                "authorization_nonce_sha256": (
                    authorization_nonce_sha256
                ),
            },
        )

    def consume(
        self,
        *,
        activation_plan: Mapping[str, Any],
        request_id: str,
        consume_attempt_id: str,
    ) -> Mapping[str, Any]:
        plan = validate_activation_plan(activation_plan)
        if not _is_sha(request_id) or not _is_sha(consume_attempt_id):
            raise UpstreamSyncPasskeyError(
                "upstream_sync_consume_attempt_invalid"
            )
        result = self._invoke(
            "consume_upstream_sync_activation",
            {
                "activation_plan": plan,
                "request_id": request_id,
                "consume_attempt_id": consume_attempt_id,
            },
        )
        if not isinstance(result.get("authorization_bundle"), Mapping):
            raise UpstreamSyncPasskeyError(
                "upstream_sync_owner_gate_response_invalid"
            )
        return result


__all__ = [
    "ACTION_CASE_ID",
    "ACTION_SCOPE",
    "ACTION_STAGE",
    "ACTION_TARGET_SYSTEM",
    "ACTIVATION_PLAN_SCHEMA",
    "ALLOWED_OPERATIONS",
    "AUTHORIZATION_BUNDLE_SCHEMA",
    "LEGACY_COLLECTOR_TIMER_UNIT",
    "LEGACY_CRON_JOB_ID",
    "OPERATION",
    "TIMER_NAMES",
    "UNIT_NAMES",
    "UPSTREAM_SYNC_ACTION_SCHEMA",
    "UPSTREAM_SYNC_FACTS_SCHEMA",
    "UpstreamSyncPasskeyBoundary",
    "UpstreamSyncPasskeyError",
    "build_activation_plan",
    "build_authorization_bundle",
    "build_upstream_sync_action_envelope",
    "mechanical_approval_facts",
    "validate_activation_plan",
    "validate_authorization_bundle",
    "validate_upstream_sync_action_envelope",
]
