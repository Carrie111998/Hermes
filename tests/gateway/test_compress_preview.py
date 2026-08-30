"""Tests for gateway /compress --preview/--dry-run/--aggressive flags
(PR #3243 salvage).

The preview path must return a report WITHOUT building an agent or
touching the transcript; --aggressive must return an explanatory
message rather than being mis-parsed as a focus topic.
"""

from datetime import datetime
from unittest.mock import MagicMock

import pytest

from gateway.config import GatewayConfig, Platform, PlatformConfig
from gateway.platforms.base import MessageEvent
from gateway.session import SessionEntry, SessionSource, build_session_key


def _make_source() -> SessionSource:
    return SessionSource(
        platform=Platform.TELEGRAM,
        user_id="u1",
        chat_id="c1",
        user_name="tester",
        chat_type="dm",
    )


def _make_event(text: str) -> MessageEvent:
    return MessageEvent(text=text, source=_make_source(), message_id="m1")


def _make_history(n_pairs: int = 3) -> list[dict[str, str]]:
    h: list[dict[str, str]] = []
    for i in range(n_pairs):
        h.append({"role": "user", "content": f"u{i}"})
        h.append({"role": "assistant", "content": f"a{i}"})
    return h


def _make_runner(history: list[dict[str, str]]):
    from gateway.run import GatewayRunner

    runner = object.__new__(GatewayRunner)
    runner.config = GatewayConfig(
        platforms={Platform.TELEGRAM: PlatformConfig(enabled=True, token="***")}
    )
    session_entry = SessionEntry(
        session_key=build_session_key(_make_source()),
        session_id="sess-1",
        created_at=datetime.now(),
        updated_at=datetime.now(),
        platform=Platform.TELEGRAM,
        chat_type="dm",
    )
    runner.session_store = MagicMock()
    runner.session_store.get_or_create_session.return_value = session_entry
    runner.session_store.load_transcript.return_value = history
    runner.session_store.rewrite_transcript = MagicMock()
    runner.session_store.update_session = MagicMock()
    runner.session_store._save = MagicMock()
    runner._session_db = None
    return runner


@pytest.mark.asyncio
async def test_preview_with_here_boundary():
    runner = _make_runner(_make_history(4))
    result = await runner._handle_compress_command(
        _make_event("/compress --preview here 2")
    )
    assert "last 2 exchange" in result
    assert "4 of 8" in result
    runner.session_store.rewrite_transcript.assert_not_called()


@pytest.mark.asyncio
async def test_aggressive_dry_run_shows_preview_plus_note():
    runner = _make_runner(_make_history(3))
    result = await runner._handle_compress_command(
        _make_event("/compress --aggressive --dry-run")
    )
    assert "no changes made" in result.lower()
    assert "--aggressive is not supported" in result
    runner.session_store.rewrite_transcript.assert_not_called()


def _make_tool_history() -> list[dict]:
    return [
        {"role": "user", "content": "read the config"},
        {"role": "assistant", "content": "",
         "tool_calls": [{"id": "c1", "function": {"name": "terminal", "arguments": "{}"}}]},
        {"role": "tool", "tool_call_id": "c1", "content": "X" * 2000},
        {"role": "assistant", "content": "done."},
    ]


@pytest.mark.asyncio
async def test_preview_counts_tool_results_and_tool_call_turns():
    """#98360: the preview must count the rows the real run compresses
    (user/assistant/tool, tool results included) — not a user+assistant,
    content-only subset that dropped tool results and empty-content tool_call
    turns, undercounting both tokens and the "N of M" message count."""
    runner = _make_runner(_make_tool_history())
    result = await runner._handle_compress_command(
        _make_event("/compress --preview")
    )
    # All four compressible rows counted, not just the two user+assistant
    # rows with non-empty content the old filter kept.
    assert "4 of 4" in result
    assert "2 of 2" not in result
    runner.session_store.rewrite_transcript.assert_not_called()


