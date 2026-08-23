"""Tests for the top-level `./hermes` launcher script."""

import runpy
import subprocess
import sys
import types
from pathlib import Path


def test_launcher_delegates_to_argparse_entrypoint(monkeypatch):
    """`./hermes` should use `hermes_cli.main`, not the legacy Fire wrapper."""
    launcher_path = Path(__file__).resolve().parents[2] / "hermes"
    called = []

    fake_main_module = types.ModuleType("hermes_cli.main")

    def fake_main():
        called.append("hermes_cli.main")

    fake_main_module.main = fake_main
    monkeypatch.setitem(sys.modules, "hermes_cli.main", fake_main_module)

    fake_cli_module = types.ModuleType("cli")

    def legacy_cli_main(*args, **kwargs):
        raise AssertionError("launcher should not import cli.main")

    fake_cli_module.main = legacy_cli_main
    monkeypatch.setitem(sys.modules, "cli", fake_cli_module)

    fake_fire_module = types.ModuleType("fire")

    def legacy_fire(*args, **kwargs):
        raise AssertionError("launcher should not invoke fire.Fire")

    fake_fire_module.Fire = legacy_fire
    monkeypatch.setitem(sys.modules, "fire", fake_fire_module)

    monkeypatch.setattr(sys, "argv", [str(launcher_path), "gateway", "status"])

    runpy.run_path(str(launcher_path), run_name="__main__")

    assert called == ["hermes_cli.main"]


def _launcher() -> Path:
    return Path(__file__).resolve().parents[2] / "hermes"


def test_launcher_fails_actionably_when_deps_are_missing():
    """Running under an interpreter with none of Hermes' dependencies — a
    bare uv-managed CPython, or a generated .desktop Exec= whose interpreter
    escaped the install venv — must name the running interpreter and the fix,
    never a bare `ModuleNotFoundError` traceback (#92882, #90292, #91504)."""
    result = subprocess.run(
        [sys.executable, "-S", str(_launcher()), "--help"],
        capture_output=True,
        text=True,
        timeout=120,
    )

    assert result.returncode != 0
    assert "Traceback" not in result.stderr
    assert "ModuleNotFoundError" not in result.stderr
    assert "hermes:" in result.stderr
    assert sys.executable in result.stderr
    assert "venv" in result.stderr.lower()


def test_launcher_still_runs_cli_when_deps_are_present():
    """The import guard must not change the healthy path: with dependencies
    installed, `./hermes --help` still exits 0 and prints usage."""
    result = subprocess.run(
        [sys.executable, str(_launcher()), "--help"],
        capture_output=True,
        text=True,
        timeout=120,
    )

    assert result.returncode == 0
    assert "usage" in (result.stdout + result.stderr).lower()
