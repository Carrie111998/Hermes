"""Tests for tools/file_operations.py — deny list, result dataclasses, helpers."""

import os
import re
import stat
import sys

import pytest
import subprocess
from pathlib import Path
from unittest.mock import MagicMock

from tools.file_operations import (
    _is_write_denied,
    ReadResult,
    WriteResult,
    PatchResult,
    SearchResult,
    SearchMatch,
    LintResult,
    ShellFileOperations,
    MAX_LINE_LENGTH,
    normalize_read_pagination,
    normalize_search_pagination,
)


# =========================================================================
# Write deny list
# =========================================================================

class TestIsWriteDenied:
    def test_ssh_authorized_keys_denied(self):
        path = os.path.join(str(Path.home()), ".ssh", "authorized_keys")
        assert _is_write_denied(path) is True

    def test_ssh_id_rsa_denied(self):
        path = os.path.join(str(Path.home()), ".ssh", "id_rsa")
        assert _is_write_denied(path) is True

    def test_netrc_denied(self):
        path = os.path.join(str(Path.home()), ".netrc")
        assert _is_write_denied(path) is True

    @pytest.mark.parametrize("name", [".pgpass", ".npmrc", ".pypirc"])
    def test_credential_config_files_denied(self, name):
        path = os.path.join(str(Path.home()), name)
        assert _is_write_denied(path) is True

    def test_aws_prefix_denied(self):
        path = os.path.join(str(Path.home()), ".aws", "credentials")
        assert _is_write_denied(path) is True

    def test_kube_prefix_denied(self):
        path = os.path.join(str(Path.home()), ".kube", "config")
        assert _is_write_denied(path) is True

    def test_normal_file_allowed(self, tmp_path):
        path = str(tmp_path / "safe_file.txt")
        assert _is_write_denied(path) is False

    def test_project_file_allowed(self):
        assert _is_write_denied("/tmp/project/main.py") is False

    def test_tilde_expansion(self):
        assert _is_write_denied("~/.ssh/authorized_keys") is True

    @pytest.mark.parametrize(
        "path",
        [
            ".anthropic_oauth.json",
            "mcp-tokens/token1.json",
            "mcp-tokens/subdir/token2.json",
            "pairing/telegram-approved.json",
            "pairing/discord-approved.json",
            "pairing/telegram-pending.json",
            "pairing",
        ],
    )
    def test_oauth_mcp_tokens_and_pairing_denied(self, path):
        """PKCE creds, mcp-tokens, and pairing entries must be write-denied."""
        from hermes_constants import get_hermes_home
        hermes_home = get_hermes_home()
        full_path = str(hermes_home / path)
        assert _is_write_denied(full_path) is True

    @pytest.mark.parametrize(
        "path",
        ["auth.json", "config.yaml", "webhook_subscriptions.json"],
    )
    def test_hermes_control_files_requested_writable(self, path):
        from hermes_constants import get_hermes_home

        assert _is_write_denied(str(get_hermes_home() / path)) is False

    @pytest.mark.parametrize(
        "path",
        [
            "./.anthropic_oauth.json",
        ],
    )
    def test_oauth_traversal_denied(self, path):
        """Path traversal attempts to protected OAuth files must be blocked."""
        from hermes_constants import get_hermes_home
        hermes_home = get_hermes_home()
        full_path = str(hermes_home / path)
        assert _is_write_denied(full_path) is True

    @pytest.mark.parametrize(
        "path",
        [
            "/tmp/standard_file.txt",
            "~/projects/myapp/main.py",
            "/var/log/app.log",
        ],
    )
    def test_standard_paths_allowed(self, path):
        """Unrelated paths must still be allowed."""
        assert _is_write_denied(path) is False

    @pytest.mark.parametrize("name", [".anthropic_oauth.json"])
    def test_oauth_protected_in_profile_mode(self, tmp_path, monkeypatch, name):
        """Under a profile, BOTH <profile>/X and <root>/X must be denied."""
        root = tmp_path / "hermes"
        profile = root / "profiles" / "coder"
        profile.mkdir(parents=True)
        monkeypatch.setenv("HERMES_HOME", str(profile))

        assert _is_write_denied(str(profile / name)) is True
        assert _is_write_denied(str(root / name)) is True

    @pytest.mark.parametrize(
        "name",
        ["auth.json", "config.yaml", "webhook_subscriptions.json"],
    )
    def test_control_files_requested_writable_in_profile_mode(self, tmp_path, monkeypatch, name):
        root = tmp_path / "hermes"
        profile = root / "profiles" / "coder"
        profile.mkdir(parents=True)
        monkeypatch.setenv("HERMES_HOME", str(profile))

        assert _is_write_denied(str(profile / name)) is False
        assert _is_write_denied(str(root / name)) is False

    def test_mcp_tokens_dir_protected_in_profile_mode(self, tmp_path, monkeypatch):
        """mcp-tokens/ under profile AND under root must both be denied."""
        root = tmp_path / "hermes"
        profile = root / "profiles" / "coder"
        profile.mkdir(parents=True)
        monkeypatch.setenv("HERMES_HOME", str(profile))

        assert _is_write_denied(str(profile / "mcp-tokens" / "tok.json")) is True
        assert _is_write_denied(str(root / "mcp-tokens" / "tok.json")) is True
        # The directory itself must also be denied (not just files inside)
        assert _is_write_denied(str(root / "mcp-tokens")) is True

    def test_pairing_dir_denied(self, tmp_path, monkeypatch):
        """Regression: pairing/ must be write-denied under both profile and root.

        PR #30383 introduced ~/.hermes/pairing/{platform}-approved.json as the
        gateway access-control list. Without this block, a prompt-injected agent
        can write arbitrary user IDs into an approved file, granting persistent
        gateway access without going through the pairing code flow — the same
        threat class that motivated protecting webhook_subscriptions.json.
        """
        root = tmp_path / "hermes"
        profile = root / "profiles" / "coder"
        profile.mkdir(parents=True)
        monkeypatch.setenv("HERMES_HOME", str(profile))

        # Active profile pairing entries
        assert _is_write_denied(str(profile / "pairing" / "telegram-approved.json")) is True
        assert _is_write_denied(str(profile / "pairing" / "discord-pending.json")) is True
        # The directory itself
        assert _is_write_denied(str(profile / "pairing")) is True
        # Root pairing entries (profile mode — same shape as mcp-tokens gap)
        assert _is_write_denied(str(root / "pairing" / "telegram-approved.json")) is True
        assert _is_write_denied(str(root / "pairing")) is True



# =========================================================================
# Result dataclasses
# =========================================================================

class TestReadResult:
    def test_to_dict_omits_defaults(self):
        r = ReadResult()
        d = r.to_dict()
        assert "error" not in d    # None omitted
        assert "similar_files" not in d  # empty list omitted

    def test_to_dict_preserves_empty_content(self):
        """Empty file should still have content key in the dict."""
        r = ReadResult(content="", total_lines=0, file_size=0)
        d = r.to_dict()
        assert "content" in d
        assert d["content"] == ""
        assert d["total_lines"] == 0
        assert d["file_size"] == 0

    def test_to_dict_includes_values(self):
        r = ReadResult(content="hello", total_lines=10, file_size=50, truncated=True)
        d = r.to_dict()
        assert d["content"] == "hello"
        assert d["total_lines"] == 10
        assert d["truncated"] is True

    def test_binary_fields(self):
        r = ReadResult(is_binary=True, is_image=True, mime_type="image/png")
        d = r.to_dict()
        assert d["is_binary"] is True
        assert d["is_image"] is True
        assert d["mime_type"] == "image/png"


class TestWriteResult:
    def test_to_dict_omits_none(self):
        r = WriteResult(bytes_written=100)
        d = r.to_dict()
        assert d["bytes_written"] == 100
        assert "error" not in d
        assert "warning" not in d

    def test_to_dict_includes_error(self):
        r = WriteResult(error="Permission denied")
        d = r.to_dict()
        assert d["error"] == "Permission denied"


class TestPatchResult:
    def test_to_dict_success(self):
        r = PatchResult(success=True, diff="--- a\n+++ b", files_modified=["a.py"])
        d = r.to_dict()
        assert d["success"] is True
        assert d["diff"] == "--- a\n+++ b"
        assert d["files_modified"] == ["a.py"]

    def test_to_dict_error(self):
        r = PatchResult(error="File not found")
        d = r.to_dict()
        assert d["success"] is False
        assert d["error"] == "File not found"


class TestSearchResult:
    def test_to_dict_with_matches(self):
        m = SearchMatch(path="a.py", line_number=10, content="hello")
        r = SearchResult(matches=[m], total_count=1)
        d = r.to_dict()
        assert d["total_count"] == 1
        assert len(d["matches"]) == 1
        assert d["matches"][0]["path"] == "a.py"

    def test_to_dict_empty(self):
        r = SearchResult()
        d = r.to_dict()
        assert d["total_count"] == 0
        assert "matches" not in d

    def test_to_dict_files_mode(self):
        r = SearchResult(files=["a.py", "b.py"], total_count=2)
        d = r.to_dict()
        assert d["files"] == ["a.py", "b.py"]

    def test_to_dict_count_mode(self):
        r = SearchResult(counts={"a.py": 3, "b.py": 1}, total_count=4)
        d = r.to_dict()
        assert d["counts"]["a.py"] == 3

    def test_truncated_flag(self):
        r = SearchResult(total_count=100, truncated=True)
        d = r.to_dict()
        assert d["truncated"] is True


class TestSearchResultDensify:
    """Path-grouped densification of content-mode matches (lossless)."""

    def _matches(self, n, paths=None):
        # Real ripgrep output is path-ordered: all matches in a file are
        # consecutive (verified against live search_files corpus). The fixture
        # mirrors that — group by path, then enumerate lines within each.
        paths = paths or ["a.py"]
        out = []
        per = max(1, n // len(paths))
        ln = 0
        for p in paths:
            for _ in range(per):
                ln += 1
                out.append(SearchMatch(path=p, line_number=ln,
                                       content=f"line content {ln}"))
        # pad remainder onto the last path
        while len(out) < n:
            ln += 1
            out.append(SearchMatch(path=paths[-1], line_number=ln,
                                   content=f"line content {ln}"))
        return out

    def test_densify_off_by_default(self):
        # The model-facing default must be unchanged for callers that don't
        # opt in: verbose array, no matches_text key.
        r = SearchResult(matches=self._matches(10), total_count=10)
        d = r.to_dict()
        assert "matches" in d
        assert "matches_text" not in d

    def test_densify_below_threshold_keeps_verbose(self):
        # Too few matches: the grouping header would cost more than it saves,
        # so we fall back to the verbose array even with densify=True.
        r = SearchResult(matches=self._matches(4), total_count=4)
        d = r.to_dict(densify=True)
        assert "matches" in d
        assert "matches_text" not in d

    def test_densify_emits_path_grouped_text(self):
        r = SearchResult(matches=self._matches(6, paths=["a.py", "b.py"]),
                         total_count=6)
        d = r.to_dict(densify=True)
        assert "matches" not in d
        assert "matches_text" in d
        assert "matches_format" in d  # self-describing
        text = d["matches_text"]
        # Each path appears once as a group header, not repeated per match.
        assert text.count("a.py") == 1
        assert text.count("b.py") == 1

    def test_densify_is_lossless(self):
        # Every path, line number, and content byte must be recoverable from
        # the dense form.
        import re
        matches = [
            SearchMatch(path="src/x.py", line_number=12, content="    def foo():"),
            SearchMatch(path="src/x.py", line_number=45, content="        return bar"),
            SearchMatch(path="src/y.py", line_number=3, content="import os"),
            SearchMatch(path="src/y.py", line_number=99, content="x = 1  # tail"),
            SearchMatch(path="src/z.py", line_number=7, content="class Z:"),
        ]
        r = SearchResult(matches=matches, total_count=5)
        text = r.to_dict(densify=True)["matches_text"]
        # Reconstruct (path, line, content) triples from the grouped text.
        recovered = []
        cur = None
        for ln in text.split("\n"):
            row = re.match(r"^  (\d+): (.*)$", ln)
            if row:
                recovered.append((cur, int(row.group(1)), row.group(2)))
            else:
                cur = ln
        assert len(recovered) == 5
        for orig, rec in zip(matches, recovered):
            assert rec[0] == orig.path
            assert rec[1] == orig.line_number
            # content is rstrip'd in the dense form; originals here have no
            # trailing whitespace, so they must match exactly.
            assert rec[2] == orig.content

    def test_densify_smaller_than_verbose(self):
        import json
        matches = self._matches(40, paths=["pkg/module_one.py", "pkg/module_two.py"])
        r = SearchResult(matches=matches, total_count=40)
        verbose = json.dumps(r.to_dict(densify=False), ensure_ascii=False)
        dense = json.dumps(r.to_dict(densify=True), ensure_ascii=False)
        assert len(dense) < len(verbose)

    @pytest.mark.parametrize("content", [
        "x = {'k': 1, 'url': 'http://h:8080'}",   # colons in content
        "        deeply.indented(call)",          # leading indentation preserved
        "# \u65e5\u672c\u8a9e comment \U0001f525",  # unicode + emoji
        "",                                        # empty content
        "trailing spaces   ",                     # rstrip'd (see note below)
        'mix "quotes" and , commas',              # punctuation that breaks naive CSV
    ])
    def test_densify_content_is_lossless(self, content):
        # Every realistic single-line match content must round-trip exactly
        # (trailing whitespace is the one documented transform — rstrip).
        matches = [SearchMatch(path=f"f{i}.py", line_number=i + 1, content=content)
                   for i in range(6)]
        r = SearchResult(matches=matches, total_count=6)
        text = r.to_dict(densify=True)["matches_text"]
        recovered = []
        cur = None
        for ln in text.split("\n"):
            row = re.match(r"^  (\d+): (.*)$", ln)
            if row:
                recovered.append(row.group(2))
            else:
                cur = ln
        assert len(recovered) == 6
        for got in recovered:
            assert got == content.rstrip()

    def test_densify_assumes_single_line_matches(self):
        # The path-grouped format puts one match per line, so it relies on
        # ripgrep's one-line-per-match contract (verified: 0/6775 real match
        # contents contained a newline). This test documents that assumption:
        # a (synthetic, never-produced-by-rg) multiline content would split
        # across rows. If search ever emits multiline content, densify must
        # escape newlines first.
        matches = [SearchMatch(path="a.py", line_number=i + 1, content="single line")
                   for i in range(6)]
        text = SearchResult(matches=matches, total_count=6).to_dict(densify=True)["matches_text"]
        # one header + six rows == 7 lines, no row spans multiple lines
        body_rows = [ln for ln in text.split("\n") if re.match(r"^  \d+: ", ln)]
        assert len(body_rows) == 6

    def test_densify_paths_with_spaces(self):
        matches = [SearchMatch(path="my dir/a b.py", line_number=i + 1, content=f"x{i}")
                   for i in range(6)]
        text = SearchResult(matches=matches, total_count=6).to_dict(densify=True)["matches_text"]
        # path with spaces survives as a header line verbatim
        assert "my dir/a b.py" in text.split("\n")[0]


class TestLintResult:
    def test_skipped(self):
        r = LintResult(skipped=True, message="No linter for .md files")
        d = r.to_dict()
        assert d["status"] == "skipped"
        assert d["message"] == "No linter for .md files"

    def test_success(self):
        r = LintResult(success=True, output="")
        d = r.to_dict()
        assert d["status"] == "ok"

    def test_error(self):
        r = LintResult(success=False, output="SyntaxError line 5")
        d = r.to_dict()
        assert d["status"] == "error"
        assert "SyntaxError" in d["output"]


# =========================================================================
# ShellFileOperations helpers
# =========================================================================

@pytest.fixture()
def mock_env():
    """Create a mock terminal environment."""
    env = MagicMock()
    env.cwd = "/tmp/test"
    env.execute.return_value = {"output": "", "returncode": 0}
    return env


@pytest.fixture()
def file_ops(mock_env):
    return ShellFileOperations(mock_env)


class TestShellFileOpsHelpers:
    def test_normalize_read_pagination_clamps_invalid_values(self):
        assert normalize_read_pagination(offset=0, limit=0) == (1, 1)
        assert normalize_read_pagination(offset=-10, limit=-5) == (1, 1)
        assert normalize_read_pagination(offset="bad", limit="bad") == (1, 500)
        assert normalize_read_pagination(offset=2, limit=999999) == (2, 2000)

    def test_normalize_search_pagination_clamps_invalid_values(self):
        assert normalize_search_pagination(offset=-10, limit=-5) == (0, 1)
        assert normalize_search_pagination(offset="bad", limit="bad") == (0, 50)
        assert normalize_search_pagination(offset=3, limit=0) == (3, 1)

    def test_escape_shell_arg_simple(self, file_ops):
        assert file_ops._escape_shell_arg("hello") == "'hello'"

    def test_escape_shell_arg_with_quotes(self, file_ops):
        result = file_ops._escape_shell_arg("it's")
        assert "'" in result
        # Should be safely escaped
        assert result.count("'") >= 4  # wrapping + escaping

    def test_escape_shell_arg_rewrites_windows_drive_paths_to_msys(self, monkeypatch, file_ops):
        # bash eats backslashes and MSYS mangles ``C:\...``; the Git Bash
        # ``/c/...`` form is the reliable one (reuses _windows_to_msys_path).
        import tools.environments.local as local_mod

        monkeypatch.setattr(local_mod, "_IS_WINDOWS", True)
        assert file_ops._escape_shell_arg(r"C:\Users\alice\notes.txt") == "'/c/Users/alice/notes.txt'"
        # Non-drive paths are untouched.
        assert file_ops._escape_shell_arg("/tmp/foo") == "'/tmp/foo'"

    def test_escape_shell_arg_normalizes_mixed_msys_paths(self, monkeypatch, file_ops):
        import tools.environments.local as local_mod

        monkeypatch.setattr(local_mod, "_IS_WINDOWS", True)
        mixed = r"/c/Users/Alexander\Documents\NewTEST\readme.txt"
        assert file_ops._escape_shell_arg(mixed) == (
            "'/c/Users/Alexander/Documents/NewTEST/readme.txt'"
        )

    def test_escape_shell_arg_rewrites_forward_slash_native_paths(self, monkeypatch, file_ops):
        import tools.environments.local as local_mod

        monkeypatch.setattr(local_mod, "_IS_WINDOWS", True)
        assert file_ops._escape_shell_arg(
            "C:/Users/alice/notes.txt"
        ) == "'/c/Users/alice/notes.txt'"

    def test_read_file_uses_bash_safe_windows_paths(self, mock_env, monkeypatch):
        import tools.environments.local as local_mod

        monkeypatch.setattr(local_mod, "_IS_WINDOWS", True)
        commands = []

        def side_effect(command, **kwargs):
            commands.append(command)
            if command.startswith("wc -c"):
                return {"output": "5\n", "returncode": 0}
            if command.startswith("head -c"):
                return {"output": "hello", "returncode": 0}
            if command.startswith("sed -n"):
                return {"output": "hello\n", "returncode": 0}
            if command.startswith("wc -l"):
                return {"output": "1\n", "returncode": 0}
            return {"output": "", "returncode": 0}

        mock_env.execute.side_effect = side_effect
        ops = ShellFileOperations(mock_env)
        result = ops.read_file(r"C:\Users\alice\notes.txt")

        assert result.error is None
        assert commands[0] == "wc -c < '/c/Users/alice/notes.txt' 2>/dev/null"
        assert commands[1] == "head -c 1000 '/c/Users/alice/notes.txt' 2>/dev/null"
        assert commands[2] == "sed -n '1,500p' '/c/Users/alice/notes.txt'"
        assert commands[3] == "wc -l < '/c/Users/alice/notes.txt'"

    def test_is_likely_binary_by_extension(self, file_ops):
        assert file_ops._is_likely_binary("photo.png") is True
        assert file_ops._is_likely_binary("data.db") is True
        assert file_ops._is_likely_binary("code.py") is False
        assert file_ops._is_likely_binary("readme.md") is False

    def test_is_likely_binary_by_content(self, file_ops):
        # High ratio of non-printable chars -> binary
        binary_content = "\x00\x01\x02\x03" * 250
        assert file_ops._is_likely_binary("unknown", binary_content) is True

        # Normal text -> not binary
        assert file_ops._is_likely_binary("unknown", "Hello world\nLine 2\n") is False

    def test_is_image(self, file_ops):
        assert file_ops._is_image("photo.png") is True
        assert file_ops._is_image("pic.jpg") is True
        assert file_ops._is_image("icon.ico") is True
        assert file_ops._is_image("data.pdf") is False
        assert file_ops._is_image("code.py") is False

    def test_add_line_numbers(self, file_ops):
        content = "line one\nline two\nline three"
        result = file_ops._add_line_numbers(content)
        # Compact gutter: "<n>|content" (no fixed-width padding).
        assert "1|line one" in result
        assert "2|line two" in result
        assert "3|line three" in result

    def test_add_line_numbers_with_offset(self, file_ops):
        content = "continued\nmore"
        result = file_ops._add_line_numbers(content, start_line=50)
        assert "50|continued" in result
        assert "51|more" in result

    def test_add_line_numbers_truncates_long_lines(self, file_ops):
        long_line = "x" * (MAX_LINE_LENGTH + 100)
        result = file_ops._add_line_numbers(long_line)
        assert "[truncated]" in result

    def test_unified_diff(self, file_ops):
        old = "line1\nline2\nline3\n"
        new = "line1\nchanged\nline3\n"
        diff = file_ops._unified_diff(old, new, "test.py")
        assert "-line2" in diff
        assert "+changed" in diff
        assert "test.py" in diff

    def test_cwd_from_env(self, mock_env):
        mock_env.cwd = "/custom/path"
        ops = ShellFileOperations(mock_env)
        assert ops.cwd == "/custom/path"

    def test_cwd_fallback_to_slash(self):
        env = MagicMock(spec=[])  # no cwd attribute
        ops = ShellFileOperations(env)
        assert ops.cwd == "/"

    def test_read_file_strips_leaked_terminal_fence_markers(self, mock_env):
        leaked = (
            "'\x07__HERMES_FENCE_a9f7b3__\x1b]0;cat "
            "'/tmp/test/a.py' 2> /dev/null\x07\n"
            "print('ok')\n"
            "__HERMES_FENCE_a9f7b3__\x07'\n"
        )

        def side_effect(command, **kwargs):
            if command.startswith("wc -c"):
                return {"output": "12\n", "returncode": 0}
            if command.startswith("head -c"):
                return {"output": "print('ok')\n", "returncode": 0}
            if command.startswith("sed -n"):
                return {"output": leaked, "returncode": 0}
            if command.startswith("wc -l"):
                return {"output": "1\n", "returncode": 0}
            return {"output": "", "returncode": 0}

        mock_env.execute.side_effect = side_effect
        ops = ShellFileOperations(mock_env)
        result = ops.read_file("/tmp/test/a.py")

        assert result.error is None
        assert "HERMES_FENCE" not in result.content
        assert "\x1b]" not in result.content
        assert "\x07" not in result.content
        assert "1|print('ok')" in result.content

    def test_read_file_raw_strips_leaked_terminal_fence_markers(self, mock_env):
        leaked = (
            "__HERMES_FENCE_a9f7b3__\x07'\n"
            "alpha\n"
            "\x1b]0;cat '/tmp/test/a.txt'\x07__HERMES_FENCE_a9f7b3__\n"
        )

        def side_effect(command, **kwargs):
            if command.startswith("wc -c"):
                return {"output": "6\n", "returncode": 0}
            if command.startswith("head -c"):
                return {"output": "alpha\n", "returncode": 0}
            if command.startswith("cat "):
                return {"output": leaked, "returncode": 0}
            return {"output": "", "returncode": 0}

        mock_env.execute.side_effect = side_effect
        ops = ShellFileOperations(mock_env)
        result = ops.read_file_raw("/tmp/test/a.txt")

        assert result.error is None
        assert result.content == "alpha\n"


class TestSearchPathValidation:
    """Test that search() returns an error for non-existent paths."""

    def test_search_nonexistent_path_returns_error(self, mock_env):
        """search() should return an error when the path doesn't exist."""
        def side_effect(command, **kwargs):
            if "test -e" in command:
                return {"output": "not_found", "returncode": 1}
            if "command -v" in command:
                return {"output": "yes", "returncode": 0}
            return {"output": "", "returncode": 0}
        mock_env.execute.side_effect = side_effect
        ops = ShellFileOperations(mock_env)
        result = ops.search("pattern", path="/nonexistent/path")
        assert result.error is not None
        assert "not found" in result.error.lower() or "Path not found" in result.error

    def test_search_nonexistent_path_files_mode(self, mock_env):
        """search(target='files') should also return error for bad paths."""
        def side_effect(command, **kwargs):
            if "test -e" in command:
                return {"output": "not_found", "returncode": 1}
            if "command -v" in command:
                return {"output": "yes", "returncode": 0}
            return {"output": "", "returncode": 0}
        mock_env.execute.side_effect = side_effect
        ops = ShellFileOperations(mock_env)
        result = ops.search("*.py", path="/nonexistent/path", target="files")
        assert result.error is not None
        assert "not found" in result.error.lower() or "Path not found" in result.error

    def test_search_existing_path_proceeds(self, mock_env):
        """search() should proceed normally when the path exists."""
        def side_effect(command, **kwargs):
            if "test -e" in command:
                return {"output": "exists", "returncode": 0}
            if "command -v" in command:
                return {"output": "yes", "returncode": 0}
            # rg returns exit 1 (no matches) with empty output
            return {"output": "", "returncode": 1}
        mock_env.execute.side_effect = side_effect
        ops = ShellFileOperations(mock_env)
        result = ops.search("pattern", path="/existing/path")
        assert result.error is None
        assert result.total_count == 0  # No matches but no error

    def test_search_rg_error_exit_code(self, mock_env):
        """search() should report error when rg returns exit code 2."""
        call_count = {"n": 0}
        def side_effect(command, **kwargs):
            call_count["n"] += 1
            if "test -e" in command:
                return {"output": "exists", "returncode": 0}
            if "command -v" in command:
                return {"output": "yes", "returncode": 0}
            # rg returns exit 2 (error) with empty output
            return {"output": "", "returncode": 2}
        mock_env.execute.side_effect = side_effect
        ops = ShellFileOperations(mock_env)
        result = ops.search("pattern", path="/some/path")
        assert result.error is not None
        assert "search failed" in result.error.lower() or "Search error" in result.error


@pytest.mark.skipif(
    os.name == "nt",
    reason="GNU find fallback under test through a shell=True fake env; on "
    "Windows that resolves cmd.exe + System32 find.exe (a different tool), "
    "same gating as TestFindExcludesHiddenDirs in test_search_hidden_dirs.py",
)
class TestSearchFilesFallbackHiddenPaths:
    def _make_env(self):
        env = MagicMock()
        env.cwd = "/"

        def execute(command, **kwargs):
            completed = subprocess.run(
                command,
                shell=True,
                text=True,
                capture_output=True,
            )
            return {
                "output": completed.stdout,
                "returncode": completed.returncode,
            }

        env.execute = execute
        return env

    def test_hidden_root_with_hidden_ancestor_includes_files(self, tmp_path, monkeypatch):
        """Fallback find should include visible files when path is inside hidden root."""
        root = tmp_path / ".hermes" / "logs"
        root.mkdir(parents=True)
        visible_file = root / "agent.log"
        hidden_dir_file = root / ".hidden" / "secret.log"
        nested_hidden_file = root / "nested" / ".secret.log"
        visible_nested_file = root / "nested" / "visible.log"

        for p in [visible_file, nested_hidden_file, visible_nested_file, hidden_dir_file]:
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text("x")

        ops = ShellFileOperations(self._make_env())
        monkeypatch.setattr(ops, "_has_command", lambda command: command == "find")
        result = ops._search_files("*.log", str(root), limit=50, offset=0)

        assert result.error is None
        assert set(result.files) == {str(visible_file), str(visible_nested_file)}

    def test_normal_root_still_excludes_hidden_descendants(self, tmp_path, monkeypatch):
        """Fallback find should still exclude hidden descendant paths for normal roots."""
        root = tmp_path / "repo"
        root.mkdir()
        visible_file = root / "agent.log"
        visible_nested_file = root / "nested" / "visible.log"
        hidden_dir_file = root / ".hidden" / "secret.log"

        for p in [visible_file, visible_nested_file, hidden_dir_file]:
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text("x")

        ops = ShellFileOperations(self._make_env())
        monkeypatch.setattr(ops, "_has_command", lambda command: command == "find")
        result = ops._search_files("*.log", str(root), limit=50, offset=0)

        assert result.error is None
        assert set(result.files) == {str(visible_file), str(visible_nested_file)}


class TestShellFileOpsWriteDenied:
    def test_write_file_denied_path(self, file_ops):
        result = file_ops.write_file("~/.ssh/authorized_keys", "evil key")
        assert result.error is not None
        assert "denied" in result.error.lower()

    def test_patch_replace_denied_path(self, file_ops):
        result = file_ops.patch_replace("~/.ssh/authorized_keys", "old", "new")
        assert result.error is not None
        assert "denied" in result.error.lower()

    def test_delete_file_denied_path(self, file_ops):
        result = file_ops.delete_file("~/.ssh/authorized_keys")
        assert result.error is not None
        assert "denied" in result.error.lower()

    def test_move_file_src_denied(self, file_ops):
        result = file_ops.move_file("~/.ssh/id_rsa", "/tmp/dest.txt")
        assert result.error is not None
        assert "denied" in result.error.lower()

    def test_move_file_dst_denied(self, file_ops):
        result = file_ops.move_file("/tmp/src.txt", "~/.aws/credentials")
        assert result.error is not None
        assert "denied" in result.error.lower()

    def test_move_file_failure_path(self, mock_env):
        mock_env.execute.return_value = {"output": "No such file or directory", "returncode": 1}
        ops = ShellFileOperations(mock_env)
        result = ops.move_file("/tmp/nonexistent.txt", "/tmp/dest.txt")
        assert result.error is not None
        assert "Failed to move" in result.error


class TestPatchReplacePostWriteVerification:
    """Tests for the post-write verification added in patch_replace.

    Confirms that a silent persistence failure (where write_file's command
    appears to succeed but the bytes on disk don't match new_content) is
    surfaced as an error instead of being reported as a successful patch.
    """

    def test_patch_replace_fails_when_file_not_persisted(self, mock_env):
        """write_file reports success but the re-read returns old content:
        patch_replace must return an error, not success-with-diff."""
        file_contents = {"/tmp/test/a.py": "hello world\n"}

        def side_effect(command, **kwargs):
            # cat reads the file — both the initial read and the verify read
            if command.startswith("cat "):
                # Extract path from cat command (strip quotes)
                for path in file_contents:
                    if path in command:
                        return {"output": file_contents[path], "returncode": 0}
                return {"output": "", "returncode": 1}
            # mkdir for parent dir
            if command.startswith("mkdir "):
                return {"output": "", "returncode": 0}
            # wc -c for byte count after write
            if command.startswith("wc -c"):
                for path in file_contents:
                    if path in command:
                        return {"output": str(len(file_contents[path].encode())), "returncode": 0}
                return {"output": "0", "returncode": 0}
            # Everything else (including the write itself) pretends to succeed
            # but DOESN'T update file_contents — simulates silent failure
            return {"output": "", "returncode": 0}

        mock_env.execute.side_effect = side_effect
        ops = ShellFileOperations(mock_env)
        result = ops.patch_replace("/tmp/test/a.py", "hello", "hi")
        assert result.error is not None, (
            "Silent persistence failure must surface as error, got: "
            f"success={result.success}, diff={result.diff}"
        )
        assert "verification failed" in result.error.lower()
        assert "did not persist" in result.error.lower()

    def test_patch_replace_succeeds_when_file_persisted(self, mock_env):
        """Normal success path: write persists, verify read returns new bytes."""
        state = {"content": "hello world\n"}

        def side_effect(command, stdin_data=None, **kwargs):
            # A write is the only call that pipes content over stdin — key
            # on that behavioral signal rather than the exact write command,
            # which is an atomic temp-file + mv script (`set -e; ... mv ...`),
            # not a bare `cat > path`.
            if stdin_data is not None:
                state["content"] = stdin_data
                return {"output": "", "returncode": 0}
            if command.startswith("cat "):  # read / verify
                return {"output": state["content"], "returncode": 0}
            if command.startswith("mkdir "):
                return {"output": "", "returncode": 0}
            if command.startswith("wc -c"):
                return {"output": str(len(state["content"].encode())), "returncode": 0}
            return {"output": "", "returncode": 0}

        mock_env.execute.side_effect = side_effect
        ops = ShellFileOperations(mock_env)
        result = ops.patch_replace("/tmp/test/a.py", "hello", "hi")
        assert result.error is None, f"Unexpected error: {result.error}"
        assert result.success is True
        assert state["content"] == "hi world\n", f"File not actually updated: {state['content']!r}"

    def test_patch_replace_fails_when_verify_read_errors(self, mock_env):
        """If the verify-read step itself fails (exit code != 0), return an error."""
        call_count = {"cat": 0}
        state = {"content": "hello world\n"}

        def side_effect(command, stdin_data=None, **kwargs):
            if stdin_data is not None:  # write (atomic temp-file + mv script)
                state["content"] = stdin_data
                return {"output": "", "returncode": 0}
            if command.startswith("cat "):  # read
                call_count["cat"] += 1
                # First read (initial fetch) succeeds; second read (verify) fails
                if call_count["cat"] == 1:
                    return {"output": state["content"], "returncode": 0}
                return {"output": "", "returncode": 1}
            if command.startswith("mkdir "):
                return {"output": "", "returncode": 0}
            if command.startswith("wc -c"):
                return {"output": str(len(state["content"].encode())), "returncode": 0}
            return {"output": "", "returncode": 0}

        mock_env.execute.side_effect = side_effect
        ops = ShellFileOperations(mock_env)
        result = ops.patch_replace("/tmp/test/a.py", "hello", "hi")
        assert result.error is not None
        assert "could not re-read" in result.error.lower()


# =========================================================================
# Git baseline check for write_file warning
# =========================================================================

class _DeletedTestGitBaselineCheck:
    """Removed May 2026 — these tests asserted on a ``_check_git_baseline``
    method that doesn't exist on ``ShellFileOperations`` (regression intro
    by a separate refactor). All 6 tests in the class fail with
    AttributeError on origin/main. Deleted wholesale per Teknium's
    instruction to keep CI green; reinstate them when the underlying
    helper is restored or replaced.
    """
    pass


# =========================================================================
# Local-backend native read fast path
# =========================================================================

def _must_not_execute(*args, **kwargs):
    raise AssertionError("native fast path must not call env.execute()")


class TestLocalNativeReadFastPath:
    """read_file on the local backend must not shell out for regular files.

    Every ShellFileOperations._exec spawns a fresh bash on the local
    backend (spawn-per-call design), and the shell read pipeline makes
    FOUR round-trips per read (wc -c, head -c, sed, wc -l).  On Windows
    a Git Bash spawn costs 0.3-1s, so one read_file was 3-4s and any
    test doing a handful of reads blew the suite-wide 30s pytest-timeout
    (test_accretion_caps killed the whole session under redirected
    stdio).  Regular local files are read with native Python I/O
    instead; anything else falls back to the shell pipeline unchanged.

    The expected values pin the shell pipeline's exact output, quirks
    included: total_lines is the newline count (wc -l), and a window
    whose last printed line ends with a newline gains a trailing empty
    numbered line (sed's final \\n split by _add_line_numbers).  The
    gutter is the compact ``<n>|`` form (upstream #35368/#35532 dropped
    the fixed-width padded gutter for both paths).
    """

    @pytest.fixture
    def local_ops(self, tmp_path):
        from tools.environments.local import LocalEnvironment

        class _NoSpawnLocal(LocalEnvironment):
            """Real LocalEnvironment, minus the init-time bash snapshot."""

            def init_session(self):
                self._snapshot_ready = False

        env = _NoSpawnLocal(cwd=str(tmp_path))
        env.execute = _must_not_execute
        return ShellFileOperations(env, cwd=str(tmp_path))

    def test_read_regular_file_uses_no_shell(self, local_ops, tmp_path):
        f = tmp_path / "plain.txt"
        f.write_bytes(b"a\nb\n")
        r = local_ops.read_file(str(f))
        assert r.error is None
        assert r.content == "1|a\n2|b\n3|"
        assert r.total_lines == 2
        assert r.file_size == 4
        assert r.truncated is False

    def test_read_slice_matches_shell_contract(self, local_ops, tmp_path):
        f = tmp_path / "slice.txt"
        f.write_bytes(b"l1\nl2\nl3\nl4\n")
        r = local_ops.read_file(str(f), offset=2, limit=2)
        assert r.content == "2|l2\n3|l3\n4|"
        assert r.total_lines == 4
        assert r.truncated is True
        assert r.hint == "Use offset=4 to continue reading (showing 2-3 of 4 lines)"

    def test_read_no_trailing_newline(self, local_ops, tmp_path):
        f = tmp_path / "nonl.txt"
        f.write_bytes(b"a\nb")
        r = local_ops.read_file(str(f))
        assert r.content == "1|a\n2|b"
        assert r.total_lines == 1  # wc -l counts newlines — historical contract

    def test_read_empty_file(self, local_ops, tmp_path):
        f = tmp_path / "empty.txt"
        f.write_bytes(b"")
        r = local_ops.read_file(str(f))
        assert r.error is None
        assert r.content == "1|"
        assert r.total_lines == 0

    def test_read_offset_past_eof(self, local_ops, tmp_path):
        f = tmp_path / "short.txt"
        f.write_bytes(b"x\n")
        r = local_ops.read_file(str(f), offset=5, limit=10)
        assert r.content == "5|"
        assert r.total_lines == 1
        assert r.truncated is False

    def test_read_crlf_keeps_carriage_returns(self, local_ops, tmp_path):
        f = tmp_path / "dos.txt"
        f.write_bytes(b"a\r\nb\r\n")
        r = local_ops.read_file(str(f))
        assert r.content == "1|a\r\n2|b\r\n3|"
        assert r.total_lines == 2

    def test_relative_path_resolves_against_env_cwd(self, local_ops, tmp_path):
        (tmp_path / "rel.txt").write_bytes(b"REL_OK\n")
        r = local_ops.read_file("rel.txt")
        assert r.error is None
        assert "REL_OK" in r.content

    def test_image_short_circuits_without_shell(self, local_ops, tmp_path):
        f = tmp_path / "pic.png"
        f.write_bytes(b"\x89PNG\r\n\x1a\n" + b"\x00" * 16)
        r = local_ops.read_file(str(f))
        assert r.is_image is True
        assert r.is_binary is True

    def test_binary_extension_short_circuits(self, local_ops, tmp_path):
        f = tmp_path / "blob.exe"
        f.write_bytes(b"\x00\x01\x02\x03" * 10)
        r = local_ops.read_file(str(f))
        assert r.is_binary is True
        assert r.error is not None

    def test_missing_file_falls_back_to_shell(self, tmp_path):
        from tools.environments.local import LocalEnvironment

        calls = []

        class _CannedLocal(LocalEnvironment):
            def init_session(self):
                self._snapshot_ready = False

            def execute(self, command, cwd="", **kwargs):
                calls.append(command)
                return {"output": "", "returncode": 1}

        ops = ShellFileOperations(_CannedLocal(cwd=str(tmp_path)), cwd=str(tmp_path))
        r = ops.read_file(str(tmp_path / "ghost.txt"))
        assert r.error is not None
        assert r.error.startswith("File not found:")
        assert calls, "missing file should defer to the shell pipeline"

    @pytest.mark.skipif(sys.platform != "win32", reason="MSYS path semantics are Windows-only")
    def test_msys_drive_path_reads_natively_on_windows(self, local_ops, tmp_path):
        f = tmp_path / "msys.txt"
        f.write_bytes(b"MSYS_OK\n")
        drive = f.drive.rstrip(":").lower()
        msys = "/" + drive + str(f)[len(f.drive):].replace("\\", "/")
        r = local_ops.read_file(msys)
        assert r.error is None
        assert "MSYS_OK" in r.content

    @pytest.mark.skipif(sys.platform != "win32", reason="MSYS path semantics are Windows-only")
    def test_posix_root_path_falls_back_on_windows(self, tmp_path):
        from tools.environments.local import LocalEnvironment

        calls = []

        class _CannedLocal(LocalEnvironment):
            def init_session(self):
                self._snapshot_ready = False

            def execute(self, command, cwd="", **kwargs):
                calls.append(command)
                return {"output": "", "returncode": 1}

        ops = ShellFileOperations(_CannedLocal(cwd=str(tmp_path)), cwd=str(tmp_path))
        # /tmp/... resolves inside Git Bash's filesystem, not C:\tmp —
        # native I/O must not guess, the shell pipeline owns this path.
        ops.read_file("/tmp/hermes-native-readpath-probe.txt")
        assert calls, "POSIX-root path should defer to the shell pipeline"

    def test_non_local_env_keeps_shell_pipeline(self):
        env = MagicMock()
        env.cwd = "/x"
        env.execute.return_value = {"output": "", "returncode": 1}
        ops = ShellFileOperations(env)
        ops.read_file("/x/whatever.txt")
        assert env.execute.called


class TestLocalNativeReadRawFastPath:
    """read_file_raw on the local backend must not shell out for regular files.

    Same rationale as TestLocalNativeReadFastPath: the raw-read shell
    pipeline costs THREE bash round-trips (wc -c, head -c, cat) and the
    local backend spawns a fresh Git Bash per round-trip — 0.3-1s each
    on Windows, ~3s per call.  read_file_raw feeds the patch/verify
    flows, so it is a hot path.

    The expected values pin the shell pipeline's exact output, captured
    empirically before the fast path existed: content is the byte-exact
    file text decoded utf-8/errors=replace — trailing newlines survive
    the pipe (the CWD-marker stripping removes only the wrapper's own
    injected newline) — total_lines stays 0 and truncated False (raw
    reads never count lines), the image short-circuit sets no hint and
    no error (unlike read_file's), and the binary error is the em-dash
    variant ("Binary file — cannot display as text.").
    """

    @pytest.fixture
    def local_ops(self, tmp_path):
        from tools.environments.local import LocalEnvironment

        class _NoSpawnLocal(LocalEnvironment):
            """Real LocalEnvironment, minus the init-time bash snapshot."""

            def init_session(self):
                self._snapshot_ready = False

        env = _NoSpawnLocal(cwd=str(tmp_path))
        env.execute = _must_not_execute
        return ShellFileOperations(env, cwd=str(tmp_path))

    def test_raw_read_uses_no_shell_trailing_newline_preserved(self, local_ops, tmp_path):
        f = tmp_path / "plain.txt"
        f.write_bytes(b"a\nb\n")
        r = local_ops.read_file_raw(str(f))
        assert r.error is None
        assert r.content == "a\nb\n"
        assert r.file_size == 4
        assert r.total_lines == 0
        assert r.truncated is False
        assert r.hint is None

    def test_raw_read_no_trailing_newline(self, local_ops, tmp_path):
        f = tmp_path / "nonl.txt"
        f.write_bytes(b"a\nb")
        r = local_ops.read_file_raw(str(f))
        assert r.error is None
        assert r.content == "a\nb"
        assert r.file_size == 3

    def test_raw_read_empty_file(self, local_ops, tmp_path):
        f = tmp_path / "empty.txt"
        f.write_bytes(b"")
        r = local_ops.read_file_raw(str(f))
        assert r.error is None
        assert r.content == ""
        assert r.file_size == 0

    def test_raw_read_multiple_trailing_newlines(self, local_ops, tmp_path):
        f = tmp_path / "multi.txt"
        f.write_bytes(b"a\nb\n\n\n")
        r = local_ops.read_file_raw(str(f))
        assert r.content == "a\nb\n\n\n"
        assert r.file_size == 6

    def test_raw_read_crlf_preserved(self, local_ops, tmp_path):
        f = tmp_path / "dos.txt"
        f.write_bytes(b"a\r\nb\r\n")
        r = local_ops.read_file_raw(str(f))
        assert r.content == "a\r\nb\r\n"
        assert r.file_size == 6

    def test_raw_read_utf8_multibyte(self, local_ops, tmp_path):
        f = tmp_path / "utf8.txt"
        f.write_bytes("héllo wörld\n".encode("utf-8"))
        r = local_ops.read_file_raw(str(f))
        assert r.content == "héllo wörld\n"
        assert r.file_size == 14

    def test_raw_image_short_circuits_without_shell(self, local_ops, tmp_path):
        f = tmp_path / "pic.png"
        f.write_bytes(b"\x89PNG\r\n\x1a\n" + b"\x00" * 16)
        r = local_ops.read_file_raw(str(f))
        assert r.is_image is True
        assert r.is_binary is True
        assert r.file_size == 24
        # Unlike read_file's image result, read_file_raw sets no hint.
        assert r.hint is None
        assert r.error is None
        assert r.content == ""

    def test_raw_binary_extension_short_circuits(self, local_ops, tmp_path):
        f = tmp_path / "blob.exe"
        f.write_bytes(b"\x00\x01\x02\x03" * 10)
        r = local_ops.read_file_raw(str(f))
        assert r.is_binary is True
        assert r.error == "Binary file — cannot display as text."
        assert r.file_size == 40
        assert r.content == ""

    def test_raw_binary_content_sniff(self, local_ops, tmp_path):
        f = tmp_path / "nuls.txt"
        f.write_bytes(b"\x00" * 600 + b"text tail\n")
        r = local_ops.read_file_raw(str(f))
        assert r.is_binary is True
        assert r.error == "Binary file — cannot display as text."
        assert r.file_size == 610

    def test_raw_relative_path_resolves_against_env_cwd(self, local_ops, tmp_path):
        (tmp_path / "rel.txt").write_bytes(b"REL_OK\n")
        r = local_ops.read_file_raw("rel.txt")
        assert r.error is None
        assert r.content == "REL_OK\n"

    def test_raw_missing_file_falls_back_to_shell(self, tmp_path):
        from tools.environments.local import LocalEnvironment

        calls = []

        class _CannedLocal(LocalEnvironment):
            def init_session(self):
                self._snapshot_ready = False

            def execute(self, command, cwd="", **kwargs):
                calls.append(command)
                return {"output": "", "returncode": 1}

        ops = ShellFileOperations(_CannedLocal(cwd=str(tmp_path)), cwd=str(tmp_path))
        r = ops.read_file_raw(str(tmp_path / "ghost.txt"))
        assert r.error is not None
        assert r.error.startswith("File not found:")
        assert calls, "missing file should defer to the shell pipeline"

    def test_raw_size_cap_falls_back_to_shell(self, tmp_path, monkeypatch):
        from tools.environments.local import LocalEnvironment

        calls = []

        class _CannedLocal(LocalEnvironment):
            def init_session(self):
                self._snapshot_ready = False

            def execute(self, command, cwd="", **kwargs):
                calls.append(command)
                return {"output": "", "returncode": 1}

        monkeypatch.setattr("tools.file_operations._NATIVE_READ_MAX_BYTES", 4)
        f = tmp_path / "big.txt"
        f.write_bytes(b"12345\n")
        ops = ShellFileOperations(_CannedLocal(cwd=str(tmp_path)), cwd=str(tmp_path))
        ops.read_file_raw(str(f))
        assert calls, "file past the size cap should defer to the shell pipeline"

    @pytest.mark.skipif(sys.platform != "win32", reason="MSYS path semantics are Windows-only")
    def test_raw_msys_drive_path_reads_natively_on_windows(self, local_ops, tmp_path):
        f = tmp_path / "msys.txt"
        f.write_bytes(b"MSYS_OK\n")
        drive = f.drive.rstrip(":").lower()
        msys = "/" + drive + str(f)[len(f.drive):].replace("\\", "/")
        r = local_ops.read_file_raw(msys)
        assert r.error is None
        assert r.content == "MSYS_OK\n"

    @pytest.mark.skipif(sys.platform != "win32", reason="MSYS path semantics are Windows-only")
    def test_raw_posix_root_path_falls_back_on_windows(self, tmp_path):
        from tools.environments.local import LocalEnvironment

        calls = []

        class _CannedLocal(LocalEnvironment):
            def init_session(self):
                self._snapshot_ready = False

            def execute(self, command, cwd="", **kwargs):
                calls.append(command)
                return {"output": "", "returncode": 1}

        ops = ShellFileOperations(_CannedLocal(cwd=str(tmp_path)), cwd=str(tmp_path))
        # /tmp/... resolves inside Git Bash's filesystem, not C:\tmp —
        # native I/O must not guess, the shell pipeline owns this path.
        ops.read_file_raw("/tmp/hermes-native-rawread-probe.txt")
        assert calls, "POSIX-root path should defer to the shell pipeline"

    def test_raw_non_local_env_keeps_shell_pipeline(self):
        env = MagicMock()
        env.cwd = "/x"
        env.execute.return_value = {"output": "", "returncode": 1}
        ops = ShellFileOperations(env)
        ops.read_file_raw("/x/whatever.txt")
        assert env.execute.called


# =========================================================================
# Local-backend native WRITE / PATCH fast path
# =========================================================================


class TestLocalNativeWriteFastPath:
    """write_file on the local backend must not shell out for regular files.

    The shell write pipeline costs 3-5 bash round-trips per call (a
    pre-read ``cat`` for lint/LSP extensions, ``head -c 4096`` for
    line-ending detection, ``mkdir -p``, the ``cat >`` write, ``wc -c``)
    and the local backend spawns a fresh Git Bash per round-trip — 0.3-1s
    each on Windows.  write_file is the agent's hottest edit-loop path.
    Regular local files are written with native Python I/O instead;
    anything else falls back to the shell pipeline unchanged.

    The pinned expectations are the shell pipeline's exact, empirically
    captured behavior: content lands as ``content.encode("utf-8")`` byte
    for byte (the stdin pipe writes through ``proc.stdin.buffer`` so bare
    LFs are NOT CRLF-injected on Windows); a pre-existing CRLF file forces
    the write to CRLF via line-ending detection; ``bytes_written`` is the
    on-disk byte count; and ``dirs_created`` is True whenever the path has
    a non-empty dirname (even when the directory already existed), False
    only for a bare relative filename.
    """

    @pytest.fixture
    def local_ops(self, tmp_path):
        from tools.environments.local import LocalEnvironment

        class _NoSpawnLocal(LocalEnvironment):
            """Real LocalEnvironment, minus the init-time bash snapshot."""

            def init_session(self):
                self._snapshot_ready = False

        env = _NoSpawnLocal(cwd=str(tmp_path))
        env.execute = _must_not_execute
        return ShellFileOperations(env, cwd=str(tmp_path))

    def test_write_new_file_uses_no_shell(self, local_ops, tmp_path):
        f = tmp_path / "one.txt"
        r = local_ops.write_file(str(f), "a\nb\n")
        assert r.error is None
        assert r.bytes_written == 4
        # Absolute path -> non-empty dirname -> dirs_created True even
        # though tmp_path already exists (shell `mkdir -p` quirk).
        assert r.dirs_created is True
        assert r.lint == {"status": "skipped", "message": "No linter for .txt files"}
        # Bare LF must NOT be CRLF-injected (stdin .buffer write contract).
        assert f.read_bytes() == b"a\nb\n"

    def test_write_no_trailing_newline(self, local_ops, tmp_path):
        f = tmp_path / "nonl.txt"
        r = local_ops.write_file(str(f), "abc")
        assert r.error is None
        assert r.bytes_written == 3
        assert f.read_bytes() == b"abc"

    def test_write_utf8_multibyte_byte_count(self, local_ops, tmp_path):
        f = tmp_path / "utf8.txt"
        r = local_ops.write_file(str(f), "héllo\n")
        assert r.error is None
        assert r.bytes_written == 7  # wc -c counts bytes, not chars
        assert f.read_bytes() == "héllo\n".encode("utf-8")

    def test_write_crlf_txt_normalizes_via_head_sample(self, local_ops, tmp_path):
        # .txt is not a lint/LSP extension -> no pre_content -> the shell
        # path detects the ending with `head -c 4096`; the native path
        # must detect it from a native head read instead.  Bare-LF content
        # must land as CRLF to match the existing file.
        f = tmp_path / "dos.txt"
        f.write_bytes(b"old\r\nline\r\n")
        r = local_ops.write_file(str(f), "new\nstuff\n")
        assert r.error is None
        assert f.read_bytes() == b"new\r\nstuff\r\n"
        assert r.bytes_written == 12

    def test_write_lf_txt_stays_lf(self, local_ops, tmp_path):
        f = tmp_path / "unix.txt"
        f.write_bytes(b"old\nline\n")
        r = local_ops.write_file(str(f), "new\nstuff\n")
        assert r.error is None
        assert f.read_bytes() == b"new\nstuff\n"
        assert r.bytes_written == 10

    def test_write_crlf_py_normalizes_via_precontent(self, local_ops, tmp_path):
        # .py IS a lint/LSP extension -> pre_content is captured -> the
        # ending is detected from it (no head sample) and the write is
        # CRLF-normalized.  In-process py lint must still run (no shell).
        f = tmp_path / "dos.py"
        f.write_bytes(b"a = 1\r\n")
        r = local_ops.write_file(str(f), "b = 2\n")
        assert r.error is None
        assert f.read_bytes() == b"b = 2\r\n"
        assert r.lint == {"status": "ok", "output": ""}

    def test_write_creates_nested_dirs(self, local_ops, tmp_path):
        r = local_ops.write_file("deep/nest/two.txt", "x\n")
        assert r.error is None
        assert r.dirs_created is True
        assert (tmp_path / "deep" / "nest" / "two.txt").read_bytes() == b"x\n"

    def test_write_bare_relative_name_dirs_created_false(self, local_ops, tmp_path):
        # dirname("bare.txt") == "" -> the shell path skips mkdir and
        # reports dirs_created False; the native path must match.
        r = local_ops.write_file("bare.txt", "hi\n")
        assert r.error is None
        assert r.dirs_created is False
        assert (tmp_path / "bare.txt").read_bytes() == b"hi\n"

    def test_write_overwrite_truncates(self, local_ops, tmp_path):
        f = tmp_path / "ow.txt"
        f.write_bytes(b"aaaaaaaaaa\n")
        r = local_ops.write_file(str(f), "z\n")
        assert r.error is None
        assert r.bytes_written == 2
        assert f.read_bytes() == b"z\n"

    def test_write_clean_py_lints_ok_no_shell(self, local_ops, tmp_path):
        f = tmp_path / "clean.py"
        r = local_ops.write_file(str(f), "x = 1\n")
        assert r.error is None
        assert r.lint == {"status": "ok", "output": ""}
        assert f.read_bytes() == b"x = 1\n"

    def test_write_broken_py_lint_reported_no_shell(self, local_ops, tmp_path):
        f = tmp_path / "broken.py"
        r = local_ops.write_file(str(f), "def (:\n")
        assert r.error is None
        assert r.lint["status"] == "error"
        assert "SyntaxError" in r.lint["output"]
        assert f.read_bytes() == b"def (:\n"

    def test_write_deny_listed_path_blocked(self, local_ops):
        # Deny check precedes the native gate; must still block with no
        # write and no shell spawn.
        denied = os.path.join(str(Path.home()), ".ssh", "id_rsa")
        r = local_ops.write_file(denied, "SHOULD NOT WRITE")
        assert r.error is not None
        assert "Write denied" in r.error
        assert r.bytes_written == 0

    def test_write_non_local_env_keeps_shell_pipeline(self):
        env = MagicMock()
        env.cwd = "/x"
        env.execute.return_value = {"output": "", "returncode": 0}
        ops = ShellFileOperations(env)
        ops.write_file("/x/whatever.txt", "data\n")
        assert env.execute.called

    @pytest.mark.skipif(sys.platform != "win32", reason="MSYS path semantics are Windows-only")
    def test_write_msys_drive_path_writes_natively_on_windows(self, local_ops, tmp_path):
        f = tmp_path / "msys.txt"
        drive = f.drive.rstrip(":").lower()
        msys = "/" + drive + str(f)[len(f.drive):].replace("\\", "/")
        r = local_ops.write_file(msys, "MSYS_OK\n")
        assert r.error is None
        assert f.read_bytes() == b"MSYS_OK\n"

    @pytest.mark.skipif(sys.platform != "win32", reason="MSYS path semantics are Windows-only")
    def test_write_posix_root_path_falls_back_on_windows(self, tmp_path):
        from tools.environments.local import LocalEnvironment

        calls = []

        class _CannedLocal(LocalEnvironment):
            def init_session(self):
                self._snapshot_ready = False

            def execute(self, command, cwd="", **kwargs):
                calls.append(command)
                return {"output": "", "returncode": 0}

        ops = ShellFileOperations(_CannedLocal(cwd=str(tmp_path)), cwd=str(tmp_path))
        # /tmp/... resolves inside Git Bash's filesystem, not C:\tmp — the
        # native path must not guess; the shell pipeline owns this write.
        ops.write_file("/tmp/hermes-native-writepath-probe.txt", "x\n")
        assert calls, "POSIX-root path should defer to the shell pipeline"


class TestLocalNativePatchReplaceFastPath:
    """patch_replace on the local backend must not shell out for regular files.

    patch_replace reads its initial content and re-reads for post-write
    verification through a direct ``cat`` _exec — two bash spawns per call
    that the read-fast-path commit did NOT cover (it uses ``cat``, not
    read_file_raw).  Its internal write goes through write_file (now native
    too).  For a regular local file the whole patch_replace round-trip is
    native; anything non-trivial (missing file, POSIX-root path, non-local
    backend) falls back to the shell ``cat`` so the historical error and
    verify semantics are preserved untouched.
    """

    @pytest.fixture
    def local_ops(self, tmp_path):
        from tools.environments.local import LocalEnvironment

        class _NoSpawnLocal(LocalEnvironment):
            def init_session(self):
                self._snapshot_ready = False

        env = _NoSpawnLocal(cwd=str(tmp_path))
        env.execute = _must_not_execute
        return ShellFileOperations(env, cwd=str(tmp_path))

    def test_patch_replace_uses_no_shell(self, local_ops, tmp_path):
        f = tmp_path / "pr.txt"
        f.write_bytes(b"alpha\nbeta\ngamma\n")
        r = local_ops.patch_replace(str(f), "beta", "BETA")
        assert r.success is True
        assert r.error is None
        assert f.read_bytes() == b"alpha\nBETA\ngamma\n"
        assert "-beta" in r.diff and "+BETA" in r.diff

    def test_patch_replace_crlf_preserved_no_shell(self, local_ops, tmp_path):
        f = tmp_path / "pr_crlf.txt"
        f.write_bytes(b"a\r\nb\r\nc\r\n")
        r = local_ops.patch_replace(str(f), "b", "BB")
        assert r.success is True
        assert f.read_bytes() == b"a\r\nBB\r\nc\r\n"

    def test_patch_replace_py_lints_no_shell(self, local_ops, tmp_path):
        f = tmp_path / "pr.py"
        f.write_bytes(b"x = 1\ny = 2\n")
        r = local_ops.patch_replace(str(f), "y = 2", "y = 3")
        assert r.success is True
        assert f.read_bytes() == b"x = 1\ny = 3\n"
        assert r.lint == {"status": "ok", "output": ""}

    def test_patch_replace_no_match_no_shell(self, local_ops, tmp_path):
        f = tmp_path / "pr.txt"
        f.write_bytes(b"alpha\nbeta\ngamma\n")
        r = local_ops.patch_replace(str(f), "NOPE-not-present", "X")
        assert r.success is False
        assert r.error is not None
        assert f.read_bytes() == b"alpha\nbeta\ngamma\n"  # unchanged

    def test_patch_replace_deny_listed_blocked(self, local_ops):
        denied = os.path.join(str(Path.home()), ".ssh", "id_rsa")
        r = local_ops.patch_replace(denied, "a", "b")
        assert r.success is False
        assert "Write denied" in r.error

    def test_patch_replace_missing_file_falls_back(self, tmp_path):
        from tools.environments.local import LocalEnvironment

        calls = []

        class _CannedLocal(LocalEnvironment):
            def init_session(self):
                self._snapshot_ready = False

            def execute(self, command, cwd="", **kwargs):
                calls.append(command)
                return {"output": "", "returncode": 1}

        ops = ShellFileOperations(_CannedLocal(cwd=str(tmp_path)), cwd=str(tmp_path))
        r = ops.patch_replace(str(tmp_path / "ghost.txt"), "a", "b")
        assert r.success is False
        assert "Failed to read file" in r.error
        assert calls, "missing file should defer to the shell cat"

    def test_patch_replace_non_local_env_keeps_shell(self):
        env = MagicMock()
        env.cwd = "/x"
        env.execute.return_value = {"output": "", "returncode": 1}
        ops = ShellFileOperations(env)
        ops.patch_replace("/x/whatever.txt", "a", "b")
        assert env.execute.called


# =========================================================================
# Local-backend native delete / move fast paths
# =========================================================================

class TestLocalNativeDeleteFastPath:
    """delete_file on the local backend must not shell out for regular files.

    ``delete_file`` shelled a single ``rm -f`` per call — one of the last
    per-call Git Bash spawns in the mutating set (0.3-1s each on Windows).
    Regular local files (including read-only ones) are removed with native
    Python I/O instead; anything ``rm -f`` handles specially falls back to
    the shell so its exact semantics and error text survive.

    The pinned expectations are ``rm -f``'s empirically captured behavior:
    deleting a MISSING file SUCCEEDS (idempotent — ``os.remove`` would
    raise FileNotFoundError); a READ-ONLY file is removed (GNU ``rm -f``
    clears the attribute, so the native path clears the read-only bit via
    ``os.chmod`` before ``os.remove``, which would otherwise raise
    PermissionError on Windows); and a DIRECTORY target is left to the
    shell, which fails it with the exact "Is a directory" message.
    """

    @pytest.fixture
    def local_ops(self, tmp_path):
        from tools.environments.local import LocalEnvironment

        class _NoSpawnLocal(LocalEnvironment):
            """Real LocalEnvironment, minus the init-time bash snapshot."""

            def init_session(self):
                self._snapshot_ready = False

        env = _NoSpawnLocal(cwd=str(tmp_path))
        env.execute = _must_not_execute
        return ShellFileOperations(env, cwd=str(tmp_path))

    def test_delete_existing_file_uses_no_shell(self, local_ops, tmp_path):
        f = tmp_path / "gone.txt"
        f.write_bytes(b"bye\n")
        r = local_ops.delete_file(str(f))
        assert r.error is None
        assert not f.exists()

    def test_delete_missing_file_is_idempotent_no_shell(self, local_ops, tmp_path):
        # rm -f on a missing file succeeds; os.remove would raise
        # FileNotFoundError, so the native path must treat absent as success.
        r = local_ops.delete_file(str(tmp_path / "never.txt"))
        assert r.error is None

    def test_delete_readonly_file_no_shell(self, local_ops, tmp_path):
        # Windows read-only attribute (git object files): rm -f removes it;
        # os.remove raises PermissionError, so the native path clears the
        # read-only bit first.
        f = tmp_path / "ro.txt"
        f.write_bytes(b"ro\n")
        os.chmod(str(f), stat.S_IREAD)
        r = local_ops.delete_file(str(f))
        assert r.error is None
        assert not f.exists()

    def test_delete_empty_file_no_shell(self, local_ops, tmp_path):
        f = tmp_path / "empty.txt"
        f.write_bytes(b"")
        r = local_ops.delete_file(str(f))
        assert r.error is None
        assert not f.exists()

    def test_delete_deny_listed_path_blocked(self, local_ops):
        # Deny check precedes the native gate; must still block with no
        # delete and no shell spawn.
        denied = os.path.join(str(Path.home()), ".ssh", "id_rsa")
        r = local_ops.delete_file(denied)
        assert r.error is not None
        assert "denied" in r.error.lower()

    def test_delete_directory_falls_back_to_shell(self, tmp_path):
        # rm -f on a directory fails with a specific "Is a directory"
        # message; the native path must defer so that exact error survives.
        from tools.environments.local import LocalEnvironment

        calls = []

        class _CannedLocal(LocalEnvironment):
            def init_session(self):
                self._snapshot_ready = False

            def execute(self, command, cwd="", **kwargs):
                calls.append(command)
                return {"output": "rm: cannot remove: Is a directory", "returncode": 1}

        d = tmp_path / "adir"
        d.mkdir()
        ops = ShellFileOperations(_CannedLocal(cwd=str(tmp_path)), cwd=str(tmp_path))
        r = ops.delete_file(str(d))
        assert calls, "directory delete should defer to the shell rm"
        assert r.error is not None
        assert d.exists()

    def test_delete_non_local_env_keeps_shell_pipeline(self):
        env = MagicMock()
        env.cwd = "/x"
        env.execute.return_value = {"output": "", "returncode": 0}
        ops = ShellFileOperations(env)
        ops.delete_file("/x/whatever.txt")
        assert env.execute.called

    @pytest.mark.skipif(sys.platform != "win32", reason="MSYS path semantics are Windows-only")
    def test_delete_msys_drive_path_no_shell(self, local_ops, tmp_path):
        f = tmp_path / "msys.txt"
        f.write_bytes(b"x\n")
        drive = f.drive.rstrip(":").lower()
        msys = "/" + drive + str(f)[len(f.drive):].replace("\\", "/")
        r = local_ops.delete_file(msys)
        assert r.error is None
        assert not f.exists()

    @pytest.mark.skipif(sys.platform != "win32", reason="MSYS path semantics are Windows-only")
    def test_delete_posix_root_path_falls_back_on_windows(self, tmp_path):
        from tools.environments.local import LocalEnvironment

        calls = []

        class _CannedLocal(LocalEnvironment):
            def init_session(self):
                self._snapshot_ready = False

            def execute(self, command, cwd="", **kwargs):
                calls.append(command)
                return {"output": "", "returncode": 0}

        ops = ShellFileOperations(_CannedLocal(cwd=str(tmp_path)), cwd=str(tmp_path))
        # /tmp/... resolves inside Git Bash's filesystem, not C:\tmp — the
        # native path must not guess; the shell pipeline owns this delete.
        ops.delete_file("/tmp/hermes-native-delpath-probe.txt")
        assert calls, "POSIX-root path should defer to the shell pipeline"


class TestLocalNativeMoveFastPath:
    """move_file on the local backend must not shell out for regular files.

    ``move_file`` shelled a single ``mv`` per call.  A regular file moved
    to a non-directory destination on the same volume is renamed with
    native ``os.replace`` (which overwrites an existing file, matching
    ``mv``); everything ``mv`` handles specially — moving INTO a target
    directory (``dst/basename``), a read-only destination, a missing
    source, a missing destination parent, a cross-device move, or
    src == dst — falls back to the shell so its exact behavior and error
    text survive.

    The pinned expectations are ``mv``'s empirically captured behavior:
    a non-existent dst is created; an existing regular dst is overwritten
    (``os.replace`` matches); an existing-directory dst means "move into
    it" (which ``os.replace`` raises on, so we defer); and a read-only dst
    is force-overwritten by ``mv`` but raises PermissionError under
    ``os.replace`` on Windows (so we defer).
    """

    @pytest.fixture
    def local_ops(self, tmp_path):
        from tools.environments.local import LocalEnvironment

        class _NoSpawnLocal(LocalEnvironment):
            """Real LocalEnvironment, minus the init-time bash snapshot."""

            def init_session(self):
                self._snapshot_ready = False

        env = _NoSpawnLocal(cwd=str(tmp_path))
        env.execute = _must_not_execute
        return ShellFileOperations(env, cwd=str(tmp_path))

    def test_move_to_nonexistent_dst_uses_no_shell(self, local_ops, tmp_path):
        src = tmp_path / "src.txt"
        dst = tmp_path / "dst.txt"
        src.write_bytes(b"payload\n")
        r = local_ops.move_file(str(src), str(dst))
        assert r.error is None
        assert not src.exists()
        assert dst.read_bytes() == b"payload\n"

    def test_move_overwrites_existing_file_no_shell(self, local_ops, tmp_path):
        # mv overwrites an existing regular dst; os.replace matches it.
        src = tmp_path / "src.txt"
        dst = tmp_path / "dst.txt"
        src.write_bytes(b"NEW\n")
        dst.write_bytes(b"OLD-and-longer\n")
        r = local_ops.move_file(str(src), str(dst))
        assert r.error is None
        assert not src.exists()
        assert dst.read_bytes() == b"NEW\n"

    def test_move_deny_listed_src_blocked(self, local_ops, tmp_path):
        # Deny check precedes the native gate; no move, no shell spawn.
        denied = os.path.join(str(Path.home()), ".ssh", "id_rsa")
        r = local_ops.move_file(denied, str(tmp_path / "dst.txt"))
        assert r.error is not None
        assert "denied" in r.error.lower()

    def test_move_deny_listed_dst_blocked(self, local_ops, tmp_path):
        src = tmp_path / "src.txt"
        src.write_bytes(b"x\n")
        denied = os.path.join(str(Path.home()), ".aws", "credentials")
        r = local_ops.move_file(str(src), denied)
        assert r.error is not None
        assert "denied" in r.error.lower()
        # deny precedes the native gate -> src untouched, no shell spawn
        assert src.exists()

    def test_move_into_existing_directory_falls_back(self, tmp_path):
        # mv moves a file INTO a target directory (dst/basename); os.replace
        # raises on that, so the native path must defer to the shell.
        from tools.environments.local import LocalEnvironment

        calls = []

        class _CannedLocal(LocalEnvironment):
            def init_session(self):
                self._snapshot_ready = False

            def execute(self, command, cwd="", **kwargs):
                calls.append(command)
                return {"output": "", "returncode": 0}

        src = tmp_path / "src.txt"
        src.write_bytes(b"x\n")
        dstdir = tmp_path / "destdir"
        dstdir.mkdir()
        ops = ShellFileOperations(_CannedLocal(cwd=str(tmp_path)), cwd=str(tmp_path))
        ops.move_file(str(src), str(dstdir))
        assert calls, "move into a directory should defer to the shell mv"

    def test_move_readonly_dst_falls_back(self, tmp_path):
        # os.replace over a read-only dst raises PermissionError on Windows;
        # mv force-overwrites it. Defer so the shell wins.
        from tools.environments.local import LocalEnvironment

        calls = []

        class _CannedLocal(LocalEnvironment):
            def init_session(self):
                self._snapshot_ready = False

            def execute(self, command, cwd="", **kwargs):
                calls.append(command)
                return {"output": "", "returncode": 0}

        src = tmp_path / "src.txt"
        src.write_bytes(b"x\n")
        dst = tmp_path / "dst_ro.txt"
        dst.write_bytes(b"old\n")
        os.chmod(str(dst), stat.S_IREAD)
        try:
            ops = ShellFileOperations(_CannedLocal(cwd=str(tmp_path)), cwd=str(tmp_path))
            ops.move_file(str(src), str(dst))
            assert calls, "read-only dst should defer to the shell mv"
        finally:
            os.chmod(str(dst), stat.S_IWRITE)  # let tmp_path teardown remove it

    def test_move_missing_src_falls_back(self, tmp_path):
        from tools.environments.local import LocalEnvironment

        calls = []

        class _CannedLocal(LocalEnvironment):
            def init_session(self):
                self._snapshot_ready = False

            def execute(self, command, cwd="", **kwargs):
                calls.append(command)
                return {"output": "mv: cannot stat: No such file or directory", "returncode": 1}

        ops = ShellFileOperations(_CannedLocal(cwd=str(tmp_path)), cwd=str(tmp_path))
        r = ops.move_file(str(tmp_path / "ghost.txt"), str(tmp_path / "dst.txt"))
        assert calls, "missing src should defer to the shell mv"
        assert r.error is not None

    def test_move_dst_parent_missing_falls_back(self, tmp_path):
        # os.replace raises FileNotFoundError (atomically, src intact) when
        # the dst parent is missing; mv reports a specific error. Defer.
        from tools.environments.local import LocalEnvironment

        calls = []

        class _CannedLocal(LocalEnvironment):
            def init_session(self):
                self._snapshot_ready = False

            def execute(self, command, cwd="", **kwargs):
                calls.append(command)
                return {"output": "mv: cannot move: No such file or directory", "returncode": 1}

        src = tmp_path / "src.txt"
        src.write_bytes(b"x\n")
        ops = ShellFileOperations(_CannedLocal(cwd=str(tmp_path)), cwd=str(tmp_path))
        r = ops.move_file(str(src), str(tmp_path / "nodir" / "dst.txt"))
        assert calls, "missing dst parent should defer to the shell mv"
        assert r.error is not None
        assert src.exists()  # os.replace failed atomically — src untouched

    def test_move_same_path_falls_back(self, tmp_path):
        # mv on src == dst is a successful no-op; os.replace(p, p) semantics
        # are murky across platforms, so the native path defers.
        from tools.environments.local import LocalEnvironment

        calls = []

        class _CannedLocal(LocalEnvironment):
            def init_session(self):
                self._snapshot_ready = False

            def execute(self, command, cwd="", **kwargs):
                calls.append(command)
                return {"output": "", "returncode": 0}

        p = tmp_path / "same.txt"
        p.write_bytes(b"same\n")
        ops = ShellFileOperations(_CannedLocal(cwd=str(tmp_path)), cwd=str(tmp_path))
        ops.move_file(str(p), str(p))
        assert calls, "src == dst should defer to the shell mv"
        assert p.read_bytes() == b"same\n"  # not lost

    def test_move_non_local_env_keeps_shell_pipeline(self):
        env = MagicMock()
        env.cwd = "/x"
        env.execute.return_value = {"output": "", "returncode": 0}
        ops = ShellFileOperations(env)
        ops.move_file("/x/a.txt", "/x/b.txt")
        assert env.execute.called

    @pytest.mark.skipif(sys.platform != "win32", reason="MSYS path semantics are Windows-only")
    def test_move_msys_drive_path_no_shell(self, local_ops, tmp_path):
        src = tmp_path / "src.txt"
        src.write_bytes(b"MSYS\n")
        dst = tmp_path / "dst.txt"
        drive = src.drive.rstrip(":").lower()
        msys_src = "/" + drive + str(src)[len(src.drive):].replace("\\", "/")
        msys_dst = "/" + drive + str(dst)[len(dst.drive):].replace("\\", "/")
        r = local_ops.move_file(msys_src, msys_dst)
        assert r.error is None
        assert not src.exists()
        assert dst.read_bytes() == b"MSYS\n"

    @pytest.mark.skipif(sys.platform != "win32", reason="MSYS path semantics are Windows-only")
    def test_move_posix_root_path_falls_back_on_windows(self, tmp_path):
        from tools.environments.local import LocalEnvironment

        calls = []

        class _CannedLocal(LocalEnvironment):
            def init_session(self):
                self._snapshot_ready = False

            def execute(self, command, cwd="", **kwargs):
                calls.append(command)
                return {"output": "", "returncode": 0}

        ops = ShellFileOperations(_CannedLocal(cwd=str(tmp_path)), cwd=str(tmp_path))
        # /tmp/... resolves inside Git Bash, not C:\tmp — defer to the shell.
        ops.move_file("/tmp/hermes-native-movesrc.txt", str(tmp_path / "d.txt"))
        assert calls, "POSIX-root src should defer to the shell pipeline"
