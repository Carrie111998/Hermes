import asyncio
import contextvars
import unittest
from types import SimpleNamespace

from agent.relay_runtime import RelayRuntime, RelaySession


class _Subscribers:
    def __init__(self):
        self.sync_calls = 0
        self.async_calls = 0

    def flush(self):
        self.sync_calls += 1
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            return
        raise RuntimeError(
            "subscribers.flush() cannot block a running asyncio event loop; "
            "use 'await subscribers.flush_async()'"
        )

    async def flush_async(self):
        self.async_calls += 1


class RelayRuntimeCloseTest(unittest.TestCase):
    def test_close_session_uses_async_flush_inside_running_loop(self):
        subscribers = _Subscribers()
        relay = SimpleNamespace(
            subscribers=subscribers,
            scope=SimpleNamespace(pop=lambda *_args, **_kwargs: None),
        )
        runtime = RelayRuntime(relay=relay, profile_key="test")
        session = RelaySession(
            session_id="s1",
            handle=None,
            context=contextvars.Context(),
        )
        runtime._sessions["s1"] = session

        try:
            asyncio.run(self._close(runtime))
        finally:
            runtime.shutdown()

        self.assertEqual(subscribers.sync_calls, 0)
        self.assertEqual(subscribers.async_calls, 1)
        self.assertNotIn("s1", runtime._sessions)

    @staticmethod
    async def _close(runtime):
        runtime.close_session({"session_id": "s1"})


if __name__ == "__main__":
    unittest.main()
