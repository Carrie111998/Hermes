import json
from pathlib import Path

from tools.file_tools import (
    read_file_tool,
    register_fusion_readonly_root,
    search_tool,
    unregister_fusion_readonly_root,
    write_file_tool,
)
from toolsets import resolve_toolset


BLOCKED = {
    "terminal",
    "process",
    "execute_code",
    "write_file",
    "patch",
    "memory",
    "delegate_task",
    "send_message",
    "clarify",
    "cronjob",
    "browser_navigate",
    "image_generate",
    "text_to_speech",
}


def test_fusion_readonly_toolset_excludes_side_effect_tools():
    tools = set(resolve_toolset("fusion_readonly"))
    assert {"read_file", "search_files", "web_search", "web_extract", "skill_view", "session_search", "todo"}.issubset(tools)
    assert not (tools & BLOCKED)


def test_fusion_file_guard_confines_reads_searches_and_writes(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    inside = repo / "inside.txt"
    inside.write_text("needle\n", encoding="utf-8")
    outside = tmp_path / "outside.txt"
    outside.write_text("secret\n", encoding="utf-8")
    link = repo / "escape.txt"
    link.symlink_to(outside)

    task_id = "fusion-test-scope"
    register_fusion_readonly_root(task_id, str(repo))
    try:
        inside_read = json.loads(read_file_tool("inside.txt", task_id=task_id))
        assert "needle" in inside_read["content"]

        outside_read = json.loads(read_file_tool(str(outside), task_id=task_id))
        assert "Fusion read-only read denied" in outside_read["error"]

        symlink_read = json.loads(read_file_tool("escape.txt", task_id=task_id))
        assert "Fusion read-only read denied" in symlink_read["error"]

        search = json.loads(search_tool("needle", path=".", task_id=task_id))
        assert search["total_count"] == 1

        write_result = json.loads(write_file_tool("inside.txt", "mutate", task_id=task_id))
        assert "Fusion read-only write denied" in write_result["error"]
        assert inside.read_text(encoding="utf-8") == "needle\n"
    finally:
        unregister_fusion_readonly_root(task_id)
