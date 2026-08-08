from __future__ import annotations

import hashlib
import os
from types import SimpleNamespace

import pytest

from scripts.canary import production_initial_release_bootstrap as bootstrap
from scripts.canary import production_release_consumer_inventory as inventory
from scripts.canary import production_release_update_contract as update_contract
from scripts.canary import production_release_update_inputs as update_inputs
from tests.scripts.canary.test_package_production_cutover_artifacts import (
    _unit_inputs_v4,
)


PREDECESSOR = "f" * 40
TARGET = "a" * 40


def _safety_plan() -> dict:
    catalog = inventory.expected_consumer_catalog()
    long_running = sorted(
        name
        for name, spec in catalog.items()
        if spec.activation_class == inventory.ACTIVATION_CLASS_LONG_RUNNING
    )
    public = [
        "muncho-discord-connector.service",
        "hermes-cloud-gateway.service",
    ]
    triggers = sorted(
        name for name, spec in catalog.items() if spec.kind in {"socket", "timer"}
    )
    return {
        "precommit_long_running_service_units": sorted(
            set(long_running).difference(public)
        ),
        "public_ingress_service_units": public,
        "startup_oneshot_service_units": [
            "muncho-canonical-writer-phase-b-readiness.service"
        ],
        "precommit_disabled_trigger_units": triggers,
        "postcommit_enabled_trigger_units": triggers,
    }


def _target_observations(*, connector_active: bool = True):
    safety = _safety_plan()
    long_running = {
        *safety["precommit_long_running_service_units"],
        *safety["public_ingress_service_units"],
    }
    startup = set(safety["startup_oneshot_service_units"])
    triggers = set(safety["postcommit_enabled_trigger_units"])
    result = []
    for name in inventory.expected_consumer_catalog():
        if name in long_running:
            active = not (
                name == "muncho-discord-connector.service"
                and not connector_active
            )
            properties = {
                "ActiveState": "active" if active else "inactive",
                "SubState": "running" if active else "dead",
                "UnitFileState": "enabled",
            }
        elif name in startup:
            properties = {
                "ActiveState": "active",
                "SubState": "exited",
                "UnitFileState": "enabled",
            }
        elif name in triggers:
            properties = {
                "ActiveState": "active",
                "SubState": "waiting",
                "UnitFileState": "enabled",
            }
        else:
            properties = {
                "ActiveState": "inactive",
                "SubState": "dead",
                "UnitFileState": "static",
            }
        result.append(
            inventory.UnitObservation(
                name=name,
                properties=properties,
                files={},
            )
        )
    return tuple(result)


def test_target_active_state_requires_exact_postcommit_cohorts() -> None:
    result = bootstrap._target_active_state(
        _target_observations(), _safety_plan()
    )

    assert result["long_running_service_count"] == 18
    assert result["startup_oneshot_service_count"] == 1
    assert result["enabled_trigger_unit_count"] == 30


def test_target_active_state_rejects_public_connector_stopped() -> None:
    with pytest.raises(
        bootstrap.ProductionInitialReleaseBootstrapError,
        match="long_running_service_unready",
    ):
        bootstrap._target_active_state(
            _target_observations(connector_active=False), _safety_plan()
        )


def test_catalog_projection_is_the_exact_recurrent_consumer_set() -> None:
    projection, consumer_set = bootstrap._catalog_projection(
        PREDECESSOR, TARGET
    )
    expected = update_inputs.build_release_consumer_set(
        predecessor_revision=PREDECESSOR,
        release_revision=TARGET,
    )

    assert consumer_set == expected
    assert projection == expected["consumers"]
    assert len(projection) == inventory.EXPECTED_UNIT_COUNT


def test_loader_accepts_exact_persisted_recurrent_v4_triplet(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from tests.scripts.canary import (
        test_production_cutover_unit_input_rotation as rotation_tests,
    )

    target = "b" * 40
    _private, _predecessor, trusted, documents = (
        rotation_tests._release_rotation_state(
            monkeypatch,
            tmp_path,
            now=1_900_000_000,
            target_revision=target,
        )
    )
    prepared = rotation_tests._prepare_release(
        documents,
        trusted,
        now=1_900_000_000,
    )
    rotation_tests._finalize_release(documents, trusted, prepared)

    exact_reader = bootstrap.unit_rotation._read_exact

    def read_as_test_owner(path, *, uid, gid, mode, maximum=1024 * 1024):
        assert uid == 0
        assert gid == 0
        return exact_reader(
            path,
            uid=os.geteuid(),
            gid=os.getegid(),
            mode=mode,
            maximum=maximum,
        )

    monkeypatch.setattr(bootstrap.unit_rotation, "_read_exact", read_as_test_owner)
    monkeypatch.setattr(
        bootstrap.unit_rotation,
        "_release_require_live_triplet_no_extended_metadata",
        lambda **_kwargs: None,
    )

    loaded = bootstrap._load_recurrent_unit_input_triplet(target)

    assert loaded["plan"] == documents["plan"]
    assert loaded["approval"] == documents["approval"]
    assert loaded["fixed_inputs"] == documents["fixed"]
    assert loaded["fixed_inputs"]["fixed_inputs_sha256"] == (
        documents["fixed"]["fixed_inputs_sha256"]
    )
    assert loaded["fixed_inputs_file_sha256"] != (
        loaded["fixed_inputs"]["fixed_inputs_sha256"]
    )


def test_collect_host_receipt_binds_immutable_target_and_full_catalog(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    release = tmp_path / f"hermes-agent-{TARGET[:12]}"
    cutover_inputs = _unit_inputs_v4("1" * 64)
    manifest_path = release / "ops/muncho/cutover/artifacts/manifest.json"
    manifest_path.parent.mkdir(parents=True)
    manifest_path.write_bytes(
        bootstrap._canonical({
            "unit_inputs": cutover_inputs,
        }) + b"\n"
    )
    monkeypatch.setattr(
        bootstrap.update_contract,
        "expected_release_root",
        lambda _revision: str(release),
    )
    monkeypatch.setattr(
        bootstrap.package,
        "verify_release_artifacts",
        lambda *_args, **_kwargs: {
            "manifest_sha256": "2" * 64,
            "source": {
                "alias_projection_package_sha256": "3" * 64,
                "connector_unit_template_sha256": "c" * 64,
                "connector_config_template_sha256": "d" * 64,
                "gateway_connector_drop_in_sha256": "e" * 64,
            },
        },
    )
    monkeypatch.setattr(
        bootstrap.host_observer,
        "validate_host_observation_receipt",
        lambda _value: {
            "phase": inventory.InventoryPhase.TARGET_ACTIVE.value,
            "receipt_sha256": "4" * 64,
        },
    )
    monkeypatch.setattr(
        bootstrap.unit_inputs_v4,
        "project_fixed_inputs_to_cutover_v4",
        lambda _fixed: cutover_inputs,
    )

    safety = _safety_plan()
    safety.update({
        "predecessor_revision": PREDECESSOR,
        "release_revision": TARGET,
        "service_operation_classes": {
            name: "test_exact_operation_class"
            for name in {
                *safety["precommit_long_running_service_units"],
                *safety["public_ingress_service_units"],
            }
        },
        "protected_voice_service_units": [
            "hermes-cloud-gateway.service"
        ],
        "postcommit_public_start_order": safety[
            "public_ingress_service_units"
        ],
        "voice_guard_probes": [{}],
        "precommit_health_probes": [{} for _ in range(8)],
        "postcommit_health_probes": [{} for _ in range(3)],
        "session_drain": {},
        "external_ingress_gate": {},
        "runtime_safety_plan_sha256": "5" * 64,
    })

    def safety_builder(**kwargs):
        return {
            **safety,
            "release_consumer_set_sha256": kwargs[
                "release_consumer_set_sha256"
            ],
            "consumer_catalog_sha256": kwargs[
                "consumer_catalog_sha256"
            ],
        }

    terminal_unsigned = {
        "plan_sha256": "6" * 64,
        "alias_projection_package_sha256": "3" * 64,
        "alias_projection_activation_authority_sha256": "7" * 64,
        "alias_projection_activation_receipt_sha256": "8" * 64,
    }
    unit_plan = {
        "schema": bootstrap.unit_inputs_v4.PLAN_SCHEMA,
        "release_revision": TARGET,
        "plan_sha256": "f" * 64,
    }
    unit_approval = {
        "schema": bootstrap.unit_inputs_v4.APPROVAL_SCHEMA,
        "release_revision": TARGET,
        "approval_sha256": "0" * 64,
    }
    fixed_inputs = {
        "schema": bootstrap.unit_inputs_v4.FIXED_INPUTS_SCHEMA,
        "release_revision": TARGET,
        "unit_input_authority_plan_sha256": unit_plan["plan_sha256"],
        "unit_input_authority_approval_sha256": unit_approval[
            "approval_sha256"
        ],
        "fixed_inputs_sha256": "1" * 64,
    }
    receipt = bootstrap.collect_bootstrap_host_receipt(
        predecessor_revision=PREDECESSOR,
        release_revision=TARGET,
        release_verifier=lambda *_args, **_kwargs: {
            "receipt_sha256": "9" * 64,
            "manifest_sha256": "a" * 64,
            "payload_tree_sha256": "b" * 64,
            "root_uid": 0,
            "root_gid": 0,
            "root_mode": "0555",
            "root_xattrs": [],
        },
        host_observer_fn=lambda **_kwargs: SimpleNamespace(
            receipt={},
            unit_observations=_target_observations(),
        ),
        runtime_safety_builder=safety_builder,
        runtime_safety_validator=lambda value, **_kwargs: value,
        unit_input_triplet_loader=lambda _revision: {
            "plan": unit_plan,
            "approval": unit_approval,
            "fixed_inputs": fixed_inputs,
            "plan_path": str(bootstrap.package.STAGED_UNIT_INPUT_PLAN_PATH),
            "approval_path": str(
                bootstrap.package.STAGED_UNIT_INPUT_APPROVAL_PATH
            ),
            "fixed_inputs_path": str(
                bootstrap.package.FIXED_UNIT_INPUTS_PATH
            ),
            "plan_file_sha256": "2" * 64,
            "approval_file_sha256": "3" * 64,
            "fixed_inputs_file_sha256": "4" * 64,
        },
        cutover_terminal_loader=lambda _revision: {
            **terminal_unsigned,
            "receipt_sha256": bootstrap._sha(terminal_unsigned),
        },
    )
    expected_set = update_inputs.build_release_consumer_set(
        predecessor_revision=PREDECESSOR,
        release_revision=TARGET,
    )

    assert receipt["release_consumer_set"] == expected_set
    assert receipt["consumer_catalog"] == expected_set["consumers"]
    assert receipt["consumer_unit_count"] == 79
    assert receipt["release_create_only"] is True
    assert receipt["release_root_owned"] is True
    assert receipt["release_read_only"] is True
    assert receipt["fixed_unit_inputs_sha256"] == "1" * 64
    assert receipt["unit_input_authority"]["plan"] == unit_plan
    assert receipt["predecessor_active_observer_called"] is False
    assert receipt["trust_anchors"]["connector_unit_template_sha256"] == (
        "c" * 64
    )


def _activation_inputs() -> dict:
    public_hex = "1" * 64
    owner_key_id = hashlib.sha256(bytes.fromhex(public_hex)).hexdigest()
    decision = {
        "mode": bootstrap.TRUTH_MODE,
        "accepted_event_ids": [],
        "accepted_event_receipts": [],
        "decision_sha256": "2" * 64,
        "truth_epoch_sha256": "3" * 64,
    }
    freeze = {
        "release_revision": TARGET,
        "plan_sha256": "4" * 64,
        "owner_subject_sha256": "5" * 64,
        "owner_public_key_ed25519_hex": public_hex,
        "owner_key_id": owner_key_id,
        "cutover_authority": {"legacy_truth_decision": decision},
    }
    terminal_unsigned = {
        "alias_projection_package_sha256": "6" * 64,
        "alias_projection_activation_authority_sha256": "7" * 64,
        "alias_projection_activation_receipt_sha256": "8" * 64,
    }
    terminal = {
        **terminal_unsigned,
        "receipt_sha256": bootstrap._sha(terminal_unsigned),
    }
    host = {
        "schema": bootstrap.HOST_RECEIPT_SCHEMA,
        "legacy_predecessor_revision": PREDECESSOR,
        "release_revision": TARGET,
        "consumer_unit_count": 79,
        "fixed_unit_inputs_sha256": "9" * 64,
        "unit_input_authority": {
            "plan": {
                "schema": bootstrap.unit_inputs_v4.PLAN_SCHEMA,
                "release_revision": TARGET,
                "plan_sha256": "6" * 64,
                "owner_subject_sha256": freeze["owner_subject_sha256"],
                "owner_public_key_ed25519_hex": public_hex,
                "owner_key_id": owner_key_id,
            },
            "approval": {
                "schema": bootstrap.unit_inputs_v4.APPROVAL_SCHEMA,
                "release_revision": TARGET,
                "approval_sha256": "7" * 64,
            },
            "fixed_inputs": {
                "schema": bootstrap.unit_inputs_v4.FIXED_INPUTS_SCHEMA,
                "release_revision": TARGET,
                "unit_input_authority_plan_sha256": "6" * 64,
                "unit_input_authority_approval_sha256": "7" * 64,
                "fixed_inputs_sha256": "9" * 64,
            },
        },
        "release_publication_receipt_sha256": "a" * 64,
        "release_payload_tree_sha256": "b" * 64,
        "release_consumer_set_sha256": "c" * 64,
        "consumer_catalog_sha256": "d" * 64,
        "immutable_unit_paths_sha256": "e" * 64,
        "runtime_safety_plan": {
            "runtime_safety_plan_sha256": "f" * 64,
        },
        "target_active_observation": {"receipt_sha256": "0" * 64},
        "trust_anchors": {
            "alias_projection_package_sha256": "6" * 64,
        },
        "receipt_sha256": "1" * 64,
    }
    return {
        "legacy_predecessor_revision": PREDECESSOR,
        "release_revision": TARGET,
        "freeze_plan": freeze,
        "freeze_approval": {"approval_sha256": "a" * 64},
        "cutover_plan": {
            "release_revision": TARGET,
            "plan_sha256": "b" * 64,
        },
        "cutover_terminal_receipt": terminal,
        "convergence_receipt": {"receipt_sha256": "c" * 64},
        "workflow_receipt": {"receipt_sha256": "d" * 64},
        "host_receipt": host,
    }


def test_activation_receipt_becomes_exact_stage_c_predecessor_trust() -> None:
    inputs = _activation_inputs()
    activation = bootstrap.build_terminal_activation_receipt(**inputs)
    envelope = bootstrap.build_terminal_envelope(
        workflow_receipt=inputs["workflow_receipt"],
        activation_receipt=activation,
    )

    trust = update_contract.validate_predecessor_trust(
        envelope["predecessor_trust"],
        expected_trust_sha256=envelope["predecessor_trust"]["trust_sha256"],
    )
    assert activation["truth_mode"] == "start_new_truth_epoch"
    assert activation["predecessor_active_observer_called"] is False
    assert trust["release_revision"] == TARGET
    assert trust["authority_plan_sha256"] == "6" * 64
    assert trust["authority_approval_sha256"] == "7" * 64
    assert trust["activation_receipt_sha256"] == activation["receipt_sha256"]
    assert trust["fixed_inputs_sha256"] == "9" * 64


def test_bootstrap_rejects_same_or_prefix_colliding_release() -> None:
    with pytest.raises(
        bootstrap.ProductionInitialReleaseBootstrapError,
        match="revision_invalid",
    ):
        bootstrap._revision_pair(PREDECESSOR, PREDECESSOR)
    with pytest.raises(
        bootstrap.ProductionInitialReleaseBootstrapError,
        match="revision_invalid",
    ):
        bootstrap._revision_pair(PREDECESSOR, PREDECESSOR[:12] + "0" * 28)
