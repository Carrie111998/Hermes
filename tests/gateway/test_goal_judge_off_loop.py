"""Regression: gateway /goal judge must not block the asyncio event loop.

A standing /goal runs ``evaluate_after_turn`` (sync auxiliary LLM) after each
gateway turn. When that call ran inline on the event loop, a slow or timed-out
local model froze liveness probes and the loop-liveness watchdog hard-exited
with code 75. The judge must run off-loop so messaging stays responsive.
"""

from __future__ import annotations

import asyncio
import time
import uuid
from datetime import datetime
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from gateway.config import GatewayConfig, Platform, PlatformConfig
from gateway.session import SessionEntry, SessionSource, build_session_key


@pytest.fixture()
def hermes_home(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.setenv("HERMES_HOME", str(home))

    from hermes_cli import goals

    goals._DB_CACHE.clear()
    yield home
    goals._DB_CACHE.clear()


def _make_source() -> SessionSource:
    return SessionSource(
        platform=Platform.FEISHU,
        user_id="u1",
        chat_id="c1",
        user_name="tester",
        chat_type="dm",
    )


class _RecordingAdapter:
    def __init__(self) -> None:
        self._pending_messages: dict = {}
        self.sends: list[dict] = []

    async def send(self, chat_id: str, content: str, reply_to=None, metadata=None):
        self.sends.append({"chat_id": chat_id, "content": content, "metadata": metadata})

        class _R:
            success = True
            message_id = "mock-msg"

        return _R()


def _make_runner(session_id: str | None = None):
    from gateway.run import GatewayRunner

    runner = object.__new__(GatewayRunner)
    runner.config = GatewayConfig(
        platforms={Platform.FEISHU: PlatformConfig(enabled=True, token="***")},
    )
    runner.adapters = {}
    runner._running_agents = {}
    runner._running_agents_ts = {}
    runner._queued_events = {}

    src = _make_source()
    session_entry = SessionEntry(
        session_key=build_session_key(src),
        session_id=session_id or f"goal-offloop-{uuid.uuid4().hex[:8]}",
        created_at=datetime.now(),
        updated_at=datetime.now(),
        platform=Platform.FEISHU,
        chat_type="dm",
    )
    runner.session_store = MagicMock()
    runner.session_store.get_or_create_session.return_value = session_entry
    runner.session_store._generate_session_key.return_value = build_session_key(src)

    adapter = _RecordingAdapter()
    runner.adapters[Platform.FEISHU] = adapter
    return runner, adapter, session_entry, src


@pytest.mark.asyncio
async def test_goal_judge_runs_off_event_loop(hermes_home):
    """A blocking judge must not starve other awaitables on the gateway loop."""
    from hermes_cli.goals import GoalManager

    runner, adapter, session_entry, src = _make_runner()
    GoalManager(session_entry.session_id).set("ship the release")

    progress: list[int] = []

    async def _loop_ticks() -> None:
        for i in range(6):
            progress.append(i)
            await asyncio.sleep(0.05)

    def _slow_judge(*_args, **_kwargs):
        time.sleep(0.3)
        return ("continue", "still needs work", False, None, False)

    tick_task = asyncio.create_task(_loop_ticks())
    await asyncio.sleep(0)

    with patch("hermes_cli.goals.judge_goal", side_effect=_slow_judge):
        await runner._post_turn_goal_continuation(
            session_entry=session_entry,
            source=src,
            final_response="partial progress",
        )

    await tick_task

    assert len(progress) >= 3, (
        "event loop made no progress while goal_judge blocked — "
        "judge is still running inline on the gateway loop"
    )
    assert adapter._pending_messages, "continuation must still be enqueued after off-loop judge"


@pytest.mark.asyncio
async def test_inactive_goal_skips_off_loop_evaluate(hermes_home):
    """No standing goal → do not schedule the off-loop judge at all."""
    runner, adapter, session_entry, src = _make_runner()

    with patch("hermes_cli.goals.judge_goal") as judge:
        await runner._post_turn_goal_continuation(
            session_entry=session_entry,
            source=src,
            final_response="hello",
        )

    judge.assert_not_called()
    assert not adapter._pending_messages
