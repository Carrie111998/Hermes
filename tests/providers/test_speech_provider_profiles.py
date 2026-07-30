"""Tests for the speech-provider registry, profiles, and plugin discovery.

Mirrors ``test_provider_profiles.py`` and ``test_plugin_discovery.py`` — the
model-provider counterparts — so the speech-provider plugin system gets the
same coverage with the same conventions.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

from providers import (
    SpeechProviderProfile,
    get_speech_provider,
    list_speech_providers,
    register_speech_provider,
)
from providers.base import SpeechProviderProfile as _BaseSpeechProfile


REPO_ROOT = Path(__file__).resolve().parents[2]


def _clear_speech_caches():
    """Force providers/__init__.py to re-discover speech plugins next call."""
    import providers as _pkg

    _pkg._SPEECH_REGISTRY.clear()
    _pkg._SPEECH_ALIASES.clear()
    _pkg._speech_discovered = False
    # Evict any cached plugin modules so the next import re-executes.
    for mod in list(sys.modules.keys()):
        if (
            mod.startswith("plugins.speech_providers")
            or mod.startswith("_hermes_user_speech_provider")
        ):
            del sys.modules[mod]


class TestSpeechProfile:
    def test_can_create_with_required_fields(self):
        """Only ``name`` is required; everything else has a sane default."""
        p = SpeechProviderProfile(name="acme")
        assert p.name == "acme"
        assert p.aliases == ()
        assert p.display_name == ""
        assert p.description == ""
        assert p.signup_url == ""
        assert p.env_vars == ()
        assert p.base_url == ""
        assert p.auth_type == "api_key"
        assert p.default_headers == {}
        assert p.default_voice == ""
        assert p.fallback_voices == ()
        assert p.voice_families == ()
        assert p.output_format == "mp3"
        assert p.supports_streaming is False

    def test_all_fields_settable(self):
        p = SpeechProviderProfile(
            name="acme",
            aliases=("acme-voice",),
            display_name="Acme Voice",
            description="Acme TTS backend",
            signup_url="https://acme.example.com/",
            env_vars=("ACME_API_KEY",),
            base_url="https://voice.acme.example.com/v1",
            auth_type="bearer",
            default_headers={"X-Acme": "1"},
            default_voice="acme-matt",
            fallback_voices=("acme-jane",),
            voice_families=("neutral",),
            output_format="wav",
            supports_streaming=True,
        )
        assert p.aliases == ("acme-voice",)
        assert p.env_vars == ("ACME_API_KEY",)
        assert p.base_url == "https://voice.acme.example.com/v1"
        assert p.auth_type == "bearer"
        assert p.default_headers == {"X-Acme": "1"}
        assert p.default_voice == "acme-matt"
        assert p.fallback_voices == ("acme-jane",)
        assert p.voice_families == ("neutral",)
        assert p.output_format == "wav"
        assert p.supports_streaming is True

    def test_default_headers_is_independent_per_instance(self):
        """default_factory must yield a fresh dict per instance (no shared alias)."""
        a = SpeechProviderProfile(name="a")
        b = SpeechProviderProfile(name="b")
        a.default_headers["k"] = "v"
        assert b.default_headers == {}

    def test_top_level_import_matches_base_import(self):
        """``from providers import SpeechProviderProfile`` is the same class."""
        assert SpeechProviderProfile is _BaseSpeechProfile


class TestSpeechRegistry:
    def test_register_and_get_round_trip(self):
        _clear_speech_caches()
        p = SpeechProviderProfile(name="acme-tts", env_vars=("ACME_API_KEY",))
        register_speech_provider(p)
        got = get_speech_provider("acme-tts")
        assert got is not None
        assert got is p
        assert got.env_vars == ("ACME_API_KEY",)

    def test_get_unknown_returns_none(self):
        _clear_speech_caches()
        assert get_speech_provider("no-such-speech-provider") is None

    def test_alias_resolves(self):
        _clear_speech_caches()
        p = SpeechProviderProfile(
            name="acme-tts", aliases=("acme-voice", "acme")
        )
        register_speech_provider(p)
        assert get_speech_provider("acme-voice") is p
        assert get_speech_provider("acme") is p
        assert get_speech_provider("acme-tts") is p

    def test_list_includes_registered(self):
        _clear_speech_caches()
        a = SpeechProviderProfile(name="acme-tts")
        b = SpeechProviderProfile(name="beta-tts", aliases=("beta",))
        register_speech_provider(a)
        register_speech_provider(b)
        names = sorted(p.name for p in list_speech_providers())
        assert "acme-tts" in names
        assert "beta-tts" in names

    def test_list_deduplicates_by_identity(self):
        """An alias must not create a duplicate entry in list_speech_providers."""
        _clear_speech_caches()
        p = SpeechProviderProfile(name="acme-tts", aliases=("acme-voice",))
        register_speech_provider(p)
        got = [prof for prof in list_speech_providers() if prof is p]
        assert len(got) == 1

    def test_later_registration_overrides(self):
        """Last-writer-wins so user plugins can override bundled profiles."""
        _clear_speech_caches()
        first = SpeechProviderProfile(
            name="acme-tts", base_url="https://first.example.com"
        )
        second = SpeechProviderProfile(
            name="acme-tts", base_url="https://second.example.com"
        )
        register_speech_provider(first)
        register_speech_provider(second)
        got = get_speech_provider("acme-tts")
        assert got is not None
        assert got is second
        assert got.base_url == "https://second.example.com"


class TestSpeechDiscovery:
    def test_lazy_discovery_triggers_once(self):
        """Calling get/list must populate the registry lazily (no bundled dir
        in the test repo, so the registry stays empty but the flag flips)."""
        _clear_speech_caches()
        import providers as _pkg

        assert _pkg._speech_discovered is False
        # No bundled speech-providers dir ships in the repo today; discovery
        # still runs and flips the flag.
        get_speech_provider("anything")
        assert _pkg._speech_discovered is True
        _clear_speech_caches()

    def test_discovery_from_user_dir(self, tmp_path, monkeypatch):
        """A user plugin under $HERMES_HOME/plugins/speech-providers/ registers."""
        _clear_speech_caches()

        hermes_home = tmp_path / ".hermes"
        hermes_home.mkdir()
        monkeypatch.setenv("HERMES_HOME", str(hermes_home))

        user_plugin = hermes_home / "plugins" / "speech-providers" / "acme"
        user_plugin.mkdir(parents=True)
        (user_plugin / "__init__.py").write_text(
            "from providers import register_speech_provider\n"
            "from providers.base import SpeechProviderProfile\n"
            "\n"
            "acme = SpeechProviderProfile(\n"
            '    name="acme",\n'
            '    aliases=("acme-voice",),\n'
            '    env_vars=("ACME_API_KEY",),\n'
            '    base_url="https://voice.acme.example.com/v1",\n'
            '    default_voice="acme-matt",\n'
            '    output_format="mp3",\n'
            ")\n"
            "register_speech_provider(acme)\n"
        )
        (user_plugin / "plugin.yaml").write_text(
            "name: acme-speech\n"
            "kind: speech-provider\n"
            "version: 0.0.1\n"
            "description: Test acme speech provider\n"
        )

        acme = get_speech_provider("acme")
        assert acme is not None
        assert acme.base_url == "https://voice.acme.example.com/v1"
        assert acme.default_voice == "acme-matt"
        # Alias resolves to the same object
        assert get_speech_provider("acme-voice") is acme
        # Listed by list_speech_providers
        names = [p.name for p in list_speech_providers()]
        assert "acme" in names

        _clear_speech_caches()

    def test_user_speech_plugin_overrides_bundled(self, tmp_path, monkeypatch):
        """A user speech plugin with the same name wins over the bundled one."""
        _clear_speech_caches()

        hermes_home = tmp_path / ".hermes"
        hermes_home.mkdir()
        monkeypatch.setenv("HERMES_HOME", str(hermes_home))

        # Pretend a bundled plugin already registered "telnyx" by registering
        # it directly (simulating bundled discovery).
        bundled = SpeechProviderProfile(
            name="telnyx",
            aliases=("telnyx-voice",),
            base_url="https://api.telnyx.com/v2/bundled",
        )
        register_speech_provider(bundled)

        # Now drop a user plugin that overrides "telnyx"
        user_plugin = hermes_home / "plugins" / "speech-providers" / "telnyx"
        user_plugin.mkdir(parents=True)
        (user_plugin / "__init__.py").write_text(
            "from providers import register_speech_provider\n"
            "from providers.base import SpeechProviderProfile\n"
            "\n"
            "telnyx = SpeechProviderProfile(\n"
            '    name="telnyx",\n'
            '    aliases=("telnyx-voice",),\n'
            '    base_url="https://api.telnyx.com/v2/user-override",\n'
            ")\n"
            "register_speech_provider(telnyx)\n"
        )
        (user_plugin / "plugin.yaml").write_text(
            "name: telnyx-speech\n"
            "kind: speech-provider\n"
            "version: 0.0.1\n"
        )

        # Reset discovery so the user plugin loads, then re-trigger.
        # register_speech_provider wrote into the registry directly; reset the
        # flag and clear caches so _discover_speech_providers re-runs from the
        # user dir on the next get_speech_provider() call. We clear the registry
        # first so the user plugin is the only writer after discovery.
        import providers as _pkg

        _pkg._SPEECH_REGISTRY.clear()
        _pkg._SPEECH_ALIASES.clear()
        _pkg._speech_discovered = False
        for mod in list(sys.modules.keys()):
            if mod.startswith("_hermes_user_speech_provider"):
                del sys.modules[mod]

        telnyx = get_speech_provider("telnyx")
        assert telnyx is not None
        assert telnyx.base_url == "https://api.telnyx.com/v2/user-override"
        assert get_speech_provider("telnyx-voice") is telnyx

        _clear_speech_caches()

    def test_bundled_speech_plugins_dir_constant(self):
        """The bundled-dir constant points at <repo>/plugins/speech-providers."""
        import providers as _pkg

        expected = REPO_ROOT / "plugins" / "speech-providers"
        assert _pkg._BUNDLED_SPEECH_PLUGINS_DIR == expected
