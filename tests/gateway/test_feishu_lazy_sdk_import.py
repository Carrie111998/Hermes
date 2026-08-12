"""The Feishu adapter must not drag the lark_oapi SDK in at import time.

``load_gateway_config()`` calls ``platform_registry.plugin_entries()``, whose
``_resolve_all()`` imports *every* bundled platform adapter before the gateway
binds :8642.  ``lark_oapi`` is ~9k small modules; on a chronically-full disk
that single import measured 36.6s of a 40.9s resolve-all (89.5%) -- long enough
for the Hermes-Gateway-Watchdog (~6 min respawn) to stack concurrent boots that
mutually starve on disk I/O.

So the invariant is: importing the adapter -- and resolving the whole platform
registry -- must leave ``lark_oapi`` unimported.  The SDK loads on first real
use via ``check_feishu_requirements()``.

Each check runs in a subprocess: ``sys.modules`` is process-global, so a sibling
test that already imported ``lark_oapi`` would otherwise mask a regression.
"""

import subprocess
import sys
from pathlib import Path
from unittest.mock import MagicMock

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]


# pyproject's addopts set a global ``--timeout`` (30s; the nightly gate passes
# 60s) which is FAR below the 300s these subprocess tests are written against,
# so the author's budget was unreachable and pytest-timeout won every race.
# That is not a slow-test annoyance: pytest-timeout's thread method HARD-EXITS
# the whole pytest process, so one of these blowing its cap takes the entire
# FILE down and every test in it reports as never having run. On 2026-08-11
# that is exactly what happened to 8 feishu files plus test_matrix.py in the
# nightly gate's per-file run.
#
# These tests each spawn a clean interpreter that imports lark_oapi -- 101 MB
# across 21,169 files. Measured at 17.05s on an idle box (7th slowest in the
# suite) but disk-bound, so it scales with contention and blows a 30-60s cap
# under load. Give them a per-test cap that matches _run()'s own budget; the
# marker overrides the global setting. Sized slightly ABOVE _run's timeout so
# subprocess.TimeoutExpired fires first and reports WHICH command hung,
# instead of pytest-timeout hard-exiting with a thread dump.
# 2026-08-11, second sizing: 300 was still not enough. Only ONE of these tests
# (test_check_feishu_requirements_binds_sdk_on_first_use) actually completes the
# import — the other two assert lark stays absent and return quickly — and that
# one blew the 300s cap in the nightly gate's 24-worker run. Direct measurement
# of a bare `import lark_oapi` on this box: ~130s with the page cache warm,
# 339.5s cold. 300s was under the cold figure before contention is added at all.
#
# 600s is bounded by the harness's own 900s per-file cap: the file's other three
# tests plus one attempt of this one leave headroom, and --file-timeout applies
# per attempt so the automatic retry does not stack against it.
#
# If this blows again it is a capacity signal about the host (11k modules on an
# MSIX path), not a lazy-binding regression — the assertion it guards is about
# WHICH symbols get bound, not how fast.
_SUBPROCESS_TIMEOUT_S = 600
slow_subprocess = pytest.mark.timeout(_SUBPROCESS_TIMEOUT_S + 30)


def _run(code: str, timeout: int = _SUBPROCESS_TIMEOUT_S) -> subprocess.CompletedProcess:
    """Run *code* in a clean interpreter rooted at the repo under test.

    ``cwd=REPO_ROOT`` puts this checkout ahead of any installed copy on
    ``sys.path``, so the test exercises the working tree rather than a stale
    site-packages install.
    """
    return subprocess.run(
        [sys.executable, "-c", code],
        cwd=str(REPO_ROOT),
        capture_output=True,
        text=True,
        timeout=timeout,
    )


def _lark_installed() -> bool:
    """True when the real ``lark_oapi`` SDK is importable from disk.

    Deliberately *not* ``importlib.util.find_spec``: that consults
    ``sys.modules`` first and raises ``ValueError`` if the named module is
    already there without a ``__spec__`` -- which is exactly what a sibling
    test's ``MagicMock`` stub looks like.  ``PathFinder`` searches ``sys.path``
    instead, so the answer reflects the on-disk SDK that the clean
    subprocesses in ``_run`` will actually import.
    """
    from importlib.machinery import PathFinder

    return PathFinder.find_spec("lark_oapi") is not None


requires_lark = pytest.mark.skipif(
    not _lark_installed(), reason="lark_oapi not installed"
)


def test_lark_installed_ignores_injected_sys_modules_stub(monkeypatch):
    """The probe must describe the on-disk SDK, not an in-process stub.

    ``tests/gateway/test_feishu_approval_buttons.py`` installs a ``MagicMock``
    under ``sys.modules["lark_oapi"]`` at *import* time, and it sorts before
    this module -- so during collection of ``tests/gateway`` the stub is
    already in place.  ``MagicMock`` refuses to synthesise dunder attributes,
    so reading ``__spec__`` off it raises ``AttributeError``, which
    ``importlib.util.find_spec`` re-raises as ``ValueError``.  That aborted
    collection for the entire directory, not just this file.

    The stub must change neither the answer nor the control flow: every real
    check here runs in a subprocess with a clean interpreter, so what matters
    is whether the SDK is importable from disk.
    """
    baseline = _lark_installed()

    monkeypatch.setitem(sys.modules, "lark_oapi", MagicMock())

    assert _lark_installed() == baseline


@requires_lark
@slow_subprocess
def test_importing_feishu_adapter_does_not_import_lark_oapi():
    """Importing the adapter module must not pull in the SDK."""
    proc = _run(
        "import sys\n"
        "import plugins.platforms.feishu.adapter as a\n"
        "leaked = sorted(m for m in sys.modules if m.split('.')[0] == 'lark_oapi')\n"
        "print('LEAKED', len(leaked))\n"
        "print('HAS_REGISTER', callable(getattr(a, 'register', None)))\n"
    )
    assert proc.returncode == 0, f"import failed:\n{proc.stdout}\n{proc.stderr}"
    assert "HAS_REGISTER True" in proc.stdout, proc.stdout
    assert "LEAKED 0" in proc.stdout, (
        "feishu adapter imported lark_oapi at module scope:\n" + proc.stdout
    )


@requires_lark
@slow_subprocess
def test_platform_registry_resolve_all_does_not_import_lark_oapi():
    """The pre-bind resolve-all must not pay for the Feishu SDK.

    This is the path ``load_gateway_config()`` actually takes.
    """
    proc = _run(
        "import sys\n"
        "from hermes_cli.plugins import discover_plugins\n"
        "discover_plugins()\n"
        "from gateway.platform_registry import platform_registry as pr\n"
        "names = {e.name for e in pr.plugin_entries()}\n"
        "leaked = sorted(m for m in sys.modules if m.split('.')[0] == 'lark_oapi')\n"
        "print('FEISHU_REGISTERED', 'feishu' in names)\n"
        "print('LEAKED', len(leaked))\n"
    )
    assert proc.returncode == 0, f"resolve-all failed:\n{proc.stdout}\n{proc.stderr}"
    # Deferring the SDK must not cost us the platform itself.
    assert "FEISHU_REGISTERED True" in proc.stdout, proc.stdout
    assert "LEAKED 0" in proc.stdout, (
        "platform_registry resolve-all imported lark_oapi:\n" + proc.stdout
    )


@requires_lark
@slow_subprocess
def test_check_feishu_requirements_binds_sdk_on_first_use():
    """Deferring must not break real Feishu use.

    ``check_feishu_requirements()`` has to import the SDK and rebind the
    module globals.  It must do so with a plain import: the pinned lazy-dep
    spec (lark-oapi==1.6.8) does not match every working install (1.5.3 here),
    so routing this through ``lazy_deps.ensure()`` would report the SDK as
    missing and attempt a pip install on a box where Feishu already works.
    """
    proc = _run(
        "import plugins.platforms.feishu.adapter as a\n"
        "ok = a.check_feishu_requirements()\n"
        "print('OK', ok)\n"
        "print('AVAILABLE', a.FEISHU_AVAILABLE)\n"
        "print('LARK_BOUND', a.lark is not None)\n"
        "print('WS_BOUND', a.FeishuWSClient is not None)\n"
    )
    assert proc.returncode == 0, f"check failed:\n{proc.stdout}\n{proc.stderr}"
    assert "OK True" in proc.stdout, proc.stdout
    assert "AVAILABLE True" in proc.stdout, proc.stdout
    assert "LARK_BOUND True" in proc.stdout, proc.stdout
    assert "WS_BOUND True" in proc.stdout, proc.stdout
