"""
Smoke test for VoiceSTTHelper
==============================

Exercises:
  * lazy singleton resolution (does NOT load Whisper if STT is off)
  * graceful failure on missing file
  * graceful failure on too-small (silent) audio
  * real transcription on a synthetic 3-second German-tone OGG
  * the format_note() presentation helper

Usage:
  /home/bratan/.hermes/hermes-agent/venv/bin/python tests/telegram_voice_stt_smoke.py

Exits 0 on full success, 1 on any failure.
"""

from __future__ import annotations

import os
import subprocess
import sys
import tempfile
from pathlib import Path

# Make sure the agent package is importable when this file is run directly.
HERE = Path(__file__).resolve().parent
ROOT = HERE.parent  # /home/bratan/.hermes/hermes-agent
sys.path.insert(0, str(ROOT))
os.chdir(ROOT)

FAILED = []


def step(label: str, ok: bool, detail: str = "") -> None:
    mark = "PASS" if ok else "FAIL"
    print(f"[{mark}] {label}{(' — ' + detail) if detail else ''}")
    if not ok:
        FAILED.append(label)


def main() -> int:
    # ------------------------------------------------------------------
    # 1. Importable — adapter must be able to `from plugins.platforms
    #    .telegram.stt_helper import get_voice_stt_helper`.
    # ------------------------------------------------------------------
    try:
        from plugins.platforms.telegram.stt_helper import (
            VoiceSTTHelper,
            TranscriptionResult,
            get_voice_stt_helper,
        )
        step("import VoiceSTTHelper", True)
    except Exception as e:
        step("import VoiceSTTHelper", False, f"{type(e).__name__}: {e}")
        return 1

    helper = get_voice_stt_helper()
    step("singleton returns same instance",
         get_voice_stt_helper() is helper)

    # ------------------------------------------------------------------
    # 2. Configured probe — does NOT load the model.
    # ------------------------------------------------------------------
    cfg_ok = helper.is_configured()
    step("is_configured() returns bool", isinstance(cfg_ok, bool),
         f"value={cfg_ok}")
    if not cfg_ok:
        print("  STT stack not importable in this venv — skipping real "
              "transcribe stage. Pipeline still degrades gracefully.")
    else:
        # ------------------------------------------------------------------
        # 3. Missing file → graceful failure.
        # ------------------------------------------------------------------
        r = helper.transcribe("/tmp/this-file-does-not-exist.ogg")
        step("missing file → success=False",
             r.success is False and "missing" in (r.error or "").lower(),
             f"error={r.error!r}")

        # ------------------------------------------------------------------
        # 4. Too-small file → graceful failure (no model call).
        # ------------------------------------------------------------------
        tiny = Path(tempfile.mkstemp(suffix=".ogg")[1])
        tiny.write_bytes(b"\x00" * 100)  # 100 bytes — silence-like
        r = helper.transcribe(str(tiny))
        step("too-small file → success=False",
             r.success is False and "small" in (r.error or "").lower(),
             f"error={r.error!r}")
        tiny.unlink(missing_ok=True)

        # ------------------------------------------------------------------
        # 5. Real OGG/Opus (Telegram's native voice format) via ffmpeg.
        #    We synthesise real speech with edge-tts (already installed in
        #    the hermes venv) so Whisper has something to recognise — a
        #    pure sine tone gives empty transcripts which proves nothing.
        # ------------------------------------------------------------------
        wav = Path(tempfile.mkstemp(suffix=".wav")[1])
        mp3 = Path(tempfile.mkstemp(suffix=".mp3")[1])
        ogg = Path(tempfile.mkstemp(suffix=".ogg")[1])
        speech_text = "Hallo Yuno, das ist ein Stimm Test für die Spracherkennung."
        try:
            edge_tts_ok = False
            try:
                r_tts = subprocess.run(
                    ["edge-tts", "--voice", "de-DE-KatjaNeural",
                     "--text", speech_text, "--write-media", str(mp3)],
                    capture_output=True, text=True, timeout=30,
                )
                edge_tts_ok = r_tts.returncode == 0 and mp3.stat().st_size > 1024
            except Exception as e:
                print(f"  edge-tts unavailable: {e}")

            if edge_tts_ok:
                step("edge-tts generated real speech MP3", True,
                     f"size={mp3.stat().st_size} B")
                subprocess.run(
                    ["ffmpeg", "-y", "-i", str(mp3),
                     "-c:a", "libopus", "-b:a", "32k", str(ogg)],
                    capture_output=True, check=True,
                )
                step("ffmpeg re-encoded MP3 → OGG/Opus (Telegram format)",
                     ogg.stat().st_size > 1024,
                     f"size={ogg.stat().st_size} B")
                expects_success = True
            else:
                # Fall back to sine tone (will legitimately produce no
                # transcript, but proves the pipeline runs end-to-end).
                print("  edge-tts unavailable — falling back to sine tone")
                subprocess.run(
                    ["ffmpeg", "-y", "-f", "lavfi",
                     "-i", "sine=frequency=440:duration=3",
                     "-ar", "16000", "-ac", "1", str(ogg)],
                    capture_output=True, check=True,
                )
                expects_success = False

            r = helper.transcribe(str(ogg))
            # A 3 s sine tone is not speech, so an empty transcript is the
            # *expected* outcome. What we are proving here is that the
            # pipeline runs end-to-end: provider resolved, ffmpeg decoded
            # the OGG/Opus, Whisper ran, and the result came back structured.
            step("real OGG transcription returns a result object",
                 isinstance(r, TranscriptionResult),
                 f"type={type(r).__name__}")
            step("real OGG transcription completes (no exception)",
                 r.duration_seconds > 0,
                 f"duration={r.duration_seconds:.2f}s")
            step("provider is non-empty after a real call",
                 r.provider not in (None, ""),
                 f"provider={r.provider!r}")
            # On pure-tone input we expect success=False with error set.
            # With edge-tts speech we expect success=True and a transcript
            # containing at least one keyword from the speech text.
            if expects_success:
                step("real speech produced a transcript",
                     r.success is True and len(r.transcript or "") > 0,
                     f"transcript={r.transcript!r}")
                # Don't over-assert: Whisper capitalisation / punctuation
                # normalisation is fine, just look for any keyword.
                haystack = (r.transcript or "").lower()
                keywords = ["hallo", "yuno", "test", "stimm", "die"]
                hit = [k for k in keywords if k in haystack]
                step("transcript contains expected keywords",
                     len(hit) >= 1,
                     f"hits={hit!r} transcript={r.transcript!r}")
            else:
                step("real OGG reports honest 'empty/no speech' result",
                     r.success is False and r.error is not None,
                     f"success={r.success} error={r.error!r}")
        finally:
            wav.unlink(missing_ok=True)
            mp3.unlink(missing_ok=True)
            ogg.unlink(missing_ok=True)

    # ------------------------------------------------------------------
    # 6. format_note() — pure presentation, always available.
    # ------------------------------------------------------------------
    note = VoiceSTTHelper.format_note("Hallo Yuno, das ist ein Test.")
    step("format_note wraps transcript",
         note.startswith("🎤") and "Hallo Yuno" in note,
         repr(note)[:80])
    step("format_note empty input → empty string",
         VoiceSTTHelper.format_note("") == "")
    step("format_note whitespace-only → empty string",
         VoiceSTTHelper.format_note("   \n  ") == "")

    # ------------------------------------------------------------------
    print()
    if FAILED:
        print(f"FAILED ({len(FAILED)}/{len(FAILED) + sum(1 for _ in [])})")
        for f in FAILED:
            print(f"  - {f}")
        return 1
    print("ALL CHECKS PASSED")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())