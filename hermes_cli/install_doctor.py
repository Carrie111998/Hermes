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

import argparse
import ast
import importlib.util
import json
import os
import re
import shutil
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


def _finder_version_key(path: Path) -> tuple[int, ...]:
    """Numeric version from a generated finder filename, for ordering.

    Sorting these as strings ranks 0_9_0 after 0_10_0, which would select a
    stale older finder once a double-digit minor exists.
    """
    match = re.search(r"__editable___hermes_agent_(.+?)_finder\.py$", path.name)
    if not match:
        return ()
    return tuple(int(part) for part in match.group(1).split("_") if part.isdigit())


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
            matches = list(root.glob("__editable___hermes_agent_*_finder.py"))
        except OSError:
            continue
        if matches:
            return max(matches, key=_finder_version_key)
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
        # `if not mapping` (not `is None`) on purpose: os.path.commonpath([])
        # raises ValueError on an empty dict, so an empty mapping must also
        # take this branch. Only the DIAGNOSIS degrades here -- `path` still
        # falls back to the running module's repo root, same as the
        # no-finder branch, so `declared` stays resolvable and the breadth
        # verdict is never silently skipped just because the MAPPING parse
        # failed. See Finding A in the 2026-08-10 final-review report.
        fallback = Path(__file__).resolve().parents[1]
        has_pyproject = (fallback / "pyproject.toml").is_file()
        return InstallRoot(
            path=fallback if has_pyproject else None,
            provenance=(
                f"editable finder {finder.name} found, but its MAPPING did not "
                "parse (setuptools' generated format may have changed) — "
                f"falling back to {fallback} for declarations"
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
        try:
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
                encoding="utf-8",
                timeout=120,
            )
        except subprocess.TimeoutExpired as exc:
            raise ProbeError(
                f"probe subprocess timed out after {exc.timeout} seconds"
            ) from exc

    if proc.returncode != 0 or not proc.stdout.strip():
        raise ProbeError(
            f"probe subprocess failed (exit {proc.returncode}): "
            f"{(proc.stderr or '').strip()[:500]}"
        )
    try:
        return json.loads(proc.stdout)
    except json.JSONDecodeError as exc:
        raise ProbeError(f"probe returned non-JSON output: {exc}") from exc


@dataclass(frozen=True)
class Findings:
    """The verdict, plus everything needed to explain it.

    ``finder_is_stale`` disambiguates the two, differently-caused ways a
    declared name can fail to resolve: ``None`` when no determination was
    possible (no MAPPING parsed, no declarations, or nothing missing);
    ``True`` when the finder omits at least one missing name (the finder
    itself is out of date and reinstalling regenerates it); ``False`` when
    the finder already LISTS every missing name (the finder is complete but
    is not being loaded at all -- reinstalling regenerates a finder that is
    already correct and fixes nothing).
    """

    missing: tuple[str, ...]
    broken_imports: tuple[tuple[str, str], ...]
    diagnosis: str | None
    finder_is_stale: bool | None
    notes: tuple[str, ...]
    checked_breadth: bool

    @property
    def ok(self) -> bool:
        """True when nothing was found wrong.

        NOTE: also True when breadth was SKIPPED. A caller reporting a clean
        bill of health must consult `checked_breadth` as well — `ok` alone
        cannot distinguish "checked and clean" from "did not check".
        """
        return not self.missing and not self.broken_imports


def analyze(
    declared: set[str] | None,
    probe_result: dict,
    install_root: InstallRoot,
) -> Findings:
    """Turn a probe result into a verdict.

    ``declared`` is None when no root yielded a readable pyproject.toml (a
    sealed wheel install), and an empty set when a pyproject WAS readable but
    declared no top-level names (e.g. the list form of ``packages =`` rather
    than ``[tool.setuptools.packages.find].include``). Either way Breadth is
    then SKIPPED with an explicit note rather than reported as a pass — there
    is nothing to have drifted from, and an unqualified "clean" would
    overstate what was checked. Depth still runs, because importing the
    entrypoints is meaningful on any install.
    """
    resolved = probe_result.get("resolved") or {}
    notes: list[str] = []

    if not declared:
        checked_breadth = False
        missing: list[str] = []
        if declared is None:
            notes.append(
                "Breadth check SKIPPED: no pyproject.toml was readable at the "
                "resolved install root, so there is no declaration list to diff "
                "against. Import checks still ran."
            )
        else:
            notes.append(
                "Breadth check SKIPPED: the pyproject.toml at the resolved "
                "install root declared no top-level names. That usually means "
                "it does not use [tool.setuptools.packages.find].include, so "
                "there is nothing to diff against. Import checks still ran."
            )
    else:
        checked_breadth = True
        # Breadth is defined as a diff against `declared`: probe entries for
        # names the probe happened to resolve but that are not in `declared`
        # are intentionally not reported here.
        missing = sorted(
            name
            for name in declared
            if not resolved.get(name, {}).get("ok", False)
        )

    broken = tuple(
        (name, entry.get("error") or "import failed")
        for name, entry in sorted((probe_result.get("imports") or {}).items())
        if not entry.get("ok", False)
    )

    finder_is_stale: bool | None = None
    diagnosis = None
    if missing and install_root.mapping is not None and declared is not None:
        absent_from_finder = [name for name in missing if name not in install_root.mapping]
        finder_is_stale = bool(absent_from_finder)
        if finder_is_stale:
            held = len(set(install_root.mapping) & declared)
            diagnosis = (
                f"The editable finder holds {held} of the {len(declared)} declared "
                "names. It is generated ONCE at install time and is never "
                "regenerated as packages are added, so it drifts silently every "
                "time a new top-level package lands."
            )
        else:
            diagnosis = (
                "The editable finder LISTS every name that failed to resolve, so "
                "it is not stale — it is not being loaded at all. Usual causes: a "
                "different interpreter than the one that owns the install, user "
                "site-packages disabled (PYTHONNOUSERSITE=1 or python -s), or a "
                "deleted .pth file. Reinstalling will NOT fix this."
            )

    return Findings(
        missing=tuple(missing),
        broken_imports=broken,
        diagnosis=diagnosis,
        finder_is_stale=finder_is_stale,
        notes=tuple(notes),
        checked_breadth=checked_breadth,
    )


def _pip_is_importable() -> bool:
    """Whether the interpreter running this check can run ``-m pip``.

    Split out as a named function so tests can stub it — importing pip for
    real would bind the assertion to whatever created the developer's venv.
    ``find_spec`` rather than ``import pip`` keeps this cheap and avoids
    executing pip's package body just to answer a yes/no.
    """
    try:
        return importlib.util.find_spec("pip") is not None
    except Exception:
        # A broken/partial pip (ImportError from a mangled .pth, ValueError
        # from a namespace shim) means "cannot run -m pip", not a crash of
        # the whole doctor section.
        return False


def _quote(value: str) -> str:
    """Quote a path only when it needs it, so the command stays copy-pasteable."""
    return f'"{value}"' if " " in value else value


def reinstall_command(python: str | None = None) -> str:
    """The editable-reinstall command that will actually RUN here.

    Neither form is universally right, so this detects rather than
    hardcodes:

    * ``python -m pip install -e . --no-deps`` when the interpreter that
      owns the install has pip.
    * ``uv pip install -e . --no-deps --python <interpreter>`` when it does
      not. uv does not install pip into the environments it creates, so on
      a uv-made ``.venv`` the pip form fails immediately with "No module
      named pip" — observed 2026-08-12 on a uv-created agent-src ``.venv``,
      where doctor's hardcoded pip text was unrunnable as printed.

    ``--python`` is not decoration: without it uv resolves an environment
    from the cwd, which need not be the one that owns this install. A bare
    ``pip`` from PATH is never emitted as a fallback — on Windows that
    resolves to the scoop/MSIX Python and would install into an entirely
    different interpreter.
    """
    python = python or sys.executable
    if _pip_is_importable():
        return f"{_quote(python)} -m pip install -e . --no-deps"
    uv = shutil.which("uv") or "uv"
    return f"{_quote(uv)} pip install -e . --no-deps --python {_quote(python)}"


def _reinstall_notes(python: str | None = None) -> list[str]:
    """The traps that go with whichever form ``reinstall_command`` picked."""
    if _pip_is_importable():
        return []
    notes = [
        "This interpreter has no pip — uv does not install one into the",
        "environments it creates — so the uv form above is the one that runs.",
        "`--python` is NOT optional: without it uv resolves an environment",
        "from the cwd, which need not be the one that owns this install.",
        "Do NOT substitute a bare `pip` from PATH: that belongs to a",
        "different interpreter (scoop/MSIX Python on Windows) and would",
        "install into the wrong environment entirely.",
    ]
    if shutil.which("uv") is None:
        notes.extend([
            "",
            "uv is not on PATH here. Install it (https://astral.sh/uv), or",
            f"bootstrap pip into this interpreter with `{_quote(python or sys.executable)}"
            " -m ensurepip` and use the pip form instead.",
        ])
    return notes


def remedy_lines(install_root: InstallRoot, finder_is_stale: bool | None = True) -> list[str]:
    """The fix, the interpreter that must apply it, and the trap that blocks it.

    ``finder_is_stale`` picks which remedy applies. ``True`` or ``None``
    (finder genuinely out of date, or no determination was possible) get the
    reinstall block below — ``None`` defaults to reinstall because that is
    the right default when undetermined. ``False`` (the finder already lists
    every missing name — it is complete but not being loaded) gets a
    different remedy entirely; telling that caller to reinstall would send
    them down a path that cannot work, since reinstalling regenerates a
    finder that is already correct.
    """
    where = str(install_root.path) if install_root.path else "the agent-src checkout"
    if finder_is_stale is False:
        return [
            "Remedy — the editable finder is already complete, so reinstalling",
            "will NOT fix this. It is not being LOADED by the interpreter that",
            "ran this check:",
            "",
            f"    {sys.executable}",
            "",
            "The finder file was found and its MAPPING was read successfully — the",
            "problem is that this interpreter never imports it. Usual causes:",
            "",
            "  - A different interpreter than the one that owns the install is",
            "    running (check which `python` / `py` resolves to at the call",
            "    site, e.g. in a service or Scheduled Task definition).",
            "  - User site-packages is disabled: PYTHONNOUSERSITE=1 is set in the",
            "    environment, or the interpreter was launched with `python -s`.",
            "    A scrubbed environment (Windows service, Scheduled Task) is a",
            "    common way for this to happen without anyone intending it.",
            "  - The .pth file that points site-packages at the editable finder",
            "    was deleted or never installed for this interpreter.",
            "",
            f"Confirm by checking whether {where} appears on sys.path for the",
            "interpreter above, and whether its site-packages actually loads",
            "user site (site.ENABLE_USER_SITE).",
        ]
    lines = [
        "Remedy — regenerate the finder by reinstalling the editable package:",
        "",
        f"    cd {where}",
        f"    {reinstall_command()}",
        "",
        "Use the interpreter that OWNS the install (the one this ran under):",
        f"    {sys.executable}",
    ]
    notes = _reinstall_notes()
    if notes:
        lines.extend(["", *notes])
    lines.extend([
        "",
        "If the install fails with WinError 32 (file in use): a console-script wrapper",
        "stays resident as the PARENT of its python process, so a gateway",
        "started via hermes.exe holds the install open. Stop it and relaunch",
        "as a module instead, which avoids the wrapper entirely:",
        "",
        "    python -m hermes_cli.main gateway",
    ])
    return lines


def render(findings: Findings, install_root: InstallRoot, probe_result: dict) -> list[str]:
    """Human-readable report lines."""
    lines: list[str] = [
        f"install root : {install_root.provenance}",
        f"interpreter  : {probe_result.get('executable') or sys.executable}",
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
    lines.extend(["", *remedy_lines(install_root, findings.finder_is_stale)])
    return lines


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
        remediation = _remediation_one_liner(root, findings.finder_is_stale)
    return rows, remediation


def _remediation_one_liner(install_root: InstallRoot, finder_is_stale: bool | None) -> str:
    """A short, actionable remediation for the doctor summary block.

    `doctor_section_lines` used to join the entire `remedy_lines` block
    (bullets, blank lines, an indented command) with spaces into one
    ~1140-character line, which doctor renders as a numbered summary item
    that wraps to ~14 terminal lines among one-line neighbours. This stays
    a one-liner and points at the standalone tool for the full remedy.

    It must also not contradict `finder_is_stale`: when the finder is
    already complete (False), nothing "drifted" -- the problem is that this
    interpreter never loads it.
    """
    if finder_is_stale is False:
        return (
            f"Editable finder is complete but not loaded by this interpreter "
            f"({sys.executable}) — reinstalling will not fix this. Full "
            "remedy: python -m hermes_cli.install_doctor"
        )
    where = str(install_root.path) if install_root.path else "the agent-src checkout"
    return (
        f"Editable install has drifted: run `{reinstall_command()}` from "
        f"{where}. Full remedy: python -m hermes_cli.install_doctor"
    )


def _cli() -> None:
    argparse.ArgumentParser(description=__doc__).parse_args()
    sys.exit(run())


if __name__ == "__main__":
    _cli()
