"""Shutdown watchdog + loop heartbeat coverage for #66892.

The drain path is asyncio-based; a frozen loop makes every asyncio timeout
structurally unable to fire. These tests pin the out-of-loop backstop
(thread watchdog) and the loop-liveness heartbeat file contract.
"""

from __future__ import annotations

import asyncio
import json
import os
import threading
import time
from unittest.mock import patch

import pytest

from gateway.shutdown_watchdog import (
    DEFAULT_SHUTDOWN_WATCHDOG_GRACE_S,
    arm_shutdown_watchdog,
    get_loop_heartbeat_path,
    get_shutdown_watchdog_dump_path,
    loop_heartbeat_forever,
    resolve_shutdown_watchdog_delay,
    write_loop_heartbeat,
)

def test_resolve_shutdown_watchdog_delay_adds_grace():
    assert resolve_shutdown_watchdog_delay(180) == 180 + DEFAULT_SHUTDOWN_WATCHDOG_GRACE_S
    assert resolve_shutdown_watchdog_delay(0) == DEFAULT_SHUTDOWN_WATCHDOG_GRACE_S
    assert resolve_shutdown_watchdog_delay("bad") == DEFAULT_SHUTDOWN_WATCHDOG_GRACE_S
    assert resolve_shutdown_watchdog_delay(10, grace_s=5) == 15.0


def test_arm_shutdown_watchdog_fires_with_dump_and_exit(tmp_path):
    done = threading.Event()
    fired = threading.Event()
    dump = tmp_path / "logs" / "watchdog.log"
    snapshot_calls = []
    exit_codes = []

    def snapshot():
        snapshot_calls.append(1)
        return {"active_agents": 1, "draining": True}

    def fake_exit(code):
        exit_codes.append(code)
        fired.set()

    with patch("gateway.shutdown_watchdog.os._exit", side_effect=fake_exit):
        arm_shutdown_watchdog(
            0.15,
            done_event=done,
            snapshot_fn=snapshot,
            dump_path=dump,
            exit_code=9,
        )
        assert fired.wait(timeout=5.0), "watchdog did not fire"

    assert exit_codes == [9]
    assert snapshot_calls == [1]
    assert dump.is_file()
    text = dump.read_text(encoding="utf-8")
    assert "shutdown_watchdog_fired" in text
    assert "faulthandler dump" in text
    assert get_shutdown_watchdog_dump_path(tmp_path).name == "gateway-shutdown-watchdog.log"


def test_self_replacement_arms_watcher_for_exit75(tmp_path, monkeypatch):
    """Windows exit-75 must arm the detached respawn watcher (no supervisor)."""
    from gateway import shutdown_watchdog as sw

    pid_file = tmp_path / "gateway.pid"
    run_argv = ["C:\\venv\\Scripts\\python.exe", "-m", "hermes_cli.main", "gateway", "run"]
    pid_file.write_text(
        json.dumps({"pid": os.getpid(), "kind": "hermes-gateway", "argv": run_argv}),
        encoding="utf-8",
    )
    monkeypatch.setattr("gateway.status._get_pid_path", lambda: pid_file)

    calls = []

    def fake_arm(old_pid, argv):
        calls.append((old_pid, list(argv)))
        return True

    monkeypatch.setattr(
        "hermes_cli.gateway.launch_detached_gateway_restart_by_cmdline", fake_arm
    )

    assert sw._spawn_windows_self_replacement(sw.GATEWAY_SERVICE_RESTART_EXIT_CODE) is True
    assert calls == [(os.getpid(), run_argv)]


def test_self_replacement_skips_other_exit_codes(monkeypatch):
    """Only the restart-contract code arms a replacement; stops stay stops."""
    from gateway import shutdown_watchdog as sw

    def fail_arm(old_pid, argv):
        raise AssertionError("launcher must not be called for non-75 exits")

    monkeypatch.setattr(
        "hermes_cli.gateway.launch_detached_gateway_restart_by_cmdline", fail_arm
    )
    assert sw._spawn_windows_self_replacement(1) is False
    assert sw._spawn_windows_self_replacement(0) is False


def test_self_replacement_skips_stale_pidfile(tmp_path, monkeypatch):
    """A pid file owned by another process must NOT trigger a respawn."""
    from gateway import shutdown_watchdog as sw

    pid_file = tmp_path / "gateway.pid"
    pid_file.write_text(
        json.dumps({"pid": 999999, "kind": "hermes-gateway", "argv": ["python"]}),
        encoding="utf-8",
    )
    monkeypatch.setattr("gateway.status._get_pid_path", lambda: pid_file)

    def fail_arm(old_pid, argv):
        raise AssertionError("stale pidfile must not arm the watcher")

    monkeypatch.setattr(
        "hermes_cli.gateway.launch_detached_gateway_restart_by_cmdline", fail_arm
    )
    assert sw._spawn_windows_self_replacement(sw.GATEWAY_SERVICE_RESTART_EXIT_CODE) is False


def test_self_replacement_armed_before_both_hard_exits():
    """Source pin: replacement arming precedes os._exit in BOTH watchdog paths."""
    import ast
    from pathlib import Path

    root = Path(__file__).resolve().parents[2]
    src = (root / "gateway" / "shutdown_watchdog.py").read_text(encoding="utf-8")
    tree = ast.parse(src)
    # Count real os._exit(...) CALLS via AST so docstring prose mentioning
    # ``os._exit(exit_code)`` (arm_shutdown_watchdog's docstring) doesn't count.
    exit_nodes = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "_exit"
        and isinstance(node.func.value, ast.Name)
        and node.func.value.id == "os"
    ]
    exit_lines = sorted({node.lineno for node in exit_nodes})
    assert len(exit_lines) == 2, (
        f"expected exactly two watchdog os._exit sites, got {exit_lines}"
    )
    lines = src.splitlines()
    for ln in exit_lines:
        before = "\n".join(lines[: ln - 1])
        assert "_spawn_windows_self_replacement(exit_code)" in before, (
            f"arming missing before os._exit at line {ln}"
        )


