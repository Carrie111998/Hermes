"""``project_venv_dir`` must resolve ONE canonical environment.

The dual-layout problem (installers historically created ``venv``, ``uv venv``
defaults to ``.venv``) left checkouts with BOTH and made different subsystems
resolve to different ones. The resolver now prefers the venv the current
interpreter runs from, then ``.venv`` (canonical), then ``venv`` (legacy).
"""

import sys
from pathlib import Path

import pytest

from hermes_constants import project_venv_dir


@pytest.fixture
def root(tmp_path):
    return tmp_path / "hermes-agent"


def _venv(root, name, *, python_marker=True):
    d = root / name
    (d / "Scripts").mkdir(parents=True, exist_ok=True)
    if python_marker:
        (d / "Scripts" / "python.exe").write_bytes(b"MZ")
    (d / "pyvenv.cfg").write_text("home = X\n", encoding="utf-8")
    return d


def test_single_legacy_layout_still_resolves(tmp_path, monkeypatch, root):
    """Old installs (only ``venv``) keep working — legacy is a supported layout."""
    _venv(root, "venv")

    assert project_venv_dir(root) == root / "venv"


def test_canonical_layout_wins_over_legacy_when_both_exist(tmp_path, monkeypatch, root):
    """Both present → ``.venv`` (canonical); the old ``venv``-wins rule is gone."""
    _venv(root, "venv")
    _venv(root, ".venv")

    assert project_venv_dir(root) == root / ".venv"


def test_running_interpreter_wins_even_from_legacy_layout(tmp_path, monkeypatch, root):
    """A process actually running from the legacy venv keeps that environment —
    the active interpreter outranks canonical naming."""
    _venv(root, ".venv")
    _venv(root, "venv")
    monkeypatch.setattr(sys, "prefix", str(root / "venv"))

    assert project_venv_dir(root) == root / "venv"


def test_running_interpreter_inside_canonical_venv(tmp_path, monkeypatch, root):
    _venv(root, ".venv")
    _venv(root, "venv")
    monkeypatch.setattr(sys, "prefix", str(root / ".venv"))

    assert project_venv_dir(root) == root / ".venv"


def test_prefix_outside_project_is_ignored(tmp_path, monkeypatch, root):
    """A base/system interpreter (or another project's venv) must not hijack
    resolution — falls back to canonical naming."""
    _venv(root, ".venv")
    _venv(root, "venv")
    monkeypatch.setattr(sys, "prefix", str(tmp_path / "elsewhere"))

    assert project_venv_dir(root) == root / ".venv"


def test_no_layout_returns_none(tmp_path, monkeypatch, root):
    assert project_venv_dir(root) is None


def test_prefix_probe_tolerates_unresolvable_paths(tmp_path, monkeypatch, root):
    """resolve() can raise on odd paths; the resolver must degrade to probing."""
    _venv(root, ".venv")
    monkeypatch.setattr(sys, "prefix", str(root / "gone"))
    # resolve() on a missing path returns the path non-strict; the resolver
    # must degrade to canonical probing instead of crashing or mis-picking.
    assert project_venv_dir(root) == root / ".venv"