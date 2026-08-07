"""Tests for the durable retained-subagent registry (tracker #79686 P1).

Covers: schema additions on async_delegations + the delegation_child_usage
ledger, child-manifest persistence, retention on completion, tombstoning
(addressability removed, transcript untouched), TTL/cap pruning, restart
rehydration through recover_abandoned_delegations, the compression-lineage
follow-up authority model (design credit @0xbWy, #76512), persisted
child-usage attribution with reapply-on-load totals, and the budget-exempt
delegate_task(follow_up=...) paths.
"""

import json
import sqlite3
import time
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from tools import async_delegation as ad


@pytest.fixture(autouse=True)
def _clean_state(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    ad._reset_for_tests()
    yield
    ad._reset_for_tests()


def _db_conn(tmp_path) -> sqlite3.Connection:
    return sqlite3.connect(tmp_path / "state.db")


def _insert_delegation(delegation_id, *, parent_session_id="parent-1", state="running"):
    with ad._DB_LOCK, ad._transaction() as conn:
        conn.execute(
            """INSERT OR REPLACE INTO async_delegations
               (delegation_id, origin_session, origin_ui_session_id,
                parent_session_id, state, dispatched_at, updated_at,
                delivery_state, delivery_attempts, origin_session_id)
               VALUES (?, '', '', ?, ?, ?, ?, 'pending', 0, '')""",
            (delegation_id, parent_session_id, state, time.time(), time.time()),
        )


def _dispatch_and_retain(
    delegation_id="deleg_test0001",
    *,
    parent_session_id="parent-1",
    children=None,
):
    _insert_delegation(delegation_id, parent_session_id=parent_session_id)
    ad.record_dispatched_children(
        delegation_id,
        children
        or [
            {
                "subagent_id": "sa-0-abc12345",
                "session_id": "child-sess-1",
                "model": "test/model",
                "goal": "do the thing",
            }
        ],
    )
    ad.retain_completed_delegation(delegation_id, usage={"cost_usd": 0.5})
    return delegation_id


# ---------------------------------------------------------------------------
# Schema
# ---------------------------------------------------------------------------


def test_schema_adds_retained_columns_and_usage_table(tmp_path):
    conn = ad._connect()
    conn.close()
    raw = _db_conn(tmp_path)
    try:
        cols = {r[1] for r in raw.execute("PRAGMA table_info(async_delegations)")}
        for expected in (
            "retained", "retained_at", "tombstoned_at", "child_session_id",
            "children_json", "owner_profile", "usage_json",
        ):
            assert expected in cols
        tables = {
            r[0]
            for r in raw.execute("SELECT name FROM sqlite_master WHERE type='table'")
        }
        assert "delegation_child_usage" in tables
    finally:
        raw.close()


def test_schema_upgrade_is_backwards_compatible(tmp_path):
    """A legacy DB without the new columns gains them on connect."""
    raw = _db_conn(tmp_path)
    raw.execute(
        """CREATE TABLE async_delegations (
            delegation_id TEXT PRIMARY KEY,
            origin_session TEXT NOT NULL,
            origin_ui_session_id TEXT NOT NULL DEFAULT '',
            parent_session_id TEXT,
            state TEXT NOT NULL,
            dispatched_at REAL NOT NULL,
            completed_at REAL,
            updated_at REAL NOT NULL,
            event_json TEXT,
            result_json TEXT,
            delivery_state TEXT NOT NULL DEFAULT 'pending',
            delivery_attempts INTEGER NOT NULL DEFAULT 0,
            delivered_at REAL
        )"""
    )
    raw.commit()
    raw.close()
    conn = ad._connect()
    try:
        cols = {r[1] for r in conn.execute("PRAGMA table_info(async_delegations)")}
        assert "retained" in cols and "children_json" in cols
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Retained registry lifecycle
# ---------------------------------------------------------------------------


def test_retain_and_list_children():
    _dispatch_and_retain()
    entries = ad.list_retained_children()
    assert len(entries) == 1
    entry = entries[0]
    assert entry["child_id"] == "sa-0-abc12345"
    assert entry["child_session_id"] == "child-sess-1"
    assert entry["model"] == "test/model"
    assert entry["delegation_id"] == "deleg_test0001"


def test_find_retained_child_by_any_id():
    _dispatch_and_retain()
    for key in ("sa-0-abc12345", "child-sess-1", "deleg_test0001"):
        assert ad.find_retained_child(key) is not None
    assert ad.find_retained_child("nope") is None


def test_tombstone_removes_addressability_only(tmp_path):
    _dispatch_and_retain()
    assert ad.tombstone_retained_child("sa-0-abc12345") is True
    assert ad.find_retained_child("sa-0-abc12345") is None
    # Second tombstone is a no-op (already de-registered).
    assert ad.tombstone_retained_child("sa-0-abc12345") is False
    # The durable row (and thus everything pointing at the transcript)
    # still exists — tombstoning never deletes.
    raw = _db_conn(tmp_path)
    try:
        row = raw.execute(
            "SELECT tombstoned_at, children_json FROM async_delegations "
            "WHERE delegation_id='deleg_test0001'"
        ).fetchone()
    finally:
        raw.close()
    assert row is not None
    assert row[0] is not None
    assert "child-sess-1" in (row[1] or "")


def test_ttl_prune_tombstones_expired_rows(tmp_path):
    _dispatch_and_retain()
    # Backdate retained_at past the TTL.
    raw = _db_conn(tmp_path)
    raw.execute(
        "UPDATE async_delegations SET retained_at=? WHERE delegation_id=?",
        (time.time() - 100 * 3600, "deleg_test0001"),
    )
    raw.commit()
    raw.close()
    pruned = ad.prune_retained_children(ttl_hours=72)
    assert pruned == 1
    assert ad.list_retained_children() == []


def test_cap_prune_keeps_newest():
    for i in range(4):
        _dispatch_and_retain(
            f"deleg_cap{i:05d}",
            children=[{"subagent_id": f"sa-{i}", "session_id": f"cs-{i}"}],
        )
        time.sleep(0.01)
    ad.prune_retained_children(max_retained=2, ttl_hours=9999)
    remaining = {e["delegation_id"] for e in ad.list_retained_children()}
    assert remaining == {"deleg_cap00002", "deleg_cap00003"}


def test_completion_persist_retains_only_success():
    _insert_delegation("deleg_ok000001")
    _insert_delegation("deleg_err00001")
    ad.record_dispatched_children(
        "deleg_ok000001", [{"subagent_id": "sa-ok", "session_id": "cs-ok"}]
    )
    ad.record_dispatched_children(
        "deleg_err00001", [{"subagent_id": "sa-err", "session_id": "cs-err"}]
    )
    ad._persist_completion(
        {"delegation_id": "deleg_ok000001", "status": "completed"},
        {"status": "completed", "summary": "done"},
    )
    ad._persist_completion(
        {"delegation_id": "deleg_err00001", "status": "error"},
        {"status": "error", "summary": None},
    )
    ids = {e["delegation_id"] for e in ad.list_retained_children()}
    assert ids == {"deleg_ok000001"}


def test_recover_abandoned_delegations_rehydrates_retained(tmp_path):
    """Restart path: retained rows survive and are validated during recovery."""
    _dispatch_and_retain()
    # Simulate restart: in-memory registry gone, durable rows remain.
    ad._reset_for_tests()
    ad.recover_abandoned_delegations()
    entries = ad.list_retained_children()
    assert [e["child_session_id"] for e in entries] == ["child-sess-1"]


# ---------------------------------------------------------------------------
# Follow-up authority (compression lineage; design credit @0xbWy #76512)
# ---------------------------------------------------------------------------


def _session_db(tmp_path):
    from hermes_state import SessionDB

    return SessionDB(Path(tmp_path) / "state.db")


def test_lineage_authority_allows_compression_continuation(tmp_path):
    db = _session_db(tmp_path)
    try:
        db.create_session("A", source="cli")
        db.end_session("A", "compression")
        db.create_session("B", source="cli", parent_session_id="A")
    finally:
        db.close()
    _dispatch_and_retain(parent_session_id="A")
    entry = ad.find_retained_child("sa-0-abc12345")
    # The compression continuation B shares A's lineage root → allowed.
    assert ad.check_follow_up_authority(entry, "B") is None
    # A itself is also allowed.
    assert ad.check_follow_up_authority(entry, "A") is None


def test_lineage_authority_rejects_siblings_and_foreigners(tmp_path):
    db = _session_db(tmp_path)
    try:
        db.create_session("A", source="cli")
        db.end_session("A", "compression")
        db.create_session("B", source="cli", parent_session_id="A")
        db.create_session(
            "branch", source="cli", parent_session_id="A",
            model_config={"_branched_from": "A"},
        )
        db.create_session(
            "delegatechild", source="delegate", parent_session_id="A",
            model_config={"_delegate_from": "A"},
        )
        db.create_session("stranger", source="cli")
    finally:
        db.close()
    _dispatch_and_retain(parent_session_id="A")
    entry = ad.find_retained_child("sa-0-abc12345")
    for foreign in ("branch", "delegatechild", "stranger", None, ""):
        assert ad.check_follow_up_authority(entry, foreign) is not None


def test_lineage_authority_rejects_child_self_follow_up(tmp_path):
    db = _session_db(tmp_path)
    try:
        db.create_session("A", source="cli")
    finally:
        db.close()
    _dispatch_and_retain(parent_session_id="A")
    entry = ad.find_retained_child("sa-0-abc12345")
    err = ad.check_follow_up_authority(entry, "child-sess-1")
    assert err is not None and "itself" in err


def test_foreign_profile_is_rejected(tmp_path, monkeypatch):
    db = _session_db(tmp_path)
    try:
        db.create_session("A", source="cli")
    finally:
        db.close()
    _dispatch_and_retain(parent_session_id="A")
    entry = dict(ad.find_retained_child("sa-0-abc12345"))
    entry["owner_profile"] = "someone-else"
    monkeypatch.setattr(ad, "_current_owner_profile", lambda: "me")
    err = ad.check_follow_up_authority(entry, "A")
    assert err is not None and "profile" in err


# ---------------------------------------------------------------------------
# Persisted usage attribution
# ---------------------------------------------------------------------------


def test_usage_attribution_roundtrip_and_totals():
    ad._connect().close()  # ensure schema
    ad.record_child_usage_attribution(
        parent_session_id="p1",
        parent_turn_id="turn-1",
        child_session_id="c1",
        usage={"cost_usd": 0.25, "tokens": {"input": 100, "output": 50}},
        aggregate={"session_estimated_cost_usd": 1.25},
    )
    ad.record_child_usage_attribution(
        parent_session_id="p1",
        child_session_id="c2",
        usage={"cost_usd": 0.75, "tokens": {"input": 10, "output": 5}},
    )
    ad.record_child_usage_attribution(
        parent_session_id="other",
        usage={"cost_usd": 99.0},
    )
    records = ad.load_child_usage_attributions("p1")
    assert len(records) == 2
    assert records[0]["parent_turn_id"] == "turn-1"
    assert records[0]["aggregate"]["session_estimated_cost_usd"] == 1.25
    totals = ad.load_child_usage_totals("p1")
    assert totals["cost_usd"] == pytest.approx(1.0)
    assert totals["input_tokens"] == pytest.approx(110)
    assert totals["output_tokens"] == pytest.approx(55)
    assert ad.load_child_usage_totals("missing") == {
        "cost_usd": 0.0, "input_tokens": 0.0, "output_tokens": 0.0,
    }


def test_finalize_child_results_persists_attribution(tmp_path):
    import tools.delegate_tool as dt

    ad._connect().close()
    parent = MagicMock()
    parent.session_id = "parent-durable"
    parent._current_turn_id = "turn-9"
    parent._memory_manager = None
    parent.session_estimated_cost_usd = 0.0
    parent.session_cost_source = "none"
    parent.session_cost_status = "unknown"
    child = MagicMock()
    child.session_id = "child-durable"
    results = [
        {
            "task_index": 0,
            "status": "completed",
            "summary": "ok",
            "tokens": {"input": 7, "output": 3},
            "api_calls": 2,
            "_child_role": "leaf",
            "_child_cost_usd": 0.42,
            "duration_seconds": 1.0,
        }
    ]
    dt._finalize_child_results(results, [{"goal": "g"}], [(0, {"goal": "g"}, child)], parent)
    assert parent.session_estimated_cost_usd == pytest.approx(0.42)
    totals = ad.load_child_usage_totals("parent-durable")
    assert totals["cost_usd"] == pytest.approx(0.42)
    recs = ad.load_child_usage_attributions("parent-durable")
    assert recs[0]["child_session_id"] == "child-durable"
    assert recs[0]["usage"]["tokens"] == {"input": 7, "output": 3}


# ---------------------------------------------------------------------------
# delegate_task(follow_up=...)
# ---------------------------------------------------------------------------


def _parent_agent(depth=0):
    parent = MagicMock()
    parent._delegate_depth = depth
    parent.session_id = "A"
    parent._session_db = None
    return parent


def test_follow_up_steers_running_child(monkeypatch):
    import tools.delegate_tool as dt

    agent = MagicMock()
    agent.session_id = "live-child-sess"
    agent.steer.return_value = True
    dt._register_subagent(
        {"subagent_id": "sa-live-1", "agent": agent, "accepting_steer": True}
    )
    try:
        out = json.loads(
            dt.delegate_task(
                goal="please also check X",
                follow_up="sa-live-1",
                parent_agent=_parent_agent(),
            )
        )
        assert out["status"] == "queued"
        assert out["child_id"] == "sa-live-1"
        agent.steer.assert_called_once()
        # Addressing by the child's session id works too.
        agent.steer.reset_mock()
        out2 = json.loads(
            dt.delegate_task(
                goal="more", follow_up="live-child-sess",
                parent_agent=_parent_agent(),
            )
        )
        assert out2["status"] == "queued"
    finally:
        dt._unregister_subagent("sa-live-1")


def test_follow_up_is_budget_exempt(monkeypatch):
    """A follow-up succeeds even when the spawn depth budget is exhausted."""
    import tools.delegate_tool as dt

    agent = MagicMock()
    agent.steer.return_value = True
    dt._register_subagent(
        {"subagent_id": "sa-deep-1", "agent": agent, "accepting_steer": True}
    )
    try:
        parent = _parent_agent(depth=99)  # far beyond max_spawn_depth
        out = json.loads(
            dt.delegate_task(goal="msg", follow_up="sa-deep-1", parent_agent=parent)
        )
        assert out["status"] == "queued"
        # And it bypasses the spawn-pause kill switch as well.
        dt.set_spawn_paused(True)
        try:
            out2 = json.loads(
                dt.delegate_task(goal="msg2", follow_up="sa-deep-1", parent_agent=parent)
            )
            assert out2["status"] == "queued"
        finally:
            dt.set_spawn_paused(False)
    finally:
        dt._unregister_subagent("sa-deep-1")


def test_follow_up_unknown_target_errors():
    import tools.delegate_tool as dt

    out = json.loads(
        dt.delegate_task(goal="hi", follow_up="sa-ghost", parent_agent=_parent_agent())
    )
    assert "error" in out
    assert "not a running subagent" in out["error"]


def test_follow_up_requires_goal():
    import tools.delegate_tool as dt

    out = json.loads(
        dt.delegate_task(follow_up="sa-anything", parent_agent=_parent_agent())
    )
    assert "error" in out


def test_follow_up_tombstoned_child_errors(tmp_path):
    import tools.delegate_tool as dt

    db = _session_db(tmp_path)
    try:
        db.create_session("A", source="cli")
    finally:
        db.close()
    _dispatch_and_retain(parent_session_id="A")
    assert ad.tombstone_retained_child("sa-0-abc12345")
    out = json.loads(
        dt.delegate_task(
            goal="hi", follow_up="sa-0-abc12345", parent_agent=_parent_agent()
        )
    )
    assert "error" in out


def test_follow_up_resumes_completed_child(tmp_path, monkeypatch):
    """Completed retained child: transcript re-opens; follow-up is a NEW user
    turn on the child's own session and the new summary is returned."""
    import tools.delegate_tool as dt

    db = _session_db(tmp_path)
    try:
        db.create_session("A", source="cli")
        db.create_session("child-sess-1", source="delegate", parent_session_id="A")
        db.append_message("child-sess-1", "user", "original goal")
        db.append_message("child-sess-1", "assistant", "original answer")
    finally:
        db.close()
    _dispatch_and_retain(parent_session_id="A")

    captured = {}

    def fake_build(**kwargs):
        captured["build"] = kwargs
        child = MagicMock()
        child.session_id = kwargs.get("resume_session_id") or "child-sess-1"
        return child

    def fake_run(task_index, goal, child, parent_agent, **kw):
        captured["run_goal"] = goal
        captured["resume_history"] = getattr(child, "_delegate_resume_history", None)
        return {
            "task_index": task_index,
            "status": "completed",
            "summary": "follow-up answer",
            "api_calls": 1,
            "duration_seconds": 0.1,
        }

    monkeypatch.setattr(dt, "_build_child_preserving_parent_tools", fake_build)
    monkeypatch.setattr(dt, "_run_single_child", fake_run)
    monkeypatch.setattr(dt, "_finalize_child_results", lambda *a, **k: None)

    parent = _parent_agent()
    from hermes_state import SessionDB

    parent._session_db = SessionDB(Path(tmp_path) / "state.db")
    try:
        out = json.loads(
            dt.delegate_task(
                goal="one more thing",
                follow_up="sa-0-abc12345",
                parent_agent=parent,
            )
        )
    finally:
        parent._session_db.close()

    assert out["mode"] == "follow_up"
    assert out["results"][0]["summary"] == "follow-up answer"
    # The child was rebuilt ON its own persisted session id.
    assert captured["build"]["resume_session_id"] == "child-sess-1"
    # The persisted transcript rides in as the resume history (system rows
    # stripped; dialogue preserved in order).
    roles = [m["role"] for m in captured["resume_history"]]
    assert roles == ["user", "assistant"]
    assert captured["run_goal"].startswith("one more thing")


def test_follow_up_foreign_lineage_rejected(tmp_path, monkeypatch):
    import tools.delegate_tool as dt

    db = _session_db(tmp_path)
    try:
        db.create_session("A", source="cli")
        db.create_session("stranger", source="cli")
        db.create_session("child-sess-1", source="delegate", parent_session_id="A")
        db.append_message("child-sess-1", "user", "g")
        db.append_message("child-sess-1", "assistant", "a")
    finally:
        db.close()
    _dispatch_and_retain(parent_session_id="A")

    parent = _parent_agent()
    parent.session_id = "stranger"
    out = json.loads(
        dt.delegate_task(
            goal="hi", follow_up="sa-0-abc12345", parent_agent=parent
        )
    )
    assert "error" in out
    assert "lineage" in out["error"]


# ---------------------------------------------------------------------------
# Schema surface
# ---------------------------------------------------------------------------


def test_delegate_schema_exposes_follow_up():
    from tools.delegate_tool import DELEGATE_TASK_SCHEMA

    props = DELEGATE_TASK_SCHEMA["parameters"]["properties"]
    assert "follow_up" in props
    assert props["follow_up"]["type"] == "string"


def test_config_defaults_include_retention_knobs():
    from hermes_cli.config_defaults import DEFAULT_CONFIG

    deleg = DEFAULT_CONFIG["delegation"]
    assert deleg["max_retained"] == 10
    assert deleg["retained_ttl_hours"] == 72
