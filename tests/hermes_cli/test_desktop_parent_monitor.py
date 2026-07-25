import asyncio

import pytest

from hermes_cli.desktop_parent_monitor import (
    DesktopParentContract,
    monitor_desktop_parent,
    parse_desktop_parent_contract,
)


def test_parent_contract_is_desktop_only_and_backwards_compatible():
    assert parse_desktop_parent_contract({}, current_pid=99) is None
    assert parse_desktop_parent_contract({"HERMES_DESKTOP": "1"}, current_pid=99) is None


def test_parent_contract_validates_pid_and_nonce():
    env = {
        "HERMES_DESKTOP": "1",
        "HERMES_DESKTOP_PARENT_PID": "42",
        "HERMES_DESKTOP_PARENT_NONCE": "a" * 32,
    }
    assert parse_desktop_parent_contract(env, current_pid=99) == DesktopParentContract(
        pid=42, nonce="a" * 32
    )
    with pytest.raises(ValueError, match="incomplete"):
        parse_desktop_parent_contract(
            {"HERMES_DESKTOP": "1", "HERMES_DESKTOP_PARENT_PID": "42"},
            current_pid=99,
        )
    with pytest.raises(ValueError, match="PID"):
        parse_desktop_parent_contract(
            {**env, "HERMES_DESKTOP_PARENT_PID": "99"}, current_pid=99
        )
    with pytest.raises(ValueError, match="nonce"):
        parse_desktop_parent_contract(
            {**env, "HERMES_DESKTOP_PARENT_NONCE": "short"}, current_pid=99
        )


def test_monitor_requests_shutdown_only_after_consecutive_misses():
    class Server:
        should_exit = False

    outcomes = iter([False, True, False, False])
    server = Server()
    asyncio.run(
        monitor_desktop_parent(
            server,
            DesktopParentContract(pid=42, nonce="a" * 32),
            check_alive=lambda _pid: next(outcomes),
            interval_seconds=0,
            missed_checks=2,
        )
    )
    assert server.should_exit is True
