from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
from pathlib import Path
from typing import Any, Mapping, Sequence

import pytest

from scripts.canary import production_release_builder_phase as phase
from scripts.canary import production_release_rotation_stager_input_author as author
from scripts.canary import production_release_rotation_stager_installer as installer
from scripts.canary import production_release_rotation_stager_launcher as launcher


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


def _installer_source(tmp_path: Path) -> tuple[Path, str]:
    files = {
        relative: (ROOT / relative).read_bytes()
        for relative in installer._SOURCE_ASSETS
    }
    files["scripts/canary/production_cutover_unit_input_rotation.py"] = (
        ROOT / "scripts/canary/production_cutover_unit_input_rotation.py"
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
    assert argv[1:3] == ("-I", "-c")
    assert str(release) in argv[3]
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
