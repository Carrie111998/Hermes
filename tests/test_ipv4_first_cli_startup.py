"""CLI startup regression tests for the default IPv4-first DNS ordering."""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path


def _import_main_with_home(repo_root: Path, hermes_home: Path) -> subprocess.CompletedProcess[str]:
    env = os.environ.copy()
    env["HERMES_HOME"] = str(hermes_home)
    env["PYTHONPATH"] = str(repo_root)
    env.pop("HERMES_REDACT_SECRETS", None)
    return subprocess.run(
        [
            sys.executable,
            "-c",
            (
                "import socket; "
                "import hermes_cli.main; "
                "print(getattr(socket.getaddrinfo, '_hermes_ipv4first_patched', False))"
            ),
        ],
        cwd=repo_root,
        env=env,
        text=True,
        capture_output=True,
        timeout=20,
        check=False,
    )


def test_cli_startup_applies_ipv4_first_for_configless_profile(tmp_path):
    """network.ipv4_first defaults on even when HERMES_HOME has no config.yaml."""
    repo_root = Path(__file__).resolve().parents[1]
    hermes_home = tmp_path / "configless-profile"
    hermes_home.mkdir()

    result = _import_main_with_home(repo_root, hermes_home)

    assert result.returncode == 0, result.stderr
    assert result.stdout.strip().endswith("True")


def test_cli_startup_respects_explicit_ipv4_first_false(tmp_path):
    """Users can opt out of the default without enabling force_ipv4."""
    repo_root = Path(__file__).resolve().parents[1]
    hermes_home = tmp_path / "profile"
    hermes_home.mkdir()
    (hermes_home / "config.yaml").write_text("network:\n  ipv4_first: false\n", encoding="utf-8")

    result = _import_main_with_home(repo_root, hermes_home)

    assert result.returncode == 0, result.stderr
    assert result.stdout.strip().endswith("False")
