"""hermes install-doctor — detect drift between DECLARED and INSTALLED packages.

``pyproject.toml`` declares the packages the project ships. The editable
finder in site-packages is a SEPARATE artifact, generated once at install
time and never regenerated as packages are added. Those are different
properties, and until this module only the first was tested.

On 2026-08-10 the installed finder held 18 of the 23 declared top-level
packages. ``events/subscribers/jobflow_dispatcher.py`` imports
``jobflow_dispatch`` at module level and ``events/gateway_integration.py``
imports that subscriber at module level, so from any cwd outside agent-src
the chain raised ModuleNotFoundError and took down all 13 event-bus
subscribers. The running gateway was healthy only because it happened to
have been launched from agent-src.

THE FALSIFIER IS A NEUTRAL CWD. From inside agent-src every package
resolves via cwd and the drift is invisible.

This lives in a doctor command rather than a pytest test on purpose: it
asserts a property of the developer's ENVIRONMENT, not of the repo. As a
test it would have to skip for anyone who has not reinstalled, and would
then silently no-op for every developer and in CI — checking the property
only where it already holds. The checker's logic is covered instead by
tests/hermes_cli/test_install_doctor.py, which is hermetic and never skips.

Design: docs/superpowers/specs/2026-08-10-editable-finder-drift-guard-design.md
"""

from __future__ import annotations

import ast
import json
import os
import re
import site
import subprocess
import sys
import sysconfig
import tempfile
import tomllib
from dataclasses import dataclass
from pathlib import Path

#: Import chains whose MODULE-LEVEL imports must survive a neutral cwd.
#: Breadth (find_spec over every declared name) cannot catch a chain that
#: breaks below the top level, which is exactly how the 2026-08-10 outage
#: presented: `events` itself resolved fine.
SMOKE_ENTRYPOINTS: tuple[str, ...] = (
    # events.gateway_integration -> events/subscribers/jobflow_dispatcher.py
    # -> jobflow_dispatch. This is the chain that took down all 13
    # subscribers, and it is what a reboot-time gateway launch executes.
    "events.gateway_integration",
)

# The `: dict[str, str]` annotation is present in the setuptools that
# generated the installs we ship against — matching a bare `MAPPING = `
# against those files returns None. It is optional here rather than
# required so both shapes parse if setuptools ever drops it.
# Anchored at line start so the trailing NAMESPACES block cannot match.
_MAPPING_RE = re.compile(
    r"^MAPPING\s*(?::\s*dict\[[^\]]*\])?\s*=\s*(\{.*?\})\s*$",
    re.MULTILINE | re.DOTALL,
)


def declared_names(pyproject_path: Path) -> set[str]:
    """Top-level names a pyproject declares: packages AND py-modules.

    Both land in the finder's MAPPING and both fail identically when the
    finder is stale, so both belong in the breadth check. On this project
    that is 23 packages + 14 py-modules = the 37 MAPPING entries observed.
    """
    data = tomllib.loads(pyproject_path.read_text(encoding="utf-8"))
    setuptools_cfg = data.get("tool", {}).get("setuptools", {})
    include = setuptools_cfg.get("packages", {}).get("find", {}).get("include", [])

    names: set[str] = set()
    for entry in include:
        # Collapses both "events" and "events.*" to "events".
        top = entry.split(".", 1)[0].strip()
        if top:
            names.add(top)
    names.update(setuptools_cfg.get("py-modules", []))
    return names


def parse_finder_mapping(source: str) -> dict[str, str] | None:
    """Extract MAPPING from a setuptools-generated editable finder.

    Returns None when the generated format has drifted. Callers MUST treat
    None as "no diagnosis available", never as "no drift" — this parse is
    coupled to setuptools' generated-file layout and is the one layer
    allowed to fail soft. The verdict comes from find_spec breadth and
    entrypoint import depth, which are mechanism-independent.
    """
    match = _MAPPING_RE.search(source)
    if match is None:
        return None
    try:
        value = ast.literal_eval(match.group(1))
    except Exception:
        # Any failure of ast.literal_eval must degrade to None, never raise.
        # The regex only validates braces; it cannot rule out unhashable dict
        # keys (which raise TypeError), malformed structures, or other parse
        # errors. The diagnosis layer is coupled to setuptools' generated-file
        # layout; failures here just mean "no explanation available", and the
        # verdict comes from find_spec breadth and entrypoint import depth.
        # BaseException (KeyboardInterrupt, SystemExit) is still allowed to
        # propagate.
        return None
    return value if isinstance(value, dict) else None


@dataclass(frozen=True)
class InstallRoot:
    """Where the install points, and how we worked that out.

    ``path`` is the source root whose pyproject.toml is authoritative for
    "declared". ``provenance`` is always rendered, so it is never ambiguous
    which install was graded. ``mapping`` is None whenever the diagnosis
    layer is unavailable.
    """

    path: Path | None
    provenance: str
    mapping: dict[str, str] | None


def find_editable_finder(search_roots: list[Path] | None = None) -> Path | None:
    """Locate the hermes-agent editable finder in site-packages.

    User site-packages is searched first: that is where a `pip install -e .`
    without a venv actually lands on the WindowsApps interpreter.
    """
    if search_roots is None:
        search_roots = []
        try:
            search_roots.append(Path(site.getusersitepackages()))
        except Exception:  # pragma: no cover - platform-dependent
            pass
        try:
            search_roots.extend(Path(p) for p in site.getsitepackages())
        except Exception:  # pragma: no cover - virtualenv without the attr
            pass
        search_roots.append(Path(sysconfig.get_paths()["purelib"]))

    for root in search_roots:
        try:
            matches = sorted(root.glob("__editable___hermes_agent_*_finder.py"))
        except OSError:
            continue
        if matches:
            return matches[-1]
    return None


def resolve_install_root(finder: Path | None = None) -> InstallRoot:
    """Resolve the root whose pyproject.toml is authoritative for 'declared'.

    The INSTALL root, deliberately — not the cwd repo's. That keeps the
    semantics self-consistent ("does the installed environment expose
    everything the INSTALLED project declares") and stops a worktree from
    reporting drift for packages that only exist on its own branch, which a
    reinstall from agent-src would not fix anyway.
    """
    if finder is None:
        finder = find_editable_finder()

    if finder is None:
        fallback = Path(__file__).resolve().parents[1]
        has_pyproject = (fallback / "pyproject.toml").is_file()
        return InstallRoot(
            path=fallback if has_pyproject else None,
            provenance=(
                "no editable finder found (wheel install?); falling back to the "
                f"repo root of the running hermes_cli: {fallback}"
                + ("" if has_pyproject else " — which has no pyproject.toml")
            ),
            mapping=None,
        )

    mapping = parse_finder_mapping(finder.read_text(encoding="utf-8"))
    if not mapping:
        return InstallRoot(
            path=None,
            provenance=(
                f"editable finder {finder.name} found, but its MAPPING did not "
                "parse — setuptools' generated format may have changed"
            ),
            mapping=None,
        )

    # Parents, not the targets themselves: commonpath over a single target
    # would return the package directory rather than the root.
    root = Path(os.path.commonpath([str(Path(t).parent) for t in mapping.values()]))
    return InstallRoot(
        path=root,
        provenance=f"editable finder {finder.name} -> {root}",
        mapping=mapping,
    )


class ProbeError(RuntimeError):
    """The probe subprocess did not return usable JSON."""


# Runs in a SEPARATE interpreter with an empty cwd. Keep it dependency-free
# and self-contained: it cannot import anything from this package, because
# importing this package is part of what is under test.
_PROBE_SRC = r"""
import importlib, importlib.util, json, sys

names = json.loads(sys.argv[1])
entrypoints = json.loads(sys.argv[2])

resolved = {}
for name in names:
    try:
        spec = importlib.util.find_spec(name)
    except Exception as exc:
        resolved[name] = {"ok": False, "origin": None,
                          "error": "%s: %s" % (type(exc).__name__, exc)}
        continue
    if spec is None:
        resolved[name] = {"ok": False, "origin": None, "error": "not found"}
    else:
        origin = spec.origin
        if origin is None and spec.submodule_search_locations:
            origin = list(spec.submodule_search_locations)[0]
        resolved[name] = {"ok": True, "origin": origin, "error": None}

imports = {}
for name in entrypoints:
    try:
        importlib.import_module(name)
    except Exception as exc:
        imports[name] = {"ok": False, "error": "%s: %s" % (type(exc).__name__, exc)}
    else:
        imports[name] = {"ok": True, "error": None}

json.dump({"resolved": resolved, "imports": imports,
           "executable": sys.executable}, sys.stdout)
"""


def probe(names, entrypoints, python: str | None = None, env: dict | None = None) -> dict:
    """Resolve ``names`` and import ``entrypoints`` from a NEUTRAL cwd.

    The subprocess runs with cwd set to a freshly created EMPTY directory --
    not %TEMP% itself, so a stray module left there cannot perturb the
    result.

    Deliberately NOT run with ``-P``: a real reboot-time launch DOES prepend
    cwd to sys.path, and reproducing that faithfully is the entire point. An
    empty cwd supplies the neutral-cwd falsifier without diverging from how
    the gateway actually starts. Run this from inside agent-src instead and
    every package resolves via cwd, hiding the drift completely.

    Uses ``sys.executable`` -- the interpreter that invoked doctor, which is
    the install being graded. The returned "executable" is rendered so it is
    never ambiguous which environment was checked.

    ``env`` overrides the subprocess environment. It exists so the guard can
    be proven end-to-end against the real interpreter (see Task 6): setting
    PYTHONNOUSERSITE=1 suppresses the user-site editable finder and
    reproduces a genuinely package-less install, which is the only way to
    exercise the breadth and depth layers for real rather than via fixtures.
    """
    python = python or sys.executable
    with tempfile.TemporaryDirectory(prefix="hermes-install-doctor-") as neutral:
        proc = subprocess.run(
            [
                python,
                "-c",
                _PROBE_SRC,
                json.dumps(sorted(names)),
                json.dumps(list(entrypoints)),
            ],
            cwd=neutral,
            env=env,
            capture_output=True,
            text=True,
            timeout=120,
        )

    if proc.returncode != 0 or not proc.stdout.strip():
        raise ProbeError(
            f"probe subprocess failed (exit {proc.returncode}): "
            f"{(proc.stderr or '').strip()[:500]}"
        )
    try:
        return json.loads(proc.stdout)
    except json.JSONDecodeError as exc:
        raise ProbeError(f"probe returned non-JSON output: {exc}") from exc
