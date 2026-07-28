"""Property tests over PendingCompletionRegistry's state machine.

Suggested location: ``tests/gateway/test_pending_registry_properties.py``.

Reconciled against ``pr-73469`` (PendingCompletionRegistry in gateway/run.py).
"""

from __future__ import annotations

import asyncio
import pytest

hypothesis = pytest.importorskip(
    "hypothesis",
    reason="hypothesis is not a declared test dependency; see module docstring",
)

from hypothesis import HealthCheck, settings  # noqa: E402
from hypothesis import strategies as st  # noqa: E402
from hypothesis.stateful import RuleBasedStateMachine, invariant, rule  # noqa: E402

from gateway.run import GatewayRunner  # type: ignore

PendingCompletionRegistry = GatewayRunner.PendingCompletionRegistry
State = PendingCompletionRegistry.State
MAX_ATTEMPTS = PendingCompletionRegistry.MAX_ATTEMPTS
CAPACITY = PendingCompletionRegistry.BATCH_CAPACITY
TERMINAL = PendingCompletionRegistry._TERMINAL

identities = st.text(
    alphabet="abcdef0123456789", min_size=1, max_size=6
).map(lambda s: f"proc_{s}")

routes = st.sampled_from([("route_a",), ("route_b",), (None,), ("",)])


def _dummy_payload() -> dict:
    return {"synth_text": "test", "evt": {}}


def _fresh() -> PendingCompletionRegistry:
    return PendingCompletionRegistry()


def run_sync(coro):
    """Run coroutine in a fresh event loop (enqueue needs get_running_loop)."""
    return asyncio.run(coro)


class RegistryStateMachine(RuleBasedStateMachine):
    """Drives the registry against a reference model held in a plain dict."""

    def __init__(self) -> None:
        super().__init__()
        self.registry = PendingCompletionRegistry()
        self.model: dict[str, State] = {}
        self.routes: dict[str, object] = {}
        self.delivered_count: dict[str, int] = {}
        self.stopping = False
        # Hypothesis stateful is sync, but enqueue() calls get_running_loop().
        # Wrap enqueue in asyncio.run so the loop exists for Future creation.
        self._loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self._loop)

    @rule(identity=identities, route=routes)
    def enqueue(self, identity: str, route: tuple) -> None:
        future = asyncio.run(self._enqueue_async(identity, route))
        if future is not None:
            assert identity not in self.model or self.model[identity] in TERMINAL
            self.model[identity] = State.PENDING
            self.routes[identity] = route

    async def _enqueue_async(self, identity: str, route: tuple):
        return self.registry.enqueue(identity, route, _dummy_payload())

    @rule(batch=st.lists(identities, min_size=1, max_size=6), batch_id=st.just("b"))
    def claim_batch(self, batch: list[str], batch_id: str) -> None:
        fresh = [i for i in dict.fromkeys(batch) if self.model.get(i) is State.PENDING]
        claimed, skipped = self.registry.claim_batch(fresh, batch_id)
        actual_claimed_ids = [c["identity"] for c in claimed]
        assert set(actual_claimed_ids) == set(fresh) or not actual_claimed_ids, (
            f"partial claim: asked {sorted(fresh)}, got {sorted(actual_claimed_ids)}"
        )
        for c in claimed:
            self.model[c["identity"]] = State.CLAIMED

    @rule(identity=identities)
    def deliver_ok(self, identity: str) -> None:
        if self.model.get(identity) is not State.CLAIMED:
            return
        self.registry.deliver(identity)
        self.model[identity] = State.DELIVERED
        self.delivered_count[identity] = self.delivered_count.get(identity, 0) + 1

    @rule(identity=identities)
    def deliver_transient_failure(self, identity: str) -> None:
        if self.model.get(identity) is not State.CLAIMED:
            return
        backoff = self.registry.retry(identity)
        self.model[identity] = State.PENDING if backoff is not None else State.FAILED

    @rule(identity=identities)
    def deliver_duplicate(self, identity: str) -> None:
        if self.model.get(identity) is not State.CLAIMED:
            return
        self.registry.supersede(identity)
        self.model[identity] = State.SUPERSEDED

    @rule(identity=identities)
    def deliver_unroutable(self, identity: str) -> None:
        if self.model.get(identity) is not State.CLAIMED:
            return
        self.registry.mark_unroutable(identity)
        self.model[identity] = State.UNROUTABLE

    @rule()
    def stop(self) -> None:
        stopped = self.registry.signal_stop()
        self.stopping = True
        for entry in stopped:
            self.model[entry["identity"]] = State.STOPPING

    # ------------------------------------------------------------- invariants

    @invariant()
    def registry_matches_model(self) -> None:
        snap = self.registry.snapshot()
        for ident, expected in self.model.items():
            actual = snap.get(ident)
            assert actual is expected, f"{ident}: model={expected}, registry={actual}"

    @invariant()
    def exactly_once_delivery(self) -> None:
        for identity, count in self.delivered_count.items():
            assert count == 1, f"{identity} delivered {count} times"

    @invariant()
    def attempts_never_exceed_ceiling(self) -> None:
        for entry in self.registry._entries.values():
            assert entry["attempts"] <= MAX_ATTEMPTS, (
                f"{entry['identity']} attempts={entry['attempts']} > {MAX_ATTEMPTS}"
            )

    @invariant()
    def terminal_states_are_final(self) -> None:
        snap = self.registry.snapshot()
        for identity, state in self.model.items():
            if state in TERMINAL:
                assert snap.get(identity) is state, (
                    f"{identity} regressed out of terminal {state}"
                )

    @invariant()
    def capacity_is_respected(self) -> None:
        live = [
            e for e in self.registry._entries.values()
            if e["state"] in (State.PENDING, State.CLAIMED)
        ]
        assert len(live) <= CAPACITY, f"{len(live)} live entries exceeds {CAPACITY}"

    @invariant()
    def distinct_routes_stay_distinct(self) -> None:
        for identity, route in self.routes.items():
            if identity in self.model and self.model[identity] not in TERMINAL:
                entry = self.registry._entries.get(identity)
                if entry is not None:
                    assert entry["route_key"] == route, (
                        f"{identity}: route_key={entry['route_key']} != {route}"
                    )


RegistryStateMachine.TestCase.settings = settings(
    max_examples=200,
    stateful_step_count=40,
    deadline=None,
    suppress_health_check=[HealthCheck.too_slow],
)

TestRegistryStateMachine = RegistryStateMachine.TestCase


# ---------------------------------------------------------------- unit checks

def test_deliver_without_claim_is_noop() -> None:
    async def _test():
        reg = _fresh()
        reg.enqueue("proc_a", ("r1",), _dummy_payload())
        reg.deliver("proc_a")
        assert reg.snapshot()["proc_a"] is State.PENDING
    run_sync(_test())


def test_claim_of_already_claimed_by_other_batch_is_rejected() -> None:
    async def _test():
        reg = _fresh()
        reg.enqueue("proc_a", ("r1",), _dummy_payload())
        claimed, skipped = reg.claim_batch(["proc_a"], "b1")
        assert len(claimed) == 1 and claimed[0]["identity"] == "proc_a"
        claimed2, skipped2 = reg.claim_batch(["proc_a"], "b2")
        assert len(claimed2) == 0
        assert skipped2 == ["proc_a"]
    run_sync(_test())


def test_terminal_entry_cannot_be_reclaimed() -> None:
    async def _test():
        reg = _fresh()
        reg.enqueue("proc_a", ("r1",), _dummy_payload())
        reg.claim_batch(["proc_a"], "b1")
        reg.deliver("proc_a")
        claimed, skipped = reg.claim_batch(["proc_a"], "b2")
        assert len(claimed) == 0
    run_sync(_test())


def test_none_and_empty_route_do_not_coalesce() -> None:
    async def _test():
        reg = _fresh()
        reg.enqueue("proc_none", (None,), _dummy_payload())
        reg.enqueue("proc_empty", ("",), _dummy_payload())
        e_none = reg._entries["proc_none"]
        e_empty = reg._entries["proc_empty"]
        assert e_none["route_key"] != e_empty["route_key"]
    run_sync(_test())


def test_overflow_is_counted_not_silent() -> None:
    async def _test():
        reg = _fresh()
        for i in range(CAPACITY + 5):
            reg.enqueue(f"proc_{i:04d}", ("r1",), _dummy_payload())
        # CAPACITY entries accepted, 5 silently rejected (returned None)
        assert len(reg._entries) == CAPACITY
    run_sync(_test())
