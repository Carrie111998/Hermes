"""Tests for the post-STT keyword gate on Discord voice channels.

The gate lives in ``DiscordAdapter._matches_voice_keyword`` and
``_load_voice_keywords`` in plugins/platforms/discord/adapter.py.  The matching
logic is pure (no discord.py, no I/O), so it is tested via the standard
``object.__new__(DiscordAdapter)`` helper used elsewhere in the voice suite.
"""

from unittest.mock import patch

import pytest

from plugins.platforms.discord.adapter import DiscordAdapter


def _adapter(keywords):
    """Minimal DiscordAdapter whose gate is armed with *keywords*."""
    adapter = object.__new__(DiscordAdapter)
    adapter._voice_keywords = [k.lower() for k in keywords]
    return adapter


class TestMatchesVoiceKeyword:
    def test_disabled_gate_passthrough(self):
        # Empty keyword list = gate disabled: every utterance is kept as-is.
        adapter = _adapter([])
        assert adapter._matches_voice_keyword("bonjour tout le monde") == \
            "bonjour tout le monde"
        assert adapter._matches_voice_keyword("") == ""

    def test_matching_prefix_is_stripped(self):
        adapter = _adapter(["hey hermes", "jarvis"])
        assert adapter._matches_voice_keyword(
            "hey hermes, quelle heure est-il ?") == "quelle heure est-il ?"
        assert adapter._matches_voice_keyword(
            "Hey Hermes donne la météo") == "donne la météo"
        assert adapter._matches_voice_keyword(
            "jarvis allume la lumière") == "allume la lumière"

    def test_case_insensitive(self):
        adapter = _adapter(["jarvis"])
        assert adapter._matches_voice_keyword("JARVIS la lumière") == \
            "la lumière"
        assert adapter._matches_voice_keyword("jArViS la lumière") == \
            "la lumière"

    def test_non_keyword_utterance_filtered_out(self):
        adapter = _adapter(["hey hermes"])
        assert adapter._matches_voice_keyword("bonjour, tu es là ?") is None
        assert adapter._matches_voice_keyword("salut") is None

    def test_requires_word_boundary(self):
        # "hey hermesphone" must NOT trigger the "hey hermes" keyword.
        adapter = _adapter(["hey hermes"])
        assert adapter._matches_voice_keyword("hey hermesphone teste") is None

    def test_keyword_only_returns_empty(self):
        adapter = _adapter(["hey hermes"])
        assert adapter._matches_voice_keyword("hey hermes") == ""
        assert adapter._matches_voice_keyword("  HEY HERMES  ") == ""

    def test_separator_after_keyword(self):
        adapter = _adapter(["hey hermes"])
        assert adapter._matches_voice_keyword("hey hermes!coucou") == "coucou"
        assert adapter._matches_voice_keyword("hey hermes: vas-y") == "vas-y"


class TestLoadVoiceKeywords:
    def test_accepts_json_list_string(self):
        # ``hermes config set`` may persist the value as a JSON-ish string.
        with patch(
            "hermes_cli.config.read_raw_config",
            return_value={"discord": {"voice_keywords": '["hey hermes"]'}},
        ):
            adapter = object.__new__(DiscordAdapter)
            assert adapter._load_voice_keywords() == ["hey hermes"]

    def test_accepts_comma_string(self):
        with patch(
            "hermes_cli.config.read_raw_config",
            return_value={"discord": {"voice_keywords": "hey hermes,jarvis, "}},
        ):
            adapter = object.__new__(DiscordAdapter)
            assert adapter._load_voice_keywords() == ["hey hermes", "jarvis"]

    def test_accepts_yaml_list(self):
        with patch(
            "hermes_cli.config.read_raw_config",
            return_value={"discord": {"voice_keywords": ["Hey Hermes", "Jarvis"]}},
        ):
            adapter = object.__new__(DiscordAdapter)
            assert adapter._load_voice_keywords() == ["hey hermes", "jarvis"]

    def test_empty_is_disabled(self):
        for raw in (None, "", [], "[]"):
            with patch(
                "hermes_cli.config.read_raw_config",
                return_value={"discord": {"voice_keywords": raw}},
            ):
                adapter = object.__new__(DiscordAdapter)
                assert adapter._load_voice_keywords() == []
