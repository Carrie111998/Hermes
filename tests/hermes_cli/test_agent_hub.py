import json
from pathlib import Path

import pytest

from hermes_cli import agent_hub


@pytest.fixture
def isolated_hub(tmp_path, monkeypatch):
    monkeypatch.setattr(agent_hub, "get_hermes_home", lambda: tmp_path)
    monkeypatch.setattr(agent_hub, "_resolve_skill_files", lambda names, home: [])
    return tmp_path


def test_harness_catalog_reports_runtime_availability(monkeypatch):
    monkeypatch.setattr(
        agent_hub.shutil,
        "which",
        lambda command: f"/usr/bin/{command}" if command == "codex" else None,
    )

    catalog = {item["id"]: item for item in agent_hub.harness_catalog()}

    assert catalog["codex"]["available"] is True
    assert catalog["claude"]["available"] is False
    assert catalog["antigravity"]["available"] is False


def test_codex_conversation_persists_and_resumes(isolated_hub, monkeypatch):
    monkeypatch.setattr(agent_hub.shutil, "which", lambda command: f"/bin/{command}")
    calls = []

    def fake_run(command, **kwargs):
        calls.append((command, kwargs))
        turn = len(calls)
        stdout = "\n".join(
            [
                json.dumps({"type": "thread.started", "thread_id": "native-123"}),
                json.dumps(
                    {
                        "type": "item.completed",
                        "item": {
                            "type": "agent_message",
                            "text": f"answer {turn}",
                        },
                    }
                ),
            ]
        )
        return agent_hub.subprocess.CompletedProcess(command, 0, stdout, "")

    monkeypatch.setattr(agent_hub.subprocess, "run", fake_run)

    first = agent_hub.run_turn(
        harness="codex",
        prompt="Build the feature",
        conversation_id="conversation-1",
        cwd=str(isolated_hub),
    )
    second = agent_hub.run_turn(
        harness="codex",
        prompt="Now test it",
        conversation_id="conversation-1",
        cwd=str(isolated_hub),
    )

    assert first["native_session_id"] == "native-123"
    assert second["messages"][-1]["content"] == "answer 2"
    assert calls[0][0][:3] == ["codex", "exec", "--json"]
    assert calls[1][0][:4] == ["codex", "exec", "resume", "--json"]
    assert "native-123" in calls[1][0]
    assert [row["id"] for row in agent_hub.list_conversations()] == [
        "conversation-1"
    ]


def test_selected_skills_and_attachments_are_added_to_native_prompt(
    isolated_hub, monkeypatch
):
    skill_path = isolated_hub / "skills" / "review" / "SKILL.md"
    skill_path.parent.mkdir(parents=True)
    skill_path.write_text("# Review", encoding="utf-8")
    monkeypatch.setattr(agent_hub.shutil, "which", lambda command: f"/bin/{command}")
    monkeypatch.setattr(
        agent_hub,
        "_resolve_skill_files",
        lambda names, home: [skill_path],
    )
    captured = {}

    def fake_run(command, **kwargs):
        captured["prompt"] = kwargs["input"]
        stdout = json.dumps(
            {
                "type": "item.completed",
                "item": {"type": "agent_message", "text": "done"},
            }
        )
        return agent_hub.subprocess.CompletedProcess(command, 0, stdout, "")

    monkeypatch.setattr(agent_hub.subprocess, "run", fake_run)

    agent_hub.run_turn(
        harness="codex",
        prompt="Review this",
        conversation_id="with-context",
        cwd=str(isolated_hub),
        skills=["review"],
        attachments=["/tmp/example.patch"],
    )

    assert str(skill_path) in captured["prompt"]
    assert "/tmp/example.patch" in captured["prompt"]
    assert captured["prompt"].endswith("Review this")


def test_discord_binding_round_trip(isolated_hub):
    saved = agent_hub.save_binding(
        "123",
        "claude",
        channel_name="Guild / #dev",
        skills=["review", "tests"],
        cwd=str(isolated_hub),
    )

    assert saved["harness"] == "claude"
    assert agent_hub.load_bindings()["123"]["skills"] == ["review", "tests"]
    assert agent_hub.delete_binding("123") is True
    assert agent_hub.load_bindings() == {}
