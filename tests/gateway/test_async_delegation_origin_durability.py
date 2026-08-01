"""Behavioral coverage for immutable async-delegation origin routing."""

import json
import queue
from collections import OrderedDict
from datetime import datetime
from unittest.mock import AsyncMock, MagicMock

import pytest

from gateway.config import Platform
from gateway.run import GatewayRunner
from gateway.session import SessionEntry, SessionSource
from gateway.session_context import clear_session_vars, set_session_vars
from tools import async_delegation as ad


def _feishu_source(*, chat_id="chat-origin", message_id="om-origin", profile="coder"):
    return SessionSource(
        platform=Platform.FEISHU,
        chat_id=chat_id,
        chat_type="dm",
        user_id="user-origin",
        message_id=message_id,
        profile=profile,
    )


def _entry(session_id="sess-current"):
    return SessionEntry(
        session_key="agent:coder:feishu:dm:chat-origin",
        session_id=session_id,
        created_at=datetime.now(),
        updated_at=datetime.now(),
        platform=Platform.FEISHU,
        chat_type="dm",
    )


def test_restart_restore_retains_origin_snapshot_and_additively_migrates(tmp_path, monkeypatch):
    db_path = tmp_path / "state.db"
    monkeypatch.setattr(ad, "_db_path", lambda: db_path)

    # Exact pre-change table shape: initialization must add columns in place.
    import sqlite3

    conn = sqlite3.connect(db_path)
    conn.execute(
        """CREATE TABLE async_delegations (
            delegation_id TEXT PRIMARY KEY, origin_session TEXT NOT NULL,
            origin_ui_session_id TEXT NOT NULL DEFAULT '', parent_session_id TEXT,
            state TEXT NOT NULL, dispatched_at REAL NOT NULL, completed_at REAL,
            updated_at REAL NOT NULL, event_json TEXT, result_json TEXT,
            delivery_state TEXT NOT NULL DEFAULT 'pending',
            delivery_attempts INTEGER NOT NULL DEFAULT 0, delivered_at REAL,
            owner_pid INTEGER, owner_started_at INTEGER, task_json TEXT,
            delivery_claim TEXT, delivery_claimed_at REAL,
            origin_session_id TEXT NOT NULL DEFAULT ''
        )"""
    )
    conn.commit()
    conn.close()

    source = _feishu_source().to_dict()
    record = {
        "delegation_id": "deleg-restart",
        "goal": "finish later",
        "context": None,
        "toolsets": None,
        "role": "leaf",
        "model": "m",
        "session_key": "agent:coder:feishu:dm:chat-origin",
        "origin_ui_session_id": "",
        "origin_session_id": "",
        "origin_message_id": "om-origin",
        "origin_source": source,
        "origin_profile": "coder",
        "parent_session_id": "sess-parent",
        "status": "running",
        "dispatched_at": 1.0,
    }
    ad._persist_dispatch(record)
    event = {
        "type": "async_delegation",
        "delegation_id": "deleg-restart",
        "session_key": record["session_key"],
        "parent_session_id": "sess-parent",
        "origin_message_id": "om-origin",
        "origin_source": source,
        "origin_profile": "coder",
        "status": "completed",
        "completed_at": 2.0,
    }
    ad._persist_completion(event, {"summary": "done"})

    restored_queue = queue.Queue()
    assert ad.restore_undelivered_completions(restored_queue) == 1
    restored = restored_queue.get_nowait()
    assert restored["restored"] is True
    assert restored["origin_message_id"] == "om-origin"
    assert restored["origin_source"] == source
    assert restored["origin_profile"] == "coder"

    durable = ad.get_durable_delegation("deleg-restart")
    assert durable["origin_message_id"] == "om-origin"
    assert durable["origin_source"] == source
    assert durable["origin_profile"] == "coder"


def test_capture_is_detached_from_mutable_turn_source(monkeypatch):
    source = _feishu_source()
    payload = source.to_dict()
    tokens = set_session_vars(
        platform="feishu",
        chat_id=source.chat_id,
        chat_type=source.chat_type,
        user_id=source.user_id,
        session_key="agent:coder:feishu:dm:chat-origin",
        message_id=source.message_id,
        profile="coder",
        source_snapshot=payload,
    )
    try:
        captured = ad.capture_current_origin()
        payload["chat_id"] = "chat-mutated"
        source.chat_id = "chat-mutated"
    finally:
        clear_session_vars(tokens)

    assert captured["origin_message_id"] == "om-origin"
    assert captured["origin_source"]["chat_id"] == "chat-origin"
    assert captured["origin_profile"] == "coder"


def test_immutable_event_source_wins_over_mutable_store_and_cache():
    runner = object.__new__(GatewayRunner)
    wrong = _feishu_source(chat_id="chat-new", message_id="om-new")
    entry = MagicMock(origin=wrong)
    runner.session_store = MagicMock()
    runner.session_store._entries = {
        "agent:coder:feishu:dm:chat-origin": entry,
    }
    runner._session_sources = OrderedDict(
        [("agent:coder:feishu:dm:chat-origin", wrong)]
    )

    resolved = runner._build_process_event_source(
        {
            "type": "async_delegation",
            "session_key": "agent:coder:feishu:dm:chat-origin",
            "origin_source": _feishu_source().to_dict(),
            "origin_message_id": "om-origin",
            "origin_profile": "coder",
        }
    )

    assert resolved.chat_id == "chat-origin"
    assert resolved.message_id == "om-origin"
    assert resolved.profile == "coder"


@pytest.mark.asyncio
async def test_synthetic_completion_uses_origin_profile_adapter_and_anchor():
    runner = object.__new__(GatewayRunner)
    default_adapter = MagicMock(supports_async_delivery=True)
    default_adapter.handle_message = AsyncMock()
    coder_adapter = MagicMock(supports_async_delivery=True)
    coder_adapter.handle_message = AsyncMock()
    runner.adapters = {Platform.FEISHU: default_adapter}
    runner._profile_adapters = {"coder": {Platform.FEISHU: coder_adapter}}
    runner._active_profile_name = lambda: "default"

    delivered = await runner._inject_watch_notification(
        "delegation done",
        {
            "type": "async_delegation",
            "session_key": "agent:coder:feishu:dm:chat-origin",
            "origin_source": _feishu_source().to_dict(),
            "origin_message_id": "om-origin",
            "origin_profile": "coder",
            "parent_session_id": "sess-parent",
        },
    )

    assert delivered is True
    default_adapter.handle_message.assert_not_awaited()
    coder_adapter.handle_message.assert_awaited_once()
    event = coder_adapter.handle_message.await_args.args[0]
    assert event.message_id == "om-origin"
    assert event.source.message_id == "om-origin"
    assert event.metadata["gateway_origin_profile"] == "coder"
    assert event.source.platform == Platform.FEISHU
    assert event.source.profile == "coder"


@pytest.mark.asyncio
async def test_compression_continuation_cannot_cross_origin_profile():
    runner = object.__new__(GatewayRunner)
    rows = {
        "sess-parent": {
            "id": "sess-parent",
            "ended_at": "2026-07-31T00:00:00",
            "end_reason": "compression",
            "profile_name": "coder",
        },
        "sess-tip": {
            "id": "sess-tip",
            "ended_at": None,
            "profile_name": "default",
        },
    }
    runner._session_db = MagicMock()
    runner._session_db.get_session = AsyncMock(side_effect=lambda sid: rows.get(sid))
    runner._session_db.get_compression_tip = AsyncMock(return_value="sess-tip")
    runner.session_store = MagicMock()

    resolved = await runner._resolve_async_delegation_session(
        _entry("sess-parent"),
        "sess-parent",
        "coder",
    )

    assert resolved is None
    runner.session_store.switch_session.assert_not_called()
    runner.session_store.advance_compression_session.assert_not_called()


def test_corrupt_new_origin_fails_closed_instead_of_using_cache():
    runner = object.__new__(GatewayRunner)
    runner.session_store = MagicMock()
    runner._session_sources = OrderedDict(
        [("agent:coder:feishu:dm:chat-origin", _feishu_source())]
    )

    assert runner._build_process_event_source(
        {
            "session_key": "agent:coder:feishu:dm:chat-origin",
            "origin_source": {"platform": "not-a-platform"},
        }
    ) is None
