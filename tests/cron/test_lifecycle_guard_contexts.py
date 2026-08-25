"""Gateway lifecycle guard: command shapes and execution context (2026-07-28).

Scope reminder, mirrored from ``cron/lifecycle_guard.py``: this guard is an
accident-and-carelessness barrier, NOT a security boundary. Every profile's
agent runs as the same UNIX user and can reach the same APIs from Python. These
tests lock in the two bypasses seen in production and the fail-open contract of
the PID check.
"""

from pathlib import Path

import pytest

from cron import lifecycle_guard as lg

REPO = Path(__file__).resolve().parents[2]


BLOCKED = [
    # canonical shape
    "hermes gateway restart",
    "hermes gateway stop",
    # bypass #1, seen 2026-07-27 22:19:48 and 2026-07-28 00:33:16: global flags
    # between the binary and the subcommand
    "hermes --profile default gateway restart",
    "hermes -p altex gateway stop",
    "hermes --profile altex gateway restart && echo done",
    # incident 2026-07-27 started here: a second poller on the same bot token
    "hermes --profile altex gateway install",
    "hermes gateway uninstall",
    # systemd shapes
    "systemctl --user restart hermes-gateway.service",
    "systemctl --user stop hermes-gateway",
    # registering a unit directly is the same vector as `gateway install`
    "systemctl --user enable hermes-gateway-altex.service",
    "systemctl --user disable hermes-gateway",
    (
        "systemd-run --user --unit=hermes-gateway-restart-once --on-active=2s "
        "--collect /usr/bin/systemctl --user restart hermes-gateway.service"
    ),
    # bypass #2, seen 2026-07-28 01:25:45: kill AFTER the gateway identifier
    'pid=$(systemctl --user show hermes-gateway.service -p MainPID --value); kill -USR1 "$pid"',
    # and the historical order, which the old pattern already caught
    "pkill -f hermes-gateway",
    "/usr/bin/pkill -f hermes-gateway",
    "command /usr/bin/pkill -f hermes-gateway",
    "env -i PATH=/usr/bin /usr/bin/pkill -f hermes-gateway",
    "kill -9 $(pgrep -f hermes-gateway)",
    # newline separator, not just ';'
    'pid=$(systemctl --user show hermes-gateway.service -p MainPID --value)\nkill -TERM "$pid"',
]

ALLOWED = [
    # read-only inspection must stay usable
    "hermes gateway status",
    "hermes --profile altex gateway status",
    "systemctl --user show hermes-gateway.service -p MainPID --value",
    "systemctl --user is-active hermes-gateway.service",
    # read-only verb that merely contains "enable" as a substring
    "systemctl --user is-enabled hermes-gateway.service",
    "systemctl --user list-unit-files | grep hermes-gateway",
    "journalctl --user -u hermes-gateway.service -n 50 --no-pager",
    # the word "kill" as an argument, not a command — this is why the kill
    # branch is anchored to a command position
    "journalctl --user -u hermes-gateway.service | grep -i kill",
    "grep -rn kill /home/hermesagent/.hermes/hermes-agent/gateway/run.py",
    # start is deliberately not a lifecycle verb here
    "hermes gateway start",
    # the update conveyor: its own systemctl calls live inside the script, not
    # in the command text the agent submits
    "hermes-safe-update",
    "/home/hermesagent/.local/bin/hermes-safe-update --check-only",
    # unrelated subcommands and prose
    "hermes --profile altex sessions list",
    "echo 'we should restart the gateway tomorrow'",
    "hermes cron list",
]


@pytest.mark.parametrize("command", BLOCKED)
def test_lifecycle_shapes_are_blocked(command):
    assert lg.contains_gateway_lifecycle_command(command) is True


@pytest.mark.parametrize("command", ALLOWED)
def test_benign_shapes_are_allowed(command):
    assert lg.contains_gateway_lifecycle_command(command) is False


@pytest.mark.parametrize(
    "command",
    [
        "kill -USR1 133375",
        "/bin/kill -TERM 133375",
        "command /bin/kill -TERM 133375",
        "env -i PATH=/usr/bin /usr/bin/kill -TERM 133375",
        "sudo /bin/kill -TERM 133375",
    ],
)
def test_kill_by_pid_is_blocked_when_pid_resolves(monkeypatch, command):
    monkeypatch.setattr(lg, "_resolve_gateway_main_pid", lambda: 133375)
    assert lg.kill_targets_gateway_main_pid(command) is True


def test_kill_by_pid_fails_open_when_pid_unknown(monkeypatch):
    """Unresolvable PID must ALLOW: losing a legitimate kill is worse."""
    monkeypatch.setattr(lg, "_resolve_gateway_main_pid", lambda: None)
    assert lg.kill_targets_gateway_main_pid("kill -USR1 133375") is False


def test_kill_by_pid_does_not_overmatch(monkeypatch):
    monkeypatch.setattr(lg, "_resolve_gateway_main_pid", lambda: 133375)
    # substring of a longer number must not count
    assert lg.kill_targets_gateway_main_pid("kill -9 1333750") is False
    # no kill at a command position -> no PID lookup, no block
    assert lg.kill_targets_gateway_main_pid("echo 133375") is False
    assert lg.kill_targets_gateway_main_pid("grep 133375 /tmp/pids") is False
    assert lg.kill_targets_gateway_main_pid("echo /bin/kill -TERM 133375") is False
    # A different command mentioning the PID must not be attributed to kill.
    assert lg.kill_targets_gateway_main_pid("kill -TERM 99; echo 133375") is False


def test_pid_lookup_is_skipped_without_a_kill(monkeypatch):
    """The subprocess lookup must not run for ordinary commands."""

    def _boom():
        raise AssertionError("PID resolution ran for a command without kill")

    monkeypatch.setattr(lg, "_resolve_gateway_main_pid", _boom)
    assert lg.kill_targets_gateway_main_pid("ls -la /tmp") is False


def test_terminal_guard_is_no_longer_env_gated():
    """Regression lock: the guard must not depend on _HERMES_GATEWAY.

    That marker is set only by gateway/run.py, so gating on it left serve,
    cli and standalone cron uncovered.
    """
    src = (REPO / "tools/terminal_tool.py").read_text(encoding="utf-8")
    assert 'os.environ.get("_HERMES_GATEWAY") == "1"' not in src
    assert "contains_gateway_lifecycle_command(command)" in src
    assert "kill_targets_gateway_main_pid(command)" in src
