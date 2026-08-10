from __future__ import annotations

import argparse
import json
from pathlib import Path
from unittest.mock import patch

import pytest

from hermes_cli import kanban as kanban_cli
from hermes_cli import kanban_db as kb
from hermes_cli.commands import COMMAND_REGISTRY


@pytest.fixture
def kanban_home(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    kb.init_db()
    return home


def _run_cli(*argv: str) -> int:
    root = argparse.ArgumentParser()
    subparsers = root.add_subparsers(dest="cmd")
    kanban_cli.build_parser(subparsers)
    args = root.parse_args(["kanban", *argv])
    return kanban_cli.kanban_command(args)


def _parse_cli(*argv: str):
    root = argparse.ArgumentParser()
    subparsers = root.add_subparsers(dest="cmd")
    kanban_cli.build_parser(subparsers)
    return root.parse_args(["kanban", *argv])


def test_recover_triage_holds_task_blocked_without_rewriting_spec(kanban_home):
    with kb.connect() as conn:
        parent = kb.create_task(conn, title="done parent")
        assert kb.complete_task(conn, parent)
        task_id = kb.create_task(
            conn,
            title="preserve title",
            body="preserve body",
            assignee="sourcing",
            parents=[parent],
            triage=True,
        )

    with kb.connect() as conn:
        assert kb.recover_triage_task(
            conn,
            task_id,
            reason="completion evidence already exists",
            actor="operator",
        )

    with kb.connect() as conn:
        task = kb.get_task(conn, task_id)
        events = kb.list_events(conn, task_id)

    assert task.status == "blocked"
    assert task.title == "preserve title"
    assert task.body == "preserve body"
    assert task.assignee == "sourcing"
    event = next(event for event in events if event.kind == "triage_recovered")
    assert event.payload == {
        "actor": "operator",
        "reason": "completion evidence already exists",
        "status": "blocked",
    }


def test_recover_triage_rejects_blank_reason_and_non_triage(kanban_home):
    with kb.connect() as conn:
        triage_id = kb.create_task(conn, title="triage", triage=True)
        ready_id = kb.create_task(conn, title="ready")
        with pytest.raises(ValueError, match="reason cannot be blank"):
            kb.recover_triage_task(conn, triage_id, reason="   ", actor="operator")
        assert kb.recover_triage_task(
            conn,
            ready_id,
            reason="not triage",
            actor="operator",
        ) is False
        assert kb.get_task(conn, triage_id).status == "triage"
        assert kb.get_task(conn, ready_id).status == "ready"


def test_recover_triage_hold_survives_readiness_sweep(kanban_home):
    with kb.connect() as conn:
        task_id = kb.create_task(conn, title="must remain held", triage=True)
        assert kb.recover_triage_task(
            conn,
            task_id,
            reason="operator must complete explicitly",
            actor="operator",
        )

        assert kb.recompute_ready(conn) == 0
        assert kb.get_task(conn, task_id).status == "blocked"


def test_recover_triage_rejects_kanban_worker_context(kanban_home, monkeypatch):
    with kb.connect() as conn:
        task_id = kb.create_task(conn, title="human breakpoint", triage=True)
        monkeypatch.setenv("HERMES_KANBAN_TASK", "t_worker")

        with pytest.raises(PermissionError, match="operator-only"):
            kb.recover_triage_task(
                conn,
                task_id,
                reason="worker attempted recovery",
                actor="worker",
            )

        monkeypatch.delenv("HERMES_KANBAN_TASK")
        assert kb.get_task(conn, task_id).status == "triage"


def test_cli_recover_triage_is_status_only_and_json(kanban_home, capsys):
    with kb.connect() as conn:
        task_id = kb.create_task(
            conn,
            title="already specified",
            body="complete evidence",
            triage=True,
        )

    rc = _run_cli(
        "recover-triage",
        task_id,
        "--reason",
        "completion contract satisfied",
        "--json",
    )

    assert rc == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload == {
        "task_id": task_id,
        "recovered": True,
        "status": "blocked",
    }
    with kb.connect() as conn:
        task = kb.get_task(conn, task_id)
    assert task.title == "already specified"
    assert task.body == "complete evidence"
    assert task.status == "blocked"


def test_cli_recover_triage_requires_reason(kanban_home):
    with pytest.raises(SystemExit) as excinfo:
        _run_cli("recover-triage", "t_missing")
    assert excinfo.value.code == 2


def test_cli_recover_triage_rejects_delegated_child_context(
    kanban_home,
    monkeypatch,
    capsys,
):
    with kb.connect() as conn:
        task_id = kb.create_task(conn, title="operator only", triage=True)
    monkeypatch.setenv("HERMES_DELEGATED_CHILD_CONTEXT", "1")

    with patch.object(kb, "recover_triage_task", wraps=kb.recover_triage_task) as mutator:
        rc = _run_cli(
            "recover-triage",
            task_id,
            "--reason",
            "must not land",
        )

    assert rc == 1
    assert "delegate_task child contexts cannot mutate" in capsys.readouterr().err
    assert mutator.call_count == 0
    monkeypatch.delenv("HERMES_DELEGATED_CHILD_CONTEXT")
    with kb.connect() as conn:
        assert kb.get_task(conn, task_id).status == "triage"


def test_recover_triage_is_classified_as_delegated_child_mutation(kanban_home):
    args = _parse_cli(
        "recover-triage",
        "t_example",
        "--reason",
        "operator only",
    )
    with patch(
        "agent.delegation_context.is_delegated_child_process_context",
        return_value=True,
    ):
        assert kanban_cli._is_delegated_child_cli_mutation(args) is True


def test_recover_triage_is_listed_in_central_command_registry():
    command = next(item for item in COMMAND_REGISTRY if item.name == "kanban")
    assert "recover-triage" in command.subcommands
