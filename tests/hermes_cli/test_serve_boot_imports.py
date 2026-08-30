"""Import-graph budget for the headless backend boot.

A profiled `hermes serve` (isolated HERMES_HOME, `-X importtime`) spends
**1310 ms of its 1770 ms** spawn→``HERMES_BACKEND_READY`` window importing
modules — 74% of the boot. The desktop cannot show a window until that
sentinel lands, so anything that joins this import graph is paid by every
launch on every machine, forever.

The two largest components are load-bearing and deliberately left alone:

  · ``hermes_cli.web_server`` (368 ms) — the app itself, 317 routes.
  · ``tui_gateway.server`` (250 ms) — imported before the bind for the
    flush-on-SIGTERM handlers (#94724). It redirects ``sys.stdout`` →
    ``sys.stderr`` at import time, which is why
    ``_write_machine_sentinel_line`` writes to fd 1 directly (#96282).
    Deferring it past the READY write would change stdout state exactly
    when the desktop is parsing fd 1 for the port sentinel — a boot the
    desktop declares dead, not merely a slow one.

What this test guards is the *third* category: modules that join the graph
by accident. Measured marginally (importing them on top of everything a
headless serve already needs), today's CLI-parser-registration tail costs
43 ms — not the 152 ms its cumulative profile time suggests, because it
shares most of its dependencies with serve. That margin is too small to
justify restructuring argparse registration; it is not too small to be
worth stopping from growing.

Deliberately counts modules rather than milliseconds: wall-clock import
time varies with page cache, CPU governor and disk, and a timing assertion
here would flake in CI while telling you less. A module count is exact, and
growth in it is what actually drives the milliseconds.
"""

from __future__ import annotations

import json
import subprocess
import sys

# Measured: 731 modules after importing web_server plus the lifespan's
# _warm_gateway_module set. Headroom for ordinary feature work; a jump past
# this means something sizeable joined the boot graph and wants a look.
MAX_BOOT_MODULES = 800

# Never on the backend boot graph. Each is heavy, and none is needed to bind
# a socket — if one appears, a module-scope import crept in where a
# function-scope one belongs.
FORBIDDEN_BOOT_IMPORTS = (
    "matplotlib",
    "numpy",
    "pandas",
    "playwright",
    "PIL",
    "scipy",
    "torch",
    "transformers",
)

# The lifespan warms these before the socket accepts (see
# `_warm_gateway_module`), so they are part of the real boot cost and belong
# in the measurement.
_PROBE = """
import json, sys
import hermes_cli.web_server  # noqa: F401
for _mod in (
    "hermes_cli.gateway",
    "hermes_cli.auth",
    "hermes_cli.copilot_auth",
    "hermes_cli.runtime_provider",
    "hermes_cli.skin_engine",
    "hermes_cli.inventory",
    "hermes_cli.model_switch",
):
    try:
        __import__(_mod)
    except Exception:
        pass
print(json.dumps({"count": len(sys.modules), "modules": sorted(sys.modules)}))
"""


def _boot_graph() -> dict:
    """Import graph of a headless backend boot, in a clean interpreter.

    Must be a subprocess: pytest has already imported much of the tree, so
    an in-process ``len(sys.modules)`` would measure the test session.
    """
    result = subprocess.run(
        [sys.executable, "-c", _PROBE],
        capture_output=True,
        text=True,
        timeout=300,
    )

    assert result.returncode == 0, f"boot probe failed:\n{result.stderr[-4000:]}"

    return json.loads(result.stdout.strip().splitlines()[-1])


def test_backend_boot_import_graph_stays_within_budget():
    graph = _boot_graph()

    assert graph["count"] <= MAX_BOOT_MODULES, (
        f"backend boot now imports {graph['count']} modules (budget {MAX_BOOT_MODULES}). "
        "74% of the spawn→READY window is imports, so this is startup latency for "
        "every user. Prefer a function-scope import over a module-scope one, or "
        "raise the budget with a note saying what grew."
    )


def test_backend_boot_pulls_no_heavyweight_libraries():
    loaded = set(_boot_graph()["modules"])

    leaked = sorted(name for name in FORBIDDEN_BOOT_IMPORTS if name in loaded)

    assert not leaked, (
        f"{', '.join(leaked)} reached the backend boot graph. None is needed to bind "
        "the serve socket — move the import inside the function that uses it."
    )
