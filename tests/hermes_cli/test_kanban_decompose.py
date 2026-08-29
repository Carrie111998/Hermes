"""Tests for the decomposer module + `hermes kanban decompose` CLI surface.

The auxiliary LLM client is mocked — no network calls. Tests exercise the
prompt plumbing, response parsing, DB writes (via the real DB helper),
and the assignee-fallback logic.
"""

from __future__ import annotations

import json as jsonlib
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_decompose as decomp


def _fake_aux_response(content: str):
    resp = MagicMock()
    resp.choices = [MagicMock()]
    resp.choices[0].message.content = content
    return resp


def _mock_client_returning(content: str):
    client = MagicMock()
    client.chat.completions.create = MagicMock(return_value=_fake_aux_response(content))
    return client


def _patch_aux_client(content: str, *, model: str = "test-model"):
    # decompose_task now routes through call_llm (see #35566) — mock it at
    # the source module so task config, extra_body, and retries stay out of
    # unit-test scope.
    return patch(
        "agent.auxiliary_client.call_llm",
        return_value=_fake_aux_response(content),
    )


def _patch_extra_body():
    # No-op shim retained for call-site compatibility: extra_body plumbing
    # now lives inside call_llm, which _patch_aux_client already mocks.
    return patch("agent.auxiliary_client.get_auxiliary_extra_body", return_value={})


def _patch_list_profiles(names: list[str]):
    """Pretend the named profiles exist. The decomposer uses
    profiles_mod.list_profiles() to build the roster + valid-set, and
    profiles_mod.profile_exists() to resolve orchestrator/default."""
    from types import SimpleNamespace
    fake_profiles = [
        SimpleNamespace(
            name=n, is_default=(i == 0), description=f"desc for {n}",
            description_auto=False, model="m", provider="p", skill_count=1,
        )
        for i, n in enumerate(names)
    ]
    return [
        patch("hermes_cli.profiles.list_profiles", return_value=fake_profiles),
        patch("hermes_cli.profiles.profile_exists", side_effect=lambda x: x in names),
        patch("hermes_cli.profiles.get_active_profile_name", return_value=names[0] if names else "default"),
    ]


def test_decompose_with_fanout_creates_children(kanban_home):
    with kb.connect() as conn:
        tid = kb.create_task(conn, title="ship a feature", triage=True)

    llm_payload = jsonlib.dumps({
        "fanout": True,
        "rationale": "test split",
        "tasks": [
            {"title": "research", "body": "look it up", "assignee": "researcher", "parents": []},
            {"title": "build", "body": "code it", "assignee": "engineer", "parents": [0]},
        ],
    })

    patches = _patch_list_profiles(["orchestrator", "researcher", "engineer"])
    for p in patches:
        p.start()
    try:
        with _patch_aux_client(llm_payload), _patch_extra_body():
            outcome = decomp.decompose_task(tid, author="me")
    finally:
        for p in patches:
            p.stop()

    assert outcome.ok, outcome.reason
    assert outcome.fanout is True
    assert outcome.child_ids and len(outcome.child_ids) == 2

    with kb.connect() as conn:
        root = kb.get_task(conn, tid)
        c0 = kb.get_task(conn, outcome.child_ids[0])
        c1 = kb.get_task(conn, outcome.child_ids[1])
    assert root.status == "todo"
    assert c0.status == "ready"
    assert c1.status == "todo"
    assert c0.assignee == "researcher"
    assert c1.assignee == "engineer"


def test_decompose_fanout_false_invalid_llm_assignee_uses_default(kanban_home):
    with kb.connect() as conn:
        tid = kb.create_task(conn, title="route me safely", triage=True)

    llm_payload = jsonlib.dumps({
        "fanout": False,
        "rationale": "single unit",
        "title": "Tightened title",
        "body": "Route to fallback.",
        "assignee": "made_up",
    })

    patches = _patch_list_profiles(["orchestrator", "fallback"])
    for p in patches:
        p.start()
    try:
        with _patch_aux_client(llm_payload), _patch_extra_body(), patch(
            "hermes_cli.kanban_decompose._load_config",
            return_value={"kanban": {"default_assignee": "fallback"}},
        ):
            outcome = decomp.decompose_task(tid, author="me")
    finally:
        for p in patches:
            p.stop()

    assert outcome.ok, outcome.reason
    with kb.connect() as conn:
        task = kb.get_task(conn, tid)
    assert task is not None
    assert task.assignee == "fallback"


def test_decompose_returns_false_when_task_not_triage(kanban_home):
    with kb.connect() as conn:
        tid = kb.create_task(conn, title="x", assignee="worker")  # ready, not triage

    patches = _patch_list_profiles(["orchestrator"])
    for p in patches:
        p.start()
    try:
        outcome = decomp.decompose_task(tid, author="me")
    finally:
        for p in patches:
            p.stop()
    assert outcome.ok is False
    assert "not in triage" in outcome.reason


def test_decompose_null_assignee_children_routed_to_default(kanban_home):
    """Regression: every child task in decomposer output must have an assignee.

    Feeds a minimal/unmatched task body and null assignees in the LLM
    response. The decomposer must route every child to the default_assignee
    — no child should end up with absent/null assignee.
    """
    with kb.connect() as conn:
        tid = kb.create_task(conn, title="do stuff", triage=True)

    # LLM returns null assignees for all children — should route to default.
    llm_payload = jsonlib.dumps({
        "fanout": True,
        "rationale": "test unmatched routing",
        "tasks": [
            {"title": "task A", "body": "minimal", "assignee": None, "parents": []},
            {"title": "task B", "body": "unmatched", "assignee": None, "parents": [0]},
        ],
    })

    patches = _patch_list_profiles(["orchestrator", "default", "fallback"])
    for p in patches:
        p.start()
    try:
        with _patch_aux_client(llm_payload), _patch_extra_body(), patch(
            "hermes_cli.kanban_decompose._load_config",
            return_value={"kanban": {"default_assignee": "fallback"}},
        ):
            outcome = decomp.decompose_task(tid, author="me")
    finally:
        for p in patches:
            p.stop()

    assert outcome.ok, outcome.reason
    assert outcome.fanout is True
    assert outcome.child_ids and len(outcome.child_ids) == 2

    with kb.connect() as conn:
        c0 = kb.get_task(conn, outcome.child_ids[0])
        c1 = kb.get_task(conn, outcome.child_ids[1])

    # Every child must have a non-null assignee — routed to the default.
    assert c0.assignee is not None, "child[0] was left with null assignee"
    assert c1.assignee is not None, "child[1] was left with null assignee"
    assert c0.assignee == "fallback"
    assert c1.assignee == "fallback"


def test_decompose_rejects_null_assignee_in_decompose_triage_task(kanban_home):
    """Direct call to decompose_triage_task with null assignee must raise.

    The decomposer module normalizes upstream, but decompose_triage_task
    itself must defensively reject children with null/empty assignees so
    a direct caller can't bypass the router and create unassigned ready
    cards.
    """
    with kb.connect() as conn:
        tid = kb.create_task(conn, title="root", triage=True)

    children = [
        {"title": "child A", "body": "work", "assignee": None, "parents": []},
    ]
    with pytest.raises(ValueError, match="assignee is required"):
        kb.decompose_triage_task(
            conn,
            tid,
            root_assignee="orchestrator",
            children=children,
            author="tester",
        )


def test_create_task_without_assignee_lands_in_triage(kanban_home):
    """Regression: create_task() must not leave a card in 'ready' with no assignee.

    A task created without --triage and without --assignee previously landed
    in 'ready' and was invisible to the dispatcher. It must now be forced to
    'triage' so the auto-decomposer can route it.
    """
    with kb.connect() as conn:
        tid = kb.create_task(conn, title="orphan task")
        task = kb.get_task(conn, tid)

    assert task.status == "triage", (
        f"unassigned task should land in triage, got {task.status!r}"
    )
    assert task.assignee is None

    # An assigned task without triage should still go to ready.
    with kb.connect() as conn:
        tid2 = kb.create_task(conn, title="assigned task", assignee="worker")
        task2 = kb.get_task(conn, tid2)

    assert task2.status == "ready", (
        f"assigned task should be ready, got {task2.status!r}"
    )
    assert task2.assignee == "worker"


def test_recompute_ready_forces_unassigned_to_triage(kanban_home):
    """Regression: recompute_ready() must not promote an unassigned card
    to 'ready' when kanban.default_assignee is unset.

    A child task with no assignee and a done parent would previously be
    promoted by recompute_ready to 'ready' and sit invisible to the
    dispatcher.  It must be forced to 'triage' instead.
    """
    with kb.connect() as conn:
        parent = kb.create_task(conn, title="parent", assignee="worker")
        kb.complete_task(conn, parent)
        child = kb.create_task(conn, title="child", parents=[parent])
        # create_task already forces triage; simulate the bypass by
        # moving the child back to 'todo' directly (as an external
        # writer could).
        with kb.write_txn(conn):
            conn.execute(
                "UPDATE tasks SET status = ? WHERE id = ?",
                ("todo", child),
            )
        kb.recompute_ready(conn)
        task = kb.get_task(conn, child)

    assert task.status == "triage", (
        f"recompute_ready should force unassigned to triage, got {task.status!r}"
    )


def test_promote_task_rejects_unassigned_without_default(kanban_home):
    """Regression: promote_task() must refuse to promote an unassigned
    card to 'ready' when kanban.default_assignee is unset.

    The operator can override with --force (deliberate action).
    """
    with kb.connect() as conn:
        tid = kb.create_task(conn, title="unassigned", assignee="worker")
        with kb.write_txn(conn):
            conn.execute(
                "UPDATE tasks SET status = ?, assignee = ? WHERE id = ?",
                ("todo", None, tid),
            )
        ok, reason = kb.promote_task(conn, tid, actor="test")

    assert not ok, "promote_task should reject unassigned card"
    assert "no assignee" in reason

    # --force overrides (deliberate operator action)
    with kb.connect() as conn:
        ok2, reason2 = kb.promote_task(conn, tid, actor="test", force=True)
        task = kb.get_task(conn, tid)

    assert ok2, "promote_task with --force should succeed"
    assert task.status == "ready"


def test_unblock_task_forces_unassigned_to_triage(kanban_home):
    """Regression: unblock_task() must not land an unassigned card in
    'ready' when kanban.default_assignee is unset.

    A blocked unassigned task with no open parents would previously be
    flipped to 'ready' and sit invisible.  It must be forced to 'triage'.
    """
    with kb.connect() as conn:
        tid = kb.create_task(conn, title="blocked orphan", assignee="worker")
        with kb.write_txn(conn):
            conn.execute(
                "UPDATE tasks SET status = ?, assignee = ? WHERE id = ?",
                ("blocked", None, tid),
            )
        result = kb.unblock_task(conn, tid)
        task = kb.get_task(conn, tid)

    assert result is True
    assert task.status == "triage", (
        f"unblock_task should force unassigned to triage, got {task.status!r}"
    )


