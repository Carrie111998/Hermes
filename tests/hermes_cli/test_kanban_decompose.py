"""Tests for the decomposer module + `hermes kanban decompose` CLI surface.

The auxiliary LLM client is mocked — no network calls. Tests exercise the
prompt plumbing, response parsing, DB writes (via the real DB helper),
and the assignee-fallback logic.
"""

from __future__ import annotations

import json as jsonlib
import threading
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


def test_same_lineage_constraint_refuses_before_llm(kanban_home):
    """A durable operator constraint gates the real entrypoint before inference."""
    with kb.connect_closing() as conn:
        tid = kb.create_task(conn, title="keep this task whole", triage=True)
        kb.set_decomposition_constraint(
            conn,
            tid,
            constraint=kb.DECOMPOSITION_CONSTRAINT_SAME_LINEAGE,
            reason="operator approved no decomposition",
        )

    with patch("agent.auxiliary_client.call_llm") as call_llm:
        outcome = decomp.decompose_task(tid, author="auto-decomposer")

    assert outcome.ok is False
    assert "SAME-LINEAGE" in outcome.reason
    call_llm.assert_not_called()


def test_manual_author_spoof_cannot_bypass_and_refusal_audit_is_idempotent(kanban_home):
    with kb.connect_closing() as conn:
        tid = kb.create_task(conn, title="whole", triage=True)
        kb.set_decomposition_constraint(
            conn, tid, constraint=kb.DECOMPOSITION_CONSTRAINT_SAME_LINEAGE,
            reason="keep lineage",
        )

    for author in ("operator", "root", "auto-decomposer"):
        assert not decomp.decompose_task(tid, author=author).ok

    with kb.connect_closing() as conn:
        count = conn.execute(
            "SELECT COUNT(*) FROM task_events "
            "WHERE task_id = ? AND kind = 'decomposition_refused'", (tid,),
        ).fetchone()[0]
    assert count == 1


def test_direct_manual_graph_write_cannot_bypass_constraint(kanban_home):
    with kb.connect_closing() as conn:
        tid = kb.create_task(conn, title="whole", triage=True)
        kb.set_decomposition_constraint(
            conn, tid, constraint=kb.DECOMPOSITION_CONSTRAINT_SAME_LINEAGE,
            reason="keep lineage",
        )
        with pytest.raises(RuntimeError, match="SAME-LINEAGE"):
            kb.decompose_triage_task(
                conn,
                tid,
                root_assignee="operator",
                children=[{"title": "bypass", "parents": []}],
                author="operator",
            )
        assert kb.get_task(conn, tid).status == "triage"


@pytest.mark.parametrize("payload", [None, "not-json", '{"version":1}'])
def test_malformed_typed_constraint_events_fail_closed_before_llm(kanban_home, payload):
    with kb.connect_closing() as conn:
        tid = kb.create_task(conn, title="whole", triage=True)
        with kb.write_txn(conn):
            conn.execute(
                "INSERT INTO task_events(task_id, kind, payload, created_at) "
                "VALUES (?, 'decomposition_constraint_set', ?, 1)", (tid, payload),
            )
    with patch("agent.auxiliary_client.call_llm") as call_llm:
        outcome = decomp.decompose_task(tid)
    assert not outcome.ok
    assert "malformed or ambiguous" in outcome.reason
    call_llm.assert_not_called()


def test_ambiguous_typed_constraints_fail_closed(kanban_home):
    with kb.connect_closing() as conn:
        tid = kb.create_task(conn, title="whole", triage=True)
        payload = jsonlib.dumps({
            "version": 1,
            "constraint": kb.DECOMPOSITION_CONSTRAINT_SAME_LINEAGE,
            "reason": "duplicate",
        })
        with kb.write_txn(conn):
            conn.execute(
                "INSERT INTO task_events(task_id, kind, payload, created_at) "
                "VALUES (?, 'decomposition_constraint_set', ?, 1)", (tid, payload),
            )
            conn.execute(
                "INSERT INTO task_events(task_id, kind, payload, created_at) "
                "VALUES (?, 'decomposition_constraint_set', ?, 2)", (tid, payload),
            )
    assert not decomp.decompose_task(tid).ok


@pytest.mark.parametrize("fanout", [False, True])
def test_policy_cursor_change_invalidates_respec_and_fanout_writes(
    kanban_home, fanout
):
    with kb.connect_closing() as conn:
        tid = kb.create_task(conn, title="race", triage=True)
    payload = ({
        "fanout": True,
        "tasks": [{"title": "child", "body": "x", "assignee": "orchestrator", "parents": []}],
    } if fanout else {"fanout": False, "title": "changed", "body": "changed"})

    def race_policy(*args, **kwargs):
        with kb.connect_closing() as conn:
            with kb.write_txn(conn):
                conn.execute(
                    "INSERT INTO task_events(task_id, kind, payload, created_at) "
                    "VALUES (?, 'decomposition_constraint_set', ?, 1)",
                    (tid, jsonlib.dumps({
                        "version": 1,
                        "constraint": kb.DECOMPOSITION_CONSTRAINT_SAME_LINEAGE,
                        "reason": "arrived during inference",
                    })),
                )
        return _fake_aux_response(jsonlib.dumps(payload))

    patches = _patch_list_profiles(["orchestrator"])
    for p in patches:
        p.start()
    try:
        with patch("agent.auxiliary_client.call_llm", side_effect=race_policy):
            outcome = decomp.decompose_task(tid)
    finally:
        for p in patches:
            p.stop()
    assert not outcome.ok
    with kb.connect_closing() as conn:
        assert kb.get_task(conn, tid).status == "triage"
        assert conn.execute(
            "SELECT COUNT(*) FROM task_decomposition_reservations WHERE task_id = ?",
            (tid,),
        ).fetchone()[0] == 0
        assert conn.execute("SELECT COUNT(*) FROM task_links").fetchone()[0] == 0


def test_reserve_vs_constraint_set_has_exactly_one_winner(kanban_home):
    with kb.connect_closing() as conn:
        tid = kb.create_task(conn, title="linearize", triage=True)
    barrier = threading.Barrier(2)
    outcomes = []

    def reserve():
        with kb.connect_closing() as conn:
            barrier.wait()
            outcomes.append(("reserve", kb.reserve_decomposition(conn, tid)))

    def constrain():
        with kb.connect_closing() as conn:
            barrier.wait()
            try:
                kb.set_decomposition_constraint(
                    conn, tid,
                    constraint=kb.DECOMPOSITION_CONSTRAINT_SAME_LINEAGE,
                    reason="race",
                )
            except RuntimeError:
                outcomes.append(("constraint", False))
            else:
                outcomes.append(("constraint", True))

    threads = [threading.Thread(target=reserve), threading.Thread(target=constrain)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    reserve_result = next(value for kind, value in outcomes if kind == "reserve")
    constraint_result = next(value for kind, value in outcomes if kind == "constraint")
    assert int(reserve_result.ok) + int(constraint_result) == 1


def test_crash_reservation_fails_closed_until_bounded_audited_recovery(kanban_home):
    with kb.connect_closing() as conn:
        tid = kb.create_task(conn, title="crashed decomposer", triage=True)
        reservation = kb.reserve_decomposition(conn, tid)
        assert reservation.ok
        with kb.write_txn(conn):
            conn.execute(
                "UPDATE task_decomposition_reservations SET reserved_at = 1 "
                "WHERE task_id = ?", (tid,),
            )

    blocked = decomp.decompose_task(tid)
    assert not blocked.ok
    assert "already reserved" in blocked.reason

    with kb.connect_closing() as conn:
        with pytest.raises(ValueError, match="between"):
            kb.recover_stale_decomposition_reservation(
                conn, tid, older_than_seconds=0, reason="unsafe",
            )
        assert kb.recover_stale_decomposition_reservation(
            conn, tid, older_than_seconds=60, reason="operator verified crash",
        )
        event = conn.execute(
            "SELECT payload FROM task_events WHERE task_id = ? "
            "AND kind = 'decomposition_reservation_recovered'", (tid,),
        ).fetchone()
        assert jsonlib.loads(event["payload"])["reason"] == "operator verified crash"
