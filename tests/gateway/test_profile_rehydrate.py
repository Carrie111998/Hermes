"""Tests for session-metadata rehydration of /profile choices.

Covers ``GatewayRunner._rehydrate_profile_from_session`` — the lookup step
that turns a previously-picked profile (saved via ``set_session_metadata``)
back into ``source.profile`` on the next inbound event, surviving gateway
restarts.
"""

import sys
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest


def _ensure_telegram_mock():
    if "telegram" in sys.modules and hasattr(sys.modules["telegram"], "__file__"):
        return
    mod = MagicMock()
    mod.ext.ContextTypes.DEFAULT_TYPE = type(None)
    mod.constants.ParseMode.MARKDOWN_V2 = "MarkdownV2"
    for name in ("telegram", "telegram.ext", "telegram.constants", "telegram.request"):
        sys.modules.setdefault(name, mod)


_ensure_telegram_mock()

from gateway.run import GatewayRunner  # noqa: E402


def _make_runner(entries):
    """Build a bare GatewayRunner with a stubbed session_store.

    Only the bits the rehydration method touches are real — everything else
    is a MagicMock to keep the test independent of gateway init.
    """
    store = MagicMock()
    store._entries = entries
    runner = GatewayRunner.__new__(GatewayRunner)
    runner.session_store = store
    runner.config = MagicMock(multiplex_profiles=True)
    return runner


def _entry(platform, chat_id, thread_id=None, user_id=None, profile=None):
    """Build a fake session entry with the shape _rehydrate expects."""
    origin = {"platform": platform, "chat_id": chat_id}
    if thread_id:
        origin["thread_id"] = thread_id
    if user_id:
        origin["user_id"] = user_id
    return SimpleNamespace(origin=origin, metadata={"profile": profile} if profile else {})


def _source(platform="telegram", chat_id="12345", thread_id=None, user_id=None):
    return SimpleNamespace(
        platform=SimpleNamespace(value=platform),
        chat_id=chat_id,
        thread_id=thread_id,
        user_id=user_id,
        username=None,
    )


class TestRehydrateProfile:
    def test_returns_picked_profile_when_match(self):
        entries = {
            "agent:social:telegram:dm:12345": _entry("telegram", "12345", profile="social"),
        }
        runner = _make_runner(entries)
        result = runner._rehydrate_profile_from_session(_source(chat_id="12345"))
        assert result == "social"

    def test_returns_none_when_no_match(self):
        entries = {
            "agent:social:telegram:dm:99999": _entry("telegram", "99999", profile="social"),
        }
        runner = _make_runner(entries)
        result = runner._rehydrate_profile_from_session(_source(chat_id="12345"))
        assert result is None

    def test_platform_mismatch_excluded(self):
        entries = {
            "agent:social:discord:dm:12345": _entry("discord", "12345", profile="social"),
        }
        runner = _make_runner(entries)
        result = runner._rehydrate_profile_from_session(_source(platform="telegram", chat_id="12345"))
        assert result is None

    def test_empty_profile_metadata_excluded(self):
        entries = {
            "agent:main:telegram:dm:12345": _entry("telegram", "12345", profile=""),
        }
        runner = _make_runner(entries)
        result = runner._rehydrate_profile_from_session(_source(chat_id="12345"))
        assert result is None

    def test_no_session_store_returns_none(self):
        runner = GatewayRunner.__new__(GatewayRunner)
        runner.session_store = None
        assert runner._rehydrate_profile_from_session(_source()) is None

    def test_empty_entries_returns_none(self):
        runner = _make_runner({})
        assert runner._rehydrate_profile_from_session(_source()) is None

    def test_thread_id_wildcard_when_source_has_none(self):
        entries = {
            "agent:social:telegram:dm:12345:99": _entry(
                "telegram", "12345", thread_id="99", profile="social"
            ),
        }
        runner = _make_runner(entries)
        # Source has thread_id=None — treated as "any thread" so the entry
        # for thread 99 still matches (the user may have switched topics).
        result = runner._rehydrate_profile_from_session(
            _source(chat_id="12345", thread_id=None)
        )
        assert result == "social"

    def test_thread_id_match_required_when_both_set(self):
        entries = {
            "agent:social:telegram:dm:12345:99": _entry(
                "telegram", "12345", thread_id="99", profile="social"
            ),
            "agent:coder:telegram:dm:12345:42": _entry(
                "telegram", "12345", thread_id="42", profile="coder"
            ),
        }
        runner = _make_runner(entries)
        # Source has thread_id=99 — must match the thread-99 entry, not thread-42.
        result = runner._rehydrate_profile_from_session(
            _source(chat_id="12345", thread_id="99")
        )
        assert result == "social"