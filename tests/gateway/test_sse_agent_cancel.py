"""Tests for SSE client disconnect → agent task cancellation.

When a streaming /v1/chat/completions client disconnects mid-stream
(network drop, browser tab close), the agent is interrupted via
agent.interrupt() so it stops making LLM API calls, and the asyncio
task wrapper is cancelled.
"""

import asyncio
import queue
import threading
from unittest.mock import AsyncMock, MagicMock, patch

import pytest


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_adapter():
    """Build a minimal APIServerAdapter with mocked internals."""
    from gateway.platforms.api_server import APIServerAdapter
    from gateway.config import PlatformConfig

    config = PlatformConfig(enabled=True, token="test-key")
    adapter = APIServerAdapter(config)
    return adapter


def _make_request():
    """Build a mock aiohttp request."""
    req = MagicMock()
    req.headers = {}
    return req


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

class TestSSEAgentCancelOnDisconnect:
    """gateway/platforms/api_server.py — _write_sse_chat_completion()"""

    def test_agent_task_cancelled_on_client_disconnect(self):
        """When response.write raises ConnectionResetError (client dropped),
        the agent task must be cancelled."""
        adapter = _make_adapter()

        stream_q = queue.Queue()
        stream_q.put("hello ")  # Some data already queued

        # Agent task that runs forever (simulates a long LLM call)
        agent_done = asyncio.Event()

        async def fake_agent():
            await agent_done.wait()
            return {"final_response": "done"}, {"input_tokens": 10, "output_tokens": 5, "total_tokens": 15}

        async def run():
            from aiohttp import web

            agent_task = asyncio.ensure_future(fake_agent())

            # Mock response that raises ConnectionResetError on second write
            mock_response = AsyncMock(spec=web.StreamResponse)
            call_count = 0

            async def write_side_effect(data):
                nonlocal call_count
                call_count += 1
                if call_count >= 2:
                    raise ConnectionResetError("client disconnected")

            mock_response.write = AsyncMock(side_effect=write_side_effect)
            mock_response.prepare = AsyncMock()

            with patch.object(type(adapter), '_write_sse_chat_completion',
                              adapter._write_sse_chat_completion):
                # Patch StreamResponse creation
                with patch("gateway.platforms.api_server.web.StreamResponse",
                           return_value=mock_response):
                    await adapter._write_sse_chat_completion(
                        _make_request(), "cmpl-123", "gpt-4", 1234567890,
                        stream_q, agent_task,
                    )

            # The critical assertion: agent_task must be cancelled
            assert agent_task.cancelled() or agent_task.done()
            # Clean up
            agent_done.set()

        asyncio.run(run())


    def test_broken_pipe_also_cancels_agent(self):
        """BrokenPipeError (another disconnect variant) also cancels the task."""
        adapter = _make_adapter()

        stream_q = queue.Queue()

        async def fake_agent():
            await asyncio.sleep(0.2)  # Never completes
            return {}, {}

        async def run():
            from aiohttp import web

            agent_task = asyncio.ensure_future(fake_agent())

            mock_response = AsyncMock(spec=web.StreamResponse)
            mock_response.write = AsyncMock(side_effect=BrokenPipeError("pipe broken"))
            mock_response.prepare = AsyncMock()

            with patch("gateway.platforms.api_server.web.StreamResponse",
                       return_value=mock_response):
                await adapter._write_sse_chat_completion(
                    _make_request(), "cmpl-789", "gpt-4", 1234567890,
                    stream_q, agent_task,
                )

            assert agent_task.cancelled() or agent_task.done()

        asyncio.run(run())

    def test_already_done_task_not_cancelled_on_disconnect(self):
        """If agent already finished before disconnect, don't try to cancel."""
        adapter = _make_adapter()

        stream_q = queue.Queue()
        stream_q.put("data")

        async def fake_agent():
            return {"final_response": "done"}, {}

        async def run():
            from aiohttp import web

            agent_task = asyncio.ensure_future(fake_agent())
            await asyncio.sleep(0)  # Let agent complete

            mock_response = AsyncMock(spec=web.StreamResponse)
            call_count = 0

            async def write_side_effect(data):
                nonlocal call_count
                call_count += 1
                if call_count >= 2:
                    raise ConnectionResetError("late disconnect")

            mock_response.write = AsyncMock(side_effect=write_side_effect)
            mock_response.prepare = AsyncMock()

            with patch("gateway.platforms.api_server.web.StreamResponse",
                       return_value=mock_response):
                await adapter._write_sse_chat_completion(
                    _make_request(), "cmpl-done", "gpt-4", 1234567890,
                    stream_q, agent_task,
                )

            # Task was already done — should not be cancelled
            assert agent_task.done()
            assert not agent_task.cancelled()

        asyncio.run(run())

    def test_agent_interrupt_called_on_disconnect(self):
        """When the client disconnects, agent.interrupt() must be called
        so the agent thread stops making LLM API calls."""
        adapter = _make_adapter()

        stream_q = queue.Queue()
        stream_q.put("hello ")

        agent_done = asyncio.Event()

        async def fake_agent():
            await agent_done.wait()
            return {"final_response": "done"}, {}

        # Mock agent with an interrupt method
        mock_agent = MagicMock()
        mock_agent.interrupt = MagicMock()

        async def run():
            from aiohttp import web

            agent_task = asyncio.ensure_future(fake_agent())
            agent_ref = [mock_agent]

            mock_response = AsyncMock(spec=web.StreamResponse)
            call_count = 0

            async def write_side_effect(data):
                nonlocal call_count
                call_count += 1
                if call_count >= 2:
                    raise ConnectionResetError("client disconnected")

            mock_response.write = AsyncMock(side_effect=write_side_effect)
            mock_response.prepare = AsyncMock()

            with patch("gateway.platforms.api_server.web.StreamResponse",
                       return_value=mock_response):
                await adapter._write_sse_chat_completion(
                    _make_request(), "cmpl-int", "gpt-4", 1234567890,
                    stream_q, agent_task, agent_ref,
                )

            # agent.interrupt() must have been called
            mock_agent.interrupt.assert_called_once_with("SSE client disconnected")
            # Clean up
            agent_done.set()

        asyncio.run(run())

    def test_cancel_signal_cannot_erase_process_ownership_before_reap(self, monkeypatch):
        """The cancellation signal may synchronously release the worker and
        clear ownership before the hard interrupt.  Reaping must use the
        snapshot captured before either signal is published.
        """
        from gateway.platforms.api_server import (
            _clear_turn_process_ownership,
            _publish_turn_process_ownership,
        )
        from tools.process_registry import process_registry

        adapter = _make_adapter()
        stream_q = queue.Queue()
        stream_q.put("hello")
        calls = []
        monkeypatch.setattr(
            process_registry,
            "snapshot_running_ids",
            lambda _tid: frozenset({"preexisting-process"}),
        )
        monkeypatch.setattr(
            process_registry,
            "kill_started_since",
            lambda task_id, baseline, *, source: calls.append(
                (task_id, baseline, source)
            )
            or 1,
        )

        async def run():
            from aiohttp import web

            agent_done = asyncio.Event()

            async def fake_agent():
                await agent_done.wait()
                return {"final_response": "done"}, {}

            mock_agent = MagicMock()
            mock_agent.interrupt = MagicMock()
            _publish_turn_process_ownership(mock_agent, "sse-snapshot")

            class ClearingSignal:
                def set(self):
                    _clear_turn_process_ownership(mock_agent)
                    agent_done.set()

            agent_task = asyncio.ensure_future(fake_agent())
            mock_response = AsyncMock(spec=web.StreamResponse)
            call_count = 0

            async def write_side_effect(_data):
                nonlocal call_count
                call_count += 1
                if call_count >= 2:
                    raise ConnectionResetError("client disconnected")

            mock_response.write = AsyncMock(side_effect=write_side_effect)
            mock_response.prepare = AsyncMock()

            with patch(
                "gateway.platforms.api_server.web.StreamResponse",
                return_value=mock_response,
            ):
                await adapter._write_sse_chat_completion(
                    _make_request(),
                    "cmpl-snapshot",
                    "gpt-4",
                    1234567890,
                    stream_q,
                    agent_task,
                    [mock_agent, ClearingSignal()],
                )

            for _ in range(100):
                if calls:
                    break
                await asyncio.sleep(0.01)
            assert calls == [
                (
                    "sse-snapshot",
                    frozenset({"preexisting-process"}),
                    "api_server_sse_disconnect",
                )
            ]
            mock_agent.interrupt.assert_called_once_with("SSE client disconnected")

        asyncio.run(run())

    def test_agent_ref_none_still_cancels_task(self):
        """When agent_ref is not provided (None), the task is still cancelled
        on disconnect — just without the interrupt() call."""
        adapter = _make_adapter()

        stream_q = queue.Queue()

        async def fake_agent():
            await asyncio.sleep(999)
            return {}, {}

        async def run():
            from aiohttp import web

            agent_task = asyncio.ensure_future(fake_agent())

            mock_response = AsyncMock(spec=web.StreamResponse)
            mock_response.write = AsyncMock(side_effect=BrokenPipeError("gone"))
            mock_response.prepare = AsyncMock()

            with patch("gateway.platforms.api_server.web.StreamResponse",
                       return_value=mock_response):
                # No agent_ref passed — should still handle disconnect cleanly
                await adapter._write_sse_chat_completion(
                    _make_request(), "cmpl-noref", "gpt-4", 1234567890,
                    stream_q, agent_task,
                )

            assert agent_task.cancelled() or agent_task.done()

        asyncio.run(run())

    def test_prepare_failure_interrupts_and_joins_agent(self):
        adapter = _make_adapter()
        stream_q = queue.Queue()
        agent_done = asyncio.Event()
        mock_agent = MagicMock()
        cancel_signal = threading.Event()

        async def fake_agent():
            await agent_done.wait()
            return {
                "completed": False,
                "failed": True,
                "receipt_terminal_success": False,
            }, {}

        async def run():
            from aiohttp import web

            mock_agent.interrupt.side_effect = lambda _reason: agent_done.set()
            agent_task = asyncio.create_task(fake_agent())
            response = AsyncMock(spec=web.StreamResponse)
            response.prepare = AsyncMock(
                side_effect=ConnectionResetError("prepare disconnected")
            )
            with patch(
                "gateway.platforms.api_server.web.StreamResponse",
                return_value=response,
            ):
                await adapter._write_sse_chat_completion(
                    _make_request(),
                    "cmpl-prepare",
                    "gpt-4",
                    1234567890,
                    stream_q,
                    agent_task,
                    [mock_agent, cancel_signal],
                )

            assert cancel_signal.is_set()
            mock_agent.interrupt.assert_called_once_with("SSE client disconnected")
            assert agent_task.done()
            assert not agent_task.cancelled()

        asyncio.run(run())

    def test_cancellation_while_waiting_for_delta_interrupts_and_joins_agent(self):
        adapter = _make_adapter()
        stream_q = queue.Queue()
        agent_done = asyncio.Event()
        first_write = asyncio.Event()
        mock_agent = MagicMock()
        cancel_signal = threading.Event()

        async def fake_agent():
            await agent_done.wait()
            return {
                "completed": False,
                "failed": True,
                "receipt_terminal_success": False,
            }, {}

        async def run():
            from aiohttp import web

            mock_agent.interrupt.side_effect = lambda _reason: agent_done.set()
            agent_task = asyncio.create_task(fake_agent())
            response = AsyncMock(spec=web.StreamResponse)
            response.prepare = AsyncMock()

            async def write(_data):
                first_write.set()

            response.write = AsyncMock(side_effect=write)
            with patch(
                "gateway.platforms.api_server.web.StreamResponse",
                return_value=response,
            ):
                writer_task = asyncio.create_task(
                    adapter._write_sse_chat_completion(
                        _make_request(),
                        "cmpl-wait",
                        "gpt-4",
                        1234567890,
                        stream_q,
                        agent_task,
                        [mock_agent, cancel_signal],
                    )
                )
                await first_write.wait()
                writer_task.cancel()
                with pytest.raises(asyncio.CancelledError):
                    await writer_task

            assert cancel_signal.is_set()
            mock_agent.interrupt.assert_called_once_with("SSE client disconnected")
            assert agent_task.done()
            assert not agent_task.cancelled()

        asyncio.run(run())

    def test_non_transport_writer_failure_interrupts_agent_and_never_reports_success(self):
        adapter = _make_adapter()
        stream_q = queue.Queue()
        stream_q.put("delta")
        agent_done = asyncio.Event()
        mock_agent = MagicMock()
        cancel_signal = threading.Event()
        chunks = []

        async def fake_agent():
            await agent_done.wait()
            return {
                "completed": False,
                "failed": True,
                "receipt_terminal_success": False,
            }, {}

        async def run():
            from aiohttp import web

            mock_agent.interrupt.side_effect = lambda _reason: agent_done.set()
            agent_task = asyncio.create_task(fake_agent())
            response = AsyncMock(spec=web.StreamResponse)
            response.prepare = AsyncMock()
            call_count = 0

            async def write(data):
                nonlocal call_count
                call_count += 1
                if call_count == 2:
                    raise RuntimeError("writer codec failed")
                chunks.append(data.decode())

            response.write = AsyncMock(side_effect=write)
            with patch(
                "gateway.platforms.api_server.web.StreamResponse",
                return_value=response,
            ):
                await adapter._write_sse_chat_completion(
                    _make_request(),
                    "cmpl-writer",
                    "gpt-4",
                    1234567890,
                    stream_q,
                    agent_task,
                    [mock_agent, cancel_signal],
                )

            assert cancel_signal.is_set()
            mock_agent.interrupt.assert_called_once_with("SSE client disconnected")
            assert agent_task.done()
            rendered = "".join(chunks)
            assert '"receipt_terminal_success": false' in rendered
            assert '"finish_reason": "stop"' not in rendered

        asyncio.run(run())


def _capturing_response():
    """Mock StreamResponse that records all written SSE bytes as text."""
    from aiohttp import web

    chunks: list = []
    resp = AsyncMock(spec=web.StreamResponse)
    resp.prepare = AsyncMock()

    async def _write(data):
        chunks.append(data.decode() if isinstance(data, (bytes, bytearray)) else data)

    resp.write = AsyncMock(side_effect=_write)
    return resp, chunks


def _finish_reason(chunks: list):
    """Extract the terminal finish_reason and its chunk from captured SSE."""
    import json

    sse = "".join(chunks)
    finish = None
    for line in sse.splitlines():
        if line.startswith("data: ") and '"finish_reason"' in line:
            obj = json.loads(line[6:])
            if obj["choices"][0].get("finish_reason") is not None:
                finish = obj
    return (finish["choices"][0]["finish_reason"] if finish else None), finish, sse


class TestSSEAgentFailureFinishReason:
    """gateway/platforms/api_server.py — _write_sse_chat_completion()

    A clean stream-queue termination (sentinel received) followed by an agent
    failure must NOT report finish_reason: "stop". Both failure modes — an
    ``agent_task`` that raises and a ``result`` dict flagged failed — surface
    as finish_reason: "error", mirroring the non-streaming path. Issue #12422.
    """

    def _run(self, fake_agent, queue_items=("partial",)):
        adapter = _make_adapter()
        stream_q = queue.Queue()
        for item in queue_items:
            stream_q.put(item)
        stream_q.put(None)  # clean end-of-stream sentinel

        async def run():
            agent_task = asyncio.ensure_future(fake_agent())
            resp, chunks = _capturing_response()
            with patch("gateway.platforms.api_server.web.StreamResponse",
                       return_value=resp):
                await adapter._write_sse_chat_completion(
                    _make_request(), "cmpl-fail", "gpt-4", 1234567890,
                    stream_q, agent_task,
                )
            return _finish_reason(chunks)

        return asyncio.run(run())

    def test_agent_task_raises_reports_error_not_stop(self):
        async def crash():
            raise RuntimeError("boom from agent")

        reason, finish, sse = self._run(crash)
        assert reason == "error"
        assert "error" in finish
        assert "data: [DONE]" in sse

    def test_failed_result_dict_reports_error_not_stop(self):
        async def failed():
            return (
                {"final_response": "", "failed": True, "completed": False,
                 "error": "upstream model 500"},
                {"input_tokens": 5, "output_tokens": 0, "total_tokens": 5},
            )

        reason, finish, _ = self._run(failed)
        assert reason == "error"
        assert finish.get("hermes", {}).get("failed") is True

    def test_truncated_result_reports_length(self):
        async def trunc():
            return (
                {"final_response": "half", "partial": True, "completed": False,
                 "error": "output was truncated"},
                {"input_tokens": 5, "output_tokens": 3, "total_tokens": 8},
            )

        reason, finish, _ = self._run(trunc)
        assert reason == "length"
        assert finish["hermes"]["error_code"] == "output_truncated"

    def test_successful_completion_reports_stop(self):
        async def ok():
            return (
                {
                    "final_response": "hi",
                    "completed": True,
                    "receipt_terminal_success": True,
                    "failed": False,
                    "partial": False,
                    "interrupted": False,
                    "cleanup_errors": [],
                    "session_id": "child-session",
                },
                {"input_tokens": 5, "output_tokens": 2, "total_tokens": 7},
            )

        reason, finish, _ = self._run(ok)
        assert reason == "stop"
        assert "error" not in finish
        assert finish["hermes"]["receipt_terminal_success"] is True
        assert finish["hermes"]["session_id"] == "child-session"

    @pytest.mark.parametrize(
        "result",
        [
            {"completed": True, "receipt_terminal_success": False},
            {
                "completed": True,
                "receipt_terminal_success": False,
                "cleanup_errors": ["private cleanup detail"],
            },
            {
                "completed": False,
                "receipt_terminal_success": False,
                "interrupted": True,
            },
            {
                "completed": False,
                "receipt_terminal_success": False,
                "partial": True,
            },
        ],
    )
    def test_noncanonical_terminal_result_never_reports_stop(self, result):
        async def noncanonical():
            return (
                {"final_response": "partial", "session_id": "parent", **result},
                {"input_tokens": 5, "output_tokens": 1, "total_tokens": 6},
            )

        reason, finish, _ = self._run(noncanonical)
        assert reason in {"error", "length"}
        assert finish["hermes"]["receipt_terminal_success"] is False
