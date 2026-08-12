"""End-to-end: a built wheel, installed without a source tree, must resolve
i18n catalogs and render human strings — not raw key paths.

This is the test that would have caught #27632 / #35374 / #23943. Metadata
unit tests (test_packaging_metadata.py) prove the glob is declared; this proves
the runtime actually finds the catalogs after a real pip install.

This lives in tests/ (NOT tests/e2e/) so it is collected by the dedicated CI
step in Task 9, not by the existing `python -m pytest tests/e2e/` runner.

Assumption: `from agent import i18n` must import with only stdlib + pyyaml
available (the test installs the wheel --no-deps + pyyaml). agent/__init__.py's
jiter preload swallows ImportError, and i18n.py imports yaml lazily inside
_load_catalog, so this holds today. If i18n.py ever gains a top-level non-stdlib
import, add it to the pip install line below.

Marked `integration` because it shells out to `uv build` + `venv` + `pip` and
takes ~15-30s. Run with: pytest -m integration tests/test_wheel_locales_e2e.py
"""

from __future__ import annotations

import glob
import os
import subprocess
import sys
import tarfile
import venv
from pathlib import Path

import pytest

from hermes_cli._subprocess_compat import run_text_capture

REPO_ROOT = Path(__file__).resolve().parents[1]

# ── Timeout budgets ─────────────────────────────────────────────────────────
#
# The pytest-timeout marker is a BACKSTOP and must therefore exceed the sum of
# the per-subprocess budgets inside the test. Get that backwards and the inner
# timeouts become unreachable: pytest-timeout fires first and kills the whole
# session with a stack dump, instead of the subprocess raising TimeoutExpired
# and the test failing on its own assert with the child's stderr attached.
#
# It WAS backwards — a flat ``@pytest.mark.timeout(300)`` guarded a test whose
# inner budgets sum to ~1330s, so `uv build`'s 600s could never fire. Observed
# 2026-08-12: a slow `uv build --sdist` was killed by the 300s marker while
# ``proc.wait(timeout=600)`` was still legitimately waiting, which reads in the
# traceback as a hang in the subprocess helper rather than a budget the test
# could never reach.
#
# Deriving the marker from the parts keeps the two from drifting apart again;
# that independent drift is the actual defect, not either number.
_BUILD_TIMEOUT = 600      # `uv build` — generous for a cold CI build
_PIP_TIMEOUT = 300        # each `pip install` into the throwaway venv
_PROBE_TIMEOUT = 120      # the installed-wheel probe
# run_text_capture's bound is `timeout` PLUS a synchronous tree-kill
# (taskkill measured at 8.5-11.6s on Windows), so budget the tail too.
_KILL_TAIL = 15
# venv.create() takes no timeout argument at all, so it can only be covered by
# slack in the backstop.
_VENV_CREATE_SLACK = 120
# The backstop must be strictly GREATER than the inner sum, not equal to it:
# at equality both deadlines land together and which one reports the failure is
# a race. This margin also covers fixture setup and pytest's own overhead,
# which sit inside the marker's window but outside every inner budget.
_BACKSTOP_SLACK = 60

_SDIST_TEST_BUDGET = _BUILD_TIMEOUT + _KILL_TAIL + _BACKSTOP_SLACK
_WHEEL_TEST_BUDGET = (
    _BUILD_TIMEOUT + _KILL_TAIL
    + _VENV_CREATE_SLACK
    + 2 * _PIP_TIMEOUT
    + _PROBE_TIMEOUT
    + _BACKSTOP_SLACK
)


@pytest.mark.integration
# Backstop only — see the budget block above. Overrides the global --timeout=30
# (a per-test marker beats the CLI flag), and must stay >= the inner budgets.
@pytest.mark.timeout(_WHEEL_TEST_BUDGET)
def test_installed_wheel_renders_i18n_strings(tmp_path):
    # 1. Build the wheel from the current tree.
    wheel_dir = tmp_path / "wheel"
    # run_text_capture, not capture_output=True: `uv build` runs the PEP 517
    # build backend in its own process, so the backend is a grandchild of this
    # call and inherits the capture pipe handles. On Windows it holds the write
    # end open, the pipe never reaches EOF, and subprocess.run kills only uv at
    # 600s before blocking on a drain that can never finish. Same class as the
    # uv/pip installs converted in tools/lazy_deps.py.
    build = run_text_capture(
        ["uv", "build", "--wheel", "--out-dir", str(wheel_dir), "."],
        cwd=REPO_ROOT,
        timeout=_BUILD_TIMEOUT,
    )
    assert build.returncode == 0, f"uv build failed:\n{build.stderr}"
    wheels = glob.glob(str(wheel_dir / "*.whl"))
    assert wheels, "no wheel produced"
    wheel = wheels[0]

    # 2. Fresh venv, install the wheel WITHOUT deps (we only exercise i18n,
    #    which needs pyyaml). --force-reinstall guards against pip's
    #    same-version no-op.
    venv_dir = tmp_path / "venv"
    venv.create(venv_dir, with_pip=True)
    # venv puts its interpreter and console scripts under ``Scripts/`` with an
    # ``.exe`` suffix on Windows and ``bin/`` elsewhere. Hardcoding
    # ``bin/python`` made this test POSIX-only by construction: on Windows the
    # very first subprocess below died with FileNotFoundError (WinError 2)
    # before it could assert anything about the wheel — so the packaging
    # regression this file exists to catch went unwatched on the platform the
    # developer actually runs.
    bindir = venv_dir / ("Scripts" if os.name == "nt" else "bin")
    vpy = bindir / ("python.exe" if os.name == "nt" else "python")
    subprocess.run(
        [str(vpy), "-m", "pip", "install", "-q", "pyyaml"],
        check=True, timeout=_PIP_TIMEOUT,
    )
    subprocess.run(
        [str(vpy), "-m", "pip", "install", "-q", "--no-deps", "--force-reinstall", wheel],
        check=True,
        timeout=_PIP_TIMEOUT,
    )

    # 3. Run from a directory that is NOT the source tree, with a clean env
    #    (no PYTHONPATH leaking the repo, no HERMES_BUNDLED_LOCALES).
    probe = (
        "from agent import i18n;"
        "import sys;"
        "r = i18n.t('gateway.reset.header_default', lang='en');"
        "s = i18n.t('gateway.status.header', lang='en');"
        "print(repr(r)); print(repr(s));"
        "sys.exit(0 if (r != 'gateway.reset.header_default' "
        "and s != 'gateway.status.header') else 1)"
    )
    env = {k: v for k, v in os.environ.items() if k not in ("PYTHONPATH", "HERMES_BUNDLED_LOCALES")}
    # os.pathsep, not a literal ":" — on Windows the separator is ";" and a
    # ":"-joined value silently collapses the whole PATH into one bogus entry.
    env["PATH"] = os.pathsep.join([str(bindir), env["PATH"]])
    env["VIRTUAL_ENV"] = str(venv_dir)
    run = subprocess.run(
        [str(vpy), "-c", probe],
        cwd=str(tmp_path),  # NOT the repo root
        capture_output=True,
        text=True,
        env=env,
        timeout=_PROBE_TIMEOUT,
    )
    assert run.returncode == 0, (
        "installed wheel returned raw i18n keys instead of human strings:\n"
        f"stdout: {run.stdout}\nstderr: {run.stderr}"
    )


@pytest.mark.integration
# Backstop only — see the budget block above. Must stay >= the build budget
# plus its tree-kill tail, or `uv build`'s timeout can never fire.
@pytest.mark.timeout(_SDIST_TEST_BUDGET)
def test_built_sdist_ships_locale_catalogs(tmp_path):
    """The sdist must carry locales/ too.

    The wheel is covered above; the sdist is a separately shipped artifact
    (PyPI, and the form distro/Homebrew packagers build from). MANIFEST.in
    `graft locales` is what puts the catalogs in the tarball — a stale graft or
    a setuptools change would pass the metadata unit test (which only inspects
    the declaration) while the actual artifact regresses. This inspects the
    real tarball so that path can't rot silently. Closes the sdist half of
    #27632 / #35374 / #23943.
    """
    sdist_dir = tmp_path / "sdist"
    # run_text_capture: the PEP 517 backend is a grandchild here too — see the
    # note on the --wheel build above.
    build = run_text_capture(
        ["uv", "build", "--sdist", "--out-dir", str(sdist_dir), "."],
        cwd=REPO_ROOT,
        timeout=_BUILD_TIMEOUT,
    )
    assert build.returncode == 0, f"uv build --sdist failed:\n{build.stderr}"
    tarballs = glob.glob(str(sdist_dir / "*.tar.gz"))
    assert tarballs, "no sdist produced"

    with tarfile.open(tarballs[0]) as tf:
        # Members are prefixed with the sdist root dir, e.g.
        # hermes_agent-0.15.1/locales/en.yaml — match on the suffix.
        catalogs = [m for m in tf.getnames() if "/locales/" in m and m.endswith(".yaml")]

    # Compare against the canonical language list rather than a hardcoded floor
    # so adding/removing a catalog updates the guard automatically and a dropped
    # catalog (not just a fully-empty graft) trips it.
    from agent.i18n import SUPPORTED_LANGUAGES

    expected = len(SUPPORTED_LANGUAGES)
    assert len(catalogs) == expected, (
        f"sdist shipped {len(catalogs)} locale catalogs, expected {expected} "
        f"({len(SUPPORTED_LANGUAGES)} supported languages) — check `graft "
        "locales` in MANIFEST.in"
    )
    assert any(m.endswith("/locales/en.yaml") for m in catalogs), (
        f"sdist missing locales/en.yaml; shipped: {catalogs[:5]}"
    )
