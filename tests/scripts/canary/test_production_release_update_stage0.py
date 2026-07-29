from __future__ import annotations

import hashlib
import json
import os
import stat
import sys
from copy import deepcopy
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import (
    Ed25519PrivateKey,
)

from scripts.canary import production_release_builder_phase as builder_phase
from scripts.canary import production_release_builder_runtime as builder
from scripts.canary import production_release_update_contract as contract
from scripts.canary import production_release_update_inputs as update_inputs
from scripts.canary import production_release_update_stage0 as stage0
from scripts.canary import production_release_unit_inputs_v4 as unit_v4
from tests.scripts.canary import (
    test_production_release_host_observer as host_test,
)
from tests.scripts.canary import (
    test_production_release_unit_inputs_v4 as v4_test,
)
from tests.scripts.canary import (
    test_production_release_update_inputs as input_test,
)


NOW = 1_900_000_000
PREDECESSOR = "1" * 40
TARGET = "2" * 40
BUILDER_UID = 29104
BUILDER_GID = 29104
_BASE_V4_PAYLOAD = v4_test._payload()
RUNTIME_UIDS = tuple(_BASE_V4_PAYLOAD["reserved_runtime_uids"])
RESERVED_GIDS = list(_BASE_V4_PAYLOAD["reserved_runtime_gids"])


@dataclass
class Fixture:
    roots: stage0.Stage0Roots
    private: Ed25519PrivateKey
    trust: dict[str, Any]
    plan_values: dict[str, Any]
    publication: dict[str, Any]
    release_root: Path
    activation_receipt_sha256: str
    input_file_sha256: dict[str, str]
    input_internal_identities: dict[str, str]


def _canonical_line(value: Mapping[str, Any]) -> bytes:
    return contract.canonical_bytes(value) + b"\n"


def _write_json(path: Path, value: Mapping[str, Any]) -> str:
    raw = _canonical_line(value)
    path.write_bytes(raw)
    path.chmod(0o444)
    return hashlib.sha256(raw).hexdigest()


def _replace_json(path: Path, value: Mapping[str, Any]) -> None:
    path.chmod(0o644)
    path.write_bytes(_canonical_line(value))
    path.chmod(0o444)


def _authority(
    private: Ed25519PrivateKey | None = None,
) -> tuple[Ed25519PrivateKey, dict[str, Any]]:
    private = private or Ed25519PrivateKey.generate()
    public = private.public_key().public_bytes(
        encoding=serialization.Encoding.Raw,
        format=serialization.PublicFormat.Raw,
    )
    trust = contract.build_predecessor_trust(
        release_revision=PREDECESSOR,
        authority_plan_sha256="01" * 32,
        authority_approval_sha256="02" * 32,
        fixed_inputs_sha256="03" * 32,
        activation_receipt_sha256="04" * 32,
        owner_subject_sha256="b" * 64,
        owner_public_key_ed25519_hex=public.hex(),
        owner_key_id=contract.sha256_bytes(public),
    )
    return private, dict(trust)


def _approval(
    private: Ed25519PrivateKey,
    plan: Mapping[str, Any],
    *,
    issued_at_unix: int = NOW - 10,
    expires_at_unix: int = NOW + 300,
) -> dict[str, Any]:
    unsigned = {
        "schema": contract.APPROVAL_SCHEMA,
        "purpose": contract.APPROVAL_PURPOSE,
        "plan_sha256": plan["plan_sha256"],
        "predecessor_revision": plan["predecessor_revision"],
        "release_revision": plan["release_revision"],
        "owner_subject_sha256": plan["owner_subject_sha256"],
        "owner_public_key_ed25519_hex": plan[
            "owner_public_key_ed25519_hex"
        ],
        "owner_key_id": plan["owner_key_id"],
        "nonce_sha256": "16" * 32,
        "issued_at_unix": issued_at_unix,
        "expires_at_unix": expires_at_unix,
        "approved": True,
    }
    signature = private.sign(contract.canonical_bytes(unsigned)).hex()
    signed = {**unsigned, "signature_ed25519_hex": signature}
    return {
        **signed,
        "approval_sha256": contract.sha256_bytes(
            contract.canonical_bytes(signed)
        ),
    }


def _process_free_evidence() -> dict[str, Any]:
    unsigned = {
        "schema": builder.PROCESS_FREE_EVIDENCE_SCHEMA,
        "unit": "muncho-release-builder@tx-1.service",
        "fragment_path": (
            "/etc/systemd/system/muncho-release-builder@.service"
        ),
        "fragment_sha256": "a" * 64,
        "drop_in_paths": [],
        "wrapper_path": "/usr/libexec/muncho-release-builder-phase",
        "wrapper_sha256": "c" * 64,
        "invocation_id": "b" * 32,
        "systemd_state": {
            "load": "loaded",
            "active": "inactive",
            "sub": "dead",
            "result": "success",
            "main_pid": 0,
            "exec_main_pid": 0,
            "exec_main_code": "exited",
            "exec_main_status": 0,
        },
        "control_group": (
            "/system.slice/muncho-release-builder@tx-1.service"
        ),
        "cgroup_status": "removed",
        "inspected_cgroups": [],
        "builder_uid": BUILDER_UID,
        "builder_gid": BUILDER_GID,
        "builder_uid_pids_before": [],
        "builder_uid_pids_after": [],
        "secret_material_recorded": False,
        "secret_digest_recorded": False,
    }
    record = {
        **unsigned,
        "evidence_sha256": hashlib.sha256(
            contract.canonical_bytes(unsigned)
        ).hexdigest(),
    }
    return dict(
        builder.build_process_free_evidence_set(
            record,
            record,
            builder_uid=BUILDER_UID,
            builder_gid=BUILDER_GID,
        )
    )


def _publish_release(release_root: Path) -> Mapping[str, Any]:
    interpreter = release_root / ".venv/bin/python"
    interpreter.parent.mkdir(parents=True, mode=0o700)
    interpreter.write_bytes(b"not-an-executed-interpreter\n")
    interpreter.chmod(0o700)
    entrypoint = (
        release_root
        / "scripts/canary/production_release_update_entrypoint.py"
    )
    entrypoint.parent.mkdir(parents=True, mode=0o700)
    entrypoint.write_bytes(
        b"raise RuntimeError('candidate code must not execute in stage 0')\n"
    )
    entrypoint.chmod(0o600)
    payload = release_root / "package/payload.dat"
    payload.parent.mkdir(parents=True, mode=0o700)
    payload.write_bytes(b"sealed payload\n")
    payload.chmod(0o600)
    identities = builder.ReleaseIdentities(
        builder_uid=BUILDER_UID,
        builder_gid=BUILDER_GID,
        reserved_runtime_uids=RUNTIME_UIDS,
        reserved_runtime_gids=tuple(RESERVED_GIDS),
    )
    return builder._publish_release_filesystem(
        release_root,
        revision=TARGET,
        identities=identities,
        process_free_evidence=_process_free_evidence(),
        staging_uid=os.geteuid(),
        staging_gid=os.lstat(release_root).st_gid,
        publication_uid=os.geteuid(),
        publication_gid=os.lstat(release_root.parent).st_gid,
        _xattr_reader=lambda _descriptor: (),
    )


def _self_hash(
    value: Mapping[str, Any],
    digest_field: str,
) -> dict[str, Any]:
    unsigned = {
        key: item
        for key, item in value.items()
        if key != digest_field
    }
    return {
        **unsigned,
        digest_field: builder_phase.sha256_bytes(
            builder_phase.canonical_bytes(unsigned)
        ),
    }


def _build_publication(
    *,
    private: Ed25519PrivateKey,
    trust: Mapping[str, Any],
    plan_values: Mapping[str, Any],
    expires_at_unix: int = NOW + 300,
) -> dict[str, Any]:
    plan = contract.build_plan(
        trusted_predecessor=trust,
        expected_predecessor_trust_sha256=str(trust["trust_sha256"]),
        values=plan_values,
    )
    approval = _approval(
        private,
        plan,
        expires_at_unix=expires_at_unix,
    )
    return dict(
        contract.build_publication(
            plan=plan,
            approval=approval,
            trusted_predecessor=trust,
            expected_predecessor_trust_sha256=str(
                trust["trust_sha256"]
            ),
            now_unix=NOW,
        )
    )


def _fixture(tmp_path: Path) -> Fixture:
    base = tmp_path.resolve()
    authority_root = base / "authority"
    input_root = base / "inputs"
    pin_path = base / "external/predecessor-trust.sha256"
    release_parent = base / "releases"
    for directory in (
        authority_root,
        input_root,
        pin_path.parent,
        release_parent,
    ):
        directory.mkdir(parents=True, exist_ok=True)
        directory.chmod(0o755)
    roots = stage0.Stage0Roots(
        authority_root=authority_root,
        input_root=input_root,
        external_pin_path=pin_path,
        release_root_parent=release_parent,
    )

    release_root = release_parent / f"hermes-agent-{TARGET[:12]}"
    release_root.mkdir(mode=0o700)
    receipt = _publish_release(release_root)
    manifest = json.loads(
        (release_root / builder.MANIFEST_NAME).read_text()
    )

    input_file_sha256: dict[str, str] = {}
    uv = input_root / stage0._BINARY_INPUTS["uv_sha256"]
    uv.write_bytes(b"sealed uv binary\n")
    uv.chmod(0o555)
    input_file_sha256["uv_sha256"] = hashlib.sha256(
        uv.read_bytes()
    ).hexdigest()
    source_tree_oid = "3" * 40
    interpreter = release_root / contract.INTERPRETER_RELATIVE_PATH
    entrypoint = release_root / contract.ENTRYPOINT_RELATIVE_PATH
    interpreter_sha256 = hashlib.sha256(
        interpreter.read_bytes()
    ).hexdigest()
    entrypoint_sha256 = hashlib.sha256(entrypoint.read_bytes()).hexdigest()
    builder_identity = {
        "user": "muncho-release-builder",
        "group": "muncho-release-builder",
        "uid": BUILDER_UID,
        "gid": BUILDER_GID,
    }

    source_manifest = _self_hash(
        {
            "schema": builder_phase.SOURCE_V3_MANIFEST_SCHEMA,
            "release_revision": TARGET,
            "source_tree_oid": source_tree_oid,
            "object_format": "sha1",
            "tree_listing_name": builder_phase.TREE_LISTING_NAME,
            "tree_listing_sha256": "31" * 32,
            "tree_listing_size": 1,
            "tree_entry_count": 1,
            "blob_directory_name": (
                builder_phase.SOURCE_BLOB_DIRECTORY_NAME
            ),
            "blobs": [
                {
                    "object_id": "4" * 40,
                    "filename": f"{'4' * 40}.blob",
                    "sha256": "32" * 32,
                    "size": 0,
                }
            ],
            "secret_material_recorded": False,
            "secret_digest_recorded": False,
        },
        "manifest_sha256",
    )
    input_file_sha256["source_v3_manifest_sha256"] = _write_json(
        input_root / stage0._JSON_INPUTS["source_v3_manifest_sha256"],
        source_manifest,
    )
    runtime_manifest = _self_hash(
        {
            "schema": builder_phase.RUNTIME_DEPENDENCY_MANIFEST_SCHEMA,
            "release_revision": TARGET,
            "wheel_directory_name": (
                builder_phase.RUNTIME_WHEEL_DIRECTORY_NAME
            ),
            "wheels": [
                {
                    "filename": "muncho_runtime-1.0-py3-none-any.whl",
                    "sha256": "33" * 32,
                    "size": 1,
                }
            ],
            "installation": dict(builder_phase._INSTALLATION),
            "secret_material_recorded": False,
            "secret_digest_recorded": False,
        },
        "manifest_sha256",
    )
    input_file_sha256["runtime_dependency_manifest_sha256"] = _write_json(
        input_root
        / stage0._JSON_INPUTS["runtime_dependency_manifest_sha256"],
        runtime_manifest,
    )
    request = _self_hash(
        {
            "schema": builder_phase.REQUEST_SCHEMA,
            "job_id": TARGET,
            "release_revision": TARGET,
            "source_tree_oid": source_tree_oid,
            "source_v3_manifest_name": builder_phase.SOURCE_MANIFEST_NAME,
            "source_v3_manifest_sha256": input_file_sha256[
                "source_v3_manifest_sha256"
            ],
            "runtime_dependency_manifest_name": (
                builder_phase.RUNTIME_MANIFEST_NAME
            ),
            "runtime_dependency_manifest_sha256": input_file_sha256[
                "runtime_dependency_manifest_sha256"
            ],
            "uv_name": builder_phase.UV_NAME,
            "uv_sha256": input_file_sha256["uv_sha256"],
            "uv_size": uv.stat().st_size,
            "python_executable_path": "/usr/bin/python3.11",
            "python_executable_sha256": interpreter_sha256,
            "python_executable_size": interpreter.stat().st_size,
            "candidate_name": builder_phase.CANDIDATE_NAME,
            "interpreter_relative_path": (
                builder_phase.INTERPRETER_RELATIVE_PATH
            ),
            "entrypoint_relative_path": (
                builder_phase.ENTRYPOINT_RELATIVE_PATH
            ),
            "builder_identity": builder_identity,
            "resume_policy": (
                "reject-nonempty-output-requires-root-cleanup"
            ),
            "secret_material_recorded": False,
            "secret_digest_recorded": False,
        },
        "request_sha256",
    )
    input_file_sha256["builder_request_sha256"] = _write_json(
        input_root / stage0._JSON_INPUTS["builder_request_sha256"],
        request,
    )
    terminal = _self_hash(
        {
            "schema": builder_phase.TERMINAL_RECEIPT_SCHEMA,
            "release_revision": TARGET,
            "candidate_name": builder_phase.CANDIDATE_NAME,
            "source_tree_oid": source_tree_oid,
            "builder_request_sha256": input_file_sha256[
                "builder_request_sha256"
            ],
            "builder_request_identity_sha256": request["request_sha256"],
            "source_v3_manifest_sha256": input_file_sha256[
                "source_v3_manifest_sha256"
            ],
            "source_v3_manifest_identity_sha256": source_manifest[
                "manifest_sha256"
            ],
            "runtime_dependency_manifest_sha256": input_file_sha256[
                "runtime_dependency_manifest_sha256"
            ],
            "runtime_dependency_manifest_identity_sha256": (
                runtime_manifest["manifest_sha256"]
            ),
            "uv_sha256": input_file_sha256["uv_sha256"],
            "python_executable_sha256": interpreter_sha256,
            "source_materialization_sha256": "34" * 32,
            "retained_wheels_sha256": "35" * 32,
            "payload_manifest_name": builder_phase.PAYLOAD_MANIFEST_NAME,
            "payload_manifest_sha256": "36" * 32,
            "payload_manifest_file_sha256": "37" * 32,
            "payload_tree_sha256": "38" * 32,
            "interpreter_relative_path": (
                builder_phase.INTERPRETER_RELATIVE_PATH
            ),
            "interpreter_sha256": interpreter_sha256,
            "entrypoint_relative_path": (
                builder_phase.ENTRYPOINT_RELATIVE_PATH
            ),
            "entrypoint_sha256": entrypoint_sha256,
            "venv_argv_sha256": "39" * 32,
            "install_argv_sha256": "3a" * 32,
            "command_environment_sha256": "3b" * 32,
            "builder_identity": builder_identity,
            "resume_policy": (
                "reject-nonempty-output-requires-root-cleanup"
            ),
            "terminal": True,
            "secret_material_recorded": False,
            "secret_digest_recorded": False,
        },
        "receipt_sha256",
    )
    input_file_sha256["builder_terminal_receipt_sha256"] = _write_json(
        input_root
        / stage0._JSON_INPUTS["builder_terminal_receipt_sha256"],
        terminal,
    )
    input_file_sha256["candidate_seal_receipt_sha256"] = _write_json(
        input_root
        / stage0._JSON_INPUTS["candidate_seal_receipt_sha256"],
        receipt,
    )

    private, trust = _authority()
    payload = dict(
        unit_v4.build_payload(
            v3_payload=v4_test._v3_payload(),
            builder_identity=builder_identity,
            builder_terminal_receipt_sha256=terminal["receipt_sha256"],
            whole_tree_manifest_sha256=manifest["manifest_sha256"],
            candidate_seal_receipt_sha256=receipt["receipt_sha256"],
            runtime_dependency_manifest_sha256=input_file_sha256[
                "runtime_dependency_manifest_sha256"
            ],
        )
    )
    unit_plan, unit_approval, unit_publication = v4_test._unit_documents(
        private,
        trust,
        payload,
    )
    host_receipt = dict(
        host_test._observe(host_test._harness()).receipt
    )
    consumer_set = update_inputs.build_release_consumer_set(
        predecessor_revision=PREDECESSOR,
        release_revision=TARGET,
    )
    host_manifest = input_test._host_manifest(
        payload,
        unit_plan,
        unit_approval,
    )
    cron_index = input_test._cron_index()
    alias_index = input_test._alias_index()
    artifact_identities = {
        "host_inventory_sha256": host_receipt["receipt_sha256"],
        "release_consumer_set_sha256": consumer_set[
            "consumer_set_sha256"
        ],
        "host_artifact_manifest_sha256": host_manifest[
            "manifest_sha256"
        ],
        "cron_artifact_index_sha256": cron_index[
            "artifact_index_sha256"
        ],
        "alias_artifact_index_sha256": alias_index["package_sha256"],
        "successor_unit_input_publication_sha256": unit_publication[
            "publication_sha256"
        ],
    }
    activation = update_inputs.build_activation_plan(
        predecessor_revision=PREDECESSOR,
        release_revision=TARGET,
        artifact_identities=artifact_identities,
    )
    rollback = update_inputs.build_rollback_plan(
        predecessor_revision=PREDECESSOR,
        release_revision=TARGET,
        artifact_identities=artifact_identities,
    )
    semantic_documents = {
        "host_inventory_sha256": host_receipt,
        "release_consumer_set_sha256": consumer_set,
        "host_artifact_manifest_sha256": host_manifest,
        "cron_artifact_index_sha256": cron_index,
        "alias_artifact_index_sha256": alias_index,
        "successor_unit_input_publication_sha256": unit_publication,
        "activation_plan_sha256": activation,
        "rollback_plan_sha256": rollback,
    }
    for field, document in semantic_documents.items():
        input_file_sha256[field] = _write_json(
            input_root / stage0._JSON_INPUTS[field],
            document,
        )
    input_internal_identities = {
        "builder_terminal_receipt_sha256": terminal["receipt_sha256"],
        "candidate_seal_receipt_sha256": receipt["receipt_sha256"],
        **artifact_identities,
        "activation_plan_sha256": activation[
            "activation_plan_sha256"
        ],
        "rollback_plan_sha256": rollback["rollback_plan_sha256"],
    }
    plan_values: dict[str, Any] = {
        "release_revision": TARGET,
        "release_root": contract.expected_release_root(TARGET),
        "source_tree_oid": source_tree_oid,
        "source_v3_manifest_sha256": input_file_sha256[
            "source_v3_manifest_sha256"
        ],
        "builder_request_sha256": input_file_sha256[
            "builder_request_sha256"
        ],
        "runtime_dependency_manifest_sha256": input_file_sha256[
            "runtime_dependency_manifest_sha256"
        ],
        "uv_sha256": input_file_sha256["uv_sha256"],
        **input_internal_identities,
        "whole_tree_manifest_sha256": manifest["manifest_sha256"],
        "interpreter_relative_path": contract.INTERPRETER_RELATIVE_PATH,
        "interpreter_sha256": interpreter_sha256,
        "entrypoint_relative_path": contract.ENTRYPOINT_RELATIVE_PATH,
        "entrypoint_sha256": entrypoint_sha256,
        "builder_identity": payload["builder_identity"],
        "release_owner": {
            "uid": payload["release_owner_uid"],
            "gid": payload["release_owner_gid"],
        },
        "reserved_runtime_uids": payload["reserved_runtime_uids"],
        "reserved_runtime_gids": payload["reserved_runtime_gids"],
        "created_at_unix": NOW - 30,
    }
    publication = _build_publication(
        private=private,
        trust=trust,
        plan_values=plan_values,
    )
    _write_json(
        authority_root / stage0.PREDECESSOR_TRUST_NAME,
        trust,
    )
    _write_json(
        authority_root / stage0.UPDATE_PUBLICATION_NAME,
        publication,
    )
    pin_path.write_bytes(
        f"{trust['trust_sha256']}\n".encode("ascii")
    )
    pin_path.chmod(0o444)
    return Fixture(
        roots=roots,
        private=private,
        trust=trust,
        plan_values=plan_values,
        publication=publication,
        release_root=release_root,
        activation_receipt_sha256=str(
            trust["activation_receipt_sha256"]
        ),
        input_file_sha256=input_file_sha256,
        input_internal_identities=input_internal_identities,
    )


def _verify(
    fixture: Fixture,
    *,
    now_unix: int = NOW,
    cas: str | None = None,
) -> stage0.VerifiedLaunchBundle:
    return stage0._verify_stage0_for_test(
        roots=fixture.roots,
        expected_predecessor_activation_receipt_sha256=(
            fixture.activation_receipt_sha256 if cas is None else cas
        ),
        now_unix=now_unix,
        expected_uid=os.geteuid(),
        expected_gid=os.lstat(fixture.roots.authority_root).st_gid,
    )


def _replace_publication(
    fixture: Fixture,
    *,
    changes: Mapping[str, Any],
) -> None:
    values = deepcopy(fixture.plan_values)
    values.update(changes)
    publication = _build_publication(
        private=fixture.private,
        trust=fixture.trust,
        plan_values=values,
    )
    _replace_json(
        fixture.roots.authority_root / stage0.UPDATE_PUBLICATION_NAME,
        publication,
    )


@pytest.mark.parametrize("attribute", ["geteuid", "getegid"])
def test_posix_identity_helpers_fail_closed_when_unavailable(
    monkeypatch: pytest.MonkeyPatch,
    attribute: str,
) -> None:
    monkeypatch.delattr(stage0.os, attribute, raising=False)
    helper = (
        stage0._posix_effective_uid
        if attribute == "geteuid"
        else stage0._posix_effective_gid
    )

    with pytest.raises(
        stage0.ProductionReleaseUpdateStage0Error,
        match="release_update_stage0_contract_invalid",
    ):
        helper(failure_code="release_update_stage0_contract_invalid")


def test_stage0_returns_only_held_verified_descriptors(
    tmp_path: Path,
) -> None:
    fixture = _fixture(tmp_path)

    with _verify(fixture) as bundle:
        assert bundle.publication["release_revision"] == TARGET
        assert bundle.builder_receipt["terminal"] is True
        assert (
            bundle.input_internal_identities
            == fixture.input_internal_identities
        )
        assert bundle.fixed_v4_inputs[
            "release_update_publication_sha256"
        ] == bundle.publication["publication_sha256"]
        assert bundle.interpreter_descriptor >= 0
        assert bundle.entrypoint_descriptor >= 0
        assert stat.S_ISREG(bundle.interpreter_identity.mode)
        assert stat.S_ISREG(bundle.entrypoint_identity.mode)
        for field, identity in fixture.input_internal_identities.items():
            assert bundle.publication["plan"][field] == identity
            assert bundle.held_files[field].sha256 == (
                fixture.input_file_sha256[field]
            )
            assert identity != fixture.input_file_sha256[field]
        for field in stage0._FILE_DIGEST_JSON_INPUTS:
            assert bundle.publication["plan"][field] == (
                bundle.held_files[field].sha256
            )
        assert bundle.publication["plan"]["interpreter_sha256"] == (
            bundle.held_files["interpreter"].sha256
        )
        assert bundle.publication["plan"]["entrypoint_sha256"] == (
            bundle.held_files["entrypoint"].sha256
        )
        bundle.assert_stable()

    with pytest.raises(
        stage0.ProductionReleaseUpdateStage0Error,
        match="bundle_closed",
    ):
        bundle.assert_stable()


@pytest.mark.parametrize(
    "field",
    tuple(stage0._INTERNAL_IDENTITY_FIELDS),
)
def test_signed_plan_cannot_use_file_hash_as_internal_identity(
    tmp_path: Path,
    field: str,
) -> None:
    fixture = _fixture(tmp_path)
    file_sha256 = fixture.input_file_sha256[field]
    assert file_sha256 != fixture.input_internal_identities[field]
    _replace_publication(
        fixture,
        changes={field: file_sha256},
    )

    with pytest.raises(stage0.ProductionReleaseUpdateStage0Error):
        _verify(fixture)


@pytest.mark.parametrize(
    ("field", "identity_field"),
    (
        ("source_v3_manifest_sha256", "manifest_sha256"),
        ("builder_request_sha256", "request_sha256"),
        ("runtime_dependency_manifest_sha256", "manifest_sha256"),
    ),
)
def test_signed_plan_cannot_use_document_identity_as_file_hash(
    tmp_path: Path,
    field: str,
    identity_field: str,
) -> None:
    fixture = _fixture(tmp_path)
    document = json.loads(
        (
            fixture.roots.input_root / stage0._JSON_INPUTS[field]
        ).read_text()
    )
    internal_identity = document[identity_field]
    assert internal_identity != fixture.input_file_sha256[field]
    _replace_publication(
        fixture,
        changes={field: internal_identity},
    )

    with pytest.raises(
        stage0.ProductionReleaseUpdateStage0Error,
        match="file_invalid",
    ):
        _verify(fixture)


@pytest.mark.parametrize(
    "field",
    ("interpreter_sha256", "entrypoint_sha256"),
)
def test_executable_plan_fields_are_physical_file_hashes(
    tmp_path: Path,
    field: str,
) -> None:
    fixture = _fixture(tmp_path)
    _replace_publication(
        fixture,
        changes={field: fixture.input_internal_identities[
            "builder_terminal_receipt_sha256"
        ]},
    )

    with pytest.raises(
        stage0.ProductionReleaseUpdateStage0Error,
        match="(builder_binding|file)_invalid",
    ):
        _verify(fixture)


def test_forged_trust_and_signed_envelope_fail_external_pin(
    tmp_path: Path,
) -> None:
    fixture = _fixture(tmp_path)
    forged_private, forged_trust = _authority()
    forged_publication = _build_publication(
        private=forged_private,
        trust=forged_trust,
        plan_values=fixture.plan_values,
    )
    _replace_json(
        fixture.roots.authority_root / stage0.PREDECESSOR_TRUST_NAME,
        forged_trust,
    )
    _replace_json(
        fixture.roots.authority_root / stage0.UPDATE_PUBLICATION_NAME,
        forged_publication,
    )

    with pytest.raises(stage0.ProductionReleaseUpdateStage0Error):
        _verify(fixture)


def test_expired_owner_approval_fails_closed(tmp_path: Path) -> None:
    fixture = _fixture(tmp_path)

    with pytest.raises(stage0.ProductionReleaseUpdateStage0Error):
        _verify(fixture, now_unix=NOW + 301)


def test_predecessor_activation_receipt_cas_drift_fails(
    tmp_path: Path,
) -> None:
    fixture = _fixture(tmp_path)

    with pytest.raises(
        stage0.ProductionReleaseUpdateStage0Error,
        match="predecessor_cas_mismatch",
    ):
        _verify(fixture, cas="f" * 64)


@pytest.mark.parametrize("kind", ["symlink", "hardlink", "fifo"])
def test_plan_bound_input_rejects_symlink_hardlink_and_special_file(
    tmp_path: Path,
    kind: str,
) -> None:
    fixture = _fixture(tmp_path)
    target = fixture.roots.input_root / "host-inventory.json"
    if kind == "symlink":
        original = fixture.roots.input_root / "host-inventory.original"
        target.rename(original)
        target.symlink_to(original)
    elif kind == "hardlink":
        os.link(target, fixture.roots.input_root / "host-inventory.alias")
    else:
        target.unlink()
        os.mkfifo(target, 0o444)

    with pytest.raises(stage0.ProductionReleaseUpdateStage0Error):
        _verify(fixture)


def test_held_bundle_detects_plan_input_path_swap(tmp_path: Path) -> None:
    fixture = _fixture(tmp_path)
    bundle = _verify(fixture)
    target = fixture.roots.input_root / "host-inventory.json"
    original = fixture.roots.input_root / "host-inventory.original"
    target.rename(original)
    target.write_bytes(original.read_bytes())
    target.chmod(0o444)
    try:
        with pytest.raises(
            stage0.ProductionReleaseUpdateStage0Error,
            match="(directory|release)_drift",
        ):
            bundle.assert_stable()
    finally:
        bundle.close()


def test_stage0_detects_input_path_swap_during_verification(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fixture = _fixture(tmp_path)
    real_verify = stage0._verify_release
    target = fixture.roots.input_root / "host-inventory.json"

    def swapping_verify(*args: Any, **kwargs: Any) -> Mapping[str, Any]:
        receipt = real_verify(*args, **kwargs)
        original = fixture.roots.input_root / "host-inventory.original"
        target.rename(original)
        target.write_bytes(original.read_bytes())
        target.chmod(0o444)
        return receipt

    monkeypatch.setattr(stage0, "_verify_release", swapping_verify)
    with pytest.raises(
        stage0.ProductionReleaseUpdateStage0Error,
        match="(directory|release)_drift",
    ):
        _verify(fixture)


def test_held_bundle_detects_one_byte_input_drift(tmp_path: Path) -> None:
    fixture = _fixture(tmp_path)
    bundle = _verify(fixture)
    target = fixture.roots.input_root / "host-inventory.json"
    raw = target.read_bytes()
    target.chmod(0o644)
    target.write_bytes(bytes([raw[0] ^ 1]) + raw[1:])
    target.chmod(0o444)
    try:
        with pytest.raises(
            stage0.ProductionReleaseUpdateStage0Error,
            match="release_drift",
        ):
            bundle.assert_stable()
    finally:
        bundle.close()


def test_owner_signed_noncanonical_json_still_fails_closed(
    tmp_path: Path,
) -> None:
    fixture = _fixture(tmp_path)
    field = "host_inventory_sha256"
    target = fixture.roots.input_root / stage0._JSON_INPUTS[field]
    value = json.loads(target.read_text())
    raw = json.dumps(value, indent=2, sort_keys=True).encode("utf-8") + b"\n"
    target.chmod(0o644)
    target.write_bytes(raw)
    target.chmod(0o444)
    _replace_publication(
        fixture,
        changes={field: hashlib.sha256(raw).hexdigest()},
    )

    with pytest.raises(
        stage0.ProductionReleaseUpdateStage0Error,
        match="json_invalid",
    ):
        _verify(fixture)


def test_extended_metadata_is_rejected_and_rechecked_on_held_bundle(
    tmp_path: Path,
) -> None:
    fixture = _fixture(tmp_path)

    with pytest.raises(
        stage0.ProductionReleaseUpdateStage0Error,
        match="extended_metadata_invalid",
    ):
        stage0._verify_stage0_for_test(
            roots=fixture.roots,
            expected_predecessor_activation_receipt_sha256=(
                fixture.activation_receipt_sha256
            ),
            now_unix=NOW,
            expected_uid=os.geteuid(),
            expected_gid=os.lstat(fixture.roots.authority_root).st_gid,
            xattr_reader=lambda _descriptor: ("user.injected",),
        )

    metadata_present = False

    def changing_reader(_descriptor: int) -> tuple[str, ...]:
        return ("user.injected",) if metadata_present else ()

    bundle = stage0._verify_stage0_for_test(
        roots=fixture.roots,
        expected_predecessor_activation_receipt_sha256=(
            fixture.activation_receipt_sha256
        ),
        now_unix=NOW,
        expected_uid=os.geteuid(),
        expected_gid=os.lstat(fixture.roots.authority_root).st_gid,
        xattr_reader=changing_reader,
    )
    metadata_present = True
    try:
        with pytest.raises(
            stage0.ProductionReleaseUpdateStage0Error,
            match="extended_metadata_invalid",
        ):
            bundle.assert_stable()
    finally:
        bundle.close()


def test_held_bundle_detects_whole_release_root_path_swap(
    tmp_path: Path,
) -> None:
    fixture = _fixture(tmp_path)
    bundle = _verify(fixture)
    original = fixture.release_root.with_name(
        f"{fixture.release_root.name}.original"
    )
    fixture.release_root.rename(original)
    fixture.release_root.mkdir(mode=0o555)
    try:
        with pytest.raises(
            stage0.ProductionReleaseUpdateStage0Error,
            match="directory_drift",
        ):
            bundle.assert_stable()
    finally:
        bundle.close()


@pytest.mark.parametrize(
    "relative_path",
    [
        contract.INTERPRETER_RELATIVE_PATH,
        contract.ENTRYPOINT_RELATIVE_PATH,
    ],
)
def test_held_bundle_detects_interpreter_or_entrypoint_replacement(
    tmp_path: Path,
    relative_path: str,
) -> None:
    fixture = _fixture(tmp_path)
    bundle = _verify(fixture)
    target = fixture.release_root / relative_path
    original = target.with_name(f"{target.name}.original")
    fixture.release_root.chmod(0o755)
    target.parent.chmod(0o755)
    target.rename(original)
    target.write_bytes(original.read_bytes())
    target.chmod(stat.S_IMODE(original.stat().st_mode))
    target.parent.chmod(0o555)
    fixture.release_root.chmod(0o555)
    try:
        with pytest.raises(
            stage0.ProductionReleaseUpdateStage0Error,
            match="(directory|release)_drift",
        ):
            bundle.assert_stable()
    finally:
        bundle.close()


@pytest.mark.parametrize(
    "field",
    [
        "builder_terminal_receipt_sha256",
        "whole_tree_manifest_sha256",
    ],
)
def test_signed_plan_rejects_wrong_builder_receipt_or_manifest(
    tmp_path: Path,
    field: str,
) -> None:
    fixture = _fixture(tmp_path)
    _replace_publication(fixture, changes={field: "f" * 64})

    with pytest.raises(
        stage0.ProductionReleaseUpdateStage0Error,
        match="(builder_binding|semantic_binding|release_binding)_invalid",
    ):
        _verify(fixture)


def test_owner_signed_builder_receipt_with_cross_binding_drift_fails(
    tmp_path: Path,
) -> None:
    fixture = _fixture(tmp_path)
    field = "builder_terminal_receipt_sha256"
    target = fixture.roots.input_root / stage0._JSON_INPUTS[field]
    receipt = json.loads(target.read_text())
    receipt["source_tree_oid"] = "5" * 40
    receipt = _self_hash(receipt, "receipt_sha256")
    _replace_json(target, receipt)
    _replace_publication(
        fixture,
        changes={field: receipt["receipt_sha256"]},
    )

    with pytest.raises(
        stage0.ProductionReleaseUpdateStage0Error,
        match="builder_binding_invalid",
    ):
        _verify(fixture)


def test_owner_signed_candidate_seal_not_equal_to_release_fails(
    tmp_path: Path,
) -> None:
    fixture = _fixture(tmp_path)
    field = "candidate_seal_receipt_sha256"
    target = fixture.roots.input_root / stage0._JSON_INPUTS[field]
    receipt = json.loads(target.read_text())
    receipt["process_free_evidence_sha256"] = "f" * 64
    receipt = _self_hash(receipt, "receipt_sha256")
    _replace_json(target, receipt)
    _replace_publication(
        fixture,
        changes={field: receipt["receipt_sha256"]},
    )

    with pytest.raises(
        stage0.ProductionReleaseUpdateStage0Error,
        match="(semantic_binding|release_binding)_invalid",
    ):
        _verify(fixture)


def test_release_payload_one_byte_drift_fails_full_tree_verification(
    tmp_path: Path,
) -> None:
    fixture = _fixture(tmp_path)
    payload = fixture.release_root / "package/payload.dat"
    fixture.release_root.chmod(0o755)
    payload.parent.chmod(0o755)
    payload.chmod(0o644)
    raw = payload.read_bytes()
    payload.write_bytes(raw[:-2] + b"X\n")
    payload.chmod(0o444)
    payload.parent.chmod(0o555)
    fixture.release_root.chmod(0o555)

    with pytest.raises(stage0.ProductionReleaseUpdateStage0Error):
        _verify(fixture)


def test_public_production_entrypoint_exposes_no_test_roots_or_clock() -> None:
    with pytest.raises(TypeError):
        stage0.verify_stage0(
            roots=stage0.production_roots(),
            expected_predecessor_activation_receipt_sha256="a" * 64,
            now_unix=NOW,
        )
