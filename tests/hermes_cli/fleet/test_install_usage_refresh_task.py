"""Tests for scripts/install_fleet_usage_refresh_task.ps1 schtasks /TR shaping.

Windows schtasks rejects /TR values longer than 261 characters. Task Scheduler's
restricted PATH also cannot resolve bare ``pwsh.exe`` (Last Result 0x80070002).
The installer must:

- verify pwsh via Get-Command and embed the verified absolute ``.Source`` path
  (quoted) into /TR
- omit default HermesHome and keep flags minimal so default action stays <=261
- support custom HermesHome only when total action <=261; otherwise fail closed
  with a measured length error before schtasks /Create
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


def _code_only(text: str) -> str:
    """Strip full-line comments so source invariants inspect code only."""
    return "\n".join(
        line for line in text.splitlines() if not line.lstrip().startswith("#")
    )


def test_installer_source_resolves_and_embeds_pwsh_source() -> None:
    text = _installer_text()
    code_only = _code_only(text)
    # Must verify PS7 exists via Get-Command ...
    assert "Get-Command pwsh" in code_only
    # ...and embed the verified full executable path from .Source into /TR.
    assert ".Source" in code_only
    # Fail closed before schtasks when over the limit.
    assert "261" in code_only
    assert "schtasks /Create" in code_only
    create_idx = code_only.index("schtasks /Create")
    length_guard_idx = code_only.index("SchtasksTrMax")
    assert length_guard_idx < create_idx
    # DryRun must short-circuit before create.
    assert "DryRun" in code_only
    assert code_only.index("if ($DryRun)") < create_idx
    # Must not hardcode bare command-name-only form as the /TR executable.
    # Allow comments / error strings, but code that builds /TR must use .Source.
    assert "Get-Command pwsh" in code_only
    # Quoted path form for the executable (handles spaces in Program Files).
    assert '"{0}"' in code_only or "\"{0}\"" in code_only


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


def _run_dry_run(
    *, hermes_home: str | None = None, extra: list[str] | None = None
) -> subprocess.CompletedProcess[str]:
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


def _extract_tr_executable(tr: str) -> str:
    """First /TR token is the executable; may be quoted."""
    tr = tr.strip()
    if tr.startswith('"'):
        end = tr.find('"', 1)
        assert end > 1, f"unclosed quoted executable in TR={tr}"
        return tr[1:end]
    return tr.split(" ", 1)[0]


def _host_pwsh_source() -> str:
    proc = subprocess.run(
        [
            "pwsh",
            "-NoProfile",
            "-NonInteractive",
            "-Command",
            "(Get-Command pwsh -ErrorAction Stop).Source",
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr
    source = proc.stdout.strip()
    assert source, "empty pwsh Source"
    assert Path(source).is_file(), f"pwsh Source not a file: {source}"
    return source


@pytest.mark.skipif(shutil.which("pwsh") is None, reason="pwsh not on PATH")
def test_dry_run_default_live_path_tr_under_limit_and_shape() -> None:
    """Default live activation path must render absolute pwsh /TR <=261."""
    localappdata = os.environ.get("LOCALAPPDATA")
    assert localappdata, "LOCALAPPDATA required for default HermesHome"
    default_home = str(Path(localappdata) / "hermes")
    host_pwsh = _host_pwsh_source()

    # Mirrors the live activation that previously failed: explicit default home.
    proc = _run_dry_run(hermes_home=default_home.replace("\\", "/"))
    assert proc.returncode == 0, proc.stdout + proc.stderr
    length, tr = _parse_tr(proc.stdout)

    assert length <= SCHTASKS_TR_MAX, (
        f"/TR length {length} exceeds {SCHTASKS_TR_MAX}: {tr}"
    )

    exe = _extract_tr_executable(tr)
    # Absolute existing pwsh path — not bare pwsh.exe (Task Scheduler PATH miss).
    assert exe.lower() != "pwsh.exe", f"bare pwsh.exe not allowed in /TR: {tr}"
    assert re.match(r"^[A-Za-z]:\\", exe), f"executable must be absolute: {exe}"
    assert Path(exe).is_file(), f"executable does not exist: {exe}"
    assert exe.lower().endswith("pwsh.exe"), f"executable must be pwsh.exe: {exe}"
    # Must match the host's verified Get-Command Source (case-insensitive).
    assert os.path.normcase(os.path.normpath(exe)) == os.path.normcase(
        os.path.normpath(host_pwsh)
    ), f"TR exe {exe!r} != host Source {host_pwsh!r}"

    assert "fleet_refresh_usage.ps1" in tr
    assert "wsl" not in tr.lower()
    assert "powershell.exe" not in tr.lower()
    # Default home should be omitted to save budget.
    assert "-HermesHome" not in tr
    # Minimal necessary flags retained.
    assert "-File" in tr
    assert "-NoProfile" in tr
    # Executable token itself must be quoted (spaces in Program Files / WindowsApps).
    assert tr.lstrip().startswith('"'), f"executable must be quoted: {tr}"


@pytest.mark.skipif(shutil.which("pwsh") is None, reason="pwsh not on PATH")
def test_dry_run_omits_home_when_unset_uses_default() -> None:
    # Child must not inherit HERMES_HOME or the omit-default logic is skipped.
    host_pwsh = _host_pwsh_source()
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
    exe = _extract_tr_executable(tr)
    assert exe.lower() != "pwsh.exe"
    assert Path(exe).is_file()
    assert os.path.normcase(os.path.normpath(exe)) == os.path.normcase(
        os.path.normpath(host_pwsh)
    )


@pytest.mark.skipif(shutil.which("pwsh") is None, reason="pwsh not on PATH")
def test_dry_run_custom_hermes_home_only_when_tr_fits() -> None:
    """Custom -HermesHome is supported only when total /TR stays <=261.

    On hosts where the absolute pwsh + script path already consumes most of
    the budget, a non-default home must fail closed with a measured error
    before schtasks /Create — not silently truncate or use bare pwsh.exe.
    """
    # Baseline default DryRun (no ambient HERMES_HOME) for remaining budget.
    base_proc = subprocess.run(
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
    assert base_proc.returncode == 0, base_proc.stdout + base_proc.stderr
    base_len, base_tr = _parse_tr(base_proc.stdout)
    assert base_len <= SCHTASKS_TR_MAX
    room = SCHTASKS_TR_MAX - base_len

    # Suffix cost: ' -HermesHome "<path>"' == 15 + len(path) + 2 quotes... 
    # ' -HermesHome ' = 14 chars including leading space and trailing space before quote,
    # plus quotes around path: 14 + 2 + len(path) = 16 + len(path).
    # Exact: " -HermesHome \"{path}\"" => 1+12+1+1+len(path)+1 = 16+len(path).
    overhead = 16  # chars added around the raw home path value

    # Prefer a short distinctive custom home when it fits; otherwise prove fail-closed.
    short_custom = "D:\\h"
    short_cost = overhead + len(short_custom)

    if room >= short_cost:
        proc = _run_dry_run(hermes_home=short_custom)
        assert proc.returncode == 0, proc.stdout + proc.stderr
        length, tr = _parse_tr(proc.stdout)
        assert length <= SCHTASKS_TR_MAX
        assert "-HermesHome" in tr
        assert short_custom in tr or "D:\\h" in tr
        exe = _extract_tr_executable(tr)
        assert exe.lower() != "pwsh.exe"
        assert Path(exe).is_file()
        assert exe.lower().endswith("pwsh.exe")
        assert "fleet_refresh_usage.ps1" in tr
        assert "wsl" not in tr.lower()
        assert "powershell.exe" not in tr.lower()
    else:
        # No room even for the shortest practical custom home: must fail measured.
        proc = _run_dry_run(hermes_home=short_custom)
        assert proc.returncode != 0, proc.stdout + proc.stderr
        combined = (proc.stdout + proc.stderr).lower()
        assert "261" in combined or "exceed" in combined or "length" in combined
        assert "registered" not in combined
        # Default baseline still used absolute pwsh (not bare) when it fit.
        exe = _extract_tr_executable(base_tr)
        assert exe.lower() != "pwsh.exe"
        assert Path(exe).is_file()


@pytest.mark.skipif(shutil.which("pwsh") is None, reason="pwsh not on PATH")
def test_dry_run_fails_before_schtasks_when_tr_too_long(tmp_path: Path) -> None:
    # Build an absurdly long custom home so /TR must exceed 261 even with full pwsh path.
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
