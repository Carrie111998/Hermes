from __future__ import annotations

import argparse
import contextlib
import json
import os
import stat
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


def test_fixed_runner_uses_declared_argv_and_resources_only(monkeypatch, capsys):
    from hermes_cli import kanban_sensitive

    task = SimpleNamespace(sensitive_execution=True, sensitive_runner_id="fixed-v1", protected_resource_ids=["resource-a"])
    monkeypatch.setenv("HERMES_KANBAN_TASK", "t_12345678")
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
    assert json.loads(captured["kwargs"]["env"]["HERMES_KANBAN_SENSITIVE_RESOURCES"]) == {"resource-a": "/protected/exact"}
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
