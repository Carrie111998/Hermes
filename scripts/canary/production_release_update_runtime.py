#!/usr/bin/env python3
"""Crash-safe release-update state machine over a durable append-only journal.

Host collection and mutation live behind injected actions.  The state machine
defines the ordering contract: recovery is fully gated before application
mutation, rollback is allowed only before the durable commit intent, and every
post-commit recovery is forward-only.
"""

from __future__ import annotations

import hashlib
import json
import re
import time
from dataclasses import dataclass
from typing import Any, Mapping, NoReturn, Protocol, Sequence

from scripts.canary import production_cutover_activation_lock as authority_lock
from scripts.canary import production_release_update_contract as authority


INTENT_SCHEMA = "muncho-production-release-update-intent.v4"
AUTHORITY_RECORD_SCHEMA = "muncho-production-release-update-authority-record.v2"
EVENT_SCHEMA = "muncho-production-release-update-event.v2"
ZERO_SHA256 = "0" * 64
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_REVISION = re.compile(r"^[0-9a-f]{40}$")

FORWARD_PHASES = (
    "candidate_validated",
    "voice_guard_initial",
    "prestate_archived",
    "unit_inputs_prepared",
    "recovery_gate_installed",
    "pre_fence_cas_validated",
    "application_mutation_intent",
    "voice_guard_final",
    "consumers_fenced",
    "release_consumers_zeroed",
    "host_payloads_applied",
    "target_started_disabled",
    "target_health_validated",
    "unit_inputs_finalize_preauthorized",
    "activation_commit_intent",
    "unit_inputs_finalized",
    "release_pointer_rotated",
    "target_consumers_enabled",
    "terminal_validated",
    "completed",
)
ROLLBACK_PHASES = (
    "rollback_intent",
    "target_stopped",
    "host_prestate_restored",
    "predecessor_consumers_restored",
    "rollback_validated",
    "rolled_back",
)
PREAUTHORIZED_ROLLBACK_PHASES = (
    "rollback_intent",
    "unit_inputs_finalize_preauthorization_cancelled",
    *ROLLBACK_PHASES[1:],
)
ABORT_PHASES = (
    "approval_expired_abort_intent",
    "preapplication_cleanup",
    "aborted",
)
TRANSACTION_PHASES = frozenset(
    set(FORWARD_PHASES)
    | set(PREAUTHORIZED_ROLLBACK_PHASES)
    | set(ABORT_PHASES)
)
ACTION_PHASES = frozenset(
    set(FORWARD_PHASES)
    - {
        "application_mutation_intent",
        "activation_commit_intent",
        "completed",
    }
    | set(PREAUTHORIZED_ROLLBACK_PHASES) - {"rollback_intent", "rolled_back"}
    | {"preapplication_cleanup"}
)
REVALIDATION_PHASES = frozenset({
    "completed_revalidated",
    "rolled_back_revalidated",
    "aborted_revalidated",
    "pre_mutation_cas_revalidated",
})
ACTION_RECEIPT_PHASES = ACTION_PHASES | REVALIDATION_PHASES
_ACTION_RECEIPT_SCHEMA_VERSIONS: Mapping[str, int] = {
    "unit_inputs_prepared": 2,
    "unit_inputs_finalized": 2,
}
COMMIT_PHASE = "activation_commit_intent"
UNIT_INPUT_PREAUTHORIZATION_PHASE = "unit_inputs_finalize_preauthorized"
UNIT_INPUT_PREAUTHORIZATION_CANCEL_PHASE = (
    "unit_inputs_finalize_preauthorization_cancelled"
)
UNIT_INPUT_PREAUTHORIZATION_DISCRIMINATOR_PHASE = "target_health_validated"
FIRST_APPLICATION_MUTATION_PHASE = "application_mutation_intent"
TERMINAL_PHASES = frozenset({"completed", "rolled_back", "aborted"})
EXPECTED_CONSUMER_UNIT_COUNT = 75
EXPECTED_SERVICE_UNIT_COUNT = 47
EXPECTED_TRIGGER_UNIT_COUNT = 28
EXPECTED_LONG_RUNNING_SERVICE_UNIT_COUNT = 18
EXPECTED_STARTUP_ONESHOT_SERVICE_UNIT_COUNT = 1
EXPECTED_TRIGGERED_ONESHOT_SERVICE_UNIT_COUNT = 28
EXPECTED_ONESHOT_SERVICE_UNIT_COUNT = (
    EXPECTED_STARTUP_ONESHOT_SERVICE_UNIT_COUNT
    + EXPECTED_TRIGGERED_ONESHOT_SERVICE_UNIT_COUNT
)

_ACTION_RECEIPT_BASE_FIELDS = frozenset({
    "schema",
    "phase",
    "intent_sha256",
    "publication_sha256",
    "plan_sha256",
    "approval_sha256",
    "predecessor_revision",
    "release_revision",
    "idempotency_key",
    "prior_receipts_sha256",
    "secret_material_recorded",
    "secret_digest_recorded",
})
_LIVE_CAS_EVIDENCE_FIELDS = frozenset({
    "host_observation_receipt_sha256",
    "observed_predecessor_activation_receipt_sha256",
    "observed_pointer_revision",
    "observed_unit_inputs_revision",
    "consumer_inventory_sha256",
    "expected_consumer_unit_count",
    "expected_service_unit_count",
    "expected_long_running_service_unit_count",
    "expected_oneshot_service_unit_count",
    "expected_startup_oneshot_service_unit_count",
    "expected_triggered_oneshot_service_unit_count",
    "expected_trigger_unit_count",
    "compare_and_swap_matched",
})
_ACTION_RECEIPT_EVIDENCE_FIELDS: Mapping[str, frozenset[str]] = {
    "candidate_validated": frozenset({
        "release_root",
        "candidate_tree_sha256",
        "candidate_seal_receipt_sha256",
        "builder_terminal_receipt_sha256",
        "verified_regular_file_count",
        "release_root_owned",
        "release_tree_read_only",
    }),
    "voice_guard_initial": frozenset({
        "voice_guard_observation_sha256",
        "protected_service_set_sha256",
        "observed_active_revision",
        "healthy_voice_target_count",
        "all_required_voice_targets_healthy",
    }),
    "prestate_archived": frozenset({
        "prestate_archive_sha256",
        "host_inventory_sha256",
        "activation_plan_sha256",
        "rollback_plan_sha256",
        "host_artifact_manifest_sha256",
        "archived_target_set_sha256",
        "archived_target_count",
        "archive_fsynced",
    }),
    "unit_inputs_prepared": frozenset({
        "prepared_unit_input_publication_sha256",
        "prepared_unit_input_set_sha256",
        "prepared_unit_input_count",
        "unit_input_rotation_transaction_sha256",
        "unit_input_prepared_receipt_sha256",
        "predecessor_unit_inputs_revision",
        "successor_unit_inputs_revision",
        "active_inputs_unchanged",
    }),
    "recovery_gate_installed": frozenset({
        "recovery_gate_artifact_sha256",
        "recovery_gate_unit_sha256",
        "host_artifact_manifest_sha256",
        "recovery_gate_enabled",
        "recovery_gate_verified",
    }),
    "pre_fence_cas_validated": _LIVE_CAS_EVIDENCE_FIELDS,
    "pre_mutation_cas_revalidated": _LIVE_CAS_EVIDENCE_FIELDS,
    "voice_guard_final": frozenset({
        "voice_guard_observation_sha256",
        "protected_service_set_sha256",
        "observed_active_revision",
        "healthy_voice_target_count",
        "all_required_voice_targets_healthy",
    }),
    "consumers_fenced": frozenset({
        "host_observation_receipt_sha256",
        "consumer_inventory_sha256",
        "fenced_unit_count",
        "remaining_active_unit_count",
        "remaining_consumer_process_count",
        "fence_verified",
    }),
    "release_consumers_zeroed": frozenset({
        "host_observation_receipt_sha256",
        "consumer_inventory_sha256",
        "observed_consumer_process_count",
        "observed_unknown_process_count",
        "observed_mutable_pointer_process_count",
        "need_daemon_reload_unit_count",
        "all_release_consumers_zeroed",
    }),
    "host_payloads_applied": frozenset({
        "host_payload_manifest_sha256",
        "applied_target_set_sha256",
        "applied_target_count",
        "applied_revision",
        "payloads_fsynced",
    }),
    "target_started_disabled": frozenset({
        "host_observation_receipt_sha256",
        "consumer_inventory_sha256",
        "started_long_running_service_unit_count",
        "started_startup_oneshot_service_unit_count",
        "started_triggered_oneshot_service_unit_count",
        "enabled_trigger_unit_count",
        "observed_target_revision",
        "target_process_set_sha256",
        "target_runtime_classes_ready",
        "only_declared_cutover_service_classes_started",
        "external_ingress_disabled",
    }),
    "target_health_validated": frozenset({
        "health_observation_sha256",
        "target_process_set_sha256",
        "observed_target_revision",
        "validated_endpoint_count",
        "validated_connector_count",
        "external_ingress_disabled",
        "all_required_health_checks_passed",
    }),
    "unit_inputs_finalize_preauthorized": frozenset({
        "unit_input_publication_sha256",
        "unit_input_rotation_transaction_sha256",
        "unit_input_prepared_receipt_sha256",
        "unit_input_preauthorization_receipt_sha256",
        "unit_input_abort_receipt_sha256",
        "unit_input_activation_begin_sha256",
        "predecessor_unit_inputs_revision",
        "successor_unit_inputs_revision",
        "observed_unit_inputs_revision",
        "preauthorization_persisted",
        "authoritative_inputs_unchanged",
    }),
    "unit_inputs_finalized": frozenset({
        "unit_input_publication_sha256",
        "unit_input_activation_receipt_sha256",
        "unit_input_activation_begin_sha256",
        "unit_input_rotation_transaction_sha256",
        "unit_input_prepared_receipt_sha256",
        "unit_input_preauthorization_receipt_sha256",
        "unit_input_abort_receipt_sha256",
        "finalized_unit_input_set_sha256",
        "finalized_unit_input_count",
        "predecessor_unit_inputs_revision",
        "successor_unit_inputs_revision",
        "observed_unit_inputs_revision",
        "authoritative_inputs_active",
    }),
    "release_pointer_rotated": frozenset({
        "previous_pointer_revision",
        "current_pointer_revision",
        "unit_input_activation_receipt_sha256",
        "pointer_target",
        "pointer_target_sha256",
        "pointer_parent_fsynced",
        "compare_and_swap_succeeded",
    }),
    "target_consumers_enabled": frozenset({
        "host_observation_receipt_sha256",
        "consumer_inventory_sha256",
        "active_long_running_service_unit_count",
        "completed_startup_oneshot_service_unit_count",
        "direct_started_triggered_oneshot_service_unit_count",
        "enabled_trigger_unit_count",
        "observed_target_revision",
        "unknown_consumer_process_count",
        "all_expected_consumers_enabled",
    }),
    "terminal_validated": frozenset({
        "host_observation_receipt_sha256",
        "consumer_inventory_sha256",
        "observed_pointer_revision",
        "observed_unit_inputs_revision",
        "active_long_running_service_unit_count",
        "completed_startup_oneshot_service_unit_count",
        "direct_started_triggered_oneshot_service_unit_count",
        "enabled_trigger_unit_count",
        "unknown_consumer_process_count",
        "need_daemon_reload_unit_count",
        "terminal_health_observation_sha256",
        "all_required_health_checks_passed",
    }),
    "target_stopped": frozenset({
        "host_observation_receipt_sha256",
        "consumer_inventory_sha256",
        "target_revision",
        "stopped_target_process_count",
        "remaining_target_process_count",
        "target_services_inactive",
    }),
    "unit_inputs_finalize_preauthorization_cancelled": frozenset({
        "unit_input_publication_sha256",
        "unit_input_rotation_transaction_sha256",
        "unit_input_prepared_receipt_sha256",
        "unit_input_preauthorization_receipt_sha256",
        "unit_input_abort_receipt_sha256",
        "unit_input_activation_begin_sha256",
        "predecessor_unit_inputs_revision",
        "successor_unit_inputs_revision",
        "observed_unit_inputs_revision",
        "preauthorization_persisted",
        "abort_persisted",
        "preauthorization_terminal",
        "authoritative_inputs_unchanged",
    }),
    "host_prestate_restored": frozenset({
        "prestate_archive_sha256",
        "restored_target_set_sha256",
        "restored_target_count",
        "restored_revision",
        "restore_fsynced",
    }),
    "predecessor_consumers_restored": frozenset({
        "host_observation_receipt_sha256",
        "consumer_inventory_sha256",
        "active_long_running_service_unit_count",
        "completed_startup_oneshot_service_unit_count",
        "direct_started_triggered_oneshot_service_unit_count",
        "enabled_trigger_unit_count",
        "observed_active_revision",
        "remaining_target_process_count",
        "predecessor_health_observation_sha256",
        "predecessor_healthy",
    }),
    "rollback_validated": frozenset({
        "host_observation_receipt_sha256",
        "consumer_inventory_sha256",
        "observed_pointer_revision",
        "observed_unit_inputs_revision",
        "active_long_running_service_unit_count",
        "completed_startup_oneshot_service_unit_count",
        "direct_started_triggered_oneshot_service_unit_count",
        "enabled_trigger_unit_count",
        "remaining_target_process_count",
        "restored_prestate_archive_sha256",
        "unknown_consumer_process_count",
        "need_daemon_reload_unit_count",
        "rollback_health_observation_sha256",
        "all_required_health_checks_passed",
    }),
    "completed_revalidated": frozenset({
        "terminal_receipt_sha256",
        "host_observation_receipt_sha256",
        "consumer_inventory_sha256",
        "observed_pointer_revision",
        "observed_unit_inputs_revision",
        "active_long_running_service_unit_count",
        "completed_startup_oneshot_service_unit_count",
        "direct_started_triggered_oneshot_service_unit_count",
        "enabled_trigger_unit_count",
        "unknown_consumer_process_count",
        "need_daemon_reload_unit_count",
        "terminal_health_observation_sha256",
        "all_required_health_checks_passed",
    }),
    "rolled_back_revalidated": frozenset({
        "rollback_receipt_sha256",
        "host_observation_receipt_sha256",
        "consumer_inventory_sha256",
        "observed_pointer_revision",
        "observed_unit_inputs_revision",
        "active_long_running_service_unit_count",
        "completed_startup_oneshot_service_unit_count",
        "direct_started_triggered_oneshot_service_unit_count",
        "enabled_trigger_unit_count",
        "remaining_target_process_count",
        "restored_prestate_archive_sha256",
        "unknown_consumer_process_count",
        "need_daemon_reload_unit_count",
        "rollback_health_observation_sha256",
        "all_required_health_checks_passed",
    }),
    "preapplication_cleanup": frozenset({
        "cleanup_observation_sha256",
        "retained_prestate_archive_sha256",
        "prepared_unit_inputs_discarded",
        "recovery_gate_removed",
        "application_state_unchanged",
    }),
    "aborted_revalidated": frozenset({
        "cleanup_receipt_sha256",
        "observed_pointer_revision",
        "observed_unit_inputs_revision",
        "recovery_gate_absent",
        "prepared_inputs_inactive",
        "no_application_mutation",
    }),
}

_PLAN_PROJECTION_FIELDS = (
    "release_root",
    "source_tree_oid",
    "source_v3_manifest_sha256",
    "builder_request_sha256",
    "builder_terminal_receipt_sha256",
    "candidate_seal_receipt_sha256",
    "whole_tree_manifest_sha256",
    "runtime_dependency_manifest_sha256",
    "uv_sha256",
    "interpreter_sha256",
    "entrypoint_sha256",
    "host_inventory_sha256",
    "release_consumer_set_sha256",
    "host_artifact_manifest_sha256",
    "cron_artifact_index_sha256",
    "alias_artifact_index_sha256",
    "successor_unit_input_publication_sha256",
    "activation_plan_sha256",
    "rollback_plan_sha256",
)
_INTENT_FIELDS = frozenset({
    "schema",
    "publication_sha256",
    "plan_sha256",
    "approval_sha256",
    "predecessor_trust_sha256",
    "predecessor_current_receipt_sha256",
    "predecessor_revision",
    "release_revision",
    *_PLAN_PROJECTION_FIELDS,
    "approval_issued_at_unix",
    "approval_expires_at_unix",
    "transaction_nonce_sha256",
    "created_at_unix",
    "secret_material_recorded",
    "secret_digest_recorded",
    "intent_sha256",
})
_AUTHORITY_RECORD_FIELDS = frozenset({
    "schema",
    "intent",
    "publication",
    "trusted_predecessor",
    "expected_predecessor_trust_sha256",
    "predecessor_current_receipt_sha256",
    "secret_material_recorded",
    "secret_digest_recorded",
    "authority_record_sha256",
})
_EVENT_FIELDS = frozenset({
    "schema",
    "intent_sha256",
    "sequence",
    "phase",
    "prior_event_sha256",
    "receipt",
    "receipt_sha256",
    "created_at_unix",
    "secret_material_recorded",
    "secret_digest_recorded",
    "event_sha256",
})


class ProductionReleaseUpdateRuntimeError(RuntimeError):
    """Stable, secret-free release update state-machine failure."""


class ReleaseUpdateActions(Protocol):
    """Idempotent host action boundary used by the transaction state machine.

    The runtime holds the global activation lock around every call.  An action
    that invokes a unit-input rotation primitive may reacquire that same lock
    only through its supported same-thread reentrant path; it must not acquire
    another activation lock first or invert the global-before-local order.
    """

    def perform(
        self,
        phase: str,
        *,
        intent: Mapping[str, Any],
        receipts: Mapping[str, Mapping[str, Any]],
    ) -> Mapping[str, Any]:
        """Perform or exactly resume one phase and return a public receipt."""


class ReleaseUpdateJournal(Protocol):
    """Durable create-only event store."""

    @property
    def authority_record(self) -> Mapping[str, Any]:
        """Return the exact durable signed-authority transaction header."""

    def load(self) -> Sequence[Mapping[str, Any]]:
        """Return every persisted event in sequence order."""

    def append(self, event: Mapping[str, Any]) -> Mapping[str, Any]:
        """Durably append or exactly replay one event, then return readback."""


@dataclass(frozen=True)
class TransactionState:
    intent: Mapping[str, Any]
    events: tuple[Mapping[str, Any], ...]
    receipts: Mapping[str, Mapping[str, Any]]
    next_forward_phase: str | None
    next_rollback_phase: str | None
    next_abort_phase: str | None
    commit_intent_persisted: bool
    unit_input_preauthorization_persisted: bool
    application_mutation_started: bool
    terminal_phase: str | None


def _canonical(value: Any) -> bytes:
    try:
        return json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8", errors="strict")
    except (TypeError, ValueError, UnicodeError) as exc:
        raise ProductionReleaseUpdateRuntimeError(
            "release_update_runtime_json_invalid"
        ) from exc


def _sha(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def action_idempotency_key(
    intent: Mapping[str, Any],
    phase: str,
) -> str:
    validated = validate_intent(intent)
    if phase not in ACTION_RECEIPT_PHASES:
        raise ProductionReleaseUpdateRuntimeError(
            "release_update_runtime_phase_invalid"
        )
    return _sha(
        _canonical({
            "intent_sha256": validated["intent_sha256"],
            "phase": phase,
        })
    )


def action_receipt_schema(phase: str) -> str:
    """Return the exact schema for one action or revalidation receipt."""

    if phase not in ACTION_RECEIPT_PHASES:
        raise ProductionReleaseUpdateRuntimeError(
            "release_update_runtime_phase_invalid"
        )
    version = _ACTION_RECEIPT_SCHEMA_VERSIONS.get(phase, 1)
    return (
        f"muncho-production-release-update-{phase}-receipt.v{version}"
    )


def _mapping(
    value: Any,
    fields: frozenset[str],
    code: str,
) -> dict[str, Any]:
    if not isinstance(value, Mapping) or set(value) != fields:
        raise ProductionReleaseUpdateRuntimeError(code)
    return dict(value)


def _self_hashed(
    value: Any,
    *,
    fields: frozenset[str],
    digest_field: str,
    code: str,
) -> dict[str, Any]:
    raw = _mapping(value, fields, code)
    digest = raw[digest_field]
    unsigned = {key: item for key, item in raw.items() if key != digest_field}
    if (
        not isinstance(digest, str)
        or _SHA256.fullmatch(digest) is None
        or digest != _sha(_canonical(unsigned))
    ):
        raise ProductionReleaseUpdateRuntimeError(code)
    return raw


def build_intent(
    *,
    publication: Mapping[str, Any],
    trusted_predecessor: Mapping[str, Any],
    expected_predecessor_trust_sha256: str,
    predecessor_current_receipt_sha256: str,
) -> Mapping[str, Any]:
    raw_approval = publication.get("approval") if isinstance(
        publication,
        Mapping,
    ) else None
    signed_issued_at_unix = (
        raw_approval.get("issued_at_unix")
        if isinstance(raw_approval, Mapping)
        else None
    )
    if type(signed_issued_at_unix) is not int:
        raise ProductionReleaseUpdateRuntimeError(
            "release_update_runtime_authority_invalid"
        )
    try:
        validated = authority.validate_publication(
            publication,
            trusted_predecessor=trusted_predecessor,
            expected_predecessor_trust_sha256=(expected_predecessor_trust_sha256),
            now_unix=signed_issued_at_unix,
        )
    except authority.ProductionReleaseUpdateContractError as exc:
        raise ProductionReleaseUpdateRuntimeError(
            "release_update_runtime_authority_invalid"
        ) from exc
    plan = validated["plan"]
    approval = validated["approval"]
    if (
        _SHA256.fullmatch(str(predecessor_current_receipt_sha256)) is None
        or predecessor_current_receipt_sha256
        != plan["predecessor_activation_receipt_sha256"]
    ):
        raise ProductionReleaseUpdateRuntimeError(
            "release_update_runtime_predecessor_changed"
        )
    unsigned = {
        "schema": INTENT_SCHEMA,
        "publication_sha256": validated["publication_sha256"],
        "plan_sha256": plan["plan_sha256"],
        "approval_sha256": approval["approval_sha256"],
        "predecessor_trust_sha256": expected_predecessor_trust_sha256,
        "predecessor_current_receipt_sha256": (predecessor_current_receipt_sha256),
        "predecessor_revision": plan["predecessor_revision"],
        "release_revision": plan["release_revision"],
        **{name: plan[name] for name in _PLAN_PROJECTION_FIELDS},
        "approval_issued_at_unix": approval["issued_at_unix"],
        "approval_expires_at_unix": approval["expires_at_unix"],
        "transaction_nonce_sha256": approval["nonce_sha256"],
        "created_at_unix": approval["issued_at_unix"],
        "secret_material_recorded": False,
        "secret_digest_recorded": False,
    }
    intent = {**unsigned, "intent_sha256": _sha(_canonical(unsigned))}
    return validate_intent(intent)


def validate_intent(value: Any) -> Mapping[str, Any]:
    raw = _self_hashed(
        value,
        fields=_INTENT_FIELDS,
        digest_field="intent_sha256",
        code="release_update_runtime_intent_invalid",
    )
    if (
        raw.get("schema") != INTENT_SCHEMA
        or any(
            _SHA256.fullmatch(str(raw.get(name, ""))) is None
            for name in (
                "publication_sha256",
                "plan_sha256",
                "approval_sha256",
                "predecessor_trust_sha256",
                "predecessor_current_receipt_sha256",
                "transaction_nonce_sha256",
            )
        )
        or _REVISION.fullmatch(str(raw.get("predecessor_revision", ""))) is None
        or _REVISION.fullmatch(str(raw.get("release_revision", ""))) is None
        or raw["predecessor_revision"] == raw["release_revision"]
        or raw["predecessor_revision"][:12] == raw["release_revision"][:12]
        or not isinstance(raw.get("release_root"), str)
        or raw["release_root"]
        != authority.expected_release_root(str(raw["release_revision"]))
        or not isinstance(raw.get("source_tree_oid"), str)
        or re.fullmatch(r"[0-9a-f]{40}(?:[0-9a-f]{24})?", raw["source_tree_oid"])
        is None
        or any(
            _SHA256.fullmatch(str(raw.get(name, ""))) is None
            for name in _PLAN_PROJECTION_FIELDS
            if name not in {"release_root", "source_tree_oid"}
        )
        or type(raw.get("approval_issued_at_unix")) is not int
        or type(raw.get("approval_expires_at_unix")) is not int
        or type(raw.get("created_at_unix")) is not int
        or raw["created_at_unix"] != raw["approval_issued_at_unix"]
        or not raw["created_at_unix"] < raw["approval_expires_at_unix"]
        or raw["created_at_unix"] <= 0
        or raw.get("secret_material_recorded") is not False
        or raw.get("secret_digest_recorded") is not False
    ):
        raise ProductionReleaseUpdateRuntimeError(
            "release_update_runtime_intent_invalid"
        )
    return raw


def build_authority_record(
    *,
    publication: Mapping[str, Any],
    trusted_predecessor: Mapping[str, Any],
    expected_predecessor_trust_sha256: str,
    predecessor_current_receipt_sha256: str,
) -> Mapping[str, Any]:
    """Build the exact signed-authority header persisted before event zero."""

    intent = build_intent(
        publication=publication,
        trusted_predecessor=trusted_predecessor,
        expected_predecessor_trust_sha256=(expected_predecessor_trust_sha256),
        predecessor_current_receipt_sha256=(predecessor_current_receipt_sha256),
    )
    unsigned = {
        "schema": AUTHORITY_RECORD_SCHEMA,
        "intent": dict(intent),
        "publication": dict(publication),
        "trusted_predecessor": dict(trusted_predecessor),
        "expected_predecessor_trust_sha256": (expected_predecessor_trust_sha256),
        "predecessor_current_receipt_sha256": (predecessor_current_receipt_sha256),
        "secret_material_recorded": False,
        "secret_digest_recorded": False,
    }
    record = {
        **unsigned,
        "authority_record_sha256": _sha(_canonical(unsigned)),
    }
    return validate_authority_record(record)


def validate_authority_record(value: Any) -> Mapping[str, Any]:
    """Rebind an intent to its signed publication and external predecessor."""

    raw = _self_hashed(
        value,
        fields=_AUTHORITY_RECORD_FIELDS,
        digest_field="authority_record_sha256",
        code="release_update_runtime_authority_record_invalid",
    )
    intent = validate_intent(raw.get("intent"))
    expected_trust = raw.get("expected_predecessor_trust_sha256")
    predecessor_receipt = raw.get("predecessor_current_receipt_sha256")
    publication = raw.get("publication")
    trusted_predecessor = raw.get("trusted_predecessor")
    if (
        raw.get("schema") != AUTHORITY_RECORD_SCHEMA
        or not isinstance(publication, Mapping)
        or not isinstance(trusted_predecessor, Mapping)
        or _SHA256.fullmatch(str(expected_trust or "")) is None
        or _SHA256.fullmatch(str(predecessor_receipt or "")) is None
        or raw.get("secret_material_recorded") is not False
        or raw.get("secret_digest_recorded") is not False
    ):
        raise ProductionReleaseUpdateRuntimeError(
            "release_update_runtime_authority_record_invalid"
        )
    try:
        rebuilt = build_intent(
            publication=publication,
            trusted_predecessor=trusted_predecessor,
            expected_predecessor_trust_sha256=str(expected_trust),
            predecessor_current_receipt_sha256=str(predecessor_receipt),
        )
    except (ProductionReleaseUpdateRuntimeError, TypeError, ValueError) as exc:
        raise ProductionReleaseUpdateRuntimeError(
            "release_update_runtime_authority_record_invalid"
        ) from exc
    if rebuilt != intent:
        raise ProductionReleaseUpdateRuntimeError(
            "release_update_runtime_authority_record_invalid"
        )
    return {
        **raw,
        "intent": intent,
        "publication": dict(publication),
        "trusted_predecessor": dict(trusted_predecessor),
    }


def _validate_receipt(value: Any) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise ProductionReleaseUpdateRuntimeError(
            "release_update_runtime_receipt_invalid"
        )
    raw = dict(value)
    if (
        not raw
        or raw.get("secret_material_recorded") is not False
        or raw.get("secret_digest_recorded") is not False
    ):
        raise ProductionReleaseUpdateRuntimeError(
            "release_update_runtime_receipt_invalid"
        )
    _canonical(raw)
    return raw


def _action_receipt_invalid() -> NoReturn:
    raise ProductionReleaseUpdateRuntimeError(
        "release_update_runtime_action_receipt_invalid"
    )


def _require_action(condition: bool) -> None:
    if not condition:
        _action_receipt_invalid()


def _receipt_for_evidence(
    receipts: Mapping[str, Mapping[str, Any]],
    phase: str,
) -> Mapping[str, Any]:
    receipt = receipts.get(phase)
    if not isinstance(receipt, Mapping):
        _action_receipt_invalid()
    return receipt


def _sha_field(receipt: Mapping[str, Any], name: str) -> str:
    value = receipt.get(name)
    if not isinstance(value, str) or _SHA256.fullmatch(value) is None:
        _action_receipt_invalid()
    return value


def _revision_field(receipt: Mapping[str, Any], name: str) -> str:
    value = receipt.get(name)
    if not isinstance(value, str) or _REVISION.fullmatch(value) is None:
        _action_receipt_invalid()
    return value


def _count_field(
    receipt: Mapping[str, Any],
    name: str,
    *,
    positive: bool = False,
) -> int:
    value = receipt.get(name)
    if type(value) is not int or value < 0 or (positive and value == 0):
        _action_receipt_invalid()
    return value


def _true_field(receipt: Mapping[str, Any], name: str) -> None:
    _require_action(receipt.get(name) is True)


def _validate_action_evidence(
    receipt: Mapping[str, Any],
    *,
    intent: Mapping[str, Any],
    phase: str,
    receipts: Mapping[str, Mapping[str, Any]],
) -> None:
    predecessor = str(intent["predecessor_revision"])
    release = str(intent["release_revision"])

    if phase == "candidate_validated":
        _require_action(
            receipt.get("release_root") == intent["release_root"]
            and _sha_field(receipt, "candidate_tree_sha256")
            == intent["whole_tree_manifest_sha256"]
            and _sha_field(receipt, "candidate_seal_receipt_sha256")
            == intent["candidate_seal_receipt_sha256"]
            and _sha_field(receipt, "builder_terminal_receipt_sha256")
            == intent["builder_terminal_receipt_sha256"]
        )
        _count_field(receipt, "verified_regular_file_count", positive=True)
        _true_field(receipt, "release_root_owned")
        _true_field(receipt, "release_tree_read_only")
        return

    if phase in {"voice_guard_initial", "voice_guard_final"}:
        _sha_field(receipt, "voice_guard_observation_sha256")
        protected_set = _sha_field(receipt, "protected_service_set_sha256")
        _require_action(
            _revision_field(receipt, "observed_active_revision") == predecessor
        )
        healthy_count = _count_field(
            receipt,
            "healthy_voice_target_count",
            positive=True,
        )
        _true_field(receipt, "all_required_voice_targets_healthy")
        if phase == "voice_guard_final":
            initial = _receipt_for_evidence(
                receipts,
                "voice_guard_initial",
            )
            _require_action(
                protected_set == initial.get("protected_service_set_sha256")
                and healthy_count == initial.get("healthy_voice_target_count")
            )
        return

    if phase == "prestate_archived":
        _sha_field(receipt, "prestate_archive_sha256")
        _require_action(
            _sha_field(receipt, "host_inventory_sha256")
            == intent["host_inventory_sha256"]
            and _sha_field(receipt, "activation_plan_sha256")
            == intent["activation_plan_sha256"]
            and _sha_field(receipt, "rollback_plan_sha256")
            == intent["rollback_plan_sha256"]
            and _sha_field(receipt, "host_artifact_manifest_sha256")
            == intent["host_artifact_manifest_sha256"]
        )
        _sha_field(receipt, "archived_target_set_sha256")
        _count_field(receipt, "archived_target_count", positive=True)
        _true_field(receipt, "archive_fsynced")
        return

    if phase == "unit_inputs_prepared":
        _require_action(
            _sha_field(
                receipt,
                "prepared_unit_input_publication_sha256",
            )
            == intent["successor_unit_input_publication_sha256"]
            and _revision_field(
                receipt,
                "predecessor_unit_inputs_revision",
            )
            == predecessor
            and _revision_field(
                receipt,
                "successor_unit_inputs_revision",
            )
            == release
        )
        _require_action(
            _sha_field(receipt, "prepared_unit_input_set_sha256")
            != ZERO_SHA256
            and _sha_field(
                receipt,
                "unit_input_rotation_transaction_sha256",
            )
            != ZERO_SHA256
            and _sha_field(
                receipt,
                "unit_input_prepared_receipt_sha256",
            )
            != ZERO_SHA256
        )
        _count_field(receipt, "prepared_unit_input_count", positive=True)
        _true_field(receipt, "active_inputs_unchanged")
        return

    if phase == "recovery_gate_installed":
        _sha_field(receipt, "recovery_gate_artifact_sha256")
        _sha_field(receipt, "recovery_gate_unit_sha256")
        _require_action(
            _sha_field(receipt, "host_artifact_manifest_sha256")
            == intent["host_artifact_manifest_sha256"]
        )
        _true_field(receipt, "recovery_gate_enabled")
        _true_field(receipt, "recovery_gate_verified")
        return

    if phase in {
        "pre_fence_cas_validated",
        "pre_mutation_cas_revalidated",
    }:
        _sha_field(receipt, "host_observation_receipt_sha256")
        _require_action(
            _sha_field(
                receipt,
                "observed_predecessor_activation_receipt_sha256",
            )
            == intent["predecessor_current_receipt_sha256"]
            and _revision_field(receipt, "observed_pointer_revision") == predecessor
            and _revision_field(receipt, "observed_unit_inputs_revision") == predecessor
        )
        _require_action(
            _sha_field(receipt, "consumer_inventory_sha256")
            == intent["release_consumer_set_sha256"]
        )
        unit_count = _count_field(
            receipt,
            "expected_consumer_unit_count",
            positive=True,
        )
        service_count = _count_field(
            receipt,
            "expected_service_unit_count",
            positive=True,
        )
        long_running_count = _count_field(
            receipt,
            "expected_long_running_service_unit_count",
            positive=True,
        )
        oneshot_count = _count_field(
            receipt,
            "expected_oneshot_service_unit_count",
            positive=True,
        )
        startup_oneshot_count = _count_field(
            receipt,
            "expected_startup_oneshot_service_unit_count",
            positive=True,
        )
        triggered_oneshot_count = _count_field(
            receipt,
            "expected_triggered_oneshot_service_unit_count",
            positive=True,
        )
        trigger_count = _count_field(
            receipt,
            "expected_trigger_unit_count",
            positive=True,
        )
        _require_action(
            unit_count == EXPECTED_CONSUMER_UNIT_COUNT
            and service_count == EXPECTED_SERVICE_UNIT_COUNT
            and long_running_count == EXPECTED_LONG_RUNNING_SERVICE_UNIT_COUNT
            and oneshot_count == EXPECTED_ONESHOT_SERVICE_UNIT_COUNT
            and startup_oneshot_count == EXPECTED_STARTUP_ONESHOT_SERVICE_UNIT_COUNT
            and triggered_oneshot_count == EXPECTED_TRIGGERED_ONESHOT_SERVICE_UNIT_COUNT
            and trigger_count == EXPECTED_TRIGGER_UNIT_COUNT
            and unit_count == service_count + trigger_count
            and service_count == long_running_count + oneshot_count
            and oneshot_count == startup_oneshot_count + triggered_oneshot_count
            and triggered_oneshot_count == trigger_count
        )
        _true_field(receipt, "compare_and_swap_matched")
        return

    if phase == "preapplication_cleanup":
        archive = receipts.get("prestate_archived")
        expected_archive = (
            ZERO_SHA256 if archive is None else archive.get("prestate_archive_sha256")
        )
        _sha_field(receipt, "cleanup_observation_sha256")
        _require_action(
            _sha_field(receipt, "retained_prestate_archive_sha256") == expected_archive
        )
        _true_field(receipt, "prepared_unit_inputs_discarded")
        _true_field(receipt, "recovery_gate_removed")
        _true_field(receipt, "application_state_unchanged")
        return

    if phase == "aborted_revalidated":
        cleanup = _receipt_for_evidence(
            receipts,
            "preapplication_cleanup",
        )
        _require_action(
            _sha_field(receipt, "cleanup_receipt_sha256") == _sha(_canonical(cleanup))
            and _revision_field(receipt, "observed_pointer_revision") == predecessor
            and _revision_field(receipt, "observed_unit_inputs_revision") == predecessor
        )
        _true_field(receipt, "recovery_gate_absent")
        _true_field(receipt, "prepared_inputs_inactive")
        _true_field(receipt, "no_application_mutation")
        return

    inventory = _receipt_for_evidence(
        receipts,
        "pre_fence_cas_validated",
    )
    inventory_sha256 = inventory.get("consumer_inventory_sha256")
    expected_units = inventory.get("expected_consumer_unit_count")
    expected_long_running = inventory.get("expected_long_running_service_unit_count")
    expected_startup_oneshots = inventory.get(
        "expected_startup_oneshot_service_unit_count"
    )
    expected_triggers = inventory.get("expected_trigger_unit_count")

    if phase == "consumers_fenced":
        _sha_field(receipt, "host_observation_receipt_sha256")
        _require_action(
            _sha_field(receipt, "consumer_inventory_sha256") == inventory_sha256
            and _count_field(receipt, "fenced_unit_count") == expected_units
            and _count_field(receipt, "remaining_active_unit_count") == 0
            and _count_field(
                receipt,
                "remaining_consumer_process_count",
            )
            == 0
        )
        _true_field(receipt, "fence_verified")
        return

    if phase == "release_consumers_zeroed":
        _sha_field(receipt, "host_observation_receipt_sha256")
        _require_action(
            _sha_field(receipt, "consumer_inventory_sha256") == inventory_sha256
            and _count_field(receipt, "observed_consumer_process_count") == 0
            and _count_field(receipt, "observed_unknown_process_count") == 0
            and _count_field(
                receipt,
                "observed_mutable_pointer_process_count",
            )
            == 0
            and _count_field(receipt, "need_daemon_reload_unit_count") == 0
        )
        _true_field(receipt, "all_release_consumers_zeroed")
        return

    if phase == "host_payloads_applied":
        archived = _receipt_for_evidence(receipts, "prestate_archived")
        _require_action(
            _sha_field(receipt, "host_payload_manifest_sha256")
            == intent["host_artifact_manifest_sha256"]
            and _sha_field(receipt, "applied_target_set_sha256")
            == archived.get("archived_target_set_sha256")
            and _count_field(receipt, "applied_target_count")
            == archived.get("archived_target_count")
            and _revision_field(receipt, "applied_revision") == release
        )
        _true_field(receipt, "payloads_fsynced")
        return

    if phase == "target_started_disabled":
        _sha_field(receipt, "host_observation_receipt_sha256")
        _require_action(
            _sha_field(receipt, "consumer_inventory_sha256") == inventory_sha256
            and _count_field(
                receipt,
                "started_long_running_service_unit_count",
            )
            == expected_long_running
            and _count_field(
                receipt,
                "started_startup_oneshot_service_unit_count",
            )
            == expected_startup_oneshots
            and _count_field(
                receipt,
                "started_triggered_oneshot_service_unit_count",
            )
            == 0
            and _count_field(receipt, "enabled_trigger_unit_count") == 0
            and _revision_field(receipt, "observed_target_revision") == release
        )
        _sha_field(receipt, "target_process_set_sha256")
        _true_field(receipt, "target_runtime_classes_ready")
        _true_field(
            receipt,
            "only_declared_cutover_service_classes_started",
        )
        _true_field(receipt, "external_ingress_disabled")
        return

    if phase == "target_health_validated":
        started = _receipt_for_evidence(
            receipts,
            "target_started_disabled",
        )
        _sha_field(receipt, "health_observation_sha256")
        _require_action(
            _sha_field(receipt, "target_process_set_sha256")
            == started.get("target_process_set_sha256")
            and _revision_field(receipt, "observed_target_revision") == release
        )
        _count_field(receipt, "validated_endpoint_count", positive=True)
        _count_field(receipt, "validated_connector_count")
        _true_field(receipt, "external_ingress_disabled")
        _true_field(receipt, "all_required_health_checks_passed")
        return

    if phase == UNIT_INPUT_PREAUTHORIZATION_PHASE:
        prepared = _receipt_for_evidence(
            receipts,
            "unit_inputs_prepared",
        )
        _require_action(
            _sha_field(receipt, "unit_input_publication_sha256")
            == intent["successor_unit_input_publication_sha256"]
            and _sha_field(
                receipt,
                "unit_input_rotation_transaction_sha256",
            )
            == prepared.get("unit_input_rotation_transaction_sha256")
            and _sha_field(
                receipt,
                "unit_input_prepared_receipt_sha256",
            )
            == prepared.get("unit_input_prepared_receipt_sha256")
            and _sha_field(
                receipt,
                "unit_input_preauthorization_receipt_sha256",
            )
            != ZERO_SHA256
            and _sha_field(
                receipt,
                "unit_input_abort_receipt_sha256",
            )
            == ZERO_SHA256
            and _sha_field(
                receipt,
                "unit_input_activation_begin_sha256",
            )
            == ZERO_SHA256
            and _revision_field(
                receipt,
                "predecessor_unit_inputs_revision",
            )
            == predecessor
            and _revision_field(
                receipt,
                "successor_unit_inputs_revision",
            )
            == release
            and _revision_field(
                receipt,
                "observed_unit_inputs_revision",
            )
            == predecessor
        )
        _true_field(receipt, "preauthorization_persisted")
        _true_field(receipt, "authoritative_inputs_unchanged")
        return

    if phase == "unit_inputs_finalized":
        prepared = _receipt_for_evidence(
            receipts,
            "unit_inputs_prepared",
        )
        preauthorization = _receipt_for_evidence(
            receipts,
            UNIT_INPUT_PREAUTHORIZATION_PHASE,
        )
        _require_action(
            _sha_field(receipt, "unit_input_publication_sha256")
            == intent["successor_unit_input_publication_sha256"]
            and _sha_field(
                receipt,
                "unit_input_rotation_transaction_sha256",
            )
            == prepared.get("unit_input_rotation_transaction_sha256")
            and _sha_field(
                receipt,
                "unit_input_prepared_receipt_sha256",
            )
            == prepared.get("unit_input_prepared_receipt_sha256")
            and _sha_field(
                receipt,
                "unit_input_preauthorization_receipt_sha256",
            )
            == preauthorization.get(
                "unit_input_preauthorization_receipt_sha256"
            )
            and _sha_field(
                receipt,
                "unit_input_activation_begin_sha256",
            )
            != ZERO_SHA256
            and _sha_field(
                receipt,
                "unit_input_activation_receipt_sha256",
            )
            != ZERO_SHA256
            and _sha_field(
                receipt,
                "unit_input_abort_receipt_sha256",
            )
            == ZERO_SHA256
            and _sha_field(receipt, "finalized_unit_input_set_sha256")
            == prepared.get("prepared_unit_input_set_sha256")
            and _count_field(receipt, "finalized_unit_input_count")
            == prepared.get("prepared_unit_input_count")
            and _revision_field(
                receipt,
                "predecessor_unit_inputs_revision",
            )
            == predecessor
            and _revision_field(
                receipt,
                "successor_unit_inputs_revision",
            )
            == release
            and _revision_field(receipt, "observed_unit_inputs_revision") == release
        )
        _true_field(receipt, "authoritative_inputs_active")
        return

    if phase == UNIT_INPUT_PREAUTHORIZATION_CANCEL_PHASE:
        prepared = _receipt_for_evidence(
            receipts,
            "unit_inputs_prepared",
        )
        preauthorization = receipts.get(UNIT_INPUT_PREAUTHORIZATION_PHASE)
        preauthorization_sha256 = _sha_field(
            receipt,
            "unit_input_preauthorization_receipt_sha256",
        )
        abort_sha256 = _sha_field(
            receipt,
            "unit_input_abort_receipt_sha256",
        )
        preauthorization_persisted = receipt.get(
            "preauthorization_persisted"
        )
        abort_persisted = receipt.get("abort_persisted")
        _require_action(
            _sha_field(receipt, "unit_input_publication_sha256")
            == intent["successor_unit_input_publication_sha256"]
            and _sha_field(
                receipt,
                "unit_input_rotation_transaction_sha256",
            )
            == prepared.get("unit_input_rotation_transaction_sha256")
            and _sha_field(
                receipt,
                "unit_input_prepared_receipt_sha256",
            )
            == prepared.get("unit_input_prepared_receipt_sha256")
            and _sha_field(
                receipt,
                "unit_input_activation_begin_sha256",
            )
            == ZERO_SHA256
            and _revision_field(
                receipt,
                "predecessor_unit_inputs_revision",
            )
            == predecessor
            and _revision_field(
                receipt,
                "successor_unit_inputs_revision",
            )
            == release
            and _revision_field(
                receipt,
                "observed_unit_inputs_revision",
            )
            == predecessor
            and (
                (
                    preauthorization_persisted is False
                    and abort_persisted is False
                    and preauthorization_sha256 == ZERO_SHA256
                    and abort_sha256 == ZERO_SHA256
                )
                or (
                    preauthorization_persisted is True
                    and abort_persisted is True
                    and preauthorization_sha256 != ZERO_SHA256
                    and abort_sha256 != ZERO_SHA256
                )
            )
        )
        if isinstance(preauthorization, Mapping):
            _require_action(
                preauthorization_persisted is True
                and preauthorization_sha256
                == preauthorization.get(
                    "unit_input_preauthorization_receipt_sha256"
                )
            )
        _true_field(receipt, "preauthorization_terminal")
        _true_field(receipt, "authoritative_inputs_unchanged")
        return

    if phase == "release_pointer_rotated":
        finalized = _receipt_for_evidence(
            receipts,
            "unit_inputs_finalized",
        )
        _require_action(
            _revision_field(receipt, "previous_pointer_revision") == predecessor
            and _revision_field(receipt, "current_pointer_revision") == release
            and _sha_field(
                receipt,
                "unit_input_activation_receipt_sha256",
            )
            == finalized.get("unit_input_activation_receipt_sha256")
            and receipt.get("pointer_target") == intent["release_root"]
            and _sha_field(receipt, "pointer_target_sha256")
            == _sha(str(intent["release_root"]).encode("utf-8"))
        )
        _true_field(receipt, "pointer_parent_fsynced")
        _true_field(receipt, "compare_and_swap_succeeded")
        return

    if phase == "target_consumers_enabled":
        _sha_field(receipt, "host_observation_receipt_sha256")
        _require_action(
            _sha_field(receipt, "consumer_inventory_sha256") == inventory_sha256
            and _count_field(
                receipt,
                "active_long_running_service_unit_count",
            )
            == expected_long_running
            and _count_field(
                receipt,
                "completed_startup_oneshot_service_unit_count",
            )
            == expected_startup_oneshots
            and _count_field(
                receipt,
                "direct_started_triggered_oneshot_service_unit_count",
            )
            == 0
            and _count_field(receipt, "enabled_trigger_unit_count") == expected_triggers
            and _revision_field(receipt, "observed_target_revision") == release
            and _count_field(receipt, "unknown_consumer_process_count") == 0
        )
        _true_field(receipt, "all_expected_consumers_enabled")
        return

    if phase in {"terminal_validated", "completed_revalidated"}:
        if phase == "completed_revalidated":
            terminal = _receipt_for_evidence(
                receipts,
                "terminal_validated",
            )
            _require_action(
                _sha_field(receipt, "terminal_receipt_sha256")
                == _sha(_canonical(terminal))
            )
        _sha_field(receipt, "host_observation_receipt_sha256")
        _require_action(
            _sha_field(receipt, "consumer_inventory_sha256") == inventory_sha256
            and _revision_field(receipt, "observed_pointer_revision") == release
            and _revision_field(receipt, "observed_unit_inputs_revision") == release
            and _count_field(
                receipt,
                "active_long_running_service_unit_count",
            )
            == expected_long_running
            and _count_field(
                receipt,
                "completed_startup_oneshot_service_unit_count",
            )
            == expected_startup_oneshots
            and _count_field(
                receipt,
                "direct_started_triggered_oneshot_service_unit_count",
            )
            == 0
            and _count_field(receipt, "enabled_trigger_unit_count") == expected_triggers
            and _count_field(receipt, "unknown_consumer_process_count") == 0
            and _count_field(receipt, "need_daemon_reload_unit_count") == 0
        )
        _sha_field(receipt, "terminal_health_observation_sha256")
        _true_field(receipt, "all_required_health_checks_passed")
        return

    if phase == "target_stopped":
        _sha_field(receipt, "host_observation_receipt_sha256")
        _require_action(
            _sha_field(receipt, "consumer_inventory_sha256") == inventory_sha256
            and _revision_field(receipt, "target_revision") == release
        )
        _count_field(receipt, "stopped_target_process_count")
        _require_action(_count_field(receipt, "remaining_target_process_count") == 0)
        _true_field(receipt, "target_services_inactive")
        return

    if phase == "host_prestate_restored":
        archived = _receipt_for_evidence(receipts, "prestate_archived")
        _require_action(
            _sha_field(receipt, "prestate_archive_sha256")
            == archived.get("prestate_archive_sha256")
            and _sha_field(receipt, "restored_target_set_sha256")
            == archived.get("archived_target_set_sha256")
            and _count_field(receipt, "restored_target_count")
            == archived.get("archived_target_count")
            and _revision_field(receipt, "restored_revision") == predecessor
        )
        _true_field(receipt, "restore_fsynced")
        return

    if phase == "predecessor_consumers_restored":
        _sha_field(receipt, "host_observation_receipt_sha256")
        _require_action(
            _sha_field(receipt, "consumer_inventory_sha256") == inventory_sha256
            and _count_field(
                receipt,
                "active_long_running_service_unit_count",
            )
            == expected_long_running
            and _count_field(
                receipt,
                "completed_startup_oneshot_service_unit_count",
            )
            == expected_startup_oneshots
            and _count_field(
                receipt,
                "direct_started_triggered_oneshot_service_unit_count",
            )
            == 0
            and _count_field(receipt, "enabled_trigger_unit_count") == expected_triggers
            and _revision_field(receipt, "observed_active_revision") == predecessor
            and _count_field(receipt, "remaining_target_process_count") == 0
        )
        _sha_field(receipt, "predecessor_health_observation_sha256")
        _true_field(receipt, "predecessor_healthy")
        return

    if phase in {"rollback_validated", "rolled_back_revalidated"}:
        if phase == "rolled_back_revalidated":
            rollback = _receipt_for_evidence(
                receipts,
                "rollback_validated",
            )
            _require_action(
                _sha_field(receipt, "rollback_receipt_sha256")
                == _sha(_canonical(rollback))
            )
        archived = _receipt_for_evidence(receipts, "prestate_archived")
        _sha_field(receipt, "host_observation_receipt_sha256")
        _require_action(
            _sha_field(receipt, "consumer_inventory_sha256") == inventory_sha256
            and _revision_field(receipt, "observed_pointer_revision") == predecessor
            and _revision_field(receipt, "observed_unit_inputs_revision") == predecessor
            and _count_field(
                receipt,
                "active_long_running_service_unit_count",
            )
            == expected_long_running
            and _count_field(
                receipt,
                "completed_startup_oneshot_service_unit_count",
            )
            == expected_startup_oneshots
            and _count_field(
                receipt,
                "direct_started_triggered_oneshot_service_unit_count",
            )
            == 0
            and _count_field(receipt, "enabled_trigger_unit_count") == expected_triggers
            and _count_field(receipt, "remaining_target_process_count") == 0
            and _sha_field(
                receipt,
                "restored_prestate_archive_sha256",
            )
            == archived.get("prestate_archive_sha256")
            and _count_field(receipt, "unknown_consumer_process_count") == 0
            and _count_field(receipt, "need_daemon_reload_unit_count") == 0
        )
        _sha_field(receipt, "rollback_health_observation_sha256")
        _true_field(receipt, "all_required_health_checks_passed")
        return

    _action_receipt_invalid()


def _validate_bound_action_receipt(
    value: Any,
    *,
    intent: Mapping[str, Any],
    phase: str,
    receipts: Mapping[str, Mapping[str, Any]],
) -> Mapping[str, Any]:
    receipt = _validate_receipt(value)
    evidence_fields = _ACTION_RECEIPT_EVIDENCE_FIELDS.get(phase)
    if (
        evidence_fields is None
        or set(receipt) != _ACTION_RECEIPT_BASE_FIELDS | evidence_fields
        or receipt.get("schema") != action_receipt_schema(phase)
        or receipt.get("phase") != phase
        or receipt.get("intent_sha256") != intent["intent_sha256"]
        or receipt.get("publication_sha256") != intent["publication_sha256"]
        or receipt.get("plan_sha256") != intent["plan_sha256"]
        or receipt.get("approval_sha256") != intent["approval_sha256"]
        or receipt.get("predecessor_revision") != intent["predecessor_revision"]
        or receipt.get("release_revision") != intent["release_revision"]
        or receipt.get("idempotency_key") != action_idempotency_key(intent, phase)
        or receipt.get("prior_receipts_sha256") != _sha(_canonical(receipts))
    ):
        _action_receipt_invalid()
    _validate_action_evidence(
        receipt,
        intent=intent,
        phase=phase,
        receipts=receipts,
    )
    return receipt


def _event_unsigned(
    *,
    intent: Mapping[str, Any],
    sequence: int,
    phase: str,
    prior_event_sha256: str,
    receipt: Mapping[str, Any],
    created_at_unix: int,
) -> Mapping[str, Any]:
    return {
        "schema": EVENT_SCHEMA,
        "intent_sha256": intent["intent_sha256"],
        "sequence": sequence,
        "phase": phase,
        "prior_event_sha256": prior_event_sha256,
        "receipt": dict(receipt),
        "receipt_sha256": _sha(_canonical(receipt)),
        "created_at_unix": created_at_unix,
        "secret_material_recorded": False,
        "secret_digest_recorded": False,
    }


def build_event(
    *,
    intent: Mapping[str, Any],
    sequence: int,
    phase: str,
    prior_event_sha256: str,
    receipt: Mapping[str, Any],
    created_at_unix: int,
) -> Mapping[str, Any]:
    validated_intent = validate_intent(intent)
    validated_receipt = _validate_receipt(receipt)
    if (
        type(sequence) is not int
        or sequence < 0
        or phase not in TRANSACTION_PHASES
        or _SHA256.fullmatch(prior_event_sha256) is None
        or type(created_at_unix) is not int
        or created_at_unix < validated_intent["created_at_unix"]
    ):
        raise ProductionReleaseUpdateRuntimeError(
            "release_update_runtime_event_invalid"
        )
    unsigned = _event_unsigned(
        intent=validated_intent,
        sequence=sequence,
        phase=phase,
        prior_event_sha256=prior_event_sha256,
        receipt=validated_receipt,
        created_at_unix=created_at_unix,
    )
    event = {**unsigned, "event_sha256": _sha(_canonical(unsigned))}
    return validate_event(event, intent=validated_intent)


def validate_event(
    value: Any,
    *,
    intent: Mapping[str, Any],
) -> Mapping[str, Any]:
    validated_intent = validate_intent(intent)
    raw = _self_hashed(
        value,
        fields=_EVENT_FIELDS,
        digest_field="event_sha256",
        code="release_update_runtime_event_invalid",
    )
    receipt = _validate_receipt(raw.get("receipt"))
    if (
        raw.get("schema") != EVENT_SCHEMA
        or raw.get("intent_sha256") != validated_intent["intent_sha256"]
        or type(raw.get("sequence")) is not int
        or raw["sequence"] < 0
        or raw.get("phase") not in TRANSACTION_PHASES
        or _SHA256.fullmatch(str(raw.get("prior_event_sha256", ""))) is None
        or raw.get("receipt_sha256") != _sha(_canonical(receipt))
        or type(raw.get("created_at_unix")) is not int
        or raw["created_at_unix"] < validated_intent["created_at_unix"]
        or raw.get("secret_material_recorded") is not False
        or raw.get("secret_digest_recorded") is not False
    ):
        raise ProductionReleaseUpdateRuntimeError(
            "release_update_runtime_event_invalid"
        )
    return {**raw, "receipt": receipt}


def _rollback_phases_for_forward_prefix(
    forward_prefix: Sequence[str],
) -> tuple[str, ...]:
    """Return the only rollback sequence authorized by a durable prefix.

    ``target_health_validated`` is deliberately the write-ahead discriminator:
    the following preauthorization action may have become durable even when
    its runtime event append was interrupted.  Every rollback from that prefix
    must therefore reconcile/cancel the exact unit-input transaction before
    stopping the target or restoring host state.
    """

    if UNIT_INPUT_PREAUTHORIZATION_DISCRIMINATOR_PHASE in forward_prefix:
        return PREAUTHORIZED_ROLLBACK_PHASES
    return ROLLBACK_PHASES


def _expected_phase_sequence(phases: Sequence[str]) -> bool:
    if not phases:
        return True
    terminal = phases[-1] if phases[-1] in TERMINAL_PHASES else None
    if terminal == "completed":
        return tuple(phases) == FORWARD_PHASES
    if terminal == "rolled_back":
        if "rollback_intent" not in phases:
            return False
        rollback_index = phases.index("rollback_intent")
        forward_prefix = phases[:rollback_index]
        rollback_phases = _rollback_phases_for_forward_prefix(forward_prefix)
        return (
            tuple(forward_prefix) == FORWARD_PHASES[:rollback_index]
            and tuple(phases[rollback_index:]) == rollback_phases
            and FIRST_APPLICATION_MUTATION_PHASE in forward_prefix
            and COMMIT_PHASE not in forward_prefix
        )
    if terminal == "aborted":
        if "approval_expired_abort_intent" not in phases:
            return False
        abort_index = phases.index("approval_expired_abort_intent")
        return (
            tuple(phases[:abort_index]) == FORWARD_PHASES[:abort_index]
            and tuple(phases[abort_index:]) == ABORT_PHASES
            and FIRST_APPLICATION_MUTATION_PHASE not in phases[:abort_index]
            and COMMIT_PHASE not in phases[:abort_index]
        )
    if "approval_expired_abort_intent" in phases:
        abort_index = phases.index("approval_expired_abort_intent")
        abort_prefix = phases[abort_index:]
        return (
            tuple(phases[:abort_index]) == FORWARD_PHASES[:abort_index]
            and tuple(abort_prefix) == ABORT_PHASES[: len(abort_prefix)]
            and FIRST_APPLICATION_MUTATION_PHASE not in phases[:abort_index]
            and COMMIT_PHASE not in phases[:abort_index]
        )
    if "rollback_intent" in phases:
        rollback_index = phases.index("rollback_intent")
        forward_prefix = phases[:rollback_index]
        rollback_prefix = phases[rollback_index:]
        rollback_phases = _rollback_phases_for_forward_prefix(forward_prefix)
        return (
            tuple(forward_prefix) == FORWARD_PHASES[:rollback_index]
            and tuple(rollback_prefix)
            == rollback_phases[: len(rollback_prefix)]
            and FIRST_APPLICATION_MUTATION_PHASE in forward_prefix
            and COMMIT_PHASE not in forward_prefix
        )
    return tuple(phases) == FORWARD_PHASES[: len(phases)]


def load_state(
    *,
    intent: Mapping[str, Any],
    events: Sequence[Mapping[str, Any]],
) -> TransactionState:
    validated_intent = validate_intent(intent)
    validated: list[Mapping[str, Any]] = []
    prior = ZERO_SHA256
    prior_created_at = int(validated_intent["created_at_unix"])
    phases: list[str] = []
    receipts: dict[str, Mapping[str, Any]] = {}
    for expected_sequence, value in enumerate(events):
        event = validate_event(value, intent=validated_intent)
        phase = str(event["phase"])
        receipt = event["receipt"]
        try:
            if phase in ACTION_PHASES:
                _validate_bound_action_receipt(
                    receipt,
                    intent=validated_intent,
                    phase=phase,
                    receipts=receipts,
                )
            elif receipt != _internal_receipt(
                phase=phase,
                intent=validated_intent,
            ):
                raise ProductionReleaseUpdateRuntimeError(
                    "release_update_runtime_action_receipt_invalid"
                )
        except ProductionReleaseUpdateRuntimeError as exc:
            raise ProductionReleaseUpdateRuntimeError(
                "release_update_runtime_journal_invalid"
            ) from exc
        if (
            event["sequence"] != expected_sequence
            or event["prior_event_sha256"] != prior
            or phase in receipts
            or event["created_at_unix"] < prior_created_at
        ):
            raise ProductionReleaseUpdateRuntimeError(
                "release_update_runtime_journal_invalid"
            )
        prior = str(event["event_sha256"])
        prior_created_at = int(event["created_at_unix"])
        phases.append(phase)
        receipts[phase] = receipt
        validated.append(event)
    if not _expected_phase_sequence(phases):
        raise ProductionReleaseUpdateRuntimeError(
            "release_update_runtime_journal_invalid"
        )

    terminal = phases[-1] if phases and phases[-1] in TERMINAL_PHASES else None
    rollback_started = "rollback_intent" in phases
    abort_started = "approval_expired_abort_intent" in phases
    next_forward: str | None = None
    next_rollback: str | None = None
    next_abort: str | None = None
    if terminal is None:
        if abort_started:
            abort_count = len(phases) - phases.index("approval_expired_abort_intent")
            next_abort = ABORT_PHASES[abort_count]
        elif rollback_started:
            rollback_index = phases.index("rollback_intent")
            rollback_count = len(phases) - rollback_index
            rollback_phases = _rollback_phases_for_forward_prefix(
                phases[:rollback_index]
            )
            next_rollback = rollback_phases[rollback_count]
        else:
            next_forward = FORWARD_PHASES[len(phases)]
    return TransactionState(
        intent=validated_intent,
        events=tuple(validated),
        receipts=dict(receipts),
        next_forward_phase=next_forward,
        next_rollback_phase=next_rollback,
        next_abort_phase=next_abort,
        commit_intent_persisted=COMMIT_PHASE in phases,
        unit_input_preauthorization_persisted=(
            UNIT_INPUT_PREAUTHORIZATION_PHASE in phases
        ),
        application_mutation_started=FIRST_APPLICATION_MUTATION_PHASE in phases,
        terminal_phase=terminal,
    )


def _internal_receipt(
    *,
    phase: str,
    intent: Mapping[str, Any],
) -> Mapping[str, Any]:
    base: dict[str, Any] = {
        "schema": f"muncho-production-release-update-{phase}.v1",
        "phase": phase,
        "intent_sha256": intent["intent_sha256"],
        "predecessor_revision": intent["predecessor_revision"],
        "release_revision": intent["release_revision"],
        "secret_material_recorded": False,
        "secret_digest_recorded": False,
    }
    if phase == COMMIT_PHASE:
        base.update({
            "rollback_allowed": False,
            "forward_only": True,
        })
    elif phase == "rollback_intent":
        base.update({
            "rollback_allowed": True,
            "forward_only": False,
        })
    elif phase == FIRST_APPLICATION_MUTATION_PHASE:
        base.update({
            "rollback_required_on_recovery": True,
            "application_mutation_authorized": True,
        })
    elif phase == "approval_expired_abort_intent":
        base.update({
            "approval_expired": True,
            "application_mutation_forbidden": True,
        })
    elif phase in TERMINAL_PHASES:
        base["terminal"] = True
    return base


def _append(
    *,
    state: TransactionState,
    journal: ReleaseUpdateJournal,
    phase: str,
    receipt: Mapping[str, Any],
    now_unix: int,
) -> TransactionState:
    prior = ZERO_SHA256 if not state.events else str(state.events[-1]["event_sha256"])
    event = build_event(
        intent=state.intent,
        sequence=len(state.events),
        phase=phase,
        prior_event_sha256=prior,
        receipt=receipt,
        created_at_unix=now_unix,
    )
    persisted = journal.append(event)
    if persisted != event:
        raise ProductionReleaseUpdateRuntimeError(
            "release_update_runtime_journal_write_invalid"
        )
    reloaded = load_state(intent=state.intent, events=journal.load())
    if (
        len(reloaded.events) != len(state.events) + 1
        or reloaded.events[:-1] != state.events
        or reloaded.events[-1] != event
    ):
        raise ProductionReleaseUpdateRuntimeError(
            "release_update_runtime_journal_write_invalid"
        )
    return reloaded


def _perform_phase(
    *,
    state: TransactionState,
    phase: str,
    actions: ReleaseUpdateActions,
    journal: ReleaseUpdateJournal,
    now_unix: int,
) -> TransactionState:
    if phase in {
        FIRST_APPLICATION_MUTATION_PHASE,
        COMMIT_PHASE,
        "completed",
        "rollback_intent",
        "rolled_back",
        "approval_expired_abort_intent",
        "aborted",
    }:
        receipt = _internal_receipt(phase=phase, intent=state.intent)
    elif phase in ACTION_PHASES:
        receipt = _validate_bound_action_receipt(
            actions.perform(
                phase,
                intent=state.intent,
                receipts=state.receipts,
            ),
            intent=state.intent,
            phase=phase,
            receipts=state.receipts,
        )
    else:
        raise ProductionReleaseUpdateRuntimeError(
            "release_update_runtime_phase_invalid"
        )
    return _append(
        state=state,
        journal=journal,
        phase=phase,
        receipt=receipt,
        now_unix=now_unix,
    )


def _rollback(
    *,
    state: TransactionState,
    actions: ReleaseUpdateActions,
    journal: ReleaseUpdateJournal,
) -> TransactionState:
    if state.commit_intent_persisted:
        raise ProductionReleaseUpdateRuntimeError(
            "release_update_runtime_rollback_forbidden"
        )
    if not state.application_mutation_started:
        raise ProductionReleaseUpdateRuntimeError(
            "release_update_runtime_rollback_unnecessary"
        )
    while state.terminal_phase is None:
        phase = state.next_rollback_phase or "rollback_intent"
        state = _perform_phase(
            state=state,
            phase=phase,
            actions=actions,
            journal=journal,
            now_unix=_recovery_now(state),
        )
    return state


def _abort_expired(
    *,
    state: TransactionState,
    actions: ReleaseUpdateActions,
    journal: ReleaseUpdateJournal,
) -> TransactionState:
    if state.commit_intent_persisted or state.application_mutation_started:
        raise ProductionReleaseUpdateRuntimeError(
            "release_update_runtime_abort_forbidden"
        )
    while state.terminal_phase is None:
        phase = state.next_abort_phase or "approval_expired_abort_intent"
        state = _perform_phase(
            state=state,
            phase=phase,
            actions=actions,
            journal=journal,
            now_unix=_recovery_now(state),
        )
    return state


def _clock_lower_bound(state: TransactionState | None) -> int:
    return (
        1
        if state is None
        else (
            int(state.intent["created_at_unix"])
            if not state.events
            else int(state.events[-1]["created_at_unix"])
        )
    )


def _wall_clock_now() -> int:
    try:
        return int(time.time())
    except (OverflowError, TypeError, ValueError) as exc:
        raise ProductionReleaseUpdateRuntimeError(
            "release_update_runtime_clock_invalid"
        ) from exc


def _observed_now(state: TransactionState | None = None) -> int:
    observed = _wall_clock_now()
    lower_bound = _clock_lower_bound(state)
    if observed < lower_bound:
        raise ProductionReleaseUpdateRuntimeError(
            "release_update_runtime_clock_invalid"
        )
    return observed


def _recovery_now(state: TransactionState) -> int:
    """Return a monotonic journal time after the safe direction is fixed.

    Rollback, abort completion, and post-commit completion have no alternative
    safe direction.  A backward or temporarily unreadable wall clock must not
    strand a fenced or committed host, so recovery advances on the durable
    journal's logical clock instead.
    """

    lower_bound = _clock_lower_bound(state)
    try:
        observed = _wall_clock_now()
    except ProductionReleaseUpdateRuntimeError:
        return lower_bound
    return max(observed, lower_bound)


def _approval_expired(
    intent: Mapping[str, Any],
    *,
    now_unix: int,
) -> bool:
    if type(now_unix) is not int or now_unix < int(intent["approval_issued_at_unix"]):
        raise ProductionReleaseUpdateRuntimeError(
            "release_update_runtime_clock_invalid"
        )
    return now_unix >= int(intent["approval_expires_at_unix"])


def _verify_terminal_state(
    state: TransactionState,
    *,
    actions: ReleaseUpdateActions,
) -> None:
    if state.terminal_phase not in TERMINAL_PHASES:
        raise ProductionReleaseUpdateRuntimeError(
            "release_update_runtime_terminal_state_invalid"
        )
    phase = f"{state.terminal_phase}_revalidated"
    try:
        _validate_bound_action_receipt(
            actions.perform(
                phase,
                intent=state.intent,
                receipts=state.receipts,
            ),
            intent=state.intent,
            phase=phase,
            receipts=state.receipts,
        )
    except Exception as exc:
        raise ProductionReleaseUpdateRuntimeError(
            "release_update_runtime_terminal_revalidation_failed"
        ) from exc


def _execute_update_locked(
    *,
    intent: Mapping[str, Any],
    actions: ReleaseUpdateActions,
    journal: ReleaseUpdateJournal,
) -> TransactionState:
    """Execute or exactly resume one transaction.

    Pre-commit action failure triggers exact rollback once application
    mutation has begun.  Post-commit failure is retained for forward-only
    recovery and is never converted into rollback.
    """

    state = load_state(intent=intent, events=journal.load())
    if state.terminal_phase is not None:
        return state
    if state.next_rollback_phase is not None:
        try:
            return _rollback(
                state=state,
                actions=actions,
                journal=journal,
            )
        except Exception as exc:
            raise ProductionReleaseUpdateRuntimeError(
                "release_update_runtime_rollback_pending"
            ) from exc
    if state.next_abort_phase is not None:
        try:
            return _abort_expired(
                state=state,
                actions=actions,
                journal=journal,
            )
        except Exception as exc:
            raise ProductionReleaseUpdateRuntimeError(
                "release_update_runtime_abort_pending"
            ) from exc
    if (
        state.application_mutation_started
        and not state.commit_intent_persisted
    ):
        # A fresh public invocation cannot prove whether a prior process died
        # between a host action and its event append.  Once pre-commit
        # application mutation is already durable, rollback is therefore the
        # only safe restart direction regardless of whether the caller chose
        # ``execute_update`` or ``recover_update``.
        try:
            return _rollback(
                state=state,
                actions=actions,
                journal=journal,
            )
        except Exception as exc:
            raise ProductionReleaseUpdateRuntimeError(
                "release_update_runtime_rollback_pending"
            ) from exc

    while state.terminal_phase is None:
        if (
            state.commit_intent_persisted
            or state.unit_input_preauthorization_persisted
        ):
            # The durable unit-input preauthorization is the final freshness
            # gate.  From this point through finalization, journal time is
            # purely logical: neither approval expiry nor an unavailable,
            # backward, or newly advanced wall clock may reopen the decision.
            observed_now = _clock_lower_bound(state)
        else:
            try:
                observed_now = _observed_now(state)
            except ProductionReleaseUpdateRuntimeError as exc:
                if not state.application_mutation_started:
                    raise
                try:
                    return _rollback(
                        state=state,
                        actions=actions,
                        journal=journal,
                    )
                except Exception as rollback_exc:
                    raise ProductionReleaseUpdateRuntimeError(
                        "release_update_runtime_rollback_pending"
                    ) from ExceptionGroup(
                        "release update clock and rollback failed",
                        [exc, rollback_exc],
                    )
        if (
            not state.commit_intent_persisted
            and not state.unit_input_preauthorization_persisted
            and _approval_expired(
                state.intent,
                now_unix=observed_now,
            )
        ):
            if state.application_mutation_started:
                try:
                    return _rollback(
                        state=state,
                        actions=actions,
                        journal=journal,
                    )
                except Exception as exc:
                    raise ProductionReleaseUpdateRuntimeError(
                        "release_update_runtime_rollback_pending"
                    ) from exc
            try:
                return _abort_expired(
                    state=state,
                    actions=actions,
                    journal=journal,
                )
            except Exception as exc:
                raise ProductionReleaseUpdateRuntimeError(
                    "release_update_runtime_abort_pending"
                ) from exc
        phase = state.next_forward_phase
        if phase is None:
            raise ProductionReleaseUpdateRuntimeError(
                "release_update_runtime_journal_invalid"
            )
        try:
            if phase == FIRST_APPLICATION_MUTATION_PHASE:
                _validate_bound_action_receipt(
                    actions.perform(
                        "pre_mutation_cas_revalidated",
                        intent=state.intent,
                        receipts=state.receipts,
                    ),
                    intent=state.intent,
                    phase="pre_mutation_cas_revalidated",
                    receipts=state.receipts,
                )
            state = _perform_phase(
                state=state,
                phase=phase,
                actions=actions,
                journal=journal,
                now_unix=observed_now,
            )
        except Exception as exc:
            current = load_state(intent=intent, events=journal.load())
            if current.commit_intent_persisted:
                raise ProductionReleaseUpdateRuntimeError(
                    "release_update_runtime_forward_completion_pending"
                ) from exc
            if current.application_mutation_started:
                try:
                    return _rollback(
                        state=current,
                        actions=actions,
                        journal=journal,
                    )
                except Exception as rollback_exc:
                    raise ProductionReleaseUpdateRuntimeError(
                        "release_update_runtime_rollback_pending"
                    ) from ExceptionGroup(
                        "release update and rollback failed",
                        [exc, rollback_exc],
                    )
            raise ProductionReleaseUpdateRuntimeError(
                "release_update_runtime_precommit_failed"
            ) from exc
    return state


def _recover_update_locked(
    *,
    intent: Mapping[str, Any],
    actions: ReleaseUpdateActions,
    journal: ReleaseUpdateJournal,
) -> TransactionState:
    """Recover a boot-interrupted transaction.

    A pre-commit transaction that already fenced consumers rolls back.
    A transaction that has not mutated the application may safely resume.
    Any transaction with commit intent is completed forward.
    """

    state = load_state(intent=intent, events=journal.load())
    if state.terminal_phase is not None:
        return state
    if state.next_abort_phase is not None:
        try:
            return _abort_expired(
                state=state,
                actions=actions,
                journal=journal,
            )
        except Exception as exc:
            raise ProductionReleaseUpdateRuntimeError(
                "release_update_runtime_abort_pending"
            ) from exc
    if (
        state.application_mutation_started
        and not state.commit_intent_persisted
    ):
        try:
            return _rollback(
                state=state,
                actions=actions,
                journal=journal,
            )
        except Exception as exc:
            raise ProductionReleaseUpdateRuntimeError(
                "release_update_runtime_rollback_pending"
            ) from exc
    if not state.commit_intent_persisted and _approval_expired(
        state.intent,
        now_unix=_observed_now(state),
    ):
        try:
            return _abort_expired(
                state=state,
                actions=actions,
                journal=journal,
            )
        except Exception as exc:
            raise ProductionReleaseUpdateRuntimeError(
                "release_update_runtime_abort_pending"
            ) from exc
    return _execute_update_locked(
        intent=intent,
        actions=actions,
        journal=journal,
    )


def _run_with_lock(
    *,
    authority_record: Mapping[str, Any],
    actions: ReleaseUpdateActions,
    journal: ReleaseUpdateJournal,
    recover: bool,
    require_root: bool,
    lock_factory: Any | None,
) -> TransactionState:
    record = validate_authority_record(authority_record)
    try:
        journal_record = validate_authority_record(journal.authority_record)
    except Exception as exc:
        raise ProductionReleaseUpdateRuntimeError(
            "release_update_runtime_journal_authority_invalid"
        ) from exc
    if journal_record != record:
        raise ProductionReleaseUpdateRuntimeError(
            "release_update_runtime_journal_authority_invalid"
        )
    if (
        type(require_root) is not bool
        or (require_root and lock_factory is not None)
        or (lock_factory is not None and not callable(lock_factory))
    ):
        raise ProductionReleaseUpdateRuntimeError("release_update_runtime_lock_invalid")
    try:
        context = authority_lock.authority_activation_lock(
            require_root=require_root,
            lock_factory=lock_factory,
        )
    except Exception as exc:
        raise ProductionReleaseUpdateRuntimeError(
            "release_update_runtime_lock_unavailable"
        ) from exc
    try:
        with context:
            runner = _recover_update_locked if recover else _execute_update_locked
            state = runner(
                intent=record["intent"],
                actions=actions,
                journal=journal,
            )
            # Every successful public execution/recovery return proves current
            # terminal host state exactly once.  This includes a transaction
            # that reached its terminal event during this invocation, not only
            # a later exact replay.  Recovery coordinators may therefore retire
            # the active marker immediately after this boundary returns while
            # retaining the outer activation lock.
            _verify_terminal_state(state, actions=actions)
            return state
    except authority_lock.AuthorityActivationLockError as exc:
        raise ProductionReleaseUpdateRuntimeError(
            "release_update_runtime_lock_unavailable"
        ) from exc


def execute_update(
    *,
    authority_record: Mapping[str, Any],
    actions: ReleaseUpdateActions,
    journal: ReleaseUpdateJournal,
) -> TransactionState:
    """Run one production transaction under the fixed root-owned lock.

    The public boundary deliberately exposes no root, clock, or lock
    override.  Test-only seams live in underscored helpers below.
    """

    return _run_with_lock(
        authority_record=authority_record,
        actions=actions,
        journal=journal,
        recover=False,
        require_root=True,
        lock_factory=None,
    )


def recover_update(
    *,
    authority_record: Mapping[str, Any],
    actions: ReleaseUpdateActions,
    journal: ReleaseUpdateJournal,
) -> TransactionState:
    """Recover production under the same fixed root-owned lock."""

    return _run_with_lock(
        authority_record=authority_record,
        actions=actions,
        journal=journal,
        recover=True,
        require_root=True,
        lock_factory=None,
    )


def _execute_update_for_test(
    *,
    authority_record: Mapping[str, Any],
    actions: ReleaseUpdateActions,
    journal: ReleaseUpdateJournal,
    lock_factory: Any | None = None,
) -> TransactionState:
    """Exercise the transaction loop without production root authority."""

    return _run_with_lock(
        authority_record=authority_record,
        actions=actions,
        journal=journal,
        recover=False,
        require_root=False,
        lock_factory=lock_factory,
    )


def _recover_update_for_test(
    *,
    authority_record: Mapping[str, Any],
    actions: ReleaseUpdateActions,
    journal: ReleaseUpdateJournal,
    lock_factory: Any | None = None,
) -> TransactionState:
    """Exercise recovery without production root authority."""

    return _run_with_lock(
        authority_record=authority_record,
        actions=actions,
        journal=journal,
        recover=True,
        require_root=False,
        lock_factory=lock_factory,
    )
