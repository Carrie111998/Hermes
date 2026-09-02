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




# --------------------------------------------------------------------------
# Containment: triage tasks parked on a non-spawnable assignee (#62985)
#
# A task whose assignee names a control-plane lane rather than a Hermes
# profile is pulled by a terminal via ``claim_task``; the harness must
# never launch it. Before the guard, auto-decompose rewrote that assignee
# to ``kanban.default_assignee`` and promoted the task out of triage, so
# the dispatcher spawned work its owner had deliberately withheld.
# --------------------------------------------------------------------------

NONSPAWNABLE = "orion-cc"  # a terminal lane, not a Hermes profile


def _aux_client_must_not_be_called():
    return patch(
        "agent.auxiliary_client.call_llm",
        side_effect=AssertionError("decomposer reached the LLM for a parked task"),
    )


def test_decompose_refuses_a_task_parked_on_a_nonspawnable_assignee(kanban_home):
    with kb.connect() as conn:
        tid = kb.create_task(
            conn, title="held by a terminal lane",
            assignee=NONSPAWNABLE, triage=True,
        )

    patches = _patch_list_profiles(["orchestrator", "fallback"])
    for p in patches:
        p.start()
    try:
        with _aux_client_must_not_be_called(), patch(
            "hermes_cli.kanban_decompose._load_config",
            return_value={"kanban": {"default_assignee": "fallback"}},
        ):
            outcome = decomp.decompose_task(tid, author="me")
    finally:
        for p in patches:
            p.stop()

    assert outcome.ok is False
    assert "not a spawnable profile" in outcome.reason

    with kb.connect() as conn:
        task = kb.get_task(conn, tid)
    assert task.status == "triage"
    assert task.assignee == NONSPAWNABLE


def test_decompose_still_runs_for_a_task_assigned_to_a_real_profile(kanban_home):
    with kb.connect() as conn:
        tid = kb.create_task(
            conn, title="ordinary triage work",
            assignee="engineer", triage=True,
        )

    llm_payload = jsonlib.dumps({
        "fanout": False,
        "rationale": "single unit",
        "title": "Tightened title",
        "body": "Do the thing.",
        "assignee": "engineer",
    })

    patches = _patch_list_profiles(["orchestrator", "engineer"])
    for p in patches:
        p.start()
    try:
        with _patch_aux_client(llm_payload), _patch_extra_body():
            outcome = decomp.decompose_task(tid, author="me")
    finally:
        for p in patches:
            p.stop()

    assert outcome.ok, outcome.reason
    with kb.connect() as conn:
        task = kb.get_task(conn, tid)
    assert task.status != "triage"
    assert task.assignee == "engineer"


def test_decompose_proceeds_for_an_unassigned_triage_task(kanban_home):
    """The guard is about *deliberate* parking. An unassigned triage task
    is exactly what the decomposer exists to route, so it must pass."""
    with kb.connect() as conn:
        tid = kb.create_task(conn, title="nobody owns this yet", triage=True)

    llm_payload = jsonlib.dumps({
        "fanout": False, "rationale": "r",
        "title": "T", "body": "B", "assignee": "fallback",
    })

    patches = _patch_list_profiles(["orchestrator", "fallback"])
    for p in patches:
        p.start()
    try:
        with _patch_aux_client(llm_payload), _patch_extra_body():
            outcome = decomp.decompose_task(tid, author="me")
    finally:
        for p in patches:
            p.stop()

    assert outcome.ok, outcome.reason


def test_list_triage_ids_skips_tasks_parked_on_nonspawnable_assignees(kanban_home):
    with kb.connect() as conn:
        routable = kb.create_task(conn, title="route me", triage=True)
        parked = kb.create_task(
            conn, title="parked", assignee=NONSPAWNABLE, triage=True,
        )

    patches = _patch_list_profiles(["orchestrator", "fallback"])
    for p in patches:
        p.start()
    try:
        ids = decomp.list_triage_ids()
    finally:
        for p in patches:
            p.stop()

    assert routable in ids
    assert parked not in ids


def test_guard_fails_open_when_the_profile_registry_is_unreadable(kanban_home):
    """A broken profile registry must not freeze ordinary decomposition.
    Mirrors the dispatcher, which also treats an unreadable registry as
    'assume spawnable' rather than refusing every task."""
    with kb.connect() as conn:
        tid = kb.create_task(
            conn, title="registry is down", assignee="engineer", triage=True,
        )

    with patch(
        "hermes_cli.profiles.profile_exists",
        side_effect=RuntimeError("registry unreadable"),
    ):
        assert decomp._assignee_is_spawnable("engineer") is True
        assert decomp.list_triage_ids() == [tid]


def test_dispatch_never_reassigns_or_spawns_a_parked_triage_task(kanban_home):
    """Regression for #62985, end to end: with ``kanban.default_assignee``
    configured, a triage task parked on a non-spawnable assignee survives
    both an auto-decompose tick and a dispatcher tick untouched."""
    with kb.connect() as conn:
        tid = kb.create_task(
            conn, title="containment", assignee=NONSPAWNABLE, triage=True,
        )

    spawned = []

    def _recording_spawn(*args, **kwargs):
        spawned.append((args, kwargs))
        return 4242

    patches = _patch_list_profiles(["orchestrator", "fallback"])
    for p in patches:
        p.start()
    try:
        with _aux_client_must_not_be_called(), patch(
            "hermes_cli.kanban_decompose._load_config",
            return_value={"kanban": {"default_assignee": "fallback"}},
        ):
            # The auto-decompose tick picks its work through this call.
            assert decomp.list_triage_ids() == []
            # And the guard holds even if a task id reaches it directly.
            assert decomp.decompose_task(tid, author="tick").ok is False

        with kb.connect() as conn:
            result = kb.dispatch_once(
                conn, spawn_fn=_recording_spawn, dry_run=False,
                default_assignee="fallback",
            )
    finally:
        for p in patches:
            p.stop()

    assert spawned == []
    assert tid not in [t[0] for t in result.spawned]
    assert tid not in result.auto_assigned_default

    with kb.connect() as conn:
        task = kb.get_task(conn, tid)
    assert task.status == "triage"
    assert task.assignee == NONSPAWNABLE
