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


@pytest.fixture
def kanban_home(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    kb.init_db()
    return home


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
        tid = kb.create_task(conn, title="x")  # ready, not triage

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


def test_decompose_routes_each_explicit_board_by_its_effective_profile_policy(
    kanban_home,
):
    kb.create_board(
        "alpha-board",
        allowed_profiles=["alpha-orchestrator", "alpha-worker"],
    )
    kb.create_board(
        "beta-board",
        allowed_profiles=["beta-orchestrator", "beta-worker"],
    )
    with kb.connect(board="alpha-board") as conn:
        alpha_tid = kb.create_task(conn, title="alpha work", triage=True)
    with kb.connect(board="beta-board") as conn:
        beta_tid = kb.create_task(conn, title="beta work", triage=True)

    llm_payload = jsonlib.dumps({
        "fanout": True,
        "rationale": "one routed child",
        "tasks": [{
            "title": "do it",
            "body": "board-scoped work",
            "assignee": "not-installed",
            "parents": [],
        }],
    })
    installed = [
        "global-active",
        "alpha-orchestrator",
        "alpha-worker",
        "beta-orchestrator",
        "beta-worker",
    ]
    config = {
        "kanban": {
            "orchestrator_profile": "beta-orchestrator",
            "default_assignee": "beta-worker",
        }
    }
    prompts: list[str] = []

    def call_llm_for(board_to_switch_to: str):
        def _call_llm(**kwargs):
            prompts.append(kwargs["messages"][1]["content"])
            kb.set_current_board(board_to_switch_to)
            return _fake_aux_response(llm_payload)

        return _call_llm

    patches = _patch_list_profiles(installed)
    for profile_patch in patches:
        profile_patch.start()
    try:
        with patch(
            "hermes_cli.kanban_decompose._load_config",
            return_value=config,
        ), patch(
            "agent.auxiliary_client.call_llm",
            side_effect=call_llm_for("beta-board"),
        ):
            alpha_outcome = decomp.decompose_task(
                alpha_tid,
                board="alpha-board",
                author="me",
            )
        with patch(
            "hermes_cli.kanban_decompose._load_config",
            return_value=config,
        ), patch(
            "agent.auxiliary_client.call_llm",
            side_effect=call_llm_for("alpha-board"),
        ):
            beta_outcome = decomp.decompose_task(
                beta_tid,
                board="beta-board",
                author="me",
            )
    finally:
        for profile_patch in patches:
            profile_patch.stop()

    assert alpha_outcome.ok, alpha_outcome.reason
    assert beta_outcome.ok, beta_outcome.reason
    assert alpha_outcome.child_ids
    assert beta_outcome.child_ids
    assert len(prompts) == 2
    assert "alpha-orchestrator" in prompts[0]
    assert "alpha-worker" in prompts[0]
    assert "beta-orchestrator" not in prompts[0]
    assert "beta-worker" not in prompts[0]
    assert "beta-orchestrator" in prompts[1]
    assert "beta-worker" in prompts[1]
    assert "alpha-orchestrator" not in prompts[1]
    assert "alpha-worker" not in prompts[1]

    with kb.connect(board="alpha-board") as conn:
        alpha_root = kb.get_task(conn, alpha_tid)
        alpha_child = kb.get_task(conn, alpha_outcome.child_ids[0])
    with kb.connect(board="beta-board") as conn:
        beta_root = kb.get_task(conn, beta_tid)
        beta_child = kb.get_task(conn, beta_outcome.child_ids[0])

    # Alpha's configured choices are excluded, so both routes use the first
    # installed effective profile. Beta's configured choices are effective.
    assert alpha_root is not None
    assert alpha_child is not None
    assert beta_root is not None
    assert beta_child is not None
    assert alpha_root.assignee == "alpha-orchestrator"
    assert alpha_child.assignee == "alpha-orchestrator"
    assert beta_root.assignee == "beta-orchestrator"
    assert beta_child.assignee == "beta-worker"


def test_decompose_empty_effective_roster_is_non_ok_without_mutation(kanban_home):
    kb.create_board("empty-board", allowed_profiles=["existing"])
    with kb.connect(board="empty-board") as conn:
        tid = kb.create_task(
            conn,
            title="must stay triage",
            body="must stay unchanged",
            assignee="existing",
            triage=True,
        )
    kb.write_board_metadata("empty-board", allowed_profiles=[])
    with kb.connect(board="empty-board") as conn:
        before_root = kb.get_task(conn, tid)
        before_task_ids = [task.id for task in kb.list_tasks(conn, limit=100)]
        before_comment_ids = [comment.id for comment in kb.list_comments(conn, tid)]
        before_event_ids = [event.id for event in kb.list_events(conn, tid)]

    patches = _patch_list_profiles(["global-active", "existing"])
    for profile_patch in patches:
        profile_patch.start()
    try:
        with patch("agent.auxiliary_client.call_llm") as call_llm:
            outcome = decomp.decompose_task(
                tid,
                board="empty-board",
                author="me",
            )
    finally:
        for profile_patch in patches:
            profile_patch.stop()

    assert outcome.ok is False
    assert "no installed profiles" in outcome.reason.lower()
    call_llm.assert_not_called()
    with kb.connect(board="empty-board") as conn:
        root = kb.get_task(conn, tid)
        tasks = kb.list_tasks(conn, limit=100)
        comments = kb.list_comments(conn, tid)
        events = kb.list_events(conn, tid)
    assert root == before_root
    assert [task.id for task in tasks] == before_task_ids == [tid]
    assert [comment.id for comment in comments] == before_comment_ids
    assert [event.id for event in events] == before_event_ids


def test_decompose_single_task_policy_race_is_non_ok_without_mutation(kanban_home):
    board = "policy-race-board"
    allowed_profile = "policy-worker"
    kb.create_board(board, allowed_profiles=[allowed_profile])
    with kb.connect(board=board) as conn:
        tid = kb.create_task(
            conn,
            title="rough title",
            body="rough body",
            triage=True,
        )
        before_root = kb.get_task(conn, tid)
        before_task_count = len(kb.list_tasks(conn, limit=100))
        before_comments = kb.list_comments(conn, tid)
        before_events = kb.list_events(conn, tid)
    assert before_root is not None
    before_task_state = (
        before_root.status,
        before_root.title,
        before_root.body,
        before_root.assignee,
        before_root.current_run_id,
    )

    llm_payload = jsonlib.dumps({
        "fanout": False,
        "rationale": "single unit",
        "title": "tightened title",
        "body": "tightened body",
        "assignee": allowed_profile,
    })

    def tighten_policy_then_return(**_kwargs):
        kb.write_board_metadata(board, allowed_profiles=[])
        return _fake_aux_response(llm_payload)

    patches = _patch_list_profiles([allowed_profile])
    for profile_patch in patches:
        profile_patch.start()
    try:
        with patch(
            "agent.auxiliary_client.call_llm",
            side_effect=tighten_policy_then_return,
        ):
            outcome = decomp.decompose_task(tid, board=board, author="me")
    finally:
        for profile_patch in patches:
            profile_patch.stop()

    assert outcome.ok is False
    assert outcome.reason.startswith("DB rejected task: ")
    assert "not allowed" in outcome.reason.lower()
    with kb.connect(board=board) as conn:
        root = kb.get_task(conn, tid)
        task_count = len(kb.list_tasks(conn, limit=100))
        comments = kb.list_comments(conn, tid)
        events = kb.list_events(conn, tid)
    assert root is not None
    assert (
        root.status,
        root.title,
        root.body,
        root.assignee,
        root.current_run_id,
    ) == before_task_state
    assert root == before_root
    assert task_count == before_task_count == 1
    assert comments == before_comments
    assert events == before_events
