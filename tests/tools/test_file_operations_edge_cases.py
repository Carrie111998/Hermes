"""Tests for edge cases in tools/file_operations.py.

Covers:
- ``_is_likely_binary()`` content-analysis branch (dead-code removal regression guard)
- ``_check_lint()`` robustness against file paths containing curly braces
- ``_sample_utf8_text()`` byte-accurate UTF-8 sampling (#76886 — read_file
  misclassifying valid CJK/Korean/Thai/Cyrillic text as binary when the
  1000-byte sample boundary cuts a multibyte char)
"""

import base64
import subprocess

import pytest
from unittest.mock import MagicMock, patch

from tools.file_operations import ShellFileOperations, _parse_search_context_line


# =========================================================================
# _is_likely_binary edge cases
# =========================================================================


class TestIsLikelyBinary:
    """Verify content-analysis logic after dead-code removal."""

    @pytest.fixture()
    def ops(self):
        return ShellFileOperations.__new__(ShellFileOperations)

    def test_binary_extension_returns_true(self, ops):
        """Known binary extensions should short-circuit without content analysis."""
        assert ops._is_likely_binary("image.png") is True
        assert ops._is_likely_binary("archive.tar.gz", content_sample="hello") is True

    def test_text_content_returns_false(self, ops):
        """Normal printable text should not be classified as binary."""
        sample = "Hello, world!\nThis is a normal text file.\n"
        assert ops._is_likely_binary("unknown.xyz", content_sample=sample) is False


    def test_just_above_threshold(self, ops):
        """301/1000 = 30.1% non-printable → should be binary."""
        sample = "\x00" * 301 + "a" * 699
        assert ops._is_likely_binary("data.xyz", content_sample=sample) is True

    def test_tabs_and_newlines_excluded(self, ops):
        """Tabs, carriage returns, and newlines should not count as non-printable."""
        sample = "\t" * 400 + "\n" * 300 + "\r" * 200 + "a" * 100
        assert ops._is_likely_binary("file.txt", content_sample=sample) is False

    def test_content_sample_longer_than_1000(self, ops):
        """Only the first 1000 characters should be analysed."""
        # First 1000 chars: 200 NUL + 800 printable = 20% → not binary
        # Remaining 1000 chars: all NUL → ignored by [:1000] slice
        sample = "\x00" * 200 + "a" * 800 + "\x00" * 1000
        assert ops._is_likely_binary("file.xyz", content_sample=sample) is False


# =========================================================================
# _check_lint edge cases
# =========================================================================


class TestCheckLintBracePaths:
    """Verify _check_lint handles file paths with curly braces safely.

    Uses ``.js`` to exercise the shell-linter path since ``.py`` now goes
    through the in-process ast.parse linter (see TestCheckLintInproc).
    """

    @pytest.fixture()
    def ops(self):
        obj = ShellFileOperations.__new__(ShellFileOperations)
        obj._command_cache = {}
        return obj

    def test_normal_path(self, ops):
        """Normal path without braces should work as before."""
        with patch.object(ops, "_has_command", return_value=True), \
             patch.object(ops, "_exec") as mock_exec:
            mock_exec.return_value = MagicMock(exit_code=0, stdout="")
            result = ops._check_lint("/tmp/test_file.js")

        assert result.success is True
        # Verify the command was built correctly
        cmd_arg = mock_exec.call_args[0][0]
        assert "'/tmp/test_file.js'" in cmd_arg

    def test_path_with_curly_braces(self, ops):
        """Path containing ``{`` and ``}`` must not raise KeyError/ValueError."""
        with patch.object(ops, "_has_command", return_value=True), \
             patch.object(ops, "_exec") as mock_exec:
            mock_exec.return_value = MagicMock(exit_code=0, stdout="")
            # This would raise KeyError with .format() but works with .replace()
            result = ops._check_lint("/tmp/{test}_file.js")

        assert result.success is True
        cmd_arg = mock_exec.call_args[0][0]
        assert "{test}" in cmd_arg

    def test_path_with_nested_braces(self, ops):
        """Path with complex brace patterns like ``{{var}}`` should be safe."""
        with patch.object(ops, "_has_command", return_value=True), \
             patch.object(ops, "_exec") as mock_exec:
            mock_exec.return_value = MagicMock(exit_code=0, stdout="")
            result = ops._check_lint("/tmp/{{var}}.js")

        assert result.success is True

    def test_unsupported_extension_skipped(self, ops):
        """Extensions without a linter should return a skipped result."""
        result = ops._check_lint("/tmp/file.unknown_ext")
        assert result.skipped is True

    def test_missing_linter_skipped(self, ops):
        """When the linter binary is not installed, skip gracefully."""
        with patch.object(ops, "_has_command", return_value=False):
            result = ops._check_lint("/tmp/test.js")
        assert result.skipped is True

    def test_lint_failure_returns_output(self, ops):
        """When the linter exits non-zero, result should capture output."""
        with patch.object(ops, "_has_command", return_value=True), \
             patch.object(ops, "_exec") as mock_exec:
            mock_exec.return_value = MagicMock(
                exit_code=1,
                stdout="SyntaxError: invalid syntax",
            )
            result = ops._check_lint("/tmp/bad.js")

        assert result.success is False
        assert "SyntaxError" in result.output


class TestCheckLintInproc:
    """Verify in-process linters (.py via ast.parse, .json, .yaml, .toml).

    These bypass the shell linter table entirely and parse content
    directly in Python — no subprocess, no toolchain dependency.
    """

    @pytest.fixture()
    def ops(self):
        obj = ShellFileOperations.__new__(ShellFileOperations)
        obj._command_cache = {}
        return obj

    def test_python_inproc_clean(self, ops):
        """Valid Python content passes in-process ast.parse."""
        result = ops._check_lint("/tmp/ok.py", content="x = 1\n")
        assert result.success is True
        assert not result.skipped
        assert result.output == ""


    def test_json_inproc_clean(self, ops):
        result = ops._check_lint("/tmp/a.json", content='{"a": 1}')
        assert result.success is True


    def test_toml_inproc_error(self, ops):
        result = ops._check_lint("/tmp/b.toml", content='[section\nk = "v"')
        assert result.success is False
        assert "TOMLDecodeError" in result.output


class TestCheckLintDelta:
    """Verify _check_lint_delta() filters pre-existing errors from post-edit output."""

    @pytest.fixture()
    def ops(self):
        obj = ShellFileOperations.__new__(ShellFileOperations)
        obj._command_cache = {}
        return obj

    def test_clean_post_no_pre_lint(self, ops):
        """Hot path: post-write is clean, pre-lint should be skipped entirely."""
        with patch.object(ops, "_check_lint", wraps=ops._check_lint) as wrapped:
            r = ops._check_lint_delta("/tmp/a.py", pre_content="x = 0\n", post_content="x = 1\n")
            # Post-lint called exactly once (clean), pre-lint never called.
            assert wrapped.call_count == 1
        assert r.success is True


    def test_pre_existing_remains_flagged_but_not_new(self, ops):
        """Single-error parsers (ast) may miss that post is OK — be cautious."""
        # Pre has line-1 error, post keeps it (and doesn't add anything new)
        pre = 'def a(:\n    pass\n'
        post = 'def a(:\n    pass\n\nprint(42)\n'  # still line 1 broken
        r = ops._check_lint_delta("/tmp/d.py", pre_content=pre, post_content=post)
        # File is still broken — don't lie and claim success — but flag it as pre-existing
        assert r.success is False
        assert "pre-existing" in (r.message or "").lower()


# =========================================================================
# Pagination bounds
# =========================================================================


class TestPaginationBounds:
    """Invalid pagination inputs should not leak into shell commands."""

    def test_read_file_clamps_offset_and_limit_before_building_sed_range(self):
        env = MagicMock()
        env.cwd = "/tmp"
        ops = ShellFileOperations(env)
        commands = []

        def fake_exec(command, *args, **kwargs):
            commands.append(command)
            if command.startswith("wc -c"):
                return MagicMock(exit_code=0, stdout="12")
            if command.startswith("head -c"):
                # read_file now samples via `head -c 1000 ... | base64`
                # (byte-accurate UTF-8 sampling, #76886).
                return MagicMock(
                    exit_code=0,
                    stdout=base64.encodebytes(b"line1\nline2\n").decode(),
                )
            if command.startswith("sed -n"):
                return MagicMock(exit_code=0, stdout="line1\n")
            if command.startswith("wc -l"):
                return MagicMock(exit_code=0, stdout="2")
            return MagicMock(exit_code=0, stdout="")

        with patch.object(ops, "_exec", side_effect=fake_exec):
            result = ops.read_file("notes.txt", offset=0, limit=0)

        assert result.error is None
        assert "1|line1" in result.content
        sed_commands = [cmd for cmd in commands if cmd.startswith("sed -n")]
        assert sed_commands == ["sed -n '1,1p' 'notes.txt'"]

    def test_search_clamps_offset_and_limit_before_building_head_pipeline(self):
        env = MagicMock()
        env.cwd = "/tmp"
        ops = ShellFileOperations(env)
        commands = []

        def fake_exec(command, *args, **kwargs):
            commands.append(command)
            if command.startswith("test -e"):
                return MagicMock(exit_code=0, stdout="exists")
            if command.startswith("rg --files"):
                return MagicMock(exit_code=0, stdout="a.py\n")
            return MagicMock(exit_code=0, stdout="")

        with patch.object(ops, "_has_command", side_effect=lambda cmd: cmd == "rg"), \
             patch.object(ops, "_exec", side_effect=fake_exec):
            result = ops.search("*.py", target="files", path=".", offset=-4, limit=-2)

        assert result.files == ["a.py"]
        rg_commands = [cmd for cmd in commands if cmd.startswith("rg --files")]
        assert rg_commands
        assert "| head -n 1" in rg_commands[0]


# =========================================================================
# Search context parsing
# =========================================================================


class TestSearchContextParsing:
    def test_search_with_grep_uses_extended_regex(self):
        env = MagicMock()
        env.cwd = "/tmp"
        ops = ShellFileOperations(env)

        with patch.object(ops, "_exec") as mock_exec:
            mock_exec.return_value = MagicMock(
                exit_code=0,
                stdout="./first.txt:1:foo\n./second.txt:1:bar\n",
            )
            result = ops._search_with_grep(
                "foo|bar",
                path=".",
                file_glob=None,
                limit=10,
                offset=0,
                output_mode="content",
                context=0,
            )

        cmd_arg = mock_exec.call_args[0][0]
        assert cmd_arg.startswith("set -o pipefail; grep -rnHE ")
        assert result.error is None
        assert result.total_count == 2
        assert [match.content for match in result.matches] == ["foo", "bar"]

    def test_parse_search_context_line_prefers_rightmost_numeric_separator(self):
        parsed = _parse_search_context_line("dir/file-12-name.py-8-context here")

        assert parsed == ("dir/file-12-name.py", 8, "context here")


    def test_search_with_grep_context_handles_filename_with_dash_digits(self):
        env = MagicMock()
        env.cwd = "/tmp"
        ops = ShellFileOperations(env)

        with patch.object(ops, "_exec") as mock_exec:
            mock_exec.return_value = MagicMock(
                exit_code=0,
                stdout="dir/file-12-name.py-8-context here\n",
            )
            result = ops._search_with_grep(
                "needle",
                path=".",
                file_glob=None,
                limit=10,
                offset=0,
                output_mode="content",
                context=1,
            )

        assert result.error is None
        assert result.total_count == 1
        assert result.matches[0].path == "dir/file-12-name.py"
        assert result.matches[0].line_number == 8
        assert result.matches[0].content == "context here"


# =========================================================================
# _sample_utf8_text — byte-accurate UTF-8 sampling (#76886)
# =========================================================================


def _make_real_shell_env(cwd: str) -> MagicMock:
    """Mock env whose execute() runs the command in a real shell.

    Uses ``sh`` explicitly so the ``head | base64`` pipeline in
    ``_sample_utf8_text`` exercises real coreutils on both POSIX and
    Windows (git-bash) hosts.

    Faithful to the real Hermes terminal env: subprocess stdout is
    decoded as UTF-8 with ``errors="replace"`` (NOT the locale codec),
    because the #76886 bug only manifests when the sample crosses that
    lossy decode boundary. A locale-decode harness (e.g. cp1252) would
    turn the cut bytes into mojibake without U+FFFD and silently miss
    the regression.
    """
    env = MagicMock()
    env.cwd = cwd

    def execute(command, **kwargs):
        completed = subprocess.run(
            ["sh", "-c", command],
            capture_output=True,
            input=kwargs.get("stdin_data"),
        )
        return {
            "output": completed.stdout.decode("utf-8", errors="replace"),
            "returncode": completed.returncode,
        }

    env.execute = execute
    return env


def _b64_sample(raw: bytes) -> str:
    """Encode raw sample bytes exactly as the shell pipeline returns them:
    ``head -c 1000 file | base64`` (base64 may wrap lines at 76 cols)."""
    return base64.encodebytes(raw).decode()


class TestSampleUtf8Text:
    """Unit tests for the byte-accurate UTF-8 sampler."""

    def _ops_with_sample(self, raw: bytes, exit_code: int = 0) -> ShellFileOperations:
        env = MagicMock()
        env.cwd = "/tmp"
        env.execute.return_value = {
            "output": _b64_sample(raw),
            "returncode": exit_code,
        }
        return ShellFileOperations(env)

    def test_clean_utf8_text_passes_through(self):
        raw = "plain ascii text\n".encode() * 50  # 800 bytes, all ASCII
        ops = self._ops_with_sample(raw)
        sample = ops._sample_utf8_text("/tmp/f.txt", file_size=len(raw))
        assert sample is not None
        assert "\ufffd" not in sample
        assert sample == raw.decode("utf-8")

    def test_boundary_cut_multibyte_char_is_text_not_binary(self):
        # 998 ASCII bytes + a 3-byte CJK char that starts at byte 998:
        # ``head -c 1000`` keeps bytes 0..999, i.e. the first 2 bytes of
        # the CJK char. The old lossy decode produced a synthetic U+FFFD
        # and flagged the file binary (the #76886 regression).
        raw = b"a" * 998 + "中".encode("utf-8")[:2]
        assert len(raw) == 1000  # exactly the sample window
        ops = self._ops_with_sample(raw)
        sample = ops._sample_utf8_text("/tmp/f.txt", file_size=len(raw) + 10)
        assert sample is not None
        assert sample == "a" * 998
        assert "\ufffd" not in sample

    def test_genuine_mid_stream_invalid_bytes_still_binary(self):
        raw = b"ok" + b"\xff\xfe\x00\x01" + b"tail" * 100
        ops = self._ops_with_sample(raw)
        sample = ops._sample_utf8_text("/tmp/f.bin", file_size=len(raw) + 10)
        assert sample is None  # mid-stream corruption -> binary

    def test_small_file_ending_mid_char_is_binary(self):
        # File smaller than the sample window that genuinely ends with an
        # incomplete sequence: that is true truncation, not a boundary
        # artifact — the mojibake round-trip guard must keep it binary.
        raw = b"abc" + "中".encode("utf-8")[:2]  # 5 bytes, incomplete tail
        ops = self._ops_with_sample(raw)
        sample = ops._sample_utf8_text("/tmp/f.txt", file_size=len(raw))
        assert sample is None

    def test_exec_failure_falls_back_to_empty_sample(self):
        env = MagicMock()
        env.cwd = "/tmp"
        env.execute.return_value = {"output": "", "returncode": 1}
        ops = ShellFileOperations(env)
        assert ops._sample_utf8_text("/tmp/f.txt", file_size=10) == ""


class TestReadFileUtf8BoundaryE2E:
    """End-to-end: read_file/read_file_raw must not reject CJK text whose
    1000-byte boundary cuts a multibyte char (regression for #76886)."""

    def test_read_file_cjk_boundary_cut_not_binary(self, tmp_path):
        # 998 ASCII bytes, then CJK: byte 999 is the first byte of a
        # 3-byte char, so head -c 1000 cuts it mid-sequence.
        body = b"a" * 998 + "中文测试\n".encode("utf-8") * 20
        p = tmp_path / "cjk_notes.md"
        p.write_bytes(body)

        ops = ShellFileOperations(_make_real_shell_env(str(tmp_path)))
        result = ops.read_file(str(p))

        assert result.is_binary is False
        assert result.error is None
        assert "中文测试" in result.content

    def test_read_file_raw_cjk_boundary_cut_not_binary(self, tmp_path):
        body = b"a" * 998 + "中文测试\n".encode("utf-8") * 20
        p = tmp_path / "cjk_notes.md"
        p.write_bytes(body)

        ops = ShellFileOperations(_make_real_shell_env(str(tmp_path)))
        result = ops.read_file_raw(str(p))

        assert result.is_binary is False
        assert result.error is None
        assert "中文测试" in result.content

    def test_read_file_genuine_binary_still_rejected(self, tmp_path):
        # Invalid UTF-8 mid-stream (not a boundary artifact) must remain
        # binary after the fix.
        body = b"a" * 998 + b"\xff\xfe\x00\x01" + b"b" * 200
        p = tmp_path / "junk.dat"
        p.write_bytes(body)

        ops = ShellFileOperations(_make_real_shell_env(str(tmp_path)))
        result = ops.read_file(str(p))

        assert result.is_binary is True

