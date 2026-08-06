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


def test_decompose_no_aux_client_configured(kanban_home):
    with kb.connect() as conn:
        tid = kb.create_task(conn, title="x", triage=True)

    patches = _patch_list_profiles(["orchestrator"])
    for p in patches:
        p.start()
    try:
        # call_llm raises RuntimeError when no provider is configured; the
        # decomposer must convert that into a failed outcome, not a crash.
        with patch(
            "agent.auxiliary_client.call_llm",
            side_effect=RuntimeError("No LLM provider configured"),
        ):
            outcome = decomp.decompose_task(tid, author="me")
    finally:
        for p in patches:
            p.stop()

    assert outcome.ok is False
    # call_llm's no-provider RuntimeError surfaces via the LLM-error branch.
    assert "LLM error" in outcome.reason


def test_list_triage_ids_uses_explicit_board(kanban_home):
    with kb.connect_closing() as conn:
        default_id = kb.create_task(conn, title="default", triage=True)
    with kb.connect_closing(board="target-board") as conn:
        target_id = kb.create_task(
            conn,
            title="target",
            triage=True,
            board="target-board",
        )

    assert decomp.list_triage_ids(board="target-board") == [target_id]
    assert default_id not in decomp.list_triage_ids(board="target-board")


def test_decompose_task_uses_explicit_board(kanban_home):
    with kb.connect_closing(board="target-board") as conn:
        tid = kb.create_task(
            conn,
            title="target task",
            triage=True,
            board="target-board",
        )

    llm_payload = jsonlib.dumps({
        "fanout": True,
        "rationale": "target-board split",
        "tasks": [
            {
                "title": "target child",
                "body": "work in the target board",
                "assignee": "orchestrator",
                "parents": [],
            },
        ],
    })
    patches = _patch_list_profiles(["orchestrator"])
    for p in patches:
        p.start()
    try:
        with _patch_aux_client(llm_payload), _patch_extra_body():
            outcome = decomp.decompose_task(
                tid,
                author="me",
                board="target-board",
            )
    finally:
        for p in patches:
            p.stop()

    assert outcome.ok, outcome.reason
    assert outcome.child_ids and len(outcome.child_ids) == 1
    with kb.connect_closing(board="target-board") as conn:
        root = kb.get_task(conn, tid)
        child = kb.get_task(conn, outcome.child_ids[0])
    with kb.connect_closing() as conn:
        assert kb.get_task(conn, tid) is None
        assert kb.get_task(conn, outcome.child_ids[0]) is None
    assert root.status == "todo"
    assert child.title == "target child"


# ── [hermes-v2] H-22: idempotent freeform replay ──────────────────────


def test_decompose_replay_returns_existing_children_without_recall(
    kanban_home, monkeypatch
):
    """[hermes-v2] H-22: a triage root that ALREADY has children
    (from a prior successful decompose) must NOT be re-decomposed on
    the next call. We mock the auxiliary client so a stray invocation
    would change the response shape — if decompose_task re-called the
    LLM, the second child_id list would differ and break downstream
    lookups keyed on the original IDs. The outcome must report
    ``ok=True`` with the existing child IDs and a sensible
    ``already decomposed`` reason.
    """
    with kb.connect() as conn:
        root = kb.create_task(conn, title="replayable triage", triage=True)
        child_a = kb.create_task(
            conn, title="already there A", parents=[root]
        )
        child_b = kb.create_task(
            conn, title="already there B", parents=[root]
        )

    # The mock below MUST NOT be hit — if it is, the test fails by
    # raising (no aux client configured => RuntimeError). The test
    # still calls decompose_task from a non-running state so the
    # ``auxiliary client unavailable`` branch would surface that error
    # if our replay short-circuit missed.
    unexpected_call = False

    def _must_not_call(*args, **kwargs):  # pragma: no cover - failure path
        nonlocal unexpected_call
        unexpected_call = True
        raise RuntimeError(
            "decompose_task re-invoked the auxiliary client on a "
            "replay of an already-decomposed triage root"
        )

    monkeypatch.setattr(
        "agent.auxiliary_client.call_llm", _must_not_call
    )

    outcome = decomp.decompose_task(root, author="me")

    assert unexpected_call is False, (
        "decompose_task must NOT call call_llm on an already-decomposed root"
    )
    assert outcome.ok is True, outcome.reason
    assert "already decomposed" in outcome.reason
    assert outcome.fanout is True
    # The existing child IDs must be returned — set comparison avoids
    # any ordering nit (``child_ids`` is sorted by the DB query for
    # stability, not insertion order).
    assert sorted(outcome.child_ids) == sorted([child_a, child_b])


def test_decompose_replay_retries_when_no_children_exist(
    kanban_home,
):
    """A triage root with NO existing children (e.g. the prior attempt
    raised ``auxiliary client unavailable`` or returned
    ``fanout=true with empty tasks list``) MUST be allowed to retry.
    The dispatcher relies on this retry to eventually surface a
    successful decomposition — without it, a single transient
    failure would freeze the root in triage forever.
    """
    with kb.connect() as conn:
        tid = kb.create_task(conn, title="retry me", triage=True)

    llm_payload = jsonlib.dumps({
        "fanout": True,
        "rationale": "retry split",
        "tasks": [
            {
                "title": "first child",
                "body": "do the work",
                "assignee": "orchestrator",
                "parents": [],
            },
        ],
    })

    patches = _patch_list_profiles(["orchestrator"])
    for p in patches:
        p.start()
    try:
        with _patch_aux_client(llm_payload), _patch_extra_body():
            outcome = decomp.decompose_task(tid, author="me")
    finally:
        for p in patches:
            p.stop()

    assert outcome.ok is True, outcome.reason
    assert outcome.child_ids and len(outcome.child_ids) == 1
    assert outcome.fanout is True
