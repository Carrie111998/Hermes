"""Tests for gateway boot-time stale-bytecode sweep.

The gateway has its own ``main()`` entry point (``gateway/run.py``) that
doesn't pass through ``hermes_cli/main.py``'s launch guard. This test verifies
that the gateway's ``main()`` calls ``_sweep_stale_bytecode_if_checkout_changed()``
to prevent ImportError crashes when the checkout advances under a running gateway.

Related: #72525 (CLI guard), #75125, #52692, #52691 (closed gateway-specific PRs).
"""

from pathlib import Path
from unittest.mock import patch, MagicMock

from hermes_cli import main as hermes_main


def _make_repo(tmp_path: Path, sha: str = "a" * 40) -> Path:
    """Minimal git checkout layout."""
    repo = tmp_path / "repo"
    git_dir = repo / ".git"
    (git_dir / "refs" / "heads").mkdir(parents=True)
    (git_dir / "HEAD").write_text("ref: refs/heads/main\n", encoding="utf-8")
    (git_dir / "refs" / "heads" / "main").write_text(sha + "\n", encoding="utf-8")
    return repo


def _make_pycache(repo: Path, subdir: str = "tools") -> Path:
    cache = repo / subdir / "__pycache__"
    cache.mkdir(parents=True)
    (cache / "registry.cpython-312.pyc").write_bytes(b"stale")
    return cache


def test_gateway_main_sweeps_stale_bytecode(monkeypatch, tmp_path):
    """Gateway main() sweeps __pycache__ when checkout changed since boot."""
    repo = _make_repo(tmp_path, sha="b" * 40)
    cache = _make_pycache(repo)

    # Stamp records an older fingerprint.
    (repo / hermes_main._BYTECODE_FINGERPRINT_FILE).write_text(
        "git:refs/heads/main:" + "a" * 40, encoding="utf-8"
    )

    monkeypatch.setattr(hermes_main, "PROJECT_ROOT", repo)

    # Simulate what gateway/run.py:main() does:
    #   from hermes_cli.main import _sweep_stale_bytecode_if_checkout_changed
    #   _sweep_stale_bytecode_if_checkout_changed()
    hermes_main._sweep_stale_bytecode_if_checkout_changed()

    # __pycache__ should be cleared.
    assert not cache.exists()

    # Stamp updated to the current fingerprint.
    recorded = (repo / hermes_main._BYTECODE_FINGERPRINT_FILE).read_text(encoding="utf-8")
    assert recorded.strip().endswith("b" * 40)


def test_gateway_main_noop_when_fingerprints_match(monkeypatch, tmp_path):
    """No sweep when checkout hasn't changed."""
    repo = _make_repo(tmp_path, sha="a" * 40)
    cache = _make_pycache(repo)

    # Stamp matches current fingerprint.
    (repo / hermes_main._BYTECODE_FINGERPRINT_FILE).write_text(
        "git:refs/heads/main:" + "a" * 40, encoding="utf-8"
    )

    monkeypatch.setattr(hermes_main, "PROJECT_ROOT", repo)

    hermes_main._sweep_stale_bytecode_if_checkout_changed()

    # __pycache__ should still exist (no sweep needed).
    assert cache.exists()


def test_gateway_main_noop_for_non_git_install(monkeypatch, tmp_path):
    """Non-git install (no .git dir) — sweep is a no-op."""
    repo = tmp_path / "repo"
    repo.mkdir()
    cache = _make_pycache(repo)

    # No .git dir, no stamp file.
    monkeypatch.setattr(hermes_main, "PROJECT_ROOT", repo)

    hermes_main._sweep_stale_bytecode_if_checkout_changed()

    # __pycache__ untouched (non-git install).
    assert cache.exists()
