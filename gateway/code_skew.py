"""Detect when the gateway is running stale code after a hot ``git pull``.

The gateway is a single long-lived process; its ``sys.modules`` is frozen at
boot. If the checkout is updated underneath it (a manual ``git pull``, or the
window before ``hermes update``'s graceful restart fires), a first-time lazy
import on a new code path can resolve a freshly-pulled consumer module against a
stale cached dependency -> ImportError (see
``tests/test_stale_utils_module_import.py`` for the exact failure).

We snapshot the checkout revision at gateway startup and compare on demand, so
risky callers (e.g. ``/model`` switching) can refuse with a clear "restart the
gateway" message instead of crashing on a cryptic import error.

If the revision can't be read (non-git install, IO error), the boot snapshot
stays ``None`` and skew detection no-ops — it never produces a false positive.
"""

from __future__ import annotations

from pathlib import Path

_PROJECT_ROOT = Path(__file__).resolve().parent.parent
_boot_fingerprint: str | None = None


def _tree_fingerprint() -> str | None:
    """Fingerprint of the checked-out TREE (``HEAD^{tree}``), not the commit.

    Two commits with identical content (a branch flip, a rebase that changes
    nothing, a merge that only rewrites history) must not read as "stale
    code": the modules on disk are byte-for-byte what the process loaded, so a
    restart would change nothing. Comparing commit SHAs produced exactly that
    false positive on 2026-08-27 — a branch switch with an identical tree
    503'd the dashboard model picker for hours. Spawning ``git`` here is fine:
    this runs once at boot and then only on demand (model switch / picker
    load), never on a hot path. Returns ``None`` when git is unavailable so
    the caller falls back to the ref/SHA reader.
    """
    try:
        import subprocess

        out = subprocess.run(
            ["git", "-C", str(_PROJECT_ROOT), "rev-parse", "HEAD^{tree}"],
            capture_output=True, text=True, timeout=5, check=False,
        )
        tree = out.stdout.strip() if out.returncode == 0 else ""
        if tree and len(tree) >= 7 and all(c in "0123456789abcdef" for c in tree):
            return f"tree:HEAD:{tree}"
    except Exception:
        pass
    return None


def _fingerprint() -> str | None:
    """Current checkout fingerprint: tree hash first, ref/SHA reader as fallback.

    ``hermes_cli.main`` is always already imported in a gateway process (it's
    the entry point), so the fallback import is free and avoids duplicating
    the worktree-aware ref resolution.
    """
    tree = _tree_fingerprint()
    if tree:
        return tree
    try:
        from hermes_cli.main import _read_git_revision_fingerprint

        return _read_git_revision_fingerprint(_PROJECT_ROOT)
    except Exception:
        return None


def record_boot_fingerprint() -> None:
    """Snapshot the checkout revision at gateway startup (idempotent)."""
    global _boot_fingerprint
    if _boot_fingerprint is None:
        _boot_fingerprint = _fingerprint()


def _short(fingerprint: str) -> str:
    """Render a ``git:<ref>:<sha>`` fingerprint as a compact label."""
    sha = fingerprint.rsplit(":", 1)[-1]
    if sha and sha != "unresolved" and len(sha) > 10:
        return sha[:10]
    return sha or fingerprint


def detect_code_skew() -> tuple[str, str] | None:
    """Return ``(boot_rev, disk_rev)`` short labels if the checkout drifted
    since boot, else ``None``."""
    if _boot_fingerprint is None:
        return None
    current = _fingerprint()
    if current is None or current == _boot_fingerprint:
        return None
    return _short(_boot_fingerprint), _short(current)
