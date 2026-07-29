from __future__ import annotations

from copy import deepcopy
from contextlib import nullcontext
from functools import lru_cache
from typing import Any, Mapping, Sequence
from unittest.mock import patch

import pytest

from scripts.canary import production_release_update_runtime as runtime
from tests.scripts.canary.test_production_release_update_contract import (
    _documents,
)


NOW = 1_900_000_000


@lru_cache(maxsize=1)
def _authority_record() -> Mapping[str, Any]:
    _private, trusted, plan, _approval, publication = _documents()
    return runtime.build_authority_record(
        publication=publication,
        trusted_predecessor=trusted,
        expected_predecessor_trust_sha256=str(trusted["trust_sha256"]),
        predecessor_current_receipt_sha256=str(
            plan["predecessor_activation_receipt_sha256"]
        ),
    )


def _intent() -> Mapping[str, Any]:
    return _authority_record()["intent"]


def test_signed_publication_deterministically_defines_transaction_identity() -> None:
    _private, trusted, plan, approval, publication = _documents()
    values = {
        "publication": publication,
        "trusted_predecessor": trusted,
        "expected_predecessor_trust_sha256": str(trusted["trust_sha256"]),
        "predecessor_current_receipt_sha256": str(
            plan["predecessor_activation_receipt_sha256"]
        ),
    }

    first_intent = runtime.build_intent(**values)
    replayed_intent = runtime.build_intent(**values)
    first_record = runtime.build_authority_record(**values)
    replayed_record = runtime.build_authority_record(**values)

    assert first_intent == replayed_intent
    assert first_record == replayed_record
    assert first_record["intent"] == first_intent
    assert first_intent["schema"] == "muncho-production-release-update-intent.v3"
    assert first_intent["transaction_nonce_sha256"] == approval["nonce_sha256"]
    assert first_intent["created_at_unix"] == approval["issued_at_unix"]
    assert first_intent["created_at_unix"] == first_intent["approval_issued_at_unix"]


@pytest.mark.parametrize(
    "builder",
    (runtime.build_intent, runtime.build_authority_record),
)
@pytest.mark.parametrize(
    ("override_name", "override_value"),
    (
        ("transaction_nonce_sha256", "f" * 64),
        ("created_at_unix", NOW),
    ),
)
def test_transaction_identity_rejects_caller_overrides(
    builder: Any,
    override_name: str,
    override_value: object,
) -> None:
    _private, trusted, plan, _approval, publication = _documents()
    values = {
        "publication": publication,
        "trusted_predecessor": trusted,
        "expected_predecessor_trust_sha256": str(trusted["trust_sha256"]),
        "predecessor_current_receipt_sha256": str(
            plan["predecessor_activation_receipt_sha256"]
        ),
        override_name: override_value,
    }

    with pytest.raises(TypeError):
        builder(**values)


def _digest(label: str) -> str:
    return runtime._sha(runtime._canonical({"evidence": label}))


def _phase_evidence(
    phase: str,
    *,
    intent: Mapping[str, Any],
    receipts: Mapping[str, Mapping[str, Any]],
) -> Mapping[str, Any]:
    predecessor = intent["predecessor_revision"]
    release = intent["release_revision"]
    inventory = receipts.get(
        "pre_fence_cas_validated",
        {
            "consumer_inventory_sha256": intent["release_consumer_set_sha256"],
            "expected_consumer_unit_count": (runtime.EXPECTED_CONSUMER_UNIT_COUNT),
            "expected_service_unit_count": runtime.EXPECTED_SERVICE_UNIT_COUNT,
            "expected_long_running_service_unit_count": (
                runtime.EXPECTED_LONG_RUNNING_SERVICE_UNIT_COUNT
            ),
            "expected_oneshot_service_unit_count": (
                runtime.EXPECTED_ONESHOT_SERVICE_UNIT_COUNT
            ),
            "expected_startup_oneshot_service_unit_count": (
                runtime.EXPECTED_STARTUP_ONESHOT_SERVICE_UNIT_COUNT
            ),
            "expected_triggered_oneshot_service_unit_count": (
                runtime.EXPECTED_TRIGGERED_ONESHOT_SERVICE_UNIT_COUNT
            ),
            "expected_trigger_unit_count": runtime.EXPECTED_TRIGGER_UNIT_COUNT,
        },
    )
    inventory_sha256 = inventory["consumer_inventory_sha256"]
    long_running_count = inventory["expected_long_running_service_unit_count"]
    startup_oneshot_count = inventory["expected_startup_oneshot_service_unit_count"]
    trigger_count = inventory["expected_trigger_unit_count"]

    if phase == "candidate_validated":
        return {
            "release_root": intent["release_root"],
            "candidate_tree_sha256": intent["whole_tree_manifest_sha256"],
            "candidate_seal_receipt_sha256": intent["candidate_seal_receipt_sha256"],
            "builder_terminal_receipt_sha256": intent[
                "builder_terminal_receipt_sha256"
            ],
            "verified_regular_file_count": 8178,
            "release_root_owned": True,
            "release_tree_read_only": True,
        }
    if phase in {"voice_guard_initial", "voice_guard_final"}:
        initial = receipts.get("voice_guard_initial", {})
        return {
            "voice_guard_observation_sha256": _digest(f"{phase}-observation"),
            "protected_service_set_sha256": initial.get(
                "protected_service_set_sha256",
                _digest("protected-voice-services"),
            ),
            "observed_active_revision": predecessor,
            "healthy_voice_target_count": initial.get(
                "healthy_voice_target_count",
                2,
            ),
            "all_required_voice_targets_healthy": True,
        }
    if phase == "prestate_archived":
        return {
            "prestate_archive_sha256": _digest("prestate-archive"),
            "host_inventory_sha256": intent["host_inventory_sha256"],
            "activation_plan_sha256": intent["activation_plan_sha256"],
            "rollback_plan_sha256": intent["rollback_plan_sha256"],
            "host_artifact_manifest_sha256": intent["host_artifact_manifest_sha256"],
            "archived_target_set_sha256": _digest("archived-target-set"),
            "archived_target_count": 89,
            "archive_fsynced": True,
        }
    if phase == "unit_inputs_prepared":
        return {
            "prepared_unit_input_publication_sha256": intent[
                "successor_unit_input_publication_sha256"
            ],
            "prepared_unit_input_set_sha256": _digest("prepared-unit-input-set"),
            "prepared_unit_input_count": 3,
            "active_inputs_unchanged": True,
        }
    if phase == "recovery_gate_installed":
        return {
            "recovery_gate_artifact_sha256": _digest("recovery-gate-artifact"),
            "recovery_gate_unit_sha256": _digest("recovery-gate-unit"),
            "host_artifact_manifest_sha256": intent["host_artifact_manifest_sha256"],
            "recovery_gate_enabled": True,
            "recovery_gate_verified": True,
        }
    if phase in {
        "pre_fence_cas_validated",
        "pre_mutation_cas_revalidated",
    }:
        return {
            "host_observation_receipt_sha256": _digest("pre-fence-host-observation"),
            "observed_predecessor_activation_receipt_sha256": intent[
                "predecessor_current_receipt_sha256"
            ],
            "observed_pointer_revision": predecessor,
            "observed_unit_inputs_revision": predecessor,
            "consumer_inventory_sha256": inventory_sha256,
            "expected_consumer_unit_count": (runtime.EXPECTED_CONSUMER_UNIT_COUNT),
            "expected_service_unit_count": runtime.EXPECTED_SERVICE_UNIT_COUNT,
            "expected_long_running_service_unit_count": (
                runtime.EXPECTED_LONG_RUNNING_SERVICE_UNIT_COUNT
            ),
            "expected_oneshot_service_unit_count": (
                runtime.EXPECTED_ONESHOT_SERVICE_UNIT_COUNT
            ),
            "expected_startup_oneshot_service_unit_count": (
                runtime.EXPECTED_STARTUP_ONESHOT_SERVICE_UNIT_COUNT
            ),
            "expected_triggered_oneshot_service_unit_count": (
                runtime.EXPECTED_TRIGGERED_ONESHOT_SERVICE_UNIT_COUNT
            ),
            "expected_trigger_unit_count": runtime.EXPECTED_TRIGGER_UNIT_COUNT,
            "compare_and_swap_matched": True,
        }
    if phase == "consumers_fenced":
        return {
            "host_observation_receipt_sha256": _digest(
                "consumers-fenced-host-observation"
            ),
            "consumer_inventory_sha256": inventory_sha256,
            "fenced_unit_count": runtime.EXPECTED_CONSUMER_UNIT_COUNT,
            "remaining_active_unit_count": 0,
            "remaining_consumer_process_count": 0,
            "fence_verified": True,
        }
    if phase == "release_consumers_zeroed":
        return {
            "host_observation_receipt_sha256": _digest(
                "release-consumers-zeroed-host-observation"
            ),
            "consumer_inventory_sha256": inventory_sha256,
            "observed_consumer_process_count": 0,
            "observed_unknown_process_count": 0,
            "observed_mutable_pointer_process_count": 0,
            "need_daemon_reload_unit_count": 0,
            "all_release_consumers_zeroed": True,
        }
    if phase == "host_payloads_applied":
        archived = receipts["prestate_archived"]
        return {
            "host_payload_manifest_sha256": intent["host_artifact_manifest_sha256"],
            "applied_target_set_sha256": archived["archived_target_set_sha256"],
            "applied_target_count": archived["archived_target_count"],
            "applied_revision": release,
            "payloads_fsynced": True,
        }
    if phase == "target_started_disabled":
        return {
            "host_observation_receipt_sha256": _digest(
                "target-started-disabled-host-observation"
            ),
            "consumer_inventory_sha256": inventory_sha256,
            "started_long_running_service_unit_count": long_running_count,
            "started_startup_oneshot_service_unit_count": (startup_oneshot_count),
            "started_triggered_oneshot_service_unit_count": 0,
            "enabled_trigger_unit_count": 0,
            "observed_target_revision": release,
            "target_process_set_sha256": _digest("target-process-set"),
            "target_runtime_classes_ready": True,
            "only_declared_cutover_service_classes_started": True,
            "external_ingress_disabled": True,
        }
    if phase == "target_health_validated":
        return {
            "health_observation_sha256": _digest("target-health"),
            "target_process_set_sha256": receipts["target_started_disabled"][
                "target_process_set_sha256"
            ],
            "observed_target_revision": release,
            "validated_endpoint_count": 4,
            "validated_connector_count": 2,
            "external_ingress_disabled": True,
            "all_required_health_checks_passed": True,
        }
    if phase == "unit_inputs_finalized":
        prepared = receipts["unit_inputs_prepared"]
        return {
            "unit_input_publication_sha256": intent[
                "successor_unit_input_publication_sha256"
            ],
            "unit_input_activation_receipt_sha256": _digest("unit-input-activation"),
            "finalized_unit_input_set_sha256": prepared[
                "prepared_unit_input_set_sha256"
            ],
            "finalized_unit_input_count": prepared["prepared_unit_input_count"],
            "observed_unit_inputs_revision": release,
            "authoritative_inputs_active": True,
        }
    if phase == "release_pointer_rotated":
        return {
            "previous_pointer_revision": predecessor,
            "current_pointer_revision": release,
            "unit_input_activation_receipt_sha256": receipts["unit_inputs_finalized"][
                "unit_input_activation_receipt_sha256"
            ],
            "pointer_target": intent["release_root"],
            "pointer_target_sha256": runtime._sha(
                str(intent["release_root"]).encode("utf-8")
            ),
            "pointer_parent_fsynced": True,
            "compare_and_swap_succeeded": True,
        }
    if phase == "target_consumers_enabled":
        return {
            "host_observation_receipt_sha256": _digest(
                "target-consumers-enabled-host-observation"
            ),
            "consumer_inventory_sha256": inventory_sha256,
            "active_long_running_service_unit_count": long_running_count,
            "completed_startup_oneshot_service_unit_count": (startup_oneshot_count),
            "direct_started_triggered_oneshot_service_unit_count": 0,
            "enabled_trigger_unit_count": trigger_count,
            "observed_target_revision": release,
            "unknown_consumer_process_count": 0,
            "all_expected_consumers_enabled": True,
        }
    if phase in {"terminal_validated", "completed_revalidated"}:
        evidence = {
            "host_observation_receipt_sha256": _digest(f"{phase}-host-observation"),
            "consumer_inventory_sha256": inventory_sha256,
            "observed_pointer_revision": release,
            "observed_unit_inputs_revision": release,
            "active_long_running_service_unit_count": long_running_count,
            "completed_startup_oneshot_service_unit_count": (startup_oneshot_count),
            "direct_started_triggered_oneshot_service_unit_count": 0,
            "enabled_trigger_unit_count": trigger_count,
            "unknown_consumer_process_count": 0,
            "need_daemon_reload_unit_count": 0,
            "terminal_health_observation_sha256": _digest(f"{phase}-health"),
            "all_required_health_checks_passed": True,
        }
        if phase == "completed_revalidated":
            evidence["terminal_receipt_sha256"] = runtime._sha(
                runtime._canonical(receipts["terminal_validated"])
            )
        return evidence
    if phase == "target_stopped":
        return {
            "host_observation_receipt_sha256": _digest(
                "target-stopped-host-observation"
            ),
            "consumer_inventory_sha256": inventory_sha256,
            "target_revision": release,
            "stopped_target_process_count": long_running_count,
            "remaining_target_process_count": 0,
            "target_services_inactive": True,
        }
    if phase == "host_prestate_restored":
        archived = receipts["prestate_archived"]
        return {
            "prestate_archive_sha256": archived["prestate_archive_sha256"],
            "restored_target_set_sha256": archived["archived_target_set_sha256"],
            "restored_target_count": archived["archived_target_count"],
            "restored_revision": predecessor,
            "restore_fsynced": True,
        }
    if phase == "predecessor_consumers_restored":
        return {
            "host_observation_receipt_sha256": _digest(
                "predecessor-consumers-restored-host-observation"
            ),
            "consumer_inventory_sha256": inventory_sha256,
            "active_long_running_service_unit_count": long_running_count,
            "completed_startup_oneshot_service_unit_count": (startup_oneshot_count),
            "direct_started_triggered_oneshot_service_unit_count": 0,
            "enabled_trigger_unit_count": trigger_count,
            "observed_active_revision": predecessor,
            "remaining_target_process_count": 0,
            "predecessor_health_observation_sha256": _digest("predecessor-health"),
            "predecessor_healthy": True,
        }
    if phase in {"rollback_validated", "rolled_back_revalidated"}:
        archived = receipts["prestate_archived"]
        evidence = {
            "host_observation_receipt_sha256": _digest(f"{phase}-host-observation"),
            "consumer_inventory_sha256": inventory_sha256,
            "observed_pointer_revision": predecessor,
            "observed_unit_inputs_revision": predecessor,
            "active_long_running_service_unit_count": long_running_count,
            "completed_startup_oneshot_service_unit_count": (startup_oneshot_count),
            "direct_started_triggered_oneshot_service_unit_count": 0,
            "enabled_trigger_unit_count": trigger_count,
            "remaining_target_process_count": 0,
            "restored_prestate_archive_sha256": archived["prestate_archive_sha256"],
            "unknown_consumer_process_count": 0,
            "need_daemon_reload_unit_count": 0,
            "rollback_health_observation_sha256": _digest(f"{phase}-health"),
            "all_required_health_checks_passed": True,
        }
        if phase == "rolled_back_revalidated":
            evidence["rollback_receipt_sha256"] = runtime._sha(
                runtime._canonical(receipts["rollback_validated"])
            )
        return evidence
    if phase == "preapplication_cleanup":
        archived = receipts.get("prestate_archived")
        return {
            "cleanup_observation_sha256": _digest("preapplication-cleanup"),
            "retained_prestate_archive_sha256": (
                runtime.ZERO_SHA256
                if archived is None
                else archived["prestate_archive_sha256"]
            ),
            "prepared_unit_inputs_discarded": True,
            "recovery_gate_removed": True,
            "application_state_unchanged": True,
        }
    if phase == "aborted_revalidated":
        return {
            "cleanup_receipt_sha256": runtime._sha(
                runtime._canonical(receipts["preapplication_cleanup"])
            ),
            "observed_pointer_revision": predecessor,
            "observed_unit_inputs_revision": predecessor,
            "recovery_gate_absent": True,
            "prepared_inputs_inactive": True,
            "no_application_mutation": True,
        }
    raise AssertionError(f"missing test evidence for {phase}")


def _valid_action_receipt(
    phase: str,
    *,
    intent: Mapping[str, Any],
    receipts: Mapping[str, Mapping[str, Any]],
) -> Mapping[str, Any]:
    return {
        "schema": (f"muncho-production-release-update-{phase}-receipt.v1"),
        "phase": phase,
        "intent_sha256": intent["intent_sha256"],
        "publication_sha256": intent["publication_sha256"],
        "plan_sha256": intent["plan_sha256"],
        "approval_sha256": intent["approval_sha256"],
        "predecessor_revision": intent["predecessor_revision"],
        "release_revision": intent["release_revision"],
        "idempotency_key": runtime.action_idempotency_key(intent, phase),
        "prior_receipts_sha256": runtime._sha(runtime._canonical(receipts)),
        **_phase_evidence(
            phase,
            intent=intent,
            receipts=receipts,
        ),
        "secret_material_recorded": False,
        "secret_digest_recorded": False,
    }


class MemoryJournal:
    def __init__(
        self,
        events: Sequence[Mapping[str, Any]] = (),
        *,
        fail_append_phase: str | None = None,
        authority_record: Mapping[str, Any] | None = None,
    ) -> None:
        self.events = [deepcopy(dict(item)) for item in events]
        self.fail_append_phase = fail_append_phase
        self.failed = False
        self._authority_record = deepcopy(dict(authority_record or _authority_record()))

    @property
    def authority_record(self) -> Mapping[str, Any]:
        return deepcopy(self._authority_record)

    def load(self) -> Sequence[Mapping[str, Any]]:
        return deepcopy(self.events)

    def append(self, event: Mapping[str, Any]) -> Mapping[str, Any]:
        if self.fail_append_phase == event["phase"] and not self.failed:
            self.failed = True
            raise OSError("injected durable write failure")
        self.events.append(deepcopy(dict(event)))
        return deepcopy(dict(event))


class LostAppendJournal(MemoryJournal):
    def append(self, event: Mapping[str, Any]) -> Mapping[str, Any]:
        return deepcopy(dict(event))


class PersistThenRaiseJournal(MemoryJournal):
    def __init__(self, phase: str) -> None:
        super().__init__()
        self.phase = phase

    def append(self, event: Mapping[str, Any]) -> Mapping[str, Any]:
        self.events.append(deepcopy(dict(event)))
        if event["phase"] == self.phase and not self.failed:
            self.failed = True
            raise OSError("persisted then interrupted")
        return deepcopy(dict(event))


def _execute(
    *,
    actions: "FakeActions",
    journal: MemoryJournal,
    now_unix: int = NOW,
    authority_record: Mapping[str, Any] | None = None,
) -> runtime.TransactionState:
    with patch.object(runtime.time, "time", return_value=now_unix):
        return runtime._execute_update_for_test(
            authority_record=authority_record or _authority_record(),
            actions=actions,
            journal=journal,
            lock_factory=nullcontext,
        )


def _recover(
    *,
    actions: "FakeActions",
    journal: MemoryJournal,
    now_unix: int = NOW,
    authority_record: Mapping[str, Any] | None = None,
) -> runtime.TransactionState:
    with patch.object(runtime.time, "time", return_value=now_unix):
        return runtime._recover_update_for_test(
            authority_record=authority_record or _authority_record(),
            actions=actions,
            journal=journal,
            lock_factory=nullcontext,
        )


class FakeActions:
    def __init__(
        self,
        *,
        fail_once: str | None = None,
        fail_always: str | None = None,
        secret_phase: str | None = None,
        receipt_override: Mapping[str, Any] | None = None,
    ) -> None:
        self.calls: list[str] = []
        self.fail_once = fail_once
        self.fail_always = fail_always
        self.secret_phase = secret_phase
        self.receipt_override = dict(receipt_override or {})
        self.failed = False

    def perform(
        self,
        phase: str,
        *,
        intent: Mapping[str, Any],
        receipts: Mapping[str, Mapping[str, Any]],
    ) -> Mapping[str, Any]:
        self.calls.append(phase)
        if phase == self.fail_always or (phase == self.fail_once and not self.failed):
            self.failed = True
            raise RuntimeError("injected action failure")
        receipt = dict(
            _valid_action_receipt(
                phase,
                intent=intent,
                receipts=receipts,
            )
        )
        receipt["secret_material_recorded"] = phase == self.secret_phase
        receipt.update(self.receipt_override)
        return receipt


def _phases(journal: MemoryJournal) -> list[str]:
    return [str(item["phase"]) for item in journal.events]


def _receipt_for_phase(
    intent: Mapping[str, Any],
    phase: str,
    *,
    receipts: Mapping[str, Mapping[str, Any]],
) -> Mapping[str, Any]:
    if phase in runtime.ACTION_PHASES:
        return _valid_action_receipt(
            phase,
            intent=intent,
            receipts=receipts,
        )
    return runtime._internal_receipt(phase=phase, intent=intent)


def _forward_prefix(through_phase: str) -> MemoryJournal:
    intent = _intent()
    journal = MemoryJournal()
    prior = runtime.ZERO_SHA256
    receipts: dict[str, Mapping[str, Any]] = {}
    phases = runtime.FORWARD_PHASES[: runtime.FORWARD_PHASES.index(through_phase) + 1]
    for sequence, phase in enumerate(phases):
        receipt = _receipt_for_phase(
            intent,
            phase,
            receipts=receipts,
        )
        event = runtime.build_event(
            intent=intent,
            sequence=sequence,
            phase=phase,
            prior_event_sha256=prior,
            receipt=receipt,
            created_at_unix=NOW,
        )
        journal.append(event)
        prior = str(event["event_sha256"])
        receipts[phase] = receipt
    return journal


def _action_receipt_contexts() -> Mapping[str, Mapping[str, Mapping[str, Any]]]:
    intent = _intent()
    contexts: dict[str, Mapping[str, Mapping[str, Any]]] = {}

    forward_receipts: dict[str, Mapping[str, Any]] = {}
    for phase in runtime.FORWARD_PHASES:
        if phase == runtime.FIRST_APPLICATION_MUTATION_PHASE:
            contexts["pre_mutation_cas_revalidated"] = deepcopy(
                forward_receipts
            )
        if phase in runtime.ACTION_PHASES:
            contexts[phase] = deepcopy(forward_receipts)
            receipt = _valid_action_receipt(
                phase,
                intent=intent,
                receipts=forward_receipts,
            )
        else:
            receipt = runtime._internal_receipt(
                phase=phase,
                intent=intent,
            )
        forward_receipts[phase] = receipt
    contexts["completed_revalidated"] = deepcopy(forward_receipts)

    rollback_receipts: dict[str, Mapping[str, Any]] = {}
    through = runtime.FORWARD_PHASES.index("target_health_validated") + 1
    for phase in runtime.FORWARD_PHASES[:through]:
        if phase in runtime.ACTION_PHASES:
            receipt = _valid_action_receipt(
                phase,
                intent=intent,
                receipts=rollback_receipts,
            )
        else:
            receipt = runtime._internal_receipt(
                phase=phase,
                intent=intent,
            )
        rollback_receipts[phase] = receipt
    for phase in runtime.ROLLBACK_PHASES:
        if phase in runtime.ACTION_PHASES:
            contexts[phase] = deepcopy(rollback_receipts)
            receipt = _valid_action_receipt(
                phase,
                intent=intent,
                receipts=rollback_receipts,
            )
        else:
            receipt = runtime._internal_receipt(
                phase=phase,
                intent=intent,
            )
        rollback_receipts[phase] = receipt
    contexts["rolled_back_revalidated"] = deepcopy(rollback_receipts)

    abort_receipts: dict[str, Mapping[str, Any]] = {}
    through_abort_prefix = runtime.FORWARD_PHASES.index("recovery_gate_installed") + 1
    for phase in runtime.FORWARD_PHASES[:through_abort_prefix]:
        if phase in runtime.ACTION_PHASES:
            receipt = _valid_action_receipt(
                phase,
                intent=intent,
                receipts=abort_receipts,
            )
        else:
            receipt = runtime._internal_receipt(
                phase=phase,
                intent=intent,
            )
        abort_receipts[phase] = receipt
    for phase in runtime.ABORT_PHASES:
        if phase in runtime.ACTION_PHASES:
            contexts[phase] = deepcopy(abort_receipts)
            receipt = _valid_action_receipt(
                phase,
                intent=intent,
                receipts=abort_receipts,
            )
        else:
            receipt = runtime._internal_receipt(
                phase=phase,
                intent=intent,
            )
        abort_receipts[phase] = receipt
    contexts["aborted_revalidated"] = deepcopy(abort_receipts)
    return contexts


def test_forward_transaction_persists_exact_order_and_completes() -> None:
    journal = MemoryJournal()
    actions = FakeActions()

    state = _execute(actions=actions, journal=journal)

    assert state.terminal_phase == "completed"
    assert _phases(journal) == list(runtime.FORWARD_PHASES)
    assert _phases(journal).index("recovery_gate_installed") < (
        _phases(journal).index(runtime.FIRST_APPLICATION_MUTATION_PHASE)
    )
    assert _phases(journal).index("voice_guard_initial") < (
        _phases(journal).index("prestate_archived")
    )
    assert _phases(journal).index("pre_fence_cas_validated") < (
        _phases(journal).index(runtime.FIRST_APPLICATION_MUTATION_PHASE)
    )
    assert "pre_mutation_cas_revalidated" not in _phases(journal)
    assert actions.calls.index("pre_fence_cas_validated") + 1 == (
        actions.calls.index("pre_mutation_cas_revalidated")
    )
    assert _phases(journal).index("voice_guard_final") + 1 == (
        _phases(journal).index("consumers_fenced")
    )
    assert _phases(journal).index("release_consumers_zeroed") < (
        _phases(journal).index("host_payloads_applied")
    )
    assert _phases(journal).index("target_health_validated") < (
        _phases(journal).index(runtime.COMMIT_PHASE)
    )
    assert _phases(journal).index(runtime.COMMIT_PHASE) < (
        _phases(journal).index("unit_inputs_finalized")
    )
    assert runtime.COMMIT_PHASE not in actions.calls
    assert "completed" not in actions.calls


def test_every_action_receipt_phase_has_exact_semantic_contract() -> None:
    contexts = _action_receipt_contexts()

    assert set(runtime._ACTION_RECEIPT_EVIDENCE_FIELDS) == set(
        runtime.ACTION_RECEIPT_PHASES
    )
    assert set(contexts) == set(runtime.ACTION_RECEIPT_PHASES)
    for phase, receipts in contexts.items():
        receipt = _valid_action_receipt(
            phase,
            intent=_intent(),
            receipts=receipts,
        )
        assert (
            runtime._validate_bound_action_receipt(
                receipt,
                intent=_intent(),
                phase=phase,
                receipts=receipts,
            )
            == receipt
        )


def test_generic_ok_receipt_is_rejected_for_every_action_phase() -> None:
    for phase, receipts in _action_receipt_contexts().items():
        generic = {
            "schema": (f"muncho-production-release-update-{phase}-receipt.v1"),
            "phase": phase,
            "intent_sha256": _intent()["intent_sha256"],
            "publication_sha256": _intent()["publication_sha256"],
            "plan_sha256": _intent()["plan_sha256"],
            "approval_sha256": _intent()["approval_sha256"],
            "predecessor_revision": _intent()["predecessor_revision"],
            "release_revision": _intent()["release_revision"],
            "idempotency_key": runtime.action_idempotency_key(
                _intent(),
                phase,
            ),
            "prior_receipts_sha256": runtime._sha(runtime._canonical(receipts)),
            "ok": True,
            "secret_material_recorded": False,
            "secret_digest_recorded": False,
        }
        with pytest.raises(
            runtime.ProductionReleaseUpdateRuntimeError,
            match="release_update_runtime_action_receipt_invalid",
        ):
            runtime._validate_bound_action_receipt(
                generic,
                intent=_intent(),
                phase=phase,
                receipts=receipts,
            )


def test_action_receipts_reject_missing_extra_and_stale_context() -> None:
    for phase, receipts in _action_receipt_contexts().items():
        valid = dict(
            _valid_action_receipt(
                phase,
                intent=_intent(),
                receipts=receipts,
            )
        )
        missing = dict(valid)
        missing.pop(sorted(runtime._ACTION_RECEIPT_EVIDENCE_FIELDS[phase])[0])
        extra = {**valid, "uncontracted_claim": True}
        stale = {**valid, "prior_receipts_sha256": "f" * 64}

        for invalid in (missing, extra, stale):
            with pytest.raises(
                runtime.ProductionReleaseUpdateRuntimeError,
                match="release_update_runtime_action_receipt_invalid",
            ):
                runtime._validate_bound_action_receipt(
                    invalid,
                    intent=_intent(),
                    phase=phase,
                    receipts=receipts,
                )


@pytest.mark.parametrize(
    ("phase", "field", "tamper"),
    (
        (
            "voice_guard_final",
            "protected_service_set_sha256",
            "f" * 64,
        ),
        (
            "pre_fence_cas_validated",
            "observed_predecessor_activation_receipt_sha256",
            "f" * 64,
        ),
        ("consumers_fenced", "consumer_inventory_sha256", "f" * 64),
        (
            "host_payloads_applied",
            "applied_target_set_sha256",
            "f" * 64,
        ),
        (
            "target_health_validated",
            "target_process_set_sha256",
            "f" * 64,
        ),
        (
            "unit_inputs_finalized",
            "finalized_unit_input_set_sha256",
            "f" * 64,
        ),
        (
            "release_pointer_rotated",
            "unit_input_activation_receipt_sha256",
            "f" * 64,
        ),
        (
            "target_consumers_enabled",
            "active_long_running_service_unit_count",
            runtime.EXPECTED_LONG_RUNNING_SERVICE_UNIT_COUNT - 1,
        ),
        (
            "target_started_disabled",
            "started_triggered_oneshot_service_unit_count",
            1,
        ),
        (
            "release_consumers_zeroed",
            "host_observation_receipt_sha256",
            "not-a-digest",
        ),
        ("terminal_validated", "observed_pointer_revision", "f" * 40),
        (
            "host_prestate_restored",
            "prestate_archive_sha256",
            "f" * 64,
        ),
        (
            "rollback_validated",
            "restored_prestate_archive_sha256",
            "f" * 64,
        ),
        (
            "completed_revalidated",
            "terminal_receipt_sha256",
            "f" * 64,
        ),
        (
            "rolled_back_revalidated",
            "rollback_receipt_sha256",
            "f" * 64,
        ),
    ),
)
def test_action_receipt_semantic_evidence_is_cross_bound(
    phase: str,
    field: str,
    tamper: Any,
) -> None:
    receipts = _action_receipt_contexts()[phase]
    valid = _valid_action_receipt(
        phase,
        intent=_intent(),
        receipts=receipts,
    )

    with pytest.raises(
        runtime.ProductionReleaseUpdateRuntimeError,
        match="release_update_runtime_action_receipt_invalid",
    ):
        runtime._validate_bound_action_receipt(
            {**valid, field: tamper},
            intent=_intent(),
            phase=phase,
            receipts=receipts,
        )


def test_precommit_failure_after_fence_rolls_back_exactly() -> None:
    journal = MemoryJournal()
    actions = FakeActions(fail_once="host_payloads_applied")

    state = _execute(actions=actions, journal=journal)

    assert state.terminal_phase == "rolled_back"
    assert _phases(journal) == [
        *runtime.FORWARD_PHASES[
            : runtime.FORWARD_PHASES.index("host_payloads_applied")
        ],
        *runtime.ROLLBACK_PHASES,
    ]
    assert runtime.COMMIT_PHASE not in _phases(journal)
    assert actions.calls[-4:] == [
        "target_stopped",
        "host_prestate_restored",
        "predecessor_consumers_restored",
        "rollback_validated",
    ]


def test_failure_before_application_mutation_does_not_invent_rollback() -> None:
    journal = MemoryJournal()
    actions = FakeActions(fail_once="candidate_validated")

    with pytest.raises(
        runtime.ProductionReleaseUpdateRuntimeError,
        match="release_update_runtime_precommit_failed",
    ):
        _execute(actions=actions, journal=journal)

    assert journal.events == []
    assert not any(phase in actions.calls for phase in runtime.ROLLBACK_PHASES)


def test_live_cas_is_revalidated_again_after_pre_mutation_crash() -> None:
    journal = MemoryJournal()
    actions = FakeActions(fail_once="pre_mutation_cas_revalidated")

    with pytest.raises(
        runtime.ProductionReleaseUpdateRuntimeError,
        match="release_update_runtime_precommit_failed",
    ):
        _execute(actions=actions, journal=journal)

    mutation_index = runtime.FORWARD_PHASES.index(
        runtime.FIRST_APPLICATION_MUTATION_PHASE
    )
    assert _phases(journal) == list(runtime.FORWARD_PHASES[:mutation_index])
    assert runtime.FIRST_APPLICATION_MUTATION_PHASE not in _phases(journal)
    assert actions.calls.count("pre_mutation_cas_revalidated") == 1

    state = _recover(
        actions=actions,
        journal=journal,
        now_unix=NOW + 1,
    )

    assert state.terminal_phase == "completed"
    assert _phases(journal) == list(runtime.FORWARD_PHASES)
    assert actions.calls.count("pre_mutation_cas_revalidated") == 2


def test_postcommit_failure_is_forward_only_and_recovery_completes() -> None:
    journal = MemoryJournal()
    first = FakeActions(fail_once="unit_inputs_finalized")

    with pytest.raises(
        runtime.ProductionReleaseUpdateRuntimeError,
        match="release_update_runtime_forward_completion_pending",
    ):
        _execute(actions=first, journal=journal)

    assert _phases(journal)[-1] == runtime.COMMIT_PHASE
    assert "rollback_intent" not in _phases(journal)

    recovery = FakeActions()
    state = _recover(
        actions=recovery,
        journal=journal,
        now_unix=NOW + 1,
    )

    assert state.terminal_phase == "completed"
    assert _phases(journal) == list(runtime.FORWARD_PHASES)
    assert not any(phase in recovery.calls for phase in runtime.ROLLBACK_PHASES)
    assert recovery.calls[0] == "unit_inputs_finalized"


def test_boot_recovery_rolls_back_any_precommit_application_mutation() -> None:
    journal = _forward_prefix("host_payloads_applied")
    actions = FakeActions()

    state = _recover(
        actions=actions,
        journal=journal,
        now_unix=NOW + 1,
    )

    assert state.terminal_phase == "rolled_back"
    assert actions.calls == [
        "target_stopped",
        "host_prestate_restored",
        "predecessor_consumers_restored",
        "rollback_validated",
    ]


def test_fresh_execute_cannot_resume_a_precommit_mutated_journal_forward() -> None:
    journal = _forward_prefix("consumers_fenced")
    actions = FakeActions()

    state = _execute(
        actions=actions,
        journal=journal,
        now_unix=NOW + 1,
    )

    assert state.terminal_phase == "rolled_back"
    assert _phases(journal)[-len(runtime.ROLLBACK_PHASES) :] == list(
        runtime.ROLLBACK_PHASES
    )
    assert actions.calls == [
        "target_stopped",
        "host_prestate_restored",
        "predecessor_consumers_restored",
        "rollback_validated",
    ]
    assert runtime.COMMIT_PHASE not in _phases(journal)


def test_action_completed_but_event_write_failed_still_rolls_back() -> None:
    journal = MemoryJournal(fail_append_phase="target_started_disabled")
    actions = FakeActions()

    state = _execute(actions=actions, journal=journal)

    assert state.terminal_phase == "rolled_back"
    assert "target_started_disabled" in actions.calls
    assert "target_started_disabled" not in _phases(journal)
    assert _phases(journal)[-len(runtime.ROLLBACK_PHASES) :] == list(
        runtime.ROLLBACK_PHASES
    )


def test_first_mutation_action_write_gap_is_covered_by_write_ahead_intent() -> None:
    journal = MemoryJournal(fail_append_phase="consumers_fenced")
    actions = FakeActions()

    state = _execute(actions=actions, journal=journal)

    assert "consumers_fenced" in actions.calls
    assert "consumers_fenced" not in _phases(journal)
    assert runtime.FIRST_APPLICATION_MUTATION_PHASE in _phases(journal)
    assert state.terminal_phase == "rolled_back"
    assert _phases(journal)[-len(runtime.ROLLBACK_PHASES) :] == list(
        runtime.ROLLBACK_PHASES
    )


def test_terminal_exact_replay_revalidates_live_state() -> None:
    journal = MemoryJournal()
    first = FakeActions()
    completed = _execute(actions=first, journal=journal)
    replay = FakeActions()

    state = _execute(
        actions=replay,
        journal=journal,
        now_unix=NOW + 100,
    )

    assert state == completed
    assert replay.calls == ["completed_revalidated"]


def test_journal_chain_and_phase_order_are_fail_closed() -> None:
    journal = MemoryJournal()
    _execute(actions=FakeActions(), journal=journal)

    broken = journal.load()
    broken[1]["prior_event_sha256"] = runtime.ZERO_SHA256
    with pytest.raises(
        runtime.ProductionReleaseUpdateRuntimeError,
        match="release_update_runtime_event_invalid",
    ):
        runtime.load_state(intent=_intent(), events=broken)

    reordered = journal.load()
    reordered[2], reordered[3] = reordered[3], reordered[2]
    with pytest.raises(runtime.ProductionReleaseUpdateRuntimeError):
        runtime.load_state(intent=_intent(), events=reordered)


@pytest.mark.parametrize(
    ("phase", "tamper"),
    (
        ("candidate_validated", {"intent_sha256": "f" * 64}),
        ("application_mutation_intent", {"application_mutation_authorized": False}),
    ),
)
def test_rehashed_journal_cannot_rebind_or_weaken_phase_receipt(
    phase: str,
    tamper: Mapping[str, Any],
) -> None:
    intent = _intent()
    events = list(_forward_prefix(phase).load())
    event = dict(events[-1])
    receipt = dict(event["receipt"])
    event["receipt"] = {**receipt, **tamper}
    event["receipt_sha256"] = runtime._sha(runtime._canonical(event["receipt"]))
    unsigned = {key: item for key, item in event.items() if key != "event_sha256"}
    event["event_sha256"] = runtime._sha(runtime._canonical(unsigned))

    with pytest.raises(
        runtime.ProductionReleaseUpdateRuntimeError,
        match="release_update_runtime_journal_invalid",
    ):
        runtime.load_state(intent=intent, events=[*events[:-1], event])


def test_secret_bearing_action_receipt_is_rejected_and_rolled_back() -> None:
    journal = MemoryJournal()
    actions = FakeActions(secret_phase="host_payloads_applied")

    state = _execute(actions=actions, journal=journal)

    assert state.terminal_phase == "rolled_back"
    assert "host_payloads_applied" not in _phases(journal)
    assert all(
        event["receipt"]["secret_material_recorded"] is False
        for event in journal.events
    )


@pytest.mark.parametrize(
    "override",
    (
        {"phase": "wrong"},
        {"intent_sha256": "f" * 64},
        {"release_revision": "f" * 40},
        {"plan_sha256": "f" * 64},
        {"ok": False},
    ),
)
def test_meaningless_or_rebound_action_receipt_fails_closed(
    override: Mapping[str, Any],
) -> None:
    journal = MemoryJournal()
    actions = FakeActions(receipt_override=override)

    with pytest.raises(
        runtime.ProductionReleaseUpdateRuntimeError,
        match="release_update_runtime_precommit_failed",
    ):
        _execute(actions=actions, journal=journal)
    assert journal.events == []


def test_append_acknowledgement_without_durable_readback_fails_closed() -> None:
    with pytest.raises(
        runtime.ProductionReleaseUpdateRuntimeError,
        match="release_update_runtime_precommit_failed",
    ):
        _execute(
            actions=FakeActions(),
            journal=LostAppendJournal(),
        )


def test_commit_persisted_then_interrupted_remains_forward_only() -> None:
    journal = PersistThenRaiseJournal(runtime.COMMIT_PHASE)
    with pytest.raises(
        runtime.ProductionReleaseUpdateRuntimeError,
        match="release_update_runtime_forward_completion_pending",
    ):
        _execute(actions=FakeActions(), journal=journal)
    assert _phases(journal)[-1] == runtime.COMMIT_PHASE

    state = _recover(
        actions=FakeActions(),
        journal=journal,
        now_unix=NOW + 1,
    )
    assert state.terminal_phase == "completed"
    assert "rollback_intent" not in _phases(journal)


def test_backward_clock_cannot_strand_precommit_rollback_recovery() -> None:
    journal = _forward_prefix("consumers_fenced")

    state = _recover(
        actions=FakeActions(),
        journal=journal,
        now_unix=NOW - 1,
    )

    assert state.terminal_phase == "rolled_back"
    assert _phases(journal)[-len(runtime.ROLLBACK_PHASES) :] == list(
        runtime.ROLLBACK_PHASES
    )
    assert all(
        event["created_at_unix"] == NOW
        for event in journal.events[-len(runtime.ROLLBACK_PHASES) :]
    )


def test_backward_clock_cannot_strand_postcommit_forward_recovery() -> None:
    journal = _forward_prefix(runtime.COMMIT_PHASE)

    state = _recover(
        actions=FakeActions(),
        journal=journal,
        now_unix=NOW - 1,
    )

    assert state.terminal_phase == "completed"
    assert _phases(journal) == list(runtime.FORWARD_PHASES)
    assert "rollback_intent" not in _phases(journal)
    commit_index = _phases(journal).index(runtime.COMMIT_PHASE)
    assert all(
        event["created_at_unix"] == NOW
        for event in journal.events[commit_index + 1 :]
    )


def test_same_run_backward_clock_after_mutation_forces_rollback() -> None:
    journal = MemoryJournal()

    def observed_now() -> int:
        return NOW - 1 if "consumers_fenced" in _phases(journal) else NOW

    with patch.object(runtime.time, "time", side_effect=observed_now):
        state = runtime._execute_update_for_test(
            authority_record=_authority_record(),
            actions=FakeActions(),
            journal=journal,
            lock_factory=nullcontext,
        )

    assert state.terminal_phase == "rolled_back"
    assert _phases(journal)[-len(runtime.ROLLBACK_PHASES) :] == list(
        runtime.ROLLBACK_PHASES
    )
    assert runtime.COMMIT_PHASE not in _phases(journal)


def test_journal_rejects_decreasing_time_and_orphan_rollback_terminal() -> None:
    intent = _intent()
    first_receipt = _receipt_for_phase(
        intent,
        "candidate_validated",
        receipts={},
    )
    first = runtime.build_event(
        intent=intent,
        sequence=0,
        phase="candidate_validated",
        prior_event_sha256=runtime.ZERO_SHA256,
        receipt=first_receipt,
        created_at_unix=NOW + 10,
    )
    second = runtime.build_event(
        intent=intent,
        sequence=1,
        phase="voice_guard_initial",
        prior_event_sha256=str(first["event_sha256"]),
        receipt=_receipt_for_phase(
            intent,
            "voice_guard_initial",
            receipts={"candidate_validated": first_receipt},
        ),
        created_at_unix=NOW + 5,
    )
    with pytest.raises(
        runtime.ProductionReleaseUpdateRuntimeError,
        match="release_update_runtime_journal_invalid",
    ):
        runtime.load_state(intent=intent, events=[first, second])

    orphan = runtime.build_event(
        intent=intent,
        sequence=0,
        phase="rolled_back",
        prior_event_sha256=runtime.ZERO_SHA256,
        receipt=_receipt_for_phase(
            intent,
            "rolled_back",
            receipts={},
        ),
        created_at_unix=NOW,
    )
    with pytest.raises(
        runtime.ProductionReleaseUpdateRuntimeError,
        match="release_update_runtime_journal_invalid",
    ):
        runtime.load_state(intent=intent, events=[orphan])


def test_active_predecessor_receipt_is_a_compare_and_swap_input() -> None:
    _private, trusted, _plan, _approval, publication = _documents()
    with pytest.raises(
        runtime.ProductionReleaseUpdateRuntimeError,
        match="release_update_runtime_predecessor_changed",
    ):
        runtime.build_intent(
            publication=publication,
            trusted_predecessor=trusted,
            expected_predecessor_trust_sha256=str(trusted["trust_sha256"]),
            predecessor_current_receipt_sha256="f" * 64,
        )


def test_terminal_replay_detects_live_state_drift() -> None:
    journal = MemoryJournal()
    _execute(actions=FakeActions(), journal=journal)
    with pytest.raises(
        runtime.ProductionReleaseUpdateRuntimeError,
        match="release_update_runtime_terminal_revalidation_failed",
    ):
        _recover(
            actions=FakeActions(fail_always="completed_revalidated"),
            journal=journal,
            now_unix=NOW + 1,
        )


def test_self_rehashed_intent_cannot_escape_signed_authority() -> None:
    forged_intent = {
        **_intent(),
        "whole_tree_manifest_sha256": "f" * 64,
    }
    forged_intent["intent_sha256"] = runtime._sha(
        runtime._canonical({
            key: value for key, value in forged_intent.items() if key != "intent_sha256"
        })
    )
    assert runtime.validate_intent(forged_intent) == forged_intent

    forged_record = {
        **_authority_record(),
        "intent": forged_intent,
    }
    forged_record["authority_record_sha256"] = runtime._sha(
        runtime._canonical({
            key: value
            for key, value in forged_record.items()
            if key != "authority_record_sha256"
        })
    )

    with pytest.raises(
        runtime.ProductionReleaseUpdateRuntimeError,
        match="release_update_runtime_authority_record_invalid",
    ):
        runtime.validate_authority_record(forged_record)


@pytest.mark.parametrize(
    "identity_overrides",
    (
        {"transaction_nonce_sha256": "f" * 64},
        {
            "approval_issued_at_unix": NOW,
            "created_at_unix": NOW,
        },
    ),
)
def test_self_rehashed_identity_must_exactly_match_signed_approval(
    identity_overrides: Mapping[str, object],
) -> None:
    forged_intent = {**_intent(), **identity_overrides}
    forged_intent["intent_sha256"] = runtime._sha(
        runtime._canonical({
            key: value for key, value in forged_intent.items() if key != "intent_sha256"
        })
    )
    assert runtime.validate_intent(forged_intent) == forged_intent

    forged_record = {
        **_authority_record(),
        "intent": forged_intent,
    }
    forged_record["authority_record_sha256"] = runtime._sha(
        runtime._canonical({
            key: value
            for key, value in forged_record.items()
            if key != "authority_record_sha256"
        })
    )

    with pytest.raises(
        runtime.ProductionReleaseUpdateRuntimeError,
        match="release_update_runtime_authority_record_invalid",
    ):
        runtime.validate_authority_record(forged_record)


def test_journal_authority_must_exactly_match_caller_authority() -> None:
    _private, trusted, plan, _approval, publication = _documents()
    other_record = runtime.build_authority_record(
        publication=publication,
        trusted_predecessor=trusted,
        expected_predecessor_trust_sha256=str(trusted["trust_sha256"]),
        predecessor_current_receipt_sha256=str(
            plan["predecessor_activation_receipt_sha256"]
        ),
    )

    with pytest.raises(
        runtime.ProductionReleaseUpdateRuntimeError,
        match="release_update_runtime_journal_authority_invalid",
    ):
        _execute(
            actions=FakeActions(),
            journal=MemoryJournal(authority_record=other_record),
        )


def test_public_production_entrypoint_exposes_no_lock_or_root_override() -> None:
    with pytest.raises(TypeError):
        runtime.execute_update(
            authority_record=_authority_record(),
            actions=FakeActions(),
            journal=MemoryJournal(),
            lock_factory=nullcontext,
        )
    with pytest.raises(TypeError):
        runtime.execute_update(
            authority_record=_authority_record(),
            actions=FakeActions(),
            journal=MemoryJournal(),
            require_root=False,
        )


def test_expired_before_event_zero_aborts_without_application_mutation() -> None:
    journal = MemoryJournal()
    actions = FakeActions()

    state = _execute(
        actions=actions,
        journal=journal,
        now_unix=int(_intent()["approval_expires_at_unix"]),
    )

    assert state.terminal_phase == "aborted"
    assert _phases(journal) == list(runtime.ABORT_PHASES)
    assert actions.calls == ["preapplication_cleanup"]
    assert runtime.FIRST_APPLICATION_MUTATION_PHASE not in _phases(journal)


def test_expiry_after_recovery_gate_cleans_up_and_aborts() -> None:
    journal = MemoryJournal()
    actions = FakeActions()
    expires = int(_intent()["approval_expires_at_unix"])

    def observed_now() -> int:
        return expires if "recovery_gate_installed" in _phases(journal) else NOW

    with patch.object(runtime.time, "time", side_effect=observed_now):
        state = runtime._execute_update_for_test(
            authority_record=_authority_record(),
            actions=actions,
            journal=journal,
            lock_factory=nullcontext,
        )

    assert state.terminal_phase == "aborted"
    assert _phases(journal) == [
        *runtime.FORWARD_PHASES[
            : runtime.FORWARD_PHASES.index("recovery_gate_installed") + 1
        ],
        *runtime.ABORT_PHASES,
    ]
    assert actions.calls[-1] == "preapplication_cleanup"
    assert runtime.FIRST_APPLICATION_MUTATION_PHASE not in _phases(journal)


def test_expiry_after_application_mutation_forces_rollback() -> None:
    journal = MemoryJournal()
    actions = FakeActions()
    expires = int(_intent()["approval_expires_at_unix"])

    def observed_now() -> int:
        return (
            expires
            if runtime.FIRST_APPLICATION_MUTATION_PHASE in _phases(journal)
            else NOW
        )

    with patch.object(runtime.time, "time", side_effect=observed_now):
        state = runtime._execute_update_for_test(
            authority_record=_authority_record(),
            actions=actions,
            journal=journal,
            lock_factory=nullcontext,
        )

    assert state.terminal_phase == "rolled_back"
    mutation_index = runtime.FORWARD_PHASES.index(
        runtime.FIRST_APPLICATION_MUTATION_PHASE
    )
    assert _phases(journal) == [
        *runtime.FORWARD_PHASES[: mutation_index + 1],
        *runtime.ROLLBACK_PHASES,
    ]
    assert runtime.COMMIT_PHASE not in _phases(journal)


def test_expired_recovery_resumes_an_already_persisted_rollback() -> None:
    journal = _forward_prefix("consumers_fenced")
    intent = _intent()
    rollback_receipt = runtime._internal_receipt(
        phase="rollback_intent",
        intent=intent,
    )
    rollback_event = runtime.build_event(
        intent=intent,
        sequence=len(journal.events),
        phase="rollback_intent",
        prior_event_sha256=str(journal.events[-1]["event_sha256"]),
        receipt=rollback_receipt,
        created_at_unix=NOW,
    )
    journal.append(rollback_event)
    actions = FakeActions()

    state = _recover(
        actions=actions,
        journal=journal,
        now_unix=int(intent["approval_expires_at_unix"]),
    )

    assert state.terminal_phase == "rolled_back"
    assert _phases(journal)[-len(runtime.ROLLBACK_PHASES) :] == list(
        runtime.ROLLBACK_PHASES
    )
    assert "approval_expired_abort_intent" not in _phases(journal)
    assert actions.calls[0] == "target_stopped"


def test_expiry_after_durable_commit_remains_forward_only() -> None:
    journal = MemoryJournal()
    actions = FakeActions()
    expires = int(_intent()["approval_expires_at_unix"])

    def observed_now() -> int:
        return expires if runtime.COMMIT_PHASE in _phases(journal) else NOW

    with patch.object(runtime.time, "time", side_effect=observed_now):
        state = runtime._execute_update_for_test(
            authority_record=_authority_record(),
            actions=actions,
            journal=journal,
            lock_factory=nullcontext,
        )

    assert state.terminal_phase == "completed"
    assert _phases(journal) == list(runtime.FORWARD_PHASES)
    assert "rollback_intent" not in _phases(journal)
    assert "approval_expired_abort_intent" not in _phases(journal)


def test_expired_preapplication_recovery_aborts_and_replays_safely() -> None:
    journal = _forward_prefix("recovery_gate_installed")
    expires = int(_intent()["approval_expires_at_unix"])

    state = _recover(
        actions=FakeActions(),
        journal=journal,
        now_unix=expires,
    )
    assert state.terminal_phase == "aborted"

    replay = FakeActions()
    replayed = _recover(
        actions=replay,
        journal=journal,
        now_unix=expires + 1,
    )
    assert replayed == state
    assert replay.calls == ["aborted_revalidated"]
