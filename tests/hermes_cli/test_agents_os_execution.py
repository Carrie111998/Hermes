from __future__ import annotations

import io
from pathlib import Path

import pytest

from hermes_cli.agents_os_execution import (
    CancelToken,
    ClaudeAdapter,
    CodexAdapter,
    HermesAdapter,
    OpenClawAdapter,
    RuntimeAdapterRegistry,
    RuntimeExecutionError,
    RuntimeUnavailableError,
    UnsafeWorkingDirectoryError,
    default_registry,
    execute_invocation,
)


def test_hermes_uses_verified_query_flag_and_redacts_prompt(tmp_path: Path) -> None:
    invocation = HermesAdapter("hermes", [tmp_path]).build_invocation(
        prompt="sensitive prompt", cwd=tmp_path
    )
    assert invocation.argv[:4] == ("hermes", "chat", "-q", "sensitive prompt")
    assert "--oneshot" not in invocation.argv and "--yolo" not in invocation.argv
    assert invocation.evidence_argv[3] == "<redacted>"
    assert invocation.argv[invocation.argv.index("--toolsets") + 1] == "clarify"
    assert "--checkpoints" not in invocation.argv and "-Q" in invocation.argv


def test_hermes_rejects_non_conversational_toolsets(tmp_path: Path) -> None:
    adapter = HermesAdapter("hermes", [tmp_path])
    with pytest.raises(RuntimeExecutionError, match="safe conversational allowlist"):
        adapter.build_invocation(prompt="x", cwd=tmp_path, toolsets=("terminal",))


def test_cwd_must_be_absolute_and_exactly_allowlisted(tmp_path: Path) -> None:
    child = tmp_path / "child"
    child.mkdir()
    adapter = HermesAdapter("hermes", [tmp_path])
    with pytest.raises(UnsafeWorkingDirectoryError, match="absolute"):
        adapter.build_invocation(prompt="x", cwd=Path("relative"))
    with pytest.raises(UnsafeWorkingDirectoryError, match="exactly allowlisted"):
        adapter.build_invocation(prompt="x", cwd=child)


def test_codex_is_ephemeral_json_and_receives_prompt_on_stdin(tmp_path: Path) -> None:
    output = tmp_path / "last-message.txt"
    invocation = CodexAdapter("codex", [tmp_path]).build_invocation(
        prompt="do work", cwd=tmp_path, output_last_message=output
    )
    assert invocation.argv[:4] == ("codex", "exec", "--json", "--ephemeral")
    assert invocation.argv[-1] == "-"
    assert invocation.stdin == b"do work"
    assert "do work" not in invocation.evidence_argv
    assert invocation.argv[invocation.argv.index("--output-last-message") + 1] == str(output)


def test_codex_output_file_cannot_escape_cwd(tmp_path: Path) -> None:
    other = tmp_path.parent / "outside.txt"
    with pytest.raises(RuntimeExecutionError, match="directly inside cwd"):
        CodexAdapter("codex", [tmp_path]).build_invocation(
            prompt="x", cwd=tmp_path, output_last_message=other
        )


def test_claude_defaults_to_no_tools_bounded_budget_and_safe_permissions(tmp_path: Path) -> None:
    invocation = ClaudeAdapter("claude", [tmp_path]).build_invocation(prompt="x", cwd=tmp_path)
    assert invocation.argv[1] == "-p"
    assert "stream-json" in invocation.argv
    assert invocation.argv[invocation.argv.index("--tools") + 1] == ""
    assert invocation.argv[invocation.argv.index("--permission-mode") + 1] == "dontAsk"
    assert "--no-session-persistence" in invocation.argv
    assert invocation.stdin == b"x"
    with pytest.raises(RuntimeExecutionError, match="at most 100"):
        ClaudeAdapter("claude", [tmp_path]).build_invocation(
            prompt="x", cwd=tmp_path, max_budget_usd=101
        )


def test_openclaw_uses_verified_json_contract_without_delivery(tmp_path: Path) -> None:
    adapter = OpenClawAdapter("openclaw", [tmp_path])
    invocation = adapter.build_invocation(prompt="private", cwd=tmp_path)
    assert invocation.argv == (
        "openclaw", "agent", "--agent", "main", "--message", "private",
        "--timeout", "600", "--json",
    )
    assert "--deliver" not in invocation.argv and "--local" not in invocation.argv
    assert "private" not in invocation.evidence_argv
    assert invocation.evidence_argv[5] == "<redacted>"


def test_openclaw_validates_agent_and_timeout(tmp_path: Path) -> None:
    adapter = OpenClawAdapter("openclaw", [tmp_path])
    with pytest.raises(RuntimeExecutionError, match="agent_id"):
        adapter.build_invocation(prompt="x", cwd=tmp_path, agent_id="bad agent")
    with pytest.raises(RuntimeExecutionError, match="timeout_seconds"):
        adapter.build_invocation(prompt="x", cwd=tmp_path, timeout_seconds=901)


def test_registry_accepts_stable_runtime_executable_overrides(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("AGENTS_OS_HERMES_EXECUTABLE", "/opt/agents-os/hermes")
    monkeypatch.setenv("AGENTS_OS_OPENCLAW_EXECUTABLE", "/opt/agents-os/openclaw")
    registry = default_registry(allowed_cwds=[tmp_path])
    assert registry.get("hermes").executable == "/opt/agents-os/hermes"
    assert registry.get("openclaw").executable == "/opt/agents-os/openclaw"


def test_registry_supports_injected_fake_adapter() -> None:
    class FakeAdapter:
        name = "fake"

        def probe(self):
            return type("Probe", (), {"available": True})()

        def build_invocation(self, *, prompt, cwd, **options):  # pragma: no cover
            raise AssertionError("not used")

    registry = RuntimeAdapterRegistry([FakeAdapter()])
    assert registry.get("fake").name == "fake"
    assert registry.probe_all()["fake"].available
    with pytest.raises(RuntimeUnavailableError, match="unknown runtime"):
        registry.get("missing")


class _RecordingStdin(io.BytesIO):
    def __init__(self) -> None:
        super().__init__()
        self.written = b""

    def close(self) -> None:
        self.written = self.getvalue()
        super().close()


class _FakeProcess:
    def __init__(self, *, stdout: bytes = b"", stderr: bytes = b"", running: bool = False) -> None:
        self.pid = 1234
        self.stdin = _RecordingStdin()
        self.stdout = io.BytesIO(stdout)
        self.stderr = io.BytesIO(stderr)
        self.returncode = None if running else 0

    def poll(self):
        return self.returncode

    def wait(self, timeout=None):
        if self.returncode is None:
            raise __import__("subprocess").TimeoutExpired("fake", timeout)
        return self.returncode

    def kill(self):
        self.returncode = -9


def test_execution_is_shell_false_bounded_and_passes_stdin(tmp_path: Path) -> None:
    invocation = CodexAdapter("codex", [tmp_path]).build_invocation(
        prompt="input", cwd=tmp_path, output_last_message=tmp_path / "last.txt"
    )
    captured = {}
    process = _FakeProcess(stdout=b"123456", stderr=b"abcdef")

    def factory(argv, **kwargs):
        captured["argv"] = argv
        captured["kwargs"] = kwargs
        return process

    result = execute_invocation(
        invocation, timeout_seconds=1, max_stdout_bytes=4, max_stderr_bytes=3,
        process_factory=factory,
    )
    assert captured["kwargs"]["shell"] is False
    assert captured["kwargs"]["cwd"] == str(tmp_path.resolve())
    assert process.stdin.written == b"input"
    assert result.stdout == b"1234" and result.stdout_truncated
    assert result.stderr == b"abc" and result.stderr_truncated
    assert result.succeeded


def test_cancel_terminates_then_force_kills_process_tree(tmp_path: Path) -> None:
    invocation = HermesAdapter("hermes", [tmp_path]).build_invocation(prompt="x", cwd=tmp_path)
    process = _FakeProcess(running=True)
    calls = []
    token = CancelToken()
    token.cancel()

    def graceful(proc):
        calls.append("terminate")

    def forceful(proc):
        calls.append("kill")
        proc.returncode = -9

    result = execute_invocation(
        invocation, timeout_seconds=1, cancel_token=token,
        process_factory=lambda argv, **kwargs: process,
        terminate_tree=graceful, kill_tree=forceful,
    )
    assert calls == ["terminate", "kill"]
    assert result.cancelled and not result.timed_out and not result.succeeded
