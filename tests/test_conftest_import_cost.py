"""Regression: the autouse ``_reset_module_state`` fixture must not force heavy
imports during fixture setup.

Why this exists
---------------
``_reset_module_state`` (tests/conftest.py) clears process-global state between
tests. Each of its blocks used to do a plain ``from X import Y`` — which
*imports* the module purely in order to reset it. The worst offender was
``from agent import auxiliary_client``: it pulled ~950 modules (including the
whole ``openai`` type tree, 454 modules) into every pytest process during the
FIRST test's fixture setup.

``pyproject.toml`` sets ``--timeout=30``, and that cap covers fixture setup, so
on a cold filesystem the first test of a file was killed before its body ran,
with a faulthandler stack ending at the ``from agent import auxiliary_client``
line. Measured on a dev box: 22.4s of fixture setup for a file whose 25 tests
are pure string assertions.

The fixture's own docstring already stated the correct invariant — "Modules
that don't exist yet (test collection before production import) are skipped
silently — production import later creates fresh empty state." A module that
was never imported has no leaked state to clear, so the reset must be
*conditional on the module already being in* ``sys.modules`` rather than
importing it. That is both faster and semantically identical.

This test pins the invariant. It runs pytest in a SUBPROCESS on purpose:
``sys.modules`` is process-global, so a sibling test in this process that
legitimately imports ``openai`` would mask the regression entirely.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]

# Heavy trees that a trivial test must never drag in. These are exactly the
# modules the autouse fixture used to import unconditionally (plus ``openai``,
# which ``agent.auxiliary_client`` force-loads via the codex compat guard).
FORBIDDEN_PREFIXES = (
    "openai",
    "agent.auxiliary_client",
    "agent.credential_pool",
    "agent.model_metadata",
)

# Written to a temp dir and loaded with ``-p import_probe`` in the child run.
_PROBE_PLUGIN = '''\
import json
import os
import sys


def pytest_sessionfinish(session, exitstatus):
    with open(os.environ["IMPORT_PROBE_OUT"], "w", encoding="utf-8") as fh:
        json.dump(sorted(sys.modules), fh)
'''


def test_canary_noop():
    """Trivial subprocess target for the regression test below.

    Must stay dependency-free — it exists so the child run exercises conftest's
    autouse fixtures and nothing else.
    """
    assert True


@pytest.mark.timeout(300)
def test_autouse_reset_fixture_does_not_import_heavy_modules(tmp_path):
    """A trivial test must not pull the openai/auxiliary-client tree into the
    process via ``_reset_module_state``."""
    plugin_dir = tmp_path / "probe"
    plugin_dir.mkdir()
    (plugin_dir / "import_probe.py").write_text(_PROBE_PLUGIN, encoding="utf-8")
    probe_out = tmp_path / "modules.json"

    env = dict(os.environ)
    existing_pythonpath = env.get("PYTHONPATH")
    env["PYTHONPATH"] = (
        str(plugin_dir) + os.pathsep + existing_pythonpath
        if existing_pythonpath
        else str(plugin_dir)
    )
    env["IMPORT_PROBE_OUT"] = str(probe_out)
    # Keep the child off the lazy-install path so a slow/absent optional dep
    # can never turn this into a pip run. See tools/lazy_deps.py.
    env["HERMES_DISABLE_LAZY_INSTALLS"] = "1"

    nodeid = f"tests/{Path(__file__).name}::test_canary_noop"
    proc = subprocess.run(
        [
            sys.executable,
            "-m",
            "pytest",
            nodeid,
            "-p",
            "import_probe",
            "-p",
            "no:cacheprovider",
            "-q",
            "--no-header",
        ],
        cwd=str(REPO_ROOT),
        env=env,
        capture_output=True,
        text=True,
        timeout=240,
    )

    assert probe_out.exists(), (
        "probe plugin never wrote its output — the child pytest run did not "
        f"reach sessionfinish.\nstdout:\n{proc.stdout}\nstderr:\n{proc.stderr}"
    )
    loaded = json.loads(probe_out.read_text(encoding="utf-8"))

    offenders = sorted(
        {
            mod
            for mod in loaded
            for prefix in FORBIDDEN_PREFIXES
            if mod == prefix or mod.startswith(prefix + ".")
        }
    )
    assert not offenders, (
        f"A trivial test imported {len(offenders)} module(s) it does not need. "
        "The autouse _reset_module_state fixture must reset only modules that "
        "are ALREADY in sys.modules, never import them.\n"
        f"First offenders: {offenders[:12]}"
    )

    # The child ran under the real 30s-per-test cap from pyproject addopts.
    # If the fixture regresses, it dies there too — surface that clearly.
    assert proc.returncode == 0, (
        f"child pytest run failed (exit {proc.returncode}).\n"
        f"stdout:\n{proc.stdout}\nstderr:\n{proc.stderr}"
    )
