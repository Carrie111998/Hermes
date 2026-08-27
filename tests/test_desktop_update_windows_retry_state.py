"""Regression: a completed update must never be re-run by the timeout retry.

#96205: on native Windows, ``hermes update`` runs to completion (its output
shows "✓ Update complete!") but the step process does not exit cleanly — the
post-update finalization (gateway-restart hand-off) stays alive and silent
until the stall watchdog in ``Invoke-HermesStep`` terminates the tree with
the timeout sentinel 124. The old retry gate re-ran the update on ANY
non-zero, non-2 exit code, so the completed update was re-applied from
scratch ("first attempt failed; retrying once (freshly pulled fix loads on
the second run)") and the Desktop popup hung for 70+ minutes.

The fix is a retry state machine (``Get-HermesUpdateRetryState`` +
``Invoke-HermesUpdateWithRetry`` in ``scripts/desktop-update/windows.ps1``):
the completion marker ("Update complete!") is a TERMINAL state. A step that
printed it is done — the install state is updated — so it is surfaced as
success (exit 0) instead of being re-run, whatever exit code the watchdog
produced afterwards. ``scripts/desktop-update/posix.sh`` carries the same
gate shape for the sibling hand-off.

These are source-contract assertions, because Linux CI cannot execute the
PowerShell hand-off; the executable proof is the ``-SelfTestRetryState`` arm
of ``windows.ps1`` (``windows_only`` below), which drives the real gate with
fixture steps and asserts a completed update is invoked exactly once.
Sabotage-proof: removing the completion branch, reordering the gate so a
completed update reaches the retry, or dropping the self-test arm each fails
a test here.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest


REPO_ROOT = Path(__file__).resolve().parent.parent
WINDOWS_PS1 = REPO_ROOT / "scripts" / "desktop-update" / "windows.ps1"
POSIX_SH = REPO_ROOT / "scripts" / "desktop-update" / "posix.sh"


class TestRetryStateMachineSkipsCompletedUpdate:
    """The windows.ps1 retry gate must treat a completed update as terminal.

    The update printed "✓ Update complete!" and was THEN killed with 124 by
    the stall watchdog while finalizing (gateway-restart hand-off). The
    install state is done; re-running it is the #96205 retry storm.
    """

    def _src(self) -> str:
        return WINDOWS_PS1.read_text(encoding="utf-8")

    def test_state_machine_function_defines_completed_state(self):
        src = self._src()
        assert "function Get-HermesUpdateRetryState" in src, (
            "the update retry decision is no longer a state machine"
        )
        # The completion marker must map to a terminal 'completed' state.
        # A removed or inert completion branch fails here.
        assert "$Output -match 'Update complete!'" in src, (
            "the state machine no longer detects the update completion marker"
        )
        assert "return 'completed'" in src, (
            "the completion marker no longer yields the terminal 'completed' state"
        )

    def test_only_retryable_state_reaches_the_retry(self):
        src = self._src()
        # The retry ("first attempt failed; retrying once") must be guarded
        # by the retryable-state check, which in turn must come AFTER the
        # completion branch in the gate so a completed update never retries.
        msg = (
            "the gate retries outside the 'retryable' state -- a completed "
            "update can be re-run (the #96205 retry storm)"
        )
        assert "if ($state -eq 'retryable')" in src, msg
        retryable = src.index("if ($state -eq 'retryable')")
        assert "first attempt failed; retrying once" in src[retryable:], msg
        completed = src.index("if ($state -eq 'completed')")
        # Same function: the retryable branch is handled before the completed
        # branch, and the completed branch must not contain the retry.
        assert completed > retryable, "the retry branch must precede the completed branch"
        assert "first attempt failed; retrying once" not in src[completed:], msg

    def test_completed_timeout_surfaces_as_success(self):
        src = self._src()
        # A completed update must be reported as exit 0 so the hand-off
        # relaunches the NEW build instead of failing (and instead of
        # re-running the update).
        completed = src.index("if ($state -eq 'completed')")
        tail = src[completed:]
        assert "Code = 0" in tail, (
            "a completed-after-timeout update is not surfaced as success"
        )
        # The captured output must be preserved so the truthful-completion
        # check below still sees a "Desktop build failed" warning.
        assert "Output = $res.Output" in tail, (
            "the completed branch drops the step output"
        )

    def test_main_flow_runs_the_update_through_the_state_machine(self):
        src = self._src()
        assert "Invoke-HermesUpdateWithRetry" in src, (
            "the main update flow no longer goes through the retry state machine"
        )
        # The main flow's own retry gate (the pre-fix shape) must be gone:
        # the gate now lives in the state machine function.
        assert "if ($res.Code -ne 0 -and $res.Code -ne 2) {" not in src, (
            "the old any-nonzero retry gate is still in the main flow"
        )

    def test_self_test_has_retry_state_arm(self):
        src = self._src()
        assert "[switch]$SelfTestRetryState" in src, (
            "-SelfTestRetryState lost its switch: the executable regression "
            "can no longer drive the gate"
        )
        assert "RETRY-STATE SELF-TEST: PASS" in src, (
            "the self-test no longer has a pass line for the retry state machine"
        )


class TestPosixRetryGateSkipsCompletedUpdate:
    """The posix.sh sibling must carry the same gate shape (#96205 class fix)."""

    def test_completion_check_guards_the_posix_retry(self):
        src = POSIX_SH.read_text(encoding="utf-8")
        msg = (
            "the posix retry gate no longer treats a completed update as "
            "terminal -- the #96205 retry storm can re-run posix updates too"
        )
        assert 'grep -q "Update complete!"' in src, msg
        gate = src.index('grep -q "Update complete!"')
        assert "retrying once" in src[gate:], msg
        # The completion check must sit inside the non-zero retry gate,
        # upstream of the retry itself.
        assert 'if [ "$CODE" -ne 0 ]' in src[:gate], msg


@pytest.mark.windows_only
def test_completed_update_is_invoked_exactly_once_after_timeout(
    tmp_path: Path,
) -> None:
    """Execute the real retry gate against fixture steps.

    ``-SelfTestRetryState`` drives ``Invoke-HermesUpdateWithRetry`` with a
    step that "completes" (prints the marker) and is then killed with the
    timeout sentinel 124 — the exact #96205 shape. The step must be invoked
    exactly once and the result must surface as exit 0. A genuine mid-update
    timeout keeps its single retry (2 invocations), and fail-closed exit 2 is
    not retried either.
    """
    system_root = Path(os.environ.get("SystemRoot", r"C:\Windows"))
    powershell = (
        system_root / "System32" / "WindowsPowerShell" / "v1.0" / "powershell.exe"
    )
    if not powershell.is_file():
        pytest.skip(f"Windows PowerShell not found at {powershell}")

    env = {
        **os.environ,
        # The fixture writes its hand-off log under TEMP; point that at
        # tmp_path so the test leaves nothing behind.
        "TEMP": str(tmp_path),
        "TMP": str(tmp_path),
    }

    result = subprocess.run(
        [
            str(powershell),
            "-NoProfile",
            "-ExecutionPolicy",
            "Bypass",
            "-File",
            str(WINDOWS_PS1),
            "-SelfTestRetryState",
        ],
        capture_output=True,
        text=True,
        timeout=120,
        env=env,
        cwd=str(REPO_ROOT),
    )

    assert "RETRY-STATE SELF-TEST: PASS" in result.stdout, (
        "The update retry state machine regressed: a completed update is "
        "being re-run after a timeout (the #96205 retry storm that hangs the "
        "Desktop popup for 70+ minutes), or a state maps to the wrong "
        f"outcome. Fixture diagnosis follows.\n--- stdout ---\n{result.stdout}\n"
        f"--- stderr ---\n{result.stderr}"
    )
    assert result.returncode == 0, (
        f"-SelfTestRetryState exited {result.returncode}.\n"
        f"--- stdout ---\n{result.stdout}\n--- stderr ---\n{result.stderr}"
    )
