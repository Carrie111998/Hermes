"""Induced-slowness falsifier for the refresh-behind-hung-read wall-clock flake.

INDUCED SLOWNESS: the inner refresh timeout is raised to 0.5s -- well past the
OLD test's 0.1s outer budget.  This models a host so loaded that the inner
deadline lands after the outer one, which is exactly the failing condition the
flake hits under real load.  It is still comfortably inside the adapter's 2.0s
release wait, so the read is genuinely still hung when the inner timeout fires.

OLD shape must FAIL consistently.  NEW shape must PASS.

That expectation is ENFORCED, not merely asserted in prose.  The OLD-shape test
is marked ``xfail(strict=True)``: pytest reports it green only while it keeps
failing, and turns an unexpected PASS (XPASS) into a suite failure.  So this file
is safe to keep in the permanent run -- a plain failing test would be 5 red rows
forever, and a plain ``xfail`` would go quiet if the old shape ever started
passing, which is exactly the signal worth catching.  If it does XPASS, the
induced-slowness model has stopped reproducing the flake and the NEW-shape test
below is no longer evidence of anything.
"""

import asyncio
from threading import Event

import pytest

from tests.session_bridge.test_coordinator import (
    _ContinuationStore,
    _HungRefreshAdapter,
    _refresh_projection,
    _wait_until,
)
from session_bridge.coordinator import BridgeConfig, Provider, SessionBridgeCoordinator

INDUCED_INNER_TIMEOUT = 0.5  # > the OLD outer budget of 0.1
OLD_OUTER_BUDGET = 0.1
# Well above the 0.5s inner deadline, but deliberately UNDER pytest-timeout's
# 30s per-test cap (pyproject [tool.pytest.ini_options]): on a regression this
# must fail here with a readable TimeoutError, not get killed by the plugin.
NEW_DEADLOCK_GUARD = 10.0


def _build():
    operations = []
    projection = _refresh_projection(Provider.CODEX)
    session_id = f"codex:{projection.native_id}"
    store = _ContinuationStore(operations)
    store.add_external(
        session_id,
        provider=Provider.CODEX,
        native_id=projection.native_id,
        cursor="codex-cursor-durable",
        source_hash="codex-hash-durable",
    )
    started = Event()
    release = Event()
    adapter = _HungRefreshAdapter(
        projection, operations, started=started, release=release
    )
    coordinator = SessionBridgeCoordinator(
        config=BridgeConfig(),
        store=store,
        adapters={Provider.CODEX: adapter},
        clock=lambda: 100.0,
    )
    return coordinator, adapter, session_id, started, release


@pytest.mark.xfail(
    strict=True,
    reason=(
        "the OLD outer-budget shape is the flake being falsified: the inner "
        "refresh deadline (0.5s) lands after the outer budget (0.1s), so "
        "wait_for cancels and `second` is never assigned. Must keep failing; "
        "an XPASS means the induced-slowness model stopped reproducing it."
    ),
)
@pytest.mark.asyncio
@pytest.mark.parametrize("attempt", range(5))
async def test_OLD_shape_under_induced_slowness(attempt) -> None:
    coordinator, adapter, session_id, started, release = _build()

    first = await coordinator.refresh_session(session_id, timeout=0.01)
    await _wait_until(started.is_set)
    second = None
    try:
        second = await asyncio.wait_for(
            coordinator.refresh_session(session_id, timeout=INDUCED_INNER_TIMEOUT),
            timeout=OLD_OUTER_BUDGET,
        )
    except TimeoutError:
        pass
    finally:
        release.set()
        await asyncio.sleep(0.03)

    assert first.stale is True
    assert second is not None
    assert second.stale is True
    assert second.cursor == "codex-cursor-durable"
    assert adapter.read_calls == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("attempt", range(5))
async def test_NEW_shape_under_induced_slowness(attempt) -> None:
    coordinator, adapter, session_id, started, release = _build()

    first = await coordinator.refresh_session(session_id, timeout=0.01)
    await _wait_until(started.is_set)
    hung_reads = tuple(coordinator._provider_tasks)
    assert len(hung_reads) == 1
    hung_read = hung_reads[0]
    assert hung_read.done() is False

    try:
        second = await asyncio.wait_for(
            coordinator.refresh_session(session_id, timeout=INDUCED_INNER_TIMEOUT),
            timeout=NEW_DEADLOCK_GUARD,
        )
        returned_while_read_still_hung = not hung_read.done()
    finally:
        release.set()
        await _wait_until(hung_read.done)

    assert first.stale is True
    assert second.stale is True
    assert second.cursor == "codex-cursor-durable"
    assert returned_while_read_still_hung is True
    assert adapter.read_calls == 1
