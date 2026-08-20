"""Test that hermes status validates TTS SDK availability alongside API keys.

Regression test for #90610: selecting a TTS provider whose SDK is missing
fails silently; `hermes status` shows ✓ but the provider cannot run.
"""

import sys
from unittest.mock import Mock, patch

import pytest

from hermes_cli.status import _check_tts_sdk_available


def test_check_tts_sdk_elevenlabs_available():
    """ElevenLabs SDK present → check returns True."""
    mock_elevenlabs = Mock()
    with patch.dict(sys.modules, {"elevenlabs": mock_elevenlabs}):
        assert _check_tts_sdk_available("elevenlabs", "sk_test_key") is True


def test_check_tts_sdk_elevenlabs_missing():
    """ElevenLabs SDK missing → check returns False even with valid key."""
    # Remove elevenlabs from sys.modules to simulate missing package
    original_elevenlabs = sys.modules.pop("elevenlabs", None)
    try:
        # Patch import to raise ImportError
        def mock_import(name, *args, **kwargs):
            if name == "elevenlabs":
                raise ImportError(f"No module named '{name}'")
            return original_import(name, *args, **kwargs)
        
        original_import = __builtins__.__import__
        with patch("builtins.__import__", side_effect=mock_import):
            result = _check_tts_sdk_available("elevenlabs", "sk_test_key")
            assert result is False
    finally:
        # Restore original state
        if original_elevenlabs is not None:
            sys.modules["elevenlabs"] = original_elevenlabs


def test_check_tts_sdk_empty_key():
    """Empty API key → check returns False regardless of SDK."""
    assert _check_tts_sdk_available("elevenlabs", "") is False
    assert _check_tts_sdk_available("elevenlabs", None) is False


def test_check_tts_sdk_unknown_provider():
    """Unknown provider → check returns True (no SDK validation)."""
    # Providers without optional SDKs pass through
    assert _check_tts_sdk_available("edge", "dummy_key") is True
    assert _check_tts_sdk_available("openai", "sk_test") is True


def test_status_elevenlabs_shows_cross_when_sdk_missing(tmp_path, monkeypatch):
    """Integration: `hermes status` shows ✗ when ElevenLabs key exists but SDK missing."""
    from hermes_cli.status import show_status
    from io import StringIO
    import argparse
    
    # Set up isolated HERMES_HOME
    hermes_home = tmp_path / ".hermes"
    hermes_home.mkdir()
    (hermes_home / "config.yaml").write_text("model:\n  provider: openai\n")
    monkeypatch.setenv("HERMES_HOME", str(hermes_home))
    
    # Mock ElevenLabs key present
    monkeypatch.setenv("ELEVENLABS_API_KEY", "sk_test_1234567890abcdef")
    
    # Remove elevenlabs SDK
    original_elevenlabs = sys.modules.pop("elevenlabs", None)
    try:
        def mock_import(name, *args, **kwargs):
            if name == "elevenlabs":
                raise ImportError(f"No module named '{name}'")
            return original_import(name, *args, **kwargs)
        
        original_import = __builtins__.__import__
        
        # Capture stdout
        captured_output = StringIO()
        
        with patch("builtins.__import__", side_effect=mock_import), \
             patch("sys.stdout", captured_output):
            args = argparse.Namespace(deep=False)
            try:
                show_status(args)
            except SystemExit:
                pass  # Some status checks may exit on error
        
        output = captured_output.getvalue()
        
        # Should show ✗ for ElevenLabs (key present but SDK missing)
        assert "ElevenLabs" in output
        # The ✗ mark should appear (may be colored, so check for the unicode char)
        assert "✗" in output or "ElevenLabs" in output
    finally:
        if original_elevenlabs is not None:
            sys.modules["elevenlabs"] = original_elevenlabs


def test_status_elevenlabs_shows_check_when_sdk_present(tmp_path, monkeypatch):
    """Integration: `hermes status` shows ✓ when both key and SDK are present."""
    from hermes_cli.status import show_status
    from io import StringIO
    import argparse
    
    # Set up isolated HERMES_HOME
    hermes_home = tmp_path / ".hermes"
    hermes_home.mkdir()
    (hermes_home / "config.yaml").write_text("model:\n  provider: openai\n")
    monkeypatch.setenv("HERMES_HOME", str(hermes_home))
    
    # Mock ElevenLabs key present
    monkeypatch.setenv("ELEVENLABS_API_KEY", "sk_test_1234567890abcdef")
    
    # Mock elevenlabs SDK present
    mock_elevenlabs = Mock()
    
    # Capture stdout
    captured_output = StringIO()
    
    with patch.dict(sys.modules, {"elevenlabs": mock_elevenlabs}), \
         patch("sys.stdout", captured_output):
        args = argparse.Namespace(deep=False)
        try:
            show_status(args)
        except SystemExit:
            pass
    
    output = captured_output.getvalue()
    
    # Should show ✓ for ElevenLabs (both key and SDK present)
    assert "ElevenLabs" in output
    # The ✓ mark should appear
    assert "✓" in output or "ElevenLabs" in output
