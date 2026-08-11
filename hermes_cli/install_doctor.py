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
