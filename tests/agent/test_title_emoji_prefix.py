"""Tests for the optional topical emoji prefix on generated session titles."""

from unittest.mock import MagicMock, patch

import pytest

from agent.title_generator import (
    _EMOJI_RULE,
    _first_emoji_grapheme,
    _normalize_emoji_prefix,
    _title_emoji_prefix_enabled,
    generate_title,
)


def _cfg(**title_generation):
    return {"auxiliary": {"title_generation": title_generation}}


def _patch_config(cfg):
    """Patch both config readers the module may reach for."""
    return (
        patch("hermes_cli.config.load_config", return_value=cfg),
        patch("hermes_cli.config.load_config_readonly", return_value=cfg),
    )


class TestEmojiPrefixConfig:
    """The feature is opt-in and must fail closed."""

    def test_disabled_by_default(self):
        a, b = _patch_config(_cfg())
        with a, b:
            assert _title_emoji_prefix_enabled() is False

    def test_enabled_when_set(self):
        a, b = _patch_config(_cfg(emoji_prefix=True))
        with a, b:
            assert _title_emoji_prefix_enabled() is True

    def test_accepts_truthy_strings(self):
        for value in ("true", "yes", "1", "on"):
            a, b = _patch_config(_cfg(emoji_prefix=value))
            with a, b:
                assert _title_emoji_prefix_enabled() is True, value

    def test_broken_config_fails_closed(self):
        with patch(
            "hermes_cli.config.load_config_readonly",
            side_effect=RuntimeError("bad config"),
        ):
            assert _title_emoji_prefix_enabled() is False


class TestFirstEmojiGrapheme:
    """Grapheme slicing must not corrupt multi-codepoint emoji."""

    def test_zwj_sequence_survives(self):
        # Man technologist: base + ZWJ + laptop.
        assert _first_emoji_grapheme("\U0001F468\u200D\U0001F4BB") == (
            "\U0001F468\u200D\U0001F4BB"
        )

    def test_skin_tone_modifier_survives(self):
        assert _first_emoji_grapheme("\U0001F44D\U0001F3FD") == "\U0001F44D\U0001F3FD"

    def test_variation_selector_survives(self):
        assert _first_emoji_grapheme("\u2764\uFE0F") == "\u2764\uFE0F"

    def test_single_flag_survives(self):
        # Regional indicators J + P.
        assert _first_emoji_grapheme("\U0001F1EF\U0001F1F5") == (
            "\U0001F1EF\U0001F1F5"
        )

    def test_consecutive_flags_keep_only_the_first(self):
        """Regression: adjacent flags are separate graphemes, not modifiers.

        A run like 🇯🇵🇺🇸 is two complete flags. Treating regional indicators as
        modifiers made the slice consume all four code points, so a two-flag
        title kept both flags and broke the exactly-one-emoji contract.
        """
        run = "\U0001F1EF\U0001F1F5\U0001F1FA\U0001F1F8"  # JP + US
        assert _first_emoji_grapheme(run) == "\U0001F1EF\U0001F1F5"

    def test_three_flags_keep_only_the_first(self):
        run = (
            "\U0001F1EF\U0001F1F5"  # JP
            "\U0001F1FA\U0001F1F8"  # US
            "\U0001F1EB\U0001F1F7"  # FR
        )
        assert _first_emoji_grapheme(run) == "\U0001F1EF\U0001F1F5"

    def test_truncated_flag_is_kept(self):
        """A lone indicator still yields a non-empty prefix."""
        assert _first_emoji_grapheme("\U0001F1EF") == "\U0001F1EF"

    def test_flag_after_plain_emoji_stops_the_scan(self):
        run = "\U0001F41B\U0001F1EF\U0001F1F5"  # bug + JP flag
        assert _first_emoji_grapheme(run) == "\U0001F41B"

    def test_empty_input(self):
        assert _first_emoji_grapheme("") == ""
        assert _first_emoji_grapheme(None) == ""  # type: ignore[arg-type]


class TestNormalizeEmojiPrefix:
    """Model output is coerced to exactly '<emoji> <text>'."""

    def test_already_correct_is_unchanged(self):
        assert _normalize_emoji_prefix("\U0001F41B Fix login button") == (
            "\U0001F41B Fix login button"
        )

    def test_missing_space_is_inserted(self):
        assert _normalize_emoji_prefix("\U0001F41BFix login button") == (
            "\U0001F41B Fix login button"
        )

    def test_extra_leading_emoji_are_dropped(self):
        assert _normalize_emoji_prefix("\U0001F41B \U0001F40D Fix imports") == (
            "\U0001F41B Fix imports"
        )

    def test_adjacent_extra_emoji_are_dropped(self):
        assert _normalize_emoji_prefix("\U0001F41B\U0001F40D Fix imports") == (
            "\U0001F41B Fix imports"
        )

    def test_trailing_emoji_is_moved_to_front(self):
        assert _normalize_emoji_prefix("Fix login button \U0001F41B") == (
            "\U0001F41B Fix login button"
        )

    def test_consecutive_flags_normalized_to_one(self):
        """Regression companion to the grapheme test, at the public boundary."""
        title = "\U0001F1EF\U0001F1F5\U0001F1FA\U0001F1F8 Fix imports"
        assert _normalize_emoji_prefix(title) == "\U0001F1EF\U0001F1F5 Fix imports"

    def test_plain_title_is_untouched(self):
        """We never invent an emoji — no-emoji output degrades to today's."""
        assert _normalize_emoji_prefix("Fix login button") == "Fix login button"

    def test_emoji_only_title_is_kept(self):
        assert _normalize_emoji_prefix("\U0001F41B") == "\U0001F41B"

    def test_zwj_sequence_prefix_survives(self):
        title = "\U0001F468\u200D\U0001F4BB Refactor auth module"
        assert _normalize_emoji_prefix(title) == title

    def test_empty_input(self):
        assert _normalize_emoji_prefix("") == ""
        assert _normalize_emoji_prefix(None) == ""  # type: ignore[arg-type]

    def test_cjk_text_is_not_treated_as_emoji(self):
        """CJK code points must not be mistaken for pictographs."""
        assert _normalize_emoji_prefix("修复登录按钮") == "修复登录按钮"


class TestGenerateTitleEmojiIntegration:
    """The prompt rule and the normalization are wired to the config flag."""

    @staticmethod
    def _mock_llm(content):
        """Return (callable, captured) — kwargs land in the captured dict."""
        captured = {}

        def _call(**kwargs):
            captured.update(kwargs)
            resp = MagicMock()
            resp.choices = [MagicMock()]
            resp.choices[0].message.content = content
            return resp

        return _call, captured

    def test_rule_absent_when_disabled(self):
        mock, captured = self._mock_llm('{"title": "Fix login button"}')
        a, b = _patch_config(_cfg(enabled=True))
        with a, b, patch("agent.title_generator.call_llm", mock):
            title = generate_title("the login button does nothing on mobile")

        prompt = captured["messages"][0]["content"]
        assert _EMOJI_RULE not in prompt
        assert title == "Fix login button"

    def test_rule_present_when_enabled(self):
        mock, captured = self._mock_llm('{"title": "\U0001F41B Fix login button"}')
        a, b = _patch_config(_cfg(enabled=True, emoji_prefix=True))
        with a, b, patch("agent.title_generator.call_llm", mock):
            title = generate_title("the login button does nothing on mobile")

        prompt = captured["messages"][0]["content"]
        assert _EMOJI_RULE in prompt
        assert title == "\U0001F41B Fix login button"

    def test_model_output_is_normalized(self):
        """A sloppy model answer is coerced, not stored verbatim."""
        mock, _ = self._mock_llm('{"title": "\U0001F41B\U0001F40D Fix imports"}')
        a, b = _patch_config(_cfg(enabled=True, emoji_prefix=True))
        with a, b, patch("agent.title_generator.call_llm", mock):
            assert generate_title("imports are broken") == (
                "\U0001F41B Fix imports"
            )

    def test_ignored_instruction_degrades_to_plain_title(self):
        mock, _ = self._mock_llm('{"title": "Fix login button"}')
        a, b = _patch_config(_cfg(enabled=True, emoji_prefix=True))
        with a, b, patch("agent.title_generator.call_llm", mock):
            assert generate_title("login is broken") == "Fix login button"

    def test_language_rule_still_applies(self):
        """Pinned language must survive the emoji rule injection."""
        mock, captured = self._mock_llm('{"title": "\U0001F41B Corriger le bouton"}')
        a, b = _patch_config(
            _cfg(enabled=True, emoji_prefix=True, language="French")
        )
        with a, b, patch("agent.title_generator.call_llm", mock):
            generate_title("le bouton ne marche pas")

        prompt = captured["messages"][0]["content"]
        assert "French" in prompt
        assert _EMOJI_RULE in prompt

    def test_no_extra_llm_call(self):
        """The emoji rides the existing request — no added latency."""
        calls = []

        def _call(**kwargs):
            calls.append(kwargs)
            resp = MagicMock()
            resp.choices = [MagicMock()]
            resp.choices[0].message.content = '{"title": "\U0001F41B Fix it"}'
            return resp

        a, b = _patch_config(_cfg(enabled=True, emoji_prefix=True))
        with a, b, patch("agent.title_generator.call_llm", _call):
            generate_title("something is broken")

        assert len(calls) == 1
