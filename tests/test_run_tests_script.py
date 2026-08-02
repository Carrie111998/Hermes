"""Tests for the canonical shell test runner's virtualenv selection."""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path


def _fake_venv(root: Path, label: str, *, has_pytest: bool = True) -> None:
    bin_dir = root / "bin"
    bin_dir.mkdir(parents=True)
    (bin_dir / "activate").write_text("# test marker\n", encoding="utf-8")
    python = bin_dir / "python"
    pytest_probe_exit = 0 if has_pytest else 1
    python.write_text(
        "#!/bin/sh\n"
        'if [ "${1:-}" = "-c" ]; then\n'
        f"  exit {pytest_probe_exit}\n"
        "fi\n"
        f"echo {label}\n",
        encoding="utf-8",
    )
    python.chmod(0o755)


def test_linked_worktree_prefers_authoritative_home_dotvenv(tmp_path):
    repo = tmp_path / "repo"
    scripts = repo / "scripts"
    scripts.mkdir(parents=True)
    source = Path(__file__).resolve().parents[1] / "scripts" / "run_tests.sh"
    shutil.copy2(source, scripts / "run_tests.sh")

    home = tmp_path / "home"
    install = home / ".hermes" / "hermes-agent"
    _fake_venv(install / ".venv", "AUTHORITATIVE_DOTVENV")
    _fake_venv(install / "venv", "LEGACY_VENV")

    env = {"HOME": str(home), "PATH": os.environ["PATH"]}
    result = subprocess.run(
        ["bash", str(scripts / "run_tests.sh")],
        cwd=repo,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    assert "AUTHORITATIVE_DOTVENV" in result.stdout
    assert "LEGACY_VENV" not in result.stdout


def test_checkout_legacy_venv_precedes_installed_home_dotvenv(tmp_path):
    repo = tmp_path / "repo"
    scripts = repo / "scripts"
    scripts.mkdir(parents=True)
    source = Path(__file__).resolve().parents[1] / "scripts" / "run_tests.sh"
    shutil.copy2(source, scripts / "run_tests.sh")
    _fake_venv(repo / "venv", "CHECKOUT_LEGACY_VENV")

    home = tmp_path / "home"
    install = home / ".hermes" / "hermes-agent"
    _fake_venv(install / ".venv", "INSTALLED_DOTVENV")

    env = {"HOME": str(home), "PATH": os.environ["PATH"]}
    result = subprocess.run(
        ["bash", str(scripts / "run_tests.sh")],
        cwd=repo,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    assert "CHECKOUT_LEGACY_VENV" in result.stdout
    assert "INSTALLED_DOTVENV" not in result.stdout


def test_checkout_venv_without_pytest_falls_back_to_installed_home_dotvenv(tmp_path):
    repo = tmp_path / "repo"
    scripts = repo / "scripts"
    scripts.mkdir(parents=True)
    source = Path(__file__).resolve().parents[1] / "scripts" / "run_tests.sh"
    shutil.copy2(source, scripts / "run_tests.sh")
    _fake_venv(repo / ".venv", "INCOMPLETE_CHECKOUT_DOTVENV", has_pytest=False)

    home = tmp_path / "home"
    install = home / ".hermes" / "hermes-agent"
    _fake_venv(install / ".venv", "INSTALLED_DOTVENV")

    env = {"HOME": str(home), "PATH": os.environ["PATH"]}
    result = subprocess.run(
        ["bash", str(scripts / "run_tests.sh")],
        cwd=repo,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    assert "INSTALLED_DOTVENV" in result.stdout
    assert "INCOMPLETE_CHECKOUT_DOTVENV" not in result.stdout


def test_runner_replaces_home_and_hermes_home_before_test_process(tmp_path):
    """A collection/subprocess path must never inherit the operator's live home."""
    repo = tmp_path / "repo"
    scripts = repo / "scripts"
    scripts.mkdir(parents=True)
    source = Path(__file__).resolve().parents[1] / "scripts" / "run_tests.sh"
    shutil.copy2(source, scripts / "run_tests.sh")

    checkout_venv = repo / ".venv"
    bin_dir = checkout_venv / "bin"
    bin_dir.mkdir(parents=True)
    (bin_dir / "activate").write_text("# test marker\n", encoding="utf-8")
    python = bin_dir / "python"
    python.write_text(
        "#!/bin/sh\n"
        'if [ "${1:-}" = "-c" ]; then exit 0; fi\n'
        'printf "TEST_HOME=%s\\n" "$HOME"\n'
        'printf "TEST_HERMES_HOME=%s\\n" "$HERMES_HOME"\n'
        'printf "TEST_LIVE_STATE=%s\\n" "${HERMES_LIVE_STATE_SENTINEL:-}"\n',
        encoding="utf-8",
    )
    python.chmod(0o755)

    operator_home = tmp_path / "operator-home"
    operator_home.mkdir()
    env = {
        "HOME": str(operator_home),
        "PATH": os.environ["PATH"],
        "HERMES_LIVE_STATE_SENTINEL": str(operator_home / "live-state"),
    }
    result = subprocess.run(
        ["bash", str(scripts / "run_tests.sh")],
        cwd=repo,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    lines = dict(
        line.split("=", 1)
        for line in result.stdout.splitlines()
        if line.startswith("TEST_")
    )
    assert Path(lines["TEST_HOME"]) != operator_home
    assert Path(lines["TEST_HOME"]).resolve() == Path(lines["TEST_HOME"])
    assert Path(lines["TEST_HERMES_HOME"]) == Path(lines["TEST_HOME"]) / ".hermes"
    assert lines["TEST_LIVE_STATE"] == ""
