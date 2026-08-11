# Editable-Install Drift Guard Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

> **STATUS: EXECUTED 2026-08-11. This plan is now a historical record, not a
> specification.** Six task reviews and one end-to-end verification changed
> decisions after it was written, so its code blocks no longer match the
> shipped module. **`hermes_cli/install_doctor.py` is authoritative.** Known
> divergences: `analyze` gates the breadth skip on `if not declared:` (not
> `is None`); `Findings` has a sixth field `finder_is_stale`; `remedy_lines`
> takes a second parameter; `resolve_install_root` falls back to a usable root
> when the MAPPING will not parse; and the stated test counts are outdated
> (44 now). See the "Corrections" section of the design spec for why each
> changed.

**Goal:** Detect when the INSTALLED editable finder no longer exposes every package `pyproject.toml` declares, and name the reinstall remedy.

**Architecture:** A new standalone module `hermes_cli/install_doctor.py` (following the `hermes_cli/events_doctor.py` precedent) resolves the install root from the editable finder, reads that root's `pyproject.toml`, then spawns one subprocess from a freshly created empty directory to `find_spec` every declared name and import a short list of real entrypoints. A neutral cwd is the falsifier: from inside `agent-src` every package resolves via cwd and the drift is invisible. The MAPPING parse is used only to *explain* drift and is allowed to fail soft.

**Tech Stack:** Python 3.11 stdlib only — `tomllib`, `ast`, `re`, `importlib.util`, `subprocess`, `tempfile`, `dataclasses`. pytest for tests.

## Global Constraints

- **Python floor 3.11**, ceiling `<3.14` (`requires-python = ">=3.11,<3.14"`). `tomllib` is stdlib — do not add a `toml` dependency.
- **No new third-party dependencies.** Stdlib only.
- **Ruff `PLW1514` is enforced** (`preview = true`). Every `open()` / `read_text()` / `write_text()` in text mode MUST pass `encoding="utf-8"` explicitly. A bare call fails lint.
- **Whole Pyflakes `F` group is enforced locally.** No unused imports or names.
- **pytest per-test timeout is 30s** (`--timeout=30 --timeout-method=thread`). The probe subprocess in tests must stay well under that.
- **Tests must be hermetic.** No test may depend on whether the developer's machine has an editable install. Use synthetic fixtures and injected probe results; the only real subprocess allowed is one that probes stdlib names.
- **Never use `eval`.** Parse the finder MAPPING with `ast.literal_eval`.
- **Layer discipline:** the MAPPING parse is a *diagnosis* layer and fails soft. `find_spec` breadth + entrypoint import depth carry the verdict. A `None` from the parse means "no diagnosis available", never "no drift".
- **Windows paths.** MAPPING values contain backslash-escaped Windows paths. Fixtures must round-trip through `repr()` so they stay valid Python.

---

### Task 1: Declaration parsing and finder MAPPING parsing

The two pure functions everything else builds on. No I/O beyond reading a path the caller supplies.

**Files:**
- Create: `hermes_cli/install_doctor.py`
- Test: `tests/hermes_cli/test_install_doctor.py`

**Interfaces:**
- Consumes: nothing (first task)
- Produces:
  - `declared_names(pyproject_path: Path) -> set[str]`
  - `parse_finder_mapping(source: str) -> dict[str, str] | None`
  - `SMOKE_ENTRYPOINTS: tuple[str, ...]`

- [ ] **Step 1: Write the failing tests**

Create `tests/hermes_cli/test_install_doctor.py`:

```python
"""Hermetic tests for hermes_cli.install_doctor.

Every test here runs identically on a machine with no editable install.
The checker's LOGIC is what is under test; the environment assertion lives
in the doctor command, not in pytest. See
docs/superpowers/specs/2026-08-10-editable-finder-drift-guard-design.md.
"""

from pathlib import Path

from hermes_cli.install_doctor import (
    declared_names,
    parse_finder_mapping,
)

REPO_ROOT = Path(__file__).resolve().parents[2]

#: The five packages the installed finder was missing on 2026-08-10 while
#: pyproject declared all 23 correctly.
DRIFTED_ON_2026_08_10 = (
    "activity_policy",
    "activity_telemetry",
    "devflow_delegation",
    "jobflow_dispatch",
    "session_bridge",
)


def _finder_source(mapping: dict[str, str], *, annotated: bool = True) -> str:
    """Build a stand-in for a setuptools-generated editable finder."""
    decl = "MAPPING: dict[str, str] = " if annotated else "MAPPING = "
    return (
        "from __future__ import annotations\n"
        "import sys\n"
        f"{decl}{mapping!r}\n"
        "NAMESPACES: dict[str, list[str]] = {}\n"
        "def install():\n    pass\n"
    )


def test_declared_names_covers_packages_and_py_modules(tmp_path):
    pyproject = tmp_path / "pyproject.toml"
    pyproject.write_text(
        "[tool.setuptools]\n"
        'py-modules = ["hermes_constants", "utils"]\n'
        "[tool.setuptools.packages.find]\n"
        'include = ["events", "events.*", "jobflow_dispatch", "jobflow_dispatch.*"]\n',
        encoding="utf-8",
    )

    assert declared_names(pyproject) == {
        "events",
        "jobflow_dispatch",
        "hermes_constants",
        "utils",
    }


def test_declared_names_on_real_pyproject_includes_the_drifted_five():
    """Anchor on the real declaration list — it was always correct."""
    names = declared_names(REPO_ROOT / "pyproject.toml")
    for name in DRIFTED_ON_2026_08_10:
        assert name in names, f"{name} vanished from pyproject packages.find include"
    # py-modules must be covered too: they share the finder's MAPPING and
    # fail identically. hermes_constants is imported nearly everywhere.
    assert "hermes_constants" in names


def test_parse_finder_mapping_reads_the_annotated_form():
    source = _finder_source({"events": r"C:\repo\events"})
    assert parse_finder_mapping(source) == {"events": r"C:\repo\events"}


def test_parse_finder_mapping_reads_the_unannotated_form():
    """The `: dict[str, str]` annotation must be optional, not required.

    Matching a bare `MAPPING = ` against today's generated file returns None,
    which is why the annotation has to be in the pattern — but hard-requiring
    it would break the day setuptools drops it.
    """
    source = _finder_source({"events": r"C:\repo\events"}, annotated=False)
    assert parse_finder_mapping(source) == {"events": r"C:\repo\events"}


def test_parse_finder_mapping_returns_none_on_garbage():
    assert parse_finder_mapping("this is not a finder at all\n") is None


def test_parse_finder_mapping_returns_none_when_value_is_not_a_dict():
    assert parse_finder_mapping("MAPPING: dict[str, str] = ['nope']\n") is None


def test_parse_finder_mapping_ignores_the_namespaces_block():
    """NAMESPACES follows MAPPING in the real file and must not be captured."""
    source = _finder_source({"a": "/x", "b": "/y"})
    assert parse_finder_mapping(source) == {"a": "/x", "b": "/y"}


def test_smoke_entrypoints_include_the_regression_chain():
    from hermes_cli.install_doctor import SMOKE_ENTRYPOINTS

    assert "events.gateway_integration" in SMOKE_ENTRYPOINTS
```

- [ ] **Step 2: Run tests to verify they fail**

```bash
python -m pytest tests/hermes_cli/test_install_doctor.py -v
```

Expected: collection error — `ModuleNotFoundError: No module named 'hermes_cli.install_doctor'`.

- [ ] **Step 3: Write the minimal implementation**

Create `hermes_cli/install_doctor.py`:

```python
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
import re
import tomllib
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
    except (ValueError, SyntaxError):
        return None
    return value if isinstance(value, dict) else None
```

- [ ] **Step 4: Run tests to verify they pass**

```bash
python -m pytest tests/hermes_cli/test_install_doctor.py -v
```

Expected: 8 passed.

- [ ] **Step 5: Lint**

```bash
python -m ruff check --no-cache hermes_cli/install_doctor.py tests/hermes_cli/test_install_doctor.py
```

Expected: `All checks passed!`

- [ ] **Step 6: Commit**

```bash
git add hermes_cli/install_doctor.py tests/hermes_cli/test_install_doctor.py
git commit -m "feat(install-doctor): parse declared names and finder MAPPING"
```

---

### Task 2: Install-root resolution

Decide WHICH `pyproject.toml` is the source of truth. The install root's — not the cwd repo's — so running doctor from a worktree reports on the `agent-src` install rather than flagging phantom drift from worktree-only packages.

**Files:**
- Modify: `hermes_cli/install_doctor.py`
- Test: `tests/hermes_cli/test_install_doctor.py`

**Interfaces:**
- Consumes: `parse_finder_mapping(source: str) -> dict[str, str] | None` (Task 1)
- Produces:
  - `InstallRoot` frozen dataclass with fields `path: Path | None`, `provenance: str`, `mapping: dict[str, str] | None`
  - `find_editable_finder(search_roots: list[Path] | None = None) -> Path | None`
  - `resolve_install_root(finder: Path | None = None) -> InstallRoot`

- [ ] **Step 1: Write the failing tests**

Append to `tests/hermes_cli/test_install_doctor.py`:

```python
def test_find_editable_finder_picks_the_hermes_agent_finder(tmp_path):
    from hermes_cli.install_doctor import find_editable_finder

    (tmp_path / "__editable___hermes_agent_0_19_0_finder.py").write_text(
        "x = 1\n", encoding="utf-8"
    )
    (tmp_path / "__editable___hermes_hudui_0_3_1_finder.py").write_text(
        "x = 1\n", encoding="utf-8"
    )

    found = find_editable_finder([tmp_path])
    assert found is not None
    assert found.name == "__editable___hermes_agent_0_19_0_finder.py"


def test_find_editable_finder_returns_none_when_absent(tmp_path):
    from hermes_cli.install_doctor import find_editable_finder

    assert find_editable_finder([tmp_path]) is None


def test_resolve_install_root_uses_the_common_parent_of_mapping_targets(tmp_path):
    from hermes_cli.install_doctor import resolve_install_root

    root = tmp_path / "agent-src"
    finder = tmp_path / "__editable___hermes_agent_0_19_0_finder.py"
    finder.write_text(
        _finder_source(
            {
                "events": str(root / "events"),
                "jobflow_dispatch": str(root / "jobflow_dispatch"),
                "hermes_constants": str(root / "hermes_constants"),
            }
        ),
        encoding="utf-8",
    )

    resolved = resolve_install_root(finder)
    assert resolved.path == root
    assert resolved.mapping is not None
    assert "jobflow_dispatch" in resolved.mapping
    assert finder.name in resolved.provenance


def test_resolve_install_root_handles_a_single_mapping_entry(tmp_path):
    """commonpath over targets alone would return the package dir, not the root."""
    from hermes_cli.install_doctor import resolve_install_root

    root = tmp_path / "agent-src"
    finder = tmp_path / "__editable___hermes_agent_0_19_0_finder.py"
    finder.write_text(
        _finder_source({"events": str(root / "events")}), encoding="utf-8"
    )

    assert resolve_install_root(finder).path == root


def test_resolve_install_root_reports_an_unparseable_finder(tmp_path):
    from hermes_cli.install_doctor import resolve_install_root

    finder = tmp_path / "__editable___hermes_agent_0_19_0_finder.py"
    finder.write_text("garbage, no mapping here\n", encoding="utf-8")

    resolved = resolve_install_root(finder)
    assert resolved.path is None
    assert resolved.mapping is None
    assert "did not parse" in resolved.provenance


def test_resolve_install_root_falls_back_when_no_finder_exists(monkeypatch):
    """A wheel install has no finder; fall back to the running module's repo."""
    import hermes_cli.install_doctor as mod

    monkeypatch.setattr(mod, "find_editable_finder", lambda *a, **k: None)
    resolved = mod.resolve_install_root()

    assert resolved.mapping is None
    assert "no editable finder" in resolved.provenance
```

- [ ] **Step 2: Run tests to verify they fail**

```bash
python -m pytest tests/hermes_cli/test_install_doctor.py -v -k "finder or install_root"
```

Expected: FAIL with `ImportError: cannot import name 'find_editable_finder'`.

- [ ] **Step 3: Write the implementation**

Add to the imports at the top of `hermes_cli/install_doctor.py`:

```python
import os
import site
import sysconfig
from dataclasses import dataclass
```

Append to `hermes_cli/install_doctor.py`:

```python
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
```

- [ ] **Step 4: Run tests to verify they pass**

```bash
python -m pytest tests/hermes_cli/test_install_doctor.py -v
```

Expected: 14 passed.

- [ ] **Step 5: Lint and commit**

```bash
python -m ruff check --no-cache hermes_cli/install_doctor.py tests/hermes_cli/test_install_doctor.py
git add hermes_cli/install_doctor.py tests/hermes_cli/test_install_doctor.py
git commit -m "feat(install-doctor): resolve the install root from the finder MAPPING"
```

---

### Task 3: The neutral-cwd probe

The falsifier. One subprocess, empty cwd, `find_spec` every name and import every entrypoint.

**Files:**
- Modify: `hermes_cli/install_doctor.py`
- Test: `tests/hermes_cli/test_install_doctor.py`

**Interfaces:**
- Consumes: nothing from earlier tasks
- Produces:
  - `ProbeError(RuntimeError)`
  - `probe(names, entrypoints, python: str | None = None, env: dict | None = None) -> dict` returning
    `{"resolved": {name: {"ok": bool, "origin": str | None, "error": str | None}},
      "imports": {name: {"ok": bool, "error": str | None}},
      "executable": str}`

  The `env` seam exists so the guard can be proven end-to-end against the REAL
  interpreter and REAL import machinery (Task 6) by suppressing the user-site
  finder, rather than only against synthetic fixtures.

- [ ] **Step 1: Write the failing tests**

Append to `tests/hermes_cli/test_install_doctor.py`:

```python
def test_probe_resolves_present_and_missing_names():
    """Real subprocess, stdlib-only targets — hermetic on any machine."""
    from hermes_cli.install_doctor import probe

    result = probe(["json", "hermes_definitely_missing_xyz"], [])

    assert result["resolved"]["json"]["ok"] is True
    assert result["resolved"]["json"]["origin"]
    assert result["resolved"]["hermes_definitely_missing_xyz"]["ok"] is False
    assert result["executable"]


def test_probe_reports_a_failing_entrypoint_import():
    from hermes_cli.install_doctor import probe

    result = probe([], ["hermes_definitely_missing_xyz"])

    entry = result["imports"]["hermes_definitely_missing_xyz"]
    assert entry["ok"] is False
    assert "ModuleNotFoundError" in entry["error"]


def test_probe_runs_from_a_directory_that_is_not_the_repo(tmp_path):
    """The probe must not resolve names via the caller's cwd.

    A module dropped in the CALLER's cwd must be invisible to the probe --
    that is the whole falsifier. Run with cwd=tmp_path containing a decoy.
    """
    import os

    from hermes_cli.install_doctor import probe

    (tmp_path / "hermes_decoy_module.py").write_text("x = 1\n", encoding="utf-8")
    previous = os.getcwd()
    os.chdir(tmp_path)
    try:
        result = probe(["hermes_decoy_module"], [])
    finally:
        os.chdir(previous)

    assert result["resolved"]["hermes_decoy_module"]["ok"] is False, (
        "probe resolved a module from the caller's cwd — the neutral-cwd "
        "guarantee is broken and the guard would report false clean"
    )
```

- [ ] **Step 2: Run tests to verify they fail**

```bash
python -m pytest tests/hermes_cli/test_install_doctor.py -v -k probe
```

Expected: FAIL with `ImportError: cannot import name 'probe'`.

- [ ] **Step 3: Write the implementation**

Add to the imports at the top of `hermes_cli/install_doctor.py`:

```python
import json
import subprocess
import sys
import tempfile
```

Append to `hermes_cli/install_doctor.py`:

```python
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
```

- [ ] **Step 4: Run tests to verify they pass**

```bash
python -m pytest tests/hermes_cli/test_install_doctor.py -v
```

Expected: 17 passed. The three probe tests each spawn one interpreter; total well under the 30s per-test cap.

- [ ] **Step 5: Lint and commit**

```bash
python -m ruff check --no-cache hermes_cli/install_doctor.py tests/hermes_cli/test_install_doctor.py
git add hermes_cli/install_doctor.py tests/hermes_cli/test_install_doctor.py
git commit -m "feat(install-doctor): probe imports from a neutral cwd"
```

---

### Task 4: Analysis, remedy text, and rendering

Turn a probe result into a verdict plus output that names the remedy.

**Files:**
- Modify: `hermes_cli/install_doctor.py`
- Test: `tests/hermes_cli/test_install_doctor.py`

**Interfaces:**
- Consumes: `InstallRoot` (Task 2), the probe result shape (Task 3)
- Produces:
  - `Findings` frozen dataclass with fields `missing: tuple[str, ...]`, `broken_imports: tuple[tuple[str, str], ...]`, `diagnosis: str | None`, `notes: tuple[str, ...]`, `checked_breadth: bool`, and property `ok: bool`
  - `analyze(declared: set[str] | None, probe_result: dict, install_root: InstallRoot) -> Findings`
  - `remedy_lines(install_root: InstallRoot) -> list[str]`
  - `render(findings: Findings, install_root: InstallRoot, probe_result: dict) -> list[str]`

- [ ] **Step 1: Write the failing tests**

Append to `tests/hermes_cli/test_install_doctor.py`:

```python
def _probe_result(resolved_ok, resolved_missing=(), imports=None):
    return {
        "resolved": {
            **{n: {"ok": True, "origin": f"/root/{n}", "error": None} for n in resolved_ok},
            **{n: {"ok": False, "origin": None, "error": "not found"} for n in resolved_missing},
        },
        "imports": imports or {},
        "executable": "/usr/bin/python3.11",
    }


def _install_root_with(mapping, root=Path("/root")):
    from hermes_cli.install_doctor import InstallRoot

    return InstallRoot(path=root, provenance="test", mapping=mapping)


def test_analyze_reports_exactly_the_drifted_five():
    """The 18-of-23 case, reproduced against the checker."""
    from hermes_cli.install_doctor import analyze

    present = [f"pkg{i}" for i in range(18)]
    declared = set(present) | set(DRIFTED_ON_2026_08_10)
    mapping = {name: f"/root/{name}" for name in present}

    findings = analyze(
        declared,
        _probe_result(present, DRIFTED_ON_2026_08_10),
        _install_root_with(mapping),
    )

    assert findings.missing == tuple(sorted(DRIFTED_ON_2026_08_10))
    assert findings.ok is False
    assert findings.checked_breadth is True
    assert "18" in findings.diagnosis and "23" in findings.diagnosis


def test_analyze_is_clean_when_everything_resolves():
    from hermes_cli.install_doctor import analyze

    names = ["events", "jobflow_dispatch"]
    findings = analyze(
        set(names),
        _probe_result(names, imports={"events.gateway_integration": {"ok": True, "error": None}}),
        _install_root_with({n: f"/root/{n}" for n in names}),
    )

    assert findings.ok is True
    assert findings.missing == ()
    assert findings.broken_imports == ()


def test_analyze_catches_a_broken_chain_when_breadth_is_clean():
    """The depth layer must fire even when every top-level resolves."""
    from hermes_cli.install_doctor import analyze

    findings = analyze(
        {"events"},
        _probe_result(
            ["events"],
            imports={
                "events.gateway_integration": {
                    "ok": False,
                    "error": "ModuleNotFoundError: No module named 'jobflow_dispatch'",
                }
            },
        ),
        _install_root_with({"events": "/root/events"}),
    )

    assert findings.missing == ()
    assert findings.ok is False
    assert findings.broken_imports[0][0] == "events.gateway_integration"
    assert "jobflow_dispatch" in findings.broken_imports[0][1]


def test_analyze_still_reports_drift_when_the_diagnosis_is_unavailable():
    """An unparseable finder degrades the explanation, never the verdict."""
    from hermes_cli.install_doctor import analyze

    findings = analyze(
        {"events", "jobflow_dispatch"},
        _probe_result(["events"], ["jobflow_dispatch"]),
        _install_root_with(None),
    )

    assert findings.missing == ("jobflow_dispatch",)
    assert findings.ok is False
    assert findings.diagnosis is None


def test_analyze_skips_breadth_without_declarations_and_says_so():
    """A sealed wheel install has no source tree to diff against."""
    from hermes_cli.install_doctor import analyze

    findings = analyze(
        None,
        _probe_result([], imports={"events.gateway_integration": {"ok": True, "error": None}}),
        _install_root_with(None, root=None),
    )

    assert findings.checked_breadth is False
    assert findings.ok is True
    assert any("no pyproject" in note.lower() for note in findings.notes)


def test_remedy_names_the_command_the_root_and_the_console_script_trap():
    from hermes_cli.install_doctor import remedy_lines

    root_path = Path("/agent-src")
    text = "\n".join(remedy_lines(_install_root_with({}, root=root_path)))

    assert "pip install -e . --no-deps" in text
    # Compare against str(Path(...)), NOT the literal "/agent-src":
    # remedy_lines renders str(path), which is "\agent-src" on Windows.
    assert str(root_path) in text
    assert "WinError 32" in text
    assert "python -m hermes_cli.main" in text


def test_render_failure_output_contains_the_remedy():
    from hermes_cli.install_doctor import analyze, render

    root = _install_root_with({"events": "/root/events"})
    result = _probe_result(["events"], ["jobflow_dispatch"])
    findings = analyze({"events", "jobflow_dispatch"}, result, root)

    text = "\n".join(render(findings, root, result))

    assert "jobflow_dispatch" in text
    assert "pip install -e . --no-deps" in text
    assert "/usr/bin/python3.11" in text
```

- [ ] **Step 2: Run tests to verify they fail**

```bash
python -m pytest tests/hermes_cli/test_install_doctor.py -v -k "analyze or remedy or render"
```

Expected: FAIL with `ImportError: cannot import name 'analyze'`.

- [ ] **Step 3: Write the implementation**

Append to `hermes_cli/install_doctor.py`:

```python
@dataclass(frozen=True)
class Findings:
    """The verdict, plus everything needed to explain it."""

    missing: tuple[str, ...]
    broken_imports: tuple[tuple[str, str], ...]
    diagnosis: str | None
    notes: tuple[str, ...]
    checked_breadth: bool

    @property
    def ok(self) -> bool:
        return not self.missing and not self.broken_imports


def analyze(
    declared: set[str] | None,
    probe_result: dict,
    install_root: InstallRoot,
) -> Findings:
    """Turn a probe result into a verdict.

    ``declared`` is None when no root yielded a readable pyproject.toml (a
    sealed wheel install). Breadth is then SKIPPED with an explicit note
    rather than reported as a pass — there is nothing to have drifted from,
    and an unqualified "clean" would overstate what was checked. Depth still
    runs, because importing the entrypoints is meaningful on any install.
    """
    resolved = probe_result.get("resolved", {})
    notes: list[str] = []

    if declared is None:
        checked_breadth = False
        missing: list[str] = []
        notes.append(
            "Breadth check SKIPPED: no pyproject.toml was readable at the "
            "resolved install root, so there is no declaration list to diff "
            "against. Import checks still ran."
        )
    else:
        checked_breadth = True
        missing = sorted(
            name
            for name in declared
            if not resolved.get(name, {}).get("ok", False)
        )

    broken = tuple(
        (name, entry.get("error") or "import failed")
        for name, entry in sorted(probe_result.get("imports", {}).items())
        if not entry.get("ok", False)
    )

    diagnosis = None
    if missing and install_root.mapping is not None and declared is not None:
        held = len(set(install_root.mapping) & declared)
        diagnosis = (
            f"The editable finder holds {held} of the {len(declared)} declared "
            "names. It is generated ONCE at install time and is never "
            "regenerated as packages are added, so it drifts silently every "
            "time a new top-level package lands."
        )

    return Findings(
        missing=tuple(missing),
        broken_imports=broken,
        diagnosis=diagnosis,
        notes=tuple(notes),
        checked_breadth=checked_breadth,
    )


def remedy_lines(install_root: InstallRoot) -> list[str]:
    """The fix, the interpreter that must apply it, and the trap that blocks it."""
    where = str(install_root.path) if install_root.path else "the agent-src checkout"
    return [
        "Remedy — regenerate the finder by reinstalling the editable package:",
        "",
        f"    cd {where}",
        "    pip install -e . --no-deps",
        "",
        "Use the interpreter that OWNS the install (the one this ran under):",
        f"    {sys.executable}",
        "",
        "If pip fails with WinError 32 (file in use): a console-script wrapper",
        "stays resident as the PARENT of its python process, so a gateway",
        "started via hermes.exe holds the install open. Stop it and relaunch",
        "as a module instead, which avoids the wrapper entirely:",
        "",
        "    python -m hermes_cli.main gateway",
    ]


def render(findings: Findings, install_root: InstallRoot, probe_result: dict) -> list[str]:
    """Human-readable report lines."""
    lines: list[str] = [
        f"install root : {install_root.provenance}",
        f"interpreter  : {probe_result.get('executable', sys.executable)}",
    ]
    for note in findings.notes:
        lines.append(f"note         : {note}")

    if findings.ok:
        if findings.checked_breadth:
            lines.append("[OK] every declared package resolves from a neutral cwd")
        else:
            lines.append("[OK] import checks passed (breadth not checked — see note)")
        return lines

    if findings.missing:
        lines.append(
            f"[FAIL] {len(findings.missing)} declared name(s) do NOT resolve from a "
            "neutral cwd:"
        )
        lines.extend(f"         - {name}" for name in findings.missing)
    for name, error in findings.broken_imports:
        lines.append(f"[FAIL] import {name} failed: {error}")

    if findings.diagnosis:
        lines.extend(["", f"Why: {findings.diagnosis}"])
    lines.extend(["", *remedy_lines(install_root)])
    return lines
```

- [ ] **Step 4: Run tests to verify they pass**

```bash
python -m pytest tests/hermes_cli/test_install_doctor.py -v
```

Expected: 24 passed.

- [ ] **Step 5: Lint and commit**

```bash
python -m ruff check --no-cache hermes_cli/install_doctor.py tests/hermes_cli/test_install_doctor.py
git add hermes_cli/install_doctor.py tests/hermes_cli/test_install_doctor.py
git commit -m "feat(install-doctor): analyze drift and name the reinstall remedy"
```

---

### Task 5: CLI entrypoint and `hermes doctor` wiring

Two surfaces over one core, with different failure conventions.

**Files:**
- Modify: `hermes_cli/install_doctor.py`
- Modify: `hermes_cli/doctor.py:752-754`
- Test: `tests/hermes_cli/test_install_doctor.py`

**Interfaces:**
- Consumes: `analyze`, `render`, `resolve_install_root`, `declared_names`, `probe`, `SMOKE_ENTRYPOINTS`
- Produces:
  - `run(probe_fn=probe, root: InstallRoot | None = None, stream=None) -> int` — 0 clean, 1 drift
  - `doctor_section_lines(probe_fn=probe, root: InstallRoot | None = None) -> tuple[list[tuple[str, str, str]], str | None]` — rows of `(status, text, detail)` where status is `"ok" | "warn" | "fail"`, plus an optional remediation string

- [ ] **Step 1: Write the failing tests**

Append to `tests/hermes_cli/test_install_doctor.py`:

```python
def test_run_returns_zero_when_clean(tmp_path):
    from hermes_cli.install_doctor import InstallRoot, run

    root_dir = tmp_path / "agent-src"
    root_dir.mkdir()
    (root_dir / "pyproject.toml").write_text(
        "[tool.setuptools.packages.find]\ninclude = [\"events\", \"events.*\"]\n",
        encoding="utf-8",
    )
    root = InstallRoot(path=root_dir, provenance="test", mapping={"events": "x"})

    def fake_probe(names, entrypoints, python=None):
        return _probe_result(list(names), imports={e: {"ok": True, "error": None} for e in entrypoints})

    assert run(probe_fn=fake_probe, root=root, stream=[]) == 0


def test_run_returns_one_on_drift_and_prints_the_remedy(tmp_path):
    from hermes_cli.install_doctor import InstallRoot, run

    root_dir = tmp_path / "agent-src"
    root_dir.mkdir()
    (root_dir / "pyproject.toml").write_text(
        "[tool.setuptools.packages.find]\n"
        'include = ["events", "jobflow_dispatch"]\n',
        encoding="utf-8",
    )
    root = InstallRoot(path=root_dir, provenance="test", mapping={"events": "x"})

    def fake_probe(names, entrypoints, python=None):
        return _probe_result(["events"], ["jobflow_dispatch"])

    out = []
    assert run(probe_fn=fake_probe, root=root, stream=out) == 1
    text = "\n".join(out)
    assert "jobflow_dispatch" in text
    assert "pip install -e . --no-deps" in text


def test_doctor_section_lines_yields_a_remediation_on_drift(tmp_path):
    from hermes_cli.install_doctor import InstallRoot, doctor_section_lines

    root_dir = tmp_path / "agent-src"
    root_dir.mkdir()
    (root_dir / "pyproject.toml").write_text(
        "[tool.setuptools.packages.find]\n"
        'include = ["events", "jobflow_dispatch"]\n',
        encoding="utf-8",
    )
    root = InstallRoot(path=root_dir, provenance="test", mapping={"events": "x"})

    def fake_probe(names, entrypoints, python=None):
        return _probe_result(["events"], ["jobflow_dispatch"])

    rows, remediation = doctor_section_lines(probe_fn=fake_probe, root=root)

    assert any(status == "fail" for status, _, _ in rows)
    assert remediation and "pip install -e . --no-deps" in remediation


def test_doctor_section_lines_is_ok_when_clean(tmp_path):
    from hermes_cli.install_doctor import InstallRoot, doctor_section_lines

    root_dir = tmp_path / "agent-src"
    root_dir.mkdir()
    (root_dir / "pyproject.toml").write_text(
        "[tool.setuptools.packages.find]\ninclude = [\"events\"]\n", encoding="utf-8"
    )
    root = InstallRoot(path=root_dir, provenance="test", mapping={"events": "x"})

    def fake_probe(names, entrypoints, python=None):
        return _probe_result(list(names), imports={e: {"ok": True, "error": None} for e in entrypoints})

    rows, remediation = doctor_section_lines(probe_fn=fake_probe, root=root)

    assert remediation is None
    assert all(status != "fail" for status, _, _ in rows)
```

- [ ] **Step 2: Run tests to verify they fail**

```bash
python -m pytest tests/hermes_cli/test_install_doctor.py -v -k "run_returns or doctor_section"
```

Expected: FAIL with `ImportError: cannot import name 'run'`.

- [ ] **Step 3: Implement `run` and `doctor_section_lines`**

Add `import argparse` to the imports at the top of `hermes_cli/install_doctor.py`, then append:

```python
def _collect(probe_fn, root: InstallRoot | None):
    """Shared core for both surfaces: resolve, declare, probe, analyze."""
    root = root if root is not None else resolve_install_root()

    declared: set[str] | None = None
    if root.path is not None:
        pyproject = root.path / "pyproject.toml"
        if pyproject.is_file():
            declared = declared_names(pyproject)

    probe_result = probe_fn(sorted(declared) if declared else [], SMOKE_ENTRYPOINTS)
    return root, analyze(declared, probe_result, root), probe_result


def run(probe_fn=probe, root: InstallRoot | None = None, stream=None) -> int:
    """Standalone surface. Returns 0 when clean, 1 on drift.

    Exit-code semantics match events_doctor, so a laptop-monitor probe or CI
    step can call this directly.
    """
    root, findings, probe_result = _collect(probe_fn, root)
    lines = render(findings, root, probe_result)
    if stream is None:
        for line in lines:
            print(line)
    else:
        stream.extend(lines)
    return 0 if findings.ok else 1


def doctor_section_lines(probe_fn=probe, root: InstallRoot | None = None):
    """`hermes doctor` surface.

    Does NOT exit on drift — doctor renders the section and funnels a
    remediation line into its existing summary block alongside every other
    finding.
    """
    root, findings, probe_result = _collect(probe_fn, root)

    rows: list[tuple[str, str, str]] = []
    if findings.ok:
        if findings.checked_breadth:
            rows.append(("ok", "Installed packages match pyproject declarations", ""))
        else:
            rows.append(("warn", "Breadth not checked", findings.notes[0] if findings.notes else ""))
    else:
        if findings.missing:
            rows.append((
                "fail",
                f"{len(findings.missing)} declared package(s) missing from the install",
                ", ".join(findings.missing),
            ))
        for name, error in findings.broken_imports:
            rows.append(("fail", f"import {name} failed", error))
        if findings.diagnosis:
            rows.append(("warn", findings.diagnosis, ""))

    remediation = None
    if not findings.ok:
        remediation = "Editable install has drifted. " + " ".join(
            line.strip() for line in remedy_lines(root) if line.strip()
        )
    return rows, remediation


def _cli() -> None:
    argparse.ArgumentParser(description=__doc__).parse_args()
    sys.exit(run())


if __name__ == "__main__":
    _cli()
```

- [ ] **Step 4: Run tests to verify they pass**

```bash
python -m pytest tests/hermes_cli/test_install_doctor.py -v
```

Expected: 28 passed.

- [ ] **Step 5: Wire into `hermes doctor`**

In `hermes_cli/doctor.py`, find this exact block (around line 750-754):

```python
    # Detect drift between pyproject.toml and hermes_cli/__init__.py versions
    # (a git conflict resolution can silently revert one but not the other).
    _check_version_consistency(issues)

    _section("SSL / CA Certificates")
```

Insert a new section between `_check_version_consistency(issues)` and `_section("SSL / CA Certificates")`:

```python
    # Detect drift between pyproject.toml and hermes_cli/__init__.py versions
    # (a git conflict resolution can silently revert one but not the other).
    _check_version_consistency(issues)

    _section("Install Integrity")
    try:
        from hermes_cli.install_doctor import doctor_section_lines

        rows, remediation = doctor_section_lines()
        for status, text, detail in rows:
            if status == "ok":
                check_ok(text, detail)
            elif status == "warn":
                check_warn(text, detail)
            else:
                check_fail(text, detail)
        if remediation:
            manual_issues.append(remediation)
    except Exception as e:
        # Never let a bug in the install check block the rest of doctor.
        check_warn(f"Install integrity check failed: {e}")

    _section("SSL / CA Certificates")
```

- [ ] **Step 6: Verify the standalone CLI runs on this machine**

```bash
python -m hermes_cli.install_doctor
```

Expected: exit 0, and output naming the install root as `C:\Users\diego\.hermes\agent-src` plus `[OK] every declared package resolves from a neutral cwd`. The drift was repaired before this work started, so a clean result here is correct — Task 6 is what proves detection.

- [ ] **Step 7: Verify `hermes doctor` renders the section**

```bash
python -m hermes_cli.main doctor
```

Expected: an "Install Integrity" section appears after "Python Environment", showing the OK row, and doctor completes normally.

- [ ] **Step 8: Lint and commit**

```bash
python -m ruff check --no-cache hermes_cli/install_doctor.py hermes_cli/doctor.py tests/hermes_cli/test_install_doctor.py
git add hermes_cli/install_doctor.py hermes_cli/doctor.py tests/hermes_cli/test_install_doctor.py
git commit -m "feat(install-doctor): add CLI entrypoint and hermes doctor section"
```

---

### Task 6: Prove detection against a deliberately drifted real finder

Every test so far uses synthetic fixtures. This task proves the guard fires against the real generated artifact shape — the spec requires it before this is called done.

**Files:**
- No production changes. Verification only, plus a recorded result.

**Interfaces:**
- Consumes: `find_editable_finder`, `resolve_install_root`, `run` (Task 5)

- [ ] **Step 1: Copy the real finder and delete five entries from its MAPPING**

This writes ONLY into a scratch directory. It must not modify site-packages.

```bash
python - <<'PY'
import re, shutil, tempfile
from pathlib import Path
from hermes_cli.install_doctor import find_editable_finder, parse_finder_mapping

real = find_editable_finder()
print("real finder:", real)
src = real.read_text(encoding="utf-8")
mapping = parse_finder_mapping(src)
print("real mapping entries:", len(mapping))

drifted = {k: v for k, v in mapping.items() if k not in {
    "activity_policy", "activity_telemetry", "devflow_delegation",
    "jobflow_dispatch", "session_bridge",
}}
print("drifted mapping entries:", len(drifted))

out = Path(tempfile.mkdtemp(prefix="drift-demo-")) / real.name
new_src = re.sub(
    r"^MAPPING\s*(?::\s*dict\[[^\]]*\])?\s*=\s*\{.*?\}\s*$",
    "MAPPING: dict[str, str] = " + repr(drifted),
    src, count=1, flags=re.MULTILINE | re.DOTALL,
)
out.write_text(new_src, encoding="utf-8")
print("wrote:", out)
PY
```

Expected: `real mapping entries: 37`, `drifted mapping entries: 32`, and a path to the copy.

- [ ] **Step 2: Confirm the diagnosis layer reads the real artifact**

Substitute the path printed above for `<DRIFTED>`:

```bash
python -c "from pathlib import Path; from hermes_cli.install_doctor import resolve_install_root; r = resolve_install_root(Path(r'<DRIFTED>')); print(r.provenance); print('entries:', len(r.mapping)); print('absent:', sorted({'activity_policy','activity_telemetry','devflow_delegation','jobflow_dispatch','session_bridge'} - set(r.mapping)))"
```

Expected: the root resolves to `C:\Users\diego\.hermes\agent-src`, `entries: 32`, and all five names listed as absent.

This proves `resolve_install_root` and the MAPPING parse handle the real generated file — not a fixture. It does **not** prove the breadth layer, because breadth runs against the live interpreter, where the drift has already been repaired and all five genuinely resolve. Step 3 is what proves that layer.

- [ ] **Step 3: Prove the breadth and depth layers fire against the real interpreter**

`PYTHONNOUSERSITE=1` suppresses the user-site editable finder, reproducing an interpreter that genuinely cannot see the packages — the same condition the drift created, using real import machinery rather than fixtures.

```bash
python - <<'PY'
import os
from pathlib import Path
from hermes_cli.install_doctor import (
    SMOKE_ENTRYPOINTS, analyze, declared_names, probe, render, resolve_install_root,
)

root = resolve_install_root()
declared = declared_names(root.path / "pyproject.toml")
blind = {**os.environ, "PYTHONNOUSERSITE": "1"}

result = probe(sorted(declared), SMOKE_ENTRYPOINTS, env=blind)
findings = analyze(declared, result, root)

print("declared:", len(declared))
print("missing :", len(findings.missing))
print("broken  :", [n for n, _ in findings.broken_imports])
print("ok      :", findings.ok)
print()
print("\n".join(render(findings, root, result)))
PY
```

Expected: `declared: 37`, a non-zero `missing` count covering the hermes packages, `broken: ['events.gateway_integration']`, `ok: False`, and rendered output containing the `pip install -e . --no-deps` remedy.

This is the real proof: the guard detects a package-less install through the actual interpreter, and the depth layer independently catches `events.gateway_integration` — the exact chain that took down all 13 subscribers.

- [ ] **Step 4: Confirm the guard is clean against the unmodified environment**

```bash
python -m hermes_cli.install_doctor
```

Expected: exit 0. Together with Step 3, this shows the guard distinguishes a healthy install from a broken one rather than always reporting one verdict.

- [ ] **Step 5: Run the full new test file plus the existing packaging tests**

```bash
python -m pytest tests/hermes_cli/test_install_doctor.py tests/test_packaging_metadata.py -v
```

Expected: all pass. The existing packaging tests must be untouched — this guard is additive and covers a different property.

- [ ] **Step 6: Clean up the scratch copy**

Substitute the parent directory of the path printed in Step 1:

```bash
python -c "import shutil; shutil.rmtree(r'<DRIFTED_PARENT>')"
```

- [ ] **Step 7: Commit the verification note**

Append a "Verified 2026-08-10" section to the spec recording the observed numbers: 37 real MAPPING entries, 32 after removing the five, and the `PYTHONNOUSERSITE` run's missing count and `broken: ['events.gateway_integration']`. Then:

```bash
git add -f docs/superpowers/specs/2026-08-10-editable-finder-drift-guard-design.md
git commit -m "docs(spec): record drift-guard verification against the real finder"
```

Note the `-f`: `docs/superpowers/*` is gitignored from an inherited upstream merge, but this fork tracks specs deliberately.

---

## Self-Review

**Spec coverage:**

| Spec requirement | Task |
|---|---|
| `declared_names` incl. py-modules | 1 |
| MAPPING regex tolerant of missing annotation, `ast.literal_eval` | 1 |
| `SMOKE_ENTRYPOINTS` with commented rationale | 1 |
| Install root from finder MAPPING, fallback to hermes_cli repo root | 2 |
| Always report which root was used | 2 (`provenance`), rendered in 4 |
| One subprocess, fresh empty temp dir, no `-P` | 3 |
| Breadth layer (`find_spec` all declared) | 3 + 4 |
| Depth layer (entrypoint import) | 3 + 4 |
| Diagnosis layer fails soft | 4 (`test_analyze_still_reports_drift_when_the_diagnosis_is_unavailable`) |
| Sealed-wheel case skips breadth with explicit note | 4 (`test_analyze_skips_breadth_without_declarations_and_says_so`) |
| Remedy names command, root, interpreter, WinError 32 trap | 4 |
| Standalone exit 0/1; doctor appends to `issues` instead | 5 |
| `hermes doctor` section wrapped in try/except | 5 |
| Hermetic fixture tests (18/23, no-annotation, garbage, py-modules, remedy text) | 1, 4 |
| Demo against a deliberately drifted real finder | 6 |

No gaps.

**Placeholder scan:** No `TBD`/`TODO`/"similar to Task N" anywhere. Two defects found and fixed during this review rather than shipped:

1. Task 4 Step 3 originally contained a mangled inline `diagnosis` expression that a follow-up step "corrected". That is broken code in a plan, not a two-step edit — Step 3 now carries the correct `held = len(set(install_root.mapping) & declared)` form and the extra step is gone.
2. Task 6 originally asserted the guard would exit 1 against a drifted finder copy, then contradicted itself in a note admitting the five packages still resolve on this repaired machine. Rewritten: Step 2 proves only what it can (the MAPPING parse reads the real artifact — 32 of 37), and a new Step 3 proves the breadth and depth layers for real by running the probe with `PYTHONNOUSERSITE=1`, which required adding the `env` seam to `probe` in Task 3.

**Type consistency:** `InstallRoot(path, provenance, mapping)` is constructed identically in Tasks 2, 4, 5, and 6. `Findings(missing, broken_imports, diagnosis, notes, checked_breadth)` is constructed once in `analyze` and read in `render` and `doctor_section_lines` with matching field names. The probe result shape `{"resolved", "imports", "executable"}` is produced by `_PROBE_SRC` (Task 3) and consumed by the `_probe_result` test helper and `analyze` (Task 4) with the same inner keys — `ok`, `origin`, `error`. The real `probe` is `(names, entrypoints, python=None, env=None)`; `_collect` calls `probe_fn` with two positional arguments only, so the `(names, entrypoints, python=None)` test fakes in Task 5 remain compatible.
