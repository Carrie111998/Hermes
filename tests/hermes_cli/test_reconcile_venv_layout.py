"""Boot-time venv-layout reconciliation.

When both ``venv`` and ``.venv`` exist, ``reconcile_venv_layout`` parks the
sibling of the resolved (active/canonical) environment so every subsystem
agrees on one environment. Parking is a rename — atomic and recoverable —
and the function never raises (boot path).
"""

import sys
from pathlib import Path

import pytest

from hermes_cli._install_repair import reconcile_venv_layout


def _make(root, name):
    d = root / name
    (d / "Scripts").mkdir(parents=True, exist_ok=True)
    (d / "Scripts" / "python.exe").write_bytes(b"MZ")
    (d / "pyvenv.cfg").write_text("home = X\n", encoding="utf-8")
    return d


def test_parks_legacy_venv_when_both_exist(tmp_path):
    root = tmp_path / "checkout"
    _make(root, "venv")
    _make(root, ".venv")

    parked = reconcile_venv_layout(root, windows=True)

    assert parked is not None
    assert parked.name.startswith("venv.legacy-")
    assert parked.exists()
    assert not (root / "venv").exists()
    assert (root / ".venv" / "Scripts" / "python.exe").exists()


def test_parks_other_sibling_when_running_from_legacy(tmp_path, monkeypatch):
    """Reconciliation keeps the environment the running interpreter uses:
    a process inside the legacy venv parks ``.venv`` instead."""
    root = tmp_path / "checkout"
    _make(root, "venv")
    _make(root, ".venv")
    monkeypatch.setattr(sys, "prefix", str(root / "venv"))

    parked = reconcile_venv_layout(root, windows=True)

    assert parked is not None
    assert parked.name.startswith(".venv.legacy-")
    assert not (root / ".venv").exists()
    assert (root / "venv" / "Scripts" / "python.exe").exists()


def test_single_layout_is_a_noop(tmp_path):
    root = tmp_path / "checkout"
    _make(root, ".venv")

    assert reconcile_venv_layout(root, windows=True) is None
    assert (root / ".venv" / "Scripts" / "python.exe").exists()


def test_broken_resolved_venv_never_prompts_parking(tmp_path):
    """If the resolved venv has no interpreter, do NOT park anything — the
    sibling may be the only usable environment."""
    root = tmp_path / "checkout"
    (root / "venv").mkdir(parents=True)  # legacy dir without Scripts/python
    _make(root, ".venv")
    (root / ".venv" / "Scripts" / "python.exe").unlink()  # canonical broken

    assert reconcile_venv_layout(root, windows=True) is None
    assert (root / "venv").exists()


def test_never_raises(tmp_path):
    root = tmp_path / "checkout"
    root.mkdir()
    # a *file* named venv is not a dir — resolver skips it, no crash
    (root / "venv").write_text("not a dir", encoding="utf-8")
    _make(root, ".venv")
    (root / ".venv" / "Scripts" / "python.exe").unlink()

    assert reconcile_venv_layout(root, windows=True) is None