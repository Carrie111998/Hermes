"""Regression: the Windows Desktop update hand-off must not wait on pipe EOF.

``scripts/desktop-update/windows.ps1`` runs each update step through
``Invoke-HermesStep``, which starts the step with ``RedirectStandardOutput`` /
``RedirectStandardError`` and used to collect its output with
``ReadToEndAsync().Result``.

``.Result`` on that task does not return when the *step* exits -- it returns
when the *pipe* reaches EOF. On Windows the write end of a redirected pipe is
handed to the child as an inheritable handle, so every descendant spawned
without its own redirection holds a duplicate, and EOF waits for the last of
them to close it. ``hermes update`` deliberately runs build steps with stdout
inherited (the tee-stderr runner in ``hermes_cli/main.py``), so the process
tree under a step is arbitrarily deep and not something the hand-off can
enumerate. When one of those descendants is a resident gateway, the pipe never
closes and ``Invoke-HermesStep`` blocks for the life of the gateway.

Everything the hand-off owes the Desktop is downstream of that call:
``.hermes-update-result.json`` is never written, ``.hermes-update-in-progress``
is never cleared, and the Desktop is never relaunched -- so the app sits on
"Updating Hermes" until the user kills the gateway by hand, and the stale
marker then refuses the next update too (#90455).

The fix drains both pipes in chunks into a ``StringBuilder`` and bounds the
drain *after* the step has exited. The bound cannot truncate a slow step: the
clock only starts once the process is gone, at which point everything it wrote
is already sitting in the pipe buffer waiting to be read.

The source-level guards below run on every host, because Linux CI cannot
execute the PowerShell hand-off -- the same reason
``test_desktop_update_windows_python_handoff.py`` is written that way. The
``windows_only`` test at the bottom is the executable proof: it drives the
script's own ``-SelfTestPipeDrain`` fixture, which builds a step whose
grandchild outlives it holding the inherited handle.
"""

from __future__ import annotations

import os
import re
import subprocess
from pathlib import Path

import pytest


REPO_ROOT = Path(__file__).resolve().parent.parent
WINDOWS_PS1 = REPO_ROOT / "scripts" / "desktop-update" / "windows.ps1"


def _read() -> str:
    return WINDOWS_PS1.read_text(encoding="utf-8")


def _invoke_hermes_step_body() -> str:
    """Return just the body of ``Invoke-HermesStep``.

    Scoped deliberately: ``WaitForExit`` and ``.Result`` are legitimate
    elsewhere in the script, so a whole-file grep would fire on unrelated
    code.
    """
    source = _read()
    start = source.find("function Invoke-HermesStep(")
    assert start != -1, (
        "Invoke-HermesStep no longer exists in "
        "scripts/desktop-update/windows.ps1. The update hand-off structure "
        "changed -- update this guard rather than deleting it."
    )
    end = source.find("\n}", start)
    assert end != -1, "Could not find the end of Invoke-HermesStep."
    return source[start:end]


def test_step_drain_does_not_wait_for_pipe_eof() -> None:
    body = _invoke_hermes_step_body()

    assert "ReadToEndAsync" not in body, (
        "Invoke-HermesStep must not collect step output with "
        "ReadToEndAsync(): that task completes on pipe EOF, not on process "
        "exit, so any surviving descendant holding the inherited write handle "
        "(a resident gateway is the reported case) blocks the hand-off "
        "forever. .Result also cannot hand back a partial read, so there is "
        "no way to abandon it without losing the whole step's output. Read in "
        "chunks into a StringBuilder instead (#90455)."
    )


def test_step_drain_is_bounded_once_the_step_has_exited() -> None:
    body = _invoke_hermes_step_body()

    assert "$script:StepDrainGraceSeconds" in body, (
        "Invoke-HermesStep must bound how long it waits for the step's pipes "
        "to reach EOF after the step process itself has exited (#90455)."
    )
    assert "HasExited" in body, (
        "The drain bound must be keyed on the step having exited, so a slow "
        "step is never mistaken for a stuck pipe."
    )
    assert re.search(r"\$abandonAt\s*=\s*\(Get-Date\)\.AddSeconds", body), (
        "The abandon deadline must be armed from the moment the step exits, "
        "not from when it started -- a long `uv pip install` is not a leaked "
        "pipe (#90455)."
    )


def test_step_drain_does_not_use_the_stream_waiting_waitforexit() -> None:
    body = _invoke_hermes_step_body()

    assert not re.search(r"WaitForExit\(\s*\)", body), (
        "The parameterless Process.WaitForExit() also waits on redirected "
        "streams, which reintroduces exactly the unbounded pipe-EOF wait this "
        "guard exists to prevent. Use the bounded WaitForExit(ms) overload "
        "(#90455)."
    )


def test_abandoned_drain_is_reported_in_the_hand_off_log() -> None:
    body = _invoke_hermes_step_body()

    assert "pipe drain abandoned" in body, (
        "Abandoning a step's pipes must leave a line in "
        "logs/desktop-update-handoff.log. A silently truncated step log is "
        "indistinguishable from a step that printed nothing, and that log is "
        "what `hermes debug share` collects for update reports (#90455)."
    )


@pytest.mark.windows_only
def test_pipe_drain_self_test_returns_while_a_descendant_holds_the_pipe(
    tmp_path: Path,
) -> None:
    """Execute the real drain against a step that leaks its pipe handle.

    ``-SelfTestPipeDrain`` starts a step which spawns a grandchild with
    ``UseShellExecute = $false`` and no redirection -- the shape that makes the
    grandchild inherit the step's stdout/stderr -- then exits 7 while the
    grandchild sleeps on. The fixture asserts the grandchild was still alive
    when ``Invoke-HermesStep`` returned, so a pass cannot be a timing
    coincidence, and that both the exit code and the step's output survived the
    abandonment.

    Measured on Windows 11 / PowerShell 5.1: 4.3s with the fix, 47.4s (the
    grandchild's full lifetime) with the previous drain restored.
    """
    system_root = Path(os.environ.get("SystemRoot", r"C:\Windows"))
    powershell = (
        system_root / "System32" / "WindowsPowerShell" / "v1.0" / "powershell.exe"
    )
    if not powershell.is_file():
        pytest.skip(f"Windows PowerShell not found at {powershell}")

    env = {
        **os.environ,
        # The fixture writes its child script, pid file and hand-off log under
        # TEMP; point that at tmp_path so the test leaves nothing behind.
        "TEMP": str(tmp_path),
        "TMP": str(tmp_path),
        # Keep the test quick. The grace is what the fix bounds; the hold is
        # how long the leaking grandchild lives. hold >> grace is what makes a
        # regression measurable rather than lucky.
        "HERMES_UPDATE_PIPE_DRAIN_SECONDS": "3",
        "HERMES_SELFTEST_HOLD_SECONDS": "45",
    }

    result = subprocess.run(
        [
            str(powershell),
            "-NoProfile",
            "-ExecutionPolicy",
            "Bypass",
            "-File",
            str(WINDOWS_PS1),
            "-SelfTestPipeDrain",
        ],
        capture_output=True,
        text=True,
        # Comfortably past the 45s hold so a regression fails with the
        # fixture's own diagnosis instead of an opaque timeout.
        timeout=180,
        env=env,
        cwd=str(REPO_ROOT),
    )

    assert "PIPE-DRAIN SELF-TEST: PASS" in result.stdout, (
        "Invoke-HermesStep did not return while a descendant still held its "
        "stdout/stderr pipe. The Desktop update hand-off is deadlocked again "
        f"(#90455).\n--- stdout ---\n{result.stdout}\n"
        f"--- stderr ---\n{result.stderr}"
    )
    assert result.returncode == 0, (
        f"-SelfTestPipeDrain exited {result.returncode}.\n"
        f"--- stdout ---\n{result.stdout}\n--- stderr ---\n{result.stderr}"
    )
