"""M2 stale platform-lock takeover + GATEWAY_STOPPED synthesis.

Spec: profiles/sentinel/workspace/gateway-restart-cluster-2026-04-30.md.

Covers the wiring layer between gateway/platforms/base.py and
gateway/status.py: BasePlatformAdapter._acquire_platform_lock passes
``pid_recheck_after_seconds=2.0`` to acquire_scoped_lock and synthesizes
a GATEWAY_STOPPED for any dead previous PID it just took over from.

The recheck behaviour itself (the Windows tasklist-zombie defence) is
covered by tests/gateway/test_status.py::TestScopedLocks; this file
verifies the platform-level callers wire it correctly and that the
synthesis fires only on the takeover-success path.
"""

import json
from unittest.mock import MagicMock

import pytest

from events.schema import EventType
from gateway import status
from gateway.platforms import base as platform_base


class _StubPlatform:
    """Minimal Platform enum stand-in — only ``.value`` is read."""
    value = "telegram"


class _StubAdapter:
    """Minimal stand-in for BasePlatformAdapter exposing the methods
    _acquire_platform_lock + _synthesize_previous_gateway_stopped touch."""

    name = "Telegram"
    platform = _StubPlatform()

    def __init__(self):
        self._platform_lock_scope = None
        self._platform_lock_identity = None
        self._fatal_error_code = None
        self._fatal_error_message = None
        self._fatal_error_retryable = True
        self._fatal_called_with = None

    def _set_fatal_error(self, code, message, *, retryable):
        self._fatal_called_with = (code, message, retryable)
        self._fatal_error_code = code
        self._fatal_error_message = message
        self._fatal_error_retryable = retryable

    # Reuse the real methods from BasePlatformAdapter unbound — they only
    # touch the attrs we set above, gateway.status, and the get_bus()
    # helper, all of which we can monkeypatch.
    _acquire_platform_lock = platform_base.BasePlatformAdapter._acquire_platform_lock
    _synthesize_previous_gateway_stopped = (
        platform_base.BasePlatformAdapter._synthesize_previous_gateway_stopped
    )


@pytest.fixture
def isolated_lock_dir(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_GATEWAY_LOCK_DIR", str(tmp_path / "locks"))
    return tmp_path / "locks"


@pytest.fixture
def captured_bus(monkeypatch):
    """Replace events.gateway_integration.get_bus with one returning a
    MagicMock so we can assert on synthesized emits."""
    import events.gateway_integration as gi
    fake_bus = MagicMock()
    fake_bus.emit = MagicMock(return_value="evt-123")
    monkeypatch.setattr(gi, "_bus", fake_bus)
    return fake_bus


def _seed_lock(lock_dir, scope, identity, pid, start_time=123):
    lock_path = status._get_scope_lock_path(scope, identity)
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    lock_path.write_text(json.dumps({
        "pid": pid,
        "start_time": start_time,
        "kind": "hermes-gateway",
    }))


class TestPlatformLockRecheckPath:
    """The 2-second recheck must propagate from ``_acquire_platform_lock``
    into ``acquire_scoped_lock`` and bridge the Windows tasklist-zombie
    race that produced the FATAL state at restart #5 2026-04-30."""

    def test_takeover_synthesizes_gateway_stopped_for_dead_pid(
        self, isolated_lock_dir, captured_bus, monkeypatch
    ):
        # Lock claimed by PID 99999 with start_time 123.
        _seed_lock(isolated_lock_dir, "telegram-bot-token", "secret", pid=99999)

        # Simulate the tasklist-zombie race: first check alive (Windows
        # not yet reaped), second check dead. The recheck loop in
        # acquire_scoped_lock takes the dead branch on the second call.
        call_count = {"n": 0}
        def flaky_pid_exists(pid):
            call_count["n"] += 1
            return call_count["n"] == 1
        monkeypatch.setattr(status, "_pid_exists", flaky_pid_exists)
        monkeypatch.setattr(status, "pid_exists", flaky_pid_exists)
        monkeypatch.setattr(status.time, "sleep", lambda _s: None)

        adapter = _StubAdapter()
        ok = adapter._acquire_platform_lock(
            "telegram-bot-token", "secret", "Telegram bot token"
        )
        assert ok is True
        assert call_count["n"] == 2  # alive then dead — recheck path

        # Synthesis fired exactly once for the dead previous PID.
        assert captured_bus.emit.call_count == 1
        kwargs = captured_bus.emit.call_args.kwargs
        assert kwargs["event_type"] == EventType.GATEWAY_STOPPED
        assert kwargs["source"] == "gateway-stale-lock-detector"
        assert kwargs["payload"]["pid"] == 99999
        assert kwargs["payload"]["exit_reason"] == "detected_dead"
        assert kwargs["payload"]["scope"] == "telegram-bot-token"
        assert kwargs["payload"]["platform"] == "telegram"
        # Adapter never reached fatal — the takeover succeeded.
        assert adapter._fatal_called_with is None

    def test_no_synthesis_when_lock_was_uncontested(
        self, isolated_lock_dir, captured_bus, monkeypatch
    ):
        # No lock file present — clean acquire, no previous owner.
        monkeypatch.setattr(status, "_pid_exists", lambda pid: False)
        monkeypatch.setattr(status, "pid_exists", lambda pid: False)

        adapter = _StubAdapter()
        ok = adapter._acquire_platform_lock(
            "telegram-bot-token", "secret", "Telegram bot token"
        )
        assert ok is True
        # No previous PID — no synthesis.
        assert captured_bus.emit.call_count == 0

    def test_genuine_two_gateways_running_still_fatals(
        self, isolated_lock_dir, captured_bus, monkeypatch
    ):
        # Lock claimed by a genuinely-alive PID. Both pre- and post-recheck
        # checks return alive; legitimate two-gateways-running case must
        # still fail with FATAL — never synthesize, never take over.
        _seed_lock(isolated_lock_dir, "telegram-bot-token", "secret", pid=99999)
        monkeypatch.setattr(status, "_pid_exists", lambda pid: True)
        monkeypatch.setattr(status, "pid_exists", lambda pid: True)
        monkeypatch.setattr(status, "_get_process_start_time", lambda pid: 123)
        monkeypatch.setattr(status.time, "sleep", lambda _s: None)

        adapter = _StubAdapter()
        ok = adapter._acquire_platform_lock(
            "telegram-bot-token", "secret", "Telegram bot token"
        )
        assert ok is False
        # FATAL set with the canonical message format.
        assert adapter._fatal_called_with is not None
        code, message, retryable = adapter._fatal_called_with
        assert code == "telegram-bot-token_lock"
        assert "PID 99999" in message
        assert retryable is False
        # No synthesis — the previous PID is genuinely alive.
        assert captured_bus.emit.call_count == 0
