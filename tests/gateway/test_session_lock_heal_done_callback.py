"""Regression tests for issue #75280 — stale session lock causes permanent
gateway deadlock because _heal_stale_session_lock only fires reactively on
inbound message.

The fix adds a done_callback in _start_session_processing that proactively
heals the session lock when the owner task exits, regardless of exit reason
(success, error, or cancellation).
"""

import asyncio
from unittest.mock import MagicMock

import pytest

from gateway.config import GatewayConfig, Platform, PlatformConfig
from gateway.platforms.base import (
    BasePlatformAdapter,
    MessageEvent,
    MessageType,
)
from gateway.session import SessionSource, build_session_key


class _StubAdapter(BasePlatformAdapter):
    async def connect(self, *, is_reconnect: bool = False):
        pass  # type: ignore[return-value]

    async def disconnect(self):
        pass

    async def send(self, chat_id, text, **kwargs):
        pass  # type: ignore[return-value]

    async def get_chat_info(self, chat_id):
        return {}  # type: ignore[return-value]


def _make_adapter():
    config = PlatformConfig(enabled=True, token="test-token")
    adapter = _StubAdapter(config, Platform.TELEGRAM)
    adapter._busy_text_mode = ""
    adapter.sent_responses = []  # type: ignore[attr-defined]

    async def _mock_send_retry(chat_id, content, **kwargs):
        adapter.sent_responses.append(content)  # type: ignore[attr-defined]

    adapter._send_with_retry = _mock_send_retry  # type: ignore[attr-defined]
    return adapter


def _session_key(chat_id="12345"):
    source = SessionSource(
        platform=Platform.TELEGRAM, chat_id=chat_id, chat_type="dm"
    )
    return build_session_key(source)


# ===========================================================================
# Tests: proactive session-lock heal via done_callback
# ===========================================================================


class TestProactiveSessionLockHeal:
    """Verify that the done_callback installed by _start_session_processing
    proactively heals the session lock when the owner task exits."""

    @pytest.mark.asyncio
    async def test_heal_callback_heals_on_task_success(self):
        """When the owner task completes successfully, the done_callback heals
        the session lock without waiting for an inbound message."""
        adapter = _make_adapter()
        sk = _session_key()

        guard = asyncio.Event()
        adapter._active_sessions[sk] = guard

        async def _ok():
            return None

        task = asyncio.create_task(_ok())
        adapter._session_tasks[sk] = task
        adapter._background_tasks.add(task)
        task.add_done_callback(adapter._background_tasks.discard)
        task.add_done_callback(adapter._make_session_lock_heal_callback(sk, task))

        await task

        # After the callback fires, the lock should be healed.
        assert sk not in adapter._active_sessions, (
            "session lock should be healed after task completion"
        )
        assert sk not in adapter._session_tasks, (
            "_session_tasks entry should be removed after heal"
        )

    @pytest.mark.asyncio
    async def test_heal_callback_heals_on_task_error(self):
        """When the owner task raises an error, the done_callback still heals
        the session lock (the bug scenario from #75280)."""
        adapter = _make_adapter()
        sk = _session_key()

        guard = asyncio.Event()
        adapter._active_sessions[sk] = guard

        async def _fail():
            raise RuntimeError("relay scope corruption")

        task = asyncio.create_task(_fail())
        adapter._session_tasks[sk] = task
        adapter._background_tasks.add(task)
        task.add_done_callback(adapter._background_tasks.discard)
        task.add_done_callback(adapter._make_session_lock_heal_callback(sk, task))

        with pytest.raises(RuntimeError, match="relay scope corruption"):
            await task

        # Lock must be healed even though the task errored.
        assert sk not in adapter._active_sessions, (
            "session lock should be healed even when task raises an error"
        )
        assert sk not in adapter._session_tasks

    @pytest.mark.asyncio
    async def test_heal_callback_noop_when_task_replaced(self):
        """If a newer task has taken over the session, the old task's callback
        is a no-op — prevents cross-contamination."""
        adapter = _make_adapter()
        sk = _session_key()

        guard_a = asyncio.Event()
        adapter._active_sessions[sk] = guard_a

        async def _ok_a():
            return None

        async def _ok_b():
            await asyncio.sleep(10)  # long-running "newer" task
            return None

        task_a = asyncio.create_task(_ok_a())
        task_b = asyncio.create_task(_ok_b())

        # Wait for task_a to complete.
        await task_a

        # Simulate: task_b replaced task_a as the session owner.
        adapter._session_tasks[sk] = task_b
        adapter._active_sessions[sk] = guard_a

        task_a.add_done_callback(adapter._make_session_lock_heal_callback(sk, task_a))

        # task_a's callback should NOT heal because task_b owns the session now.
        assert sk in adapter._active_sessions, (
            "old callback should not heal when a newer task owns the session"
        )
        assert adapter._session_tasks[sk] is task_b

        # Clean up task_b.
        task_b.cancel()
        try:
            await task_b
        except asyncio.CancelledError:
            pass

    @pytest.mark.asyncio
    async def test_heal_callback_clears_pending_messages(self):
        """When the done_callback heals, it also clears pending messages for
        that session (matching _heal_stale_session_lock behavior)."""
        adapter = _make_adapter()
        sk = _session_key()

        guard = asyncio.Event()
        adapter._active_sessions[sk] = guard
        fake_event = asyncio.Event()
        adapter._pending_messages[sk] = fake_event

        async def _ok():
            return None

        task = asyncio.create_task(_ok())
        adapter._session_tasks[sk] = task
        adapter._background_tasks.add(task)
        task.add_done_callback(adapter._background_tasks.discard)
        task.add_done_callback(adapter._make_session_lock_heal_callback(sk, task))

        await task

        assert sk not in adapter._pending_messages, (
            "pending messages should be cleared on heal"
        )

    @pytest.mark.asyncio
    async def test_heal_callback_noop_when_no_task_entry(self):
        """If _session_tasks has no entry for the key, the callback is a no-op
        (the _heal_stale_session_lock already handles this)."""
        adapter = _make_adapter()
        sk = _session_key()

        guard = asyncio.Event()
        adapter._active_sessions[sk] = guard
        # No _session_tasks entry.

        async def _ok():
            return None

        task = asyncio.create_task(_ok())
        task.add_done_callback(adapter._make_session_lock_heal_callback(sk, task))

        await task

        # Should NOT raise; _heal_stale_session_lock returns False when no task entry.
        assert sk in adapter._active_sessions
