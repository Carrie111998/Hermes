"""Lint guard: no new raw yaml.safe_load(config.yaml) reads outside owner modules.

The drift class this kills: scattered ``yaml.safe_load`` reads of the user's
``config.yaml`` silently miss the managed-scope overlay, ``${ENV_VAR}``
expansion, profile-aware pathing, and root-model normalization. Each new
config feature has historically required an N-site sweep (incident chain:
9cbcc0c9c8 → 732293cf87 → b0e47a98f9 → 1928aa0443).

Canonical owners:

  * ``hermes_cli/config.py`` — ``load_config()`` / ``load_config_readonly()``
    (merged + managed + env-expanded), ``read_raw_config()`` and
    ``read_user_config_raw()`` (the ONLY legal raw primitives: write-back
    round-trips + raw-file diagnostics).
  * ``gateway/config.py`` — the gateway's ``load_gateway_config`` owner.
  * ``gateway/run.py`` — ``_load_gateway_config()``'s monkeypatched-home
    fallback path (delegates to ``read_raw_config`` when paths agree).

Everything else must import one of those. If this test fails on your new
code, use ``load_config()``/``load_config_readonly()`` for behavioral reads,
or ``read_user_config_raw()`` for write-back round-trips — do not add your
file to the allowlist without a reason of the same class.
"""

from __future__ import annotations

import os
import re
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent.parent

# Files where a yaml.safe_load near a config.yaml reference is legal.
# Keep this list SHORT and justified:
ALLOWLIST = {
    # Canonical loader owners.
    "hermes_cli/config.py",
    "gateway/config.py",
    # _load_gateway_config()'s fallback path for tests that monkeypatch
    # gateway.run._hermes_home (delegates to read_raw_config otherwise).
    "gateway/run.py",
    # Reads the MANAGED-scope config.yaml (/etc/hermes/...), not the user's —
    # it IS the overlay source; the canonical loaders call into it.
    "hermes_cli/managed_scope.py",
    # Parse-health probe: intentionally answers "does the raw file parse?".
    "gateway/readiness.py",
}

# Directories that never count (tests may build fixture configs freely).
EXCLUDED_DIR_PARTS = {
    "tests", ".venv", ".git", ".worktrees", "__pycache__", "node_modules",
    "website", "docs", "scripts", "examples", "apps",
}

# A safe_load within this many lines of a config.yaml reference is treated
# as a raw user-config read.
PROXIMITY = 6

SAFE_LOAD_RE = re.compile(r"\bsafe_load\s*\(")
CONFIG_YAML_RE = re.compile(r"""["']config\.yaml["']""")


def _iter_source_files():
    def _handle_walk_error(error: OSError) -> None:
        if isinstance(error, FileNotFoundError):
            return
        raise error

    for root, dirs, files in os.walk(REPO_ROOT, onerror=_handle_walk_error):
        # Prune before descent. Besides avoiding huge dependency/generated
        # trees, os.walk tolerates an ephemeral directory disappearing while
        # another process performs the checkout's bytecode-cache sweep.
        dirs[:] = [name for name in dirs if name not in EXCLUDED_DIR_PARTS]
        root_path = Path(root)
        for name in files:
            if not name.endswith(".py"):
                continue
            path = root_path / name
            yield path.relative_to(REPO_ROOT), path


def test_source_scan_tolerates_disappearing_generated_directory(
    monkeypatch, tmp_path: Path
):
    """Concurrent bytecode cleanup must not crash repository traversal."""
    package = tmp_path / "package"
    volatile = package / "generated-cache"
    volatile.mkdir(parents=True)
    (package / "stable.py").write_text("value = 1\n", encoding="utf-8")
    (volatile / "stale.py").write_text("value = 2\n", encoding="utf-8")

    original_scandir = os.scandir
    scandir_hit = False

    def disappearing_scandir(path):
        nonlocal scandir_hit
        if Path(path) == volatile:
            scandir_hit = True
            raise FileNotFoundError(str(volatile))
        return original_scandir(path)

    monkeypatch.setitem(globals(), "REPO_ROOT", tmp_path)
    monkeypatch.setattr(os, "scandir", disappearing_scandir)

    assert [rel for rel, _ in _iter_source_files()] == [Path("package/stable.py")]
    assert scandir_hit, "test never exercised the disappearing-directory branch"


def test_source_scan_does_not_hide_unreadable_source_directory(
    monkeypatch, tmp_path: Path
):
    package = tmp_path / "package"
    blocked = package / "private-source"
    blocked.mkdir(parents=True)
    (blocked / "hidden.py").write_text("value = 1\n", encoding="utf-8")
    original_scandir = os.scandir

    def denied_scandir(path):
        if Path(path) == blocked:
            raise PermissionError(str(blocked))
        return original_scandir(path)

    monkeypatch.setitem(globals(), "REPO_ROOT", tmp_path)
    monkeypatch.setattr(os, "scandir", denied_scandir)

    with pytest.raises(PermissionError, match="private-source"):
        list(_iter_source_files())


def test_no_raw_config_yaml_reads_outside_owner_modules():
    offenders: list[str] = []
    for rel, path in _iter_source_files():
        rel_str = str(rel).replace("\\", "/")
        if rel_str in ALLOWLIST:
            continue
        try:
            lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
        except FileNotFoundError:
            continue
        cfg_lines = [i for i, ln in enumerate(lines) if CONFIG_YAML_RE.search(ln)]
        if not cfg_lines:
            continue
        for i, ln in enumerate(lines):
            if not SAFE_LOAD_RE.search(ln):
                continue
            # Comment/docstring mentions don't count.
            stripped = ln.strip()
            if stripped.startswith("#"):
                continue
            if any(abs(i - j) <= PROXIMITY for j in cfg_lines):
                offenders.append(f"{rel_str}:{i + 1}: {stripped}")

    assert not offenders, (
        "Raw yaml.safe_load of config.yaml outside allowlisted owner modules.\n"
        "Behavioral reads must use hermes_cli.config.load_config()/"
        "load_config_readonly() (or gateway _load_gateway_config); write-back "
        "round-trips and raw-file diagnostics must use "
        "hermes_cli.config.read_user_config_raw().\nOffenders:\n  "
        + "\n  ".join(offenders)
    )


def test_read_user_config_raw_exists_and_documented():
    """The shared raw primitive must exist and carry its legality docstring."""
    from hermes_cli.config import read_user_config_raw

    doc = read_user_config_raw.__doc__ or ""
    assert "ONLY legal for write-back round-trips and raw-file diagnostics" in doc
    assert "load_config()" in doc
