"""A per-test timeout must fail one test, not destroy the run.

Guards ``tests/_nonfatal_timeout.py``. See that module for why: pytest-timeout's
``thread`` method -- the only one that works on Windows -- answers a timeout with
``os._exit(1)``, which leaves the run with no summary line and a failure set that
depends on where the process happened to die.

The control arm is the point of this test. Both arms exit non-zero, so the exit
code proves nothing; what separates them is whether pytest lived long enough to
print ``1 failed``.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]

PROBE = '''
import time


def test_sleeps_past_the_cap():
    time.sleep(10)


def test_runs_after_the_timeout():
    assert True
'''


def _run_probe(tmp_path, *plugin_args):
    probe_dir = tmp_path / "probe"
    probe_dir.mkdir()
    (probe_dir / "test_probe.py").write_text(PROBE, encoding="utf-8")
    # --rootdir bounds collection at the probe: without it a pytest spawned with
    # cwd=REPO_ROOT walks all of C:\Users -> ... -> %TEMP% to reach the probe.
    cmd = [
        sys.executable,
        "-m",
        "pytest",
        str(probe_dir),
        f"--rootdir={probe_dir}",
        "-p",
        "no:cacheprovider",
        *plugin_args,
        "--timeout=2",
        "--timeout-method=thread",
        "-q",
    ]
    return subprocess.run(
        cmd,
        cwd=str(REPO_ROOT),
        capture_output=True,
        text=True,
        timeout=180,
    )


@pytest.mark.timeout(240)
def test_stock_thread_timeout_kills_the_process_without_a_summary(tmp_path):
    """Control: this is the behaviour being fixed."""
    result = _run_probe(tmp_path)
    combined = result.stdout + result.stderr
    assert "1 failed" not in combined, (
        "expected the stock thread method to die before printing a summary; "
        f"got:\n{combined}"
    )


@pytest.mark.timeout(240)
def test_nonfatal_timeout_reports_a_failure_and_finishes_the_run(tmp_path):
    result = _run_probe(tmp_path, "-p", "tests._nonfatal_timeout")
    combined = result.stdout + result.stderr

    # The timing-out test is reported as a normal failure...
    assert "1 failed" in combined, f"no summary line in:\n{combined}"
    # ...and the run continued past it rather than dying at os._exit.
    assert "1 passed" in combined, (
        f"the test after the timeout never ran:\n{combined}"
    )
    assert "Timeout (>2.0s)" in combined, f"timeout not attributed:\n{combined}"
