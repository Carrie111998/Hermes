import json

from tools import multi_agent_tool
from tools.registry import registry


class Parent:
    session_id = "parent-test"


def test_multi_agent_tool_registered_in_delegation_toolset():
    entry = registry.get_entry("multi_agent_orchestrate")

    assert entry is not None
    assert entry.toolset == "delegation"


def test_multi_agent_tool_clamps_loop_limit_and_uses_config_defaults(monkeypatch):
    captured = {}

    def fake_load_config():
        return {
            "multi_agent": {
                "enabled": True,
                "max_correction_loops": 2,
                "require_review": False,
                "roles": {
                    "developer": {"toolsets": ["terminal", "file"]},
                    "tester": {"toolsets": ["terminal"]},
                    "reviewer": {"toolsets": ["file"]},
                },
            }
        }

    def fake_workflow(**kwargs):
        captured.update(kwargs)
        return {"status": "TEST_PASSED", "run_id": "run-1"}

    monkeypatch.setattr(multi_agent_tool, "load_config", fake_load_config)
    monkeypatch.setattr(multi_agent_tool, "run_development_workflow", fake_workflow)

    raw = multi_agent_tool.multi_agent_orchestrate(
        objective="Bearbeite Issue #1",
        task_context={"issue_id": "#1", "issue_title": "Fix"},
        parent_agent=Parent(),
    )

    result = json.loads(raw)
    assert result["status"] == "TEST_PASSED"
    assert captured["max_correction_loops"] == 2
    assert captured["run_reviewer"] is False
    assert captured["developer_toolsets"] == ["terminal", "file"]
    assert captured["tester_toolsets"] == ["terminal"]


def test_multi_agent_tool_rejects_without_parent():
    raw = multi_agent_tool.multi_agent_orchestrate(
        objective="x",
        task_context={"issue_id": "#1"},
        parent_agent=None,
    )

    result = json.loads(raw)
    assert "error" in result
