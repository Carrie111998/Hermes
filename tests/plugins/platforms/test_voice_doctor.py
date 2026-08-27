"""Unit tests for /voice doctor readiness diagnostics.

Tests the module-level helper functions (``_check_pynacl``,
``_check_opus``, ``_check_ffmpeg``, ``_check_stt_provider``,
``_check_davey``) and the overall report invariants.

These are pure-logic tests that do NOT require a live discord.py client
or network access.  Package-level monkeypatching simulates missing deps.
"""

from unittest.mock import MagicMock, patch

import builtins
import pytest
import sys

from plugins.platforms.discord.voice_doctor import (
    _check_davey,
    _check_ffmpeg,
    _check_opus,
    _check_pynacl,
    _check_stt_provider,
)

# ═══════════════════════════════════════════════════════════════════════════════
# _check_pynacl
# ═══════════════════════════════════════════════════════════════════════════════


def test_pynacl_installed(monkeypatch):
    """When nacl imports, the check returns ✅.

    Stubbed via ``sys.modules`` so the test does not depend on PyNaCl being
    installed in the ambient environment (CI does not ship it — it arrives
    only as a discord.py transitive dependency).
    """
    monkeypatch.setitem(sys.modules, "nacl", MagicMock())

    icon, detail = _check_pynacl()
    assert icon == "✅"
    assert "installed" in detail


def test_pynacl_missing(monkeypatch):
    """When nacl raises ImportError, the check returns ❌ with a fix hint."""
    monkeypatch.setattr("builtins.__import__", _fail_import("nacl"))

    icon, detail = _check_pynacl()
    assert icon == "❌"
    assert "pip install pynacl" in detail


# ═══════════════════════════════════════════════════════════════════════════════
# _check_opus
# ═══════════════════════════════════════════════════════════════════════════════


def test_opus_loaded(monkeypatch):
    """When discord.opus.is_loaded() is True, the check returns ✅."""
    stub = MagicMock()
    stub.opus.is_loaded = lambda: True
    monkeypatch.setitem(sys.modules, "discord", stub)

    icon, detail = _check_opus()
    assert icon == "✅"
    assert "loaded" in detail


def test_opus_not_loaded(monkeypatch):
    """When discord.opus.is_loaded() is False, the check warns (⚠️)."""
    stub = MagicMock()
    stub.opus.is_loaded = lambda: False
    monkeypatch.setitem(sys.modules, "discord", stub)

    icon, detail = _check_opus()
    assert icon == "⚠️"
    assert "not loaded" in detail
    assert "automatically" in detail


def test_opus_discord_not_installed(monkeypatch):
    """When discord.py is not importable, the check returns ❌ with install hint."""
    # Remove discord from sys.modules if present
    if "discord" in sys.modules:
        monkeypatch.setitem(sys.modules, "discord", None)
        monkeypatch.delitem(sys.modules, "discord")

    # Make importing discord raise ImportError
    original_import = builtins.__import__

    def _fail_discord_import(name, *args, **kwargs):
        if name == "discord" or name.startswith("discord."):
            raise ImportError(f"No module named {name}")
        return original_import(name, *args, **kwargs)

    monkeypatch.setattr("builtins.__import__", _fail_discord_import)

    icon, detail = _check_opus()
    assert icon == "❌"
    assert "discord.py not installed" in detail
    assert "pip install discord.py" in detail


# ═══════════════════════════════════════════════════════════════════════════════
# _check_ffmpeg
# ═══════════════════════════════════════════════════════════════════════════════


def test_ffmpeg_found(monkeypatch):
    """When resolve_ffmpeg_executable returns a real path, the check returns ✅."""

    def _mock_resolve():
        return "/usr/local/bin/ffmpeg"

    monkeypatch.setattr(
        "plugins.platforms.discord.ffmpeg_utils.resolve_ffmpeg_executable",
        _mock_resolve,
    )

    icon, detail = _check_ffmpeg()
    assert icon == "✅"
    assert "ffmpeg" in detail


def test_ffmpeg_not_on_path(monkeypatch):
    """When ffmpeg resolves to bare 'ffmpeg' but is not on PATH, the check
    returns ❌ with a fix hint."""

    def _mock_resolve():
        return "ffmpeg"

    monkeypatch.setattr(
        "plugins.platforms.discord.ffmpeg_utils.resolve_ffmpeg_executable",
        _mock_resolve,
    )
    monkeypatch.setattr("shutil.which", lambda _: None)

    icon, detail = _check_ffmpeg()
    assert icon == "❌"
    assert "brew install ffmpeg" in detail or "apt install ffmpeg" in detail


def test_ffmpeg_resolver_unavailable(monkeypatch):
    """When the ffmpeg_utils module itself is not importable, the check fails."""
    original_import = builtins.__import__

    def _bad_import(name, *args, **kwargs):
        if "ffmpeg_utils" in name:
            raise ImportError(f"No module named {name}")
        return original_import(name, *args, **kwargs)

    monkeypatch.setattr("builtins.__import__", _bad_import)

    icon, detail = _check_ffmpeg()
    assert icon == "❌"
    assert "install ffmpeg" in detail


# ═══════════════════════════════════════════════════════════════════════════════
# _check_stt_provider
# ═══════════════════════════════════════════════════════════════════════════════


def test_stt_provider_configured(monkeypatch, tmp_path):
    """When the config has stt.provider set, the check returns ✅ with provider name."""
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    import yaml

    (home / "config.yaml").write_text(
        yaml.safe_dump({"stt": {"provider": "openai"}})
    )

    icon, detail = _check_stt_provider()
    assert icon == "✅"
    assert "STT provider — openai" in detail


def test_stt_autodetect_available(monkeypatch, tmp_path):
    """When no STT provider is set and faster-whisper is importable, ✅."""
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    import yaml

    (home / "config.yaml").write_text(yaml.safe_dump({"stt": {}}))

    import sys
    from unittest.mock import MagicMock

    monkeypatch.setitem(sys.modules, "faster_whisper", MagicMock())

    icon, detail = _check_stt_provider()
    assert icon == "✅"
    assert "faster-whisper" in detail
    assert "autodetect" in detail


def test_stt_autodetect_unavailable(monkeypatch, tmp_path):
    """When no STT provider is set and faster-whisper is not importable, ⚠️."""
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    import yaml

    (home / "config.yaml").write_text(yaml.safe_dump({"stt": {}}))

    # Ensure faster_whisper is NOT importable
    if "faster_whisper" in sys.modules:
        monkeypatch.setitem(sys.modules, "faster_whisper", None)
        monkeypatch.delitem(sys.modules, "faster_whisper")

    # Use a __import__ that fails only for faster_whisper
    original_import = builtins.__import__

    def _importer(name, *args, **kwargs):
        if name == "faster_whisper":
            raise ImportError(f"No module named {name}")
        return original_import(name, *args, **kwargs)

    monkeypatch.setattr("builtins.__import__", _importer)

    icon, detail = _check_stt_provider()
    assert icon == "⚠️"
    assert "No STT provider configured" in detail
    assert "faster-whisper not installed" in detail
    assert "pip install faster-whisper" in detail


def test_stt_disabled(monkeypatch, tmp_path):
    """When stt.enabled is false, the check warns (⚠️) with a fix hint."""
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    import yaml

    (home / "config.yaml").write_text(
        yaml.safe_dump({"stt": {"enabled": False}})
    )

    icon, detail = _check_stt_provider()
    assert icon == "⚠️"
    assert "STT disabled" in detail
    assert "stt.enabled: false" in detail
    assert "stt.enabled: true" in detail


def test_stt_provider_no_key_leak(monkeypatch, tmp_path):
    """The report must never contain API-key-like patterns.

    When a user accidentally places an API key into the stt.provider field,
    the report must NOT echo the raw value — it must return a safe label.
    """
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    import yaml

    (home / "config.yaml").write_text(
        yaml.safe_dump(
            {
                "stt": {
                    "provider": "sk-12345abc",
                }
            }
        )
    )

    icon, detail = _check_stt_provider()
    # The raw key-like value must never appear in the detail
    assert "sk-12345abc" not in detail
    # Instead, the safe generic label must be used
    assert "configured (custom)" in detail


def test_stt_autodetect_oserror(monkeypatch, tmp_path):
    """When faster-whisper C-extensions raise OSError, the check handles it gracefully."""
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    import yaml

    (home / "config.yaml").write_text(yaml.safe_dump({"stt": {}}))

    # Make faster_whisper raise OSError (broken C-extension install)
    original_import = builtins.__import__

    def _importer(name, *args, **kwargs):
        if name == "faster_whisper":
            raise OSError("dlopen: cannot load shared library")
        return original_import(name, *args, **kwargs)

    monkeypatch.setattr("builtins.__import__", _importer)

    icon, detail = _check_stt_provider()
    assert icon == "⚠️"
    assert "No STT provider configured" in detail
    assert "faster-whisper not installed" in detail


# ═══════════════════════════════════════════════════════════════════════════════
# _check_davey
# ═══════════════════════════════════════════════════════════════════════════════


def test_davey_installed():
    """When davey is importable, the check returns ✅."""
    # Try real import first; fallback to mock
    try:
        import davey  # noqa: F401
    except ImportError:
        pytest.skip("davey package is not installed")

    icon, detail = _check_davey()
    assert icon == "✅"
    assert "installed" in detail


def test_davey_missing(monkeypatch):
    """When davey raises ImportError, the check warns (⚠️)."""
    monkeypatch.setattr("builtins.__import__", _fail_import("davey"))

    icon, detail = _check_davey()
    assert icon == "⚠️"
    assert "davey not installed" in detail
    assert "nacl-only" in detail


# ═══════════════════════════════════════════════════════════════════════════════
# Report invariants
# ═══════════════════════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_voice_doctor_report_contains_all_checks(monkeypatch):
    """The full report emitted by _voice_doctor_report must contain every
    expected check label, regardless of individual pass/fail status."""
    from plugins.platforms.discord.adapter import DiscordAdapter
    from gateway.config import PlatformConfig

    # Build a minimal adapter instance
    adapter = DiscordAdapter(
        PlatformConfig(enabled=True, token="fake_token")
    )
    # Simulate no connected client so permission check is lenient
    adapter._client = None

    # Mock all the helper functions to return known values so the
    # report is deterministic and we can check labels.
    mock_checks = {
        "_check_pynacl": ("✅", "PyNaCl — installed"),
        "_check_opus": ("✅", "Opus — loaded"),
        "_check_ffmpeg": ("✅", "ffmpeg — /usr/bin/ffmpeg"),
        "_check_stt_provider": ("✅", "STT provider — configured"),
        "_check_davey": ("⚠️", "davey not installed — DAVE-encrypted ..."),
    }

    for func_name, (icon, detail) in mock_checks.items():
        monkeypatch.setattr(
            f"plugins.platforms.discord.voice_doctor.{func_name}",
            lambda _icon=icon, _detail=detail: (_icon, _detail),
        )

    # Mock the permission check
    async def _mock_perms(_self, _interaction):
        return ("✅", "Bot permissions — Connect, Speak, Use Voice Activity granted")

    monkeypatch.setattr(
        "plugins.platforms.discord.adapter.DiscordAdapter._check_voice_permissions",
        _mock_perms,
    )

    # Create a minimal fake interaction that has a guild_id but no real guild
    interaction = MagicMock()
    interaction.guild = None
    interaction.guild_id = 12345
    interaction.channel_id = 67890
    interaction.user.id = 111
    interaction.user.name = "testuser"

    report = await adapter._voice_doctor_report(interaction)

    # All check labels present
    assert "PyNaCl" in report
    assert "Opus" in report
    assert "ffmpeg" in report
    assert "STT provider" in report
    assert "Bot permissions" in report
    assert "davey" in report

    # Summary line — warns for davey ⚠️
    assert "OK — 1 warning(s) found" in report

    # No key-like values
    assert "sk-" not in report


@pytest.mark.asyncio
async def test_voice_doctor_report_counts_failures(monkeypatch):
    """When multiple checks fail, the summary reflects the correct count."""
    from plugins.platforms.discord.adapter import DiscordAdapter
    from gateway.config import PlatformConfig

    adapter = DiscordAdapter(
        PlatformConfig(enabled=True, token="fake_token")
    )
    adapter._client = None

    # Two failures, one warning, rest pass
    mock_checks = {
        "_check_pynacl": ("❌", "PyNaCl — missing. Fix: pip install pynacl"),
        "_check_opus": ("✅", "Opus — loaded"),
        "_check_ffmpeg": ("❌", "ffmpeg — not found. Fix: install ffmpeg"),
        "_check_stt_provider": ("⚠️", "No STT provider configured"),
        "_check_davey": ("✅", "davey — installed"),
    }

    for func_name, (icon, detail) in mock_checks.items():
        monkeypatch.setattr(
            f"plugins.platforms.discord.voice_doctor.{func_name}",
            lambda _icon=icon, _detail=detail: (_icon, _detail),
        )

    async def _mock_perms(_self, _interaction):
        return ("✅", "Bot permissions — granted")

    monkeypatch.setattr(
        "plugins.platforms.discord.adapter.DiscordAdapter._check_voice_permissions",
        _mock_perms,
    )

    interaction = MagicMock()
    interaction.guild = None
    interaction.guild_id = 12345

    report = await adapter._voice_doctor_report(interaction)

    # Should say "2 issue(s) found"
    assert "2 issue(s) found" in report
    assert "pip install pynacl" in report
    assert "install ffmpeg" in report


@pytest.mark.asyncio
async def test_voice_doctor_report_only_warnings(monkeypatch):
    """When no ❌ checks but one or more ⚠️, the summary says 'OK — N warning(s)'."""
    from plugins.platforms.discord.adapter import DiscordAdapter
    from gateway.config import PlatformConfig

    adapter = DiscordAdapter(
        PlatformConfig(enabled=True, token="fake_token")
    )
    adapter._client = None

    # No failures, one warning
    mock_checks = {
        "_check_pynacl": ("✅", "PyNaCl — installed"),
        "_check_opus": ("⚠️", "Opus — not loaded"),
        "_check_ffmpeg": ("✅", "ffmpeg — /usr/bin/ffmpeg"),
        "_check_stt_provider": ("✅", "STT provider — configured"),
        "_check_davey": ("✅", "davey — installed"),
    }

    for func_name, (icon, detail) in mock_checks.items():
        monkeypatch.setattr(
            f"plugins.platforms.discord.voice_doctor.{func_name}",
            lambda _icon=icon, _detail=detail: (_icon, _detail),
        )

    async def _mock_perms(_self, _interaction):
        return ("✅", "Bot permissions — granted")

    monkeypatch.setattr(
        "plugins.platforms.discord.adapter.DiscordAdapter._check_voice_permissions",
        _mock_perms,
    )

    interaction = MagicMock()
    interaction.guild = None
    interaction.guild_id = 12345

    report = await adapter._voice_doctor_report(interaction)

    # Should say "OK — 1 warning(s) found"
    assert "OK — 1 warning(s) found" in report


@pytest.mark.asyncio
async def test_voice_doctor_permissions_dm(monkeypatch):
    """When interaction.guild is None (DM), the permission check warns."""
    from plugins.platforms.discord.adapter import DiscordAdapter
    from gateway.config import PlatformConfig

    adapter = DiscordAdapter(
        PlatformConfig(enabled=True, token="fake_token")
    )
    adapter._client = None

    interaction = MagicMock()
    interaction.guild = None
    interaction.guild_id = None

    icon, detail = await adapter._check_voice_permissions(interaction)
    assert icon == "⚠️"
    assert "cannot check in DMs" in detail


# ═══════════════════════════════════════════════════════════════════════════════
# Helper: fail imports for a specific module name
# ═══════════════════════════════════════════════════════════════════════════════

def _fail_import(mod_name: str):
    """Return a __import__ replacement that raises ImportError for *mod_name*."""
    original_import = builtins.__import__

    def _importer(name, *args, **kwargs):
        if name == mod_name or name.startswith(mod_name + "."):
            raise ImportError(f"No module named {name}")
        # Support both: direct import and "from ... import ..." via __import__
        # __import__('nacl') returns the top-level module; any submodule
        # reference also hits the same name check.
        return original_import(name, *args, **kwargs)

    return _importer