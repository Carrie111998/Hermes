"""Regression coverage for excluding per-user agent config directories."""

import pytest

from agent.subdirectory_hints import SubdirectoryHintTracker


@pytest.mark.parametrize("dirname", [".claude", ".codex", ".cursor", ".config"])
def test_agent_config_directory_never_injects_context(tmp_path, dirname):
    target = tmp_path / dirname
    target.mkdir()
    (target / "CLAUDE.md").write_text("foreign agent instructions")

    tracker = SubdirectoryHintTracker(working_dir=str(tmp_path))

    assert tracker.check_tool_call(
        "read_file", {"path": str(target / "CLAUDE.md")}
    ) is None


def test_unrelated_hidden_project_directory_remains_eligible(tmp_path):
    target = tmp_path / ".project-meta"
    target.mkdir()
    (target / "AGENTS.md").write_text("project-specific instructions")

    tracker = SubdirectoryHintTracker(working_dir=str(tmp_path))
    result = tracker.check_tool_call(
        "read_file", {"path": str(target / "AGENTS.md")}
    )

    assert result is not None
    assert "project-specific instructions" in result
