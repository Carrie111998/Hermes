from __future__ import annotations

import subprocess

import pytest

from hermes_cli.mux_broker import MuxBroker, MuxError, parse_pane_pid_rows, parse_session_rows


def completed(stdout: str = "", returncode: int = 0, stderr: str = "") -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess([], returncode, stdout, stderr)


def test_parse_session_rows_groups_live_rmux_state() -> None:
    rows = parse_session_rows(
        "arch\t2\t1\t1787000000\t/opt/AGK-OS\t120x40\n"
        "runtime\t1\t0\t1787000100\t/home/ops/hermes\t90x30\n"
    )
    assert rows == [
        {
            "name": "arch",
            "windows": 2,
            "attached": True,
            "activity": 1787000000,
            "cwd": "/opt/AGK-OS",
            "size": "120x40",
            "status": "attached",
        },
        {
            "name": "runtime",
            "windows": 1,
            "attached": False,
            "activity": 1787000100,
            "cwd": "/home/ops/hermes",
            "size": "90x30",
            "status": "detached",
        },
    ]


def test_parse_pane_pid_rows_uses_first_valid_pane_per_session() -> None:
    assert parse_pane_pid_rows("arch\t4321\narch\t9999\nruntime\t4322\nbad/name\t3\n") == {
        "arch": 4321,
        "runtime": 4322,
    }


def test_list_sessions_uses_argv_only_and_rmux_first() -> None:
    calls: list[list[str]] = []

    def run(argv: list[str], **_kwargs):
        calls.append(argv)
        if argv[1] == "list-sessions":
            return completed("arch\t1\t0\t1\t/opt/agk\t80x24\n")
        if argv[1] == "list-panes":
            return completed("arch\t4321\n")
        raise AssertionError(argv)

    broker = MuxBroker(which=lambda name: f"/bin/{name}" if name == "rmux" else None, run=run)
    result = broker.list_sessions()
    assert result["engine"] == "rmux"
    assert result["sessions"][0]["name"] == "arch"
    assert result["sessions"][0]["pane_pid"] == 4321
    assert calls[0][0] == "/bin/rmux"
    assert calls[1][1] == "list-panes"
    assert broker.shell is False


def test_capture_rejects_session_name_injection_before_spawn() -> None:
    calls = []
    broker = MuxBroker(which=lambda _name: "/bin/rmux", run=lambda *args, **kwargs: calls.append((args, kwargs)))
    with pytest.raises(MuxError, match="invalid session name"):
        broker.capture("good;rm -rf /", lines=100)
    assert calls == []


def test_capture_returns_ansi_without_interpreting_it() -> None:
    calls = []

    def run(argv: list[str], **_kwargs):
        calls.append(argv)
        return completed("\x1b[32mgreen\x1b[0m\n")

    broker = MuxBroker(which=lambda _name: "/bin/rmux", run=run)
    assert broker.capture("arch", lines=250)["ansi"] == "\x1b[32mgreen\x1b[0m\n"
    assert calls[0][-4:] == ["-S", "-250", "-t", "arch"]


def test_send_input_uses_literal_text_and_allowlisted_keys() -> None:
    calls = []

    def run(argv: list[str], **_kwargs):
        calls.append(argv)
        return completed()

    broker = MuxBroker(which=lambda _name: "/bin/rmux", run=run)
    broker.send_input("arch", text="hello $(touch /tmp/nope)")
    broker.send_input("arch", key="Enter")
    assert calls[0] == ["/bin/rmux", "send-keys", "-l", "-t", "arch", "--", "hello $(touch /tmp/nope)"]
    assert calls[1] == ["/bin/rmux", "send-keys", "-t", "arch", "Enter"]
    with pytest.raises(MuxError, match="unsupported key"):
        broker.send_input("arch", key="run-shell")


def test_resize_is_bounded_and_fixed_argv() -> None:
    calls = []

    def run(argv: list[str], **_kwargs):
        calls.append(argv)
        return completed()

    broker = MuxBroker(which=lambda _name: "/bin/rmux", run=run)
    broker.resize("arch", cols=120, rows=40)
    assert calls[0] == ["/bin/rmux", "resize-pane", "-t", "arch", "-x", "120", "-y", "40"]
    with pytest.raises(MuxError, match="terminal size"):
        broker.resize("arch", cols=5, rows=40)


def test_literal_input_uses_end_of_options_for_leading_dash() -> None:
    calls = []

    def run(argv: list[str], **_kwargs):
        calls.append(argv)
        return completed()

    broker = MuxBroker(which=lambda _name: "/bin/rmux", run=run)
    broker.send_input("arch", text="-N")
    assert calls[0] == ["/bin/rmux", "send-keys", "-l", "-t", "arch", "--", "-N"]


def test_literal_input_is_bounded() -> None:
    broker = MuxBroker(which=lambda _name: "/bin/rmux", run=lambda *_args, **_kwargs: completed())
    with pytest.raises(MuxError, match="input too large"):
        broker.send_input("arch", text="x" * 8193)


def test_create_session_quotes_backend_owned_command_and_validates_cwd(tmp_path) -> None:
    calls = []

    def run(argv: list[str], **_kwargs):
        calls.append(argv)
        return completed()

    broker = MuxBroker(which=lambda _name: "/bin/rmux", run=run)
    broker.create(
        "claude-auth-demo",
        cwd=str(tmp_path),
        command=["env", f"CLAUDE_CONFIG_DIR={tmp_path / 'slot dir'}", "claude", "auth", "login", "--claudeai"],
    )
    assert calls[0][:7] == ["/bin/rmux", "new-session", "-d", "-s", "claude-auth-demo", "-c", str(tmp_path)]
    assert "'CLAUDE_CONFIG_DIR=" in calls[0][7]
    with pytest.raises(MuxError, match="working directory"):
        broker.create("bad", cwd=str(tmp_path / "missing"))


def test_missing_mux_is_a_typed_unavailable_result() -> None:
    broker = MuxBroker(which=lambda _name: None)
    assert broker.list_sessions() == {"available": False, "engine": None, "sessions": [], "error": "mux_unavailable"}
