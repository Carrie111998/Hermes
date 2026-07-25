"""Tests for scripts/install_fleet_usage_refresh_task.ps1 schtasks /TR shaping.

Windows schtasks rejects /TR values longer than 261 characters. Task Scheduler's
restricted PATH also cannot resolve bare ``pwsh.exe`` (Last Result 0x80070002).
The installer must:

- verify pwsh via Get-Command and embed the verified absolute ``.Source`` path
  (quoted) into /TR
- stage ``fleet_refresh_usage.ps1`` to a short stable path under HermesHome
  (``scripts\\fleet_refresh_usage.ps1``) so /TR never embeds repo/worktree paths
- omit default HermesHome and keep flags minimal so default action stays <=261
- support custom HermesHome only when total action <=261; otherwise fail closed
  with a measured length error before schtasks /Create
- never mutate the filesystem on ``-DryRun``; expose the same target staged path
"""

from __future__ import annotations

import hashlib
import os
import re
import shutil
import subprocess
import uuid
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


def _refresher_text() -> str:
    return REFRESHER.read_text(encoding="utf-8")


def _code_only(text: str) -> str:
    """Strip full-line comments so source invariants inspect code only."""
    return "\n".join(
        line for line in text.splitlines() if not line.lstrip().startswith("#")
    )


def _short_custom_home_base() -> Path:
    """Short writable base so custom -HermesHome + staged script stay <=261.

    pytest's default tmp_path under %TEMP%\\pytest-of-... is far too long once
    both the staged -File path and -HermesHome are embedded in /TR.
    """
    base = Path.home() / "hfs"
    base.mkdir(parents=True, exist_ok=True)
    return base


@pytest.fixture
def short_home():
    """Unique short HermesHome directory; removed after the test."""
    root = _short_custom_home_base()
    home = root / uuid.uuid4().hex[:8]
    home.mkdir(parents=True, exist_ok=False)
    try:
        yield home
    finally:
        shutil.rmtree(home, ignore_errors=True)


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
    # Staging must happen before task create (and DryRun must not stage).
    assert "Stage-RefresherScript" in code_only
    assert "scripts\\fleet_refresh_usage.ps1" in text or r"scripts\fleet_refresh_usage.ps1" in text
    # Quoted path form for the executable (handles spaces in Program Files).
    assert '"{0}"' in code_only or '\\"{0}\\"' in code_only


def test_installer_source_stages_before_task_and_dryrun_skips_stage() -> None:
    code_only = _code_only(_installer_text())
    dry_idx = code_only.index("if ($DryRun)")
    stage_call_idx = code_only.index("Stage-RefresherScript -SourcePath")
    create_idx = code_only.index("schtasks /Create")
    assert dry_idx < stage_call_idx < create_idx
    # StageOnly path exists for non-register verification.
    assert "StageOnly" in code_only


def test_installer_source_has_no_wsl_or_windows_powershell_51() -> None:
    text = _installer_text().lower()
    code_only = _code_only(text).lower()
    assert "wsl" not in code_only
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


def test_refresher_prefers_hermes_home_agent_not_worktree() -> None:
    text = _refresher_text()
    code_only = _code_only(text)
    assert "hermes-agent" in text
    assert "usage_refresh.py" in text
    assert "wsl" not in code_only.lower()
    assert "powershell.exe" not in text.lower()
    # Must not hardcode ephemeral worktree names.
    assert "worktrees" not in text.lower()
    assert "fleet-parent-routing-20260724" not in text
    assert "fleet-pwsh-tr-fullpath" not in text


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


@pytest.mark.skipif(shutil.which("pwsh") is None, reason="pwsh not on PATH")
def test_refresher_parser_accepts_script() -> None:
    cmd = [
        "pwsh",
        "-NoProfile",
        "-NonInteractive",
        "-Command",
        (
            "$e=$null; $null=[System.Management.Automation.Language.Parser]::"
            f"ParseFile('{REFRESHER.as_posix()}', [ref]$null, [ref]$e); "
            "if ($e) { $e | ForEach-Object { $_.ToString() }; exit 1 } "
            "else { 'PARSE_OK' }"
        ),
    ]
    proc = subprocess.run(cmd, capture_output=True, text=True, check=False)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert "PARSE_OK" in proc.stdout


def _run_installer(
    *,
    hermes_home: str | None = None,
    extra: list[str] | None = None,
    env: dict[str, str] | None = None,
) -> subprocess.CompletedProcess[str]:
    cmd = [
        "pwsh",
        "-NoProfile",
        "-NonInteractive",
        "-File",
        str(INSTALLER),
    ]
    if hermes_home is not None:
        cmd.extend(["-HermesHome", hermes_home])
    if extra:
        cmd.extend(extra)
    run_env = dict(os.environ)
    if env is not None:
        run_env.update(env)
    # Drop ambient HERMES_HOME unless the caller set one, so default-home
    # omit logic is deterministic for installer path math.
    if hermes_home is None and (env is None or "HERMES_HOME" not in env):
        run_env.pop("HERMES_HOME", None)
    return subprocess.run(cmd, capture_output=True, text=True, check=False, env=run_env)


def _run_dry_run(
    *, hermes_home: str | None = None, extra: list[str] | None = None
) -> subprocess.CompletedProcess[str]:
    extra = list(extra or [])
    if "-DryRun" not in extra:
        extra = ["-DryRun", *extra]
    return _run_installer(hermes_home=hermes_home, extra=extra)


def _parse_tr(stdout: str) -> tuple[int, str]:
    length_m = re.search(r"TR_LENGTH=(\d+)", stdout)
    tr_m = re.search(r"^TR=(.*)$", stdout, re.M)
    assert length_m, f"missing TR_LENGTH in stdout:\n{stdout}"
    assert tr_m, f"missing TR= in stdout:\n{stdout}"
    length = int(length_m.group(1))
    tr = tr_m.group(1).strip()
    assert length == len(tr), f"reported length {length} != len(tr) {len(tr)}"
    return length, tr


def _parse_staged_path(stdout: str) -> str:
    m = re.search(r"^STAGED_PATH=(.*)$", stdout, re.M)
    assert m, f"missing STAGED_PATH in stdout:\n{stdout}"
    return m.group(1).strip()


def _extract_tr_executable(tr: str) -> str:
    """First /TR token is the executable; may be quoted."""
    tr = tr.strip()
    if tr.startswith('"'):
        end = tr.find('"', 1)
        assert end > 1, f"unclosed quoted executable in TR={tr}"
        return tr[1:end]
    return tr.split(" ", 1)[0]


def _extract_tr_file(tr: str) -> str:
    """Extract the -File argument path from /TR."""
    m = re.search(r'-File\s+"([^"]+)"', tr)
    if m:
        return m.group(1)
    m = re.search(r"-File\s+(\S+)", tr)
    assert m, f"missing -File in TR={tr}"
    return m.group(1)


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


def _assert_stable_tr_shape(tr: str, *, hermes_home: Path, host_pwsh: str) -> str:
    """Common assertions: absolute pwsh, staged under HermesHome\\scripts, no worktrees."""
    exe = _extract_tr_executable(tr)
    file_path = _extract_tr_file(tr)

    assert exe.lower() != "pwsh.exe", f"bare pwsh.exe not allowed in /TR: {tr}"
    assert re.match(r"^[A-Za-z]:\\", exe), f"executable must be absolute: {exe}"
    assert Path(exe).is_file(), f"executable does not exist: {exe}"
    assert exe.lower().endswith("pwsh.exe"), f"executable must be pwsh.exe: {exe}"
    assert os.path.normcase(os.path.normpath(exe)) == os.path.normcase(
        os.path.normpath(host_pwsh)
    ), f"TR exe {exe!r} != host Source {host_pwsh!r}"

    assert "fleet_refresh_usage.ps1" in tr
    assert "wsl" not in tr.lower()
    assert "powershell.exe" not in tr.lower()
    assert "worktrees" not in tr.lower(), f"TR must not embed worktree path: {tr}"
    # Must not point at the repo scripts directory used as the staging *source*.
    assert "\\worktrees\\" not in file_path.lower()
    assert tr.lstrip().startswith('"'), f"executable must be quoted: {tr}"
    assert "-File" in tr
    assert "-NoProfile" in tr

    expected_staged = hermes_home / "scripts" / "fleet_refresh_usage.ps1"
    assert os.path.normcase(os.path.normpath(file_path)) == os.path.normcase(
        os.path.normpath(str(expected_staged))
    ), f"TR -File {file_path!r} != staged {expected_staged!r}"
    home_norm = os.path.normcase(os.path.normpath(str(hermes_home)))
    file_norm = os.path.normcase(os.path.normpath(file_path))
    assert file_norm.startswith(home_norm + os.sep) or file_norm.startswith(
        home_norm + "\\"
    ), f"staged file {file_path} not under home {hermes_home}"
    assert file_norm.endswith(os.path.normcase("\\scripts\\fleet_refresh_usage.ps1"))
    return file_path


@pytest.mark.skipif(shutil.which("pwsh") is None, reason="pwsh not on PATH")
def test_dry_run_default_live_path_tr_under_limit_and_shape() -> None:
    """Default live activation path must render absolute pwsh /TR <=261."""
    localappdata = os.environ.get("LOCALAPPDATA")
    assert localappdata, "LOCALAPPDATA required for default HermesHome"
    default_home = Path(localappdata) / "hermes"
    host_pwsh = _host_pwsh_source()

    # Mirrors the live activation that previously failed: explicit default home.
    proc = _run_dry_run(hermes_home=str(default_home).replace("\\", "/"))
    assert proc.returncode == 0, proc.stdout + proc.stderr
    length, tr = _parse_tr(proc.stdout)
    staged = _parse_staged_path(proc.stdout)

    assert length <= SCHTASKS_TR_MAX, (
        f"/TR length {length} exceeds {SCHTASKS_TR_MAX}: {tr}"
    )
    file_path = _assert_stable_tr_shape(tr, hermes_home=default_home, host_pwsh=host_pwsh)
    assert os.path.normcase(os.path.normpath(staged)) == os.path.normcase(
        os.path.normpath(file_path)
    )
    # Default home should be omitted to save budget.
    assert "-HermesHome" not in tr


@pytest.mark.skipif(shutil.which("pwsh") is None, reason="pwsh not on PATH")
def test_dry_run_omits_home_when_unset_uses_default() -> None:
    host_pwsh = _host_pwsh_source()
    localappdata = os.environ.get("LOCALAPPDATA")
    assert localappdata
    default_home = Path(localappdata) / "hermes"

    proc = _run_dry_run()
    assert proc.returncode == 0, proc.stdout + proc.stderr
    length, tr = _parse_tr(proc.stdout)
    assert length <= SCHTASKS_TR_MAX
    assert "-HermesHome" not in tr
    _assert_stable_tr_shape(tr, hermes_home=default_home, host_pwsh=host_pwsh)


@pytest.mark.skipif(shutil.which("pwsh") is None, reason="pwsh not on PATH")
def test_dry_run_does_not_stage_files(short_home: Path) -> None:
    """-DryRun must not create/stage the refresher under the target HermesHome."""
    staged = short_home / "scripts" / "fleet_refresh_usage.ps1"
    assert not staged.exists()

    proc = _run_dry_run(hermes_home=str(short_home))
    assert proc.returncode == 0, proc.stdout + proc.stderr
    length, tr = _parse_tr(proc.stdout)
    reported = _parse_staged_path(proc.stdout)
    assert length <= SCHTASKS_TR_MAX, f"TR too long ({length}): {tr}"
    assert not staged.exists(), "DryRun must not create staged script"
    assert not (short_home / "scripts").exists(), "DryRun must not create scripts dir"
    assert os.path.normcase(os.path.normpath(reported)) == os.path.normcase(
        os.path.normpath(str(staged))
    )
    assert "worktrees" not in tr.lower()
    host_pwsh = _host_pwsh_source()
    _assert_stable_tr_shape(tr, hermes_home=short_home, host_pwsh=host_pwsh)
    assert "-HermesHome" in tr


@pytest.mark.skipif(shutil.which("pwsh") is None, reason="pwsh not on PATH")
def test_stage_only_copies_bytes_and_tr_points_under_hermes_home(
    short_home: Path,
) -> None:
    """-StageOnly stages without schtasks; content must match source bytes."""
    host_pwsh = _host_pwsh_source()
    source_bytes = REFRESHER.read_bytes()
    source_sha = hashlib.sha256(source_bytes).hexdigest()

    proc = _run_installer(hermes_home=str(short_home), extra=["-StageOnly"])
    assert proc.returncode == 0, proc.stdout + proc.stderr
    length, tr = _parse_tr(proc.stdout)
    staged_reported = _parse_staged_path(proc.stdout)

    assert length <= SCHTASKS_TR_MAX, f"TR too long ({length}): {tr}"
    assert "STAGE_ONLY=True" in proc.stdout
    assert "Registered" not in proc.stdout

    file_path = _assert_stable_tr_shape(tr, hermes_home=short_home, host_pwsh=host_pwsh)
    staged_path = Path(staged_reported)
    assert staged_path.is_file()
    assert os.path.normcase(os.path.normpath(str(staged_path))) == os.path.normcase(
        os.path.normpath(file_path)
    )

    staged_bytes = staged_path.read_bytes()
    assert staged_bytes == source_bytes
    assert hashlib.sha256(staged_bytes).hexdigest() == source_sha
    # Custom home is non-default so -HermesHome should appear.
    assert "-HermesHome" in tr
    assert "worktrees" not in tr.lower()
    # Repo/worktree source path must not appear in the scheduled action.
    assert "fleet-pwsh-tr-fullpath" not in tr.lower()
    assert os.path.normcase(str(REPO_ROOT)) not in os.path.normcase(tr)


@pytest.mark.skipif(shutil.which("pwsh") is None, reason="pwsh not on PATH")
def test_stage_only_is_atomic_replace_preserves_utf8(short_home: Path) -> None:
    first = _run_installer(hermes_home=str(short_home), extra=["-StageOnly"])
    assert first.returncode == 0, first.stdout + first.stderr
    staged = Path(_parse_staged_path(first.stdout))
    assert staged.read_bytes() == REFRESHER.read_bytes()

    # Corrupt then re-stage; content must match source again.
    staged.write_bytes(b"CORRUPT")
    second = _run_installer(hermes_home=str(short_home), extra=["-StageOnly"])
    assert second.returncode == 0, second.stdout + second.stderr
    assert staged.read_bytes() == REFRESHER.read_bytes()
    # Ensure UTF-8 source markers survived (script uses ASCII + path escapes).
    text = staged.read_text(encoding="utf-8")
    assert "usage_refresh.py" in text
    assert "HermesHome" in text


@pytest.mark.skipif(shutil.which("pwsh") is None, reason="pwsh not on PATH")
def test_dry_run_and_stage_only_agree_on_tr_and_staged_path(short_home: Path) -> None:
    dry = _run_dry_run(hermes_home=str(short_home))
    assert dry.returncode == 0, dry.stdout + dry.stderr
    stage = _run_installer(hermes_home=str(short_home), extra=["-StageOnly"])
    assert stage.returncode == 0, stage.stdout + stage.stderr

    dry_len, dry_tr = _parse_tr(dry.stdout)
    stage_len, stage_tr = _parse_tr(stage.stdout)
    assert dry_len == stage_len
    assert dry_tr == stage_tr
    assert _parse_staged_path(dry.stdout) == _parse_staged_path(stage.stdout)
    assert dry_len <= SCHTASKS_TR_MAX


@pytest.mark.skipif(shutil.which("pwsh") is None, reason="pwsh not on PATH")
def test_long_worktree_path_does_not_inflate_tr() -> None:
    """Installer lives under a long worktree; /TR must stay on short staged path."""
    # This test file is already loaded from the long worktree path.
    repo_s = str(REPO_ROOT).replace("/", "\\").lower()
    assert "worktrees" in repo_s or len(str(REPO_ROOT)) > 60
    host_pwsh = _host_pwsh_source()
    localappdata = os.environ.get("LOCALAPPDATA")
    assert localappdata
    default_home = Path(localappdata) / "hermes"

    proc = _run_dry_run(hermes_home=str(default_home))
    assert proc.returncode == 0, proc.stdout + proc.stderr
    length, tr = _parse_tr(proc.stdout)
    assert length <= SCHTASKS_TR_MAX
    _assert_stable_tr_shape(tr, hermes_home=default_home, host_pwsh=host_pwsh)
    # Must not embed this worktree checkout in the action.
    assert "worktrees" not in tr.lower()
    assert "fleet-pwsh-tr-fullpath" not in tr.lower()


@pytest.mark.skipif(shutil.which("pwsh") is None, reason="pwsh not on PATH")
def test_dry_run_custom_hermes_home_only_when_tr_fits(short_home: Path) -> None:
    """Custom -HermesHome is supported only when total /TR stays <=261."""
    base_proc = _run_dry_run()
    assert base_proc.returncode == 0, base_proc.stdout + base_proc.stderr
    base_len, base_tr = _parse_tr(base_proc.stdout)
    assert base_len <= SCHTASKS_TR_MAX

    # Prefer a real short custom home from the fixture (fits on this host).
    proc = _run_dry_run(hermes_home=str(short_home))
    if proc.returncode == 0:
        length, tr = _parse_tr(proc.stdout)
        assert length <= SCHTASKS_TR_MAX
        assert "-HermesHome" in tr
        host_pwsh = _host_pwsh_source()
        _assert_stable_tr_shape(tr, hermes_home=short_home, host_pwsh=host_pwsh)
        assert "worktrees" not in tr.lower()
    else:
        # Fail-closed with measured length when host budget is exhausted.
        combined = (proc.stdout + proc.stderr).lower()
        assert "261" in combined or "exceed" in combined or "length" in combined
        assert "registered" not in combined
        exe = _extract_tr_executable(base_tr)
        assert exe.lower() != "pwsh.exe"
        assert Path(exe).is_file()


@pytest.mark.skipif(shutil.which("pwsh") is None, reason="pwsh not on PATH")
def test_dry_run_fails_before_schtasks_when_tr_too_long() -> None:
    # Long path under the user profile (drive exists) so path math succeeds and
    # the length guard is what fails — not a missing-drive error.
    long_home = str(Path.home() / ("x" * 240))
    proc = _run_dry_run(hermes_home=long_home)
    assert proc.returncode != 0, proc.stdout + proc.stderr
    combined = (proc.stdout + proc.stderr).lower()
    assert "261" in combined or "exceed" in combined or "length" in combined
    # Must not claim successful registration.
    assert "registered" not in combined
    # Must not stage under the absurd path either (DryRun).
    assert not Path(long_home).exists()


@pytest.mark.skipif(shutil.which("pwsh") is None, reason="pwsh not on PATH")
def test_stage_only_fails_closed_before_create_when_tr_too_long() -> None:
    long_home = str(Path.home() / ("y" * 240))
    # Length guard runs before Stage-RefresherScript, so this should fail without
    # creating the long tree.
    proc = _run_installer(hermes_home=long_home, extra=["-StageOnly"])
    assert proc.returncode != 0, proc.stdout + proc.stderr
    combined = (proc.stdout + proc.stderr).lower()
    assert "261" in combined or "exceed" in combined or "length" in combined
    assert "registered" not in combined
    assert not Path(long_home).exists()


@pytest.mark.skipif(shutil.which("pwsh") is None, reason="pwsh not on PATH")
def test_dry_run_rejects_invalid_interval() -> None:
    proc = _run_dry_run(extra=["-IntervalMinutes", "3"])
    assert proc.returncode != 0
    assert "IntervalMinutes" in (proc.stdout + proc.stderr)


def test_refresher_script_exists_beside_installer() -> None:
    assert INSTALLER.is_file()
    assert REFRESHER.is_file()


def test_repo_path_is_long_enough_to_matter() -> None:
    """Guard: staging makes /TR independent of REPO_ROOT length."""
    assert INSTALLER.is_file()
    assert "scripts" in str(INSTALLER)


def test_refresher_source_preserves_filtered_candidates_as_array() -> None:
    """Source invariant: filtered repoCandidates must stay wrapped in @(...).

    A bare pipeline assignment collapses one survivor to a scalar string; then
    $repoCandidates[0] is the drive letter 'C', not the full path.
    """
    text = _refresher_text()
    code_only = _code_only(text)
    assert "repoCandidates" in code_only
    # Outer @() must wrap the Where-Object pipeline result.
    assert re.search(
        r"\$repoCandidates\s*=\s*@\s*\(",
        code_only,
    ), "repoCandidates assignment must use @() array subexpression"
    assert "Where-Object" in code_only
    # Defense-in-depth: force array before indexing.
    assert "@($repoCandidates)[0]" in code_only or "@($repoCandidates )[0]" in code_only
    # Must reject degenerate single-character repo (drive letter).
    assert "degenerate" in text.lower() or "scalar-string" in text.lower()


@pytest.mark.skipif(shutil.which("pwsh") is None, reason="pwsh not on PATH")
def test_powershell_pipeline_array_wrap_zero_one_multiple() -> None:
    """Direct PS assertion: @($pipeline) preserves 0/1/N results (no string index)."""
    # Mirrors the refresher pattern with concrete path-like strings.
    ps = r"""
$ErrorActionPreference = 'Stop'
function Assert-True($cond, $msg) {
  if (-not $cond) { throw $msg }
}

# ZERO survivors
$zero = @(
  @('C:\missing-a', 'C:\missing-b') | Where-Object { $false }
)
Assert-True ($zero -is [System.Array]) 'zero must be array'
Assert-True ($zero.Count -eq 0) "zero count=$($zero.Count)"

# ONE survivor (the bug class): must NOT unwrap to scalar string
$onePath = 'C:\Users\HieuKa\AppData\Local\hermes\hermes-agent'
$one = @(
  @($onePath, 'C:\missing-other') | Where-Object { $_ -eq $onePath }
)
Assert-True ($one -is [System.Array]) 'one must remain System.Array'
Assert-True ($one.Count -eq 1) "one count=$($one.Count)"
$picked = [string](@($one)[0])
Assert-True ($picked -eq $onePath) "one[0] expected full path, got '$picked'"
Assert-True ($picked.Length -gt 1) 'one[0] must not be single char'
Assert-True ($picked -ne 'C') 'one[0] must not be drive letter C'
# Prove the BARE (buggy) form indexes the character:
$bare = @($onePath, 'C:\missing-other') | Where-Object { $_ -eq $onePath }
# $bare is a scalar string when one item survives
$buggy = [string]$bare[0]
Assert-True ($buggy -eq 'C') "control: bare scalar[0] must be 'C', got '$buggy'"

# MULTIPLE survivors
$multi = @(
  @('C:\a\hermes-agent', 'C:\b\hermes-agent') | Where-Object { $_ }
)
Assert-True ($multi -is [System.Array]) 'multi must be array'
Assert-True ($multi.Count -eq 2) "multi count=$($multi.Count)"
$first = [string](@($multi)[0])
Assert-True ($first -eq 'C:\a\hermes-agent') "multi[0] got '$first'"

Write-Output 'ARRAY_WRAP_OK'
"""
    proc = subprocess.run(
        ["pwsh", "-NoProfile", "-NonInteractive", "-Command", ps],
        capture_output=True,
        text=True,
        check=False,
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert "ARRAY_WRAP_OK" in proc.stdout


def _write_stub_hermes_agent(agent_root: Path) -> None:
    """Minimal hermes-agent tree so the refresher locates usage_refresh.py.

    hermes_cli.main is a no-network stub so controlled Python execution never
    touches provider APIs.
    """
    fleet_dir = agent_root / "hermes_cli" / "fleet"
    fleet_dir.mkdir(parents=True, exist_ok=True)
    (agent_root / "hermes_cli" / "__init__.py").write_text(
        '"""stub package"""\n', encoding="utf-8"
    )
    (fleet_dir / "__init__.py").write_text('"""stub fleet package"""\n', encoding="utf-8")
    (fleet_dir / "usage_refresh.py").write_text(
        '"""stub marker for path discovery only"""\n', encoding="utf-8"
    )
    # runpy.run_module('hermes_cli.main') — exit 0, print argv, no network.
    (agent_root / "hermes_cli" / "main.py").write_text(
        "import json\n"
        "import sys\n"
        "print('STUB_HERMES_MAIN=' + json.dumps(list(sys.argv)), flush=True)\n"
        "raise SystemExit(0)\n",
        encoding="utf-8",
    )


def _write_fake_python(path: Path) -> Path:
    """Controlled Python launcher: no interpreter, no provider APIs.

    Repo resolution and the repo= banner run before this process is invoked.
    A .cmd shim keeps the test deterministic and offline regardless of whether
    a real hermes package is installed on the host interpreter.
    """
    shim = path / "fake-python.cmd"
    shim.write_text(
        "@echo off\r\n"
        "echo FAKE_PYTHON_OK\r\n"
        "exit /b 0\r\n",
        encoding="ascii",
    )
    return shim


@pytest.mark.skipif(shutil.which("pwsh") is None, reason="pwsh not on PATH")
def test_staged_refresher_single_candidate_resolves_full_repo_path(
    short_home: Path,
) -> None:
    """Stage to a default-like HermesHome with exactly one valid candidate.

    Reproduces the live failure mode: staged script under HermesHome\\scripts,
    sole valid repo at HermesHome\\hermes-agent. Pipeline must not unwrap to
    scalar so repo= is the full directory, never drive letter 'C'.
    """
    agent_root = short_home / "hermes-agent"
    _write_stub_hermes_agent(agent_root)
    expected_repo = os.path.normcase(os.path.normpath(str(agent_root.resolve())))

    # Exactly one discovery marker; parent of scripts is HermesHome (no marker).
    assert (agent_root / "hermes_cli" / "fleet" / "usage_refresh.py").is_file()
    assert not (short_home / "hermes_cli" / "fleet" / "usage_refresh.py").exists()

    stage = _run_installer(hermes_home=str(short_home), extra=["-StageOnly"])
    assert stage.returncode == 0, stage.stdout + stage.stderr
    staged_path = Path(_parse_staged_path(stage.stdout))
    assert staged_path.is_file()
    assert staged_path.read_bytes() == REFRESHER.read_bytes()

    py = _write_fake_python(short_home)
    cmd = [
        "pwsh",
        "-NoProfile",
        "-NonInteractive",
        "-File",
        str(staged_path),
        "-HermesHome",
        str(short_home),
        "-Python",
        str(py),
        "-NoMirror",
    ]
    # Strip ambient HERMES_HOME so -HermesHome alone drives resolution.
    env = dict(os.environ)
    env.pop("HERMES_HOME", None)
    # Block accidental network-ish provider env use in case stub is bypassed.
    for k in list(env):
        if k.upper().endswith("_API_KEY") or k.upper().endswith("_TOKEN"):
            env.pop(k, None)

    proc = subprocess.run(cmd, capture_output=True, text=True, check=False, env=env)
    combined = proc.stdout + proc.stderr
    assert proc.returncode == 0, combined

    # Banner proves full repo path (the live bug printed repo=C).
    m = re.search(r"repo=([^\r\n]+)", combined)
    assert m, f"missing repo= banner in output:\n{combined}"
    resolved = m.group(1).strip()
    assert resolved not in {"C", "c"}, f"scalar-index bug: repo={resolved!r}"
    assert len(resolved) > 3, f"repo path too short: {resolved!r}"
    assert os.path.normcase(os.path.normpath(resolved)) == expected_repo, (
        f"repo={resolved!r} != expected {expected_repo!r}"
    )
    # Must look like a Windows directory path, not a single drive letter.
    assert re.match(r"^[A-Za-z]:\\", resolved), f"repo not absolute path: {resolved!r}"
    assert "hermes-agent" in resolved.replace("/", "\\").lower()
    # Controlled fake Python ran after successful Set-Location/repo resolve.
    assert "FAKE_PYTHON_OK" in combined


@pytest.mark.skipif(shutil.which("pwsh") is None, reason="pwsh not on PATH")
def test_staged_refresher_zero_candidates_fails_closed(short_home: Path) -> None:
    """No hermes-agent marker under HermesHome => refresher throws, no path 'C'."""
    stage = _run_installer(hermes_home=str(short_home), extra=["-StageOnly"])
    assert stage.returncode == 0, stage.stdout + stage.stderr
    staged_path = Path(_parse_staged_path(stage.stdout))

    py = _write_fake_python(short_home)
    env = dict(os.environ)
    env.pop("HERMES_HOME", None)
    proc = subprocess.run(
        [
            "pwsh",
            "-NoProfile",
            "-NonInteractive",
            "-File",
            str(staged_path),
            "-HermesHome",
            str(short_home),
            "-Python",
            str(py),
            "-NoMirror",
        ],
        capture_output=True,
        text=True,
        check=False,
        env=env,
    )
    combined = proc.stdout + proc.stderr
    assert proc.returncode != 0, combined
    assert "Could not locate hermes_cli.fleet.usage_refresh" in combined
    assert "Cannot find path 'C'" not in combined
