"""Behavioral contract for canary-first update + verified rollback (#44877)."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from hermes_cli.update_inventory import RuntimeRecord, UpdatePlan
from hermes_cli.update_rollout import (
    CheckpointError,
    RollbackError,
    RolloutConfig,
    RolloutError,
    RolloutExecutionError,
    _CANARY_PROVIDER_SMOKE_PREFIX,
    _CANARY_SMOKE_MODULES,
    _bounded_smoke_run,
    _profile_smoke,
    _provider_smoke_turn,
    _validate_windows_coordinator_paths,
    _verify_restored_interpreter,
    checkpoint_root,
    create_checkpoint,
    dependency_state_matches_checkpoint,
    load_rollout_config,
    plan_from_checkpoint,
    prune_checkpoints_after_commit,
    quiesce_profile_gateway,
    quiesce_restart_and_verify_fleet,
    quiesce_rollout_fleet,
    quiesce_rollout_fleet_for_update,
    read_checkpoint,
    restart_and_verify_fleet,
    restart_profile_gateway,
    resolve_checkpoint,
    restore_and_verify_fleet,
    restore_checkpoint,
    run_canary_rollout,
    stable_gateway_health,
    validate_rollout_coordinator,
    validate_rollout_plan,
)


def _git(repo: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", *args],
        cwd=repo,
        check=True,
        capture_output=True,
        text=True,
    )
    return result.stdout.strip()


def _validate_windows_paths(project: Path, **overrides) -> None:
    external = project.parent / "coordinator"
    values = {
        "executable": external / "python.exe",
        "prefix": external,
        "exec_prefix": external,
        "cwd": project.parent,
        "search_paths": [external / "Lib"],
        "module_paths": [],
        "loaded_images": [],
    }
    values.update(overrides)
    _validate_windows_coordinator_paths(project, **values)


@pytest.fixture()
def repo(tmp_path: Path) -> Path:
    root = tmp_path / "checkout"
    root.mkdir()
    _git(root, "init")
    _git(root, "config", "user.email", "tests@example.invalid")
    _git(root, "config", "user.name", "Hermes Tests")
    (root / "tracked.txt").write_text("old-code", encoding="utf-8")
    (root / ".gitignore").write_text(
        "venv/\n.update-incomplete\n.lazy-refresh-incomplete\n",
        encoding="utf-8",
    )
    venv = root / "venv"
    (venv / "lib" / "demo-1.0.dist-info").mkdir(parents=True)
    (venv / "pyvenv.cfg").write_text("home = /old/python\n", encoding="utf-8")
    (venv / "lib" / "payload.bin").write_bytes(b"OLD-DEPENDENCY")
    (venv / "lib" / "demo-1.0.dist-info" / "METADATA").write_text(
        "Name: demo\nVersion: 1.0\n", encoding="utf-8"
    )
    if os.name != "nt":
        (venv / "lib" / "payload-link").symlink_to("payload.bin")
        interpreter = venv / "bin" / "python"
        interpreter.parent.mkdir()
        interpreter.write_text(
            "#!/bin/sh\nprintf '%s\\n' hermes-rollback-interpreter-ok\n",
            encoding="utf-8",
        )
        interpreter.chmod(0o755)
    _git(root, "add", "tracked.txt", ".gitignore")
    _git(root, "commit", "-m", "old")
    return root


@pytest.fixture(autouse=True)
def _windows_restored_interpreter_probe(monkeypatch):
    """Native-Windows unit tests use a synthetic venv without a PE binary."""

    if sys.platform == "win32":
        monkeypatch.setattr(
            "hermes_cli.update_rollout._verify_restored_interpreter",
            lambda venv, project: {
                "ok": True,
                "kind": "interpreter",
                "path": str(venv / "Scripts" / "python.exe"),
            },
        )


def _config(**overrides) -> RolloutConfig:
    values = {
        "enabled": True,
        "canary_profile": "canary",
        "batch_size": 1,
        "health_timeout_seconds": 10,
        "healthy_after_seconds": 1,
        "smoke_timeout_seconds": 5,
        "canary_smoke_agent_turn": False,
        "restart_timeout_seconds": 10,
        "checkpoint_keep": 3,
    }
    values.update(overrides)
    return RolloutConfig(**values)


def _configure_tauri_parent_proof(tmp_path: Path, monkeypatch):
    import hermes_cli.update_cmd as update_cmd
    import hermes_cli.update_lock as update_lock

    home = tmp_path / "tauri-control-home"
    home.mkdir()
    correlation_id = "12345678-1234-4678-9234-567812345678"
    marker = home / ".hermes-update-in-progress"
    parent_pid = os.getppid()
    marker.write_text(
        f"{parent_pid}\n{int(time.time())}\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(update_cmd, "get_hermes_home", lambda: home)
    monkeypatch.setattr(update_lock, "update_marker_path", lambda: marker)
    monkeypatch.setenv("HERMES_UPDATE_CORRELATION_ID", correlation_id)
    monkeypatch.setenv("HERMES_UPDATE_HANDOFF_PID", str(parent_pid))
    monkeypatch.setenv(
        "HERMES_UPDATE_TAURI_READY_PATH",
        str(home / f".update_coordinator_ready.{correlation_id}"),
    )
    monkeypatch.setenv(
        "HERMES_UPDATE_TAURI_OUTCOME_PATH",
        str(home / f".update_exit_code.{correlation_id}"),
    )
    return SimpleNamespace(
        home=home,
        marker=marker,
        correlation_id=correlation_id,
        parent_pid=parent_pid,
    )


def _plan(*profiles: str) -> UpdatePlan:
    return UpdatePlan(
        install_method="git",
        runtimes=[
            RuntimeRecord(
                kind="gateway",
                profile=profile,
                pid=100 + index,
                supervisor="manual",
                restart_via="manual",
            )
            for index, profile in enumerate(profiles)
        ],
    )


def _quiesce_ok(profile, runtime):
    return {
        "ok": True,
        "quiesced": True,
        "profile": profile,
        "old_pid": runtime.pid,
    }


def test_rollout_is_disabled_by_default_and_bounded_when_enabled():
    assert load_rollout_config({}).enabled is False
    config = load_rollout_config(
        {
            "updates": {
                "canary_profile": "Work",
                "rollout_batch_size": 0,
                "canary_health_timeout_seconds": 99999,
                "canary_healthy_after_seconds": 99999,
                "canary_smoke_agent_turn": True,
                "rollback_checkpoint_keep": 0,
            }
        }
    )
    assert config.enabled is True
    assert config.canary_profile == "work"
    assert config.batch_size == 1
    assert config.health_timeout_seconds == 900
    assert config.healthy_after_seconds == 300
    assert config.canary_smoke_agent_turn is True
    assert config.checkpoint_keep == 1

    # The provider turn can incur usage.  Fail closed instead of treating
    # truthy strings/numbers as an opt-in.
    assert load_rollout_config(
        {"updates": {"canary_smoke_agent_turn": "true"}}
    ).canary_smoke_agent_turn is False
    assert load_rollout_config(
        {"updates": {"canary_smoke_agent_turn": 1}}
    ).canary_smoke_agent_turn is False


@pytest.mark.parametrize(
    ("platform", "requires_full_drain"),
    [("win32", True), ("linux", False), ("darwin", False)],
)
def test_preapply_full_fleet_quiesce_is_windows_only(
    platform: str, requires_full_drain: bool
):
    import hermes_cli.update_cmd as update_cmd

    assert (
        update_cmd._rollout_requires_preapply_fleet_quiesce(platform)
        is requires_full_drain
    )


def test_checkpoint_is_external_and_restores_exact_code_and_venv(repo: Path):
    old_sha = _git(repo, "rev-parse", "HEAD")
    (repo / ".update-incomplete").write_text("preexisting", encoding="utf-8")
    # Even an explicitly bad base inside the checkout is rerouted outside it.
    checkpoint = create_checkpoint(
        repo,
        config=_config(),
        plan=_plan("canary"),
        base=repo / "state" / "checkpoints",
    )
    with pytest.raises(ValueError):
        checkpoint.resolve().relative_to(repo.resolve())
    metadata = read_checkpoint(checkpoint)
    assert metadata["pre_sha"] == old_sha
    assert metadata["dependency_state"]["venv_present"] is True
    assert metadata["dependency_state"]["manifest_version"] == 2
    assert "mode" in metadata["dependency_state"]["manifest_fields"]
    assert metadata["dependency_state"]["manifest_sha256"]
    assert metadata["dependency_state"]["directory_count"] >= 3
    assert metadata["dependency_state"]["entry_count"] > metadata[
        "dependency_state"
    ]["file_count"]
    assert metadata["runtime_profiles"] == ["canary"]

    (repo / "tracked.txt").write_text("new-code", encoding="utf-8")
    _git(repo, "add", "tracked.txt")
    _git(repo, "commit", "-m", "new")
    (repo / "venv" / "lib" / "payload.bin").write_bytes(b"NEW-DEPENDENCY")
    (repo / ".update-incomplete").write_text("candidate", encoding="utf-8")
    (repo / ".lazy-refresh-incomplete").write_text(
        "candidate-only", encoding="utf-8"
    )

    result = restore_checkpoint(checkpoint, repo)
    assert result["restored"] is True
    assert result["verified"] is True
    assert result["interpreter"]["ok"] is True
    assert _git(repo, "rev-parse", "HEAD") == old_sha
    assert (repo / "tracked.txt").read_text(encoding="utf-8") == "old-code"
    assert (repo / "venv" / "lib" / "payload.bin").read_bytes() == b"OLD-DEPENDENCY"
    assert (repo / ".update-incomplete").read_text(encoding="utf-8") == "preexisting"
    assert not (repo / ".lazy-refresh-incomplete").exists()


def test_same_size_checkpoint_tamper_is_rejected(repo: Path):
    checkpoint = create_checkpoint(repo, config=_config(), base=repo.parent / "cp")
    snapshot = checkpoint / "venv" / "lib" / "payload.bin"
    original = snapshot.read_bytes()
    snapshot.write_bytes(b"X" * len(original))
    with pytest.raises(RollbackError, match="manifest"):
        restore_checkpoint(checkpoint, repo)


def test_checkpoint_rejects_live_venv_mutation_during_copy(
    repo: Path, monkeypatch
):
    import hermes_cli.update_rollout as rollout

    original_copytree = rollout.shutil.copytree

    def copy_then_mutate(source, destination, *args, **kwargs):
        result = original_copytree(source, destination, *args, **kwargs)
        if Path(source) == repo / "venv":
            (repo / "venv" / "lib" / "payload.bin").write_bytes(b"RACED")
        return result

    monkeypatch.setattr(rollout.shutil, "copytree", copy_then_mutate)

    with pytest.raises(CheckpointError, match="changed while"):
        create_checkpoint(
            repo,
            config=_config(),
            base=repo.parent / "cp-race",
        )

    assert not list((repo.parent / "cp-race").glob(".stage-*"))


def test_checkpoint_retention_is_deferred_until_candidate_commit(repo: Path):
    base = repo.parent / "cp-retention"
    first = create_checkpoint(
        repo, config=_config(checkpoint_keep=1), base=base, prune=False
    )
    second = create_checkpoint(
        repo, config=_config(checkpoint_keep=1), base=base, prune=False
    )

    assert first.exists()
    assert second.exists()

    prune_checkpoints_after_commit(second, keep=1)

    assert not first.exists()
    assert second.exists()


@pytest.mark.linux_only
def test_checkpoint_mode_tamper_is_rejected(repo: Path):
    """Linux lane: POSIX mode bits are part of the dependency manifest."""

    checkpoint = create_checkpoint(repo, config=_config(), base=repo.parent / "cp")
    snapshot = checkpoint / "venv" / "lib" / "payload.bin"
    snapshot.chmod(snapshot.stat().st_mode | 0o100)
    with pytest.raises(RollbackError, match="manifest"):
        restore_checkpoint(checkpoint, repo)


@pytest.mark.linux_only
def test_restored_interpreter_must_be_executable(tmp_path: Path):
    """Linux lane: execute permission is required before the smoke process."""

    venv = tmp_path / "venv"
    interpreter = venv / "bin" / "python"
    interpreter.parent.mkdir(parents=True)
    interpreter.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    interpreter.chmod(0o644)
    with pytest.raises(RollbackError, match="not executable"):
        _verify_restored_interpreter(venv, tmp_path)


def test_dependency_state_match_detects_mutation_and_absence(repo: Path):
    checkpoint = create_checkpoint(repo, config=_config(), base=repo.parent / "cp")
    assert dependency_state_matches_checkpoint(checkpoint, repo) is True
    (repo / "venv" / "lib" / "payload.bin").write_bytes(b"MUTATED")
    assert dependency_state_matches_checkpoint(checkpoint, repo) is False

    shutil.rmtree(repo / "venv")
    absent = create_checkpoint(
        repo, config=_config(), base=repo.parent / "absent-checkpoint"
    )
    assert dependency_state_matches_checkpoint(absent, repo) is True
    (repo / "venv").mkdir()
    assert dependency_state_matches_checkpoint(absent, repo) is False


@pytest.mark.parametrize("attribute", ["executable", "prefix", "exec_prefix"])
def test_windows_coordinator_rejects_live_venv_python_state(
    tmp_path: Path, attribute: str
):
    project = tmp_path / "project"
    live_value = project / "venv" / "Scripts" / "python.exe"

    with pytest.raises(RolloutError, match=f"sys.{attribute}"):
        _validate_windows_paths(project, **{attribute: live_value})


@pytest.mark.parametrize("native_name", ["native.pyd", "native.dll", "native.abi3.so"])
def test_windows_coordinator_rejects_loaded_native_venv_extension(
    tmp_path: Path, native_name: str
):
    project = tmp_path / "project"

    with pytest.raises(RolloutError, match="module _rollout_native_test"):
        _validate_windows_paths(
            project,
            module_paths=[
                ("_rollout_native_test", project / ".venv" / "Lib" / native_name)
            ],
        )


def test_windows_coordinator_rejects_live_venv_sys_path_and_source_modules(
    tmp_path: Path,
):
    project = tmp_path / "project"

    with pytest.raises(RolloutError) as raised:
        _validate_windows_paths(
            project,
            search_paths=[project / "venv" / "Lib" / "site-packages"],
            module_paths=[
                ("_rollout_source_test", project / "venv" / "Lib" / "source.py")
            ],
        )

    detail = str(raised.value)
    assert "sys.path[0]" in detail
    assert "module _rollout_source_test" in detail


def test_windows_coordinator_rejects_directly_loaded_venv_dll(
    tmp_path: Path,
):
    project = tmp_path / "project"

    with pytest.raises(RolloutError, match="loaded image"):
        _validate_windows_paths(
            project,
            loaded_images=[project / ".venv" / "Lib" / "direct.dll"],
        )


def test_windows_coordinator_allows_fully_external_runtime(
    tmp_path: Path,
):
    project = tmp_path / "project"
    external = tmp_path / "coordinator"
    _validate_windows_paths(
        project,
        executable=external / "python.exe",
        prefix=external,
        exec_prefix=external,
        search_paths=[external / "Lib"],
        module_paths=[
            ("_rollout_source_test", project / "hermes_cli" / "update_rollout.py")
        ],
    )


def test_windows_coordinator_rejects_live_venv_cwd(tmp_path: Path):
    project = tmp_path / "project"
    cwd = project / "venv" / "worker"
    cwd.mkdir(parents=True)

    with pytest.raises(RolloutError, match="cwd="):
        _validate_windows_paths(project, cwd=cwd)


@pytest.mark.windows_only
def test_windows_coordinator_fails_closed_when_image_enumeration_fails(
    tmp_path: Path, monkeypatch
):
    import hermes_cli.update_rollout as rollout

    project = tmp_path / "project"
    monkeypatch.setattr(
        rollout,
        "_windows_process_module_paths",
        lambda: (_ for _ in ()).throw(RolloutError("enumeration failed")),
    )

    with pytest.raises(RolloutError, match="enumeration failed"):
        validate_rollout_coordinator(project)


def test_windows_coordinator_fails_closed_on_unresolvable_path(
    tmp_path: Path,
):
    class BrokenPath:
        def __str__(self):
            raise OSError("path unavailable")

    project = tmp_path / "project"

    with pytest.raises(RolloutError, match="sys.executable"):
        _validate_windows_paths(project, executable=BrokenPath())


def test_critical_import_probe_uses_checkpoint_selected_dot_venv(
    tmp_path: Path, monkeypatch
):
    import hermes_cli.update_cmd as update_cmd

    interpreter = tmp_path / ".venv" / "bin" / "python"
    interpreter.parent.mkdir(parents=True)
    interpreter.write_text("", encoding="utf-8")
    calls: list[list[str]] = []

    def run(command, **kwargs):
        calls.append(list(command))
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(update_cmd.subprocess, "run", run)
    monkeypatch.setattr(update_cmd._m(), "_is_windows", lambda: False)

    assert update_cmd._validate_critical_modules_import(
        tmp_path, venv_name=".venv"
    ) == (True, None, None)
    assert calls[0][0] == str(interpreter)


def test_restore_runs_windows_coordinator_guard_before_checkpoint_io(
    tmp_path: Path, monkeypatch
):
    import hermes_cli.update_rollout as rollout

    project = tmp_path / "project"
    monkeypatch.setattr(
        rollout,
        "validate_rollout_coordinator",
        lambda root: (_ for _ in ()).throw(RolloutError("external interpreter")),
    )

    with pytest.raises(RolloutError, match="external interpreter"):
        restore_checkpoint(tmp_path / "missing-checkpoint", project)


def test_canary_kernel_guards_before_first_restart(tmp_path: Path, monkeypatch):
    import hermes_cli.update_rollout as rollout

    events: list[str] = []
    monkeypatch.setattr(
        rollout,
        "validate_rollout_coordinator",
        lambda project: (_ for _ in ()).throw(RolloutError("external required")),
    )

    with pytest.raises(RolloutError, match="external required"):
        run_canary_rollout(
            _plan("canary"),
            expected_sha="a" * 40,
            checkpoint=tmp_path / "checkpoint",
            config=_config(),
            project_root=tmp_path,
            restart_profile=lambda *args: events.append("restart") or {},
        )

    assert events == []


def test_restore_kernel_guards_before_quiescence(tmp_path: Path, monkeypatch):
    import hermes_cli.update_rollout as rollout

    events: list[str] = []
    monkeypatch.setattr(
        rollout,
        "validate_rollout_coordinator",
        lambda project: (_ for _ in ()).throw(RolloutError("external required")),
    )

    with pytest.raises(RolloutError, match="external required"):
        restore_and_verify_fleet(
            tmp_path / "checkpoint",
            _plan("canary"),
            config=_config(),
            project_root=tmp_path,
            quiesce_profile=lambda *args: events.append("quiesce") or {},
        )

    assert events == []


def test_restore_quiesce_failure_restarts_attempted_current_generation(
    tmp_path: Path, monkeypatch
):
    import hermes_cli.update_rollout as rollout

    restarts: list[dict] = []
    monkeypatch.setattr(rollout, "validate_rollout_coordinator", lambda project: None)
    monkeypatch.setattr(
        rollout,
        "_git_identity",
        lambda project: ("b" * 40, "main", False),
    )
    monkeypatch.setattr(
        rollout,
        "quiesce_rollout_fleet",
        lambda *args, **kwargs: {
            "ok": False,
            "attempted_profiles": ["canary", "later"],
            "errors": [{"profile": "later", "error": "stop failed"}],
        },
    )

    def restart(plan, **kwargs):
        restarts.append(kwargs)
        return {"verified": True}

    monkeypatch.setattr(rollout, "restart_and_verify_fleet", restart)
    monkeypatch.setattr(
        rollout,
        "restore_checkpoint",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            AssertionError("checkpoint restore must not run")
        ),
    )

    with pytest.raises(RollbackError, match="current generation restarted"):
        restore_and_verify_fleet(
            tmp_path / "checkpoint",
            _plan("canary", "later"),
            config=_config(),
            project_root=tmp_path,
        )

    assert restarts == [
        {
            "expected_sha": "b" * 40,
            "config": _config(),
            "project_root": tmp_path,
            "profiles": ["canary", "later"],
            "restart_profile": None,
            "health_gate": None,
        }
    ]


def test_restore_quiesce_baseexception_restarts_full_current_generation(
    tmp_path: Path, monkeypatch
):
    import hermes_cli.update_rollout as rollout

    class AbortQuiesce(BaseException):
        pass

    restarts: list[dict] = []
    monkeypatch.setattr(rollout, "validate_rollout_coordinator", lambda project: None)
    monkeypatch.setattr(
        rollout,
        "_git_identity",
        lambda project: ("c" * 40, "main", False),
    )
    monkeypatch.setattr(
        rollout,
        "quiesce_rollout_fleet",
        lambda *args, **kwargs: (_ for _ in ()).throw(AbortQuiesce()),
    )

    def restart(plan, **kwargs):
        restarts.append(kwargs)
        return {"verified": True}

    monkeypatch.setattr(rollout, "restart_and_verify_fleet", restart)
    monkeypatch.setattr(
        rollout,
        "restore_checkpoint",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            AssertionError("checkpoint restore must not run")
        ),
    )

    with pytest.raises(AbortQuiesce):
        restore_and_verify_fleet(
            tmp_path / "checkpoint",
            _plan("canary", "later"),
            config=_config(),
            project_root=tmp_path,
        )

    assert restarts == [
        {
            "expected_sha": "c" * 40,
            "config": _config(),
            "project_root": tmp_path,
            "profiles": None,
            "restart_profile": None,
            "health_gate": None,
        }
    ]


@pytest.mark.parametrize(
    ("failure_phase", "recovery_sha", "message"),
    [
        ("restore", "b" * 40, "current generation restarted"),
        ("after_restore", "a" * 40, "restored generation restarted"),
    ],
)
def test_restore_failure_restarts_the_generation_left_on_disk(
    tmp_path: Path,
    monkeypatch,
    failure_phase: str,
    recovery_sha: str,
    message: str,
):
    import hermes_cli.update_rollout as rollout

    restarts: list[dict] = []
    monkeypatch.setattr(rollout, "validate_rollout_coordinator", lambda project: None)
    monkeypatch.setattr(
        rollout,
        "_git_identity",
        lambda project: ("b" * 40, "main", False),
    )
    monkeypatch.setattr(
        rollout,
        "quiesce_rollout_fleet",
        lambda *args, **kwargs: {
            "ok": True,
            "attempted_profiles": ["canary"],
            "quiesced_profiles": ["canary"],
            "errors": [],
        },
    )

    def restore(*args, **kwargs):
        if failure_phase == "restore":
            raise RuntimeError("restore failed")
        return {"restored": True, "sha": "a" * 40}

    def after_restore():
        if failure_phase == "after_restore":
            raise RuntimeError("stash reconciliation failed")

    def restart(plan, **kwargs):
        restarts.append(kwargs)
        return {"verified": True}

    monkeypatch.setattr(rollout, "restore_checkpoint", restore)
    monkeypatch.setattr(rollout, "restart_and_verify_fleet", restart)

    with pytest.raises(RollbackError, match=message):
        restore_and_verify_fleet(
            tmp_path / "checkpoint",
            _plan("canary"),
            config=_config(),
            project_root=tmp_path,
            after_restore=after_restore,
        )

    assert restarts[0]["expected_sha"] == recovery_sha
    assert restarts[0]["profiles"] is None


@pytest.mark.linux_only
def test_checkpoint_symlink_target_tamper_is_rejected(repo: Path):
    checkpoint = create_checkpoint(repo, config=_config(), base=repo.parent / "cp")
    link = checkpoint / "venv" / "lib" / "payload-link"
    link.unlink()
    # Keep the target text exactly the same size as ``payload.bin``: link
    # type + target bytes, not size metadata, must protect the snapshot.
    link.symlink_to("changed.bin")
    with pytest.raises(RollbackError, match="manifest"):
        restore_checkpoint(checkpoint, repo)


@pytest.mark.linux_only
@pytest.mark.parametrize("venv_name", ["venv", ".venv"])
def test_checkpoint_refuses_a_linked_venv_root(
    repo: Path, tmp_path: Path, venv_name: str
):
    original = repo / "venv"
    external = tmp_path / f"external-{venv_name.removeprefix('.')}"
    original.rename(external)
    (repo / venv_name).symlink_to(external, target_is_directory=True)

    with pytest.raises(CheckpointError, match="real directory"):
        create_checkpoint(repo, config=_config(), base=repo.parent / "cp")

    assert (external / "lib" / "payload.bin").exists()


@pytest.mark.linux_only
def test_rollout_target_preparation_refuses_a_linked_venv_root(tmp_path: Path):
    import hermes_cli.update_cmd as update_cmd

    project = tmp_path / "project"
    external = tmp_path / "external-venv"
    project.mkdir()
    external.mkdir()
    (external / "sentinel").write_text("owned", encoding="utf-8")
    (project / "venv").symlink_to(external, target_is_directory=True)
    with pytest.raises(RuntimeError, match="real directory"):
        update_cmd._prepare_rollout_target_venv(project, "venv")

    assert (external / "sentinel").read_text(encoding="utf-8") == "owned"


def test_quiesce_does_not_reacquire_a_reused_initial_pid(monkeypatch):
    import hermes_cli.update_rollout as rollout
    from gateway import status

    clock = {"now": 0.0, "exists_probes": 0}
    signalled: list[tuple[int, bool]] = []

    monkeypatch.setattr(status, "get_running_pid", lambda **kwargs: None)

    def pid_exists(pid):
        clock["exists_probes"] += 1
        # The original exits on the first observation. Reusing 4242 during
        # the absence window must never make it a candidate again.
        return clock["exists_probes"] > 1

    monkeypatch.setattr(status, "_pid_exists", pid_exists)
    monkeypatch.setattr(status, "_looks_like_gateway_process", lambda pid: True)
    monkeypatch.setattr(
        status,
        "terminate_pid",
        lambda pid, force=False: signalled.append((pid, force)),
    )
    monkeypatch.setattr(
        rollout.time,
        "monotonic",
        lambda: clock["now"],
    )
    monkeypatch.setattr(
        rollout.time,
        "sleep",
        lambda seconds: clock.__setitem__("now", clock["now"] + seconds),
    )

    ok, stopped = rollout._quiesce_until_stable(
        initial_pid=4242,
        initial_start_time=100.0,
        timeout_seconds=5,
        terminate=True,
    )

    assert ok is True
    assert stopped == []
    assert signalled == []
    assert clock["exists_probes"] == 1


def test_quiesce_rejects_gateway_like_pid_reused_one_second_later(monkeypatch):
    import hermes_cli.update_rollout as rollout
    import psutil
    from gateway import status

    clock = {"now": 0.0}
    signalled: list[tuple[int, bool]] = []

    class ReusedGatewayProcess:
        def create_time(self):
            return 101.0

    monkeypatch.setattr(status, "get_running_pid", lambda **kwargs: None)
    monkeypatch.setattr(status, "_pid_exists", lambda pid: True)
    monkeypatch.setattr(status, "_looks_like_gateway_process", lambda pid: True)
    monkeypatch.setattr(
        status,
        "terminate_pid",
        lambda pid, force=False: signalled.append((pid, force)),
    )
    monkeypatch.setattr(psutil, "Process", lambda pid: ReusedGatewayProcess())
    monkeypatch.setattr(rollout.time, "monotonic", lambda: clock["now"])
    monkeypatch.setattr(
        rollout.time,
        "sleep",
        lambda seconds: clock.__setitem__("now", clock["now"] + seconds),
    )

    ok, stopped = rollout._quiesce_until_stable(
        initial_pid=4242,
        initial_start_time=100.0,
        timeout_seconds=5,
        terminate=True,
    )

    assert ok is True
    assert stopped == []
    assert signalled == []


def test_manual_quiesce_uses_full_grace_before_force_and_proves_absence(
    monkeypatch,
):
    import hermes_cli.update_rollout as rollout
    import psutil
    from gateway import status

    clock = {"now": 0.0, "forced": False}
    signalled: list[tuple[float, int, bool]] = []

    monkeypatch.setattr(
        status,
        "get_running_pid",
        lambda **kwargs: None if clock["forced"] else 4242,
    )
    monkeypatch.setattr(status, "_pid_exists", lambda pid: not clock["forced"])
    monkeypatch.setattr(status, "_looks_like_gateway_process", lambda pid: True)
    monkeypatch.setattr(status, "write_planned_stop_marker", lambda pid: None)
    monkeypatch.setattr(
        psutil,
        "Process",
        lambda pid: type(
            "OriginalGatewayProcess",
            (),
            {"create_time": lambda self: 100.0},
        )(),
    )

    def terminate(pid, force=False):
        signalled.append((clock["now"], pid, force))
        if force:
            clock["forced"] = True

    monkeypatch.setattr(status, "terminate_pid", terminate)
    monkeypatch.setattr(rollout.time, "monotonic", lambda: clock["now"])
    monkeypatch.setattr(
        rollout.time,
        "sleep",
        lambda seconds: clock.__setitem__("now", clock["now"] + seconds),
    )

    ok, stopped = rollout._quiesce_until_stable(
        initial_pid=4242,
        initial_start_time=100.0,
        timeout_seconds=5.0,
        terminate=True,
    )

    assert ok is True
    assert stopped == [4242]
    assert signalled[0] == (0.0, 4242, False)
    force_calls = [call for call in signalled if call[2] is True]
    assert len(force_calls) == 1
    # Five-second budget minus the one-second stable-absence proof and one
    # polling interval.  The old implementation forced at two seconds.
    assert force_calls[0][0] >= 3.9
    assert clock["now"] <= 5.0


@pytest.mark.parametrize(
    ("method", "updatable"),
    [("docker", False), ("zip", False), ("unknown", False)],
)
def test_non_git_or_non_in_place_rollout_refuses_before_checkpoint(
    repo: Path, method: str, updatable: bool
):
    plan = _plan("canary")
    plan.install_method = method
    plan.updatable_in_place = updatable
    checkpoint_dir = repo.parent / "must-not-exist"

    with pytest.raises(RolloutError, match="in-place Git install"):
        validate_rollout_plan(plan, _config())

    assert not checkpoint_dir.exists()


def test_manual_quiesce_refuses_without_validated_relaunch_argv():
    runtime = _plan("canary").runtimes[0]
    with pytest.raises(RolloutError, match="validated relaunch argv"):
        quiesce_profile_gateway("canary", runtime, config=_config())


def test_manual_quiesce_refuses_uncontrollable_external_supervisor():
    runtime = _plan("canary").runtimes[0]
    runtime.detail["argv"] = [
        sys.executable,
        "-m",
        "gateway.run",
        "--external-supervisor",
    ]
    with pytest.raises(RolloutError, match="external supervisor"):
        quiesce_profile_gateway("canary", runtime, config=_config())


@pytest.mark.parametrize("supervisor", ["desktop", "external", "mystery", ""])
def test_rollout_plan_rejects_unverifiable_supervisors(supervisor: str):
    plan = _plan("canary")
    plan.runtimes[0].supervisor = supervisor
    with pytest.raises(RolloutError, match="unsupported or unverifiable"):
        validate_rollout_plan(plan, _config())


def test_rollout_plan_rejects_duplicate_profile_authority():
    plan = _plan("canary", "canary")
    with pytest.raises(RolloutError, match="duplicate gateway runtime"):
        validate_rollout_plan(plan, _config())


def test_rollout_plan_rejects_supervisor_restart_route_mismatch():
    plan = _plan("canary")
    plan.runtimes[0].restart_via = "systemd"
    with pytest.raises(RolloutError, match="requires restart_via"):
        validate_rollout_plan(plan, _config())


def test_checkpoint_failure_leaves_install_untouched(repo: Path, monkeypatch):
    import hermes_cli.update_rollout as rollout

    sha = _git(repo, "rev-parse", "HEAD")
    payload = (repo / "venv" / "lib" / "payload.bin").read_bytes()

    def fail_copy(*args, **kwargs):
        raise OSError("disk full")

    monkeypatch.setattr(rollout.shutil, "copytree", fail_copy)
    base = repo.parent / "checkpoints"
    with pytest.raises(CheckpointError, match="disk full"):
        create_checkpoint(repo, config=_config(), base=base)
    assert _git(repo, "rev-parse", "HEAD") == sha
    assert (repo / "venv" / "lib" / "payload.bin").read_bytes() == payload
    assert not list(base.glob("*/checkpoint.json"))


@pytest.mark.parametrize("reference", ["../outside", "a/b", "", "..", "x" * 129])
def test_explicit_rollback_rejects_invalid_or_traversal_ids(
    repo: Path, reference: str
):
    with pytest.raises(CheckpointError):
        resolve_checkpoint(reference, repo, base=repo.parent / "empty")


def test_explicit_rollback_refuses_dirty_tree(repo: Path):
    checkpoint = create_checkpoint(repo, config=_config(), base=repo.parent / "cp")
    (repo / "tracked.txt").write_text("local edit", encoding="utf-8")
    with pytest.raises(RollbackError, match="local changes"):
        restore_checkpoint(checkpoint, repo)


def test_transaction_owned_reset_explicitly_discards_tracked_apply_dirt(repo: Path):
    checkpoint = create_checkpoint(repo, config=_config(), base=repo.parent / "cp")
    (repo / "tracked.txt").write_text("transaction-generated", encoding="utf-8")

    restored = restore_checkpoint(
        checkpoint, repo, transaction_owned_reset=True
    )

    assert restored["transaction_owned_reset"] is True
    assert (repo / "tracked.txt").read_text(encoding="utf-8") == "old-code"
    assert _git(repo, "status", "--porcelain") == ""


def test_interpreter_smoke_failure_compensates_candidate_state(repo: Path):
    checkpoint = create_checkpoint(repo, config=_config(), base=repo.parent / "cp")
    (repo / "tracked.txt").write_text("candidate-code", encoding="utf-8")
    _git(repo, "add", "tracked.txt")
    _git(repo, "commit", "-m", "candidate")
    candidate_sha = _git(repo, "rev-parse", "HEAD")
    candidate_payload = b"CANDIDATE-DEPENDENCY"
    (repo / "venv" / "lib" / "payload.bin").write_bytes(candidate_payload)

    def fail_interpreter(venv, project):
        assert (venv / "lib" / "payload.bin").read_bytes() == b"OLD-DEPENDENCY"
        raise RollbackError("restored interpreter failed")

    with pytest.raises(RollbackError, match="restored interpreter failed"):
        restore_checkpoint(
            checkpoint,
            repo,
            interpreter_verifier=fail_interpreter,
        )

    assert _git(repo, "rev-parse", "HEAD") == candidate_sha
    assert (repo / "tracked.txt").read_text(encoding="utf-8") == "candidate-code"
    assert (repo / "venv" / "lib" / "payload.bin").read_bytes() == candidate_payload


def test_checkpoint_rejects_traversal_venv_name_before_restore(repo: Path):
    checkpoint = create_checkpoint(repo, config=_config(), base=repo.parent / "cp")
    metadata_path = checkpoint / "checkpoint.json"
    metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
    metadata["venv_name"] = "../../outside"
    metadata_path.write_text(json.dumps(metadata), encoding="utf-8")
    outside = repo.parent / "outside"
    outside.mkdir()
    sentinel = outside / "sentinel.txt"
    sentinel.write_text("untouched", encoding="utf-8")

    with pytest.raises(CheckpointError, match="unsafe venv_name"):
        read_checkpoint(checkpoint)
    with pytest.raises(CheckpointError, match="unsafe venv_name"):
        restore_checkpoint(checkpoint, repo)

    assert sentinel.read_text(encoding="utf-8") == "untouched"


def test_restore_swap_interrupt_compensates_git_and_live_venv(
    repo: Path, monkeypatch
):
    class AbortSwap(BaseException):
        pass

    checkpoint = create_checkpoint(repo, config=_config(), base=repo.parent / "cp")
    (repo / "tracked.txt").write_text("candidate-code", encoding="utf-8")
    _git(repo, "add", "tracked.txt")
    _git(repo, "commit", "-m", "candidate")
    candidate_sha = _git(repo, "rev-parse", "HEAD")
    candidate_payload = b"CANDIDATE-DEPENDENCY"
    (repo / "venv" / "lib" / "payload.bin").write_bytes(candidate_payload)

    original_replace = Path.replace

    def interrupt_candidate_promotion(path: Path, target: Path):
        target = Path(target)
        if (
            path.name.startswith(f".{repo.name}-venv-restore-")
            and target == repo / "venv"
        ):
            raise AbortSwap()
        return original_replace(path, target)

    monkeypatch.setattr(Path, "replace", interrupt_candidate_promotion)

    with pytest.raises(AbortSwap):
        restore_checkpoint(checkpoint, repo)

    assert _git(repo, "rev-parse", "HEAD") == candidate_sha
    assert (repo / "tracked.txt").read_text(encoding="utf-8") == "candidate-code"
    assert (repo / "venv" / "lib" / "payload.bin").read_bytes() == candidate_payload


@pytest.mark.parametrize("interrupt_phase", ["live_to_old", "stage_to_live"])
def test_restore_swap_post_rename_interrupt_compensates_exact_candidate(
    repo: Path, monkeypatch, interrupt_phase: str
):
    class AbortAfterRename(BaseException):
        pass

    checkpoint = create_checkpoint(repo, config=_config(), base=repo.parent / "cp")
    (repo / "tracked.txt").write_text("candidate-code", encoding="utf-8")
    _git(repo, "add", "tracked.txt")
    _git(repo, "commit", "-m", "candidate")
    candidate_sha = _git(repo, "rev-parse", "HEAD")
    candidate_payload = b"CANDIDATE-DEPENDENCY"
    (repo / "venv" / "lib" / "payload.bin").write_bytes(candidate_payload)

    original_replace = Path.replace
    interrupted = False

    def interrupt_after_rename(path: Path, target: Path):
        nonlocal interrupted
        target = Path(target)
        is_live_move = path == repo / "venv" and target.name.startswith(
            f".{repo.name}-venv-previous-"
        )
        is_stage_move = path.name.startswith(
            f".{repo.name}-venv-restore-"
        ) and target == repo / "venv"
        result = original_replace(path, target)
        should_interrupt = (
            interrupt_phase == "live_to_old" and is_live_move
        ) or (interrupt_phase == "stage_to_live" and is_stage_move)
        if should_interrupt and not interrupted:
            interrupted = True
            raise AbortAfterRename()
        return result

    monkeypatch.setattr(Path, "replace", interrupt_after_rename)

    with pytest.raises(AbortAfterRename):
        restore_checkpoint(checkpoint, repo)

    assert interrupted is True
    assert _git(repo, "rev-parse", "HEAD") == candidate_sha
    assert (repo / "tracked.txt").read_text(encoding="utf-8") == "candidate-code"
    assert (repo / "venv" / "lib" / "payload.bin").read_bytes() == candidate_payload
    assert not list(repo.parent.glob(f".{repo.name}-venv-previous-*"))
    assert not list(repo.parent.glob(f".{repo.name}-venv-restore-*"))


def test_reverse_rename_failure_reconstructs_candidate_venv(
    repo: Path, monkeypatch
):
    checkpoint = create_checkpoint(repo, config=_config(), base=repo.parent / "cp")
    (repo / "tracked.txt").write_text("candidate-code", encoding="utf-8")
    _git(repo, "add", "tracked.txt")
    _git(repo, "commit", "-m", "candidate")
    candidate_sha = _git(repo, "rev-parse", "HEAD")
    candidate_payload = b"CANDIDATE-DEPENDENCY"
    (repo / "venv" / "lib" / "payload.bin").write_bytes(candidate_payload)

    original_replace = Path.replace

    def fail_candidate_reverse_rename(path: Path, target: Path):
        if (
            path.name.startswith(f".{repo.name}-venv-previous-")
            and Path(target) == repo / "venv"
        ):
            raise OSError("simulated reverse rename failure")
        return original_replace(path, target)

    monkeypatch.setattr(Path, "replace", fail_candidate_reverse_rename)

    def reject_restored_interpreter(venv, project):
        raise RollbackError("restored interpreter failed")

    with pytest.raises(RollbackError, match="restored interpreter failed"):
        restore_checkpoint(
            checkpoint,
            repo,
            interpreter_verifier=reject_restored_interpreter,
        )

    assert _git(repo, "rev-parse", "HEAD") == candidate_sha
    assert (repo / "tracked.txt").read_text(encoding="utf-8") == "candidate-code"
    assert (repo / "venv" / "lib" / "payload.bin").read_bytes() == candidate_payload


def test_explicit_rollback_uses_saved_plan_when_canary_is_currently_down(
    repo: Path,
):
    saved = _plan("canary", "later")
    checkpoint = create_checkpoint(
        repo,
        config=_config(),
        plan=saved,
        base=repo.parent / "cp",
    )
    metadata = read_checkpoint(checkpoint)

    # A current inventory with no runtimes models a canary that cannot answer
    # its control socket.  The checkpoint remains the restart source of truth.
    recovered = plan_from_checkpoint(metadata, UpdatePlan(install_method="git"))
    assert [runtime.profile for runtime in recovered.runtimes] == [
        "canary",
        "later",
    ]
    assert [runtime.pid for runtime in recovered.runtimes] == [100, 101]

    events: list[tuple[str, str]] = []
    result = run_canary_rollout(
        recovered,
        expected_sha=metadata["pre_sha"],
        checkpoint=checkpoint,
        config=_config(),
        project_root=repo,
        restart_profile=lambda profile, runtime: events.append(
            ("restart", profile)
        )
        or {"profile": profile, "old_pid": runtime.pid},
        health_gate=lambda profile, sha, old_pid: events.append(
            ("health", profile)
        )
        or {"ok": True, "profile": profile, "sha": sha, "pid": old_pid + 1},
    )
    assert result["status"] == "healthy"
    assert events[:2] == [("restart", "canary"), ("health", "canary")]


def test_checkpoint_plan_overlays_supervisor_and_matching_restart_route(repo: Path):
    checkpoint = create_checkpoint(
        repo,
        config=_config(),
        plan=_plan("canary"),
        base=repo.parent / "cp",
    )
    live = _plan("canary")
    live.runtimes[0].supervisor = "systemd"
    live.runtimes[0].restart_via = "systemd"

    recovered = plan_from_checkpoint(read_checkpoint(checkpoint), live)

    assert recovered.runtimes[0].supervisor == "systemd"
    assert recovered.runtimes[0].restart_via == "systemd"


@pytest.mark.parametrize(
    "failure_kind",
    ["missing-target-sha", "unexpected-coordinator-exception"],
)
def test_post_apply_failure_restores_and_verifies_the_complete_saved_fleet(
    repo: Path, failure_kind: str
):
    old_sha = _git(repo, "rev-parse", "HEAD")
    plan = _plan("later", "canary")
    checkpoint = create_checkpoint(
        repo,
        config=_config(),
        plan=plan,
        base=repo.parent / "cp",
    )
    (repo / "tracked.txt").write_text(f"candidate-{failure_kind}", encoding="utf-8")
    _git(repo, "add", "tracked.txt")
    _git(repo, "commit", "-m", failure_kind)
    (repo / "venv" / "lib" / "payload.bin").write_bytes(b"CANDIDATE-BROKEN")

    events: list[tuple[str, str, str]] = []
    result = restore_and_verify_fleet(
        checkpoint,
        plan,
        config=_config(),
        project_root=repo,
        restart_profile=lambda profile, runtime: events.append(
            ("restart", profile, "")
        )
        or {"profile": profile, "old_pid": runtime.pid},
        health_gate=lambda profile, sha, old_pid: events.append(
            ("health", profile, sha)
        )
        or {"ok": True, "profile": profile, "sha": sha, "pid": old_pid + 1},
        quiesce_profile=lambda profile, runtime: events.append(
            ("quiesce", profile, "")
        )
        or _quiesce_ok(profile, runtime),
        quiesce_worker_probe=lambda: [],
        after_restore=lambda: events.append(("restored", "", "")),
    )

    assert result["verified"] is True
    assert result["restarted_profiles"] == ["canary", "later"]
    assert _git(repo, "rev-parse", "HEAD") == old_sha
    assert (repo / "venv" / "lib" / "payload.bin").read_bytes() == b"OLD-DEPENDENCY"
    assert events == [
        ("quiesce", "canary", ""),
        ("quiesce", "later", ""),
        ("restored", "", ""),
        ("restart", "canary", ""),
        ("health", "canary", old_sha),
        ("restart", "later", ""),
        ("health", "later", old_sha),
    ]


def test_canary_then_bounded_batches():
    plan = _plan("zeta", "canary", "alpha", "beta")
    events: list[tuple[str, str]] = []

    def restart(profile, runtime):
        events.append(("restart", profile))
        return {"profile": profile, "old_pid": runtime.pid, "killed_pids": [runtime.pid]}

    def health(profile, sha, old_pid):
        events.append(("health", profile))
        return {"ok": True, "profile": profile, "pid": old_pid + 1000, "sha": sha}

    result = run_canary_rollout(
        plan,
        expected_sha="b" * 40,
        checkpoint=Path("checkpoint-1"),
        config=_config(batch_size=2),
        project_root=Path("."),
        restart_profile=restart,
        health_gate=health,
        rollback=lambda path: pytest.fail("rollback must not run"),
    )
    assert result["status"] == "healthy"
    assert result["batches"] == [["alpha", "beta"], ["zeta"]]
    assert events == [
        ("restart", "canary"),
        ("health", "canary"),
        ("restart", "alpha"),
        ("restart", "beta"),
        ("health", "alpha"),
        ("health", "beta"),
        ("restart", "zeta"),
        ("health", "zeta"),
    ]


def test_staged_canary_keeps_untouched_profiles_serving_until_their_batch():
    live = {"canary": True, "later": True}
    events: list[tuple[str, str]] = []

    def quiesce(profile, runtime):
        # The old-generation peer must remain available while the canary is
        # stopped, restarted, and smoke-gated.
        if profile == "canary":
            assert live["later"] is True
        live[profile] = False
        events.append(("quiesce", profile))
        return _quiesce_ok(profile, runtime)

    def restart(profile, runtime):
        assert live[profile] is False
        live[profile] = True
        events.append(("restart", profile))
        return {"profile": profile, "old_pid": runtime.pid}

    def health(profile, sha, old_pid):
        assert live[profile] is True
        if profile == "canary":
            assert live["later"] is True
        events.append(("health", profile))
        return {"ok": True, "profile": profile, "sha": sha}

    result = run_canary_rollout(
        _plan("canary", "later"),
        expected_sha="b" * 40,
        checkpoint=Path("checkpoint-1"),
        config=_config(batch_size=1),
        project_root=Path("."),
        restart_profile=restart,
        health_gate=health,
        rollback=lambda path: pytest.fail("rollback must not run"),
        quiesce_profile=quiesce,
        quiesce_worker_probe=lambda: [],
        prequiesced_profiles=[],
    )

    assert result["status"] == "healthy"
    assert result["prequiesced_profiles"] == []
    assert [stage["quiesced_profiles"] for stage in result["quiesce"]] == [
        ["canary"],
        ["later"],
    ]
    assert [stage["workers"]["profiles"] for stage in result["quiesce"]] == [
        ["canary"],
        ["later"],
    ]
    assert events == [
        ("quiesce", "canary"),
        ("restart", "canary"),
        ("health", "canary"),
        ("quiesce", "later"),
        ("restart", "later"),
        ("health", "later"),
    ]


def test_opt_in_provider_smoke_runs_only_for_canary_and_is_receipted(
    tmp_path: Path, monkeypatch
):
    import hermes_cli.update_receipt as update_receipt
    import hermes_cli.update_rollout as rollout

    calls: list[tuple[str, bool]] = []

    def health(profile, sha, **kwargs):
        provider_turn = kwargs["smoke_agent_turn"]
        calls.append((profile, provider_turn))
        smoke = {
            "ok": True,
            "kind": "agent-bootstrap",
            "mode": "provider-turn" if provider_turn else "structural",
            "profile": profile,
        }
        if provider_turn:
            smoke["agent_turn"] = {
                "ok": True,
                "kind": "agent-turn",
                "mode": "provider-turn",
                "api_calls": 1,
                "response_received": True,
            }
        return {"ok": True, "profile": profile, "sha": sha, "smoke": smoke}

    monkeypatch.setattr(rollout, "stable_gateway_health", health)
    result = run_canary_rollout(
        _plan("canary", "later"),
        expected_sha="new",
        checkpoint=Path("cp"),
        config=_config(canary_smoke_agent_turn=True),
        project_root=Path("."),
        restart_profile=lambda profile, runtime: {
            "profile": profile,
            "old_pid": runtime.pid,
        },
        rollback=lambda path: pytest.fail("rollback must not run"),
    )

    assert calls == [("canary", True), ("later", False)]
    assert result["smoke"]["mode"] == "provider-turn"
    assert result["smoke"]["ok"] is True
    assert result["smoke"]["result"]["agent_turn"]["api_calls"] == 1

    receipt_home = tmp_path / "receipt-home"
    receipt_home.mkdir()
    monkeypatch.setattr(
        "hermes_cli.config.get_hermes_home", lambda: receipt_home
    )
    update_receipt._current = None
    try:
        update_receipt.begin_update_receipt()
        update_receipt.record_canary(**result)
        receipt_path = update_receipt.finalize_update_receipt("success")
    finally:
        update_receipt._current = None
    persisted = json.loads(receipt_path.read_text(encoding="utf-8"))
    assert persisted["canary"]["smoke"] == result["smoke"]


def test_opt_in_provider_smoke_failure_rolls_back_before_later_batches(
    monkeypatch,
):
    import hermes_cli.update_rollout as rollout

    events: list[tuple[str, str]] = []

    def health(profile, sha, **kwargs):
        events.append((f"health-{sha}", profile))
        assert kwargs["smoke_agent_turn"] is (profile == "canary")
        if sha == "new" and profile == "canary":
            raise RolloutError("provider smoke timed out")
        return {
            "ok": True,
            "profile": profile,
            "sha": sha,
            "smoke": {"ok": True, "mode": "provider-turn"},
        }

    monkeypatch.setattr(rollout, "stable_gateway_health", health)

    with pytest.raises(RolloutExecutionError) as raised:
        run_canary_rollout(
            _plan("canary", "later"),
            expected_sha="new",
            checkpoint=Path("cp"),
            config=_config(canary_smoke_agent_turn=True),
            project_root=Path("."),
            restart_profile=lambda profile, runtime: events.append(
                ("restart", profile)
            )
            or {"profile": profile, "old_pid": runtime.pid},
            rollback=lambda path: {"restored": True, "sha": "old"},
            quiesce_profile=_quiesce_ok,
            quiesce_worker_probe=lambda: [],
        )

    result = raised.value.result
    assert ("restart", "later") not in events
    assert result["smoke"]["mode"] == "provider-turn"
    assert result["smoke"]["ok"] is False
    assert "provider smoke timed out" in result["smoke"]["result"]["error"]
    assert result["rollback"]["verified"] is True


def test_canary_failure_stops_remaining_and_verifies_rollback():
    events: list[tuple[str, str]] = []

    def restart(profile, runtime):
        events.append(("restart", profile))
        return {"profile": profile, "old_pid": runtime.pid}

    def health(profile, sha, old_pid):
        events.append((f"health-{sha[0]}", profile))
        if sha.startswith("n"):
            raise RuntimeError("candidate unhealthy")
        return {"ok": True, "profile": profile, "pid": 900, "sha": sha}

    with pytest.raises(RolloutExecutionError) as raised:
        run_canary_rollout(
            _plan("canary", "later"),
            expected_sha="new",
            checkpoint=Path("cp"),
            config=_config(),
            project_root=Path("."),
            restart_profile=restart,
            health_gate=health,
            rollback=lambda path: {"restored": True, "verified": True, "sha": "old"},
            quiesce_profile=_quiesce_ok,
            quiesce_worker_probe=lambda: [],
        )
    result = raised.value.result
    assert ("restart", "later") not in events
    assert result["rollback"]["verified"] is True
    assert result["rollback"]["restarted_profiles"] == ["canary"]


def test_later_batch_failure_recovers_every_advanced_profile_canary_first():
    events: list[tuple[str, str]] = []

    def restart(profile, runtime):
        events.append(("restart", profile))
        return {"profile": profile, "old_pid": runtime.pid}

    def health(profile, sha, old_pid):
        events.append((f"health-{sha}", profile))
        if sha == "new" and profile == "beta":
            raise RuntimeError("beta failed")
        return {"ok": True, "profile": profile, "pid": old_pid + 1000, "sha": sha}

    with pytest.raises(RolloutExecutionError) as raised:
        run_canary_rollout(
            _plan("canary", "alpha", "beta", "never"),
            expected_sha="new",
            checkpoint=Path("cp"),
            config=_config(batch_size=1),
            project_root=Path("."),
            restart_profile=restart,
            health_gate=health,
            rollback=lambda path: {"restored": True, "verified": True, "sha": "old"},
            quiesce_profile=_quiesce_ok,
            quiesce_worker_probe=lambda: [],
        )
    # "never" was not advanced. Canary, alpha, and the failed beta generation
    # are all restarted on restored disk, in canary-first order.
    assert ("restart", "never") not in events
    assert raised.value.result["rollback"]["restarted_profiles"] == [
        "canary",
        "alpha",
        "beta",
    ]
    assert raised.value.result["rollback"]["verified"] is True


def test_rollback_failure_is_distinct_and_truthful():
    def restart(profile, runtime):
        return {"profile": profile, "old_pid": runtime.pid}

    def health(profile, sha, old_pid):
        raise RuntimeError("canary failed")

    def rollback(path):
        raise RollbackError("venv locked")

    with pytest.raises(RolloutExecutionError) as raised:
        run_canary_rollout(
            _plan("canary", "later"),
            expected_sha="new",
            checkpoint=Path("cp"),
            config=_config(),
            project_root=Path("."),
            restart_profile=restart,
            health_gate=health,
            rollback=rollback,
            quiesce_profile=_quiesce_ok,
            quiesce_worker_probe=lambda: [],
        )
    rollback_result = raised.value.result["rollback"]
    assert rollback_result["attempted"] is True
    assert rollback_result["restored"] is False
    assert rollback_result["verified"] is False
    assert "venv locked" in rollback_result["error"]


def test_restart_raise_is_quiesced_before_windows_sensitive_restore():
    events: list[tuple[str, str]] = []
    state = {"restored": False}

    def restart(profile, runtime):
        events.append(("restart-old" if state["restored"] else "restart-new", profile))
        if not state["restored"] and profile == "beta":
            raise RuntimeError("restart raised after spawning beta")
        return {"profile": profile, "old_pid": runtime.pid}

    def rollback(path):
        # Every attempted profile, including the restart that raised before
        # returning a record, is stopped before the venv restore callback.
        assert events[-3:] == [
            ("quiesce", "canary"),
            ("quiesce", "alpha"),
            ("quiesce", "beta"),
        ]
        events.append(("restore", path.name))
        state["restored"] = True
        return {"restored": True, "sha": "old"}

    with pytest.raises(RolloutExecutionError) as raised:
        run_canary_rollout(
            _plan("canary", "alpha", "beta"),
            expected_sha="new",
            checkpoint=Path("cp"),
            config=_config(batch_size=2),
            project_root=Path("."),
            restart_profile=restart,
            health_gate=lambda profile, sha, old_pid: {
                "ok": True,
                "profile": profile,
                "sha": sha,
            },
            rollback=rollback,
            quiesce_profile=lambda profile, runtime: events.append(
                ("quiesce", profile)
            )
            or _quiesce_ok(profile, runtime),
            quiesce_worker_probe=lambda: [],
        )

    rollback_result = raised.value.result["rollback"]
    assert rollback_result["attempted_profiles"] == ["canary", "alpha", "beta"]
    assert rollback_result["verified"] is True
    assert events.index(("quiesce", "beta")) < events.index(("restore", "cp"))


def test_recovery_continues_after_one_profile_restart_raises():
    events: list[tuple[str, str]] = []
    state = {"restored": False}

    def restart(profile, runtime):
        phase = "old" if state["restored"] else "new"
        events.append((f"restart-{phase}", profile))
        if state["restored"] and profile == "canary":
            raise RuntimeError("canary recovery restart failed")
        return {"profile": profile, "old_pid": runtime.pid}

    def health(profile, sha, old_pid):
        if sha == "new" and profile == "beta":
            raise RuntimeError("candidate beta failed")
        return {"ok": True, "profile": profile, "sha": sha}

    def rollback(path):
        state["restored"] = True
        return {"restored": True, "sha": "old"}

    with pytest.raises(RolloutExecutionError) as raised:
        run_canary_rollout(
            _plan("canary", "alpha", "beta"),
            expected_sha="new",
            checkpoint=Path("cp"),
            config=_config(batch_size=1),
            project_root=Path("."),
            restart_profile=restart,
            health_gate=health,
            rollback=rollback,
            quiesce_profile=_quiesce_ok,
            quiesce_worker_probe=lambda: [],
        )

    rollback_result = raised.value.result["rollback"]
    assert ("restart-old", "alpha") in events
    assert ("restart-old", "beta") in events
    assert rollback_result["verified"] is False
    assert rollback_result["restarted_profiles"] == ["alpha", "beta"]
    assert rollback_result["errors"][0]["profile"] == "canary"


def test_recovery_attempts_later_profiles_before_reraising_baseexception():
    class AbortRestart(BaseException):
        pass

    attempted: list[str] = []

    def restart(profile, runtime):
        attempted.append(profile)
        if profile == "canary":
            raise AbortRestart()
        return {"profile": profile, "old_pid": runtime.pid}

    with pytest.raises(AbortRestart):
        restart_and_verify_fleet(
            _plan("canary", "later"),
            expected_sha="a" * 40,
            config=_config(),
            project_root=Path("."),
            restart_profile=restart,
            health_gate=lambda profile, sha, old_pid: {"ok": True},
        )

    assert attempted == ["canary", "later"]


def test_manual_restart_never_signals_a_reused_saved_pid(
    monkeypatch, tmp_path
):
    import hermes_cli.gateway as gateway_cli
    import hermes_cli.update_rollout as rollout
    import psutil
    from gateway import control_socket, status

    runtime = RuntimeRecord(
        kind="gateway",
        profile="default",
        pid=4242,
        supervisor="manual",
        restart_via="manual",
        detail={
            "argv": ["python", "-m", "hermes_cli.main", "gateway", "run"],
            "start_time": 100.0,
        },
    )
    launched: list[list[str]] = []

    class ReusedProcess:
        def create_time(self):
            return 101.0

    monkeypatch.setattr(control_socket, "identify_gateway", lambda *a, **k: None)
    monkeypatch.setattr(status, "_pid_exists", lambda pid: True)
    monkeypatch.setattr(status, "_looks_like_gateway_process", lambda pid: True)
    monkeypatch.setattr(psutil, "Process", lambda pid: ReusedProcess())
    arm = monkeypatch.setattr(
        gateway_cli,
        "_prepare_profile_gateway_update_restart",
        lambda *a, **k: (_ for _ in ()).throw(
            AssertionError("reused PID must not receive a restart watcher")
        ),
    )
    monkeypatch.setattr(
        gateway_cli,
        "_graceful_restart_via_sigusr1",
        lambda *a, **k: (_ for _ in ()).throw(
            AssertionError("reused PID must not be signalled")
        ),
    )
    monkeypatch.setattr(
        rollout.subprocess,
        "Popen",
        lambda argv, **kwargs: launched.append(list(argv)) or SimpleNamespace(pid=5000),
    )

    result = restart_profile_gateway("default", runtime, config=_config())

    assert arm is None
    assert launched == [runtime.detail["argv"]]
    assert result["old_pid"] is None
    assert result["killed_pids"] == []


def test_live_final_pass_restarts_only_profiles_proven_quiesced():
    live = {"canary": True, "later": True}
    restarted: list[str] = []

    def quiesce(profile, runtime):
        if profile == "later":
            return {"ok": False, "quiesced": False, "profile": profile}
        live[profile] = False
        return {"ok": True, "quiesced": True, "profile": profile}

    def restart(profile, runtime):
        # Calling this for the ambiguous still-live profile would model the
        # duplicate manual-gateway regression this orchestration prevents.
        assert live[profile] is False
        live[profile] = True
        restarted.append(profile)
        return {"profile": profile, "old_pid": runtime.pid}

    result = quiesce_restart_and_verify_fleet(
        _plan("canary", "later"),
        expected_sha="a" * 40,
        config=_config(),
        project_root=Path("."),
        quiesce_profile=quiesce,
        quiesce_worker_probe=lambda: [],
        restart_profile=restart,
        health_gate=lambda profile, sha, old_pid: {"ok": True},
    )

    assert restarted == ["canary"]
    assert live == {"canary": True, "later": True}
    assert result["verified"] is False
    assert result["quiesce"]["quiesced_profiles"] == ["canary"]


def test_live_final_pass_is_canary_first_and_keeps_later_profiles_available():
    live = {"canary": True, "alpha": True, "beta": True}
    events: list[tuple[str, str]] = []

    def quiesce(profile, runtime):
        if profile == "canary":
            assert live["alpha"] is True
            assert live["beta"] is True
        if profile == "alpha":
            assert live["beta"] is True
        live[profile] = False
        events.append(("quiesce", profile))
        return _quiesce_ok(profile, runtime)

    def restart(profile, runtime):
        assert live[profile] is False
        live[profile] = True
        events.append(("restart", profile))
        return {"profile": profile, "old_pid": runtime.pid}

    def health(profile, sha, old_pid):
        assert live[profile] is True
        if profile == "canary":
            assert live["alpha"] is True
            assert live["beta"] is True
        if profile == "alpha":
            assert live["beta"] is True
        events.append(("health", profile))
        return {"ok": True, "profile": profile, "sha": sha}

    result = quiesce_restart_and_verify_fleet(
        _plan("canary", "alpha", "beta"),
        expected_sha="a" * 40,
        config=_config(batch_size=1),
        project_root=Path("."),
        quiesce_profile=quiesce,
        quiesce_worker_probe=lambda: [],
        restart_profile=restart,
        health_gate=health,
    )

    assert result["verified"] is True
    assert result["status"] == "healthy"
    assert result["batches"] == [["alpha"], ["beta"]]
    assert [
        stage["workers"]["profiles"]
        for stage in result["quiesce"]["stages"]
    ] == [["canary"], ["alpha"], ["beta"]]
    assert events == [
        ("quiesce", "canary"),
        ("restart", "canary"),
        ("health", "canary"),
        ("quiesce", "alpha"),
        ("restart", "alpha"),
        ("health", "alpha"),
        ("quiesce", "beta"),
        ("restart", "beta"),
        ("health", "beta"),
    ]


def test_live_final_pass_failed_canary_never_touches_later_profiles():
    events: list[tuple[str, str]] = []
    health_attempts = 0

    def quiesce(profile, runtime):
        events.append(("quiesce", profile))
        return _quiesce_ok(profile, runtime)

    def restart(profile, runtime):
        events.append(("restart", profile))
        return {"profile": profile, "old_pid": runtime.pid}

    def health(profile, sha, old_pid):
        nonlocal health_attempts
        health_attempts += 1
        events.append(("health", profile))
        return {"ok": False, "profile": profile, "sha": sha}

    result = quiesce_restart_and_verify_fleet(
        _plan("canary", "later"),
        expected_sha="a" * 40,
        config=_config(batch_size=1),
        project_root=Path("."),
        quiesce_profile=quiesce,
        quiesce_worker_probe=lambda: [],
        restart_profile=restart,
        health_gate=health,
    )

    assert result["verified"] is False
    assert result["status"] == "failed"
    assert result["attempted_profiles"] == ["canary"]
    assert health_attempts == 2  # failed gate plus one bounded liveness retry
    assert not any(profile == "later" for _action, profile in events)


def test_live_final_pass_interrupt_recovers_every_proven_stopped_profile():
    class AbortQuiesce(BaseException):
        pass

    live = {"canary": True, "later": True}
    quiesce_attempts: list[str] = []
    restarted: list[str] = []

    def quiesce(profile, runtime):
        quiesce_attempts.append(profile)
        live[profile] = False
        if profile == "later" and quiesce_attempts.count("later") == 1:
            raise AbortQuiesce()
        return {"ok": True, "quiesced": True, "profile": profile}

    def restart(profile, runtime):
        assert live[profile] is False
        live[profile] = True
        restarted.append(profile)
        return {"profile": profile, "old_pid": runtime.pid}

    with pytest.raises(AbortQuiesce):
        quiesce_restart_and_verify_fleet(
            _plan("canary", "later"),
            expected_sha="a" * 40,
            config=_config(),
            project_root=Path("."),
            quiesce_profile=quiesce,
            quiesce_worker_probe=lambda: [],
            restart_profile=restart,
            health_gate=lambda profile, sha, old_pid: {"ok": True},
        )

    assert quiesce_attempts == ["canary", "later", "later"]
    assert restarted == ["canary", "later"]
    assert live == {"canary": True, "later": True}


def test_preapply_quiesce_interrupt_reproves_stop_before_relaunch():
    class AbortAfterStop(BaseException):
        pass

    live = {"canary": True, "later": True}
    quiesce_attempts: list[str] = []
    restarted: list[str] = []
    recovery_receipts: list[dict] = []

    def quiesce(profile, runtime):
        quiesce_attempts.append(profile)
        live[profile] = False
        if profile == "canary" and quiesce_attempts.count(profile) == 1:
            # Model an interrupt delivered after the OS stop completed but
            # before the callback could publish its structured receipt.
            raise AbortAfterStop()
        return {"ok": True, "quiesced": True, "profile": profile}

    def restart(profile, runtime):
        assert live[profile] is False
        live[profile] = True
        restarted.append(profile)
        return {"profile": profile, "old_pid": runtime.pid}

    with pytest.raises(AbortAfterStop):
        quiesce_rollout_fleet_for_update(
            _plan("canary", "later"),
            expected_sha="a" * 40,
            config=_config(),
            project_root=Path("."),
            quiesce_profile=quiesce,
            quiesce_worker_probe=lambda: [],
            restart_profile=restart,
            health_gate=lambda profile, sha, old_pid: {"ok": True},
            recovery_callback=lambda result: recovery_receipts.append(
                dict(result)
            ),
        )

    assert quiesce_attempts == ["canary", "canary"]
    assert restarted == ["canary"]
    assert live == {"canary": True, "later": True}
    assert recovery_receipts[0]["verified"] is True
    assert recovery_receipts[0]["recovery_profiles"] == ["canary"]


def test_rollback_restarts_profiles_quiesced_before_apply():
    state = {"restored": False}
    quiesced: list[str] = []
    restarted_old: list[str] = []

    def restart(profile, runtime):
        if state["restored"]:
            restarted_old.append(profile)
        return {"profile": profile, "old_pid": runtime.pid}

    def health(profile, sha, old_pid):
        if sha == "new":
            raise RuntimeError("canary rejected")
        return {"ok": True, "profile": profile, "sha": sha}

    def rollback(path):
        state["restored"] = True
        return {"restored": True, "sha": "old"}

    with pytest.raises(RolloutExecutionError) as raised:
        run_canary_rollout(
            _plan("canary", "later"),
            expected_sha="new",
            checkpoint=Path("cp"),
            config=_config(),
            project_root=Path("."),
            restart_profile=restart,
            health_gate=health,
            rollback=rollback,
            quiesce_profile=lambda profile, runtime: quiesced.append(profile)
            or _quiesce_ok(profile, runtime),
            quiesce_worker_probe=lambda: [],
            prequiesced_profiles=["canary", "later"],
        )

    assert quiesced == ["canary"]
    assert restarted_old == ["canary", "later"]
    assert raised.value.result["rollback"]["recovery_profiles"] == [
        "canary",
        "later",
    ]


def test_false_health_result_fails_closed_before_next_profile():
    events: list[tuple[str, str]] = []

    def health(profile, sha, old_pid):
        events.append(("health", profile))
        return {"ok": sha == "old", "profile": profile, "sha": sha}

    with pytest.raises(RolloutExecutionError) as raised:
        run_canary_rollout(
            _plan("canary", "later"),
            expected_sha="new",
            checkpoint=Path("cp"),
            config=_config(),
            project_root=Path("."),
            restart_profile=lambda profile, runtime: events.append(
                ("restart", profile)
            )
            or {"profile": profile, "old_pid": runtime.pid},
            health_gate=health,
            rollback=lambda path: {"restored": True, "sha": "old"},
            quiesce_profile=_quiesce_ok,
            quiesce_worker_probe=lambda: [],
        )

    assert events[:2] == [("restart", "canary"), ("health", "canary")]
    assert ("restart", "later") not in events
    assert "did not verify" in raised.value.result["failure"]


def test_quiesce_failure_prevents_restore():
    restore_calls: list[Path] = []
    restarts: list[str] = []

    with pytest.raises(RolloutExecutionError) as raised:
        run_canary_rollout(
            _plan("canary", "later"),
            expected_sha="new",
            checkpoint=Path("cp"),
            config=_config(),
            project_root=Path("."),
            restart_profile=lambda profile, runtime: restarts.append(profile) or {
                "profile": profile,
                "old_pid": runtime.pid,
            },
            health_gate=lambda profile, sha, old_pid: (_ for _ in ()).throw(
                RuntimeError("candidate failed")
            ),
            rollback=lambda path: restore_calls.append(path) or {"restored": True},
            quiesce_profile=lambda profile, runtime: {
                "ok": False,
                "profile": profile,
            },
            quiesce_worker_probe=lambda: [],
            prequiesced_profiles=["canary", "later"],
        )

    assert restore_calls == []
    assert raised.value.result["rollback"]["restore_attempted"] is False
    assert raised.value.result["rollback"]["quiesce"]["ok"] is False
    assert restarts == ["canary", "canary", "later"]
    assert raised.value.result["rollback"]["current_generation_recovery"][
        "recovery_profiles"
    ] == ["canary", "later"]


def test_fleet_quiesce_waits_bounded_for_kanban_workers():
    clock = {"now": 0.0}
    worker_states = iter([[42], [42], []])
    result = quiesce_rollout_fleet(
        _plan("canary", "later"),
        config=_config(),
        quiesce_profile=_quiesce_ok,
        worker_probe=lambda: next(worker_states),
        worker_timeout_seconds=1,
        monotonic=lambda: clock["now"],
        sleep=lambda seconds: clock.__setitem__("now", clock["now"] + seconds),
    )
    assert result["ok"] is True
    assert result["workers"]["ok"] is True
    assert result["workers"]["pids"] == []
    assert result["workers"]["profiles"] == ["canary", "later"]
    assert result["quiesced_profiles"] == ["canary", "later"]


def test_fleet_quiesce_fails_closed_when_worker_probe_errors():
    def fail_probe():
        raise OSError("board database unavailable")

    result = quiesce_rollout_fleet(
        _plan("canary"),
        config=_config(),
        quiesce_profile=_quiesce_ok,
        worker_probe=fail_probe,
    )
    assert result["ok"] is False
    assert result["workers"]["ok"] is False
    assert result["errors"][-1]["profile"] == "kanban-workers"


def test_fleet_quiesce_worker_wait_has_a_hard_timeout():
    clock = {"now": 0.0}
    result = quiesce_rollout_fleet(
        _plan("canary"),
        config=_config(),
        quiesce_profile=_quiesce_ok,
        worker_probe=lambda: [77],
        worker_timeout_seconds=0.2,
        monotonic=lambda: clock["now"],
        sleep=lambda seconds: clock.__setitem__("now", clock["now"] + seconds),
    )
    assert result["ok"] is False
    assert result["workers"]["pids"] == [77]
    assert "timeout" in result["workers"]["error"]


def test_restart_and_verify_fleet_continues_without_a_second_restore():
    events: list[tuple[str, str]] = []

    def health(profile, sha, old_pid):
        events.append(("health", profile))
        return {"ok": profile != "canary", "profile": profile, "sha": sha}

    result = restart_and_verify_fleet(
        _plan("canary", "later"),
        expected_sha="old",
        config=_config(),
        project_root=Path("."),
        restart_profile=lambda profile, runtime: events.append(("restart", profile))
        or {"profile": profile, "old_pid": runtime.pid},
        health_gate=health,
    )

    assert result["verified"] is False
    assert result["attempted_profiles"] == ["canary", "later"]
    assert ("restart", "later") in events
    assert result["errors"][0]["profile"] == "canary"


def test_health_gate_requires_fresh_continuously_stable_identity():
    clock = {"now": 0.0}
    identities = iter(
        [
            {"pid": 10, "start_time": 1, "code_sha": "new"},  # old pid
            {"pid": 20, "start_time": 2, "code_sha": "new"},
            {"pid": 21, "start_time": 3, "code_sha": "new"},  # reset window
            {"pid": 21, "start_time": 3, "code_sha": "new"},
            {"pid": 21, "start_time": 3, "code_sha": "new"},
            {"pid": 21, "start_time": 3, "code_sha": "new"},  # post-smoke
        ]
    )
    latest: dict[str, object] = {}

    def probe(_profile):
        identity = next(identities)
        latest["identity"] = identity
        return identity

    def status_probe(_profile):
        identity = latest["identity"]
        return {
            **identity,
            "answering_pid": identity["pid"],
            "gateway_state": "running",
        }

    smoke_calls: list[str] = []
    result = stable_gateway_health(
        "canary",
        "new",
        previous_pid=10,
        stable_seconds=0.7,
        timeout_seconds=5,
        project_root=Path("."),
        smoke_timeout_seconds=1,
        probe=probe,
        status_probe=status_probe,
        smoke=lambda profile: smoke_calls.append(profile) or {"ok": True},
        monotonic=lambda: clock["now"],
        sleep=lambda seconds: clock.__setitem__("now", clock["now"] + 0.4),
        poll_seconds=0.1,
    )
    assert result["pid"] == 21
    assert smoke_calls == ["canary"]


@pytest.mark.parametrize(
    "post_smoke_identity",
    [
        None,
        {"pid": 22, "start_time": 4, "code_sha": "new"},
    ],
    ids=["gateway-stopped", "gateway-restarted"],
)
def test_health_gate_rejects_identity_lost_during_smoke(post_smoke_identity):
    state = {
        "identity": {"pid": 21, "start_time": 3, "code_sha": "new"}
    }

    def smoke(profile):
        state["identity"] = post_smoke_identity
        return {"ok": True}

    with pytest.raises(RolloutError, match="readiness changed during smoke"):
        stable_gateway_health(
            "canary",
            "new",
            previous_pid=10,
            stable_seconds=0,
            timeout_seconds=1,
            project_root=Path("."),
            smoke_timeout_seconds=1,
            probe=lambda profile: state["identity"],
            status_probe=lambda profile: (
                {
                    **state["identity"],
                    "answering_pid": state["identity"]["pid"],
                    "gateway_state": "running",
                }
                if state["identity"]
                else None
            ),
            smoke=smoke,
            monotonic=lambda: 0.0,
            sleep=lambda seconds: None,
        )


@pytest.mark.parametrize(
    "bad_status",
    [
        {"gateway_state": "starting"},
        {"gateway_state": "startup_failed"},
        {"answering_pid": 22},
    ],
    ids=["starting", "startup-failed", "different-answering-pid"],
)
def test_health_gate_resets_window_until_same_pid_status_is_running(bad_status):
    clock = {"now": 0.0}
    identity = {"pid": 21, "start_time": 3, "code_sha": "new"}
    healthy = {
        **identity,
        "answering_pid": 21,
        "gateway_state": "running",
    }
    statuses = iter(
        [
            healthy,
            {**healthy, **bad_status},
            healthy,
            healthy,
            healthy,
            healthy,  # post-smoke proof
        ]
    )
    smoke_calls: list[str] = []

    result = stable_gateway_health(
        "canary",
        "new",
        previous_pid=10,
        stable_seconds=0.7,
        timeout_seconds=5,
        project_root=Path("."),
        smoke_timeout_seconds=1,
        probe=lambda profile: identity,
        status_probe=lambda profile: next(statuses),
        smoke=lambda profile: smoke_calls.append(profile) or {"ok": True},
        monotonic=lambda: clock["now"],
        sleep=lambda seconds: clock.__setitem__("now", clock["now"] + 0.4),
        poll_seconds=0.1,
    )

    assert result["gateway_state"] == "running"
    assert result["status_answering_pid"] == 21
    assert result["stable_seconds"] >= 0.7
    assert smoke_calls == ["canary"]


def test_health_gate_rejects_non_running_status_after_smoke():
    identity = {"pid": 21, "start_time": 3, "code_sha": "new"}
    state = {"gateway_state": "running"}

    def smoke(_profile):
        state["gateway_state"] = "startup_failed"
        return {"ok": True}

    with pytest.raises(RolloutError, match="readiness changed during smoke"):
        stable_gateway_health(
            "canary",
            "new",
            previous_pid=10,
            stable_seconds=0,
            timeout_seconds=1,
            project_root=Path("."),
            smoke_timeout_seconds=1,
            probe=lambda profile: identity,
            status_probe=lambda profile: {
                **identity,
                "answering_pid": 21,
                "gateway_state": state["gateway_state"],
            },
            smoke=smoke,
            monotonic=lambda: 0.0,
            sleep=lambda seconds: None,
        )

def test_profile_smoke_runs_isolated_real_agent_bootstrap(
    tmp_path: Path, monkeypatch
):
    import hermes_cli.update_rollout as rollout

    project_root = tmp_path / "checkout"
    project_root.mkdir()
    venv = project_root / "venv"
    profile_home = tmp_path / "profiles" / "canary"
    seen: dict[str, object] = {}

    monkeypatch.setattr(
        rollout, "_find_venv", lambda root: (venv, "venv", True)
    )
    monkeypatch.setattr(
        "hermes_cli.profiles.get_profile_dir", lambda profile: profile_home
    )
    monkeypatch.setenv("PYTHONHOME", "/coordinator/python")
    monkeypatch.setenv("PYTHONPATH", "/coordinator/modules")

    def fake_run(command, **kwargs):
        seen["command"] = command
        seen["kwargs"] = kwargs
        return SimpleNamespace(
            returncode=0,
            stdout="hermes-rollout-smoke-ok\n",
            stderr="",
        )

    monkeypatch.setattr(rollout, "_bounded_smoke_run", fake_run)

    result = _profile_smoke(project_root, "canary", 4.5)

    command = seen["command"]
    kwargs = seen["kwargs"]
    assert command[1:3] == ["-I", "-c"]
    probe = command[3]
    for module in _CANARY_SMOKE_MODULES:
        assert repr(module) in probe
    assert "get_all_tool_names" in probe
    assert "get_all_toolsets" in probe
    assert kwargs["cwd"] == project_root
    assert kwargs["timeout"] == 4.5
    assert kwargs["env"]["HERMES_HOME"] == str(profile_home)
    assert "PYTHONHOME" not in kwargs["env"]
    assert "PYTHONPATH" not in kwargs["env"]
    assert result == {
        "ok": True,
        "kind": "agent-bootstrap",
        "mode": "structural",
        "profile": "canary",
        "modules": list(_CANARY_SMOKE_MODULES),
    }


def test_profile_smoke_runs_opt_in_provider_turn_after_structural_checks(
    tmp_path: Path, monkeypatch
):
    import hermes_cli.update_rollout as rollout

    project_root = tmp_path / "checkout"
    project_root.mkdir()
    venv = project_root / "venv"
    profile_home = tmp_path / "profiles" / "canary"
    seen: dict[str, object] = {}
    provider_result = {
        "ok": True,
        "kind": "agent-turn",
        "mode": "provider-turn",
        "profile": "canary",
        "provider": "test-provider",
        "model": "test-model",
        "api_calls": 1,
        "completed": True,
        "response_received": True,
    }

    monkeypatch.setattr(
        rollout, "_find_venv", lambda root: (venv, "venv", True)
    )
    monkeypatch.setattr(
        "hermes_cli.profiles.get_profile_dir", lambda profile: profile_home
    )

    def fake_run(command, **kwargs):
        seen["command"] = command
        return SimpleNamespace(
            returncode=0,
            stdout=(
                f"{_CANARY_PROVIDER_SMOKE_PREFIX}"
                f"{json.dumps(provider_result, sort_keys=True)}\n"
                "hermes-rollout-smoke-ok\n"
            ),
            stderr="",
        )

    monkeypatch.setattr(rollout, "_bounded_smoke_run", fake_run)

    result = _profile_smoke(
        project_root, "canary", 4.5, agent_turn=True
    )

    probe = seen["command"][3]
    assert "get_all_tool_names" in probe
    assert "_provider_smoke_turn('canary')" in probe
    assert probe.index("get_all_toolsets") < probe.index("_provider_smoke_turn")
    assert result["mode"] == "provider-turn"
    assert result["agent_turn"] == provider_result


def test_provider_smoke_turn_uses_profile_runtime_without_persistence(
    monkeypatch,
):
    import run_agent

    captured: dict[str, object] = {}

    class FakeAgent:
        def __init__(self, **kwargs):
            captured["kwargs"] = kwargs

        def run_conversation(self, prompt):
            captured["prompt"] = prompt
            return {
                "completed": True,
                "failed": False,
                "partial": False,
                "api_calls": 1,
                "final_response": "ok",
            }

        def close(self):
            captured["closed"] = True

    monkeypatch.setattr(
        "hermes_cli.config.load_config",
        lambda: {
            "model": {
                "default": {"model": "smoke-model", "provider": "smoke"},
                "provider": "auto",
            }
        },
    )
    runtime_request: dict[str, object] = {}

    def resolve_runtime(**kwargs):
        runtime_request.update(kwargs)
        return {
            "provider": "smoke",
            "requested_provider": "smoke",
            "api_mode": "chat_completions",
            "base_url": "https://provider.invalid/v1",
            "api_key": "secret",
        }

    monkeypatch.setattr(
        "hermes_cli.runtime_provider.resolve_runtime_provider",
        resolve_runtime,
    )
    monkeypatch.setattr(
        "hermes_cli.fallback_config.get_fallback_chain", lambda config: []
    )
    monkeypatch.setattr(run_agent, "AIAgent", FakeAgent)

    result = _provider_smoke_turn("canary")

    kwargs = captured["kwargs"]
    assert kwargs["model"] == "smoke-model"
    assert kwargs["enabled_toolsets"] == []
    assert kwargs["max_iterations"] == 1
    assert kwargs["session_db"] is None
    assert kwargs["skip_context_files"] is True
    assert kwargs["skip_memory"] is True
    assert kwargs["skip_background_review"] is True
    assert runtime_request == {
        "requested": "smoke",
        "target_model": "smoke-model",
    }
    assert captured["closed"] is True
    assert result["mode"] == "provider-turn"
    assert result["provider"] == "smoke"
    assert result["api_calls"] == 1


@pytest.mark.parametrize(
    "turn",
    [
        {"failed": True, "api_calls": 1, "final_response": ""},
        {"partial": True, "api_calls": 1, "final_response": "partial"},
        {"completed": False, "api_calls": 1, "final_response": "interrupted"},
        {"completed": True, "api_calls": 0, "final_response": "cached"},
    ],
    ids=["provider-failure", "partial", "interrupted", "no-provider-call"],
)
def test_provider_smoke_turn_fails_closed(monkeypatch, turn):
    import run_agent

    class FakeAgent:
        def __init__(self, **kwargs):
            pass

        def run_conversation(self, prompt):
            return turn

        def close(self):
            pass

    monkeypatch.setattr(
        "hermes_cli.config.load_config",
        lambda: {"model": {"default": "smoke-model", "provider": "smoke"}},
    )
    monkeypatch.setattr(
        "hermes_cli.runtime_provider.resolve_runtime_provider",
        lambda **kwargs: {"provider": "smoke"},
    )
    monkeypatch.setattr(
        "hermes_cli.fallback_config.get_fallback_chain", lambda config: []
    )
    monkeypatch.setattr(run_agent, "AIAgent", FakeAgent)

    with pytest.raises(RolloutError, match="provider smoke"):
        _provider_smoke_turn("canary")


def test_profile_smoke_fails_gate_when_critical_import_fails(
    tmp_path: Path, monkeypatch
):
    import hermes_cli.update_rollout as rollout

    project_root = tmp_path / "checkout"
    project_root.mkdir()
    monkeypatch.setattr(
        rollout,
        "_find_venv",
        lambda root: (project_root / "venv", "venv", True),
    )
    monkeypatch.setattr(
        "hermes_cli.profiles.get_profile_dir", lambda profile: tmp_path / profile
    )
    monkeypatch.setattr(
        rollout,
        "_bounded_smoke_run",
        lambda *args, **kwargs: SimpleNamespace(
            returncode=1,
            stdout="",
            stderr="RuntimeError: critical module 'run_agent' failed to import\n",
        ),
    )

    with pytest.raises(RolloutError, match="run_agent"):
        _profile_smoke(project_root, "canary", 1)


def test_profile_smoke_rejects_missing_candidate_venv(
    tmp_path: Path, monkeypatch
):
    import hermes_cli.update_rollout as rollout

    project_root = tmp_path / "checkout"
    project_root.mkdir()
    monkeypatch.setattr(
        rollout,
        "_find_venv",
        lambda root: (project_root / "venv", "venv", False),
    )
    monkeypatch.setattr(
        rollout,
        "_bounded_smoke_run",
        lambda *args, **kwargs: pytest.fail(
            "a missing candidate venv must not fall back to the coordinator"
        ),
    )

    with pytest.raises(RolloutError, match="candidate project venv"):
        _profile_smoke(project_root, "canary", 1)


@pytest.mark.parametrize(
    "failure",
    [
        OSError("interpreter is unavailable"),
        subprocess.TimeoutExpired(["python", "-I", "-c"], 1),
    ],
    ids=["spawn-error", "timeout"],
)
def test_profile_smoke_fails_gate_when_child_cannot_complete(
    tmp_path: Path, monkeypatch, failure: BaseException
):
    import hermes_cli.update_rollout as rollout

    project_root = tmp_path / "checkout"
    project_root.mkdir()
    monkeypatch.setattr(
        rollout,
        "_find_venv",
        lambda root: (project_root / "venv", "venv", True),
    )
    monkeypatch.setattr(
        "hermes_cli.profiles.get_profile_dir", lambda profile: tmp_path / profile
    )

    def fail_run(*args, **kwargs):
        raise failure

    monkeypatch.setattr(rollout, "_bounded_smoke_run", fail_run)

    with pytest.raises(RolloutError, match="smoke process could not run"):
        _profile_smoke(project_root, "canary", 1)


def test_opt_in_provider_smoke_surfaces_auth_failure(tmp_path: Path, monkeypatch):
    import hermes_cli.update_rollout as rollout

    project_root = tmp_path / "checkout"
    project_root.mkdir()
    monkeypatch.setattr(
        rollout,
        "_find_venv",
        lambda root: (project_root / "venv", "venv", True),
    )
    monkeypatch.setattr(
        "hermes_cli.profiles.get_profile_dir", lambda profile: tmp_path / profile
    )
    monkeypatch.setattr(
        rollout,
        "_bounded_smoke_run",
        lambda *args, **kwargs: SimpleNamespace(
            returncode=1,
            stdout="",
            stderr="AuthError: profile credential was rejected\n",
        ),
    )

    with pytest.raises(RolloutError, match="credential was rejected"):
        _profile_smoke(project_root, "canary", 5, agent_turn=True)


def test_opt_in_provider_smoke_timeout_is_bounded(tmp_path: Path, monkeypatch):
    import hermes_cli.update_rollout as rollout

    project_root = tmp_path / "checkout"
    project_root.mkdir()
    monkeypatch.setattr(
        rollout,
        "_find_venv",
        lambda root: (project_root / "venv", "venv", True),
    )
    monkeypatch.setattr(
        "hermes_cli.profiles.get_profile_dir", lambda profile: tmp_path / profile
    )

    def timeout(*args, **kwargs):
        raise subprocess.TimeoutExpired(args[0], kwargs["timeout"])

    monkeypatch.setattr(rollout, "_bounded_smoke_run", timeout)

    with pytest.raises(RolloutError, match="TimeoutExpired"):
        _profile_smoke(project_root, "canary", 5, agent_turn=True)


@pytest.mark.live_system_guard_bypass
def test_smoke_child_timeout_kills_the_real_process_tree(tmp_path: Path):
    started = time.monotonic()
    with pytest.raises(subprocess.TimeoutExpired):
        _bounded_smoke_run(
            [sys.executable, "-c", "import time; time.sleep(300)"],
            cwd=tmp_path,
            env=os.environ,
            timeout=1.0,
        )
    assert time.monotonic() - started < 30


def test_gateway_confirmation_requires_matching_id_and_correlation(
    tmp_path: Path, monkeypatch
):
    import hermes_cli.update_cmd as update_cmd

    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setattr("hermes_constants.get_hermes_home", lambda: home)
    state = {"now": 0.0, "writes": 0}

    def sleep(seconds):
        state["now"] += seconds
        prompt = json.loads((home / ".update_prompt.json").read_text(encoding="utf-8"))
        state["writes"] += 1
        if state["writes"] == 1:
            response = {
                "id": "stale-id",
                "correlation_id": "corr",
                "answer": "yes",
            }
        else:
            response = {
                "id": prompt["id"],
                "correlation_id": "corr",
                "answer": "yes",
            }
        (home / ".update_response").write_text(json.dumps(response), encoding="utf-8")

    fake_time = SimpleNamespace(monotonic=lambda: state["now"], sleep=sleep)
    monkeypatch.setattr(update_cmd, "_time", fake_time)
    answer = update_cmd._gateway_prompt(
        "Proceed?",
        "n",
        timeout=5,
        kind="update_confirmation",
        context={"correlation_id": "corr"},
    )
    assert answer == "yes"
    assert state["writes"] == 2  # stale approval was ignored


@pytest.mark.parametrize("supervisor", ["systemd", "launchd", "manual"])
def test_verified_tauri_parent_never_rehands_to_gateway_supervisor(
    tmp_path: Path, monkeypatch, supervisor: str
):
    import hermes_cli.update_cmd as update_cmd

    _configure_tauri_parent_proof(tmp_path, monkeypatch)
    identify_calls: list[str] = []
    monkeypatch.setattr(
        update_cmd,
        "_verified_independent_windows_worker",
        lambda: False,
    )
    monkeypatch.setattr(
        "gateway.control_socket.identify_gateway",
        lambda home, timeout=1: (
            identify_calls.append(supervisor) or {"supervisor": supervisor}
        ),
    )
    monkeypatch.setattr(
        update_cmd.subprocess,
        "run",
        lambda *args, **kwargs: pytest.fail(
            f"verified Tauri parent must not launch {supervisor} worker"
        ),
    )

    assert update_cmd._handoff_gateway_rollout_worker_if_needed(_config()) is False
    assert identify_calls == []


@pytest.mark.parametrize(
    ("spoof", "message"),
    [
        ("ready_path", "outside the control profile"),
        ("outcome_path", "outside the control profile"),
        ("parent_pid", "does not name the direct parent"),
        ("marker_owner", "does not own the live update marker"),
        ("missing_outcome", "is incomplete"),
    ],
)
def test_tauri_parent_spoof_evidence_fails_closed(
    tmp_path: Path, monkeypatch, spoof: str, message: str
):
    import hermes_cli.update_cmd as update_cmd

    proof = _configure_tauri_parent_proof(tmp_path, monkeypatch)
    if spoof == "ready_path":
        monkeypatch.setenv(
            "HERMES_UPDATE_TAURI_READY_PATH",
            str(tmp_path / "outside-ready"),
        )
    elif spoof == "outcome_path":
        monkeypatch.setenv(
            "HERMES_UPDATE_TAURI_OUTCOME_PATH",
            str(tmp_path / "outside-outcome"),
        )
    elif spoof == "parent_pid":
        monkeypatch.setenv("HERMES_UPDATE_HANDOFF_PID", str(os.getpid()))
    elif spoof == "marker_owner":
        proof.marker.write_text(
            f"{os.getpid()}\n{int(time.time())}\n",
            encoding="utf-8",
        )
    elif spoof == "missing_outcome":
        monkeypatch.delenv("HERMES_UPDATE_TAURI_OUTCOME_PATH")

    monkeypatch.setattr(
        update_cmd,
        "_verified_independent_windows_worker",
        lambda: False,
    )
    with pytest.raises(RuntimeError, match=message):
        update_cmd._handoff_gateway_rollout_worker_if_needed(_config())


def test_malformed_handoff_correlation_is_rejected_before_path_construction(
    tmp_path: Path, monkeypatch
):
    import hermes_cli.update_cmd as update_cmd

    home = tmp_path / "control-home"
    home.mkdir()
    monkeypatch.setattr(update_cmd, "get_hermes_home", lambda: home)
    monkeypatch.setenv("HERMES_UPDATE_CORRELATION_ID", "../../escape")
    monkeypatch.setattr(
        "gateway.control_socket.identify_gateway",
        lambda *args, **kwargs: pytest.fail(
            "malformed correlation must fail before supervisor discovery"
        ),
    )

    with pytest.raises(RuntimeError, match="correlation id is malformed"):
        update_cmd._handoff_gateway_rollout_worker_if_needed(_config())
    assert list(home.iterdir()) == []


@pytest.mark.linux_only
def test_systemd_gateway_canary_uses_verified_independent_worker(
    tmp_path: Path, monkeypatch
):
    import hermes_cli.update_cmd as update_cmd

    home = tmp_path / "profile"
    home.mkdir()
    project = tmp_path / "project"
    project.mkdir()
    calls: list[list[str]] = []
    monkeypatch.delenv("HERMES_UPDATE_CORRELATION_ID", raising=False)
    monkeypatch.setattr(update_cmd, "get_hermes_home", lambda: home)
    monkeypatch.setattr(update_cmd._m(), "PROJECT_ROOT", project)
    monkeypatch.setattr(update_cmd, "_verified_independent_update_unit", lambda: False)
    monkeypatch.setenv(
        "HERMES_UPDATE_OUTPUT_PATH", str(home / ".update_output.txt")
    )
    monkeypatch.setattr(
        "gateway.control_socket.identify_gateway",
        lambda home, timeout=1: {"supervisor": "systemd"},
    )
    monkeypatch.setattr(sys, "argv", ["hermes", "update", "--gateway"])

    def run(command, **kwargs):
        calls.append(list(command))
        if command[0] == "systemd-run":
            env_values = {
                item.removeprefix("--setenv=").split("=", 1)[0]: item.split(
                    "=", 2
                )[2]
                for item in command
                if item.startswith("--setenv=")
            }
            ready = Path(env_values["HERMES_UPDATE_WORKER_READY"])
            stage = ready.with_name(f"{ready.name}.test.tmp")
            stage.write_text(
                env_values["HERMES_UPDATE_CORRELATION_ID"], encoding="utf-8"
            )
            stage.replace(ready)
            return SimpleNamespace(returncode=0, stdout="", stderr="")
        return SimpleNamespace(returncode=0, stdout="active\n", stderr="")

    monkeypatch.setattr(update_cmd.subprocess, "run", run)
    assert update_cmd._handoff_gateway_rollout_worker_if_needed(_config()) is True
    launch = calls[0]
    assert launch[0] == "systemd-run"
    assert any("HERMES_UPDATE_INDEPENDENT_UNIT=" in arg for arg in launch)
    assert (
        f"--property=StandardOutput=append:{home / '.update_output.txt'}"
        in launch
    )
    assert (
        f"--property=StandardError=append:{home / '.update_output.txt'}"
        in launch
    )
    assert launch[-2:] == ["update", "--gateway"]
    assert not (project / "venv").exists()  # lifecycle handoff is pre-mutation


@pytest.mark.windows_only
def test_windows_gateway_canary_accepts_only_correlated_job_breakaway(
    tmp_path: Path, monkeypatch
):
    import hermes_cli.update_cmd as update_cmd

    home = tmp_path / "profile"
    home.mkdir()
    monkeypatch.setattr(update_cmd, "get_hermes_home", lambda: home)
    monkeypatch.setattr(update_cmd, "_verified_independent_update_unit", lambda: False)
    monkeypatch.setattr(
        update_cmd, "_verified_independent_launchd_worker", lambda: False
    )
    monkeypatch.setattr(update_cmd, "_windows_process_outside_job", lambda: True)
    monkeypatch.setenv("HERMES_UPDATE_CORRELATION_ID", "corr-1")
    monkeypatch.setenv("HERMES_UPDATE_WINDOWS_DETACHED", "corr-1")
    monkeypatch.setattr(
        "gateway.control_socket.identify_gateway",
        lambda home, timeout=1: {"supervisor": "manual"},
    )

    assert update_cmd._handoff_gateway_rollout_worker_if_needed(_config()) is False

    monkeypatch.setenv("HERMES_UPDATE_WINDOWS_DETACHED", "different")
    with pytest.raises(RuntimeError, match="job breakaway"):
        update_cmd._handoff_gateway_rollout_worker_if_needed(_config())


@pytest.mark.macos_only
def test_launchd_gateway_canary_uses_pid_verified_transient_worker(
    tmp_path: Path, monkeypatch
):
    import hermes_cli.update_cmd as update_cmd

    home = tmp_path / "profile"
    home.mkdir()
    project = tmp_path / "project"
    project.mkdir()
    calls: list[list[str]] = []
    correlation = "launchd-correlation"
    monkeypatch.setattr(update_cmd, "get_hermes_home", lambda: home)
    monkeypatch.setattr(update_cmd._m(), "PROJECT_ROOT", project)
    monkeypatch.setattr(update_cmd, "_verified_independent_update_unit", lambda: False)
    monkeypatch.setattr(
        update_cmd, "_verified_independent_launchd_worker", lambda: False
    )
    monkeypatch.setattr(
        update_cmd, "_verified_independent_windows_worker", lambda: False
    )
    monkeypatch.setenv("HERMES_UPDATE_CORRELATION_ID", correlation)
    monkeypatch.setenv(
        "HERMES_UPDATE_OUTPUT_PATH", str(home / ".update_output.txt")
    )
    monkeypatch.setattr(
        "gateway.control_socket.identify_gateway",
        lambda home, timeout=1: {"supervisor": "launchd"},
    )
    monkeypatch.setattr(sys, "argv", ["hermes", "update", "--gateway"])

    def run(command, **kwargs):
        calls.append(list(command))
        if command[:2] == ["launchctl", "submit"]:
            ready = home / f".update_worker_ready.{correlation}"
            ready.write_text(f"{correlation}\n4242", encoding="utf-8")
            return SimpleNamespace(returncode=0, stdout="", stderr="")
        if command[:2] == ["launchctl", "print"]:
            return SimpleNamespace(
                returncode=0, stdout="state = running\n\tpid = 4242\n", stderr=""
            )
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(update_cmd.subprocess, "run", run)

    assert update_cmd._handoff_gateway_rollout_worker_if_needed(_config()) is True
    submitted = calls[0]
    assert submitted[:4] == [
        "launchctl",
        "submit",
        "-l",
        "ai.hermes.update.launchdcorrelation",
    ]
    assert submitted[-2:] == ["update", "--gateway"]
    worker_index = submitted.index("-c") + 1
    compile(submitted[worker_index], "<launchd-update-worker>", "exec")
    assert submitted[worker_index + 2] == "ai.hermes.update.launchdcorrelation"
    assert submitted[4:8] == [
        "-o",
        str(home / ".update_output.txt"),
        "-e",
        str(home / ".update_output.txt"),
    ]
    assert not list(home.glob(".update_worker_*"))


@pytest.mark.parametrize(("exit_code", "expected_marker"), [(None, "0"), (7, "7")])
@pytest.mark.live_system_guard_bypass
def test_launchd_worker_always_exits_cleanly_with_truthful_marker(
    tmp_path: Path, exit_code: int | None, expected_marker: str
):
    import hermes_cli.update_cmd as update_cmd

    project = tmp_path / "project"
    package = project / "hermes_cli"
    package.mkdir(parents=True)
    (package / "__init__.py").write_text("", encoding="utf-8")
    outcome = "return None" if exit_code is None else f"raise SystemExit({exit_code})"
    (package / "main.py").write_text(
        f"def main():\n    {outcome}\n",
        encoding="utf-8",
    )
    home = tmp_path / "profile"
    home.mkdir()
    correlation = f"launchd-{expected_marker}"
    ready = home / f".update_worker_ready.{correlation}"
    env_path = home / f".update_worker_env.{correlation}"
    env_path.write_text(
        json.dumps(
            {
                "HERMES_HOME": str(home),
                "HERMES_UPDATE_CORRELATION_ID": correlation,
                "HERMES_UPDATE_WORKER_READY": str(ready),
                "HERMES_UPDATE_WORKER_DELAY": "0",
                "HERMES_UPDATE_WORKING_DIRECTORY": str(project),
            }
        ),
        encoding="utf-8",
    )

    completed = subprocess.run(
        [
            sys.executable,
            "-c",
            update_cmd._launchd_rollout_worker_source(),
            str(env_path),
            f"ai.hermes.update.{correlation.replace('-', '')[:24]}",
            "update",
        ],
        cwd=project,
        env={"PATH": os.environ.get("PATH", ""), "PYTHONPATH": str(project)},
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
    )

    assert completed.returncode == 0
    assert (
        home / f".update_exit_code.{correlation}"
    ).read_text(encoding="utf-8") == expected_marker
    assert ready.read_text(encoding="utf-8").splitlines()[0] == correlation
    assert not env_path.exists()


@pytest.mark.macos_only
@pytest.mark.live_system_guard_bypass
def test_launchd_worker_removes_exact_transient_label_after_terminal_status(
    tmp_path: Path,
):
    import hermes_cli.update_cmd as update_cmd

    project = tmp_path / "project"
    package = project / "hermes_cli"
    package.mkdir(parents=True)
    (package / "__init__.py").write_text("", encoding="utf-8")
    (package / "main.py").write_text(
        "def main():\n    return None\n",
        encoding="utf-8",
    )
    home = tmp_path / "profile"
    home.mkdir()
    correlation = "12345678-1234-5678-9234-567812345678"
    label = "ai.hermes.update.123456781234567892345678"
    ready = home / f".update_worker_ready.{correlation}"
    env_path = home / f".update_worker_env.{correlation}"
    env_path.write_text(
        json.dumps(
            {
                "HERMES_HOME": str(home),
                "HERMES_UPDATE_CORRELATION_ID": correlation,
                "HERMES_UPDATE_WORKER_READY": str(ready),
                "HERMES_UPDATE_WORKER_DELAY": "0",
                "HERMES_UPDATE_WORKING_DIRECTORY": str(project),
            }
        ),
        encoding="utf-8",
    )
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    record = tmp_path / "launchctl-record"
    launchctl = fake_bin / "launchctl"
    launchctl.write_text(
        '#!/bin/sh\nprintf "%s\\n" "$*" >> "$LAUNCHCTL_RECORD"\n',
        encoding="utf-8",
    )
    launchctl.chmod(0o755)

    completed = subprocess.run(
        [
            sys.executable,
            "-c",
            update_cmd._launchd_rollout_worker_source(),
            str(env_path),
            label,
            "update",
        ],
        cwd=project,
        env={
            "PATH": f"{fake_bin}{os.pathsep}{os.environ.get('PATH', '')}",
            "PYTHONPATH": str(project),
            "LAUNCHCTL_RECORD": str(record),
        },
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
    )

    assert completed.returncode == 0
    assert (
        home / f".update_exit_code.{correlation}"
    ).read_text(encoding="utf-8") == "0"
    assert record.read_text(encoding="utf-8").splitlines() == [f"remove {label}"]


@pytest.mark.live_system_guard_bypass
def test_launchd_worker_missing_environment_stops_keepalive_retry(
    tmp_path: Path,
):
    import hermes_cli.update_cmd as update_cmd

    home = tmp_path / "profile"
    home.mkdir()
    env_path = home / ".update_worker_env.missing-correlation"

    completed = subprocess.run(
        [
            sys.executable,
            "-c",
            update_cmd._launchd_rollout_worker_source(),
            str(env_path),
            "ai.hermes.update.missingcorrelation",
            "update",
        ],
        cwd=tmp_path,
        env={"PATH": os.environ.get("PATH", "")},
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
    )

    assert completed.returncode == 0
    assert (
        home / ".update_exit_code.missing-correlation"
    ).read_text(encoding="utf-8") == "1"
    assert not list(home.glob(".update_worker_ready.*"))


def test_launchd_worker_environment_excludes_provider_credentials(
    tmp_path: Path, monkeypatch
):
    import hermes_cli.update_cmd as update_cmd

    monkeypatch.setenv("OPENAI_API_KEY", "must-not-persist")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "must-not-persist")
    monkeypatch.setenv("PATH", "/safe/bin")
    worker_env = update_cmd._launchd_rollout_worker_environment(
        home=tmp_path,
        label="ai.hermes.update.test",
        ready_path=tmp_path / ".ready",
        correlation_id="corr",
    )

    assert worker_env["PATH"] == "/safe/bin"
    assert worker_env["HERMES_HOME"] == str(tmp_path)
    assert "OPENAI_API_KEY" not in worker_env
    assert "ANTHROPIC_API_KEY" not in worker_env


@pytest.mark.macos_only
def test_launchd_handoff_failure_removes_transient_worker(
    tmp_path: Path, monkeypatch
):
    import hermes_cli.update_cmd as update_cmd

    home = tmp_path / "profile"
    home.mkdir()
    calls: list[list[str]] = []
    clock = {"now": 0.0}
    monkeypatch.setattr(update_cmd, "get_hermes_home", lambda: home)
    monkeypatch.setattr(update_cmd._m(), "PROJECT_ROOT", tmp_path)
    monkeypatch.setattr(update_cmd, "_verified_independent_update_unit", lambda: False)
    monkeypatch.setattr(
        update_cmd, "_verified_independent_launchd_worker", lambda: False
    )
    monkeypatch.setattr(
        update_cmd, "_verified_independent_windows_worker", lambda: False
    )
    monkeypatch.setattr(
        "gateway.control_socket.identify_gateway",
        lambda home, timeout=1: {"supervisor": "launchd"},
    )
    monkeypatch.setattr(
        update_cmd,
        "_time",
        SimpleNamespace(
            monotonic=lambda: clock["now"],
            sleep=lambda seconds: clock.__setitem__("now", clock["now"] + 1),
        ),
    )

    def run(command, **kwargs):
        calls.append(list(command))
        if command[:2] == ["launchctl", "submit"]:
            return SimpleNamespace(returncode=0, stdout="", stderr="")
        if command[:2] == ["launchctl", "bootout"]:
            return SimpleNamespace(returncode=0, stdout="", stderr="")
        return SimpleNamespace(returncode=0, stdout="not running", stderr="")

    monkeypatch.setattr(update_cmd.subprocess, "run", run)

    with pytest.raises(RuntimeError, match="did not acknowledge readiness"):
        update_cmd._handoff_gateway_rollout_worker_if_needed(_config())

    assert any(command[:2] == ["launchctl", "bootout"] for command in calls)
    assert not list(home.glob(".update_worker_*"))


@pytest.mark.macos_only
def test_launchd_independent_worker_proof_requires_own_job_pid(monkeypatch):
    import hermes_cli.update_cmd as update_cmd

    label = "ai.hermes.update.abc123"
    monkeypatch.setenv("HERMES_UPDATE_INDEPENDENT_LAUNCHD", label)
    monkeypatch.setattr(update_cmd.os, "getuid", lambda: 501)
    monkeypatch.setattr(
        update_cmd.subprocess,
        "run",
        lambda *args, **kwargs: SimpleNamespace(
            returncode=0,
            stdout=f"pid = {update_cmd.os.getpid()}",
            stderr="",
        ),
    )
    assert update_cmd._verified_independent_launchd_worker() is True

    monkeypatch.setattr(
        update_cmd.subprocess,
        "run",
        lambda *args, **kwargs: SimpleNamespace(
            returncode=0,
            stdout=f"pid = {update_cmd.os.getpid()}0",
            stderr="",
        ),
    )
    assert update_cmd._verified_independent_launchd_worker() is False

    monkeypatch.setattr(
        update_cmd.subprocess,
        "run",
        lambda *args, **kwargs: SimpleNamespace(
            returncode=0, stdout="pid = 999999", stderr=""
        ),
    )
    assert update_cmd._verified_independent_launchd_worker() is False


@pytest.mark.linux_only
def test_systemd_gateway_canary_rejects_unscoped_output_path(
    tmp_path: Path, monkeypatch
):
    import hermes_cli.update_cmd as update_cmd

    home = tmp_path / "profile"
    home.mkdir()
    project = tmp_path / "project"
    project.mkdir()
    monkeypatch.setattr(update_cmd, "get_hermes_home", lambda: home)
    monkeypatch.setattr(update_cmd._m(), "PROJECT_ROOT", project)
    monkeypatch.setattr(update_cmd, "_verified_independent_update_unit", lambda: False)
    monkeypatch.setattr(
        "gateway.control_socket.identify_gateway",
        lambda home, timeout=1: {"supervisor": "systemd"},
    )
    monkeypatch.setenv(
        "HERMES_UPDATE_OUTPUT_PATH", str(tmp_path / "foreign" / "output.txt")
    )
    monkeypatch.setattr(
        update_cmd.subprocess,
        "run",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            AssertionError("systemd-run must not execute")
        ),
    )

    with pytest.raises(RuntimeError, match="outside the control profile"):
        update_cmd._handoff_gateway_rollout_worker_if_needed(_config())


@pytest.mark.linux_only
def test_systemd_active_without_worker_handshake_is_stopped(
    tmp_path: Path, monkeypatch
):
    import hermes_cli.update_cmd as update_cmd

    home = tmp_path / "profile"
    home.mkdir()
    project = tmp_path / "project"
    project.mkdir()
    calls: list[list[str]] = []
    clock = {"now": 0.0}
    monkeypatch.delenv("HERMES_UPDATE_CORRELATION_ID", raising=False)
    monkeypatch.setattr(update_cmd, "get_hermes_home", lambda: home)
    monkeypatch.setattr(update_cmd._m(), "PROJECT_ROOT", project)
    monkeypatch.setattr(update_cmd, "_verified_independent_update_unit", lambda: False)
    monkeypatch.setattr(
        "gateway.control_socket.identify_gateway",
        lambda home, timeout=1: {"supervisor": "systemd"},
    )
    monkeypatch.setattr(sys, "argv", ["hermes", "update", "--gateway"])
    monkeypatch.setattr(
        update_cmd,
        "_time",
        SimpleNamespace(
            monotonic=lambda: clock["now"],
            sleep=lambda seconds: clock.__setitem__(
                "now", clock["now"] + seconds
            ),
        ),
    )

    def run(command, **kwargs):
        calls.append(list(command))
        if command[0] == "systemd-run":
            return SimpleNamespace(returncode=0, stdout="", stderr="")
        if "show" in command:
            return SimpleNamespace(returncode=0, stdout="active\n", stderr="")
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(update_cmd.subprocess, "run", run)
    with pytest.raises(RuntimeError, match="did not acknowledge readiness"):
        update_cmd._handoff_gateway_rollout_worker_if_needed(_config())

    assert any(command[:3] == ["systemctl", "--user", "stop"] for command in calls)
    assert not list(home.glob(".update_worker_ready.*"))


@pytest.mark.parametrize(
    "failure",
    [subprocess.TimeoutExpired("systemctl show", 5), KeyboardInterrupt()],
)
@pytest.mark.linux_only
def test_systemd_postlaunch_handshake_failure_always_stops_unit(
    tmp_path: Path, monkeypatch, failure
):
    import hermes_cli.update_cmd as update_cmd

    home = tmp_path / "profile"
    home.mkdir()
    calls: list[list[str]] = []
    monkeypatch.setattr(update_cmd, "get_hermes_home", lambda: home)
    monkeypatch.setattr(update_cmd._m(), "PROJECT_ROOT", tmp_path)
    monkeypatch.setattr(update_cmd, "_verified_independent_update_unit", lambda: False)
    monkeypatch.setattr(
        "gateway.control_socket.identify_gateway",
        lambda home, timeout=1: {"supervisor": "systemd"},
    )

    def run(command, **kwargs):
        calls.append(list(command))
        if command[0] == "systemd-run":
            return SimpleNamespace(returncode=0, stdout="", stderr="")
        if "show" in command:
            raise failure
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(update_cmd.subprocess, "run", run)
    with pytest.raises(type(failure)):
        update_cmd._handoff_gateway_rollout_worker_if_needed(_config())

    assert any(command[:3] == ["systemctl", "--user", "stop"] for command in calls)


@pytest.mark.linux_only
def test_systemd_handshake_does_not_combine_stale_active_with_late_ready(
    tmp_path: Path, monkeypatch
):
    import hermes_cli.update_cmd as update_cmd

    home = tmp_path / "profile"
    home.mkdir()
    calls: list[list[str]] = []
    clock = {"now": 0.0, "shows": 0, "correlation": ""}
    monkeypatch.setattr(update_cmd, "get_hermes_home", lambda: home)
    monkeypatch.setattr(update_cmd._m(), "PROJECT_ROOT", tmp_path)
    monkeypatch.setattr(update_cmd, "_verified_independent_update_unit", lambda: False)
    monkeypatch.setattr(
        "gateway.control_socket.identify_gateway",
        lambda home, timeout=1: {"supervisor": "systemd"},
    )

    def sleep(seconds):
        if clock["shows"] == 1:
            ready = next(home.glob(".update_worker_ready.*"))
            ready.write_text(clock["correlation"], encoding="utf-8")
        clock["now"] += 1.0

    monkeypatch.setattr(
        update_cmd,
        "_time",
        SimpleNamespace(monotonic=lambda: clock["now"], sleep=sleep),
    )

    def run(command, **kwargs):
        calls.append(list(command))
        if command[0] == "systemd-run":
            for item in command:
                if item.startswith("--setenv=HERMES_UPDATE_CORRELATION_ID="):
                    clock["correlation"] = item.rsplit("=", 1)[1]
            return SimpleNamespace(returncode=0, stdout="", stderr="")
        if "show" in command:
            clock["shows"] += 1
            state = "active\n" if clock["shows"] == 1 else "failed\n"
            return SimpleNamespace(returncode=0, stdout=state, stderr="")
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(update_cmd.subprocess, "run", run)
    with pytest.raises(RuntimeError, match="did not acknowledge readiness"):
        update_cmd._handoff_gateway_rollout_worker_if_needed(_config())

    assert any(command[:3] == ["systemctl", "--user", "stop"] for command in calls)


def test_gateway_handoff_parent_exits_nonterminal_before_mutation(
    tmp_path: Path, monkeypatch
):
    import hermes_cli.update_cmd as update_cmd
    import hermes_cli.update_rollout as rollout

    home = tmp_path / "profile"
    home.mkdir()
    project = tmp_path / "project"
    project.mkdir()
    monkeypatch.setattr(update_cmd, "get_hermes_home", lambda: home)
    monkeypatch.setattr(update_cmd._m(), "PROJECT_ROOT", project)
    monkeypatch.setattr(rollout, "load_rollout_config", lambda config=None: _config())
    monkeypatch.setattr(
        update_cmd, "_handoff_gateway_rollout_worker_if_needed", lambda config: True
    )

    def mutation_started():
        pytest.fail("handoff parent reached update planning/mutation")

    monkeypatch.setattr(
        update_cmd._m(), "_capture_active_lazy_features", mutation_started
    )
    args = SimpleNamespace(rollback=None)

    with pytest.raises(SystemExit) as raised:
        update_cmd._cmd_update_impl(args, gateway_mode=True)

    assert raised.value.code == update_cmd.UPDATE_EXIT_INDEPENDENT_HANDOFF
    assert raised.value.code != 0
    assert not (home / ".update_exit_code").exists()
    assert list(project.iterdir()) == []


def test_rollout_engine_never_publishes_a_terminal_marker_while_gating(
    tmp_path: Path, monkeypatch
):
    marker = tmp_path / ".update_exit_code"
    observations: list[bool] = []

    def health(profile, sha, old_pid):
        observations.append(marker.exists())
        return {"ok": True, "profile": profile, "sha": sha, "pid": old_pid + 1}

    result = run_canary_rollout(
        _plan("canary", "later"),
        expected_sha="new",
        checkpoint=Path("cp"),
        config=_config(),
        project_root=Path("."),
        restart_profile=lambda profile, runtime: {
            "profile": profile,
            "old_pid": runtime.pid,
        },
        health_gate=health,
    )

    assert result["status"] == "healthy"
    assert observations == [False, False]
    assert not marker.exists()


def test_explicit_rollback_bypasses_candidate_managed_admission(monkeypatch):
    import hermes_cli.config as cli_config
    import hermes_cli.main as cli_main
    import hermes_cli.update_lock as update_lock

    calls: list[tuple[str, bool]] = []

    class FakeLock:
        holder = None

        def acquire(self):
            return True

        def release(self):
            calls.append(("release", False))

    monkeypatch.setattr(cli_config, "is_managed", lambda: True)
    monkeypatch.setattr(cli_config, "detect_install_method", lambda root=None: "docker")
    monkeypatch.setattr(update_lock, "UpdateLock", FakeLock)
    monkeypatch.setattr(
        cli_main, "_install_hangup_protection", lambda gateway_mode: {}
    )
    monkeypatch.setattr(cli_main, "_finalize_update_output", lambda state: None)
    monkeypatch.setattr(
        cli_main,
        "_cmd_update_impl",
        lambda args, gateway_mode: calls.append((args.rollback, gateway_mode)),
    )

    cli_main.cmd_update(
        SimpleNamespace(
            rollback="checkpoint-1",
            plan=False,
            check=False,
            gateway=False,
        )
    )

    assert calls[0] == ("checkpoint-1", False)
    assert calls[-1] == ("release", False)


def test_transaction_receipt_has_stable_fields(tmp_path: Path, monkeypatch):
    import hermes_cli.update_receipt as receipt

    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setattr("hermes_cli.config.get_hermes_home", lambda: home)
    receipt._current = None
    receipt.begin_update_receipt()
    receipt.record_update_context(
        "corr-1", origin_profile="work", install_id="install-1"
    )
    receipt.record_checkpoint(id="cp-1", pre_sha="a" * 40, status="ready")
    receipt.record_canary(status="healthy", canary_profile="work")
    receipt.record_rollback(attempted=False, verified=False)
    path = receipt.finalize_update_receipt("success")
    payload = json.loads(path.read_text(encoding="utf-8"))
    assert payload["correlation_id"] == "corr-1"
    assert payload["origin"]["origin_profile"] == "work"
    assert payload["checkpoint"]["id"] == "cp-1"
    assert payload["canary"]["status"] == "healthy"
    assert payload["rollback"]["attempted"] is False
