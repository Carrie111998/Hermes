"""Gateable pytest entry points for the stress scripts.

Each stress script is an ``__main__``-executable program that exits 0 iff
every check inside it passed. Rather than restructure eight programs into
pytest modules, this runs each one exactly the way its README documents —
as a subprocess — and asserts the exit code.

Running them out-of-process is deliberate, not laziness:

* it is the same code path humans use, so a green test here means the
  documented command really works;
* the scripts mutate ``os.environ['HERMES_HOME']``, ``sys.path`` and the
  ``hermes_cli`` entries of ``sys.modules`` at will, and several spawn
  their own children — importing them into the pytest process would leak
  that state across tests;
* a hung or crashing script is bounded by the subprocess timeout instead
  of taking the session with it.

These are marked ``stress`` and deselected by default (see ``addopts`` in
pyproject.toml), because the full set runs for roughly 25 minutes. Run
them explicitly:

    pytest tests/stress -m stress
    pytest tests/stress -m stress -k property_fuzzing
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

from tests.timeout_budget import scaled

_HERE = Path(__file__).parent
_SELF = Path(__file__).name
REPO_ROOT = _HERE.parents[1]

# Discovered, not hardcoded: a stress script added later is picked up
# automatically instead of silently going unrun.
SCRIPTS = sorted(p.name for p in _HERE.glob("test_*.py") if p.name != _SELF)

# Incidental safety net, not the assertion (tests/timeout_budget.py rule 2).
# The slowest scripts measured ~530s (property_fuzzing) and ~500s
# (benchmarks) on an idle box, and this host routinely runs several agent
# sessions at once: the same lane took 26m unloaded and 35m alongside three
# sibling pytest runs. A bound only ~25% above the idle measurement would
# convert that contention into a red that looks like a regression, so the
# base is set well clear of it. It still bounds a genuinely hung script,
# which is all this is for.
SCRIPT_TIMEOUT = scaled(1800)


def test_stress_scripts_are_discovered():
    """Guard the discovery glob itself.

    If this file's glob or the conftest ignore list drifts, the parametrised
    test below would silently shrink to nothing and report all-green while
    running no stress coverage at all.
    """
    assert len(SCRIPTS) >= 8, f"expected the stress scripts, found {SCRIPTS}"
    assert _SELF not in SCRIPTS


@pytest.mark.stress
@pytest.mark.timeout(SCRIPT_TIMEOUT + 120)
@pytest.mark.parametrize("script", SCRIPTS)
def test_stress_script(script):
    path = _HERE / script
    try:
        proc = subprocess.run(
            [sys.executable, str(path)],
            cwd=str(REPO_ROOT),
            capture_output=True,
            text=True,
            timeout=SCRIPT_TIMEOUT,
        )
    except subprocess.TimeoutExpired:
        pytest.fail(f"{script} exceeded {SCRIPT_TIMEOUT:.0f}s")

    if proc.returncode != 0:
        out_tail = "\n".join((proc.stdout or "").splitlines()[-40:])
        err_tail = "\n".join((proc.stderr or "").splitlines()[-20:])
        pytest.fail(
            f"{script} exited {proc.returncode}\n"
            f"--- stdout (tail) ---\n{out_tail}\n"
            f"--- stderr (tail) ---\n{err_tail}"
        )

    # A passing script must also have taken its HERMES_HOME with it.
    #
    # This exit code is not enough on its own: _temphome.cleanup_home
    # cannot raise (a temp dir it failed to remove is not a reason to fail
    # the kernel assertions it was supporting), so it reports the survivor
    # on stdout -- and stdout is discarded above unless the run is already
    # red. That blind spot is not hypothetical: test_subprocess_e2e.py
    # leaked its home on every green run for the life of the fix, printing
    # this exact warning into output nobody read, while the lane reported
    # 8 passed. %TEMP% is the shared resource here, so a green run that
    # leaks is a real defect and the lane is the only thing positioned to
    # notice.
    #
    # Matched on the script's own stdout rather than by censusing %TEMP%
    # around the subprocess: sibling sessions run these same scripts from
    # other worktrees, so a census would attribute their directories to
    # this run and go red at random. The warning line is unambiguously
    # this process's.
    #
    # `[temp] kept for debugging:` and `[temp] kept (HERMES_STRESS_KEEP_TEMP
    # set):` are deliberate retention, not leaks, and are not matched.
    leaked = [
        ln for ln in (proc.stdout or "").splitlines()
        if "[temp] WARNING" in ln
    ]
    if leaked:
        detail = "\n".join(leaked)
        pytest.fail(
            f"{script} exited 0 but did not remove its HERMES_HOME:\n{detail}\n"
            "A passing run must clean up after itself; see "
            "tests/stress/_temphome.py."
        )
