"""Lifecycle retention for explicitly owned temporary sessions."""

import json

import pytest

from agent.session_retention import (
    RETENTION_ARCHIVE,
    RETENTION_DELETE,
    RETENTION_KEEP,
    accept_temporary_child_result,
    apply_completed_retention,
    child_session_ids_from_result,
    is_explicitly_temporary,
    mark_result_accepted,
    resolve_retention_policy,
    sweep_accepted_temporary_sessions,
)
from hermes_state import SessionDB


@pytest.fixture()
def db(tmp_path):
    session_db = SessionDB(db_path=tmp_path / "state.db")
    yield session_db
    session_db.close()


def _cfg(**extra):
    payload = {"max_iterations": 10}
    payload.update(extra)
    return payload


def _create(db, sid, *, source="cli", parent=None, cfg=None, title=None, pinned=False):
    kwargs = {"session_id": sid, "source": source, "model": "test", "model_config": cfg}
    if parent is not None:
        kwargs["parent_session_id"] = parent
    db.create_session(**kwargs)
    if title:
        db.set_session_title(sid, title)
    if pinned:
        db.set_session_pinned(sid, True)


def test_source_tool_is_not_ownership():
    assert is_explicitly_temporary({"source": "tool", "model_config": {}}) is False
    assert is_explicitly_temporary({"source": "subagent", "model_config": {}}) is False
    assert is_explicitly_temporary({"model_config": {"_delegate_from": "parent"}}) is True
    assert is_explicitly_temporary({"model_config": {"_ephemeral": True}}) is True


def test_unknown_policy_falls_back_to_keep():
    assert resolve_retention_policy({"delegation": {"completed_session_retention": "explode"}}) == RETENTION_KEEP
    assert resolve_retention_policy({"delegation": {}}) == RETENTION_KEEP


def test_delete_after_terminal_and_accepted(db):
    _create(db, "parent", cfg=_cfg())
    _create(db, "child", source="tool", parent="parent", cfg=_cfg(_delegate_from="parent"))
    db.end_session("child", "agent_close")

    assert apply_completed_retention(db, "child", policy=RETENTION_DELETE) == "skipped"
    assert db.get_session("child") is not None

    mark_result_accepted(db, "child")
    assert apply_completed_retention(db, "child", policy=RETENTION_DELETE) == "deleted"
    assert db.get_session("child") is None
    assert db.get_session("parent") is not None


def test_pending_delivery_keeps_child(db):
    _create(db, "parent", cfg=_cfg())
    _create(db, "child", parent="parent", cfg=_cfg(_delegate_from="parent"))
    db.end_session("child", "agent_close")
    assert apply_completed_retention(db, "child", policy=RETENTION_DELETE) == "skipped"
    assert db.get_session("child") is not None


def test_active_child_remains(db):
    _create(db, "parent", cfg=_cfg())
    _create(db, "child", parent="parent", cfg=_cfg(_delegate_from="parent"))
    mark_result_accepted(db, "child")
    assert apply_completed_retention(db, "child", policy=RETENTION_DELETE) == "skipped"
    assert db.get_session("child") is not None


def test_user_created_session_remains_regardless_of_policy(db):
    _create(db, "user", source="cli", cfg=_cfg(), title="My chat")
    db.end_session("user", "cli_close")
    mark_result_accepted(db, "user")
    assert apply_completed_retention(db, "user", policy=RETENTION_DELETE) == "skipped"
    assert db.get_session("user") is not None


def test_source_tool_without_ownership_is_not_deleted(db):
    _create(db, "toolrun", source="tool", cfg=_cfg())
    db.end_session("toolrun", "cli_close")
    mark_result_accepted(db, "toolrun")
    assert apply_completed_retention(db, "toolrun", policy=RETENTION_DELETE) == "skipped"
    assert db.get_session("toolrun") is not None


def test_cancelled_child_deleted_only_after_acceptance(db):
    _create(db, "parent", cfg=_cfg())
    _create(db, "child", parent="parent", cfg=_cfg(_delegate_from="parent"))
    db.end_session("child", "interrupted")
    assert apply_completed_retention(db, "child", policy=RETENTION_DELETE) == "skipped"
    assert accept_temporary_child_result(db, "child", policy=RETENTION_DELETE) == "deleted"
    assert db.get_session("child") is None


def test_crash_before_acceptance_then_recovery_accepts_once(db):
    _create(db, "parent", cfg=_cfg())
    _create(db, "child", parent="parent", cfg=_cfg(_delegate_from="parent"))
    db.end_session("child", "agent_close")
    assert sweep_accepted_temporary_sessions(db, policy=RETENTION_DELETE) == 0
    assert db.get_session("child") is not None
    assert accept_temporary_child_result(db, "child", policy=RETENTION_DELETE) == "deleted"
    assert accept_temporary_child_result(db, "child", policy=RETENTION_DELETE) == "deleted"
    assert db.get_session("child") is None


def test_nested_tree_deletes_child_first(db):
    _create(db, "parent", cfg=_cfg())
    _create(db, "mid", parent="parent", cfg=_cfg(_delegate_from="parent"))
    _create(db, "leaf", parent="mid", cfg=_cfg(_delegate_from="mid"))
    db.end_session("leaf", "agent_close")
    db.end_session("mid", "agent_close")
    mark_result_accepted(db, "leaf")
    mark_result_accepted(db, "mid")

    assert apply_completed_retention(db, "mid", policy=RETENTION_DELETE) == "deleted"
    assert db.get_session("leaf") is None
    assert db.get_session("mid") is None
    assert db.get_session("parent") is not None


def test_nested_incomplete_leaf_blocks_parent_delete(db):
    _create(db, "parent", cfg=_cfg())
    _create(db, "mid", parent="parent", cfg=_cfg(_delegate_from="parent"))
    _create(db, "leaf", parent="mid", cfg=_cfg(_delegate_from="mid"))
    db.end_session("mid", "agent_close")
    db.end_session("leaf", "agent_close")
    mark_result_accepted(db, "mid")
    assert apply_completed_retention(db, "mid", policy=RETENTION_DELETE) == "skipped"
    assert db.get_session("mid") is not None
    assert db.get_session("leaf") is not None


def test_keep_and_archive_policies(db):
    _create(db, "parent", cfg=_cfg())
    _create(db, "keep_child", parent="parent", cfg=_cfg(_delegate_from="parent"))
    _create(db, "arch_child", parent="parent", cfg=_cfg(_delegate_from="parent"))
    db.end_session("keep_child", "agent_close")
    db.end_session("arch_child", "agent_close")
    mark_result_accepted(db, "keep_child")
    mark_result_accepted(db, "arch_child")

    assert apply_completed_retention(db, "keep_child", policy=RETENTION_KEEP) == "kept"
    assert db.get_session("keep_child") is not None
    assert not db.get_session("keep_child").get("archived")

    assert apply_completed_retention(db, "arch_child", policy=RETENTION_ARCHIVE) == "archived"
    assert db.get_session("arch_child") is not None
    assert db.get_session("arch_child").get("archived")


def test_ephemeral_oneshot_deletes_only_own_session(db):
    _create(db, "keeper", source="cli", cfg=_cfg())
    _create(db, "worker", source="cli", cfg=_cfg(_ephemeral=True))
    db.end_session("worker", "cli_close")
    assert accept_temporary_child_result(db, "worker", policy=RETENTION_DELETE) == "deleted"
    assert db.get_session("worker") is None
    assert db.get_session("keeper") is not None


def test_child_session_ids_from_result():
    assert child_session_ids_from_result(
        {"results": [{"child_session_id": "a"}, {"child_session_id": "b"}]}
    ) == ["a", "b"]
    assert child_session_ids_from_result({"child_session_id": "solo"}) == ["solo"]


def test_chat_parser_exposes_ephemeral_without_aliasing_source():
    from hermes_cli._parser import build_top_level_parser

    parser, _subparsers, _chat = build_top_level_parser()
    args = parser.parse_args(["chat", "-q", "hi", "--ephemeral", "--source", "tool"])
    assert args.ephemeral is True
    assert args.source == "tool"
