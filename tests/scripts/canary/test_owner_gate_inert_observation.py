from __future__ import annotations

import base64
import copy
import hashlib
import inspect
import os
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Mapping, cast

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from scripts.canary import full_canary_owner_launcher as launcher
from scripts.canary import owner_gate_activation_seal as activation_seal
from scripts.canary import owner_gate_foundation as foundation
from scripts.canary import owner_gate_inert_observation as inert
from scripts.canary import owner_gate_outer_stage0 as outer
from tests.scripts.canary import test_owner_gate_preflight as preflight_fixture


REVISION = "a" * 40
FOUNDATION_REVISION = "b" * 40
RELEASE_TREE = "a" * 40
FOUNDATION_TREE = "c" * 40
KIT_RELEASE_ID = "b" * 64


def _owner_directory(path: Path) -> None:
    path.mkdir()
    os.chown(path, -1, os.getegid())
    path.chmod(0o700)


def _terminal_receipt(
    *,
    binding: inert._ReleaseBinding,
    loaded: SimpleNamespace,
) -> dict[str, Any]:
    ancestry = loaded.chain.foundation_a.ancestry_evidence
    install_unsigned = {
        "schema": "muncho-owner-gate-offline-install-receipt.v1",
        "release_revision": binding.release_revision,
        "package_sha256": binding.package["package_sha256"],
        "source_tree_oid": binding.source_tree_oid,
        "pre_foundation_authority_sha256": (
            loaded.chain.pre_foundation_authority_sha256
        ),
        "foundation_apply_receipt_sha256": (
            loaded.chain.foundation_apply_receipt_sha256
        ),
        "project_ancestry_evidence_sha256": (
            ancestry.signed_evidence_sha256
        ),
        "project_ancestry_chain_sha256": ancestry.value[
            "stable_chain_sha256"
        ],
        "resource_ancestor_chain": [
            item["resource_name"] for item in ancestry.ordered_chain[1:]
        ],
        "installed_at_unix": preflight_fixture.NOW - 10,
        "release_path": str(
            inert.stage0_iap.cloud_stage0.RELEASE_BASE
            / binding.release_revision
        ),
        "release_tree_sha256": "1" * 64,
        "transaction_prefix_sha256": "2" * 64,
        "phase_evidence_sha256": {
            name: f"{index:x}" * 64
            for index, name in enumerate(
                inert.stage0_iap._INSTALL_PHASES_WITHOUT_RECEIPT,
                start=1,
            )
        },
        "authority_receipt_public_key_sha256": "3" * 64,
        "authority_receipt_public_key_id": "4" * 64,
        "credential_id_sha256": "5" * 64,
        "executor_hosts_receipt_sha256": "6" * 64,
        "current_release_selected": False,
        "systemd_units_enabled": [],
        "activation_performed": False,
        "activation_seal_created": False,
        "iam_binding_created": False,
        "cloud_mutation_performed": False,
        "caddy_cutover_performed": False,
    }
    install_receipt = {
        **install_unsigned,
        "receipt_sha256": foundation.sha256_json(install_unsigned),
        "signer_key_id": "4" * 64,
        "signature_ed25519_b64url": base64.urlsafe_b64encode(
            b"s" * 64
        )
        .rstrip(b"=")
        .decode("ascii"),
    }
    terminal_unsigned = {
        "schema": inert.stage0_iap.INERT_CLOUD_BUNDLE_TERMINAL_SCHEMA,
        "release_sha": binding.release_revision,
        "source_tree_oid": binding.source_tree_oid,
        "package_sha256": binding.package["package_sha256"],
        "kit_release_id": KIT_RELEASE_ID,
        "trusted_runner_path": str(
            outer.RELEASE_BASE / KIT_RELEASE_ID / outer.TRUSTED_RUNNER
        ),
        "bundle_path": str(
            outer.BUNDLE_INCOMING_BASE / binding.release_revision
        ),
        "pre_foundation_authority_sha256": (
            loaded.chain.pre_foundation_authority_sha256
        ),
        "foundation_apply_receipt_sha256": (
            loaded.chain.foundation_apply_receipt_sha256
        ),
        "project_ancestry_evidence_sha256": (
            ancestry.signed_evidence_sha256
        ),
        "project_ancestry_chain_sha256": ancestry.value[
            "stable_chain_sha256"
        ],
        "resource_ancestor_chain": [
            item["resource_name"] for item in ancestry.ordered_chain[1:]
        ],
        "operation_order": [
            "transport_exact_stage0_and_bundle",
            "cloud-verify",
            "cloud-preflight",
            "cloud-install",
        ],
        "transport_receipt_sha256": "7" * 64,
        "cloud_verify_receipt_sha256": "8" * 64,
        "cloud_preflight_receipt_sha256": "9" * 64,
        "cloud_install_receipt_sha256": install_receipt[
            "receipt_sha256"
        ],
        "cloud_install_receipt_file_sha256": hashlib.sha256(
            inert._canonical(install_receipt)
        ).hexdigest(),
        "cloud_install_receipt": install_receipt,
        "cloud_install_signature_framing_validated": True,
        "cloud_install_signature_cryptographically_verified": False,
        "inert_cloud_bundle_installed": True,
        "host_filesystem_materialization_performed": True,
        "current_release_selected": False,
        "systemd_units_enabled": [],
        "service_activation_performed": False,
        "activation_performed": False,
        "activation_seal_created": False,
        "iam_binding_created": False,
        "caddy_cutover_performed": False,
        "cloud_mutation_performed": False,
        "cloud_control_plane_mutation_performed": False,
    }
    return {
        **terminal_unsigned,
        "terminal_receipt_sha256": foundation.sha256_json(
            terminal_unsigned
        ),
    }


def _attest_with_terminal(
    observation: Mapping[str, Any],
    *,
    key: Ed25519PrivateKey,
    terminal_receipt_sha256: str,
    host_observation_report_sha256: str | None = None,
) -> dict[str, Any]:
    body = {
        name: copy.deepcopy(item)
        for name, item in observation.items()
        if name not in {"report_sha256", "attestation"}
    }
    if "release_binding" in body:
        body["release_binding"]["terminal_receipt_sha256"] = (
            terminal_receipt_sha256
        )
        if host_observation_report_sha256 is not None:
            body["release_binding"]["host_observation_report_sha256"] = (
                host_observation_report_sha256
            )
    else:
        body["release"]["terminal_receipt_sha256"] = (
            terminal_receipt_sha256
        )
    return preflight_fixture._attest(body, key)


def _stream_source(path: Path, payload: bytes) -> Path:
    path.mkdir()
    member = path / "member.bin"
    member.write_bytes(payload)
    member.chmod(0o444)
    return path


def _fixed_inputs(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> dict[str, Any]:
    owner_home = tmp_path / "owner"
    hermes = owner_home / ".hermes"
    input_root = hermes / "owner-gate-inert-observation-inputs"
    release_root = input_root / REVISION
    for path in (owner_home, hermes, input_root, release_root):
        _owner_directory(path)

    monkeypatch.setattr(inert, "OWNER_HOME", owner_home)
    monkeypatch.setattr(inert, "INPUT_ROOT", input_root)

    kit_build = outer.write_tree_stream(
        _stream_source(tmp_path / "kit-source", b"exact outer stage zero\n"),
        release_root / inert.KIT_STREAM_NAME,
        purpose="outer-stage0-kit",
        release_id=KIT_RELEASE_ID,
    )
    bundle_build = outer.write_tree_stream(
        _stream_source(tmp_path / "bundle-source", b"exact owner bundle\n"),
        release_root / inert.BUNDLE_STREAM_NAME,
        purpose="owner-gate-bundle",
        release_id=REVISION,
    )
    unsigned_pins = {
        "schema": inert.INPUT_PINS_SCHEMA,
        "release_revision": REVISION,
        "kit_release_id": KIT_RELEASE_ID,
        "kit_tree_manifest_sha256": kit_build["stream_manifest_sha256"],
        "kit_stream_sha256": hashlib.sha256(
            (release_root / inert.KIT_STREAM_NAME).read_bytes()
        ).hexdigest(),
        "bundle_tree_manifest_sha256": bundle_build["stream_manifest_sha256"],
        "bundle_stream_sha256": hashlib.sha256(
            (release_root / inert.BUNDLE_STREAM_NAME).read_bytes()
        ).hexdigest(),
    }
    pins = {
        **unsigned_pins,
        "pins_sha256": foundation.sha256_json(unsigned_pins),
    }
    pins_path = release_root / inert.PINS_NAME
    pins_path.write_bytes(foundation.canonical_json_bytes(pins))
    pins_path.chmod(0o400)
    return {
        "owner_home": owner_home,
        "hermes": hermes,
        "input_root": input_root,
        "release_root": release_root,
        "pins": pins,
        "pins_path": pins_path,
        "kit_path": release_root / inert.KIT_STREAM_NAME,
        "bundle_path": release_root / inert.BUNDLE_STREAM_NAME,
    }


def _rewrite(path: Path, raw: bytes, *, mode: int = 0o400) -> None:
    path.chmod(0o600)
    path.write_bytes(raw)
    path.chmod(mode)


def test_fixed_release_root_and_mode_0400_pins_load_exactly(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    prepared = _fixed_inputs(tmp_path, monkeypatch)

    loaded = inert._PinnedObservationInputs.load(REVISION)

    assert loaded.release_root == prepared["release_root"]
    assert loaded.pins == prepared["pins"]
    assert Path(loaded.kit_stream.path) == prepared["kit_path"]
    assert Path(loaded.bundle_stream.path) == prepared["bundle_path"]
    assert stat_mode(prepared["input_root"]) == 0o700
    assert stat_mode(prepared["release_root"]) == 0o700
    assert stat_mode(prepared["pins_path"]) == 0o400
    assert stat_mode(prepared["kit_path"]) == 0o400
    assert stat_mode(prepared["bundle_path"]) == 0o400
    loaded.assert_stable()


def stat_mode(path: Path) -> int:
    return path.stat(follow_symlinks=False).st_mode & 0o777


@pytest.mark.parametrize(
    "attack",
    (
        "pins_symlink",
        "pins_hardlink",
        "pins_mode",
        "pins_tamper",
        "kit_symlink",
        "kit_hardlink",
        "kit_mode",
        "kit_tamper",
        "root_mode",
        "release_symlink",
    ),
)
def test_fixed_inputs_reject_alias_mode_and_byte_tampering(
    attack: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    prepared = _fixed_inputs(tmp_path, monkeypatch)
    pins_path: Path = prepared["pins_path"]
    kit_path: Path = prepared["kit_path"]

    if attack == "pins_symlink":
        target = tmp_path / "pins-target"
        target.write_bytes(pins_path.read_bytes())
        target.chmod(0o400)
        pins_path.unlink()
        pins_path.symlink_to(target)
    elif attack == "pins_hardlink":
        os.link(pins_path, prepared["release_root"] / "pins-alias")
    elif attack == "pins_mode":
        pins_path.chmod(0o600)
    elif attack == "pins_tamper":
        _rewrite(pins_path, pins_path.read_bytes() + b"\n")
    elif attack == "kit_symlink":
        target = tmp_path / "kit-target"
        target.write_bytes(kit_path.read_bytes())
        target.chmod(0o400)
        kit_path.unlink()
        kit_path.symlink_to(target)
    elif attack == "kit_hardlink":
        os.link(kit_path, prepared["release_root"] / "kit-alias")
    elif attack == "kit_mode":
        kit_path.chmod(0o600)
    elif attack == "kit_tamper":
        raw = bytearray(kit_path.read_bytes())
        raw[-1] ^= 1
        _rewrite(kit_path, bytes(raw))
    elif attack == "root_mode":
        prepared["input_root"].chmod(0o755)
    elif attack == "release_symlink":
        release_root: Path = prepared["release_root"]
        moved = tmp_path / "moved-release"
        release_root.rename(moved)
        release_root.symlink_to(moved, target_is_directory=True)
    else:  # pragma: no cover - table is deliberately closed above
        raise AssertionError(attack)

    with pytest.raises(launcher.OwnerLauncherError):
        inert._PinnedObservationInputs.load(REVISION)


@pytest.mark.parametrize("change", ("pins", "kit", "release_inventory"))
def test_loaded_inputs_reject_every_later_identity_change(
    change: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    prepared = _fixed_inputs(tmp_path, monkeypatch)
    loaded = inert._PinnedObservationInputs.load(REVISION)

    if change == "pins":
        raw = bytearray(prepared["pins_path"].read_bytes())
        raw[-1] = ord("0") if raw[-1] != ord("0") else ord("1")
        _rewrite(prepared["pins_path"], bytes(raw))
    elif change == "kit":
        raw = prepared["kit_path"].read_bytes()
        prepared["kit_path"].unlink()
        prepared["kit_path"].write_bytes(raw)
        prepared["kit_path"].chmod(0o400)
    else:
        extra = prepared["release_root"] / "unexpected"
        extra.write_bytes(b"unexpected\n")
        extra.chmod(0o400)

    with pytest.raises(launcher.OwnerLauncherError):
        loaded.assert_stable()


def test_fixed_inputs_replay_with_identical_pins_and_stream_manifests(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _fixed_inputs(tmp_path, monkeypatch)

    first = inert._PinnedObservationInputs.load(REVISION)
    second = inert._PinnedObservationInputs.load(REVISION)

    assert first.pins_raw == second.pins_raw
    assert first.pins == second.pins
    assert first.kit_stream.manifest_raw == second.kit_stream.manifest_raw
    assert first.bundle_stream.manifest_raw == second.bundle_stream.manifest_raw
    assert first.kit_stream._fingerprint == second.kit_stream._fingerprint
    assert first.bundle_stream._fingerprint == second.bundle_stream._fingerprint
    first.assert_stable()
    second.assert_stable()


def _fixed_evidence_store(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    network_collected_at: int = preflight_fixture.NOW - 1,
) -> SimpleNamespace:
    monkeypatch.setattr(
        preflight_fixture.trust,
        "PINNED_RELEASE_TRUST_PUBLIC_KEY_SHA256",
        preflight_fixture.RELEASE_KEY_ID,
    )
    monkeypatch.setattr(
        inert.preflight.ingress,
        "PINNED_RELEASE_TRUST_PUBLIC_KEY_SHA256",
        preflight_fixture.RELEASE_KEY_ID,
    )
    owner_home = tmp_path / "owner"
    hermes = owner_home / ".hermes"
    for path in (owner_home, hermes):
        _owner_directory(path)
    evidence_root = hermes / "owner-gate-inert-observation-evidence"
    monkeypatch.setattr(inert, "OWNER_HOME", owner_home)
    monkeypatch.setattr(inert, "EVIDENCE_ROOT", evidence_root)
    phase_root = inert._evidence_phase_root(REVISION)

    network_key = Ed25519PrivateKey.generate()
    cloud_key = Ed25519PrivateKey.generate()
    host_key = Ed25519PrivateKey.generate()
    network_mapping = preflight_fixture._signed_network_evidence(
        network_key,
        collected_at=network_collected_at,
    )
    network_key_id = hashlib.sha256(
        network_key.public_key().public_bytes_raw()
    ).hexdigest()
    network_evidence = foundation.ProductionNetworkEvidence.from_mapping(
        network_mapping,
        public_key=network_key.public_key(),
        expected_public_key_id=network_key_id,
        now_unix=network_collected_at,
    )
    plan = foundation.build_plan(
        spec=foundation.OwnerGateSpec(
            release_revision=REVISION,
            boot_image_self_link=preflight_fixture.IMAGE,
            package_inventory_sha256="5" * 64,
            interpreter_sha256="6" * 64,
            network_collector_public_key_id=network_key_id,
            organization_id="123456789012",
            ancestry_evidence_sha256="9" * 64,
            cloud_collector_public_key_id=hashlib.sha256(
                cloud_key.public_key().public_bytes_raw()
            ).hexdigest(),
            host_collector_public_key_id=hashlib.sha256(
                host_key.public_key().public_bytes_raw()
            ).hexdigest(),
        ),
        network_evidence=network_evidence,
        network_collector_public_key=network_key.public_key(),
        now_unix=network_collected_at,
    )
    production_ingress_observation = (
        preflight_fixture._production_ingress_envelope(plan, iam=False)
    )
    inputs = SimpleNamespace(
        pins={
            "pins_sha256": "d" * 64,
            "kit_release_id": KIT_RELEASE_ID,
        }
    )
    ancestry = SimpleNamespace(
        signed_evidence_sha256="9" * 64,
        value={"stable_chain_sha256": "a" * 64},
        ordered_chain=(
            {"resource_name": f"projects/{foundation.PROJECT}"},
            {"resource_name": plan.spec.organization_resource},
        ),
    )
    chain = SimpleNamespace(
        foundation_source_revision=FOUNDATION_REVISION,
        foundation_source_tree_oid=FOUNDATION_TREE,
        pre_foundation_authority_sha256="7" * 64,
        foundation_apply_receipt_sha256="8" * 64,
        foundation_a=SimpleNamespace(ancestry_evidence=ancestry),
    )
    loaded = SimpleNamespace(chain=chain)
    package = {
        "package_sha256": "3" * 64,
        "package_inventory_sha256": "5" * 64,
        "interpreter_sha256": "6" * 64,
    }
    binding = inert._ReleaseBinding(
        release_revision=REVISION,
        source_tree_oid=RELEASE_TREE,
        package=package,
        authority={
            "boot_image_self_link": plan.spec.boot_image_self_link,
            "project_ancestry_evidence_sha256": "9" * 64,
            "resource_ancestor_chain": [plan.spec.organization_resource],
            "collector_public_key_ids": {
                "network": plan.spec.network_collector_public_key_id,
                "cloud": plan.spec.cloud_collector_public_key_id,
                "host": plan.spec.host_collector_public_key_id,
            },
        },
        direct_iam_raw=b"direct-iam",
        foundation_source_revision=FOUNDATION_REVISION,
        foundation_source_tree_oid=FOUNDATION_TREE,
        release_public_key=preflight_fixture.RELEASE_KEY.public_key(),
    )
    terminal_receipt = _terminal_receipt(
        binding=binding,
        loaded=loaded,
    )
    host_observation = _attest_with_terminal(
        preflight_fixture._host(plan, host_key, iam=False),
        key=host_key,
        terminal_receipt_sha256=terminal_receipt[
            "terminal_receipt_sha256"
        ],
    )
    cloud_observation = _attest_with_terminal(
        preflight_fixture._cloud(plan, cloud_key, iam=False),
        key=cloud_key,
        terminal_receipt_sha256=terminal_receipt[
            "terminal_receipt_sha256"
        ],
        host_observation_report_sha256=host_observation[
            "report_sha256"
        ],
    )
    report = inert.preflight.build_preflight_report(
        plan=plan,
        production_ingress_observation=production_ingress_observation,
        release_public_key=preflight_fixture.RELEASE_KEY.public_key(),
        cloud_observation=cloud_observation,
        host_observation=host_observation,
        cloud_collector_public_key=cloud_key.public_key(),
        host_collector_public_key=host_key.public_key(),
        now_unix=preflight_fixture.NOW,
    )
    receipt, payloads = inert._build_receipt(
        binding=binding,
        inputs=inputs,  # type: ignore[arg-type]
        loaded=loaded,  # type: ignore[arg-type]
        network_evidence=network_mapping,
        plan=plan,
        production_ingress_observation=production_ingress_observation,
        terminal_receipt=terminal_receipt,
        cloud_observation=cloud_observation,
        host_observation=host_observation,
        report=report,
    )
    return SimpleNamespace(
        phase_root=phase_root,
        inputs=inputs,
        loaded=loaded,
        binding=binding,
        package=package,
        plan=plan,
        network_mapping=network_mapping,
        network_evidence=network_evidence,
        network_key=network_key,
        cloud_key=cloud_key,
        host_key=host_key,
        production_ingress_observation=production_ingress_observation,
        cloud_observation=cloud_observation,
        host_observation=host_observation,
        terminal_receipt=terminal_receipt,
        report=report,
        receipt=receipt,
        payloads=payloads,
    )


def _load_fixed_transaction(fixed: SimpleNamespace, *, now_unix: int):
    return inert._load_evidence_transaction(
        phase_root=fixed.phase_root,
        transaction_name=fixed.receipt["evidence_set_sha256"],
        binding=fixed.binding,
        inputs=fixed.inputs,
        loaded=fixed.loaded,
        network_key=fixed.network_key.public_key(),
        cloud_key=fixed.cloud_key.public_key(),
        host_key=fixed.host_key.public_key(),
        now_unix=now_unix,
    )


def _frozen_fixed_transaction(fixed: SimpleNamespace) -> inert._FrozenInertEvidence:
    inert._publish_evidence(
        phase_root=fixed.phase_root,
        receipt=fixed.receipt,
        payloads=fixed.payloads,
    )
    transaction_root = (
        fixed.phase_root / fixed.receipt["evidence_set_sha256"]
    )
    evidence_raw: dict[str, bytes] = {}
    evidence_identities: dict[str, tuple[Any, ...]] = {}
    evidence: dict[str, Mapping[str, Any]] = {}
    for name in inert._TRANSACTION_EVIDENCE_NAMES:
        identity, raw = inert._read_pinned_file(
            transaction_root / name,
            maximum=inert.MAX_EVIDENCE_BYTES,
            code="test_invalid",
        )
        evidence_identities[name] = identity
        evidence_raw[name] = raw
        evidence[name] = inert._decode_document(raw, code="test_invalid")
    fixed.inputs.assert_stable = lambda: None
    return inert._FrozenInertEvidence(
        phase_root=fixed.phase_root,
        transaction_root=transaction_root,
        transaction_identity=inert._require_directory(
            transaction_root,
            parent=fixed.phase_root,
            code="test_invalid",
            mode=0o500,
        ),
        evidence_identities=evidence_identities,
        evidence_raw=evidence_raw,
        evidence=evidence,
        receipt=fixed.receipt,
        inputs=fixed.inputs,
        binding=fixed.binding,
        loaded=fixed.loaded,
        network_key=fixed.network_key.public_key(),
        cloud_key=fixed.cloud_key.public_key(),
        host_key=fixed.host_key.public_key(),
        network_evidence=fixed.network_evidence,
        plan=fixed.plan,
    )


def test_evidence_publish_is_exact_durable_activation_named_and_compact(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fixed = _fixed_evidence_store(tmp_path, monkeypatch)

    inert._publish_evidence(
        phase_root=fixed.phase_root,
        receipt=fixed.receipt,
        payloads=fixed.payloads,
    )

    transaction = fixed.phase_root / fixed.receipt["evidence_set_sha256"]
    assert set(os.listdir(fixed.phase_root)) == {
        fixed.receipt["evidence_set_sha256"]
    }
    assert set(os.listdir(transaction)) == inert._TRANSACTION_NAMES
    assert stat_mode(fixed.phase_root) == 0o700
    assert stat_mode(transaction) == 0o500
    assert tuple(inert._EVIDENCE_NAMES) == (
        activation_seal.NETWORK_EVIDENCE_NAME,
        activation_seal.INERT_PRODUCTION_INGRESS_OBSERVATION_NAME,
        activation_seal.INERT_CLOUD_OBSERVATION_NAME,
        activation_seal.INERT_HOST_OBSERVATION_NAME,
        activation_seal.INERT_PREFLIGHT_NAME,
    )
    assert len(inert._EVIDENCE_NAMES) == 5
    assert (
        inert.INERT_CLOUD_BUNDLE_TERMINAL_RECEIPT_NAME
        not in inert._EVIDENCE_NAMES
    )
    assert tuple(inert._TRANSACTION_EVIDENCE_NAMES) == (
        *inert._EVIDENCE_NAMES,
        inert.INERT_CLOUD_BUNDLE_TERMINAL_RECEIPT_NAME,
    )
    for name in inert._TRANSACTION_NAMES:
        path = transaction / name
        assert stat_mode(path) == 0o400
        assert path.stat().st_nlink == 1
        assert path.read_bytes() == fixed.payloads[name]
    persisted, fresh = _load_fixed_transaction(
        fixed,
        now_unix=preflight_fixture.NOW,
    )
    assert fresh is True
    assert persisted == fixed.receipt
    assert persisted["schema"] == (
        "muncho-owner-gate-inert-observation-receipt.v3"
    )
    assert inert.EVIDENCE_SET_ID_SCHEMA == (
        "muncho-owner-gate-inert-observation-set-id.v3"
    )
    assert set(persisted["evidence_files"]) == set(
        inert._TRANSACTION_EVIDENCE_NAMES
    )
    assert len(persisted["evidence_files"]) == 6
    assert persisted[
        "inert_cloud_bundle_terminal_receipt_sha256"
    ] == fixed.terminal_receipt["terminal_receipt_sha256"]
    assert inert._decode_document(
        fixed.payloads[
            inert.INERT_CLOUD_BUNDLE_TERMINAL_RECEIPT_NAME
        ],
        code="test",
    ) == fixed.terminal_receipt
    assert not {
        "cloud_observation",
        "host_observation",
        "preflight_report",
    }.intersection(persisted)
    assert len(inert._canonical(persisted)) < launcher._MAX_JSON_LINE_BYTES


def test_fresh_replay_is_exact_and_requires_no_new_publish(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fixed = _fixed_evidence_store(tmp_path, monkeypatch)
    inert._publish_evidence(
        phase_root=fixed.phase_root,
        receipt=fixed.receipt,
        payloads=fixed.payloads,
    )

    first = inert._find_fresh_replay(
        phase_root=fixed.phase_root,
        binding=fixed.binding,
        inputs=fixed.inputs,
        loaded=fixed.loaded,
        network_key=fixed.network_key.public_key(),
        cloud_key=fixed.cloud_key.public_key(),
        host_key=fixed.host_key.public_key(),
        now_unix=preflight_fixture.NOW,
    )
    second = inert._find_fresh_replay(
        phase_root=fixed.phase_root,
        binding=fixed.binding,
        inputs=fixed.inputs,
        loaded=fixed.loaded,
        network_key=fixed.network_key.public_key(),
        cloud_key=fixed.cloud_key.public_key(),
        host_key=fixed.host_key.public_key(),
        now_unix=preflight_fixture.NOW,
    )

    assert first == second == fixed.receipt
    assert set(os.listdir(fixed.phase_root)) == {
        fixed.receipt["evidence_set_sha256"]
    }


def test_no_replace_collision_preserves_original_transaction(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fixed = _fixed_evidence_store(tmp_path, monkeypatch)
    inert._publish_evidence(
        phase_root=fixed.phase_root,
        receipt=fixed.receipt,
        payloads=fixed.payloads,
    )
    transaction = fixed.phase_root / fixed.receipt["evidence_set_sha256"]
    original = {
        name: (transaction / name).read_bytes()
        for name in inert._TRANSACTION_NAMES
    }

    with pytest.raises(
        launcher.OwnerLauncherError,
        match="owner_gate_inert_observation_manual_reconciliation_required",
    ):
        inert._publish_evidence(
            phase_root=fixed.phase_root,
            receipt=fixed.receipt,
            payloads=fixed.payloads,
        )

    assert {
        name: (transaction / name).read_bytes()
        for name in inert._TRANSACTION_NAMES
    } == original
    assert (fixed.phase_root / inert.PENDING_NAME).is_dir()


def test_stale_valid_history_is_retained_but_not_replayed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fixed = _fixed_evidence_store(tmp_path, monkeypatch)
    inert._publish_evidence(
        phase_root=fixed.phase_root,
        receipt=fixed.receipt,
        payloads=fixed.payloads,
    )
    stale_at = (
        preflight_fixture.NOW + foundation.PREFLIGHT_MAX_AGE_SECONDS + 1
    )

    replay = inert._find_fresh_replay(
        phase_root=fixed.phase_root,
        binding=fixed.binding,
        inputs=fixed.inputs,
        loaded=fixed.loaded,
        network_key=fixed.network_key.public_key(),
        cloud_key=fixed.cloud_key.public_key(),
        host_key=fixed.host_key.public_key(),
        now_unix=stale_at,
    )

    assert replay is None
    assert (fixed.phase_root / fixed.receipt["evidence_set_sha256"]).is_dir()


def test_stale_final_network_history_is_retained_while_preflight_is_fresh(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fixed = _fixed_evidence_store(
        tmp_path,
        monkeypatch,
        network_collected_at=(
            preflight_fixture.NOW - foundation.PREFLIGHT_MAX_AGE_SECONDS
        ),
    )
    inert._publish_evidence(
        phase_root=fixed.phase_root,
        receipt=fixed.receipt,
        payloads=fixed.payloads,
    )

    replay = inert._find_fresh_replay(
        phase_root=fixed.phase_root,
        binding=fixed.binding,
        inputs=fixed.inputs,
        loaded=fixed.loaded,
        network_key=fixed.network_key.public_key(),
        cloud_key=fixed.cloud_key.public_key(),
        host_key=fixed.host_key.public_key(),
        now_unix=preflight_fixture.NOW + 1,
    )

    assert replay is None
    assert (fixed.phase_root / fixed.receipt["evidence_set_sha256"]).is_dir()


@pytest.mark.parametrize(
    "attack",
    (
        "network_tamper",
        "cloud_symlink",
        "host_hardlink",
        "preflight_mode",
        "terminal_omission",
        "terminal_substitution",
        "terminal_self_hash",
        "nested_install_hash",
        "receipt_tamper",
        "transaction_mode",
        "transaction_substitution",
        "file_owner",
    ),
)
def test_persisted_evidence_rejects_alias_mode_owner_and_substitution(
    attack: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fixed = _fixed_evidence_store(tmp_path, monkeypatch)
    inert._publish_evidence(
        phase_root=fixed.phase_root,
        receipt=fixed.receipt,
        payloads=fixed.payloads,
    )
    transaction = fixed.phase_root / fixed.receipt["evidence_set_sha256"]
    transaction.chmod(0o700)
    if attack == "network_tamper":
        path = transaction / inert.NETWORK_EVIDENCE_NAME
        _rewrite(path, path.read_bytes() + b"\n")
    elif attack == "cloud_symlink":
        path = transaction / inert.INERT_CLOUD_OBSERVATION_NAME
        target = tmp_path / "cloud-target"
        target.write_bytes(path.read_bytes())
        target.chmod(0o400)
        path.unlink()
        path.symlink_to(target)
    elif attack == "host_hardlink":
        os.link(
            transaction / inert.INERT_HOST_OBSERVATION_NAME,
            transaction / "host-alias",
        )
    elif attack == "preflight_mode":
        (transaction / inert.INERT_PREFLIGHT_NAME).chmod(0o600)
    elif attack == "terminal_omission":
        (
            transaction
            / inert.INERT_CLOUD_BUNDLE_TERMINAL_RECEIPT_NAME
        ).unlink()
    elif attack == "terminal_substitution":
        terminal = copy.deepcopy(fixed.terminal_receipt)
        install = terminal["cloud_install_receipt"]
        terminal["release_sha"] = "d" * 40
        terminal["source_tree_oid"] = "e" * 40
        install["release_revision"] = terminal["release_sha"]
        install["source_tree_oid"] = terminal["source_tree_oid"]
        install["release_path"] = str(
            inert.stage0_iap.cloud_stage0.RELEASE_BASE
            / terminal["release_sha"]
        )
        install_unsigned = {
            name: item
            for name, item in install.items()
            if name
            not in {
                "receipt_sha256",
                "signer_key_id",
                "signature_ed25519_b64url",
            }
        }
        install["receipt_sha256"] = foundation.sha256_json(
            install_unsigned
        )
        terminal["cloud_install_receipt_sha256"] = install[
            "receipt_sha256"
        ]
        terminal["cloud_install_receipt_file_sha256"] = hashlib.sha256(
            inert._canonical(install)
        ).hexdigest()
        terminal["terminal_receipt_sha256"] = foundation.sha256_json({
            name: item
            for name, item in terminal.items()
            if name != "terminal_receipt_sha256"
        })
        _rewrite(
            transaction
            / inert.INERT_CLOUD_BUNDLE_TERMINAL_RECEIPT_NAME,
            inert._canonical(terminal),
        )
    elif attack == "terminal_self_hash":
        terminal = copy.deepcopy(fixed.terminal_receipt)
        terminal["transport_receipt_sha256"] = "f" * 64
        _rewrite(
            transaction
            / inert.INERT_CLOUD_BUNDLE_TERMINAL_RECEIPT_NAME,
            inert._canonical(terminal),
        )
    elif attack == "nested_install_hash":
        terminal = copy.deepcopy(fixed.terminal_receipt)
        install = terminal["cloud_install_receipt"]
        install["release_tree_sha256"] = "f" * 64
        terminal["cloud_install_receipt_file_sha256"] = hashlib.sha256(
            inert._canonical(install)
        ).hexdigest()
        terminal["terminal_receipt_sha256"] = foundation.sha256_json({
            name: item
            for name, item in terminal.items()
            if name != "terminal_receipt_sha256"
        })
        _rewrite(
            transaction
            / inert.INERT_CLOUD_BUNDLE_TERMINAL_RECEIPT_NAME,
            inert._canonical(terminal),
        )
    elif attack == "receipt_tamper":
        path = transaction / inert.RECEIPT_NAME
        _rewrite(path, path.read_bytes() + b"\n")
    elif attack == "transaction_mode":
        pass
    elif attack == "transaction_substitution":
        transaction.rename(fixed.phase_root / ("e" * 64))
    elif attack == "file_owner":
        transaction.chmod(0o500)
        monkeypatch.setattr(inert.os, "geteuid", lambda: os.getuid() + 1)
    else:  # pragma: no cover - table is deliberately closed above
        raise AssertionError(attack)
    if attack not in {"transaction_mode", "transaction_substitution", "file_owner"}:
        transaction.chmod(0o500)

    with pytest.raises(
        launcher.OwnerLauncherError,
        match="owner_gate_inert_observation_manual_reconciliation_required",
    ):
        inert._find_fresh_replay(
            phase_root=fixed.phase_root,
            binding=fixed.binding,
            inputs=fixed.inputs,
            loaded=fixed.loaded,
            network_key=fixed.network_key.public_key(),
            cloud_key=fixed.cloud_key.public_key(),
            host_key=fixed.host_key.public_key(),
            now_unix=preflight_fixture.NOW,
        )


@pytest.mark.parametrize("side", ("cloud", "host"))
def test_terminal_receipt_rejects_observation_cross_binding(
    side: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fixed = _fixed_evidence_store(tmp_path, monkeypatch)
    cloud_observation = copy.deepcopy(fixed.cloud_observation)
    host_observation = copy.deepcopy(fixed.host_observation)
    if side == "cloud":
        cloud_observation["release_binding"][
            "terminal_receipt_sha256"
        ] = "f" * 64
    else:
        host_observation["release"]["terminal_receipt_sha256"] = "f" * 64

    with pytest.raises(
        launcher.OwnerLauncherError,
        match="owner_gate_inert_observation_evidence_invalid",
    ):
        inert._build_receipt(
            binding=fixed.binding,
            inputs=fixed.inputs,
            loaded=fixed.loaded,
            network_evidence=fixed.network_mapping,
            plan=fixed.plan,
            production_ingress_observation=(
                fixed.production_ingress_observation
            ),
            terminal_receipt=fixed.terminal_receipt,
            cloud_observation=cloud_observation,
            host_observation=host_observation,
            report=fixed.report,
        )


@pytest.mark.parametrize("drift", ("pending", "unknown", "partial"))
def test_partial_and_namespace_drift_require_manual_reconciliation(
    drift: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fixed = _fixed_evidence_store(tmp_path, monkeypatch)
    if drift == "pending":
        _owner_directory(fixed.phase_root / inert.PENDING_NAME)
    elif drift == "unknown":
        _owner_directory(fixed.phase_root / "caller-name")
    else:
        partial = fixed.phase_root / ("f" * 64)
        _owner_directory(partial)
        partial.chmod(0o500)

    with pytest.raises(
        launcher.OwnerLauncherError,
        match="owner_gate_inert_observation_manual_reconciliation_required",
    ):
        inert._find_fresh_replay(
            phase_root=fixed.phase_root,
            binding=fixed.binding,
            inputs=fixed.inputs,
            loaded=fixed.loaded,
            network_key=fixed.network_key.public_key(),
            cloud_key=fixed.cloud_key.public_key(),
            host_key=fixed.host_key.public_key(),
            now_unix=preflight_fixture.NOW,
        )


def test_interrupted_publish_leaves_pending_for_manual_reconciliation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fixed = _fixed_evidence_store(tmp_path, monkeypatch)
    real_write = inert._write_staged_file
    writes = 0

    def interrupt_second_write(path: Path, raw: bytes) -> None:
        nonlocal writes
        writes += 1
        if writes == 2:
            raise launcher.OwnerLauncherError("simulated_write_failure")
        real_write(path, raw)

    monkeypatch.setattr(inert, "_write_staged_file", interrupt_second_write)
    with pytest.raises(
        launcher.OwnerLauncherError,
        match="owner_gate_inert_observation_manual_reconciliation_required",
    ):
        inert._publish_evidence(
            phase_root=fixed.phase_root,
            receipt=fixed.receipt,
            payloads=fixed.payloads,
        )
    assert (fixed.phase_root / inert.PENDING_NAME).is_dir()

    with pytest.raises(
        launcher.OwnerLauncherError,
        match="owner_gate_inert_observation_manual_reconciliation_required",
    ):
        inert._find_fresh_replay(
            phase_root=fixed.phase_root,
            binding=fixed.binding,
            inputs=fixed.inputs,
            loaded=fixed.loaded,
            network_key=fixed.network_key.public_key(),
            cloud_key=fixed.cloud_key.public_key(),
            host_key=fixed.host_key.public_key(),
            now_unix=preflight_fixture.NOW,
        )


def test_activation_guards_skip_only_expired_inert_host_short_ttl(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fixed = _fixed_evidence_store(tmp_path, monkeypatch)
    frozen = _frozen_fixed_transaction(fixed)
    after_host_short_ttl = preflight_fixture.NOW + 400

    with pytest.raises(
        launcher.OwnerLauncherError,
        match="owner_gate_inert_observation_stale",
    ):
        frozen.assert_stable(now_unix=after_host_short_ttl)
    frozen.assert_activation_collection_window_stable(
        now_unix=after_host_short_ttl
    )
    frozen.assert_activation_staging_window_stable(
        now_unix=after_host_short_ttl
    )


def test_activation_guard_network_clock_ends_only_after_fresh_pair_boundary(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fixed = _fixed_evidence_store(
        tmp_path,
        monkeypatch,
        network_collected_at=(
            preflight_fixture.NOW - foundation.PREFLIGHT_MAX_AGE_SECONDS
        ),
    )
    frozen = _frozen_fixed_transaction(fixed)
    after_network_expiry = preflight_fixture.NOW + 100

    with pytest.raises(
        launcher.OwnerLauncherError,
        match="owner_gate_inert_observation_stale",
    ):
        frozen.assert_activation_collection_window_stable(
            now_unix=after_network_expiry
        )
    frozen.assert_activation_staging_window_stable(
        now_unix=after_network_expiry
    )


@pytest.mark.parametrize("expired", ("preflight", "ingress"))
def test_activation_staging_guard_keeps_seal_operational_clocks(
    expired: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fixed = _fixed_evidence_store(tmp_path, monkeypatch)
    frozen = _frozen_fixed_transaction(fixed)
    now_unix = (
        preflight_fixture.NOW + foundation.PREFLIGHT_MAX_AGE_SECONDS + 1
        if expired == "preflight"
        else int(
            fixed.production_ingress_observation["fresh_through_unix"]
        )
        + 1
    )

    with pytest.raises(
        launcher.OwnerLauncherError,
        match="owner_gate_inert_observation_stale",
    ):
        frozen.assert_activation_staging_window_stable(now_unix=now_unix)


def test_production_orchestration_resamples_time_after_remote_collection(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        preflight_fixture.trust,
        "PINNED_RELEASE_TRUST_PUBLIC_KEY_SHA256",
        preflight_fixture.RELEASE_KEY_ID,
    )
    monkeypatch.setattr(
        inert.preflight.ingress,
        "PINNED_RELEASE_TRUST_PUBLIC_KEY_SHA256",
        preflight_fixture.RELEASE_KEY_ID,
    )
    network_key = Ed25519PrivateKey.generate()
    cloud_key = Ed25519PrivateKey.generate()
    host_key = Ed25519PrivateKey.generate()
    network_public = network_key.public_key()
    cloud_public = cloud_key.public_key()
    host_public = host_key.public_key()
    plan = preflight_fixture._plan(network_key, cloud_key, host_key)
    network_mapping = preflight_fixture._signed_network_evidence(network_key)
    network_evidence = foundation.ProductionNetworkEvidence.from_mapping(
        network_mapping,
        public_key=network_public,
        expected_public_key_id=plan.spec.network_collector_public_key_id,
        now_unix=preflight_fixture.NOW,
    )
    production_ingress_observation = (
        preflight_fixture._production_ingress_envelope(plan, iam=False)
    )
    pair = object()
    host_preparation = object()
    executable = cast(launcher.TrustedGcloudExecutable, object())
    configuration = cast(launcher.PinnedGcloudConfiguration, object())
    owner_identity = cast(launcher.GcloudOwnerAccessToken, object())
    raw_artifacts = object()
    kit_stream = object()
    bundle_stream = object()
    phase_root = object()
    package = {
        "package_sha256": "3" * 64,
        "package_inventory_sha256": "5" * 64,
        "interpreter_sha256": "6" * 64,
    }
    ancestry = SimpleNamespace(
        signed_evidence_sha256="9" * 64,
        value={"stable_chain_sha256": "a" * 64},
        ordered_chain=(
            {"resource_name": f"projects/{foundation.PROJECT}"},
            {"resource_name": plan.spec.organization_resource},
        ),
    )
    chain = SimpleNamespace(
        foundation_source_revision=FOUNDATION_REVISION,
        foundation_source_tree_oid=FOUNDATION_TREE,
        pre_foundation_authority_sha256="7" * 64,
        foundation_apply_receipt_sha256="8" * 64,
        foundation_a=SimpleNamespace(ancestry_evidence=ancestry),
    )
    binding = inert._ReleaseBinding(
        release_revision=REVISION,
        source_tree_oid=RELEASE_TREE,
        package=package,
        authority={
            "collector_public_key_ids": {
                "network": plan.spec.network_collector_public_key_id,
                "cloud": plan.spec.cloud_collector_public_key_id,
                "host": plan.spec.host_collector_public_key_id,
            },
        },
        direct_iam_raw=b"direct-iam",
        foundation_source_revision=FOUNDATION_REVISION,
        foundation_source_tree_oid=FOUNDATION_TREE,
        release_public_key=preflight_fixture.RELEASE_KEY.public_key(),
    )
    events: list[str] = []
    published: dict[str, Any] = {}

    class Transport:
        def prepare_owner_gate_host_observation(
            self,
            **kwargs: object,
        ) -> object:
            assert kwargs == {
                "phase": "inert",
                "kit_stream": kit_stream,
                "bundle_stream": bundle_stream,
            }
            events.append("prepare")
            return host_preparation

    transport = Transport()

    class Inputs:
        def __init__(self) -> None:
            self.pins = {
                "pins_sha256": "d" * 64,
                "kit_release_id": KIT_RELEASE_ID,
            }
            self.kit_stream = kit_stream
            self.bundle_stream = bundle_stream

        def assert_stable(self) -> None:
            events.append("stable")

    inputs = Inputs()
    loaded = SimpleNamespace(chain=chain, raw_artifacts=raw_artifacts)
    terminal_receipt = _terminal_receipt(
        binding=binding,
        loaded=loaded,
    )
    host_observation = _attest_with_terminal(
        preflight_fixture._host(plan, host_key, iam=False),
        key=host_key,
        terminal_receipt_sha256=terminal_receipt[
            "terminal_receipt_sha256"
        ],
    )
    cloud_observation = _attest_with_terminal(
        preflight_fixture._cloud(plan, cloud_key, iam=False),
        key=cloud_key,
        terminal_receipt_sha256=terminal_receipt[
            "terminal_receipt_sha256"
        ],
        host_observation_report_sha256=host_observation[
            "report_sha256"
        ],
    )

    def load_inputs(release_revision: str) -> Inputs:
        assert release_revision == REVISION
        events.append("inputs")
        return inputs

    def load_binding(release_revision: str, observed_bundle: object):
        assert release_revision == REVISION
        assert observed_bundle is bundle_stream
        events.append("binding")
        return binding

    def load_foundation(foundation_revision: str) -> SimpleNamespace:
        assert foundation_revision == FOUNDATION_REVISION
        events.append("foundation")
        return loaded

    def bind_release(observed_binding: object, observed_loaded: object) -> None:
        assert observed_binding is binding
        assert observed_loaded is loaded
        events.append("bind")

    def collect_network(**kwargs: object):
        assert kwargs == {
            "binding": binding,
            "public_key": network_public,
            "gcloud_executable": executable,
            "gcloud_configuration": configuration,
        }
        events.append("network")
        return network_mapping, network_evidence

    def final_plan(
        observed_binding: object,
        observed_network: object,
        observed_key: object,
    ):
        assert observed_binding is binding
        assert observed_network is network_evidence
        assert observed_key is network_public
        events.append("plan")
        return plan

    def release_private_key(observed_binding: object) -> Ed25519PrivateKey:
        assert observed_binding is binding
        events.append("release-key")
        return preflight_fixture.RELEASE_KEY

    def build_production_transport(
        observed_identity: object,
        **kwargs: object,
    ) -> object:
        assert observed_identity is owner_identity
        assert kwargs == {
            "gcloud_executable": executable,
            "gcloud_configuration": configuration,
        }
        events.append("production-transport")
        return object()

    production_transport = object()

    def wrap_production_transport(observed: object) -> object:
        del observed
        events.append("ingress-transport")
        return production_transport

    def collect_ingress(observed: object, **kwargs: object) -> Mapping[str, Any]:
        assert observed is production_transport
        assert kwargs == {
            "phase": "inert",
            "release_revision": REVISION,
            "plan_sha256": plan.sha256,
            "release_private_key": preflight_fixture.RELEASE_KEY,
        }
        events.append("ingress")
        return production_ingress_observation

    def build_transport(**kwargs: object) -> object:
        assert kwargs == {
            "release_sha": REVISION,
            "owner_identity": owner_identity,
            "gcloud_executable": executable,
            "gcloud_configuration": configuration,
            "foundation_artifacts": raw_artifacts,
        }
        events.append("transport")
        return transport

    def collect_pair(**kwargs: object) -> object:
        assert kwargs == {
            "plan": plan,
            "foundation_apply_chain": chain,
            "final_network_evidence": network_evidence,
            "final_network_collector_public_key": network_public,
            "production_ingress_observation": production_ingress_observation,
            "phase": "inert",
            "collected_at_unix": None,
            "gcloud_executable": executable,
            "gcloud_configuration": configuration,
            "owner_identity": owner_identity,
            "stage0_transport": transport,
            "kit_stream": kit_stream,
            "bundle_stream": bundle_stream,
            "host_preparation": host_preparation,
        }
        events.append("collect")
        return pair, terminal_receipt

    consumes = 0

    def consume_pair(observed: object, **kwargs: object):
        nonlocal consumes
        consumes += 1
        assert consumes == 1
        assert observed is pair
        assert kwargs == {"plan": plan, "phase": "inert"}
        assert events[-1] == "collect"
        events.append("consume")
        return cloud_observation, host_observation

    def collector_key(
        release_revision: str,
        *,
        role: str,
        expected_key_id: str,
    ):
        assert release_revision == REVISION
        assert expected_key_id == {
            "network": plan.spec.network_collector_public_key_id,
            "cloud": plan.spec.cloud_collector_public_key_id,
            "host": plan.spec.host_collector_public_key_id,
        }[role]
        events.append(f"key-{role}")
        return {
            "network": network_public,
            "cloud": cloud_public,
            "host": host_public,
        }[role]

    real_preflight = inert.preflight.build_preflight_report

    def build_preflight(**kwargs: Any) -> Mapping[str, Any]:
        events.append("preflight")
        return real_preflight(**kwargs)

    class EvidenceLease:
        def __enter__(self) -> object:
            events.append("lease")
            return phase_root

        def __exit__(self, *_args: object) -> None:
            events.append("unlock")

    def find_replay(**kwargs: object):
        expected_now = (
            preflight_fixture.NOW
            if published
            else preflight_fixture.NOW - 2
        )
        assert kwargs == {
            "phase_root": phase_root,
            "binding": binding,
            "inputs": inputs,
            "loaded": loaded,
            "network_key": network_public,
            "cloud_key": cloud_public,
            "host_key": host_public,
            "now_unix": expected_now,
        }
        events.append("scan")
        return published.get("receipt")

    def publish_evidence(**kwargs: object) -> None:
        assert kwargs["phase_root"] is phase_root
        published.update(kwargs)
        events.append("publish")

    monkeypatch.setattr(inert._PinnedObservationInputs, "load", load_inputs)
    monkeypatch.setattr(inert, "_load_release_binding", load_binding)
    monkeypatch.setattr(inert, "_load_successful_foundation", load_foundation)
    monkeypatch.setattr(inert, "_bind_release_to_foundation", bind_release)
    monkeypatch.setattr(inert, "_collect_final_network_evidence", collect_network)
    monkeypatch.setattr(inert, "_final_plan", final_plan)
    monkeypatch.setattr(inert, "_release_private_key", release_private_key)
    monkeypatch.setattr(
        inert.production_cutover,
        "ProductionCutoverTransport",
        build_production_transport,
    )
    monkeypatch.setattr(
        inert.production_ingress,
        "OwnerGateProductionIngressTransport",
        wrap_production_transport,
    )
    monkeypatch.setattr(
        inert.production_ingress,
        "collect_and_sign_production_ingress_observation",
        collect_ingress,
    )
    monkeypatch.setattr(
        inert.stage0_iap,
        "OwnerGateStage0IapTransport",
        build_transport,
    )
    monkeypatch.setattr(
        inert.cloud_author,
        "_collect_and_author_bound_pair_from_preparation",
        collect_pair,
    )
    monkeypatch.setattr(
        inert.cloud_author,
        "consume_bound_observation_pair",
        consume_pair,
    )
    monkeypatch.setattr(inert, "_collector_key", collector_key)
    monkeypatch.setattr(inert.preflight, "build_preflight_report", build_preflight)
    monkeypatch.setattr(inert, "_evidence_lease", lambda _revision: EvidenceLease())
    monkeypatch.setattr(inert, "_find_fresh_replay", find_replay)
    monkeypatch.setattr(inert, "_publish_evidence", publish_evidence)
    clock = iter((
        preflight_fixture.NOW - 2,
        preflight_fixture.NOW,
        preflight_fixture.NOW,
    ))
    monkeypatch.setattr(inert.time, "time", lambda: float(next(clock)))

    receipt = inert._collect_inert_observation(
        release_revision=REVISION,
        gcloud_executable=executable,
        gcloud_configuration=configuration,
        owner_identity=owner_identity,
    )

    assert consumes == 1
    assert events == [
        "inputs",
        "binding",
        "foundation",
        "bind",
        "key-network",
        "key-cloud",
        "key-host",
        "lease",
        "scan",
        "transport",
        "prepare",
        "network",
        "plan",
        "release-key",
        "production-transport",
        "ingress-transport",
        "ingress",
        "collect",
        "consume",
        "preflight",
        "stable",
        "publish",
        "scan",
        "stable",
        "unlock",
    ]
    assert "cloud_observation" not in receipt
    assert "host_observation" not in receipt
    assert "preflight_report" not in receipt
    persisted_payloads = published["payloads"]
    assert inert._decode_document(
        persisted_payloads[inert.NETWORK_EVIDENCE_NAME],
        code="test",
    ) == network_mapping
    assert inert._decode_document(
        persisted_payloads[
            inert.INERT_PRODUCTION_INGRESS_OBSERVATION_NAME
        ],
        code="test",
    ) == production_ingress_observation
    assert inert._decode_document(
        persisted_payloads[inert.INERT_CLOUD_OBSERVATION_NAME],
        code="test",
    ) == cloud_observation
    assert inert._decode_document(
        persisted_payloads[inert.INERT_HOST_OBSERVATION_NAME],
        code="test",
    ) == host_observation
    assert inert._decode_document(
        persisted_payloads[
            inert.INERT_CLOUD_BUNDLE_TERMINAL_RECEIPT_NAME
        ],
        code="test",
    ) == terminal_receipt
    persisted_preflight = inert._decode_document(
        persisted_payloads[inert.INERT_PREFLIGHT_NAME],
        code="test",
    )
    assert persisted_preflight["schema"] == inert.preflight.PREFLIGHT_SCHEMA
    assert persisted_preflight["mutation_performed"] is False
    assert receipt["cloud_mutation_performed"] is False
    assert receipt["service_activation_performed"] is False
    assert receipt["production_ingress_observation_sha256"] == (
        production_ingress_observation["envelope_sha256"]
    )
    assert receipt["release_revision"] == REVISION
    assert receipt["source_tree_oid"] == RELEASE_TREE
    assert receipt["foundation_source_revision"] == FOUNDATION_REVISION
    assert receipt["foundation_source_tree_oid"] == FOUNDATION_TREE
    assert receipt[
        "inert_cloud_bundle_terminal_receipt_sha256"
    ] == terminal_receipt["terminal_receipt_sha256"]
    unsigned_receipt = {
        key: value for key, value in receipt.items() if key != "receipt_sha256"
    }
    assert receipt["receipt_sha256"] == foundation.sha256_json(unsigned_receipt)
    assert inert._canonical(receipt) == foundation.canonical_json_bytes(receipt)
    assert len(inert._canonical(receipt)) < launcher._MAX_JSON_LINE_BYTES

    events.clear()
    replayed = inert._collect_inert_observation(
        release_revision=REVISION,
        gcloud_executable=executable,
        gcloud_configuration=configuration,
        owner_identity=owner_identity,
    )
    assert replayed == receipt
    assert consumes == 1
    assert events == [
        "inputs",
        "binding",
        "foundation",
        "bind",
        "key-network",
        "key-cloud",
        "key-host",
        "lease",
        "scan",
        "stable",
        "unlock",
    ]


def test_public_surface_has_no_path_phase_transport_or_injection_capability() -> None:
    signature = inspect.signature(inert.collect_inert_observation)

    assert tuple(signature.parameters) == (
        "release_revision",
        "gcloud_executable",
        "gcloud_configuration",
        "owner_identity",
    )
    assert all(
        parameter.kind is inspect.Parameter.KEYWORD_ONLY
        for parameter in signature.parameters.values()
    )
    assert not hasattr(inert, "main")
    assert not {
        "path",
        "root",
        "phase",
        "evidence",
        "transport",
        "exchange",
        "popen_factory",
        "timeout_seconds",
    }.intersection(signature.parameters)
    with pytest.raises(
        launcher.OwnerLauncherError,
        match="owner_gate_inert_observation_capability_invalid",
    ):
        inert.collect_inert_observation(
            release_revision=REVISION,
            gcloud_executable=object(),  # type: ignore[arg-type]
            gcloud_configuration=object(),  # type: ignore[arg-type]
            owner_identity=object(),  # type: ignore[arg-type]
        )


def test_launcher_parser_exposes_one_mutually_exclusive_pathless_action() -> None:
    parser = launcher._cli_parser()
    parsed = parser.parse_args([
        "--release-sha",
        REVISION,
        "--observe-owner-gate-inert",
    ])

    assert parsed.observe_owner_gate_inert is True
    with pytest.raises(SystemExit):
        parser.parse_args([
            "--release-sha",
            REVISION,
            "--observe-owner-gate-inert",
            "--publish-stopped-release",
        ])
    for forbidden in (
        "--owner-gate-input-root",
        "--owner-gate-evidence",
        "--owner-gate-phase",
        "--owner-gate-transport",
        "--owner-gate-remote-command",
    ):
        with pytest.raises(SystemExit):
            parser.parse_args([
                "--release-sha",
                REVISION,
                "--observe-owner-gate-inert",
                forbidden,
                "/attacker-controlled",
            ])


def test_public_action_rejects_unsealed_runtime_before_fixed_inputs(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    executable = object.__new__(launcher.TrustedGcloudExecutable)
    configuration = object.__new__(launcher.PinnedGcloudConfiguration)
    owner_identity = object.__new__(launcher.GcloudOwnerAccessToken)
    owner_identity._gcloud_executable = executable
    owner_identity._gcloud_configuration = configuration
    reached: list[str] = []

    def reject_unsealed(*_args: object, **_kwargs: object) -> None:
        raise launcher.OwnerLauncherError("trusted_owner_support_path_invalid")

    monkeypatch.setattr(
        launcher,
        "require_trusted_owner_support_activation",
        reject_unsealed,
    )
    monkeypatch.setattr(
        inert,
        "_collect_inert_observation",
        lambda **_kwargs: reached.append("inputs"),
    )

    with pytest.raises(
        launcher.OwnerLauncherError,
        match="trusted_owner_support_path_invalid",
    ):
        inert.collect_inert_observation(
            release_revision=REVISION,
            gcloud_executable=executable,
            gcloud_configuration=configuration,
            owner_identity=owner_identity,
        )
    assert reached == []


def test_direct_launcher_canonical_bridge_is_exact_and_provenance_bound(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    digest = "c" * 64

    class Provenance:
        def __init__(self, *, module_path: str) -> None:
            assert module_path == str(Path(launcher.__file__).absolute())

        def __call__(self, release_sha: str) -> str:
            assert release_sha == REVISION
            return digest

    monkeypatch.setattr(launcher, "_CANONICAL_LAUNCHER_BRIDGE", None)
    monkeypatch.setattr(launcher, "__name__", "__main__")
    monkeypatch.setitem(__import__("sys").modules, "__main__", launcher)
    monkeypatch.setattr(
        launcher,
        "require_local_launcher_provenance",
        lambda release_sha: digest if release_sha == REVISION else "",
    )
    monkeypatch.setattr(launcher, "LocalLauncherProvenance", Provenance)

    launcher._install_canonical_launcher_bridge(REVISION)

    assert (
        __import__("sys").modules[launcher._CANONICAL_LAUNCHER_MODULE]
        is launcher
    )
    assert launcher._canonical_launcher_bridge_valid(launcher) is True
    assert launcher._canonical_launcher_bridge_valid(object()) is False
