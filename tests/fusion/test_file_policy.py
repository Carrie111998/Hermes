import json
from pathlib import Path

from tools.file_tools import (
    patch_tool,
    read_file_tool,
    register_fusion_readonly_root,
    register_fusion_write_root,
    search_tool,
    unregister_fusion_readonly_root,
    unregister_fusion_write_root,
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

SPIKE_BLOCKED = BLOCKED - {"write_file", "patch"}


def test_fusion_readonly_toolset_excludes_side_effect_tools():
    tools = set(resolve_toolset("fusion_readonly"))
    assert {"read_file", "search_files", "web_search", "web_extract", "skill_view", "session_search", "todo"}.issubset(tools)
    assert not (tools & BLOCKED)


def test_fusion_spike_toolset_allows_only_scoped_file_writes():
    tools = set(resolve_toolset("fusion_spike"))
    assert {"read_file", "search_files", "write_file", "patch", "skill_view", "session_search", "todo"}.issubset(tools)
    assert not (tools & SPIKE_BLOCKED)


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


def test_fusion_spike_write_root_allows_writes_only_inside_worktree(tmp_path):
    worktree = tmp_path / "worktree"
    worktree.mkdir()
    inside = worktree / "inside.txt"
    inside.write_text("needle\n", encoding="utf-8")
    outside = tmp_path / "outside.txt"
    outside.write_text("secret\n", encoding="utf-8")
    link = worktree / "escape.txt"
    link.symlink_to(outside)

    task_id = "fusion-spike-scope"
    register_fusion_write_root(task_id, str(worktree))
    try:
        write_result = json.loads(write_file_tool("new.txt", "created\n", task_id=task_id))
        assert not write_result.get("error")
        assert (worktree / "new.txt").read_text(encoding="utf-8") == "created\n"

        patch_result = json.loads(patch_tool(path="inside.txt", old_string="needle", new_string="changed", task_id=task_id))
        assert not patch_result.get("error")
        assert inside.read_text(encoding="utf-8") == "changed\n"

        outside_write = json.loads(write_file_tool(str(outside), "mutate", task_id=task_id))
        assert "Fusion spike write denied" in outside_write["error"]
        assert outside.read_text(encoding="utf-8") == "secret\n"

        symlink_write = json.loads(write_file_tool("escape.txt", "mutate", task_id=task_id))
        assert "Fusion spike write denied" in symlink_write["error"]
        assert outside.read_text(encoding="utf-8") == "secret\n"

        search = json.loads(search_tool("created", path=".", task_id=task_id))
        assert search["total_count"] == 1
    finally:
        unregister_fusion_write_root(task_id)
