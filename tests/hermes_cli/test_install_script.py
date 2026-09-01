import os
from pathlib import Path
import shutil
import subprocess
import sys

import pytest


REPO_ROOT = Path(__file__).resolve().parents[2]
INSTALL_SCRIPT = REPO_ROOT / "scripts" / "install.sh"
INSTALL_PS1 = REPO_ROOT / "scripts" / "install.ps1"
POWERSHELL = next(
    (candidate for candidate in ("pwsh", "powershell") if shutil.which(candidate)),
    None,
)


def _make_executable(path: Path, content: str) -> Path:
    path.write_text(content, encoding="utf-8")
    path.chmod(0o755)
    return path


def _make_python(path: Path, version: str) -> Path:
    return _make_executable(
        path,
        f"""#!/bin/bash
if [ "$1" = "--version" ]; then
    echo "Python {version}"
    exit 0
fi
exit 0
""",
    )


def _run_venv_stage(
    tmp_path: Path,
    *,
    compatible_python: Path | None,
) -> tuple[subprocess.CompletedProcess[str], list[list[str]], dict[str, Path]]:
    test_home = tmp_path / "home"
    hermes_home = tmp_path / "hermes-home"
    install_dir = tmp_path / "install"
    managed_bin = hermes_home / "bin"
    for directory in (test_home, managed_bin, install_dir):
        directory.mkdir(parents=True)

    python311 = _make_python(tmp_path / "python311", "3.11.12")
    python314 = _make_python(tmp_path / "python314", "3.14.0")
    install_marker = tmp_path / "python-installed"
    call_log = tmp_path / "uv-calls.tsv"

    compatible_result = (
        f'echo "{compatible_python}"\n    exit 0'
        if compatible_python is not None
        else "exit 1"
    )
    _make_executable(
        managed_bin / "uv",
        f"""#!/bin/bash
set -e
{{
    printf '%s' "${{UV_PYTHON:-}}"
    for arg in "$@"; do
        printf '\\t%s' "$arg"
    done
    printf '\\n'
}} >> "{call_log}"

if [ "$1" = "--version" ]; then
    echo "uv 0.test"
    exit 0
fi

if [ "$1" = "python" ] && [ "$2" = "find" ] && [ "$3" = "3.11" ]; then
    if [ -f "{install_marker}" ]; then
        echo "{python311}"
        exit 0
    fi
    exit 1
fi

if [ "$1" = "python" ] && [ "$2" = "find" ] && [ "$3" = ">=3.11,<3.14" ]; then
    {compatible_result}
fi

if [ "$1" = "python" ] && [ "$2" = "install" ] && [ "$3" = "3.11" ]; then
    touch "{install_marker}"
    exit 0
fi

if [ "$1" = "venv" ] && [ "$3" = "--python" ]; then
    mkdir -p "$2/bin"
    cp "$4" "$2/bin/python"
    chmod +x "$2/bin/python"
    exit 0
fi

echo "unexpected uv invocation: $*" >&2
exit 2
""",
    )

    env = os.environ.copy()
    env.update(
        HOME=str(test_home),
        HERMES_HOME=str(hermes_home),
        UV_PYTHON=str(python314),
    )
    result = subprocess.run(
        [
            "bash",
            str(INSTALL_SCRIPT),
            "--stage",
            "venv",
            "--json",
            "--non-interactive",
            "--dir",
            str(install_dir),
        ],
        cwd=REPO_ROOT,
        env=env,
        capture_output=True,
        text=True,
    )

    calls = [
        line.split("\t") for line in call_log.read_text(encoding="utf-8").splitlines()
    ]
    paths = {
        "install_dir": install_dir,
        "install_marker": install_marker,
        "python311": python311,
        "python314": python314,
    }
    return result, calls, paths


def test_install_script_is_valid_shell():
    result = subprocess.run(
        ["bash", "-n", str(INSTALL_SCRIPT)],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr


def test_venv_stage_reuses_existing_supported_python(tmp_path):
    python313 = _make_python(tmp_path / "python313", "3.13.12")

    result, calls, paths = _run_venv_stage(
        tmp_path,
        compatible_python=python313,
    )
    args = [call[1:] for call in calls]

    assert result.returncode == 0, result.stderr
    assert ["python", "find", "3.11"] in args
    assert ["python", "find", ">=3.11,<3.14"] in args
    assert ["python", "install", "3.11"] not in args
    assert ["venv", "venv", "--python", str(python313)] in args
    assert not paths["install_marker"].exists()
    assert (
        subprocess.check_output(
            [paths["install_dir"] / "venv/bin/python", "--version"],
            text=True,
        ).strip()
        == "Python 3.13.12"
    )


def test_venv_stage_rejects_python314_and_installs_supported_floor(tmp_path):
    result, calls, paths = _run_venv_stage(
        tmp_path,
        compatible_python=None,
    )
    args = [call[1:] for call in calls]

    assert result.returncode == 0, result.stderr
    assert ["python", "find", ">=3.11,<3.14"] in args
    assert ["python", "install", "3.11"] in args
    assert ["venv", "venv", "--python", str(paths["python311"])] in args
    assert all(str(paths["python314"]) not in call for call in args)
    assert paths["install_marker"].exists()


@pytest.mark.skipif(POWERSHELL is None, reason="needs PowerShell")
def test_windows_python_stage_reuses_supported_python_before_install(tmp_path):
    call_log = tmp_path / "uv-calls.tsv"
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    fake_uv = fake_bin / "uv.ps1"
    fake_uv.write_text(
        r"""
$line = $args -join "`t"
[System.IO.File]::AppendAllText(
    $env:HERMES_TEST_UV_LOG,
    $line + [Environment]::NewLine
)

if ($args.Count -eq 1 -and $args[0] -eq "--version") {
    Write-Output "uv 0.test"
    exit 0
}

if ($args.Count -eq 3 -and $args[0] -eq "python" -and $args[1] -eq "find") {
    if ($args[2] -eq "3.12") {
        Write-Output $env:HERMES_TEST_COMPATIBLE_PYTHON
        exit 0
    }
    exit 1
}

if ($args.Count -ge 2 -and $args[0] -eq "python" -and $args[1] -eq "install") {
    [System.IO.File]::WriteAllText($env:HERMES_TEST_INSTALL_MARKER, "called")
    exit 0
}

exit 2
""".lstrip(),
        encoding="utf-8",
    )

    install_marker = tmp_path / "python-installed"
    env = os.environ.copy()
    env.update(
        HERMES_TEST_COMPATIBLE_PYTHON=sys.executable,
        HERMES_TEST_INSTALL_MARKER=str(install_marker),
        HERMES_TEST_UV_LOG=str(call_log),
        PATH=os.pathsep.join((str(fake_bin), env["PATH"])),
    )
    result = subprocess.run(
        [
            POWERSHELL,
            "-NoProfile",
            "-ExecutionPolicy",
            "Bypass",
            "-File",
            str(INSTALL_PS1),
            "-Stage",
            "python",
            "-Json",
            "-NonInteractive",
            "-HermesHome",
            str(tmp_path / "hermes-home"),
            "-InstallDir",
            str(tmp_path / "install"),
        ],
        cwd=REPO_ROOT,
        env=env,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0, f"{result.stdout}\n{result.stderr}"
    calls = [
        line.split("\t") for line in call_log.read_text(encoding="utf-8").splitlines()
    ]
    assert ["python", "find", "3.11"] in calls
    assert ["python", "find", "3.12"] in calls
    assert not any(call[:2] == ["python", "install"] for call in calls)
    assert not install_marker.exists()
