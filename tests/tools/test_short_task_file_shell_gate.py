"""Managed file tools use a clean, non-login, process-tracked shell."""

from __future__ import annotations

import json
import os
from types import SimpleNamespace

import pytest

from tools.environments import local as local_env
from tools.file_operations import ShellFileOperations


def _policy_snapshot(*, enabled: bool) -> str:
    return json.dumps(
        {
            "schema": 2,
            "enabled": enabled,
            "soft_iteration_limit": 36,
            "max_handoffs": 8,
            "max_iterations": 90,
            "failure_limit": 2,
            "validation_error": None,
        }
    )


@pytest.fixture
def managed_env(monkeypatch, tmp_path):
    monkeypatch.setenv("HERMES_KANBAN_TASK", "t_short")
    monkeypatch.setenv(
        "HERMES_KANBAN_SHORT_TASK_HANDOFF_POLICY",
        _policy_snapshot(enabled=True),
    )
    monkeypatch.setenv("HERMES_KANBAN_WORKSPACE", str(tmp_path))
    return tmp_path


def test_managed_backend_skips_login_snapshot_entirely(
    managed_env, monkeypatch
):
    def forbidden_run(*_args, **_kwargs):
        pytest.fail("managed backend attempted login/profile snapshot")

    monkeypatch.setattr(local_env.LocalEnvironment, "_run_bash", forbidden_run)

    env = local_env.LocalEnvironment(cwd=str(managed_env), timeout=5)

    assert env._snapshot_ready is False
    assert env._prefer_nonlogin is True


def test_managed_shell_uses_clean_argv_and_environment(
    managed_env, monkeypatch
):
    env = local_env.LocalEnvironment.__new__(local_env.LocalEnvironment)
    env.cwd = str(managed_env)
    env.timeout = 5
    env.env = {}
    captured = {}

    class FakeProcess:
        pid = 43210

    def fake_popen(args, **kwargs):
        captured["args"] = args
        captured["env"] = kwargs["env"]
        return FakeProcess()

    monkeypatch.setenv("BASH_ENV", str(managed_env / "bash-env.sh"))
    monkeypatch.setenv("ENV", str(managed_env / "env.sh"))
    monkeypatch.setenv("ZDOTDIR", str(managed_env / "zdir"))
    monkeypatch.setenv("BASH_FUNC_injected%%", "() { touch /tmp/bad; }")
    monkeypatch.setenv("LD_PRELOAD", str(managed_env / "inject.so"))
    monkeypatch.setenv("PATH", str(managed_env / "untrusted-bin"))
    monkeypatch.setattr(
        local_env,
        "_resolve_shell_init_files",
        lambda: pytest.fail("custom init files were resolved"),
    )
    monkeypatch.setattr(local_env.subprocess, "Popen", fake_popen)
    monkeypatch.setattr(
        local_env, "register_short_task_foreground_process", lambda *_a: None
    )
    monkeypatch.setattr(
        local_env, "_begin_short_task_foreground_cleanup", lambda: None
    )

    proc = env._run_bash("true", login=True, timeout=5)

    assert proc.pid == 43210
    assert captured["args"][1:4] == ["--noprofile", "--norc", "-c"]
    assert "-l" not in captured["args"]
    for key in (
        "BASH_ENV",
        "ENV",
        "ZDOTDIR",
        "BASH_FUNC_injected%%",
        "LD_PRELOAD",
    ):
        assert key not in captured["env"]
    if os.name != "nt":
        assert captured["env"]["PATH"] == os.defpath


def test_profile_and_bash_env_cannot_create_side_effect_but_real_file_tools_work(
    managed_env, monkeypatch
):
    profile_home = managed_env / "profile-home"
    profile_home.mkdir()
    profile_marker = managed_env / "profile-ran"
    bash_env_marker = managed_env / "bash-env-ran"
    (profile_home / ".bash_profile").write_text(
        f"touch {profile_marker}\n"
    )
    bash_env = managed_env / "bash-env.sh"
    bash_env.write_text(f"touch {bash_env_marker}\n")
    monkeypatch.setenv("HOME", str(profile_home))
    monkeypatch.setenv("BASH_ENV", str(bash_env))

    env = local_env.LocalEnvironment(cwd=str(managed_env), timeout=10)
    ops = ShellFileOperations(env, cwd=str(managed_env))
    target = managed_env / "normal.txt"
    try:
        written = ops.write_file(str(target), "safe\n")
        patched = ops.patch_replace(str(target), "safe", "safer")
        read = ops.read_file(str(target), 1, 20)
        searched = ops.search(
            "safer",
            path=str(managed_env),
            target="content",
            file_glob="*.txt",
            limit=20,
        )
    finally:
        env.cleanup()

    assert written.error is None
    assert patched.error is None
    assert read.error is None
    assert searched.error is None
    assert searched.total_count >= 1
    assert "safer" in read.content
    assert target.read_text() == "safer\n"
    assert not profile_marker.exists()
    assert not bash_env_marker.exists()


@pytest.mark.parametrize("worker", [False, True])
def test_ordinary_and_disabled_workers_keep_login_snapshot_behavior(
    tmp_path, monkeypatch, worker
):
    if worker:
        monkeypatch.setenv("HERMES_KANBAN_TASK", "t_review")
        monkeypatch.setenv(
            "HERMES_KANBAN_SHORT_TASK_HANDOFF_POLICY",
            _policy_snapshot(enabled=False),
        )
    else:
        monkeypatch.delenv("HERMES_KANBAN_TASK", raising=False)
        monkeypatch.delenv(
            "HERMES_KANBAN_SHORT_TASK_HANDOFF_POLICY", raising=False
        )

    env = local_env.LocalEnvironment.__new__(local_env.LocalEnvironment)
    from tools.environments.base import BaseEnvironment

    BaseEnvironment.__init__(env, cwd=str(tmp_path), timeout=5, env={})
    calls = []
    fake_proc = SimpleNamespace()
    monkeypatch.setattr(
        env,
        "_run_bash",
        lambda _cmd, *, login, timeout: calls.append(login) or fake_proc,
    )
    monkeypatch.setattr(
        env,
        "_wait_for_process",
        lambda _proc, timeout: {"returncode": 0, "output": ""},
    )
    monkeypatch.setattr(env, "_update_cwd", lambda _result: None)

    env.init_session()

    assert calls == [True]
    assert env._snapshot_ready is True
    assert env._prefer_nonlogin is False


def test_legacy_review_marker_without_managed_lane_keeps_login_snapshot_behavior(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("HERMES_KANBAN_TASK", "t_legacy_review")
    monkeypatch.setenv("HERMES_KANBAN_REVIEW_MODE", "1")
    monkeypatch.delenv("HERMES_KANBAN_MANAGED_LANE", raising=False)
    monkeypatch.delenv("HERMES_KANBAN_MANAGED_BOOTSTRAP", raising=False)
    monkeypatch.delenv(
        "HERMES_KANBAN_MANAGED_BOOTSTRAP_VERIFIED", raising=False
    )
    monkeypatch.delenv(
        "HERMES_KANBAN_SHORT_TASK_HANDOFF_POLICY", raising=False
    )

    env = local_env.LocalEnvironment.__new__(local_env.LocalEnvironment)
    from tools.environments.base import BaseEnvironment

    BaseEnvironment.__init__(env, cwd=str(tmp_path), timeout=5, env={})
    calls = []
    fake_proc = SimpleNamespace()
    monkeypatch.setattr(
        env,
        "_run_bash",
        lambda _cmd, *, login, timeout: calls.append(login) or fake_proc,
    )
    monkeypatch.setattr(
        env,
        "_wait_for_process",
        lambda _proc, timeout: {"returncode": 0, "output": ""},
    )
    monkeypatch.setattr(env, "_update_cwd", lambda _result: None)

    env.init_session()

    assert calls == [True]
    assert env._snapshot_ready is True
    assert env._prefer_nonlogin is False
