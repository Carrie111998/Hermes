from __future__ import annotations

import copy
import hashlib
import io
import os
import runpy
import stat
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Mapping

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import (
    Ed25519PrivateKey,
)

from scripts.canary import owner_gate_foundation as foundation
from scripts.canary import owner_gate_preparation_readback as readback
from scripts.canary import owner_gate_stage0 as stage0
from scripts.canary import trusted_signer_provisioning as provisioning
from scripts.canary import trusted_signer_stage0 as host_runtime


REVISION = "a" * 40
SOURCE_TREE = "b" * 40


def _key_id(key: Ed25519PrivateKey) -> str:
    return hashlib.sha256(key.public_key().public_bytes_raw()).hexdigest()


def _manifest(
    *,
    cloud_key: Ed25519PrivateKey,
    host_key: Ed25519PrivateKey,
) -> dict[str, Any]:
    inventory: dict[str, Any] = {
        name: f"value-{name}"
        for name in stage0.INVENTORY_FIELDS
    }
    inventory.update({
        "schema": stage0.PACKAGE_SCHEMA,
        "release_revision": REVISION,
        "source_tree_oid": SOURCE_TREE,
        "foundation_source_revision": "c" * 40,
        "foundation_source_tree_oid": "d" * 40,
        "release_root": str(readback.OWNER_RELEASE_BASE / REVISION),
        "activation_performed": False,
        "cloud_mutation_performed": False,
        "generic_shell_entrypoint": False,
        "local_gcloud_runtime_fallback": False,
        "secret_material_recorded": False,
        "secret_digest_recorded": False,
        "resource_ancestor_chain": ["organizations/123456"],
    })
    manifest: dict[str, Any] = {
        **inventory,
        **{
            name: f"value-{name}"
            for name in stage0.MANIFEST_FIELDS - stage0.INVENTORY_FIELDS
        },
    }
    manifest.update({
        "package_inventory_sha256": foundation.sha256_json(inventory),
        "collector_public_key_ids": {
            "network": "1" * 64,
            "cloud": _key_id(cloud_key),
            "host": _key_id(host_key),
        },
        "caller_self_hash_is_authority": False,
    })
    unsigned = {
        name: item
        for name, item in manifest.items()
        if name != "package_sha256"
    }
    manifest["package_sha256"] = foundation.sha256_json(unsigned)
    return manifest


def _host_runtime_receipt(package_sha256: str) -> dict[str, Any]:
    release = host_runtime.HOST_RELEASE_BASE / REVISION
    unsigned = {
        "schema": host_runtime.HOST_RUNTIME_RECEIPT_SCHEMA,
        "release_revision": REVISION,
        "package_sha256": package_sha256,
        "preflight_sha256": "2" * 64,
        "release": {
            "path": str(release),
            "uid": 0,
            "gid": 0,
            "mode": "0555",
            "projection_sha256": "3" * 64,
            "projection_count": 37,
        },
        "sudoers": {
            "path": str(host_runtime.HOST_SUDOERS_PATH),
            "uid": 0,
            "gid": 0,
            "mode": "0440",
            "sha256": "4" * 64,
        },
        "runtime_inventory_sha256": "5" * 64,
        "runtime_interpreter": str(release / "venv/bin/python"),
        "host_attestor_entrypoint": str(
            release / "bin/muncho-host-observation-attestor"
        ),
        "host_provisioner_entrypoint": str(
            release / "bin/muncho-host-trusted-signer-provision"
        ),
        "offline_runtime": True,
        "network_install_required": False,
        "generic_usr_bin_python3_runtime": False,
        "current_link_absent": True,
        "activation_seal_absent": True,
        "service_start_performed": False,
        "service_enablement_mutated": False,
        "iam_mutation_performed": False,
        "cloud_mutation_performed": False,
        "private_key_material_received": False,
        "private_key_digest_recorded": False,
    }
    return {
        **unsigned,
        "receipt_sha256": foundation.sha256_json(unsigned),
    }


def _carrier_envelope(
    *,
    runtime: Mapping[str, Any],
    package: Mapping[str, Any],
    release_key: Ed25519PrivateKey,
) -> dict[str, Any]:
    carrier: dict[str, Any] = {
        "schema": readback.CARRIER_SCHEMA,
        "captured_phase": "inert",
        "allowed_rehydrate_phase": "post_iam",
        "release_revision": REVISION,
        "package_inventory_sha256": package[
            "package_inventory_sha256"
        ],
        "input_pins": {"schema": "test-pins"},
        "binding": {
            "source_tree_oid": SOURCE_TREE,
            "package_sha256": package["package_sha256"],
            "package_inventory_sha256": package[
                "package_inventory_sha256"
            ],
            "interpreter_sha256": "1" * 64,
            "cloud_collector_public_key_id": package[
                "collector_public_key_ids"
            ]["cloud"],
            "host_collector_public_key_id": package[
                "collector_public_key_ids"
            ]["host"],
            "bootstrap_pip_version": "24.0",
            "bootstrap_pip_sha256": "2" * 64,
            "kit_release_id": "3" * 64,
            "trusted_runner_path": "/opt/test/trusted-runner",
            "bundle_path": f"/opt/test/{REVISION}",
        },
        "foundation": {"schema": "test-foundation"},
        "terminal_receipt_sha256": "c" * 64,
        "terminal_artifact_sha256": "d" * 64,
        "host_runtime_receipt": dict(runtime),
        "cloud_signer_provisioning_receipt": {"role": "cloud"},
        "cloud_signer_provisioning_receipt_file_sha256": "e" * 64,
        "cloud_signer_readiness": {"role": "cloud"},
        "host_signer_provisioning_receipt": {"role": "host"},
        "host_signer_provisioning_receipt_file_sha256": "f" * 64,
        "host_signer_readiness": {"role": "host"},
        "freshness_asserted": False,
        "present_time_host_state_asserted": False,
        "present_time_iam_state_asserted": False,
        "prepared_under_iam_binding_present": False,
        "activation_performed": False,
        "cloud_mutation_performed": False,
        "service_activation_performed": False,
    }
    carrier["carrier_sha256"] = foundation.sha256_json(carrier)
    signed = {
        "schema": readback.CARRIER_ENVELOPE_SCHEMA,
        "carrier": carrier,
        "carrier_sha256": carrier["carrier_sha256"],
        "release_public_key_id": _key_id(release_key),
    }
    return {
        **signed,
        "signature_ed25519_b64url": readback._b64url_encode(
            release_key.sign(foundation.canonical_json_bytes(signed))
        ),
    }


def _install_receipt(package: Mapping[str, Any]) -> dict[str, Any]:
    receipt: dict[str, Any] = {
        name: "6" * 64
        for name in readback._INSTALL_RECEIPT_FIELDS
    }
    receipt.update({
        "schema": "muncho-owner-gate-offline-install-receipt.v1",
        "release_revision": REVISION,
        "source_tree_oid": SOURCE_TREE,
        "package_sha256": package["package_sha256"],
        "resource_ancestor_chain": package["resource_ancestor_chain"],
        "installed_at_unix": 1_800_000_000,
        "release_path": str(readback.OWNER_RELEASE_BASE / REVISION),
        "systemd_units_enabled": [],
        "current_release_selected": False,
        "activation_performed": False,
        "activation_seal_created": False,
        "iam_binding_created": False,
        "cloud_mutation_performed": False,
        "caddy_cutover_performed": False,
        "authority_receipt_public_key_id": "7" * 64,
        "signer_key_id": "7" * 64,
        "signature_ed25519_b64url": readback._b64url_encode(b"\x08" * 64),
    })
    for name in readback._INSTALL_LINEAGE_FIELDS:
        receipt[name] = package[name]
    signed = {
        name: item
        for name, item in receipt.items()
        if name not in {"signer_key_id", "signature_ed25519_b64url"}
    }
    unsigned = {
        name: item
        for name, item in signed.items()
        if name != "receipt_sha256"
    }
    receipt["receipt_sha256"] = foundation.sha256_json(unsigned)
    return receipt


def _readiness(
    *,
    role: str,
    package: Mapping[str, Any],
    receipt_sha256: str,
) -> dict[str, Any]:
    unsigned = {
        "schema": provisioning.READINESS_SCHEMA,
        "role": role,
        "release_revision": REVISION,
        "package_sha256": package["package_sha256"],
        "public_key_id": package["collector_public_key_ids"][role],
        "provisioning_receipt_sha256": receipt_sha256,
        "private_public_identity_matched": True,
        "config_exact": True,
        "replay_directory_exact": True,
        "sudoers_exact": True,
        "offline_runtime_exact": True,
        "activation_seal_absent": True,
        "current_link_absent": True,
        "services_inactive_disabled": True,
        "activation_performed": False,
        "iam_mutation_performed": False,
    }
    return {
        **unsigned,
        "readiness_sha256": foundation.sha256_json(unsigned),
    }


def _fixture() -> dict[str, Any]:
    cloud_key = Ed25519PrivateKey.generate()
    host_key = Ed25519PrivateKey.generate()
    release_key = Ed25519PrivateKey.generate()
    package = _manifest(cloud_key=cloud_key, host_key=host_key)
    runtime = _host_runtime_receipt(str(package["package_sha256"]))
    carrier = _carrier_envelope(
        runtime=runtime,
        package=package,
        release_key=release_key,
    )
    install = _install_receipt(package)
    cloud = _readiness(
        role="cloud",
        package=package,
        receipt_sha256="9" * 64,
    )
    host = _readiness(
        role="host",
        package=package,
        receipt_sha256="a" * 64,
    )
    request = readback.build_preparation_readback_request(
        release_revision=REVISION,
        carrier_envelope=carrier,
        release_public_key=release_key.public_key(),
        inert_evidence_set_sha256="d" * 64,
        iam_transaction_id="e" * 64,
    )
    expected_lineage = {
        "source_tree_oid": SOURCE_TREE,
        "package_sha256": package["package_sha256"],
        "package_inventory_sha256": package[
            "package_inventory_sha256"
        ],
        "install_receipt_sha256": install["receipt_sha256"],
        "install_receipt_file_sha256": hashlib.sha256(
            foundation.canonical_json_bytes(install)
        ).hexdigest(),
        "host_runtime_receipt_sha256": runtime["receipt_sha256"],
        "cloud_signer_provisioning_receipt_sha256": cloud[
            "provisioning_receipt_sha256"
        ],
        "cloud_signer_readiness_sha256": cloud["readiness_sha256"],
        "host_signer_provisioning_receipt_sha256": host[
            "provisioning_receipt_sha256"
        ],
        "host_signer_readiness_sha256": host["readiness_sha256"],
        "cloud_public_key_id": _key_id(cloud_key),
    }
    return {
        "cloud_key": cloud_key,
        "release_key": release_key,
        "package": package,
        "runtime": runtime,
        "carrier": carrier,
        "install": install,
        "cloud": cloud,
        "host": host,
        "request": request,
        "expected_lineage": expected_lineage,
    }


def _collect(
    fixture: Mapping[str, Any],
    monkeypatch: pytest.MonkeyPatch,
) -> Mapping[str, Any]:
    monkeypatch.setattr(readback.os, "geteuid", lambda: 0)
    monkeypatch.setattr(
        readback,
        "_load_current_package_and_install",
        lambda _revision: (
            fixture["package"],
            fixture["install"],
            foundation.canonical_json_bytes(fixture["install"]),
        ),
    )
    monkeypatch.setattr(
        readback,
        "_load_release_carrier_public_key",
        lambda **_kwargs: fixture["release_key"].public_key(),
    )
    monkeypatch.setattr(
        readback.host_runtime,
        "verify_host_offline_runtime",
        lambda _revision, *, expected_receipt: dict(expected_receipt),
    )
    monkeypatch.setattr(
        readback.provisioning,
        "verify_cloud_signer_inert_readiness",
        lambda _revision: fixture["cloud"],
    )
    monkeypatch.setattr(
        readback.provisioning,
        "verify_host_signer_runtime_readiness",
        lambda _revision: fixture["host"],
    )
    cloud_key = fixture["cloud_key"]
    monkeypatch.setattr(
        readback,
        "_load_cloud_signer",
        lambda *, expected_public_key_id: (
            cloud_key,
            cloud_key.public_key(),
            expected_public_key_id,
        ),
    )
    return readback.collect_preparation_readback(
        fixture["request"],
        release_revision=REVISION,
    )


def _resign(
    response: dict[str, Any],
    *,
    key: Ed25519PrivateKey,
) -> None:
    unsigned = {
        name: item
        for name, item in response.items()
        if name not in {"attestation", "response_sha256"}
    }
    response["response_sha256"] = foundation.sha256_json(unsigned)
    report = {
        name: item
        for name, item in response.items()
        if name != "attestation"
    }
    response["attestation"]["signature_ed25519_b64url"] = (
        readback._b64url_encode(
            key.sign(foundation.canonical_json_bytes(report))
        )
    )


def test_request_uses_a_fresh_exact_32_byte_challenge() -> None:
    first = _fixture()["request"]
    second = _fixture()["request"]

    assert first["challenge_b64url"] != second["challenge_b64url"]
    assert len(
        readback._b64url_decode(
            first["challenge_b64url"],
            size=32,
            code="test",
        )
    ) == 32
    assert first["phase"] == "post_iam"


def test_three_requests_keep_lineage_but_never_reuse_challenge_or_hash(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fixture = _fixture()
    requests = [
        readback.build_preparation_readback_request(
            release_revision=REVISION,
            carrier_envelope=fixture["carrier"],
            release_public_key=fixture["release_key"].public_key(),
            inert_evidence_set_sha256="d" * 64,
            iam_transaction_id="e" * 64,
        )
        for _ in range(3)
    ]
    responses = []
    for request in requests:
        current = {**fixture, "request": request}
        response = _collect(current, monkeypatch)
        responses.append(
            readback.validate_preparation_readback_response(
                response,
                request=request,
                cloud_public_key=fixture["cloud_key"].public_key(),
                expected_lineage=fixture["expected_lineage"],
            )
        )

    assert len({item["challenge_b64url"] for item in requests}) == 3
    assert len({item["request_sha256"] for item in requests}) == 3
    assert {
        (
            item["carrier_sha256"],
            item["terminal_receipt_sha256"],
            item["inert_evidence_set_sha256"],
            item["iam_transaction_id"],
        )
        for item in responses
    } == {
        (
            fixture["request"]["carrier_sha256"],
            fixture["request"]["terminal_receipt_sha256"],
            fixture["request"]["inert_evidence_set_sha256"],
            fixture["request"]["iam_transaction_id"],
        )
    }


def test_target_returns_signed_raw_facts_and_owner_validates_lineage(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fixture = _fixture()
    response = _collect(fixture, monkeypatch)

    checked = readback.validate_preparation_readback_response(
        response,
        request=fixture["request"],
        cloud_public_key=fixture["cloud_key"].public_key(),
        expected_lineage=fixture["expected_lineage"],
    )

    assert checked["host_runtime_receipt"] == fixture["runtime"]
    assert checked["cloud_signer_readiness"] == fixture["cloud"]
    assert "ok" not in checked
    assert "authorized" not in checked


def test_target_terminal_recheck_rejects_live_change_before_signing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fixture = _fixture()
    changed_install = copy.deepcopy(fixture["install"])
    changed_install["installed_at_unix"] += 1
    captures = 0
    signer_loads = 0

    monkeypatch.setattr(readback.os, "geteuid", lambda: 0)

    def load_live(_revision: str) -> tuple[Mapping[str, Any], Mapping[str, Any], bytes]:
        nonlocal captures
        install = fixture["install"] if captures == 0 else changed_install
        captures += 1
        return (
            fixture["package"],
            install,
            foundation.canonical_json_bytes(install),
        )

    monkeypatch.setattr(
        readback,
        "_load_current_package_and_install",
        load_live,
    )
    monkeypatch.setattr(
        readback,
        "_load_release_carrier_public_key",
        lambda **_kwargs: fixture["release_key"].public_key(),
    )
    monkeypatch.setattr(
        readback.host_runtime,
        "verify_host_offline_runtime",
        lambda _revision, *, expected_receipt: dict(expected_receipt),
    )
    monkeypatch.setattr(
        readback.provisioning,
        "verify_cloud_signer_inert_readiness",
        lambda _revision: fixture["cloud"],
    )
    monkeypatch.setattr(
        readback.provisioning,
        "verify_host_signer_runtime_readiness",
        lambda _revision: fixture["host"],
    )

    def load_signer(*, expected_public_key_id: str) -> tuple[Any, ...]:
        nonlocal signer_loads
        signer_loads += 1
        key = fixture["cloud_key"]
        return key, key.public_key(), expected_public_key_id

    monkeypatch.setattr(readback, "_load_cloud_signer", load_signer)

    with pytest.raises(
        readback.OwnerGatePreparationReadbackError,
        match="owner_gate_preparation_readback_live_state_changed",
    ):
        readback.collect_preparation_readback(
            fixture["request"],
            release_revision=REVISION,
        )

    assert captures == 2
    assert signer_loads == 1


@pytest.mark.parametrize(
    "changed_component",
    (
        "host_runtime",
        "cloud_readiness",
        "host_readiness",
        "signer_key_load_side_effect",
    ),
)
def test_target_terminal_recheck_rejects_other_live_projection_drift(
    changed_component: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fixture = _fixture()
    changed_runtime = copy.deepcopy(fixture["runtime"])
    changed_runtime["release"]["projection_sha256"] = "0" * 64
    changed_runtime["receipt_sha256"] = foundation.sha256_json({
        name: item
        for name, item in changed_runtime.items()
        if name != "receipt_sha256"
    })
    changed_cloud = copy.deepcopy(fixture["cloud"])
    changed_cloud["provisioning_receipt_sha256"] = "b" * 64
    changed_cloud["readiness_sha256"] = foundation.sha256_json({
        name: item
        for name, item in changed_cloud.items()
        if name != "readiness_sha256"
    })
    changed_host = copy.deepcopy(fixture["host"])
    changed_host["provisioning_receipt_sha256"] = "c" * 64
    changed_host["readiness_sha256"] = foundation.sha256_json({
        name: item
        for name, item in changed_host.items()
        if name != "readiness_sha256"
    })
    captures = 0
    signer_loaded = False

    monkeypatch.setattr(readback.os, "geteuid", lambda: 0)

    def load_live(
        _revision: str,
    ) -> tuple[Mapping[str, Any], Mapping[str, Any], bytes]:
        nonlocal captures
        captures += 1
        install = fixture["install"]
        if (
            changed_component == "signer_key_load_side_effect"
            and signer_loaded
        ):
            install = copy.deepcopy(install)
            install["installed_at_unix"] += 1
        return (
            fixture["package"],
            install,
            foundation.canonical_json_bytes(install),
        )

    monkeypatch.setattr(
        readback,
        "_load_current_package_and_install",
        load_live,
    )
    monkeypatch.setattr(
        readback,
        "_load_release_carrier_public_key",
        lambda **_kwargs: fixture["release_key"].public_key(),
    )
    monkeypatch.setattr(
        readback.host_runtime,
        "verify_host_offline_runtime",
        lambda _revision, *, expected_receipt: (
            changed_runtime
            if changed_component == "host_runtime" and captures == 2
            else dict(expected_receipt)
        ),
    )
    monkeypatch.setattr(
        readback.provisioning,
        "verify_cloud_signer_inert_readiness",
        lambda _revision: (
            changed_cloud
            if changed_component == "cloud_readiness" and captures == 2
            else fixture["cloud"]
        ),
    )
    monkeypatch.setattr(
        readback.provisioning,
        "verify_host_signer_runtime_readiness",
        lambda _revision: (
            changed_host
            if changed_component == "host_readiness" and captures == 2
            else fixture["host"]
        ),
    )

    def load_signer(*, expected_public_key_id: str) -> tuple[Any, ...]:
        nonlocal signer_loaded
        signer_loaded = True
        key = fixture["cloud_key"]
        return key, key.public_key(), expected_public_key_id

    monkeypatch.setattr(readback, "_load_cloud_signer", load_signer)

    with pytest.raises(
        readback.OwnerGatePreparationReadbackError,
        match="owner_gate_preparation_readback_live_state_changed",
    ):
        readback.collect_preparation_readback(
            fixture["request"],
            release_revision=REVISION,
        )

    assert captures == 2
    assert signer_loaded is True


def test_target_rejects_caller_rehashed_runtime_before_host_code(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fixture = _fixture()
    request = copy.deepcopy(fixture["request"])
    carrier = request["carrier_envelope"]["carrier"]
    runtime = carrier["host_runtime_receipt"]
    runtime["release"]["projection_sha256"] = "0" * 64
    runtime["receipt_sha256"] = foundation.sha256_json({
        name: item
        for name, item in runtime.items()
        if name != "receipt_sha256"
    })
    carrier["carrier_sha256"] = foundation.sha256_json({
        name: item
        for name, item in carrier.items()
        if name != "carrier_sha256"
    })
    request["carrier_envelope"]["carrier_sha256"] = carrier[
        "carrier_sha256"
    ]
    request["carrier_sha256"] = carrier["carrier_sha256"]
    request["request_sha256"] = foundation.sha256_json({
        name: item
        for name, item in request.items()
        if name != "request_sha256"
    })
    host_code_calls = 0

    monkeypatch.setattr(readback.os, "geteuid", lambda: 0)
    monkeypatch.setattr(
        readback,
        "_load_current_package_and_install",
        lambda _revision: (
            fixture["package"],
            fixture["install"],
            foundation.canonical_json_bytes(fixture["install"]),
        ),
    )
    monkeypatch.setattr(
        readback,
        "_load_release_carrier_public_key",
        lambda **_kwargs: fixture["release_key"].public_key(),
    )

    def host_code(*_args: object, **_kwargs: object) -> Mapping[str, Any]:
        nonlocal host_code_calls
        host_code_calls += 1
        return fixture["runtime"]

    monkeypatch.setattr(
        readback.host_runtime,
        "verify_host_offline_runtime",
        host_code,
    )
    with pytest.raises(
        readback.OwnerGatePreparationReadbackError,
        match="owner_gate_preparation_readback_carrier_invalid",
    ):
        readback.collect_preparation_readback(
            request,
            release_revision=REVISION,
        )

    assert host_code_calls == 0


def test_owner_rejects_signed_nested_lineage_substitution(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fixture = _fixture()
    response = copy.deepcopy(_collect(fixture, monkeypatch))
    response["cloud_signer_readiness"][
        "provisioning_receipt_sha256"
    ] = "f" * 64
    readiness_unsigned = {
        name: item
        for name, item in response["cloud_signer_readiness"].items()
        if name != "readiness_sha256"
    }
    response["cloud_signer_readiness"]["readiness_sha256"] = (
        foundation.sha256_json(readiness_unsigned)
    )
    _resign(response, key=fixture["cloud_key"])

    with pytest.raises(
        readback.OwnerGatePreparationReadbackError,
        match="owner_gate_preparation_readback_lineage_invalid",
    ):
        readback.validate_preparation_readback_response(
            response,
            request=fixture["request"],
            cloud_public_key=fixture["cloud_key"].public_key(),
            expected_lineage=fixture["expected_lineage"],
        )


def test_owner_binds_historical_runtime_receipt_to_frozen_lineage(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fixture = _fixture()
    response = _collect(fixture, monkeypatch)
    wrong_lineage = {
        **fixture["expected_lineage"],
        "host_runtime_receipt_sha256": "0" * 64,
    }

    with pytest.raises(
        readback.OwnerGatePreparationReadbackError,
        match="owner_gate_preparation_readback_lineage_invalid",
    ):
        readback.validate_preparation_readback_response(
            response,
            request=fixture["request"],
            cloud_public_key=fixture["cloud_key"].public_key(),
            expected_lineage=wrong_lineage,
        )


@pytest.mark.parametrize("case", ("signature", "pinned_key"))
def test_owner_rejects_wrong_attestation_authority(
    case: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fixture = _fixture()
    response = copy.deepcopy(_collect(fixture, monkeypatch))
    public_key = fixture["cloud_key"].public_key()
    if case == "signature":
        response["attestation"]["signature_ed25519_b64url"] = (
            readback._b64url_encode(b"\x00" * 64)
        )
    else:
        public_key = Ed25519PrivateKey.generate().public_key()

    with pytest.raises(
        readback.OwnerGatePreparationReadbackError,
        match="owner_gate_preparation_readback_response_invalid",
    ):
        readback.validate_preparation_readback_response(
            response,
            request=fixture["request"],
            cloud_public_key=public_key,
            expected_lineage=fixture["expected_lineage"],
        )


def test_owner_rejects_even_signed_remote_boolean_authority(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fixture = _fixture()
    response = copy.deepcopy(_collect(fixture, monkeypatch))
    response["ok"] = True
    _resign(response, key=fixture["cloud_key"])

    with pytest.raises(
        readback.OwnerGatePreparationReadbackError,
        match="owner_gate_preparation_readback_response_invalid",
    ):
        readback.validate_preparation_readback_response(
            response,
            request=fixture["request"],
            cloud_public_key=fixture["cloud_key"].public_key(),
            expected_lineage=fixture["expected_lineage"],
        )


@pytest.mark.parametrize(
    "raw",
    (
        b'{"a":1, "b":2}\n',
        b'{"a":1,"a":1}\n',
        b"{}\n{}\n",
    ),
)
def test_stdin_requires_one_exact_canonical_frame(raw: bytes) -> None:
    with pytest.raises(
        readback.OwnerGatePreparationReadbackError,
        match="owner_gate_preparation_readback_stdin_invalid",
    ):
        readback._read_stdin(io.BytesIO(raw))


def test_stdin_is_strictly_bounded() -> None:
    with pytest.raises(
        readback.OwnerGatePreparationReadbackError,
        match="owner_gate_preparation_readback_stdin_invalid",
    ):
        readback._read_stdin(
            io.BytesIO(b"x" * (readback.MAX_FRAME_BYTES + 2))
        )


def test_stdout_is_strictly_bounded() -> None:
    with pytest.raises(
        readback.OwnerGatePreparationReadbackError,
        match="owner_gate_preparation_readback_stdout_invalid",
    ):
        readback._write_stdout(
            io.BytesIO(),
            {"payload": "x" * readback.MAX_FRAME_BYTES},
        )


def test_sha_fields_are_strings_not_json_numbers() -> None:
    assert readback._is_sha256("1" * 64)
    assert not readback._is_sha256(int("1" * 64))


@pytest.mark.parametrize(
    ("field", "replacement"),
    (
        ("challenge_b64url", readback._b64url_encode(b"\xff" * 32)),
        ("carrier_sha256", "0" * 64),
        ("terminal_receipt_sha256", "1" * 64),
        ("inert_evidence_set_sha256", "2" * 64),
        ("iam_transaction_id", "3" * 64),
    ),
)
def test_owner_rejects_signed_request_binding_substitution(
    field: str,
    replacement: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fixture = _fixture()
    response = copy.deepcopy(_collect(fixture, monkeypatch))
    response[field] = replacement
    _resign(response, key=fixture["cloud_key"])

    with pytest.raises(
        readback.OwnerGatePreparationReadbackError,
        match="owner_gate_preparation_readback_response_invalid",
    ):
        readback.validate_preparation_readback_response(
            response,
            request=fixture["request"],
            cloud_public_key=fixture["cloud_key"].public_key(),
            expected_lineage=fixture["expected_lineage"],
        )


def test_fixed_wrapper_accepts_only_root_exact_release(
    tmp_path: Path,
) -> None:
    wrapper = (
        Path(__file__).parents[3]
        / "ops/muncho/owner-gate/bin/"
        "muncho-owner-gate-preparation-readback"
    )
    namespace = runpy.run_path(str(wrapper), run_name="readback_wrapper_test")
    validator = namespace["_validated_release"]
    install = tmp_path / "opt/muncho-owner-gate"
    releases = install / "releases"
    release = releases / REVISION
    entrypoint = release / "bin" / wrapper.name
    interpreter = release / "venv/bin/python"
    entrypoint.parent.mkdir(parents=True)
    interpreter.parent.mkdir(parents=True)
    entrypoint.write_bytes(b"fixed wrapper\n")
    interpreter.write_bytes(b"fixed interpreter\n")
    install.chmod(0o755)
    releases.chmod(0o755)
    release.chmod(0o555)
    entrypoint.chmod(0o555)
    interpreter.chmod(0o555)

    def root_lstat(path: os.PathLike[str] | str) -> SimpleNamespace:
        state = os.lstat(path)
        return SimpleNamespace(
            st_mode=state.st_mode,
            st_uid=0,
            st_gid=0,
            st_nlink=state.st_nlink,
        )

    flags = SimpleNamespace(
        isolated=1,
        ignore_environment=1,
        no_user_site=1,
        safe_path=True,
        dont_write_bytecode=1,
    )
    common = {
        "entrypoint": entrypoint,
        "executable": interpreter,
        "releases_root": releases,
        "lstat_fn": root_lstat,
        "getuid_fn": lambda: 0,
        "geteuid_fn": lambda: 0,
        "getgid_fn": lambda: 0,
        "getegid_fn": lambda: 0,
        "flags": flags,
    }
    assert validator(**common, getgroups_fn=lambda: []) == release
    with pytest.raises(
        SystemExit,
        match="owner_gate_preparation_readback_runtime_invalid",
    ):
        validator(**common, getgroups_fn=lambda: [1])
