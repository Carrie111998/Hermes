from __future__ import annotations

import stat
from pathlib import Path
from types import SimpleNamespace

import pytest

from scripts.canary import trusted_signer_stage0 as stage0


def _directory_state(
    mode: int,
    *,
    uid: int = 0,
    gid: int = 0,
) -> SimpleNamespace:
    return SimpleNamespace(
        st_mode=stat.S_IFDIR | mode,
        st_uid=uid,
        st_gid=gid,
    )


@pytest.mark.parametrize("mode", (0o755, 0o775, 0o1777))
def test_host_runtime_lock_accepts_pinned_root_owned_parent_modes(
    mode: int,
) -> None:
    assert stage0._lock_parent_is_trusted(_directory_state(mode))


@pytest.mark.parametrize(
    "state",
    (
        _directory_state(0o777),
        _directory_state(0o1775),
        _directory_state(0o1777, uid=1),
        _directory_state(0o1777, gid=1),
        SimpleNamespace(
            st_mode=stat.S_IFREG | 0o1777,
            st_uid=0,
            st_gid=0,
        ),
        SimpleNamespace(
            st_mode=stat.S_IFLNK | 0o1777,
            st_uid=0,
            st_gid=0,
        ),
    ),
)
def test_host_runtime_lock_rejects_untrusted_parent_contract(
    state: SimpleNamespace,
) -> None:
    assert not stage0._lock_parent_is_trusted(state)


def test_host_runtime_receipt_sha_fields_require_strings() -> None:
    assert stage0._is_sha256("1" * 64)
    assert not stage0._is_sha256(int("1" * 64))


def _local_snapshot(path: Path) -> tuple[bytes, tuple[int, ...]]:
    value = path.stat()
    return path.read_bytes(), (
        value.st_dev,
        value.st_ino,
        value.st_nlink,
        value.st_uid,
        value.st_gid,
        stat.S_IMODE(value.st_mode),
        value.st_size,
        value.st_mtime_ns,
        value.st_ctime_ns,
    )


def test_predecessor_sudoers_replacement_is_atomic_and_replayable(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    destination = tmp_path / "managed-sudoers"
    temporary = tmp_path / ".managed-sudoers.stage0-staged"
    destination.write_bytes(b"managed predecessor\n")
    destination.chmod(0o440)
    successor = tmp_path / ("a" * 40)
    commands: list[tuple[str, ...]] = []

    monkeypatch.setattr(stage0, "_sudoers_snapshot", _local_snapshot)
    monkeypatch.setattr(
        stage0,
        "_validate_predecessor_sudoers",
        lambda raw, *, successor_release: successor_release,
    )
    monkeypatch.setattr(stage0, "_fsync_directory", lambda _path: None)
    monkeypatch.setattr(stage0.os, "fchown", lambda *_args: None)
    monkeypatch.setattr(
        stage0,
        "HOST_CURRENT_LINK",
        tmp_path / "current-absent",
    )
    monkeypatch.setattr(
        stage0,
        "HOST_ACTIVATION_SEAL",
        tmp_path / "activation-absent",
    )

    stage0._replace_predecessor_sudoers(
        b"managed successor\n",
        successor_release=successor,
        destination=destination,
        temporary=temporary,
        runner=lambda argv: commands.append(tuple(argv)) or b"",
        after_open=None,
    )

    assert destination.read_bytes() == b"managed successor\n"
    assert stat.S_IMODE(destination.stat().st_mode) == 0o440
    assert not temporary.exists()
    assert commands == [
        ("/usr/sbin/visudo", "-cf", str(temporary)),
    ]


def test_predecessor_sudoers_replacement_rejects_destination_race(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    destination = tmp_path / "managed-sudoers"
    temporary = tmp_path / ".managed-sudoers.stage0-staged"
    destination.write_bytes(b"managed predecessor\n")
    destination.chmod(0o440)

    monkeypatch.setattr(stage0, "_sudoers_snapshot", _local_snapshot)
    monkeypatch.setattr(
        stage0,
        "_validate_predecessor_sudoers",
        lambda raw, *, successor_release: successor_release,
    )
    monkeypatch.setattr(stage0, "_fsync_directory", lambda _path: None)
    monkeypatch.setattr(stage0.os, "fchown", lambda *_args: None)
    monkeypatch.setattr(
        stage0,
        "HOST_CURRENT_LINK",
        tmp_path / "current-absent",
    )
    monkeypatch.setattr(
        stage0,
        "HOST_ACTIVATION_SEAL",
        tmp_path / "activation-absent",
    )

    def race() -> None:
        destination.chmod(0o640)
        destination.write_bytes(b"raced destination\n")
        destination.chmod(0o440)

    with pytest.raises(
        stage0.TrustedSignerStage0Error,
        match="trusted_signer_stage0_sudoers_conflict",
    ):
        stage0._replace_predecessor_sudoers(
            b"managed successor\n",
            successor_release=tmp_path / ("a" * 40),
            destination=destination,
            temporary=temporary,
            runner=lambda _argv: b"",
            after_open=race,
        )

    assert destination.read_bytes() == b"raced destination\n"
    assert temporary.read_bytes() == b"managed successor\n"


def test_predecessor_sudoers_must_match_one_immutable_managed_release(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    predecessor_revision = "b" * 40
    successor_revision = "c" * 40
    release_base = tmp_path / "releases"
    predecessor = release_base / predecessor_revision
    template = (
        predecessor
        / "ops/muncho/owner-gate/"
        "muncho-host-observation-attestor.sudoers.in"
    )
    template.parent.mkdir(parents=True)
    template.write_bytes(
        b"Cmnd_Alias TEST = "
        b"/opt/muncho-trusted-observation/releases/@RELEASE_SHA@/"
        b"venv/bin/python -I -B\n"
    )
    template.chmod(0o444)
    for relative in (
        "venv/bin/python",
        "bin/muncho-host-trusted-signer-provision",
        "bin/muncho-host-observation-attestor",
    ):
        path = predecessor / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"executable\n")
        path.chmod(0o555)
    predecessor.chmod(0o555)
    rendered = template.read_bytes().replace(
        b"@RELEASE_SHA@",
        predecessor_revision.encode("ascii"),
    )
    broken_staging = rendered.replace(
        f"/releases/{predecessor_revision}/".encode("ascii"),
        f"/releases/.{predecessor_revision}.bootstrap/".encode("ascii"),
    )
    real_lstat = Path.lstat

    def root_lstat(path: Path) -> SimpleNamespace:
        value = real_lstat(path)
        return SimpleNamespace(
            st_mode=value.st_mode,
            st_uid=0,
            st_gid=0,
        )

    def root_reader(path: Path, **kwargs: object) -> bytes:
        assert kwargs["expected_uid"] == 0
        assert kwargs.get("expected_gid", 0) == 0
        return path.read_bytes()

    monkeypatch.setattr(stage0, "HOST_RELEASE_BASE", release_base)
    monkeypatch.setattr(Path, "lstat", root_lstat)
    monkeypatch.setattr(stage0.stage0, "_read_regular", root_reader)

    def render(
        _release: Path,
        *,
        command_release: Path | None = None,
    ) -> bytes:
        return broken_staging if command_release is not None else rendered

    monkeypatch.setattr(stage0, "_render_sudoers", render)

    assert stage0._validate_predecessor_sudoers(
        rendered,
        successor_release=release_base / successor_revision,
    ) == predecessor
    assert stage0._validate_predecessor_sudoers(
        broken_staging,
        successor_release=release_base / successor_revision,
    ) == predecessor

    ambiguous = rendered + rendered.replace(
        predecessor_revision.encode("ascii"),
        ("d" * 40).encode("ascii"),
    )
    with pytest.raises(
        stage0.TrustedSignerStage0Error,
        match="trusted_signer_stage0_sudoers_conflict",
    ):
        stage0._validate_predecessor_sudoers(
            ambiguous,
            successor_release=release_base / successor_revision,
        )


def _runtime_receipt(
    *,
    revision: str,
    package_sha256: str,
    release_evidence: dict[str, object],
    sudoers_evidence: dict[str, object],
    runtime_inventory: dict[str, object],
) -> dict[str, object]:
    release = Path(str(release_evidence["path"]))
    unsigned = {
        "schema": stage0.HOST_RUNTIME_RECEIPT_SCHEMA,
        "release_revision": revision,
        "package_sha256": package_sha256,
        "preflight_sha256": "1" * 64,
        "release": release_evidence,
        "sudoers": sudoers_evidence,
        "runtime_inventory_sha256": stage0.stage0.sha256_json(
            runtime_inventory
        ),
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
        "receipt_sha256": stage0.stage0.sha256_json(unsigned),
    }


def test_verify_host_offline_runtime_recomputes_exact_receipt_without_mutation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    revision = "e" * 40
    package_sha256 = "2" * 64
    release_base = tmp_path / "releases"
    release = release_base / revision
    current = tmp_path / "current"
    activation = tmp_path / "activation"
    sudoers_path = tmp_path / "sudoers"
    release_evidence = {
        "path": str(release),
        "uid": 0,
        "gid": 0,
        "mode": "0555",
        "projection_sha256": "3" * 64,
        "projection_count": 41,
    }
    sudoers_evidence = {
        "path": str(sudoers_path),
        "uid": 0,
        "gid": 0,
        "mode": "0440",
        "sha256": "4" * 64,
    }
    runtime_inventory = {"installed": "exact"}
    expected = _runtime_receipt(
        revision=revision,
        package_sha256=package_sha256,
        release_evidence=release_evidence,
        sudoers_evidence=sudoers_evidence,
        runtime_inventory=runtime_inventory,
    )
    commands: list[tuple[str, ...]] = []
    before = tuple(tmp_path.iterdir())

    monkeypatch.setattr(stage0.os, "geteuid", lambda: 0)
    monkeypatch.setattr(stage0, "HOST_RELEASE_BASE", release_base)
    monkeypatch.setattr(stage0, "HOST_SUDOERS_PATH", sudoers_path)
    monkeypatch.setattr(stage0, "HOST_CURRENT_LINK", current)
    monkeypatch.setattr(stage0, "HOST_ACTIVATION_SEAL", activation)
    monkeypatch.setattr(
        stage0,
        "_load_host_release_package",
        lambda selected, *, release_revision: {
            "package_sha256": package_sha256,
        },
    )
    monkeypatch.setattr(
        stage0,
        "_verify_sealed_host_release",
        lambda selected: release_evidence,
    )
    monkeypatch.setattr(
        stage0,
        "_verify_host_sudoers",
        lambda selected, *, sudoers_path: sudoers_evidence,
    )
    monkeypatch.setattr(
        stage0.stage0,
        "validate_runtime_inventory",
        lambda raw, *, venv, manifest: runtime_inventory,
    )

    def runner(argv: tuple[str, ...], **_kwargs: object) -> bytes:
        commands.append(tuple(argv))
        return b'{"installed":"exact"}'

    checked = stage0.verify_host_offline_runtime(
        revision,
        expected_receipt=expected,
        command_runner=runner,
        release_base=release_base,
        sudoers_path=sudoers_path,
        current_link=current,
        activation_seal=activation,
    )

    assert checked == expected
    assert commands == [(
        str(release / "venv/bin/python"),
        "-I",
        "-B",
        "-c",
        stage0.stage0._runtime_inventory_probe_code(),
    )]
    assert tuple(tmp_path.iterdir()) == before


def test_verify_host_offline_runtime_rejects_live_projection_drift(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    revision = "e" * 40
    package_sha256 = "2" * 64
    release_base = tmp_path / "releases"
    release = release_base / revision
    sudoers_path = tmp_path / "sudoers"
    frozen_release = {
        "path": str(release),
        "uid": 0,
        "gid": 0,
        "mode": "0555",
        "projection_sha256": "3" * 64,
        "projection_count": 41,
    }
    live_release = {
        **frozen_release,
        "projection_sha256": "9" * 64,
    }
    sudoers_evidence = {
        "path": str(sudoers_path),
        "uid": 0,
        "gid": 0,
        "mode": "0440",
        "sha256": "4" * 64,
    }
    runtime_inventory = {"installed": "exact"}
    expected = _runtime_receipt(
        revision=revision,
        package_sha256=package_sha256,
        release_evidence=frozen_release,
        sudoers_evidence=sudoers_evidence,
        runtime_inventory=runtime_inventory,
    )

    monkeypatch.setattr(stage0.os, "geteuid", lambda: 0)
    monkeypatch.setattr(stage0, "HOST_RELEASE_BASE", release_base)
    monkeypatch.setattr(stage0, "HOST_SUDOERS_PATH", sudoers_path)
    monkeypatch.setattr(
        stage0,
        "HOST_CURRENT_LINK",
        tmp_path / "current",
    )
    monkeypatch.setattr(
        stage0,
        "HOST_ACTIVATION_SEAL",
        tmp_path / "activation",
    )
    monkeypatch.setattr(
        stage0,
        "_load_host_release_package",
        lambda *_args, **_kwargs: {"package_sha256": package_sha256},
    )
    monkeypatch.setattr(
        stage0,
        "_verify_sealed_host_release",
        lambda _selected: live_release,
    )
    monkeypatch.setattr(
        stage0,
        "_verify_host_sudoers",
        lambda *_args, **_kwargs: sudoers_evidence,
    )
    monkeypatch.setattr(
        stage0.stage0,
        "validate_runtime_inventory",
        lambda *_args, **_kwargs: runtime_inventory,
    )
    commands = 0

    def runner(*_args: object, **_kwargs: object) -> bytes:
        nonlocal commands
        commands += 1
        return b"{}"

    with pytest.raises(
        stage0.TrustedSignerStage0Error,
        match="trusted_signer_stage0_host_runtime_receipt_mismatch",
    ):
        stage0.verify_host_offline_runtime(
            revision,
            expected_receipt=expected,
            command_runner=runner,
            release_base=release_base,
            sudoers_path=sudoers_path,
            current_link=tmp_path / "current",
            activation_seal=tmp_path / "activation",
        )
    assert commands == 0


def test_verify_host_offline_runtime_rechecks_projection_after_spawn(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    revision = "e" * 40
    package_sha256 = "2" * 64
    release_base = tmp_path / "releases"
    release = release_base / revision
    sudoers_path = tmp_path / "sudoers"
    frozen_release = {
        "path": str(release),
        "uid": 0,
        "gid": 0,
        "mode": "0555",
        "projection_sha256": "3" * 64,
        "projection_count": 41,
    }
    drifted_release = {
        **frozen_release,
        "projection_sha256": "9" * 64,
    }
    sudoers_evidence = {
        "path": str(sudoers_path),
        "uid": 0,
        "gid": 0,
        "mode": "0440",
        "sha256": "4" * 64,
    }
    runtime_inventory = {"installed": "exact"}
    expected = _runtime_receipt(
        revision=revision,
        package_sha256=package_sha256,
        release_evidence=frozen_release,
        sudoers_evidence=sudoers_evidence,
        runtime_inventory=runtime_inventory,
    )
    scans = iter((frozen_release, drifted_release))
    commands = 0

    monkeypatch.setattr(stage0.os, "geteuid", lambda: 0)
    monkeypatch.setattr(stage0, "HOST_RELEASE_BASE", release_base)
    monkeypatch.setattr(stage0, "HOST_SUDOERS_PATH", sudoers_path)
    monkeypatch.setattr(
        stage0,
        "HOST_CURRENT_LINK",
        tmp_path / "current",
    )
    monkeypatch.setattr(
        stage0,
        "HOST_ACTIVATION_SEAL",
        tmp_path / "activation",
    )
    monkeypatch.setattr(
        stage0,
        "_load_host_release_package",
        lambda *_args, **_kwargs: {"package_sha256": package_sha256},
    )
    monkeypatch.setattr(
        stage0,
        "_verify_sealed_host_release",
        lambda _selected: next(scans),
    )
    monkeypatch.setattr(
        stage0,
        "_verify_host_sudoers",
        lambda *_args, **_kwargs: sudoers_evidence,
    )
    monkeypatch.setattr(
        stage0.stage0,
        "validate_runtime_inventory",
        lambda *_args, **_kwargs: runtime_inventory,
    )

    def runner(*_args: object, **_kwargs: object) -> bytes:
        nonlocal commands
        commands += 1
        return b'{"installed":"exact"}'

    with pytest.raises(
        stage0.TrustedSignerStage0Error,
        match="trusted_signer_stage0_host_runtime_receipt_mismatch",
    ):
        stage0.verify_host_offline_runtime(
            revision,
            expected_receipt=expected,
            command_runner=runner,
            release_base=release_base,
            sudoers_path=sudoers_path,
            current_link=tmp_path / "current",
            activation_seal=tmp_path / "activation",
        )
    assert commands == 1


def test_verify_host_offline_runtime_rejects_activation_marker_race(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    revision = "e" * 40
    package_sha256 = "2" * 64
    release_base = tmp_path / "releases"
    release = release_base / revision
    current = tmp_path / "current"
    activation = tmp_path / "activation"
    sudoers_path = tmp_path / "sudoers"
    release_evidence = {
        "path": str(release),
        "uid": 0,
        "gid": 0,
        "mode": "0555",
        "projection_sha256": "3" * 64,
        "projection_count": 41,
    }
    sudoers_evidence = {
        "path": str(sudoers_path),
        "uid": 0,
        "gid": 0,
        "mode": "0440",
        "sha256": "4" * 64,
    }
    runtime_inventory = {"installed": "exact"}
    expected = _runtime_receipt(
        revision=revision,
        package_sha256=package_sha256,
        release_evidence=release_evidence,
        sudoers_evidence=sudoers_evidence,
        runtime_inventory=runtime_inventory,
    )

    monkeypatch.setattr(stage0.os, "geteuid", lambda: 0)
    monkeypatch.setattr(stage0, "HOST_RELEASE_BASE", release_base)
    monkeypatch.setattr(stage0, "HOST_SUDOERS_PATH", sudoers_path)
    monkeypatch.setattr(stage0, "HOST_CURRENT_LINK", current)
    monkeypatch.setattr(stage0, "HOST_ACTIVATION_SEAL", activation)
    monkeypatch.setattr(
        stage0,
        "_load_host_release_package",
        lambda *_args, **_kwargs: {"package_sha256": package_sha256},
    )
    monkeypatch.setattr(
        stage0,
        "_verify_sealed_host_release",
        lambda _selected: release_evidence,
    )
    monkeypatch.setattr(
        stage0,
        "_verify_host_sudoers",
        lambda *_args, **_kwargs: sudoers_evidence,
    )
    monkeypatch.setattr(
        stage0.stage0,
        "validate_runtime_inventory",
        lambda *_args, **_kwargs: runtime_inventory,
    )

    def race(*_args: object, **_kwargs: object) -> bytes:
        activation.write_bytes(b"concurrent activation")
        return b"{}"

    with pytest.raises(
        stage0.TrustedSignerStage0Error,
        match="trusted_signer_stage0_activation_forbidden",
    ):
        stage0.verify_host_offline_runtime(
            revision,
            expected_receipt=expected,
            command_runner=race,
            release_base=release_base,
            sudoers_path=sudoers_path,
            current_link=current,
            activation_seal=activation,
        )
