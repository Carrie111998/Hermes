"""Regression: importing ``hermes_cli.auth`` must not import ``httpx``.

Why this exists
---------------
``hermes_cli/auth.py`` is an 8,583-line credential module whose HTTP use is
entirely OAuth: device-code logins, token refreshes, discovery documents. None
of that happens at import time. But it carried ``import httpx`` at module
scope, and ``gateway.config.load_gateway_config()`` does ``from hermes_cli.auth
import has_usable_secret`` -- so every ``hermes send`` to a real platform paid
for it.

``import httpx`` is worth 256 modules on this box. httpx 0.28.1's ``__init__``
runs ``from ._main import main``, which is its CLI entry point and drags in
click, rich, pygments and attrs. A Telegram notification was loading a terminal
syntax highlighter. Measured on PRECISION 2026-08-20:

    import hermes_cli.auth            304 -> 138 modules
    hermes send --to <platform> msg   516 -> 369 modules

This is the third and last instance of one defect found in the same sweep. The
first two were ``gateway/__init__.py`` (tests/hermes_cli/test_send_import_cost.py)
and nine bundled backend plugins
(tests/hermes_cli/test_plugin_discovery_import_cost.py).

Why the fix is shaped the way it is
-----------------------------------
The 38 ``httpx`` references split cleanly: 16 functions touch it at runtime and
open with ``httpx = _ensure_httpx()``; 7 only name it in a ``client:
httpx.Client`` parameter annotation, which ``from __future__ import
annotations`` already makes a string, so a TYPE_CHECKING import covers them.

``_ensure_httpx()`` reads the module GLOBAL rather than doing a plain local
``import httpx``, and that distinction is load-bearing. Eight tests in
tests/hermes_cli/test_auth_qwen_provider.py use ``patch("hermes_cli.auth.httpx")``
-- whole-attribute replacement with a MagicMock. A local import would fetch the
real httpx and sail straight past the patch, turning a mocked test into a live
network call against a token endpoint. Eight further references patch through
the attribute (``patch("hermes_cli.auth.httpx.Client")``), which the PEP 562
``__getattr__`` serves by importing the real module and caching it.

Both patch styles are asserted below, because "the import got cheaper" is worth
nothing if it also quietly disarmed the test suite's mocking.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]

# Every module that must stay out of a bare ``import hermes_cli.auth``. rich,
# click and pygments are here as httpx's CLI-extra companions: they are the
# loudest symptom, and naming them makes a regression's blast radius obvious in
# the failure message.
FORBIDDEN = ("httpx", "rich", "click", "pygments", "requests", "urllib3")

# Both figures are modules OVER the bare interpreter floor (~70 here), which is
# how every measurement in this sweep was taken -- see the method note in
# tests/hermes_cli/test_send_import_cost.py. Generous ceiling over the observed
# 138: the pre-fix cost was 304, and the smallest regression that could
# reappear -- a module-scope ``import httpx`` -- is worth ~166, so 200 leaves
# room to grow without letting that back in.
MAX_AUTH_IMPORT_MODULES = 200
BASELINE_AUTH_IMPORT_MODULES = 304


def _child_env() -> dict:
    env = dict(os.environ)
    # Keep the child off the lazy-install path so a slow or absent optional dep
    # can never turn this into a pip run. See tools/lazy_deps.py.
    env["HERMES_DISABLE_LAZY_INSTALLS"] = "1"
    # Pin the checkout under test first: an editable install can otherwise
    # point ``hermes_cli`` at a different tree, and this would pass against
    # code that is not the code under test.
    existing = env.get("PYTHONPATH")
    env["PYTHONPATH"] = (
        str(REPO_ROOT) + os.pathsep + existing if existing else str(REPO_ROOT)
    )
    return env


def _run(code: str) -> subprocess.CompletedProcess:
    """Run *code* in a fresh interpreter.

    Subprocess, not in-process: ``sys.modules`` is process-global, so a sibling
    test that legitimately imported httpx would mask every assertion here.
    """
    return subprocess.run(
        [sys.executable, "-c", code],
        cwd=str(REPO_ROOT),
        env=_child_env(),
        capture_output=True,
        text=True,
        timeout=900,
    )


@pytest.mark.timeout(900)
def test_importing_auth_does_not_import_httpx():
    proc = _run(
        "import sys\n"
        "import hermes_cli.auth  # noqa: F401\n"
        f"watch = {FORBIDDEN!r}\n"
        "print('OFFENDERS=[' + ','.join(m for m in watch if m in sys.modules) + ']')\n"
    )
    assert proc.returncode == 0, (
        f"importing hermes_cli.auth failed.\nstdout:\n{proc.stdout}\n"
        f"stderr tail:\n{proc.stderr[-3000:]}"
    )
    assert "OFFENDERS=[]" in proc.stdout, (
        f"hermes_cli.auth imported {proc.stdout.strip()} at module scope.\n"
        "load_gateway_config() imports this module for has_usable_secret, so "
        "every `hermes send` pays for it. Use `httpx = _ensure_httpx()` inside "
        "the function that needs it -- not a module-scope import, and not a "
        "bare function-local `import httpx` either (see the module docstring)."
    )


def _importtime_modules(code: str) -> list[str]:
    proc = subprocess.run(
        [sys.executable, "-X", "importtime", "-c", code],
        cwd=str(REPO_ROOT),
        env=_child_env(),
        capture_output=True,
        text=True,
        timeout=900,
    )
    assert proc.returncode == 0, (
        f"importtime run failed for {code!r}.\nstderr tail:\n{proc.stderr[-3000:]}"
    )
    return [
        line.split("|")[-1].strip()
        for line in proc.stderr.splitlines()
        if line.startswith("import time:") and "cumulative" not in line
    ]


@pytest.mark.timeout(900)
def test_auth_import_cost_stays_near_the_floor():
    """Cost is measured OVER the bare interpreter floor, not absolute.

    ``python -X importtime -c pass`` is ~70 modules here and moves with the
    interpreter, the sys.path entries and the number of editable installs. The
    floor is re-measured in the same interpreter each run so this asserts the
    thing that is actually ours.
    """
    floor = _importtime_modules("pass")
    total = _importtime_modules("import hermes_cli.auth")
    assert floor and total, "no importtime report parsed -- the measurement failed"

    cost = len(total) - len(floor)
    assert cost < MAX_AUTH_IMPORT_MODULES, (
        f"`import hermes_cli.auth` cost {cost} modules over the {len(floor)}-"
        f"module bare floor (ceiling {MAX_AUTH_IMPORT_MODULES}, pre-fix "
        f"baseline {BASELINE_AUTH_IMPORT_MODULES}). Something re-introduced a "
        "heavy module-scope import."
    )


@pytest.mark.timeout(900)
def test_httpx_attribute_still_resolves_to_the_real_module():
    """``hermes_cli.auth.httpx`` must still BE httpx, lazily.

    ``patch("hermes_cli.auth.httpx.Client")`` resolves the attribute off the
    module and then patches the real class; if this identity broke, those
    patches would silently target something else.
    """
    proc = _run(
        "import sys\n"
        "import hermes_cli.auth as auth\n"
        "assert 'httpx' not in sys.modules, 'httpx was imported eagerly'\n"
        "import httpx\n"
        "assert auth.httpx is httpx, 'auth.httpx is not the httpx module'\n"
        "print('SAME')\n"
    )
    assert proc.returncode == 0 and "SAME" in proc.stdout, (
        f"stdout:\n{proc.stdout}\nstderr tail:\n{proc.stderr[-3000:]}"
    )


@pytest.mark.timeout(900)
def test_whole_module_patch_of_auth_httpx_is_honoured():
    """``patch("hermes_cli.auth.httpx")`` must reach the call sites.

    This is the one that a naive fix breaks. Eight tests in
    test_auth_qwen_provider.py mock httpx this way; if ``_ensure_httpx()``
    imported directly instead of reading the module global, those tests would
    keep passing their assertions while the code underneath made real HTTP
    calls to a token endpoint.
    """
    proc = _run(
        "from unittest.mock import patch\n"
        "import hermes_cli.auth as auth\n"
        "with patch('hermes_cli.auth.httpx') as fake:\n"
        "    got = auth._ensure_httpx()\n"
        "    assert got is fake, 'whole-module patch was ignored: %r' % (got,)\n"
        "import httpx\n"
        "assert auth._ensure_httpx() is httpx, 'patch was not undone'\n"
        "print('HONOURED')\n"
    )
    assert proc.returncode == 0 and "HONOURED" in proc.stdout, (
        f"stdout:\n{proc.stdout}\nstderr tail:\n{proc.stderr[-3000:]}"
    )


@pytest.mark.timeout(900)
def test_attribute_patch_of_auth_httpx_is_honoured():
    """``patch("hermes_cli.auth.httpx.Client")`` must reach the call sites."""
    proc = _run(
        "from unittest.mock import patch\n"
        "import hermes_cli.auth as auth\n"
        "import httpx\n"
        "sentinel = object()\n"
        "with patch('hermes_cli.auth.httpx.Client', sentinel):\n"
        "    assert auth._ensure_httpx().Client is sentinel\n"
        "assert httpx.Client is not sentinel, 'patch was not undone'\n"
        "print('HONOURED')\n"
    )
    assert proc.returncode == 0 and "HONOURED" in proc.stdout, (
        f"stdout:\n{proc.stdout}\nstderr tail:\n{proc.stderr[-3000:]}"
    )


@pytest.mark.timeout(900)
def test_unknown_attribute_still_raises_attribute_error():
    """The PEP 562 hook must not swallow real typos.

    ``__getattr__`` intercepts every failed attribute lookup on the module, so
    a hook that returned something for any name would turn a misspelled
    function into a confusing runtime failure far from its cause.
    """
    proc = _run(
        "import hermes_cli.auth as auth\n"
        "try:\n"
        "    auth.no_such_attribute_at_all\n"
        "except AttributeError:\n"
        "    print('RAISED')\n"
        "assert getattr(auth, 'nope', 'default') == 'default'\n"
    )
    assert proc.returncode == 0 and "RAISED" in proc.stdout, (
        f"stdout:\n{proc.stdout}\nstderr tail:\n{proc.stderr[-3000:]}"
    )
