"""Regression: importing ``agent.account_usage`` must not import ``httpx``.

Why this exists
---------------
This is the FOURTH instance of the defect swept in
tests/hermes_cli/test_auth_import_cost.py, whose docstring calls itself "the
third and last". It was not the last: ``agent/account_usage.py`` carried
``import httpx`` at module scope and so silently undid ``hermes_cli.auth``'s
lazy-httpx work for every caller that reached httpx through this module.

What it cost, measured on PRECISION 2026-08-20
----------------------------------------------
``import ai_usage.__main__`` -- the entry point of the 5-minutely
``AIUsageCollector`` scheduled task -- spent 3.67 s of its 5.02 s total inside
httpx, i.e. 73%, at module scope, on every single run::

    import httpx           7.56 s        import httpx._client    4.91 s

so httpx's CLI subtree (``__init__`` runs ``from ._main import main``, dragging
in click, rich and pygments) was ~2.65 s of pure waste for four HTTP GETs.

After the fix::

    import ai_usage.__main__    5.02 s -> 0.88 s      395 -> 226 modules
    import agent.account_usage                        213 modules (148 over floor)

THE FAILURE THIS CAUSED -- why an import cost was a correctness bug
-------------------------------------------------------------------
``AIUsageCollector`` runs at Windows task ``Priority 7`` =
``BELOW_NORMAL_PRIORITY_CLASS``, which every child process inherits. The same
import measured **15.54 s at Normal against 475.12 s at BelowNormal** (30.6x,
both ``exit 0`` -- starvation, not a hang). 475 s is longer than the task's own
``PT6M`` ``ExecutionTimeLimit``, so the task was killed *during import* -- 145
times in 7 days, 43 in the last 48 h alone. ``main()`` never ran, no fetch was
ever attempted, and ``collect()``'s 90 s cooperative deadline never got the
chance to engage and carry previous values forward. The observable symptom was
a stale ``ai-tokens.json`` and a scheduler kill with no log line.

That is the point of this test: the module-scope import was not merely slow, it
made a bounded task unable to reach its own budget. Keeping the import cheap is
what lets ``collect()`` start and degrade gracefully instead of producing
nothing.

Why the fix is shaped the way it is
-----------------------------------
All six functions that touch httpx at runtime open with ``httpx =
_ensure_httpx()``; nothing names it at module scope. ``_ensure_httpx()`` reads
the module GLOBAL rather than doing a plain local ``import httpx``, and that is
load-bearing: ten sites in tests/agent/test_account_usage.py patch through the
attribute (``monkeypatch.setattr(account_usage.httpx, "Client", ...)``). A
function-local import would fetch the real httpx and sail straight past those
patches, turning mocked tests into live network calls against provider
endpoints. The PEP 562 ``__getattr__`` serves the attribute access.

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

# Every module that must stay out of a bare ``import agent.account_usage``.
# rich, click and pygments are httpx's CLI-extra companions: they are the
# loudest symptom, and naming them makes a regression's blast radius obvious.
FORBIDDEN = ("httpx", "rich", "click", "pygments", "requests", "urllib3")

# Modules OVER the bare interpreter floor (~65 here), the same method as the
# rest of this sweep -- see tests/hermes_cli/test_send_import_cost.py. Observed
# 148 after the fix; a re-introduced module-scope ``import httpx`` is worth
# ~169, so a 210 ceiling leaves real room to grow while still catching it.
MAX_IMPORT_MODULES = 210
BASELINE_IMPORT_MODULES = 317


def _child_env() -> dict:
    env = dict(os.environ)
    # Keep the child off the lazy-install path so a slow or absent optional dep
    # can never turn this into a pip run. See tools/lazy_deps.py.
    env["HERMES_DISABLE_LAZY_INSTALLS"] = "1"
    # Pin the checkout under test first: an editable install can otherwise
    # point ``agent`` at a different tree, and this would pass against code
    # that is not the code under test.
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
def test_importing_account_usage_does_not_import_httpx():
    proc = _run(
        "import sys\n"
        "import agent.account_usage  # noqa: F401\n"
        f"watch = {FORBIDDEN!r}\n"
        "print('OFFENDERS=[' + ','.join(m for m in watch if m in sys.modules) + ']')\n"
    )
    assert proc.returncode == 0, (
        f"importing agent.account_usage failed.\nstdout:\n{proc.stdout}\n"
        f"stderr tail:\n{proc.stderr[-3000:]}"
    )
    assert "OFFENDERS=[]" in proc.stdout, (
        f"agent.account_usage imported {proc.stdout.strip()} at module scope.\n"
        "The 5-minutely AIUsageCollector task imports this module and runs at "
        "BelowNormal priority, where the httpx import alone outran its whole "
        "ExecutionTimeLimit. Use `httpx = _ensure_httpx()` inside the function "
        "that needs it -- not a module-scope import, and not a bare "
        "function-local `import httpx` either (see the module docstring)."
    )


@pytest.mark.timeout(900)
def test_collector_entrypoint_does_not_import_httpx():
    """The thing the scheduled task actually runs must stay cheap too.

    ``agent.account_usage`` staying clean is necessary but not sufficient:
    ``ai_usage/__main__.py`` imports ``fetch_account_usage`` at module scope,
    so a heavy import re-introduced anywhere in that chain lands right back on
    the task's critical path.
    """
    proc = _run(
        "import sys\n"
        "import ai_usage.__main__  # noqa: F401\n"
        f"watch = {FORBIDDEN!r}\n"
        "print('OFFENDERS=[' + ','.join(m for m in watch if m in sys.modules) + ']')\n"
    )
    assert proc.returncode == 0, (
        f"importing ai_usage.__main__ failed.\nstdout:\n{proc.stdout}\n"
        f"stderr tail:\n{proc.stderr[-3000:]}"
    )
    assert "OFFENDERS=[]" in proc.stdout, (
        f"ai_usage.__main__ imported {proc.stdout.strip()} at module scope -- "
        "this is the AIUsageCollector task's entry point."
    )


@pytest.mark.timeout(900)
def test_account_usage_import_cost_stays_near_the_floor():
    """Cost is measured OVER the bare interpreter floor, not absolute.

    ``python -X importtime -c pass`` is ~65 modules here and moves with the
    interpreter, the sys.path entries and the number of editable installs. The
    floor is re-measured in the same interpreter each run so this asserts the
    thing that is actually ours.
    """
    floor = _importtime_modules("pass")
    total = _importtime_modules("import agent.account_usage")
    assert floor and total, "no importtime report parsed -- the measurement failed"

    cost = len(total) - len(floor)
    assert cost < MAX_IMPORT_MODULES, (
        f"`import agent.account_usage` cost {cost} modules over the "
        f"{len(floor)}-module bare floor (ceiling {MAX_IMPORT_MODULES}, "
        f"pre-fix baseline {BASELINE_IMPORT_MODULES}). Something "
        "re-introduced a heavy module-scope import."
    )


@pytest.mark.timeout(900)
def test_httpx_attribute_still_resolves_to_the_real_module():
    """``agent.account_usage.httpx`` must still BE httpx, lazily.

    Ten sites in tests/agent/test_account_usage.py resolve this attribute and
    then patch ``Client`` on it; if the identity broke, those patches would
    silently target something else and the mocked tests would go live.
    """
    proc = _run(
        "import sys\n"
        "import agent.account_usage as au\n"
        "assert 'httpx' not in sys.modules, 'httpx was imported eagerly'\n"
        "import httpx\n"
        "assert au.httpx is httpx, 'au.httpx is not the httpx module'\n"
        "print('SAME')\n"
    )
    assert proc.returncode == 0 and "SAME" in proc.stdout, (
        f"stdout:\n{proc.stdout}\nstderr tail:\n{proc.stderr[-3000:]}"
    )


@pytest.mark.timeout(900)
def test_whole_module_patch_of_account_usage_httpx_is_honoured():
    """``patch("agent.account_usage.httpx")`` must reach the call sites.

    This is the one a naive fix breaks: if ``_ensure_httpx()`` imported
    directly instead of reading the module global, a whole-attribute patch
    would be ignored and the code underneath would make real HTTP calls to
    provider endpoints while the test still passed.
    """
    proc = _run(
        "from unittest.mock import patch\n"
        "import agent.account_usage as au\n"
        "with patch('agent.account_usage.httpx') as fake:\n"
        "    got = au._ensure_httpx()\n"
        "    assert got is fake, 'whole-module patch was ignored: %r' % (got,)\n"
        "import httpx\n"
        "assert au._ensure_httpx() is httpx, 'patch was not undone'\n"
        "print('HONOURED')\n"
    )
    assert proc.returncode == 0 and "HONOURED" in proc.stdout, (
        f"stdout:\n{proc.stdout}\nstderr tail:\n{proc.stderr[-3000:]}"
    )


@pytest.mark.timeout(900)
def test_attribute_patch_of_account_usage_httpx_is_honoured():
    """``setattr(account_usage.httpx, "Client", ...)`` must reach the call sites.

    This is the exact style the ten existing sites in
    tests/agent/test_account_usage.py use.
    """
    proc = _run(
        "from unittest.mock import patch\n"
        "import agent.account_usage as au\n"
        "import httpx\n"
        "sentinel = object()\n"
        "with patch('agent.account_usage.httpx.Client', sentinel):\n"
        "    assert au._ensure_httpx().Client is sentinel\n"
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
        "import agent.account_usage as au\n"
        "try:\n"
        "    au.no_such_attribute_at_all\n"
        "except AttributeError:\n"
        "    print('RAISED')\n"
        "assert getattr(au, 'nope', 'default') == 'default'\n"
    )
    assert proc.returncode == 0 and "RAISED" in proc.stdout, (
        f"stdout:\n{proc.stdout}\nstderr tail:\n{proc.stderr[-3000:]}"
    )
