"""Tests for rate_limit_episode_state module."""

import json
import tempfile
from pathlib import Path
from unittest.mock import patch

import pytest

from events import rate_limit_episode_state, paths


class TestLoadState:
    """Test _load_state() fail-open behavior."""

    def test_load_missing_file_returns_empty_dict(self, tmp_path):
        """Loading a missing state file returns {} without error."""
        missing = tmp_path / "does_not_exist.json"
        with patch("events.paths.rate_limit_state_path", return_value=missing):
            result = rate_limit_episode_state._load_state()
        assert result == {}

    def test_load_valid_json_returns_dict(self, tmp_path):
        """Loading valid JSON state returns the deserialized dict."""
        state_file = tmp_path / "state.json"
        expected = {
            "deepseek/deepseek-v4-pro": {
                "episode_opened_at": "2026-08-14T10:00:00Z",
                "last_hit_at": "2026-08-14T10:05:00Z",
                "hit_count": 3,
            }
        }
        state_file.write_text(json.dumps(expected), encoding="utf-8")

        with patch("events.paths.rate_limit_state_path", return_value=state_file):
            result = rate_limit_episode_state._load_state()
        assert result == expected

    def test_load_malformed_json_returns_empty_dict(self, tmp_path):
        """Loading malformed JSON returns {} without raising."""
        state_file = tmp_path / "state.json"
        state_file.write_text("{invalid json}", encoding="utf-8")

        with patch("events.paths.rate_limit_state_path", return_value=state_file):
            result = rate_limit_episode_state._load_state()
        assert result == {}

    def test_load_non_dict_toplevel_returns_empty_dict(self, tmp_path):
        """Loading JSON that is not a dict at top level returns {}."""
        state_file = tmp_path / "state.json"
        state_file.write_text(json.dumps(["list", "not", "dict"]), encoding="utf-8")

        with patch("events.paths.rate_limit_state_path", return_value=state_file):
            result = rate_limit_episode_state._load_state()
        assert result == {}

    def test_load_unreadable_file_returns_empty_dict(self, tmp_path):
        """Loading an unreadable file returns {} without raising."""
        state_file = tmp_path / "state.json"
        state_file.write_text(json.dumps({}), encoding="utf-8")
        # Make file unreadable
        state_file.chmod(0o000)

        try:
            with patch("events.paths.rate_limit_state_path", return_value=state_file):
                result = rate_limit_episode_state._load_state()
            assert result == {}
        finally:
            # Restore permission so pytest can clean up
            state_file.chmod(0o644)


class TestSaveState:
    """Test _save_state() atomic write behavior."""

    def test_save_creates_file_with_valid_json(self, tmp_path):
        """Saving state creates a valid JSON file."""
        state_file = tmp_path / "state.json"
        state = {
            "deepseek/deepseek-v4-pro": {
                "episode_opened_at": "2026-08-14T10:00:00Z",
                "hit_count": 5,
            }
        }

        with patch("events.paths.rate_limit_state_path", return_value=state_file):
            result = rate_limit_episode_state._save_state(state)

        assert result is True
        assert state_file.exists()
        with open(state_file, encoding="utf-8") as f:
            loaded = json.load(f)
        assert loaded == state

    def test_save_empty_dict_succeeds(self, tmp_path):
        """Saving an empty dict succeeds."""
        state_file = tmp_path / "state.json"

        with patch("events.paths.rate_limit_state_path", return_value=state_file):
            result = rate_limit_episode_state._save_state({})

        assert result is True
        assert state_file.exists()
        with open(state_file, encoding="utf-8") as f:
            loaded = json.load(f)
        assert loaded == {}

    def test_save_overwrites_existing_file(self, tmp_path):
        """Saving state overwrites an existing state file."""
        state_file = tmp_path / "state.json"
        # Write initial state
        old_state = {"old_provider/model": {"hit_count": 1}}
        state_file.write_text(json.dumps(old_state), encoding="utf-8")

        # Overwrite with new state
        new_state = {"new_provider/model": {"hit_count": 2}}

        with patch("events.paths.rate_limit_state_path", return_value=state_file):
            result = rate_limit_episode_state._save_state(new_state)

        assert result is True
        with open(state_file, encoding="utf-8") as f:
            loaded = json.load(f)
        assert loaded == new_state
        assert "old_provider/model" not in loaded

    def test_save_creates_parent_directory(self, tmp_path):
        """Saving state creates parent directories if needed."""
        state_file = tmp_path / "nested" / "dir" / "state.json"
        state = {"provider/model": {"hit_count": 1}}

        with patch("events.paths.rate_limit_state_path", return_value=state_file):
            result = rate_limit_episode_state._save_state(state)

        assert result is True
        assert state_file.exists()

    @pytest.mark.skipif(True, reason="Windows chmod doesn't prevent writes like Unix")
    def test_save_to_unwritable_directory_returns_false(self, tmp_path):
        """Saving to an unwritable directory returns False."""
        state_file = tmp_path / "state.json"
        state = {"provider/model": {"hit_count": 1}}

        # Make temp dir unwritable
        tmp_path.chmod(0o555)

        try:
            with patch("events.paths.rate_limit_state_path", return_value=state_file):
                result = rate_limit_episode_state._save_state(state)
            assert result is False
        finally:
            # Restore permission so pytest can clean up
            tmp_path.chmod(0o755)


class TestLoadSaveRoundTrip:
    """Test that load/save preserve state correctly."""

    def test_roundtrip_preserves_complex_state(self, tmp_path):
        """Save then load preserves the original state."""
        state_file = tmp_path / "state.json"
        original = {
            "deepseek/deepseek-v4-pro": {
                "episode_opened_at": "2026-08-14T10:00:00Z",
                "last_hit_at": "2026-08-14T10:05:30Z",
                "hit_count": 5,
                "diverted_calls": 12,
                "reason": "rate_limit",
                "outcome": "diverted",
            },
            "openai-codex/gpt-5.6-sol": {
                "episode_opened_at": "2026-08-14T09:30:00Z",
                "hit_count": 1,
            },
        }

        with patch("events.paths.rate_limit_state_path", return_value=state_file):
            # Save
            assert rate_limit_episode_state._save_state(original) is True
            # Load
            loaded = rate_limit_episode_state._load_state()

        assert loaded == original

    def test_empty_state_roundtrip(self, tmp_path):
        """Save and load empty state works correctly."""
        state_file = tmp_path / "state.json"

        with patch("events.paths.rate_limit_state_path", return_value=state_file):
            assert rate_limit_episode_state._save_state({}) is True
            loaded = rate_limit_episode_state._load_state()

        assert loaded == {}
