from __future__ import annotations

import hashlib
import json
import os
from dataclasses import replace
from pathlib import Path
from typing import Mapping

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import (
    Ed25519PrivateKey,
)

from scripts.canary import trusted_signer_provisioning as provisioning


class SimulatedCrash(RuntimeError):
    pass


def _crash() -> None:
    raise SimulatedCrash("simulated_power_loss")


@pytest.mark.parametrize(
    "hook",
    (
        "after_open",
        "after_write_chunk",
        "after_fsync",
        "after_publish_link",
    ),
)
def test_exclusive_install_replays_every_crash_window(
    tmp_path: Path,
    hook: str,
) -> None:
    parent = tmp_path / "protected"
    parent.mkdir(mode=0o700)
    destination = parent / "signer.key"
    payload = bytes(range(64))

    with pytest.raises(SimulatedCrash):
        provisioning._install_exclusive(
            destination,
            payload,
            uid=os.getuid(),
            gid=os.getgid(),
            mode=0o400,
            include_digest=False,
            after_open=_crash if hook == "after_open" else None,
            after_write_chunk=(
                _crash if hook == "after_write_chunk" else None
            ),
            after_fsync=_crash if hook == "after_fsync" else None,
            after_publish_link=(
                _crash if hook == "after_publish_link" else None
            ),
        )

    evidence = provisioning._install_exclusive(
        destination,
        payload,
        uid=os.getuid(),
        gid=os.getgid(),
        mode=0o400,
        include_digest=False,
    )
    assert destination.read_bytes() == payload
    assert destination.stat().st_nlink == 1
    assert not (parent / ".signer.key.muncho-staged").exists()
    assert "sha256" not in evidence
    assert evidence["size"] == 64


def test_exclusive_install_rejects_nonprefix_staged_bytes(
    tmp_path: Path,
) -> None:
    parent = tmp_path / "protected"
    parent.mkdir(mode=0o700)
    destination = parent / "signer.key"
    staged = parent / ".signer.key.muncho-staged"
    staged.write_bytes(b"not-a-prefix")
    staged.chmod(0o400)
    staged_identity = staged.stat()

    with pytest.raises(
        provisioning.TrustedSignerProvisioningError,
        match="trusted_signer_staging_invalid",
    ):
        provisioning._install_exclusive(
            destination,
            b"expected-secret-seed-material",
            uid=staged_identity.st_uid,
            gid=staged_identity.st_gid,
            mode=0o400,
            include_digest=False,
        )
    assert staged.read_bytes() == b"not-a-prefix"
    assert not destination.exists()


def test_exclusive_install_never_replaces_conflicting_final(
    tmp_path: Path,
) -> None:
    parent = tmp_path / "protected"
    parent.mkdir(mode=0o700)
    destination = parent / "signer.key"
    destination.write_bytes(b"existing")
    destination.chmod(0o400)
    destination_identity = destination.stat()

    with pytest.raises(
        provisioning.TrustedSignerProvisioningError,
        match="trusted_signer_install_conflict",
    ):
        provisioning._install_exclusive(
            destination,
            b"replacement",
            uid=destination_identity.st_uid,
            gid=destination_identity.st_gid,
            mode=0o400,
            include_digest=False,
        )
    assert destination.read_bytes() == b"existing"


def test_exclusive_install_rejects_symlink_final(tmp_path: Path) -> None:
    parent = tmp_path / "protected"
    parent.mkdir(mode=0o700)
    target = parent / "target"
    target.write_bytes(b"target")
    target.chmod(0o400)
    destination = parent / "signer.key"
    destination.symlink_to(target)

    with pytest.raises(provisioning.TrustedSignerProvisioningError):
        provisioning._install_exclusive(
            destination,
            b"target",
            uid=os.getuid(),
            gid=os.getgid(),
            mode=0o400,
            include_digest=False,
        )
    assert destination.is_symlink()


def test_selected_release_rejects_non_root_symlink_owner(
    tmp_path: Path,
) -> None:
    revision = "a" * 40
    release = tmp_path / "releases" / revision
    release.mkdir(parents=True)
    current = tmp_path / "current"
    current.symlink_to(release)
    layout = provisioning.cloud_layout(revision)
    object.__setattr__(layout, "release_base", release.parent)
    object.__setattr__(layout, "release", release)
    object.__setattr__(layout, "current_link", current)

    with pytest.raises(
        provisioning.TrustedSignerProvisioningError,
        match="trusted_signer_current_release_invalid",
    ):
        provisioning._selected_release_evidence(layout)


def test_release_projection_accepts_signed_zero_byte_runtime_files(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    revision = "a" * 40
    release = tmp_path / revision
    public_raw = bytes(range(32))
    public_key_id = hashlib.sha256(public_raw).hexdigest()
    payloads = {
        "bin/provision": (b"entrypoint", 0o555),
        "scripts/canary/trusted_signer_provisioning.py": (
            b"signer provisioning source",
            0o444,
        ),
        "scripts/canary/storage_growth_trusted_collector.py": (
            b"storage collector source",
            0o444,
        ),
    }
    interpreter = b"exact offline interpreter"
    files = {
        **payloads,
        "venv/bin/python": (interpreter, 0o555),
        "venv/lib/python3.11/site-packages/example/py.typed": (b"", 0o444),
        "trust/cloud-observation-attestation.pub": (public_raw, 0o444),
    }
    for relative, (raw, mode) in files.items():
        path = release / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(raw)
        path.chmod(mode)
    manifest = {
        "release_revision": revision,
        "package_sha256": "b" * 64,
        "collector_public_key_ids": {
            role: public_key_id for role in ("network", "cloud", "host")
        },
        "runtime_source_closure": [
            "scripts/canary/trusted_signer_provisioning.py",
            "scripts/canary/storage_growth_trusted_collector.py",
        ],
        "wheels": [{"project": "cryptography", "version": "49.0.0"}],
        "interpreter_sha256": hashlib.sha256(interpreter).hexdigest(),
        "payloads": [
            {
                "release_relative": relative,
                "mode": f"{mode:04o}",
                "sha256": hashlib.sha256(raw).hexdigest(),
                "size": len(raw),
            }
            for relative, (raw, mode) in payloads.items()
        ],
    }
    authority_manifest = release / "package-manifest.json"
    authority_manifest.write_bytes(
        provisioning.foundation.canonical_json_bytes(manifest)
    )
    authority_manifest.chmod(0o444)
    for directory in sorted(
        (path for path in release.rglob("*") if path.is_dir()),
        key=lambda path: len(path.parts),
        reverse=True,
    ):
        directory.chmod(0o555)
    release.chmod(0o555)
    uid = os.getuid()
    gid = os.getgid()
    layout = provisioning.SignerLayout(
        role="cloud",
        release_base=release.parent,
        release=release,
        authority_manifest=authority_manifest,
        pinned_public_key=release / "trust/cloud-observation-attestation.pub",
        private_key=tmp_path / "private.key",
        installed_public_key=tmp_path / "installed.pub",
        config=tmp_path / "config.json",
        replay_directory=tmp_path / "replay",
        receipt=tmp_path / "receipt.json",
        lock=tmp_path / "lock",
        activation_seal=tmp_path / "activation-seal",
        current_link=tmp_path / "current",
        private_uid=uid,
        private_gid=gid,
        config_uid=uid,
        config_gid=gid,
        replay_uid=uid,
        replay_gid=gid,
        receipt_uid=uid,
        receipt_gid=gid,
        release_uid=uid,
        release_gid=gid,
        runtime_entrypoint_name="provision",
    )
    monkeypatch.setattr(
        provisioning,
        "_validate_layout_directories",
        lambda _layout: None,
    )

    evidence = provisioning._validate_release_and_authority(layout)

    assert evidence["runtime"]["immutable_release_projection_count"] > len(files)
    assert len(evidence["runtime"]["immutable_release_projection_sha256"]) == 64


def _runtime_projection_case(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> tuple[
    provisioning.SignerLayout,
    bytes,
    bytes,
    Path,
    Path,
]:
    current_revision = "b" * 40
    historical_revision = "a" * 40
    uid = os.getuid()
    gid = os.getgid()
    release_base = tmp_path / "releases"
    current_release = release_base / current_revision
    historical_release = release_base / historical_revision
    current_release.mkdir(parents=True)
    historical_release.mkdir()
    receipt_root = tmp_path / "receipts"
    receipt_root.mkdir()
    public_root = tmp_path / "public"
    public_root.mkdir()
    runtime_receipt = public_root / "cloud-signer-provisioning-receipt.json"

    def layout(revision: str) -> provisioning.SignerLayout:
        release = release_base / revision
        return provisioning.SignerLayout(
            role="cloud",
            release_base=release_base,
            release=release,
            authority_manifest=release / "package-manifest.json",
            pinned_public_key=release / "cloud.pub",
            private_key=tmp_path / "private.key",
            installed_public_key=tmp_path / "installed.pub",
            config=tmp_path / "config.json",
            replay_directory=tmp_path / "replay",
            receipt=receipt_root / f"cloud-signer-{revision}.json",
            lock=tmp_path / "lock",
            activation_seal=tmp_path / "activation-seal",
            current_link=tmp_path / "current",
            private_uid=uid,
            private_gid=gid,
            config_uid=uid,
            config_gid=gid,
            replay_uid=uid,
            replay_gid=gid,
            receipt_uid=uid,
            receipt_gid=gid,
            runtime_receipt=runtime_receipt,
            runtime_receipt_uid=uid,
            runtime_receipt_gid=gid,
            runtime_receipt_mode=0o440,
            release_uid=uid,
            release_gid=gid,
            runtime_entrypoint_name="provision",
        )

    current_layout = layout(current_revision)
    historical_layout = layout(historical_revision)
    historical_public_raw = (
        Ed25519PrivateKey.generate().public_key().public_bytes_raw()
    )
    historical_public_id = hashlib.sha256(historical_public_raw).hexdigest()
    historical_authority = {
        "package_sha256": "1" * 64,
        "manifest_sha256": "2" * 64,
        "public_raw": historical_public_raw,
        "public_key_id": historical_public_id,
        "runtime": {"release": str(historical_release)},
    }
    historical_receipt = {
        "schema": provisioning.PROVISIONING_RECEIPT_SCHEMA,
        "role": "cloud",
        "release_revision": historical_revision,
        "package_sha256": historical_authority["package_sha256"],
        "package_manifest_sha256": historical_authority["manifest_sha256"],
        "public_key_id": historical_public_id,
        "runtime": historical_authority["runtime"],
    }
    current_receipt = {
        "schema": provisioning.PROVISIONING_RECEIPT_SCHEMA,
        "role": "cloud",
        "release_revision": current_revision,
        "package_sha256": "3" * 64,
        "package_manifest_sha256": "4" * 64,
        "public_key_id": "5" * 64,
        "runtime": {"release": str(current_release)},
    }
    historical_raw = provisioning.foundation.canonical_json_bytes(
        historical_receipt
    )
    current_raw = provisioning.foundation.canonical_json_bytes(current_receipt)
    historical_layout.receipt.write_bytes(historical_raw)
    historical_layout.receipt.chmod(0o444)
    current_layout.receipt.write_bytes(current_raw)
    current_layout.receipt.chmod(0o444)
    runtime_receipt.write_bytes(historical_raw)
    runtime_receipt.chmod(0o440)

    monkeypatch.setattr(
        provisioning,
        "cloud_layout",
        lambda revision: (
            historical_layout
            if revision == historical_revision
            else (_ for _ in ()).throw(AssertionError("unexpected revision"))
        ),
    )
    monkeypatch.setattr(
        provisioning,
        "_validate_release_and_authority",
        lambda selected: (
            historical_authority
            if selected is historical_layout
            else (_ for _ in ()).throw(AssertionError("unexpected layout"))
        ),
    )
    monkeypatch.setattr(
        provisioning,
        "_verify_receipt",
        lambda receipt, *, public_key: dict(receipt),
    )
    return (
        current_layout,
        current_raw,
        historical_raw,
        historical_layout.receipt,
        runtime_receipt,
    )


def test_runtime_receipt_projection_replaces_only_verified_historical_copy(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    (
        layout,
        current_raw,
        historical_raw,
        historical_receipt,
        runtime_receipt,
    ) = _runtime_projection_case(tmp_path, monkeypatch)
    current = provisioning._canonical_mapping(
        current_raw,
        code="test_invalid",
    )

    evidence = provisioning._install_runtime_receipt_projection(
        layout,
        current,
        current_raw,
        public_key=Ed25519PrivateKey.generate().public_key(),
    )

    assert runtime_receipt.read_bytes() == current_raw
    assert historical_receipt.read_bytes() == historical_raw
    assert layout.receipt.read_bytes() == current_raw
    assert evidence["path"] == str(runtime_receipt)
    assert evidence["sha256"] == hashlib.sha256(current_raw).hexdigest()
    assert not (
        runtime_receipt.parent
        / f".{runtime_receipt.name}.muncho-replacement"
    ).exists()


def test_runtime_receipt_projection_rejects_unbacked_historical_copy(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    (
        layout,
        current_raw,
        historical_raw,
        historical_receipt,
        runtime_receipt,
    ) = _runtime_projection_case(tmp_path, monkeypatch)
    historical_receipt.chmod(0o644)
    historical_receipt.write_bytes(b'{"different":"authoritative-copy"}')
    historical_receipt.chmod(0o444)
    current = provisioning._canonical_mapping(
        current_raw,
        code="test_invalid",
    )

    with pytest.raises(
        provisioning.TrustedSignerProvisioningError,
        match="trusted_signer_runtime_receipt_recovery_invalid",
    ):
        provisioning._install_runtime_receipt_projection(
            layout,
            current,
            current_raw,
            public_key=Ed25519PrivateKey.generate().public_key(),
        )

    assert runtime_receipt.read_bytes() == historical_raw
    assert layout.receipt.read_bytes() == current_raw


def test_runtime_receipt_projection_replays_after_staged_crash(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    (
        layout,
        current_raw,
        historical_raw,
        historical_receipt,
        runtime_receipt,
    ) = _runtime_projection_case(tmp_path, monkeypatch)
    current = provisioning._canonical_mapping(
        current_raw,
        code="test_invalid",
    )
    replacement = (
        runtime_receipt.parent
        / f".{runtime_receipt.name}.muncho-replacement"
    )

    with pytest.raises(SimulatedCrash):
        provisioning._install_runtime_receipt_projection(
            layout,
            current,
            current_raw,
            public_key=Ed25519PrivateKey.generate().public_key(),
            after_stage=_crash,
        )

    assert runtime_receipt.read_bytes() == historical_raw
    assert replacement.read_bytes() == current_raw
    assert historical_receipt.read_bytes() == historical_raw

    provisioning._install_runtime_receipt_projection(
        layout,
        current,
        current_raw,
        public_key=Ed25519PrivateKey.generate().public_key(),
    )

    assert runtime_receipt.read_bytes() == current_raw
    assert not replacement.exists()


def _projection_rollover_case(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> tuple[
    provisioning.SignerLayout,
    Mapping[str, object],
    Mapping[str, bytes],
    Mapping[str, bytes],
]:
    previous_revision = "a" * 40
    current_revision = "b" * 40
    uid = os.getuid()
    gid = os.getgid()
    release_base = tmp_path / "releases"
    previous_release = release_base / previous_revision
    current_release = release_base / current_revision
    previous_release.mkdir(parents=True)
    current_release.mkdir()
    receipt_root = tmp_path / "receipts"
    receipt_root.mkdir()

    def layout(revision: str) -> provisioning.SignerLayout:
        release = release_base / revision
        return provisioning.SignerLayout(
            role="cloud",
            release_base=release_base,
            release=release,
            authority_manifest=release / "package-manifest.json",
            pinned_public_key=release / "cloud.pub",
            private_key=tmp_path / "private.key",
            installed_public_key=tmp_path / "installed.pub",
            config=tmp_path / "config.json",
            replay_directory=tmp_path / "replay",
            receipt=receipt_root / f"cloud-signer-{revision}.json",
            lock=tmp_path / "lock",
            activation_seal=tmp_path / "activation-seal",
            current_link=tmp_path / "current",
            private_uid=uid,
            private_gid=gid,
            config_uid=uid,
            config_gid=gid,
            replay_uid=uid,
            replay_gid=gid,
            receipt_uid=uid,
            receipt_gid=gid,
            sudoers=tmp_path / "sudoers",
            sudoers_template=release / "sudoers.in",
            release_uid=uid,
            release_gid=gid,
            runtime_entrypoint_name="provision",
        )

    current_layout = layout(current_revision)
    previous_layout = layout(previous_revision)
    projection_paths = {
        "installed_public_key": (
            current_layout.installed_public_key,
            uid,
            gid,
            0o444,
        ),
        "config": (current_layout.config, uid, gid, 0o444),
        "sudoers": (current_layout.sudoers, uid, gid, 0o440),
    }
    monkeypatch.setattr(
        provisioning,
        "_projection_paths",
        lambda selected: (
            projection_paths
            if selected in {current_layout, previous_layout}
            else (_ for _ in ()).throw(AssertionError("unexpected layout"))
        ),
    )
    previous_public = Ed25519PrivateKey.generate().public_key().public_bytes_raw()
    current_private = Ed25519PrivateKey.generate()
    current_public = current_private.public_key().public_bytes_raw()
    previous_authority: Mapping[str, object] = {
        "package_sha256": "1" * 64,
        "public_raw": previous_public,
        "public_key_id": hashlib.sha256(previous_public).hexdigest(),
    }
    current_authority: Mapping[str, object] = {
        "package_sha256": "2" * 64,
        "public_raw": current_public,
        "public_key_id": hashlib.sha256(current_public).hexdigest(),
    }
    previous_payloads = {
        "installed_public_key": previous_public,
        "config": b'{"projection":"previous"}',
        "sudoers": (
            f"root ALL=(root) NOPASSWD: "
            f"{release_base}/{previous_revision}/bin/provision\n"
        ).encode("ascii"),
    }
    current_payloads = {
        "installed_public_key": current_public,
        "config": b'{"projection":"current"}',
        "sudoers": (
            f"root ALL=(root) NOPASSWD: "
            f"{release_base}/{current_revision}/bin/provision\n"
        ).encode("ascii"),
    }
    current_layout.private_key.write_bytes(current_private.private_bytes_raw())
    current_layout.private_key.chmod(0o400)
    for name, (path, _uid, _gid, mode) in provisioning._projection_paths(
        current_layout
    ).items():
        path.write_bytes(previous_payloads[name])
        path.chmod(mode)

    monkeypatch.setattr(
        provisioning,
        "_projection_layout",
        lambda revision, *, role: (
            previous_layout
            if revision == previous_revision and role == "cloud"
            else (
                current_layout
                if revision == current_revision and role == "cloud"
                else (_ for _ in ()).throw(
                    AssertionError("unexpected release")
                )
            )
        ),
    )
    monkeypatch.setattr(
        provisioning,
        "_validate_release_and_authority",
        lambda selected: (
            previous_authority
            if selected is previous_layout
            else (
                current_authority
                if selected is current_layout
                else (_ for _ in ()).throw(
                    AssertionError("unexpected layout")
                )
            )
        ),
    )
    monkeypatch.setattr(
        provisioning,
        "_projection_payloads",
        lambda selected, *, authority: (
            previous_payloads
            if selected is previous_layout and authority is previous_authority
            else (
                current_payloads
                if selected is current_layout and authority is current_authority
                else (_ for _ in ()).throw(
                    AssertionError("unexpected projection")
                )
            )
        ),
    )
    return (
        current_layout,
        current_authority,
        previous_payloads,
        current_payloads,
    )


def test_projection_rollover_replaces_only_exact_predecessor(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    (
        layout,
        authority,
        previous_payloads,
        current_payloads,
    ) = _projection_rollover_case(tmp_path, monkeypatch)

    provisioning._recover_release_bound_projections(
        layout,
        authority=authority,
    )

    for name, (path, _uid, _gid, _mode) in provisioning._projection_paths(
        layout
    ).items():
        assert path.read_bytes() == current_payloads[name]
        assert path.read_bytes() != previous_payloads[name]
    intent = provisioning._projection_intent_path(layout)
    assert intent.exists()
    assert intent.stat().st_mode & 0o777 == 0o444


def test_projection_rollover_replays_mixed_crash_state(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    (
        layout,
        authority,
        _previous_payloads,
        current_payloads,
    ) = _projection_rollover_case(tmp_path, monkeypatch)

    def crash_after_config_stage(name: str) -> None:
        if name == "config":
            raise SimulatedCrash("simulated_projection_rollover_crash")

    with pytest.raises(SimulatedCrash):
        provisioning._recover_release_bound_projections(
            layout,
            authority=authority,
            after_stage=crash_after_config_stage,
        )

    provisioning._recover_release_bound_projections(
        layout,
        authority=authority,
    )

    for name, (path, _uid, _gid, _mode) in provisioning._projection_paths(
        layout
    ).items():
        assert path.read_bytes() == current_payloads[name]
        replacement = path.parent / (
            f".{path.name}.muncho-projection-{layout.release.name}"
        )
        assert not replacement.exists()


def test_projection_rollover_rejects_unbound_projection(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    (
        layout,
        authority,
        _previous_payloads,
        current_payloads,
    ) = _projection_rollover_case(tmp_path, monkeypatch)
    layout.installed_public_key.chmod(0o644)
    unbound_public = Ed25519PrivateKey.generate().public_key().public_bytes_raw()
    assert unbound_public != current_payloads["installed_public_key"]
    layout.installed_public_key.write_bytes(unbound_public)
    layout.installed_public_key.chmod(0o444)

    with pytest.raises(
        provisioning.TrustedSignerProvisioningError,
        match="trusted_signer_projection_reconciliation_invalid",
    ):
        provisioning._recover_release_bound_projections(
            layout,
            authority=authority,
        )

    assert not provisioning._projection_intent_path(layout).exists()
    assert not provisioning._projection_reconciliation_intent_path(layout).exists()


def test_projection_rollover_never_replaces_mismatched_private_lineage(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    (
        layout,
        authority,
        previous_payloads,
        _current_payloads,
    ) = _projection_rollover_case(tmp_path, monkeypatch)
    layout.private_key.chmod(0o600)
    layout.private_key.write_bytes(Ed25519PrivateKey.generate().private_bytes_raw())
    layout.private_key.chmod(0o400)

    with pytest.raises(
        provisioning.TrustedSignerProvisioningError,
        match="trusted_signer_private_public_mismatch",
    ):
        provisioning._recover_release_bound_projections(
            layout,
            authority=authority,
        )

    for name, (path, _uid, _gid, _mode) in provisioning._projection_paths(
        layout
    ).items():
        assert path.read_bytes() == previous_payloads[name]
    assert not provisioning._projection_intent_path(layout).exists()


def test_projection_rollover_rechecks_inert_boundary_after_stage(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    (
        layout,
        authority,
        previous_payloads,
        current_payloads,
    ) = _projection_rollover_case(tmp_path, monkeypatch)

    def activate_after_stage(name: str) -> None:
        if name == "installed_public_key":
            layout.activation_seal.write_bytes(b"activation-race")

    with pytest.raises(
        provisioning.TrustedSignerProvisioningError,
        match="trusted_signer_activation_not_inert",
    ):
        provisioning._recover_release_bound_projections(
            layout,
            authority=authority,
            after_stage=activate_after_stage,
        )

    assert (
        layout.installed_public_key.read_bytes()
        == previous_payloads["installed_public_key"]
    )
    layout.activation_seal.unlink()

    provisioning._recover_release_bound_projections(
        layout,
        authority=authority,
    )

    for name, (path, _uid, _gid, _mode) in provisioning._projection_paths(
        layout
    ).items():
        assert path.read_bytes() == current_payloads[name]


def test_projection_reconciliation_recovers_fragmented_release_state(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    (
        layout,
        authority,
        previous_payloads,
        current_payloads,
    ) = _projection_rollover_case(tmp_path, monkeypatch)
    assert layout.sudoers is not None
    layout.sudoers.chmod(0o640)
    layout.sudoers.write_bytes(current_payloads["sudoers"])
    layout.sudoers.chmod(0o440)

    def crash_after_config_stage(name: str) -> None:
        if name == "config":
            raise SimulatedCrash("simulated_fragment_reconciliation_crash")

    with pytest.raises(SimulatedCrash):
        provisioning._recover_release_bound_projections(
            layout,
            authority=authority,
            after_stage=crash_after_config_stage,
        )

    intent_path = provisioning._projection_reconciliation_intent_path(layout)
    intent = json.loads(intent_path.read_text())
    assert intent["schema"] == provisioning.PROJECTION_RECONCILIATION_SCHEMA
    assert (
        intent["source_projections"]["installed_public_key"][
            "release_revision"
        ]
        == "a" * 40
    )
    assert (
        intent["source_projections"]["config"]["release_revision"]
        == "a" * 40
    )
    assert (
        intent["source_projections"]["sudoers"]["release_revision"]
        == layout.release.name
    )
    assert not provisioning._projection_intent_path(layout).exists()

    provisioning._recover_release_bound_projections(
        layout,
        authority=authority,
    )

    for name, (path, _uid, _gid, _mode) in provisioning._projection_paths(
        layout
    ).items():
        assert path.read_bytes() == current_payloads[name]
        assert not (
            path.parent
            / f".{path.name}.muncho-projection-{layout.release.name}"
        ).exists()
    assert previous_payloads["sudoers"] != current_payloads["sudoers"]


def _private_key_rollover_case(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> tuple[
    provisioning.SignerLayout,
    Mapping[str, object],
    bytes,
    bytes,
]:
    previous_revision = "c" * 40
    current_revision = "d" * 40
    uid = os.getuid()
    gid = os.getgid()
    release_base = tmp_path / "releases"
    previous_release = release_base / previous_revision
    current_release = release_base / current_revision
    previous_release.mkdir(parents=True)
    current_release.mkdir()
    receipt_root = tmp_path / "receipts"
    receipt_root.mkdir()

    def make_layout(revision: str) -> provisioning.SignerLayout:
        release = release_base / revision
        return provisioning.SignerLayout(
            role="host",
            release_base=release_base,
            release=release,
            authority_manifest=release / "package-manifest.json",
            pinned_public_key=release / "host.pub",
            private_key=tmp_path / "private.key",
            installed_public_key=tmp_path / "installed.pub",
            config=tmp_path / "config.json",
            replay_directory=tmp_path / "replay",
            receipt=receipt_root / f"host-signer-{revision}.json",
            lock=tmp_path / "lock",
            activation_seal=tmp_path / "activation-seal",
            current_link=tmp_path / "current",
            private_uid=uid,
            private_gid=gid,
            config_uid=uid,
            config_gid=gid,
            replay_uid=uid,
            replay_gid=gid,
            receipt_uid=uid,
            receipt_gid=gid,
            release_uid=uid,
            release_gid=gid,
            sudoers=tmp_path / "sudoers",
            sudoers_template=release / "sudoers.in",
            runtime_entrypoint_name="provision",
        )

    previous_layout = make_layout(previous_revision)
    current_layout = make_layout(current_revision)
    previous_private = Ed25519PrivateKey.generate()
    current_private = Ed25519PrivateKey.generate()
    previous_seed = previous_private.private_bytes_raw()
    current_seed = current_private.private_bytes_raw()
    previous_public = previous_private.public_key().public_bytes_raw()
    current_public = current_private.public_key().public_bytes_raw()
    previous_authority: Mapping[str, object] = {
        "package_sha256": "3" * 64,
        "manifest_sha256": "4" * 64,
        "public_raw": previous_public,
        "public_key_id": hashlib.sha256(previous_public).hexdigest(),
        "runtime": {"release": previous_revision},
    }
    current_authority: Mapping[str, object] = {
        "package_sha256": "5" * 64,
        "manifest_sha256": "6" * 64,
        "public_raw": current_public,
        "public_key_id": hashlib.sha256(current_public).hexdigest(),
        "runtime": {"release": current_revision},
    }
    previous_receipt: Mapping[str, object] = {
        "release_revision": previous_revision,
        "receipt_sha256": "7" * 64,
    }
    previous_receipt_raw = b"immutable signed predecessor receipt"
    previous_layout.receipt.write_bytes(previous_receipt_raw)
    previous_layout.receipt.chmod(0o444)
    current_layout.private_key.write_bytes(previous_seed)
    current_layout.private_key.chmod(0o400)

    monkeypatch.setattr(
        provisioning,
        "_projection_layout",
        lambda revision, *, role: (
            previous_layout
            if revision == previous_revision and role == "host"
            else (
                current_layout
                if revision == current_revision and role == "host"
                else (_ for _ in ()).throw(
                    AssertionError("unexpected signer release")
                )
            )
        ),
    )
    monkeypatch.setattr(
        provisioning,
        "_validate_release_and_authority",
        lambda selected: (
            previous_authority
            if selected is previous_layout
            else (
                current_authority
                if selected is current_layout
                else (_ for _ in ()).throw(
                    AssertionError("unexpected signer layout")
                )
            )
        ),
    )
    monkeypatch.setattr(
        provisioning,
        "_validated_historical_signer_receipt",
        lambda selected, *, authority: (
            (previous_receipt, previous_receipt_raw)
            if selected is previous_layout and authority is previous_authority
            else (_ for _ in ()).throw(
                AssertionError("unexpected predecessor receipt")
            )
        ),
    )
    return current_layout, current_authority, previous_seed, current_seed


def test_private_key_rollover_replaces_only_proven_predecessor_without_digest(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    layout, authority, previous_seed, current_seed = _private_key_rollover_case(
        tmp_path,
        monkeypatch,
    )

    evidence = provisioning._recover_release_bound_private_key(
        layout,
        authority=authority,
        seed=current_seed,
    )

    assert layout.private_key.read_bytes() == current_seed
    assert layout.private_key.read_bytes() != previous_seed
    assert "sha256" not in evidence
    intent_path = provisioning._private_key_rollover_intent_path(layout)
    intent_raw = intent_path.read_bytes()
    intent = json.loads(intent_raw)
    assert intent["schema"] == provisioning.PRIVATE_KEY_ROLLOVER_SCHEMA
    assert intent["private_key_replacement_authorized"] is True
    assert intent["private_key_material_recorded"] is False
    assert intent["private_key_digest_recorded"] is False
    assert set(intent["private_key"]) == {"path", "uid", "gid", "mode", "size"}
    assert current_seed.hex().encode("ascii") not in intent_raw
    assert previous_seed.hex().encode("ascii") not in intent_raw
    assert hashlib.sha256(current_seed).hexdigest().encode("ascii") not in intent_raw
    assert hashlib.sha256(previous_seed).hexdigest().encode("ascii") not in intent_raw
    assert not provisioning._private_key_rollover_stage_path(layout).exists()


def test_private_key_rollover_selects_canonical_equivalent_predecessor(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    layout, _authority, previous_seed, _current_seed = (
        _private_key_rollover_case(tmp_path, monkeypatch)
    )
    first_revision = "b" * 40
    later_revision = "c" * 40
    later_layout = provisioning._projection_layout(
        later_revision,
        role="host",
    )
    first_release = later_layout.release_base / first_revision
    first_release.mkdir()
    first_layout = replace(
        later_layout,
        release=first_release,
        authority_manifest=first_release / "package-manifest.json",
        pinned_public_key=first_release / "host.pub",
        receipt=(
            later_layout.receipt.parent
            / f"host-signer-{first_revision}.json"
        ),
        sudoers_template=first_release / "sudoers.in",
    )
    first_layout.receipt.write_bytes(b"first signed predecessor receipt")
    first_layout.receipt.chmod(0o444)
    previous_public = (
        Ed25519PrivateKey.from_private_bytes(previous_seed)
        .public_key()
        .public_bytes_raw()
    )
    later_authority = provisioning._validate_release_and_authority(
        later_layout
    )
    first_authority = {
        **later_authority,
        "package_sha256": "8" * 64,
        "manifest_sha256": "9" * 64,
        "runtime": {"release": first_revision},
    }
    receipts = {
        first_revision: (
            {
                "release_revision": first_revision,
                "receipt_sha256": "a" * 64,
            },
            b"first signed predecessor receipt",
        ),
        later_revision: (
            {
                "release_revision": later_revision,
                "receipt_sha256": "7" * 64,
            },
            b"immutable signed predecessor receipt",
        ),
    }

    monkeypatch.setattr(
        provisioning,
        "_projection_layout",
        lambda revision, *, role: (
            first_layout
            if revision == first_revision and role == "host"
            else (
                later_layout
                if revision == later_revision and role == "host"
                else (_ for _ in ()).throw(
                    AssertionError("unexpected signer release")
                )
            )
        ),
    )
    monkeypatch.setattr(
        provisioning,
        "_validate_release_and_authority",
        lambda selected: (
            first_authority
            if selected is first_layout
            else (
                later_authority
                if selected is later_layout
                else (_ for _ in ()).throw(
                    AssertionError("unexpected signer layout")
                )
            )
        ),
    )
    monkeypatch.setattr(
        provisioning,
        "_validated_historical_signer_receipt",
        lambda selected, *, authority: receipts[selected.release.name],
    )

    selected, selected_authority, receipt, receipt_raw = (
        provisioning._find_private_key_predecessor(
            layout,
            existing_public_raw=previous_public,
        )
    )

    assert selected is first_layout
    assert selected_authority is first_authority
    assert receipt == receipts[first_revision][0]
    assert receipt_raw == receipts[first_revision][1]


@pytest.mark.parametrize("crash_window", ("after_stage", "after_replace"))
def test_private_key_rollover_replays_crash_windows(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    crash_window: str,
) -> None:
    layout, authority, previous_seed, current_seed = _private_key_rollover_case(
        tmp_path,
        monkeypatch,
    )

    with pytest.raises(SimulatedCrash):
        provisioning._recover_release_bound_private_key(
            layout,
            authority=authority,
            seed=current_seed,
            after_stage=(
                _crash if crash_window == "after_stage" else None
            ),
            after_replace=(
                _crash if crash_window == "after_replace" else None
            ),
        )

    if crash_window == "after_stage":
        assert layout.private_key.read_bytes() == previous_seed
        assert provisioning._private_key_rollover_stage_path(layout).exists()
    else:
        assert layout.private_key.read_bytes() == current_seed

    evidence = provisioning._recover_release_bound_private_key(
        layout,
        authority=authority,
        seed=current_seed,
    )
    assert layout.private_key.read_bytes() == current_seed
    assert "sha256" not in evidence
    assert not provisioning._private_key_rollover_stage_path(layout).exists()


def test_private_key_rollover_rejects_unknown_lineage_without_mutation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    layout, authority, previous_seed, current_seed = _private_key_rollover_case(
        tmp_path,
        monkeypatch,
    )
    layout.receipt.parent.joinpath(
        f"host-signer-{'c' * 40}.json"
    ).unlink()

    with pytest.raises(
        provisioning.TrustedSignerProvisioningError,
        match="trusted_signer_private_key_rollover_invalid",
    ):
        provisioning._recover_release_bound_private_key(
            layout,
            authority=authority,
            seed=current_seed,
        )

    assert layout.private_key.read_bytes() == previous_seed
    assert not provisioning._private_key_rollover_intent_path(layout).exists()
    assert not provisioning._private_key_rollover_stage_path(layout).exists()


def test_private_key_rollover_rechecks_inert_boundary_before_mutation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    layout, authority, previous_seed, current_seed = _private_key_rollover_case(
        tmp_path,
        monkeypatch,
    )
    layout.activation_seal.write_bytes(b"active")

    with pytest.raises(
        provisioning.TrustedSignerProvisioningError,
        match="trusted_signer_activation_not_inert",
    ):
        provisioning._recover_release_bound_private_key(
            layout,
            authority=authority,
            seed=current_seed,
        )

    assert layout.private_key.read_bytes() == previous_seed
    assert not provisioning._private_key_rollover_intent_path(layout).exists()
    assert not provisioning._private_key_rollover_stage_path(layout).exists()
