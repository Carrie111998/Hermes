"""Deterministic test harness for the gateway completion-delivery path.

Suggested location: ``tests/gateway/_harness.py`` (check the repo's existing
convention first — if there is a ``tests/support/`` package, prefer that).

Why this exists
---------------
Both review rounds on #72675 were driven by hand-written fault probes: remove an
adapter mid-window, block delivery and cancel, and so on. Each probe was rebuilt
from scratch, and none of them survived into the suite. This harness makes those
probes a reusable fixture so any future change to the delivery path can prove
correctness rather than argue it.

Hard rule: **no real ``asyncio.sleep`` anywhere in tests built on this.** Wall-clock
sleeps are flaky under CI load, and the reviewer measured and reported test timings —
they will be noticed.

Required production seam (one line)
-----------------------------------
The batch window must be awaited through an injectable callable rather than a
direct ``asyncio.sleep``. In ``GatewayRunner``::

    # production default, set in __init__
    self._batch_window_sleep = asyncio.sleep

and in the flush::

    await self._batch_window_sleep(self._completion_notification_batch_window)

That is the only change to production code this harness needs. Cancellation is then
injected with no further seams, because the two interesting points are both places
where the code is already suspended:

* ``in_window``            -> suspended on ``_batch_window_sleep``
* ``during_adapter_await`` -> suspended on the adapter's delivery gate

ADAPT BEFORE USE
----------------
Names marked ``ADAPT:`` below are guesses at the real API and must be reconciled
with ``gateway/run.py``. Do not assume they are correct.
"""

from __future__ import annotations

import asyncio
import contextlib
from dataclasses import dataclass, field
from typing import Any, Callable, Literal

CancelPoint = Literal[
    "never",
    "before_window",
    "in_window",
    "during_adapter_await",
    "after_adapter_return",
]

AdapterBehaviour = Literal[
    "ok",
    "transient_fail",
    "blocked",
    "disconnect_then_recover",
]


class VirtualClock:
    """Logical time. ``sleep`` suspends until ``advance`` passes the deadline.

    Deliberately not a full event-loop clock replacement: it only needs to control
    the batch window, and keeping it small keeps it obvious.
    """

    def __init__(self) -> None:
        self.now: float = 0.0
        self._waiters: list[tuple[float, asyncio.Future[None]]] = []

    async def sleep(self, delay: float) -> None:
        if delay <= 0:
            await asyncio.sleep(0)  # yield a tick, no wall time
            return
        loop = asyncio.get_running_loop()
        fut: asyncio.Future[None] = loop.create_future()
        self._waiters.append((self.now + delay, fut))
        await fut

    def advance(self, delta: float) -> None:
        """Release every waiter whose deadline has passed."""
        self.now += delta
        still_waiting = []
        for deadline, fut in self._waiters:
            if deadline <= self.now and not fut.done():
                fut.set_result(None)
            elif not fut.done():
                still_waiting.append((deadline, fut))
        self._waiters = still_waiting

    @property
    def pending_sleepers(self) -> int:
        return sum(1 for _, fut in self._waiters if not fut.done())

    async def settle(self, ticks: int = 3) -> None:
        """Let the loop run without advancing logical time."""
        for _ in range(ticks):
            await asyncio.sleep(0)


class ControllableAdapter:
    """Stands in for a gateway adapter. Every delivery is observable and gateable."""

    def __init__(self) -> None:
        self.behaviour: AdapterBehaviour = "ok"
        self.gate = asyncio.Event()
        self.gate.set()  # open by default; clear() to block delivery
        self.calls: list[dict[str, Any]] = []
        self.connected = True
        self._fail_once = False

    async def deliver(self, payload: dict[str, Any]) -> bool:
        """ADAPT: match the real adapter's delivery signature and return type."""
        await self.gate.wait()

        if self.behaviour == "blocked":
            # Caller is expected to cancel; this never returns on its own.
            await asyncio.Event().wait()

        if self.behaviour == "disconnect_then_recover" and self.connected:
            self.connected = False
            raise ConnectionError("adapter disconnected")

        if self.behaviour == "transient_fail":
            raise ConnectionError("transient delivery failure")

        self.calls.append(payload)
        return True

    def block(self) -> None:
        self.gate.clear()

    def release(self) -> None:
        self.gate.set()

    def recover(self) -> None:
        self.connected = True
        self.behaviour = "ok"


@dataclass
class GatewayHarness:
    """Fixture object wired around a GatewayRunner under test."""

    runner: Any                                  # ADAPT: GatewayRunner instance
    clock: VirtualClock = field(default_factory=VirtualClock)
    adapter: ControllableAdapter = field(default_factory=ControllableAdapter)
    events: list[str] = field(default_factory=list)

    # ------------------------------------------------------------------ setup

    def install(self) -> None:
        """Wire clock and adapter into the runner. Call once, before any enqueue."""
        self.runner._batch_window_sleep = self.clock.sleep       # ADAPT
        self.runner._adapters = {"test": self.adapter}           # ADAPT

    def record(self, name: str) -> None:
        """Ordering probe. Call from instrumented production points, or from
        wrappers installed in the test — never assert order with sleeps."""
        self.events.append(name)

    # ---------------------------------------------------------------- driving

    async def enqueue_completions(self, n: int, *, route: str = "r1") -> list[Any]:
        """ADAPT: build n plausible completion payloads and enqueue each."""
        results = []
        for i in range(n):
            results.append(
                await self.runner._enqueue_process_completion_notification(
                    identity=f"proc_{i:04d}",
                    route_key=route,
                    payload={"exit_code": 0, "reason": "exited", "elapsed": 1.0},
                )
            )
        return results

    async def run_flush(
        self,
        *,
        window: float = 0.1,
        cancel_at: CancelPoint = "never",
    ) -> None:
        """Drive one flush to completion, optionally cancelling at a named point.

        No production seam is needed for cancellation: both interesting points are
        places where the flush is already suspended (the virtual clock, or the
        adapter gate), so the test simply cancels while it is parked there.
        """
        if cancel_at == "before_window":
            task = asyncio.create_task(self._flush(window))
            task.cancel()
            await self._await_cancelled(task)
            return

        if cancel_at == "during_adapter_await":
            self.adapter.block()

        task = asyncio.create_task(self._flush(window))
        await self.clock.settle()

        if cancel_at == "in_window":
            assert self.clock.pending_sleepers, "flush is not parked in the window"
            task.cancel()
            await self._await_cancelled(task)
            return

        # release the window; flush proceeds to delivery
        self.clock.advance(window)
        await self.clock.settle()

        if cancel_at == "during_adapter_await":
            task.cancel()
            await self._await_cancelled(task)
            self.adapter.release()
            return

        if cancel_at == "after_adapter_return":
            self.adapter.release()
            await self.clock.settle()
            task.cancel()
            await self._await_cancelled(task)
            return

        self.adapter.release()
        await task

    async def _flush(self, window: float) -> None:
        """ADAPT: call the real flush with whatever arguments it takes."""
        await self.runner._flush_process_completion_batch(route_key="r1")

    @staticmethod
    async def _await_cancelled(task: asyncio.Task[Any]) -> None:
        with contextlib.suppress(asyncio.CancelledError):
            await task

    async def stop_gateway(self) -> None:
        """ADAPT: call the real shutdown path."""
        await self.runner._stop_impl_body()

    # ------------------------------------------------------------- assertions

    def assert_no_unresolved_futures(self) -> None:
        pending = [
            e for e in self._registry_entries()
            if getattr(e, "future", None) is not None and not e.future.done()
        ]
        assert not pending, f"unresolved waiter futures: {pending}"

    def assert_no_orphaned_tasks(self) -> None:
        leaked = [
            t for t in asyncio.all_tasks()
            if t is not asyncio.current_task()
            and not t.done()
            and "flush_process_completion" in (t.get_name() or "")
        ]
        assert not leaked, f"orphaned flush tasks: {leaked}"

    def assert_no_stale_registry_entries(self) -> None:
        """No entry may sit in CLAIMED once the loop is quiescent — CLAIMED is a
        transient state held only across the adapter await."""
        stuck = [e for e in self._registry_entries() if e.state is _State().CLAIMED]
        assert not stuck, f"entries stuck in CLAIMED: {stuck}"

    def assert_no_legacy_state(self) -> None:
        """Gate 2: the old mechanism must be gone, not merely unused."""
        for attr in (
            "_completion_notification_batches",
            "_completion_notification_batch_tasks",
            "_record_coalesced_completion_siblings",
        ):
            assert not hasattr(self.runner, attr), f"legacy state survives: {attr}"

    def assert_ordering(self, *expected: str) -> None:
        seen = [e for e in self.events if e in expected]
        assert seen == list(expected), f"order was {seen}, expected {list(expected)}"

    def synthetic_turns_delivered(self) -> int:
        return len(self.adapter.calls)

    def delivered_payload(self) -> dict[str, Any]:
        assert len(self.adapter.calls) == 1, f"expected 1 turn, got {len(self.adapter.calls)}"
        return self.adapter.calls[0]

    # ----------------------------------------------------------------- internals

    def _registry_entries(self) -> list[Any]:
        """ADAPT: real accessor for registry entries."""
        return list(self.runner._pending_completions.entries())


class _State:  # ADAPT: import the real State enum and delete this shim
    CLAIMED = "CLAIMED"


# --------------------------------------------------------------------- fixtures

def make_harness(runner: Any) -> GatewayHarness:
    h = GatewayHarness(runner=runner)
    h.install()
    return h
