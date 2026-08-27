"""
Voice-readiness diagnostic helpers for the Discord adapter.

These module-level helpers power the ``/voice doctor`` slash command, checking
whether the local environment is capable of Discord voice: PyNaCl, Opus codec,
ffmpeg, STT provider configuration, and DAVE E2EE (``davey``).

**Why this module exists**

This module MUST remain importable even when ``discord.py`` is NOT installed.
In CI, ``discord.py`` is an optional platform extra and is absent from the test
environment, but the :mod:`test_voice_doctor` unit tests import these helpers
to verify their logic.  By isolating them here without a hard module-level
dependency on ``discord``, we keep the test suite green on CI and avoid false
positives from a missing optional dependency.

``_check_opus`` is the only helper that needs ``discord`` at runtime; it
handles the missing dependency with a try/except and returns a clear
"not installed" diagnostic.  All other helpers have no dependency on the
``discord`` package.
"""

from __future__ import annotations


def _check_pynacl() -> tuple[str, str]:
    """Check whether PyNaCl is importable."""
    try:
        import nacl  # noqa: F401 — only testing importability
    except ImportError:
        return ("❌", "PyNaCl — missing. Fix: pip install pynacl")
    return ("✅", "PyNaCl — installed")


def _check_opus() -> tuple[str, str]:
    """Check whether the Opus codec is loaded.

    Gracefully handles the case where ``discord.py`` itself is not installed
    (returning a ❌ not-installed diagnostic) so this module can be imported
    in CI environments that do not ship the optional ``discord.py`` extra.
    """
    try:
        import discord
    except ImportError:
        return (
            "❌",
            "Opus — discord.py not installed — install it to use Discord voice "
            "(pip install discord.py)",
        )
    if discord.opus.is_loaded():
        return ("✅", "Opus — loaded")
    return (
        "⚠️",
        "Opus — not loaded; Hermes will attempt to load it automatically",
    )


def _check_ffmpeg() -> tuple[str, str]:
    """Check whether an ffmpeg/ffprobe executable is discoverable."""
    try:
        from .ffmpeg_utils import resolve_ffmpeg_executable
    except ImportError:
        try:
            from ffmpeg_utils import resolve_ffmpeg_executable
        except ImportError:
            return ("❌", "ffmpeg — resolver unavailable. Fix: install ffmpeg")

    try:
        resolved = resolve_ffmpeg_executable()
        if not resolved or resolved == "ffmpeg":
            # resolve_ffmpeg_executable defaults to "ffmpeg" as a last resort
            # — confirm it actually exists on PATH
            import shutil
            if not shutil.which("ffmpeg"):
                return (
                    "❌",
                    "ffmpeg — not found. Fix: install ffmpeg "
                    "(brew install ffmpeg / apt install ffmpeg)",
                )
        return ("✅", f"ffmpeg — {resolved}")
    except Exception:
        return ("❌", "ffmpeg — resolution failed. Fix: install ffmpeg")


def _check_stt_provider() -> tuple[str, str]:
    """Check the configured STT provider (presence-only, no key values)."""
    try:
        from hermes_cli.config import read_raw_config
        cfg = read_raw_config() or {}
        stt = cfg.get("stt") or {}
        provider = stt.get("provider")
        enabled = stt.get("enabled", True)
    except Exception:
        provider = None
        enabled = True

    # Check if STT is explicitly disabled (is_truthy semantics)
    from utils import is_truthy_value

    if not is_truthy_value(enabled, default=True):
        return (
            "⚠️",
            "STT disabled in config (stt.enabled: false). "
            "Fix: set stt.enabled: true in config.yaml",
        )

    if provider and str(provider).strip():
        raw = str(provider).strip()
        known = {"local", "groq", "openai", "mistral", "elevenlabs", "deepinfra", "xai", "whisper", "faster-whisper"}
        if raw.lower() in known:
            return ("✅", f"STT provider — {raw}")
        return ("✅", "STT provider — configured (custom)")
    # Autodetect: try faster-whisper
    try:
        import faster_whisper  # noqa: F401
    except Exception:
        return (
            "⚠️",
            "No STT provider configured and faster-whisper not installed — "
            "voice transcription unavailable. "
            "Fix: set stt.provider in config.yaml or pip install faster-whisper",
        )
    return ("✅", "STT — local faster-whisper available (autodetect)")


def _check_davey() -> tuple[str, str]:
    """Check whether the ``davey`` (DAVE E2EE) package is available."""
    try:
        import davey  # noqa: F401 — only testing importability
    except ImportError:
        return (
            "⚠️",
            "davey not installed — DAVE-encrypted voice channels unsupported "
            "(older nacl-only mode will be used)",
        )
    return ("✅", "davey — installed")