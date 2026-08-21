"""Contract tests for profile-owned direct-command Kanban workers."""

from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb


SUPERVISOR = [sys.executable, "-P", "-m", "hermes_cli.kanban_command_worker"]


def _make_task(*, assignee: str = "engine", max_runtime_seconds: int | None = None):
    return kb.Task(
        id="t_worker_cmd",
        title="worker command",
        body="card text must not become command argv",
        assignee=assignee,
        status="running",
        priority=0,
        created_by="test",
        created_at=1,
        started_at=None,
        completed_at=None,
        workspace_kind="dir",
        workspace_path=None,
        claim_lock="lock",
        claim_expires=None,
        tenant=None,
        current_run_id=7,
        max_runtime_seconds=max_runtime_seconds,
    )


def _write_profile(root: Path, name: str, config_body: str) -> None:
    profile = root / "profiles" / name
    profile.mkdir(parents=True, exist_ok=True)
    profile.joinpath("config.yaml").write_text(config_body, encoding="utf-8")


@pytest.fixture()
def spawn_env(monkeypatch, tmp_path):
    root = tmp_path / ".hermes"
    root.mkdir()
    root.joinpath("config.yaml").write_text("{}\n", encoding="utf-8")
    monkeypatch.setenv("HERMES_HOME", str(root))

    captured: dict = {}

    class FakeProc:
        pid = 4242

    def fake_popen(cmd, *args, **kwargs):
        captured["cmd"] = list(cmd)
        captured["kwargs"] = kwargs
        captured["env"] = dict(kwargs.get("env") or {})
        return FakeProc()

    monkeypatch.setattr(kb, "_retag_legacy_worker_sessions", lambda _root: None)
    monkeypatch.setattr(subprocess, "Popen", fake_popen)
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    return root, workspace, captured


def test_profile_command_argv_is_literal_and_replaces_only_native_worker(
    spawn_env, monkeypatch
):
    root, workspace, captured = spawn_env
    monkeypatch.setenv("WORKER_COMMAND_SECRET", "host-secret-must-not-leak")
    _write_profile(
        root,
        "engine",
        "worker:\n  command:\n    - /home/david/bin/codex-sub\n"
        "    - luna\n    - --\n    - fixed instruction; read HERMES_KANBAN_TASK_ID\n",
    )

    task = _make_task()
    pid = kb._default_spawn(task, str(workspace), board="builds")

    assert pid == 4242
    assert captured["cmd"] == SUPERVISOR
    assert captured["kwargs"].get("shell", False) is False
    assert json.loads(captured["env"]["HERMES_KANBAN_WORKER_COMMAND"]) == [
        "/home/david/bin/codex-sub",
        "luna",
        "--",
        "fixed instruction; read HERMES_KANBAN_TASK_ID",
    ]
    assert captured["kwargs"]["cwd"] == str(workspace)
    assert captured["env"]["HERMES_KANBAN_TASK_ID"] == task.id
    assert captured["env"]["HERMES_KANBAN_TASK"] == task.id
    assert captured["env"]["HERMES_KANBAN_WORKSPACE"] == str(workspace)
    assert captured["env"]["HERMES_KANBAN_BOARD"] == "builds"
    assert captured["env"]["HERMES_KANBAN_RUN_ID"] == "7"


def test_profile_command_does_not_expand_environment_references(spawn_env, monkeypatch):
    root, workspace, captured = spawn_env
    monkeypatch.setenv("WORKER_COMMAND_SECRET", "host-secret-must-not-leak")
    _write_profile(
        root,
        "engine",
        "worker:\n  command:\n    - /bin/echo\n"
        "    - '${WORKER_COMMAND_SECRET}'\n"
        "    - '$WORKER_COMMAND_SECRET'\n"
        "    - '\\${WORKER_COMMAND_SECRET}'\n",
    )

    kb._default_spawn(_make_task(), str(workspace))

    assert json.loads(captured["env"]["HERMES_KANBAN_WORKER_COMMAND"]) == [
        "/bin/echo",
        "${WORKER_COMMAND_SECRET}",
        "$WORKER_COMMAND_SECRET",
        "\\${WORKER_COMMAND_SECRET}",
    ]


def test_profile_without_command_keeps_native_agent_argv(spawn_env):
    root, workspace, captured = spawn_env
    _write_profile(root, "engine", "toolsets:\n  - hermes-cli\n")

    kb._default_spawn(_make_task(), str(workspace))

    assert captured["cmd"] != SUPERVISOR
    assert "chat" in captured["cmd"]
    assert "work kanban task t_worker_cmd" in captured["cmd"]


def test_profile_without_command_keeps_native_inherited_environment(
    spawn_env, monkeypatch
):
    root, workspace, captured = spawn_env
    _write_profile(root, "engine", "toolsets:\n  - hermes-cli\n")
    monkeypatch.setenv("OPENAI_API_KEY", "native-worker-secret")
    monkeypatch.setenv("NATIVE_WORKER_SENTINEL", "preserve-me")

    kb._default_spawn(_make_task(), str(workspace))

    assert captured["env"]["OPENAI_API_KEY"] == "native-worker-secret"
    assert captured["env"]["NATIVE_WORKER_SENTINEL"] == "preserve-me"


@pytest.mark.parametrize(
    "declared, message",
    [
        ('worker:\n  command: "/bin/worker --flag"\n', "argv"),
        ("worker:\n  command:\n    - ./run.sh\n", "absolute"),
        ("worker:\n  command:\n    - worker\n", "absolute"),
        ("worker:\n  command:\n    - /bin/worker\n    - 7\n", "argv"),
    ],
)
def test_invalid_or_untrusted_command_fails_closed(spawn_env, declared, message):
    root, workspace, captured = spawn_env
    _write_profile(root, "engine", declared)

    with pytest.raises(RuntimeError, match=message):
        kb._default_spawn(_make_task(), str(workspace))
    assert "cmd" not in captured


def test_root_config_command_is_not_assignee_config(spawn_env):
    root, workspace, captured = spawn_env
    root.joinpath("config.yaml").write_text(
        "worker:\n  command:\n    - /bin/echo\n", encoding="utf-8"
    )

    with pytest.raises(RuntimeError, match="named profile"):
        kb._default_spawn(_make_task(assignee="default"), str(workspace))
    assert "cmd" not in captured


def test_missing_absolute_executable_fails_closed(spawn_env):
    root, workspace, captured = spawn_env
    _write_profile(
        root,
        "engine",
        f"worker:\n  command:\n    - {workspace / 'missing-worker'}\n",
    )

    with pytest.raises(RuntimeError, match="missing or not executable"):
        kb._default_spawn(_make_task(), str(workspace))
    assert "cmd" not in captured


def test_command_environment_is_sanitized_but_keeps_routing_context(
    spawn_env, monkeypatch
):
    root, workspace, captured = spawn_env
    _write_profile(root, "engine", "worker:\n  command:\n    - /bin/echo\n")
    monkeypatch.setenv("OPENAI_API_KEY", "parent-openai-secret")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "parent-anthropic-secret")
    monkeypatch.setenv("AUXILIARY_VISION_API_KEY", "parent-aux-secret")
    monkeypatch.setenv("GATEWAY_RELAY_SECRET", "parent-relay-secret")
    monkeypatch.setenv("GH_TOKEN", "parent-gh-secret")
    monkeypatch.setenv("HERMES_KANBAN_TASK", "stale-card-from-parent")

    kb._default_spawn(_make_task(), str(workspace), board="builds")

    env = captured["env"]
    for key in (
        "OPENAI_API_KEY",
        "ANTHROPIC_API_KEY",
        "AUXILIARY_VISION_API_KEY",
        "GATEWAY_RELAY_SECRET",
        "GH_TOKEN",
    ):
        assert key not in env
    assert env["HOME"]
    assert env["PATH"]
    assert env["HERMES_PROFILE"] == "engine"
    assert env["HERMES_KANBAN_TASK_ID"] == "t_worker_cmd"
    assert env["HERMES_KANBAN_TASK"] == "t_worker_cmd"
    assert env["HERMES_KANBAN_WORKSPACE"] == str(workspace)


def test_supervisor_re_sanitizes_its_arbitrary_child_environment(monkeypatch):
    from hermes_cli import kanban_command_worker as worker

    monkeypatch.setenv("OPENAI_API_KEY", "parent-openai-secret")
    monkeypatch.setenv("GATEWAY_RELAY_SECRET", "parent-relay-secret")
    monkeypatch.setenv("HERMES_KANBAN_WORKER_COMMAND", "[\"/bin/echo\"]")
    monkeypatch.setenv("HERMES_KANBAN_TASK_ID", "t_worker_cmd")
    monkeypatch.setenv("HERMES_KANBAN_WORKSPACE", "/workspace")

    env = worker._child_env()

    assert "OPENAI_API_KEY" not in env
    assert "GATEWAY_RELAY_SECRET" not in env
    assert "HERMES_KANBAN_WORKER_COMMAND" not in env
    assert env["HERMES_KANBAN_TASK_ID"] == "t_worker_cmd"
    assert env["HERMES_KANBAN_WORKSPACE"] == "/workspace"


@pytest.fixture()
def kanban_home(monkeypatch, tmp_path):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    kb.init_db()
    return home


def _claim_with_run(conn, task_id: str) -> int:
    task = kb.claim_task(conn, task_id, claimer=f"{kb._claimer_id().split(':', 1)[0]}:w0")
    assert task is not None
    row = conn.execute(
        "SELECT current_run_id FROM tasks WHERE id = ?", (task_id,)
    ).fetchone()
    return int(row["current_run_id"])


def _run_supervisor(home: Path, task_id: str, run_id: int, argv: list[str]):
    env = os.environ.copy()
    repo_root = str(Path(__file__).parents[2])
    env["PYTHONPATH"] = os.pathsep.join(
        part for part in (repo_root, env.get("PYTHONPATH", "")) if part
    )
    env.update(
        {
            "HERMES_HOME": str(home),
            "HERMES_KANBAN_DB": str(kb.kanban_db_path()),
            "HERMES_KANBAN_BOARD": "default",
            "HERMES_KANBAN_TASK_ID": task_id,
            "HERMES_KANBAN_TASK": task_id,
            "HERMES_KANBAN_RUN_ID": str(run_id),
            "HERMES_KANBAN_WORKER_COMMAND": json.dumps(argv),
        }
    )
    return subprocess.run(
        SUPERVISOR,
        cwd=str(Path(__file__).parents[2]),
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
    )


def test_supervisor_maps_exit_code_to_canonical_transition(kanban_home):
    with kb.connect() as conn:
        task_id = kb.create_task(conn, title="ok", assignee="engine")
        run_id = _claim_with_run(conn, task_id)

    proc = _run_supervisor(kanban_home, task_id, run_id, [sys.executable, "-c", "pass"])
    assert proc.returncode == 0, proc.stderr
    with kb.connect() as conn:
        assert kb.get_task(conn, task_id).status == "done"


def test_supervisor_nonzero_blocks_but_expected_run_id_preserves_prior_transition(
    kanban_home,
):
    with kb.connect() as conn:
        task_id = kb.create_task(conn, title="fail", assignee="engine")
        run_id = _claim_with_run(conn, task_id)

    proc = _run_supervisor(
        kanban_home,
        task_id,
        run_id,
        [sys.executable, "-c", "import sys; sys.exit(3)"],
    )
    assert proc.returncode == 0, proc.stderr
    with kb.connect() as conn:
        assert kb.get_task(conn, task_id).status == "blocked"

        # A stale supervisor cannot overwrite a later canonical transition.
        assert not kb.complete_task(
            conn,
            task_id,
            summary="stale",
            expected_run_id=run_id,
        )


def test_missing_command_reports_no_canonical_outcome(kanban_home, tmp_path):
    with kb.connect() as conn:
        task_id = kb.create_task(conn, title="missing", assignee="engine")
        run_id = _claim_with_run(conn, task_id)

    proc = _run_supervisor(
        kanban_home,
        task_id,
        run_id,
        [str(tmp_path / "does-not-exist")],
    )
    assert proc.returncode != 0
    with kb.connect() as conn:
        assert kb.get_task(conn, task_id).status == "running"


@pytest.mark.live_system_guard_bypass
@pytest.mark.skipif(sys.platform == "win32", reason="POSIX signal witness")
def test_dispatcher_timeout_owns_timeout_state_and_supervisor_only_cleans_up(
    kanban_home, tmp_path,
):
    grandchild_pid_path = tmp_path / "grandchild.pid"
    stubborn = (
        "import pathlib, signal, subprocess, sys, time; "
        f"p=subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(30)']); "
        f"pathlib.Path({str(grandchild_pid_path)!r}).write_text(str(p.pid)); "
        "signal.signal(signal.SIGTERM, signal.SIG_IGN); time.sleep(30)"
    )
    with kb.connect() as conn:
        task_id = kb.create_task(conn, title="timeout", assignee="engine")
        run_id = _claim_with_run(conn, task_id)
        old = int(time.time()) - 10
        conn.execute(
            "UPDATE tasks SET worker_pid = ?, max_runtime_seconds = 1, "
            "started_at = ? WHERE id = ?",
            (0, old, task_id),
        )
        conn.execute(
            "UPDATE task_runs SET started_at = ? WHERE id = ?", (old, run_id)
        )
        conn.commit()

        env = os.environ.copy()
        repo_root = str(Path(__file__).parents[2])
        env["PYTHONPATH"] = os.pathsep.join(
            part for part in (repo_root, env.get("PYTHONPATH", "")) if part
        )
        env.update(
            {
                "HERMES_HOME": str(kanban_home),
                "HERMES_KANBAN_DB": str(kb.kanban_db_path()),
                "HERMES_KANBAN_BOARD": "default",
                "HERMES_KANBAN_TASK_ID": task_id,
                "HERMES_KANBAN_TASK": task_id,
                "HERMES_KANBAN_RUN_ID": str(run_id),
                "HERMES_KANBAN_COMMAND_TERM_GRACE": "0.2",
                "HERMES_KANBAN_WORKER_COMMAND": json.dumps(
                    [sys.executable, "-c", stubborn]
                ),
            }
        )
        proc = subprocess.Popen(
            SUPERVISOR,
            cwd=str(Path(__file__).parents[2]),
            env=env,
        )
        conn.execute(
            "UPDATE tasks SET worker_pid = ? WHERE id = ?", (proc.pid, task_id)
        )
        conn.commit()
        deadline = time.time() + 5
        while time.time() < deadline and not grandchild_pid_path.exists():
            time.sleep(0.05)
        assert grandchild_pid_path.exists()
        timed_out = kb.enforce_max_runtime(conn)
        assert timed_out == [task_id]

    assert proc.wait(timeout=10) != 0
    grandchild_pid = int(grandchild_pid_path.read_text(encoding="utf-8"))
    deadline = time.time() + 5
    while time.time() < deadline and kb._pid_alive(grandchild_pid):
        time.sleep(0.05)
    assert not kb._pid_alive(grandchild_pid)
    with kb.connect() as conn:
        task = kb.get_task(conn, task_id)
        assert task.status == "ready"
        assert task.consecutive_failures == 1
        kinds = [
            row["kind"]
            for row in conn.execute(
                "SELECT kind FROM task_events WHERE task_id = ?", (task_id,)
            )
        ]
        assert "timed_out" in kinds
        assert "blocked" not in kinds


def test_windows_supervisor_termination_uses_tree_kill(monkeypatch):
    from hermes_cli import _subprocess_compat as compat
    from hermes_cli import kanban_command_worker as worker

    child = object()
    killed = []
    monkeypatch.setattr(compat, "IS_WINDOWS", True)
    monkeypatch.setattr(compat, "kill_process_tree", lambda proc: killed.append(proc))

    worker._terminate_child_tree(child)

    assert killed == [child]


def test_windows_dispatcher_termination_uses_tree_kill(monkeypatch):
    from hermes_cli import _subprocess_compat as compat

    calls = []

    def fake_run(argv, **kwargs):
        calls.append((argv, kwargs))
        return subprocess.CompletedProcess(argv, 0)

    monkeypatch.setattr(compat, "IS_WINDOWS", True)
    monkeypatch.setattr(compat.subprocess, "run", fake_run)
    compat.terminate_process_tree(1234, force=True)

    assert calls
    assert calls[0][0] == ["taskkill", "/T", "/F", "/PID", "1234"]
