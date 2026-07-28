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

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]


def _run(code: str, timeout: int = 300) -> subprocess.CompletedProcess:
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
    import importlib.util

    return importlib.util.find_spec("lark_oapi") is not None


requires_lark = pytest.mark.skipif(
    not _lark_installed(), reason="lark_oapi not installed"
)


@requires_lark
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
