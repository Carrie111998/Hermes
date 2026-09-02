"""Per-contact WhatsApp toolset overrides (adapter.toolsets_for_source).

``platform_toolsets.whatsapp`` is platform-wide, so every allowlisted contact
shares one toolset list. ``platforms.whatsapp.extra.contact_toolsets`` maps a
contact identifier to the toolset list used for runs that contact triggers, so
a low-trust contact can answer messages without inheriting the platform list
(whose WhatsApp default includes ``terminal``).

Keys resolve through the same phone/JID/LID alias matching as ``allow_from``,
and the gateway validates any override through ``_get_platform_tools`` — an
entry cannot self-grant a toolset that platform config could not.
"""

import logging

from gateway.platforms.base import BasePlatformAdapter
from gateway.platforms.whatsapp_common import WhatsAppBehaviorMixin
from gateway.run import GatewayRunner
from hermes_cli.tools_config import _get_platform_tools


class _Cfg:
    def __init__(self, extra):
        self.extra = extra


class _Src:
    def __init__(self, chat_id, user_id=None, chat_type="dm"):
        self.chat_id = chat_id
        self.user_id = user_id
        self.chat_type = chat_type


def _make_adapter(contact_toolsets, *, key="contact_toolsets"):
    wa = object.__new__(WhatsAppBehaviorMixin)
    wa.config = _Cfg({key: contact_toolsets} if contact_toolsets is not None else {})
    wa.name = "whatsapp"
    return wa


def _make_runner(adapter):
    gr = object.__new__(GatewayRunner)
    gr._adapter_for_source = lambda source: adapter
    return gr


BASE_CONFIG = {"platform_toolsets": {"whatsapp": ["web", "vision", "terminal"]}}


class TestContactToolsetsMatching:
    def test_dm_sender_match_returns_list(self):
        wa = _make_adapter({"923001234567": ["web", "vision"]})
        src = _Src("923001234567@s.whatsapp.net", user_id="923001234567@s.whatsapp.net")
        assert wa.toolsets_for_source(src) == ["web", "vision"]

    def test_jid_key_matches_bare_number_sender(self):
        # Operators configure either form; both must resolve to one contact.
        wa = _make_adapter({"923001234567@s.whatsapp.net": ["web"]})
        assert wa.toolsets_for_source(_Src("923001234567", user_id="923001234567")) == [
            "web"
        ]

    def test_bare_number_key_matches_jid_sender(self):
        wa = _make_adapter({"923001234567": ["web"]})
        src = _Src("923001234567@s.whatsapp.net", user_id="923001234567@s.whatsapp.net")
        assert wa.toolsets_for_source(src) == ["web"]

    def test_unlisted_contact_returns_none(self):
        wa = _make_adapter({"923001234567": ["web"]})
        assert wa.toolsets_for_source(_Src("923009999999", user_id="923009999999")) is None

    def test_camel_case_key_accepted(self):
        wa = _make_adapter({"923001234567": ["web"]}, key="contactToolsets")
        assert wa.toolsets_for_source(_Src("923001234567", user_id="923001234567")) == [
            "web"
        ]

    def test_no_mapping_returns_none(self):
        assert _make_adapter(None).toolsets_for_source(_Src("923001234567")) is None
        assert _make_adapter({}).toolsets_for_source(_Src("923001234567")) is None

    def test_group_jid_entry_matches_group_chat(self):
        wa = _make_adapter({"12345-67890@g.us": ["web"]})
        src = _Src("12345-67890@g.us", user_id="923001234567", chat_type="group")
        assert wa.toolsets_for_source(src) == ["web"]

    def test_group_falls_back_to_sender_entry(self):
        # A per-contact restriction still applies inside a group chat.
        wa = _make_adapter({"923001234567": ["web"]})
        src = _Src("12345-67890@g.us", user_id="923001234567", chat_type="group")
        assert wa.toolsets_for_source(src) == ["web"]

    def test_group_entry_wins_over_sender_entry(self):
        wa = _make_adapter(
            {"12345-67890@g.us": ["vision"], "923001234567": ["web"]}
        )
        src = _Src("12345-67890@g.us", user_id="923001234567", chat_type="group")
        assert wa.toolsets_for_source(src) == ["vision"]

    def test_empty_source_returns_none(self):
        wa = _make_adapter({"923001234567": ["web"]})
        assert wa.toolsets_for_source(_Src("", user_id="")) is None


class TestContactToolsetsMalformedEntries:
    def test_empty_list_is_ignored_not_treated_as_deny(self, caplog):
        # An empty override is falsy, so the gateway would fall back to the
        # platform list — silently WIDENING access. Must be refused loudly.
        wa = _make_adapter({"923001234567": []})
        with caplog.at_level(logging.WARNING):
            result = wa.toolsets_for_source(
                _Src("923001234567", user_id="923001234567")
            )
        assert result is None
        assert "empty" in caplog.text.lower()

    def test_blank_strings_are_stripped_then_ignored(self, caplog):
        wa = _make_adapter({"923001234567": ["  ", ""]})
        with caplog.at_level(logging.WARNING):
            assert (
                wa.toolsets_for_source(_Src("923001234567", user_id="923001234567"))
                is None
            )

    def test_non_list_value_is_ignored(self, caplog):
        wa = _make_adapter({"923001234567": "web"})
        with caplog.at_level(logging.WARNING):
            result = wa.toolsets_for_source(
                _Src("923001234567", user_id="923001234567")
            )
        assert result is None
        assert "must be a list" in caplog.text

    def test_non_dict_mapping_is_ignored(self):
        wa = _make_adapter(["923001234567"])
        assert wa.toolsets_for_source(_Src("923001234567", user_id="923001234567")) is None

    def test_wildcard_key_is_not_honored(self):
        # "*" would shadow platform_toolsets.whatsapp; that's what the platform
        # setting is for, so it must not match here.
        wa = _make_adapter({"*": ["web"]})
        assert wa.toolsets_for_source(_Src("923001234567", user_id="923001234567")) is None

    def test_blank_key_is_skipped(self):
        wa = _make_adapter({"   ": ["web"]})
        assert wa.toolsets_for_source(_Src("923001234567", user_id="923001234567")) is None

    def test_values_are_stringified_and_trimmed(self):
        wa = _make_adapter({"923001234567": ["  web  ", "vision"]})
        assert wa.toolsets_for_source(
            _Src("923001234567", user_id="923001234567")
        ) == ["web", "vision"]


class TestBaseAdapterDefaultUnchanged:
    def test_base_adapter_still_returns_none(self):
        wa = _make_adapter({"923001234567": ["web"]})
        assert (
            BasePlatformAdapter.toolsets_for_source(wa, _Src("923001234567")) is None
        )


class TestGatewayIntegration:
    def test_override_replaces_platform_resolution(self):
        wa = _make_adapter({"923001234567": ["web", "vision"]})
        gr = _make_runner(wa)
        res = GatewayRunner._resolve_enabled_toolsets_for_source(
            gr,
            BASE_CONFIG,
            _Src("923001234567", user_id="923001234567"),
            "whatsapp",
        )
        assert "web" in res and "vision" in res
        # The security promise: the platform list carries terminal, the
        # per-contact override does not, and the override fully replaces it.
        assert "terminal" not in res

    def test_override_validated_like_platform_config(self):
        override = ["web", "vision", "discord_admin"]
        wa = _make_adapter({"923001234567": override})
        gr = _make_runner(wa)
        res = GatewayRunner._resolve_enabled_toolsets_for_source(
            gr,
            BASE_CONFIG,
            _Src("923001234567", user_id="923001234567"),
            "whatsapp",
        )
        expected = sorted(
            _get_platform_tools(
                {"platform_toolsets": {"whatsapp": list(override)}}, "whatsapp"
            )
        )
        assert res == expected
        # Platform-restricted toolsets are dropped, exactly as for platform config.
        assert "discord_admin" not in res

    def test_unlisted_contact_uses_platform_resolution(self):
        wa = _make_adapter({"923001234567": ["web"]})
        gr = _make_runner(wa)
        res = GatewayRunner._resolve_enabled_toolsets_for_source(
            gr,
            BASE_CONFIG,
            _Src("923009999999", user_id="923009999999"),
            "whatsapp",
        )
        assert res == sorted(_get_platform_tools(BASE_CONFIG, "whatsapp"))

    def test_original_config_not_mutated(self):
        cfg = {"platform_toolsets": {"whatsapp": ["web", "terminal"]}}
        wa = _make_adapter({"923001234567": ["web"]})
        gr = _make_runner(wa)
        GatewayRunner._resolve_enabled_toolsets_for_source(
            gr, cfg, _Src("923001234567", user_id="923001234567"), "whatsapp"
        )
        assert cfg["platform_toolsets"]["whatsapp"] == ["web", "terminal"]

    def test_adapter_exception_falls_back_to_platform_resolution(self):
        wa = _make_adapter({})
        wa.toolsets_for_source = lambda source: (_ for _ in ()).throw(
            RuntimeError("boom")
        )
        gr = _make_runner(wa)
        res = GatewayRunner._resolve_enabled_toolsets_for_source(
            gr, BASE_CONFIG, _Src("923001234567"), "whatsapp"
        )
        assert res == sorted(_get_platform_tools(BASE_CONFIG, "whatsapp"))
