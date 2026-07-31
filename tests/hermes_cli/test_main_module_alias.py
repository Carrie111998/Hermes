"""`python -m hermes_cli.main` must not re-execute its top-level bootstrap when
the module is later imported under its canonical name.

Regression for the serve-rehoming bug: `-m` registers main.py only as
`__main__`; the first canonical `import hermes_cli.main` (setup.status /
runtime_check handlers, /api/pty argv builder, plugin aux-task registration)
re-executed every top-level statement. Pass 1 had already consumed
`-p <name>` from sys.argv, so pass 2's unconditional `_apply_profile_override()`
fell through to the sticky active_profile and permanently rewrote
os.environ["HERMES_HOME"] — a serve launched `-p default` was silently rehomed
to the sticky profile the moment the desktop asked for runtime readiness.

The fix aliases the running `__main__` module under `__spec__.name`, so the
canonical import reuses it. This test drives the real seam in a child process:
execute main.py as `__main__` with an explicit `-p default`, then import it
canonically and assert the process home did not move.
"""

import os
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]

CHILD = r"""
import os, runpy, sys

root = sys.argv.pop()  # temp HERMES root, passed last
os.environ["HERMES_HOME"] = root

# Pass 1: how `hermes -p default serve` starts (fast-exiting argv).
sys.argv = ["hermes", "-p", "default", "--help"]
try:
    runpy.run_module("hermes_cli.main", run_name="__main__", alter_sys=True)
except SystemExit:
    pass

assert os.environ["HERMES_HOME"] == root, "pass 1 must pin the explicit -p default root"
assert "hermes_cli.main" in sys.modules, "running module must self-register under its canonical name"

# Pass 2: the lazy canonical import RPC handlers perform mid-flight.
import hermes_cli.main  # noqa: F401

assert os.environ["HERMES_HOME"] == root, (
    "canonical import re-executed top-level bootstrap and rehomed the process: "
    + os.environ["HERMES_HOME"]
)
print("OK")
"""


def test_canonical_import_does_not_rehome_running_main(tmp_path):
    # A sticky active_profile pointing at an existing named profile is the
    # trigger: pass 2 (if it runs) follows it because argv no longer carries -p.
    root = tmp_path / "hermes-root"
    profile = root / "profiles" / "lured"
    profile.mkdir(parents=True)
    (root / "active_profile").write_text("lured\n", encoding="utf-8")

    env = dict(os.environ)
    env.pop("HERMES_PROFILE", None)

    # The child must run under an interpreter that can import hermes_cli with
    # its dependencies. Test runners that overlay pytest onto a bare python
    # (uv run --with pytest) leave sys.executable dep-less; prefer the project
    # venv's interpreter when present.
    venv_python = REPO_ROOT / "venv" / "bin" / "python"
    python = str(venv_python) if venv_python.exists() else sys.executable

    proc = subprocess.run(
        [python, "-c", CHILD, str(root)],
        capture_output=True,
        text=True,
        timeout=120,
        cwd=str(REPO_ROOT),
        env=env,
    )

    assert proc.returncode == 0, proc.stderr[-2000:]
    assert "OK" in proc.stdout
