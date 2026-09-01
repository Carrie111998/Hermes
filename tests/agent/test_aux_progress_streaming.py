"""Tests for the auxiliary forward-progress streaming layer.

Slow summary models must not be punished like hung ones (#see PR): when a
forward-progress hook is installed (context compression), the primary
auxiliary call streams and ticks the hook only for substantive payloads, so
outer watchdogs (gateway session hygiene) can extend their deadline on
liveness. Without a hook, behavior stays non-streaming unless the provider
explicitly requires the streamed path.
"""

import threading
import time
from http.server import BaseHTTPRequestHandler, HTTPServer
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from agent.auxiliary_client import (
    _AnthropicCompletionsAdapter,
    _ChatStreamAccumulator,
    _CodexCompletionsAdapter,
    _acreate_with_stream,
    _aggregate_chat_stream,
    _aggregate_chat_stream_async,
    _anthropic_event_has_content,
    _aux_stream_total_ceiling,
    _codex_event_has_content,
    _copy_request_local_streaming_client,
    _create_with_progress,
    _notify_aux_progress,
    _provider_requires_stream,
    aux_progress_hook,
)
from agent.conversation_compression import CompressionCommitFence


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _chunk(content=None, reasoning=None, reasoning_details=None,
           finish_reason=None, usage=None, tool_calls=None, model="m1",
           chunk_id="c1"):
    delta = SimpleNamespace(
        content=content,
        reasoning=reasoning,
        reasoning_content=None,
        reasoning_details=reasoning_details,
        tool_calls=tool_calls,
    )
    choice = SimpleNamespace(delta=delta, finish_reason=finish_reason)
    return SimpleNamespace(
        id=chunk_id, model=model, choices=[choice], usage=usage,
    )


class _FakeClient:
    """OpenAI-shaped client whose create() returns a canned value or stream."""

    def __init__(self, response=None, stream_chunks=None, stream_error=None):
        self.calls = []
        self._response = response
        self._stream_chunks = stream_chunks
        self._stream_error = stream_error
        completions = SimpleNamespace(create=self._create)
        self.chat = SimpleNamespace(completions=completions)

    def _create(self, **kwargs):
        self.calls.append(kwargs)
        if kwargs.get("stream"):
            if self._stream_error is not None:
                raise self._stream_error
            return iter(self._stream_chunks or [])
        return self._response


_COMPLETE = SimpleNamespace(
    id="r1", model="m1", object="chat.completion",
    choices=[SimpleNamespace(
        index=0,
        message=SimpleNamespace(role="assistant", content="non-streamed"),
        finish_reason="stop",
    )],
    usage=None,
)


# ---------------------------------------------------------------------------
# aux_progress_hook plumbing
# ---------------------------------------------------------------------------

class TestAuxProgressHook:
    def test_hook_installed_and_restored(self):
        ticks = []
        with aux_progress_hook(lambda: ticks.append(1)):
            _notify_aux_progress()
        _notify_aux_progress()  # outside — must not tick
        assert ticks == [1]



    def test_hook_is_thread_local(self):
        ticks = []
        seen_in_thread = []

        def _other_thread():
            # No hook installed on this thread.
            _notify_aux_progress()
            seen_in_thread.append(len(ticks))

        with aux_progress_hook(lambda: ticks.append(1)):
            t = threading.Thread(target=_other_thread)
            t.start()
            t.join()
        assert seen_in_thread == [0]


# ---------------------------------------------------------------------------
# _create_with_progress
# ---------------------------------------------------------------------------

class TestCreateWithProgress:

    def test_hook_upgrades_to_streaming_and_ticks_only_for_payload(self):
        empty_tool_call = SimpleNamespace(
            index=0,
            id=None,
            function=SimpleNamespace(name=None, arguments=""),
        )
        chunks = [
            SimpleNamespace(id=None, model=None, choices=[], usage=None),
            _chunk(content="", reasoning="", tool_calls=[empty_tool_call]),
            _chunk(reasoning="thinking..."),
            _chunk(content="Hello "),
            _chunk(content="world", finish_reason="stop",
                   usage=SimpleNamespace(prompt_tokens=5, completion_tokens=2,
                                         total_tokens=7)),
        ]
        client = _FakeClient(stream_chunks=chunks)
        ticks = []
        with aux_progress_hook(lambda: ticks.append(1)):
            result = _create_with_progress(
                client, {"model": "m1", "messages": [], "timeout": 30},
            )
        assert client.calls[0]["stream"] is True
        assert result.choices[0].message.content == "Hello world"
        assert result.choices[0].message.reasoning == "thinking..."
        assert result.choices[0].finish_reason == "stop"
        assert result.usage.total_tokens == 7
        # 1 dispatch tick (preserved for the watchdog's historical liveness
        # signal — see _create_with_progress) + 1 per substantive chunk.
        assert ticks == [1, 1, 1, 1]

    def test_completed_response_ticks_only_terminal_signals(self):
        calls = []

        def _create(**kwargs):
            calls.append(kwargs)
            return _COMPLETE

        client = SimpleNamespace(
            chat=SimpleNamespace(completions=SimpleNamespace(create=_create))
        )
        ticks = []

        with aux_progress_hook(lambda: ticks.append(1)):
            result = _create_with_progress(client, {"model": "m1", "messages": []})

        assert calls[0]["stream"] is True
        assert result is _COMPLETE
        # A completed response object carries the full summary payload, and
        # the dispatch tick is the watchdog's historical liveness signal:
        # both are one-shot terminal ticks, not per-frame keepalives, so
        # neither can defeat an inactivity timeout.
        assert ticks == [1, 1]

    def test_streaming_rejected_falls_back_to_plain_call(self):
        client = _FakeClient(
            response=_COMPLETE,
            stream_error=RuntimeError("stream is not supported by this model"),
        )
        with aux_progress_hook(lambda: None):
            result = _create_with_progress(
                client, {"model": "m1", "messages": []},
            )
        assert result is _COMPLETE
        # streamed attempt + non-streaming fallback
        assert len(client.calls) == 2
        assert client.calls[0].get("stream") is True
        assert "stream" not in client.calls[1]




# ---------------------------------------------------------------------------
# _aggregate_chat_stream
# ---------------------------------------------------------------------------

class TestAggregateChatStream:
    def test_request_local_copy_overrides_the_cached_http_pool(self, monkeypatch):
        shared_pool = object()
        request_pool = object()
        local_client = object()
        source = SimpleNamespace(
            _client=shared_pool,
            base_url="https://proxy.example/v1",
            copy=MagicMock(return_value=local_client),
        )
        monkeypatch.setattr(
            "agent.auxiliary_client._openai_http_client_kwargs",
            lambda base_url: {"http_client": request_pool},
        )

        copied, owned = _copy_request_local_streaming_client(source)

        assert copied is local_client
        assert owned is True
        source.copy.assert_called_once_with(http_client=request_pool)

    def test_tool_call_deltas_are_reassembled(self):
        tc0 = SimpleNamespace(
            index=0, id="call_1",
            function=SimpleNamespace(name="do_thing", arguments='{"a"'),
        )
        tc1 = SimpleNamespace(
            index=0, id=None,
            function=SimpleNamespace(name=None, arguments=': 1}'),
        )
        chunks = [
            _chunk(tool_calls=[tc0]),
            _chunk(tool_calls=[tc1], finish_reason="tool_calls"),
        ]
        result = _aggregate_chat_stream(iter(chunks))
        tool_calls = result.choices[0].message.tool_calls
        assert len(tool_calls) == 1
        assert tool_calls[0].id == "call_1"
        assert tool_calls[0].function.name == "do_thing"
        assert tool_calls[0].function.arguments == '{"a": 1}'
        assert result.choices[0].finish_reason == "tool_calls"


    def test_stream_close_is_called(self):
        closed = []

        class _Stream:
            def __iter__(self):
                return iter([_chunk(content="ok", finish_reason="stop")])

            def close(self):
                closed.append(True)

        result = _aggregate_chat_stream(_Stream())
        assert result.choices[0].message.content == "ok"
        assert closed == [True]

    def test_no_progress_watchdog_aborts_request_client_and_times_out(self, monkeypatch):
        release = threading.Event()
        aborted = []

        class _BlockedStream:
            def __iter__(self):
                release.wait(timeout=1.0)
                return iter(())

            def close(self):
                pass

        request_client = object()

        def _abort(client):
            aborted.append(client)
            release.set()
            return 1

        monkeypatch.setattr(
            "agent.auxiliary_client._AUX_STREAM_NO_PROGRESS_TIMEOUT_SECONDS", 0.02
        )
        monkeypatch.setattr(
            "agent.agent_runtime_helpers.force_close_tcp_sockets", _abort
        )

        started = time.monotonic()
        with pytest.raises(TimeoutError, match="no-progress timeout"):
            _aggregate_chat_stream(
                _BlockedStream(), request_client=request_client, total_ceiling=1.0
            )

        assert time.monotonic() - started < 0.5
        assert aborted == [request_client]

    @pytest.mark.windows_only
    def test_no_progress_watchdog_interrupts_real_openai_socket(self, monkeypatch):
        """The watchdog must wake the actual Windows SDK receive, not a mock."""
        peer_release = threading.Event()

        class _StalledSSE(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def do_POST(self):
                length = int(self.headers.get("Content-Length", "0"))
                self.rfile.read(length)
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
                self.send_header("Connection", "close")
                self.end_headers()
                self.wfile.flush()
                peer_release.wait(timeout=2.0)

            def log_message(self, format, *args):
                pass

        server = HTTPServer(("127.0.0.1", 0), _StalledSSE)
        server_thread = threading.Thread(target=server.serve_forever, daemon=True)
        server_thread.start()
        monkeypatch.setattr(
            "agent.auxiliary_client._AUX_STREAM_NO_PROGRESS_TIMEOUT_SECONDS", 0.1
        )

        from openai import OpenAI

        client = OpenAI(
            api_key="test",
            base_url=f"http://127.0.0.1:{server.server_address[1]}/v1",
            max_retries=0,
        )
        result = []
        started = time.monotonic()

        def _request():
            try:
                with aux_progress_hook(lambda: None):
                    _create_with_progress(
                        client,
                        {"model": "m1", "messages": [], "timeout": 5.0},
                    )
            except Exception as exc:
                result.append(exc)

        worker = threading.Thread(target=_request)
        worker.start()
        worker.join(timeout=1.0)
        elapsed = time.monotonic() - started
        peer_release.set()
        worker.join(timeout=3.0)
        client.close()
        server.shutdown()
        server.server_close()
        server_thread.join(timeout=1.0)

        assert not worker.is_alive(), "stalled SDK receive ignored the watchdog"
        assert elapsed < 1.0
        assert len(result) == 1
        assert isinstance(result[0], TimeoutError)
        assert "no-progress timeout" in str(result[0])

    def test_substantive_chunks_rearm_no_progress_watchdog(self, monkeypatch):
        monkeypatch.setattr(
            "agent.auxiliary_client._AUX_STREAM_NO_PROGRESS_TIMEOUT_SECONDS", 0.04
        )

        class _SlowHealthyStream:
            def __iter__(self):
                for text in ("a", "b", "c"):
                    time.sleep(0.025)
                    yield _chunk(content=text)

            def close(self):
                pass

        result = _aggregate_chat_stream(
            _SlowHealthyStream(), request_client=object(), total_ceiling=1.0
        )
        assert result.choices[0].message.content == "abc"

    def test_force_stream_without_hook_rearms_request_local_watchdog(self, monkeypatch):
        monkeypatch.setattr(
            "agent.auxiliary_client._AUX_STREAM_NO_PROGRESS_TIMEOUT_SECONDS", 0.04
        )

        class _SlowLocalClient(_FakeClient):
            def _create(self, **kwargs):
                self.calls.append(kwargs)

                def _chunks():
                    for text in ("a", "b", "c"):
                        time.sleep(0.025)
                        yield _chunk(content=text)

                return _chunks()

        cached_client = _FakeClient(stream_chunks=[])
        local_client = _SlowLocalClient()
        local_client.close = lambda: None
        monkeypatch.setattr(
            "agent.auxiliary_client._copy_request_local_streaming_client",
            lambda client: (local_client, True),
        )

        result = _create_with_progress(
            cached_client,
            {"model": "m1", "messages": [], "timeout": 1.0},
            force_stream=True,
        )

        assert cached_client.calls == []
        assert local_client.calls[0]["stream"] is True
        assert result.choices[0].message.content == "abc"

    def test_request_local_worker_discards_chunks_after_caller_timeout(self, monkeypatch):
        from agent.auxiliary_client import _aux_provider_response, _aux_timing_hook

        peer_release = threading.Event()
        worker_closed = threading.Event()
        caller_returned = threading.Event()
        late_callbacks = []
        close_threads = []

        class _LateLocalClient(_FakeClient):
            def _create(self, **kwargs):
                self.calls.append(kwargs)

                def _chunks():
                    peer_release.wait(timeout=1.0)
                    yield _chunk(content="stale")

                return _chunks()

            def close(self):
                close_threads.append(threading.get_ident())
                worker_closed.set()

        cached_client = _FakeClient(stream_chunks=[])
        local_client = _LateLocalClient()
        monkeypatch.setattr(
            "agent.auxiliary_client._AUX_STREAM_NO_PROGRESS_TIMEOUT_SECONDS", 0.02
        )
        monkeypatch.setattr(
            "agent.auxiliary_client._copy_request_local_streaming_client",
            lambda client: (local_client, True),
        )
        monkeypatch.setattr(
            "agent.agent_runtime_helpers.force_close_tcp_sockets", lambda client: 0
        )

        def _record_callback(kind):
            if caller_returned.is_set():
                late_callbacks.append(kind)

        caller_thread = threading.get_ident()
        with (
            aux_progress_hook(lambda: _record_callback("progress")),
            _aux_timing_hook(
                _aux_provider_response, lambda: _record_callback("timing")
            ),
            pytest.raises(TimeoutError, match="no-progress timeout"),
        ):
            _create_with_progress(
                cached_client, {"model": "m1", "messages": [], "timeout": 1.0}
            )

        caller_returned.set()
        peer_release.set()
        assert worker_closed.wait(timeout=1.0)
        assert late_callbacks == []
        assert close_threads and close_threads[0] != caller_thread

    def test_stream_attempt_uses_and_closes_request_local_client(self, monkeypatch):
        cached_client = _FakeClient(stream_chunks=[])
        local_client = _FakeClient(
            stream_chunks=[_chunk(content="isolated", finish_reason="stop")]
        )
        closed = []
        local_client.close = lambda: closed.append(True)
        monkeypatch.setattr(
            "agent.auxiliary_client._copy_request_local_streaming_client",
            lambda client: (local_client, True),
        )

        with aux_progress_hook(lambda: None):
            result = _create_with_progress(
                cached_client, {"model": "m1", "messages": [], "timeout": 30}
            )

        assert cached_client.calls == []
        assert local_client.calls[0]["stream"] is True
        assert result.choices[0].message.content == "isolated"
        assert closed == [True]



# ---------------------------------------------------------------------------
# Content-bearing progress classification
# ---------------------------------------------------------------------------


class TestContentBearingProgress:
    @pytest.mark.parametrize(
        "event",
        [
            SimpleNamespace(type="response.created"),
            SimpleNamespace(type="response.output_item.added", item={"type": "message"}),
            SimpleNamespace(type="response.content_part.added", part={"type": "output_text"}),
            SimpleNamespace(type="response.output_text.delta", delta=""),
            SimpleNamespace(type="response.reasoning_summary_text.delta", delta=""),
            SimpleNamespace(type="response.function_call_arguments.delta", delta=""),
            SimpleNamespace(
                type="response.output_item.added",
                item=SimpleNamespace(type="function_call"),
            ),
            {"type": "response.output_text.delta", "delta": ""},
        ],
    )
    def test_codex_empty_and_structural_events_are_not_progress(self, event):
        assert _codex_event_has_content(event) is False

    @pytest.mark.parametrize(
        "event",
        [
            SimpleNamespace(type="response.output_text.delta", delta="token"),
            SimpleNamespace(type="response.reasoning_summary_text.delta", delta="thought"),
            SimpleNamespace(type="response.function_call_arguments.delta", delta='{"x"'),
            SimpleNamespace(
                type="response.output_item.added",
                item=SimpleNamespace(
                    type="function_call",
                    id="item_1",
                    call_id="call_1",
                    name="lookup",
                ),
            ),
            {"type": "response.output_text.delta", "delta": "token"},
        ],
    )
    def test_codex_nonempty_deltas_are_progress(self, event):
        assert _codex_event_has_content(event) is True

    @pytest.mark.parametrize(
        ("delta", "expected"),
        [
            (SimpleNamespace(type="text_delta", text="", thinking=None), False),
            (SimpleNamespace(type="text_delta", text="token", thinking=None), True),
            (SimpleNamespace(type="thinking_delta", text=None, thinking="thought"), True),
            (SimpleNamespace(type="input_json_delta", partial_json=""), False),
            (SimpleNamespace(type="input_json_delta", partial_json='{"x"'), True),
            # Signed-thinking / citation payloads the transport emits
            # (relay_llm.py signature_delta + citations_delta).
            (SimpleNamespace(type="signature_delta", signature="sig-1"), True),
            (SimpleNamespace(type="signature_delta", signature=""), False),
            (SimpleNamespace(type="citations_delta", citation={"cited": 1}), True),
        ],
    )
    def test_anthropic_requires_nonempty_delta_payload(self, delta, expected):
        event = SimpleNamespace(type="content_block_delta", delta=delta)
        assert _anthropic_event_has_content(event) is expected

    @pytest.mark.parametrize(
        ("block", "expected"),
        [
            (SimpleNamespace(type="text"), False),
            (SimpleNamespace(type="tool_use", id=None, name=None), False),
            (SimpleNamespace(type="tool_use", id="toolu_1", name="lookup"), True),
        ],
    )
    def test_anthropic_tool_start_requires_identity(self, block, expected):
        event = SimpleNamespace(type="content_block_start", content_block=block)
        assert _anthropic_event_has_content(event) is expected

    def test_chat_stream_empty_tool_scaffolding_is_not_progress(self):
        ticks = []
        empty_tool_call = SimpleNamespace(
            index=0,
            id=None,
            function=SimpleNamespace(name=None, arguments=""),
        )
        accumulator = _ChatStreamAccumulator()

        with aux_progress_hook(lambda: ticks.append(1)):
            accumulator.feed(_chunk(tool_calls=[empty_tool_call]))

        assert ticks == []

    @pytest.mark.parametrize(
        "tool_call",
        [
            SimpleNamespace(index=0, id="call_1", function=None),
            SimpleNamespace(
                index=0,
                id=None,
                function=SimpleNamespace(name="lookup", arguments=""),
            ),
            SimpleNamespace(
                index=0,
                id=None,
                function=SimpleNamespace(name=None, arguments='{"q"'),
            ),
        ],
    )
    def test_chat_stream_substantive_tool_fragments_are_progress(self, tool_call):
        ticks = []
        accumulator = _ChatStreamAccumulator()

        with aux_progress_hook(lambda: ticks.append(1)):
            accumulator.feed(_chunk(tool_calls=[tool_call]))

        assert ticks == [1]

    def test_openrouter_reasoning_details_keep_compression_alive(self):
        """Reasoning-only streams must refresh the compression idle fence."""
        ticks = []
        accumulator = _ChatStreamAccumulator()
        detail = {"type": "reasoning.summary", "summary": "working..."}

        with aux_progress_hook(lambda: ticks.append(1)):
            accumulator.feed(_chunk(reasoning_details=[detail]))

        result = accumulator.finish()
        assert ticks == [1]
        assert result.choices[0].message.reasoning_details == [detail]

    def test_structural_reasoning_details_are_not_progress(self):
        ticks = []
        accumulator = _ChatStreamAccumulator()

        with aux_progress_hook(lambda: ticks.append(1)):
            accumulator.feed(
                _chunk(
                    reasoning_details=[
                        {"type": "reasoning.encrypted", "signature": "sig"}
                    ]
                )
            )

        assert ticks == []

    def test_codex_adapter_updates_fence_only_for_substantive_events(self):
        events = [
            SimpleNamespace(type="response.created"),
            SimpleNamespace(type="response.output_text.delta", delta=""),
            SimpleNamespace(type="response.output_text.delta", delta="token"),
            SimpleNamespace(
                type="response.output_item.added",
                item=SimpleNamespace(
                    type="function_call", id="item_1", call_id="call_1", name="lookup"
                ),
            ),
        ]
        real_client = SimpleNamespace(
            base_url="https://chatgpt.com/backend-api/codex",
            responses=SimpleNamespace(create=lambda **_kwargs: iter(events)),
        )
        adapter = _CodexCompletionsAdapter(real_client, "gpt-5.6-sol")
        fence = CompressionCommitFence()
        touches = []

        def _touch():
            touches.append(1)
            fence.touch_progress()

        def _consume(stream, *, model, on_event):
            del model
            for event in stream:
                on_event(event)
            return SimpleNamespace(output=[], usage=None)

        with (
            patch("agent.codex_runtime._consume_codex_event_stream", _consume),
            aux_progress_hook(_touch),
        ):
            adapter.create(messages=[{"role": "user", "content": "summarize"}])

        assert touches == [1, 1]

    def test_anthropic_adapter_updates_fence_only_for_substantive_events(self):
        events = [
            SimpleNamespace(type="ping"),
            SimpleNamespace(
                type="content_block_delta",
                delta=SimpleNamespace(type="text_delta", text=""),
            ),
            SimpleNamespace(
                type="content_block_delta",
                delta=SimpleNamespace(type="input_json_delta", partial_json='{"q"'),
            ),
            SimpleNamespace(
                type="content_block_start",
                content_block=SimpleNamespace(
                    type="tool_use", id="toolu_1", name="lookup"
                ),
            ),
        ]
        adapter = _AnthropicCompletionsAdapter(
            MagicMock(), "claude-sonnet-4-6", is_oauth=False
        )
        fence = CompressionCommitFence()
        touches = []

        def _touch():
            touches.append(1)
            fence.touch_progress()

        def _create_message(*_args, **kwargs):
            for event in events:
                kwargs["on_stream_event"](event)
            raise RuntimeError("stop after callback verification")

        with (
            patch(
                "agent.anthropic_adapter.build_anthropic_kwargs",
                return_value={"model": "claude-sonnet-4-6", "messages": []},
            ),
            patch(
                "agent.anthropic_adapter.create_anthropic_message",
                side_effect=_create_message,
            ),
            aux_progress_hook(_touch),
            pytest.raises(RuntimeError, match="stop after callback verification"),
        ):
            adapter.create(messages=[{"role": "user", "content": "summarize"}])

        assert touches == [1, 1]

    def test_keepalive_chunks_do_not_reset_the_compression_fence(self):
        """End-to-end bug pin (#96707): content-free frames must not refresh
        CompressionCommitFence._last_progress.

        The waiter in conversation_compression charges its idle budget from
        seconds_since_progress(); before the fix, every keepalive chunk fed
        through _ChatStreamAccumulator ticked the fence, so a stalled
        summary stream never hit the inactivity timeout."""
        fence = CompressionCommitFence()
        accumulator = _ChatStreamAccumulator()
        keepalive = SimpleNamespace(id=None, model=None, choices=[], usage=None)
        empty_role_chunk = _chunk(content="", reasoning="")

        with aux_progress_hook(fence.touch_progress):
            for _ in range(5):
                accumulator.feed(keepalive)
                accumulator.feed(empty_role_chunk)
        # No substantive payload arrived: the fence must not report progress.
        assert fence.progress_observed is False

        with aux_progress_hook(fence.touch_progress):
            accumulator.feed(_chunk(content="token"))
        assert fence.seconds_since_progress() < 0.05

    def test_content_free_frames_still_record_ttfp_timing(self):
        """The fast-lane telemetry contract (#96945/#96963) survives the
        gating: time_to_first_progress_ms must record on the FIRST frame of
        any kind (transport liveness), not only on the first token."""
        from agent.auxiliary_client import (
            _aux_provider_response,
            _aux_timing_hook,
            _notify_aux_timing_response,
        )

        timings: dict = {}

        def _timed_response() -> None:
            timings.setdefault("time_to_first_progress_ms", 42)

        keepalive = SimpleNamespace(id=None, model=None, choices=[], usage=None)
        accumulator = _ChatStreamAccumulator()

        with (
            _aux_timing_hook(_aux_provider_response, _timed_response),
            aux_progress_hook(lambda: None),
        ):
            accumulator.feed(keepalive)

        assert timings["time_to_first_progress_ms"] == 42


# ---------------------------------------------------------------------------
# Ceiling arithmetic
# ---------------------------------------------------------------------------

class TestStreamCeiling:
    def test_floor_applies_to_small_timeouts(self):
        assert _aux_stream_total_ceiling(30) == 600.0


    def test_none_timeout_gets_floor(self):
        assert _aux_stream_total_ceiling(None) == 600.0


# ---------------------------------------------------------------------------
# CompressionCommitFence progress surface
# ---------------------------------------------------------------------------

class TestFenceProgress:
    def test_touch_progress_resets_idle_clock(self):
        fence = CompressionCommitFence()
        time.sleep(0.05)
        assert fence.seconds_since_progress() >= 0.04
        fence.touch_progress()
        assert fence.seconds_since_progress() < 0.05

    def test_fence_hook_wiring_matches_compressor_usage(self):
        # conversation_compression installs fence.touch_progress as the hook;
        # verify the pair works end-to-end through _notify_aux_progress.
        fence = CompressionCommitFence()
        time.sleep(0.05)
        with aux_progress_hook(fence.touch_progress):
            _notify_aux_progress()
        assert fence.seconds_since_progress() < 0.05


# ---------------------------------------------------------------------------
# Stream-only providers (credit @kudi88, PR #60686)
# ---------------------------------------------------------------------------

class TestProviderRequiresStream:

    def test_normal_endpoints_are_not(self):
        assert _provider_requires_stream(
            "openrouter", "https://openrouter.ai/api/v1"
        ) is False
        assert _provider_requires_stream("auto", None) is False
        assert _provider_requires_stream("auto", "") is False

    def test_config_marker_matches_custom_endpoint(self):
        with patch(
            "hermes_cli.config.load_config",
            return_value={"auxiliary": {"stream_only_base_urls": ["my-proxy.example.com"]}},
        ):
            assert _provider_requires_stream(
                "custom", "https://my-proxy.example.com/v1"
            ) is True
            assert _provider_requires_stream(
                "custom", "https://other.example.com/v1"
            ) is False



class TestForceStream:
    def test_force_stream_streams_without_a_hook(self):
        chunks = [_chunk(content="hi", finish_reason="stop")]
        client = _FakeClient(stream_chunks=chunks)
        # NO aux_progress_hook installed — force_stream alone must stream.
        result = _create_with_progress(
            client, {"model": "m1", "messages": []}, force_stream=True,
        )
        assert client.calls[0]["stream"] is True
        assert result.choices[0].message.content == "hi"

    def test_force_stream_does_not_retry_nonstreaming_on_failure(self):
        client = _FakeClient(
            response=_COMPLETE,
            stream_error=RuntimeError("HTTP 400 bad request"),
        )
        with pytest.raises(RuntimeError, match="bad request"):
            _create_with_progress(
                client, {"model": "m1", "messages": []}, force_stream=True,
            )
        # No silent non-streaming retry — the provider rejects those anyway.
        assert len(client.calls) == 1


class TestAsyncStreamAggregation:
    @pytest.mark.asyncio
    async def test_async_stream_is_consumed_with_async_for(self):
        # The sweeper review of PR #60686 flagged that awaiting create() and
        # then iterating synchronously raises — the async contract is
        # ``async for``. Verify the async aggregator consumes a real async
        # iterator and preserves tool-call deltas.
        tc0 = SimpleNamespace(
            index=0, id="call_9",
            function=SimpleNamespace(name="lookup", arguments='{"q":'),
        )
        tc1 = SimpleNamespace(
            index=0, id=None,
            function=SimpleNamespace(name=None, arguments='"x"}'),
        )
        raw_chunks = [
            _chunk(content="part1 "),
            _chunk(tool_calls=[tc0]),
            _chunk(tool_calls=[tc1], content="part2", finish_reason="tool_calls"),
        ]

        class _AsyncStream:
            def __init__(self, items):
                self._items = list(items)
                self.closed = False

            def __aiter__(self):
                return self

            async def __anext__(self):
                if not self._items:
                    raise StopAsyncIteration
                return self._items.pop(0)

            async def close(self):
                self.closed = True

        stream = _AsyncStream(raw_chunks)
        result = await _aggregate_chat_stream_async(stream)
        msg = result.choices[0].message
        assert msg.content == "part1 part2"
        assert msg.tool_calls[0].function.name == "lookup"
        assert msg.tool_calls[0].function.arguments == '{"q":"x"}'
        assert result.choices[0].finish_reason == "tool_calls"
        assert stream.closed is True

    @pytest.mark.asyncio
    async def test_acreate_with_stream_passes_stream_kwargs(self):
        calls = []

        class _AsyncStream:
            def __init__(self, items):
                self._items = list(items)

            def __aiter__(self):
                return self

            async def __anext__(self):
                if not self._items:
                    raise StopAsyncIteration
                return self._items.pop(0)

        class _AsyncClient:
            def __init__(self):
                completions = SimpleNamespace(create=self._create)
                self.chat = SimpleNamespace(completions=completions)

            async def _create(self, **kwargs):
                calls.append(kwargs)
                return _AsyncStream([_chunk(content="ok", finish_reason="stop")])

        result = await _acreate_with_stream(
            _AsyncClient(), {"model": "m1", "messages": [], "timeout": 30},
        )
        assert calls[0]["stream"] is True
        assert result.choices[0].message.content == "ok"
