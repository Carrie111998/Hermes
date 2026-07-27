"""Tests for progressive subdirectory hint discovery."""

import os
import pytest
from pathlib import Path
from unittest.mock import patch

from agent.subdirectory_hints import SubdirectoryHintTracker


@pytest.fixture
def project(tmp_path):
    """Create a mock project tree with hint files in subdirectories."""
    # Root — already loaded at startup
    (tmp_path / "AGENTS.md").write_text("Root project instructions")

    # backend/ — has its own AGENTS.md
    backend = tmp_path / "backend"
    backend.mkdir()
    (backend / "AGENTS.md").write_text("Backend-specific instructions:\n- Use FastAPI\n- Always add type hints")

    # backend/src/ — no hints
    (backend / "src").mkdir()
    (backend / "src" / "main.py").write_text("print('hello')")

    # frontend/ — has CLAUDE.md
    frontend = tmp_path / "frontend"
    frontend.mkdir()
    (frontend / "CLAUDE.md").write_text("Frontend rules:\n- Use TypeScript\n- No any types")

    # docs/ — no hints
    (tmp_path / "docs").mkdir()
    (tmp_path / "docs" / "README.md").write_text("Documentation")

    # deep/nested/path/ — has .cursorrules
    deep = tmp_path / "deep" / "nested" / "path"
    deep.mkdir(parents=True)
    (deep / ".cursorrules").write_text("Cursor rules for nested path")

    return tmp_path


class TestSubdirectoryHintTracker:
    """Unit tests for SubdirectoryHintTracker."""

    def test_working_dir_not_loaded(self, project):
        """Working dir is pre-marked as loaded (startup handles it)."""
        tracker = SubdirectoryHintTracker(working_dir=str(project))
        # Reading a file in the root should NOT trigger hints
        result = tracker.check_tool_call("read_file", {"path": str(project / "AGENTS.md")})
        assert result is None

    def test_discovers_agents_md_via_ancestor_walk(self, project):
        """Reading backend/src/main.py discovers backend/AGENTS.md via ancestor walk."""
        tracker = SubdirectoryHintTracker(working_dir=str(project))
        result = tracker.check_tool_call(
            "read_file", {"path": str(project / "backend" / "src" / "main.py")}
        )
        # backend/src/ has no hints, but ancestor walk finds backend/AGENTS.md
        assert result is not None
        assert "Backend-specific instructions" in result
        # Second read in same subtree should not re-trigger
        result2 = tracker.check_tool_call(
            "read_file", {"path": str(project / "backend" / "AGENTS.md")}
        )
        assert result2 is None  # backend/ already loaded

    def test_discovers_claude_md(self, project):
        """Frontend CLAUDE.md should be discovered."""
        tracker = SubdirectoryHintTracker(working_dir=str(project))
        result = tracker.check_tool_call(
            "read_file", {"path": str(project / "frontend" / "index.ts")}
        )
        assert result is not None
        assert "Frontend rules" in result

    def test_no_duplicate_loading(self, project):
        """Same directory should not be loaded twice."""
        tracker = SubdirectoryHintTracker(working_dir=str(project))
        result1 = tracker.check_tool_call(
            "read_file", {"path": str(project / "frontend" / "a.ts")}
        )
        assert result1 is not None

        result2 = tracker.check_tool_call(
            "read_file", {"path": str(project / "frontend" / "b.ts")}
        )
        assert result2 is None  # already loaded

    def test_no_hints_in_empty_directory(self, project):
        """Directories without hint files return None."""
        tracker = SubdirectoryHintTracker(working_dir=str(project))
        result = tracker.check_tool_call(
            "read_file", {"path": str(project / "docs" / "README.md")}
        )
        assert result is None

    def test_terminal_command_path_extraction(self, project):
        """Paths extracted from terminal commands."""
        tracker = SubdirectoryHintTracker(working_dir=str(project))
        result = tracker.check_tool_call(
            "terminal", {"command": f"cat {project / 'frontend' / 'index.ts'}"}
        )
        assert result is not None
        assert "Frontend rules" in result

    def test_terminal_cd_command(self, project):
        """cd into a directory with hints."""
        tracker = SubdirectoryHintTracker(working_dir=str(project))
        result = tracker.check_tool_call(
            "terminal", {"command": f"cd {project / 'backend'} && ls"}
        )
        assert result is not None
        assert "Backend-specific instructions" in result

    def test_relative_path(self, project):
        """Relative paths resolved against working_dir."""
        tracker = SubdirectoryHintTracker(working_dir=str(project))
        result = tracker.check_tool_call(
            "read_file", {"path": "frontend/index.ts"}
        )
        assert result is not None
        assert "Frontend rules" in result

    def test_outside_working_dir_still_checked(self, tmp_path, project):
        """Paths outside working_dir are still checked for hints."""
        other_project = tmp_path / "other"
        other_project.mkdir()
        (other_project / "AGENTS.md").write_text("Other project rules")
        tracker = SubdirectoryHintTracker(working_dir=str(project))
        result = tracker.check_tool_call(
            "read_file", {"path": str(other_project / "file.py")}
        )
        assert result is not None
        assert "Other project rules" in result

    def test_workdir_arg(self, project):
        """The workdir argument from terminal tool is checked."""
        tracker = SubdirectoryHintTracker(working_dir=str(project))
        result = tracker.check_tool_call(
            "terminal", {"command": "ls", "workdir": str(project / "frontend")}
        )
        assert result is not None
        assert "Frontend rules" in result

    def test_deeply_nested_cursorrules(self, project):
        """Deeply nested .cursorrules should be discovered."""
        tracker = SubdirectoryHintTracker(working_dir=str(project))
        result = tracker.check_tool_call(
            "read_file", {"path": str(project / "deep" / "nested" / "path" / "file.py")}
        )
        assert result is not None
        assert "Cursor rules for nested path" in result

    def test_hint_format_includes_path(self, project):
        """Discovered hints should indicate which file they came from."""
        tracker = SubdirectoryHintTracker(working_dir=str(project))
        result = tracker.check_tool_call(
            "read_file", {"path": str(project / "backend" / "file.py")}
        )
        assert result is not None
        assert "Subdirectory context discovered:" in result
        assert "AGENTS.md" in result

    def test_truncation_of_large_hints(self, tmp_path):
        """Hint files over the limit are truncated."""
        sub = tmp_path / "bigdir"
        sub.mkdir()
        (sub / "AGENTS.md").write_text("x" * 20_000)

        tracker = SubdirectoryHintTracker(working_dir=str(tmp_path))
        result = tracker.check_tool_call(
            "read_file", {"path": str(sub / "file.py")}
        )
        assert result is not None
        assert "truncated" in result.lower()
        # Should be capped
        assert len(result) < 20_000

    def test_empty_args(self, project):
        """Empty args should not crash."""
        tracker = SubdirectoryHintTracker(working_dir=str(project))
        assert tracker.check_tool_call("read_file", {}) is None
        assert tracker.check_tool_call("terminal", {"command": ""}) is None

    def test_url_in_command_ignored(self, project):
        """URLs in shell commands should not be treated as paths."""
        tracker = SubdirectoryHintTracker(working_dir=str(project))
        result = tracker.check_tool_call(
            "terminal", {"command": "curl https://example.com/frontend/api"}
        )
        assert result is None


class TestPermissionErrorHandling:
    """Regression tests for PermissionError in filesystem checks (ref #6214)."""

    def test_is_valid_subdir_permission_error(self, tmp_path):
        """_is_valid_subdir should return False when is_dir() raises PermissionError."""
        tracker = SubdirectoryHintTracker(working_dir=str(tmp_path))
        restricted = tmp_path / "restricted"
        restricted.mkdir()
        with patch.object(Path, "is_dir", side_effect=PermissionError("Permission denied")):
            assert tracker._is_valid_subdir(restricted) is False

    def test_load_hints_permission_error_on_is_file(self, tmp_path):
        """_load_hints_for_directory should skip files when is_file() raises PermissionError."""
        tracker = SubdirectoryHintTracker(working_dir=str(tmp_path))
        restricted = tmp_path / "restricted"
        restricted.mkdir()
        original_is_file = Path.is_file
        def patched_is_file(self):
            if "restricted" in str(self):
                raise PermissionError("Permission denied")
            return original_is_file(self)
        with patch.object(Path, "is_file", patched_is_file):
            result = tracker._load_hints_for_directory(restricted)
        assert result is None

    def test_check_tool_call_survives_inaccessible_path(self, project):
        """Full check_tool_call should not crash when a path is inaccessible."""
        tracker = SubdirectoryHintTracker(working_dir=str(project))
        original_is_dir = Path.is_dir
        def patched_is_dir(self):
            if "backend" in str(self) and "src" not in str(self):
                raise PermissionError("Permission denied")
            return original_is_dir(self)
        with patch.object(Path, "is_dir", patched_is_dir):
            # Should not raise — gracefully skip the inaccessible directory
            result = tracker.check_tool_call(
                "read_file", {"path": str(project / "backend" / "src" / "main.py")}
            )
            # Result may be None (backend skipped) — the key point is no crash
            assert result is None or isinstance(result, str)


class TestContentDeduplication:
    """The same context content must never be injected twice (ref: symlinked
    shared workspaces, hardlinks, and copied backups all alias one file)."""

    def test_symlinked_duplicate_not_reinjected(self, tmp_path):
        """Two directories whose AGENTS.md is the same file yield one injection."""
        real = tmp_path / "real"
        real.mkdir()
        (real / "AGENTS.md").write_text("Shared workspace instructions")

        mirror = tmp_path / "mirror"
        mirror.mkdir()
        (mirror / "AGENTS.md").symlink_to(real / "AGENTS.md")

        tracker = SubdirectoryHintTracker(working_dir=str(tmp_path))
        first = tracker.check_tool_call("read_file", {"path": str(real / "x.py")})
        second = tracker.check_tool_call("read_file", {"path": str(mirror / "y.py")})

        assert first is not None
        assert "Shared workspace instructions" in first
        assert second is None

    def test_identical_copy_not_reinjected(self, tmp_path):
        """Byte-identical copies in unrelated directories dedupe by digest."""
        a = tmp_path / "a"
        b = tmp_path / "b"
        a.mkdir()
        b.mkdir()
        (a / "AGENTS.md").write_text("Same content")
        (b / "AGENTS.md").write_text("Same content")

        tracker = SubdirectoryHintTracker(working_dir=str(tmp_path))
        assert tracker.check_tool_call("read_file", {"path": str(a / "f.py")}) is not None
        assert tracker.check_tool_call("read_file", {"path": str(b / "f.py")}) is None

    def test_differing_content_still_injected(self, tmp_path):
        """Dedupe must not suppress genuinely different context."""
        a = tmp_path / "a"
        b = tmp_path / "b"
        a.mkdir()
        b.mkdir()
        (a / "AGENTS.md").write_text("Alpha rules")
        (b / "AGENTS.md").write_text("Beta rules")

        tracker = SubdirectoryHintTracker(working_dir=str(tmp_path))
        first = tracker.check_tool_call("read_file", {"path": str(a / "f.py")})
        second = tracker.check_tool_call("read_file", {"path": str(b / "f.py")})

        assert first is not None and "Alpha rules" in first
        assert second is not None and "Beta rules" in second

    def test_working_dir_content_seeded(self, tmp_path):
        """A copy of the CWD's own context file is not re-injected."""
        (tmp_path / "AGENTS.md").write_text("Root instructions")
        elsewhere = tmp_path / "elsewhere"
        elsewhere.mkdir()
        (elsewhere / "AGENTS.md").write_text("Root instructions")

        tracker = SubdirectoryHintTracker(working_dir=str(tmp_path))
        assert tracker.check_tool_call("read_file", {"path": str(elsewhere / "f.py")}) is None


class TestExcludedDirectories:
    """Backups, vendored deps, and caches hold copies — never context."""

    @pytest.mark.parametrize(
        "excluded",
        ["backups", "node_modules", ".git", "venv", "site-packages", ".Trash", "vendor"],
    )
    def test_excluded_directory_skipped(self, tmp_path, excluded):
        target = tmp_path / excluded / "snapshot"
        target.mkdir(parents=True)
        (target / "AGENTS.md").write_text("Stale archived instructions")

        tracker = SubdirectoryHintTracker(working_dir=str(tmp_path))
        assert tracker.check_tool_call("read_file", {"path": str(target / "f.py")}) is None

    def test_excluded_ancestor_blocks_descendant(self, tmp_path):
        """A hint nested under an excluded ancestor is still skipped."""
        deep = tmp_path / "backups" / "2026" / "proj"
        deep.mkdir(parents=True)
        (deep / "AGENTS.md").write_text("Archived")

        tracker = SubdirectoryHintTracker(working_dir=str(tmp_path))
        assert tracker.check_tool_call("read_file", {"path": str(deep / "f.py")}) is None

    def test_working_dir_inside_excluded_name_still_works(self, tmp_path):
        """If the user works inside e.g. vendor/, its own subdirs stay eligible."""
        root = tmp_path / "vendor" / "myproject"
        root.mkdir(parents=True)
        sub = root / "pkg"
        sub.mkdir()
        (sub / "AGENTS.md").write_text("Package rules")

        tracker = SubdirectoryHintTracker(working_dir=str(root))
        result = tracker.check_tool_call("read_file", {"path": str(sub / "f.py")})
        assert result is not None and "Package rules" in result

    def test_normal_directory_unaffected(self, tmp_path):
        normal = tmp_path / "backend"
        normal.mkdir()
        (normal / "AGENTS.md").write_text("Backend rules")

        tracker = SubdirectoryHintTracker(working_dir=str(tmp_path))
        result = tracker.check_tool_call("read_file", {"path": str(normal / "f.py")})
        assert result is not None and "Backend rules" in result


class TestTruncationIsAudible:
    """Silent truncation is how AGENTS.md stopped being fully loaded for months.
    Dropping content must always leave a trace in the log."""

    def test_oversized_hint_warns(self, tmp_path, caplog):
        import logging
        from agent.subdirectory_hints import _MAX_HINT_CHARS

        sub = tmp_path / "big"
        sub.mkdir()
        (sub / "AGENTS.md").write_text("x" * (_MAX_HINT_CHARS + 5_000))

        tracker = SubdirectoryHintTracker(working_dir=str(tmp_path))
        with caplog.at_level(logging.WARNING, logger="agent.subdirectory_hints"):
            result = tracker.check_tool_call("read_file", {"path": str(sub / "f.py")})

        assert result is not None
        assert any(
            "dropping" in r.message.lower() and r.levelno == logging.WARNING
            for r in caplog.records
        ), caplog.text

    def test_within_limit_stays_quiet(self, tmp_path, caplog):
        import logging

        sub = tmp_path / "small"
        sub.mkdir()
        (sub / "AGENTS.md").write_text("short and fine")

        tracker = SubdirectoryHintTracker(working_dir=str(tmp_path))
        with caplog.at_level(logging.WARNING, logger="agent.subdirectory_hints"):
            result = tracker.check_tool_call("read_file", {"path": str(sub / "f.py")})

        assert result is not None
        assert not [r for r in caplog.records if r.levelno >= logging.WARNING]
