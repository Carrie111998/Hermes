"""Hard-green Phase 3 acceptance cells for the poisoned-state contract.

These cells assert that ``run_bounded_*`` marks a timed-out backend suspect
exactly once, never marks on completion, and tolerates backends that have not
adopted the protocol. They exercise the real deadline machinery without
mocking the layer under test.
"""

from __future__ import annotations

import asyncio
import time
from typing import Any

from agent.deadline import run_bounded_async, run_bounded_sync

TIMEOUT_S = 0.05


class RecordingBackend:
    """Minimal structural adopter that records suspect reasons."""

    def __init__(self) -> None:
        self.suspect_reasons: list[str] = []

    def mark_suspect(self, reason: str) -> None:
        self.suspect_reasons.append(reason)

    def ensure_healthy(self) -> bool:
        return not self.suspect_reasons


def test_timeout_marks_backend_suspect_once() -> None:
    """An async timeout marks its backend once with the operation label."""

    async def never() -> Any:
        await asyncio.Event().wait()

    async def run() -> RecordingBackend:
        backend = RecordingBackend()
        result = await run_bounded_async(
            never(), TIMEOUT_S, label="cell3a-async", backend=backend
        )
        assert result.timed_out, "a never-completing awaitable must time out"
        return backend

    backend = asyncio.run(run())
    assert len(backend.suspect_reasons) == 1
    assert "cell3a-async" in backend.suspect_reasons[0]


def test_completion_never_marks_backend() -> None:
    """A call that completes on time leaves its backend unmarked."""

    async def immediate() -> str:
        return "done"

    async def run() -> RecordingBackend:
        backend = RecordingBackend()
        result = await run_bounded_async(
            immediate(), TIMEOUT_S, label="cell3a-complete", backend=backend
        )
        assert not result.timed_out and result.value == "done"
        return backend

    backend = asyncio.run(run())
    assert backend.suspect_reasons == []


def test_sync_flavor_marks_on_timeout() -> None:
    """The synchronous flavor enforces the same suspect-mark contract."""

    def block() -> None:
        time.sleep(10)

    backend = RecordingBackend()
    result = run_bounded_sync(block, TIMEOUT_S, label="cell3a-sync", backend=backend)
    assert result.timed_out
    assert len(backend.suspect_reasons) == 1
    assert "cell3a-sync" in backend.suspect_reasons[0]


def test_non_adopting_backend_is_tolerated() -> None:
    """Missing mark_suspect cannot weaken the deadline bound."""

    class PlainBackend:
        pass

    async def never() -> Any:
        await asyncio.Event().wait()

    async def run() -> Any:
        result = await run_bounded_async(
            never(), TIMEOUT_S, label="cell3a-plain", backend=PlainBackend()
        )
        assert result.timed_out
        return result

    result = asyncio.run(run())
    assert result.label == "cell3a-plain"
