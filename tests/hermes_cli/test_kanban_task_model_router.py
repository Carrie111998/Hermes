"""Opt-in Kanban task metadata persistence and worker spawn routing."""

from __future__ import annotations

import json
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


def _routing_metadata(**overrides):
    metadata = {
        "enabled": True,
        "risk_level": "low",
        "external_send_requested": False,
        "cross_file": False,
        "tool_count": 0,
        "multi_step_verification": False,
        "luna_insufficiency": False,
        "high_value": False,
        "terra_insufficient": False,
        "deep_reasoning_required": False,
    }
    metadata.update(overrides)
    return metadata


def _capture_spawn(monkeypatch, tmp_path):
    monkeypatch.setattr(kb, "_resolve_hermes_argv", lambda: ["hermes"])
    monkeypatch.setattr(kb, "_resolve_worker_cli_toolsets", lambda _home: None)
    captured = {}

    class FakeProc:
        pid = 4242

    def fake_popen(cmd, *args, **kwargs):
        captured["cmd"] = list(cmd)
        return FakeProc()

    monkeypatch.setattr(subprocess, "Popen", fake_popen)
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    return captured, workspace


def test_routing_metadata_persists_as_optional_json(conn):
    metadata = _routing_metadata(cross_file=True)
    task_id = kb.create_task(
        conn,
        title="structured task",
        assignee="default",
        routing_metadata=metadata,
    )

    task = kb.get_task(conn, task_id)
    assert task is not None
    assert task.routing_metadata == metadata
    raw = conn.execute(
        "SELECT routing_metadata FROM tasks WHERE id = ?", (task_id,)
    ).fetchone()["routing_metadata"]
    assert json.loads(raw) == metadata

    default_id = kb.create_task(conn, title="legacy-compatible")
    assert kb.get_task(conn, default_id).routing_metadata is None


@pytest.mark.parametrize(
    "metadata",
    [
        {"prompt": "do not persist"},
        {"enabled": "true"},
        {"risk_level": "critical"},
        {"tool_count": True},
        {"tool_count": -1},
    ],
)
def test_routing_metadata_rejects_unknown_keys_and_wrong_types(conn, metadata):
    with pytest.raises(ValueError):
        kb.create_task(
            conn,
            title="invalid metadata",
            assignee="default",
            routing_metadata=metadata,
        )


def test_default_spawn_routes_opted_in_task_and_records_run_event(
    conn, kanban_home, monkeypatch, tmp_path
):
    (kanban_home / "config.yaml").write_text(
        "model:\n  default: gpt-5.6-luna\n  provider: openai-codex\n",
        encoding="utf-8",
    )
    task_id = kb.create_task(
        conn,
        title="cross-file task",
        assignee="default",
        routing_metadata=_routing_metadata(cross_file=True),
    )
    task = kb.get_task(conn, task_id)
    captured, workspace = _capture_spawn(monkeypatch, tmp_path)

    assert task is not None
    assert kb._default_spawn(task, str(workspace), conn=conn) == 4242
    cmd = captured["cmd"]
    model_index = cmd.index("-m")
    assert cmd[model_index + 1] == "gpt-5.6-terra"
    provider_index = cmd.index("--provider")
    assert cmd[provider_index + 1] == "openai-codex"

    routed = [event for event in kb.list_events(conn, task_id) if event.kind == "model_routed"]
    assert len(routed) == 1
    assert routed[0].run_id is None
    assert routed[0].payload == {
        "selected_provider": "openai-codex",
        "selected_model": "gpt-5.6-terra",
        "routed": True,
        "rule": "terra",
        "reason_codes": ["cross_file"],
        "explicit_pin": False,
        "bypass": False,
        "route_version": "v1",
    }


def test_no_route_preserves_argv_and_emits_no_event(
    conn, kanban_home, monkeypatch, tmp_path
):
    (kanban_home / "config.yaml").write_text(
        "model:\n  default: gpt-5.6-luna\n  provider: openai-codex\n",
        encoding="utf-8",
    )
    task_id = kb.create_task(
        conn,
        title="routine task",
        assignee="default",
        routing_metadata=_routing_metadata(),
    )
    task = kb.get_task(conn, task_id)
    captured, workspace = _capture_spawn(monkeypatch, tmp_path)

    assert task is not None
    assert kb._default_spawn(task, str(workspace), conn=conn) == 4242
    assert "-m" not in captured["cmd"]
    assert "--provider" not in captured["cmd"]
    assert not any(event.kind == "model_routed" for event in kb.list_events(conn, task_id))


@pytest.mark.parametrize("routing_metadata", [None, {}])
def test_missing_routing_metadata_spawn_stays_on_luna(
    conn, kanban_home, monkeypatch, tmp_path, routing_metadata
):
    (kanban_home / "config.yaml").write_text(
        "model:\n  default: gpt-5.6-luna\n  provider: openai-codex\n",
        encoding="utf-8",
    )
    task_id = kb.create_task(
        conn,
        title="unannotated task",
        assignee="default",
        routing_metadata=routing_metadata,
    )
    task = kb.get_task(conn, task_id)
    captured, workspace = _capture_spawn(monkeypatch, tmp_path)

    assert task is not None
    assert kb._default_spawn(task, str(workspace), conn=conn) == 4242
    cmd = captured["cmd"]
    assert "-m" not in cmd
    assert "--provider" not in cmd
    assert not any(event.kind == "model_routed" for event in kb.list_events(conn, task_id))


def test_explicit_pin_keeps_existing_argv_and_has_priority(
    conn, kanban_home, monkeypatch, tmp_path
):
    (kanban_home / "config.yaml").write_text(
        "model:\n  default: gpt-5.6-luna\n  provider: openai-codex\n",
        encoding="utf-8",
    )
    task_id = kb.create_task(
        conn,
        title="pinned task",
        assignee="default",
        model_override="gpt-5.5",
        provider_override="openrouter",
        routing_metadata=_routing_metadata(
            high_value=True,
            terra_insufficient=True,
            cross_file=True,
        ),
    )
    task = kb.get_task(conn, task_id)
    captured, workspace = _capture_spawn(monkeypatch, tmp_path)

    assert task is not None
    assert kb._default_spawn(task, str(workspace), conn=conn) == 4242
    cmd = captured["cmd"]
    model_index = cmd.index("-m")
    assert cmd[model_index + 1] == "gpt-5.5"
    assert cmd[model_index + 2:model_index + 4] == ["--provider", "openrouter"]
    assert not any(event.kind == "model_routed" for event in kb.list_events(conn, task_id))


def test_route_event_does_not_copy_task_prose(
    conn, kanban_home, monkeypatch, tmp_path
):
    (kanban_home / "config.yaml").write_text(
        "model:\n  default: gpt-5.6-luna\n  provider: openai-codex\n",
        encoding="utf-8",
    )
    secret_prompt = "SECRET_PROMPT_SHOULD_NOT_BE_IN_EVENT"
    task_id = kb.create_task(
        conn,
        title=secret_prompt,
        body=secret_prompt,
        assignee="default",
        routing_metadata=_routing_metadata(cross_file=True),
    )
    task = kb.get_task(conn, task_id)
    captured, workspace = _capture_spawn(monkeypatch, tmp_path)

    assert task is not None
    kb._default_spawn(task, str(workspace), conn=conn)
    event_payloads = [
        event.payload
        for event in kb.list_events(conn, task_id)
        if event.kind == "model_routed"
    ]
    assert secret_prompt not in json.dumps(event_payloads, ensure_ascii=False)


def test_sol_spawn_adds_argv_and_records_allowlisted_event(
    conn, kanban_home, monkeypatch, tmp_path
):
    (kanban_home / "config.yaml").write_text(
        "model:\n  default: gpt-5.6-luna\n  provider: openai-codex\n",
        encoding="utf-8",
    )
    task_id = kb.create_task(
        conn,
        title="deep reasoning task",
        assignee="default",
        routing_metadata=_routing_metadata(high_value=True, deep_reasoning_required=True),
    )
    task = kb.get_task(conn, task_id)
    captured, workspace = _capture_spawn(monkeypatch, tmp_path)

    assert task is not None
    assert kb._default_spawn(task, str(workspace), conn=conn) == 4242
    cmd = captured["cmd"]
    model_index = cmd.index("-m")
    assert cmd[model_index + 1] == "gpt-5.6-sol"
    provider_index = cmd.index("--provider")
    assert cmd[provider_index + 1] == "openai-codex"

    routed = [event for event in kb.list_events(conn, task_id) if event.kind == "model_routed"]
    assert len(routed) == 1
    assert routed[0].payload == {
        "selected_provider": "openai-codex",
        "selected_model": "gpt-5.6-sol",
        "routed": True,
        "rule": "sol",
        "reason_codes": ["high_value", "deep_reasoning_required"],
        "explicit_pin": False,
        "bypass": False,
        "route_version": "v1",
    }
