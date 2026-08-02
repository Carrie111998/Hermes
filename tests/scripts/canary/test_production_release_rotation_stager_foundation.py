from __future__ import annotations

import hashlib
import json
import os
import re
import shlex
import stat
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Mapping, Sequence

import pytest

from scripts.canary import production_release_builder_phase as phase
from scripts.canary import production_release_rotation_stager_input_author as author
from scripts.canary import production_release_rotation_stager_installer as installer
from scripts.canary import production_release_rotation_stager_launcher as launcher
from scripts.canary import (
    production_successor_rebind_owner_runtime_launcher as foundation_launcher,
)


ROOT = Path(__file__).parents[3]
REMOTE_URL = "https://github.com/lomliev/hermes-agent.git"


def _run(*argv: str, cwd: Path) -> str:
    completed = subprocess.run(
        argv,
        cwd=cwd,
        check=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        env={
            **os.environ,
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_CONFIG_GLOBAL": "/dev/null",
        },
    )
    return completed.stdout.strip()


def _commit_source(tmp_path: Path, files: Mapping[str, bytes]) -> tuple[Path, str]:
    source = tmp_path / "source"
    source.mkdir()
    _run("git", "init", "-q", cwd=source)
    _run("git", "config", "user.name", "test", cwd=source)
    _run("git", "config", "user.email", "test@example.invalid", cwd=source)
    _run("git", "remote", "add", "fork", REMOTE_URL, cwd=source)
    for relative, raw in files.items():
        selected = source / relative
        selected.parent.mkdir(parents=True, exist_ok=True)
        selected.write_bytes(raw)
    _run("git", "add", ".", cwd=source)
    _run("git", "commit", "-qm", "fixture", cwd=source)
    return source, _run("git", "rev-parse", "HEAD", cwd=source)


def _self_hash(value: Mapping[str, Any], field: str) -> Mapping[str, Any]:
    return {
        **value,
        field: author.sha256_bytes(author.canonical_bytes(value)),
    }


def _author_fixture(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> tuple[dict[str, Any], Mapping[str, Any]]:
    source, revision = _commit_source(
        tmp_path,
        {
            phase.UNIT_INPUT_ROTATION_STAGER_ENTRYPOINT_RELATIVE_PATH: (
                b"#!/usr/bin/env python3\nVALUE = 1\n"
            ),
            "README.md": b"exact source\n",
        },
    )
    wheelhouse = tmp_path / "wheelhouse"
    wheelhouse.mkdir()
    wheel = wheelhouse / "runtime-1.0-py3-none-any.whl"
    wheel.write_bytes(b"already verified exact wheel")
    wheel.chmod(0o444)
    unsigned = {
        "schema": author.WHEELHOUSE_SCHEMA,
        "release_revision": revision,
        "target": author.TARGET,
        "complete_transitive_closure": True,
        "network_required": False,
        "source_build_allowed": False,
        "installation": dict(phase._INSTALLATION),
        "wheels": [
            {
                "filename": wheel.name,
                "sha256": hashlib.sha256(wheel.read_bytes()).hexdigest(),
                "size": wheel.stat().st_size,
            }
        ],
        "verification_receipt_sha256": "9" * 64,
        "secret_material_recorded": False,
        "secret_digest_recorded": False,
    }
    manifest = _self_hash(unsigned, "manifest_sha256")
    manifest_path = tmp_path / "wheelhouse.json"
    manifest_path.write_bytes(author.canonical_bytes(manifest) + b"\n")
    manifest_path.chmod(0o444)
    uv = tmp_path / "uv"
    uv.write_bytes(b"exact uv")
    uv.chmod(0o555)
    python = tmp_path / "python3.11"
    python.write_bytes(b"exact python")
    python.chmod(0o555)
    monkeypatch.setattr(
        phase,
        "_PYTHON_PATH",
        re.compile(f"^{re.escape(str(python))}$"),
    )
    values = {
        "source_root": source,
        "source_remote": "fork",
        "repository_url": REMOTE_URL,
        "release_revision": revision,
        "wheelhouse_root": wheelhouse,
        "wheelhouse_manifest_path": manifest_path,
        "uv_path": uv,
        "expected_uv_sha256": hashlib.sha256(uv.read_bytes()).hexdigest(),
        "python_executable_path": python,
        "expected_python_sha256": hashlib.sha256(python.read_bytes()).hexdigest(),
        "roots": author.AuthorRoots(tmp_path / "jobs"),
        "production": False,
        "authority_uid": os.geteuid(),
        "authority_gid": os.getegid(),
        "builder_uid": os.geteuid(),
        "builder_gid": os.getegid(),
    }
    return values, manifest


def test_author_publishes_exact_immutable_input_and_empty_output(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    values, manifest = _author_fixture(tmp_path, monkeypatch)
    receipt = author._author_for_test(**values)
    input_root = Path(receipt["input_root"])
    output_root = Path(receipt["output_root"])

    assert receipt["wheelhouse_manifest_sha256"] == manifest["manifest_sha256"]
    assert receipt["output_empty"] is True
    assert tuple(output_root.iterdir()) == ()
    assert (input_root.stat().st_mode & 0o777) == 0o555
    assert tuple(sorted(item.name for item in input_root.iterdir())) == tuple(
        sorted(phase._INPUT_ROOT_NAMES)
    )
    request = json.loads((input_root / phase.REQUEST_NAME).read_text("ascii"))
    assert request["schema"] == phase.UNIT_INPUT_ROTATION_STAGER_REQUEST_SCHEMA
    assert request["purpose"] == phase.UNIT_INPUT_ROTATION_STAGER_PURPOSE
    assert request["entrypoint_relative_path"] == (
        phase.UNIT_INPUT_ROTATION_STAGER_ENTRYPOINT_RELATIVE_PATH
    )
    assert all(
        receipt[name] is False
        for name in (
            "builder_started",
            "candidate_promoted",
            "activation_performed",
            "release_pointer_mutated",
            "gateway_mutated",
            "data_mutated",
            "credentials_mutated",
            "network_access_performed",
        )
    )


def test_author_is_create_only_and_wheel_tamper_fails_closed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    values, _manifest = _author_fixture(tmp_path, monkeypatch)
    author._author_for_test(**values)
    with pytest.raises(
        author.RotationStagerInputAuthorError,
        match="rotation_stager_input_publication_conflict",
    ):
        author._author_for_test(**values)

    second = tmp_path / "second"
    second.mkdir()
    values, _manifest = _author_fixture(second, monkeypatch)
    wheel = next(Path(values["wheelhouse_root"]).iterdir())
    wheel.chmod(0o644)
    wheel.write_bytes(b"changed")
    wheel.chmod(0o444)
    with pytest.raises(
        author.RotationStagerInputAuthorError,
        match="rotation_stager_input_wheelhouse_invalid",
    ):
        author._author_for_test(**values)


def test_author_rejects_symlinked_job_parent_and_git_replacements(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    values, _manifest = _author_fixture(tmp_path, monkeypatch)
    real_jobs = tmp_path / "real-jobs"
    real_jobs.mkdir()
    linked_jobs = tmp_path / "linked-jobs"
    linked_jobs.symlink_to(real_jobs, target_is_directory=True)
    values["roots"] = author.AuthorRoots(linked_jobs)
    with pytest.raises(
        author.RotationStagerInputAuthorError,
        match="rotation_stager_input_job_parent_invalid",
    ):
        author._author_for_test(**values)
    assert author._git_environment()["GIT_NO_REPLACE_OBJECTS"] == "1"
    assert installer._git_environment()["GIT_NO_REPLACE_OBJECTS"] == "1"


def test_privileged_git_reads_trust_only_the_selected_checkout() -> None:
    source = Path("/opt/adventico-ai-platform/hermes-agent-release")
    expected = (
        "/usr/bin/git",
        "-c",
        f"safe.directory={source}",
        "-C",
        str(source),
        "cat-file",
        "--batch",
    )

    assert author._git_command(source, "cat-file", "--batch") == expected
    assert installer._git_command(source, "cat-file", "--batch") == expected


def test_root_pinned_python_accepts_standard_root_owned_0755(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    selected = tmp_path / "python3.11"
    raw = b"exact system python bytes"
    selected.write_bytes(raw)
    selected.chmod(0o755)
    real_lstat = author.os.lstat

    def root_owned_lstat(path: os.PathLike[str] | str) -> SimpleNamespace:
        state = real_lstat(path)
        return SimpleNamespace(
            st_mode=stat.S_IFREG | stat.S_IMODE(state.st_mode),
            st_uid=0,
            st_gid=0,
            st_nlink=1,
            st_size=state.st_size,
        )

    monkeypatch.setattr(author.os, "lstat", root_owned_lstat)

    digest, size = author._root_regular(
        selected,
        executable=True,
        expected_sha256=hashlib.sha256(raw).hexdigest(),
    )

    assert digest == hashlib.sha256(raw).hexdigest()
    assert size == len(raw)


def _installer_source(tmp_path: Path) -> tuple[Path, str]:
    source_paths = (
        set(installer._SOURCE_ASSETS)
        | set(installer._REVISION_STATIC_ASSETS)
        | set(installer._LATCHED_REVISION_STATIC_ASSETS)
        | set(installer._SUCCESSOR_REBIND_STATIC_ASSETS)
        | set(installer._SUCCESSOR_RUNTIME_CONTROLLER_ASSETS)
    )
    files = {relative: (ROOT / relative).read_bytes() for relative in source_paths}
    files["scripts/canary/production_cutover_unit_input_rotation.py"] = (
        ROOT / "scripts/canary/production_cutover_unit_input_rotation.py"
    ).read_bytes()
    files["scripts/canary/upstream_sync_rail_successor_rebind.py"] = (
        ROOT / "scripts/canary/upstream_sync_rail_successor_rebind.py"
    ).read_bytes()
    return _commit_source(tmp_path, files)


def _installer_roots(tmp_path: Path) -> installer.InstallerRoots:
    return installer.InstallerRoots(
        library=tmp_path / "usr/lib/muncho-release-updater",
        sysusers=tmp_path / "usr/lib/sysusers.d",
        tmpfiles=tmp_path / "usr/lib/tmpfiles.d",
        systemd=tmp_path / "etc/systemd/system",
        libexec=tmp_path / "usr/libexec",
        job_root=tmp_path / "var/lib/muncho-release-updates",
        promotion_lock=(tmp_path / "run/lock/muncho-release-builder-promotion.lock"),
        library_releases=(tmp_path / "usr/lib/muncho-release-updater-releases"),
    )


def _fake_foundation_command(
    roots: installer.InstallerRoots,
    calls: list[tuple[str, ...]],
    argv: Sequence[str],
) -> None:
    call = tuple(argv)
    calls.append(call)
    if Path(call[0]).name == "systemd-tmpfiles":
        roots.job_root.mkdir(parents=True, exist_ok=True)
        roots.job_root.chmod(0o755)
        roots.promotion_lock.parent.mkdir(parents=True, exist_ok=True)
        if not roots.promotion_lock.exists():
            roots.promotion_lock.write_bytes(b"")
        roots.promotion_lock.chmod(0o440)


def _validate_test_foundation(roots: installer.InstallerRoots) -> None:
    installer._validate_foundation(
        roots,
        root_uid=os.geteuid(),
        root_gid=os.getegid(),
        builder_gid=os.getegid(),
    )


def test_installer_is_exact_idempotent_and_never_activates(
    tmp_path: Path,
) -> None:
    source, revision = _installer_source(tmp_path)
    roots = _installer_roots(tmp_path)
    calls: list[tuple[str, ...]] = []

    first = installer._install_for_test(
        source_root=source,
        source_remote="fork",
        repository_url=REMOTE_URL,
        release_revision=revision,
        roots=roots,
        production=False,
        command_runner=lambda argv: _fake_foundation_command(roots, calls, argv),
        identity_validator=lambda: None,
        foundation_validator=_validate_test_foundation,
    )
    second = installer._install_for_test(
        source_root=source,
        source_remote="fork",
        repository_url=REMOTE_URL,
        release_revision=revision,
        roots=roots,
        production=False,
        command_runner=lambda argv: _fake_foundation_command(roots, calls, argv),
        identity_validator=lambda: None,
        foundation_validator=_validate_test_foundation,
    )

    assert first["created_asset_count"] == len(installer._SOURCE_ASSETS)
    assert second["created_asset_count"] == 0
    assert [Path(call[0]).name for call in calls] == [
        "systemd-sysusers",
        "systemd-tmpfiles",
        "systemd-sysusers",
        "systemd-tmpfiles",
    ]
    assert not any("systemctl" in item for call in calls for item in call)
    assert all(
        first[name] is False
        for name in (
            "systemd_daemon_reload_performed",
            "unit_enabled",
            "unit_started",
            "unit_scheduled",
            "activation_performed",
            "release_pointer_mutated",
            "gateway_mutated",
            "data_mutated",
            "credentials_mutated",
        )
    )
    assert (roots.library.stat().st_mode & 0o777) == 0o555
    assert first["job_root"] == str(roots.job_root)
    assert first["job_root_mode"] == "0755"
    assert (roots.job_root.stat().st_mode & 0o777) == 0o755
    assert (
        roots.libexec / "muncho-release-candidate-promoter"
    ).stat().st_mode & 0o777 == 0o555


def test_installer_rejects_divergent_existing_asset(tmp_path: Path) -> None:
    source, revision = _installer_source(tmp_path)
    roots = _installer_roots(tmp_path)
    kwargs = {
        "source_root": source,
        "source_remote": "fork",
        "repository_url": REMOTE_URL,
        "release_revision": revision,
        "roots": roots,
        "production": False,
        "command_runner": lambda argv: _fake_foundation_command(roots, [], argv),
        "identity_validator": lambda: None,
        "foundation_validator": _validate_test_foundation,
    }
    installer._install_for_test(**kwargs)
    target = roots.libexec / "muncho-release-candidate-promoter"
    target.chmod(0o755)
    target.write_bytes(b"divergent")
    target.chmod(0o555)
    with pytest.raises(
        installer.RotationStagerInstallerError,
        match="rotation_stager_installer_target_conflict",
    ):
        installer._install_for_test(**kwargs)


def test_revision_qualified_foundation_is_create_only_and_inert(
    tmp_path: Path,
) -> None:
    source, revision = _installer_source(tmp_path)
    roots = _installer_roots(tmp_path)
    calls: list[tuple[str, ...]] = []
    kwargs = {
        "source_root": source,
        "source_remote": "fork",
        "repository_url": REMOTE_URL,
        "release_revision": revision,
        "roots": roots,
        "production": False,
        "command_runner": lambda argv: _fake_foundation_command(roots, calls, argv),
        "identity_validator": lambda: None,
        "foundation_validator": _validate_test_foundation,
        "revision_qualified": True,
    }

    first = installer._install_for_test(**kwargs)
    second = installer._install_for_test(**kwargs)

    library = roots.library_releases / revision
    expected_assets = len(installer._REVISION_LIBRARY_ASSETS) + len(
        installer._REVISION_STATIC_ASSETS
    )
    assert first["schema"] == installer.REVISION_QUALIFIED_INSTALL_RECEIPT_SCHEMA
    assert first["foundation_layout"] == "revision-qualified-v2"
    assert (
        first["foundation_asset_manifest_sha256"]
        == second["foundation_asset_manifest_sha256"]
    )
    assert first["created_asset_count"] == expected_assets
    assert second["created_asset_count"] == 0
    assert (roots.library_releases.stat().st_mode & 0o777) == 0o755
    assert (library.stat().st_mode & 0o777) == 0o555
    assert (library / "scripts/canary/production_release_builder_phase.py").is_file()
    assert (
        roots.systemd / "muncho-release-builder-v2@.service"
    ).stat().st_mode & 0o777 == 0o444
    assert (
        roots.libexec / "muncho-release-foundation-exec-v2"
    ).stat().st_mode & 0o777 == 0o555
    assert not any("systemctl" in item for call in calls for item in call)
    assert first["systemd_daemon_reload_performed"] is False
    assert first["unit_started"] is False
    assert first["activation_performed"] is False


def test_latched_revision_foundation_is_create_only_and_inert(
    tmp_path: Path,
) -> None:
    source, revision = _installer_source(tmp_path)
    roots = _installer_roots(tmp_path)
    calls: list[tuple[str, ...]] = []
    kwargs = {
        "source_root": source,
        "source_remote": "fork",
        "repository_url": REMOTE_URL,
        "release_revision": revision,
        "roots": roots,
        "production": False,
        "command_runner": lambda argv: _fake_foundation_command(roots, calls, argv),
        "identity_validator": lambda: None,
        "foundation_validator": _validate_test_foundation,
        "revision_qualified_v3": True,
    }

    first = installer._install_for_test(**kwargs)
    second = installer._install_for_test(**kwargs)

    expected_assets = len(installer._REVISION_LIBRARY_ASSETS) + len(
        installer._LATCHED_REVISION_STATIC_ASSETS
    )
    assert (
        first["schema"] == installer.LATCHED_REVISION_QUALIFIED_INSTALL_RECEIPT_SCHEMA
    )
    assert first["foundation_layout"] == "latched-revision-qualified-v3"
    assert first["created_asset_count"] == expected_assets
    assert second["created_asset_count"] == 0
    assert (
        roots.systemd / "muncho-release-builder-v3@.service"
    ).stat().st_mode & 0o777 == 0o444
    unit = (roots.systemd / "muncho-release-builder-v3@.service").read_text(
        encoding="utf-8"
    )
    assert "RemainAfterExit=yes" in unit
    assert (
        roots.libexec / "muncho-release-foundation-exec-v3"
    ).stat().st_mode & 0o777 == 0o555
    assert not any("systemctl" in item for call in calls for item in call)
    assert first["systemd_daemon_reload_performed"] is False
    assert first["unit_started"] is False


def test_successor_rebind_v4_foundation_is_create_only_and_inert(
    tmp_path: Path,
) -> None:
    source, revision = _installer_source(tmp_path)
    roots = _installer_roots(tmp_path)
    calls: list[tuple[str, ...]] = []
    kwargs = {
        "source_root": source,
        "source_remote": "fork",
        "repository_url": REMOTE_URL,
        "release_revision": revision,
        "roots": roots,
        "production": False,
        "command_runner": lambda argv: _fake_foundation_command(roots, calls, argv),
        "identity_validator": lambda: None,
        "foundation_validator": _validate_test_foundation,
        "revision_qualified_v4": True,
    }

    first = installer._install_for_test(**kwargs)
    second = installer._install_for_test(**kwargs)

    expected_assets = (
        len(installer._REVISION_LIBRARY_ASSETS)
        + len(installer._SUCCESSOR_REBIND_STATIC_ASSETS)
        + len(installer._SUCCESSOR_RUNTIME_CONTROLLER_ASSETS)
        + 1
    )
    assert (
        first["schema"] == installer.SUCCESSOR_REBIND_FOUNDATION_INSTALL_RECEIPT_SCHEMA
    )
    assert first["foundation_layout"] == ("successor-rebind-revision-qualified-v4")
    assert first["created_asset_count"] == expected_assets
    assert second["created_asset_count"] == 0
    library = roots.library_releases / revision
    source_snapshot = library / "source"
    controller = library / "controller"
    assert first["successor_runtime_source_snapshot_created"] is True
    assert second["successor_runtime_source_snapshot_created"] is False
    assert _run("git", "rev-parse", "HEAD", cwd=source_snapshot) == revision
    assert _run("git", "status", "--porcelain=v1", cwd=source_snapshot) == ""
    manifest_raw = (
        controller / installer.SUCCESSOR_RUNTIME_CONTROLLER_MANIFEST_NAME
    ).read_bytes()
    assert (
        hashlib.sha256(manifest_raw).hexdigest()
        == first["successor_runtime_controller_manifest_file_sha256"]
    )
    assert stat.S_IMODE(controller.stat().st_mode) == 0o555
    wrapper = roots.libexec / "muncho-release-foundation-exec-v4"
    assert stat.S_IMODE(wrapper.stat().st_mode) == 0o555
    assert not any("systemctl" in item for call in calls for item in call)
    assert first["systemd_daemon_reload_performed"] is False
    assert first["unit_enabled"] is False
    assert first["unit_started"] is False
    assert first["unit_scheduled"] is False
    assert first["activation_performed"] is False


@pytest.mark.parametrize(
    "relative",
    (
        "ops/muncho/release-updater/muncho-release-foundation-exec-v4",
        "ops/muncho/release-updater/muncho-successor-runtime-foundation-exec",
        "scripts/canary/production_successor_rebind_owner_runtime_preexec.py",
        "scripts/canary/production_successor_rebind_owner_runtime_launcher.py",
    ),
)
def test_successor_rebind_v4_installer_rejects_stale_bound_digests(
    tmp_path: Path,
    relative: str,
) -> None:
    source, _revision = _installer_source(tmp_path)
    selected = source / relative
    selected.write_bytes(selected.read_bytes() + b"\n# stale digest fixture\n")
    _run("git", "add", relative, cwd=source)
    _run("git", "commit", "-qm", "stale bound digest", cwd=source)
    revision = _run("git", "rev-parse", "HEAD", cwd=source)
    roots = _installer_roots(tmp_path)

    with pytest.raises(
        installer.RotationStagerInstallerError,
        match="asset_binding_invalid",
    ):
        installer._install_for_test(
            source_root=source,
            source_remote="fork",
            repository_url=REMOTE_URL,
            release_revision=revision,
            roots=roots,
            production=False,
            command_runner=lambda _argv: None,
            identity_validator=lambda: None,
            foundation_validator=lambda _roots: None,
            revision_qualified_v4=True,
        )


def test_successor_source_snapshot_recovers_exact_incomplete_after_crash(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source, revision = _installer_source(tmp_path)
    roots = _installer_roots(tmp_path)
    calls: list[tuple[str, ...]] = []
    kwargs = {
        "source_root": source,
        "source_remote": "fork",
        "repository_url": REMOTE_URL,
        "release_revision": revision,
        "roots": roots,
        "production": False,
        "command_runner": lambda argv: _fake_foundation_command(roots, calls, argv),
        "identity_validator": lambda: None,
        "foundation_validator": _validate_test_foundation,
        "revision_qualified_v4": True,
    }
    real_rename = installer._rename_directory_noreplace  # noqa: SLF001
    crashed = False

    def crash_before_rename(source_path: Path, destination_path: Path) -> None:
        nonlocal crashed
        crashed = True
        assert source_path.name.endswith(".incomplete")
        assert destination_path.name == "source"
        raise KeyboardInterrupt

    monkeypatch.setattr(installer, "_rename_directory_noreplace", crash_before_rename)
    with pytest.raises(KeyboardInterrupt):
        installer._install_for_test(**kwargs)

    incomplete_entries = list(
        (roots.library_releases / revision).glob(".source.*.incomplete")
    )
    assert len(incomplete_entries) == 1
    incomplete = incomplete_entries[0]
    assert crashed is True
    assert incomplete.is_dir()
    assert not (roots.library_releases / revision / "source").exists()

    monkeypatch.setattr(installer, "_rename_directory_noreplace", real_rename)
    receipt = installer._install_for_test(**kwargs)

    assert receipt["successor_runtime_source_snapshot_created"] is True
    assert not incomplete.exists()
    snapshot = roots.library_releases / revision / "source"
    assert _run("git", "rev-parse", "HEAD", cwd=snapshot) == revision


def test_successor_rebind_v4_wrapper_has_only_one_fixed_action() -> None:
    raw = (
        ROOT / "ops/muncho/release-updater/muncho-release-foundation-exec-v4"
    ).read_text(encoding="utf-8")
    verifier = (
        ROOT / "scripts/canary/production_successor_rebind_owner_runtime_preexec.py"
    ).read_text(encoding="utf-8")

    assert "successor-rebind-owner-apply" in raw
    assert "upstream-sync-successor-owner-apply" in verifier
    assert "owner-apply-fixed" not in raw
    assert 'if [ "$#" -ne 9 ]' in raw
    assert 'if [ "$operation" != successor-rebind-owner-apply ]' in raw
    assert "launch_authority_sha256=$9" in raw
    assert '"$launch_authority_sha256"' in raw
    assert '"$launch_authority_sha256"' in raw.rsplit(
        "exec /usr/bin/python3 -I -S -B", 1
    )[1]
    assert "runtime_base=/usr/lib/muncho-successor-rebind-runtime" in raw
    assert "production_successor_rebind_owner_runtime_preexec" in raw
    assert "exec /usr/bin/python3 -I -S -B" in raw
    assert 'runpy.run_path(path,run_name="__main__")' in raw
    assert "runpy.run_module" not in raw
    assert "sys.path.insert" not in raw
    assert "expected_preexec_sha256" in raw
    assert "hermes-agent-releases" not in raw
    assert '"$@"' not in raw
    assert "systemctl" not in raw


def test_successor_runtime_wrapper_and_launcher_are_closed_before_import() -> None:
    wrapper = (
        ROOT / "ops/muncho/release-updater/muncho-successor-runtime-foundation-exec"
    ).read_text(encoding="utf-8")
    launcher = (
        ROOT / "scripts/canary/production_successor_rebind_owner_runtime_launcher.py"
    ).read_text(encoding="utf-8")

    assert (
        "fixed_wrapper=/usr/libexec/muncho-successor-runtime-foundation-exec" in wrapper
    )
    assert "library_base=/usr/lib/muncho-release-updater-releases" in wrapper
    assert "builder_uid=29104" in wrapper
    assert "builder_gid=29104" in wrapper
    assert wrapper.count("prepare-runtime)") == 1
    assert wrapper.count("build-runtime-as-dedicated-builder)") == 1
    assert wrapper.count("promote-runtime)") == 1
    assert '--reuid="$builder_uid"' in wrapper
    assert '--regid="$builder_gid"' in wrapper
    assert "--clear-groups" in wrapper
    assert "--inh-caps=-all" in wrapper
    assert "--ambient-caps=-all" in wrapper
    assert "--bounding-set=-all" in wrapper
    assert "--no-new-privs" in wrapper
    assert "/usr/bin/env -i" in wrapper
    assert "systemctl" not in wrapper
    assert "eval " not in wrapper
    dropped_identity = wrapper.index('--reuid="$builder_uid"')
    assert dropped_identity < wrapper.index(
        '/usr/bin/python3 -I -S -B "$launcher"',
        dropped_identity,
    )

    validation = launcher.index(
        "controller = _validate_controller(revision, controller_manifest_file_sha256)"
    )
    path_activation = launcher.index("sys.path.insert(0, str(controller))")
    target_import = launcher.index(
        "production_successor_rebind_owner_runtime as owner_runtime"
    )
    assert validation < path_activation < target_import
    assert "import subprocess" not in launcher
    assert "runpy.run_module" not in launcher


def _successor_controller_fixture(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> tuple[Path, str, str]:
    revision = "a" * 40
    library_base = tmp_path / "library"
    controller = library_base / revision / "controller"
    assets = {
        relative: (ROOT / relative).read_bytes()
        for relative in installer._SUCCESSOR_RUNTIME_CONTROLLER_ASSETS
    }
    for relative, (
        target_relative,
        mode,
    ) in installer._SUCCESSOR_RUNTIME_CONTROLLER_ASSETS.items():
        target = controller / target_relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(assets[relative])
        target.chmod(mode)
    manifest = installer.successor_runtime_controller_manifest_from_bytes(
        release_revision=revision,
        assets=assets,
    )
    manifest_path = controller / installer.SUCCESSOR_RUNTIME_CONTROLLER_MANIFEST_NAME
    manifest_path.write_bytes(installer._canonical(manifest) + b"\n")  # noqa: SLF001
    manifest_path.chmod(0o444)
    for directory in (
        controller / "gateway",
        controller / "scripts/canary",
        controller / "scripts",
        controller,
        controller.parent,
    ):
        directory.chmod(0o555)
    library_base.chmod(0o755)

    original_lstat = os.lstat
    original_fstat = os.fstat

    def root_owned(state: os.stat_result) -> SimpleNamespace:
        return SimpleNamespace(
            st_dev=state.st_dev,
            st_ino=state.st_ino,
            st_mode=state.st_mode,
            st_uid=0,
            st_gid=0,
            st_nlink=state.st_nlink,
            st_size=state.st_size,
            st_mtime_ns=state.st_mtime_ns,
            st_ctime_ns=state.st_ctime_ns,
        )

    monkeypatch.setattr(
        foundation_launcher.os,
        "lstat",
        lambda path: root_owned(original_lstat(path)),
    )
    monkeypatch.setattr(
        foundation_launcher.os,
        "fstat",
        lambda descriptor: root_owned(original_fstat(descriptor)),
    )
    monkeypatch.setattr(foundation_launcher, "LIBRARY_BASE", library_base)
    monkeypatch.setattr(
        foundation_launcher,
        "__file__",
        str(controller / foundation_launcher.ENTRY_RELATIVE),
    )
    return controller, revision, hashlib.sha256(manifest_path.read_bytes()).hexdigest()


@pytest.mark.parametrize("mutation", ("manifest", "extra", "symlink"))
def test_successor_controller_rejects_tamper_extra_and_symlink_before_activation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    mutation: str,
) -> None:
    controller, revision, manifest_sha256 = _successor_controller_fixture(
        tmp_path,
        monkeypatch,
    )
    manifest_path = controller / installer.SUCCESSOR_RUNTIME_CONTROLLER_MANIFEST_NAME
    if mutation == "manifest":
        manifest_path.chmod(0o644)
        manifest_path.write_bytes(manifest_path.read_bytes() + b" ")
        manifest_path.chmod(0o444)
    elif mutation == "extra":
        controller.chmod(0o755)
        extra = controller / "extra.py"
        extra.write_bytes(b"foreign\n")
        extra.chmod(0o444)
        controller.chmod(0o555)
    else:
        gateway = controller / "gateway"
        selected = gateway / "production_owner_runtime.py"
        gateway.chmod(0o755)
        selected.unlink()
        selected.symlink_to(controller / "scripts/__init__.py")
        gateway.chmod(0o555)

    original_sys_path = list(sys.path)
    with pytest.raises(
        foundation_launcher.SuccessorRuntimeFoundationLauncherError,
        match="successor_runtime_foundation_launcher_invalid",
    ):
        foundation_launcher.main(("prepare-runtime", revision, manifest_sha256))
    assert sys.path == original_sys_path


def test_sealed_controller_launcher_reaches_owner_runtime_in_isolated_subprocess(
    tmp_path: Path,
) -> None:
    revision = "b" * 40
    library_base = tmp_path / "library"
    controller = library_base / revision / "controller"
    launcher_relative = (
        "scripts/canary/production_successor_rebind_owner_runtime_launcher.py"
    )
    launcher_raw = (ROOT / launcher_relative).read_text(encoding="utf-8")
    launcher_raw = launcher_raw.replace(
        'LIBRARY_BASE = Path("/usr/lib/muncho-release-updater-releases")',
        f"LIBRARY_BASE = Path({str(library_base)!r})",
    )
    launcher_raw = launcher_raw.replace(
        "before.st_uid != 0",
        f"before.st_uid != {os.geteuid()}",
    ).replace(
        "before.st_gid != 0",
        f"before.st_gid != {os.getegid()}",
    )
    launcher_raw = launcher_raw.replace(
        "item.st_uid != 0",
        f"item.st_uid != {os.geteuid()}",
    ).replace(
        "item.st_gid != 0",
        f"item.st_gid != {os.getegid()}",
    )
    assets = {
        relative: (ROOT / relative).read_bytes()
        for relative in installer._SUCCESSOR_RUNTIME_CONTROLLER_ASSETS
    }
    assets[launcher_relative] = launcher_raw.encode("utf-8")
    for relative, (
        target_relative,
        mode,
    ) in installer._SUCCESSOR_RUNTIME_CONTROLLER_ASSETS.items():
        target = controller / target_relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(assets[relative])
        target.chmod(mode)
    manifest = installer.successor_runtime_controller_manifest_from_bytes(
        release_revision=revision,
        assets=assets,
    )
    manifest_path = controller / installer.SUCCESSOR_RUNTIME_CONTROLLER_MANIFEST_NAME
    manifest_path.write_bytes(installer._canonical(manifest) + b"\n")  # noqa: SLF001
    manifest_path.chmod(0o444)
    for directory in (
        controller / "gateway",
        controller / "scripts/canary",
        controller / "scripts",
        controller,
        controller.parent,
    ):
        directory.chmod(0o555)
    library_base.chmod(0o755)

    completed = subprocess.run(
        (
            sys.executable,
            "-I",
            "-S",
            "-B",
            str(controller / launcher_relative),
            "prepare-runtime",
            revision,
            hashlib.sha256(manifest_path.read_bytes()).hexdigest(),
        ),
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
        timeout=30,
        env={"PATH": "/usr/bin:/bin", "HOME": "/nonexistent", "LC_ALL": "C"},
    )

    assert completed.returncode == 2
    assert completed.stdout == b""
    assert completed.stderr == (
        b'{"error_code":"successor_rebind_owner_runtime_foundation_failed",'
        b'"ok":false}\n'
    )
    assert b"successor_runtime_foundation_launcher_failed" not in completed.stderr


def test_foundation_wrapper_subprocess_orders_fixed_identity_drop_before_builder(
    tmp_path: Path,
) -> None:
    tools = tmp_path / "tools"
    tools.mkdir()
    log = tmp_path / "calls.log"
    wrapper_path = tmp_path / "muncho-successor-runtime-foundation-exec"
    library_base = tmp_path / "library"
    launcher_path = library_base / ("c" * 40) / "controller/launcher.py"
    launcher_path.parent.mkdir(parents=True)
    launcher_path.write_bytes(b"sealed launcher\n")
    launcher_path.chmod(0o444)

    def executable(name: str, raw: str) -> Path:
        path = tools / name
        path.write_text(raw, encoding="utf-8")
        path.chmod(0o555)
        return path

    fake_id = executable(
        "id",
        '#!/bin/sh\ncase "$1" in -u|-g) echo 0;; *) exit 2;; esac\n',
    )
    fake_stat = executable(
        "stat",
        "#!/bin/sh\n"
        "for item do last=$item; done\n"
        f'case "$last" in {shlex.quote(str(wrapper_path))}) echo 0:0:555:1;; '
        f"{shlex.quote(str(launcher_path))}) echo 0:0:444:1;; "
        f"{shlex.quote(str(library_base))}) echo 0:0:755;; *) echo 0:0:555;; esac\n",
    )
    fake_python = executable(
        "python3",
        "#!/bin/sh\n"
        f"printf 'python' >> {shlex.quote(str(log))}\n"
        f"for item do printf ' <%s>' \"$item\" >> {shlex.quote(str(log))}; done\n"
        f"printf '\\n' >> {shlex.quote(str(log))}\n"
        f"payload=$(cat); printf 'stdin=<%s>\\n' \"$payload\" >> {shlex.quote(str(log))}\n"
        "printf '{\"ok\":true}\\n'\n",
    )
    fake_env = executable(
        "env",
        "#!/bin/sh\n"
        f"printf 'env' >> {shlex.quote(str(log))}\n"
        f"for item do printf ' <%s>' \"$item\" >> {shlex.quote(str(log))}; done\n"
        f"printf '\\n' >> {shlex.quote(str(log))}\n"
        '[ "$1" = -i ] && shift\n'
        'while [ "${1#*=}" != "$1" ]; do shift; done\n'
        'exec "$@"\n',
    )
    fake_setpriv = executable(
        "setpriv",
        "#!/bin/sh\n"
        f"printf 'setpriv' >> {shlex.quote(str(log))}\n"
        f"for item do printf ' <%s>' \"$item\" >> {shlex.quote(str(log))}; done\n"
        f"printf '\\n' >> {shlex.quote(str(log))}\n"
        'while [ "$1" != -- ]; do shift; done\n'
        'shift\nexec "$@"\n',
    )

    wrapper = (
        ROOT / "ops/muncho/release-updater/muncho-successor-runtime-foundation-exec"
    ).read_text(encoding="utf-8")
    replacements = {
        "/usr/libexec/muncho-successor-runtime-foundation-exec": str(wrapper_path),
        "/usr/lib/muncho-release-updater-releases": str(library_base),
        "/usr/bin/id": str(fake_id),
        "/usr/bin/stat": str(fake_stat),
        "/usr/bin/python3": str(fake_python),
        "/usr/bin/setpriv": str(fake_setpriv),
        "/usr/bin/env": str(fake_env),
        "/usr/bin/sha256sum": str(tools / "sha256sum"),
    }
    for old, new in replacements.items():
        wrapper = wrapper.replace(old, new)
    wrapper = wrapper.replace(
        'launcher="$controller/scripts/canary/'
        'production_successor_rebind_owner_runtime_launcher.py"',
        f"launcher={shlex.quote(str(launcher_path))}",
    )
    wrapper_path.write_text(wrapper, encoding="utf-8")
    wrapper_path.chmod(0o555)
    wrapper_sha256 = hashlib.sha256(wrapper_path.read_bytes()).hexdigest()
    launcher_sha256 = hashlib.sha256(launcher_path.read_bytes()).hexdigest()
    executable(
        "sha256sum",
        "#!/bin/sh\n"
        "for item do last=$item; done\n"
        f"case \"$last\" in {shlex.quote(str(wrapper_path))}) echo {wrapper_sha256}'  x';; "
        f"{shlex.quote(str(launcher_path))}) echo {launcher_sha256}'  x';; *) exit 2;; esac\n",
    )
    common = (
        "c" * 40,
        wrapper_sha256,
        launcher_sha256,
        "d" * 64,
    )

    prepare = subprocess.run(
        (str(wrapper_path), "prepare-runtime", *common),
        check=False,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        timeout=10,
    )
    build = subprocess.run(
        (
            str(wrapper_path),
            "build-runtime-as-dedicated-builder",
            *common,
            "e" * 40,
            "f" * 64,
        ),
        check=False,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        timeout=10,
    )
    promote_frame = b'{"frame":"exact"}\n'
    promote = subprocess.run(
        (str(wrapper_path), "promote-runtime", *common),
        input=promote_frame,
        check=False,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        timeout=10,
    )

    assert (prepare.returncode, build.returncode, promote.returncode) == (0, 0, 0)
    assert prepare.stdout == build.stdout == promote.stdout == b'{"ok":true}\n'
    assert prepare.stderr == build.stderr == promote.stderr == b""
    calls = log.read_text(encoding="utf-8")
    setpriv_line = next(
        line for line in calls.splitlines() if line.startswith("setpriv")
    )
    assert " <--reuid=29104>" in setpriv_line
    assert " <--regid=29104>" in setpriv_line
    assert " <--clear-groups>" in setpriv_line
    assert " <--inh-caps=-all>" in setpriv_line
    assert " <--ambient-caps=-all>" in setpriv_line
    assert " <--bounding-set=-all>" in setpriv_line
    assert " <--no-new-privs>" in setpriv_line
    assert setpriv_line.index("<--no-new-privs>") < setpriv_line.index(
        f"<{fake_python}>"
    )
    assert f"<{fake_env}> <-i>" in setpriv_line
    assert "stdin=<" + promote_frame.decode("ascii").strip() + ">" in calls


def test_successor_rebind_v4_preexec_does_not_import_package_initializers(
    tmp_path: Path,
) -> None:
    raw = (
        ROOT / "ops/muncho/release-updater/muncho-release-foundation-exec-v4"
    ).read_text(encoding="utf-8")
    expression_match = re.search(
        r"exec /usr/bin/python3 -I -S -B -c \\\n  '([^']+)' \\\n",
        raw,
    )
    assert expression_match is not None

    library = tmp_path / "target-revision"
    canary = library / "scripts/canary"
    canary.mkdir(parents=True)
    scripts_marker = tmp_path / "scripts-init-executed"
    canary_marker = tmp_path / "canary-init-executed"
    (library / "scripts/__init__.py").write_text(
        f"from pathlib import Path\nPath({str(scripts_marker)!r}).write_text('bad')\n",
        encoding="utf-8",
    )
    (canary / "__init__.py").write_text(
        f"from pathlib import Path\nPath({str(canary_marker)!r}).write_text('bad')\n",
        encoding="utf-8",
    )
    verifier = canary / "production_successor_rebind_owner_runtime_preexec.py"
    verifier.write_text(
        "import json,sys\nprint(json.dumps(sys.argv))\n",
        encoding="utf-8",
    )

    completed = subprocess.run(
        (
            sys.executable,
            "-I",
            "-S",
            "-B",
            "-c",
            expression_match.group(1),
            str(verifier),
            "muncho-successor-rebind-owner-runtime-preexec",
            "sentinel",
        ),
        check=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )

    assert json.loads(completed.stdout) == [
        str(verifier),
        "sentinel",
    ]
    assert not scripts_marker.exists()
    assert not canary_marker.exists()


def test_revision_qualified_foundation_keeps_two_revisions_side_by_side(
    tmp_path: Path,
) -> None:
    source, first_revision = _installer_source(tmp_path)
    roots = _installer_roots(tmp_path)
    calls: list[tuple[str, ...]] = []

    marker = source / "revision-marker.txt"
    marker.write_text("second exact revision\n", encoding="utf-8")
    _run("git", "add", "revision-marker.txt", cwd=source)
    _run("git", "commit", "-qm", "second fixture revision", cwd=source)
    second_revision = _run("git", "rev-parse", "HEAD", cwd=source)
    assert second_revision != first_revision

    common = {
        "source_root": source,
        "source_remote": "fork",
        "repository_url": REMOTE_URL,
        "roots": roots,
        "production": False,
        "command_runner": lambda argv: _fake_foundation_command(roots, calls, argv),
        "identity_validator": lambda: None,
        "foundation_validator": _validate_test_foundation,
        "revision_qualified": True,
    }
    first = installer._install_for_test(
        **common,
        release_revision=first_revision,
    )
    second = installer._install_for_test(
        **common,
        release_revision=second_revision,
    )

    first_library = roots.library_releases / first_revision
    second_library = roots.library_releases / second_revision
    assert first_library.is_dir()
    assert second_library.is_dir()
    assert first_library != second_library
    assert (
        first["foundation_asset_manifest_sha256"]
        != second["foundation_asset_manifest_sha256"]
    )
    for library in (first_library, second_library):
        assert (library.stat().st_mode & 0o777) == 0o555
        assert (
            library / "scripts/canary/production_release_builder_phase.py"
        ).is_file()


def test_revision_qualified_foundation_rejects_divergent_same_revision_asset(
    tmp_path: Path,
) -> None:
    source, revision = _installer_source(tmp_path)
    roots = _installer_roots(tmp_path)
    kwargs = {
        "source_root": source,
        "source_remote": "fork",
        "repository_url": REMOTE_URL,
        "release_revision": revision,
        "roots": roots,
        "production": False,
        "command_runner": lambda argv: _fake_foundation_command(roots, [], argv),
        "identity_validator": lambda: None,
        "foundation_validator": _validate_test_foundation,
        "revision_qualified": True,
    }
    installer._install_for_test(**kwargs)
    target = (
        roots.library_releases
        / revision
        / "scripts/canary/production_release_builder_phase.py"
    )
    target.chmod(0o644)
    target.write_bytes(b"divergent exact-revision bytes\n")
    target.chmod(0o444)

    with pytest.raises(
        installer.RotationStagerInstallerError,
        match="rotation_stager_installer_target_conflict",
    ):
        installer._install_for_test(**kwargs)


@pytest.mark.parametrize(
    "wrapper_name",
    (
        "muncho-release-foundation-exec-v2",
        "muncho-release-foundation-exec-v3",
    ),
)
def test_revision_foundation_wrapper_preserves_python_argv_contract(
    tmp_path: Path,
    wrapper_name: str,
) -> None:
    revision = "a" * 40
    library_base = tmp_path / "library-releases"
    module_root = library_base / revision / "scripts/canary"
    module_root.mkdir(parents=True)
    (module_root.parent / "__init__.py").write_text("", encoding="utf-8")
    (module_root / "__init__.py").write_text("", encoding="utf-8")
    (module_root / "production_release_rotation_stager_input_author.py").write_text(
        "import json,sys\nprint(json.dumps(sys.argv))\n",
        encoding="utf-8",
    )
    source = (ROOT / f"ops/muncho/release-updater/{wrapper_name}").read_text(
        encoding="utf-8"
    )
    source = source.replace(
        "/usr/lib/muncho-release-updater-releases",
        str(library_base),
    ).replace(
        "python=/usr/bin/python3",
        f"python={shlex.quote(sys.executable)}",
    )
    wrapper = tmp_path / "foundation-exec-v2"
    wrapper.write_text(source, encoding="utf-8")
    wrapper.chmod(0o755)

    completed = subprocess.run(
        (str(wrapper), revision, "input-author", "--sentinel"),
        check=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )

    assert json.loads(completed.stdout) == [
        "muncho-release-rotation-stager-input-author",
        "--sentinel",
    ]


def test_launcher_verifies_promoted_release_before_exact_exec(tmp_path: Path) -> None:
    revision = "a" * 40
    release = tmp_path / f"hermes-agent-{revision[:12]}"
    interpreter = release / ".venv/bin/python"
    interpreter.parent.mkdir(parents=True)
    interpreter.write_bytes(b"python")
    interpreter.chmod(0o555)
    release.chmod(0o555)
    verified: list[Path] = []
    executions: list[tuple[str, tuple[str, ...], Mapping[str, str]]] = []

    def verify(path: Path, **_kwargs: Any) -> Mapping[str, object]:
        verified.append(path)
        return {"completed": True}

    def execute(
        executable: str,
        argv: Sequence[str],
        environment: Mapping[str, str],
    ) -> None:
        executions.append((executable, tuple(argv), dict(environment)))

    with pytest.raises(
        launcher.RotationStagerLauncherError,
        match="rotation_stager_launcher_exec_returned",
    ):
        launcher._launch_for_test(
            revision=revision,
            action="prepare-release-unit-inputs",
            release_parent=tmp_path,
            production=False,
            effective_uid=0,
            expected_release_uid=os.geteuid(),
            expected_release_gid=os.getegid(),
            verifier=verify,
            purpose_validator=lambda *_args: None,
            execve=execute,
        )
    assert verified == [release]
    assert len(executions) == 1
    executable, argv, environment = executions[0]
    assert executable == str(interpreter)
    assert argv[-1] == "prepare-release-unit-inputs"
    assert argv[1:4] == ("-B", "-I", "-c")
    assert str(release) in argv[4]
    assert "PYTHONPATH" not in environment


def test_launcher_protocol_enum_matches_rotation_runtime() -> None:
    from scripts.canary import production_cutover_unit_input_rotation as rotation

    assert launcher._PHASE_ACTIONS == rotation.RELEASE_PHASE_ACTIONS
    assert launcher._ACTIONS == rotation.RELEASE_PHASE_ACTIONS | {
        "rotate-unit-input-authority"
    }


def test_launcher_legacy_rotation_invokes_module_without_phase_argv(
    tmp_path: Path,
) -> None:
    revision = "b" * 40
    release = tmp_path / f"hermes-agent-{revision[:12]}"
    interpreter = release / ".venv/bin/python"
    interpreter.parent.mkdir(parents=True)
    interpreter.write_bytes(b"python")
    interpreter.chmod(0o555)
    release.chmod(0o555)
    executions: list[tuple[str, ...]] = []
    with pytest.raises(
        launcher.RotationStagerLauncherError,
        match="rotation_stager_launcher_exec_returned",
    ):
        launcher._launch_for_test(
            revision=revision,
            action="rotate-unit-input-authority",
            release_parent=tmp_path,
            production=False,
            effective_uid=0,
            expected_release_uid=os.geteuid(),
            expected_release_gid=os.getegid(),
            verifier=lambda *_args, **_kwargs: {"completed": True},
            purpose_validator=lambda *_args: None,
            execve=lambda _executable, argv, _environment: executions.append(
                tuple(argv)
            ),
        )
    assert len(executions) == 1
    assert executions[0][-1] == "muncho-release-unit-input-rotation-stager"


def test_wrappers_are_fixed_and_have_no_service_activation_commands() -> None:
    for name in (
        "muncho-release-candidate-promoter",
        "muncho-release-rotation-stager-input-author",
        "muncho-release-unit-input-rotation-stager",
    ):
        raw = (ROOT / "ops/muncho/release-updater" / name).read_text("utf-8")
        assert "systemctl" not in raw
        assert " enable" not in raw
        assert " start" not in raw
        assert " restart" not in raw
        assert " daemon-reload" not in raw
