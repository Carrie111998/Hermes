from __future__ import annotations

import asyncio
from collections import deque

import numpy as np
import pytest

from agent.transports.codex_realtime_voice import (
    AiortcRealtimePeer,
    CodexRealtimeCapabilities,
    CodexRealtimeSession,
    CodexRealtimeStaleSpeech,
    discord_pcm_to_realtime,
    safe_realtime_error,
)


class FakeClient:
    def __init__(self, notifications: list[dict] | None = None, **kwargs) -> None:
        self.kwargs = kwargs
        self.notifications = deque(notifications or [])
        self.requests: list[tuple[str, dict]] = []
        self.initialized: dict | None = None
        self.closed = False

    def initialize(self, **kwargs):
        self.initialized = kwargs
        return {"userAgent": "codex-test"}

    def request(self, method: str, params: dict, timeout: float = 30.0):
        self.requests.append((method, params))
        if method == "thread/start":
            return {"thread": {"id": "thread-1"}}
        if method == "thread/realtime/listVoices":
            return {"voices": {"v1": ["cedar", "marin"], "v2": ["ash"]}}
        return {}

    def take_notification(self, timeout: float = 0.0):
        if self.notifications:
            return self.notifications.popleft()
        return None

    def close(self):
        self.closed = True


class FakePeer:
    def __init__(self) -> None:
        self.answer_sdp: str | None = None
        self.input_pcm: list[bytes] = []
        self.closed = False
        self.on_pcm = None
        self.on_failure = None

    async def create_offer(self, on_pcm, on_failure=None):
        self.on_pcm = on_pcm
        self.on_failure = on_failure
        return "v=offer\r\n"

    async def accept_answer(self, sdp: str):
        self.answer_sdp = sdp

    def push_input(self, pcm: bytes):
        self.input_pcm.append(pcm)
        return True

    async def close(self):
        self.closed = True


@pytest.mark.asyncio
async def test_start_uses_experimental_v3_webrtc_contract_and_lists_capabilities():
    client = FakeClient([
        {
            "method": "thread/realtime/started",
            "params": {"threadId": "thread-1", "version": "v3"},
        },
        {
            "method": "thread/realtime/sdp",
            "params": {"threadId": "thread-1", "sdp": "v=answer\r\n"},
        },
    ])
    peer = FakePeer()
    session = CodexRealtimeSession(
        cwd="/tmp",
        client_factory=lambda **kwargs: client,
        peer_factory=lambda: peer,
        binary_checker=lambda *_args: (True, "0.145.0"),
    )

    capabilities = await session.start(voice="cedar")

    assert capabilities == CodexRealtimeCapabilities(
        protocol_version="v3",
        voices=("cedar", "marin"),
        language_selection=False,
        reasoning_effort=False,
    )
    assert client.initialized is not None
    assert client.initialized["capabilities"] == {
        "experimentalApi": True,
        "optOutNotificationMethods": ["thread/realtime/outputAudio/delta"],
    }
    assert client.kwargs == {}
    assert client.requests[0] == (
        "thread/start",
        {"cwd": "/tmp", "ephemeral": True},
    )
    assert client.requests[1][0] == "thread/realtime/listVoices"
    assert client.requests[2] == (
        "thread/realtime/start",
        {
            "threadId": "thread-1",
            "clientManagedHandoffs": True,
            "includeStartupContext": False,
            "outputModality": "audio",
            "prompt": (
                "You are a low-latency speech interface for another assistant. "
                "Transcribe the user's speech accurately in the language they actually "
                "speak; preserve that language and never translate it. Do not answer "
                "the user, do not call tools, and do not delegate work. Stay silent "
                "until the client supplies text to speak."
            ),
            "transport": {"type": "webrtc", "sdp": "v=offer\r\n"},
            "version": "v3",
            "voice": "cedar",
        },
    )
    assert peer.answer_sdp == "v=answer\r\n"
    assert session.active is True

    await session.stop()


@pytest.mark.asyncio
async def test_spoken_language_guides_output_without_translating_input():
    client = FakeClient([
        {
            "method": "thread/realtime/started",
            "params": {"threadId": "thread-1", "version": "v3"},
        },
        {
            "method": "thread/realtime/sdp",
            "params": {"threadId": "thread-1", "sdp": "v=answer\r\n"},
        },
    ])
    session = CodexRealtimeSession(
        cwd="/tmp",
        spoken_language="nl-NL",
        client_factory=lambda **kwargs: client,
        peer_factory=FakePeer,
        binary_checker=lambda *_args: (True, "0.145.0"),
    )

    await session.start()

    start_params = client.requests[2][1]
    assert "nl-NL" in start_params["prompt"]
    assert "language they actually speak" in start_params["prompt"]
    assert "never translate it" in start_params["prompt"]
    assert (
        "speak that supplied text naturally in the configured language"
        in (start_params["prompt"])
    )
    # Codex app-server exposes no native language field for this route; the
    # setting is an explicit speech-interface prompt hint, not a fake protocol claim.
    assert "language" not in start_params
    await session.stop()


@pytest.mark.asyncio
async def test_transcript_and_remote_pcm_notifications_are_forwarded(monkeypatch):
    import agent.transports.codex_realtime_voice as realtime_module

    monkeypatch.setattr(realtime_module, "SPEECH_AUDIO_IDLE_TIMEOUT", 0.05)
    transcripts: list[tuple[str, int]] = []
    pcm_out: list[bytes] = []
    client = FakeClient([
        {
            "method": "thread/realtime/started",
            "params": {"threadId": "thread-1", "version": "v3"},
        },
        {
            "method": "thread/realtime/sdp",
            "params": {"threadId": "thread-1", "sdp": "answer"},
        },
    ])
    peer = FakePeer()
    session = CodexRealtimeSession(
        cwd="/tmp",
        client_factory=lambda **kwargs: client,
        peer_factory=lambda: peer,
        binary_checker=lambda *_args: (True, "0.145.0"),
        on_user_transcript=lambda text, generation: transcripts.append((
            text,
            generation,
        )),
        on_output_pcm=pcm_out.append,
    )
    await session.start()

    client.notifications.extend([
        {
            "method": "thread/realtime/transcript/done",
            "params": {
                "threadId": "foreign-thread",
                "role": "user",
                "text": "Do not route me",
            },
        },
        {
            "method": "thread/realtime/transcript/done",
            "params": {"threadId": "thread-1", "role": "user", "text": "Hallo Nabu"},
        },
    ])
    await asyncio.sleep(0.05)
    assert transcripts == [("Hallo Nabu", 1)]

    assert peer.on_pcm is not None
    # Unsolicited model audio is suppressed: Hermes remains the agent.
    peer.on_pcm(b"\x01\x02")
    assert pcm_out == []

    await session.append_speech("Hoi Maikel", transcript_generation=1)
    assert client.requests[-1] == (
        "thread/realtime/appendSpeech",
        {"threadId": "thread-1", "text": "Hoi Maikel"},
    )
    peer.on_pcm(b"\x01\x02")
    assert pcm_out == [b"\x01\x02"]
    await asyncio.sleep(0.1)
    peer.on_pcm(b"\x03\x04")
    assert pcm_out == [b"\x01\x02"]

    await session.append_speech("Nog een antwoord")
    peer.on_pcm(b"\x05\x06")
    assert pcm_out == [b"\x01\x02", b"\x05\x06"]
    client.notifications.append({
        "method": "thread/realtime/transcript/delta",
        "params": {"threadId": "thread-1", "role": "user", "delta": "Stop"},
    })
    await asyncio.sleep(0.05)
    peer.on_pcm(b"\x07\x08")
    assert pcm_out == [b"\x01\x02", b"\x05\x06"]
    await session.stop()


@pytest.mark.asyncio
async def test_new_user_generation_suppresses_stale_hermes_speech_and_late_completion():
    transcripts: list[tuple[str, int]] = []
    pcm_out: list[bytes] = []
    client = FakeClient([
        {
            "method": "thread/realtime/started",
            "params": {"threadId": "thread-1", "version": "v3"},
        },
        {
            "method": "thread/realtime/sdp",
            "params": {"threadId": "thread-1", "sdp": "answer"},
        },
    ])
    peer = FakePeer()
    session = CodexRealtimeSession(
        cwd="/tmp",
        client_factory=lambda **kwargs: client,
        peer_factory=lambda: peer,
        binary_checker=lambda *_args: (True, "0.145.0"),
        on_user_transcript=lambda text, generation: transcripts.append((
            text,
            generation,
        )),
        on_output_pcm=pcm_out.append,
    )
    await session.start()

    client.notifications.append({
        "method": "thread/realtime/transcript/done",
        "params": {"threadId": "thread-1", "role": "user", "text": "A"},
    })
    await asyncio.sleep(0.05)
    generation_a = transcripts[-1][1]
    client.notifications.extend([
        {
            "method": "thread/realtime/transcript/delta",
            "params": {"threadId": "thread-1", "role": "user", "delta": "B"},
        },
        {
            "method": "thread/realtime/transcript/done",
            "params": {"threadId": "thread-1", "role": "user", "text": "B"},
        },
    ])
    await asyncio.sleep(0.05)
    generation_b = transcripts[-1][1]

    with pytest.raises(CodexRealtimeStaleSpeech):
        await session.append_speech("late answer A", transcript_generation=generation_a)
    assert not any(
        method == "thread/realtime/appendSpeech" and params["text"] == "late answer A"
        for method, params in client.requests
    )

    await session.append_speech("answer B", transcript_generation=generation_b)
    client.notifications.append({
        "method": "thread/realtime/transcript/done",
        "params": {
            "threadId": "thread-1",
            "role": "assistant",
            "text": "late completion A",
        },
    })
    await asyncio.sleep(0.35)
    assert peer.on_pcm is not None
    peer.on_pcm(b"\x01\x02")
    assert pcm_out == [b"\x01\x02"]
    await session.stop()


@pytest.mark.asyncio
async def test_error_closes_session_and_reports_sanitized_reason():
    errors: list[str] = []
    pcm_out: list[bytes] = []
    client = FakeClient([
        {
            "method": "thread/realtime/started",
            "params": {"threadId": "thread-1", "version": "v3"},
        },
        {
            "method": "thread/realtime/sdp",
            "params": {"threadId": "thread-1", "sdp": "answer"},
        },
    ])
    peer = FakePeer()
    session = CodexRealtimeSession(
        cwd="/tmp",
        client_factory=lambda **kwargs: client,
        peer_factory=lambda: peer,
        binary_checker=lambda *_args: (True, "0.145.0"),
        on_output_pcm=pcm_out.append,
        on_error=errors.append,
    )
    await session.start()
    await session.append_speech("Hermes antwoord")
    client.notifications.append({
        "method": "thread/realtime/error",
        "params": {"threadId": "thread-1", "message": "backend unavailable"},
    })
    await asyncio.sleep(0.05)

    assert session.active is False
    assert errors == ["backend unavailable"]
    assert peer.on_pcm is not None
    peer.on_pcm(b"\x01\x02")
    assert pcm_out == []
    await session.stop()
    assert peer.closed is True
    assert client.closed is True


@pytest.mark.asyncio
async def test_remote_close_reports_failure_and_closes_resources():
    errors: list[str] = []
    client = FakeClient([
        {
            "method": "thread/realtime/started",
            "params": {"threadId": "thread-1", "version": "v3"},
        },
        {
            "method": "thread/realtime/sdp",
            "params": {"threadId": "thread-1", "sdp": "answer"},
        },
    ])
    peer = FakePeer()
    session = CodexRealtimeSession(
        cwd="/tmp",
        client_factory=lambda **kwargs: client,
        peer_factory=lambda: peer,
        binary_checker=lambda *_args: (True, "0.145.0"),
        on_error=errors.append,
    )
    await session.start()
    client.notifications.append({
        "method": "thread/realtime/closed",
        "params": {"threadId": "thread-1"},
    })
    await asyncio.sleep(0.05)

    assert session.active is False
    assert errors == ["Codex realtime session closed"]
    assert peer.closed is True
    assert client.closed is True


@pytest.mark.asyncio
async def test_append_speech_failure_reports_provider_failure_and_closes():
    class BrokenSpeechClient(FakeClient):
        def request(self, method: str, params: dict, timeout: float = 30.0):
            if method == "thread/realtime/appendSpeech":
                raise RuntimeError("speech backend unavailable")
            return super().request(method, params, timeout)

    errors: list[str] = []
    client = BrokenSpeechClient([
        {
            "method": "thread/realtime/started",
            "params": {"threadId": "thread-1", "version": "v3"},
        },
        {
            "method": "thread/realtime/sdp",
            "params": {"threadId": "thread-1", "sdp": "answer"},
        },
    ])
    peer = FakePeer()
    session = CodexRealtimeSession(
        cwd="/tmp",
        client_factory=lambda **kwargs: client,
        peer_factory=lambda: peer,
        binary_checker=lambda *_args: (True, "0.145.0"),
        on_error=errors.append,
    )
    await session.start()

    with pytest.raises(RuntimeError, match="speech backend unavailable"):
        await session.append_speech("Hermes antwoord")
    await asyncio.sleep(0.05)

    assert session.active is False
    assert errors == ["Codex realtime speech failed: speech backend unavailable"]
    assert peer.closed is True
    assert client.closed is True


@pytest.mark.asyncio
async def test_webrtc_runtime_failure_closes_session_and_reports_safe_reason():
    errors: list[str] = []
    client = FakeClient([
        {
            "method": "thread/realtime/started",
            "params": {"threadId": "thread-1", "version": "v3"},
        },
        {
            "method": "thread/realtime/sdp",
            "params": {"threadId": "thread-1", "sdp": "answer"},
        },
    ])
    peer = FakePeer()
    session = CodexRealtimeSession(
        cwd="/tmp",
        client_factory=lambda **kwargs: client,
        peer_factory=lambda: peer,
        binary_checker=lambda *_args: (True, "0.145.0"),
        on_error=errors.append,
    )
    await session.start()

    assert peer.on_failure is not None
    peer.on_failure("WebRTC connection failed")
    await asyncio.sleep(0.05)

    assert session.active is False
    assert errors == ["Codex realtime WebRTC failed: WebRTC connection failed"]
    assert peer.closed is True
    assert client.closed is True


@pytest.mark.asyncio
async def test_sideband_audio_is_ignored_for_webrtc_output():
    errors: list[str] = []
    pcm_out: list[bytes] = []
    client = FakeClient([
        {
            "method": "thread/realtime/started",
            "params": {"threadId": "thread-1", "version": "v3"},
        },
        {
            "method": "thread/realtime/sdp",
            "params": {"threadId": "thread-1", "sdp": "answer"},
        },
    ])
    peer = FakePeer()
    session = CodexRealtimeSession(
        cwd="/tmp",
        client_factory=lambda **kwargs: client,
        peer_factory=lambda: peer,
        binary_checker=lambda *_args: (True, "0.145.0"),
        on_output_pcm=pcm_out.append,
        on_error=errors.append,
    )
    await session.start()
    await session.append_speech("Hermes antwoord")
    client.notifications.append({
        "method": "thread/realtime/outputAudio/delta",
        "params": {
            "threadId": "thread-1",
            "audio": {"data": "not-base64!"},
        },
    })
    await asyncio.sleep(0.05)

    assert session.active is True
    assert errors == []
    assert pcm_out == []
    await session.stop()
    assert peer.closed is True
    assert client.closed is True


@pytest.mark.asyncio
async def test_aiortc_peer_offer_contains_audio_and_realtime_data_channel():
    pytest.importorskip("aiortc")
    peer = AiortcRealtimePeer()
    offer = await peer.create_offer(lambda _pcm: None)
    try:
        assert "m=audio" in offer
        assert "m=application" in offer  # SCTP data-channel media section
        assert peer._outgoing is not None
        first = await peer._outgoing.track.recv()
        started = asyncio.get_running_loop().time()
        second = await peer._outgoing.track.recv()
        elapsed = asyncio.get_running_loop().time() - started
        assert first.samples == 480
        assert second.samples == 480
        assert second.pts == first.pts + first.samples
        assert not np.any(first.to_ndarray())
        assert elapsed >= 0.015  # the track paces 20 ms silence between voice packets
    finally:
        await peer.close()


def test_realtime_entitlement_error_is_actionable_and_drops_backend_metadata():
    raw = (
        "unexpected status 403 Forbidden: Voice session access denied., "
        "url: https://chatgpt.com/backend-api/codex/realtime/calls, "
        "cf-ray: abc-AMS, request id: deadbeef"
    )
    safe = safe_realtime_error(raw)
    assert safe == "Codex account is not entitled to realtime voice"
    assert "chatgpt.com" not in safe
    assert "deadbeef" not in safe


def test_discord_pcm_conversion_is_20ms_24khz_mono():
    # 20 ms at 48 kHz: stereo interleaved with opposite channels.
    left = np.arange(960, dtype=np.int16)
    right = (left + 100).astype(np.int16)
    stereo = np.column_stack((left, right)).reshape(-1).tobytes()

    realtime = discord_pcm_to_realtime(stereo)
    mono = np.frombuffer(realtime, dtype=np.int16)
    assert mono.shape == (480,)
    # Average L/R, then average each adjacent 48 kHz sample pair.
    assert mono[:3].tolist() == [50, 52, 54]
