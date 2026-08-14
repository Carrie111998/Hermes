import os
from pathlib import Path
import shutil
import subprocess

import pytest


ROOT = Path(__file__).resolve().parents[2]
SH_WRAPPER = ROOT / "scripts" / "dgx_ssh.sh"
PS_WRAPPER = ROOT / "scripts" / "dgx_ssh.ps1"


def usable_wsl() -> str | None:
    """Return wsl.exe only when the Ubuntu distro is actually available."""
    wsl = shutil.which("wsl.exe")
    if wsl is None:
        return None
    probe = subprocess.run(
        [wsl, "-d", "Ubuntu", "--", "true"],
        text=True,
        capture_output=True,
        check=False,
    )
    return wsl if probe.returncode == 0 else None


def git_bash_path(path: Path) -> str:
    """Convert a Windows path to the MSYS form expected by Git Bash."""
    if os.name != "nt":
        return str(path)
    absolute = path.resolve().as_posix()
    drive, remainder = absolute.split(":", 1)
    return f"/{drive.lower()}{remainder}"


def run_shell_wrapper(tmp_path: Path, mode: str, *args: str) -> subprocess.CompletedProcess[str]:
    wsl = usable_wsl()
    bash = shutil.which("bash")
    if wsl is None and bash is None:
        pytest.skip("bash or an operational Ubuntu WSL distro is required for wrapper behavior tests")

    fake_ssh = tmp_path / "ssh"
    fake_ssh_content = """#!/usr/bin/env bash
set -u
if [[ -n "${FAKE_SSH_LOG:-}" ]]; then
    printf '<%s>\\n' "$@" >> "${FAKE_SSH_LOG}"
fi
case "${FAKE_SSH_MODE:-success}" in
    permission|bootstrap-keygen-failure)
        printf '%s\\n' 'Permission denied (publickey).' >&2
        exit 255
        ;;
    hostkey)
        printf '%s\\n' 'Host key verification failed.' >&2
        exit 255
        ;;
    success)
        if [[ "${@: -1}" == "true" ]]; then
            exit 0
        fi
        printf '%s\\n' 'SSH_OK' 'dgx-test' 'cwliao'
        exit 0
        ;;
    remote-75)
        if [[ "${@: -1}" == "true" ]]; then
            exit 0
        fi
        printf '%s\\n' 'REMOTE_COMMAND_EXIT_75'
        exit 75
        ;;
    *)
        exit 99
        ;;
esac
"""
    with fake_ssh.open("w", encoding="utf-8", newline="\n") as stream:
        stream.write(fake_ssh_content)
    fake_ssh.chmod(0o755)
    fake_windows_ssh = tmp_path / "ssh.cmd"
    fake_windows_ssh.write_text(
        '@echo off\r\n'
        'bash "%~dp0ssh" %*\r\n'
        'exit /b %ERRORLEVEL%\r\n',
        encoding="utf-8",
        newline="",
    )
    if mode == "bootstrap-keygen-failure":
        fake_keygen = tmp_path / "ssh-keygen"
        with fake_keygen.open("w", encoding="utf-8", newline="\n") as stream:
            stream.write("#!/usr/bin/env bash\nexit 42\n")
        fake_keygen.chmod(0o755)
        (tmp_path / "ssh-keygen.cmd").write_text(
            "@echo off\r\nexit /b 42\r\n",
            encoding="utf-8",
            newline="",
        )
    bash_env = tmp_path / "bash-env"
    bash_env.write_text(
        f'ssh() {{ bash "{git_bash_path(fake_ssh)}" "$@"; }}\n'
        + (
            f'ssh-keygen() {{ bash "{git_bash_path(tmp_path / "ssh-keygen")}" "$@"; }}\n'
            if mode == "bootstrap-keygen-failure"
            else ""
        ),
        encoding="utf-8",
        newline="\n",
    )
    env = os.environ.copy()
    log_path = tmp_path / "ssh-args.log"
    if wsl is not None:
        def wslpath(path: Path) -> str:
            converted = subprocess.run(
                [wsl, "-d", "Ubuntu", "--", "wslpath", "-a", str(path).replace("\\", "/")],
                text=True,
                capture_output=True,
                check=False,
            )
            assert converted.returncode == 0, converted.stderr
            return converted.stdout.strip()

        wrapper_path = wslpath(SH_WRAPPER)
        fake_path = wslpath(fake_ssh)
        wsl_tmp = wslpath(tmp_path)
        wsl_log = wslpath(log_path)
        subprocess.run([wsl, "-d", "Ubuntu", "--", "chmod", "+x", fake_path], check=True)
        wsl_env = "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"
        return subprocess.run(
            [
                wsl,
                "-d",
                "Ubuntu",
                "--",
                "env",
                f"PATH={wsl_tmp}:{wsl_env}",
                f"HOME={wsl_tmp}",
                f"XDG_RUNTIME_DIR={wsl_tmp}",
                f"FAKE_SSH_MODE={mode}",
                f"FAKE_SSH_LOG={wsl_log}",
                "bash",
                wrapper_path,
                *args,
            ],
            cwd=ROOT,
            text=True,
            capture_output=True,
            check=False,
        )

    bash_tmp = git_bash_path(tmp_path)
    bash_log = git_bash_path(log_path)
    env["PATH"] = f"{bash_tmp}:/usr/bin:/bin"
    env["HOME"] = bash_tmp
    env["XDG_RUNTIME_DIR"] = "/tmp" if wsl is None else str(tmp_path)
    env["BASH_ENV"] = git_bash_path(bash_env)
    env["FAKE_SSH_MODE"] = mode
    env["FAKE_SSH_LOG"] = bash_log
    return subprocess.run(
        [bash, str(SH_WRAPPER).replace("\\", "/"), *args],
        cwd=ROOT,
        env=env,
        text=True,
        capture_output=True,
        check=False,
    )


def test_dgx_wrapper_is_fail_closed_and_does_not_store_credentials():
    text = SH_WRAPPER.read_text(encoding="utf-8")

    assert 'readonly DGX_HOST="140.96.58.171"' in text
    assert "StrictHostKeyChecking=yes" in text
    assert "ssh-copy-id" in text
    assert "-o StrictHostKeyChecking=yes" in text
    assert "BatchMode=yes" in text
    assert "ControlPersist=10m" in text
    assert "REAUTH_REQUIRED" in text
    assert "SSH_HOST_KEY_ERROR" in text
    assert "Host key verification failed" not in text
    assert "classify_ssh_failure" in text
    assert "return 75" in text
    assert "BOOTSTRAP_KEYGEN_FAILED" in text
    assert "return 70" in text
    assert "sshpass" not in text
    assert "expect" not in text
    assert "dangerously-skip-permissions" not in text
    assert "PRIVATE_KEY" not in text
    assert "PASSWORD" not in text


def test_windows_entrypoint_delegates_to_one_wsl_policy():
    text = PS_WRAPPER.read_text(encoding="utf-8")

    assert "dgx_ssh.sh" in text
    assert "wsl.exe -d Ubuntu -- bash" in text
    assert "System32\\OpenSSH\\ssh.exe" in text
    assert "StrictHostKeyChecking=yes" in text
    assert "REAUTH_REQUIRED" in text
    assert "Get-SshFailureStatus" in text
    assert "host key verification failed" in text
    assert "if ($wslStatus -ne 75)" in text
    assert "both WSL and native Windows SSH routes failed" in text
    assert '"-o" "BatchMode=yes"' in text
    assert "wslOutput" in text
    assert "HERMES_DGX_IDENTITY" in text
    assert "Invoke-NativeProbe" in text
    assert ".Status" in text
    assert ".Output" in text
    assert "BLOCKED_WSL_UNAVAILABLE" in text


def test_shell_probe_classifies_permission_denied_as_reauthentication(tmp_path):
    result = run_shell_wrapper(tmp_path, "permission", "probe")

    assert result.returncode == 75
    assert "REAUTH_REQUIRED" in result.stderr


def test_shell_probe_preserves_host_key_failure_classification(tmp_path):
    result = run_shell_wrapper(tmp_path, "hostkey", "probe")

    assert result.returncode == 255
    assert "SSH_HOST_KEY_ERROR" in result.stderr
    assert "REAUTH_REQUIRED" not in result.stderr


def test_shell_exec_forwards_remote_command_arguments(tmp_path):
    result = run_shell_wrapper(tmp_path, "success", "exec", "echo", "hello")
    log = (tmp_path / "ssh-args.log").read_text(encoding="utf-8")

    assert result.returncode == 0
    assert "<echo>" in log
    assert "<hello>" in log


def test_shell_exec_preserves_remote_exit_75_without_reclassification(tmp_path):
    result = run_shell_wrapper(tmp_path, "remote-75", "exec", "remote-command")

    assert result.returncode == 75
    assert "REMOTE_COMMAND_EXIT_75" in result.stdout
    assert "REAUTH_REQUIRED" not in result.stderr


def test_windows_probe_falls_back_without_polluting_output_or_losing_identity(tmp_path):
    powershell = shutil.which("powershell.exe")
    if powershell is None:
        pytest.skip("Windows PowerShell is required for Windows wrapper behavior tests")

    fake_ssh = tmp_path / "ssh.cmd"
    fake_ssh.write_text(
        "@echo off\r\n"
        ">>\"%FAKE_SSH_LOG%\" echo %*\r\n"
        "echo SSH_OK\r\n"
        "echo dgx-test\r\n"
        "echo cwliao\r\n"
        "exit /b 0\r\n",
        encoding="utf-8",
        newline="",
    )
    identity = tmp_path / "configured id"
    identity.write_text("test-only-placeholder", encoding="utf-8")
    log_path = tmp_path / "native-ssh.log"
    wrapper_copy = tmp_path / "dgx_ssh.ps1"
    (tmp_path / "dgx_ssh.sh").write_text("#!/usr/bin/env bash\n", encoding="utf-8", newline="\n")
    original_assignment = '$sshExe = Join-Path $env:WINDIR "System32\\OpenSSH\\ssh.exe"'
    replacement_assignment = f'$sshExe = "{fake_ssh}"'
    wrapper_copy.write_text(
        PS_WRAPPER.read_text(encoding="utf-8").replace(original_assignment, replacement_assignment),
        encoding="utf-8",
        newline="\n",
    )
    helper = tmp_path / "invoke-wrapper.ps1"
    helper.write_text(
        "function global:wsl.exe {\n"
        "    param([Parameter(ValueFromRemainingArguments = $true)][object[]]$Args)\n"
        "    if ($Args -contains 'wslpath') {\n"
        "        if ($env:FAKE_WSL_MODE -eq 'path-failure') {\n"
        "            $global:LASTEXITCODE = 1\n"
        "            return\n"
        "        }\n"
        "        $global:LASTEXITCODE = 0\n"
        "        Write-Output '/mnt/fake/scripts/dgx_ssh.sh'\n"
        "        return\n"
        "    }\n"
        "    if ($Args -contains 'exec' -and $env:FAKE_WSL_MODE -eq 'remote-exit-75') {\n"
        "        $global:LASTEXITCODE = 75\n"
        "        Write-Output 'HERMES_DGX_REMOTE_EXIT_STATUS=75'\n"
        "        return\n"
        "    }\n"
        "    $global:LASTEXITCODE = 75\n"
        "    Write-Output 'WSL_REAUTH_REQUIRED'\n"
        "}\n"
        "$mode = if ([string]::IsNullOrWhiteSpace($env:FAKE_MODE)) { 'probe' } else { $env:FAKE_MODE }\n"
        f'if ($mode -eq "exec") {{ '
            f'if ($env:FAKE_MULTIWORD -eq "1") {{ & "{wrapper_copy}" -Mode exec bash -c "\'echo hi there\'" }} '
            f'else {{ & "{wrapper_copy}" -Mode exec hostname }} }} '
            f'elseif ($mode -eq "auth") {{ & "{wrapper_copy}" -Mode auth }} '
            f'else {{ & "{wrapper_copy}" -Mode probe }}\n'
            "if ($env:FAKE_WSL_MODE -eq 'remote-exit-75') { exit 75 }\n"
            "exit $LASTEXITCODE\n",
        encoding="utf-8",
        newline="\n",
    )
    env = os.environ.copy()
    env["FAKE_SSH_LOG"] = str(log_path)
    env["FAKE_WSL_MODE"] = "auth-failure"
    env["HERMES_DGX_IDENTITY"] = str(identity)
    env["FAKE_MODE"] = "probe"
    first = subprocess.run(
        [powershell, "-NoProfile", "-File", str(helper)],
        env=env,
        text=True,
        capture_output=True,
        check=False,
    )

    assert first.returncode == 0, first.stderr
    assert first.stdout.count("SSH_OK") == 1
    assert "WSL_REAUTH_REQUIRED" not in first.stdout
    assert str(identity) in log_path.read_text(encoding="utf-8")

    env["FAKE_WSL_MODE"] = "path-failure"
    second = subprocess.run(
        [powershell, "-NoProfile", "-File", str(helper)],
        env=env,
        text=True,
        capture_output=True,
        check=False,
    )

    assert second.returncode == 0, second.stderr
    assert second.stdout.count("SSH_OK") == 1

    env["FAKE_MODE"] = "exec"
    third = subprocess.run(
        [powershell, "-NoProfile", "-File", str(helper)],
        env=env,
        text=True,
        capture_output=True,
        check=False,
    )

    assert third.returncode == 0, third.stderr
    assert "hostname" in log_path.read_text(encoding="utf-8")

    env["FAKE_MULTIWORD"] = "1"
    sixth = subprocess.run(
        [powershell, "-NoProfile", "-File", str(helper)],
        env=env,
        text=True,
        capture_output=True,
        check=False,
    )

    assert sixth.returncode == 0, sixth.stderr
    multiword_log = log_path.read_text(encoding="utf-8")
    assert "bash" in multiword_log
    assert "-c" in multiword_log
    assert "'echo hi there'" in multiword_log

    env["FAKE_MODE"] = "auth"
    fourth = subprocess.run(
        [powershell, "-NoProfile", "-File", str(helper)],
        env=env,
        text=True,
        capture_output=True,
        check=False,
    )

    assert fourth.returncode == 0, fourth.stderr
    assert "AUTH_OK: native Windows SSH route" in fourth.stdout
    assert "BatchMode=no" in log_path.read_text(encoding="utf-8")

    before_remote_75 = log_path.read_text(encoding="utf-8")
    env["FAKE_MODE"] = "exec"
    env["FAKE_WSL_MODE"] = "remote-exit-75"
    fifth = subprocess.run(
        [powershell, "-NoProfile", "-File", str(helper)],
        env=env,
        text=True,
        capture_output=True,
        check=False,
    )

    assert fifth.returncode == 75
    assert "HERMES_DGX_REMOTE_EXIT_STATUS=75" not in fifth.stdout
    assert log_path.read_text(encoding="utf-8") == before_remote_75


def test_shell_bootstrap_distinguishes_keygen_failure_from_reauthentication(tmp_path):
    result = run_shell_wrapper(tmp_path, "bootstrap-keygen-failure", "bootstrap")

    assert result.returncode == 70
    assert "BOOTSTRAP_KEYGEN_FAILED" in result.stderr
    assert "REAUTH_REQUIRED" not in result.stderr
