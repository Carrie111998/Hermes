"""Leak-safety of the gateway hook-event bridge (``TurnRunner._event_callback_sync``).

``_event_callback_sync`` is the sync -> async bridge that carries lifecycle hook
events (notably ``session:compress``, emitted from
``agent/conversation_compression.py`` and ``agent/codex_runtime.py``) from the
agent thread onto the gateway's turn loop.  It is wired unconditionally in
``_run_agent_inner`` -- unlike ``step_callback``, which is only attached when
hooks are loaded -- so every gateway turn goes through it.

Bridging a coroutine onto a loop that is closing or gone is the shutdown race
that ``agent.async_utils.safe_schedule_threadsafe`` exists to absorb: a bare
``asyncio.run_coroutine_threadsafe`` raises before the loop ever takes
ownership, leaving the ``emit(...)`` coroutine created-but-never-awaited, which
both drops the hook silently and leaks the coroutine frame with a
``RuntimeWarning: coroutine ... was never awaited``.

These tests pin the invariant on the loop-closed branch: the coroutine must end
up *closed*, not merely have its exception swallowed -- swallowing was never
the missing half.
"""

from __future__ import annotations

import asyncio
import inspect

from gateway.turn_context import TurnContext


class _RecordingHooks:
    """Stands in for the gateway's hook registry.

    ``emit`` is a real coroutine function, so calling it constructs a coroutine
    object exactly as the production ``hooks.emit`` does.  The object is kept so
    a test can inspect whether the bridge disposed of it.
    """

    def __init__(self) -> None:
        self.coros: list = []
        self.emitted: list[tuple[str, dict]] = []

    async def emit(self, event_type: str, context: dict) -> None:
        self.emitted.append((event_type, context))


def _make_runner(ctx: TurnContext):
    from gateway.run import TurnRunner

    class _StubGatewayRunner:
        def _adapter_for_source(self, source):
            return None

    return TurnRunner(_StubGatewayRunner(), ctx)


def _bridge_one_event(loop) -> tuple[object, _RecordingHooks]:
    """Drive one ``session:compress`` event through the bridge onto ``loop``.

    Returns the ``emit()`` coroutine object the bridge was handed, so the caller
    can assert what happened to it.
    """
    hooks = _RecordingHooks()
    real_emit = hooks.emit

    def _capturing_emit(event_type: str, context: dict):
        coro = real_emit(event_type, context)
        hooks.coros.append(coro)
        return coro

    hooks.emit = _capturing_emit  # type: ignore[method-assign]

    ctx = TurnContext(_hooks_ref=hooks, _loop_for_step=loop)
    runner = _make_runner(ctx)
    runner._event_callback_sync("session:compress", {"session_id": "s-1"})

    assert len(hooks.coros) == 1
    return hooks.coros[0], hooks


class TestEventCallbackBridgeLeakSafety:
    def test_closed_step_loop_closes_the_event_coroutine(self, recwarn):
        """A loop closed by shutdown must not strand the emit coroutine.

        This is the live race: the compression path fires ``session:compress``
        from the agent thread while the gateway is tearing its loop down.
        ``run_coroutine_threadsafe`` raises inside ``call_soon_threadsafe``
        before the loop adopts the coroutine, so unless the bridge closes it,
        the hook is dropped *and* the frame leaks.
        """
        loop = asyncio.new_event_loop()
        loop.close()

        coro, hooks = _bridge_one_event(loop)

        assert inspect.getcoroutinestate(coro) == inspect.CORO_CLOSED
        # The coroutine never ran, so the hook body did not execute -- the
        # point is that it was disposed of, not that it was delivered.
        assert hooks.emitted == []
        assert not [w for w in recwarn.list if "never awaited" in str(w.message)]
