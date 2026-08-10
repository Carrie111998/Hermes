"""Tests for blocked-command recovery guidance (parser-limit + backgrounding)."""

import pytest

from tools.approval import _hardline_block_result, _PARSER_LIMIT_DESCRIPTION, _MALFORMED_EXEC_DESCRIPTION
from tools.terminal_tool import _foreground_background_guidance, _strip_heredoc_bodies


class TestParserLimitRecovery:
    def test_parser_limit_block_saves_payload_and_names_it(self, tmp_path, monkeypatch):
        monkeypatch.setenv("HERMES_HOME", str(tmp_path / ".hermes"))
        cmd = "python3 -c '" + "x = 1; " * 900 + "'"
        r = _hardline_block_result(_PARSER_LIMIT_DESCRIPTION, cmd)
        assert r["approved"] is False
        assert "RECOVERY" in r["message"]
        assert "blocked-scripts" in r["message"]
        import re as _re
        m = _re.search(r"saved to (\S+\.sh)", r["message"])
        assert m, r["message"]
        from pathlib import Path
        saved = Path(m.group(1))
        assert saved.exists()
        body = saved.read_text()
        assert cmd in body
        assert body.startswith("#!/bin/bash")
        assert f"bash {saved}" in r["message"]

    def test_save_failure_falls_back_to_manual_recipe(self, monkeypatch):
        import tools.approval as ap
        monkeypatch.setattr(ap, "_save_blocked_payload", lambda c: None)
        r = _hardline_block_result(_PARSER_LIMIT_DESCRIPTION, "python3 -c 'x'")
        assert "write_file" in r["message"]
        assert "bash /path/script.sh" in r["message"]

    def test_no_command_falls_back_to_manual_recipe(self):
        r = _hardline_block_result(_PARSER_LIMIT_DESCRIPTION)
        assert "RECOVERY" in r["message"]
        assert "write_file" in r["message"]

    def test_malformed_exec_block_has_recovery_recipe(self):
        r = _hardline_block_result(_MALFORMED_EXEC_DESCRIPTION)
        assert "RECOVERY" in r["message"]

    def test_real_hardline_blocks_unchanged(self, tmp_path, monkeypatch):
        monkeypatch.setenv("HERMES_HOME", str(tmp_path / ".hermes"))
        r = _hardline_block_result("recursive delete of root filesystem", "rm -rf --no-preserve-root /")
        assert "RECOVERY" not in r["message"]
        assert "unconditional blocklist" in r["message"]
        # And nothing was saved for a genuine hardline block.
        assert not (tmp_path / ".hermes" / "cache" / "blocked-scripts").exists()

    def test_old_saved_payloads_cleaned(self, tmp_path, monkeypatch):
        monkeypatch.setenv("HERMES_HOME", str(tmp_path / ".hermes"))
        import os
        d = tmp_path / ".hermes" / "cache" / "blocked-scripts"
        d.mkdir(parents=True)
        stale = d / "blocked-1-dead.sh"
        stale.write_text("old")
        os.utime(stale, (1, 1))
        _hardline_block_result(_PARSER_LIMIT_DESCRIPTION, "python3 -c 'y'")
        assert not stale.exists()


class TestBackgroundGuidanceRecipes:
    def test_ampersand_block_names_exact_call_shape(self):
        msg = _foreground_background_guidance("python3 server.py &")
        assert msg is not None
        assert "WITHOUT the '&'" in msg
        assert "background=true" in msg

    def test_nohup_block_names_exact_call_shape(self):
        msg = _foreground_background_guidance("nohup ./worker.sh > /dev/null 2>&1")
        assert msg is not None
        assert "WITHOUT the wrapper" in msg
        assert "notify_on_complete=true" in msg

    def test_plain_command_unaffected(self):
        assert _foreground_background_guidance("echo hello") is None

    def test_quoted_ampersand_not_flagged(self):
        assert _foreground_background_guidance('git commit -m "a & b"') is None


class TestHeredocBackgroundGuidance:
    """Heredoc bodies are inline data — '&', 'nohup', etc. inside them must not
    trigger backgrounding guidance, while real backgrounding after the heredoc
    still must."""

    def test_quoted_heredoc_python_bitwise_and_not_flagged(self):
        cmd = (
            "python3 - <<'EOF'\n"
            "import os\n"
            "st = os.stat('/tmp/x')\n"
            "print(oct(st.st_mode & 0o777))\n"
            "EOF"
        )
        assert _foreground_background_guidance(cmd) is None

    def test_unquoted_heredoc_ampersand_text_not_flagged(self):
        cmd = "python3 <<EOF\nprint('up & own')\nEOF"
        assert _foreground_background_guidance(cmd) is None

    def test_heredoc_nohup_text_not_flagged(self):
        cmd = "cat <<'EOF'\nrun it with nohup or setsid if you like\nEOF"
        assert _foreground_background_guidance(cmd) is None

    def test_dash_heredoc_tab_indented_body_not_flagged(self):
        cmd = "cat <<-EOF\n\tbody & stuff\n\tEOF"
        assert _foreground_background_guidance(cmd) is None

    def test_unterminated_heredoc_body_not_flagged(self):
        # An unterminated heredoc consumes the rest of the command as data.
        cmd = "python3 <<EOF\na & b"
        assert _foreground_background_guidance(cmd) is None

    def test_real_background_ampersand_after_heredoc_still_blocked(self):
        cmd = "python3 - <<'EOF'\nprint(1)\nEOF\nsleep 30 &"
        msg = _foreground_background_guidance(cmd)
        assert msg is not None
        assert "WITHOUT the '&'" in msg

    def test_real_nohup_after_heredoc_still_blocked(self):
        cmd = "cat <<EOF\nhi\nEOF\nnohup ./worker.sh > /dev/null 2>&1"
        msg = _foreground_background_guidance(cmd)
        assert msg is not None
        assert "WITHOUT the wrapper" in msg

    # --- Adversarial regressions for review findings ---

    def test_punctuation_bare_delimiter_does_not_swallow_trailing_nohup(self):
        cmd = "cat <<EOF-X\nline1\nEOF-X\nnohup ./worker.sh > /dev/null 2>&1"
        msg = _foreground_background_guidance(cmd)
        assert msg is not None
        assert "WITHOUT the wrapper" in msg

    def test_comment_containing_heredoc_opener_does_not_strip_nohup(self):
        cmd = "echo ok # <<EOF\nnohup bad &"
        msg = _foreground_background_guidance(cmd)
        assert msg is not None
        assert "WITHOUT the wrapper" in msg

    def test_foo_hash_bar_is_not_a_comment(self):
        # Inline `#` without preceding whitespace/operator is not a comment.
        cmd = "foo#bar <<EOF\nnohup inside\nEOF"
        assert _foreground_background_guidance(cmd) is None

    def test_multiple_heredocs_consumed_in_shell_order(self):
        cmd = (
            "cat <<A <<B\n"
            "body of A\n"
            "A\n"
            "body of B & stuff\n"
            "B\n"
            "nohup real &"
        )
        msg = _foreground_background_guidance(cmd)
        assert msg is not None
        assert "WITHOUT the wrapper" in msg

    def test_multiple_heredoc_bodies_ignore_background_operators(self):
        cmd = "cat <<A <<B\nnohup in A\nA\nsetsid in B &\nB"
        assert _foreground_background_guidance(cmd) is None

    def test_composite_single_quoted_delimiter_terminates_body(self):
        cmd = "cat <<'EO'F\nhi\nEOF\nnohup x &"
        msg = _foreground_background_guidance(cmd)
        assert msg is not None
        assert "WITHOUT the wrapper" in msg

    def test_composite_backslash_delimiter_terminates_body(self):
        cmd = "cat <<EO\\F\nhi\nEOF\nnohup x &"
        msg = _foreground_background_guidance(cmd)
        assert msg is not None
        assert "WITHOUT the wrapper" in msg

    def test_composite_double_quoted_delimiter_terminates_body(self):
        cmd = 'cat <<"EO"F\nhi\nEOF\nnohup x &'
        msg = _foreground_background_guidance(cmd)
        assert msg is not None
        assert "WITHOUT the wrapper" in msg

    def test_composite_mixed_quote_delimiter_terminates_body(self):
        cmd = "cat <<'E'\"O\"F\nhi\nEOF\nnohup x &"
        msg = _foreground_background_guidance(cmd)
        assert msg is not None
        assert "WITHOUT the wrapper" in msg

    def test_unterminated_quote_delimiter_fail_closed(self):
        cmd = "cat <<'EOF\nnohup x &"
        msg = _foreground_background_guidance(cmd)
        assert msg is not None
        assert "WITHOUT the wrapper" in msg

    def test_empty_delimiter_fail_closed(self):
        cmd = "cat << \nnohup x &"
        msg = _foreground_background_guidance(cmd)
        assert msg is not None
        assert "WITHOUT the wrapper" in msg

    @pytest.mark.parametrize(
        "body_cmd",
        [
            "cat <<EOF-X\nnohup & setsid\nEOF-X",
            "cat <<'EOF'\nnohup & setsid\nEOF",
            'cat <<"EOF"\nnohup & setsid\nEOF',
            "cat <<\\EOF\nnohup & setsid\nEOF",
            "cat <<'EO'F\nnohup & setsid\nEOF",
            "cat <<-EOF\n\tnohup & setsid\n\tEOF",
        ],
    )
    def test_background_text_inside_each_delimiter_style_ignored(self, body_cmd):
        assert _foreground_background_guidance(body_cmd) is None

    @pytest.mark.parametrize(
        "cmd,expect_amp,expect_wrapper",
        [
            ("cat <<EOF-X\nx\nEOF-X\nsleep 30 &", True, False),
            ("cat <<'EOF'\nx\nEOF\nsleep 30 &", True, False),
            ('cat <<"EOF"\nx\nEOF\nsleep 30 &', True, False),
            ("cat <<'EO'F\nx\nEOF\nsleep 30 &", True, False),
            (
                "cat <<A <<B\na\nA\nb\nB\nnohup ./w.sh > /dev/null 2>&1",
                False,
                True,
            ),
        ],
    )
    def test_real_background_after_final_delimiter_detected(
        self, cmd, expect_amp, expect_wrapper
    ):
        msg = _foreground_background_guidance(cmd)
        assert msg is not None
        if expect_amp:
            assert "WITHOUT the '&'" in msg
        if expect_wrapper:
            assert "WITHOUT the wrapper" in msg

    @pytest.mark.parametrize(
        "prefix",
        [
            'echo "<<EOF"',
            "echo '<<EOF'",
            "echo ok # <<EOF",
        ],
    )
    def test_quoted_or_commented_heredoc_opener_ignored(self, prefix):
        cmd = f"{prefix}\nnohup ./worker.sh > /dev/null 2>&1"
        msg = _foreground_background_guidance(cmd)
        assert msg is not None
        assert "WITHOUT the wrapper" in msg

    # --- Expandable / ANSI-C delimiter words: fail closed ---

    @pytest.mark.parametrize(
        "opener,suffix,expect_wrapper,expect_amp",
        [
            ("cat <<$'EOF'", "nohup ./worker.sh > /dev/null 2>&1", True, False),
            ('cat <<$"EOF"', "nohup ./worker.sh > /dev/null 2>&1", True, False),
            ("cat <<$DELIM", "sleep 30 &", False, True),
            ("cat <<EOF$X", "nohup x &", True, False),
            ("cat <<$(cmd)EOF", "nohup x &", True, False),
            ("cat <<`EOF`", "nohup x &", True, False),
        ],
    )
    def test_expandable_delimiter_fail_closed_preserves_trailing_command(
        self, opener, suffix, expect_wrapper, expect_amp
    ):
        cmd = f"{opener}\nhi\nEOF\n{suffix}"
        msg = _foreground_background_guidance(cmd)
        assert msg is not None
        if expect_wrapper:
            assert "WITHOUT the wrapper" in msg
        if expect_amp:
            assert "WITHOUT the '&'" in msg

    def test_literal_dollar_in_single_quoted_delimiter_strips_body(self):
        cmd = "cat <<'E$F'\nnohup inside\nE$F"
        assert _foreground_background_guidance(cmd) is None

    def test_backslash_escaped_dollar_delimiter_strips_body(self):
        cmd = "cat <<E\\$F\nnohup inside\nE$F"
        assert _foreground_background_guidance(cmd) is None


class TestStripHeredocArithmetic:
    """``<<`` inside shell arithmetic must not swallow trailing executable lines."""

    def test_arithmetic_shift_does_not_strip_trailing_nohup(self):
        cmd = "echo $((1 << 2))\nnohup ./worker &\n2"
        assert _strip_heredoc_bodies(cmd) == cmd

    def test_arithmetic_shift_does_not_strip_trailing_sleep_amp(self):
        cmd = "echo $((1 << 2))\nsleep 30 &\n2"
        assert _strip_heredoc_bodies(cmd) == cmd

    def test_nested_arithmetic_shifts_ignored(self):
        cmd = "echo $(( (1 << 2) << (2 + 1) ))"
        assert _strip_heredoc_bodies(cmd) == cmd

    def test_multiple_arithmetic_shifts_on_one_line(self):
        cmd = "echo $((1 << 2)) $((3 << 4))"
        assert _strip_heredoc_bodies(cmd) == cmd

    def test_arithmetic_command_form_retains_trailing_executable(self):
        cmd = "(( x = 1 << 2 ))\nnohup ./worker &"
        assert _strip_heredoc_bodies(cmd) == cmd

    def test_unmatched_dollar_arith_fail_closed(self):
        cmd = "echo $((1 << 2\nnohup ./worker &"
        assert _strip_heredoc_bodies(cmd) == cmd

    def test_cmd_sub_with_heredoc_fail_closed(self):
        cmd = "echo $(cat <<EOF\nx\nEOF)"
        assert _strip_heredoc_bodies(cmd) == cmd

    def test_real_heredoc_still_strips_body(self):
        cmd = "cat <<EOF\n<nohup-looking data>\nEOF"
        assert _strip_heredoc_bodies(cmd) == "cat <<EOF\nEOF"

    def test_arithmetic_then_real_heredoc_on_same_line(self):
        cmd = "echo $((1 << 2)); cat <<EOF\nbody\nEOF\nnohup x &"
        assert _strip_heredoc_bodies(cmd) == "echo $((1 << 2)); cat <<EOF\nEOF\nnohup x &"

    def test_real_heredoc_then_arithmetic_on_opener_line(self):
        cmd = "cat <<EOF; echo $((1 << 2))\nbody\nEOF\nnohup x &"
        assert _strip_heredoc_bodies(cmd) == "cat <<EOF; echo $((1 << 2))\nEOF\nnohup x &"

    def test_nested_dollar_arith_shift_does_not_strip_trailing_nohup(self):
        cmd = "echo $((1 + $((2 << 1)) << 3))\nnohup ./worker &\n3"
        stripped = _strip_heredoc_bodies(cmd)
        assert stripped == cmd
        assert "nohup ./worker &" in stripped

    def test_nested_dollar_arith_shift_does_not_strip_trailing_sleep_amp(self):
        cmd = "echo $((1 + $((2 << 1)) << 3))\nsleep 30 &\n3"
        assert _strip_heredoc_bodies(cmd) == cmd
        assert "sleep 30 &" in _strip_heredoc_bodies(cmd)

    def test_triple_nested_dollar_arith_retains_trailing_nohup(self):
        cmd = "echo $(( $(( $((1 << 2)) + 3 )) << 1 ))\nnohup ./worker &"
        stripped = _strip_heredoc_bodies(cmd)
        assert stripped == cmd
        assert "nohup ./worker &" in stripped

    def test_sibling_nested_dollar_arith_retains_trailing_nohup(self):
        cmd = "echo $(( $((1 << 2)) + $((3 << 4)) ))\nnohup ./worker &"
        stripped = _strip_heredoc_bodies(cmd)
        assert stripped == cmd
        assert "nohup ./worker &" in stripped

    def test_unmatched_inner_dollar_arith_fail_closed(self):
        cmd = "echo $((1 + $((2 << 1) << 3))\nnohup ./worker &\n3"
        stripped = _strip_heredoc_bodies(cmd)
        assert stripped == cmd
        assert "nohup ./worker &" in stripped

    def test_unmatched_outer_dollar_arith_extra_closer_fail_closed(self):
        cmd = "echo $((1 + $((2 << 1)) << 3)))\nnohup ./worker &"
        stripped = _strip_heredoc_bodies(cmd)
        assert stripped == cmd
        assert "nohup ./worker &" in stripped

    def test_unmatched_outer_dollar_arith_missing_closer_fail_closed(self):
        cmd = "echo $((1 + $((2 << 1)) << 3)\nnohup ./worker &"
        stripped = _strip_heredoc_bodies(cmd)
        assert stripped == cmd
        assert "nohup ./worker &" in stripped

    def test_nested_arith_then_real_heredoc_strips_fake_retains_real_nohup(self):
        cmd = (
            "echo $((1 + $((2 << 1)) << 3))\n"
            "cat <<EOF\n"
            "nohup fake\n"
            "EOF\n"
            "nohup ./worker &"
        )
        stripped = _strip_heredoc_bodies(cmd)
        assert stripped == (
            "echo $((1 + $((2 << 1)) << 3))\n"
            "cat <<EOF\n"
            "EOF\n"
            "nohup ./worker &"
        )
        assert "nohup fake" not in stripped
        assert "nohup ./worker &" in stripped

    def test_nested_arith_cmd_shift_retains_trailing_nohup(self):
        cmd = "(( x = 1 + (( y = 2 << 1 )) << 3 ))\nnohup ./worker &"
        stripped = _strip_heredoc_bodies(cmd)
        assert stripped == cmd
        assert "nohup ./worker &" in stripped


class TestHeredocArithmeticBackgroundGuidance:
    """End-to-end: arithmetic ``<<`` false positives must not hide background commands."""

    def test_nohup_after_arithmetic_shift_still_blocked(self):
        cmd = "echo $((1 << 2))\nnohup ./worker &\n2"
        msg = _foreground_background_guidance(cmd)
        assert msg is not None
        assert "WITHOUT the wrapper" in msg

    def test_sleep_amp_after_arithmetic_shift_still_blocked(self):
        cmd = "echo $((1 << 2))\nsleep 30 &\n2"
        msg = _foreground_background_guidance(cmd)
        assert msg is not None
        assert "WITHOUT the '&'" in msg

    def test_nohup_after_arithmetic_command_still_blocked(self):
        cmd = "(( x = 1 << 2 ))\nnohup ./worker &"
        msg = _foreground_background_guidance(cmd)
        assert msg is not None
        assert "WITHOUT the wrapper" in msg

    def test_real_heredoc_after_arithmetic_on_same_line_still_strips(self):
        cmd = "echo $((1 << 2)); cat <<EOF\nbody\nEOF\nnohup x &"
        msg = _foreground_background_guidance(cmd)
        assert msg is not None
        assert "WITHOUT the wrapper" in msg

    def test_nohup_after_nested_dollar_arith_shift_still_blocked(self):
        cmd = "echo $((1 + $((2 << 1)) << 3))\nnohup ./worker &\n3"
        msg = _foreground_background_guidance(cmd)
        assert msg is not None
        assert "WITHOUT the wrapper" in msg

    def test_sleep_amp_after_nested_dollar_arith_shift_still_blocked(self):
        cmd = "echo $((1 + $((2 << 1)) << 3))\nsleep 30 &\n3"
        msg = _foreground_background_guidance(cmd)
        assert msg is not None
        assert "WITHOUT the '&'" in msg

    def test_nohup_after_triple_nested_dollar_arith_still_blocked(self):
        cmd = "echo $(( $(( $((1 << 2)) + 3 )) << 1 ))\nnohup ./worker &"
        msg = _foreground_background_guidance(cmd)
        assert msg is not None
        assert "WITHOUT the wrapper" in msg

    def test_nohup_after_sibling_nested_dollar_arith_still_blocked(self):
        cmd = "echo $(( $((1 << 2)) + $((3 << 4)) ))\nnohup ./worker &"
        msg = _foreground_background_guidance(cmd)
        assert msg is not None
        assert "WITHOUT the wrapper" in msg

    def test_nohup_after_unmatched_inner_dollar_arith_still_blocked(self):
        cmd = "echo $((1 + $((2 << 1) << 3))\nnohup ./worker &\n3"
        msg = _foreground_background_guidance(cmd)
        assert msg is not None
        assert "WITHOUT the wrapper" in msg

    def test_nohup_after_unmatched_outer_extra_closer_still_blocked(self):
        cmd = "echo $((1 + $((2 << 1)) << 3)))\nnohup ./worker &"
        msg = _foreground_background_guidance(cmd)
        assert msg is not None
        assert "WITHOUT the wrapper" in msg

    def test_nohup_after_nested_arith_then_real_heredoc_still_blocked(self):
        cmd = (
            "echo $((1 + $((2 << 1)) << 3))\n"
            "cat <<EOF\n"
            "nohup fake\n"
            "EOF\n"
            "nohup ./worker &"
        )
        msg = _foreground_background_guidance(cmd)
        assert msg is not None
        assert "WITHOUT the wrapper" in msg

    def test_nohup_after_nested_arith_cmd_shift_still_blocked(self):
        cmd = "(( x = 1 + (( y = 2 << 1 )) << 3 ))\nnohup ./worker &"
        msg = _foreground_background_guidance(cmd)
        assert msg is not None
        assert "WITHOUT the wrapper" in msg
