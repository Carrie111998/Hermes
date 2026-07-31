"""Tests for the volatile-tail date line (``ensure_current_date_line``).

Long-lived gateway sessions span days.  The system prompt is built once per
session and replayed verbatim for prefix-cache warmth, so the
``Conversation started:`` date (a session-start value by design) goes stale
and models copy it into long-term memory.  The per-call volatile tail
injects ``Today's date: <date>`` on the outgoing wire system message only --
the stored prompt and the session DB row are never mutated.

Every date is mocked through ``hermes_time.now`` with a tz-aware datetime so
the helper's real formatting path is exercised.
"""

from __future__ import annotations

import re
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from agent.chat_completion_helpers import ensure_current_date_line
from agent.conversation_loop import (
    _restore_or_build_system_prompt,
    _sync_failover_system_message,
)
from agent.prompt_caching import apply_anthropic_cache_control


_MAY_17 = datetime(2026, 5, 17, 9, 30, tzinfo=timezone.utc)
_MAY_18 = datetime(2026, 5, 18, 9, 0, tzinfo=timezone.utc)

_DATE_SUNDAY = "Today's date: Sunday, May 17, 2026"
_DATE_MONDAY = "Today's date: Monday, May 18, 2026"


class TestEnsureCurrentDateLine:
    """Unit coverage for the helper itself."""

    def test_appends_date_when_missing(self):
        prompt = (
            "You are Hermes Agent.\n"
            "\n"
            "Conversation started: Wednesday, June 10, 2026"
        )
        with patch("hermes_time.now", return_value=_MAY_17):
            result = ensure_current_date_line(prompt)
        # Prefix bytes unchanged; date appended after a blank line.
        assert result.startswith(prompt)
        assert result == prompt + "\n\n" + _DATE_SUNDAY

    def test_appends_after_trailing_newline(self):
        with patch("hermes_time.now", return_value=_MAY_17):
            result = ensure_current_date_line("prefix line\n")
        assert result == "prefix line\n" + _DATE_SUNDAY

    def test_replaces_only_last_occurrence(self):
        prompt = (
            "Memory echo from user:\n"
            "Today's date: Sunday, May 16, 2026\n"
            "\n"
            "Conversation started: Wednesday, June 10, 2026\n"
            "Today's date: Saturday, May 16, 2026"
        )
        with patch("hermes_time.now", return_value=_MAY_17):
            result = ensure_current_date_line(prompt)
        # Earlier occurrences may be user content / memory echoes: untouched.
        assert "Today's date: Sunday, May 16, 2026" in result
        # Only the LAST line is replaced.
        assert "Today's date: Saturday, May 16, 2026" not in result
        assert result.endswith(_DATE_SUNDAY)

    def test_idempotent_same_day_returns_same_object(self):
        prompt = "Cached prompt.\n\n" + _DATE_SUNDAY
        with patch("hermes_time.now", return_value=_MAY_17):
            result = ensure_current_date_line(prompt)
        assert result is prompt

    def test_date_boundary_replaces_old_date(self):
        prefix = (
            "You are Hermes Agent.\n"
            "\n"
            "Conversation started: Sunday, May 17, 2026\n"
        )
        prompt = prefix + _DATE_SUNDAY
        with patch("hermes_time.now", return_value=_MAY_18):
            result = ensure_current_date_line(prompt)
        # Bytes before the date line stay identical; the stale tail is gone.
        assert result.startswith(prefix)
        assert _DATE_SUNDAY not in result
        assert result.endswith(_DATE_MONDAY)

    def test_noop_on_empty_or_non_string(self):
        for value in ("", None, 123):
            with patch("hermes_time.now", return_value=_MAY_17):
                result = ensure_current_date_line(value)
            assert result is value

    def test_date_only_format_no_time_component(self):
        with patch("hermes_time.now", return_value=_MAY_17):
            result = ensure_current_date_line("Prompt")
        last_line = result.rsplit("\n", 1)[-1]
        assert re.match(r"^Today's date: \w+, \w+ \d{1,2}, \d{4}$", last_line)
        assert "09:30" not in result
        assert "9:30" not in result

    def test_ephemeral_preserved_and_date_appended_after_it(self):
        sp = "Cached system prompt."
        ephemeral = "Session-specific instructions."
        agent = SimpleNamespace(
            _cached_system_prompt=sp,
            ephemeral_system_prompt=ephemeral,
        )
        api_messages = [
            {"role": "system", "content": sp},
            {"role": "user", "content": "hi"},
        ]
        with patch("hermes_time.now", return_value=_MAY_17):
            _sync_failover_system_message(agent, api_messages, sp)
        content = api_messages[0]["content"]
        assert content.startswith(sp)
        assert ephemeral in content
        assert content.index(ephemeral) < content.index(_DATE_SUNDAY)
        assert content.endswith(_DATE_SUNDAY)
        # The cached prompt (and the DB row it mirrors) stays untouched.
        assert agent._cached_system_prompt == sp


class TestRestoreBoundary:
    """Cross-day restore: cached prompt + DB row stay byte-identical."""

    def test_boundary_keeps_cached_and_db_row_unchanged(self):
        stored = (
            "You are Hermes Agent.\n"
            "\n"
            "Conversation started: Saturday, May 16, 2026\n"
            "Session ID: test-session-id\n"
            "Model: test-model\n"
            "Provider: openrouter\n"
            "Platform: cli"
        )
        db = MagicMock()
        db.get_session.return_value = {"system_prompt": stored}
        agent = MagicMock()
        agent._cached_system_prompt = None
        agent.session_id = "test-session-id"
        agent.model = "test-model"
        agent.provider = "openrouter"
        agent.platform = "cli"
        agent._session_db = db
        agent._use_prompt_caching = False
        agent._build_system_prompt = MagicMock(return_value="BUILT_PROMPT")

        with patch("hermes_time.now", return_value=_MAY_18):
            _restore_or_build_system_prompt(
                agent, None, [{"role": "user", "content": "hi"}]
            )

            # Restore keeps the stored prompt verbatim and writes nothing back.
            assert agent._cached_system_prompt == stored
            agent._build_system_prompt.assert_not_called()
            db.update_system_prompt.assert_not_called()
            # The wire assembly injects the current date on top of the
            # untouched cached copy; the session-start line is not rewritten.
            wire = ensure_current_date_line(agent._cached_system_prompt)
            assert _DATE_MONDAY in wire
            assert "Saturday, May 16, 2026" in wire


class TestFailoverSync:
    """Failover sync keeps cache decoration while refreshing the date tail."""

    _STATIC = "You are a helpful assistant.\n\nStable brief.\n"

    def _decorated(self, prompt):
        messages = [
            {"role": "system", "content": prompt},
            {"role": "user", "content": "what model are you?"},
        ]
        return apply_anthropic_cache_control(
            messages,
            cache_ttl=None,
            native_anthropic=True,
            static_system_prefix=self._STATIC,
        )

    def test_failover_sync_wire_new_date_keeps_decoration(self):
        prompt = self._STATIC + "Model: gpt-5.4-mini\nProvider: openai-codex"
        agent = SimpleNamespace(
            _cached_system_prompt=prompt,
            ephemeral_system_prompt=None,
        )
        api_messages = self._decorated(prompt)
        assert isinstance(api_messages[0]["content"], list)

        with patch("hermes_time.now", return_value=_MAY_17):
            _sync_failover_system_message(agent, api_messages, prompt)

        content = api_messages[0]["content"]
        assert isinstance(content, list), "cache decoration was flattened to a string"
        assert len(content) == 2
        assert all(part.get("cache_control") for part in content), (
            "the failover retry would ship zero system cache breakpoints"
        )
        # The static prefix must stay byte-identical or its cache entry misses.
        assert content[0]["text"] == self._STATIC
        # The date tail lands in the volatile block.
        assert _DATE_SUNDAY in content[1]["text"]
        assert agent._cached_system_prompt == prompt

    def test_failover_sync_single_block_shape_preserved(self):
        prompt = "Model: gpt-5.4-mini\nProvider: openai-codex"
        agent = SimpleNamespace(
            _cached_system_prompt=prompt,
            ephemeral_system_prompt=None,
        )
        # No static prefix match -> the single-block fallback layout.
        api_messages = apply_anthropic_cache_control(
            [{"role": "system", "content": prompt}],
            cache_ttl=None,
            native_anthropic=True,
            static_system_prefix=None,
        )
        assert isinstance(api_messages[0]["content"], list)
        assert len(api_messages[0]["content"]) == 1

        with patch("hermes_time.now", return_value=_MAY_17):
            _sync_failover_system_message(agent, api_messages, prompt)

        content = api_messages[0]["content"]
        assert isinstance(content, list) and len(content) == 1
        assert content[0].get("cache_control")
        assert _DATE_SUNDAY in content[0]["text"]
        assert agent._cached_system_prompt == prompt

    def test_failover_sync_unknown_shape_falls_back_to_string(self):
        prompt = "Cached prompt with identity lines."
        agent = SimpleNamespace(
            _cached_system_prompt=prompt,
            ephemeral_system_prompt=None,
        )
        # A non-text block shape is not patchable in place; the caller falls
        # back to the plain-string assignment (existing behavior).
        api_messages = [
            {
                "role": "system",
                "content": [
                    {"type": "text", "text": prompt},
                    {"type": "thinking", "text": "internal"},
                ],
            },
            {"role": "user", "content": "hi"},
        ]
        with patch("hermes_time.now", return_value=_MAY_17):
            result = _sync_failover_system_message(agent, api_messages, prompt)
        assert isinstance(api_messages[0]["content"], str)
        assert api_messages[0]["content"] == prompt + "\n\n" + _DATE_SUNDAY
        assert result == prompt
        assert agent._cached_system_prompt == prompt
