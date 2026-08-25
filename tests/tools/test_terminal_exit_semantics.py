"""Tests for terminal command exit code semantic interpretation."""

import pytest

from tools.terminal_tool import _interpret_exit_code


class TestInterpretExitCode:
    """Test _interpret_exit_code returns correct notes for known command semantics."""

    # ---- exit code 0 always returns None ----

    def test_success_returns_none(self):
        assert _interpret_exit_code("grep foo bar", 0) is None
        assert _interpret_exit_code("diff a b", 0) is None
        assert _interpret_exit_code("test -f /etc/passwd", 0) is None

    # ---- grep / rg family: exit 1 = no matches ----

    @pytest.mark.parametrize("cmd", [
        "grep 'pattern' file.txt",
        "egrep 'pattern' file.txt",
        "fgrep 'pattern' file.txt",
        "rg 'foo' .",
        "ag 'foo' .",
        "ack 'foo' .",
    ])
    def test_grep_family_no_matches(self, cmd):
        result = _interpret_exit_code(cmd, 1)
        assert result is not None
        assert "no matches" in result.lower()


    # ---- diff: exit 1 = files differ ----

    def test_diff_files_differ(self):
        result = _interpret_exit_code("diff file1 file2", 1)
        assert result is not None
        assert "differ" in result.lower()

    def test_colordiff_files_differ(self):
        result = _interpret_exit_code("colordiff file1 file2", 1)
        assert result is not None
        assert "differ" in result.lower()


    # ---- test / [: exit 1 = condition false ----

    def test_test_condition_false(self):
        result = _interpret_exit_code("test -f /nonexistent", 1)
        assert result is not None
        assert "false" in result.lower()


    # ---- find: exit 1 = partial success ----


    # ---- curl: various informational codes ----


    # ---- git: exit 1 is context-dependent ----


    # ---- pipeline / chain handling ----

    @pytest.mark.parametrize("cmd", [
        "crontab -l | grep -c parlami-queue-health.py",
        "false | grep needle",
        "false || grep needle",
        "false && grep needle",
        "false & grep needle",
        "false; grep needle",
        'grep "$(printf needle | cat)" file.txt',
        "grep `printf needle` file.txt",
        "grep needle <(printf needle)",
        r"grep 'a\' file.txt | grep needle",
        r"grep $'a\'b' file.txt | grep needle",
        "grep missing file.txt 2>&1",
        "grep missing file.txt &>/tmp/grep.out",
        "grep missing file.txt 3</dev/null 0<&3",
        "grep missing file.txt >| /tmp/grep.out",
        "grep needle /dev/null > /missing-dir/out",
    ])
    def test_masking_compound_commands_do_not_get_benign_grep_meaning(self, cmd):
        """A final grep status cannot prove earlier segments succeeded."""
        assert _interpret_exit_code(cmd, 1) is None


    @pytest.mark.parametrize("cmd", [
        "grep 'a|b' file.txt",
        'grep "a;b" file.txt',
        "grep 'a&&b' file.txt",
        r"grep a\|b file.txt",
        r"grep a\;b file.txt",
        "grep $'a|b' file.txt",
        "grep missing file.txt # | ignored",
        "grep missing \\\nfile.txt",
    ])
    def test_inert_operators_and_redirections_keep_simple_grep_semantics(self, cmd):
        result = _interpret_exit_code(cmd, 1)
        assert result is not None
        assert "no matches" in result.lower()


    # ---- full paths ----


    # ---- env var prefix ----


    # ---- unknown commands return None ----


    # ---- edge cases ----


    def test_only_env_vars(self):
        """Command with only env var assignments, no actual command."""
        assert _interpret_exit_code("FOO=bar", 1) is None
