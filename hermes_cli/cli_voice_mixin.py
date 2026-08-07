"""Voice-mode handlers for the interactive CLI (god-file decomposition Wave 1).

This module hosts the ``_voice_*`` voice-mode methods lifted out of
``cli.py``'s ``HermesCLI`` class (shard s4, cluster c6). ``HermesCLI``
inherits ``CLIVoiceMixin`` so every ``self.<method>`` call resolves unchanged
via the MRO — behavior-neutral.

Import discipline (mirrors ``hermes_cli/cli_commands_mixin.py``, the accepted
Phase-4 decomposition):
  * Neutral, non-cyclic deps are imported at module top-level below.
  * cli.py-internal symbols (``_cprint``/``_DIM``/``_RST``/``_ACCENT``/
    ``_BOLD``/``logger``/``_VoiceInputMessage``) are imported LAZILY inside
    each method via ``from cli import ...`` — that resolves at call time when
    ``cli`` is fully loaded, so this module never imports ``cli`` at top
    level (no cycle).
"""

from __future__ import annotations

import json
import os
import re
import sys
import tempfile
import threading
import time
from typing import Optional

from hermes_constants import is_termux as _is_termux_environment


class CLIVoiceMixin:
    # ====================================================================
    # Voice mode methods
    # ====================================================================

    def _voice_start_recording(self):
        """Start capturing audio from the microphone."""
        from cli import _ACCENT, _cprint, _DIM, _RST
        if getattr(self, '_should_exit', False):
            return
        from tools.voice_mode import create_audio_recorder, check_voice_requirements

        reqs = check_voice_requirements()
        if not reqs["audio_available"]:
            if _is_termux_environment():
                details = reqs.get("details", "")
                if "Termux:API Android app is not installed" in details:
                    raise RuntimeError(
                        "Termux:API command package detected, but the Android app is missing.\n"
                        "Install/update the Termux:API Android app, then retry /voice on.\n"
                        "Fallback: pkg install python-numpy portaudio && python -m pip install sounddevice"
                    )
                raise RuntimeError(
                    "Voice mode requires either Termux:API microphone access or Python audio libraries.\n"
                    "Option 1: pkg install termux-api and install the Termux:API Android app\n"
                    "Option 2: pkg install python-numpy portaudio && python -m pip install sounddevice"
                )
            raise RuntimeError(
                "Voice mode requires sounddevice and numpy.\n"
                f"Install with: {sys.executable} -m pip install sounddevice numpy"
            )
        if not reqs.get("stt_available", reqs.get("stt_key_set")):
            raise RuntimeError(
                "Voice mode requires an STT provider for transcription.\n"
                "Option 1: uv pip install faster-whisper  "
                "(free, local; `pip install faster-whisper` also works if pip is on PATH)\n"
                "Option 2: Set GROQ_API_KEY (free tier)\n"
                "Option 3: Set VOICE_TOOLS_OPENAI_KEY (paid)"
            )

        # Prevent double-start from concurrent threads (atomic check-and-set)
        with self._voice_lock:
            if self._voice_recording:
                return
            self._voice_recording = True

        # Load silence detection params from config. Shape-safe: a
        # hand-edited ``voice: true`` / ``voice: cmd+b`` leaves
        # ``load_config()['voice']`` as a non-dict; coerce to {} so
        # continuous recording falls back to the documented defaults
        # instead of crashing on ``.get()``.
        voice_cfg: dict = {}
        try:
            from hermes_cli.config import load_config
            _cfg = load_config().get("voice")
            voice_cfg = _cfg if isinstance(_cfg, dict) else {}
        except Exception:
            pass

        # Recorder creation can fail (no input device, PortAudio init error).
        # Reset the flag on failure or _voice_recording stays True forever and
        # every future voice start is silently skipped by the guard above.
        if self._voice_recorder is None:
            try:
                self._voice_recorder = create_audio_recorder()
            except Exception:
                with self._voice_lock:
                    self._voice_recording = False
                raise

        # Apply config-driven silence params (numeric-guarded so YAML
        # scalar corruption doesn't break recording start-up).
        #
        # ``bool`` is explicitly excluded from the numeric check — in
        # Python bool is a subclass of int, so a hand-edited
        # ``silence_threshold: true`` would otherwise be forwarded as
        # ``1`` instead of falling back to the 200 default (Copilot
        # round-12 on #19835).
        _threshold = voice_cfg.get("silence_threshold")
        _duration = voice_cfg.get("silence_duration")
        self._voice_recorder._silence_threshold = (
            _threshold if isinstance(_threshold, (int, float)) and not isinstance(_threshold, bool) else 200
        )
        self._voice_recorder._silence_duration = (
            _duration if isinstance(_duration, (int, float)) and not isinstance(_duration, bool) else 3.0
        )
        # voice.max_recording_seconds — hard cap on a single recording's length.
        # Same numeric guard as the silence params (bool excluded: a hand-edited
        # ``max_recording_seconds: true`` must not become ``1`` — it falls back
        # to the documented 120 default, mirroring the silence-param handling).
        # An explicit numeric value <= 0 disables the cap. Previously this
        # documented key was never read (dead config); wiring it here makes it
        # take effect.
        _max_rec = voice_cfg.get("max_recording_seconds")
        self._voice_recorder._max_recording_seconds = (
            (_max_rec if _max_rec > 0 else 0.0)
            if isinstance(_max_rec, (int, float)) and not isinstance(_max_rec, bool)
            else 120.0
        )

        def _on_silence():
            """Called by AudioRecorder when silence is detected after speech."""
            with self._voice_lock:
                if not self._voice_recording:
                    return
            _cprint(f"\n{_DIM}Silence detected, auto-stopping...{_RST}")
            if hasattr(self, '_app') and self._app:
                self._app.invalidate()
            self._voice_stop_and_transcribe()

        # Audio cue: single beep BEFORE starting stream (avoid CoreAudio conflict)
        if self._voice_beeps_enabled():
            try:
                from tools.voice_mode import play_beep
                play_beep(frequency=880, count=1)
            except Exception:
                pass

        try:
            self._voice_recorder.start(on_silence_stop=_on_silence)
        except Exception:
            with self._voice_lock:
                self._voice_recording = False
            raise
        _label = self._voice_record_key_label()
        if getattr(self._voice_recorder, "supports_silence_autostop", True):
            _recording_hint = f"auto-stops on silence | {_label} to stop & exit continuous"
        elif _is_termux_environment():
            _recording_hint = f"Termux:API capture | {_label} to stop"
        else:
            _recording_hint = f"{_label} to stop"
        _cprint(f"\n{_ACCENT}● Recording...{_RST} {_DIM}({_recording_hint}){_RST}")

        # Periodically refresh prompt to update audio level indicator
        def _refresh_level():
            while True:
                with self._voice_lock:
                    still_recording = self._voice_recording
                if not still_recording:
                    break
                if hasattr(self, '_app') and self._app:
                    self._app.invalidate()
                time.sleep(0.15)
        threading.Thread(target=_refresh_level, daemon=True).start()

    def _voice_stt_model(self) -> Optional[str]:
        """STT model override from config, or None for the provider default.

        For the local provider, prefer stt.local.model (default ``base``) so the
        CLI passes a real model name into the local STT backend.
        """
        try:
            from hermes_cli.config import load_config
            stt_config = load_config().get("stt", {})
            if not isinstance(stt_config, dict):
                return None
            provider = str(stt_config.get("provider") or "").strip().lower()
            if provider == "local":
                local_config = stt_config.get("local") or {}
                if not isinstance(local_config, dict):
                    local_config = {}
                return local_config.get("model") or "base"
            return stt_config.get("model")
        except Exception:
            return None

    def _voice_stt_provider(self) -> str:
        """Configured STT provider name (lowercased), or empty string."""
        try:
            from hermes_cli.config import load_config
            stt_config = load_config().get("stt", {})
            if not isinstance(stt_config, dict):
                return ""
            return str(stt_config.get("provider") or "").strip().lower()
        except Exception:
            return ""

    def _voice_restart_recording_async(self) -> None:
        """Restart continuous-mode recording off-thread (start() can block)."""
        from cli import _cprint, _DIM, _RST
        def _restart_recording():
            try:
                self._voice_start_recording()
                if hasattr(self, '_app') and self._app:
                    self._app.invalidate()
            except Exception as e:
                _cprint(f"{_DIM}Voice auto-restart failed: {e}{_RST}")
        threading.Thread(target=_restart_recording, daemon=True).start()

    def _voice_stop_and_transcribe(self):
        """Stop recording, transcribe via STT, and queue the transcript as input."""
        from cli import _VoiceInputMessage, _cprint, _DIM, _RST
        # Atomic guard: only one thread can enter stop-and-transcribe.
        # Set _voice_processing immediately so concurrent Ctrl+B presses
        # don't race into the START path while recorder.stop() holds its lock.
        with self._voice_lock:
            if not self._voice_recording:
                return
            self._voice_recording = False
            self._voice_processing = True

        submitted = False
        transcription_failed = False
        wav_path = None
        try:
            if self._voice_recorder is None:
                return

            wav_path = self._voice_recorder.stop()

            # Audio cue: double beep after stream stopped (no CoreAudio conflict)
            if self._voice_beeps_enabled():
                try:
                    from tools.voice_mode import play_beep
                    play_beep(frequency=660, count=2)
                except Exception:
                    pass

            if wav_path is None:
                _cprint(f"{_DIM}No speech detected.{_RST}")
                return

            # _voice_processing is already True (set atomically above)
            if hasattr(self, '_app') and self._app:
                self._app.invalidate()

            stt_model = self._voice_stt_model()
            if self._voice_stt_provider() == "local":
                _cprint(
                    f"{_DIM}Preparing local STT model '{stt_model}' "
                    f"(first use may download it from Hugging Face)...{_RST}"
                )
            else:
                _cprint(f"{_DIM}Transcribing...{_RST}")

            from tools.voice_mode import transcribe_recording
            result = transcribe_recording(wav_path, model=stt_model)

            if result.get("success") and result.get("transcript", "").strip():
                transcript = result["transcript"].strip()
                from tools.voice_mode import is_voice_stop_phrase
                if is_voice_stop_phrase(transcript):
                    # Bare "stop" (or configured phrase) ends the voice chat
                    # instead of being sent to the agent.
                    _cprint(f"{_DIM}Stop phrase detected — ending voice chat.{_RST}")
                    self._disable_voice_mode()
                    return
                self._attached_images.clear()
                if hasattr(self, '_app') and self._app:
                    self._app.invalidate()
                self._pending_input.put(_VoiceInputMessage(transcript))
                submitted = True
            elif result.get("success"):
                _cprint(f"{_DIM}No speech detected.{_RST}")
            else:
                error = result.get("error", "Unknown error")
                _cprint(f"\n{_DIM}Transcription failed: {error}{_RST}")
                transcription_failed = True

        except Exception as e:
            _cprint(f"\n{_DIM}Voice processing error: {e}{_RST}")
            transcription_failed = wav_path is not None
        finally:
            with self._voice_lock:
                self._voice_processing = False
            if hasattr(self, '_app') and self._app:
                self._app.invalidate()
            # Clean up temp file unless transcription failed. On failure, keep
            # the source recording so long dictation is not lost.
            try:
                if wav_path and os.path.isfile(wav_path):
                    if transcription_failed:
                        _cprint(f"{_DIM}Recording preserved at: {wav_path}{_RST}")
                    else:
                        os.unlink(wav_path)
            except Exception:
                pass

            # Track consecutive no-speech cycles to avoid infinite restart loops.
            # While the agent is mid-turn or TTS is speaking, the user is
            # CORRECTLY silent (waiting/listening) — those cycles must not
            # count, or a multi-minute tool run ends the voice chat under
            # the user. The stop phrase and barge-in still work during the
            # hold (they run on their own paths above).
            stop_continuous_restart = False
            _tts_done = getattr(self, "_voice_tts_done", None)
            _activity_hold = bool(
                getattr(self, "_agent_running", False)
                or (_tts_done is not None and not _tts_done.is_set())
            )
            if not submitted:
                if _activity_hold:
                    pass  # held: keep listening without counting the cycle
                else:
                    self._no_speech_count = getattr(self, '_no_speech_count', 0) + 1
                    if self._no_speech_count >= 3:
                        self._voice_continuous = False
                        self._no_speech_count = 0
                        _cprint(f"{_DIM}No speech detected 3 times, continuous mode stopped.{_RST}")
                        stop_continuous_restart = True
            else:
                self._no_speech_count = 0

            # If no transcript was submitted but continuous mode is active,
            # restart recording so the user can keep talking.
            # (When transcript IS submitted, process_loop handles restart
            # after chat() completes.)
            if (
                self._voice_continuous
                and not submitted
                and not self._voice_recording
                and not stop_continuous_restart
            ):
                self._voice_restart_recording_async()

    def _voice_speak_response_async(self, text: str) -> None:
        """Schedule TTS and mark it pending before continuous recording can restart."""
        if not self._voice_tts or not text:
            return
        self._voice_tts_done.clear()
        threading.Thread(
            target=self._voice_speak_response,
            args=(text,),
            daemon=True,
        ).start()
        # Spoken barge-in must work on the whole-file fallback path too. The
        # full-duplex agent-turn listener normally already covers playback
        # (armed at turn start in chat()); this arm is an idempotent safety
        # net for speak calls outside a chat turn — the listener refuses to
        # double-arm via _voice_fd_active.
        if self._voice_continuous:
            threading.Thread(
                target=self._voice_full_duplex_listener,
                daemon=True,
            ).start()

    def _voice_speak_response(self, text: str):
        """Speak the agent's response aloud using TTS (runs in background thread)."""
        from cli import _cprint, _DIM, _RST, logger
        if not self._voice_tts:
            return
        self._voice_tts_done.clear()
        try:
            from tools.tts_tool import text_to_speech_tool
            from tools.voice_mode import play_audio_file

            # Strip markdown and non-speech content for cleaner TTS via the
            # shared cleaner (tools/tts_text_normalize): markdown, emoji,
            # <think> blocks, verifier footer, units, newline flattening.
            try:
                from tools.tts_text_normalize import prepare_spoken_text
                tts_text = prepare_spoken_text(text, max_chars=4000)
            except Exception:
                # Legacy fallback pipeline — keep voice replies best-effort.
                tts_text = text[:4000] if len(text) > 4000 else text
                tts_text = re.sub(r'```[\s\S]*?```', ' ', tts_text)   # fenced code blocks
                tts_text = re.sub(r'\[([^\]]+)\]\([^)]+\)', r'\1', tts_text)  # [text](url) -> text
                tts_text = re.sub(r'https?://\S+', '', tts_text)      # URLs
                tts_text = re.sub(r'\*\*(.+?)\*\*', r'\1', tts_text)  # bold
                tts_text = re.sub(r'\*(.+?)\*', r'\1', tts_text)      # italic
                tts_text = re.sub(r'`(.+?)`', r'\1', tts_text)        # inline code
                tts_text = re.sub(r'^#+\s*', '', tts_text, flags=re.MULTILINE)  # headers
                tts_text = re.sub(r'^\s*[-*]\s+', '', tts_text, flags=re.MULTILINE)  # list items
                tts_text = re.sub(r'---+', '', tts_text)              # horizontal rules
                tts_text = re.sub(r'\n{3,}', '\n\n', tts_text)        # excessive newlines
                tts_text = tts_text.strip()
            if not tts_text:
                return

            # Use MP3 output for CLI playback (afplay doesn't handle OGG well).
            # The TTS tool may auto-convert MP3->OGG, but the original MP3 remains.
            os.makedirs(os.path.join(tempfile.gettempdir(), "hermes_voice"), exist_ok=True)
            mp3_path = os.path.join(
                tempfile.gettempdir(), "hermes_voice",
                f"tts_{time.strftime('%Y%m%d_%H%M%S')}.mp3",
            )

            raw_result = text_to_speech_tool(text=tts_text, output_path=mp3_path)
            try:
                tts_result = json.loads(raw_result) if isinstance(raw_result, str) else {}
            except Exception:
                tts_result = {}

            # Prefer the requested MP3 when the provider produced it. This
            # preserves reliable local playback while still supporting
            # providers that write to and return a different path.
            audio_path = mp3_path
            if not os.path.isfile(mp3_path) or os.path.getsize(mp3_path) == 0:
                audio_path = tts_result.get("file_path") or mp3_path

            if os.path.isfile(audio_path) and os.path.getsize(audio_path) > 0:
                play_audio_file(audio_path)
                # Clean up
                try:
                    cleanup_paths = {audio_path, mp3_path}
                    for path in list(cleanup_paths):
                        ogg_path = path.rsplit(".", 1)[0] + ".ogg"
                        cleanup_paths.add(ogg_path)
                    for path in cleanup_paths:
                        if os.path.isfile(path):
                            os.unlink(path)
                except OSError:
                    pass
        except Exception as e:
            logger.warning("Voice TTS playback failed: %s", e)
            _cprint(f"{_DIM}TTS playback failed: {e}{_RST}")
        finally:
            self._voice_tts_done.set()


    def _voice_full_duplex_listener(self) -> None:
        """Full-duplex agent-turn listener: mic live for the WHOLE turn.

        Armed at utterance-submit (chat() start in continuous voice mode) and
        disarmed when the turn is fully done (agent finished + TTS played).
        Replaces the old per-playback ``_voice_barge_in_monitor``, which only
        listened while TTS audio was playing — during LLM generation the mic
        was dead, so the user could not interject by voice at all (and the
        playback monitor calibrated against its own speaker bleed, making
        the trigger unreachable; see tools.voice_mode.full_duplex_listen).

        Phase behaviour:

        * generation (no TTS audio yet): speech interrupts the in-flight
          agent turn via ``self.agent.interrupt()`` — the same seam the
          typed/Ctrl+C interrupt uses — and the captured utterance is
          submitted as the next message.
        * playback: speech cuts TTS (pipeline stop event + stop_playback)
          and the interruption is captured with pre-roll and submitted.

        The stop phrase ends the voice chat in BOTH phases (a stop during
        generation means "stop everything": the turn is already interrupted
        at trip time, then ``_voice_submit_barge_utterance`` disables voice
        mode).
        """
        from cli import _cprint, _DIM, _RST, logger
        fd_active = getattr(self, "_voice_fd_active", None)
        if fd_active is None:
            fd_active = threading.Event()
            self._voice_fd_active = fd_active
        if fd_active.is_set():
            return  # one listener owns the mic for this turn
        fd_active.set()
        try:
            from hermes_cli.config import load_config
            voice_cfg = load_config().get("voice") or {}
            if not (isinstance(voice_cfg, dict) and voice_cfg.get("barge_in", True)):
                return
            from tools.voice_mode import (
                full_duplex_listen,
                is_audio_output_active,
                stop_playback,
            )

            try:
                _mult = float(voice_cfg.get("barge_in_threshold_multiplier", 0) or 0)
            except (TypeError, ValueError):
                _mult = 0.0
            try:
                _grace_ms = int(float(voice_cfg.get("barge_in_grace_seconds", 0.5)) * 1000)
            except (TypeError, ValueError):
                _grace_ms = 500

            tts_done = getattr(self, "_voice_tts_done", None)

            def _should_stop() -> bool:
                if not (getattr(self, "_voice_mode", False) and getattr(self, "_voice_continuous", False)):
                    return True
                if getattr(self, "_agent_running", False):
                    return False
                # Agent finished — keep listening until TTS fully played.
                if tts_done is not None and not tts_done.is_set():
                    return False
                return not is_audio_output_active()

            def _on_trigger(phase: str) -> None:
                # Latch BEFORE cutting anything: suppresses process_loop's
                # auto-restart until the capture is submitted.
                self._voice_barge_capture.set()
                if phase == "playback":
                    logger.debug(
                        "TTS CUT: full-duplex listener tripped during playback"
                    )
                    from tools.tts_streaming import mark_speech_interrupted
                    mark_speech_interrupted()
                    _pipe_stop = getattr(self, "_voice_tts_stop", None)
                    if _pipe_stop is not None:
                        _pipe_stop.set()
                    stop_playback()
                else:
                    # Generation phase: no audio to cut — interrupt the
                    # in-flight agent turn (same seam as typed interrupt).
                    logger.debug(
                        "full-duplex listener tripped during generation — "
                        "interrupting agent turn"
                    )
                    _pipe_stop = getattr(self, "_voice_tts_stop", None)
                    if _pipe_stop is not None:
                        _pipe_stop.set()  # never let the stale reply speak
                    try:
                        if self.agent is not None and getattr(self, "_agent_running", False):
                            _cprint(f"\n{_DIM}🎤 Voice interjection — interrupting…{_RST}")
                            self.agent.interrupt()
                    except Exception as e:
                        logger.debug("voice interjection interrupt failed: %s", e)

            wav_path = full_duplex_listen(
                _should_stop,
                is_playing=is_audio_output_active,
                on_trigger=_on_trigger,
                multiplier=_mult or None,
                grace_ms=max(0, _grace_ms),
            )
            if wav_path and self._voice_barge_capture.is_set():
                self._voice_submit_barge_utterance(wav_path)
            else:
                self._voice_barge_capture.clear()
        except Exception as e:
            self._voice_barge_capture.clear()
            logger.debug("Voice full-duplex listener failed: %s", e)
        finally:
            fd_active.clear()

    def _voice_submit_barge_utterance(self, wav_path: str) -> None:
        """Transcribe a barge-captured interruption and queue it as the next turn."""
        from cli import _VoiceInputMessage, _cprint, _DIM, _RST
        submitted = False
        try:
            from tools.voice_mode import transcribe_recording
            result = transcribe_recording(wav_path, model=self._voice_stt_model())
            transcript = (result.get("transcript") or "").strip() if result.get("success") else ""
            if transcript:
                from tools.voice_mode import is_voice_stop_phrase
                if is_voice_stop_phrase(transcript):
                    _cprint(f"\n{_DIM}Stop phrase detected — ending voice chat.{_RST}")
                    self._disable_voice_mode()
                    return
                self._pending_input.put(_VoiceInputMessage(transcript))
                submitted = True
            elif not result.get("success"):
                _cprint(f"\n{_DIM}Transcription failed: {result.get('error', 'Unknown error')}{_RST}")
        except Exception as e:
            _cprint(f"\n{_DIM}Voice processing error: {e}{_RST}")
        finally:
            try:
                if os.path.isfile(wav_path):
                    os.unlink(wav_path)
            except OSError:
                pass
            self._voice_barge_capture.clear()
            # No usable transcript: hand the mic back to the normal loop.
            if not submitted and self._voice_mode and self._voice_continuous and not self._voice_recording:
                self._voice_restart_recording_async()

    def _voice_beeps_enabled(self) -> bool:
        """Return whether CLI voice mode should play record start/stop beeps."""
        try:
            from hermes_cli.config import load_config
            from utils import is_truthy_value
            voice_cfg = load_config().get("voice", {})
            if isinstance(voice_cfg, dict):
                # is_truthy_value handles quoted YAML strings like "false"
                # which bool() would misread as True (#49883).
                return is_truthy_value(voice_cfg.get("beep_enabled", True), default=True)
        except Exception:
            pass
        return True

    def _enable_voice_mode(self):
        """Enable voice mode after checking requirements."""
        from cli import _ACCENT, _BOLD, _cprint, _DIM, _RST
        if self._voice_mode:
            _cprint(f"{_DIM}Voice mode is already enabled.{_RST}")
            return

        from tools.voice_mode import check_voice_requirements, detect_audio_environment

        # Environment detection -- warn and block in incompatible environments
        env_check = detect_audio_environment()
        if not env_check["available"]:
            _cprint(f"\n{_ACCENT}Voice mode unavailable in this environment:{_RST}")
            for warning in env_check["warnings"]:
                _cprint(f"  {_DIM}{warning}{_RST}")
            return

        reqs = check_voice_requirements()
        if not reqs["available"]:
            _cprint(f"\n{_ACCENT}Voice mode requirements not met:{_RST}")
            for line in reqs["details"].split("\n"):
                _cprint(f"  {_DIM}{line}{_RST}")
            if reqs["missing_packages"]:
                if _is_termux_environment():
                    _cprint(f"\n  {_BOLD}Option 1: pkg install termux-api{_RST}")
                    _cprint(f"  {_DIM}Then install/update the Termux:API Android app for microphone capture{_RST}")
                    _cprint(f"  {_BOLD}Option 2: pkg install python-numpy portaudio && python -m pip install sounddevice{_RST}")
                else:
                    _cprint(f"\n  {_BOLD}Install: {sys.executable} -m pip install {' '.join(reqs['missing_packages'])}{_RST}")
            return

        with self._voice_lock:
            self._voice_mode = True

        # Check config for auto_tts (shape-safe — malformed ``voice:`` YAML
        # leaves ``voice_config`` as a non-dict, so guard before .get()).
        try:
            from hermes_cli.config import load_config
            _raw_voice = load_config().get("voice")
            voice_config = _raw_voice if isinstance(_raw_voice, dict) else {}
            if voice_config.get("auto_tts", False):
                with self._voice_lock:
                    self._voice_tts = True
        except Exception:
            pass

        # Voice mode instruction is injected as a user message prefix (not a
        # system prompt change) to avoid invalidating the prompt cache.  See
        # _voice_message_prefix property and its usage in _process_message().

        tts_status = " (TTS enabled)" if self._voice_tts else ""
        # Use the startup-pinned cache so the advertised shortcut always
        # matches the live prompt_toolkit binding — reading live config
        # here would drift after a mid-session config edit (Copilot
        # round-14 on #19835, same class as round-13).
        _ptt_display = self._voice_record_key_label()
        _cprint(f"\n{_ACCENT}Voice mode enabled{tts_status}{_RST}")
        _cprint(f"  {_DIM}{_ptt_display} to start/stop recording{_RST}")
        # Spoken-stop hint sourced from voice.stop_phrases (first entry); the
        # helper returns "" when stop phrases are disabled — show no hint then.
        try:
            from tools.voice_mode import voice_stop_hint
            _stop_hint = voice_stop_hint()
        except Exception:
            _stop_hint = ""
        if _stop_hint:
            _cprint(f"  {_DIM}{_stop_hint}{_RST}")
        _cprint(f"  {_DIM}/voice tts  to toggle speech output{_RST}")
        _cprint(f"  {_DIM}/voice off  to disable voice mode{_RST}")

    def _typed_voice_stop(self, user_input) -> bool:
        """Typed bare stop phrase during an active voice chat ends the chat.

        Saying "stop" ends the voice chat (PR #73106); TYPING the same bare
        stop phrase while voice mode is on must behave identically instead of
        sending "stop" to the agent as a turn. Guarded on voice mode being ON
        — typed "stop" outside voice chat passes through to the agent exactly
        as before. Reuses ``is_voice_stop_phrase`` (same config
        ``voice.stop_phrases``, same exact-match semantics), so longer typed
        messages containing "stop" are never swallowed.
        """
        from cli import _cprint, _DIM, _RST
        if not isinstance(user_input, str):
            return False
        with self._voice_lock:
            voice_on = self._voice_mode or self._voice_continuous
        if not voice_on:
            return False
        try:
            from tools.voice_mode import is_voice_stop_phrase
            if not is_voice_stop_phrase(user_input):
                return False
        except Exception:
            return False
        _cprint(f"\n{_DIM}Stop phrase typed — ending voice chat.{_RST}")
        self._disable_voice_mode()
        return True

    def _disable_voice_mode(self):
        """Disable voice mode, cancel any active recording, and stop TTS."""
        from cli import _cprint, _DIM, _RST, logger
        recorder = None
        with self._voice_lock:
            if self._voice_recording and self._voice_recorder:
                self._voice_recorder.cancel()
                self._voice_recording = False
            recorder = self._voice_recorder
            self._voice_mode = False
            self._voice_tts = False
            self._voice_continuous = False

        # Shut down the persistent audio stream in background
        if recorder is not None:
            def _bg_shutdown(rec=recorder):
                try:
                    rec.shutdown()
                except Exception:
                    pass
            threading.Thread(target=_bg_shutdown, daemon=True).start()
            self._voice_recorder = None

        # Stop any active TTS playback (file player + streaming pipeline)
        try:
            if self._voice_tts_stop is not None:
                logger.info("TTS CUT: _disable_voice_mode setting stop event")
                self._voice_tts_stop.set()
            from tools.voice_mode import stop_playback
            stop_playback()
        except Exception:
            pass
        self._voice_tts_done.set()

        _cprint(f"\n{_DIM}Voice mode disabled.{_RST}")

    def _toggle_voice_tts(self):
        """Toggle TTS output for voice mode."""
        from cli import _ACCENT, _cprint, _DIM, _RST
        if not self._voice_mode:
            _cprint(f"{_DIM}Enable voice mode first: /voice on{_RST}")
            return

        with self._voice_lock:
            self._voice_tts = not self._voice_tts
        status = "enabled" if self._voice_tts else "disabled"

        if self._voice_tts:
            from tools.tts_tool import check_tts_requirements
            if not check_tts_requirements():
                _cprint(f"{_DIM}Warning: No TTS provider available. Install edge-tts or set API keys.{_RST}")

        _cprint(f"{_ACCENT}Voice TTS {status}.{_RST}")

    def _show_voice_status(self):
        """Show current voice mode status."""
        from cli import _BOLD, _cprint, _RST
        from tools.voice_mode import check_voice_requirements

        reqs = check_voice_requirements()

        _cprint(f"\n{_BOLD}Voice Mode Status{_RST}")
        _cprint(f"  Mode:      {'ON' if self._voice_mode else 'OFF'}")
        _cprint(f"  TTS:       {'ON' if self._voice_tts else 'OFF'}")
        _cprint(f"  Recording: {'YES' if self._voice_recording else 'no'}")
        # Display the startup-pinned label so /voice status always
        # matches the live prompt_toolkit binding (Copilot round-14 on
        # #19835, same class as round-13). Reading live config here
        # would drift after a mid-session config edit.
        _cprint(f"  Record key: {self._voice_record_key_label()}")
        _cprint(f"\n  {_BOLD}Requirements:{_RST}")
        for line in reqs["details"].split("\n"):
            _cprint(f"    {line}")
