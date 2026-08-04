"""
Voice STT Helper for the Telegram adapter
=========================================

Thin wrapper around ``tools.transcription_tools.transcribe_audio`` so the
Telegram adapter can transcribe Telegram voice / audio messages without
importing the heavy STT stack at module load time.

Design notes
------------
* **Lazy import**: ``transcribe_audio`` pulls in ``faster_whisper`` /
  ``openai-whisper`` (both ~hundreds of MB of deps). We defer that import
  until first use so a Telegram adapter that never sees voice messages
  stays light.
* **Failure-tolerant**: every call path is wrapped. The Telegram adapter
  should treat STT as best-effort — if it fails, the cached audio is
  still attached and the user-visible flow degrades to "audio file with
  no transcript", not a hard error.
* **No pydub dependency**: the upstream ``transcribe_audio`` already
  accepts OGG/Opus (Telegram's native voice format) via ffmpeg, so we
  do not duplicate decode logic.
* **Config-driven provider**: the active provider comes from
  ``stt.provider`` in ``~/.hermes/config.yaml`` — we never hard-code
  ``faster_whisper`` here.

Usage
-----
::

    from plugins.platforms.telegram.stt_helper import VoiceSTTHelper

    helper = VoiceSTTHelper()                    # lazy, no STT load yet
    result = helper.transcribe("/tmp/x.ogg")     # first call may load Whisper
    if result.success:
        note = helper.format_note(result.transcript)
        event.text = f"{event.text}\\n\\n{note}" if event.text else note

Configuration (in ``~/.hermes/config.yaml``)::

    stt:
      enabled: true
      provider: local            # or: openai, groq, mistral, xai, elevenlabs
      local:
        model: base              # tiny/base/small/medium/large-v3
        language: de
"""

from __future__ import annotations

import logging
import os
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

log = logging.getLogger("telegram.voice_stt")


@dataclass
class TranscriptionResult:
    """Outcome of a single transcribe attempt.

    Attributes:
        success: True if a non-empty transcript was produced.
        transcript: The transcribed text. Empty on failure.
        provider: Name of the STT backend that handled the call (e.g.
            "local", "openai"). ``None`` if the call never reached a
            provider (e.g. STT disabled in config).
        error: Human-readable failure reason. ``None`` on success.
        duration_seconds: Wall-clock time spent transcribing.
        audio_path: Absolute path to the cached audio file we transcribed.
    """

    success: bool
    transcript: str
    provider: Optional[str] = None
    error: Optional[str] = None
    duration_seconds: float = 0.0
    audio_path: Optional[str] = None

    @property
    def text(self) -> str:
        """Alias for ``transcript`` — easier on the eyes at call sites."""
        return self.transcript


class VoiceSTTHelper:
    """Lazy, thread-safe STT helper backed by ``transcribe_audio``.

    The first ``transcribe()`` call pays the import cost (Whisper model
    load is handled by ``transcribe_audio`` itself on first dispatch).
    Subsequent calls reuse the already-loaded stack.

    Constructor args are kept around as defaults — runtime overrides
    always come from ``~/.hermes/config.yaml`` so the helper stays
    aligned with the rest of the agent.
    """

    # Sentinels — we never hard-code model names here; the canonical
    # ``transcribe_audio`` reads them from config.
    _DEFAULT_MODEL: Optional[str] = None

    def __init__(
        self,
        model_name: Optional[str] = None,
        language: Optional[str] = None,
        max_audio_seconds: int = 180,
    ) -> None:
        self.model_name = model_name or self._DEFAULT_MODEL or os.getenv(
            "TELEGRAM_STT_MODEL"
        )
        self.language = language or os.getenv("TELEGRAM_STT_LANGUAGE")
        self.max_audio_seconds = max_audio_seconds
        self._lock = threading.Lock()
        self._transcribe_fn = None  # resolved on first transcribe()

    # ------------------------------------------------------------------ #
    # Lazy import                                                        #
    # ------------------------------------------------------------------ #

    def _resolve_transcribe(self):
        """Resolve and cache the ``transcribe_audio`` callable.

        Returns ``None`` if the upstream STT stack is missing — every
        caller must handle ``None`` and degrade gracefully.
        """
        if self._transcribe_fn is not None:
            return self._transcribe_fn
        with self._lock:
            if self._transcribe_fn is not None:
                return self._transcribe_fn
            try:
                from tools.transcription_tools import transcribe_audio

                self._transcribe_fn = transcribe_audio
                log.debug("Resolved transcribe_audio: %s", transcribe_audio)
            except Exception as e:
                # ImportError, sys.path issues, broken venv — none of these
                # should take down the Telegram adapter.
                log.warning(
                    "transcribe_audio unavailable — voice STT disabled (%s: %s)",
                    type(e).__name__,
                    e,
                )
                self._transcribe_fn = None
            return self._transcribe_fn

    # ------------------------------------------------------------------ #
    # Public API                                                         #
    # ------------------------------------------------------------------ #

    def is_configured(self) -> bool:
        """Return True if upstream STT is importable.

        Cheap probe — does NOT load any Whisper model. Use this in the
        adapter to decide whether to attempt transcription at all.
        """
        return self._resolve_transcribe() is not None

    def transcribe(
        self,
        audio_path: str,
        *,
        model: Optional[str] = None,
    ) -> TranscriptionResult:
        """Transcribe ``audio_path`` (OGG/Opus, MP3, WAV, …).

        Args:
            audio_path: Absolute path to the cached audio file.
            model: Optional Whisper-size override. ``None`` → use the
                model from ``stt.local.model`` in config.

        Returns:
            ``TranscriptionResult`` — never raises. On any failure,
            ``success`` is False and ``error`` holds the reason.
        """
        import time as _time

        path = Path(audio_path)
        if not path.exists():
            return TranscriptionResult(
                success=False,
                transcript="",
                error=f"Audio file missing: {audio_path}",
                audio_path=str(path),
            )
        if path.stat().st_size < 256:
            # Telegram sometimes delivers near-empty voice notes (the user
            # recorded silence). Skip — would just waste a model call.
            return TranscriptionResult(
                success=False,
                transcript="",
                error=f"Audio too small ({path.stat().st_size} B) — likely silence",
                audio_path=str(path),
            )

        transcribe = self._resolve_transcribe()
        if transcribe is None:
            return TranscriptionResult(
                success=False,
                transcript="",
                error="STT stack unavailable (tools.transcription_tools not importable)",
                audio_path=str(path),
            )

        start = _time.monotonic()
        try:
            effective_model = model or self.model_name
            raw = transcribe(str(path), model=effective_model)
        except Exception as e:
            log.warning("transcribe_audio raised: %s: %s", type(e).__name__, e)
            return TranscriptionResult(
                success=False,
                transcript="",
                error=f"{type(e).__name__}: {e}",
                audio_path=str(path),
                duration_seconds=_time.monotonic() - start,
            )

        duration = _time.monotonic() - start

        if not isinstance(raw, dict):
            return TranscriptionResult(
                success=False,
                transcript="",
                error=f"Unexpected transcribe_audio return type: {type(raw).__name__}",
                audio_path=str(path),
                duration_seconds=duration,
            )

        success = bool(raw.get("success"))
        transcript = (raw.get("transcript") or "").strip()
        provider = raw.get("provider")
        error = raw.get("error")

        if not success or not transcript:
            return TranscriptionResult(
                success=False,
                transcript=transcript,
                provider=provider,
                error=error or "Empty transcript",
                audio_path=str(path),
                duration_seconds=duration,
            )

        log.info(
            "Transcribed %s (%.2fs, %d chars, provider=%s)",
            path.name,
            duration,
            len(transcript),
            provider,
        )
        return TranscriptionResult(
            success=True,
            transcript=transcript,
            provider=provider,
            audio_path=str(path),
            duration_seconds=duration,
        )

    # ------------------------------------------------------------------ #
    # Presentation                                                       #
    # ------------------------------------------------------------------ #

    @staticmethod
    def format_note(transcript: str, *, marker: str = "🎤") -> str:
        """Wrap a transcript in a marker line for ``event.text`` injection.

        Example output::

            🎤 Voice-Transkript:
            Hallo Yuno, ich bin Basti.

        The note is intended to be appended via ``_append_observed_note``
        so the agent sees the transcript in its conversation context
        while still having ``event.media_urls`` populated for tools that
        want the raw audio.
        """
        text = (transcript or "").strip()
        if not text:
            return ""
        return f"{marker} Voice-Transkript:\n{text}"


# ---------------------------------------------------------------------- #
# Module-level singleton — the adapter uses this so the Whisper model   #
# is loaded at most once across the gateway lifetime.                    #
# ---------------------------------------------------------------------- #

_singleton: Optional[VoiceSTTHelper] = None
_singleton_lock = threading.Lock()


def get_voice_stt_helper() -> VoiceSTTHelper:
    """Return the process-wide VoiceSTTHelper singleton."""
    global _singleton
    if _singleton is not None:
        return _singleton
    with _singleton_lock:
        if _singleton is None:
            _singleton = VoiceSTTHelper()
    return _singleton