"""Regression tests for #98222: a statement following the background `&`
(`A && B & C`) must survive the brace-group rewrite as valid bash.

Context: the rewriter turned `A && B &` into `A && { B & }` — correct when
the `&` ends the list, but the source `&` was also the *terminator* for
whatever followed it. `A && { B & } echo x` is a syntax error, so the
remote-kernel spawn template (`... & echo PID:$!`) died on every Docker /
SSH / Modal `execute_code` call. The fix emits `A && { B & } ; echo x`.
"""

import shutil
import subprocess

import pytest

from tools.terminal_tool import _rewrite_compound_background as rewrite

_BASH = shutil.which("bash")


def _valid_bash(cmd: str) -> bool:
    return subprocess.run(
        ["bash", "-n", "-c", cmd], capture_output=True, text=True
    ).returncode == 0


needs_bash = pytest.mark.skipif(_BASH is None, reason="bash not available")


class TestStatementAfterBackground:
    """`A && B & C` — the `&` terminated BOTH the job and the list."""

    def test_kernel_spawn_template_stays_valid(self):
        cmd = (
            "cd /tmp/x && nohup env A=B python3 kernel_runner.py "
            "> /tmp/x/runner.log 2>&1 & echo PID:$!"
        )
        out = rewrite(cmd)
        assert "PID:$!" in out
        if _BASH:
            assert _valid_bash(out), out

    def test_echo_after_amp_gets_separator(self):
        assert rewrite("A && B & echo done") == "A && { B & } ; echo done"

    def test_or_chain(self):
        assert rewrite("A || B & echo done") == "A || { B & } ; echo done"

    def test_statement_with_chain_after_amp(self):
        assert rewrite("A && B & C && D") == "A && { B & } ; C && D"

    def test_pid_output_after_fix_is_numeric(self):
        # Real runtime: the echo must print the backgrounded job's PID.
        out = rewrite(
            "true && sleep 45 >/dev/null 2>&1 & echo GOTPID:$!"
        )
        r = subprocess.run(
            ["bash", "-c", out], capture_output=True, text=True, timeout=15
        )
        subprocess.run(
            ["bash", "-c", "pkill -f 'sleep 45' 2>/dev/null; true"], timeout=10
        )
        token = next((t for t in r.stdout.split() if t.startswith("GOTPID:")), "")
        assert token[7:].isdigit(), f"rc={r.returncode} out={r.stdout!r} err={r.stderr!r}"


class TestEndOfListUnchanged:
    """When nothing follows the `&`, the old output form must not change."""

    def test_end_of_string(self):
        assert rewrite("A && B &") == "A && { B & }"

    def test_newline_after_amp(self):
        assert rewrite("A && B &\nnext") == "A && { B & }\nnext"

    def test_multiline_mixed(self):
        cmd = "A && B & echo x\nC && D &"
        assert rewrite(cmd) == "A && { B & } ; echo x\nC && { D & }"


@needs_bash
class TestRuntimeSemantics:
    def test_background_does_not_block_shell(self):
        # The original subshell-wait bug class must stay fixed.
        out = rewrite("true && sleep 30 >/dev/null 2>&1 &")
        subprocess.run(["bash", "-c", out], capture_output=True, timeout=8)
        subprocess.run(
            ["bash", "-c", "pkill -f 'sleep 30' 2>/dev/null; true"], timeout=10
        )

    def test_and_semantics_preserved_with_trailing_stmt(self):
        # A fails -> B never starts, but the trailing echo still runs.
        out = rewrite("false && sleep 97 >/dev/null 2>&1 & echo SKIPPED")
        r = subprocess.run(
            ["bash", "-c", out], capture_output=True, text=True, timeout=10
        )
        chk = subprocess.run(
            ["bash", "-c", "pgrep -f 'sleep 97' || true"],
            capture_output=True, text=True, timeout=10,
        )
        assert r.stdout.strip() == "SKIPPED"
        assert chk.stdout.strip() == ""


class TestIdempotence:
    def test_rewriter_output_is_fixed_point(self):
        once = rewrite("A && B & echo x")
        assert rewrite(once) == once
