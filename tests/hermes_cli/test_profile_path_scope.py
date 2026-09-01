"""Verify that data-path resolution uses HERMES_HOME instead of Path.home().

Issue #4671: Some profile path behavior is HOME-based rather than
HERMES_HOME-based.  The four fix sites are:

  1. worktree_gc.py:150 — _archive_untracked archive destination
  2. dashboard_procs.py:738 — _hermes_home_dir() fallback
  3. env_loader.py:489 — load_hermes_dotenv home resolution inline fallback
  4. env_loader.py:803 — _process_hermes_home() except fallback

Each test sets a context-local HERMES_HOME override via
``set_hermes_home_override`` and verifies the code under test resolves
to it rather than ``~/.hermes``.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from hermes_constants import (
    get_hermes_home,
    reset_hermes_home_override,
    set_hermes_home_override,
)


# ── Helpers ──────────────────────────────────────────────────────────


@pytest.fixture
def alt_home(tmp_path: Path) -> Path:
    """A non-default Hermes home (e.g. ``/tmp/.../custom-hermes``)."""
    home = tmp_path / "custom-hermes"
    home.mkdir(parents=True, exist_ok=True)
    return home


@pytest.fixture
def override_home(alt_home: Path):
    """Install a context-local HERMES_HOME override and yield; auto-cleanup."""
    tok = set_hermes_home_override(alt_home)
    try:
        yield
    finally:
        reset_hermes_home_override(tok)


# ── Fix 1: worktree_gc._archive_untracked ───────────────────────────


def test_worktree_gc_archive_uses_hermes_home(alt_home: Path, override_home) -> None:
    """``_archive_untracked`` must archive under ``get_hermes_home()``."""
    from hermes_cli.worktree_gc import _archive_untracked

    import tempfile
    import shutil

    tmp_tree = Path(tempfile.mkdtemp())
    try:
        (tmp_tree / "scratch.txt").write_text("garbage")

        archive = _archive_untracked(tmp_tree, ["scratch.txt"])

        # archive_path must be under the overridden HERMES_HOME
        assert archive is not None
        assert str(archive).startswith(str(alt_home)), (
            f"Expected archive root {alt_home}, got {archive}"
        )
    finally:
        shutil.rmtree(tmp_tree, ignore_errors=True)


# ── Fix 2: dashboard_procs._hermes_home_dir ─────────────────────────


def test_dashboard_procs_hermes_home_dir(alt_home: Path, override_home) -> None:
    """``_hermes_home_dir()`` must use ``get_hermes_home()``, not env-only."""
    from hermes_cli.dashboard_procs import _hermes_home_dir

    result = _hermes_home_dir()
    assert result == get_hermes_home(), (
        f"Expected {get_hermes_home()}, got {result}"
    )


def test_dashboard_procs_hermes_home_dir_no_env(
    alt_home: Path, monkeypatch,
) -> None:
    """``_hermes_home_dir()`` respects context override even without env var."""
    from hermes_cli.dashboard_procs import _hermes_home_dir

    # Ensure HERMES_HOME is NOT set in the environment
    monkeypatch.delenv("HERMES_HOME", raising=False)

    # Use context override
    tok = set_hermes_home_override(alt_home)
    try:
        result = _hermes_home_dir()
        assert result == get_hermes_home(), (
            f"Expected {get_hermes_home()}, got {result}"
        )
        # Must not be ~/.hermes
        assert "custom-hermes" in str(result)
    finally:
        reset_hermes_home_override(tok)


# ── Fix 3: env_loader.load_hermes_dotenv home resolution ────────────


def test_env_loader_uses_hermes_home_inline(alt_home: Path) -> None:
    """``load_hermes_dotenv`` without explicit ``hermes_home`` must use
    the context-local override path, not ``Path.home() / .hermes``."""
    from hermes_cli.env_loader import load_hermes_dotenv

    # Create a .env under the alt home (alt_home fixture already created dir)
    (alt_home / ".env").write_text("TEST_PATH_SCOPE=from-override\n")

    tok = set_hermes_home_override(alt_home)
    try:
        os.environ.pop("TEST_PATH_SCOPE", None)  # clear any previous value
        loaded = load_hermes_dotenv(load_external_secrets=False)
        assert os.environ["TEST_PATH_SCOPE"] == "from-override"
        assert alt_home / ".env" in loaded
    finally:
        reset_hermes_home_override(tok)


# ── Fix 4: env_loader._process_hermes_home fallback ─────────────────


def test_env_loader_process_hermes_home_except_fallback(alt_home: Path) -> None:
    """``_process_hermes_home()`` fallback must not hardcode ``~/.hermes``.

    The try block already calls ``get_hermes_home()`` which respects the
    context override.  This test provokes the EXCEPT path by making the
    normal import raise, so the fallback is exercised.
    """
    from hermes_cli import env_loader

    # Make get_hermes_home raise so the except block fires
    orig = env_loader._process_hermes_home
    saved_module = __import__("hermes_constants")
    original_get = saved_module.get_hermes_home

    def _raise(*args, **kwargs):
        raise RuntimeError("simulated import failure for test")

    saved_module.get_hermes_home = _raise
    try:
        # Force a fresh import so the lazy import in _process_hermes_home
        # retries and hits our patched version
        import importlib
        importlib.reload(env_loader)
        # _process_hermes_home was redefined by reload
        from hermes_cli.env_loader import _process_hermes_home

        result = _process_hermes_home()
        # After fix, should use _get_platform_default_hermes_home()
        # which returns ~/.hermes on POSIX — at least not a crash
        assert result == Path.home() / ".hermes", (
            f"Expected platform default {Path.home() / '.hermes'}, got {result}"
        )
    finally:
        saved_module.get_hermes_home = original_get
        # Re-reload to restore the original
        importlib.reload(env_loader)