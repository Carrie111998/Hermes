from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any, Mapping
from unittest.mock import Mock, patch

import pytest

from scripts.canary import production_release_host_actions as host_actions
from scripts.canary import production_release_update_runtime as runtime
from tests.scripts.canary.test_production_release_update_runtime import (
    MemoryJournal,
    _action_receipt_contexts,
    _authority_record,
    _execute,
    _intent,
    _phase_evidence,
    _phases,
    _recover,
    _valid_action_receipt,
)


class _SimulationBackend:
    def __init__(self, *, fail_once: str | None = None) -> None:
        self.calls: list[str] = []
        self.fail_once = fail_once
        self.failed = False

    def evidence(
        self,
        phase: str,
        *,
        intent: Mapping[str, Any],
        receipts: Mapping[str, Mapping[str, Any]],
    ) -> Mapping[str, Any]:
        self.calls.append(phase)
        if phase == self.fail_once and not self.failed:
            self.failed = True
            raise RuntimeError("simulated crash-safe primitive interruption")
        return _phase_evidence(
            phase,
            intent=intent,
            receipts=receipts,
        )


class _StaticBackend:
    def __init__(self, evidence: Mapping[str, Any]) -> None:
        self.value = dict(evidence)

    def evidence(
        self,
        phase: str,
        *,
        intent: Mapping[str, Any],
        receipts: Mapping[str, Mapping[str, Any]],
    ) -> Mapping[str, Any]:
        del phase, intent, receipts
        return dict(self.value)


def _actions(
    backend: Any | None = None,
) -> host_actions.ProductionReleaseHostActions:
    return host_actions.ProductionReleaseHostActions._for_test(
        _SimulationBackend() if backend is None else backend
    )


def test_dispatcher_covers_every_exact_runtime_action_receipt_schema() -> None:
    intent = _intent()
    contexts = _action_receipt_contexts()
    backend = _SimulationBackend()
    actions = _actions(backend)

    assert set(contexts) == set(runtime.ACTION_RECEIPT_PHASES)
    for phase, receipts in contexts.items():
        receipt = actions.perform(
            phase,
            intent=intent,
            receipts=receipts,
        )

        assert receipt == _valid_action_receipt(
            phase,
            intent=intent,
            receipts=receipts,
        )
        assert set(receipt) == (
            runtime._ACTION_RECEIPT_BASE_FIELDS
            | runtime._ACTION_RECEIPT_EVIDENCE_FIELDS[phase]
        )
        assert (
            runtime._validate_bound_action_receipt(
                receipt,
                intent=intent,
                phase=phase,
                receipts=receipts,
            )
            == receipt
        )

    assert set(backend.calls) == set(runtime.ACTION_RECEIPT_PHASES)


def test_simulated_forward_transaction_and_terminal_revalidation() -> None:
    backend = _SimulationBackend()
    actions = _actions(backend)
    journal = MemoryJournal()

    completed = _execute(actions=actions, journal=journal)
    revalidated = _recover(
        actions=actions,
        journal=journal,
        now_unix=int(_intent()["created_at_unix"]) + 1,
    )

    assert completed.terminal_phase == "completed"
    assert revalidated.terminal_phase == "completed"
    assert _phases(journal) == list(runtime.FORWARD_PHASES)
    assert backend.calls[-1] == "completed_revalidated"


def test_simulated_interruption_rolls_back_exact_prestate() -> None:
    backend = _SimulationBackend(fail_once="host_payloads_applied")
    actions = _actions(backend)
    journal = MemoryJournal()

    state = _execute(actions=actions, journal=journal)

    assert state.terminal_phase == "rolled_back"
    assert _phases(journal) == [
        *runtime.FORWARD_PHASES[
            : runtime.FORWARD_PHASES.index("host_payloads_applied")
        ],
        *runtime.ROLLBACK_PHASES,
    ]
    restored = state.receipts["host_prestate_restored"]
    archived = state.receipts["prestate_archived"]
    assert restored["prestate_archive_sha256"] == archived["prestate_archive_sha256"]
    assert (
        restored["restored_target_set_sha256"] == archived["archived_target_set_sha256"]
    )


def test_simulated_expiry_aborts_before_application_mutation_and_revalidates() -> None:
    backend = _SimulationBackend()
    actions = _actions(backend)
    journal = MemoryJournal()
    expired_at = int(_intent()["approval_expires_at_unix"])

    aborted = _execute(
        actions=actions,
        journal=journal,
        now_unix=expired_at,
    )
    revalidated = _recover(
        actions=actions,
        journal=journal,
        now_unix=expired_at + 1,
    )

    assert aborted.terminal_phase == "aborted"
    assert revalidated.terminal_phase == "aborted"
    assert runtime.FIRST_APPLICATION_MUTATION_PHASE not in _phases(journal)
    assert _phases(journal) == list(runtime.ABORT_PHASES)
    assert backend.calls == [
        "preapplication_cleanup",
        "aborted_revalidated",
    ]


def test_replay_is_deterministic_and_idempotency_bound() -> None:
    backend = _SimulationBackend()
    actions = _actions(backend)
    receipts = _action_receipt_contexts()["unit_inputs_finalized"]

    first = actions.perform(
        "unit_inputs_finalized",
        intent=_intent(),
        receipts=receipts,
    )
    replay = actions.perform(
        "unit_inputs_finalized",
        intent=_intent(),
        receipts=receipts,
    )

    assert replay == first
    assert first["idempotency_key"] == runtime.action_idempotency_key(
        _intent(),
        "unit_inputs_finalized",
    )
    assert backend.calls == [
        "unit_inputs_finalized",
        "unit_inputs_finalized",
    ]


def test_out_of_order_and_tampered_prior_receipts_fail_before_backend() -> None:
    backend = _SimulationBackend()
    actions = _actions(backend)
    contexts = _action_receipt_contexts()

    with pytest.raises(
        host_actions.ProductionReleaseHostActionsError,
        match="production_release_host_action_context_invalid",
    ):
        actions.perform(
            "candidate_validated",
            intent=_intent(),
            receipts=contexts["voice_guard_initial"],
        )

    tampered = {
        phase: dict(receipt)
        for phase, receipt in contexts["voice_guard_initial"].items()
    }
    tampered["candidate_validated"]["verified_regular_file_count"] = 0
    with pytest.raises(
        host_actions.ProductionReleaseHostActionsError,
        match="production_release_host_action_context_invalid",
    ):
        actions.perform(
            "voice_guard_initial",
            intent=_intent(),
            receipts=tampered,
        )

    assert backend.calls == []


@pytest.mark.parametrize("mode", ("missing", "extra"))
def test_backend_must_return_the_exact_closed_evidence_shape(
    mode: str,
) -> None:
    evidence = dict(
        _phase_evidence(
            "candidate_validated",
            intent=_intent(),
            receipts={},
        )
    )
    if mode == "missing":
        evidence.pop("release_root")
    else:
        evidence["untrusted_claim"] = True

    with pytest.raises(
        host_actions.ProductionReleaseHostActionsError,
        match="production_release_host_action_evidence_invalid",
    ):
        _actions(_StaticBackend(evidence)).perform(
            "candidate_validated",
            intent=_intent(),
            receipts={},
        )


def test_semantically_false_success_evidence_is_rejected() -> None:
    evidence = dict(
        _phase_evidence(
            "candidate_validated",
            intent=_intent(),
            receipts={},
        )
    )
    evidence["verified_regular_file_count"] = 0

    with pytest.raises(
        host_actions.ProductionReleaseHostActionsError,
        match="production_release_host_action_receipt_invalid",
    ):
        _actions(_StaticBackend(evidence)).perform(
            "candidate_validated",
            intent=_intent(),
            receipts={},
        )


def test_invalid_phase_and_intent_are_rejected_before_backend() -> None:
    backend = _SimulationBackend()
    actions = _actions(backend)

    with pytest.raises(
        host_actions.ProductionReleaseHostActionsError,
        match="production_release_host_action_phase_invalid",
    ):
        actions.perform("not-a-phase", intent=_intent(), receipts={})

    with pytest.raises(
        host_actions.ProductionReleaseHostActionsError,
        match="production_release_host_action_intent_invalid",
    ):
        actions.perform(
            "candidate_validated",
            intent={**_intent(), "intent_sha256": "f" * 64},
            receipts={},
        )

    assert backend.calls == []


def test_public_constructor_is_closed_and_requires_linux_root() -> None:
    with pytest.raises(TypeError):
        host_actions.ProductionReleaseHostActions(object())  # type: ignore[call-arg]

    with (
        patch.object(host_actions.sys, "platform", "darwin"),
        pytest.raises(
            host_actions.ProductionReleaseHostActionsError,
            match="production_release_host_actions_requires_linux",
        ),
    ):
        host_actions.ProductionReleaseHostActions()

    with (
        patch.object(host_actions.sys, "platform", "linux"),
        patch.object(host_actions.os, "geteuid", return_value=1000),
        pytest.raises(
            host_actions.ProductionReleaseHostActionsError,
            match="production_release_host_actions_requires_root",
        ),
    ):
        host_actions.ProductionReleaseHostActions()


def test_production_candidate_validation_uses_fixed_stage0_bundle() -> None:
    authority_record = _authority_record()
    intent = _intent()
    stable = Mock()
    bundle = SimpleNamespace(
        publication=authority_record["publication"],
        predecessor_trust=authority_record["trusted_predecessor"],
        release_root=Path(str(intent["release_root"])),
        builder_manifest={
            "manifest_sha256": intent["whole_tree_manifest_sha256"],
            "payload_entries": [
                {"kind": "directory", "path": "scripts"},
                {"kind": "file", "path": "run_agent.py"},
                {"kind": "file", "path": "scripts/canary/update.py"},
            ],
        },
        builder_receipt={
            "manifest_sha256": intent["whole_tree_manifest_sha256"],
        },
        assert_stable=stable,
    )
    context = Mock()
    context.__enter__ = Mock(return_value=bundle)
    context.__exit__ = Mock(return_value=None)
    with (
        patch.object(
            host_actions.stage0,
            "verify_stage0",
            return_value=context,
        ) as verify,
    ):
        receipt = _actions(host_actions._ProductionHostActionEvidence()).perform(
            "candidate_validated",
            intent=intent,
            receipts={},
        )

    verify.assert_called_once_with(
        expected_predecessor_activation_receipt_sha256=intent[
            "predecessor_current_receipt_sha256"
        ],
    )
    assert stable.call_count == 2
    assert receipt["verified_regular_file_count"] == 2


def test_production_zero_consumer_evidence_uses_closed_world_observer() -> None:
    receipts = _action_receipt_contexts()["release_consumers_zeroed"]
    observation_receipt = {
        "receipt_sha256": "e" * 64,
        "validation": {
            "observed_process_count": 0,
            "expected_unit_count": runtime.EXPECTED_CONSUMER_UNIT_COUNT,
            "execution_service_count": runtime.EXPECTED_SERVICE_UNIT_COUNT,
            "long_running_service_count": (
                runtime.EXPECTED_LONG_RUNNING_SERVICE_UNIT_COUNT
            ),
            "startup_oneshot_service_count": (
                runtime.EXPECTED_STARTUP_ONESHOT_SERVICE_UNIT_COUNT
            ),
            "triggered_oneshot_service_count": (
                runtime.EXPECTED_TRIGGERED_ONESHOT_SERVICE_UNIT_COUNT
            ),
            "oneshot_service_count": runtime.EXPECTED_ONESHOT_SERVICE_UNIT_COUNT,
            "trigger_unit_count": runtime.EXPECTED_TRIGGER_UNIT_COUNT,
        },
        "processes": {"selected_process_count": 0},
    }
    result = SimpleNamespace(receipt={"opaque": "raw"})

    with (
        patch.object(
            host_actions.host_observer,
            "observe_and_validate_release_host",
            return_value=result,
        ) as observe,
        patch.object(
            host_actions.host_observer,
            "validate_host_observation_receipt",
            return_value=observation_receipt,
        ),
    ):
        receipt = _actions(host_actions._ProductionHostActionEvidence()).perform(
            "release_consumers_zeroed",
            intent=_intent(),
            receipts=receipts,
        )

    observe.assert_called_once_with(
        phase=host_actions.inventory.InventoryPhase.PREDECESSOR_FENCED,
        predecessor_revision=_intent()["predecessor_revision"],
        target_revision=_intent()["release_revision"],
    )
    assert receipt["observed_consumer_process_count"] == 0
    assert receipt["observed_unknown_process_count"] == 0
    assert receipt["observed_mutable_pointer_process_count"] == 0
    assert receipt["need_daemon_reload_unit_count"] == 0
    assert receipt["host_observation_receipt_sha256"] == "e" * 64


@pytest.mark.parametrize(
    "phase",
    sorted(
        runtime.ACTION_RECEIPT_PHASES
        - {
            "candidate_validated",
            "release_consumers_zeroed",
        }
    ),
)
def test_unintegrated_production_primitives_never_emit_success_receipts(
    phase: str,
) -> None:
    receipts = _action_receipt_contexts()[phase]

    with pytest.raises(
        host_actions.ProductionReleaseHostActionsError,
        match=(f"production_release_host_action_primitive_unavailable:{phase}"),
    ):
        _actions(host_actions._ProductionHostActionEvidence()).perform(
            phase,
            intent=_intent(),
            receipts=receipts,
        )
