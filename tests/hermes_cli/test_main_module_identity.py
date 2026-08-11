"""`hermes_cli.main` must be ONE module object per process.

Every Hermes launcher — the Windows scheduled task, the systemd unit, the
gateway respawn paths — invokes ``python -m hermes_cli.main``. runpy executes
main.py as ``__main__`` and does NOT populate ``sys.modules["hermes_cli.main"]``,
so the first lazy ``from hermes_cli.main import ...`` downstream used to
re-execute the whole 15k-line module body under a second module object.

That was observed live on 2026-08-11: ``gateway.code_skew._fingerprint()``,
called from ``start_gateway()``'s first statement, re-ran the body — including
``_apply_profile_override()`` — inside an already-booted gateway, and forked
every module-level global in main.py into two copies that silently disagree.

main.py now publishes ``__main__`` under its real name. The invariant is only
observable from a real ``-m`` launch, so this test spawns one.
"""

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]

# Injected via PYTHONPATH. `site` imports sitecustomize during interpreter
# startup, so this runs before runpy touches hermes_cli — and the atexit hook
# fires long after main.py's module body has finished, which is exactly when
# the real downstream importers (code_skew, plugins, web_server) run.
_PROBE = '''
import atexit, json, os, sys, time

_out = os.environ.get("HERMES_MAIN_IDENTITY_PROBE")


def _check():
    result = {"cached_before_import": "hermes_cli.main" in sys.modules}
    t0 = time.perf_counter()
    import hermes_cli.main as m

    result["reimport_seconds"] = round(time.perf_counter() - t0, 4)
    result["same_object_as_main"] = m is sys.modules.get("__main__")
    result["module_file"] = getattr(m, "__file__", None)
    with open(_out, "w", encoding="utf-8") as fh:
        json.dump(result, fh)


if _out:
    atexit.register(_check)
'''


@pytest.mark.timeout(300)  # overrides the global --timeout=30: a real CLI boot
def test_dash_m_launch_leaves_exactly_one_main_module(tmp_path):
    probe_dir = tmp_path / "probe"
    probe_dir.mkdir()
    (probe_dir / "sitecustomize.py").write_text(_PROBE, encoding="utf-8")
    result_path = tmp_path / "identity.json"

    env = dict(os.environ)
    env["PYTHONPATH"] = str(probe_dir)
    env["HERMES_MAIN_IDENTITY_PROBE"] = str(result_path)
    # Keep the spawned CLI off the developer's real profile and out of its logs.
    env["HERMES_HOME"] = str(tmp_path / "home")
    env["HERMES_GATEWAY_EXIT_DIAG"] = "0"

    # cwd is the repo root so `-m` resolves hermes_cli from this checkout rather
    # than from whatever the editable install happens to point at.
    proc = subprocess.run(
        [sys.executable, "-m", "hermes_cli.main", "version"],
        cwd=str(REPO_ROOT),
        env=env,
        capture_output=True,
        text=True,
        timeout=240,
    )
    assert proc.returncode == 0, f"stdout={proc.stdout!r} stderr={proc.stderr!r}"
    assert result_path.exists(), (
        "sitecustomize probe never ran; "
        f"stdout={proc.stdout!r} stderr={proc.stderr!r}"
    )

    data = json.loads(result_path.read_text(encoding="utf-8"))

    assert data["cached_before_import"], (
        "sys.modules['hermes_cli.main'] was empty after a `-m hermes_cli.main` "
        "run — the next lazy import re-executes the whole module body"
    )
    assert data["same_object_as_main"], (
        "`import hermes_cli.main` returned a DIFFERENT module object than the "
        "running __main__: module-level state in main.py is split in two"
    )
    assert Path(data["module_file"]).resolve() == (
        REPO_ROOT / "hermes_cli" / "main.py"
    ).resolve()


def test_alias_is_scoped_to_the_dash_m_launch():
    """A plain `import hermes_cli.main` must not be perturbed by the alias.

    The guard keys on ``__spec__``/``__name__``, so importing the module the
    ordinary way (tests, `from hermes_cli.main import ...`) leaves it named
    ``hermes_cli.main`` and distinct from whatever ``__main__`` happens to be.
    """
    import hermes_cli.main as m

    assert m.__name__ == "hermes_cli.main"
    assert m is not sys.modules["__main__"]
    assert sys.modules["hermes_cli.main"] is m
