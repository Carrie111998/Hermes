"""Gateway lifecycle for the Orca completion bridge (gateway/run.py).

The bridge and the webhook listener are one unit: the bridge has no socket, and
the listener has nowhere to put an event without the bridge. Both directions of
that coupling have a failure mode worth a test:

  * startup — if the bridge cannot come up, an already-listening webhook server
    must be taken back down and removed from the platform map, or the gateway
    serves a route that accepts authenticated completion events and drops them
    (G19)
  * shutdown — the listener must close BEFORE the bridge stops recording, or an
    event accepted in the gap is answered with a failure for work that was in
    fact delivered (G20)
"""

import asyncio
from unittest.mock import AsyncMock, MagicMock

import pytest

from gateway.config import GatewayConfig, Platform, PlatformConfig
from gateway.run import GatewayRunner, OrcaBridgeSupervisor

pytestmark = pytest.mark.asyncio


class _FakeWebhook:
    """Stands in for a started WebhookAdapter."""

    def __init__(self, bridge_routes=("orca",)):
        self._bridge_routes = list(bridge_routes)
        self.disconnected = 0
        self.calls = []

    def orca_bridge_routes(self):
        return list(self._bridge_routes)

    async def disconnect(self):
        self.disconnected += 1
        self.calls.append("webhook.disconnect")


class _FakeBridge:
    """Stands in for the tools.orca_bridge module."""

    def __init__(self, start_error=None, calls=None):
        self.start_error = start_error
        self.calls = calls if calls is not None else []
        self.started = 0
        self.stopped = 0

    def start(self):
        self.started += 1
        self.calls.append("bridge.start")
        if self.start_error:
            raise self.start_error

    def stop(self):
        self.stopped += 1
        self.calls.append("bridge.stop")

    def sweep(self):
        self.calls.append("bridge.sweep")
        return 0


# ---------------------------------------------------------------------------
# G19 — startup rollback
# ---------------------------------------------------------------------------

class TestStartupRollback:
    async def test_bridge_start_failure_shuts_the_webhook_down(self):
        """The already-started webhook server must not be left listening."""
        webhook = _FakeWebhook()
        adapters = {Platform.WEBHOOK: webhook}
        supervisor = OrcaBridgeSupervisor(
            adapters, Platform.WEBHOOK, webhook=webhook,
            bridge=_FakeBridge(start_error=RuntimeError("state.db is corrupt")),
        )

        assert await supervisor.start() is False
        assert webhook.disconnected == 1, (
            "a listener whose bridge failed must be shut down"
        )

    async def test_bridge_start_failure_cleans_the_platform_map(self):
        webhook = _FakeWebhook()
        adapters = {Platform.WEBHOOK: webhook, Platform.TELEGRAM: object()}
        supervisor = OrcaBridgeSupervisor(
            adapters, Platform.WEBHOOK, webhook=webhook,
            bridge=_FakeBridge(start_error=RuntimeError("boom")),
        )

        assert await supervisor.start() is False
        assert Platform.WEBHOOK not in adapters, (
            "nothing may route to a listener we just closed"
        )
        assert Platform.TELEGRAM in adapters, "other platforms are untouched"

    async def test_rollback_stops_the_bridge_too(self):
        """A partially-started bridge is stopped, not left half-open."""
        calls = []
        webhook = _FakeWebhook()
        webhook.calls = calls
        bridge = _FakeBridge(start_error=RuntimeError("boom"), calls=calls)
        supervisor = OrcaBridgeSupervisor(
            {Platform.WEBHOOK: webhook}, Platform.WEBHOOK,
            webhook=webhook, bridge=bridge,
        )

        assert await supervisor.start() is False
        assert bridge.stopped == 1
        assert calls.index("webhook.disconnect") < calls.index("bridge.stop")

    async def test_successful_start_keeps_everything_up(self):
        webhook = _FakeWebhook()
        adapters = {Platform.WEBHOOK: webhook}
        bridge = _FakeBridge()
        supervisor = OrcaBridgeSupervisor(
            adapters, Platform.WEBHOOK, webhook=webhook, bridge=bridge
        )

        assert await supervisor.start() is True
        assert webhook.disconnected == 0
        assert Platform.WEBHOOK in adapters
        assert bridge.started == 1
        await supervisor.shutdown()

    async def test_runner_rolls_back_on_bridge_failure(self, monkeypatch, tmp_path):
        """Same rollback, exercised through GatewayRunner._start_orca_bridge."""
        monkeypatch.setenv("HERMES_HOME", str(tmp_path))
        runner = GatewayRunner(GatewayConfig(sessions_dir=tmp_path / "sessions"))
        webhook = _FakeWebhook()
        runner.adapters[Platform.WEBHOOK] = webhook

        import tools.orca_bridge as real_bridge

        monkeypatch.setattr(
            real_bridge, "start",
            MagicMock(side_effect=RuntimeError("cannot open state.db")),
        )

        assert await runner._start_orca_bridge() is False
        assert webhook.disconnected == 1
        assert Platform.WEBHOOK not in runner.adapters
        assert runner._orca_supervisor is None

    async def test_runner_is_a_noop_without_a_bridge_route(self, monkeypatch, tmp_path):
        """No bridge route configured → nothing starts, nothing is torn down."""
        monkeypatch.setenv("HERMES_HOME", str(tmp_path))
        runner = GatewayRunner(GatewayConfig(sessions_dir=tmp_path / "sessions"))
        webhook = _FakeWebhook(bridge_routes=())
        runner.adapters[Platform.WEBHOOK] = webhook

        assert await runner._start_orca_bridge() is False
        assert Platform.WEBHOOK in runner.adapters
        assert webhook.disconnected == 0
        assert runner._orca_supervisor is None

    async def test_runner_is_a_noop_without_a_webhook_platform(self, monkeypatch, tmp_path):
        monkeypatch.setenv("HERMES_HOME", str(tmp_path))
        runner = GatewayRunner(GatewayConfig(sessions_dir=tmp_path / "sessions"))
        assert await runner._start_orca_bridge() is False
        assert runner._orca_supervisor is None


# ---------------------------------------------------------------------------
# G20 — shutdown ordering
# ---------------------------------------------------------------------------

class TestShutdownOrdering:
    async def test_webhook_is_shut_down_and_the_bridge_stopped_in_order(self):
        calls = []
        webhook = _FakeWebhook()
        webhook.calls = calls
        bridge = _FakeBridge(calls=calls)
        supervisor = OrcaBridgeSupervisor(
            {Platform.WEBHOOK: webhook}, Platform.WEBHOOK,
            webhook=webhook, bridge=bridge,
        )
        assert await supervisor.start() is True

        await supervisor.shutdown()

        assert webhook.disconnected == 1, (
            "the webhook server shutdown must actually be called"
        )
        assert bridge.stopped == 1
        assert calls.index("webhook.disconnect") < calls.index("bridge.stop"), (
            "the listener must close before the bridge stops recording"
        )

    async def test_shutdown_without_a_webhook_still_stops_the_bridge(self):
        bridge = _FakeBridge()
        supervisor = OrcaBridgeSupervisor(
            {}, Platform.WEBHOOK, webhook=None, bridge=bridge
        )
        await supervisor.shutdown()
        assert bridge.stopped == 1

    async def test_shutdown_is_idempotent(self):
        webhook = _FakeWebhook()
        bridge = _FakeBridge()
        supervisor = OrcaBridgeSupervisor(
            {}, Platform.WEBHOOK, webhook=webhook, bridge=bridge
        )
        await supervisor.shutdown()
        await supervisor.shutdown()
        assert webhook.disconnected == 1
        assert bridge.stopped == 1

    async def test_shutdown_does_not_wait_for_an_in_flight_sweep(self):
        """A sweep still talking to Orca must not wedge shutdown.

        The sweep runs on a worker thread, so cancelling the task unblocks the
        awaiter rather than the thread. What shutdown must guarantee is that it
        returns promptly anyway — a gateway that waits on an Orca round-trip
        here is a gateway that misses its SIGTERM window.
        """
        import threading

        started = threading.Event()
        release = threading.Event()

        class _HangingBridge(_FakeBridge):
            def sweep(self):
                started.set()
                release.wait(timeout=30)
                return 0

        webhook = _FakeWebhook()
        supervisor = OrcaBridgeSupervisor(
            {}, Platform.WEBHOOK, webhook=webhook, bridge=_HangingBridge()
        )
        assert await supervisor.start() is True
        assert await asyncio.to_thread(started.wait, 5) is True

        try:
            await asyncio.wait_for(supervisor.shutdown(), timeout=5)
            assert webhook.disconnected == 1
        finally:
            # Let the worker thread finish so it cannot outlive the test.
            release.set()

    async def test_a_failing_sweep_never_breaks_shutdown(self):
        class _AngryBridge(_FakeBridge):
            def sweep(self):
                raise RuntimeError("orca binary missing")

        webhook = _FakeWebhook()
        bridge = _AngryBridge()
        supervisor = OrcaBridgeSupervisor(
            {}, Platform.WEBHOOK, webhook=webhook, bridge=bridge
        )
        assert await supervisor.start() is True
        await asyncio.wait_for(supervisor.shutdown(), timeout=5)
        assert webhook.disconnected == 1
        assert bridge.stopped == 1

    async def test_runner_stop_path_delegates_to_the_supervisor(self, monkeypatch, tmp_path):
        monkeypatch.setenv("HERMES_HOME", str(tmp_path))
        runner = GatewayRunner(GatewayConfig(sessions_dir=tmp_path / "sessions"))
        supervisor = MagicMock()
        supervisor.shutdown = AsyncMock()
        runner._orca_supervisor = supervisor

        await runner._stop_orca_bridge()

        supervisor.shutdown.assert_awaited_once()
        assert runner._orca_supervisor is None

    async def test_runner_stop_swallows_supervisor_errors(self, monkeypatch, tmp_path):
        monkeypatch.setenv("HERMES_HOME", str(tmp_path))
        runner = GatewayRunner(GatewayConfig(sessions_dir=tmp_path / "sessions"))
        supervisor = MagicMock()
        supervisor.shutdown = AsyncMock(side_effect=RuntimeError("nope"))
        runner._orca_supervisor = supervisor

        await runner._stop_orca_bridge()  # must not raise
        assert runner._orca_supervisor is None

    async def test_runner_stop_without_a_bridge_is_a_noop(self, monkeypatch, tmp_path):
        monkeypatch.setenv("HERMES_HOME", str(tmp_path))
        runner = GatewayRunner(GatewayConfig(sessions_dir=tmp_path / "sessions"))
        await runner._stop_orca_bridge()
        assert runner._orca_supervisor is None


class TestStartupSweep:
    async def test_startup_sweep_runs_once(self):
        webhook = _FakeWebhook()
        bridge = _FakeBridge()
        supervisor = OrcaBridgeSupervisor(
            {}, Platform.WEBHOOK, webhook=webhook, bridge=bridge
        )
        assert await supervisor.start() is True
        # Let the sweep task get scheduled and finish.
        for _ in range(50):
            if "bridge.sweep" in bridge.calls:
                break
            await asyncio.sleep(0.01)
        await supervisor.shutdown()
        assert bridge.calls.count("bridge.sweep") == 1
