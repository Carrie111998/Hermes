"""Consumers that never cross the model-facing persistence boundary keep the
full skill body.

The incomplete-load receipt exists because a tool result was truncated on its
way to the model. Four callers of skill_view() have no tool-result budget at
all -- preloaded/slash-command skills and cron skill injection put the content
into a prompt block, and the MCP/codex surface dispatches through
model_tools.handle_function_call, which never persists. Implementing the
truthful receipt INSIDE skill_view() would silently strip all four. This file
is the guard against that.
"""

import json

import pytest

FINAL_RULE = "ALWAYS run the migration before restarting the service."
BODY_LINE = "Some ordinary instruction prose that fills the body of the skill file."


def _write_skill(skills_dir, name, target_chars):
    d = skills_dir / name
    d.mkdir(parents=True, exist_ok=True)
    head = (
        f"---\nname: {name}\ndescription: A skill used by the incomplete-load tests\n"
        f"---\n\n# {name}\n\n## Procedure\n\n"
    )
    tail = f"\n## Final Governing Rule\n\n{FINAL_RULE}\n"
    filler = BODY_LINE + "\n"
    n = max(0, (target_chars - len(head) - len(tail)) // len(filler))
    (d / "SKILL.md").write_text(head + (filler * n) + tail, encoding="utf-8")


@pytest.fixture
def oversized_home(tmp_path, monkeypatch):
    from tools.skills_tool import reset_skill_view_dedup

    home = tmp_path / ".hermes"
    skills = home / "skills"
    skills.mkdir(parents=True)
    _write_skill(skills, "oversized-skill", 120_000)
    monkeypatch.setenv("HERMES_HOME", str(home))
    reset_skill_view_dedup()
    yield home
    reset_skill_view_dedup()


class TestDirectConsumersGetTheFullBody:
    def test_skill_view_itself_is_unchanged(self, oversized_home):
        from tools.skills_tool import skill_view

        payload = json.loads(skill_view("oversized-skill"))
        assert payload["success"] is True
        assert len(payload["content"]) > 100_000
        assert FINAL_RULE in payload["content"]
        assert "load_status" not in payload

    def test_preloaded_slash_command_path_gets_the_full_body(self, oversized_home):
        from agent.skill_commands import _load_skill_payload, build_preloaded_skills_prompt

        payload, _skill_dir, name = _load_skill_payload(
            "oversized-skill", task_id="t-preload"
        )
        assert name == "oversized-skill"
        assert FINAL_RULE in payload["content"]

        prompt, names, missing = build_preloaded_skills_prompt(["oversized-skill"])
        assert names == ["oversized-skill"] and missing == []
        assert FINAL_RULE in prompt
        assert "[SKILL_INCOMPLETE:" not in prompt

    def test_cron_skill_injection_gets_the_full_body(self, oversized_home):
        """Both cron/scheduler.py call sites do json.loads(skill_view(name))."""
        from tools.skills_tool import skill_view
        from agent.skill_utils import normalize_skill_lookup_name

        loaded = json.loads(skill_view(normalize_skill_lookup_name("oversized-skill")))
        assert loaded["success"] is True
        assert FINAL_RULE in loaded["content"]

        readiness = json.loads(skill_view("oversized-skill"))
        assert readiness.get("readiness_status") == "available"
        assert FINAL_RULE in readiness["content"]

    def test_mcp_codex_surface_gets_the_full_body(self, oversized_home):
        """skill_view over MCP dispatches through model_tools.handle_function_call,
        which never calls maybe_persist_tool_result."""
        import model_tools
        from agent.transports.hermes_tools_mcp_server import EXPOSED_TOOLS

        assert "skill_view" in EXPOSED_TOOLS
        raw = model_tools.handle_function_call(
            "skill_view", {"name": "oversized-skill"}, task_id="t-mcp"
        )
        payload = json.loads(raw)
        assert payload["success"] is True
        assert FINAL_RULE in payload["content"]
        assert "load_status" not in payload
