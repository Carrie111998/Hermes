#!/usr/bin/env python3
"""Initial legacy-to-strict release bootstrap terminal authority.

This module is deliberately bootstrap-only.  It observes the first strict
target with ``TARGET_ACTIVE`` and never calls or emulates the recurrent
Stage-C ``PREDECESSOR_ACTIVE`` state.  The owner's explicit legacy predecessor
and ``start_new_truth_epoch`` decision arrive already bound by the cutover
workspace; no authored text is inspected here.
"""

from __future__ import annotations

import argparse
import copy
import hashlib
import json
import re
from pathlib import Path
from typing import Any, Callable, Mapping

from scripts.canary import package_production_cutover_artifacts as package
from scripts.canary import production_cutover_unit_input_rotation as unit_rotation
from scripts.canary import production_release_builder_runtime as builder
from scripts.canary import production_release_consumer_inventory as inventory
from scripts.canary import production_release_host_observer as host_observer
from scripts.canary import production_release_unit_inputs_v4 as unit_inputs_v4
from scripts.canary import production_release_update_contract as update_contract
from scripts.canary import production_release_update_inputs as update_inputs


HOST_RECEIPT_SCHEMA = "muncho-production-initial-bootstrap-host.v1"
ACTIVATION_RECEIPT_SCHEMA = (
    "muncho-production-initial-bootstrap-activation.v1"
)
TERMINAL_ENVELOPE_SCHEMA = (
    "muncho-production-initial-bootstrap-terminal.v1"
)
TRUTH_MODE = "start_new_truth_epoch"
LEGACY_F5_PREDECESSOR_REVISION = (
    "f5ece3598efba6635e661aaa509d783fa2d802d8"
)

_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_REVISION = re.compile(r"^[0-9a-f]{40}$")


class ProductionInitialReleaseBootstrapError(RuntimeError):
    """Stable, secret-free bootstrap terminal failure."""


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
        raise ProductionInitialReleaseBootstrapError(
            "initial_bootstrap_json_invalid"
        ) from exc


def _sha(value: Any) -> str:
    return hashlib.sha256(_canonical(value)).hexdigest()


def _hashed(unsigned: Mapping[str, Any], digest_name: str) -> dict[str, Any]:
    payload = copy.deepcopy(dict(unsigned))
    return {**payload, digest_name: _sha(payload)}


def _revision_pair(predecessor: Any, target: Any) -> tuple[str, str]:
    if (
        not isinstance(predecessor, str)
        or _REVISION.fullmatch(predecessor) is None
        or not isinstance(target, str)
        or _REVISION.fullmatch(target) is None
        or predecessor == target
        or predecessor[:12] == target[:12]
    ):
        raise ProductionInitialReleaseBootstrapError(
            "initial_bootstrap_revision_invalid"
        )
    return predecessor, target


def _load_recurrent_unit_input_triplet(
    release_revision: str,
) -> Mapping[str, Any]:
    """Validate the exact persisted v4 authority used by first recurrence."""

    try:
        plan_raw = unit_rotation._read_exact(
            package.STAGED_UNIT_INPUT_PLAN_PATH,
            uid=0,
            gid=0,
            mode=0o400,
        )
        approval_raw = unit_rotation._read_exact(
            package.STAGED_UNIT_INPUT_APPROVAL_PATH,
            uid=0,
            gid=0,
            mode=0o400,
        )
        fixed_raw = unit_rotation._read_exact(
            package.FIXED_UNIT_INPUTS_PATH,
            uid=0,
            gid=0,
            mode=package.FIXED_UNIT_INPUTS_MODE,
        )
        plan_value = unit_rotation._decode(plan_raw)
        approval_value = unit_rotation._decode(approval_raw)
        fixed_value = unit_rotation._decode(fixed_raw, newline=True)
        provisional = {
            "release_revision": plan_value["release_revision"],
            "authority_plan_sha256": plan_value["plan_sha256"],
            "authority_approval_sha256": approval_value[
                "approval_sha256"
            ],
            "fixed_inputs_sha256": fixed_value["fixed_inputs_sha256"],
            "owner_subject_sha256": plan_value["owner_subject_sha256"],
            "owner_public_key_ed25519_hex": plan_value[
                "owner_public_key_ed25519_hex"
            ],
            "owner_key_id": plan_value["owner_key_id"],
        }
        triplet = unit_rotation._release_triplet(
            uid=0,
            gid=0,
            trusted_predecessor=provisional,
        )
    except (
        KeyError,
        OSError,
        TypeError,
        ValueError,
        unit_rotation.UnitInputRotationError,
    ) as exc:
        raise ProductionInitialReleaseBootstrapError(
            "initial_bootstrap_unit_input_triplet_invalid"
        ) from exc
    if (
        triplet.authority_version != "v4"
        or triplet.revision != release_revision
        or triplet.plan["schema"] != unit_inputs_v4.PLAN_SCHEMA
        or triplet.approval["schema"] != unit_inputs_v4.APPROVAL_SCHEMA
        or triplet.fixed_inputs["schema"]
        != unit_inputs_v4.FIXED_INPUTS_SCHEMA
    ):
        raise ProductionInitialReleaseBootstrapError(
            "initial_bootstrap_unit_input_triplet_invalid"
        )
    return {
        "plan": copy.deepcopy(dict(triplet.plan)),
        "approval": copy.deepcopy(dict(triplet.approval)),
        "fixed_inputs": copy.deepcopy(dict(triplet.fixed_inputs)),
        "plan_path": str(package.STAGED_UNIT_INPUT_PLAN_PATH),
        "approval_path": str(package.STAGED_UNIT_INPUT_APPROVAL_PATH),
        "fixed_inputs_path": str(package.FIXED_UNIT_INPUTS_PATH),
        "plan_file_sha256": hashlib.sha256(plan_raw).hexdigest(),
        "approval_file_sha256": hashlib.sha256(approval_raw).hexdigest(),
        "fixed_inputs_file_sha256": hashlib.sha256(fixed_raw).hexdigest(),
    }


def _catalog_projection(
    predecessor_revision: str,
    release_revision: str,
) -> tuple[list[dict[str, Any]], Mapping[str, Any]]:
    consumer_set = update_inputs.build_release_consumer_set(
        predecessor_revision=predecessor_revision,
        release_revision=release_revision,
    )
    try:
        trusted = update_inputs.validate_release_consumer_set(
            consumer_set,
            predecessor_revision=predecessor_revision,
            release_revision=release_revision,
        )
        projection = copy.deepcopy(list(trusted["consumers"]))
    except (KeyError, TypeError, ValueError, RuntimeError) as exc:
        raise ProductionInitialReleaseBootstrapError(
            "initial_bootstrap_consumer_catalog_invalid"
        ) from exc
    if len(projection) != inventory.EXPECTED_UNIT_COUNT:
        raise ProductionInitialReleaseBootstrapError(
            "initial_bootstrap_consumer_catalog_invalid"
        )
    return projection, copy.deepcopy(dict(trusted))


def _runtime_safety_plan(
    *,
    predecessor_revision: str,
    release_revision: str,
    release_consumer_set_sha256: str,
    consumer_catalog_sha256: str,
    builder_fn: Callable[..., Mapping[str, Any]] | None,
    validator_fn: Callable[..., Mapping[str, Any]] | None,
) -> Mapping[str, Any]:
    if builder_fn is None or validator_fn is None:
        try:
            from scripts.canary import production_release_runtime_safety
        except ImportError as exc:
            raise ProductionInitialReleaseBootstrapError(
                "initial_bootstrap_runtime_safety_unavailable"
            ) from exc
        builder_fn = production_release_runtime_safety.build_runtime_safety_plan
        validator_fn = production_release_runtime_safety.validate_runtime_safety_plan
    try:
        value = builder_fn(
            predecessor_revision=predecessor_revision,
            release_revision=release_revision,
            release_consumer_set_sha256=release_consumer_set_sha256,
            consumer_catalog_sha256=consumer_catalog_sha256,
        )
        trusted = validator_fn(
            value,
            predecessor_revision=predecessor_revision,
            release_revision=release_revision,
            release_consumer_set={
                "consumer_set_sha256": release_consumer_set_sha256,
                "catalog_sha256": consumer_catalog_sha256,
            },
        )
    except (KeyError, TypeError, ValueError, RuntimeError) as exc:
        raise ProductionInitialReleaseBootstrapError(
            "initial_bootstrap_runtime_safety_invalid"
        ) from exc
    required = {
        "service_operation_classes",
        "protected_voice_service_units",
        "public_ingress_service_units",
        "precommit_long_running_service_units",
        "postcommit_public_start_order",
        "startup_oneshot_service_units",
        "precommit_disabled_trigger_units",
        "postcommit_enabled_trigger_units",
        "voice_guard_probes",
        "precommit_health_probes",
        "postcommit_health_probes",
        "session_drain",
        "external_ingress_gate",
        "runtime_safety_plan_sha256",
    }
    if (
        not isinstance(trusted, Mapping)
        or not required.issubset(trusted)
        or trusted.get("predecessor_revision") != predecessor_revision
        or trusted.get("release_revision") != release_revision
        or trusted.get("release_consumer_set_sha256")
        != release_consumer_set_sha256
        or trusted.get("consumer_catalog_sha256") != consumer_catalog_sha256
        or trusted.get("public_ingress_service_units")
        != [
            "muncho-discord-connector.service",
            "hermes-cloud-gateway.service",
        ]
        or trusted.get("postcommit_public_start_order")
        != [
            "muncho-discord-connector.service",
            "hermes-cloud-gateway.service",
        ]
        or trusted.get("protected_voice_service_units")
        != ["hermes-cloud-gateway.service"]
        or len(trusted.get("service_operation_classes", ()))
        != inventory.EXPECTED_LONG_RUNNING_SERVICE_COUNT
        or len(trusted.get("precommit_long_running_service_units", ()))
        != inventory.EXPECTED_LONG_RUNNING_SERVICE_COUNT - 2
        or len(trusted.get("startup_oneshot_service_units", ()))
        != inventory.EXPECTED_STARTUP_ONESHOT_SERVICE_COUNT
        or len(trusted.get("precommit_disabled_trigger_units", ()))
        != inventory.EXPECTED_TRIGGER_UNIT_COUNT
        or trusted.get("postcommit_enabled_trigger_units")
        != trusted.get("precommit_disabled_trigger_units")
        or len(trusted.get("voice_guard_probes", ())) != 1
        or len(trusted.get("precommit_health_probes", ())) != 8
        or len(trusted.get("postcommit_health_probes", ())) != 3
        or _SHA256.fullmatch(
            str(trusted.get("runtime_safety_plan_sha256", ""))
        )
        is None
    ):
        raise ProductionInitialReleaseBootstrapError(
            "initial_bootstrap_runtime_safety_invalid"
        )
    return copy.deepcopy(dict(trusted))


def _target_active_state(
    observations: tuple[inventory.UnitObservation, ...],
    safety: Mapping[str, Any],
) -> Mapping[str, Any]:
    by_name = {item.name: item for item in observations}
    long_running = {
        *safety["precommit_long_running_service_units"],
        *safety["public_ingress_service_units"],
    }
    startup = set(safety["startup_oneshot_service_units"])
    triggers = set(safety["postcommit_enabled_trigger_units"])
    if (
        len(by_name) < inventory.EXPECTED_UNIT_COUNT
        or len(long_running) != inventory.EXPECTED_LONG_RUNNING_SERVICE_COUNT
        or len(startup) != inventory.EXPECTED_STARTUP_ONESHOT_SERVICE_COUNT
        or len(triggers) != inventory.EXPECTED_TRIGGER_UNIT_COUNT
        or any(name not in by_name for name in long_running | startup | triggers)
    ):
        raise ProductionInitialReleaseBootstrapError(
            "initial_bootstrap_target_state_invalid"
        )
    if any(
        by_name[name].properties.get("ActiveState") != "active"
        or by_name[name].properties.get("SubState") != "running"
        for name in long_running
    ):
        raise ProductionInitialReleaseBootstrapError(
            "initial_bootstrap_long_running_service_unready"
        )
    if any(
        by_name[name].properties.get("ActiveState") != "active"
        or by_name[name].properties.get("SubState") != "exited"
        for name in startup
    ):
        raise ProductionInitialReleaseBootstrapError(
            "initial_bootstrap_startup_oneshot_unready"
        )
    if any(
        by_name[name].properties.get("ActiveState") != "active"
        or by_name[name].properties.get("UnitFileState") != "enabled"
        for name in triggers
    ):
        raise ProductionInitialReleaseBootstrapError(
            "initial_bootstrap_trigger_unready"
        )
    return {
        "long_running_service_count": len(long_running),
        "startup_oneshot_service_count": len(startup),
        "enabled_trigger_unit_count": len(triggers),
        "long_running_services_sha256": _sha(sorted(long_running)),
        "startup_oneshot_services_sha256": _sha(sorted(startup)),
        "enabled_trigger_units_sha256": _sha(sorted(triggers)),
    }


def collect_bootstrap_host_receipt(
    *,
    predecessor_revision: str,
    release_revision: str,
    release_verifier: Callable[..., Mapping[str, Any]] = (
        builder.verify_published_release
    ),
    host_observer_fn: Callable[..., Any] = (
        host_observer.observe_and_validate_release_host
    ),
    runtime_safety_builder: Callable[..., Mapping[str, Any]] | None = None,
    runtime_safety_validator: Callable[..., Mapping[str, Any]] | None = None,
    unit_input_triplet_loader: Callable[[str], Mapping[str, Any]] = (
        _load_recurrent_unit_input_triplet
    ),
    cutover_terminal_loader: Callable[[str], Mapping[str, Any]] | None = None,
) -> Mapping[str, Any]:
    """Collect the first strict target using only ``TARGET_ACTIVE``."""

    predecessor, target = _revision_pair(
        predecessor_revision, release_revision
    )
    release_root = Path(update_contract.expected_release_root(target))
    try:
        publication = release_verifier(release_root, revision=target)
        package_manifest_path = (
            release_root / "ops/muncho/cutover/artifacts/manifest.json"
        )
        raw = package_manifest_path.read_bytes()
        package_manifest = json.loads(raw.decode("utf-8", errors="strict"))
        if raw != _canonical(package_manifest) + b"\n":
            raise ValueError("cutover package manifest is not canonical")
        unit_inputs = package._unit_inputs(
            package_manifest["unit_inputs"], revision=target
        )
        if unit_inputs["schema"] != package.UNIT_INPUT_SCHEMA_V4:
            raise ValueError("fixed unit inputs are not v4")
        package_verified = package.verify_release_artifacts(
            release_root,
            target,
            release_address=release_root,
            unit_inputs=unit_inputs,
            owner_gate_receipt_public_key_id=unit_inputs[
                "owner_gate_receipt_public_key_id"
            ],
        )
    except (OSError, UnicodeError, ValueError, RuntimeError, KeyError) as exc:
        raise ProductionInitialReleaseBootstrapError(
            "initial_bootstrap_release_invalid"
        ) from exc
    try:
        unit_input_authority = copy.deepcopy(
            dict(unit_input_triplet_loader(target))
        )
        unit_plan = unit_input_authority["plan"]
        unit_approval = unit_input_authority["approval"]
        fixed_inputs = unit_input_authority["fixed_inputs"]
        projected_inputs = unit_inputs_v4.project_fixed_inputs_to_cutover_v4(
            fixed_inputs
        )
        if (
            unit_plan["schema"] != unit_inputs_v4.PLAN_SCHEMA
            or unit_approval["schema"] != unit_inputs_v4.APPROVAL_SCHEMA
            or fixed_inputs["schema"] != unit_inputs_v4.FIXED_INPUTS_SCHEMA
            or unit_plan["release_revision"] != target
            or unit_approval["release_revision"] != target
            or fixed_inputs["release_revision"] != target
            or unit_plan["plan_sha256"]
            != fixed_inputs["unit_input_authority_plan_sha256"]
            or unit_approval["approval_sha256"]
            != fixed_inputs["unit_input_authority_approval_sha256"]
            or projected_inputs != unit_inputs
            or unit_input_authority["plan_path"]
            != str(package.STAGED_UNIT_INPUT_PLAN_PATH)
            or unit_input_authority["approval_path"]
            != str(package.STAGED_UNIT_INPUT_APPROVAL_PATH)
            or unit_input_authority["fixed_inputs_path"]
            != str(package.FIXED_UNIT_INPUTS_PATH)
            or any(
                _SHA256.fullmatch(
                    str(unit_input_authority[name])
                )
                is None
                for name in (
                    "plan_file_sha256",
                    "approval_file_sha256",
                    "fixed_inputs_file_sha256",
                )
            )
            or set(unit_input_authority)
            != {
                "plan",
                "approval",
                "fixed_inputs",
                "plan_path",
                "approval_path",
                "fixed_inputs_path",
                "plan_file_sha256",
                "approval_file_sha256",
                "fixed_inputs_file_sha256",
            }
        ):
            raise ValueError("recurrent unit-input authority drifted")
    except (
        KeyError,
        TypeError,
        ValueError,
        RuntimeError,
    ) as exc:
        raise ProductionInitialReleaseBootstrapError(
            "initial_bootstrap_unit_input_triplet_invalid"
        ) from exc
    catalog_projection, consumer_set = _catalog_projection(
        predecessor, target
    )
    consumer_set_sha256 = str(consumer_set["consumer_set_sha256"])
    catalog_sha256 = str(consumer_set["catalog_sha256"])
    safety = _runtime_safety_plan(
        predecessor_revision=predecessor,
        release_revision=target,
        release_consumer_set_sha256=consumer_set_sha256,
        consumer_catalog_sha256=catalog_sha256,
        builder_fn=runtime_safety_builder,
        validator_fn=runtime_safety_validator,
    )
    try:
        observation = host_observer_fn(
            phase=inventory.InventoryPhase.TARGET_ACTIVE,
            predecessor_revision=predecessor,
            target_revision=target,
        )
        trusted_observation = host_observer.validate_host_observation_receipt(
            observation.receipt
        )
    except (ValueError, RuntimeError, AttributeError) as exc:
        raise ProductionInitialReleaseBootstrapError(
            "initial_bootstrap_target_observation_invalid"
        ) from exc
    target_state = _target_active_state(
        tuple(observation.unit_observations), safety
    )
    if cutover_terminal_loader is None:
        def cutover_terminal_loader(expected_revision: str) -> Mapping[str, Any]:
            from gateway import canonical_writer_production_cutover as cutover

            plan = cutover.CutoverPlan.from_mapping(
                cutover._load_staged_json(cutover.STAGED_CUTOVER_PLAN_PATH)
            )
            if plan.value["release_revision"] != expected_revision:
                raise ValueError("staged cutover revision drifted")
            terminal = cutover._last(
                cutover.RootCutoverJournal().load(plan.sha256), "terminal"
            )
            if terminal is None:
                raise ValueError("cutover terminal is missing")
            return copy.deepcopy(dict(terminal.value["evidence"]))
    try:
        cutover_terminal = copy.deepcopy(
            dict(cutover_terminal_loader(target))
        )
        unsigned_terminal = {
            key: item
            for key, item in cutover_terminal.items()
            if key != "receipt_sha256"
        }
        if (
            cutover_terminal.get("plan_sha256") is None
            or cutover_terminal.get("alias_projection_package_sha256")
            != package_verified["source"][
                "alias_projection_package_sha256"
            ]
            or cutover_terminal.get("receipt_sha256")
            != _sha(unsigned_terminal)
        ):
            raise ValueError("cutover terminal drifted")
    except (KeyError, TypeError, ValueError, RuntimeError) as exc:
        raise ProductionInitialReleaseBootstrapError(
            "initial_bootstrap_cutover_terminal_invalid"
        ) from exc
    immutable_paths = sorted({
        item["fragment_path"]
        for item in catalog_projection
    } | {
        path
        for item in catalog_projection
        for path in item["drop_in_paths"]
    })
    try:
        trust_anchors = {
            "writer_capability_public_key_id": unit_inputs[
                "writer_capability_public_key_id"
            ],
            "owner_gate_receipt_public_key_id": unit_inputs[
                "owner_gate_receipt_public_key_id"
            ],
            "discord_edge_receipt_public_key_id": unit_inputs[
                "discord_edge_receipt_public_key_id"
            ],
            "operational_edge_key_foundation_sha256": unit_inputs[
                "operational_edge_key_foundation_sha256"
            ],
            "connector_unit_template_sha256": package_verified["source"]
            ["connector_unit_template_sha256"],
            "connector_config_template_sha256": package_verified["source"]
            ["connector_config_template_sha256"],
            "gateway_connector_drop_in_sha256": package_verified["source"]
            ["gateway_connector_drop_in_sha256"],
            "alias_projection_package_sha256": package_verified["source"]
            ["alias_projection_package_sha256"],
        }
    except (KeyError, TypeError) as exc:
        raise ProductionInitialReleaseBootstrapError(
            "initial_bootstrap_trust_anchor_invalid"
        ) from exc
    unsigned = {
        "schema": HOST_RECEIPT_SCHEMA,
        "legacy_predecessor_revision": predecessor,
        "release_revision": target,
        "release_root": str(release_root),
        "release_publication_receipt_sha256": publication["receipt_sha256"],
        "release_manifest_sha256": publication["manifest_sha256"],
        "release_payload_tree_sha256": publication["payload_tree_sha256"],
        "root_uid": publication["root_uid"],
        "root_gid": publication["root_gid"],
        "root_mode": publication["root_mode"],
        "root_xattrs": publication["root_xattrs"],
        "release_create_only": True,
        "release_root_owned": True,
        "release_read_only": True,
        "cutover_unit_inputs_schema": unit_inputs["schema"],
        "cutover_unit_inputs_sha256": _sha(unit_inputs),
        "recurrent_fixed_inputs_schema": fixed_inputs["schema"],
        "fixed_unit_inputs_sha256": fixed_inputs["fixed_inputs_sha256"],
        "unit_input_authority": unit_input_authority,
        "cutover_artifact_manifest_sha256": package_verified[
            "manifest_sha256"
        ],
        "release_consumer_set_sha256": consumer_set_sha256,
        "consumer_catalog_sha256": catalog_sha256,
        "release_consumer_set": consumer_set,
        "consumer_catalog": catalog_projection,
        "consumer_unit_count": len(catalog_projection),
        "immutable_unit_paths": immutable_paths,
        "immutable_unit_paths_sha256": _sha(immutable_paths),
        "runtime_safety_plan": safety,
        "cutover_terminal_receipt": cutover_terminal,
        "target_active_observation": copy.deepcopy(dict(trusted_observation)),
        "target_active_state": target_state,
        "trust_anchors": trust_anchors,
        "predecessor_active_observer_called": False,
        "secret_material_recorded": False,
        "secret_digest_recorded": False,
    }
    if (
        unsigned["consumer_unit_count"] != inventory.EXPECTED_UNIT_COUNT
        or unsigned["root_uid"] != 0
        or unsigned["root_gid"] != 0
        or unsigned["root_mode"] != "0555"
        or unsigned["root_xattrs"] != []
        or trusted_observation["phase"]
        != inventory.InventoryPhase.TARGET_ACTIVE.value
    ):
        raise ProductionInitialReleaseBootstrapError(
            "initial_bootstrap_host_receipt_invalid"
        )
    return _hashed(unsigned, "receipt_sha256")


def build_terminal_activation_receipt(
    *,
    legacy_predecessor_revision: str,
    release_revision: str,
    freeze_plan: Mapping[str, Any],
    freeze_approval: Mapping[str, Any],
    cutover_plan: Mapping[str, Any],
    cutover_terminal_receipt: Mapping[str, Any],
    convergence_receipt: Mapping[str, Any],
    workflow_receipt: Mapping[str, Any],
    host_receipt: Mapping[str, Any],
) -> Mapping[str, Any]:
    predecessor, target = _revision_pair(
        legacy_predecessor_revision, release_revision
    )
    try:
        decision = freeze_plan["cutover_authority"]["legacy_truth_decision"]
        unit_authority = host_receipt["unit_input_authority"]
        unit_plan = unit_authority["plan"]
        unit_approval = unit_authority["approval"]
        fixed_inputs = unit_authority["fixed_inputs"]
        if (
            freeze_plan["release_revision"] != target
            or cutover_plan["release_revision"] != target
            or decision["mode"] != TRUTH_MODE
            or decision["accepted_event_ids"] != []
            or decision["accepted_event_receipts"] != []
            or host_receipt["schema"] != HOST_RECEIPT_SCHEMA
            or host_receipt["legacy_predecessor_revision"] != predecessor
            or host_receipt["release_revision"] != target
            or host_receipt["consumer_unit_count"]
            != inventory.EXPECTED_UNIT_COUNT
            or unit_plan["schema"] != unit_inputs_v4.PLAN_SCHEMA
            or unit_approval["schema"] != unit_inputs_v4.APPROVAL_SCHEMA
            or fixed_inputs["schema"] != unit_inputs_v4.FIXED_INPUTS_SCHEMA
            or unit_plan["release_revision"] != target
            or unit_approval["release_revision"] != target
            or fixed_inputs["release_revision"] != target
            or unit_plan["plan_sha256"]
            != fixed_inputs["unit_input_authority_plan_sha256"]
            or unit_approval["approval_sha256"]
            != fixed_inputs["unit_input_authority_approval_sha256"]
            or host_receipt["fixed_unit_inputs_sha256"]
            != fixed_inputs["fixed_inputs_sha256"]
            or any(
                unit_plan[name] != freeze_plan[name]
                for name in (
                    "owner_subject_sha256",
                    "owner_public_key_ed25519_hex",
                    "owner_key_id",
                )
            )
            or cutover_terminal_receipt["alias_projection_package_sha256"]
            != host_receipt["trust_anchors"]
            ["alias_projection_package_sha256"]
        ):
            raise ValueError("bootstrap lineage drifted")
        unsigned = {
            "schema": ACTIVATION_RECEIPT_SCHEMA,
            "legacy_predecessor_revision": predecessor,
            "release_revision": target,
            "truth_mode": TRUTH_MODE,
            "legacy_truth_decision_sha256": decision["decision_sha256"],
            "truth_epoch_sha256": decision["truth_epoch_sha256"],
            "freeze_plan_sha256": freeze_plan["plan_sha256"],
            "freeze_approval_sha256": freeze_approval["approval_sha256"],
            "cutover_plan_sha256": cutover_plan["plan_sha256"],
            "cutover_terminal_receipt_sha256": cutover_terminal_receipt[
                "receipt_sha256"
            ],
            "convergence_receipt_sha256": convergence_receipt[
                "receipt_sha256"
            ],
            "workflow_receipt_sha256": workflow_receipt["receipt_sha256"],
            "host_receipt_sha256": host_receipt["receipt_sha256"],
            "fixed_unit_inputs_sha256": host_receipt[
                "fixed_unit_inputs_sha256"
            ],
            "unit_input_authority_plan_sha256": unit_plan["plan_sha256"],
            "unit_input_authority_approval_sha256": unit_approval[
                "approval_sha256"
            ],
            "unit_input_authority_fixed_inputs_sha256": fixed_inputs[
                "fixed_inputs_sha256"
            ],
            "release_publication_receipt_sha256": host_receipt[
                "release_publication_receipt_sha256"
            ],
            "release_payload_tree_sha256": host_receipt[
                "release_payload_tree_sha256"
            ],
            "release_consumer_set_sha256": host_receipt[
                "release_consumer_set_sha256"
            ],
            "consumer_catalog_sha256": host_receipt[
                "consumer_catalog_sha256"
            ],
            "consumer_unit_count": host_receipt["consumer_unit_count"],
            "immutable_unit_paths_sha256": host_receipt[
                "immutable_unit_paths_sha256"
            ],
            "runtime_safety_plan_sha256": host_receipt[
                "runtime_safety_plan"
            ]["runtime_safety_plan_sha256"],
            "target_active_observation_receipt_sha256": host_receipt[
                "target_active_observation"
            ]["receipt_sha256"],
            "alias_projection_activation_authority_sha256": (
                cutover_terminal_receipt[
                    "alias_projection_activation_authority_sha256"
                ]
            ),
            "alias_projection_activation_receipt_sha256": (
                cutover_terminal_receipt[
                    "alias_projection_activation_receipt_sha256"
                ]
            ),
            "trust_anchors": copy.deepcopy(host_receipt["trust_anchors"]),
            "owner_subject_sha256": freeze_plan["owner_subject_sha256"],
            "owner_public_key_ed25519_hex": freeze_plan[
                "owner_public_key_ed25519_hex"
            ],
            "owner_key_id": freeze_plan["owner_key_id"],
            "target_active": True,
            "predecessor_active_observer_called": False,
            "secret_material_recorded": False,
            "secret_digest_recorded": False,
        }
    except (KeyError, TypeError, ValueError) as exc:
        raise ProductionInitialReleaseBootstrapError(
            "initial_bootstrap_activation_lineage_invalid"
        ) from exc
    return _hashed(unsigned, "receipt_sha256")


def build_terminal_envelope(
    *,
    workflow_receipt: Mapping[str, Any],
    activation_receipt: Mapping[str, Any],
) -> Mapping[str, Any]:
    try:
        predecessor_trust = update_contract.build_predecessor_trust(
            release_revision=activation_receipt["release_revision"],
            authority_plan_sha256=activation_receipt[
                "unit_input_authority_plan_sha256"
            ],
            authority_approval_sha256=activation_receipt[
                "unit_input_authority_approval_sha256"
            ],
            fixed_inputs_sha256=activation_receipt[
                "fixed_unit_inputs_sha256"
            ],
            activation_receipt_sha256=activation_receipt[
                "receipt_sha256"
            ],
            owner_subject_sha256=activation_receipt[
                "owner_subject_sha256"
            ],
            owner_public_key_ed25519_hex=activation_receipt[
                "owner_public_key_ed25519_hex"
            ],
            owner_key_id=activation_receipt["owner_key_id"],
        )
    except (KeyError, TypeError, RuntimeError) as exc:
        raise ProductionInitialReleaseBootstrapError(
            "initial_bootstrap_predecessor_trust_invalid"
        ) from exc
    unsigned = {
        "schema": TERMINAL_ENVELOPE_SCHEMA,
        "release_revision": activation_receipt["release_revision"],
        "legacy_predecessor_revision": activation_receipt[
            "legacy_predecessor_revision"
        ],
        "workflow_receipt": copy.deepcopy(dict(workflow_receipt)),
        "activation_receipt": copy.deepcopy(dict(activation_receipt)),
        "predecessor_trust": copy.deepcopy(dict(predecessor_trust)),
        "terminal": True,
        "secret_material_recorded": False,
        "secret_digest_recorded": False,
    }
    return _hashed(unsigned, "receipt_sha256")


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("collect-target-active",))
    parser.add_argument("--revision", required=True)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    receipt = collect_bootstrap_host_receipt(
        predecessor_revision=LEGACY_F5_PREDECESSOR_REVISION,
        release_revision=args.revision,
    )
    print(_canonical(receipt).decode("utf-8"))
    return 0


__all__ = [
    "ACTIVATION_RECEIPT_SCHEMA",
    "HOST_RECEIPT_SCHEMA",
    "ProductionInitialReleaseBootstrapError",
    "TERMINAL_ENVELOPE_SCHEMA",
    "build_terminal_activation_receipt",
    "build_terminal_envelope",
    "collect_bootstrap_host_receipt",
    "main",
]


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except ProductionInitialReleaseBootstrapError as exc:
        print(str(exc), file=__import__("sys").stderr)
        raise SystemExit(1) from None
