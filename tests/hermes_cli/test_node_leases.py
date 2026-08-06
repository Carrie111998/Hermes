"""POSIX physical-node lease regression tests."""

from hermes_cli import node_leases
from hermes_cli import kanban_db as kb


def _pool(tmp_path, now):
    return node_leases.PosixNodeLeasePool(
        root=tmp_path / "leases",
        now_fn=lambda: now[0],
    )


def test_profiles_on_same_capacity_one_node_cannot_both_acquire(tmp_path):
    now = [100.0]
    first = _pool(tmp_path, now)
    second = _pool(tmp_path, now)
    mapping = {"architect": "m4-pro", "planner": "m4-pro"}
    capacities = {"m4-pro": 1}

    lease = first.try_acquire(
        profile="architect",
        owner="task-a",
        profile_to_node=mapping,
        capacities=capacities,
        ttl_seconds=60,
    )
    blocked = second.try_acquire(
        profile="planner",
        owner="task-b",
        profile_to_node=mapping,
        capacities=capacities,
        ttl_seconds=60,
    )

    assert lease is not None
    assert blocked is None
    assert first.snapshot()["m4-pro"]["in_use"] == 1


def test_profiles_on_different_nodes_can_acquire_concurrently(tmp_path):
    now = [100.0]
    pool = _pool(tmp_path, now)
    mapping = {"architect": "m4-pro", "swe": "rx7900xtx"}
    capacities = {"m4-pro": 1, "rx7900xtx": 1}

    first = pool.try_acquire(
        profile="architect",
        owner="task-a",
        profile_to_node=mapping,
        capacities=capacities,
        ttl_seconds=60,
    )
    second = pool.try_acquire(
        profile="swe",
        owner="task-b",
        profile_to_node=mapping,
        capacities=capacities,
        ttl_seconds=60,
    )

    assert first is not None
    assert second is not None


def test_expired_lease_is_recovered(tmp_path):
    now = [100.0]
    pool = _pool(tmp_path, now)
    mapping = {"architect": "m4-pro", "planner": "m4-pro"}
    capacities = {"m4-pro": 1}

    assert pool.try_acquire(
        profile="architect",
        owner="task-a",
        profile_to_node=mapping,
        capacities=capacities,
        ttl_seconds=10,
    ) is not None

    now[0] = 111.0
    recovered = pool.try_acquire(
        profile="planner",
        owner="task-b",
        profile_to_node=mapping,
        capacities=capacities,
        ttl_seconds=10,
    )

    assert recovered is not None
    assert recovered.node == "m4-pro"
    assert pool.snapshot()["m4-pro"]["owners"] == ["task-b"]


def _fresh_board(tmp_path, monkeypatch):
    home = tmp_path / "hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setenv("HERMES_KANBAN_HOME", str(home))
    monkeypatch.setattr("hermes_cli.profiles.profile_exists", lambda _name: True)
    kb._INITIALIZED_PATHS.clear()
    kb.init_db()
    return home


def test_dispatcher_excludes_profiles_sharing_capacity_one_node(tmp_path, monkeypatch):
    home = _fresh_board(tmp_path, monkeypatch)
    with kb.connect_closing() as conn:
        kb.create_task(conn, title="architecture", assignee="architect")
        kb.create_task(conn, title="planning", assignee="planner")
        result = kb.dispatch_once(
            conn,
            spawn_fn=lambda *_args, **_kwargs: 12345,
            node_leases={
                "enabled": True,
                "profile_to_node": {"architect": "m4-pro", "planner": "m4-pro"},
                "capacities": {"m4-pro": 1},
                "ttl_seconds": 60,
            },
            node_lease_pool=node_leases.PosixNodeLeasePool(
                root=home / "node-leases"
            ),
        )

    assert len(result.spawned) == 1
    assert len(result.skipped_node_capped) == 1
    assert result.skipped_node_capped[0][2] == "m4-pro"


def test_dispatcher_allows_profiles_on_different_nodes(tmp_path, monkeypatch):
    home = _fresh_board(tmp_path, monkeypatch)
    with kb.connect_closing() as conn:
        kb.create_task(conn, title="architecture", assignee="architect")
        kb.create_task(conn, title="implementation", assignee="swe")
        result = kb.dispatch_once(
            conn,
            spawn_fn=lambda *_args, **_kwargs: 12345,
            node_leases={
                "enabled": True,
                "profile_to_node": {"architect": "m4-pro", "swe": "rx7900xtx"},
                "capacities": {"m4-pro": 1, "rx7900xtx": 1},
                "ttl_seconds": 60,
            },
            node_lease_pool=node_leases.PosixNodeLeasePool(
                root=home / "node-leases"
            ),
        )

    assert len(result.spawned) == 2
    assert result.skipped_node_capped == []


def test_review_worker_respects_same_physical_node_capacity(tmp_path, monkeypatch):
    home = _fresh_board(tmp_path, monkeypatch)
    with kb.connect_closing() as conn:
        kb.create_task(conn, title="active planning", assignee="planner")
        review_id = kb.create_task(conn, title="architecture review", assignee="architect")
        with kb.write_txn(conn):
            conn.execute("UPDATE tasks SET status = 'review' WHERE id = ?", (review_id,))
        result = kb.dispatch_once(
            conn,
            spawn_fn=lambda *_args, **_kwargs: 12345,
            node_leases={
                "enabled": True,
                "profile_to_node": {"architect": "m4-pro", "planner": "m4-pro"},
                "capacities": {"m4-pro": 1},
                "ttl_seconds": 60,
            },
            node_lease_pool=node_leases.PosixNodeLeasePool(
                root=home / "node-leases"
            ),
        )

    assert len(result.spawned) == 1
    assert result.skipped_node_capped == [(review_id, "architect", "m4-pro")]
