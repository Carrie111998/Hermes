"""Production endpoint and owner-boundary tests for execution write scope."""

import json
import shutil
import subprocess
import sys
import tarfile

import pytest

import tools.terminal_tool as terminal_tool


def _docker_runtime_blocker():
    docker = shutil.which("docker")
    if docker is None:
        return "docker executable unavailable"
    try:
        result = subprocess.run(
            [docker, "version"],
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return f"docker version probe failed: {exc}"
    if result.returncode != 0:
        detail = (result.stderr or result.stdout or "no diagnostic").strip()
        return f"docker version exited {result.returncode}: {detail[:240]}"
    return None


_DOCKER_OWNER_PROOF_BLOCKER = _docker_runtime_blocker()


def test_workspace_local_rejects_issue_cd_write_before_spawn(monkeypatch, tmp_path):
    workspace = tmp_path / "workspace"
    outside = tmp_path / "outside"
    workspace.mkdir()
    outside.mkdir()
    sentinel = outside / "sentinel.txt"
    spawned = []

    monkeypatch.setattr(
        terminal_tool,
        "_get_env_config",
        lambda: {
            "env_type": "local",
            "execution_write_scope": "workspace",
            "cwd": str(workspace),
            "timeout": 30,
        },
    )
    monkeypatch.setattr(terminal_tool, "_start_cleanup_thread", lambda: None)
    monkeypatch.setattr(terminal_tool, "_check_all_guards", lambda *args, **kwargs: {"approved": True})
    monkeypatch.setattr(
        terminal_tool,
        "_create_environment",
        lambda *args, **kwargs: spawned.append(True),
    )

    result = json.loads(
        terminal_tool.terminal_tool(
            f"cd {outside} && {sys.executable} -c \"open(r'{sentinel}', 'wb').write(b'x')\"",
            task_id="issue-shaped-local",
            force=True,
        )
    )

    assert result["status"] == "blocked"
    assert result["error_code"] == "unsupported_execution_backend"
    assert not spawned
    assert not sentinel.exists()


def test_private_docker_output_is_not_published(tmp_path):
    from tools.environments.file_sync import FileSyncManager

    workspace = tmp_path / "workspace"
    workspace.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    sentinel = outside / "sentinel.txt"
    sentinel.write_text("host", encoding="utf-8")
    remote_input = workspace / "input.txt"
    remote_input.write_text("input", encoding="utf-8")

    def write_private_output_tar(path):
        with tarfile.open(path, "w") as archive:
            private_output = tmp_path / "container-private-output.txt"
            private_output.write_text("private", encoding="utf-8")
            archive.add(private_output, arcname="private/output.txt")

    manager = FileSyncManager(
        get_files_fn=lambda: [(str(remote_input), "/workspace/input.txt")],
        upload_fn=lambda *_args: None,
        delete_fn=lambda *_args: None,
        bulk_download_fn=write_private_output_tar,
    )
    manager._synced_files["/workspace/input.txt"] = (0.0, 0)

    manager.sync_back(hermes_home=tmp_path / "hermes-home")

    assert sentinel.read_text(encoding="utf-8") == "host"
    assert not (tmp_path / "private" / "output.txt").exists()


@pytest.mark.skipif(
    _DOCKER_OWNER_PROOF_BLOCKER is not None,
    reason=f"Docker owner proof blocked: {_DOCKER_OWNER_PROOF_BLOCKER}",
)
def test_docker_workspace_scope_covers_dynamic_descendants(tmp_path, monkeypatch):
    workspace = tmp_path / "workspace"
    outside = tmp_path / "outside"
    workspace.mkdir()
    outside.mkdir()
    sentinel = outside / "sentinel.txt"
    sentinel.write_text("host", encoding="utf-8")
    config = {
        "env_type": "docker",
        "execution_write_scope": "workspace",
        "cwd": "/workspace",
        "host_cwd": str(workspace),
        "host_workspace_root": str(workspace),
        "docker_mount_cwd_to_workspace": True,
        "docker_image": "python:3.11-slim",
        "docker_volumes": [],
        "docker_extra_args": [],
        "docker_network": True,
        "docker_persist_across_processes": False,
        "docker_orphan_reaper": False,
        "timeout": 120,
        "lifetime_seconds": 300,
    }
    import tools.terminal_tool as terminal_tool

    monkeypatch.setattr(terminal_tool, "_get_env_config", lambda: config)
    monkeypatch.setattr(
        terminal_tool, "_check_all_guards", lambda *args, **kwargs: {"approved": True}
    )
    task_id = "docker-owner-proof"
    try:
        result = json.loads(
            terminal_tool.terminal_tool(
                "python -c 'import pathlib, subprocess; pathlib.Path(\"/workspace/private.txt\").write_bytes(b\"ok\"); pathlib.Path(\"/outside\").mkdir(); subprocess.run([\"python\",\"-c\",\"from pathlib import Path; Path(\\\"/outside/sentinel.txt\\\").write_bytes(b\\\"container\\\")\"], check=True)'",
                task_id=task_id,
                force=True,
            )
        )
        assert result["exit_code"] == 0, result
        assert (workspace / "private.txt").read_bytes() == b"ok"
        assert sentinel.read_text(encoding="utf-8") == "host"
        env = terminal_tool.get_active_env(task_id)
        assert env is not None
        run_args = env._all_run_args
        assert f"{workspace.resolve()}:/workspace" in run_args
        assert not any(
            str(outside.resolve()) in arg for arg in run_args
        )
    finally:
        terminal_tool.cleanup_vm(task_id, force_remove=True)
