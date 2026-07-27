from __future__ import annotations

import hashlib
import os
from pathlib import Path

import pytest

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
