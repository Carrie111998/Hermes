from __future__ import annotations

import hashlib
from copy import deepcopy
from dataclasses import dataclass
from typing import Any, Mapping

import pytest

from gateway import production_alias_projection_units as alias_units
from gateway import production_cron_continuity_package as trusted_cron
from scripts.canary import package_production_cutover_artifacts as host_package
from scripts.canary import production_cutover_host_authority as host_authority
from scripts.canary import production_release_consumer_inventory as inventory
from scripts.canary import production_release_update_inputs as inputs
from tests.scripts.canary import (
    test_production_cutover_host_authority as host_authority_test,
)
from tests.scripts.canary import (
    test_production_cutover_owner_launcher as owner_test,
)
from tests.scripts.canary import (
    test_production_release_host_observer as host_test,
)
from tests.scripts.canary import (
    test_production_release_unit_inputs_v4 as v4_test,
)


NOW = v4_test.NOW
PREDECESSOR = v4_test.PREDECESSOR
TARGET = owner_test.REVISION


@dataclass
class Fixture:
    private: Any
    trusted: Mapping[str, Any]
    payload: Mapping[str, Any]
    unit_publication: Mapping[str, Any]
    documents: dict[str, Mapping[str, Any]]
    update_publication: Mapping[str, Any]


def _digest(label: str) -> str:
    return hashlib.sha256(label.encode("ascii")).hexdigest()


def _rehash(value: dict[str, Any], field: str) -> None:
    unsigned = {name: item for name, item in value.items() if name != field}
    value[field] = inputs.sha256_bytes(inputs.canonical_bytes(unsigned))


def _v3_payload() -> dict[str, Any]:
    payload = deepcopy(v4_test._v3_payload())  # noqa: SLF001
    payload["discord_reconciliation_intent"]["release_revision"] = TARGET
    return payload


def _payload() -> dict[str, Any]:
    return dict(
        v4_test.unit_v4.build_payload(
            v3_payload=_v3_payload(),
            builder_identity={
                "user": "muncho-release-builder",
                "group": "muncho-release-builder",
                "uid": 29104,
                "gid": 29104,
            },
            builder_terminal_receipt_sha256="07" * 32,
            whole_tree_manifest_sha256="08" * 32,
            candidate_seal_receipt_sha256="09" * 32,
            runtime_dependency_manifest_sha256="0a" * 32,
            owner_gate_receipt_public_key_id="0b" * 32,
        )
    )


def _unit_documents(
    private: Any,
    trusted: Mapping[str, Any],
    payload: Mapping[str, Any],
) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any]]:
    plan = dict(
        v4_test.unit_v4.build_plan(
            release_revision=TARGET,
            unit_inputs=payload,
            trusted_predecessor=trusted,
            expected_predecessor_trust_sha256=str(
                trusted["trust_sha256"]
            ),
            created_at_unix=NOW - 30,
        )
    )
    approval = dict(
        v4_test.unit_v4.build_approval(
            plan=plan,
            trusted_predecessor=trusted,
            expected_predecessor_trust_sha256=str(
                trusted["trust_sha256"]
            ),
            nonce_sha256="11" * 32,
            issued_at_unix=NOW - 10,
            expires_at_unix=NOW + 300,
            now_unix=NOW,
            signer=private.sign,
        )
    )
    publication = dict(
        v4_test.unit_v4.build_publication(
            plan=plan,
            approval=approval,
            trusted_predecessor=trusted,
            expected_predecessor_trust_sha256=str(
                trusted["trust_sha256"]
            ),
            now_unix=NOW,
        )
    )
    return plan, approval, publication


def _cutover_v4_unit_inputs(
    payload: Mapping[str, Any],
    unit_plan: Mapping[str, Any],
    unit_approval: Mapping[str, Any],
) -> Mapping[str, Any]:
    projected = v4_test.unit_v4.project_payload_to_cutover_v4(payload)
    return {
        "schema": host_package.UNIT_INPUT_SCHEMA_V4,
        "release_revision": TARGET,
        "authority_plan_sha256": unit_plan["plan_sha256"],
        "authority_approval_sha256": unit_approval["approval_sha256"],
        **{
            name: item
            for name, item in projected.items()
            if name != "schema"
        },
    }


def _host_manifest(
    payload: Mapping[str, Any],
    unit_plan: Mapping[str, Any],
    unit_approval: Mapping[str, Any],
    host_transition: Mapping[str, Any],
) -> Mapping[str, Any]:
    unit_inputs = _cutover_v4_unit_inputs(payload, unit_plan, unit_approval)
    sealed_names = {
        name
        for name, (_target, binding) in (
            host_package.HOST_ARTIFACT_TARGETS.items()
        )
        if binding == "release_sealed_payload"
    }
    sealed_files = {
        name: {
            "target_path": host_package.HOST_ARTIFACT_TARGETS[name][0],
            "sha256": host_transition["files"][name]["sha256"],
            "uid": 0,
            "gid": 0,
            "mode": 0o644,
        }
        for name in sorted(sealed_names)
    }
    sealed_unsigned = {
        "schema": host_package.SEALED_RUNTIME_ARTIFACT_REQUEST_V4_SCHEMA,
        "release_revision": TARGET,
        "target": unit_inputs["target"],
        "files": sealed_files,
        "isolated_worker_lease_mountpoint": {
            "target_path": "/run/muncho-worker/leases",
            "uid": 0,
            "gid": 0,
            "mode": 0o700,
        },
        "topology_fragments": {"identity": _digest("topology")},
        "capability_bundle": {"identity": _digest("capability")},
        "isolated_worker_bundle": {"identity": _digest("worker")},
        "operational_edge_bundle": {"identity": _digest("edge")},
        "owner_gate_receipt_public_key_id": payload[
            "owner_gate_receipt_public_key_id"
        ],
        "operational_asset_verification": {
            "identity": _digest("assets")
        },
        "secret_material_recorded": False,
        "secret_digest_recorded": False,
    }
    sealed = {
        **sealed_unsigned,
        "request_sha256": inputs.sha256_bytes(
            inputs.canonical_bytes(sealed_unsigned)
        ),
    }
    source = {
        name: _digest(f"source:{name}")
        for name in inputs._HOST_SOURCE_FIELDS  # noqa: SLF001
    }
    source["sealed_runtime_artifact_request_sha256"] = sealed[
        "request_sha256"
    ]
    source["runtime_dependency_manifest_sha256"] = payload[
        "runtime_dependency_manifest_sha256"
    ]
    source["gateway_connector_drop_in_sha256"] = host_transition[
        "files"
    ]["gateway_connector_drop_in"]["sha256"]
    contract = host_package._host_artifact_contract(  # noqa: SLF001
        sealed_descriptor=sealed,
        gateway_connector_drop_in_sha256=source[
            "gateway_connector_drop_in_sha256"
        ],
    )
    release_root = v4_test.release_update.expected_release_root(TARGET)
    artifacts = {
        name: {
            "path": (
                f"{release_root}/ops/muncho/cutover/artifacts/{name}"
            ),
            "actions": list(actions),
            "sha256": _digest(f"artifact:{name}"),
            "size": 100 + index,
        }
        for index, (name, actions) in enumerate(
            host_package.ARTIFACTS.items()
        )
    }
    unsigned = {
        "schema": host_package.MANIFEST_SCHEMA,
        "release_revision": TARGET,
        "source": source,
        "unit_inputs": unit_inputs,
        "sealed_runtime_artifact_request": sealed,
        "host_artifact_contract": contract,
        "artifacts": artifacts,
        "plan_bindings": {
            binding: {
                "path": artifacts[name]["path"],
                "sha256": artifacts[name]["sha256"],
            }
            for binding, name in host_package.PLAN_BINDINGS.items()
        }
        | {
            host_package.ALIAS_PROJECTION_BINDING: {
                "path": (
                    f"{release_root}/"
                    f"{host_package.ALIAS_PROJECTION_PACKAGE_RELATIVE_ROOT}/"
                    "manifest.json"
                ),
                "sha256": source["alias_projection_package_sha256"],
            }
        },
        "secret_material_recorded": False,
    }
    return {
        **unsigned,
        "manifest_sha256": inputs.sha256_bytes(
            inputs.canonical_bytes(unsigned)
        ),
    }


def _host_mutation_authority(
    host_manifest: Mapping[str, Any],
    host_receipt: Mapping[str, Any],
    full_collector: Mapping[str, Any],
    *,
    observed_at_unix: int = NOW,
) -> tuple[Mapping[str, Any], Mapping[str, Any]]:
    initial = dict(host_authority_test._initial_from_full(  # noqa: SLF001
        dict(full_collector)
    ))
    initial["source_boot_id_sha256"] = inputs.sha256_bytes(
        host_receipt["processes"]["boot_id"].encode("ascii")
    )
    _rehash(initial, "receipt_sha256")
    request = host_authority.build_host_authority_request(
        initial_collector_receipt=initial,
        release_manifest_sha256=host_manifest["manifest_sha256"],
        gateway_target_identity=full_collector[
            "gateway_target_identity"
        ],
        writer_target_identity=full_collector["writer_target_identity"],
        connector_target_identity=full_collector[
            "connector_target_identity"
        ],
        host_transition=full_collector["host_transition"],
        capability_topology=full_collector["capability_topology"],
        cron_continuity_plan=full_collector["cron_continuity_plan"],
    )
    readback = [
        {
            "name": name,
            "sha256": full_collector["host_transition"]["files"][name][
                "sha256"
            ],
            "size": 100 + index,
            "staged_uid": 0,
            "staged_gid": 0,
            "staged_mode": 0o400,
            "target_pre": deepcopy(
                full_collector["host_transition"]["files"][name]["pre"]
            ),
        }
        for index, name in enumerate(
            sorted(host_package.HOST_ARTIFACT_TARGETS)
        )
    ]
    unsigned = {
        "schema": host_authority.RECEIPT_SCHEMA,
        "release_revision": TARGET,
        "request_sha256": request["request_sha256"],
        "initial_collector_receipt_sha256": initial["receipt_sha256"],
        "release_manifest_sha256": host_manifest["manifest_sha256"],
        "host_artifact_contract_sha256": host_manifest[
            "host_artifact_contract"
        ]["contract_sha256"],
        "gateway_target_identity": deepcopy(
            full_collector["gateway_target_identity"]
        ),
        "writer_target_identity": deepcopy(
            full_collector["writer_target_identity"]
        ),
        "connector_target_identity": deepcopy(
            full_collector["connector_target_identity"]
        ),
        "host_transition": deepcopy(full_collector["host_transition"]),
        "capability_topology": deepcopy(
            full_collector["capability_topology"]
        ),
        "cron_continuity_plan": deepcopy(
            full_collector["cron_continuity_plan"]
        ),
        "readback_file_count": len(readback),
        "readback_files": readback,
        "readback_set_sha256": inputs.sha256_bytes(
            inputs.canonical_bytes({"files": readback})
        ),
        "observed_at_unix": observed_at_unix,
        "source_boot_id_sha256": initial["source_boot_id_sha256"],
        "secret_material_recorded": False,
        "secret_digest_recorded": False,
    }
    receipt = {
        **unsigned,
        "receipt_sha256": inputs.sha256_bytes(
            inputs.canonical_bytes(unsigned)
        ),
    }
    assert host_authority.validate_host_authority_receipt(
        receipt,
        host_authority_request=request,
        initial_collector_receipt=initial,
        release_revision=TARGET,
        now_unix=observed_at_unix,
    ) == receipt
    return initial, receipt


def _cron_index() -> Mapping[str, Any]:
    rows = [
        {
            "relative_path": path,
            "sha256": _digest(f"cron:{path}"),
            "mode": mode,
            "private": private,
        }
        for path, (mode, private) in (
            inputs._expected_cron_rows().items()  # noqa: SLF001
        )
    ]
    unsigned = {
        "schema": trusted_cron.ARTIFACT_INDEX_SCHEMA,
        "release_revision": TARGET,
        "plan_relative_path": str(trusted_cron.PLAN_RELATIVE_PATH),
        "plan_sha256": _digest("cron-plan"),
        "replacement_bundle_relative_path": str(
            trusted_cron.REPLACEMENT_BUNDLE_RELATIVE_PATH
        ),
        "replacement_bundle_sha256": _digest("cron-bundle"),
        "collector_manifest_relative_path": str(
            trusted_cron.COLLECTOR_MANIFEST_RELATIVE_PATH
        ),
        "collector_manifest_sha256": _digest("cron-collector"),
        "cutover_runtime_sha256": _digest("cron-runtime"),
        "cutover_entrypoint_sha256": _digest("cron-entrypoint"),
        "files": rows,
        "file_count": len(rows),
        "units_installed": False,
        "timers_enabled": False,
        "timers_started": False,
        "jobs_store_mutated": False,
        "secret_material_recorded": False,
    }
    return {
        **unsigned,
        "artifact_index_sha256": inputs.sha256_bytes(
            inputs.canonical_bytes(unsigned)
        ),
    }


def _alias_index() -> Mapping[str, Any]:
    bundle = alias_units.render_production_alias_projection_units(
        revision=TARGET,
        database_ip="10.20.30.40",
        writer_user="muncho-canonical-writer",
        writer_group="muncho-canonical-writer",
        writer_uid=1002,
        writer_gid=2002,
        projector_user="muncho-projector",
        projector_group="muncho-projector",
        projector_uid=1003,
        projector_gid=2003,
        gateway_user="ai-platform-brain",
        gateway_group="ai-platform-brain",
        gateway_uid=1001,
        gateway_gid=2001,
        interpreter_sha256=_digest("alias-interpreter"),
        writer_module_sha256=_digest("alias-writer"),
        projector_module_sha256=_digest("alias-projector"),
        projection_reader_sha256=_digest("alias-reader"),
        team_registry_sha256=_digest("alias-team"),
        cutover_runtime_sha256=_digest("alias-runtime"),
        cutover_entrypoint_sha256=_digest("alias-entrypoint"),
    )
    return bundle.manifest()


def _release_update_values(
    *,
    payload: Mapping[str, Any],
    unit_publication_sha256: str,
    overrides: Mapping[str, Any],
) -> dict[str, Any]:
    values = v4_test._release_update_values(  # noqa: SLF001
        payload=payload,
        unit_publication_sha256=unit_publication_sha256,
    )
    values["release_revision"] = TARGET
    values["release_root"] = inputs.update_contract.expected_release_root(
        TARGET
    )
    values["host_mutation_initial_collector_receipt_sha256"] = "17" * 32
    values.update(overrides)
    return values


def _release_update_documents(
    fixture: Fixture,
    overrides: Mapping[str, Any],
) -> Mapping[str, Any]:
    plan = dict(
        inputs.update_contract.build_plan(
            trusted_predecessor=fixture.trusted,
            expected_predecessor_trust_sha256=str(
                fixture.trusted["trust_sha256"]
            ),
            values=_release_update_values(
                payload=fixture.payload,
                unit_publication_sha256=str(
                    fixture.unit_publication["publication_sha256"]
                ),
                overrides=overrides,
            ),
        )
    )
    approval_unsigned = {
        "schema": inputs.update_contract.APPROVAL_SCHEMA,
        "purpose": inputs.update_contract.APPROVAL_PURPOSE,
        "plan_sha256": plan["plan_sha256"],
        "predecessor_revision": plan["predecessor_revision"],
        "release_revision": plan["release_revision"],
        "owner_subject_sha256": plan["owner_subject_sha256"],
        "owner_public_key_ed25519_hex": plan[
            "owner_public_key_ed25519_hex"
        ],
        "owner_key_id": plan["owner_key_id"],
        "nonce_sha256": "16" * 32,
        "issued_at_unix": NOW - 5,
        "expires_at_unix": NOW + 300,
        "approved": True,
    }
    approval_signed = {
        **approval_unsigned,
        "signature_ed25519_hex": fixture.private.sign(
            inputs.update_contract.canonical_bytes(approval_unsigned)
        ).hex(),
    }
    approval = {
        **approval_signed,
        "approval_sha256": inputs.update_contract.sha256_bytes(
            inputs.update_contract.canonical_bytes(approval_signed)
        ),
    }
    return inputs.update_contract.build_publication(
        plan=plan,
        approval=approval,
        trusted_predecessor=fixture.trusted,
        expected_predecessor_trust_sha256=str(
            fixture.trusted["trust_sha256"]
        ),
        now_unix=NOW,
    )


def _signed_update(
    fixture: Fixture,
    overrides: Mapping[str, Any],
) -> Mapping[str, Any]:
    return _release_update_documents(fixture, overrides)


def _fixture() -> Fixture:
    private, trusted = v4_test._authority()
    payload = _payload()
    unit_plan, unit_approval, unit_publication = (
        _unit_documents(private, trusted, payload)
    )
    host_receipt = dict(
        host_test._observe(host_test._harness()).receipt
    )
    host_receipt["observed_at_unix_ns"] = NOW * 1_000_000_000
    host_receipt["target_revision"] = TARGET
    _rehash(host_receipt, "receipt_sha256")
    consumer_set = inputs.build_release_consumer_set(
        predecessor_revision=PREDECESSOR,
        release_revision=TARGET,
    )
    full_collector = owner_test._collector_receipt(  # noqa: SLF001
        NOW,
        owner_test.Services(),
    )
    host_manifest = _host_manifest(
        payload,
        unit_plan,
        unit_approval,
        full_collector["host_transition"],
    )
    initial_collector, host_mutation_authority = _host_mutation_authority(
        host_manifest,
        host_receipt,
        full_collector,
    )
    cron_index = _cron_index()
    alias_index = _alias_index()
    artifact_identities = {
        "host_inventory_sha256": host_receipt["receipt_sha256"],
        "release_consumer_set_sha256": consumer_set[
            "consumer_set_sha256"
        ],
        "host_artifact_manifest_sha256": host_manifest[
            "manifest_sha256"
        ],
        "host_mutation_authority_sha256": host_mutation_authority[
            "receipt_sha256"
        ],
        "host_mutation_initial_collector_receipt_sha256": initial_collector[
            "receipt_sha256"
        ],
        "cron_artifact_index_sha256": cron_index[
            "artifact_index_sha256"
        ],
        "alias_artifact_index_sha256": alias_index["package_sha256"],
        "successor_unit_input_publication_sha256": unit_publication[
            "publication_sha256"
        ],
    }
    activation = inputs.build_activation_plan(
        predecessor_revision=PREDECESSOR,
        release_revision=TARGET,
        artifact_identities=artifact_identities,
    )
    rollback = inputs.build_rollback_plan(
        predecessor_revision=PREDECESSOR,
        release_revision=TARGET,
        artifact_identities=artifact_identities,
    )
    identities = {
        **artifact_identities,
        "activation_plan_sha256": activation[
            "activation_plan_sha256"
        ],
        "rollback_plan_sha256": rollback["rollback_plan_sha256"],
    }
    placeholder = Fixture(
        private=private,
        trusted=trusted,
        payload=payload,
        unit_publication=unit_publication,
        documents={},
        update_publication={},
    )
    publication = _signed_update(placeholder, identities)
    placeholder.documents = {
        "host_inventory_sha256": host_receipt,
        "release_consumer_set_sha256": consumer_set,
        "host_artifact_manifest_sha256": host_manifest,
        "host_mutation_authority_sha256": host_mutation_authority,
        "host_mutation_initial_collector_receipt_sha256": initial_collector,
        "cron_artifact_index_sha256": cron_index,
        "alias_artifact_index_sha256": alias_index,
        "successor_unit_input_publication_sha256": unit_publication,
        "activation_plan_sha256": activation,
        "rollback_plan_sha256": rollback,
    }
    placeholder.update_publication = publication
    return placeholder


def _validate(fixture: Fixture) -> inputs.ValidatedStage0Inputs:
    return inputs.validate_stage0_inputs(
        fixture.documents,
        fixture.update_publication,
        fixture.trusted,
        str(fixture.trusted["trust_sha256"]),
        NOW,
    )


def test_all_remaining_inputs_bind_internal_identities_and_v4_fixed_inputs() -> None:
    fixture = _fixture()

    validated = _validate(fixture)

    assert validated.identities == {
        name: fixture.update_publication["plan"][name]
        for name in validated.identities
    }
    assert validated.fixed_v4_inputs[
        "unit_input_authority_publication_sha256"
    ] == fixture.unit_publication["publication_sha256"]
    assert validated.fixed_v4_inputs[
        "release_update_publication_sha256"
    ] == fixture.update_publication["publication_sha256"]
    assert validated.documents["host_inventory_sha256"]["phase"] == (
        "predecessor_active"
    )
    authority = validated.documents["host_mutation_authority_sha256"]
    assert authority["release_manifest_sha256"] == validated.documents[
        "host_artifact_manifest_sha256"
    ]["manifest_sha256"]


@pytest.mark.parametrize(
    "mutation",
    (
        "missing",
        "downgrade",
        "wrong_manifest",
        "wrong_boot",
        "readback_drift",
        "request_substitution",
        "initial_collector_substitution",
        "target_identity_downgrade",
        "capability_topology_downgrade",
        "cron_plan_downgrade",
        "non_root_staging",
    ),
)
def test_host_mutation_authority_rejects_omission_downgrade_and_drift(
    mutation: str,
) -> None:
    fixture = _fixture()
    authority = deepcopy(
        fixture.documents["host_mutation_authority_sha256"]
    )
    if mutation == "missing":
        authority.pop("readback_set_sha256")
    elif mutation == "downgrade":
        authority["schema"] = "muncho-production-cutover-host-authority.v0"
        _rehash(authority, "receipt_sha256")
    elif mutation == "wrong_manifest":
        authority["release_manifest_sha256"] = "f" * 64
        _rehash(authority, "receipt_sha256")
    elif mutation == "wrong_boot":
        authority["source_boot_id_sha256"] = "f" * 64
        _rehash(authority, "receipt_sha256")
    elif mutation == "readback_drift":
        dynamic_name = next(
            name
            for name, item in fixture.documents[
                "host_artifact_manifest_sha256"
            ]["host_artifact_contract"]["files"].items()
            if item["package_sha256"] is None
        )
        authority["host_transition"]["files"][dynamic_name]["sha256"] = (
            "f" * 64
        )
        _rehash(authority, "receipt_sha256")
    elif mutation == "request_substitution":
        authority["request_sha256"] = "f" * 64
        _rehash(authority, "receipt_sha256")
    elif mutation == "initial_collector_substitution":
        authority["initial_collector_receipt_sha256"] = "f" * 64
        _rehash(authority, "receipt_sha256")
    elif mutation == "target_identity_downgrade":
        authority["writer_target_identity"] = {"unit": "unknown.service"}
        _rehash(authority, "receipt_sha256")
    elif mutation == "capability_topology_downgrade":
        authority["capability_topology"] = {"schema": "downgraded"}
        _rehash(authority, "receipt_sha256")
    elif mutation == "cron_plan_downgrade":
        authority["cron_continuity_plan"] = {"schema": "downgraded"}
        _rehash(authority, "receipt_sha256")
    else:
        authority["readback_files"][0]["staged_uid"] = 1234
        authority["readback_files"][0]["staged_gid"] = 1234
        authority["readback_set_sha256"] = inputs.sha256_bytes(
            inputs.canonical_bytes({"files": authority["readback_files"]})
        )
        _rehash(authority, "receipt_sha256")

    with pytest.raises(
        inputs.ProductionReleaseUpdateInputsError,
        match="release_update_inputs_host_mutation_authority_invalid",
    ):
        inputs.validate_host_mutation_authority(
            authority,
            initial_collector_receipt=fixture.documents[
                "host_mutation_initial_collector_receipt_sha256"
            ],
            release_revision=TARGET,
            host_artifact_manifest=fixture.documents[
                "host_artifact_manifest_sha256"
            ],
            host_inventory=fixture.documents["host_inventory_sha256"],
            now_unix=NOW,
        )


def test_signed_plan_rejects_substituted_valid_host_mutation_authority() -> None:
    fixture = _fixture()
    authority = deepcopy(
        fixture.documents["host_mutation_authority_sha256"]
    )
    authority["observed_at_unix"] = NOW - 1
    _rehash(authority, "receipt_sha256")
    inputs.validate_host_mutation_authority(
        authority,
        initial_collector_receipt=fixture.documents[
            "host_mutation_initial_collector_receipt_sha256"
        ],
        release_revision=TARGET,
        host_artifact_manifest=fixture.documents[
            "host_artifact_manifest_sha256"
        ],
        host_inventory=fixture.documents["host_inventory_sha256"],
        now_unix=NOW,
    )
    fixture.documents["host_mutation_authority_sha256"] = authority

    with pytest.raises(
        inputs.ProductionReleaseUpdateInputsError,
        match=(
            "release_update_inputs_(?:activation_plan|plan_binding)_invalid"
        ),
    ):
        _validate(fixture)


@pytest.mark.parametrize("offset_seconds", (-10 * 365 * 24 * 60 * 60, 31))
def test_initial_collector_rejects_stale_and_future_replay(
    offset_seconds: int,
) -> None:
    fixture = _fixture()
    observed_at = NOW + offset_seconds
    inventory_receipt = deepcopy(
        fixture.documents["host_inventory_sha256"]
    )
    inventory_receipt["observed_at_unix_ns"] = observed_at * 1_000_000_000
    _rehash(inventory_receipt, "receipt_sha256")
    full_collector = owner_test._collector_receipt(  # noqa: SLF001
        observed_at,
        owner_test.Services(),
    )
    initial, authority = _host_mutation_authority(
        fixture.documents["host_artifact_manifest_sha256"],
        inventory_receipt,
        full_collector,
        observed_at_unix=observed_at,
    )

    with pytest.raises(
        inputs.ProductionReleaseUpdateInputsError,
        match="release_update_inputs_host_mutation_authority_invalid",
    ):
        inputs.validate_host_mutation_authority(
            authority,
            initial_collector_receipt=initial,
            release_revision=TARGET,
            host_artifact_manifest=fixture.documents[
                "host_artifact_manifest_sha256"
            ],
            host_inventory=inventory_receipt,
            now_unix=NOW,
        )


@pytest.mark.parametrize("offset_seconds", (-10 * 365 * 24 * 60 * 60, 31))
def test_host_mutation_authority_rejects_stale_and_future_replay(
    offset_seconds: int,
) -> None:
    fixture = _fixture()
    observed_at = NOW + offset_seconds
    authority = deepcopy(
        fixture.documents["host_mutation_authority_sha256"]
    )
    authority["observed_at_unix"] = observed_at
    _rehash(authority, "receipt_sha256")
    inventory_receipt = deepcopy(
        fixture.documents["host_inventory_sha256"]
    )
    inventory_receipt["observed_at_unix_ns"] = observed_at * 1_000_000_000
    _rehash(inventory_receipt, "receipt_sha256")

    with pytest.raises(
        inputs.ProductionReleaseUpdateInputsError,
        match="release_update_inputs_host_mutation_authority_invalid",
    ):
        inputs.validate_host_mutation_authority(
            authority,
            initial_collector_receipt=fixture.documents[
                "host_mutation_initial_collector_receipt_sha256"
            ],
            release_revision=TARGET,
            host_artifact_manifest=fixture.documents[
                "host_artifact_manifest_sha256"
            ],
            host_inventory=inventory_receipt,
            now_unix=NOW,
        )


@pytest.mark.parametrize("offset_seconds", (-10 * 365 * 24 * 60 * 60, 31))
def test_host_inventory_rejects_stale_and_future_replay(
    offset_seconds: int,
) -> None:
    fixture = _fixture()
    inventory_receipt = deepcopy(
        fixture.documents["host_inventory_sha256"]
    )
    inventory_receipt["observed_at_unix_ns"] = (
        NOW + offset_seconds
    ) * 1_000_000_000
    _rehash(inventory_receipt, "receipt_sha256")

    with pytest.raises(
        inputs.ProductionReleaseUpdateInputsError,
        match="release_update_inputs_host_mutation_authority_invalid",
    ):
        inputs.validate_host_mutation_authority(
            fixture.documents["host_mutation_authority_sha256"],
            initial_collector_receipt=fixture.documents[
                "host_mutation_initial_collector_receipt_sha256"
            ],
            release_revision=TARGET,
            host_artifact_manifest=fixture.documents[
                "host_artifact_manifest_sha256"
            ],
            host_inventory=inventory_receipt,
            now_unix=NOW,
        )


def test_initial_collector_omission_and_downgrade_fail_closed() -> None:
    fixture = _fixture()
    authority = fixture.documents["host_mutation_authority_sha256"]
    for initial in ({}, {"schema": "downgraded"}):
        with pytest.raises(
            inputs.ProductionReleaseUpdateInputsError,
            match="release_update_inputs_host_mutation_authority_invalid",
        ):
            inputs.validate_host_mutation_authority(
                authority,
                initial_collector_receipt=initial,
                release_revision=TARGET,
                host_artifact_manifest=fixture.documents[
                    "host_artifact_manifest_sha256"
                ],
                host_inventory=fixture.documents["host_inventory_sha256"],
                now_unix=NOW,
            )


def test_signed_plan_rejects_substituted_valid_collector_authority_pair() -> None:
    fixture = _fixture()
    full_collector = owner_test._collector_receipt(  # noqa: SLF001
        NOW - 1,
        owner_test.Services(),
    )
    initial, authority = _host_mutation_authority(
        fixture.documents["host_artifact_manifest_sha256"],
        fixture.documents["host_inventory_sha256"],
        full_collector,
        observed_at_unix=NOW - 1,
    )
    fixture.documents["host_mutation_initial_collector_receipt_sha256"] = (
        initial
    )
    fixture.documents["host_mutation_authority_sha256"] = authority

    with pytest.raises(
        inputs.ProductionReleaseUpdateInputsError,
        match="release_update_inputs_(?:activation_plan|plan_binding)_invalid",
    ):
        _validate(fixture)


def test_successor_plan_field_is_internal_publication_hash_not_file_hash() -> None:
    fixture = _fixture()
    document = fixture.unit_publication
    file_digest = inputs.sha256_bytes(
        inputs.canonical_bytes(document) + b"\n"
    )

    assert file_digest != document["publication_sha256"]
    assert (
        fixture.update_publication["plan"][
            "successor_unit_input_publication_sha256"
        ]
        == document["publication_sha256"]
    )
    _validate(fixture)

    fixture.update_publication = _signed_update(
        fixture,
        {"successor_unit_input_publication_sha256": file_digest},
    )
    with pytest.raises(
        inputs.ProductionReleaseUpdateInputsError,
        match="release_update_inputs_plan_binding_invalid",
    ):
        _validate(fixture)


def test_forged_internal_hash_is_rejected_before_cross_binding() -> None:
    fixture = _fixture()
    receipt = deepcopy(fixture.documents["host_inventory_sha256"])
    receipt["receipt_sha256"] = "f" * 64
    fixture.documents["host_inventory_sha256"] = receipt

    with pytest.raises(
        inputs.ProductionReleaseUpdateInputsError,
        match="release_update_inputs_host_inventory_invalid",
    ):
        _validate(fixture)


def test_rehashed_wrong_host_phase_and_revision_are_rejected() -> None:
    fixture = _fixture()
    receipt = deepcopy(fixture.documents["host_inventory_sha256"])
    receipt["phase"] = "predecessor_fenced"
    receipt["validation"]["phase"] = "predecessor_fenced"
    _rehash(receipt, "receipt_sha256")
    fixture.documents["host_inventory_sha256"] = receipt
    fixture.update_publication = _signed_update(
        fixture,
        {"host_inventory_sha256": receipt["receipt_sha256"]},
    )

    with pytest.raises(
        inputs.ProductionReleaseUpdateInputsError,
        match="release_update_inputs_host_inventory_invalid",
    ):
        _validate(fixture)

    receipt["phase"] = "predecessor_active"
    receipt["validation"]["phase"] = "predecessor_active"
    receipt["target_revision"] = "3" * 40
    _rehash(receipt, "receipt_sha256")
    fixture.update_publication = _signed_update(
        fixture,
        {"host_inventory_sha256": receipt["receipt_sha256"]},
    )
    with pytest.raises(
        inputs.ProductionReleaseUpdateInputsError,
        match="release_update_inputs_host_inventory_invalid",
    ):
        _validate(fixture)


def test_rehashed_catalog_drift_is_rejected_even_when_signed_plan_matches() -> None:
    fixture = _fixture()
    consumer_set = deepcopy(
        fixture.documents["release_consumer_set_sha256"]
    )
    consumer_set["consumers"][0]["fragment_path"] = (
        "/etc/systemd/system/forged.service"
    )
    consumer_set["catalog_sha256"] = inputs.sha256_bytes(
        inputs.canonical_bytes(consumer_set["consumers"])
    )
    _rehash(consumer_set, "consumer_set_sha256")
    fixture.documents["release_consumer_set_sha256"] = consumer_set
    fixture.update_publication = _signed_update(
        fixture,
        {
            "release_consumer_set_sha256": consumer_set[
                "consumer_set_sha256"
            ]
        },
    )

    with pytest.raises(
        inputs.ProductionReleaseUpdateInputsError,
        match="release_update_inputs_consumer_set_invalid",
    ):
        _validate(fixture)


def test_cron_wrong_row_order_is_rejected_without_host_io() -> None:
    fixture = _fixture()
    cron = deepcopy(fixture.documents["cron_artifact_index_sha256"])
    cron["files"][0], cron["files"][1] = cron["files"][1], cron["files"][0]
    _rehash(cron, "artifact_index_sha256")
    fixture.documents["cron_artifact_index_sha256"] = cron
    fixture.update_publication = _signed_update(
        fixture,
        {"cron_artifact_index_sha256": cron["artifact_index_sha256"]},
    )

    with pytest.raises(
        inputs.ProductionReleaseUpdateInputsError,
        match="release_update_inputs_cron_index_invalid",
    ):
        _validate(fixture)


def test_host_manifest_artifact_plan_binding_drift_is_rejected() -> None:
    fixture = _fixture()
    manifest = deepcopy(
        fixture.documents["host_artifact_manifest_sha256"]
    )
    manifest["plan_bindings"]["observe"]["sha256"] = "e" * 64
    _rehash(manifest, "manifest_sha256")
    fixture.documents["host_artifact_manifest_sha256"] = manifest
    fixture.update_publication = _signed_update(
        fixture,
        {"host_artifact_manifest_sha256": manifest["manifest_sha256"]},
    )

    with pytest.raises(
        inputs.ProductionReleaseUpdateInputsError,
        match="release_update_inputs_host_manifest_invalid",
    ):
        _validate(fixture)


@pytest.mark.parametrize(
    ("field", "replacement"),
    (
        ("schema", host_package.UNIT_INPUT_SCHEMA),
        ("authority_plan_sha256", "d" * 64),
        ("authority_approval_sha256", "e" * 64),
        ("owner_gate_receipt_public_key_id", "f" * 64),
        ("release_owner_uid", 1001),
    ),
)
def test_host_manifest_unit_inputs_must_be_exact_v4_projection(
    field: str,
    replacement: str | int,
) -> None:
    fixture = _fixture()
    manifest = deepcopy(
        fixture.documents["host_artifact_manifest_sha256"]
    )
    manifest["unit_inputs"][field] = replacement
    _rehash(manifest, "manifest_sha256")
    fixture.documents["host_artifact_manifest_sha256"] = manifest
    fixture.update_publication = _signed_update(
        fixture,
        {"host_artifact_manifest_sha256": manifest["manifest_sha256"]},
    )

    with pytest.raises(
        inputs.ProductionReleaseUpdateInputsError,
        match="release_update_inputs_host_manifest_invalid",
    ):
        _validate(fixture)


def test_host_manifest_owner_gate_key_must_match_signed_v4_authority() -> None:
    fixture = _fixture()
    manifest = deepcopy(
        fixture.documents["host_artifact_manifest_sha256"]
    )
    sealed = manifest["sealed_runtime_artifact_request"]
    sealed["owner_gate_receipt_public_key_id"] = "f" * 64
    _rehash(sealed, "request_sha256")
    manifest["source"]["sealed_runtime_artifact_request_sha256"] = sealed[
        "request_sha256"
    ]
    _rehash(manifest, "manifest_sha256")
    fixture.documents["host_artifact_manifest_sha256"] = manifest
    fixture.update_publication = _signed_update(
        fixture,
        {"host_artifact_manifest_sha256": manifest["manifest_sha256"]},
    )

    with pytest.raises(
        inputs.ProductionReleaseUpdateInputsError,
        match="release_update_inputs_host_manifest_invalid",
    ):
        _validate(fixture)


def test_host_manifest_runtime_dependency_must_match_signed_update() -> None:
    fixture = _fixture()
    manifest = deepcopy(
        fixture.documents["host_artifact_manifest_sha256"]
    )
    manifest["source"]["runtime_dependency_manifest_sha256"] = "f" * 64
    _rehash(manifest, "manifest_sha256")
    fixture.documents["host_artifact_manifest_sha256"] = manifest
    fixture.update_publication = _signed_update(
        fixture,
        {"host_artifact_manifest_sha256": manifest["manifest_sha256"]},
    )

    with pytest.raises(
        inputs.ProductionReleaseUpdateInputsError,
        match="release_update_inputs_host_manifest_invalid",
    ):
        _validate(fixture)


def test_plans_partition_every_allowed_mutation_path_without_active_claim() -> None:
    fixture = _fixture()
    activation = fixture.documents["activation_plan_sha256"]
    rollback = fixture.documents["rollback_plan_sha256"]
    expected_partitions = inputs._mutation_path_partitions()  # noqa: SLF001
    expected_targets = sorted(
        {
            path
            for paths in expected_partitions.values()
            for path in paths
        }
    )

    assert len(expected_partitions["consumer_fragment_paths"]) == (
        inventory.EXPECTED_UNIT_COUNT
    )
    assert len(expected_partitions["consumer_drop_in_paths"]) == 1
    assert set(expected_partitions["host_artifact_target_paths"]) == {
        target
        for target, _binding in host_package.HOST_ARTIFACT_TARGETS.values()
    }
    assert expected_partitions["live_unit_input_paths"] == sorted(
        {
            str(host_package.STAGED_UNIT_INPUT_PLAN_PATH),
            str(host_package.STAGED_UNIT_INPUT_APPROVAL_PATH),
            str(host_package.FIXED_UNIT_INPUTS_PATH),
        }
    )
    assert expected_partitions["release_pointer_paths"] == [
        str(inventory.COMPATIBILITY_RELEASE_SYMLINK)
    ]
    for plan in (activation, rollback):
        assert plan["mutation_path_partitions"] == expected_partitions
        assert plan["mutation_target_paths"] == expected_targets
        assert plan["mutation_target_count"] == len(expected_targets)
        assert plan["mutation_target_set_sha256"] == inputs.sha256_bytes(
            inputs.canonical_bytes(expected_targets)
        )
        assert plan["unmodeled_mutation_allowed"] is False
        assert plan["catalog_consumer_unit_count"] == (
            inventory.EXPECTED_UNIT_COUNT
        )
        assert plan["catalog_execution_service_count"] == (
            inventory.EXPECTED_EXECUTION_SERVICE_COUNT
        )
        assert plan["catalog_trigger_unit_count"] == (
            inventory.EXPECTED_TRIGGER_UNIT_COUNT
        )
        assert "service_unit_count" not in plan

    assert activation["schema"] == (
        "muncho-production-release-activation-plan.v4"
    )
    assert activation["forward_phase_order"] == list(
        inputs.update_runtime.FORWARD_PHASES
    )
    assert activation["unit_input_preauthorization_phase"] == (
        inputs.update_runtime.UNIT_INPUT_PREAUTHORIZATION_PHASE
    )
    assert activation["unit_input_finalization_phase"] == (
        "unit_inputs_finalized"
    )
    assert activation["unit_input_preauthorization_before_commit"] is True
    assert activation["unit_input_finalization_after_commit"] is True
    assert activation["forward_phase_order"].index(
        activation["unit_input_preauthorization_phase"]
    ) < activation["forward_phase_order"].index(
        activation["commit_phase"]
    ) < activation["forward_phase_order"].index(
        activation["unit_input_finalization_phase"]
    )

    assert rollback["schema"] == (
        "muncho-production-release-rollback-plan.v4"
    )
    assert rollback["rollback_phase_order"] == list(
        inputs.update_runtime.ROLLBACK_PHASES
    )
    assert rollback["preauthorized_rollback_phase_order"] == list(
        inputs.update_runtime.PREAUTHORIZED_ROLLBACK_PHASES
    )
    assert rollback["unit_input_preauthorization_discriminator_phase"] == (
        inputs.update_runtime.UNIT_INPUT_PREAUTHORIZATION_DISCRIMINATOR_PHASE
    )
    assert rollback["unit_input_preauthorization_cancel_phase"] == (
        inputs.update_runtime.UNIT_INPUT_PREAUTHORIZATION_CANCEL_PHASE
    )
    assert (
        rollback[
            "unit_input_preauthorization_cancel_before_host_restore"
        ]
        is True
    )
    assert rollback["preauthorized_rollback_phase_order"].index(
        rollback["unit_input_preauthorization_cancel_phase"]
    ) < rollback["preauthorized_rollback_phase_order"].index(
        "target_stopped"
    )


@pytest.mark.parametrize(
    ("document_name", "digest_field"),
    (
        ("activation_plan_sha256", "activation_plan_sha256"),
        ("rollback_plan_sha256", "rollback_plan_sha256"),
    ),
)
def test_resigned_mutation_partition_drift_is_rejected(
    document_name: str,
    digest_field: str,
) -> None:
    fixture = _fixture()
    plan = deepcopy(fixture.documents[document_name])
    plan["mutation_path_partitions"]["release_pointer_paths"] = [
        "/opt/adventico-ai-platform/forged-pointer"
    ]
    targets = sorted(
        {
            path
            for paths in plan["mutation_path_partitions"].values()
            for path in paths
        }
    )
    plan["mutation_target_paths"] = targets
    plan["mutation_target_count"] = len(targets)
    plan["mutation_target_set_sha256"] = inputs.sha256_bytes(
        inputs.canonical_bytes(targets)
    )
    _rehash(plan, digest_field)
    fixture.documents[document_name] = plan
    fixture.update_publication = _signed_update(
        fixture,
        {document_name: plan[digest_field]},
    )

    expected = (
        "release_update_inputs_activation_plan_invalid"
        if document_name == "activation_plan_sha256"
        else "release_update_inputs_rollback_plan_invalid"
    )
    with pytest.raises(
        inputs.ProductionReleaseUpdateInputsError,
        match=expected,
    ):
        _validate(fixture)


@pytest.mark.parametrize(
    ("document_name", "order_field", "digest_field"),
    (
        (
            "activation_plan_sha256",
            "forward_phase_order",
            "activation_plan_sha256",
        ),
        (
            "rollback_plan_sha256",
            "rollback_phase_order",
            "rollback_plan_sha256",
        ),
        (
            "rollback_plan_sha256",
            "preauthorized_rollback_phase_order",
            "rollback_plan_sha256",
        ),
    ),
)
def test_plan_phase_order_is_exact_and_cannot_be_resigned(
    document_name: str,
    order_field: str,
    digest_field: str,
) -> None:
    fixture = _fixture()
    plan = deepcopy(fixture.documents[document_name])
    plan[order_field][0], plan[order_field][1] = (
        plan[order_field][1],
        plan[order_field][0],
    )
    _rehash(plan, digest_field)
    fixture.documents[document_name] = plan
    fixture.update_publication = _signed_update(
        fixture,
        {document_name: plan[digest_field]},
    )

    expected = (
        "release_update_inputs_activation_plan_invalid"
        if document_name == "activation_plan_sha256"
        else "release_update_inputs_rollback_plan_invalid"
    )
    with pytest.raises(
        inputs.ProductionReleaseUpdateInputsError,
        match=expected,
    ):
        _validate(fixture)


@pytest.mark.parametrize(
    ("field", "replacement"),
    (
        (
            "unit_input_preauthorization_discriminator_phase",
            "target_started_disabled",
        ),
        (
            "unit_input_preauthorization_cancel_phase",
            "target_stopped",
        ),
        (
            "unit_input_preauthorization_cancel_before_host_restore",
            False,
        ),
    ),
)
def test_resigned_conditional_rollback_contract_drift_is_rejected(
    field: str,
    replacement: object,
) -> None:
    fixture = _fixture()
    rollback = deepcopy(fixture.documents["rollback_plan_sha256"])
    rollback[field] = replacement
    _rehash(rollback, "rollback_plan_sha256")
    fixture.documents["rollback_plan_sha256"] = rollback
    fixture.update_publication = _signed_update(
        fixture,
        {"rollback_plan_sha256": rollback["rollback_plan_sha256"]},
    )

    with pytest.raises(
        inputs.ProductionReleaseUpdateInputsError,
        match="release_update_inputs_rollback_plan_invalid",
    ):
        _validate(fixture)


def test_alias_wrong_revision_and_document_set_drift_are_rejected() -> None:
    fixture = _fixture()
    alias = deepcopy(fixture.documents["alias_artifact_index_sha256"])
    alias["release_revision"] = "3" * 40
    alias["release_root"] = (
        "/opt/adventico-ai-platform/hermes-agent-releases/"
        "hermes-agent-333333333333"
    )
    _rehash(alias, "package_sha256")
    fixture.documents["alias_artifact_index_sha256"] = alias
    fixture.update_publication = _signed_update(
        fixture,
        {"alias_artifact_index_sha256": alias["package_sha256"]},
    )

    with pytest.raises(
        inputs.ProductionReleaseUpdateInputsError,
        match="release_update_inputs_alias_index_invalid",
    ):
        _validate(fixture)

    fixture = _fixture()
    fixture.documents["unexpected"] = {}
    with pytest.raises(
        inputs.ProductionReleaseUpdateInputsError,
        match="release_update_inputs_document_set_invalid",
    ):
        _validate(fixture)
