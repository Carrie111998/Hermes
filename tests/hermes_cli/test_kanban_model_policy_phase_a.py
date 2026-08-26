"""Focused Phase A model-routing policy contracts (written before implementation)."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli.kanban_model_policy import ModelRoutingPolicy, PolicyError


@pytest.fixture
def routed_conn(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    (home / "config.yaml").write_text(
        """
model_routing:
  enabled: true
  tiers:
    T1:
      - {provider: nous, model: small}
    T4:
      - {provider: openai, model: large}
  classification:
    P0: {tags: [deterministic]}
    P1: {tier: T1}
    P4: {tier: T4}
""".strip()
    )
    kb.init_db()
    conn = kb.connect()
    yield conn
    conn.close()


def _events(conn, task_id):
    return [(e.kind, e.payload) for e in kb.list_events(conn, task_id)]


def test_policy_fails_closed_and_resolves_only_approved_candidates(routed_conn):
    policy = ModelRoutingPolicy.load()
    assert policy.resolve(priority="P1") == {"priority": "P1", "tier": "T1", "provider": "nous", "model": "small"}
    with pytest.raises(PolicyError):
        policy.validate_override("evil", "unapproved")


def test_schema_has_route_snapshot_and_future_accounting_fields(routed_conn):
    task_cols = {r["name"] for r in routed_conn.execute("PRAGMA table_info(tasks)")}
    run_cols = {r["name"] for r in routed_conn.execute("PRAGMA table_info(task_runs)")}
    assert {"routing_priority", "routing_tier", "route_snapshot"} <= task_cols
    assert {"routing_priority", "routing_tier", "route_snapshot", "input_tokens", "output_tokens", "cost_usd"} <= run_cols


def test_p0_is_deterministically_routed_without_llm_claim(routed_conn):
    task_id = kb.create_task(routed_conn, title="cleanup", assignee="worker", skills=["deterministic"])
    assert kb.claim_task(routed_conn, task_id) is None
    task = kb.get_task(routed_conn, task_id)
    assert task.status == "blocked"
    assert any(kind == "deterministic_routed" for kind, _ in _events(routed_conn, task_id))


def test_p4_without_t4_blocks_without_lower_tier_fallback(routed_conn):
    # Persist a valid policy but remove T4 to simulate unavailable resource.
    Path(__import__("os").environ["HERMES_HOME"], "config.yaml").write_text(
        "model_routing:\n  enabled: true\n  tiers: {T1: [{provider: nous, model: small}]}\n  classification: {P4: {tier: T4}}\n"
    )
    task_id = kb.create_task(routed_conn, title="hard", assignee="worker")
    routed_conn.execute("UPDATE tasks SET routing_priority='P4' WHERE id=?", (task_id,))
    assert kb.claim_task(routed_conn, task_id) is None
    assert kb.get_task(routed_conn, task_id).status == "blocked"
    assert any(kind == "model_resource_blocked" for kind, _ in _events(routed_conn, task_id))


def test_override_is_validated_and_audited(routed_conn):
    task_id = kb.create_task(routed_conn, title="t", assignee="worker")
    assert kb.set_model_override(routed_conn, task_id, "small", provider="nous")
    with pytest.raises(ValueError):
        kb.set_model_override(routed_conn, task_id, "large", provider="nous")
    events = _events(routed_conn, task_id)
    assert any(kind == "model_override_set" for kind, _ in events)
    assert any(kind == "model_override_rejected" for kind, _ in events)


def test_create_task_rejects_unauthorized_override_before_persisting(routed_conn, caplog):
    with pytest.raises(ValueError, match="routing policy rejected model override"):
        kb.create_task(
            routed_conn,
            title="unauthorized",
            assignee="worker",
            model_override="unapproved",
            provider_override="evil",
        )
    assert routed_conn.execute("SELECT COUNT(*) FROM tasks WHERE title='unauthorized'").fetchone()[0] == 0
    assert "model_override_rejected at task creation" in caplog.text


def test_create_task_override_rejects_when_registry_is_missing(routed_conn, monkeypatch, tmp_path):
    missing_home = tmp_path / "no-registry"
    missing_home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(missing_home))
    with pytest.raises(ValueError, match="model_routing config unavailable"):
        kb.create_task(
            routed_conn,
            title="missing registry",
            assignee="worker",
            model_override="small",
            provider_override="nous",
        )


def test_override_invalidates_prior_route_snapshot(routed_conn):
    task_id = kb.create_task(routed_conn, title="t", assignee="worker")
    claimed = kb.claim_task(routed_conn, task_id)
    assert claimed is not None
    assert claimed.route_snapshot is not None
    run = routed_conn.execute(
        "SELECT route_snapshot FROM task_runs WHERE task_id=? ORDER BY id DESC LIMIT 1", (task_id,)
    ).fetchone()
    assert run["route_snapshot"] == claimed.route_snapshot
    # Pretend the first worker completed; the next route must be freshly
    # authorized, rather than reusing a snapshot from before the override.
    routed_conn.execute("UPDATE tasks SET status='ready', claim_lock=NULL WHERE id=?", (task_id,))
    assert kb.set_model_override(routed_conn, task_id, "small", provider="nous")
    row = routed_conn.execute("SELECT route_snapshot FROM tasks WHERE id=?", (task_id,)).fetchone()
    assert row["route_snapshot"] is None


def test_spawn_rejects_malicious_persisted_override(routed_conn, monkeypatch, tmp_path):
    task_id = kb.create_task(routed_conn, title="t", assignee="worker")
    routed_conn.execute("UPDATE tasks SET model_override='malicious', provider_override='evil' WHERE id=?", (task_id,))
    task = kb.get_task(routed_conn, task_id)
    monkeypatch.setattr(kb.subprocess, "Popen", lambda *_a, **_kw: pytest.fail("must not spawn"))
    with pytest.raises(ValueError, match="routing policy"):
        kb._default_spawn(task, str(tmp_path))


def test_spawn_requires_a_claim_snapshot(routed_conn, monkeypatch, tmp_path):
    task_id = kb.create_task(routed_conn, title="unclaimed", assignee="worker")
    task = kb.get_task(routed_conn, task_id)
    assert task is not None
    monkeypatch.setattr(kb.subprocess, "Popen", lambda *_a, **_kw: pytest.fail("must not spawn"))
    with pytest.raises(ValueError, match="persisted route snapshot"):
        kb._default_spawn(task, str(tmp_path))


def test_p0_cannot_spawn_an_approved_persisted_model(routed_conn, monkeypatch, tmp_path):
    task_id = kb.create_task(routed_conn, title="cleanup", assignee="worker", skills=["deterministic"])
    routed_conn.execute(
        "UPDATE tasks SET model_override='small', provider_override='nous', "
        "route_snapshot=? WHERE id=?",
        (json.dumps({"priority": "P0", "tier": "T1", "provider": "nous", "model": "small"}, sort_keys=True), task_id),
    )
    task = kb.get_task(routed_conn, task_id)
    assert task is not None
    monkeypatch.setattr(kb.subprocess, "Popen", lambda *_a, **_kw: pytest.fail("must not spawn"))
    with pytest.raises(ValueError, match="P0"):
        kb._default_spawn(task, str(tmp_path))
