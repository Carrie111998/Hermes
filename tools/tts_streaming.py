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
import time
from abc import ABC, abstractmethod
from typing import Callable, Dict, Iterator, List, Optional, Tuple

from tools.tool_backend_helpers import resolve_openai_audio_api_key
from tools.tts_tool import _get_provider, _load_tts_config, get_env_value

logger = logging.getLogger(__name__)

# Upper bound on the PCM bytes accepted from one provider stream for one
# sentence. Mirrors the 16 MiB bounded-upstream-body invariant of the sync
# providers (``_read_tts_response_bytes`` in tools.tts_tool): a buggy or
# hostile endpoint must not be able to feed us unbounded audio.
_STREAM_SENTENCE_BYTE_CAP = 16 * 1024 * 1024


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
_THINK_BLOCK_RE = re.compile(
    r"<think(?:\s[^>]*)?>.*?</think\s*>",
    flags=re.DOTALL | re.IGNORECASE,
)
_THINK_OPEN_RE = re.compile(r"<think(?:\s[^>]*)?>", flags=re.IGNORECASE)
_FENCED_CODE_BLOCK_RE = re.compile(r"```[\s\S]*?```")
_VERIFIER_START_RE = re.compile(
    r"(?:⚠️?\s*)?File-mutation verifier:",
    flags=re.IGNORECASE,
)
_REGEX_ASCII_FOLD_TRANSLATION = str.maketrans(
    {"İ": "i", "ı": "i", "ſ": "s", "K": "k"}
)


def _regex_ascii_casefold(text: str) -> str:
    """Fold text like ``re.IGNORECASE`` does for ASCII literals."""
    return text.translate(_REGEX_ASCII_FOLD_TRANSLATION).casefold()


def _literal_ignorecase_matches(text: str, literal: str) -> bool:
    """Return whether equal-length *text* matches *literal* under ``re.I``."""
    return len(text) == len(literal) and re.fullmatch(
        re.escape(literal), text, flags=re.IGNORECASE
    ) is not None


def _fenced_code_ranges(text: str) -> List[Tuple[int, int]]:
    """Return complete and open triple-backtick ranges in *text*."""
    ranges: List[Tuple[int, int]] = []
    cursor = 0
    while True:
        start = text.find("```", cursor)
        if start < 0:
            break
        end = text.find("```", start + 3)
        if end < 0:
            ranges.append((start, len(text)))
            break
        ranges.append((start, end + 3))
        cursor = end + 3
    return ranges


def _position_in_ranges(position: int, ranges: List[Tuple[int, int]]) -> bool:
    return any(start <= position < end for start, end in ranges)


def _first_think_open_outside_fences(text: str) -> Optional[re.Match]:
    fenced_ranges = _fenced_code_ranges(text)
    return next(
        (
            match
            for match in _THINK_OPEN_RE.finditer(text)
            if not _position_in_ranges(match.start(), fenced_ranges)
        ),
        None,
    )


class SentenceChunker:
    """Incremental sentence cutter for LLM token deltas.

    Shared by every streaming speech surface. Protected non-spoken blocks are
    removed before sentence cutting, even when their delimiters span deltas.
    Optional pronunciation substitutions inform boundary selection without
    altering returned display text. A boundary inside a configured phrase is
    held until the phrase can be matched, so terms such as ``Dr. Smith`` reach
    the speech-only rewrite intact.
    """

    def __init__(self, min_len: int = 20, pronunciation_substitutions: object = None):
        self.min_len = min_len
        self.buf = ""
        self._discard_rest = False
        from tools.tts_text_normalize import get_pronunciation_substitutions

        self._pronunciation_substitutions = get_pronunciation_substitutions({
            "pronunciation": {"substitutions": pronunciation_substitutions},
        })
        self._pronunciation_sources = tuple(self._pronunciation_substitutions)

    @property
    def holding_protected_text(self) -> bool:
        """Whether an open protected block is waiting for its closing marker."""
        return (
            self._has_open_protected_block()
            or self._partial_protected_tail_start(self.buf) is not None
        )

    @property
    def holding_pronunciation_lookahead(self) -> bool:
        """Whether the buffer ends inside a configured source phrase."""
        return self._substitution_may_cross_boundary(len(self.buf))

    def _remove_complete_protected_blocks(self, text: str) -> str:
        fenced_ranges = _fenced_code_ranges(text)
        removable_think_blocks = [
            match
            for match in _THINK_BLOCK_RE.finditer(text)
            if not _position_in_ranges(match.start(), fenced_ranges)
        ]
        if removable_think_blocks:
            parts: List[str] = []
            cursor = 0
            for match in removable_think_blocks:
                parts.append(text[cursor:match.start()])
                cursor = match.end()
            parts.append(text[cursor:])
            text = "".join(parts)

        fenced_ranges = _fenced_code_ranges(text)
        open_think = _first_think_open_outside_fences(text)
        for verifier in _VERIFIER_START_RE.finditer(text):
            # Literal verifier examples inside fenced documentation or an open
            # think block are protected content, not the start of the actual
            # hidden verifier footer. Complete think blocks were removed above.
            if _position_in_ranges(verifier.start(), fenced_ranges) or (
                open_think is not None and open_think.start() < verifier.start()
            ):
                continue
            text = text[:verifier.start()]
            self._discard_rest = True
            break
        return text

    def _has_open_protected_block(self) -> bool:
        return bool(
            _first_think_open_outside_fences(self.buf) is not None
            or self.buf.count("```") % 2
        )

    @staticmethod
    def _partial_protected_tail_start(text: str) -> Optional[int]:
        """Return the start of an incomplete protected opening delimiter."""
        starts: List[int] = []

        last_angle = text.rfind("<")
        if last_angle >= 0:
            fragment = text[last_angle:]
            keyword = "<think"
            if (
                len(fragment) <= len(keyword)
                and _literal_ignorecase_matches(fragment, keyword[:len(fragment)])
            ) or (
                len(fragment) > len(keyword)
                and _literal_ignorecase_matches(fragment[:len(keyword)], keyword)
                and fragment[len(keyword)].isspace()
                and ">" not in fragment[len(keyword):]
            ):
                starts.append(last_angle)

        trailing_ticks = len(text) - len(text.rstrip("`"))
        if trailing_ticks in (1, 2):
            starts.append(len(text) - trailing_ticks)

        folded = _regex_ascii_casefold(text)
        verifier_openers = (
            "file-mutation verifier:",
            "⚠ file-mutation verifier:",
            "⚠️ file-mutation verifier:",
        )
        for opener in verifier_openers:
            max_prefix = min(len(folded), len(opener) - 1)
            for prefix_len in range(max_prefix, 0, -1):
                if folded.endswith(opener[:prefix_len]):
                    starts.append(len(text) - prefix_len)
                    break

        return min(starts) if starts else None

    def _strip_open_protected_tail(self, text: str) -> str:
        starts = []
        think = _first_think_open_outside_fences(text)
        if think is not None:
            starts.append(think.start())
        if text.count("```") % 2:
            fence = text.rfind("```")
            if fence >= 0:
                starts.append(fence)
        partial = self._partial_protected_tail_start(text)
        if partial is not None:
            starts.append(partial)
        return text[:min(starts)] if starts else text

    def _boundary_is_inside_fenced_code(self, boundary_end: int) -> bool:
        return any(
            match.start() < boundary_end <= match.end()
            for match in _FENCED_CODE_BLOCK_RE.finditer(self.buf)
        )

    @staticmethod
    def _is_word_char(char: str) -> bool:
        return bool(char and re.match(r"\w", char))

    def _substitution_may_cross_boundary(self, split: int) -> bool:
        """Return True when a configured source may span *split*.

        This includes an incomplete source prefix at the end of the current
        buffer, which delays only the candidate boundary until the next delta
        confirms or rejects the match.
        """
        if not self._pronunciation_sources:
            return False
        for source in self._pronunciation_sources:
            source_len = len(source)

            # At an idle boundary, a source that ends exactly at the current
            # buffer tail still has an unresolved ``(?!\w)`` right boundary.
            # Hold it until another delta supplies that character, or until the
            # explicit end-of-text flush confirms there is no character.
            exact_start = split - source_len
            if split == len(self.buf) and exact_start >= 0:
                candidate = self.buf[exact_start:split]
                if (
                    _literal_ignorecase_matches(candidate, source)
                    and (exact_start == 0 or not self._is_word_char(self.buf[exact_start - 1]))
                ):
                    return True

            first_start = max(0, split - source_len + 1)
            for start in range(first_start, split):
                end = start + source_len
                if not (start < split < end):
                    continue
                available_end = min(len(self.buf), end)
                candidate = self.buf[start:available_end]
                source_prefix = source[:available_end - start]
                if not _literal_ignorecase_matches(candidate, source_prefix):
                    continue
                if start > 0 and self._is_word_char(self.buf[start - 1]):
                    continue
                if len(self.buf) >= end:
                    if end < len(self.buf) and self._is_word_char(self.buf[end]):
                        continue
                return True
        return False

    def feed(self, delta: str) -> List[str]:
        """Absorb *delta*; return every complete sentence now ready to speak."""
        if self._discard_rest:
            return []
        self.buf = self._remove_complete_protected_blocks(self.buf + delta)
        if self.holding_protected_text:
            return []
        out: List[str] = []
        start = 0  # skip boundaries that would leave the head too short
        while m := SENTENCE_BOUNDARY_RE.search(self.buf, start):
            if self._boundary_is_inside_fenced_code(m.end()):
                start = m.end()
                continue
            head = self.buf[:m.end()]
            if len(head.strip()) < self.min_len or self._substitution_may_cross_boundary(m.end()):
                start = m.end()
                continue
            out.append(head)
            self.buf = self.buf[m.end():]
            start = 0
        return out

    def flush_for_idle(self) -> List[str]:
        """Drain an idle-safe prefix while retaining pronunciation context.

        Pronunciation substitutions use ``(?<!\\w)``.  If an idle flush clears
        a trailing word, the next delta can incorrectly make a source appear to
        start at a word boundary.  Retain that trailing word until the next
        delta (or the explicit end-of-text flush) resolves the boundary.
        """
        if not self._pronunciation_sources:
            return self.flush()

        tail = self._remove_complete_protected_blocks(self.buf)
        self.buf = tail
        if self.holding_protected_text:
            return []
        match = re.search(r"\w+$", tail)
        if match is None:
            self.buf = ""
            return [tail] if tail.strip() else []

        retain_start = match.start()
        # A punctuation-ending source may sit immediately before the trailing
        # word (for example ``C++s``).  Emitting it would invent a false
        # ``(?!\\w)`` boundary.  Pull every such valid source into the retained
        # suffix; iterate for adjacent/overlapping configured sources.
        while True:
            earlier_start: Optional[int] = None
            for source in self._pronunciation_sources:
                source_start = retain_start - len(source)
                if source_start < 0:
                    continue
                if not _literal_ignorecase_matches(
                    tail[source_start:retain_start], source
                ):
                    continue
                if source_start > 0 and self._is_word_char(
                    tail[source_start - 1]
                ):
                    continue
                if earlier_start is None or source_start < earlier_start:
                    earlier_start = source_start
            if earlier_start is None:
                break
            retain_start = earlier_start

        ready = tail[:retain_start]
        self.buf = tail[retain_start:]
        return [ready] if ready.strip() else []

    def flush(self) -> List[str]:
        """Drain the tail (end-of-text or long-idle flush)."""
        tail = self._remove_complete_protected_blocks(self.buf)
        tail = self._strip_open_protected_tail(tail).strip()
        self.buf = ""
        self._discard_rest = False
        return [tail] if tail else []


# ---------------------------------------------------------------------------
# ABC + registry
# ---------------------------------------------------------------------------

class StreamingTTSProvider(ABC):
    """Yields raw int16, little-endian, mono PCM chunks at ``sample_rate``."""

    provider_name: str = ""
    model_id: Optional[str] = None
    sample_rate: int = 24000
    channels: int = 1
    sample_width: int = 2  # bytes/sample (int16)

    def __init__(self, tts_config: Dict, section: Dict):
        self.tts_config = tts_config
        self.section = section

    @staticmethod
    @abstractmethod
    def available() -> bool:
        """True when this provider's credentials/SDK are usable right now."""

    @abstractmethod
    def stream(self, text: str) -> Iterator[bytes]:
        """Yield PCM chunks for ``text``. Raise on failure (caller logs)."""


_REGISTRY: Dict[str, type[StreamingTTSProvider]] = {}


def register(name: str) -> Callable[[type[StreamingTTSProvider]], type[StreamingTTSProvider]]:
    def _wrap(cls: type[StreamingTTSProvider]) -> type[StreamingTTSProvider]:
        cls.provider_name = name
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
_PROVIDER_PRIORITY: List[str] = ["elevenlabs", "gemini", "openai", "xai"]


def resolve_streaming_provider(
    tts_config: Dict,
    preferred: Optional[str] = None,
) -> Optional[StreamingTTSProvider]:
    """Return a ready streamer for the *configured* provider, else ``None``.

    Resolution order:

    1. ``tts.streaming.provider`` (config knob) when set:
       * a provider name pins that exact streamer (or ``None`` if unusable);
       * ``auto`` walks the priority list (``elevenlabs → gemini → openai
         → xai``) and returns the first usable streamer — an explicit
         opt-in to "give me the best chunked voice available".
    2. Otherwise the *configured* TTS provider (or ``preferred`` override).
       ``None`` means "no chunked API for this provider" — the dispatcher
       then speaks per-sentence via the sync path, preserving the user's
       chosen voice. We never silently swap to a different provider just
       to get streaming.
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

    def __init__(self, tts_config: Dict, section: Dict):
        super().__init__(tts_config, section)
        from tools.tts_tool import DEFAULT_ELEVENLABS_STREAMING_MODEL_ID

        self.model_id = str(
            section.get("streaming_model_id")
            or section.get("model_id")
            or DEFAULT_ELEVENLABS_STREAMING_MODEL_ID
        )

    @staticmethod
    def available() -> bool:
        return bool(_resolve_key("ELEVENLABS_API_KEY", "elevenlabs"))

    def stream(self, text: str) -> Iterator[bytes]:
        from tools.tts_tool import (
            DEFAULT_ELEVENLABS_VOICE_ID,
            _elevenlabs_environment_kwargs,
            _import_elevenlabs,
        )

        client = _import_elevenlabs()(
            api_key=_resolve_key("ELEVENLABS_API_KEY", "elevenlabs"),
            **_elevenlabs_environment_kwargs(self.section),
        )
        voice_id = self.section.get("voice_id", DEFAULT_ELEVENLABS_VOICE_ID)
        yield from client.text_to_speech.convert(
            text=text,
            voice_id=voice_id,
            model_id=self.model_id,
            output_format="pcm_24000",
        )


def _openai_config_api_key() -> str:
    """Return ``tts.openai.api_key`` from config.yaml, or empty string."""
    try:
        openai_cfg = (_load_tts_config().get("openai") or {})
    except Exception:
        return ""
    return openai_cfg.get("api_key") or ""


@register("openai")
class OpenAIStreamer(StreamingTTSProvider):
    """OpenAI speech with ``response_format=pcm`` (24 kHz mono int16)."""

    sample_rate = 24000

    @staticmethod
    def available() -> bool:
        return bool(_openai_config_api_key() or resolve_openai_audio_api_key())

    def stream(self, text: str) -> Iterator[bytes]:
        from openai import OpenAI

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
            yield from _capped(response.iter_bytes(), "OpenAI streaming TTS")


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
class GeminiStreamer(StreamingTTSProvider):
    """Gemini ``streamGenerateContent?alt=sse`` → base64 PCM chunks (24 kHz).

    Salvaged from PR #47588 (@Cdddo) and rebased onto the post-campaign
    infrastructure: credentials via the provider-secret resolver, requests
    (not httpx) with a bounded streamed body, and main's provider ABC.
    """

    sample_rate = 24000

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
