"""Tests for subprocess.run() timeout coverage in CLI utilities."""
import ast
from pathlib import Path

import pytest


# Parameterise over every CLI module that calls subprocess.run
_CLI_MODULES = [
    "hermes_cli/doctor.py",
    "hermes_cli/status.py",
    "hermes_cli/clipboard.py",
    "hermes_cli/banner.py",
]


def _subprocess_run_calls(filepath: str) -> list[dict]:
    """Parse a Python file and return info about subprocess.run() calls."""
    source = Path(filepath).read_text()
    tree = ast.parse(source, filename=filepath)
    calls = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        if (isinstance(func, ast.Attribute) and func.attr == "run"
                and isinstance(func.value, ast.Name)
                and func.value.id == "subprocess"):
            has_timeout = any(kw.arg == "timeout" for kw in node.keywords)
            calls.append({"line": node.lineno, "has_timeout": has_timeout})
    return calls


@pytest.mark.parametrize("filepath", _CLI_MODULES)
def test_all_subprocess_run_calls_have_timeout(filepath):
    """Every subprocess.run() call in CLI modules must specify a timeout."""
    if not Path(filepath).exists():
        pytest.skip(f"{filepath} not found")
    calls = _subprocess_run_calls(filepath)
    missing = [c for c in calls if not c["has_timeout"]]
    assert not missing, (
        f"{filepath} has subprocess.run() without timeout at "
        f"line(s): {[c['line'] for c in missing]}"
    )


# =========================================================================
# The property a bare `timeout=` kwarg does NOT buy you
# =========================================================================
#
# ``subprocess.run(..., capture_output=True, timeout=N)`` kills only the DIRECT
# child on timeout, then (on Windows) re-enters ``communicate()`` with NO
# timeout to drain the capture pipes.  That drain blocks until the pipe reaches
# EOF, which cannot happen while a grandchild still holds the write handle it
# inherited — so the call lasts as long as the GRANDCHILD does, not ``N``.
# Measured on a Windows host 2026-08-15: ``subprocess.run(["cmd","/c","start /b
# ping -n 25 127.0.0.1"], capture_output=True, timeout=2)`` returned in 24.3s.
#
# ``run_text_capture`` captures into temp files — no pipe, no reader thread,
# nothing to drain — and tree-kills on timeout.  The two tests below cover the
# two shapes that hang the stdlib, and both use a child that outlives the
# budget by ~2 minutes so a regression fails on wall clock, not on luck.

def _leaky_argv(wedge_direct_child: bool) -> list:
    """A command that leaves a grandchild holding the inherited stdout handle.

    ``wedge_direct_child`` also keeps the direct child itself running past the
    deadline, which is what forces the timeout (and therefore the tree-kill)
    rather than a clean early return.
    """
    import platform

    if platform.system() == "Windows":
        # `start /b` detaches ping from cmd.exe; `& ping` (when wedging) keeps
        # cmd.exe itself alive in the foreground.  120 pings is ~119s.
        inner = "start /b ping -n 120 127.0.0.1"
        if wedge_direct_child:
            inner += " & ping -n 120 127.0.0.1"
        return ["cmd", "/c", inner]
    inner = "sleep 120 &"
    if wedge_direct_child:
        inner += " sleep 120"
    return ["sh", "-c", inner]


@pytest.mark.timeout(240)
def test_run_text_capture_returns_when_only_the_grandchild_lingers():
    """The exact measured repro: direct child exits, grandchild keeps running.

    This is the shape that cost 24.3s against a 2s budget under the stdlib.
    Nothing here even times out — with file-backed stdio there is no pipe to
    drain, so the call returns as soon as the direct child is reaped and the
    orphan is simply left to write into a temp file nobody reads.
    """
    import subprocess
    import time

    from hermes_cli._subprocess_compat import run_text_capture

    started = time.monotonic()
    try:
        run_text_capture(_leaky_argv(wedge_direct_child=False), timeout=3.0)
    except subprocess.TimeoutExpired:
        pass  # acceptable outcome; the assertion below is the real property
    elapsed = time.monotonic() - started

    assert elapsed < 30, (
        f"run_text_capture took {elapsed:.1f}s to reap a child that exited "
        "immediately — it is waiting on a pipe the orphaned grandchild still "
        "holds open (the ~119s stdlib drain is back)"
    )


@pytest.mark.timeout(240)
def test_run_text_capture_bounds_a_wedged_child_that_leaked_a_grandchild():
    """Direct child AND grandchild outlive the budget: must still time out.

    Exercises the tree-kill path.  The bound is ``budget + the synchronous
    kill``, which ``taskkill /T /F`` caps at 10s (measured 8.5-11.6s on loaded
    Windows hosts), so 45s keeps ~3x headroom over the worst case while
    staying far below the ~119s an unbounded drain would cost.
    """
    import subprocess
    import time

    from hermes_cli._subprocess_compat import run_text_capture

    budget = 3.0
    started = time.monotonic()
    with pytest.raises(subprocess.TimeoutExpired):
        run_text_capture(_leaky_argv(wedge_direct_child=True), timeout=budget)
    elapsed = time.monotonic() - started

    assert elapsed < 45, (
        f"run_text_capture took {elapsed:.1f}s against a {budget}s budget — a "
        "leaked grandchild is holding the capture open (file-backed stdio "
        "and/or the tree-kill regressed)"
    )
