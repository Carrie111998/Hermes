"""The ``hermes-agent`` console script must parse argv, not ignore it.

``[project.scripts]`` maps ``hermes-agent = "run_agent:main"``, and the
generated shim calls ``main()`` with **no arguments**::

    from run_agent import main
    sys.exit(main())

``fire.Fire(main)`` only runs under ``if __name__ == "__main__"``, i.e. only
for ``python run_agent.py``. So every flag passed to the console script was
silently dropped and ``main()`` ran its built-in demo query against the live
API -- ``hermes-agent --help`` executed a real agent turn instead of printing
usage, and ``--list_tools`` did the same.

NOTE ON WHAT IS ASSERTED IN-PROCESS. A dozen modules across the suite run
``sys.modules.setdefault("fire", SimpleNamespace(Fire=lambda *a, **k: None))``
at import time to avoid importing the real python-fire. Because pytest imports
every test module during collection, ``fire.Fire`` is a no-op lambda for the
whole session whenever more than this file is collected. So the in-process
tests below assert the seam this module owns -- *that argv is handed to fire* --
and the end-to-end test shells out to a clean interpreter to assert what fire
actually renders. Asserting real usage text in-process would pass alone and
fail in the full suite.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

import run_agent

REPO_ROOT = Path(run_agent.__file__).parent


@pytest.fixture(autouse=True)
def _never_run_the_agent(monkeypatch):
    """Fail loudly if a flag-bearing invocation reaches the demo path.

    ``main()`` constructs an ``AIAgent`` before it sends anything, so tripping
    here proves the demo path was entered without spending an API turn.
    """
    def _boom(*args, **kwargs):
        raise AssertionError("main() entered the demo path instead of parsing argv")

    monkeypatch.setattr(run_agent, "AIAgent", _boom)


def test_flagged_invocation_delegates_to_the_cli_parser(monkeypatch):
    """``main()`` with flags in argv must hand off, not run the demo."""
    monkeypatch.setattr(sys, "argv", ["hermes-agent", "--help"])

    called = {}
    monkeypatch.setattr(run_agent, "cli_main", lambda: called.setdefault("yes", True))

    run_agent.main()

    assert called.get("yes"), "main() ignored argv instead of delegating to cli_main()"


def test_bare_invocation_still_runs_the_demo(monkeypatch):
    """No argv means no CLI intent -- the historical default is preserved."""
    monkeypatch.setattr(sys, "argv", ["hermes-agent"])

    with pytest.raises(AssertionError, match="entered the demo path"):
        run_agent.main()


def test_programmatic_call_ignores_an_unrelated_argv(monkeypatch):
    """A caller passing real arguments must never have argv reinterpreted.

    Guards the obvious footgun in the delegation above: under a test runner (or
    any host process) ``sys.argv`` carries flags that have nothing to do with
    this function, and handing them to fire would parse garbage.
    """
    monkeypatch.setattr(sys, "argv", ["pytest", "-q", "--timeout=60"])

    # Asserting only "the demo path ran" would pass for the wrong reason: fire
    # re-enters main() and would trip the same sentinel. Assert on delegation.
    def _no_delegate():
        raise AssertionError("delegated to fire on a programmatic call")

    monkeypatch.setattr(run_agent, "cli_main", _no_delegate)

    with pytest.raises(AssertionError, match="entered the demo path"):
        run_agent.main(query="an explicit programmatic query")


def test_help_renders_usage_without_running_a_turn():
    """End-to-end against real python-fire, in a clean interpreter.

    Shells out because the in-process ``fire`` is stubbed suite-wide (see the
    module docstring). This is the test that actually pins the reported bug:
    ``--help`` must print usage rather than execute an agent turn.
    """
    proc = subprocess.run(
        [sys.executable, "run_agent.py", "--help"],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=300,
    )

    combined = proc.stdout + proc.stderr
    assert proc.returncode == 0, combined[-2000:]
    assert "SYNOPSIS" in combined, combined[-2000:]
    assert "--max_turns" in combined, combined[-2000:]
    # The demo query banner must be absent -- its presence means a live turn ran.
    assert "User Query:" not in combined, combined[-2000:]
