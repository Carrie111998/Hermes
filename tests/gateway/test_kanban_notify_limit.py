"""Configurable kanban notification excerpt limits (kanban.notify_max_chars).

The notifier historically hard-coded excerpt budgets (200 for summaries and
errors, 160 for legacy results and blocked reasons), which silently chopped
worker handoffs mid-information. ``notify_max_chars`` raises the budget while
keeping a single shared ceiling: Slack hard-caps messages at 4000 chars, so
the knob clamps to 3500 and falls back to the 200-char default on junk input.
"""

import asyncio

import pytest

from gateway.config import Platform
from gateway.kanban_watchers import (
    _notify_max_chars_from_config,
    _resolve_notify_max_chars,
)
from gateway.run import GatewayRunner
from hermes_cli import kanban_db as kb


class RecordingAdapterStub:
    def __init__(self):
        self.sent = []

    async def send(self, chat_id, text, metadata=None):
        self.sent.append({"chat_id": chat_id, "text": text, "metadata": metadata or {}})

    async def handle_message(self, event):
        pass


def _make_runner(adapter=None):
    runner = GatewayRunner.__new__(GatewayRunner)
    runner._running = True
    runner.adapters = {Platform.TELEGRAM: adapter or RecordingAdapterStub()}
    runner._kanban_sub_fail_counts = {}
    runner._kanban_dispatcher_lock_handle = object()
    return runner


# --------------------------------------------------------------------------
# Config resolution
# --------------------------------------------------------------------------


def test_default_is_200_when_config_absent():
    assert _notify_max_chars_from_config({}) == 200
    assert _notify_max_chars_from_config({"kanban": {}}) == 200


def test_valid_config_value_is_used():
    assert _notify_max_chars_from_config({"kanban": {"notify_max_chars": 1200}}) == 1200


def test_min_is_one():
    # 0 and negatives would silently swallow the whole ping; clamp to 1.
    assert _notify_max_chars_from_config({"kanban": {"notify_max_chars": 0}}) == 1
    assert _notify_max_chars_from_config({"kanban": {"notify_max_chars": -50}}) == 1


def test_max_is_3500_slack_ceiling():
    assert _notify_max_chars_from_config({"kanban": {"notify_max_chars": 9999}}) == 3500


def test_junk_values_fall_back_to_default():
    for junk in ("banana", None, [400]):
        assert _notify_max_chars_from_config({"kanban": {"notify_max_chars": junk}}) == 200


def test_float_with_integral_value_is_accepted():
    assert _notify_max_chars_from_config({"kanban": {"notify_max_chars": 800.0}}) == 800


def test_resolve_uses_load_config(monkeypatch):
    # _resolve_notify_max_chars imports load_config from hermes_cli.config at
    # call time, so patch the attribute at its source module.
    monkeypatch.setattr(
        "hermes_cli.config.load_config",
        lambda: {"kanban": {"notify_max_chars": 2500}},
    )
    runner = _make_runner()
    assert _resolve_notify_max_chars(runner) == 2500
    assert runner._kanban_notify_max_chars == 2500


def test_resolve_falls_back_when_config_loader_missing(monkeypatch):
    def _boom():
        raise RuntimeError("no config")

    monkeypatch.setattr("hermes_cli.config.load_config", _boom)
    runner = _make_runner()
    _resolve_notify_max_chars(runner)
    assert runner._kanban_notify_max_chars == 200


# --------------------------------------------------------------------------
# End-to-end: a completed event's summary honours the resolved limit
# --------------------------------------------------------------------------


async def _run_one_tick(runner, monkeypatch):
    real_sleep = asyncio.sleep

    async def fake_sleep(delay):
        if delay == 5:
            return None
        runner._running = False
        await real_sleep(0)

    monkeypatch.setattr(asyncio, "sleep", fake_sleep)
    await runner._kanban_notifier_watcher(interval=1)


def test_long_summary_is_delivered_up_to_resolved_limit(tmp_path, monkeypatch):
    long_summary = "x" * 900 + " END"
    db_path = tmp_path / "limit.db"
    monkeypatch.setenv("HERMES_KANBAN_DB", str(db_path))
    kb.init_db()

    conn = kb.connect()
    try:
        tid = kb.create_task(conn, title="long handoff", assignee="worker")
        kb.add_notify_sub(conn, task_id=tid, platform="telegram", chat_id="chat-1")
        kb.complete_task(conn, tid, summary=long_summary)
    finally:
        conn.close()

    runner = _make_runner()
    monkeypatch.setattr(
        "hermes_cli.config.load_config",
        lambda: {"kanban": {"notify_max_chars": 1000}},
    )

    asyncio.run(_run_one_tick(runner, monkeypatch))

    sent = runner.adapters[Platform.TELEGRAM].sent
    assert sent, "notifier did not send any message"
    text = sent[0]["text"]
    assert "END" in text, "summary was truncated below the configured limit"


def test_default_limit_still_truncates_long_summary(tmp_path, monkeypatch):
    long_summary = "y" * 900 + " END"
    db_path = tmp_path / "default-limit.db"
    monkeypatch.setenv("HERMES_KANBAN_DB", str(db_path))
    kb.init_db()

    conn = kb.connect()
    try:
        tid = kb.create_task(conn, title="long handoff default", assignee="worker")
        kb.add_notify_sub(conn, task_id=tid, platform="telegram", chat_id="chat-1")
        kb.complete_task(conn, tid, summary=long_summary)
    finally:
        conn.close()

    runner = _make_runner()
    runner._kanban_notify_max_chars = 200

    asyncio.run(_run_one_tick(runner, monkeypatch))

    sent = runner.adapters[Platform.TELEGRAM].sent
    assert sent, "notifier did not send any message"
    text = sent[0]["text"]
    assert "END" not in text, "default limit did not truncate"
    assert "y" * 100 in text


def test_write_time_event_payload_carries_full_summary(tmp_path, monkeypatch):
    # Two-stage truncation guard: kanban_db.complete_task writes the event
    # payload the notifier renders. If the write-time cap is ever lowered
    # below the notifier ceiling again, this catches it — the payload must
    # carry at least the notifier's max budget.
    db_path = tmp_path / "write-cap.db"
    monkeypatch.setenv("HERMES_KANBAN_DB", str(db_path))
    kb.init_db()

    conn = kb.connect()
    try:
        tid = kb.create_task(conn, title="write cap", assignee="worker")
        kb.complete_task(conn, tid, summary="z" * 900 + " TAIL")
    finally:
        conn.close()

    conn = kb.connect()
    try:
        row = conn.execute(
            "SELECT payload FROM task_events WHERE task_id=? AND kind='completed'",
            (tid,),
        ).fetchone()
    finally:
        conn.close()
    import json

    payload = json.loads(row[0])
    summary = payload.get("summary") or ""
    assert "TAIL" in summary, (
        "complete_task truncated the event payload below the notifier ceiling "
        f"(payload summary is {len(summary)} chars)"
    )


def test_block_task_event_payload_keeps_full_reason(tmp_path, monkeypatch):
    db_path = tmp_path / "block-cap.db"
    monkeypatch.setenv("HERMES_KANBAN_DB", str(db_path))
    kb.init_db()

    conn = kb.connect()
    try:
        tid = kb.create_task(conn, title="block cap", assignee="worker")
        kb.block_task(conn, tid, reason="R" * 900 + " TAIL", kind="needs_input")
    finally:
        conn.close()

    conn = kb.connect()
    try:
        row = conn.execute(
            "SELECT payload FROM task_events WHERE task_id=? AND kind='blocked'",
            (tid,),
        ).fetchone()
    finally:
        conn.close()
    import json

    payload = json.loads(row[0])
    assert "TAIL" in (payload.get("reason") or "")
