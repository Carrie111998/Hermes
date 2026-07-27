"""Tests for PowerShell command wrapping in ssh_pwsh backend."""

import base64
import shutil
import subprocess
from unittest.mock import MagicMock

import pytest

from tools.environments import ssh as ssh_env
from tools.environments import ssh_pwsh as ssh_pwsh_env
from tools.environments.ssh_pwsh import (
    SSHPwshEnvironment,
    _atomic_snapshot_publish,
    _quote_pwsh_string,
    _snapshot_mutex_name,
    _with_snapshot_mutex,
)


def _mock_completed(stdout=b"", stderr=b"", returncode=0):
    return subprocess.CompletedProcess([], returncode, stdout=stdout, stderr=stderr)


@pytest.fixture
def env(monkeypatch):
    monkeypatch.setattr(ssh_env.shutil, "which", lambda _name: "/usr/bin/ssh")
    monkeypatch.setattr(ssh_env.subprocess, "run",
                        lambda *a, **k: _mock_completed(stdout=b"ok\r\n"))
    monkeypatch.setattr(ssh_env.subprocess, "Popen",
                        lambda *a, **k: MagicMock(stdout=iter([]), stderr=iter([]),
                                                  stdin=MagicMock(), returncode=0,
                                                  poll=lambda: 0,
                                                  communicate=lambda **kw: (b"", b"")))
    monkeypatch.setattr(ssh_env.BaseEnvironment, "init_session", lambda self: None)
    monkeypatch.setattr(ssh_env, "FileSyncManager",
                        lambda **kw: type("M", (), {
                            "sync": lambda self, **k: None,
                            "sync_back": lambda self, **k: None,
                        })())
    e = SSHPwshEnvironment(host="h", user="u")
    e._remote_home = "C:\\Users\\test"
    return e


class TestWrapCommand:

    def test_contains_set_location(self, env):
        wrapped = env._wrap_command("dir", "C:\\Users\\test")
        assert "Set-Location" in wrapped
        assert "C:\\Users\\test" in wrapped

    def test_contains_invoke_expression(self, env):
        wrapped = env._wrap_command("Get-ChildItem", "C:\\")
        assert "Invoke-Expression" in wrapped
        assert "Get-ChildItem" in wrapped

    def test_captures_exit_code(self, env):
        wrapped = env._wrap_command("dir", "C:\\")
        assert "$LASTEXITCODE" in wrapped

    def test_emits_cwd_marker(self, env):
        wrapped = env._wrap_command("dir", "C:\\")
        assert env._cwd_marker in wrapped

    def test_sources_snapshot_when_ready(self, env):
        env._snapshot_ready = True
        wrapped = env._wrap_command("dir", "C:\\")
        assert ". " in wrapped
        assert "hermes-snap-" in wrapped
        assert "WaitOne(5000)" in wrapped

    def test_no_source_when_snapshot_not_ready(self, env):
        env._snapshot_ready = False
        wrapped = env._wrap_command("dir", "C:\\")
        assert "hermes-snap-" not in wrapped

    def test_escapes_single_quotes(self, env):
        wrapped = env._wrap_command("echo 'hello'", "C:\\")
        assert "''hello''" in wrapped

    def test_exits_with_captured_code(self, env):
        wrapped = env._wrap_command("dir", "C:\\")
        assert "exit $script:__hermes_ec" in wrapped

    def test_snapshot_update_uses_unique_temp_and_atomic_publish(self, env):
        env._snapshot_ready = True

        wrapped = env._wrap_command("dir", "C:\\")

        temp_path = _quote_pwsh_string(env._snapshot_path + ".tmp.")
        assert f"({temp_path} + $PID)" in wrapped
        assert "[System.IO.File]::Replace(" in wrapped
        assert "[System.IO.File]::Move(" in wrapped
        assert "Start-Sleep -Milliseconds 10" in wrapped
        assert wrapped.index("Set-Content") < wrapped.index("[System.IO.File]::Replace")
        set_content_lines = [
            line for line in wrapped.splitlines() if "Set-Content" in line
        ]
        assert len(set_content_lines) == 1
        assert "Set-Content -Encoding UTF8 -ErrorAction Stop" in set_content_lines[0]
        assert f"-LiteralPath ({temp_path} + $PID)" in set_content_lines[0]
        encoded = env._encode_pwsh_command(wrapped)
        assert len("pwsh -NoProfile -EncodedCommand ") + len(encoded) < 8191

    def test_session_bootstrap_uses_atomic_snapshot_replacement(
        self, env, monkeypatch
    ):
        captured = {}

        def fake_run_bash(command, **kwargs):
            captured["command"] = command
            return MagicMock()

        monkeypatch.setattr(env, "_run_bash", fake_run_bash)
        monkeypatch.setattr(
            env,
            "_wait_for_process",
            lambda *args, **kwargs: {"output": "", "returncode": 0},
        )

        env.init_session()

        bootstrap = captured["command"]
        temp_path = _quote_pwsh_string(env._snapshot_path + ".tmp.")
        assert f"({temp_path} + $PID)" in bootstrap
        assert "[System.IO.File]::Replace(" in bootstrap
        assert "[System.IO.File]::Move(" in bootstrap
        assert "Start-Sleep -Milliseconds 10" in bootstrap
        assert bootstrap.index("Set-Content") < bootstrap.index("[System.IO.File]::Replace")

    def test_session_bootstrap_failure_leaves_snapshot_not_ready(
        self, env, monkeypatch
    ):
        monkeypatch.setattr(env, "_run_bash", lambda *args, **kwargs: MagicMock())
        monkeypatch.setattr(
            env,
            "_wait_for_process",
            lambda *args, **kwargs: {"output": "", "returncode": 1},
        )
        env._snapshot_ready = True

        env.init_session()

        assert env._snapshot_ready is False


@pytest.mark.skipif(shutil.which("pwsh") is None, reason="pwsh required")
def test_concurrent_writers_never_publish_partial_snapshot(tmp_path):
    snapshot = str(tmp_path / "hermes-snapshot.ps1").replace("'", "''")
    mutex_name = _snapshot_mutex_name(str(tmp_path / "hermes-snapshot.ps1"))
    publish = _atomic_snapshot_publish(
        "$temp", "$snapshot", mutex_name, raise_on_failure=True
    )
    read_snapshot = _with_snapshot_mutex(
        mutex_name,
        """$lines = @(Get-Content -ErrorAction Stop -LiteralPath $snapshot)
$begin = $lines[0] -replace '^BEGIN-', ''
$end = $lines[2] -replace '^END-', ''
if ($lines.Count -ne 3 -or
    $lines[0] -notmatch '^BEGIN-' -or
    $lines[2] -notmatch '^END-' -or
    $begin -ne $end) {
    throw 'snapshot contained a partial write'
}""",
        on_timeout="throw 'snapshot read lock timed out'",
    )
    script = rf"""
$ErrorActionPreference = 'Stop'
$snapshot = '{snapshot}'
@('BEGIN-0-0', ('x' * 20000), 'END-0-0') |
    Set-Content -Encoding UTF8 -LiteralPath $snapshot
$jobs = 1..4 | ForEach-Object {{
    $writer = $_
    Start-Job -ScriptBlock {{
        param($snapshot, $writer)
        1..40 | ForEach-Object {{
{read_snapshot}
            $temp = $snapshot + '.tmp.' + $PID
            @(
                "BEGIN-$writer-$_",
                ('x' * 20000),
                "END-$writer-$_"
            ) | Set-Content -Encoding UTF8 -LiteralPath $temp
{publish}
            Start-Sleep -Milliseconds 1
        }}
    }} -ArgumentList $snapshot, $writer
}}
$jobs | Wait-Job | Out-Null
$jobs | Receive-Job -ErrorAction Stop | Out-Null
$jobs | Remove-Job -Force
$lines = @(Get-Content -ErrorAction Stop -LiteralPath $snapshot)
$begin = $lines[0] -replace '^BEGIN-', ''
$end = $lines[2] -replace '^END-', ''
if ($lines.Count -ne 3 -or $begin -ne $end) {{ exit 1 }}
"""

    result = subprocess.run(
        ["pwsh", "-NoProfile", "-Command", script],
        capture_output=True,
        text=True,
        timeout=60,
    )

    assert result.returncode == 0, result.stderr


@pytest.mark.skipif(shutil.which("pwsh") is None, reason="pwsh required")
def test_publish_failure_is_bounded_and_cleans_temp(tmp_path):
    temp = str(tmp_path / "snapshot.tmp").replace("'", "''")
    destination = str(tmp_path / "destination").replace("'", "''")
    (tmp_path / "destination").mkdir()

    for raise_on_failure, expected_code in ((False, 7), (True, 1)):
        mutex_name = _snapshot_mutex_name(destination)
        publish = _atomic_snapshot_publish(
            "$temp",
            "$snapshot",
            mutex_name,
            raise_on_failure=raise_on_failure,
        )
        script = rf"""
$temp = '{temp}'
$snapshot = '{destination}'
Set-Content -Encoding UTF8 -LiteralPath $temp -Value 'complete'
$script:__hermes_ec = 7
{publish}
if (${str(raise_on_failure).lower()}) {{ exit 0 }}
exit $script:__hermes_ec
"""

        result = subprocess.run(
            ["pwsh", "-NoProfile", "-Command", script],
            capture_output=True,
            text=True,
            timeout=10,
        )

        assert result.returncode == expected_code, result.stderr
        assert not (tmp_path / "snapshot.tmp").exists()


@pytest.mark.skipif(shutil.which("pwsh") is None, reason="pwsh required")
def test_publish_lock_timeout_cleans_temp(tmp_path):
    temp = str(tmp_path / "snapshot.tmp").replace("'", "''")
    snapshot = str(tmp_path / "snapshot.ps1").replace("'", "''")
    mutex_name = _snapshot_mutex_name(str(tmp_path / "snapshot.ps1"))
    publish = _atomic_snapshot_publish(
        "$temp", "$snapshot", mutex_name, raise_on_failure=True
    )
    script = rf"""
$mutex = [System.Threading.Mutex]::new(
    $false, {_quote_pwsh_string(mutex_name)}
)
$mutex.WaitOne() | Out-Null
try {{
    $job = Start-Job -ScriptBlock {{
        param($temp, $snapshot)
        Set-Content -Encoding UTF8 -LiteralPath $temp -Value 'complete'
{publish}
    }} -ArgumentList '{temp}', '{snapshot}'
    $job | Wait-Job | Out-Null
    $state = $job.State
    $job | Receive-Job -ErrorAction SilentlyContinue | Out-Null
    $job | Remove-Job -Force
    Write-Output "STATE=$state TEMP=$(Test-Path -LiteralPath '{temp}')"
}} finally {{
    $mutex.ReleaseMutex()
    $mutex.Dispose()
    Remove-Item -Force -ErrorAction SilentlyContinue -LiteralPath '{temp}'
}}
"""

    result = subprocess.run(
        ["pwsh", "-NoProfile", "-Command", script],
        capture_output=True,
        text=True,
        timeout=15,
    )

    assert "STATE=Failed TEMP=False" in result.stdout
    assert not (tmp_path / "snapshot.tmp").exists()


@pytest.mark.skipif(shutil.which("pwsh") is None, reason="pwsh required")
def test_snapshot_assembly_failure_preserves_user_exit_code(env, tmp_path):
    env._snapshot_path = str(tmp_path / "missing" / "snapshot.ps1")
    env._snapshot_ready = True
    wrapped = env._wrap_command("$global:LASTEXITCODE = 7", str(tmp_path))

    result = subprocess.run(
        ["pwsh", "-NoProfile", "-Command", wrapped],
        capture_output=True,
        text=True,
        timeout=15,
    )

    assert result.returncode == 7, result.stderr
    assert not list(tmp_path.glob("**/*.tmp.*"))


class TestRunBash:

    def test_uses_encoded_command(self, env, monkeypatch):
        captured = []
        monkeypatch.setattr(ssh_pwsh_env, "_popen_bash",
                            lambda cmd, stdin_data=None: (
                                captured.append(cmd) or MagicMock()
                            ))
        env._run_bash("echo hello")
        assert len(captured) == 1
        cmd = captured[0]
        assert "pwsh" in cmd
        assert "-NoProfile" in cmd
        assert "-EncodedCommand" in cmd
        encoded_idx = cmd.index("-EncodedCommand") + 1
        encoded = cmd[encoded_idx]
        decoded = base64.b64decode(encoded).decode("utf-16-le")
        assert "echo hello" in decoded
