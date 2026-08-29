"""Kanban-scoped service-tier override resolution and worker propagation."""

from __future__ import annotations

import argparse
import copy
import subprocess
from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb


@pytest.fixture
def kanban_home(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    kb.init_db()
    return home


@pytest.fixture
def conn(kanban_home):
    connection = kb.connect()
    yield connection
    connection.close()


def _spawn_and_capture(monkeypatch, tmp_path, task):
    monkeypatch.setattr(kb, "_resolve_hermes_argv", lambda: ["hermes"])
    captured = {}

    class FakeProc:
        pid = 4246

    def fake_popen(cmd, *args, **kwargs):
        captured["cmd"] = list(cmd)
        return FakeProc()

    monkeypatch.setattr(subprocess, "Popen", fake_popen)
    workspace = tmp_path / "ws"
    workspace.mkdir(exist_ok=True)
    kb._default_spawn(task, str(workspace))
    return captured["cmd"]


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        (None, None),
        ("", None),
        ("   ", None),
        (" fast ", "fast"),
        ("FAST", "fast"),
        ("normal", "normal"),
        (" Normal ", "normal"),
        ("priority", None),
        ("off", None),
        ("invalid-tier", None),
    ],
)
def test_kanban_service_tier_config_accepts_only_fast_or_normal(raw, expected):
    assert kb.kanban_service_tier_config({"service_tier": raw}) == expected


@pytest.mark.parametrize("assignee", ["default", "worker"])
@pytest.mark.parametrize("tier", ["fast", "normal"])
def test_spawn_passes_tier_for_default_and_named_assignees(
    monkeypatch, tmp_path, conn, assignee, tier
):
    task_id = kb.create_task(conn, title="t", assignee=assignee)
    task = kb.get_task(conn, task_id)
    monkeypatch.setattr(kb, "kanban_service_tier_config", lambda: tier)

    cmd = _spawn_and_capture(monkeypatch, tmp_path, task)

    index = cmd.index("--service-tier")
    assert cmd[index + 1] == tier
    assert index < cmd.index("chat")
    assert cmd[cmd.index("-p") + 1] == assignee


@pytest.mark.parametrize("raw", [None, "", "   ", "invalid-tier"])
def test_spawn_omits_service_tier_for_absent_empty_or_invalid(
    monkeypatch, tmp_path, conn, raw
):
    task_id = kb.create_task(conn, title="t", assignee="worker")
    task = kb.get_task(conn, task_id)
    resolved = kb.kanban_service_tier_config({"service_tier": raw})
    monkeypatch.setattr(kb, "kanban_service_tier_config", lambda: resolved)

    cmd = _spawn_and_capture(monkeypatch, tmp_path, task)

    assert "--service-tier" not in cmd


def test_spawn_reads_service_tier_from_dispatcher_config(monkeypatch, tmp_path, conn):
    from hermes_cli import config as config_mod

    task_id = kb.create_task(conn, title="t", assignee="worker")
    task = kb.get_task(conn, task_id)
    monkeypatch.setattr(
        config_mod,
        "load_config",
        lambda: {"kanban": {"service_tier": "normal"}},
    )

    cmd = _spawn_and_capture(monkeypatch, tmp_path, task)

    index = cmd.index("--service-tier")
    assert cmd[index + 1] == "normal"


@pytest.mark.parametrize(
    ("profile_tier", "kanban_tier", "expected"),
    [
        ("fast", "normal", None),
        ("normal", "fast", "priority"),
        ("fast", None, "priority"),
        ("normal", None, None),
    ],
)
def test_profile_inheritance_and_override_both_directions(
    monkeypatch, tmp_path, conn, profile_tier, kanban_tier, expected
):
    import cli as cli_mod

    task_id = kb.create_task(conn, title="t", assignee="worker")
    task = kb.get_task(conn, task_id)
    monkeypatch.setattr(kb, "kanban_service_tier_config", lambda: kanban_tier)
    cmd = _spawn_and_capture(monkeypatch, tmp_path, task)

    parser = __import__("hermes_cli._parser", fromlist=["build_top_level_parser"])
    # ``-p <profile>`` is consumed by main._apply_profile_override before the
    # argparse surface sees the remaining worker invocation.
    args = parser.build_top_level_parser()[0].parse_args(cmd[3:])
    config = copy.deepcopy(cli_mod.CLI_CONFIG)
    config["agent"]["service_tier"] = profile_tier
    monkeypatch.setattr(cli_mod, "CLI_CONFIG", config)
    instance = cli_mod.HermesCLI(
        service_tier=getattr(args, "service_tier", None), compact=True
    )

    assert instance.service_tier == expected
    assert config["agent"]["service_tier"] == profile_tier


def test_dispatch_does_not_mutate_global_or_profile_config(
    monkeypatch, tmp_path, conn, kanban_home
):
    from hermes_cli import profiles

    global_config = kanban_home / "config.yaml"
    profile_home = kanban_home / "profiles" / "worker"
    profile_home.mkdir(parents=True)
    profile_config = profile_home / "config.yaml"
    global_config.write_text(
        "agent:\n  service_tier: fast\nkanban:\n  service_tier: normal\n",
        encoding="utf-8",
    )
    profile_config.write_text(
        "agent:\n  service_tier: fast\n",
        encoding="utf-8",
    )
    before = (global_config.read_bytes(), profile_config.read_bytes())

    monkeypatch.setattr(
        profiles,
        "resolve_profile_env",
        lambda profile: str(kanban_home if profile == "default" else profile_home),
    )
    task_id = kb.create_task(conn, title="t", assignee="worker")
    task = kb.get_task(conn, task_id)
    cmd = _spawn_and_capture(monkeypatch, tmp_path, task)

    assert cmd[cmd.index("--service-tier") + 1] == "normal"
    assert (global_config.read_bytes(), profile_config.read_bytes()) == before


@pytest.mark.parametrize(
    ("argv", "expected"),
    [
        (["--cli", "--service-tier", "normal", "chat", "-q", "hi"], "normal"),
        (["--cli", "chat", "-q", "hi", "--service-tier", "fast"], "fast"),
        (["--service-tier", "normal", "chat", "--service-tier", "fast"], "fast"),
        (["--service-tier", "fast", "-z", "hi"], "fast"),
    ],
)
def test_service_tier_parser_paths(argv, expected):
    from hermes_cli._parser import build_top_level_parser

    parser = build_top_level_parser()[0]
    args = parser.parse_args(argv)

    assert args.service_tier == expected


def test_chat_service_tier_default_cannot_clobber_root_value():
    from hermes_cli._parser import build_top_level_parser

    parser, _subparsers, chat = build_top_level_parser()
    action = next(
        item for item in chat._actions if "--service-tier" in item.option_strings
    )

    assert action.default is argparse.SUPPRESS
    parsed = parser.parse_args(["--service-tier", "normal", "chat"])
    assert parsed.service_tier == "normal"


def test_goal_mode_query_order_is_preserved(monkeypatch, tmp_path, conn):
    task_id = kb.create_task(
        conn,
        title="goal",
        assignee="worker",
        goal_mode=True,
    )
    task = kb.get_task(conn, task_id)
    monkeypatch.setattr(kb, "kanban_service_tier_config", lambda: "normal")

    cmd = _spawn_and_capture(monkeypatch, tmp_path, task)

    assert cmd[-4:] == ["chat", "-q", f"work kanban task {task.id}", "-Q"]
