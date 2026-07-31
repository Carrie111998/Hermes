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

The fix aliases the running `__main__` module under `__spec__.name` and binds
it as the `main` attribute on the `hermes_cli` parent package (a sys.modules
cache hit skips importlib's parent-attribute binding), so the canonical import
reuses it. This test drives the real seam in a child process: execute main.py
as `__main__` with an explicit `-p default`, then import it canonically and
assert the process home did not move.
"""

import os
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]


def _child_python() -> str:
    """Interpreter for spawned children.

    Test runners that overlay pytest onto a bare python (uv run --with pytest)
    leave sys.executable dep-less; prefer the project venv's interpreter when
    present so the child can import hermes_cli with its dependencies.
    """
    venv_python = REPO_ROOT / "venv" / "bin" / "python"
    return str(venv_python) if venv_python.exists() else sys.executable


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
aliased = sys.modules["hermes_cli.main"]

# Pass 2: the lazy canonical import RPC handlers perform mid-flight.
import hermes_cli.main  # noqa: F401

assert sys.modules["hermes_cli.main"] is aliased, (
    "canonical import must reuse the aliased running module, not re-load it"
)

# A sys.modules entry is only half the contract: importlib binds `main` as an
# attribute on the parent package only when it truly loads the module; a cache
# hit skips it. Handlers that dereference the dotted path need the attribute
# to exist AND be the same canonical module.
import hermes_cli

assert getattr(hermes_cli, "main", None) is sys.modules["hermes_cli.main"], (
    "parent package must expose the canonical module as its `main` attribute"
)

assert os.environ["HERMES_HOME"] == root, (
    "canonical import re-executed top-level bootstrap and rehomed the process: "
    + os.environ["HERMES_HOME"]
)
print("OK")
"""


# Grandchild: a brand-new `python -m hermes_cli.main -p other --help` process,
# spawned from the aliased parent. runpy with run_name="__main__" is what `-m`
# does, and running it under `-c` lets us observe the resolved home directly.
GRANDCHILD = r"""
import os, runpy, sys

sys.argv = ["hermes", "-p", "other", "--help"]
try:
    runpy.run_module("hermes_cli.main", run_name="__main__", alter_sys=True)
except SystemExit:
    pass

print("GRANDCHILD_HOME=" + os.path.realpath(os.environ["HERMES_HOME"]))
"""

CHILD_SPAWNS_GRANDCHILD = r"""
import os, runpy, subprocess, sys

root = sys.argv.pop()            # temp HERMES root, passed last
grandchild_src = sys.argv.pop()  # grandchild program, passed before it
expected = os.path.realpath(os.path.join(root, "profiles", "other"))
os.environ["HERMES_HOME"] = root

# Become the aliased `hermes -p default` process the fix targets.
sys.argv = ["hermes", "-p", "default", "--help"]
try:
    runpy.run_module("hermes_cli.main", run_name="__main__", alter_sys=True)
except SystemExit:
    pass

assert os.environ["HERMES_HOME"] == root, "pass 1 must pin the explicit -p default root"

# The alias is an entry in THIS process's sys.modules, so it cannot cross a
# process boundary. The grandchild inherits HERMES_HOME=<root> and the sticky
# active_profile, and must still resolve its own explicit `-p other`.
proc = subprocess.run(
    [sys.executable, "-c", grandchild_src],
    capture_output=True,
    text=True,
    timeout=180,
)
assert proc.returncode == 0, "grandchild exited %d: %s" % (proc.returncode, proc.stderr[-2000:])

home = ""
for line in proc.stdout.splitlines():
    if line.startswith("GRANDCHILD_HOME="):
        home = line.split("=", 1)[1]

assert home == expected, (
    "grandchild resolved %r instead of its own -p profile %r" % (home, expected)
)

# Recorded last so the grandchild's result is the first thing to fail: the
# parent really was in the aliased state while it spawned the grandchild.
assert "hermes_cli.main" in sys.modules, "running module must self-register under its canonical name"
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

    python = _child_python()

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


def test_child_process_still_resolves_its_own_profile(tmp_path):
    """The alias must not suppress profile parsing in spawned processes.

    An earlier attempt at this bug used an ``os.environ`` sentinel to mark the
    bootstrap as already applied. Environment variables are inherited, so every
    child hermes process silently skipped ``_apply_profile_override()`` and ran
    against its parent's home no matter what ``-p`` it was given (the #53955
    regression). Aliasing ``sys.modules`` is process-local and structurally
    cannot do that — this test pins that difference.
    """
    root = tmp_path / "hermes-root"
    (root / "profiles" / "lured").mkdir(parents=True)
    (root / "profiles" / "other").mkdir(parents=True)
    (root / "active_profile").write_text("lured\n", encoding="utf-8")

    env = dict(os.environ)
    env.pop("HERMES_PROFILE", None)

    proc = subprocess.run(
        [_child_python(), "-c", CHILD_SPAWNS_GRANDCHILD, GRANDCHILD, str(root)],
        capture_output=True,
        text=True,
        timeout=300,
        cwd=str(REPO_ROOT),
        env=env,
    )

    assert proc.returncode == 0, proc.stderr[-2000:]
    assert "OK" in proc.stdout
