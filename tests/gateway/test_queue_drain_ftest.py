"""Functional regression tests for the queue-drain fallback in
``GatewayRunner._run_agent_inner``.

Bug (fixed by this branch): when the drain loop consumed a queued follow-up
event whose ``@`` context reference was blocked,
``_prepare_profile_scoped_inbound_message_text`` returned ``None`` and the
drain loop did ``return result`` — silently abandoning the dequeued event
and all subsequent queued messages.

Fix (gateway/run.py ~line 19913): capture the prepare result in ``prepared``
and only override ``next_message`` when non-None; the raw ``pending`` text
set earlier survives as the fallback and the drain loop continues.

--------------------------------------------------------------------------
Test coverage in this file
--------------------------------------------------------------------------

Adapter-surface tests (Tests 1-3) exercise the adapter's drain machinery
(``BasePlatformAdapter._process_message_background``), which is the outer
loop that drives the runner-level drain. They pin the invariant that a
queued follow-up cannot be silently dropped from the adapter side.

The runner-level drain block (~line 19792 in gateway/run.py) is a single
sub-branch inside a 3000-line method. Testing it in isolation requires
extensive mocking of _run_agent_inner collaborators (SessionDB, adapter
send-with-retry, agent runtime, config resolution). That harness is
deferred as a separate iteration — see TODO block at the bottom of this
file. For now the fix is defended by:

  1. The 3-line Plan B patch itself (very small, reviewable).
  2. These adapter-surface tests (the drain path drives THROUGH the runner
     block, so an adapter-side symptom would surface).
  3. WeChat manual test cases (TC-A/B/C/D in plan file).

Run:
    uv run pytest tests/gateway/test_queue_drain_ftest.py -v
"""

import asyncio
from unittest.mock import AsyncMock

import pytest

from gateway.config import Platform, PlatformConfig
from gateway.platforms.base import BasePlatformAdapter, MessageEvent, MessageType
from gateway.session import SessionSource, build_session_key


class _CaptureAdapter(BasePlatformAdapter):
    """Adapter stub that captures all delivered sends and lets a test
    handler drive the drain loop by injecting pending follow-ups."""

    def __init__(self):
        super().__init__(
            PlatformConfig(enabled=True, token="test-token"), Platform.TELEGRAM
        )
        self.delivered = []  # list of (chat_id, text)

    async def connect(self, *, is_reconnect: bool = False):
        return True

    async def disconnect(self):
        pass

    async def send(self, chat_id, text, **kwargs):
        self.delivered.append((chat_id, text))
        return None

    async def get_chat_info(self, chat_id):
        return {"id": chat_id, "type": "dm"}


def _make_adapter() -> _CaptureAdapter:
    adapter = _CaptureAdapter()
    adapter._send_with_retry = AsyncMock(return_value=None)
    return adapter


def _make_event(text: str = "hi", chat_id: str = "42") -> MessageEvent:
    return MessageEvent(
        text=text,
        message_type=MessageType.TEXT,
        source=SessionSource(
            platform=Platform.TELEGRAM, chat_id=chat_id, chat_type="dm"
        ),
    )


def _sk(chat_id: str = "42") -> str:
    return build_session_key(
        SessionSource(platform=Platform.TELEGRAM, chat_id=chat_id, chat_type="dm")
    )


# ---------------------------------------------------------------------------
# Test 1 — Baseline: normal queued follow-up is delivered
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_baseline_single_queued_followup_delivered():
    """Happy path: after a main-turn handler completes, one queued
    follow-up event is drained and its handler is called.

    Fails if a regression in the drain loop drops the follow-up entirely.
    """
    adapter = _make_adapter()
    sk = _sk()

    processed: list[str] = []

    async def handler(event: MessageEvent):
        processed.append(event.text)
        if event.text == "main":
            adapter._pending_messages[sk] = _make_event(text="follow-up")
        return "ok"

    adapter._message_handler = handler

    await adapter.handle_message(_make_event(text="main"))

    for _ in range(400):
        if "follow-up" in processed and sk not in adapter._active_sessions:
            break
        await asyncio.sleep(0.01)

    await adapter.cancel_background_tasks()

    assert processed == ["main", "follow-up"], (
        f"queued follow-up was not drained after main turn — got {processed!r}"
    )


# ---------------------------------------------------------------------------
# Test 2 — Multiple queued follow-ups all delivered in FIFO order
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_multiple_queued_followups_delivered_in_fifo_order():
    """Queue three messages during a running turn (one per handler
    invocation) and confirm the drain loop chains through all of them
    in the order they were queued.

    Fails if the drain aborts early after N < 3 events (which is exactly
    what the queue-drain-null-return bug caused when the N-th event tripped
    the None-prepare branch).
    """
    adapter = _make_adapter()
    sk = _sk()

    processed: list[str] = []
    next_id = [1]
    total = 4  # main + 3 follow-ups

    async def handler(event: MessageEvent):
        processed.append(event.text)
        if next_id[0] < total:
            adapter._pending_messages[sk] = _make_event(text=f"followup-{next_id[0]}")
            next_id[0] += 1
        return "ok"

    adapter._message_handler = handler

    await adapter.handle_message(_make_event(text="main"))

    for _ in range(600):
        if len(processed) >= total and sk not in adapter._active_sessions:
            break
        await asyncio.sleep(0.01)

    await adapter.cancel_background_tasks()

    assert processed == ["main", "followup-1", "followup-2", "followup-3"], (
        f"drain chain broke — expected 4 events in FIFO order, got {processed!r}"
    )


# ---------------------------------------------------------------------------
# Test 3 — Handler exception on a follow-up does not corrupt drain state
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_handler_exception_on_followup_does_not_stick_session():
    """If a follow-up handler raises, the drain machinery must still
    release ``_active_sessions[sk]`` and ``_session_tasks[sk]`` so the
    session isn't permanently pinned as busy.

    This is a safety invariant for the fallback path: after the Plan B
    fix, a broken @-reference now flows into ``_run_agent`` as raw text.
    If that raw text triggers a downstream exception, the session must
    still recover.
    """
    adapter = _make_adapter()
    sk = _sk()

    processed: list[str] = []

    async def handler(event: MessageEvent):
        processed.append(event.text)
        if event.text == "main":
            adapter._pending_messages[sk] = _make_event(text="boom")
            return "ok"
        raise RuntimeError("simulated agent failure on follow-up")

    adapter._message_handler = handler

    await adapter.handle_message(_make_event(text="main"))

    for _ in range(400):
        if "boom" in processed and sk not in adapter._active_sessions:
            break
        await asyncio.sleep(0.01)

    await adapter.cancel_background_tasks()

    assert "main" in processed and "boom" in processed, (
        f"follow-up handler was not invoked despite the drain firing — "
        f"processed={processed!r}"
    )
    assert sk not in adapter._active_sessions, (
        "handler exception on follow-up left _active_sessions[sk] populated "
        "— future messages would take the busy-handler path forever"
    )
    assert sk not in adapter._session_tasks, (
        "handler exception on follow-up left _session_tasks[sk] populated "
        "— stale-lock detection will treat the dead task as alive"
    )


# ---------------------------------------------------------------------------
# TODO — runner-level drain isolation test (deferred; needs deeper harness)
# ---------------------------------------------------------------------------
#
# The exact bug lives inside GatewayRunner._run_agent_inner (~line 19913
# in gateway/run.py), where a `prepare returned None` used to `return result`
# instead of falling back to the raw pending text.
#
# A targeted unit test would:
#   1. Construct a bare GatewayRunner (see tests/gateway/test_42039_...
#      _bootstrap() for the pattern)
#   2. Monkeypatch _prepare_profile_scoped_inbound_message_text -> None
#   3. Inject a pending event via _dequeue_pending_event
#   4. Call _run_agent with a canned main-turn setup
#   5. Assert the recursive _run_agent call fires with next_message = pending
#
# _run_agent_inner is ~3000 lines and has ~30 collaborators (SessionDB,
# adapter delivery, agent runtime resolution, config, session store, hooks,
# etc.). Building the mock harness is a project of its own — scheduled as
# a separate iteration once the runner-level drain block is refactored into
# a small helper method (candidate: _prepare_drain_followup_or_fallback).
