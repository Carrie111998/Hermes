"""Timeout-safety tests for the gbrain CLI shell-outs.

These cover ``hermes_cli._subprocess_compat.run_text_capture`` — the helper the
honcho bridge uses for ``gbrain`` calls — with a focus on the Windows hang it
exists to prevent: ``subprocess.run(capture_output=True, timeout=N)`` does NOT
reliably time out when the spawned child spawns a *grandchild* that inherits the
capture pipe handles. The grandchild keeps the pipe's write end open, so the
reader-thread drain never reaches EOF and the caller blocks forever. The helper
tree-kills the whole process group on timeout so every write end closes.
"""

import subprocess
import sys
import time

import pytest

from hermes_cli._subprocess_compat import run_text_capture


def test_run_text_capture_returns_stdout_returncode_and_text():
    r = run_text_capture(
        [sys.executable, "-c",
         "import sys; sys.stdout.write('hello'); sys.stderr.write('oops'); sys.exit(3)"],
        timeout=30,
    )
    assert isinstance(r, subprocess.CompletedProcess)
    assert r.returncode == 3
    assert r.stdout == "hello"
    assert r.stderr == "oops"


@pytest.mark.timeout(90)  # backstop only; a correct impl returns in ~timeout seconds
def test_run_text_capture_times_out_when_grandchild_holds_pipe(tmp_path):
    """The reported Windows hang: a wedged child spawns a grandchild that
    INHERITS the capture pipes and lingers. Without a tree-kill the caller
    blocks far past ``timeout`` (waiting out the child / draining a pipe whose
    write end the grandchild still holds) instead of aborting at ``timeout``.
    """
    wedged = tmp_path / "wedged_gbrain.py"
    wedged.write_text(
        "import subprocess, sys, time\n"
        # Grandchild inherits our stdout/stderr (the captured pipe) and lingers,
        # holding the pipe's write end open so it never reaches EOF.
        "subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)'])\n"
        # The CLI itself never returns (gbrain backend down -> wedged).
        "time.sleep(60)\n",
        encoding="utf-8",
    )

    start = time.monotonic()
    with pytest.raises(subprocess.TimeoutExpired):
        run_text_capture([sys.executable, str(wedged)], timeout=3)
    elapsed = time.monotonic() - start

    # Must abort near `timeout`, NOT wait out the 60s child/grandchild sleeps.
    assert elapsed < 30, f"raised TimeoutExpired but took {elapsed:.1f}s — process tree not killed"
