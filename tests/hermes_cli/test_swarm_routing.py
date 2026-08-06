"""hermes-v2 H-41 — ``/swarm`` v1-vs-kanban routing + materialization.

Covers the four acceptance angles from the H-41 spec:

  (a) routing decision v1 vs kanban across the worker-count / runtime
      threshold;
  (b) materialization on a temp *dispatchable* board with the correct
      root → worker → verifier → synthesizer chain and the H-43 /
      worker-failure-discipline body wiring;
  (c) fail-safe fallback to v1 when the board is missing or not
      dispatchable — and the P-71 invariant that an existing board is
      never auto-flipped;
  (d) no live spawn: everything happens at the DB level; no gateway
      dispatcher / subprocess is ever invoked.
"""
from __future__ import annotations

import sys
import tempfile

import pytest

from hermes_cli.kanban_swarm import SwarmWorkerSpec
from hermes_cli import swarm_routing as sr


# --------------------------------------------------------------------------
# (a) Routing decision — pure, no DB.
# --------------------------------------------------------------------------

def test_decision_below_threshold_is_v1_default_config():
    d = sr.decide_swarm_route(worker_count=3)
    assert d.mode == "v1"
    assert d.min_workers == sr.DEFAULT_KANBAN_BACKED_MIN_WORKERS  # 4


def test_decision_at_threshold_is_kanban_default_config():
    d = sr.decide_swarm_route(worker_count=4)
    assert d.mode == "kanban"
    assert "4 >= kanban_backed_min_workers 4" in d.reason


def test_decision_respects_custom_min_workers():
    cfg = {"kanban_backed_min_workers": 6}
    assert sr.decide_swarm_route(worker_count=5, config=cfg).mode == "v1"
    assert sr.decide_swarm_route(worker_count=6, config=cfg).mode == "kanban"


def test_decision_runtime_threshold_promotes_small_batch():
    cfg = {"kanban_backed_min_workers": 4, "kanban_backed_min_runtime_seconds": 600}
    # 2 workers is below the count threshold ...
    assert sr.decide_swarm_route(worker_count=2, config=cfg).mode == "v1"
    # ... but a long expected runtime promotes it to kanban.
    d = sr.decide_swarm_route(worker_count=2, config=cfg, expected_runtime_seconds=900)
    assert d.mode == "kanban"
    assert "expected_runtime 900s" in d.reason


def test_decision_runtime_threshold_disabled_by_default():
    # No runtime threshold configured → long runtime alone never promotes.
    d = sr.decide_swarm_route(worker_count=2, expected_runtime_seconds=99999)
    assert d.mode == "v1"


def test_config_zero_min_workers_clamps_to_default_fail_safe():
    # A fat-fingered 0 must not disable the v1 path (would route everything
    # to kanban). It clamps back to the default floor.
    cfg = sr.SwarmRoutingConfig.from_mapping({"kanban_backed_min_workers": 0})
    assert cfg.min_workers == sr.DEFAULT_KANBAN_BACKED_MIN_WORKERS


def test_config_garbage_values_fall_back_to_defaults():
    cfg = sr.SwarmRoutingConfig.from_mapping(
        {"kanban_backed_min_workers": "abc", "kanban_board": "  "}
    )
    assert cfg.min_workers == sr.DEFAULT_KANBAN_BACKED_MIN_WORKERS
    assert cfg.board == sr.DEFAULT_SWARM_BOARD


# --------------------------------------------------------------------------
# Isolated HERMES_HOME fixture (mirrors test_kanban_board_dispatchable).
# --------------------------------------------------------------------------

@pytest.fixture()
def isolated_kanban_home(monkeypatch):
    test_home = tempfile.mkdtemp(prefix="swarm_routing_test_")
    monkeypatch.setenv("HERMES_HOME", test_home)
    for mod in list(sys.modules.keys()):
        if (
            mod.startswith("hermes_cli")
            or mod.startswith("hermes_state")
            or mod == "hermes_constants"
        ):
            del sys.modules[mod]
    from hermes_cli import kanban_db  # fresh import bound to this HERMES_HOME
    from hermes_cli import swarm_routing  # re-bound to the fresh kanban_db

    yield swarm_routing, kanban_db, test_home


def _mk_workers(n: int) -> list[SwarmWorkerSpec]:
    return [
        SwarmWorkerSpec(profile=f"worker-{i}", title=f"Task {i}", body=f"Do part {i}")
        for i in range(1, n + 1)
    ]


# --------------------------------------------------------------------------
# (b) Materialization on a dispatchable board.
# --------------------------------------------------------------------------

def test_materialize_builds_full_chain_on_dispatchable_board(isolated_kanban_home):
    srm, kb, _home = isolated_kanban_home
    kb.create_board(slug="swarm-work", name="Swarm Work")
    kb.write_board_metadata("swarm-work", dispatchable=True)

    result = srm.route_and_materialize_swarm(
        goal="Build feature X with proper review.",
        workers=_mk_workers(4),
        config={"kanban_board": "swarm-work"},
    )

    assert result.mode == "kanban"
    assert result.board == "swarm-work"
    assert result.board_created is False  # board pre-existed; we didn't create it
    created = result.created
    assert created is not None
    assert len(created.worker_ids) == 4

    # Inspect the graph in the swarm-work board DB.
    with kb.connect_closing(board="swarm-work") as conn:
        root = kb.get_task(conn, created.root_id)
        workers = [kb.get_task(conn, t) for t in created.worker_ids]
        verifier = kb.get_task(conn, created.verifier_id)
        synthesizer = kb.get_task(conn, created.synthesizer_id)

        assert root.status == "done"
        assert all(w.status == "ready" for w in workers)
        assert verifier.status == "todo"
        assert synthesizer.status == "todo"
        assert set(kb.parent_ids(conn, created.verifier_id)) == set(created.worker_ids)
        assert kb.parent_ids(conn, created.synthesizer_id) == [created.verifier_id]

        # H-43 worker contract is embedded in each worker body.
        for w in workers:
            assert "H-43 workspace contract" in (w.body or "")
            assert "output/result.json" in (w.body or "")
        # worker-failure-discipline gate is on the verifier (body + skill).
        assert "VERDICT: APPROVE" in (verifier.body or "")
        assert "worker-failure-discipline" in (verifier.skills or [])


def test_materialize_autocreates_and_opts_in_fresh_board(isolated_kanban_home):
    srm, kb, _home = isolated_kanban_home
    # Board does NOT exist yet; autocreate is opted in via config.
    assert kb.board_is_dispatchable("swarm-work") is False

    result = srm.route_and_materialize_swarm(
        goal="Long crash-recoverable job.",
        workers=_mk_workers(5),
        config={"kanban_board": "swarm-work", "kanban_board_autocreate": True},
    )

    assert result.mode == "kanban"
    assert result.board_created is True
    # The feature freshly created the board from the config slug → opt-in OK.
    assert kb.board_is_dispatchable("swarm-work") is True


# --------------------------------------------------------------------------
# (c) Fail-safe fallback + P-71 "never flip an existing board".
# --------------------------------------------------------------------------

def test_fallback_when_board_missing_and_autocreate_off(isolated_kanban_home):
    srm, kb, _home = isolated_kanban_home
    result = srm.route_and_materialize_swarm(
        goal="g",
        workers=_mk_workers(4),  # enough workers to WANT kanban
        config={"kanban_board": "swarm-work"},  # autocreate defaults off
    )
    assert result.mode == "v1"
    assert "does not exist" in result.reason
    # Fail-safe: no board was created, nothing opted in.
    assert kb.board_is_dispatchable("swarm-work") is False


def test_fallback_when_board_exists_but_not_dispatchable_never_flips(isolated_kanban_home):
    srm, kb, _home = isolated_kanban_home
    kb.create_board(slug="swarm-work", name="Swarm Work")  # exists, NOT dispatchable

    result = srm.route_and_materialize_swarm(
        goal="g",
        workers=_mk_workers(4),
        config={"kanban_board": "swarm-work", "kanban_board_autocreate": True},
    )
    assert result.mode == "v1"
    assert "not dispatchable" in result.reason
    # P-71 invariant: an EXISTING board is never auto-flipped, even with
    # autocreate on.
    assert kb.board_is_dispatchable("swarm-work") is False


def test_resolve_board_reports_created_flag_only_on_fresh_optin(isolated_kanban_home):
    srm, kb, _home = isolated_kanban_home
    res = srm.resolve_dispatch_board(
        {"kanban_board": "swarm-work", "kanban_board_autocreate": True}
    )
    assert res.ok is True and res.created is True
    # Second resolve now finds it already-dispatchable → created False.
    res2 = srm.resolve_dispatch_board({"kanban_board": "swarm-work"})
    assert res2.ok is True and res2.created is False


# --------------------------------------------------------------------------
# (d) No live spawn — the fallback path materialises nothing.
# --------------------------------------------------------------------------

def test_below_threshold_creates_no_tasks(isolated_kanban_home):
    srm, kb, _home = isolated_kanban_home
    kb.create_board(slug="swarm-work", name="Swarm Work")
    kb.write_board_metadata("swarm-work", dispatchable=True)

    result = srm.route_and_materialize_swarm(
        goal="small job",
        workers=_mk_workers(2),  # below default threshold
        config={"kanban_board": "swarm-work"},
    )
    assert result.mode == "v1"
    assert result.created is None
    # The dispatchable board's DB has no tasks — v1 path never touched it.
    with kb.connect_closing(board="swarm-work") as conn:
        n = conn.execute("SELECT COUNT(*) AS c FROM tasks").fetchone()["c"]
    assert n == 0


def test_worker_specs_from_delegate_tasks_mapping():
    tasks = [
        {"goal": "scan A", "context": "ctx-a", "role": "scout", "skills": ["x"]},
        {"goal": "scan B"},
        {"goal": "   "},  # dropped (empty goal)
    ]
    specs = sr.worker_specs_from_delegate_tasks(tasks, default_profile="worker")
    assert len(specs) == 2
    assert specs[0].profile == "scout" and specs[0].skills == ["x"]
    assert "ctx-a" in specs[0].body
    assert specs[1].profile == "worker"  # default when role omitted
