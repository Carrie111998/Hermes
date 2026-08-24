from __future__ import annotations

import argparse
import contextlib
import json
import os
import stat
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from hermes_cli import kanban_db as kb


def test_sensitive_task_fields_default_off_and_round_trip(tmp_path):
    conn = kb.connect(tmp_path / "kanban.db")
    try:
        ordinary_id = kb.create_task(conn, title="ordinary", dispatchable=False)
        ordinary = kb.get_task(conn, ordinary_id)
        assert ordinary is not None
        assert ordinary.sensitive_execution is False
        assert ordinary.sensitive_runner_id is None
        assert ordinary.protected_resource_ids == []

        sensitive_id = kb.create_task(
            conn,
            title="sensitive",
            dispatchable=False,
            sensitive_execution=True,
            sensitive_runner_id="deploy.fixed-v1",
            protected_resource_ids=["prod-config", "release-key"],
        )
        sensitive = kb.get_task(conn, sensitive_id)
        assert sensitive is not None
        assert sensitive.sensitive_execution is True
        assert sensitive.sensitive_runner_id == "deploy.fixed-v1"
        assert sensitive.protected_resource_ids == ["prod-config", "release-key"]
    finally:
        conn.close()


def test_sensitive_task_fields_migrate_legacy_board(tmp_path):
    db = tmp_path / "legacy.db"
    conn = kb.connect(db)
    task_id = kb.create_task(conn, title="legacy", dispatchable=False)
    conn.execute("ALTER TABLE tasks DROP COLUMN sensitive_execution")
    conn.execute("ALTER TABLE tasks DROP COLUMN sensitive_runner_id")
    conn.execute("ALTER TABLE tasks DROP COLUMN protected_resource_ids")
    conn.commit()
    conn.close()

    kb.init_db(db)
    conn = kb.connect(db)
    try:
        task = kb.get_task(conn, task_id)
        assert task is not None
        assert task.sensitive_execution is False
        assert task.sensitive_runner_id is None
        assert task.protected_resource_ids == []
    finally:
        conn.close()


@pytest.mark.parametrize("runner_id", ["", "../runner", "runner with spaces"])
def test_sensitive_task_rejects_non_opaque_runner_ids(tmp_path, runner_id):
    conn = kb.connect(tmp_path / "kanban.db")
    try:
        with pytest.raises(ValueError, match="sensitive_runner_id"):
            kb.create_task(
                conn,
                title="bad",
                dispatchable=False,
                sensitive_execution=True,
                sensitive_runner_id=runner_id,
            )
    finally:
        conn.close()


def test_non_sensitive_task_rejects_sensitive_authority(tmp_path):
    conn = kb.connect(tmp_path / "kanban.db")
    try:
        with pytest.raises(ValueError, match="sensitive_execution"):
            kb.create_task(
                conn,
                title="bad",
                dispatchable=False,
                sensitive_runner_id="deploy.fixed-v1",
            )
    finally:
        conn.close()


def test_sensitive_run_parser_accepts_no_model_arguments():
    from hermes_cli.kanban import build_parser

    root = argparse.ArgumentParser()
    subs = root.add_subparsers(dest="command")
    build_parser(subs)
    with pytest.raises(SystemExit):
        root.parse_args(["kanban", "sensitive-run", "user-argument"])


def test_sensitive_mode_does_not_bypass_generic_protected_read(monkeypatch, tmp_path):
    from agent.file_safety import get_read_block_error

    profile_home = tmp_path / "profile"
    monkeypatch.setenv("HERMES_HOME", str(profile_home))
    monkeypatch.setenv("HERMES_KANBAN_SENSITIVE", "1")
    error = get_read_block_error(str(profile_home / ".env"))
    assert error is not None
    assert "blocked" in error.lower() or "denied" in error.lower()


def test_sensitive_worker_actual_spawn_startup_and_terminal_env_are_isolated(
    monkeypatch, tmp_path
):
    root = tmp_path / ".hermes"
    profile_home = root / "profiles" / "elias"
    profile_home.mkdir(parents=True)
    root.joinpath("config.yaml").write_text("{}\n", encoding="utf-8")
    profile_home.joinpath(".env").write_text(
        "CANARY_PROVIDER_API_KEY=synthetic-canary-never-real\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("HERMES_HOME", str(root))
    monkeypatch.setenv("CANARY_PROVIDER_API_KEY", "synthetic-canary-never-real")
    monkeypatch.setattr(kb, "_resolve_hermes_argv", lambda: ["hermes"])
    real_popen = subprocess.Popen
    captured = {}

    class FakeProc:
        pid = 4247

    def fake_popen(cmd, *args, **kwargs):
        captured["cmd"] = list(cmd)
        captured["env"] = dict(kwargs["env"])
        return FakeProc()

    monkeypatch.setattr(subprocess, "Popen", fake_popen)
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    task = kb.Task(
        id="t_sensitive_startup",
        title="sensitive startup",
        body=None,
        assignee="elias",
        status="running",
        priority=0,
        created_by="test",
        created_at=1,
        started_at=None,
        completed_at=None,
        workspace_kind="dir",
        workspace_path=str(workspace),
        claim_lock="lock",
        claim_expires=None,
        tenant=None,
        current_run_id=7,
        sensitive_execution=True,
        sensitive_runner_id="fixed-v1",
    )
    assert kb._default_spawn(task, str(workspace)) == 4247
    monkeypatch.setattr(subprocess, "Popen", real_popen)
    child_env = captured["env"]
    assert "CANARY_PROVIDER_API_KEY" not in child_env
    assert captured["cmd"][1:4] == [
        "-m", "hermes_cli.kanban_sensitive_worker", "--"
    ]
    probe = """
import os
import subprocess
import sys
from pathlib import Path
from agent.secret_scope import get_secret, load_env_file
from tools.environments.local import _make_run_env

key = "CANARY_PROVIDER_API_KEY"
expected = load_env_file(Path(os.environ["HERMES_HOME"]) / ".env")[key]
import run_agent  # noqa: F401 - exercise the real worker startup dotenv path
if key in os.environ:
    raise SystemExit(10)
if get_secret(key) != expected:
    raise SystemExit(11)
terminal_env = _make_run_env({
    **os.environ,
    key: expected,
    "UNRELATED_AMBIENT_VALUE": "must-not-cross",
})
if key in terminal_env or "UNRELATED_AMBIENT_VALUE" in terminal_env:
    raise SystemExit(12)
terminal_probe = subprocess.run(
    [sys.executable, "-c", "import os; raise SystemExit('CANARY_PROVIDER_API_KEY' in os.environ)"],
    env=terminal_env,
    check=False,
)
if terminal_probe.returncode != 0:
    raise SystemExit(13)
print("sensitive-startup-ok")
"""
    proc = subprocess.run(
        [
            sys.executable,
            "-m",
            "hermes_cli.kanban_sensitive_worker",
            "--",
            sys.executable,
            "-c",
            probe,
        ],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        env=child_env,
        check=False,
        text=True,
    )

    assert proc.returncode == 0, proc.stdout
    assert "sensitive-startup-ok" in proc.stdout
    assert "synthetic-canary-never-real" not in proc.stdout


def test_sensitive_worker_credentials_use_scope_without_populating_environ(
    monkeypatch, tmp_path
):
    from agent.secret_scope import get_secret, reset_secret_scope
    from hermes_cli.config import _expand_env_vars
    from hermes_cli.kanban_sensitive import activate_sensitive_worker_credentials

    profile_home = tmp_path / "profile"
    profile_home.mkdir()
    profile_home.joinpath(".env").write_text(
        "CANARY_PROVIDER_API_KEY=synthetic-profile-canary-never-real\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("HERMES_KANBAN_SENSITIVE", "1")
    monkeypatch.delenv("CANARY_PROVIDER_API_KEY", raising=False)

    token = activate_sensitive_worker_credentials(profile_home)
    try:
        assert get_secret("CANARY_PROVIDER_API_KEY") == (
            "synthetic-profile-canary-never-real"
        )
        assert _expand_env_vars("${env:CANARY_PROVIDER_API_KEY}") == (
            "synthetic-profile-canary-never-real"
        )
        assert "CANARY_PROVIDER_API_KEY" not in os.environ
    finally:
        reset_secret_scope(token)


def test_sensitive_worker_resolves_model_auth_without_ambient_key(
    monkeypatch, tmp_path
):
    from agent.secret_scope import reset_secret_scope
    from hermes_cli.kanban_sensitive import activate_sensitive_worker_credentials
    from hermes_cli.runtime_provider import resolve_runtime_provider

    profile_home = tmp_path / "profile"
    profile_home.mkdir()
    profile_home.joinpath(".env").write_text(
        "OPENROUTER_API_KEY=synthetic-model-auth-canary-never-real\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("HERMES_HOME", str(profile_home))
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)

    token = activate_sensitive_worker_credentials(profile_home)
    try:
        runtime = resolve_runtime_provider(requested="openrouter")
        assert runtime["api_key"] == "synthetic-model-auth-canary-never-real"
        assert "OPENROUTER_API_KEY" not in os.environ
    finally:
        reset_secret_scope(token)


def test_sensitive_dotenv_reload_keeps_profile_credentials_out_of_environ(
    monkeypatch, tmp_path
):
    from agent.secret_scope import reset_secret_scope
    from hermes_cli.env_loader import load_hermes_dotenv
    from hermes_cli.kanban_sensitive import activate_sensitive_worker_credentials

    profile_home = tmp_path / "profile"
    profile_home.mkdir()
    profile_home.joinpath(".env").write_text(
        "CANARY_PROVIDER_API_KEY=synthetic-profile-canary-never-real\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("HERMES_KANBAN_SENSITIVE", "1")
    monkeypatch.delenv("CANARY_PROVIDER_API_KEY", raising=False)

    token = activate_sensitive_worker_credentials(profile_home)
    try:
        assert load_hermes_dotenv(hermes_home=profile_home) == []
        assert "CANARY_PROVIDER_API_KEY" not in os.environ
    finally:
        reset_secret_scope(token)


def test_sensitive_terminal_child_environment_is_deny_by_default(monkeypatch):
    from tools.environments.local import _sanitize_subprocess_env

    monkeypatch.setenv("HERMES_KANBAN_SENSITIVE", "1")
    child_env = _sanitize_subprocess_env({
        "Path": "/usr/bin",
        "HOME": "/tmp/synthetic-home",
        "HERMES_KANBAN_SENSITIVE": "1",
        "HERMES_KANBAN_TASK": "t_12345678",
        "CANARY_PROVIDER_API_KEY": "synthetic-canary-never-real",
        "UNRELATED_AMBIENT_VALUE": "must-not-cross",
    })

    assert child_env["Path"] == "/usr/bin"
    assert child_env["HERMES_KANBAN_TASK"] == "t_12345678"
    assert "CANARY_PROVIDER_API_KEY" not in child_env
    assert "UNRELATED_AMBIENT_VALUE" not in child_env


def test_sensitive_runner_environment_keeps_windows_runtime_without_credentials():
    from hermes_cli.kanban_sensitive import build_sensitive_runner_env

    child_env = build_sensitive_runner_env(
        {"resource-a": "/protected/exact"},
        {
            "SystemRoot": r"C:\Windows",
            "ComSpec": r"C:\Windows\System32\cmd.exe",
            "CANARY_PROVIDER_API_KEY": "synthetic-canary-never-real",
        },
    )

    assert child_env == {
        "SystemRoot": r"C:\Windows",
        "ComSpec": r"C:\Windows\System32\cmd.exe",
        "HERMES_KANBAN_SENSITIVE_RESOURCES": json.dumps(
            {"resource-a": "/protected/exact"},
            sort_keys=True,
            separators=(",", ":"),
        ),
    }


def test_fixed_runner_uses_declared_argv_and_resources_only(monkeypatch, capsys):
    from hermes_cli import kanban_sensitive

    task = SimpleNamespace(sensitive_execution=True, sensitive_runner_id="fixed-v1", protected_resource_ids=["resource-a"])
    monkeypatch.setenv("HERMES_KANBAN_TASK", "t_12345678")
    monkeypatch.setenv("CANARY_PROVIDER_API_KEY", "not-a-real-credential")
    monkeypatch.setenv("CANARY_GATEWAY_TOKEN", "not-a-real-credential")
    monkeypatch.setattr(kanban_sensitive, "assert_sensitive_worker_context", lambda: None)
    monkeypatch.setattr(kb, "connect_closing", lambda: contextlib.nullcontext(object()))
    monkeypatch.setattr(kb, "get_task", lambda _conn, _task_id: task)
    monkeypatch.setattr(
        "hermes_cli.config.load_config",
        lambda: {"kanban": {"sensitive_execution": {
            "runners": {"fixed-v1": {"argv": ["/fixed/runner", "fixed"]}},
            "resources": {"resource-a": "/protected/exact"},
        }}},
    )
    captured = {}

    def fake_run(argv, **kwargs):
        captured.update(argv=argv, kwargs=kwargs)
        return SimpleNamespace(returncode=0, stdout=b"safe\n", stderr=b"")

    monkeypatch.setattr(kanban_sensitive.subprocess, "run", fake_run)
    monkeypatch.setattr(kanban_sensitive, "active_secret_values", lambda: ())
    assert kanban_sensitive.run_sensitive_runner() == 0
    assert captured["argv"] == ["/fixed/runner", "fixed"]
    assert captured["kwargs"]["shell"] is False
    assert captured["kwargs"]["env"] == {
        "HERMES_KANBAN_SENSITIVE_RESOURCES": json.dumps(
            {"resource-a": "/protected/exact"}, sort_keys=True, separators=(",", ":")
        )
    }
    assert capsys.readouterr().out == "safe\n"


def test_fixed_runner_rejects_undeclared_resource(monkeypatch):
    from hermes_cli import kanban_sensitive

    task = SimpleNamespace(sensitive_execution=True, sensitive_runner_id="fixed-v1", protected_resource_ids=["undeclared"])
    monkeypatch.setenv("HERMES_KANBAN_TASK", "t_12345678")
    monkeypatch.setattr(kanban_sensitive, "assert_sensitive_worker_context", lambda: None)
    monkeypatch.setattr(kb, "connect_closing", lambda: contextlib.nullcontext(object()))
    monkeypatch.setattr(kb, "get_task", lambda _conn, _task_id: task)
    monkeypatch.setattr(
        "hermes_cli.config.load_config",
        lambda: {"kanban": {"sensitive_execution": {
            "runners": {"fixed-v1": {"argv": ["/fixed/runner"]}},
            "resources": {},
        }}},
    )
    with pytest.raises(RuntimeError, match="not declared"):
        kanban_sensitive.run_sensitive_runner()
