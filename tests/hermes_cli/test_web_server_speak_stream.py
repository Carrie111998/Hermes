"""/api/audio/speak-stream — desktop streaming TTS over WebSocket."""

from __future__ import annotations

import asyncio
import json
import threading
import time
from urllib.parse import urlencode

import pytest
from starlette.testclient import TestClient

from hermes_cli import web_server


@pytest.fixture
def stream_client(monkeypatch, _isolate_hermes_home):
    previous_auth_required = getattr(web_server.app.state, "auth_required", None)
    web_server.app.state.auth_required = False

    client = TestClient(web_server.app)
    try:
        yield client
    finally:
        close = getattr(client, "close", None)
        if close is not None:
            close()
        if previous_auth_required is None:
            if hasattr(web_server.app.state, "auth_required"):
                delattr(web_server.app.state, "auth_required")
        else:
            web_server.app.state.auth_required = previous_auth_required


def _url(token: str | None = None) -> str:
    return f"/api/audio/speak-stream?{urlencode({'token': token or web_server._SESSION_TOKEN})}"


class _FakeStreamer:
    sample_rate = 24000
    channels = 1
    upstream_cancellable = True

    def __init__(self, chunks):
        self.chunks = chunks
        self.requests: list[str] = []

    def stream(self, text):
        self.requests.append(text)
        yield from self.chunks


class _FailingStreamer(_FakeStreamer):
    def stream(self, text):
        self.requests.append(text)
        raise RuntimeError("provider unavailable")
        yield b""  # pragma: no cover


def _patch_provider(monkeypatch, streamer, cap=4000):
    monkeypatch.setattr("tools.tts_streaming.resolve_streaming_provider", lambda cfg: streamer)
    monkeypatch.setattr("tools.tts_tool._load_tts_config", lambda: {})
    monkeypatch.setattr("tools.tts_tool._get_provider", lambda cfg: "fake")
    monkeypatch.setattr("tools.tts_tool._resolve_max_text_length", lambda provider, cfg: cap)


def test_cancellation_signal_does_not_compete_with_a_full_output_queue():
    chunks: asyncio.Queue[tuple[str, object]] = asyncio.Queue(maxsize=1)
    frame_item = ("frame", object())
    chunks.put_nowait(frame_item)
    cancelled = asyncio.Event()
    cancelled.set()

    assert asyncio.run(web_server._next_speak_stream_item(chunks, cancelled)) is None
    # Cancellation itself never attempts a racy put_nowait sentinel or raises
    # QueueFull, even if a producer has already filled the output queue.
    assert chunks.qsize() == 1
    assert chunks.get_nowait() == frame_item






def test_streams_pcm_frames_then_end(stream_client, monkeypatch):
    streamer = _FakeStreamer([b"\x01\x02" * 480, b"\x05\x06" * 480])
    _patch_provider(monkeypatch, streamer)

    with stream_client.websocket_connect(_url()) as conn:
        start = conn.receive_json()
        assert start["type"] == "start"
        assert start["protocol"] == "hermes.audio.v1"
        assert start["encoding"] == "pcm_s16le"
        assert start["sample_rate"] == 24000
        assert start["channels"] == 1
        assert start["stream_id"] > 0
        assert start["initial_buffer_ms"] < start["max_buffer_ms"]
        assert start["upstream_cancellable"] is True
        assert start["capabilities"]["upstream_cancellable"] is True

        conn.send_text(json.dumps({"text": "Hello there.", "done": True}))
        metadata = conn.receive_json()
        assert metadata["type"] == "audio"
        assert metadata["seq"] == metadata["sequence"] == 0
        assert metadata["start_sample"] == metadata["sample_offset"] == 0
        assert metadata["sample_count"] == 480
        assert conn.receive_bytes() == b"\x01\x02" * 480
        metadata = conn.receive_json()
        assert metadata["seq"] == metadata["sequence"] == 1
        assert metadata["start_sample"] == metadata["sample_offset"] == 480
        assert metadata["sample_count"] == 480
        assert conn.receive_bytes() == b"\x05\x06" * 480
        end = conn.receive_json()
        assert end["type"] == "end"
        assert end["frames"] == end["frame_count"] == 2
        assert end["samples"] == end["sample_count"] == 960

    assert streamer.requests == ["Hello there."]


def test_non_cancellable_provider_falls_back_before_v1_start(stream_client, monkeypatch):
    class _LegacyStreamer(_FakeStreamer):
        upstream_cancellable = False

    streamer = _LegacyStreamer([b"\x00\x00" * 480])
    _patch_provider(monkeypatch, streamer)

    with stream_client.websocket_connect(_url()) as conn:
        assert conn.receive_json() == {"type": "fallback"}
        assert conn.receive()["type"] == "websocket.close"

    assert streamer.requests == []








def test_long_text_is_split_across_provider_requests(stream_client, monkeypatch):
    streamer = _FakeStreamer([b"\x00\x00" * 480])
    _patch_provider(monkeypatch, streamer, cap=24)

    with stream_client.websocket_connect(_url()) as conn:
        assert conn.receive_json()["type"] == "start"
        conn.send_text(
            json.dumps(
                {"text": "First sentence here. Second sentence here. Third one.", "done": True}
            )
        )
        # One PCM frame per split piece, then end.
        frames = 0
        while True:
            message = conn.receive()
            if message.get("bytes") is not None:
                pytest.fail("binary payload was not preceded by metadata")
            assert message.get("text") is not None, f"unexpected message: {message}"
            frame = json.loads(message["text"])
            assert frame["type"] != "error", frame
            if frame["type"] == "end":
                break
            assert frame["type"] == "audio"
            assert conn.receive_bytes()
            frames += 1

    assert len(streamer.requests) > 1
    assert frames == len(streamer.requests)
    # Nothing lost in the split: every sentence reached the provider.
    joined = " ".join(streamer.requests)
    for fragment in ("First sentence here.", "Second sentence here.", "Third one."):
        assert fragment in joined


def test_synthesis_failure_is_structured_and_terminal(stream_client, monkeypatch):
    streamer = _FailingStreamer([])
    _patch_provider(monkeypatch, streamer)

    with stream_client.websocket_connect(_url()) as conn:
        assert conn.receive_json()["type"] == "start"
        conn.send_text(json.dumps({"text": "This will fail.", "done": True}))
        error = conn.receive_json()
        assert error["type"] == "error"
        assert error["code"] == "synthesis_failed"
        assert "provider unavailable" in error["message"]


def test_type_stop_frame_cancels_without_end(stream_client, monkeypatch):
    release = __import__("threading").Event()

    class _BlockingStreamer(_FakeStreamer):
        def stream(self, text):
            self.requests.append(text)
            yield b"\x00\x00" * 480
            release.wait(timeout=1)

    streamer = _BlockingStreamer([])
    _patch_provider(monkeypatch, streamer)

    with stream_client.websocket_connect(_url()) as conn:
        assert conn.receive_json()["type"] == "start"
        conn.send_text(json.dumps({"text": "Please stop this sentence."}))
        assert conn.receive_json()["type"] == "audio"
        assert conn.receive_bytes()
        conn.send_text(json.dumps({"type": "stop"}))
        assert conn.receive()["type"] == "websocket.close"

    release.set()


def test_server_send_failure_cancels_blocked_provider(stream_client, monkeypatch):
    release = threading.Event()
    cancel_called = threading.Event()

    class _BlockingCancellableStreamer(_FakeStreamer):
        def stream(self, text):
            self.requests.append(text)
            yield b"\x00\x00" * 480
            release.wait(timeout=2)

        def cancel(self):
            cancel_called.set()
            release.set()

    streamer = _BlockingCancellableStreamer([])
    _patch_provider(monkeypatch, streamer)
    original_send_json = web_server.WebSocket.send_json

    async def _fail_audio_send(ws, data, *args, **kwargs):
        if data.get("type") == "audio":
            raise RuntimeError("simulated client send failure")
        return await original_send_json(ws, data, *args, **kwargs)

    monkeypatch.setattr(web_server.WebSocket, "send_json", _fail_audio_send)

    with stream_client.websocket_connect(_url()) as conn:
        assert conn.receive_json()["type"] == "start"
        conn.send_text(json.dumps({"text": "Cancel when the writer fails."}))
        assert conn.receive()["type"] == "websocket.close"

    assert cancel_called.wait(timeout=1)
    assert release.is_set()


def test_split_text_respects_cap_and_preserves_content():
    text = "Alpha beta. Gamma delta epsilon. Zeta eta theta iota kappa."
    pieces = web_server._split_text_for_speak_stream(text, 30)
    assert pieces
    assert all(len(piece) <= 30 for piece in pieces)
    joined = " ".join(pieces)
    for word in text.replace(".", "").split():
        assert word in joined
