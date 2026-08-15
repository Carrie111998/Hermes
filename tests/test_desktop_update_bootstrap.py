"""Windows-host regression checks for the external update bootstrap."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
BOOTSTRAP = ROOT / "scripts" / "desktop-update" / "bootstrap.ps1"
HANDOFF = ROOT / "scripts" / "desktop-update" / "windows.ps1"


def _powershell() -> str:
    return "powershell.exe"


@pytest.mark.windows_only
def test_update_scripts_parse_on_windows():
    command = (
        "$ErrorActionPreference='Stop'; "
        f"$null=[scriptblock]::Create((Get-Content -Raw -LiteralPath '{BOOTSTRAP}')); "
        f"$null=[scriptblock]::Create((Get-Content -Raw -LiteralPath '{HANDOFF}'))"
    )
    result = subprocess.run(
        [_powershell(), "-NoProfile", "-Command", command],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )
    assert result.returncode == 0, result.stderr or result.stdout


@pytest.mark.windows_only
def test_bootstrap_refuses_non_checkout_without_running_python(tmp_path):
    result = subprocess.run(
        [
            _powershell(),
            "-NoProfile",
            "-ExecutionPolicy",
            "Bypass",
            "-File",
            str(BOOTSTRAP),
            "-InstallRoot",
            str(tmp_path),
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    combined = result.stdout + result.stderr
    assert result.returncode != 0
    assert "Not a git checkout" in combined
