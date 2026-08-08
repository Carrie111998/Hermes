"""Cross-language drift guard for the update-lock protocol's shared constants.

Four values are duplicated by hand across three implementations (Python CLI,
the Tauri/Rust updater, and — for one value — the Electron desktop app)
because none of them can import from one another. Nothing enforced that they
stay in sync until now; each copy just carries a comment asking the reader
to remember. See ``hermes_cli/update_lock.py``'s module docstring and
``apps/bootstrap-installer/src-tauri/src/update.rs``'s own comments for the
full cross-reference chain.

Each constant gets its own test function so a failure or skip names exactly
which constant and which file pair disagree, rather than one opaque
pass/fail for the whole protocol.

Both the Rust and Electron files are real source files (git-tracked, not
gitignored) but are absent from Docker installs — ``.dockerignore`` excludes
all of ``apps/`` except ``apps/shared/`` ("Desktop app source (Tauri/
Electron); never installed in the container."). Every test here skips
gracefully, not fails, when its target file is missing.
"""

from __future__ import annotations

import ast
import operator
import re
from pathlib import Path

import pytest

from hermes_cli.update_lock import (
    HANDOFF_PID_ENV,
    UPDATE_EXIT_CONCURRENT,
    UPDATE_EXIT_STAGED_FOR_APPROVAL,
    UPDATE_MARKER_MAX_AGE_SECONDS,
)

_REPO_ROOT = Path(__file__).resolve().parent.parent
_UPDATE_RS = _REPO_ROOT / "apps" / "bootstrap-installer" / "src-tauri" / "src" / "update.rs"
_UPDATE_MARKER_TS = _REPO_ROOT / "apps" / "desktop" / "electron" / "update-marker.ts"

_SAFE_BINOPS = {
    ast.Add: operator.add,
    ast.Sub: operator.sub,
    ast.Mult: operator.mul,
    ast.Div: operator.truediv,
    ast.FloorDiv: operator.floordiv,
}


def _read_or_skip(path: Path, *, reason: str) -> str:
    if not path.exists():
        pytest.skip(f"{reason} ({path} not present in this checkout)")
    return path.read_text(encoding="utf-8")


def _eval_int_expr(expr: str) -> int:
    """Safely evaluate a small numeric expression like ``20 * 60``.

    A hand-rolled recursive walk over a restricted AST node set (BinOp /
    Constant / UnaryOp with +-*/ only) — deliberately not Python's
    ``eval()``, even though the source here is a trusted file in this repo
    and not external input. This is about robustness to formatting
    differences (``20*60`` vs ``20 * 60``), not sandboxing an attacker.
    """
    tree = ast.parse(expr.strip(), mode="eval").body

    def _walk(node):
        if isinstance(node, ast.Constant) and isinstance(node.value, (int, float)):
            return node.value
        if isinstance(node, ast.BinOp) and type(node.op) in _SAFE_BINOPS:
            return _SAFE_BINOPS[type(node.op)](_walk(node.left), _walk(node.right))
        if isinstance(node, ast.UnaryOp) and isinstance(node.op, ast.USub):
            return -_walk(node.operand)
        raise ValueError(f"unsupported expression element in {expr!r}: {ast.dump(node)}")

    return int(_walk(tree))


def test_update_exit_concurrent_matches_rust():
    """UPDATE_EXIT_CONCURRENT: Python vs the Tauri/Rust updater.

    The Rust updater matches on this exit code to show "Hermes is still
    running" instead of a generic failure (see update_lock.py's own
    comment) — if the two disagree, that message silently stops firing.
    """
    text = _read_or_skip(_UPDATE_RS, reason="Rust updater source not present (Docker/source-only install)")
    m = re.search(r"const\s+UPDATE_EXIT_CONCURRENT\s*:\s*i32\s*=\s*(\d+)\s*;", text)
    assert m, "Could not find UPDATE_EXIT_CONCURRENT definition in update.rs — regex may need updating"
    rust_value = int(m.group(1))
    assert rust_value == UPDATE_EXIT_CONCURRENT, (
        f"UPDATE_EXIT_CONCURRENT drift: hermes_cli/update_lock.py = {UPDATE_EXIT_CONCURRENT}, "
        f"apps/bootstrap-installer/src-tauri/src/update.rs = {rust_value}"
    )


def test_update_marker_max_age_matches_rust():
    """UPDATE_MARKER_MAX_AGE_SECONDS (Python) vs UPDATE_MARKER_MAX_AGE_SECS (Rust).

    Names differ (``_SECONDS`` vs ``_SECS``) but both express the same real
    value — the lock-marker staleness ceiling both readers use to decide
    whether a marker is a live update or a crashed one to self-heal past.
    """
    text = _read_or_skip(_UPDATE_RS, reason="Rust updater source not present (Docker/source-only install)")
    m = re.search(r"const\s+UPDATE_MARKER_MAX_AGE_SECS\s*:\s*u64\s*=\s*([^;]+);", text)
    assert m, "Could not find UPDATE_MARKER_MAX_AGE_SECS definition in update.rs — regex may need updating"
    rust_value = _eval_int_expr(m.group(1))
    assert rust_value == UPDATE_MARKER_MAX_AGE_SECONDS, (
        f"UPDATE_MARKER_MAX_AGE drift: hermes_cli/update_lock.py "
        f"UPDATE_MARKER_MAX_AGE_SECONDS = {UPDATE_MARKER_MAX_AGE_SECONDS}, "
        f"apps/bootstrap-installer/src-tauri/src/update.rs "
        f"UPDATE_MARKER_MAX_AGE_SECS = {rust_value}"
    )


def test_handoff_pid_env_matches_rust():
    """HANDOFF_PID_ENV: Python's named constant vs Rust's inline string literals.

    Rust has no named constant for this — it's the env var name used inline
    at two call sites (setting it when spawning the child, and checking for
    it when resolving the live marker holder). Both must match Python's
    HANDOFF_PID_ENV, or the handoff recognition breaks silently in whichever
    spot drifted — checking "all occurrences" rather than "at least one" is
    what catches a typo introduced in only one of the two spots.
    """
    text = _read_or_skip(_UPDATE_RS, reason="Rust updater source not present (Docker/source-only install)")
    matches = re.findall(r'"([A-Z_]*HANDOFF[A-Z_]*)"', text)
    assert matches, "Could not find any HANDOFF-related string literal in update.rs — regex may need updating"
    mismatched = sorted({v for v in matches if v != HANDOFF_PID_ENV})
    assert not mismatched, (
        f"HANDOFF_PID_ENV drift: hermes_cli/update_lock.py HANDOFF_PID_ENV = {HANDOFF_PID_ENV!r}, "
        f"but apps/bootstrap-installer/src-tauri/src/update.rs has mismatched literal(s) "
        f"{mismatched!r} among {len(matches)} occurrence(s) found"
    )


def test_update_marker_max_age_matches_electron():
    """UPDATE_MARKER_MAX_AGE_SECONDS (Python, seconds) vs UPDATE_MARKER_MAX_AGE_MS (Electron, ms).

    Different unit by design (TypeScript's Date arithmetic is ms-native) —
    compare python_seconds * 1000 against the TS value, not the raw text.
    """
    text = _read_or_skip(
        _UPDATE_MARKER_TS,
        reason="Electron update-marker.ts not present (Docker/source-only install)",
    )
    m = re.search(r"UPDATE_MARKER_MAX_AGE_MS\s*=\s*([^\n;]+)", text)
    assert m, "Could not find UPDATE_MARKER_MAX_AGE_MS definition in update-marker.ts — regex may need updating"
    ts_value = _eval_int_expr(m.group(1))
    expected_ms = UPDATE_MARKER_MAX_AGE_SECONDS * 1000
    assert ts_value == expected_ms, (
        f"UPDATE_MARKER_MAX_AGE drift: hermes_cli/update_lock.py "
        f"UPDATE_MARKER_MAX_AGE_SECONDS = {UPDATE_MARKER_MAX_AGE_SECONDS}s ({expected_ms}ms equivalent), "
        f"apps/desktop/electron/update-marker.ts UPDATE_MARKER_MAX_AGE_MS = {ts_value}ms"
    )


def test_update_exit_staged_for_approval_matches_rust_if_present():
    """UPDATE_EXIT_STAGED_FOR_APPROVAL: Python has it (added in F4), Rust doesn't yet.

    Known, deliberate gap — the Tauri desktop updater doesn't recognize a
    staged update and currently reports it as a generic failure (flagged
    during F4; fixing the Rust side is out of scope for F4 and for this
    test-only task). This is "nothing to compare yet", not "values
    disagree" — skip rather than fail while the Rust side is absent. The
    moment someone adds it there, this starts enforcing the value matches
    instead of silently staying green forever.
    """
    text = _read_or_skip(_UPDATE_RS, reason="Rust updater source not present (Docker/source-only install)")
    m = re.search(r"const\s+UPDATE_EXIT_STAGED_FOR_APPROVAL\s*:\s*i32\s*=\s*(\d+)\s*;", text)
    if not m:
        pytest.skip(
            "UPDATE_EXIT_STAGED_FOR_APPROVAL not yet mirrored in update.rs — known gap "
            "from F4 (the Tauri desktop updater doesn't recognize a staged update yet); "
            "this test will start enforcing it once it's added there."
        )
    rust_value = int(m.group(1))
    assert rust_value == UPDATE_EXIT_STAGED_FOR_APPROVAL, (
        f"UPDATE_EXIT_STAGED_FOR_APPROVAL drift: hermes_cli/update_lock.py = "
        f"{UPDATE_EXIT_STAGED_FOR_APPROVAL}, "
        f"apps/bootstrap-installer/src-tauri/src/update.rs = {rust_value}"
    )
