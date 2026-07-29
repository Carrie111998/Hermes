#!/usr/bin/env python3
"""Fail-closed production host actions for the Stage-C release runtime.

The release-update runtime owns transaction ordering and receipt semantics.
This module is the production boundary that turns one already-authorized
runtime phase into host evidence.  It deliberately has a closed public
constructor: production always uses Linux root, the fixed Stage-0 roots, the
fixed systemd/proc observer, and no injected command runner, clock, or lock.

Only actions backed by a complete crash-safe host primitive may return a
receipt.  Until the remaining mutation primitives are integrated, those
phases fail with ``production_release_host_action_primitive_unavailable``.
That is intentional: a syntactically valid success receipt without durable
host evidence would weaken recovery rather than implement it.

The private ``_for_test`` constructor is the sole simulation seam.  It lets
tests exercise the complete dispatcher and every exact runtime receipt schema
without making dependency injection part of the production API.
"""

from __future__ import annotations

import os
import sys
from types import MappingProxyType
from typing import Any, Mapping, NoReturn, Protocol

from scripts.canary import production_release_consumer_inventory as inventory
from scripts.canary import production_release_host_observer as host_observer
from scripts.canary import production_release_update_runtime as runtime
from scripts.canary import production_release_update_stage0 as stage0


class ProductionReleaseHostActionsError(RuntimeError):
    """Stable, secret-free host-action failure."""

    def __init__(self, code: str, phase: str | None = None) -> None:
        self.code = code
        self.phase = phase
        message = code if phase is None else f"{code}:{phase}"
        super().__init__(message)


def _fail(code: str, phase: str | None = None) -> NoReturn:
    raise ProductionReleaseHostActionsError(code, phase)


class _HostActionEvidenceBackend(Protocol):
    """Private evidence seam; production never accepts one from callers."""

    def evidence(
        self,
        phase: str,
        *,
        intent: Mapping[str, Any],
        receipts: Mapping[str, Mapping[str, Any]],
    ) -> Mapping[str, Any]:
        """Perform/revalidate one phase and return only its evidence fields."""


def _require_production_host() -> None:
    if not sys.platform.startswith("linux"):
        _fail("production_release_host_actions_requires_linux")
    geteuid = getattr(os, "geteuid", None)
    if not callable(geteuid) or geteuid() != 0:
        _fail("production_release_host_actions_requires_root")


def _copied_receipts(
    receipts: Mapping[str, Mapping[str, Any]],
) -> Mapping[str, Mapping[str, Any]]:
    return {phase: dict(receipt) for phase, receipt in receipts.items()}


def _validate_context_sequence(
    phase: str,
    receipts: Mapping[str, Mapping[str, Any]],
) -> None:
    phases = list(receipts)
    if phase in runtime.ACTION_PHASES:
        valid = runtime._expected_phase_sequence(  # noqa: SLF001
            [*phases, phase]
        )
    elif phase == "pre_mutation_cas_revalidated":
        mutation_index = runtime.FORWARD_PHASES.index(
            runtime.FIRST_APPLICATION_MUTATION_PHASE
        )
        valid = tuple(phases) == runtime.FORWARD_PHASES[:mutation_index]
    elif phase == "completed_revalidated":
        valid = tuple(phases) == runtime.FORWARD_PHASES
    elif phase == "rolled_back_revalidated":
        valid = (
            bool(phases)
            and phases[-1] == "rolled_back"
            and runtime._expected_phase_sequence(phases)  # noqa: SLF001
        )
    elif phase == "aborted_revalidated":
        valid = (
            bool(phases)
            and phases[-1] == "aborted"
            and runtime._expected_phase_sequence(phases)  # noqa: SLF001
        )
    else:
        _fail("production_release_host_action_phase_invalid")
    if not valid:
        _fail("production_release_host_action_context_invalid", phase)


def _validate_prior_receipts(
    *,
    intent: Mapping[str, Any],
    receipts: Mapping[str, Mapping[str, Any]],
) -> dict[str, Mapping[str, Any]]:
    validated: dict[str, Mapping[str, Any]] = {}
    for phase, receipt in receipts.items():
        if not isinstance(phase, str) or not isinstance(receipt, Mapping):
            _fail("production_release_host_action_context_invalid")
        try:
            if phase in runtime.ACTION_PHASES:
                rebound = runtime._validate_bound_action_receipt(  # noqa: SLF001
                    receipt,
                    intent=intent,
                    phase=phase,
                    receipts=validated,
                )
            else:
                rebound = runtime._validate_receipt(receipt)  # noqa: SLF001
                if rebound != runtime._internal_receipt(  # noqa: SLF001
                    phase=phase,
                    intent=intent,
                ):
                    _fail("production_release_host_action_context_invalid")
        except runtime.ProductionReleaseUpdateRuntimeError as exc:
            raise ProductionReleaseHostActionsError(
                "production_release_host_action_context_invalid"
            ) from exc
        validated[phase] = dict(rebound)
    return validated


def _receipt_base(
    *,
    phase: str,
    intent: Mapping[str, Any],
    receipts: Mapping[str, Mapping[str, Any]],
) -> dict[str, Any]:
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
        "prior_receipts_sha256": runtime._sha(  # noqa: SLF001
            runtime._canonical(receipts)  # noqa: SLF001
        ),
        "secret_material_recorded": False,
        "secret_digest_recorded": False,
    }


def _validate_evidence(
    *,
    phase: str,
    evidence: Any,
) -> dict[str, Any]:
    expected = runtime._ACTION_RECEIPT_EVIDENCE_FIELDS.get(  # noqa: SLF001
        phase
    )
    if (
        expected is None
        or not isinstance(evidence, Mapping)
        or set(evidence) != expected
    ):
        _fail("production_release_host_action_evidence_invalid", phase)
    try:
        runtime._canonical(evidence)  # noqa: SLF001
    except runtime.ProductionReleaseUpdateRuntimeError as exc:
        raise ProductionReleaseHostActionsError(
            "production_release_host_action_evidence_invalid",
            phase,
        ) from exc
    return dict(evidence)


def _publication_matches_intent(
    publication: Mapping[str, Any],
    intent: Mapping[str, Any],
) -> bool:
    plan = publication.get("plan")
    approval = publication.get("approval")
    if not isinstance(plan, Mapping) or not isinstance(approval, Mapping):
        return False
    return (
        publication.get("publication_sha256") == intent["publication_sha256"]
        and plan.get("plan_sha256") == intent["plan_sha256"]
        and approval.get("approval_sha256") == intent["approval_sha256"]
        and plan.get("predecessor_revision") == intent["predecessor_revision"]
        and plan.get("release_revision") == intent["release_revision"]
        and all(
            plan.get(name) == intent[name]
            for name in runtime._PLAN_PROJECTION_FIELDS  # noqa: SLF001
        )
    )


def _observe_release_host(
    *,
    phase: inventory.InventoryPhase,
    intent: Mapping[str, Any],
    action_phase: str,
) -> tuple[host_observer.HostObservationResult, Mapping[str, Any]]:
    try:
        observed = host_observer.observe_and_validate_release_host(
            phase=phase,
            predecessor_revision=str(intent["predecessor_revision"]),
            target_revision=str(intent["release_revision"]),
        )
        receipt = host_observer.validate_host_observation_receipt(observed.receipt)
        return observed, receipt
    except host_observer.ProductionReleaseHostObserverError as exc:
        raise ProductionReleaseHostActionsError(
            "production_release_host_action_observation_invalid",
            action_phase,
        ) from exc


class _ProductionHostActionEvidence:
    """Fixed-root production backend.

    Candidate validation and the post-fence zero-consumer observation are
    complete, read-only primitives today.  Every host mutation remains
    fail-closed until its owner-signed inventory format and crash-safe
    create-or-exact implementation are exposed as a reusable Stage-C API.
    """

    def evidence(
        self,
        phase: str,
        *,
        intent: Mapping[str, Any],
        receipts: Mapping[str, Mapping[str, Any]],
    ) -> Mapping[str, Any]:
        if phase == "candidate_validated":
            return self._candidate_validated(intent)
        if phase == "release_consumers_zeroed":
            return self._release_consumers_zeroed(
                intent=intent,
                receipts=receipts,
            )
        _fail(
            "production_release_host_action_primitive_unavailable",
            phase,
        )

    @staticmethod
    def _candidate_validated(
        intent: Mapping[str, Any],
    ) -> Mapping[str, Any]:
        try:
            with stage0.verify_stage0(
                expected_predecessor_activation_receipt_sha256=str(
                    intent["predecessor_current_receipt_sha256"]
                ),
            ) as bundle:
                bundle.assert_stable()
                if (
                    not _publication_matches_intent(
                        bundle.publication,
                        intent,
                    )
                    or bundle.predecessor_trust.get("trust_sha256")
                    != intent["predecessor_trust_sha256"]
                    or str(bundle.release_root) != intent["release_root"]
                    or bundle.builder_manifest.get("manifest_sha256")
                    != intent["whole_tree_manifest_sha256"]
                    or bundle.builder_receipt.get("manifest_sha256")
                    != intent["whole_tree_manifest_sha256"]
                ):
                    _fail(
                        "production_release_host_action_candidate_binding_invalid",
                        "candidate_validated",
                    )
                entries = bundle.builder_manifest.get("payload_entries")
                if not isinstance(entries, list):
                    _fail(
                        "production_release_host_action_candidate_binding_invalid",
                        "candidate_validated",
                    )
                regular_files = sum(
                    isinstance(entry, Mapping) and entry.get("kind") == "file"
                    for entry in entries
                )
                if regular_files <= 0:
                    _fail(
                        "production_release_host_action_candidate_binding_invalid",
                        "candidate_validated",
                    )
                evidence = {
                    "release_root": intent["release_root"],
                    "candidate_tree_sha256": intent["whole_tree_manifest_sha256"],
                    "candidate_seal_receipt_sha256": intent[
                        "candidate_seal_receipt_sha256"
                    ],
                    "builder_terminal_receipt_sha256": intent[
                        "builder_terminal_receipt_sha256"
                    ],
                    "verified_regular_file_count": regular_files,
                    "release_root_owned": True,
                    "release_tree_read_only": True,
                }
                bundle.assert_stable()
                return evidence
        except ProductionReleaseHostActionsError:
            raise
        except stage0.ProductionReleaseUpdateStage0Error as exc:
            raise ProductionReleaseHostActionsError(
                "production_release_host_action_candidate_invalid",
                "candidate_validated",
            ) from exc

    @staticmethod
    def _release_consumers_zeroed(
        *,
        intent: Mapping[str, Any],
        receipts: Mapping[str, Mapping[str, Any]],
    ) -> Mapping[str, Any]:
        pre_fence = receipts.get("pre_fence_cas_validated")
        if not isinstance(pre_fence, Mapping):
            _fail(
                "production_release_host_action_context_invalid",
                "release_consumers_zeroed",
            )
        observed, receipt = _observe_release_host(
            phase=inventory.InventoryPhase.PREDECESSOR_FENCED,
            intent=intent,
            action_phase="release_consumers_zeroed",
        )
        validation = receipt["validation"]
        if (
            validation["observed_process_count"] != 0
            or receipt["processes"]["selected_process_count"] != 0
            or validation["expected_unit_count"] != runtime.EXPECTED_CONSUMER_UNIT_COUNT
            or validation["execution_service_count"]
            != runtime.EXPECTED_SERVICE_UNIT_COUNT
            or validation["long_running_service_count"]
            != runtime.EXPECTED_LONG_RUNNING_SERVICE_UNIT_COUNT
            or validation["startup_oneshot_service_count"]
            != runtime.EXPECTED_STARTUP_ONESHOT_SERVICE_UNIT_COUNT
            or validation["triggered_oneshot_service_count"]
            != runtime.EXPECTED_TRIGGERED_ONESHOT_SERVICE_UNIT_COUNT
            or validation["oneshot_service_count"]
            != runtime.EXPECTED_ONESHOT_SERVICE_UNIT_COUNT
            or validation["trigger_unit_count"] != runtime.EXPECTED_TRIGGER_UNIT_COUNT
        ):
            _fail(
                "production_release_host_action_observation_invalid",
                "release_consumers_zeroed",
            )
        return {
            "host_observation_receipt_sha256": receipt["receipt_sha256"],
            "consumer_inventory_sha256": pre_fence["consumer_inventory_sha256"],
            "observed_consumer_process_count": 0,
            "observed_unknown_process_count": 0,
            "observed_mutable_pointer_process_count": 0,
            "need_daemon_reload_unit_count": 0,
            "all_release_consumers_zeroed": True,
        }


class ProductionReleaseHostActions:
    """Production ``ReleaseUpdateActions`` implementation.

    The public constructor accepts no dependencies.  Tests may use the
    deliberately private :meth:`_for_test` seam with a deterministic evidence
    backend.
    """

    def __init__(self) -> None:
        _require_production_host()
        self._backend: _HostActionEvidenceBackend = _ProductionHostActionEvidence()

    @classmethod
    def _for_test(
        cls,
        backend: _HostActionEvidenceBackend,
    ) -> ProductionReleaseHostActions:
        if backend is None or not callable(getattr(backend, "evidence", None)):
            _fail("production_release_host_action_test_backend_invalid")
        instance = object.__new__(cls)
        instance._backend = backend
        return instance

    def perform(
        self,
        phase: str,
        *,
        intent: Mapping[str, Any],
        receipts: Mapping[str, Mapping[str, Any]],
    ) -> Mapping[str, Any]:
        """Perform/revalidate one ordered phase and return its exact receipt."""

        if not isinstance(phase, str):
            _fail("production_release_host_action_phase_invalid")
        try:
            validated_intent = dict(runtime.validate_intent(intent))
        except runtime.ProductionReleaseUpdateRuntimeError as exc:
            raise ProductionReleaseHostActionsError(
                "production_release_host_action_intent_invalid"
            ) from exc
        if not isinstance(receipts, Mapping):
            _fail("production_release_host_action_context_invalid", phase)
        _validate_context_sequence(phase, receipts)
        validated_receipts = _validate_prior_receipts(
            intent=validated_intent,
            receipts=receipts,
        )
        copied_receipts = _copied_receipts(validated_receipts)
        evidence = _validate_evidence(
            phase=phase,
            evidence=self._backend.evidence(
                phase,
                intent=MappingProxyType(validated_intent),
                receipts=copied_receipts,
            ),
        )
        receipt = {
            **_receipt_base(
                phase=phase,
                intent=validated_intent,
                receipts=validated_receipts,
            ),
            **evidence,
        }
        try:
            validated = runtime._validate_bound_action_receipt(  # noqa: SLF001
                receipt,
                intent=validated_intent,
                phase=phase,
                receipts=validated_receipts,
            )
        except runtime.ProductionReleaseUpdateRuntimeError as exc:
            raise ProductionReleaseHostActionsError(
                "production_release_host_action_receipt_invalid",
                phase,
            ) from exc
        return dict(validated)


__all__ = [
    "ProductionReleaseHostActions",
    "ProductionReleaseHostActionsError",
]
