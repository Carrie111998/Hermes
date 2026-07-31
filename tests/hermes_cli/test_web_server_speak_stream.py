"""/api/audio/speak-stream — desktop streaming TTS over WebSocket."""

from __future__ import annotations

import json
import time
from urllib.parse import urlencode

import pytest
from starlette.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

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

    def __init__(self, chunks):
        self.chunks = chunks
        self.requests: list[str] = []

    def stream(self, text):
        self.requests.append(text)
        yield from self.chunks


def _patch_provider(
    monkeypatch,
    streamer,
    cap=4000,
    config=None,
    provider_name=None,
    resolved_providers=None,
):
    if provider_name is not None:
        streamer.provider_name = provider_name
    monkeypatch.setattr("tools.tts_streaming.resolve_streaming_provider", lambda cfg: streamer)
    monkeypatch.setattr("tools.tts_tool._load_tts_config", lambda: config or {})
    monkeypatch.setattr("tools.tts_tool._get_provider", lambda cfg: "fake")

    def _resolve_cap(provider, _cfg):
        if resolved_providers is not None:
            resolved_providers.append(provider)
        return cap

    monkeypatch.setattr("tools.tts_tool._resolve_max_text_length", _resolve_cap)






def test_streams_pcm_frames_then_end(stream_client, monkeypatch):
    streamer = _FakeStreamer([b"\x01\x02\x03\x04", b"\x05\x06"])
    _patch_provider(monkeypatch, streamer)

    with stream_client.websocket_connect(_url()) as conn:
        start = conn.receive_json()
        assert start == {"type": "start", "sample_rate": 24000, "channels": 1}

        conn.send_text(json.dumps({"text": "Hello there.", "done": True}))
        assert conn.receive_bytes() == b"\x01\x02\x03\x04"
        assert conn.receive_bytes() == b"\x05\x06"
        assert conn.receive_json() == {"type": "end"}

    assert streamer.requests == ["Hello there."]


def test_pinned_streamer_uses_its_provider_cap(stream_client, monkeypatch):
    streamer = _FakeStreamer([b"\x00\x00"])
    resolved_providers = []
    _patch_provider(
        monkeypatch,
        streamer,
        cap=32,
        config={"provider": "edge", "streaming": {"provider": "openai"}},
        provider_name="openai",
        resolved_providers=resolved_providers,
    )

    with stream_client.websocket_connect(_url()) as conn:
        assert conn.receive_json()["type"] == "start"
        conn.send_text(json.dumps({"text": ("x" * 80) + ".", "done": True}))
        while True:
            message = conn.receive()
            if message.get("bytes") is None:
                assert json.loads(message["text"]) == {"type": "end"}
                break

    assert resolved_providers == ["openai"]
    assert streamer.requests
    assert all(len(request) <= 32 for request in streamer.requests)


def test_pronunciation_precedes_chunking_and_protected_text_stays_silent(
    stream_client,
    monkeypatch,
):
    streamer = _FakeStreamer([b"\x00\x00"])
    protected = (
        "```text\n"
        "⚠️ File-mutation verifier: literal documentation sample. private. code\n"
        "```"
    )
    _patch_provider(
        monkeypatch,
        streamer,
        config={
            "pronunciation": {
                "substitutions": {
                    "Dr. Ipek": "Doctor Ipek",
                    protected: "LEAK",
                },
            },
        },
    )

    with stream_client.websocket_connect(_url()) as conn:
        assert conn.receive_json()["type"] == "start"
        conn.send_text(json.dumps({"text": protected + "Please call Dr. "}))
        # The punctuation after "Dr." must not trigger the 0.5s idle flush
        # while a configured source phrase is still incomplete.
        time.sleep(0.7)
        conn.send_text(json.dumps({"text": "ıpek tomorrow.", "done": True}))
        assert conn.receive_bytes() == b"\x00\x00"
        assert conn.receive_json() == {"type": "end"}

    assert streamer.requests == ["Please call Doctor Ipek tomorrow."]


def test_desktop_idle_waits_for_pronunciation_right_word_boundary(
    stream_client,
    monkeypatch,
):
    streamer = _FakeStreamer([b"\x00\x00"])
    _patch_provider(
        monkeypatch,
        streamer,
        config={
            "pronunciation": {
                "substitutions": {"Dr. Ipek": "Doctor Ipek"},
            },
        },
    )
    prefix = ("Visible ordinary words " * 6) + "Dr. Ipek"

    with stream_client.websocket_connect(_url()) as conn:
        assert conn.receive_json()["type"] == "start"
        conn.send_text(json.dumps({"text": prefix}))
        time.sleep(2.2)
        conn.send_text(json.dumps({"text": "son arrives.", "done": True}))
        while True:
            message = conn.receive()
            if message.get("bytes") is None:
                assert json.loads(message["text"]) == {"type": "end"}
                break

    spoken = " ".join(streamer.requests)
    assert "Doctor Ipek" not in spoken
    assert "Dr. Ipekson arrives." in spoken


def test_desktop_idle_preserves_pronunciation_left_word_boundary(
    stream_client,
    monkeypatch,
):
    streamer = _FakeStreamer([b"\x00\x00"])
    _patch_provider(
        monkeypatch,
        streamer,
        config={
            "pronunciation": {
                "substitutions": {"Ipek": "EE-peck"},
            },
        },
    )
    prefix = ("Visible ordinary words " * 6) + "my"

    with stream_client.websocket_connect(_url()) as conn:
        assert conn.receive_json()["type"] == "start"
        conn.send_text(json.dumps({"text": prefix}))
        time.sleep(2.2)
        conn.send_text(json.dumps({"text": "Ipek arrives.", "done": True}))
        while True:
            message = conn.receive()
            if message.get("bytes") is None:
                assert json.loads(message["text"]) == {"type": "end"}
                break

    spoken = " ".join(streamer.requests)
    assert "EE-peck" not in spoken
    assert "myIpek arrives." in spoken


def test_desktop_idle_preserves_punctuation_source_right_word_boundary(
    stream_client,
    monkeypatch,
):
    streamer = _FakeStreamer([b"\x00\x00"])
    _patch_provider(
        monkeypatch,
        streamer,
        config={
            "pronunciation": {
                "substitutions": {"C++": "C plus plus"},
            },
        },
    )
    prefix = ("Visible ordinary words " * 6) + "C++s"

    with stream_client.websocket_connect(_url()) as conn:
        assert conn.receive_json()["type"] == "start"
        conn.send_text(json.dumps({"text": prefix}))
        time.sleep(2.2)
        conn.send_text(json.dumps({"text": "on concludes.", "done": True}))
        while True:
            message = conn.receive()
            if message.get("bytes") is None:
                assert json.loads(message["text"]) == {"type": "end"}
                break

    spoken = " ".join(streamer.requests)
    assert "C plus plus" not in spoken
    assert "C++son concludes." in spoken


@pytest.mark.parametrize(
    ("partial", "continuation", "forbidden"),
    [
        ("Fıle-muta", "tion verifier: SECRET verifier.", "verifier"),
        ("<thı", "nk>SECRET reasoning.</thınk>Final visible answer.", "<th"),
    ],
)
def test_unicode_partial_protected_opener_survives_desktop_idle_interval(
    stream_client,
    monkeypatch,
    partial,
    continuation,
    forbidden,
):
    streamer = _FakeStreamer([b"\x00\x00"])
    _patch_provider(monkeypatch, streamer)
    prefix = ("Visible ordinary words " * 6) + partial

    with stream_client.websocket_connect(_url()) as conn:
        assert conn.receive_json()["type"] == "start"
        conn.send_text(json.dumps({"text": prefix}))
        # A non-protected long buffer would be force-flushed after ~2 seconds.
        time.sleep(2.2)
        conn.send_text(json.dumps({"text": continuation, "done": True}))
        assert conn.receive_bytes() == b"\x00\x00"
        assert conn.receive_json() == {"type": "end"}

    assert streamer.requests
    assert all("SECRET" not in request for request in streamer.requests)
    assert all(forbidden not in request.casefold() for request in streamer.requests)


def test_long_text_is_split_across_provider_requests(stream_client, monkeypatch):
    streamer = _FakeStreamer([b"\x00\x00"])
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
                frames += 1
            else:
                assert json.loads(message["text"]) == {"type": "end"}
                break

    assert len(streamer.requests) > 1
    assert frames == len(streamer.requests)
    # Nothing lost in the split: every sentence reached the provider.
    joined = " ".join(streamer.requests)
    for fragment in ("First sentence here.", "Second sentence here.", "Third one."):
        assert fragment in joined


def test_desktop_cap_uses_actual_elevenlabs_streaming_model(
    stream_client,
    monkeypatch,
):
    from tools import tts_streaming, tts_tool

    streamer = _FakeStreamer([b"\x00\x00"])
    streamer.provider_name = "elevenlabs"
    streamer.model_id = "eleven_v3"
    config = {
        "provider": "edge",
        "streaming": {"provider": "elevenlabs"},
        "elevenlabs": {
            "model_id": "eleven_flash_v2_5",
            "streaming_model_id": "eleven_v3",
        },
    }
    resolved = []
    original_resolve_cap = tts_tool._resolve_max_text_length

    def _resolve_cap(provider, cfg, *, model_id=None):
        resolved.append((provider, model_id))
        return original_resolve_cap(provider, cfg, model_id=model_id)

    monkeypatch.setattr(
        tts_streaming,
        "resolve_streaming_provider",
        lambda *_args, **_kwargs: streamer,
    )
    monkeypatch.setattr(tts_tool, "_load_tts_config", lambda: config)
    monkeypatch.setattr(tts_tool, "_get_provider", lambda _cfg: "edge")
    monkeypatch.setattr(tts_tool, "_resolve_max_text_length", _resolve_cap)

    with stream_client.websocket_connect(_url()) as conn:
        assert conn.receive_json()["type"] == "start"
        conn.send_text(json.dumps({"text": ("x" * 6000) + ".", "done": True}))
        while True:
            message = conn.receive()
            if message.get("bytes") is None:
                assert json.loads(message["text"]) == {"type": "end"}
                break

    assert resolved == [("elevenlabs", "eleven_v3")]
    request_lengths = [len(request) for request in streamer.requests]
    assert request_lengths == [5000, 1001]
    assert max(request_lengths) <= 5000


def test_split_text_respects_cap_and_preserves_content():
    text = "Alpha beta. Gamma delta epsilon. Zeta eta theta iota kappa."
    pieces = web_server._split_text_for_speak_stream(text, 30)
    assert pieces
    assert all(len(piece) <= 30 for piece in pieces)
    joined = " ".join(pieces)
    for word in text.replace(".", "").split():
        assert word in joined


