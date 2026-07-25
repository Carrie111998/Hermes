from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "skills" / "productivity" / "todoist-kanban" / "scripts" / "todoist_kanban.py"


def load_module():
    spec = importlib.util.spec_from_file_location("todoist_kanban_skill", SCRIPT)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture()
def tk(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    return load_module()


def test_classifies_only_explicit_agent_tasks(tk):
    cfg = tk.BridgeConfig()
    assert tk.classify_task({"id": "1", "content": "Book dentist", "labels": []}, cfg)["class"] == "human_commitment"
    assert tk.classify_task({"id": "2", "content": "Fix script", "labels": ["hermes"]}, cfg)["class"] == "agent_capable"
    assert tk.classify_task({"id": "3", "content": "Call bank", "labels": ["hermes", "manual"]}, cfg)["class"] == "human_commitment"
    assert tk.classify_task({"id": "4", "content": "Done", "labels": ["hermes"], "is_completed": True}, cfg)["class"] == "ignore"


def test_idempotency_key_is_stable_and_todoist_scoped(tk):
    assert tk.idempotency_key_for({"id": "123", "content": "A"}) == "todoist:123"
    assert tk.idempotency_key_for({"id": "123", "content": "B"}) == "todoist:123"


def test_create_handoff_reuses_kanban_idempotency(tk, tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_KANBAN_DB", str(tmp_path / "kanban.db"))
    task = {"id": "123", "content": "Refactor report", "labels": ["hermes"], "priority": 4}
    cfg = tk.BridgeConfig(default_assignee="worker")

    first = tk.create_handoff(task, cfg)
    second = tk.create_handoff({**task, "content": "Changed title"}, cfg)

    assert first["kanban_task_id"] == second["kanban_task_id"]
    ledger = json.loads((tmp_path / "todoist-kanban" / "ledger.json").read_text(encoding="utf-8"))
    assert ledger["tasks"]["123"]["idempotency_key"] == "todoist:123"


def test_human_task_does_not_create_handoff(tk, tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_KANBAN_DB", str(tmp_path / "kanban.db"))
    result = tk.create_handoff({"id": "h1", "content": "Buy milk", "labels": []}, tk.BridgeConfig())
    assert result["created"] is False
    assert not (tmp_path / "todoist-kanban" / "ledger.json").exists()


def test_webhook_filter_silent_for_human_task(tk, monkeypatch, capsys):
    payload = {"event_name": "item:added", "event_data": {"id": "1", "content": "Errand", "labels": []}}
    monkeypatch.setattr(sys, "stdin", type("In", (), {"read": lambda self: json.dumps(payload)})())
    assert tk.main(["webhook-filter"]) == 0
    assert capsys.readouterr().out == ""


def test_webhook_filter_emits_agent_payload(tk, monkeypatch, capsys):
    payload = {"event_name": "item:added", "event_data": {"id": "1", "content": "Write summary", "labels": ["agent"]}}
    monkeypatch.setattr(sys, "stdin", type("In", (), {"read": lambda self: json.dumps(payload)})())
    assert tk.main(["webhook-filter"]) == 0
    out = json.loads(capsys.readouterr().out)
    assert out["classification"]["class"] == "agent_capable"
    assert out["idempotency_key"] == "todoist:1"


def test_postback_posts_completed_kanban_once(tk, tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_KANBAN_DB", str(tmp_path / "kanban.db"))
    task = {"id": "123", "content": "Agent work", "labels": ["hermes"]}
    handoff = tk.create_handoff(task, tk.BridgeConfig(default_assignee="worker"))

    from hermes_cli import kanban_db as kb

    with kb.connect_closing() as conn:
        kb.complete_task(conn, handoff["kanban_task_id"], result="Evidence: changed docs and ran tests.")

    posted: list[tuple[str, str]] = []

    class FakeClient:
        def add_comment(self, task_id, content):
            posted.append((task_id, content))
            return {"id": "c1"}

    monkeypatch.setattr(tk, "TodoistClient", lambda: FakeClient())
    assert tk.main(["postback"]) == 0
    assert tk.main(["postback"]) == 0

    assert len(posted) == 1
    assert posted[0][0] == "123"
    assert "Evidence: changed docs" in posted[0][1]


@pytest.mark.parametrize("period", ["daily", "weekly"])
def test_review_summarizes_recent_handoffs(tk, tmp_path, capsys, period):
    ledger = {
        "version": 1,
        "tasks": {
            "123": {
                "todoist_task_id": "123",
                "kanban_task_id": "t_abc",
                "content": "Agent work",
                "classification": {"class": "agent_capable"},
                "last_seen_at": 4_000_000_000,
            }
        },
    }
    path = tmp_path / "todoist-kanban"
    path.mkdir()
    (path / "ledger.json").write_text(json.dumps(ledger), encoding="utf-8")
    monkey = pytest.MonkeyPatch()
    monkey.setattr(tk.time, "time", lambda: 4_000_000_010)
    try:
        assert tk.main(["review", period, "--format", "json"]) == 0
    finally:
        monkey.undo()
    out = json.loads(capsys.readouterr().out)
    assert out["period"] == period
    assert out["kanban_handoffs"] == 1
    assert out["handoffs"][0]["todoist_task_id"] == "123"
