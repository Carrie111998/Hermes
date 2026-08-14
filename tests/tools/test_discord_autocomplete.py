"""Tests for Discord slash-command autocomplete option fidelity (feature I3)."""

import pytest

from plugins.platforms.discord.autocomplete import (
    AutocompleteChoice,
    AutocompleteError,
    build_choices,
    normalize_clicked_value,
    roundtrip_value,
)


class TestBuildChoices:
    def test_filters_by_case_insensitive_substring(self):
        options = ["Alpha", "beta", "alphabet", "Gamma", "Delta"]
        choices = build_choices(options, query="ALP")
        assert [c.value for c in choices] == ["Alpha", "alphabet"]

    def test_empty_query_returns_all_options(self):
        options = ["a", "b", "c"]
        choices = build_choices(options, query="")
        assert [c.value for c in choices] == options

    def test_strips_options(self):
        options = ["  spaced  ", "clean"]
        choices = build_choices(options)
        assert [c.name for c in choices] == ["spaced", "clean"]
        assert all(c.name == c.value for c in choices)

    def test_clamps_to_discord_max_of_25(self):
        options = [f"option-{i}" for i in range(40)]
        choices = build_choices(options)
        assert len(choices) == 25
        assert choices[0].value == "option-0"
        assert choices[-1].value == "option-24"

    def test_respects_custom_max_choices(self):
        choices = build_choices(["a", "b", "c"], max_choices=2)
        assert [c.value for c in choices] == ["a", "b"]

    def test_returns_choice_objects(self):
        choices = build_choices(["x"], query="x")
        assert isinstance(choices[0], AutocompleteChoice)

    def test_whitespace_query_matches_all(self):
        choices = build_choices(["a", "b"], query="   ")
        assert [c.value for c in choices] == ["a", "b"]


class TestNormalizeClickedValue:
    def test_exact_match_wins(self):
        assert normalize_clicked_value("Alpha", ["Alpha", "beta"]) == "Alpha"

    def test_case_insensitive_match(self):
        assert normalize_clicked_value("ALPHA", ["Alpha", "beta"]) == "Alpha"

    def test_fallback_returns_clicked_trimmed(self):
        assert normalize_clicked_value("  unknown-value  ", ["Alpha"]) == "unknown-value"

    def test_empty_raises_autocomplete_error(self):
        with pytest.raises(AutocompleteError):
            normalize_clicked_value("", ["Alpha"])

    def test_whitespace_only_raises(self):
        with pytest.raises(ValueError):
            normalize_clicked_value("   ", ["Alpha"])

    def test_exact_match_preferred_over_case_insensitive(self):
        assert normalize_clicked_value("FOO", ["foo", "FOO"]) == "FOO"


class TestRoundtripValue:
    def test_identity(self):
        assert roundtrip_value("some-model-value") == "some-model-value"

    def test_identity_preserves_whitespace(self):
        assert roundtrip_value("  with whitespace  ") == "  with whitespace  "

    def test_identity_empty(self):
        assert roundtrip_value("") == ""
