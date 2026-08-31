"""Every session `info` payload must advertise `desktop_contract`.

The desktop feeds `info.desktop_contract` straight into
`reportBackendContract()`, and a MISSING field is not neutral there — it is
read as contract 0, which is how the GUI detects a backend too old to report
one at all. So an `info` dict that merely forgets the key makes a perfectly
current backend announce "Backend out of date" to the user.

This has now been fixed three times in three different payloads (#36112 in
session.create, #68392 in the activate fallback, and the live-unpersisted
resume path), each time with a test covering only the path that broke. The
recurring part is not any one path — it is that `info` gets hand-rolled
inline instead of built, and a hand-rolled one drops fields. So this test
guards the SHAPE across the whole gateway rather than adding a fourth
one-off: any inline `"info": {...}` literal has to carry the key, and any
builder that returns one has to as well.

Fixing a failure here: prefer calling an existing builder
(`_lazy_resume_info`, `_fallback_session_info`, `_session_info`) over adding
the single key back — the partial dict is usually missing cwd/branch/project
too, and those fail quietly instead of loudly.
"""

from __future__ import annotations

import ast
from pathlib import Path

GATEWAY = Path(__file__).resolve().parents[1] / "tui_gateway"

# Builders whose whole job is to return a session `info` payload. A call to one
# of these in an `"info":` slot is trusted, because the builder itself is
# checked below.
INFO_BUILDERS = {"_fallback_session_info", "_lazy_resume_info", "_session_info"}

CONTRACT_KEY = "desktop_contract"


def _dict_keys(node: ast.Dict) -> set[str]:
    return {k.value for k in node.keys if isinstance(k, ast.Constant) and isinstance(k.value, str)}


def _inline_info_dicts() -> list[tuple[str, int, ast.Dict]]:
    """Every `"info": { ... }` literal in the gateway, with where it lives."""
    found: list[tuple[str, int, ast.Dict]] = []

    for path in sorted(GATEWAY.glob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))

        for node in ast.walk(tree):
            if not isinstance(node, ast.Dict):
                continue

            for key, value in zip(node.keys, node.values):
                is_info_key = isinstance(key, ast.Constant) and key.value == "info"

                if is_info_key and isinstance(value, ast.Dict):
                    found.append((path.name, value.lineno, value))

    return found


def test_inline_info_payloads_advertise_the_backend_contract():
    missing = [
        f"{name}:{lineno}"
        for name, lineno, node in _inline_info_dicts()
        if CONTRACT_KEY not in _dict_keys(node)
    ]

    assert not missing, (
        "These inline session `info` payloads omit "
        f"`{CONTRACT_KEY}`, so the desktop will warn 'Backend out of date' "
        f"against a current backend: {missing}. Build them with one of "
        f"{sorted(INFO_BUILDERS)} instead of assembling a partial dict."
    )


def test_the_guard_can_actually_see_the_payloads():
    """A sweep that matches nothing would pass forever. Pin that it finds some."""
    assert _inline_info_dicts(), "found no inline `info` payloads — the AST sweep has drifted"


def test_info_builders_advertise_the_backend_contract():
    """The builders the inline sweep trusts must earn that trust."""
    checked: set[str] = set()

    for path in sorted(GATEWAY.glob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))

        for node in ast.walk(tree):
            if not isinstance(node, ast.FunctionDef) or node.name not in INFO_BUILDERS:
                continue

            checked.add(node.name)
            source = ast.get_source_segment(path.read_text(encoding="utf-8"), node) or ""

            assert CONTRACT_KEY in source, (
                f"{node.name} ({path.name}:{node.lineno}) returns a session `info` "
                f"payload without `{CONTRACT_KEY}`; the desktop reads that as "
                "contract 0 and falsely reports the backend out of date."
            )

    assert checked == INFO_BUILDERS, f"builders not found in the gateway: {INFO_BUILDERS - checked}"
