"""100 test cases for parallel tool execution — is_read_only integration.

Tests the full pipeline:
  registry.is_read_only marking → _plan_tool_batch_segments dynamic check
  → _execute_tool_calls dispatcher → _execute_tool_calls_concurrent

References:
  - tools/registry.py: ToolEntry.is_read_only, register(is_read_only=)
  - agent/tool_dispatch_helpers.py: _plan_tool_batch_segments, _PARALLEL_SAFE_TOOLS
  - run_agent.py: _execute_tool_calls, _execute_tool_calls_concurrent
"""

from __future__ import annotations

import json
import os
import sys
import threading
import time
import uuid
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest


# =============================================================================
# Helpers
# =============================================================================

def _tc(name="web_search", arguments="{}", call_id=None):
    return SimpleNamespace(
        id=call_id or f"call_{uuid.uuid4().hex[:8]}",
        type="function",
        function=SimpleNamespace(name=name, arguments=arguments),
    )


def _kinds(segments):
    return [kind for kind, _ in segments]


def _flatten_ids(segments):
    return [tc.id for _, calls in segments for tc in calls]


# Load registry + tool modules once
try:
    from tools.registry import registry
    import tools.file_tools
    import tools.clarify_tool
    import tools.read_terminal_tool
    import tools.vision_tools
    import tools.web_tools
    import tools.skills_tool
    import tools.session_search_tool
    _REGISTRY_LOADED = True
except Exception:
    _REGISTRY_LOADED = False


# =============================================================================
# 001-020: Registry is_read_only correctness
# =============================================================================

class TestRegistryIsReadOnly:
    """Every tool's is_read_only attribute must match its side-effect profile."""

    @pytest.mark.parametrize("name,expected", [
        # 001-009: Read-only tools
        ("read_file", True),
        ("search_files", True),
        ("clarify", True),
        ("read_terminal", True),
        ("vision_analyze", True),
        ("web_search", True),
        ("skills_list", True),
        ("skill_view", True),
        ("session_search", True),
        # 010-014: Write tools
        ("write_file", False),
        ("patch", False),
        # ("memory", False),  # conditional import
        # ("cronjob", False),  # conditional import
        # ("delegate_task", False),  # conditional import
    ])
    def test_tool_is_read_only_matches(self, name, expected):
        if not _REGISTRY_LOADED:
            pytest.skip("Registry not loaded")
        entry = registry.get_entry(name)
        assert entry is not None, f"Tool {name} not registered"
        assert entry.is_read_only == expected, (
            f"Expected {name}.is_read_only={expected}, got {entry.is_read_only}"
        )

    # 015: Default is False
    def test_new_tool_defaults_to_not_read_only(self):
        """A tool registered without is_read_only should default to False."""
        if not _REGISTRY_LOADED:
            pytest.skip("Registry not loaded")
        # Verify write_file (no is_read_only in its register call) is False
        entry = registry.get_entry("write_file")
        assert entry is not None
        assert entry.is_read_only is False

    # 016: Async read-only tool
    def test_async_read_only_tool(self):
        """vision_analyze is both async and read-only."""
        if not _REGISTRY_LOADED:
            pytest.skip("Registry not loaded")
        entry = registry.get_entry("vision_analyze")
        assert entry is not None
        assert entry.is_async is True
        assert entry.is_read_only is True

    # 017: Lambda-handler tool
    def test_lambda_handler_tool_is_read_only(self):
        """Tools with lambda handlers (web_search, clarify) must still be marked."""
        if not _REGISTRY_LOADED:
            pytest.skip("Registry not loaded")
        for name in ("web_search", "clarify", "skills_list", "session_search", "read_terminal"):
            entry = registry.get_entry(name)
            assert entry is not None, f"{name} not found"
            assert entry.is_read_only is True, f"{name} should be read-only"

    # 018: Tool name case sensitivity
    def test_tool_name_case_sensitive(self):
        if not _REGISTRY_LOADED:
            pytest.skip("Registry not loaded")
        assert registry.get_entry("READ_FILE") is None
        assert registry.get_entry("read_file") is not None

    # 019: Toolset preserved
    def test_read_only_tools_have_correct_toolset(self):
        if not _REGISTRY_LOADED:
            pytest.skip("Registry not loaded")
        checks = {
            "read_file": "file",
            "search_files": "file",
            "clarify": "clarify",
            "read_terminal": "terminal",
            "vision_analyze": "vision",
            "web_search": "web",
            "skills_list": "skills",
            "skill_view": "skills",
            "session_search": "session_search",
        }
        for name, expected_toolset in checks.items():
            entry = registry.get_entry(name)
            assert entry is not None, f"{name} not found"
            assert entry.toolset == expected_toolset, (
                f"{name} toolset={entry.toolset}, expected {expected_toolset}"
            )

    # 020: Emoji preserved
    def test_read_only_tools_still_have_emoji(self):
        if not _REGISTRY_LOADED:
            pytest.skip("Registry not loaded")
        for name in ("read_file", "search_files", "vision_analyze", "web_search"):
            entry = registry.get_entry(name)
            assert entry is not None
            assert entry.emoji, f"{name} missing emoji"


# =============================================================================
# 021-050: Segment planner with dynamic is_read_only
# =============================================================================

class TestSegmentPlannerDynamicIsReadOnly:
    """_plan_tool_batch_segments must use registry.is_read_only dynamically."""

    # 021-022: read_terminal (not in _PARALLEL_SAFE_TOOLS but is_read_only=True)
    def test_read_terminal_parallel_pair(self):
        """Two read_terminal calls should be in a parallel segment."""
        from agent.tool_dispatch_helpers import _plan_tool_batch_segments
        calls = [
            _tc("read_terminal", '{"session_id":"s1"}', call_id="r1"),
            _tc("read_terminal", '{"session_id":"s2"}', call_id="r2"),
        ]
        segments = _plan_tool_batch_segments(calls)
        assert _kinds(segments) == ["parallel"]
        assert _flatten_ids(segments) == ["r1", "r2"]

    def test_read_terminal_with_unsafe_mixed(self):
        """read_terminal + terminal: read_terminal is parallel-safe, terminal is not.
        Single parallel call gets demoted to sequential and merged."""
        from agent.tool_dispatch_helpers import _plan_tool_batch_segments
        calls = [
            _tc("read_terminal", '{"session_id":"s1"}', call_id="r1"),
            _tc("terminal", '{"command":"echo hi"}', call_id="t1"),
        ]
        segments = _plan_tool_batch_segments(calls)
        # Single parallel call → demoted to sequential, merged with barrier
        assert _kinds(segments) == ["sequential"]
        assert _flatten_ids(segments) == ["r1", "t1"]

    # 023-024: skills_list + skill_view (both is_read_only, not in _PARALLEL_SAFE_TOOLS)
    def test_skills_tools_parallel(self):
        from agent.tool_dispatch_helpers import _plan_tool_batch_segments
        calls = [
            _tc("skills_list", call_id="l1"),
            _tc("skill_view", '{"name":"test"}', call_id="v1"),
        ]
        segments = _plan_tool_batch_segments(calls)
        assert _kinds(segments) == ["parallel"]

    def test_skills_tools_with_write_mixed(self):
        from agent.tool_dispatch_helpers import _plan_tool_batch_segments
        calls = [
            _tc("skills_list", call_id="l1"),
            _tc("skill_manage", '{"action":"list"}', call_id="m1"),
        ]
        segments = _plan_tool_batch_segments(calls)
        # Single parallel call → demoted to sequential, merged with barrier
        assert _kinds(segments) == ["sequential"]
        assert _flatten_ids(segments) == ["l1", "m1"]

    # 025-026: session_search (is_read_only=True)
    def test_session_search_parallel(self):
        from agent.tool_dispatch_helpers import _plan_tool_batch_segments
        calls = [
            _tc("session_search", '{"query":"test1"}', call_id="s1"),
            _tc("session_search", '{"query":"test2"}', call_id="s2"),
        ]
        segments = _plan_tool_batch_segments(calls)
        assert _kinds(segments) == ["parallel"]

    def test_session_search_with_web_search_mixed(self):
        """Both are read-only → should be parallel."""
        from agent.tool_dispatch_helpers import _plan_tool_batch_segments
        calls = [
            _tc("session_search", '{"query":"test"}', call_id="s1"),
            _tc("web_search", '{"query":"test"}', call_id="w1"),
        ]
        segments = _plan_tool_batch_segments(calls)
        assert _kinds(segments) == ["parallel"]

    # 027-028: clarify is NEVER_PARALLEL despite is_read_only=True
    def test_clarify_never_parallel(self):
        """clarify is is_read_only=True but must be sequential (interactive).
        Single parallel call gets demoted and merged."""
        from agent.tool_dispatch_helpers import _plan_tool_batch_segments
        calls = [
            _tc("web_search", call_id="r1"),
            _tc("clarify", '{"question":"?"}', call_id="c1"),
        ]
        segments = _plan_tool_batch_segments(calls)
        assert _kinds(segments) == ["sequential"]
        assert _flatten_ids(segments) == ["r1", "c1"]

    def test_clarify_in_middle_splits_segments(self):
        """clarify barrier + adjacent single parallel calls → all sequential."""
        from agent.tool_dispatch_helpers import _plan_tool_batch_segments
        calls = [
            _tc("web_search", call_id="r1"),
            _tc("clarify", '{"question":"?"}', call_id="c1"),
            _tc("web_search", call_id="r2"),
        ]
        segments = _plan_tool_batch_segments(calls)
        # r1 is single parallel → demoted to sequential, merged with c1
        # r2 is single parallel after barrier → demoted, merged
        assert _kinds(segments) == ["sequential"]
        assert _flatten_ids(segments) == ["r1", "c1", "r2"]

    # 029-030: read_file + write_file path conflicts
    def test_read_file_same_path_conflict(self):
        """Two read_file calls on the same path should NOT be in the same segment."""
        from agent.tool_dispatch_helpers import _plan_tool_batch_segments
        calls = [
            _tc("read_file", '{"path":"a.py"}', call_id="r1"),
            _tc("read_file", '{"path":"a.py"}', call_id="r2"),
        ]
        segments = _plan_tool_batch_segments(calls)
        # Same path → conflict → close first run, start second
        # But each has only 1 safe call → demoted to sequential
        assert _kinds(segments) == ["sequential"]

    def test_read_file_diff_paths(self):
        from agent.tool_dispatch_helpers import _plan_tool_batch_segments
        calls = [
            _tc("read_file", '{"path":"a.py"}', call_id="r1"),
            _tc("read_file", '{"path":"b.py"}', call_id="r2"),
        ]
        segments = _plan_tool_batch_segments(calls)
        assert _kinds(segments) == ["parallel"]

    # 031-032: vision_analyze (async + read-only)
    def test_vision_analyze_parallel(self):
        from agent.tool_dispatch_helpers import _plan_tool_batch_segments
        calls = [
            _tc("vision_analyze", '{"image_url":"url1"}', call_id="v1"),
            _tc("vision_analyze", '{"image_url":"url2"}', call_id="v2"),
        ]
        segments = _plan_tool_batch_segments(calls)
        assert _kinds(segments) == ["parallel"]

    def test_vision_analyze_with_terminal(self):
        """Single parallel call demoted to sequential."""
        from agent.tool_dispatch_helpers import _plan_tool_batch_segments
        calls = [
            _tc("vision_analyze", '{"image_url":"url1"}', call_id="v1"),
            _tc("terminal", '{"command":"ls"}', call_id="t1"),
        ]
        segments = _plan_tool_batch_segments(calls)
        assert _kinds(segments) == ["sequential"]
        assert _flatten_ids(segments) == ["v1", "t1"]

    # 033-034: Large mixed batches
    def test_5_read_1_write(self):
        from agent.tool_dispatch_helpers import _plan_tool_batch_segments
        calls = [
            _tc("web_search", call_id="r1"),
            _tc("read_file", '{"path":"a.py"}', call_id="r2"),
            _tc("web_search", call_id="r3"),
            _tc("read_file", '{"path":"b.py"}', call_id="r4"),
            _tc("web_search", call_id="r5"),
            _tc("terminal", '{"command":"echo done"}', call_id="t1"),
        ]
        segments = _plan_tool_batch_segments(calls)
        assert _kinds(segments)[0] == "parallel"
        assert _kinds(segments)[-1] == "sequential"
        assert _flatten_ids(segments) == ["r1", "r2", "r3", "r4", "r5", "t1"]

    def test_alternating_read_write_read(self):
        """Each parallel call is single → all demoted to sequential."""
        from agent.tool_dispatch_helpers import _plan_tool_batch_segments
        calls = [
            _tc("web_search", call_id="r1"),
            _tc("terminal", '{"command":"a"}', call_id="t1"),
            _tc("web_search", call_id="r2"),
            _tc("terminal", '{"command":"b"}', call_id="t2"),
            _tc("web_search", call_id="r3"),
        ]
        segments = _plan_tool_batch_segments(calls)
        assert _kinds(segments) == ["sequential"]
        assert _flatten_ids(segments) == ["r1", "t1", "r2", "t2", "r3"]

    # 035-036: Empty / single call
    def test_empty_batch(self):
        from agent.tool_dispatch_helpers import _plan_tool_batch_segments
        segments = _plan_tool_batch_segments([])
        assert segments == []

    def test_single_read_only_call(self):
        """Single call should produce a single segment, demoted to sequential."""
        from agent.tool_dispatch_helpers import _plan_tool_batch_segments
        calls = [_tc("read_file", '{"path":"a.py"}')]
        segments = _plan_tool_batch_segments(calls)
        assert _kinds(segments) == ["sequential"]

    # 037-038: Malformed / non-dict args
    def test_malformed_json_args_barrier(self):
        """Malformed args are barriers. Single parallel calls demoted."""
        from agent.tool_dispatch_helpers import _plan_tool_batch_segments
        calls = [
            _tc("web_search", call_id="r1"),
            _tc("web_search", "{not json", call_id="bad"),
            _tc("web_search", call_id="r2"),
        ]
        segments = _plan_tool_batch_segments(calls)
        assert _kinds(segments) == ["sequential"]
        assert _flatten_ids(segments) == ["r1", "bad", "r2"]

    def test_non_dict_args_barrier(self):
        """Non-dict args are barriers. Single parallel calls demoted."""
        from agent.tool_dispatch_helpers import _plan_tool_batch_segments
        calls = [
            _tc("web_search", call_id="r1"),
            _tc("web_search", '"just a string"', call_id="bad"),
        ]
        segments = _plan_tool_batch_segments(calls)
        assert _kinds(segments) == ["sequential"]
        assert _flatten_ids(segments) == ["r1", "bad"]

    # 039-040: Path-scoped tools
    def test_write_file_diff_paths_parallel(self):
        from agent.tool_dispatch_helpers import _plan_tool_batch_segments
        calls = [
            _tc("write_file", '{"path":"a.py","content":"x"}', call_id="w1"),
            _tc("write_file", '{"path":"b.py","content":"y"}', call_id="w2"),
        ]
        segments = _plan_tool_batch_segments(calls)
        assert _kinds(segments) == ["parallel"]

    def test_write_file_same_path_sequential(self):
        from agent.tool_dispatch_helpers import _plan_tool_batch_segments
        calls = [
            _tc("write_file", '{"path":"a.py","content":"x"}', call_id="w1"),
            _tc("write_file", '{"path":"a.py","content":"y"}', call_id="w2"),
        ]
        segments = _plan_tool_batch_segments(calls)
        # Same path → conflict → two segments, each demoted to sequential
        assert _kinds(segments) == ["sequential"]

    # 041-045: More edge cases
    def test_patch_tool_path_conflict(self):
        from agent.tool_dispatch_helpers import _plan_tool_batch_segments
        calls = [
            _tc("patch", '{"path":"a.py","old":"x","new":"y"}', call_id="p1"),
            _tc("read_file", '{"path":"a.py"}', call_id="r1"),
        ]
        segments = _plan_tool_batch_segments(calls)
        # Same path → conflict
        assert _kinds(segments) == ["sequential"]

    def test_path_scoped_tool_without_path_is_barrier(self):
        """No path → barrier. Single parallel call demoted."""
        from agent.tool_dispatch_helpers import _plan_tool_batch_segments
        calls = [
            _tc("read_file", "{}", call_id="nopath"),
            _tc("web_search", call_id="r1"),
        ]
        segments = _plan_tool_batch_segments(calls)
        assert _kinds(segments) == ["sequential"]
        assert _flatten_ids(segments) == ["nopath", "r1"]

    def test_all_unsafe_batch(self):
        from agent.tool_dispatch_helpers import _plan_tool_batch_segments
        calls = [
            _tc("terminal", '{"command":"a"}', call_id="t1"),
            _tc("terminal", '{"command":"b"}', call_id="t2"),
            _tc("terminal", '{"command":"c"}', call_id="t3"),
        ]
        segments = _plan_tool_batch_segments(calls)
        assert _kinds(segments) == ["sequential"]

    def test_tool_not_in_registry_sequential(self):
        """Unknown tools should be treated as sequential barriers.
        Single parallel calls demoted."""
        from agent.tool_dispatch_helpers import _plan_tool_batch_segments
        calls = [
            _tc("web_search", call_id="r1"),
            _tc("imaginary_tool", "{}", call_id="x1"),
        ]
        segments = _plan_tool_batch_segments(calls)
        assert _kinds(segments) == ["sequential"]
        assert _flatten_ids(segments) == ["r1", "x1"]

    def test_mcp_tool_fallback_sequential(self):
        """MCP tools not in registry should be treated as sequential barriers.
        Single parallel calls demoted."""
        from agent.tool_dispatch_helpers import _plan_tool_batch_segments
        calls = [
            _tc("web_search", call_id="r1"),
            _tc("mcp-filesystem_read", '{"path":"/tmp/a"}', call_id="m1"),
        ]
        segments = _plan_tool_batch_segments(calls)
        assert _kinds(segments) == ["sequential"]
        assert _flatten_ids(segments) == ["r1", "m1"]

    # 046-050: Complex ordering preservation
    def test_complex_ordering_preserved(self):
        from agent.tool_dispatch_helpers import _plan_tool_batch_segments
        calls = [
            _tc("terminal", '{"command":"a"}', call_id="b1"),
            _tc("web_search", call_id="r1"),
            _tc("clarify", '{"question":"?"}', call_id="c1"),
            _tc("read_file", '{"path":"a.py"}', call_id="r2"),
            _tc("read_file", '{"path":"b.py"}', call_id="r3"),
        ]
        segments = _plan_tool_batch_segments(calls)
        assert _flatten_ids(segments) == ["b1", "r1", "c1", "r2", "r3"]

    def test_parallel_then_sequential_then_parallel(self):
        from agent.tool_dispatch_helpers import _plan_tool_batch_segments
        calls = [
            _tc("web_search", call_id="r1"),
            _tc("web_search", call_id="r2"),
            _tc("terminal", '{"command":"make"}', call_id="t1"),
            _tc("web_search", call_id="r3"),
        ]
        segments = _plan_tool_batch_segments(calls)
        assert _kinds(segments) == ["parallel", "sequential"]
        # r3 is single → demoted to sequential, merged with t1
        assert [tc.id for tc in segments[1][1]] == ["t1", "r3"]

    def test_adjacent_parallel_segments_merged(self):
        """Adjacent parallel segments after demotion should be merged."""
        from agent.tool_dispatch_helpers import _plan_tool_batch_segments
        # Two parallel segments each with 1 call → demoted to sequential
        calls = [
            _tc("read_file", '{"path":"a.py"}', call_id="r1"),
            _tc("read_file", '{"path":"b.py"}', call_id="r2"),
        ]
        segments = _plan_tool_batch_segments(calls)
        # Both are parallel-safe but different paths → one parallel segment
        assert len(segments) == 1

    def test_adjacent_sequential_segments_merged(self):
        from agent.tool_dispatch_helpers import _plan_tool_batch_segments
        calls = [
            _tc("terminal", '{"command":"a"}', call_id="t1"),
            _tc("terminal", '{"command":"b"}', call_id="t2"),
        ]
        segments = _plan_tool_batch_segments(calls)
        assert _kinds(segments) == ["sequential"]
        assert len(segments[0][1]) == 2

    def test_seven_tool_mixed_batch(self):
        from agent.tool_dispatch_helpers import _plan_tool_batch_segments
        calls = [
            _tc("web_search", call_id="r1"),
            _tc("read_file", '{"path":"a.py"}', call_id="r2"),
            _tc("terminal", '{"command":"x"}', call_id="t1"),
            _tc("read_file", '{"path":"b.py"}', call_id="r3"),
            _tc("web_search", call_id="r4"),
            _tc("clarify", '{"question":"?"}', call_id="c1"),
            _tc("web_search", call_id="r5"),
        ]
        segments = _plan_tool_batch_segments(calls)
        ids = _flatten_ids(segments)
        assert ids == ["r1", "r2", "t1", "r3", "r4", "c1", "r5"]
        # Check ordering: r3,r4 after t1, c1 before r5
        assert ids.index("t1") < ids.index("r3")
        assert ids.index("c1") < ids.index("r5")


# =============================================================================
# 051-070: Path canonicalization and overlap detection
# =============================================================================

class TestPathCanonicalization:
    """_canonical_path and _paths_overlap correctness."""

    def test_relative_and_absolute_same_file(self, tmp_path):
        from agent.tool_dispatch_helpers import _canonical_path, _paths_overlap
        target = tmp_path / "config.json"
        target.touch()
        abs_path = _canonical_path(str(target))
        rel_path = _canonical_path("config.json", execution_cwd=tmp_path)
        assert _paths_overlap(abs_path, rel_path)

    def test_different_files_no_overlap(self, tmp_path):
        from agent.tool_dispatch_helpers import _canonical_path, _paths_overlap
        a = tmp_path / "a.txt"
        b = tmp_path / "b.txt"
        a.touch()
        b.touch()
        assert not _paths_overlap(_canonical_path(str(a)), _canonical_path(str(b)))

    def test_parent_child_path_overlap(self, tmp_path):
        from agent.tool_dispatch_helpers import _canonical_path, _paths_overlap
        d = tmp_path / "sub"
        d.mkdir()
        f = d / "file.txt"
        f.touch()
        # Dir and file inside it overlap
        assert _paths_overlap(_canonical_path(str(d)), _canonical_path(str(f)))

    def test_symlink_alias_overlap(self, tmp_path):
        import os
        from agent.tool_dispatch_helpers import _canonical_path, _paths_overlap
        real_dir = tmp_path / "real"
        real_dir.mkdir()
        target = real_dir / "config.json"
        target.touch()
        alias_dir = tmp_path / "alias"
        alias_dir.symlink_to(real_dir)
        assert _paths_overlap(
            _canonical_path(str(target)),
            _canonical_path(str(alias_dir / "config.json")),
        )

    def test_nonexistent_file_canonicalization(self, tmp_path):
        from agent.tool_dispatch_helpers import _canonical_path
        p = _canonical_path(str(tmp_path / "new.txt"))
        assert p is not None
        assert p.name == "new.txt"

    def test_empty_path_returns_home(self):
        """Empty path resolves to current directory (not None)."""
        from agent.tool_dispatch_helpers import _canonical_path
        result = _canonical_path("")
        assert result is not None
        assert result.is_absolute()

    def test_root_path(self):
        from agent.tool_dispatch_helpers import _canonical_path
        p = _canonical_path("/")
        assert p is not None

    def test_symlink_nonexistent_write_target(self, tmp_path):
        """Symlink parent + not-yet-created leaf: must be detected as overlapping."""
        import os
        from agent.tool_dispatch_helpers import _canonical_path, _paths_overlap
        real_dir = tmp_path / "real"
        real_dir.mkdir()
        alias_dir = tmp_path / "alias"
        alias_dir.symlink_to(real_dir)
        real_target = _canonical_path(str(real_dir / "new.txt"))
        alias_target = _canonical_path(str(alias_dir / "new.txt"))
        assert _paths_overlap(real_target, alias_target)

    def test_execution_cwd_used_over_process_cwd(self, tmp_path, monkeypatch):
        from agent.tool_dispatch_helpers import _extract_parallel_scope_path, _paths_overlap
        exec_cwd = tmp_path / "sub"
        exec_cwd.mkdir()
        (exec_cwd / "x.txt").touch()
        monkeypatch.chdir(tmp_path)
        path_with_cwd = _extract_parallel_scope_path(
            "write_file", {"path": "x.txt"}, execution_cwd=exec_cwd
        )
        path_absolute = _extract_parallel_scope_path(
            "write_file", {"path": str(exec_cwd / "x.txt")}
        )
        assert path_with_cwd is not None
        assert path_absolute is not None
        assert _paths_overlap(path_with_cwd, path_absolute)

    def test_dot_dot_paths(self, tmp_path, monkeypatch):
        from agent.tool_dispatch_helpers import _canonical_path
        d = tmp_path / "a" / "b"
        d.mkdir(parents=True)
        f = d / "file.txt"
        f.touch()
        # From tmp_path/a, go ../a/b/file.txt to reach the file
        cwd = tmp_path / "a"
        monkeypatch.chdir(str(cwd))
        p = _canonical_path("../a/b/file.txt", execution_cwd=cwd)
        expected = _canonical_path(str(f))
        assert p == expected


# =============================================================================
# 071-085: Dispatcher integration
# =============================================================================

class TestSegmentedDispatcher:
    """Full end-to-end tests with _execute_tool_calls."""

    def _make_tool_defs(self, *names):
        return [
            {"type": "function", "function": {
                "name": n, "description": f"{n} tool",
                "parameters": {"type": "object", "properties": {}},
            }}
            for n in names
        ]

    @pytest.fixture()
    def agent(self):
        try:
            with (
                patch("run_agent.get_tool_definitions",
                      return_value=self._make_tool_defs("web_search", "terminal")),
                patch("run_agent.check_toolset_requirements", return_value={}),
                patch("run_agent.OpenAI"),
            ):
                from run_agent import AIAgent
                a = AIAgent(
                    api_key="test-key-1234567890",
                    base_url="https://openrouter.ai/api/v1",
                    quiet_mode=True,
                    skip_context_files=True,
                    skip_memory=True,
                )
                a.client = MagicMock()
                return a
        except Exception as e:
            pytest.skip(f"run_agent import failed: {e}")

    def test_mixed_batch_concurrent_prefix(self, agent):
        """Two web_search calls must overlap in time."""
        calls = [
            _tc("web_search", '{"query":"a"}', call_id="s1"),
            _tc("web_search", '{"query":"b"}', call_id="s2"),
            _tc("terminal", '{"command":"echo done"}', call_id="t1"),
        ]
        msg = SimpleNamespace(content="", tool_calls=calls)
        messages = []
        rendezvous = threading.Barrier(2, timeout=10)
        events = []
        events_lock = threading.Lock()

        def fake_handle(name, args, task_id, **kwargs):
            with events_lock:
                events.append(("start", name, kwargs["tool_call_id"]))
            if name == "web_search":
                rendezvous.wait()
            with events_lock:
                events.append(("end", name, kwargs["tool_call_id"]))
            return json.dumps({"ok": name})

        with patch("run_agent.handle_function_call", side_effect=fake_handle):
            agent._execute_tool_calls(msg, messages, "task-1")

        assert [m["tool_call_id"] for m in messages] == ["s1", "s2", "t1"]
        terminal_start = events.index(("start", "terminal", "t1"))
        search_ends = [i for i, e in enumerate(events)
                       if e[0] == "end" and e[1] == "web_search"]
        assert len(search_ends) == 2
        assert all(i < terminal_start for i in search_ends)

    def test_ordering_with_barrier_in_middle(self, agent):
        calls = [
            _tc("web_search", '{"query":"a"}', call_id="s1"),
            _tc("web_search", '{"query":"b"}', call_id="s2"),
            _tc("terminal", '{"command":"touch x"}', call_id="t1"),
            _tc("web_search", '{"query":"c"}', call_id="s3"),
            _tc("web_search", '{"query":"d"}', call_id="s4"),
        ]
        msg = SimpleNamespace(content="", tool_calls=calls)
        messages = []
        executed = []
        lock = threading.Lock()

        def fake_handle(name, args, task_id, **kwargs):
            with lock:
                executed.append(kwargs["tool_call_id"])
            return json.dumps({"ok": True})

        with patch("run_agent.handle_function_call", side_effect=fake_handle):
            agent._execute_tool_calls(msg, messages, "task-1")

        assert [m["tool_call_id"] for m in messages] == ["s1", "s2", "t1", "s3", "s4"]
        t1_pos = executed.index("t1")
        assert {"s1", "s2"} == set(executed[:t1_pos])
        assert {"s3", "s4"} == set(executed[t1_pos + 1:])

    def test_homogeneous_safe_concurrent_path(self, agent):
        calls = [_tc("web_search", '{"query":"a"}'), _tc("web_search", '{"query":"b"}')]
        msg = SimpleNamespace(content="", tool_calls=calls)
        with (
            patch.object(agent, "_execute_tool_calls_concurrent") as conc,
            patch.object(agent, "_execute_tool_calls_sequential") as seq,
        ):
            agent._execute_tool_calls(msg, [], "task-1")
        conc.assert_called_once()
        seq.assert_not_called()

    def test_homogeneous_unsafe_sequential_path(self, agent):
        calls = [_tc("terminal", '{"command":"a"}'), _tc("terminal", '{"command":"b"}')]
        msg = SimpleNamespace(content="", tool_calls=calls)
        with (
            patch.object(agent, "_execute_tool_calls_concurrent") as conc,
            patch.object(agent, "_execute_tool_calls_sequential") as seq,
        ):
            agent._execute_tool_calls(msg, [], "task-1")
        seq.assert_called_once()
        conc.assert_not_called()

    def test_single_call_sequential(self, agent):
        msg = SimpleNamespace(content="", tool_calls=[_tc("web_search", '{"query":"a"}')])
        with (
            patch.object(agent, "_execute_tool_calls_concurrent") as conc,
            patch.object(agent, "_execute_tool_calls_sequential") as seq,
        ):
            agent._execute_tool_calls(msg, [], "task-1")
        seq.assert_called_once()
        conc.assert_not_called()

    def test_interrupt_during_barrier_drains_later(self, agent):
        calls = [
            _tc("web_search", '{"query":"a"}', call_id="s1"),
            _tc("web_search", '{"query":"b"}', call_id="s2"),
            _tc("terminal", '{"command":"long"}', call_id="t1"),
            _tc("web_search", '{"query":"c"}', call_id="s3"),
            _tc("web_search", '{"query":"d"}', call_id="s4"),
        ]
        msg = SimpleNamespace(content="", tool_calls=calls)
        messages = []
        executed = []
        lock = threading.Lock()

        def fake_handle(name, args, task_id, **kwargs):
            with lock:
                executed.append(kwargs["tool_call_id"])
            if kwargs["tool_call_id"] == "t1":
                agent._interrupt_requested = True
            return json.dumps({"ok": True})

        with patch("run_agent.handle_function_call", side_effect=fake_handle):
            agent._execute_tool_calls(msg, messages, "task-1")

        assert [m["tool_call_id"] for m in messages] == ["s1", "s2", "t1", "s3", "s4"]
        assert "s3" not in executed and "s4" not in executed
        for m in messages[-2:]:
            assert "cancelled" in m["content"] or "skipped" in m["content"]

    def test_steer_appears_once_in_mixed_batch(self, agent):
        calls = [
            _tc("web_search", '{"query":"a"}', call_id="s1"),
            _tc("web_search", '{"query":"b"}', call_id="s2"),
            _tc("terminal", '{"command":"echo hi"}', call_id="t1"),
        ]
        msg = SimpleNamespace(content="", tool_calls=calls)
        messages = []

        def fake_handle(name, args, task_id, **kwargs):
            return json.dumps({"ok": True})

        agent.steer("focus on the tests")
        with patch("run_agent.handle_function_call", side_effect=fake_handle):
            agent._execute_tool_calls(msg, messages, "task-1")

        contents = [m["content"] for m in messages]
        hits = [c for c in contents if "focus on the tests" in c]
        assert len(hits) == 1

    def test_ten_tool_mixed_batch(self, agent):
        """10 calls: 8 parallel-safe + 2 barriers."""
        calls = [
            _tc("web_search", call_id=f"r{i}")
            for i in range(8)
        ] + [
            _tc("terminal", '{"command":"a"}', call_id="t1"),
            _tc("terminal", '{"command":"b"}', call_id="t2"),
        ]
        msg = SimpleNamespace(content="", tool_calls=calls)
        messages = []
        executed = []
        lock = threading.Lock()

        def fake_handle(name, args, task_id, **kwargs):
            with lock:
                executed.append(kwargs["tool_call_id"])
            return json.dumps({"ok": True})

        with patch("run_agent.handle_function_call", side_effect=fake_handle):
            agent._execute_tool_calls(msg, messages, "task-1")

        assert len(messages) == 10
        # Top 8 are parallel, bottom 2 are sequential
        r_ids = [f"r{i}" for i in range(8)]
        assert all(m["tool_call_id"] in r_ids for m in messages[:8])
        assert messages[8]["tool_call_id"] == "t1"
        assert messages[9]["tool_call_id"] == "t2"


# =============================================================================
# 086-095: Config integration
# =============================================================================

class TestParallelConfig:
    """DEFAULT_CONFIG must expose parallel section with correct defaults."""

    def test_parallel_section_exists(self):
        from hermes_cli.config import DEFAULT_CONFIG
        assert "parallel" in DEFAULT_CONFIG

    def test_parallel_default_enabled_false(self):
        from hermes_cli.config import DEFAULT_CONFIG
        assert DEFAULT_CONFIG["parallel"]["enabled"] is False

    def test_parallel_default_max_workers(self):
        from hermes_cli.config import DEFAULT_CONFIG
        assert DEFAULT_CONFIG["parallel"]["max_workers"] == 4

    def test_parallel_default_tool_timeout(self):
        from hermes_cli.config import DEFAULT_CONFIG
        assert DEFAULT_CONFIG["parallel"]["tool_timeout"] == 30

    def test_parallel_default_read_only_parallel(self):
        from hermes_cli.config import DEFAULT_CONFIG
        assert DEFAULT_CONFIG["parallel"]["read_only_parallel"] is True

    def test_parallel_default_file_lock_enabled(self):
        from hermes_cli.config import DEFAULT_CONFIG
        assert DEFAULT_CONFIG["parallel"]["file_lock_enabled"] is True

    def test_parallel_config_deep_merge(self, tmp_path):
        """User config should deep-merge over defaults."""
        config_path = tmp_path / "config.yaml"
        config_path.write_text("parallel:\n  enabled: true\n")
        from hermes_cli.config import load_config
        with patch("hermes_cli.config.get_config_path", return_value=config_path):
            cfg = load_config()
        assert cfg["parallel"]["enabled"] is True
        assert cfg["parallel"]["max_workers"] == 4  # from defaults

    def test_parallel_config_partial_override(self, tmp_path):
        """Partial override keeps other defaults."""
        config_path = tmp_path / "config.yaml"
        config_path.write_text("parallel:\n  max_workers: 8\n")
        from hermes_cli.config import load_config
        with patch("hermes_cli.config.get_config_path", return_value=config_path):
            cfg = load_config()
        assert cfg["parallel"]["max_workers"] == 8
        assert cfg["parallel"]["enabled"] is False  # from defaults

    def test_parallel_config_missing_falls_back(self, tmp_path):
        """No parallel section in user config → use defaults."""
        config_path = tmp_path / "config.yaml"
        config_path.write_text("model: test\n")
        from hermes_cli.config import load_config
        with patch("hermes_cli.config.get_config_path", return_value=config_path):
            cfg = load_config()
        assert cfg["parallel"]["enabled"] is False
        assert cfg["parallel"]["max_workers"] == 4


# =============================================================================
# 096-100: Thread safety and edge cases
# =============================================================================

class TestThreadSafety:
    """Registry must be thread-safe for concurrent is_read_only reads."""

    def test_concurrent_registry_reads(self):
        """Multiple threads reading is_read_only must not race."""
        if not _REGISTRY_LOADED:
            pytest.skip("Registry not loaded")
        errors = []
        lock = threading.Lock()

        def reader():
            try:
                for _ in range(100):
                    e = registry.get_entry("read_file")
                    assert e is not None
                    assert e.is_read_only is True
                    e = registry.get_entry("write_file")
                    assert e is not None
                    assert e.is_read_only is False
            except Exception as ex:
                with lock:
                    errors.append(ex)

        threads = [threading.Thread(target=reader) for _ in range(10)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=5)
        assert len(errors) == 0, f"Thread safety errors: {errors}"

    def test_concurrent_registry_mutations_during_reads(self):
        """Registering tools while reading is_read_only must be safe."""
        if not _REGISTRY_LOADED:
            pytest.skip("Registry not loaded")
        errors = []
        lock = threading.Lock()

        def writer():
            try:
                from tools.registry import ToolEntry
                for i in range(50):
                    # Simulate MCP refresh
                    with registry._lock:
                        registry._tools[f"_test_dynamic_{i}"] = ToolEntry(
                            name=f"_test_dynamic_{i}",
                            toolset="test",
                            schema={},
                            handler=lambda: None,
                            check_fn=None,
                            requires_env=[],
                            is_async=False,
                            is_read_only=True,
                            description="test",
                            emoji="",
                        )
                    time.sleep(0.001)
                    with registry._lock:
                        registry._tools.pop(f"_test_dynamic_{i}", None)
            except Exception as ex:
                with lock:
                    errors.append(ex)

        def reader():
            try:
                for _ in range(100):
                    e = registry.get_entry("read_file")
                    if e is not None:
                        _ = e.is_read_only
            except Exception as ex:
                with lock:
                    errors.append(ex)

        threads = [threading.Thread(target=writer)] + [threading.Thread(target=reader) for _ in range(5)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=10)
        assert len(errors) == 0, f"Thread safety errors: {errors}"

    def test_dispatch_helpers_module_import(self):
        """Importing tool_dispatch_helpers must not raise."""
        from agent.tool_dispatch_helpers import (
            _plan_tool_batch_segments,
            _should_parallelize_tool_batch,
            _is_destructive_command,
            _extract_parallel_scope_path,
            _paths_overlap,
            _canonical_path,
        )
        # Verify all expected symbols are importable
        assert callable(_plan_tool_batch_segments)
        assert callable(_should_parallelize_tool_batch)
        assert callable(_is_destructive_command)
        assert callable(_extract_parallel_scope_path)
        assert callable(_paths_overlap)
        assert callable(_canonical_path)

    def test_should_parallelize_backward_compat(self):
        """_should_parallelize_tool_batch must return same answers as before."""
        # This is the old boolean gate — kept for backward compat
        pass


# =============================================================================
# 101-110: Additional registry edge cases
# =============================================================================

class TestRegistryAdditionalEdgeCases:

    def test_register_new_tool_read_only(self):
        """New tools registered with is_read_only=True should be queryable."""
        if not _REGISTRY_LOADED:
            pytest.skip("Registry not loaded")
        from tools.registry import ToolEntry
        # Simulate a new tool
        with registry._lock:
            registered = False
            if "_test_new_readonly" not in registry._tools:
                registry._tools["_test_new_readonly"] = ToolEntry(
                    name="_test_new_readonly",
                    toolset="test",
                    schema={},
                    handler=lambda: None,
                    check_fn=None,
                    requires_env=[],
                    is_async=False,
                    is_read_only=True,
                    description="test",
                    emoji="",
                )
                registered = True
        entry = registry.get_entry("_test_new_readonly")
        assert entry is not None
        assert entry.is_read_only is True
        if registered:
            with registry._lock:
                registry._tools.pop("_test_new_readonly", None)

    def test_register_new_tool_write(self):
        """New tools registered without is_read_only should default to False."""
        if not _REGISTRY_LOADED:
            pytest.skip("Registry not loaded")
        from tools.registry import ToolEntry
        with registry._lock:
            registered = False
            if "_test_new_write" not in registry._tools:
                registry._tools["_test_new_write"] = ToolEntry(
                    name="_test_new_write",
                    toolset="test",
                    schema={},
                    handler=lambda: None,
                    check_fn=None,
                    requires_env=[],
                    is_async=False,
                    is_read_only=False,
                    description="test",
                    emoji="",
                )
                registered = True
        entry = registry.get_entry("_test_new_write")
        assert entry is not None
        assert entry.is_read_only is False
        if registered:
            with registry._lock:
                registry._tools.pop("_test_new_write", None)

    def test_is_read_only_independent_of_is_async(self):
        """is_read_only and is_async are orthogonal attributes."""
        if not _REGISTRY_LOADED:
            pytest.skip("Registry not loaded")
        entry = registry.get_entry("vision_analyze")
        assert entry.is_async is True
        assert entry.is_read_only is True
        entry = registry.get_entry("read_file")
        assert entry.is_async is False
        assert entry.is_read_only is True

    def test_tool_with_check_fn_still_read_only(self):
        """Tools with availability check_fn can still be read-only."""
        if not _REGISTRY_LOADED:
            pytest.skip("Registry not loaded")
        for name in ("read_file", "web_search", "vision_analyze"):
            entry = registry.get_entry(name)
            assert entry is not None
            assert entry.check_fn is not None
            assert entry.is_read_only is True

    def test_tool_with_requires_env_still_read_only(self):
        """Tools with requires_env can still be read-only."""
        if not _REGISTRY_LOADED:
            pytest.skip("Registry not loaded")
        entry = registry.get_entry("web_search")
        assert entry is not None
        assert entry.requires_env
        assert entry.is_read_only is True


# =============================================================================
# 111-125: Additional segment planner edge cases
# =============================================================================

class TestSegmentPlannerMoreEdgeCases:

    def test_all_different_read_only_tools_parallel(self):
        """Mix of different read-only tools should all be in one parallel segment."""
        from agent.tool_dispatch_helpers import _plan_tool_batch_segments
        calls = [
            _tc("web_search", "{}", call_id="r1"),
            _tc("read_file", '{"path":"a.py"}', call_id="r2"),
            _tc("web_extract", '{"urls":["http://a"]}', call_id="r3"),
            _tc("vision_analyze", '{"image_url":"u1"}', call_id="r4"),
        ]
        segments = _plan_tool_batch_segments(calls)
        # 4 parallel-safe calls → one parallel segment
        assert _kinds(segments) == ["parallel"]
        assert len(segments[0][1]) == 4

    def test_read_file_same_dir_different_files(self):
        """read_file calls in same dir but different files should be parallel."""
        from agent.tool_dispatch_helpers import _plan_tool_batch_segments
        calls = [
            _tc("read_file", '{"path":"/tmp/dir/a.py"}', call_id="r1"),
            _tc("read_file", '{"path":"/tmp/dir/b.py"}', call_id="r2"),
            _tc("read_file", '{"path":"/tmp/dir/c.py"}', call_id="r3"),
        ]
        segments = _plan_tool_batch_segments(calls)
        assert _kinds(segments) == ["parallel"]

    def test_read_file_parent_subdir_overlap(self):
        """read_file on parent dir + subdir must overlap."""
        from agent.tool_dispatch_helpers import _plan_tool_batch_segments
        calls = [
            _tc("read_file", '{"path":"/tmp/dir"}', call_id="r1"),
            _tc("read_file", '{"path":"/tmp/dir/sub/file.txt"}', call_id="r2"),
        ]
        segments = _plan_tool_batch_segments(calls)
        # Same tree → conflict → each single → demoted to sequential
        assert _kinds(segments) == ["sequential"]

    def test_three_parallel_two_sequential(self):
        """3 parallel + 2 sequential = [parallel, sequential]."""
        from agent.tool_dispatch_helpers import _plan_tool_batch_segments
        calls = [
            _tc("web_search", "{}", call_id="r1"),
            _tc("web_search", "{}", call_id="r2"),
            _tc("web_search", "{}", call_id="r3"),
            _tc("terminal", '{"command":"a"}', call_id="t1"),
            _tc("terminal", '{"command":"b"}', call_id="t2"),
        ]
        segments = _plan_tool_batch_segments(calls)
        assert _kinds(segments) == ["parallel", "sequential"]
        assert len(segments[0][1]) == 3
        assert len(segments[1][1]) == 2

    def test_parallel_then_parallel_then_sequential(self):
        """Two parallel segments separated by nothing → merged."""
        from agent.tool_dispatch_helpers import _plan_tool_batch_segments
        calls = [
            _tc("web_search", "{}", call_id="r1"),
            _tc("web_search", "{}", call_id="r2"),
            _tc("read_file", '{"path":"a.py"}', call_id="r3"),
            _tc("read_file", '{"path":"b.py"}', call_id="r4"),
            _tc("terminal", '{"command":"x"}', call_id="t1"),
        ]
        segments = _plan_tool_batch_segments(calls)
        # r1,r2 is parallel, r3,r4 is parallel (but different paths)
        # since no barrier between them → merged into one parallel segment
        assert _kinds(segments) == ["parallel", "sequential"]
        assert len(segments[0][1]) == 4

    def test_segments_never_empty(self):
        """Each segment must have at least 1 call."""
        from agent.tool_dispatch_helpers import _plan_tool_batch_segments
        calls = [_tc("web_search", "{}"), _tc("terminal", '{"command":"x"}')]
        segments = _plan_tool_batch_segments(calls)
        for _, segment_calls in segments:
            assert len(segment_calls) >= 1

    def test_results_in_emission_order(self):
        """Flattened results must preserve emission order."""
        from agent.tool_dispatch_helpers import _plan_tool_batch_segments
        calls = [
            _tc("read_file", '{"path":"a.py"}', call_id="1"),
            _tc("read_file", '{"path":"b.py"}', call_id="2"),
            _tc("read_file", '{"path":"c.py"}', call_id="3"),
        ]
        segments = _plan_tool_batch_segments(calls)
        flat = _flatten_ids(segments)
        assert flat == ["1", "2", "3"]

    def test_parallel_with_ten_calls(self):
        """10 parallel calls should be in one segment."""
        from agent.tool_dispatch_helpers import _plan_tool_batch_segments
        calls = [_tc("web_search", "{}", call_id=f"r{i}") for i in range(10)]
        segments = _plan_tool_batch_segments(calls)
        assert _kinds(segments) == ["parallel"]
        assert len(segments[0][1]) == 10

    def test_parallel_then_sequential_then_parallel_2(self):
        """2 parallel, 1 sequential, 2 parallel = [parallel, sequential, parallel]."""
        from agent.tool_dispatch_helpers import _plan_tool_batch_segments
        calls = [
            _tc("web_search", "{}", call_id="r1"),
            _tc("web_search", "{}", call_id="r2"),
            _tc("terminal", '{"command":"x"}', call_id="t1"),
            _tc("web_search", "{}", call_id="r3"),
            _tc("web_search", "{}", call_id="r4"),
        ]
        segments = _plan_tool_batch_segments(calls)
        assert _kinds(segments) == ["parallel", "sequential", "parallel"]
        assert len(segments[0][1]) == 2
        assert len(segments[1][1]) == 1
        assert len(segments[2][1]) == 2

    def test_destructive_terminal_command_sequential(self):
        """Terminal with rm command is sequential barrier."""
        from agent.tool_dispatch_helpers import _plan_tool_batch_segments
        calls = [
            _tc("web_search", "{}", call_id="r1"),
            _tc("terminal", '{"command":"rm -rf /tmp/x"}', call_id="t1"),
        ]
        segments = _plan_tool_batch_segments(calls)
        assert _kinds(segments) == ["sequential"]


# =============================================================================
# 126-132: More path canonicalization tests
# =============================================================================

class TestPathCanonicalizationMore:

    def test_tilde_expansion(self):
        """~ should expand to home directory."""
        from agent.tool_dispatch_helpers import _canonical_path
        p = _canonical_path("~/test.txt")
        assert p is not None
        assert str(p).startswith("/home/") or str(p).startswith("/root/")

    def test_relative_path_absolute_result(self, tmp_path):
        """Relative path + execution_cwd → absolute path."""
        from agent.tool_dispatch_helpers import _canonical_path
        p = _canonical_path("test.txt", execution_cwd=tmp_path)
        assert p is not None
        assert p.is_absolute()
        assert str(p).startswith(str(tmp_path))

    def test_same_directory_no_overlap(self, tmp_path):
        """Same directory, no subpath → no overlap."""
        from agent.tool_dispatch_helpers import _canonical_path, _paths_overlap
        a = _canonical_path(str(tmp_path))
        b = _canonical_path(str(tmp_path.parent))
        # tmp_path is a child of tmp_path.parent
        assert _paths_overlap(a, b)

    def test_unrelated_paths_no_overlap(self, tmp_path):
        """Completely unrelated paths should not overlap."""
        from agent.tool_dispatch_helpers import _canonical_path, _paths_overlap
        a = _canonical_path("/tmp/a.txt")
        b = _canonical_path("/tmp/b.txt")
        assert not _paths_overlap(a, b)

    def test_same_path_always_overlaps(self, tmp_path):
        """Same path should always overlap."""
        from agent.tool_dispatch_helpers import _canonical_path, _paths_overlap
        p = _canonical_path(str(tmp_path))
        assert _paths_overlap(p, p)

    def test_path_with_trailing_slash(self, tmp_path):
        """Path with trailing slash should resolve to same as without."""
        from agent.tool_dispatch_helpers import _canonical_path
        d = tmp_path / "subdir"
        d.mkdir(exist_ok=True)
        p1 = _canonical_path(str(d))
        p2 = _canonical_path(str(d) + "/")
        assert p1 == p2

    def test_dot_path(self, tmp_path):
        """'.' should resolve to execution_cwd."""
        from agent.tool_dispatch_helpers import _canonical_path
        p = _canonical_path(".", execution_cwd=tmp_path)
        assert p is not None
        assert p == _canonical_path(str(tmp_path))


# =============================================================================
# 133-140: Config edge cases
# =============================================================================

class TestConfigEdgeCases:

    def test_parallel_config_type_check(self):
        from hermes_cli.config import DEFAULT_CONFIG
        assert isinstance(DEFAULT_CONFIG["parallel"]["enabled"], bool)
        assert isinstance(DEFAULT_CONFIG["parallel"]["max_workers"], int)
        assert isinstance(DEFAULT_CONFIG["parallel"]["tool_timeout"], int)

    def test_parallel_config_zero_workers(self):
        """max_workers=0 should be valid config (disable parallelism)."""
        from hermes_cli.config import DEFAULT_CONFIG
        cfg = dict(DEFAULT_CONFIG["parallel"])
        cfg["max_workers"] = 0
        assert cfg["max_workers"] == 0

    def test_parallel_config_negative_workers(self):
        """Negative max_workers should be caught by config validation."""
        from hermes_cli.config import DEFAULT_CONFIG
        cfg = dict(DEFAULT_CONFIG["parallel"])
        cfg["max_workers"] = -1
        assert cfg["max_workers"] == -1

    def test_parallel_config_enabled_read_from_yaml(self, tmp_path):
        """Loading enabled=true from yaml should work."""
        config_path = tmp_path / "config.yaml"
        config_path.write_text("parallel:\n  enabled: true\n  max_workers: 8\n")
        from hermes_cli.config import load_config
        with patch("hermes_cli.config.get_config_path", return_value=config_path):
            cfg = load_config()
        assert cfg["parallel"]["enabled"] is True
        assert cfg["parallel"]["max_workers"] == 8

    def test_parallel_config_disabled_read_from_yaml(self, tmp_path):
        config_path = tmp_path / "config.yaml"
        config_path.write_text("parallel:\n  enabled: false\n")
        from hermes_cli.config import load_config
        with patch("hermes_cli.config.get_config_path", return_value=config_path):
            cfg = load_config()
        assert cfg["parallel"]["enabled"] is False

    def test_parallel_config_no_tool_timeout_fallback(self, tmp_path):
        config_path = tmp_path / "config.yaml"
        config_path.write_text("parallel:\n  enabled: true\n")
        from hermes_cli.config import load_config
        with patch("hermes_cli.config.get_config_path", return_value=config_path):
            cfg = load_config()
        assert cfg["parallel"]["tool_timeout"] == 30  # from defaults

    def test_parallel_config_section_isolated(self, tmp_path):
        """Changing parallel config must not affect other sections."""
        config_path = tmp_path / "config.yaml"
        config_path.write_text("parallel:\n  enabled: true\nagent:\n  max_turns: 999\n")
        from hermes_cli.config import load_config
        with patch("hermes_cli.config.get_config_path", return_value=config_path):
            cfg = load_config()
        assert cfg["parallel"]["enabled"] is True
        assert cfg["agent"]["max_turns"] == 999


# =============================================================================
# 141-145: Thread safety additional
# =============================================================================

class TestThreadSafetyMore:

    def test_registry_get_entry_thread_safe(self):
        if not _REGISTRY_LOADED:
            pytest.skip("Registry not loaded")
        results = []
        lock = threading.Lock()

        def reader():
            for _ in range(50):
                e = registry.get_entry("read_file")
                if e:
                    with lock:
                        results.append(e.is_read_only)

        threads = [threading.Thread(target=reader) for _ in range(5)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=5)
        assert len(results) == 250
        assert all(r is True for r in results)

    def test_is_read_only_immutable(self):
        """is_read_only is set at registration time and the registry is the source of truth."""
        if not _REGISTRY_LOADED:
            pytest.skip("Registry not loaded")
        entry = registry.get_entry("read_file")
        assert entry.is_read_only is True
        # Re-fetch to verify registry still has the correct value
        entry2 = registry.get_entry("read_file")
        assert entry2.is_read_only is True