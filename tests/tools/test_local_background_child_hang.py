"""Regression tests for issue #8340.

When a user command backgrounds a child process (``cmd &``, ``setsid cmd &
disown``, etc.), the backgrounded grandchild inherits the write-end of our
stdout pipe via fork().  Before the fix, the drain thread's blocking
``for line in proc.stdout`` would never see EOF until that grandchild
closed the pipe — causing the terminal tool to hang for the full lifetime
of the backgrounded service (indefinitely for a uvicorn server).

The fix switches ``_drain()`` to select()-based non-blocking reads and
stops draining shortly after bash exits even if the pipe hasn't EOF'd.

On Windows — where ``select()`` only works on sockets — the drain polls
``PeekNamedPipe`` (hermes_cli._subprocess_compat.windows_pipe_readable_bytes)
with the same stop-after-exit semantics, and ``_kill_process`` takes the
whole process tree (``taskkill /T /F``) instead of ``proc.terminate()``,
so a timed-out foreground child can't outlive the kill holding the
inherited pipe write-end.  This file guards both mechanisms and runs on
every platform.
"""
import os
import subprocess
import time

import pytest

from tools.environments.local import LocalEnvironment

def _pkill(pattern: str) -> None:
    """Best-effort kill of every process whose command line contains *pattern*."""
    if os.name == "nt":
        # No pkill on Windows; match command lines via CIM.  The PowerShell
        # process's own command line contains *pattern* — exclude it by PID.
        ps = (
            "Get-CimInstance Win32_Process | Where-Object "
            f"{{ $_.ProcessId -ne $PID -and $_.CommandLine -like '*{pattern}*' }}"
            " | ForEach-Object "
            "{ Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }"
        )
        subprocess.run(["powershell", "-NoProfile", "-Command", ps], capture_output=True)
        return
    subprocess.run(f"pkill -9 -f {pattern!r} 2>/dev/null", shell=True)


def _proc_running(pattern: str) -> bool:
    """True when any live process's command line contains *pattern*.

    POSIX uses argv-form ``pgrep -f`` (no ``shell=True`` — the ``sh -c``
    wrapper's own command line would contain *pattern* and always match).
    Windows matches CIM command lines, excluding the probe's own PID.
    """
    if os.name == "nt":
        ps = (
            "Get-CimInstance Win32_Process | Where-Object "
            f"{{ $_.ProcessId -ne $PID -and $_.CommandLine -like '*{pattern}*' }}"
            " | Select-Object -First 1 -ExpandProperty ProcessId"
        )
        res = subprocess.run(
            ["powershell", "-NoProfile", "-Command", ps],
            capture_output=True,
            text=True,
        )
        return bool(res.stdout.strip())
    res = subprocess.run(["pgrep", "-f", pattern], capture_output=True)
    return res.returncode == 0


@pytest.fixture
def local_env():
    env = LocalEnvironment(cwd="/tmp")
    try:
        yield env
    finally:
        env.cleanup()


class TestBackgroundChildDoesNotHang:
    """Regression guard for issue #8340."""

    def test_plain_background_returns_promptly(self, local_env):
        """``cmd &`` with no output redirection must not hang on pipe inherit."""
        marker = "hermes_8340_plain_bg"
        cmd = f'python3 -c "import time; time.sleep(60)" & echo {marker}'
        try:
            t0 = time.monotonic()
            result = local_env.execute(cmd, timeout=15)
            elapsed = time.monotonic() - t0

            assert elapsed < 10.0, (  # hang under guard is 15s+; loose bound rides out runner stalls
                f"terminal_tool hung for {elapsed:.1f}s — drain thread "
                f"is still blocking on backgrounded child's inherited pipe fd"
            )
            assert result["returncode"] == 0
            assert marker in result["output"]
        finally:
            _pkill("time.sleep(60)")

    def test_setsid_disown_pattern_returns_promptly(self, local_env):
        """The exact pattern from the issue: setsid ... & disown."""
        cmd = (
            'setsid python3 -c "import time; time.sleep(60)" '
            '> /dev/null 2>&1 < /dev/null & disown; echo started'
        )
        try:
            t0 = time.monotonic()
            result = local_env.execute(cmd, timeout=15)
            elapsed = time.monotonic() - t0

            assert elapsed < 10.0, f"setsid+disown path hung for {elapsed:.1f}s"
            assert result["returncode"] == 0
            assert "started" in result["output"]
        finally:
            _pkill("time.sleep(60)")

    def test_foreground_streaming_output_still_captured(self, local_env):
        """Sanity: incremental output over time must still be captured in full."""
        cmd = 'for i in 1 2 3; do echo "tick $i"; sleep 0.2; done; echo done'
        t0 = time.monotonic()
        result = local_env.execute(cmd, timeout=10)
        elapsed = time.monotonic() - t0

        # Loop body sleeps ~0.6s total — elapsed should be close to that.
        assert 0.5 < elapsed < 10.0
        assert result["returncode"] == 0
        for expected in ("tick 1", "tick 2", "tick 3", "done"):
            assert expected in result["output"], f"missing {expected!r}"

    def test_high_volume_output_complete(self, local_env):
        """Sanity: select-based drain must not drop lines under load."""
        result = local_env.execute("seq 1 3000", timeout=10)
        lines = result["output"].strip().split("\n")
        assert result["returncode"] == 0
        assert len(lines) == 3000
        assert lines[0] == "1"
        assert lines[-1] == "3000"

    def test_foreground_capture_is_bounded_while_draining(
        self, local_env, monkeypatch
    ):
        monkeypatch.setattr("tools.tool_output_limits.get_max_bytes", lambda: 10_000)
        command = (
            "python3 -c \"import sys; "
            "sys.stdout.write('HEAD-SENTINEL\\n' + 'x' * 2000000 + "
            "'\\nTAIL-SENTINEL')\""
        )

        result = local_env.execute(command, timeout=10, bounded_capture=True)

        assert result["returncode"] == 0
        assert len(result["output"]) <= 10_000
        assert result["output"].startswith("HEAD-SENTINEL")
        assert result["output"].endswith("TAIL-SENTINEL")
        assert "[OUTPUT TRUNCATED" in result["output"]

    def test_default_capture_is_full_fidelity_for_internal_consumers(
        self, local_env
    ):
        """Default execute() (no bounded_capture) must return complete output.

        Internal consumers — file-operation ``cat`` reads that feed the patch
        engine, code-execution RPC reads, log reads — rely on full-fidelity
        capture. Bounding them at tool_output.max_bytes would CORRUPT files
        on read-modify-write (#64435 review finding), so only the foreground
        terminal tool opts in via bounded_capture=True.
        """
        # ~200 KB — four times the default 50 KB cap.
        command = (
            "python3 -c \"import sys; "
            "sys.stdout.write('START-MARK\\n' + ('y' * 200000) + '\\nEND-MARK')\""
        )

        result = local_env.execute(command, timeout=10)

        assert result["returncode"] == 0
        assert "[OUTPUT TRUNCATED" not in result["output"]
        assert result["output"].startswith("START-MARK")
        assert result["output"].endswith("END-MARK")
        assert len(result["output"]) > 200000

    def test_continuous_output_still_honors_foreground_timeout(
        self, local_env, monkeypatch
    ):
        monkeypatch.setattr("tools.tool_output_limits.get_max_bytes", lambda: 5_000)
        command = (
            "python3 -c \"import sys; "
            "chunk = 'x' * 4096; "
            "exec('while True: sys.stdout.write(chunk); sys.stdout.flush()')\""
        )

        started = time.monotonic()
        result = local_env.execute(command, timeout=1, bounded_capture=True)
        elapsed = time.monotonic() - started

        assert elapsed < 10.0
        assert result["returncode"] == 124
        assert len(result["output"]) <= 5_000
        assert "[OUTPUT TRUNCATED" in result["output"]
        assert result["output"].endswith("[Command timed out after 1s]")

    def test_timeout_path_still_works(self, local_env):
        """Foreground command exceeding timeout must still be killed."""
        t0 = time.monotonic()
        result = local_env.execute("sleep 30", timeout=2)
        elapsed = time.monotonic() - t0

        assert elapsed < 10.0
        assert result["returncode"] == 124
        assert "timed out" in result["output"].lower()

    def test_timeout_kill_does_not_orphan_children(self, local_env):
        """A timed-out foreground child must die with bash (tree kill).

        On POSIX, ``_kill_process`` signals the whole ``setsid`` process
        group, so bash's children have always died with it.  On Windows,
        ``proc.terminate()`` is ``TerminateProcess`` on bash.exe alone —
        the foreground child survived as an orphan, kept running the very
        workload the timeout was meant to stop, and held the inherited
        stdout-pipe write-end open.  ``_kill_process`` must take the whole
        tree (``taskkill /T /F``) instead.
        """
        marker = "hermes_8340_tree_kill"
        cmd = f'python3 -c "import time; time.sleep(45); {marker} = 1"'
        try:
            t0 = time.monotonic()
            result = local_env.execute(cmd, timeout=2)
            elapsed = time.monotonic() - t0

            assert elapsed < 6.0
            assert result["returncode"] == 124
            # Give the kill a short window to settle, then require the
            # whole tree dead — no survivor may outlive execute().
            deadline = time.monotonic() + 5.0
            while time.monotonic() < deadline and _proc_running(marker):
                time.sleep(0.25)
            assert not _proc_running(marker), (
                "foreground child survived the timeout kill — "
                "_kill_process did not take the process tree"
            )
        finally:
            _pkill(marker)

    def test_utf8_output_decoded_correctly(self, local_env):
        """Multibyte UTF-8 chunks must decode cleanly under select-based reads."""
        result = local_env.execute("echo 日本語 café résumé", timeout=30)
        assert result["returncode"] == 0
        assert "日本語" in result["output"]
        assert "café" in result["output"]
        assert "résumé" in result["output"]

    def test_utf8_multibyte_across_read_boundary(self, local_env):
        """Multibyte UTF-8 characters straddling a 4096-byte ``os.read()`` boundary
        must be decoded correctly via the incremental decoder — not lost to a
        ``UnicodeDecodeError`` fallback.  Regression for a bug in the first draft
        of the fix where a strict ``bytes.decode('utf-8')`` on each raw chunk
        wiped the entire buffer as soon as any chunk split a multi-byte char.
        """
        # 10000 "日" chars = 30000 bytes — guaranteed to cross multiple 4096
        # read boundaries, and most boundaries will land in the middle of the
        # 3-byte UTF-8 encoding of U+65E5.
        cmd = (
            'python3 -c \'import sys; '
            'sys.stdout.buffer.write(chr(0x65e5).encode("utf-8") * 10000); '
            'sys.stdout.buffer.write(b"\\n")\''
        )
        result = local_env.execute(cmd, timeout=10)
        assert result["returncode"] == 0
        # All 10000 characters must survive the round-trip
        assert result["output"].count("\u65e5") == 10000, (
            f"lost multibyte chars across read boundaries: got "
            f"{result['output'].count(chr(0x65e5))} / 10000"
        )
        # And the "[binary output detected ...]" fallback must NOT fire
        assert "binary output detected" not in result["output"]

    def test_invalid_utf8_uses_replacement_not_fallback(self, local_env):
        """Truly invalid byte sequences must be substituted with U+FFFD (matching
        the pre-fix ``errors='replace'`` behaviour of the old ``TextIOWrapper``
        drain), not clobber the entire buffer with a fallback placeholder.
        """
        # Write a deliberate invalid UTF-8 lead byte sandwiched between valid ASCII
        cmd = (
            'python3 -c \'import sys; '
            'sys.stdout.buffer.write(b"before "); '
            'sys.stdout.buffer.write(b"\\xff\\xfe"); '
            'sys.stdout.buffer.write(b" after\\n")\''
        )
        result = local_env.execute(cmd, timeout=15)
        assert result["returncode"] == 0
        assert "before" in result["output"]
        assert "after" in result["output"]
        assert "binary output detected" not in result["output"]
