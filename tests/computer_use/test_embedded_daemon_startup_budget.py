"""The embedded daemon's startup budget must belong to the daemon.

`_EmbeddedCuaDaemon.start()` resolves the driver's CLI surface (`cua-driver
manifest`) before it spawns `serve`, and both were measured against one
15-second deadline. On a machine where discovery is slow the budget is spent
before the daemon is launched, and the failure surfaces as "daemon did not
become ready" with an empty stderr tail — blaming the daemon for time it never
got.
"""

import subprocess
import time
from typing import Any

import pytest

from tools.computer_use import cua_backend


class _FakeProcess:
    """A spawned daemon that stays alive and never writes to stderr."""

    def __init__(self) -> None:
        self.stderr = None
        self.terminated = False

    def poll(self) -> None:
        return None

    def terminate(self) -> None:
        self.terminated = True

    def kill(self) -> None:
        self.terminated = True

    def wait(self, timeout: float | None = None) -> int:
        return 0


@pytest.fixture
def slow_discovery(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    """Make CLI discovery burn most of the startup budget."""
    state: dict[str, Any] = {"discovery_calls": 0, "probe_calls": 0, "clock": 0.0}

    def fake_resolve(driver_cmd: str, **_kw: Any) -> tuple[str, list[str]]:
        state["discovery_calls"] += 1
        state["clock"] += 14.0  # slow, but under discovery's own 6s-per-call cap
        return driver_cmd, ["mcp", "--no-overlay"]

    def fake_clock() -> float:
        return state["clock"]

    def fake_popen(*_args: Any, **_kw: Any) -> _FakeProcess:
        return _FakeProcess()

    def fake_run(args: Any, **_kw: Any) -> subprocess.CompletedProcess[str]:
        # The readiness probe. The real daemon needs a moment to bind its
        # socket, so the first probe misses and the second succeeds — enough
        # that a budget already spent on discovery leaves no room to retry.
        if isinstance(args, (list, tuple)) and "status" in args:
            state["probe_calls"] += 1
            state["clock"] += 0.5
            code = 1 if state["probe_calls"] < 2 else 0
            return subprocess.CompletedProcess(args, code, "", "not running")
        return subprocess.CompletedProcess(args, 0, "", "")

    monkeypatch.setattr(cua_backend, "_resolve_mcp_invocation_uncached", fake_resolve)
    monkeypatch.setattr(cua_backend, "_MCP_INVOCATION_CACHE", {})
    monkeypatch.setattr(cua_backend.time, "monotonic", fake_clock)
    monkeypatch.setattr(cua_backend.subprocess, "Popen", fake_popen)
    monkeypatch.setattr(cua_backend.subprocess, "run", fake_run)
    monkeypatch.setattr(cua_backend, "_embedded_daemon_spawn_command",
                        lambda cmd, args, **_kw: [cmd, *args])
    monkeypatch.setattr(cua_backend, "resolve_cua_driver_cmd", lambda: "/usr/local/bin/cua-driver")
    return state


def test_slow_cli_discovery_does_not_consume_the_daemon_budget(
    slow_discovery: dict[str, Any],
) -> None:
    """A ready daemon must not be reported as timed out because discovery was slow."""
    daemon = cua_backend._EmbeddedCuaDaemon("/usr/local/bin/cua-driver", "unrestricted")

    daemon.start()

    assert daemon._running is True
    assert slow_discovery["probe_calls"] >= 1, (
        "startup never probed the daemon — the budget was spent before it launched"
    )


def test_a_probe_timeout_is_not_read_as_a_dead_daemon(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A probe that times out must not end the wait — it reports nothing.

    `cua-driver status` opens the daemon socket and waits for a reply, so a
    daemon that is still binding answers late rather than never. Treating that
    timeout as "not ready" is what made a healthy 2.4s startup fail at 15s with
    an empty diagnostic.
    """
    state = {"probes": 0}

    def fake_run(args: Any, **kw: Any) -> subprocess.CompletedProcess[str]:
        if isinstance(args, (list, tuple)) and "status" in args:
            state["probes"] += 1
            # The daemon is slow to bind: the first probes exceed the timeout,
            # then it answers.
            if state["probes"] < 3:
                raise subprocess.TimeoutExpired(cmd=list(args), timeout=kw.get("timeout", 2.0))
            return subprocess.CompletedProcess(args, 0, "running", "")
        return subprocess.CompletedProcess(args, 0, "", "")

    monkeypatch.setattr(cua_backend.subprocess, "run", fake_run)
    monkeypatch.setattr(cua_backend.subprocess, "Popen", lambda *a, **k: _FakeProcess())
    monkeypatch.setattr(cua_backend, "_embedded_daemon_spawn_command",
                        lambda cmd, args, **_kw: [cmd, *args])
    monkeypatch.setattr(cua_backend, "_resolve_mcp_invocation_uncached",
                        lambda cmd, **_kw: (cmd, ["mcp"]))
    monkeypatch.setattr(cua_backend, "_MCP_INVOCATION_CACHE", {})

    daemon = cua_backend._EmbeddedCuaDaemon("/usr/local/bin/cua-driver", "unrestricted")
    daemon.start()

    assert daemon._running is True
    assert state["probes"] >= 3, "gave up before the daemon had a chance to answer"


def test_probe_timeout_budget_exceeds_a_cold_macos_launch() -> None:
    """The probe must outlast the launch it is waiting on.

    A cold `open -n -g -a` needs roughly 2.5s on macOS; a 2s probe timeout can
    therefore never observe a healthy start.
    """
    assert cua_backend._EmbeddedCuaDaemon._PROBE_TIMEOUT_SECONDS > 2.5


def test_timed_out_probes_are_named_in_the_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The error must distinguish a silent daemon from an unanswered probe."""

    def always_timeout(args: Any, **kw: Any) -> subprocess.CompletedProcess[str]:
        if isinstance(args, (list, tuple)) and "status" in args:
            raise subprocess.TimeoutExpired(cmd=list(args), timeout=kw.get("timeout", 6.0))
        return subprocess.CompletedProcess(args, 0, "", "")

    monkeypatch.setattr(cua_backend.subprocess, "run", always_timeout)
    monkeypatch.setattr(cua_backend.subprocess, "Popen", lambda *a, **k: _FakeProcess())
    monkeypatch.setattr(cua_backend, "_embedded_daemon_spawn_command",
                        lambda cmd, args, **_kw: [cmd, *args])
    monkeypatch.setattr(cua_backend, "_resolve_mcp_invocation_uncached",
                        lambda cmd, **_kw: (cmd, ["mcp"]))
    monkeypatch.setattr(cua_backend, "_MCP_INVOCATION_CACHE", {})
    monkeypatch.setattr(cua_backend._EmbeddedCuaDaemon, "_START_TIMEOUT_SECONDS", 0.5)

    daemon = cua_backend._EmbeddedCuaDaemon("/usr/local/bin/cua-driver", "unrestricted")

    with pytest.raises(RuntimeError, match="readiness probe timed out"):
        daemon.start()

