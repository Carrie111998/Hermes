"""A test process must not leave a gateway-resume atexit hook armed.

`_cmd_update_impl` registers `_resume_windows_gateways_after_update` with
`atexit` as a safety net, so an update that dies partway still restores the
gateway it paused. Correct in production; hazardous in a test process.

`atexit` hooks fire at INTERPRETER SHUTDOWN -- after pytest has torn every
`monkeypatch` down and restored the real `gateway_windows._spawn_detached`. A
test that exercises the update path therefore arms a hook that, at shutdown,
cold-starts a REAL detached gateway on the live box.

Observed 2026-08-18: a `pytest tests/hermes_cli/` run printed
"Starting Windows gateway after update (PID 43828)" AFTER its own summary line.
43828 was real -- gateway-exit-diag.log recorded its spawn with
`site='update:windows-cold-start'` and a parent_chain naming that pytest
process. It lost the double-run race 7s later and the watchdog spawned a
replacement: the test suite restarted production.

The guard is the autouse `_unarm_gateway_resume_atexit_hook` fixture in
conftest.py. This module tests the mechanism end to end in a SUBPROCESS,
because `atexit` exposes no public way to ask "is this armed?" -- and
`atexit._ncallbacks()` is not usable as a probe: it does not decrement on
`unregister`, though `unregister` itself works correctly.
"""

from __future__ import annotations

import subprocess
import sys
import textwrap

# Arms the hook exactly as `_cmd_update_impl` does, with a cold-start token --
# the token shape that makes the resume path spawn a brand-new gateway rather
# than relaunch a previously-running one.
_ARM = """
import atexit
import hermes_cli.main as cli_main

fired = []
cli_main._cold_start_windows_gateway_after_update = lambda: fired.append("SPAWNED")
# atexit is LIFO: register the reporter FIRST so it runs LAST, after the hook
# under test has had its chance to fire.
atexit.register(lambda: print("HOOK_FIRED" if fired else "HOOK_DID_NOT_SPAWN"))
atexit.register(
    cli_main._resume_windows_gateways_after_update,
    {"resume_needed": True, "profiles": {}, "unmapped": [], "cold_start_if_installed": True},
)
"""

_UNARM = """
atexit.unregister(cli_main._resume_windows_gateways_after_update)
"""


def _run(body: str) -> str:
    proc = subprocess.run(
        [sys.executable, "-c", textwrap.dedent(body)],
        capture_output=True,
        text=True,
        timeout=180,
    )
    return proc.stdout


def test_an_armed_hook_really_does_reach_the_cold_start_path():
    """Establishes the hazard is real -- otherwise the guard tests prove nothing."""
    out = _run(_ARM)

    assert "HOOK_FIRED" in out, (
        "expected the armed atexit hook to reach the cold-start path at shutdown; "
        f"got: {out!r}"
    )


def test_unregistering_the_hook_prevents_the_cold_start():
    """What the conftest autouse fixture does after every test."""
    out = _run(_ARM + _UNARM)

    assert "HOOK_DID_NOT_SPAWN" in out, (
        "unregister failed to disarm the resume hook; a leaked hook would "
        f"cold-start a real gateway at interpreter exit. got: {out!r}"
    )
    assert "HOOK_FIRED" not in out
