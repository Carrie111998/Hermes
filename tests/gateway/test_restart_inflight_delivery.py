"""Regression: a restart must not tear down the process mid-delivery.

Production symptom (five occurrences, 2026-08-08 → 2026-08-23): the user gets
the SAME final answer twice, the second copy prefixed

    ♻️ Recovered reply — the gateway restarted during delivery, ...

Mechanism. ``_run_agent_inner`` releases the turn's ``_running_agents`` slot in
its ``finally``, which runs BEFORE the final response is handed to the platform
adapter. Everything after that point — ``_send_with_retry`` and the ledger's
``mark_delivered`` — was invisible to ``_active_work_count()``.

So the restart wait (#77184) declared "active work drained" while a reply was
still in flight, ``stop()`` ran, and the process was replaced between
``mark_attempting`` and ``mark_delivered``. The next boot swept the still-
``attempting`` row and redelivered a reply the user already had.

Every production occurrence shows the same fingerprint — drain-complete lands
~0.4-0.5s BEFORE ``response ready``:

    03:47:01.527  Restart deferred wait complete — active work drained
    03:47:02.004  response ready ... 2619 chars
    03:47:02.028  [Slack] Sending response
    03:47:05.290  Launched systemd planned-restart helper
    03:47:26.497  Redelivered recovered final response ... attempt 1

The fix counts obligation-backed sends in ``_active_work_count()`` for exactly
the send+settle window, so the restart wait blocks until the ledger is settled.
"""

import asyncio

import pytest


class _FakeAdapter:
    """Minimal stand-in exposing the counter contract used by the runner."""

    def __init__(self):
        self._inflight_final_deliveries = 0

    # --- mirrors BasePlatformAdapter ---
    def _begin_inflight_final_delivery(self) -> bool:
        self._inflight_final_deliveries += 1
        return True

    def _end_inflight_final_delivery(self) -> None:
        self._inflight_final_deliveries = max(
            0, self._inflight_final_deliveries - 1
        )

    @property
    def inflight_final_deliveries(self) -> int:
        return self._inflight_final_deliveries


class _FakeRunner:
    """Work-accounting surface under test.

    Deliberately binds the REAL ``GatewayRunner`` methods rather than
    reimplementing them: a fake that re-declares ``_active_work_count`` would
    keep passing even if the production wiring were removed, which is exactly
    the bug this file exists to catch.
    """

    def __init__(self, adapters):
        self.adapters = adapters
        self._running = 0

    def _running_agent_count(self) -> int:
        return self._running

    def _active_cron_job_count(self) -> int:
        return 0

    def _active_api_run_count(self) -> int:
        return 0

    # Bound from production so a regression there fails these tests.
    from gateway.run import GatewayRunner as _GR

    _inflight_final_delivery_count = _GR._inflight_final_delivery_count
    _active_work_count = _GR._active_work_count
    del _GR


class TestRestartWaitSeesInflightDelivery:
    def test_delivery_after_turn_release_is_still_active_work(self):
        """THE regression.

        The turn has ended (running_agents == 0) but the reply is mid-send.
        Before the fix _active_work_count() returned 0 here and the restart
        wait proceeded to stop(), producing the duplicate.
        """
        adapter = _FakeAdapter()
        runner = _FakeRunner({"slack": adapter})

        runner._running = 1                    # turn executing
        assert runner._active_work_count() == 1

        runner._running = 0                    # _run_agent_inner finally ran
        adapter._begin_inflight_final_delivery()   # ...then the send starts

        assert runner._active_work_count() == 1, (
            "restart wait sees an idle gateway while a reply is still in "
            "flight; stop() here is what produced the Recovered-reply "
            "duplicate"
        )

        adapter._end_inflight_final_delivery()  # ledger settled
        assert runner._active_work_count() == 0

    def test_restart_wait_blocks_until_delivery_settles(self):
        """End-to-end shape of the wait loop against a real in-flight send."""
        adapter = _FakeAdapter()
        runner = _FakeRunner({"slack": adapter})
        observed = []

        async def waiter():
            for _ in range(100):
                n = runner._active_work_count()
                observed.append(n)
                if n == 0:
                    return True
                await asyncio.sleep(0.01)
            return False

        async def deliver():
            adapter._begin_inflight_final_delivery()
            try:
                await asyncio.sleep(0.05)      # the send
            finally:
                adapter._end_inflight_final_delivery()

        async def scenario():
            d = asyncio.create_task(deliver())
            await asyncio.sleep(0.005)         # let the send claim first
            w = asyncio.create_task(waiter())
            return await asyncio.gather(d, w)

        _, drained = asyncio.run(scenario())
        assert drained is True
        assert observed[0] == 1, "wait did not observe the in-flight send"
        assert observed[-1] == 0, "wait did not settle"


class TestClaimIsReleasedOnEveryPath:
    def test_failed_send_still_releases(self):
        """A send that raises must not leak the claim.

        A leaked claim keeps the gateway permanently 'busy' — that would block
        restart AND scale-to-zero, which is worse than the duplicate.
        """
        adapter = _FakeAdapter()
        runner = _FakeRunner({"slack": adapter})

        async def failing_delivery():
            adapter._begin_inflight_final_delivery()
            try:
                raise RuntimeError("slack exploded")
            finally:
                adapter._end_inflight_final_delivery()

        with pytest.raises(RuntimeError):
            asyncio.run(failing_delivery())

        assert runner._active_work_count() == 0, "claim leaked on error path"

    def test_cancelled_send_still_releases(self):
        adapter = _FakeAdapter()
        runner = _FakeRunner({"slack": adapter})

        async def cancelled_delivery():
            adapter._begin_inflight_final_delivery()
            try:
                await asyncio.sleep(10)
            finally:
                adapter._end_inflight_final_delivery()

        async def scenario():
            t = asyncio.create_task(cancelled_delivery())
            await asyncio.sleep(0.01)
            t.cancel()
            try:
                await t
            except asyncio.CancelledError:
                pass

        asyncio.run(scenario())
        assert runner._active_work_count() == 0, "claim leaked on cancel path"

    def test_counter_never_goes_negative(self):
        adapter = _FakeAdapter()
        adapter._end_inflight_final_delivery()
        adapter._end_inflight_final_delivery()
        assert adapter.inflight_final_deliveries == 0


class TestNoFalseBusy:
    def test_idle_gateway_reports_zero(self):
        """Adding the counter must not make an idle gateway look busy."""
        runner = _FakeRunner({"slack": _FakeAdapter()})
        assert runner._active_work_count() == 0

    def test_adapter_without_counter_contributes_zero(self):
        """Platforms predating the counter must not break the sum."""

        class _Legacy:
            pass

        runner = _FakeRunner({"legacy": _Legacy(), "slack": _FakeAdapter()})
        assert runner._active_work_count() == 0

    def test_concurrent_deliveries_are_counted_independently(self):
        a, b = _FakeAdapter(), _FakeAdapter()
        runner = _FakeRunner({"slack": a, "telegram": b})

        a._begin_inflight_final_delivery()
        b._begin_inflight_final_delivery()
        b._begin_inflight_final_delivery()
        assert runner._active_work_count() == 3

        b._end_inflight_final_delivery()
        assert runner._active_work_count() == 2


class TestRealAdapterImplementsContract:
    """Guard the fake against drift from the real BasePlatformAdapter."""

    def test_base_adapter_exposes_the_counter_api(self):
        from gateway.platforms.base import BasePlatformAdapter

        for name in (
            "_begin_inflight_final_delivery",
            "_end_inflight_final_delivery",
            "inflight_final_deliveries",
        ):
            assert hasattr(BasePlatformAdapter, name), f"missing {name}"

    def test_runner_counts_adapter_claims(self):
        from gateway.run import GatewayRunner

        assert hasattr(GatewayRunner, "_inflight_final_delivery_count")

        runner = object.__new__(GatewayRunner)
        adapter = _FakeAdapter()
        runner.adapters = {"slack": adapter}

        assert runner._inflight_final_delivery_count() == 0
        adapter._begin_inflight_final_delivery()
        assert runner._inflight_final_delivery_count() == 1
