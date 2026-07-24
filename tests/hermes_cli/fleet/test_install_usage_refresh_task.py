"""Tests for scripts/install_fleet_usage_refresh_task.ps1 schtasks /TR shaping.

Windows schtasks rejects /TR values longer than 261 characters. The installer
must render a short action (pwsh.exe + minimal flags), omit default HermesHome,
and fail closed with a measured length error before calling schtasks when a
custom path is unavoidably too long.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[3]
INSTALLER = REPO_ROOT / "scripts" / "install_fleet_usage_refresh_task.ps1"
REFRESHER = REPO_ROOT / "scripts" / "fleet_refresh_usage.ps1"

# Documented Windows schtasks /TR limit from the host error:
#   ERROR: Value for '/TR' option cannot be more than 261 character(s).
SCHTASKS_TR_MAX = 261


def _installer_text() -> str:
    return INSTALLER.read_text(encoding="utf-8")


def test_installer_source_uses_short_pwsh_command_name() -> None:
    text = _installer_text()
    # Must verify PS7 exists...
    assert "Get-Command pwsh" in text
    # ...but must not bake the resolved .Source path into /TR.
    assert "pwsh.exe" in text
    # Strip comments before checking code-only invariants.
    code_only = "\n".join(
        line for line in text.splitlines() if not line.lstrip().startswith("#")
    )
    assert ".Source" not in code_only
    # Fail closed before schtasks when over the limit.
    assert "261" in code_only
    assert "schtasks /Create" in code_only
    create_idx = code_only.index("schtasks /Create")
    length_guard_idx = code_only.index("SchtasksTrMax")
    assert length_guard_idx < create_idx
    # DryRun must short-circuit before create.
    assert "DryRun" in code_only
    assert code_only.index("if ($DryRun)") < create_idx


def test_installer_source_has_no_wsl_or_windows_powershell_51() -> None:
    text = _installer_text().lower()
    assert "wsl" not in text
    assert "powershell.exe" not in text
    # Allow the comment that we do NOT use Windows PowerShell 5.1.
    assert "pwsh" in text


def test_installer_source_keeps_interval_validation_and_replace() -> None:
    text = _installer_text()
    assert "IntervalMinutes must be in [5, 119]" in text
    assert "/F" in text  # force replace
    assert "schtasks /Delete" in text or "$Remove" in text


def test_installer_source_omits_default_hermes_home_from_tr() -> None:
    text = _installer_text()
    # Must be able to skip -HermesHome when it equals %LOCALAPPDATA%\hermes.
    assert "LOCALAPPDATA" in text
    assert "HermesHome" in text
    # Explicit non-default support remains.
    assert "-HermesHome" in text


@pytest.mark.skipif(shutil.which("pwsh") is None, reason="pwsh not on PATH")
def test_installer_parser_accepts_script() -> None:
    """PowerShell parser must accept the installer (syntax gate)."""
    cmd = [
        "pwsh",
        "-NoProfile",
        "-NonInteractive",
        "-Command",
        (
            "$e=$null; $null=[System.Management.Automation.Language.Parser]::"
            f"ParseFile('{INSTALLER.as_posix()}', [ref]$null, [ref]$e); "
            "if ($e) { $e | ForEach-Object { $_.ToString() }; exit 1 } "
            "else { 'PARSE_OK' }"
        ),
    ]
    proc = subprocess.run(cmd, capture_output=True, text=True, check=False)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert "PARSE_OK" in proc.stdout


def _run_dry_run(*, hermes_home: str | None = None, extra: list[str] | None = None) -> subprocess.CompletedProcess[str]:
    cmd = [
        "pwsh",
        "-NoProfile",
        "-NonInteractive",
        "-File",
        str(INSTALLER),
        "-DryRun",
    ]
    if hermes_home is not None:
        cmd.extend(["-HermesHome", hermes_home])
    if extra:
        cmd.extend(extra)
    return subprocess.run(cmd, capture_output=True, text=True, check=False)


def _parse_tr(stdout: str) -> tuple[int, str]:
    length_m = re.search(r"TR_LENGTH=(\d+)", stdout)
    tr_m = re.search(r"^TR=(.*)$", stdout, re.M)
    assert length_m, f"missing TR_LENGTH in stdout:\n{stdout}"
    assert tr_m, f"missing TR= in stdout:\n{stdout}"
    length = int(length_m.group(1))
    tr = tr_m.group(1).strip()
    assert length == len(tr), f"reported length {length} != len(tr) {len(tr)}"
    return length, tr


@pytest.mark.skipif(shutil.which("pwsh") is None, reason="pwsh not on PATH")
def test_dry_run_default_live_path_tr_under_limit_and_shape() -> None:
    """Default live activation path must render a short, pwsh-based /TR."""
    localappdata = os.environ.get("LOCALAPPDATA")
    assert localappdata, "LOCALAPPDATA required for default HermesHome"
    default_home = str(Path(localappdata) / "hermes")

    # Mirrors the live activation that previously failed: explicit default home.
    proc = _run_dry_run(hermes_home=default_home.replace("\\", "/"))
    assert proc.returncode == 0, proc.stdout + proc.stderr
    length, tr = _parse_tr(proc.stdout)

    assert length <= SCHTASKS_TR_MAX, f"/TR length {length} exceeds {SCHTASKS_TR_MAX}: {tr}"
    assert tr.lower().startswith("pwsh.exe"), tr
    assert "fleet_refresh_usage.ps1" in tr
    assert "wsl" not in tr.lower()
    assert "powershell.exe" not in tr.lower()
    # Full WindowsApps path must not appear.
    assert "WindowsApps" not in tr
    assert "Program Files" not in tr
    # Default home should be omitted to save budget.
    assert "-HermesHome" not in tr
    # Minimal necessary flags; no need for the long historical flag set alone.
    assert "-File" in tr
    assert "-NoProfile" in tr


@pytest.mark.skipif(shutil.which("pwsh") is None, reason="pwsh not on PATH")
def test_dry_run_omits_home_when_unset_uses_default() -> None:
    # Child must not inherit HERMES_HOME or the omit-default logic is skipped.
    proc = subprocess.run(
        [
            "pwsh",
            "-NoProfile",
            "-NonInteractive",
            "-File",
            str(INSTALLER),
            "-DryRun",
        ],
        capture_output=True,
        text=True,
        check=False,
        env={k: v for k, v in os.environ.items() if k != "HERMES_HOME"},
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr
    length, tr = _parse_tr(proc.stdout)
    assert length <= SCHTASKS_TR_MAX
    assert "-HermesHome" not in tr
    assert "fleet_refresh_usage.ps1" in tr


@pytest.mark.skipif(shutil.which("pwsh") is None, reason="pwsh not on PATH")
def test_dry_run_includes_nondefault_hermes_home() -> None:
    custom = "D:\\hermes-custom-home"
    proc = _run_dry_run(hermes_home=custom)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    length, tr = _parse_tr(proc.stdout)
    assert length <= SCHTASKS_TR_MAX
    assert "-HermesHome" in tr
    assert "hermes-custom-home" in tr
    assert tr.lower().startswith("pwsh.exe")
    assert "fleet_refresh_usage.ps1" in tr


@pytest.mark.skipif(shutil.which("pwsh") is None, reason="pwsh not on PATH")
def test_dry_run_fails_before_schtasks_when_tr_too_long(tmp_path: Path) -> None:
    # Build an absurdly long custom home so /TR must exceed 261 even with pwsh.exe.
    long_home = "D:\\" + ("x" * 240)
    proc = _run_dry_run(hermes_home=long_home)
    assert proc.returncode != 0, proc.stdout + proc.stderr
    combined = (proc.stdout + proc.stderr).lower()
    assert "261" in combined or "exceed" in combined or "length" in combined
    # Must not claim successful registration.
    assert "registered" not in combined


@pytest.mark.skipif(shutil.which("pwsh") is None, reason="pwsh not on PATH")
def test_dry_run_rejects_invalid_interval() -> None:
    proc = _run_dry_run(extra=["-IntervalMinutes", "3"])
    assert proc.returncode != 0
    assert "IntervalMinutes" in (proc.stdout + proc.stderr)


def test_refresher_script_exists_beside_installer() -> None:
    assert INSTALLER.is_file()
    assert REFRESHER.is_file()
