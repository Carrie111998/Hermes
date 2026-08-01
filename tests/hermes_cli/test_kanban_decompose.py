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


def _drive_block_loop_to_triage(tid: str) -> None:
    with kb.connect() as conn:
        assert kb.claim_task(conn, tid, claimer="mike") is not None
        assert kb.block_task(conn, tid, reason="await review", kind="needs_input")
        assert kb.unblock_task(conn, tid)
        assert kb.claim_task(conn, tid, claimer="mike") is not None
        assert kb.block_task(conn, tid, reason="still await review", kind="needs_input")
        task = kb.get_task(conn, tid)
        assert task is not None
        assert task.status == "triage"


def test_block_loop_triage_requires_explicit_continuation_without_llm_or_children(
    kanban_home,
):
    with kb.connect() as conn:
        tid = kb.create_task(
            conn,
            title="existing development workflow",
            body="old bootstrap instructions",
            assignee="mike",
        )
    _drive_block_loop_to_triage(tid)
    with kb.connect() as conn:
        kb.add_comment(conn, tid, author="karim", body="Approved: bounded continuation only")

    with patch("agent.auxiliary_client.call_llm") as call_llm:
        outcome = decomp.decompose_task(tid, author="auto-decomposer")

    assert outcome.ok is False
    assert "explicit continuation" in outcome.reason
    call_llm.assert_not_called()
    with kb.connect() as conn:
        task = kb.get_task(conn, tid)
        assert task is not None
        assert task.status == "triage"
        assert task.assignee == "mike"
        assert kb.child_ids(conn, tid) == []
        assert kb.list_comments(conn, tid)[-1].body == "Approved: bounded continuation only"


def test_recent_comments_follow_body_and_are_declared_authoritative(kanban_home):
    with kb.connect() as conn:
        tid = kb.create_task(
            conn,
            title="bounded change",
            body="Old scope: replay the bootstrap",
            assignee="mike",
            triage=True,
        )
        kb.add_comment(conn, tid, author="karim", body="New scope: do not replay bootstrap")

    payload = jsonlib.dumps(
        {
            "fanout": False,
            "rationale": "single continuation",
            "title": "Bounded continuation",
            "body": "Do not replay bootstrap.",
            "assignee": "mike",
        }
    )
    with patch(
        "agent.auxiliary_client.call_llm",
        return_value=_fake_aux_response(payload),
    ) as call_llm:
        outcome = decomp.decompose_task(tid, author="me")

    assert outcome.ok is True
    messages = call_llm.call_args.kwargs["messages"]
    assert "later comments override conflicting" in messages[0]["content"].lower()
    user_prompt = messages[1]["content"]
    assert user_prompt.index("Old scope") < user_prompt.index("New scope")


def test_fanout_preserves_existing_root_owner(kanban_home):
    with kb.connect() as conn:
        tid = kb.create_task(
            conn, title="development root", assignee="mike", triage=True
        )
    payload = jsonlib.dumps(
        {
            "fanout": True,
            "rationale": "parallel units",
            "tasks": [
                {"title": "inspect", "assignee": "researcher", "parents": []}
            ],
        }
    )
    patches = _patch_list_profiles(["default", "mike", "researcher"])
    for patcher in patches:
        patcher.start()
    try:
        with _patch_aux_client(payload), patch(
            "hermes_cli.kanban_decompose._load_config",
            return_value={"kanban": {"orchestrator_profile": "default"}},
        ):
            outcome = decomp.decompose_task(tid, author="me")
    finally:
        for patcher in patches:
            patcher.stop()

    assert outcome.ok is True
    with kb.connect() as conn:
        task = kb.get_task(conn, tid)
        assert task is not None
        assert task.assignee == "mike"


