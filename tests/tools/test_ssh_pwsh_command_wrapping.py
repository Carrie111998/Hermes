"""Tests for PowerShell command wrapping in ssh_pwsh backend."""

import base64
import shlex
import shutil
import subprocess
from unittest.mock import MagicMock

import pytest

from tools.environments import ssh as ssh_env
from tools.environments import ssh_pwsh as ssh_pwsh_env
from tools.environments.ssh_pwsh import SSHPwshEnvironment


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

    def test_snapshot_update_uses_unique_temp_and_atomic_move(self, env):
        env._snapshot_ready = True

        wrapped = env._wrap_command("dir", "C:\\")

        temp_path = shlex.quote(env._snapshot_path + ".tmp.")
        live_path = shlex.quote(env._snapshot_path)
        assert f"{temp_path}$PID" in wrapped
        assert f"Move-Item -Force {temp_path}$PID {live_path}" in wrapped
        assert wrapped.index("Set-Content") < wrapped.index("Move-Item -Force")
        set_content_lines = [
            line for line in wrapped.splitlines() if "Set-Content" in line
        ]
        assert len(set_content_lines) == 1
        assert f"Set-Content -Encoding UTF8 {temp_path}$PID" in set_content_lines[0]

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
        temp_path = shlex.quote(env._snapshot_path + ".tmp.")
        live_path = shlex.quote(env._snapshot_path)
        assert f"{temp_path}$PID" in bootstrap
        assert f"Move-Item -Force {temp_path}$PID {live_path}" in bootstrap
        assert bootstrap.index("Set-Content") < bootstrap.index("Move-Item -Force")


@pytest.mark.skipif(shutil.which("pwsh") is None, reason="pwsh required")
def test_concurrent_writers_never_publish_partial_snapshot(tmp_path):
    snapshot = str(tmp_path / "hermes-snapshot.ps1").replace("'", "''")
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
            $temp = $snapshot + '.tmp.' + $PID
            @(
                "BEGIN-$writer-$_",
                ('x' * 20000),
                "END-$writer-$_"
            ) | Set-Content -Encoding UTF8 -LiteralPath $temp
            Move-Item -Force -LiteralPath $temp -Destination $snapshot
            Start-Sleep -Milliseconds 1
        }}
    }} -ArgumentList $snapshot, $writer
}}
$valid = $true
$readCount = 0
while (@($jobs | Where-Object State -eq 'Running').Count -gt 0) {{
    $lines = @(Get-Content -LiteralPath $snapshot)
    $readCount++
    $begin = $lines[0] -replace '^BEGIN-', ''
    $end = $lines[2] -replace '^END-', ''
    if ($lines.Count -ne 3 -or
        $lines[0] -notmatch '^BEGIN-' -or
        $lines[2] -notmatch '^END-' -or
        $begin -ne $end) {{
        $valid = $false
        break
    }}
}}
$jobs | Wait-Job | Out-Null
$jobs | Receive-Job | Out-Null
$jobs | Remove-Job -Force
if (-not $valid -or $readCount -eq 0) {{ exit 1 }}
"""

    result = subprocess.run(
        ["pwsh", "-NoProfile", "-Command", script],
        capture_output=True,
        text=True,
        timeout=60,
    )

    assert result.returncode == 0, result.stderr


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
