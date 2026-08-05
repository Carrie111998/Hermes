from __future__ import annotations

import copy
import hashlib
import os
from pathlib import Path

import pytest

from gateway import canonical_writer_pre_phase_b_start as permit_module


def _sha(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def _permit_fixture(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    revision = "a" * 40
    monkeypatch.setattr(permit_module, "DEFAULT_CANARY_RELEASES_ROOT", tmp_path)
    artifact_root = tmp_path / revision
    bootstrap = (
        artifact_root
        / "venv/lib/python3.11/site-packages/gateway/canonical_writer_bootstrap.py"
    )
    bootstrap.parent.mkdir(parents=True)
    bootstrap.write_bytes(b"bootstrap-module")
    manifest = artifact_root / "release-manifest.json"
    manifest.write_bytes(b'{"release":"exact"}\n')
    config = Path("/etc/muncho-canonical-writer/writer.json")
    config_raw = b'{"writer":"exact"}\n'
    observed = {
        bootstrap: bootstrap.read_bytes(),
        manifest: manifest.read_bytes(),
        config: config_raw,
    }

    def read(path: Path, **_kwargs) -> bytes:
        try:
            return observed[path]
        except KeyError:
            pytest.fail(f"unexpected exact file read: {path}")

    monkeypatch.setattr(permit_module, "_read_exact_file", read)
    monkeypatch.setattr(permit_module, "_boot_id_sha256", lambda: "b" * 64)
    value = permit_module.build_pre_phase_b_start_permit(
        revision=revision,
        artifact_root=str(artifact_root),
        artifact_sha256="c" * 64,
        release_manifest_file_sha256=_sha(manifest.read_bytes()),
        writer_config_path=str(config),
        writer_config_sha256=_sha(config_raw),
        writer_uid=999,
        writer_gid=994,
        boot_id_sha256="b" * 64,
        scope="native_observation",
        plan_sha256="d" * 64,
        owner_approval_receipt_sha256="e" * 64,
        owner_approval_expires_at_unix=1_800_000_300,
        external_iam_receipt_sha256="f" * 64,
        now_unix=1_800_000_000,
    )
    return value, artifact_root, bootstrap, config, observed


def test_build_and_validate_exact_pre_phase_b_start_permit(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    value, artifact_root, bootstrap, config, _observed = _permit_fixture(
        tmp_path, monkeypatch
    )
    monkeypatch.setattr(
        permit_module,
        "read_pre_phase_b_start_permit",
        lambda *_args, **_kwargs: copy.deepcopy(value),
    )
    monkeypatch.setattr(permit_module.os, "geteuid", lambda: 999)
    monkeypatch.setattr(permit_module.os, "getegid", lambda: 994)
    metadata = []
    monkeypatch.setattr(
        permit_module,
        "_validate_exact_file_metadata",
        lambda path, **kwargs: metadata.append((path, kwargs)),
    )

    observed = permit_module.validate_installed_pre_phase_b_start_permit(
        config_path=str(config),
        writer_uid=999,
        writer_gid=994,
        module_file=str(bootstrap),
        now_unix=1_800_000_001,
    )

    assert observed == value
    assert observed["artifact_root"] == str(artifact_root)
    assert observed["scope"] == "native_observation"
    assert observed["owner_approval_expires_at_unix"] == 1_800_000_300
    assert observed["expires_at_unix"] - observed["created_at_unix"] == 180
    assert metadata == [
        (
            artifact_root / "release-manifest.json",
            {
                "expected_uid": 0,
                "expected_gid": 0,
                "expected_mode": 0o400,
                "maximum_bytes": 2 * 1024 * 1024,
            },
        )
    ]
    assert permit_module.canonical_pre_phase_b_start_permit_bytes(value).endswith(
        b"\n"
    )


@pytest.mark.parametrize(
    ("field", "replacement"),
    (
        ("revision", "not-a-revision"),
        ("scope", "ordinary_start"),
        ("artifact_sha256", "not-a-digest"),
        ("writer_uid", 0),
        ("writer_config_path", "/tmp/writer.json"),
        ("expires_at_unix", 1_800_000_999),
        ("receipt_sha256", "0" * 64),
    ),
)
def test_permit_mapping_tamper_fails_closed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    field: str,
    replacement: object,
) -> None:
    value, *_rest = _permit_fixture(tmp_path, monkeypatch)
    value[field] = replacement
    with pytest.raises(
        permit_module.PrePhaseBStartPermitError,
        match="pre_phase_b_start_permit_invalid",
    ):
        permit_module.validate_pre_phase_b_start_permit_mapping(value)


def test_permit_expiry_never_outlives_owner_approval(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    value, artifact_root, _bootstrap, config, observed = _permit_fixture(
        tmp_path, monkeypatch
    )
    value = permit_module.build_pre_phase_b_start_permit(
        revision=value["revision"],
        artifact_root=str(artifact_root),
        artifact_sha256=value["artifact_sha256"],
        release_manifest_file_sha256=value["release_manifest_file_sha256"],
        writer_config_path=str(config),
        writer_config_sha256=value["writer_config_sha256"],
        writer_uid=value["writer_uid"],
        writer_gid=value["writer_gid"],
        boot_id_sha256=value["boot_id_sha256"],
        scope=value["scope"],
        plan_sha256=value["plan_sha256"],
        owner_approval_receipt_sha256=value["owner_approval_receipt_sha256"],
        owner_approval_expires_at_unix=1_800_000_045,
        external_iam_receipt_sha256=value["external_iam_receipt_sha256"],
        now_unix=1_800_000_000,
    )

    assert observed[artifact_root / "release-manifest.json"]
    assert value["expires_at_unix"] == 1_800_000_045
    assert value["owner_approval_expires_at_unix"] == 1_800_000_045


def test_installed_permit_expiry_and_boot_binding_fail_closed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    value, _root, bootstrap, config, _observed = _permit_fixture(
        tmp_path, monkeypatch
    )
    monkeypatch.setattr(
        permit_module,
        "read_pre_phase_b_start_permit",
        lambda *_args, **_kwargs: copy.deepcopy(value),
    )
    monkeypatch.setattr(permit_module.os, "geteuid", lambda: 999)
    monkeypatch.setattr(permit_module.os, "getegid", lambda: 994)

    with pytest.raises(permit_module.PrePhaseBStartPermitError):
        permit_module.validate_installed_pre_phase_b_start_permit(
            config_path=str(config),
            writer_uid=999,
            writer_gid=994,
            module_file=str(bootstrap),
            now_unix=value["expires_at_unix"],
        )

    monkeypatch.setattr(permit_module, "_boot_id_sha256", lambda: "0" * 64)
    with pytest.raises(permit_module.PrePhaseBStartPermitError):
        permit_module.validate_installed_pre_phase_b_start_permit(
            config_path=str(config),
            writer_uid=999,
            writer_gid=994,
            module_file=str(bootstrap),
            now_unix=value["created_at_unix"] + 1,
        )


def test_exact_file_reader_rejects_symlink_and_multiple_links(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(permit_module, "_validate_parent_chain", lambda _path: None)
    target = tmp_path / "permit.json"
    target.write_bytes(b"{}")
    target.chmod(0o400)

    assert permit_module._read_exact_file(
        target,
        expected_uid=os.getuid(),
        expected_gid=os.getgid(),
        expected_mode=0o400,
        maximum_bytes=16,
    ) == b"{}"

    linked = tmp_path / "linked.json"
    os.link(target, linked)
    with pytest.raises(permit_module.PrePhaseBStartPermitError):
        permit_module._read_exact_file(
            target,
            expected_uid=os.getuid(),
            expected_gid=os.getgid(),
            expected_mode=0o400,
            maximum_bytes=16,
        )
    linked.unlink()
    target.unlink()
    target.symlink_to(tmp_path / "missing")
    with pytest.raises(permit_module.PrePhaseBStartPermitError):
        permit_module._read_exact_file(
            target,
            expected_uid=os.getuid(),
            expected_gid=os.getgid(),
            expected_mode=0o400,
            maximum_bytes=16,
        )


def test_root_only_manifest_metadata_is_validated_without_opening(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(permit_module, "_validate_parent_chain", lambda _path: None)
    manifest = tmp_path / "release-manifest.json"
    manifest.write_bytes(b'{}\n')
    manifest.chmod(0o400)

    permit_module._validate_exact_file_metadata(
        manifest,
        expected_uid=os.getuid(),
        expected_gid=os.getgid(),
        expected_mode=0o400,
        maximum_bytes=16,
    )

    manifest.chmod(0o440)
    with pytest.raises(permit_module.PrePhaseBStartPermitError):
        permit_module._validate_exact_file_metadata(
            manifest,
            expected_uid=os.getuid(),
            expected_gid=os.getgid(),
            expected_mode=0o400,
            maximum_bytes=16,
        )
