"""Experimental Codex GPT-Live realtime voice transport.

This module owns only the Codex app-server/WebRTC boundary. Discord capture,
agent routing, and playback stay in the gateway/platform edge. The realtime
model is deliberately used as a speech interface: Hermes remains responsible
for the actual assistant turn and tool execution.
"""

from __future__ import annotations

import asyncio
import logging
import re
import time
from dataclasses import dataclass
from fractions import Fraction
from pathlib import Path
from typing import Any, Callable, Optional

from agent.transports.codex_app_server import (
    CodexAppServerClient,
    check_codex_binary,
)

logger = logging.getLogger(__name__)

MIN_CODEX_REALTIME_VERSION = (0, 145, 0)
DEFAULT_CODEX_REALTIME_PROTOCOL = "v3"
SUPPORTED_CODEX_REALTIME_WEBRTC_PROTOCOLS = frozenset({"v1", "v3"})
REALTIME_SAMPLE_RATE = 24_000
REALTIME_CHANNELS = 1
REALTIME_FRAME_SAMPLES = 480  # 20 ms at 24 kHz
SPEECH_FIRST_AUDIO_TIMEOUT = 5.0
SPEECH_AUDIO_IDLE_TIMEOUT = 0.75

_SPEECH_INTERFACE_PROMPT = (
    "You are a low-latency speech interface for another assistant. "
    "Transcribe the user's speech accurately in the language they actually "
    "speak; preserve that language and never translate it. Do not answer "
    "the user, do not call tools, and do not delegate work. Stay silent "
    "until the client supplies text to speak."
)


def _speech_interface_prompt(spoken_language: Optional[str]) -> str:
    """Build the speech-only prompt without claiming a native locale API."""

    if not spoken_language:
        return _SPEECH_INTERFACE_PROMPT
    return (
        "You are a low-latency speech interface for another assistant. "
        f"The configured spoken language is {spoken_language}. "
        "Transcribe the user's speech accurately in the language they actually "
        "speak; preserve that language and never translate it. Do not answer "
        "the user, do not call tools, and do not delegate work. Stay silent "
        "until the client supplies text to speak, then speak that supplied text "
        "naturally in the configured language."
    )


class CodexRealtimeUnavailable(RuntimeError):
    """Raised when the optional Codex realtime route cannot be started."""


class CodexRealtimeStaleSpeech(RuntimeError):
    """Raised when a Hermes reply predates the latest user utterance."""


def safe_realtime_error(message: Any) -> str:
    """Return an actionable error without backend URLs or request metadata."""
    text = str(message or "Codex realtime voice failed").strip()
    lowered = text.lower()
    if "voice session access denied" in lowered or "realtime access denied" in lowered:
        return "Codex account is not entitled to realtime voice"
    if "realtime conversation requires api key auth" in lowered:
        return "Codex realtime route requires API-key authentication"
    from agent.redact import redact_sensitive_text

    text = redact_sensitive_text(text, force=True, redact_url_credentials=True)
    text = re.sub(r"https?://\S+", "<backend-url>", text)
    text = re.sub(r"(?i)(request id|cf-ray):\s*[^,\s]+", r"\1: <redacted>", text)
    return text[:300]


@dataclass(frozen=True)
class CodexRealtimeCapabilities:
    """Capabilities Hermes may safely expose for this experimental route."""

    protocol_version: str
    voices: tuple[str, ...]
    language_selection: bool = False
    reasoning_effort: bool = False


def _require_numpy():
    try:
        import numpy as np
    except ImportError as exc:  # pragma: no cover - dependency gate exercises this
        raise CodexRealtimeUnavailable(
            "Codex realtime voice requires the optional voice dependencies"
        ) from exc
    return np


def discord_pcm_to_realtime(pcm: bytes) -> bytes:
    """Convert Discord 48 kHz stereo s16le PCM to 24 kHz mono s16le."""

    if not pcm:
        return b""
    np = _require_numpy()
    samples = np.frombuffer(pcm, dtype=np.int16)
    usable = samples.size - (samples.size % 4)
    if usable <= 0:
        return b""
    stereo = samples[:usable].reshape(-1, 2).astype(np.int32)
    mono_48k = (stereo[:, 0] + stereo[:, 1]) // 2
    mono_24k = (mono_48k[0::2] + mono_48k[1::2]) // 2
    return np.clip(mono_24k, -32768, 32767).astype(np.int16).tobytes()


class _AiortcOutgoingAudioTrack:
    """Small wrapper around a lazily-created aiortc MediaStreamTrack."""

    def __init__(self) -> None:
        from aiortc import MediaStreamTrack
        from av import AudioFrame

        queue: asyncio.Queue[bytes | None] = asyncio.Queue(maxsize=100)

        class QueuedTrack(MediaStreamTrack):
            kind = "audio"

            def __init__(track_self) -> None:
                super().__init__()
                track_self._pts = 0
                track_self._started_at: Optional[float] = None

            async def recv(track_self):
                loop = asyncio.get_running_loop()
                if track_self._started_at is None:
                    track_self._started_at = loop.time()
                target = track_self._started_at + (
                    track_self._pts / REALTIME_SAMPLE_RATE
                )
                delay = target - loop.time()
                if delay > 0:
                    await asyncio.sleep(delay)
                try:
                    pcm = queue.get_nowait()
                except asyncio.QueueEmpty:
                    pcm = b"\x00" * (REALTIME_FRAME_SAMPLES * 2)
                if pcm is None:
                    track_self.stop()
                    raise asyncio.CancelledError
                sample_count = len(pcm) // 2
                frame = AudioFrame(format="s16", layout="mono", samples=sample_count)
                frame.planes[0].update(pcm)
                frame.sample_rate = REALTIME_SAMPLE_RATE
                frame.pts = track_self._pts
                frame.time_base = Fraction(1, REALTIME_SAMPLE_RATE)
                track_self._pts += sample_count
                return frame

        self.track = QueuedTrack()
        self._queue = queue

    def push(self, pcm: bytes) -> None:
        if not pcm:
            return
        try:
            self._queue.put_nowait(pcm)
        except asyncio.QueueFull:
            try:
                self._queue.get_nowait()
            except asyncio.QueueEmpty:
                pass
            try:
                self._queue.put_nowait(pcm)
            except asyncio.QueueFull:  # pragma: no cover - defensive race
                pass

    def close(self) -> None:
        try:
            self._queue.put_nowait(None)
        except asyncio.QueueFull:
            try:
                self._queue.get_nowait()
                self._queue.put_nowait(None)
            except (asyncio.QueueEmpty, asyncio.QueueFull):
                pass
        self.track.stop()


class AiortcRealtimePeer:
    """OAuth-compatible WebRTC media peer for Codex app-server realtime."""

    def __init__(self) -> None:
        self._pc = None
        self._outgoing: Optional[_AiortcOutgoingAudioTrack] = None
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._remote_tasks: set[asyncio.Task] = set()
        self._on_pcm: Optional[Callable[[bytes], None]] = None
        self._on_failure: Optional[Callable[[str], None]] = None
        self._closing = False
        self._failure_reported = False

    async def create_offer(
        self,
        on_pcm: Callable[[bytes], None],
        on_failure: Optional[Callable[[str], None]] = None,
    ) -> str:
        try:
            from aiortc import RTCPeerConnection
        except ImportError as exc:
            raise CodexRealtimeUnavailable(
                "Codex realtime voice requires aiortc; install the realtime voice extra"
            ) from exc

        self._loop = asyncio.get_running_loop()
        self._on_pcm = on_pcm
        self._on_failure = on_failure
        self._pc = RTCPeerConnection()
        self._outgoing = _AiortcOutgoingAudioTrack()
        self._pc.addTrack(self._outgoing.track)
        # The OpenAI realtime WebRTC contract expects this channel in the SDP,
        # even though Codex app-server consumes events through its sideband WS.
        self._pc.createDataChannel("oai-events")

        @self._pc.on("track")
        def _on_track(track) -> None:
            if getattr(track, "kind", None) != "audio":
                return
            task = asyncio.create_task(self._consume_remote_audio(track))
            self._remote_tasks.add(task)
            task.add_done_callback(self._remote_tasks.discard)

        @self._pc.on("connectionstatechange")
        def _on_connection_state() -> None:
            if (
                self._pc is not None
                and self._pc.connectionState in {"failed", "closed"}
                and not self._closing
            ):
                self._notify_failure(f"WebRTC connection {self._pc.connectionState}")

        offer = await self._pc.createOffer()
        await self._pc.setLocalDescription(offer)
        await self._wait_for_ice_gathering()
        local = self._pc.localDescription
        if local is None or not local.sdp:
            raise CodexRealtimeUnavailable("WebRTC produced no local SDP offer")
        return local.sdp

    async def _wait_for_ice_gathering(self, timeout: float = 10.0) -> None:
        if self._pc is None or self._pc.iceGatheringState == "complete":
            return
        complete = asyncio.Event()

        @self._pc.on("icegatheringstatechange")
        def _on_ice_state() -> None:
            if self._pc is not None and self._pc.iceGatheringState == "complete":
                complete.set()

        try:
            await asyncio.wait_for(complete.wait(), timeout=timeout)
        except asyncio.TimeoutError as exc:
            raise CodexRealtimeUnavailable("WebRTC ICE gathering timed out") from exc

    async def accept_answer(self, sdp: str) -> None:
        if self._pc is None:
            raise CodexRealtimeUnavailable("WebRTC peer has not created an offer")
        from aiortc import RTCSessionDescription

        await self._pc.setRemoteDescription(
            RTCSessionDescription(sdp=sdp, type="answer")
        )

    def push_input(self, pcm: bytes) -> bool:
        outgoing = self._outgoing
        loop = self._loop
        if outgoing is None or loop is None or loop.is_closed():
            return False
        converted = discord_pcm_to_realtime(pcm)
        if not converted:
            return False
        loop.call_soon_threadsafe(outgoing.push, converted)
        return True

    async def _consume_remote_audio(self, track) -> None:
        from av import AudioResampler

        resampler = AudioResampler(format="s16", layout="stereo", rate=48_000)
        try:
            while True:
                frame = await track.recv()
                for output in resampler.resample(frame):
                    pcm = output.to_ndarray().tobytes()
                    if pcm and self._on_pcm is not None:
                        self._on_pcm(pcm)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            # MediaStreamError is the normal remote-track EOF during teardown.
            # Outside teardown it means speech output is gone, so fail over.
            logger.debug(
                "Codex realtime remote audio ended: %s", safe_realtime_error(exc)
            )
            if not self._closing:
                self._notify_failure("WebRTC remote audio ended")

    def _notify_failure(self, reason: str) -> None:
        if self._failure_reported or self._closing:
            return
        self._failure_reported = True
        if self._on_failure is not None:
            self._on_failure(reason)

    async def close(self) -> None:
        self._closing = True
        if self._outgoing is not None:
            self._outgoing.close()
        for task in list(self._remote_tasks):
            task.cancel()
        if self._remote_tasks:
            await asyncio.gather(*self._remote_tasks, return_exceptions=True)
        self._remote_tasks.clear()
        if self._pc is not None:
            await self._pc.close()
        self._pc = None
        self._outgoing = None
        self._on_failure = None


class CodexRealtimeSession:
    """One isolated Codex app-server thread plus one WebRTC voice session."""

    def __init__(
        self,
        *,
        cwd: str | Path,
        codex_bin: str = "codex",
        codex_home: Optional[str] = None,
        protocol_version: str = DEFAULT_CODEX_REALTIME_PROTOCOL,
        spoken_language: Optional[str] = None,
        client_factory: Callable[..., Any] = CodexAppServerClient,
        peer_factory: Callable[[], Any] = AiortcRealtimePeer,
        binary_checker: Callable[..., tuple[bool, str]] = check_codex_binary,
        on_user_transcript: Optional[Callable[[str, int], Any]] = None,
        on_output_pcm: Optional[Callable[[bytes], Any]] = None,
        on_error: Optional[Callable[[str], Any]] = None,
    ) -> None:
        self._cwd = str(cwd)
        self._codex_bin = codex_bin
        self._codex_home = codex_home
        self._protocol_version = str(protocol_version).strip().lower()
        if self._protocol_version not in SUPPORTED_CODEX_REALTIME_WEBRTC_PROTOCOLS:
            raise CodexRealtimeUnavailable(
                "Codex realtime WebRTC protocol_version must be v1 or v3"
            )
        self._spoken_language = (
            " ".join(str(spoken_language).split())[:80] if spoken_language else None
        )
        self._client_factory = client_factory
        self._peer_factory = peer_factory
        self._binary_checker = binary_checker
        self._on_user_transcript = on_user_transcript
        self._on_output_pcm = on_output_pcm
        self._on_error = on_error
        self._client = None
        self._peer = None
        self._thread_id: Optional[str] = None
        self._notification_task: Optional[asyncio.Task] = None
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._active = False
        # Remote model audio is dropped until Hermes explicitly appends the
        # final response text. This keeps GPT-Live as speech I/O instead of a
        # second assistant that can answer independently of Hermes.
        self._speech_gate = False
        self._speech_generation = 0
        self._speech_started_at: Optional[float] = None
        self._speech_last_pcm_at: Optional[float] = None
        self._speech_watchdog_task: Optional[asyncio.Task] = None
        self._peer_failure_reason: Optional[str] = None
        self._failure_notified = False
        self._stop_lock = asyncio.Lock()

    @property
    def active(self) -> bool:
        return self._active

    async def start(self, *, voice: Optional[str] = None) -> CodexRealtimeCapabilities:
        if self._active:
            raise RuntimeError("Codex realtime session is already active")
        self._loop = asyncio.get_running_loop()
        self._peer_failure_reason = None
        self._failure_notified = False
        ok, detail = await asyncio.to_thread(
            self._binary_checker,
            self._codex_bin,
            MIN_CODEX_REALTIME_VERSION,
        )
        if not ok:
            raise CodexRealtimeUnavailable(detail)

        self._client = self._client_factory(
            codex_bin=self._codex_bin,
            codex_home=self._codex_home,
            extra_args=[
                "-c",
                "features.realtime_conversation=true",
                "-c",
                'sandbox_mode="read-only"',
                "-c",
                'approval_policy="never"',
            ],
        )
        self._peer = self._peer_factory()
        try:
            capabilities = await asyncio.to_thread(self._initialize_client, voice)
            offer_sdp = await self._peer.create_offer(
                self._handle_peer_pcm,
                self._handle_peer_failure,
            )
            await asyncio.to_thread(self._request_start, offer_sdp, voice)
            answer_sdp = await self._wait_for_startup_notifications()
            await self._peer.accept_answer(answer_sdp)
            if self._peer_failure_reason:
                raise CodexRealtimeUnavailable(self._peer_failure_reason)
        except Exception:
            await self._close_resources()
            raise

        self._active = True
        self._notification_task = asyncio.create_task(
            self._notification_loop(), name="codex-realtime-notifications"
        )
        return capabilities

    def _initialize_client(
        self, requested_voice: Optional[str]
    ) -> CodexRealtimeCapabilities:
        assert self._client is not None
        self._client.initialize(
            client_name="hermes-realtime-voice",
            client_title="Hermes Realtime Voice",
            client_version="1",
            capabilities={
                "experimentalApi": True,
                # WebRTC output is played from the negotiated remote audio
                # track. Suppress sideband PCM to avoid duplicate playback and
                # unnecessary base64 traffic over app-server stdio.
                "optOutNotificationMethods": ["thread/realtime/outputAudio/delta"],
            },
        )
        thread_result = self._client.request(
            "thread/start",
            {"cwd": self._cwd, "ephemeral": True},
            timeout=15,
        )
        thread = thread_result.get("thread") or {}
        self._thread_id = (
            thread.get("id")
            or thread.get("sessionId")
            or thread_result.get("threadId")
            or thread_result.get("sessionId")
        )
        if not self._thread_id:
            raise CodexRealtimeUnavailable("Codex thread/start returned no thread id")
        voices_result = self._client.request(
            "thread/realtime/listVoices", {}, timeout=10
        )
        raw_voices = voices_result.get("voices") or {}
        if isinstance(raw_voices, dict):
            voices = tuple(str(value) for value in raw_voices.get("v1", []) if value)
        else:
            voices = ()
        if requested_voice and voices and requested_voice not in voices:
            raise CodexRealtimeUnavailable(
                f"Codex realtime voice {requested_voice!r} is not supported for "
                f"{self._protocol_version}; "
                f"choose one of: {', '.join(voices)}"
            )
        return CodexRealtimeCapabilities(
            protocol_version=self._protocol_version,
            voices=voices,
        )

    def _request_start(self, offer_sdp: str, voice: Optional[str]) -> None:
        assert self._client is not None and self._thread_id is not None
        params: dict[str, Any] = {
            "threadId": self._thread_id,
            "clientManagedHandoffs": True,
            "includeStartupContext": False,
            "outputModality": "audio",
            "prompt": _speech_interface_prompt(self._spoken_language),
            "transport": {"type": "webrtc", "sdp": offer_sdp},
            "version": self._protocol_version,
        }
        if voice:
            params["voice"] = voice
        self._client.request("thread/realtime/start", params, timeout=30)

    async def _wait_for_startup_notifications(self, timeout: float = 30.0) -> str:
        assert self._client is not None
        deadline = time.monotonic() + timeout
        started = False
        answer_sdp: Optional[str] = None
        while time.monotonic() < deadline and (not started or answer_sdp is None):
            notification = await asyncio.to_thread(self._client.take_notification, 0.2)
            if notification is None:
                continue
            method = notification.get("method")
            params = notification.get("params") or {}
            if (
                str(method or "").startswith("thread/realtime/")
                and params.get("threadId") != self._thread_id
            ):
                continue
            if method == "thread/realtime/started":
                version = str(params.get("version") or "")
                if version and version != self._protocol_version:
                    raise CodexRealtimeUnavailable(
                        f"Codex realtime started unsupported protocol {version!r}"
                    )
                started = True
            elif method == "thread/realtime/sdp":
                answer_sdp = str(params.get("sdp") or "") or None
            elif method == "thread/realtime/error":
                raise CodexRealtimeUnavailable(
                    safe_realtime_error(
                        params.get("message") or "Codex realtime startup failed"
                    )
                )
            elif method == "thread/realtime/closed":
                raise CodexRealtimeUnavailable("Codex realtime closed during startup")
        if not started or answer_sdp is None:
            raise CodexRealtimeUnavailable("Codex realtime startup timed out")
        return answer_sdp

    async def _notification_loop(self) -> None:
        assert self._client is not None
        try:
            while self._active:
                notification = await asyncio.to_thread(
                    self._client.take_notification, 0.2
                )
                if notification is None:
                    client_alive = (
                        self._client.is_alive()
                        if hasattr(self._client, "is_alive")
                        else True
                    )
                    if not client_alive:
                        self._fail("Codex app-server exited")
                        await self.stop()
                        return
                    continue
                method = notification.get("method")
                params = notification.get("params") or {}
                if (
                    str(method or "").startswith("thread/realtime/")
                    and params.get("threadId") != self._thread_id
                ):
                    continue
                if (
                    method == "thread/realtime/transcript/delta"
                    and params.get("role") == "user"
                ):
                    # Barge-in closes the speech gate before a new user utterance
                    # can trigger independent remote-model audio.
                    self._speech_generation += 1
                    self._speech_gate = False
                    self._cancel_speech_watchdog()
                    continue
                if method == "thread/realtime/transcript/done":
                    role = params.get("role")
                    if role == "user":
                        self._speech_generation += 1
                        self._speech_gate = False
                        self._cancel_speech_watchdog()
                        text = str(params.get("text") or "").strip()
                        if text:
                            self._emit(
                                self._on_user_transcript,
                                text,
                                self._speech_generation,
                            )
                    # Assistant transcript notifications carry no item or
                    # appendSpeech generation identifier. They therefore cannot
                    # safely close a newer speech gate; WebRTC audio inactivity
                    # provides the response boundary instead.
                elif method == "thread/realtime/outputAudio/delta":
                    # WebRTC audio arrives on the negotiated remote media
                    # track. Ignore defensive sideband copies so one response
                    # cannot be played twice.
                    continue
                elif method == "thread/realtime/error":
                    self._fail(
                        safe_realtime_error(
                            params.get("message") or "Codex realtime error"
                        )
                    )
                    await self.stop()
                    return
                elif method == "thread/realtime/closed":
                    self._fail(
                        str(params.get("reason") or "Codex realtime session closed")
                    )
                    await self.stop()
                    return
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            self._fail(f"Codex realtime protocol error: {exc}")
            await self.stop()

    def _emit_output_pcm(self, pcm: bytes) -> None:
        if pcm:
            self._emit(self._on_output_pcm, pcm)

    def _handle_peer_pcm(self, pcm: bytes) -> None:
        if self._speech_gate:
            self._speech_last_pcm_at = time.monotonic()
            self._emit_output_pcm(pcm)

    def _handle_peer_failure(self, reason: str) -> None:
        safe_reason = safe_realtime_error(reason)
        self._peer_failure_reason = safe_reason
        if self._active:
            self._fail(f"Codex realtime WebRTC failed: {safe_reason}")
            self._schedule_stop()

    async def _watch_speech_gate(
        self, generation: int, maximum_duration: float
    ) -> None:
        """Close one speech gate after output ends or never starts."""

        try:
            while self._speech_gate and generation == self._speech_generation:
                await asyncio.sleep(0.05)
                now = time.monotonic()
                started_at = self._speech_started_at
                last_pcm_at = self._speech_last_pcm_at
                if started_at is None:
                    return
                if now - started_at >= maximum_duration:
                    self._speech_gate = False
                    return
                if last_pcm_at is None:
                    if now - started_at >= SPEECH_FIRST_AUDIO_TIMEOUT:
                        self._speech_gate = False
                        return
                elif now - last_pcm_at >= SPEECH_AUDIO_IDLE_TIMEOUT:
                    self._speech_gate = False
                    return
        finally:
            if self._speech_watchdog_task is asyncio.current_task():
                self._speech_watchdog_task = None

    def _cancel_speech_watchdog(self) -> None:
        task, self._speech_watchdog_task = self._speech_watchdog_task, None
        if task is not None and not task.done():
            task.cancel()

    @staticmethod
    def _emit(callback: Optional[Callable[..., Any]], *values: Any) -> None:
        if callback is None:
            return
        try:
            result = callback(*values)
            if asyncio.iscoroutine(result):
                asyncio.create_task(result)
        except Exception:
            logger.exception("Codex realtime callback failed")

    def _fail(self, reason: str) -> None:
        if self._failure_notified:
            return
        self._failure_notified = True
        self._active = False
        self._speech_generation += 1
        self._speech_gate = False
        self._cancel_speech_watchdog()
        self._speech_started_at = None
        self._speech_last_pcm_at = None
        self._emit(self._on_error, safe_realtime_error(reason))

    def _schedule_stop(self) -> None:
        loop = self._loop
        if loop is None or loop.is_closed():
            return
        loop.call_soon_threadsafe(lambda: asyncio.create_task(self.stop()))

    def push_discord_pcm(self, pcm: bytes) -> bool:
        if not self._active or self._peer is None:
            return False
        try:
            return bool(self._peer.push_input(pcm))
        except Exception as exc:
            self._fail(f"Codex realtime input failed: {exc}")
            self._schedule_stop()
            return False

    async def append_speech(
        self,
        text: str,
        *,
        transcript_generation: Optional[int] = None,
    ) -> bool:
        cleaned = str(text or "").strip()
        if (
            not cleaned
            or not self._active
            or self._client is None
            or not self._thread_id
        ):
            return False
        if (
            transcript_generation is not None
            and transcript_generation != self._speech_generation
        ):
            raise CodexRealtimeStaleSpeech(
                "Hermes reply predates the latest realtime user utterance"
            )
        self._speech_generation += 1
        generation = self._speech_generation
        # Do not trust any remote PCM until app-server has accepted this exact
        # Hermes-authored speech request. Opening the gate before the JSON-RPC
        # acknowledgement can expose delayed or unsolicited provider audio.
        self._speech_gate = False
        self._cancel_speech_watchdog()
        self._speech_started_at = None
        self._speech_last_pcm_at = None
        try:
            await asyncio.to_thread(
                self._client.request,
                "thread/realtime/appendSpeech",
                {"threadId": self._thread_id, "text": cleaned},
                15,
            )
        except Exception as exc:
            safe_reason = safe_realtime_error(f"Codex realtime speech failed: {exc}")
            self._fail(safe_reason)
            self._schedule_stop()
            raise CodexRealtimeUnavailable(safe_reason) from None
        if not self._active:
            return False
        if generation != self._speech_generation:
            raise CodexRealtimeStaleSpeech(
                "Hermes reply was superseded while realtime speech was starting"
            )
        self._speech_gate = True
        self._speech_started_at = time.monotonic()
        # Fail closed if WebRTC output never starts or never becomes idle.
        gate_timeout = min(180.0, max(10.0, len(cleaned) / 8.0))
        self._speech_watchdog_task = asyncio.create_task(
            self._watch_speech_gate(generation, gate_timeout)
        )
        return True

    async def stop(self) -> None:
        async with self._stop_lock:
            was_active = self._active
            self._active = False
            self._speech_generation += 1
            self._speech_gate = False
            watchdog = self._speech_watchdog_task
            self._cancel_speech_watchdog()
            self._speech_started_at = None
            self._speech_last_pcm_at = None
            if watchdog is not None:
                await asyncio.gather(watchdog, return_exceptions=True)
            task = self._notification_task
            self._notification_task = None
            if task is not None and task is not asyncio.current_task():
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
            if was_active and self._client is not None and self._thread_id:
                try:
                    await asyncio.to_thread(
                        self._client.request,
                        "thread/realtime/stop",
                        {"threadId": self._thread_id},
                        10,
                    )
                except Exception as exc:
                    logger.debug(
                        "Codex realtime stop request failed: %s",
                        safe_realtime_error(exc),
                    )
            await self._close_resources()

    async def _close_resources(self) -> None:
        peer, self._peer = self._peer, None
        client, self._client = self._client, None
        self._loop = None
        self._thread_id = None
        self._peer_failure_reason = None
        if peer is not None:
            try:
                await peer.close()
            except Exception as exc:
                logger.debug(
                    "Codex realtime WebRTC close failed: %s",
                    safe_realtime_error(exc),
                )
        if client is not None:
            try:
                await asyncio.to_thread(client.close)
            except Exception as exc:
                logger.debug(
                    "Codex realtime app-server close failed: %s",
                    safe_realtime_error(exc),
                )
