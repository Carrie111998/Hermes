from __future__ import annotations

import argparse
import json
import sqlite3
import uuid
from types import SimpleNamespace

import pytest

from agent import conversation_loop
from hermes_cli.cost import ledger
from hermes_cli.session import api, controller, schema
from hermes_cli.session.estimator import (
    estimate_next_turn_input_tokens,
    estimate_tokens,
)
from hermes_cli.session.rotation_config import ROTATION_CAPS
from hermes_cli.subcommands import session as session_cli
from hermes_cli.verdict import (
    DispatchEnvelope,
    LeafVerdict,
    record_dispatch,
    record_verdict,
)


@pytest.fixture
def db_path(tmp_path):
    return tmp_path / "kanban.db"


def _open(db_path, task="task-1", **kwargs):
    return api.open_session(
        task_id=task,
        lane=kwargs.pop("lane", "platform"),
        profile=kwargs.pop("profile", "atlas"),
        route=kwargs.pop("route", "direct_cli"),
        db_path=db_path,
        **kwargs,
    )


def _dispatch(task: str, attempt: int = 1):
    return DispatchEnvelope(
        task_id=task,
        attempt_number=attempt,
        rung_id="r0_baseline",
        model_slug="test/model",
        mode="single",
        strategy_payload={"attempt": attempt},
    )


def _verdict(task: str, attempt: int = 1):
    return LeafVerdict(
        task_id=task,
        attempt_number=attempt,
        rung_id="r0_baseline",
        model_used="test/model",
        outcome="success",
        confidence=1.0,
        strategy_hash=f"hash-{attempt}",
    )


def _agent():
    return SimpleNamespace(
        session_id="runtime-session",
        lane="platform",
        _rotation_task_id=None,
        _cached_system_prompt="system",
        _build_system_prompt=lambda message=None: message or "system",
    )


# Estimator (4)
def test_estimate_tokens_empty_string_returns_zero():
    assert estimate_tokens("") == 0


def test_estimate_tokens_short_string_at_least_one():
    assert estimate_tokens("x") == 1


def test_estimate_next_turn_sums_system_history_and_pending():
    assert estimate_next_turn_input_tokens(
        "x" * 40,
        [{"role": "user", "content": "y" * 20}],
        "z" * 12,
    ) == 10 + 5 + 10 + 3


def test_estimate_next_turn_includes_message_overhead():
    without = estimate_next_turn_input_tokens("", [], "")
    with_two = estimate_next_turn_input_tokens(
        "",
        [{"content": ""}, {"content": ""}],
        "",
    )
    assert with_two - without == 20


# Rotation policy (5)
def test_should_not_rotate_below_soft_limit():
    assert controller.should_rotate(
        system_prompt="",
        conversation_history=[],
        pending_user_message="short",
    ) == (False, "")


def test_should_rotate_at_soft_limit():
    assert controller.should_rotate(
        system_prompt="x" * (ROTATION_CAPS.soft_limit_tokens * 4),
        conversation_history=[],
        pending_user_message="",
    ) == (True, "soft_limit")


def test_should_rotate_at_hard_limit():
    assert controller.should_rotate(
        system_prompt="x" * (ROTATION_CAPS.hard_limit_tokens * 4),
        conversation_history=[],
        pending_user_message="",
    ) == (True, "hard_limit")


def test_hard_limit_precedence_over_soft():
    rotate, reason = controller.should_rotate(
        system_prompt="x" * (ROTATION_CAPS.hard_limit_tokens * 4),
        conversation_history=[],
        pending_user_message="",
    )
    assert rotate is True
    assert reason == "hard_limit"


def test_rotation_reason_returned_correctly():
    soft = controller.should_rotate(
        system_prompt="x" * (ROTATION_CAPS.soft_limit_tokens * 4),
        conversation_history=[],
        pending_user_message="",
    )
    hard = controller.should_rotate(
        system_prompt="x" * (ROTATION_CAPS.hard_limit_tokens * 4),
        conversation_history=[],
        pending_user_message="",
    )
    assert soft[1] == "soft_limit"
    assert hard[1] == "hard_limit"


# Session API (7)
def test_open_session_writes_row_and_returns_uuid(db_path):
    session_id = _open(db_path)
    assert uuid.UUID(session_id)
    assert api.get_open_session_for_task("task-1", db_path)["id"] == session_id


def test_close_session_marks_row_closed(db_path):
    session_id = _open(db_path)
    api.close_session(
        session_id=session_id,
        rotation_reason="manual",
        token_count_at_close=123,
        db_path=db_path,
    )
    row = api.list_sessions_for_task("task-1", db_path)[0]
    assert row["closed_ts"]
    assert row["rotation_reason"] == "manual"
    assert row["token_count_at_close"] == 123


def test_close_session_idempotent(db_path):
    session_id = _open(db_path)
    for _ in range(2):
        api.close_session(
            session_id=session_id,
            rotation_reason="manual",
            token_count_at_close=1,
            db_path=db_path,
        )
    assert api.list_sessions_for_task("task-1", db_path)[0]["closed_ts"]


def test_get_open_session_returns_only_open(db_path):
    old = _open(db_path)
    api.close_session(
        session_id=old,
        rotation_reason="manual",
        token_count_at_close=1,
        db_path=db_path,
    )
    new = _open(db_path, parent_session_id=old)
    assert api.get_open_session_for_task("task-1", db_path)["id"] == new


def test_list_sessions_for_task_ordered_by_opened_ts(db_path):
    first = _open(db_path)
    api.close_session(
        session_id=first,
        rotation_reason="manual",
        token_count_at_close=1,
        db_path=db_path,
    )
    second = _open(db_path, parent_session_id=first)
    assert [row["id"] for row in api.list_sessions_for_task("task-1", db_path)] == [
        first,
        second,
    ]


def test_parent_session_id_persisted(db_path):
    parent = _open(db_path)
    api.close_session(
        session_id=parent,
        rotation_reason="manual",
        token_count_at_close=1,
        db_path=db_path,
    )
    child = _open(db_path, parent_session_id=parent)
    assert api.get_open_session_for_task("task-1", db_path)[
        "parent_session_id"
    ] == parent
    assert child


def test_all_indexes_exist(db_path):
    schema.migrate(db_path)
    conn = schema.connect(db_path)
    try:
        indexes = {
            row["name"]
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='index'"
            )
        }
    finally:
        conn.close()
    assert {
        "idx_sessions_task",
        "idx_sessions_parent",
        "idx_sessions_opened",
        "idx_sessions_closed",
    }.issubset(indexes)


# Handoff summary (6)
def test_build_handoff_summary_deterministic_no_llm(db_path):
    _open(db_path)
    assert api.build_handoff_summary("task-1", db_path) == api.build_handoff_summary(
        "task-1", db_path
    )


def test_build_handoff_includes_last_5_verdicts(db_path):
    session_id = _open(db_path)
    for attempt in range(1, 7):
        record_verdict(
            _verdict("task-1", attempt),
            db_path=db_path,
            session_id=session_id,
        )
    summary = api.build_handoff_summary("task-1", db_path)
    assert len(summary["recent_leaf_verdicts"]) == 5
    assert summary["recent_leaf_verdicts"][0]["confidence"] == 1.0


def test_build_handoff_includes_active_side_effects(db_path):
    _open(db_path)
    from hermes_cli.side_effects import api as effects

    effects.reserve(
        task_id="task-1",
        lane="platform",
        action_type="test.action",
        payload={"x": 1},
        db_path=db_path,
    )
    summary = api.build_handoff_summary("task-1", db_path)
    assert summary["active_side_effects"][0]["status"] == "pending"


def test_build_handoff_includes_cost_totals(db_path):
    session_id = _open(db_path)
    row = ledger.record_call(
        task_id="task-1",
        lane="platform",
        vendor="apple",
        api_call_kind="test",
        force_zero=True,
        session_id=session_id,
        db_path=db_path,
    )
    conn = schema.connect(db_path)
    try:
        conn.execute("UPDATE cost_ledger SET aud_amount=1.25 WHERE id=?", (row.id,))
        conn.commit()
    finally:
        conn.close()
    summary = api.build_handoff_summary("task-1", db_path)
    assert summary["cost_totals"]["task_spend_aud"] == 1.25


def test_build_handoff_truncates_to_max_chars():
    wrapped = api.serialize_handoff({"huge": "x" * 20_000})
    assert len(wrapped) <= ROTATION_CAPS.handoff_summary_max_chars
    assert '"truncated":true' in wrapped


def test_serialize_handoff_wraps_in_tags():
    wrapped = api.serialize_handoff({"b": 2, "a": 1})
    assert wrapped.startswith("<hermes:handoff>{")
    assert wrapped.endswith("}</hermes:handoff>")
    assert wrapped.index('"a"') < wrapped.index('"b"')


# Rotation controller (5)
def test_rotate_now_closes_old_and_opens_new(db_path):
    old = _open(db_path)
    new, _ = controller.rotate_now(
        current_session_id=old,
        task_id="task-1",
        lane="platform",
        profile="atlas",
        route="direct_cli",
        reason="soft_limit",
        token_count_at_close=100_000,
        db_path=db_path,
    )
    rows = api.list_sessions_for_task("task-1", db_path)
    assert rows[0]["closed_ts"]
    assert rows[1]["id"] == new


def test_rotate_now_preserves_task_and_lane_and_profile_and_route(db_path):
    old = _open(db_path, lane="dayroute", profile="p", route="r")
    controller.rotate_now(
        current_session_id=old,
        task_id="task-1",
        lane="dayroute",
        profile="p",
        route="r",
        reason="soft_limit",
        token_count_at_close=100_000,
        db_path=db_path,
    )
    row = api.get_open_session_for_task("task-1", db_path)
    assert (row["task_id"], row["lane"], row["profile"], row["route"]) == (
        "task-1",
        "dayroute",
        "p",
        "r",
    )


def test_rotate_now_parent_session_id_set_correctly(db_path):
    old = _open(db_path)
    new, _ = controller.rotate_now(
        current_session_id=old,
        task_id="task-1",
        lane="platform",
        profile="atlas",
        route="direct_cli",
        reason="soft_limit",
        token_count_at_close=100_000,
        db_path=db_path,
    )
    assert api.get_open_session_for_task("task-1", db_path)["parent_session_id"] == old
    assert new


def test_rotate_now_returns_handoff_prefix_containing_summary(db_path):
    old = _open(db_path)
    _, prefix = controller.rotate_now(
        current_session_id=old,
        task_id="task-1",
        lane="platform",
        profile="atlas",
        route="direct_cli",
        reason="soft_limit",
        token_count_at_close=100_000,
        db_path=db_path,
    )
    assert "<hermes:handoff>" in prefix
    assert '"task_id":"task-1"' in prefix


def test_rotate_now_hard_limit_triggers_telegram_dedup(db_path, monkeypatch):
    old = _open(db_path)
    seen = []
    monkeypatch.setattr(
        controller,
        "_send_hard_rotation_alert",
        lambda **kwargs: seen.append(kwargs) or True,
    )
    controller.rotate_now(
        current_session_id=old,
        task_id="task-1",
        lane="platform",
        profile="atlas",
        route="direct_cli",
        reason="hard_limit",
        token_count_at_close=160_000,
        db_path=db_path,
    )
    assert seen[0]["task_id"] == "task-1"
    assert seen[0]["token_count"] == 160_000


# Integration with conversation loop (4)
def test_conversation_loop_below_soft_limit_no_rotation(monkeypatch):
    agent = _agent()
    monkeypatch.setattr(conversation_loop, "_register_runtime_session", lambda *a, **k: None)
    history = [{"role": "user", "content": "short"}]
    assert conversation_loop.prepare_session_rotation(
        agent,
        user_message="next",
        system_message=None,
        conversation_history=history,
        task_id="task-1",
        lane="platform",
        profile="atlas",
        route="direct_cli",
    ) == history
    assert agent.session_id == "runtime-session"


def test_conversation_loop_at_soft_limit_rotates_transparently(monkeypatch):
    agent = _agent()
    agent._cached_system_prompt = "x" * 400_000
    monkeypatch.setattr(conversation_loop, "_register_runtime_session", lambda *a, **k: None)
    monkeypatch.setattr(
        controller,
        "rotate_now",
        lambda **kwargs: ("new-session", "<hermes:handoff>{}</hermes:handoff>"),
    )
    monkeypatch.setattr(
        conversation_loop,
        "_transition_runtime_session",
        lambda agent, **kwargs: setattr(agent, "session_id", kwargs["new_session_id"]),
    )
    result = conversation_loop.prepare_session_rotation(
        agent,
        user_message="next",
        system_message=None,
        conversation_history=[],
        task_id="task-1",
        lane="platform",
        profile="atlas",
        route="direct_cli",
    )
    assert result == []
    assert agent.session_id == "new-session"


def test_conversation_loop_hard_limit_rotates_and_alerts(monkeypatch):
    agent = _agent()
    agent._cached_system_prompt = "x" * 640_000
    seen = []
    monkeypatch.setattr(conversation_loop, "_register_runtime_session", lambda *a, **k: None)
    monkeypatch.setattr(
        controller,
        "rotate_now",
        lambda **kwargs: seen.append(kwargs) or (
            "new-session",
            "<hermes:handoff>{}</hermes:handoff>",
        ),
    )
    monkeypatch.setattr(conversation_loop, "_transition_runtime_session", lambda *a, **k: None)
    conversation_loop.prepare_session_rotation(
        agent,
        user_message="next",
        system_message=None,
        conversation_history=[],
        task_id="task-1",
        lane="platform",
        profile="atlas",
        route="direct_cli",
    )
    assert seen[0]["reason"] == "hard_limit"


def test_new_session_first_turn_includes_handoff_in_system_prompt(monkeypatch):
    agent = _agent()
    agent._cached_system_prompt = "x" * 400_000
    captured = {}
    monkeypatch.setattr(conversation_loop, "_register_runtime_session", lambda *a, **k: None)
    monkeypatch.setattr(
        controller,
        "rotate_now",
        lambda **kwargs: ("new-session", "<hermes:handoff>{}</hermes:handoff>"),
    )
    monkeypatch.setattr(
        conversation_loop,
        "_transition_runtime_session",
        lambda agent, **kwargs: captured.update(kwargs),
    )
    conversation_loop.prepare_session_rotation(
        agent,
        user_message="next",
        system_message=None,
        conversation_history=[],
        task_id="task-1",
        lane="platform",
        profile="atlas",
        route="direct_cli",
    )
    prompt = captured["handoff_system_prompt"]
    assert prompt.startswith("<hermes:handoff>")
    assert prompt.endswith("x" * 400_000)


# Attribution (3)
def test_record_call_accepts_session_id_kwarg(db_path):
    session_id = _open(db_path)
    entry = ledger.record_call(
        task_id="task-1",
        lane="platform",
        vendor="apple",
        api_call_kind="test",
        force_zero=True,
        session_id=session_id,
        db_path=db_path,
    )
    assert entry.session_id == session_id


def test_record_verdict_accepts_session_id_kwarg(db_path):
    session_id = _open(db_path)
    row_id = record_verdict(
        _verdict("task-1"),
        db_path=db_path,
        session_id=session_id,
    )
    conn = schema.connect(db_path)
    try:
        value = conn.execute(
            "SELECT session_id FROM leaf_verdicts WHERE id=?", (row_id,)
        ).fetchone()["session_id"]
    finally:
        conn.close()
    assert value == session_id


def test_record_dispatch_accepts_session_id_kwarg(db_path):
    session_id = _open(db_path)
    row_id = record_dispatch(
        _dispatch("task-1"),
        db_path=db_path,
        session_id=session_id,
    )
    conn = schema.connect(db_path)
    try:
        value = conn.execute(
            "SELECT session_id FROM dispatch_envelopes WHERE id=?", (row_id,)
        ).fetchone()["session_id"]
    finally:
        conn.close()
    assert value == session_id


# Migration (3)
def test_sessions_table_migration_lazy_and_idempotent(db_path):
    schema.ensure_migrated(db_path)
    schema.migrate(db_path)
    conn = schema.connect(db_path)
    try:
        columns = tuple(
            row["name"] for row in conn.execute("PRAGMA table_info(sessions)")
        )
    finally:
        conn.close()
    assert columns == schema.EXPECTED_COLUMNS


def test_session_id_columns_added_to_existing_tables(db_path):
    schema.migrate(db_path)
    conn = schema.connect(db_path)
    try:
        for table in ("cost_ledger", "leaf_verdicts", "dispatch_envelopes"):
            columns = {
                row["name"] for row in conn.execute(f"PRAGMA table_info({table})")
            }
            assert "session_id" in columns
    finally:
        conn.close()


def test_existing_rows_remain_null_session_id(db_path):
    schema.migrate(db_path)
    entry = ledger.record_call(
        task_id="legacy",
        lane="platform",
        vendor="apple",
        api_call_kind="test",
        force_zero=True,
        db_path=db_path,
    )
    assert entry.session_id is None


# CLI (3)
def test_session_list_prints_expected_columns(db_path, monkeypatch, capsys):
    monkeypatch.setattr(schema, "DB_PATH", db_path)
    _open(db_path)
    assert session_cli._cmd_list(argparse.Namespace(task=None, limit=10)) == 0
    output = capsys.readouterr().out
    assert "TASK  SESSION  PARENT  LANE" in output
    assert "task-1" in output


def test_session_show_prints_row_and_related_counts(db_path, monkeypatch, capsys):
    monkeypatch.setattr(schema, "DB_PATH", db_path)
    session_id = _open(db_path)
    assert session_cli._cmd_show(argparse.Namespace(session_id=session_id)) == 0
    output = capsys.readouterr().out
    assert session_id in output
    assert "cost_ledger: 0" in output
    assert "leaf_verdicts: 0" in output


def test_session_rotate_without_confirm_is_noop(monkeypatch, capsys):
    row = {
        "id": "old",
        "task_id": "task-1",
        "lane": "platform",
        "profile": "atlas",
        "route": "direct_cli",
        "token_count_at_close": None,
    }
    monkeypatch.setattr(api, "get_open_session_for_task", lambda task: row)
    monkeypatch.setattr(
        controller,
        "rotate_now",
        lambda **kwargs: pytest.fail("dry run must not rotate"),
    )
    result = session_cli._cmd_rotate(
        argparse.Namespace(task="task-1", reason="manual", confirm=False)
    )
    assert result == 0
    assert "Dry run: would rotate" in capsys.readouterr().out
