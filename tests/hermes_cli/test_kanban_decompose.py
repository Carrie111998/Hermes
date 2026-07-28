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
        tid = kb.create_task(
            conn,
            title="plan a multi-step feature",
            assignee="planner",
            triage=True,
        )

    llm_payload = jsonlib.dumps({
        "fanout": True,
        "rationale": "test split",
        "tasks": [
            {"title": "research", "body": "look it up", "assignee": "researcher", "parents": []},
            {"title": "build", "body": "code it", "assignee": "engineer", "parents": [0]},
        ],
    })

    patches = _patch_list_profiles(
        ["orchestrator", "planner", "researcher", "engineer"]
    )
    for p in patches:
        p.start()
    try:
        with _patch_aux_client(llm_payload), _patch_extra_body():
            outcome = decomp.decompose_task(tid, author="me")
        with kb.connect() as conn:
            dispatch = kb.dispatch_once(conn, dry_run=True)
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
    assert any(task_id == c0.id for task_id, _, _ in dispatch.spawned)


def test_historical_codex_mission_cannot_be_decomposed(kanban_home):
    """The guard survives stale triage status and later reassignment."""
    with kb.connect() as conn:
        tid = kb.create_task(
            conn,
            title="already delivered",
            assignee="codex",
        )
        assert kb.claim_task(conn, tid, claimer="worker") is not None
        assert kb.block_task(conn, tid, reason="superseded")
        with kb.write_txn(conn):
            conn.execute(
                "UPDATE tasks SET status='triage', assignee='orchestrator' "
                "WHERE id=?",
                (tid,),
            )

    with patch("agent.auxiliary_client.call_llm") as call_llm:
        outcome = decomp.decompose_task(tid, author="auto-decomposer")

    assert outcome.ok is False
    assert "atomic" in outcome.reason
    call_llm.assert_not_called()
    with kb.connect() as conn:
        assert kb.get_task(conn, tid).status == "triage"
        assert conn.execute(
            "SELECT COUNT(*) FROM task_links WHERE child_id=?",
            (tid,),
        ).fetchone()[0] == 0


def test_blocked_codex_mission_is_not_redecomposed_or_dispatched(kanban_home):
    """A delivered Codex mission stays atomic after a repeated block.

    This reproduces the production loop: worker block, external unblock,
    same-cause re-block, auto-decompose, then duplicate child dispatch.
    """
    with kb.connect() as conn:
        tid = kb.create_task(
            conn,
            title="deliver the existing PR",
            assignee="codex",
        )
        assert kb.claim_task(conn, tid, claimer="worker") is not None
        assert kb.block_task(
            conn,
            tid,
            reason="PR already delivered",
            kind="needs_input",
        )
        if kb.unblock_task(conn, tid):
            assert kb.claim_task(conn, tid, claimer="worker") is not None
            assert kb.block_task(
                conn,
                tid,
                reason="PR already delivered",
                kind="needs_input",
            )

    llm_payload = jsonlib.dumps({
        "fanout": True,
        "rationale": "obsolete split",
        "tasks": [
            {
                "title": "Repeat research",
                "body": "Re-open work that was already delivered.",
                "assignee": "research",
                "parents": [],
            },
        ],
    })
    patches = _patch_list_profiles(["orchestrator", "codex", "research"])
    for p in patches:
        p.start()
    try:
        outcomes = []
        with _patch_aux_client(llm_payload), _patch_extra_body():
            for triage_id in decomp.list_triage_ids():
                outcomes.append(
                    decomp.decompose_task(
                        triage_id,
                        author="auto-decomposer",
                    )
                )
        with kb.connect() as conn:
            dispatch = kb.dispatch_once(conn, dry_run=True)
            root = kb.get_task(conn, tid)
            children = conn.execute(
                "SELECT id FROM tasks WHERE id != ?",
                (tid,),
            ).fetchall()
    finally:
        for p in patches:
            p.stop()

    assert root is not None
    assert root.status == "blocked"
    assert outcomes == []
    assert children == []
    assert dispatch.spawned == []


def test_existing_generated_child_of_terminal_codex_mission_is_not_claimed(
    kanban_home,
):
    """Legacy duplicate children are inert even if they are already ready."""
    with kb.connect() as conn:
        root_id = kb.create_task(
            conn,
            title="delivered Codex mission",
            assignee="codex",
        )
        assert kb.claim_task(conn, root_id, claimer="worker") is not None
        assert kb.block_task(conn, root_id, reason="delivered")

        child_id = kb.create_task(
            conn,
            title="obsolete Research duplicate",
            assignee="research",
            created_by="auto-decomposer",
        )
        with kb.write_txn(conn):
            conn.execute(
                "UPDATE tasks SET status='todo', assignee='orchestrator' "
                "WHERE id=?",
                (root_id,),
            )
            conn.execute(
                "INSERT INTO task_events "
                "(task_id, kind, payload, created_at) "
                "VALUES (?, 'created', ?, 1)",
                (
                    child_id,
                    jsonlib.dumps({"from_decompose_of": root_id}),
                ),
            )

        dispatch = kb.dispatch_once(conn, dry_run=True)
        claim = kb.claim_task(conn, child_id, claimer="worker")

    assert dispatch.spawned == []
    assert claim is None


def test_legacy_reviewer_descendant_of_terminal_codex_mission_is_inert(
    kanban_home,
):
    """The terminal guard follows obsolete Coding to Reviewer chains."""
    with kb.connect() as conn:
        root_id = kb.create_task(
            conn,
            title="delivered Codex mission",
            assignee="codex",
        )
        assert kb.block_task(conn, root_id, reason="PR already delivered")

        coding_id = kb.create_task(
            conn,
            title="obsolete Coding child",
            assignee="coding",
            created_by="auto-decomposer",
        )
        reviewer_id = kb.create_task(
            conn,
            title="obsolete Reviewer grandchild",
            assignee="reviewer",
            created_by="legacy-recovery",
        )
        with kb.write_txn(conn):
            conn.execute(
                "INSERT INTO task_events "
                "(task_id, kind, payload, created_at) "
                "VALUES (?, 'created', ?, 1), (?, 'created', ?, 2)",
                (
                    coding_id,
                    jsonlib.dumps({"from_decompose_of": root_id}),
                    reviewer_id,
                    jsonlib.dumps({"from_decompose_of": coding_id}),
                ),
            )

        claim = kb.claim_task(conn, reviewer_id, claimer="worker")

    assert claim is None


def test_completed_codex_mission_cannot_be_claimed_after_stale_requeue(
    kanban_home,
):
    """A delivered Codex mission stays terminal even if its status regresses."""
    with kb.connect() as conn:
        task_id = kb.create_task(
            conn,
            title="already delivered Codex mission",
            assignee="codex",
        )
        assert kb.claim_task(conn, task_id, claimer="worker") is not None
        assert kb.complete_task(conn, task_id, summary="PR delivered")
        with kb.write_txn(conn):
            conn.execute(
                "UPDATE tasks SET status='ready', completed_at=NULL WHERE id=?",
                (task_id,),
            )

        dispatch = kb.dispatch_once(conn, dry_run=True)
        claim = kb.claim_task(conn, task_id, claimer="worker")

    assert dispatch.spawned == []
    assert claim is None


def test_decompose_fanout_false_assigns_default_when_unassigned(kanban_home):
    with kb.connect() as conn:
        tid = kb.create_task(conn, title="just one thing", triage=True)

    llm_payload = jsonlib.dumps({
        "fanout": False,
        "rationale": "single unit",
        "title": "Tightened title",
        "body": "**Goal**\nDo the thing.",
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
    assert outcome.fanout is False
    assert outcome.new_title == "Tightened title"
    with kb.connect() as conn:
        task = kb.get_task(conn, tid)
    assert task is not None
    # specify path with no parents -> recompute_ready flips to 'ready'
    assert task.status == "ready"
    assert task.title == "Tightened title"
    assert task.assignee == "fallback"


def test_decompose_fanout_false_preserves_existing_assignee(kanban_home):
    with kb.connect() as conn:
        tid = kb.create_task(
            conn,
            title="already routed",
            assignee="engineer",
            triage=True,
        )

    llm_payload = jsonlib.dumps({
        "fanout": False,
        "rationale": "single unit",
        "title": "Tightened title",
        "body": "Keep existing lane.",
        "assignee": "fallback",
    })

    patches = _patch_list_profiles(["orchestrator", "engineer", "fallback"])
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
    assert task.assignee == "engineer"
    assert task.title == "Tightened title"


def test_decompose_fanout_false_uses_valid_llm_assignee(kanban_home):
    with kb.connect() as conn:
        tid = kb.create_task(conn, title="route me", triage=True)

    llm_payload = jsonlib.dumps({
        "fanout": False,
        "rationale": "single unit",
        "title": "Tightened title",
        "body": "Route to specialist.",
        "assignee": "engineer",
    })

    patches = _patch_list_profiles(["orchestrator", "engineer", "fallback"])
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
    assert task.assignee == "engineer"


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


def test_decompose_unknown_assignee_falls_back_to_default(kanban_home):
    with kb.connect() as conn:
        tid = kb.create_task(conn, title="x", triage=True)

    # Roster only has 'orchestrator' and 'fallback'; LLM picks 'made_up'.
    llm_payload = jsonlib.dumps({
        "fanout": True,
        "rationale": "test",
        "tasks": [
            {"title": "do X", "body": "", "assignee": "made_up", "parents": []},
        ],
    })

    patches = _patch_list_profiles(["orchestrator", "fallback"])
    for p in patches:
        p.start()
    try:
        with patch.dict(
            "os.environ", {}, clear=False,
        ), _patch_aux_client(llm_payload), _patch_extra_body(), \
            patch(
                "hermes_cli.kanban_decompose._load_config",
                return_value={
                    "kanban": {
                        "orchestrator_profile": "orchestrator",
                        "default_assignee": "fallback",
                    }
                },
            ):
            outcome = decomp.decompose_task(tid, author="me")
    finally:
        for p in patches:
            p.stop()

    assert outcome.ok, outcome.reason
    assert outcome.child_ids and len(outcome.child_ids) == 1
    with kb.connect() as conn:
        child = kb.get_task(conn, outcome.child_ids[0])
    # 'made_up' wasn't in roster, so assignee rewritten to 'fallback'
    assert child.assignee == "fallback"


def test_decompose_handles_malformed_llm_json(kanban_home):
    with kb.connect() as conn:
        tid = kb.create_task(conn, title="x", triage=True)

    patches = _patch_list_profiles(["orchestrator"])
    for p in patches:
        p.start()
    try:
        with _patch_aux_client("not json at all, sorry"), _patch_extra_body():
            outcome = decomp.decompose_task(tid, author="me")
    finally:
        for p in patches:
            p.stop()

    assert outcome.ok is False
    assert "malformed JSON" in outcome.reason


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
