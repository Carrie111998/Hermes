"""Provider-agnostic streaming TTS: sentence text → int16 PCM chunk iterator.

The keystone of Hermes' conversational voice UX. `stream_tts_to_speaker`
(``tools.tts_tool``) owns the sentence buffer, sounddevice output, and
stop/queue protocol; this module owns the *provider* half — turning one
sentence into audio the moment it's ready, so playback starts on sentence one
instead of after the whole reply.

Two provider shapes, one contract (int16 mono PCM at ``sample_rate``):

* **True streamers** (`StreamingTTSProvider.stream`) — chunked APIs
  (ElevenLabs pcm_24000, OpenAI pcm, …) that yield audio as it synthesizes.
  Lowest time-to-first-audio.
* **Everyone else** — providers with no chunked API still get per-*sentence*
  playback via the proven sync `text_to_speech_tool` path (handled by the
  dispatcher, not here), so edge (the default) is conversational too.

Adding a streamer is `@register("name")` on a `StreamingTTSProvider` subclass;
the dispatcher, config gate (`tts.<name>.streaming`), and resolver come free.
"""

from __future__ import annotations

import logging
import re
import struct
import threading
import time
from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Callable, Dict, Iterable, Iterator, List, Optional

from tools.tool_backend_helpers import resolve_openai_audio_api_key
from tools.tts_tool import _get_provider, _load_tts_config, get_env_value

logger = logging.getLogger(__name__)

# Upper bound on the PCM bytes accepted from one provider stream for one
# sentence. Mirrors the 16 MiB bounded-upstream-body invariant of the sync
# providers (``_read_tts_response_bytes`` in tools.tts_tool): a buggy or
# hostile endpoint must not be able to feed us unbounded audio.
_STREAM_SENTENCE_BYTE_CAP = 16 * 1024 * 1024


# ---------------------------------------------------------------------------
# Provider-neutral PCM framing
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class AudioFormat:
    """The only audio representation exchanged by streaming TTS providers.

    Provider SDKs are allowed to chunk their response however they like, but
    the gateway's hot path is deliberately narrower: mono, signed little
    endian 16-bit PCM at a declared sample rate.  Keeping this as a value
    object makes the validation explicit at the provider boundary and gives
    frames a format to carry without relying on provider class attributes.
    """

    sample_rate: int = 24000
    encoding: str = "pcm_s16le"
    channels: int = 1
    sample_width: int = 2

    def __post_init__(self) -> None:
        if self.encoding != "pcm_s16le":
            raise ValueError("audio encoding must be pcm_s16le")
        if self.channels != 1:
            raise ValueError("canonical streaming audio must be mono")
        if self.sample_width != 2:
            raise ValueError("canonical streaming audio must use 16-bit samples")
        if isinstance(self.sample_rate, bool) or self.sample_rate <= 0:
            raise ValueError("sample_rate must be a positive integer")
        if int(self.sample_rate) != self.sample_rate:
            raise ValueError("sample_rate must be an integer")
        object.__setattr__(self, "sample_rate", int(self.sample_rate))

    @property
    def bytes_per_sample(self) -> int:
        return self.channels * self.sample_width

    def validate_chunk(self, chunk: bytes) -> bytes:
        """Return *chunk* as bytes, rejecting non-sample-aligned payloads."""
        if not isinstance(chunk, (bytes, bytearray, memoryview)):
            raise TypeError("audio chunks must be bytes-like")
        payload = bytes(chunk)
        if len(payload) % self.bytes_per_sample:
            raise ValueError("audio chunk is not sample-aligned")
        return payload


@dataclass(frozen=True)
class AudioFrame:
    """A contiguous, loss-detectable unit of canonical PCM audio."""

    seq: int
    start_sample: int
    pcm: bytes
    format: AudioFormat

    def __post_init__(self) -> None:
        if self.seq < 0 or self.start_sample < 0:
            raise ValueError("frame sequence and start_sample must be non-negative")
        payload = self.format.validate_chunk(self.pcm)
        object.__setattr__(self, "pcm", payload)

    @property
    def data(self) -> bytes:
        """Alias used by transports that call binary frame data ``data``."""
        return self.pcm

    @property
    def payload(self) -> bytes:
        return self.pcm

    @property
    def samples(self) -> bytes:
        return self.pcm

    @property
    def sample_count(self) -> int:
        return len(self.pcm) // self.format.bytes_per_sample

    @property
    def sequence(self) -> int:
        return self.seq

    @property
    def encoding(self) -> str:
        return self.format.encoding

    @property
    def sample_rate(self) -> int:
        return self.format.sample_rate

    @property
    def audio_format(self) -> AudioFormat:
        return self.format

    @property
    def duration_ms(self) -> float:
        return self.sample_count * 1000 / self.format.sample_rate


class AudioFramer:
    """Turn arbitrary sample-aligned PCM chunks into fixed-duration frames."""

    def __init__(self, audio_format: AudioFormat, frame_ms: int = 20):
        if isinstance(frame_ms, bool) or frame_ms <= 0:
            raise ValueError("frame_ms must be a positive integer")
        if int(frame_ms) != frame_ms:
            raise ValueError("frame_ms must be an integer")
        frame_samples, remainder = divmod(
            audio_format.sample_rate * int(frame_ms), 1000
        )
        if remainder:
            raise ValueError("frame_ms does not map to whole samples at this rate")
        self.audio_format = audio_format
        self.frame_ms = int(frame_ms)
        self.frame_samples = frame_samples
        self._buffer = bytearray()
        self._next_seq = 0
        self._next_start_sample = 0
        self._closed = False

    @property
    def next_seq(self) -> int:
        return self._next_seq

    @property
    def next_start_sample(self) -> int:
        return self._next_start_sample

    def feed(self, chunk: bytes) -> List[AudioFrame]:
        """Consume one provider chunk and return every complete frame ready."""
        if self._closed:
            raise RuntimeError("audio framer is already flushed")
        if not isinstance(chunk, (bytes, bytearray, memoryview)):
            raise TypeError("audio chunks must be bytes-like")
        # HTTP/SDK chunk boundaries are transport details and may split an
        # int16 sample. Preserve that byte until the next chunk; only the
        # completed stream is required to be sample-aligned.
        payload = bytes(chunk)
        self._buffer.extend(payload)
        frame_bytes = self.frame_samples * self.audio_format.bytes_per_sample
        out: List[AudioFrame] = []
        while len(self._buffer) >= frame_bytes:
            pcm = bytes(self._buffer[:frame_bytes])
            del self._buffer[:frame_bytes]
            out.append(self._make_frame(pcm))
        return out

    # ``push`` is intentionally an alias: provider adapters often use that
    # spelling while the gateway tests/readme use ``feed``.
    push = feed

    def flush(self) -> List[AudioFrame]:
        """Emit one final partial frame, if any, and close the framer."""
        if self._closed:
            return []
        self._closed = True
        if not self._buffer:
            return []
        if len(self._buffer) % self.audio_format.bytes_per_sample:
            raise ValueError("audio stream ended with a partial sample")
        pcm = bytes(self._buffer)
        self._buffer.clear()
        return [self._make_frame(pcm)]

    finish = flush

    def frames(self, chunks: Iterable[bytes]) -> Iterator[AudioFrame]:
        """Yield framed audio for *chunks*, including the final partial frame."""
        for chunk in chunks:
            yield from self.feed(chunk)
        yield from self.flush()

    def _make_frame(self, pcm: bytes) -> AudioFrame:
        frame = AudioFrame(
            seq=self._next_seq,
            start_sample=self._next_start_sample,
            pcm=pcm,
            format=self.audio_format,
        )
        self._next_seq += 1
        self._next_start_sample += frame.sample_count
        return frame


def _resolve_key(env_var: str, provider_id: str) -> str:
    """Provider secret lookup: config > env/.env > credential pool.

    Thin, monkeypatchable seam over ``tools.tts_tool._resolve_provider_key``
    (which delegates to ``resolve_provider_secret``). ALL streaming-provider
    key lookups go through here — never bare ``get_env_value``.
    """
    try:
        from tools.tts_tool import _resolve_provider_key

        return _resolve_provider_key(env_var, provider_id) or ""
    except Exception:
        return get_env_value(env_var) or ""


# ---------------------------------------------------------------------------
# Interruption latch — lets the model know it was cut off mid-speech
# ---------------------------------------------------------------------------
# When the user barges in on a spoken reply (talks over it, types, hits the
# record key), the surface marks the latch; the next turn's submit path takes
# it and prepends SPEECH_INTERRUPTED_NOTE to the model-bound message (API-call
# local — never persisted, same as the CLI's model-switch notes). The TTL
# keeps a stale barge from annotating an unrelated message minutes later.

SPEECH_INTERRUPTED_NOTE = (
    "[Note: the user interrupted your previous spoken reply before it finished.]"
)
_INTERRUPT_TTL_S = 120.0
_interrupted_at: Optional[float] = None


def mark_speech_interrupted() -> None:
    global _interrupted_at
    _interrupted_at = time.monotonic()


def take_speech_interrupted() -> bool:
    """Pop the latch; True when a barge happened within the TTL."""
    global _interrupted_at
    at, _interrupted_at = _interrupted_at, None
    return at is not None and time.monotonic() - at < _INTERRUPT_TTL_S

# Sentence boundary: after .!? followed by whitespace, or a blank line.
SENTENCE_BOUNDARY_RE = re.compile(r"(?<=[.!?])(?:\s|\n)|(?:\n\n)")
_THINK_BLOCK_RE = re.compile(r"<think[\s>].*?</think>", flags=re.DOTALL)


class SentenceChunker:
    """Incremental sentence cutter for LLM token deltas.

    Shared by the speaker pipeline (`stream_tts_to_speaker`) and the
    speak-stream WebSocket so every surface cuts speech identically. Strips
    ``<think>`` blocks (even split across deltas) and merges fragments shorter
    than *min_len* into the following sentence, so "Ha!" rides along with the
    sentence after it instead of stalling as a tiny clip.
    """

    def __init__(self, min_len: int = 20):
        self.min_len = min_len
        self.buf = ""

    def feed(self, delta: str) -> List[str]:
        """Absorb *delta*; return every complete sentence now ready to speak."""
        self.buf = _THINK_BLOCK_RE.sub("", self.buf + delta)
        if "<think" in self.buf and "</think>" not in self.buf:
            return []  # open think tag — the closing tag may arrive next delta
        out: List[str] = []
        start = 0  # skip boundaries that would leave the head too short
        while m := SENTENCE_BOUNDARY_RE.search(self.buf, start):
            head = self.buf[: m.end()]
            if len(head.strip()) < self.min_len:
                start = m.end()
                continue
            out.append(head)
            self.buf = self.buf[m.end():]
            start = 0
        return out

    def flush(self) -> List[str]:
        """Drain the tail (end-of-text or long-idle flush)."""
        tail = _THINK_BLOCK_RE.sub("", self.buf).strip()
        self.buf = ""
        return [tail] if tail else []


# ---------------------------------------------------------------------------
# ABC + registry
# ---------------------------------------------------------------------------

class StreamingTTSProvider(ABC):
    """Yields raw int16, little-endian, mono PCM chunks at ``sample_rate``."""

    sample_rate: int = 24000
    channels: int = 1
    sample_width: int = 2  # bytes/sample (int16)
    # ``True`` means ``cancel()`` can actively close an in-flight upstream
    # response.  False is honest local cancellation only: the transport stops
    # forwarding audio, but the provider request is allowed to finish.
    upstream_cancellable: bool = False

    def __init__(self, tts_config: Dict, section: Dict):
        self.tts_config = tts_config
        self.section = section

    @property
    def audio_format(self) -> AudioFormat:
        """Declared canonical format for this provider's raw stream."""
        return AudioFormat(
            sample_rate=self.sample_rate,
            channels=self.channels,
            sample_width=self.sample_width,
        )

    @property
    def canonical_format(self) -> AudioFormat:
        return self.audio_format

    @staticmethod
    @abstractmethod
    def available() -> bool:
        """True when this provider's credentials/SDK are usable right now."""

    @abstractmethod
    def stream(self, text: str) -> Iterator[bytes]:
        """Yield PCM chunks for ``text``. Raise on failure (caller logs)."""

    def stream_frames(self, text: str, frame_ms: int = 20) -> Iterator[AudioFrame]:
        """Adapt the legacy byte iterator to the provider-neutral frame stream."""
        yield from AudioFramer(self.audio_format, frame_ms=frame_ms).frames(
            self.stream(text)
        )

    def cancel(self) -> None:
        """Idempotent, thread-safe best-effort cancellation contract.

        Non-cancellable adapters intentionally keep this as a no-op.  The
        transport and caller still stop locally; they must not claim that the
        remote request was interrupted.
        """


class _ResponseCancellationMixin:
    """Close a public response handle safely across a streaming worker race."""

    upstream_cancellable = True

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._response_lock = threading.Lock()
        self._active_response = None
        self._cancel_requested = False

    def _begin_response(self) -> bool:
        """Start one operation, preserving a cancellation that won a race."""
        with self._response_lock:
            # A cancelled provider instance is not reused for another request.
            # This is intentional: callers cancel the whole speech session,
            # and retaining the latch covers cancel-before-handle assignment.
            return not self._cancel_requested

    def _attach_response(self, response) -> bool:
        """Publish *response* unless cancellation already won the race."""
        with self._response_lock:
            if self._cancel_requested:
                close = True
            else:
                self._active_response = response
                close = False
        if close:
            try:
                response.close()
            except Exception:
                # SDK response types do not share a narrower close-error
                # hierarchy; retain the unexpected adapter failure while
                # cancellation itself remains non-raising.
                logger.warning("Streaming provider response close failed", exc_info=True)
            return False
        return True

    def _detach_response(self, response) -> None:
        with self._response_lock:
            if self._active_response is response:
                self._active_response = None

    def cancel(self) -> None:
        """Set the latch and close the active public response at most once."""
        with self._response_lock:
            self._cancel_requested = True
            response, self._active_response = self._active_response, None
        if response is not None:
            try:
                response.close()
            except Exception:
                logger.warning("Streaming provider response close failed", exc_info=True)


_REGISTRY: Dict[str, type[StreamingTTSProvider]] = {}


def register(name: str) -> Callable[[type[StreamingTTSProvider]], type[StreamingTTSProvider]]:
    def _wrap(cls: type[StreamingTTSProvider]) -> type[StreamingTTSProvider]:
        _REGISTRY[name] = cls
        return cls

    return _wrap


def _try_instantiate(name: str, tts_config: Dict) -> Optional[StreamingTTSProvider]:
    """Construct the registered streamer *name* if it's usable, else None."""
    cls = _REGISTRY.get(name)
    if cls is None or not cls.available():
        return None
    try:
        return cls(tts_config, tts_config.get(name) or {})
    except Exception as exc:  # pragma: no cover - defensive
        logger.debug("streaming provider %s init failed: %s", name, exc)
        return None


# Fallback priority for ``tts.streaming.provider: auto`` — best chunked
# latency/quality first. Deliberately hard-coded (a UX decision, not a
# config knob); edge is absent because it has no chunked-PCM API — the
# dispatcher's per-sentence sync path keeps it conversational instead.
_PROVIDER_PRIORITY: List[str] = ["elevenlabs", "gemini", "openai", "xai", "finite_fish"]


def resolve_streaming_provider(
    tts_config: Dict,
    preferred: Optional[str] = None,
) -> Optional[StreamingTTSProvider]:
    """Return a ready streamer for the *configured* provider, else ``None``.

    Resolution order:

    1. ``tts.streaming.provider`` (config knob) when set:
       * a provider name pins that exact streamer (or ``None`` if unusable);
       * ``auto`` walks the priority list (``elevenlabs → gemini → openai
         → xai → finite_fish``) and returns the first usable streamer — an
         explicit opt-in to "give me the best chunked voice available".
         ``finite_fish`` is a request-streaming S2 Pro adapter (streamed WAV
         decoded to canonical PCM); its presence here does not claim that
         Fish has passed Hermes' realtime qualification gates.
    2. Otherwise the *configured* TTS provider (or ``preferred`` override).
       ``None`` means "no chunked API for this provider" — the dispatcher
       then speaks per-sentence via the sync path, preserving the user's
       chosen voice. We never silently swap to a different provider just
       to get streaming. The gateway's ``hermes.audio.v1`` transport frames
       every usable stream into 20 ms metadata-plus-binary PCM pairs; Desktop
       plays those frames from one bounded, adaptive AudioWorklet clock. A
       client stop/disconnect invokes the provider's best-effort ``cancel``
       hook; provider/transport failure before audio starts degrades to the
       existing whole-response fallback.
    """
    streaming_cfg = tts_config.get("streaming") or {}
    pinned = str(streaming_cfg.get("provider") or "").lower().strip()
    if pinned == "auto":
        for name in _PROVIDER_PRIORITY:
            inst = _try_instantiate(name, tts_config)
            if inst is not None:
                return inst
        return None
    if pinned:
        return _try_instantiate(pinned, tts_config)

    name = (preferred or _get_provider(tts_config)).lower().strip()
    return _try_instantiate(name, tts_config)


# ---------------------------------------------------------------------------
# Providers
# ---------------------------------------------------------------------------

@register("elevenlabs")
class ElevenLabsStreamer(StreamingTTSProvider):
    """ElevenLabs chunked HTTP → pcm_24000 (the original reference path)."""

    sample_rate = 24000
    upstream_cancellable = False

    @staticmethod
    def available() -> bool:
        return bool(_resolve_key("ELEVENLABS_API_KEY", "elevenlabs"))

    def stream(self, text: str) -> Iterator[bytes]:
        from tools.tts_tool import (
            DEFAULT_ELEVENLABS_STREAMING_MODEL_ID,
            DEFAULT_ELEVENLABS_VOICE_ID,
            _elevenlabs_environment_kwargs,
            _import_elevenlabs,
        )

        client = _import_elevenlabs()(
            api_key=_resolve_key("ELEVENLABS_API_KEY", "elevenlabs"),
            **_elevenlabs_environment_kwargs(self.section),
        )
        voice_id = self.section.get("voice_id", DEFAULT_ELEVENLABS_VOICE_ID)
        model_id = self.section.get(
            "streaming_model_id",
            self.section.get("model_id", DEFAULT_ELEVENLABS_STREAMING_MODEL_ID),
        )
        yield from client.text_to_speech.convert(
            text=text,
            voice_id=voice_id,
            model_id=model_id,
            output_format="pcm_24000",
        )


def _openai_config_api_key() -> str:
    """Return ``tts.openai.api_key`` from config.yaml, or empty string."""
    try:
        openai_cfg = (_load_tts_config().get("openai") or {})
    except Exception:
        return ""
    return openai_cfg.get("api_key") or ""


def _finite_fish_config_api_key() -> str:
    """Return the explicitly configured Finite Fish streaming credential."""
    try:
        section = (_load_tts_config().get("finite_fish") or {})
    except Exception:
        return ""
    return str(
        section.get("api_key") or _resolve_key("FINITE_TTS_API_KEY", "finite_fish") or ""
    ).strip()


def _finite_fish_config_base_url() -> str:
    try:
        section = (_load_tts_config().get("finite_fish") or {})
    except Exception:
        return ""
    return str(section.get("base_url") or get_env_value("FINITE_TTS_BASE_URL") or "").strip()


@register("openai")
class OpenAIStreamer(_ResponseCancellationMixin, StreamingTTSProvider):
    """OpenAI speech with ``response_format=pcm`` (24 kHz mono int16)."""

    sample_rate = 24000
    upstream_cancellable = True

    @staticmethod
    def available() -> bool:
        return bool(_openai_config_api_key() or resolve_openai_audio_api_key())

    def stream(self, text: str) -> Iterator[bytes]:
        from openai import OpenAI

        if not self._begin_response():
            return

        client = OpenAI(
            api_key=(self.section.get("api_key") or resolve_openai_audio_api_key()),
            base_url=(
                self.section.get("base_url")
                or get_env_value("OPENAI_BASE_URL")
                or None
            ),
        )
        model = self.section.get("model", "gpt-4o-mini-tts")
        voice = self.section.get("voice", "alloy")
        with client.audio.speech.with_streaming_response.create(
            model=model,
            voice=voice,
            input=text,
            response_format="pcm",
        ) as response:
            if not self._attach_response(response):
                return
            try:
                yield from _capped(response.iter_bytes(), "OpenAI streaming TTS")
            finally:
                self._detach_response(response)


@register("finite_fish")
class FiniteFishStreamer(_ResponseCancellationMixin, StreamingTTSProvider):
    """Finite Fish S2 Pro streaming WAV, exposed as canonical raw PCM."""

    sample_rate = 24000
    upstream_cancellable = True

    @staticmethod
    def available() -> bool:
        return bool(_finite_fish_config_api_key() and _finite_fish_config_base_url())

    def stream(self, text: str) -> Iterator[bytes]:
        import requests

        if not self._begin_response():
            return

        api_key = str(
            self.section.get("api_key")
            or _finite_fish_config_api_key()
            or _resolve_key("FINITE_TTS_API_KEY", "finite_fish")
            or ""
        ).strip()
        base_url = str(
            self.section.get("base_url") or _finite_fish_config_base_url()
        ).strip().rstrip("/")
        if not api_key or not base_url:
            raise RuntimeError(
                "Finite Fish streaming TTS requires api_key and base_url"
            )
        if not re.match(r"^https?://[^/\s]+", base_url, flags=re.IGNORECASE):
            raise RuntimeError("Finite Fish streaming TTS base_url must be http(s)")
        payload = {
            "model": str(self.section.get("model") or "kokoro-82m-tts"),
            "voice": str(self.section.get("voice") or "default"),
            "input": text,
            "response_format": "wav",
            # The provider still relays the HTTP body incrementally. Keeping the
            # OpenAI payload non-streaming selects the artifact-capacity lane at
            # front doors that distinguish model streams from chunked audio.
            "stream": bool(self.section.get("request_stream", False)),
            "stream_format": "audio",
        }
        with requests.post(
            f"{base_url}/audio/speech",
            headers={
                "Authorization": f"Bearer {api_key}",
                "Accept": "audio/wav",
            },
            json=payload,
            stream=True,
            timeout=(10, 600),
        ) as response:
            if not self._attach_response(response):
                return
            try:
                response.raise_for_status()
                content_type = response.headers.get("Content-Type", "").split(";", 1)[0]
                if content_type.lower() != "audio/wav":
                    raise RuntimeError(
                        f"Finite Fish returned {content_type or 'no content type'}"
                    )
                chunks = (
                    chunk
                    for chunk in response.iter_content(chunk_size=8192)
                    if chunk
                )
                yield from _capped(
                    _streaming_wav_pcm(chunks), "Finite Fish streaming TTS"
                )
            finally:
                self._detach_response(response)


def _streaming_wav_pcm(chunks: Iterator[bytes]) -> Iterator[bytes]:
    """Validate Fish's streamed WAV header, then yield aligned PCM payload."""

    def payloads() -> Iterator[bytes]:
        prefix = b""
        for chunk in chunks:
            prefix += chunk
            if len(prefix) > 64 * 1024:
                raise RuntimeError("Finite Fish WAV header exceeded 64 KiB")
            if len(prefix) < 44:
                continue
            if (
                prefix[:4] != b"RIFF"
                or prefix[8:12] != b"WAVE"
                or prefix[12:16] != b"fmt "
            ):
                raise RuntimeError("Finite Fish returned an invalid WAV stream")
            audio_format, channels, sample_rate, byte_rate, block_align, bits = (
                struct.unpack_from("<HHIIHH", prefix, 20)
            )
            data_at = prefix.find(b"data", 36)
            if data_at < 0 or data_at + 8 > len(prefix):
                continue
            if (
                audio_format != 1
                or channels != 1
                or sample_rate != 24000
                or bits != 16
                or byte_rate != sample_rate * 2
                or block_align != 2
            ):
                raise RuntimeError("Finite Fish returned an incompatible WAV stream")
            pcm = prefix[data_at + 8 :]
            if pcm:
                yield pcm
            break
        else:
            raise RuntimeError("Finite Fish returned a truncated WAV stream")
        yield from chunks

    carry = b""
    for payload in payloads():
        payload = carry + payload
        aligned = len(payload) - len(payload) % 2
        if aligned:
            yield payload[:aligned]
        carry = payload[aligned:]
    if carry:
        raise RuntimeError("Finite Fish returned a partial PCM sample")


def _capped(chunks: Iterator[bytes], label: str) -> Iterator[bytes]:
    """Pass chunks through, aborting past the 16 MiB per-sentence cap.

    The streaming mirror of ``_read_tts_response_bytes``'s bounded-body
    invariant: one sentence of PCM should never approach the cap, so
    exceeding it means a runaway/hostile upstream — stop pulling.
    """
    total = 0
    for chunk in chunks:
        total += len(chunk)
        if total > _STREAM_SENTENCE_BYTE_CAP:
            logger.warning("%s exceeded %d bytes for one sentence; truncating",
                           label, _STREAM_SENTENCE_BYTE_CAP)
            return
        yield chunk


@register("gemini")
class GeminiStreamer(_ResponseCancellationMixin, StreamingTTSProvider):
    """Gemini ``streamGenerateContent?alt=sse`` → base64 PCM chunks (24 kHz).

    Salvaged from PR #47588 (@Cdddo) and rebased onto the post-campaign
    infrastructure: credentials via the provider-secret resolver, requests
    (not httpx) with a bounded streamed body, and main's provider ABC.
    """

    sample_rate = 24000
    upstream_cancellable = True

    @staticmethod
    def available() -> bool:
        return bool(
            _resolve_key("GEMINI_API_KEY", "gemini")
            or _resolve_key("GOOGLE_API_KEY", "gemini")
        )

    def stream(self, text: str) -> Iterator[bytes]:
        import base64
        import json as _json

        import requests

        if not self._begin_response():
            return

        from tools.tts_tool import (
            DEFAULT_GEMINI_TTS_BASE_URL,
            DEFAULT_GEMINI_TTS_MODEL,
            DEFAULT_GEMINI_TTS_VOICE,
        )

        api_key = (
            _resolve_key("GEMINI_API_KEY", "gemini")
            or _resolve_key("GOOGLE_API_KEY", "gemini")
        )
        model = str(self.section.get("model", DEFAULT_GEMINI_TTS_MODEL)).strip() or DEFAULT_GEMINI_TTS_MODEL
        voice = str(self.section.get("voice", DEFAULT_GEMINI_TTS_VOICE)).strip() or DEFAULT_GEMINI_TTS_VOICE
        base_url = str(
            self.section.get("base_url")
            or get_env_value("GEMINI_BASE_URL")
            or DEFAULT_GEMINI_TTS_BASE_URL
        ).strip().rstrip("/")

        payload = {
            "contents": [{"parts": [{"text": text}]}],
            "generationConfig": {
                "responseModalities": ["AUDIO"],
                "speechConfig": {
                    "voiceConfig": {
                        "prebuiltVoiceConfig": {"voiceName": voice},
                    },
                },
            },
        }
        # ``?alt=sse`` flips the response from a single JSON blob to an SSE
        # feed of base64 PCM chunks — the whole point of this provider.
        url = f"{base_url}/models/{model}:streamGenerateContent"

        def _sse_chunks() -> Iterator[bytes]:
            with requests.post(
                url,
                params={"alt": "sse", "key": api_key},
                json=payload,
                timeout=60,
                stream=True,
            ) as response:
                if not self._attach_response(response):
                    return
                try:
                    response.raise_for_status()
                    for line in response.iter_lines(decode_unicode=True):
                        if not line or not line.startswith("data: "):
                            continue
                        try:
                            event = _json.loads(line[len("data: "):])
                            parts = event["candidates"][0]["content"]["parts"]
                        except (ValueError, KeyError, IndexError, TypeError):
                            continue
                        for part in parts:
                            inline = part.get("inlineData") or part.get("inline_data") or {}
                            b64 = inline.get("data", "")
                            if not b64:
                                continue
                            try:
                                yield base64.b64decode(b64)
                            except (ValueError, TypeError) as exc:
                                logger.warning("Gemini SSE: bad base64 audio: %s", exc)
                finally:
                    self._detach_response(response)

        yield from _capped(_sse_chunks(), "Gemini streaming TTS")


@register("xai")
class XAIStreamer(StreamingTTSProvider):
    """xAI WebSocket TTS → binary PCM frames (24 kHz mono int16).

    Salvaged from PR #47588 (@Cdddo): xAI's chunked TTS API is
    WebSocket-only (``wss://api.x.ai/v1/tts``). Credentials route through
    ``resolve_xai_http_credentials`` (OAuth or XAI_API_KEY), same as the
    sync ``_generate_xai_tts`` path. The async WS loop is bridged to the
    sync iterator contract via ``_collect_async`` — the seam unit tests
    monkeypatch.
    """

    sample_rate = 24000
    upstream_cancellable = False

    @staticmethod
    def available() -> bool:
        try:
            from tools.xai_http import resolve_xai_http_credentials

            creds = resolve_xai_http_credentials()
            return bool(str(creds.get("api_key") or "").strip())
        except Exception:
            return False

    def stream(self, text: str) -> Iterator[bytes]:
        yield from _capped(iter(self._collect_async(text)), "xAI streaming TTS")

    # -- async→sync bridge (test seam) ------------------------------------

    def _collect_async(self, text: str) -> List[bytes]:
        import asyncio

        return asyncio.run(self._drain_async(text))

    async def _drain_async(self, text: str) -> List[bytes]:
        frames: List[bytes] = []
        async for frame in self._async_frames(text):
            frames.append(frame)
        return frames

    async def _async_frames(self, text: str):
        import json as _json

        import websockets

        from tools.tts_tool import DEFAULT_XAI_VOICE_ID
        from tools.xai_http import resolve_xai_http_credentials

        creds = resolve_xai_http_credentials()
        api_key = str(creds.get("api_key") or "").strip()
        if not api_key:
            raise RuntimeError("No xAI credentials for streaming TTS")
        voice = str(self.section.get("voice_id", DEFAULT_XAI_VOICE_ID)).strip() or DEFAULT_XAI_VOICE_ID
        ws_url = str(
            self.section.get("streaming_url") or "wss://api.x.ai/v1/tts"
        ).strip()

        async with websockets.connect(
            ws_url, extra_headers={"Authorization": f"Bearer {api_key}"}
        ) as ws:
            await ws.send(_json.dumps({
                "text": text,
                "voice_id": voice,
                "response_format": "pcm",
            }))
            try:
                while True:
                    message = await ws.recv()
                    if isinstance(message, (bytes, bytearray, memoryview)):
                        yield bytes(message)
                        continue
                    try:
                        envelope = _json.loads(message)
                    except (ValueError, TypeError):
                        if message == "done":
                            return
                        continue
                    etype = envelope.get("type")
                    if etype == "done":
                        return
                    if etype == "error":
                        logger.warning("xAI WS error envelope: %s",
                                       envelope.get("error") or envelope.get("message") or envelope)
                        return
            except Exception as exc:
                if exc.__class__.__name__ == "ConnectionClosed":
                    return
                logger.warning("xAI WS receive failed: %s", exc)
                return
