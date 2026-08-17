from __future__ import annotations

import json
from pathlib import Path

import pytest
import yaml

from hermes_cli import kanban_db as kb


@pytest.fixture
def kanban_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    for name in (
        "HERMES_KANBAN_HOME",
        "HERMES_KANBAN_DB",
        "HERMES_KANBAN_BOARD",
    ):
        monkeypatch.delenv(name, raising=False)
    kb.init_db(board="default")
    return home


def _set_machine_allowed(home: Path, value: object) -> None:
    (home / "config.yaml").write_text(
        yaml.safe_dump({"kanban": {"allowed_profiles": value}}),
        encoding="utf-8",
    )


def _set_board_allowed(board: str, value: object) -> None:
    kb.write_board_metadata(board, allowed_profiles=value)


def _set_board_allowed_raw(board: str, value: object) -> None:
    """Simulate malformed metadata written by hand outside the production API."""
    path = kb.board_metadata_path(board)
    payload: dict[str, object] = {}
    if path.is_file():
        loaded = json.loads(path.read_text(encoding="utf-8"))
        if isinstance(loaded, dict):
            payload.update(loaded)
    payload["allowed_profiles"] = value
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(payload, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )


def _install_profile(home: Path, name: str) -> None:
    profile_dir = home / "profiles" / name
    profile_dir.mkdir(parents=True, exist_ok=True)
    (profile_dir / "config.yaml").write_text("model: {}\n", encoding="utf-8")


def _move_to_review(conn, task_id: str) -> None:
    conn.execute("UPDATE tasks SET status = 'review' WHERE id = ?", (task_id,))
    conn.commit()


def _task_mutation_snapshot(conn, task_id: str) -> dict[str, object]:
    task_row = conn.execute(
        "SELECT * FROM tasks WHERE id = ?", (task_id,)
    ).fetchone()
    assert task_row is not None
    current_run_id = task_row["current_run_id"]
    current_run_row = (
        conn.execute(
            "SELECT * FROM task_runs WHERE id = ?", (current_run_id,)
        ).fetchone()
        if current_run_id is not None
        else None
    )
    return {
        "database_bytes": conn.serialize(),
        "task": dict(task_row),
        "current_run": dict(current_run_row) if current_run_row is not None else None,
        "runs": [
            dict(row)
            for row in conn.execute(
                "SELECT * FROM task_runs WHERE task_id = ? ORDER BY id", (task_id,)
            )
        ],
        "events": [
            dict(row)
            for row in conn.execute(
                "SELECT * FROM task_events WHERE task_id = ? ORDER BY id", (task_id,)
            )
        ],
        "comments": [
            dict(row)
            for row in conn.execute(
                "SELECT * FROM task_comments WHERE task_id = ? ORDER BY id", (task_id,)
            )
        ],
    }


def test_board_metadata_profile_policy_roundtrips_without_losing_other_fields(
    kanban_home: Path,
) -> None:
    metadata = kb.create_board(
        "policy-board",
        name="Policy Board",
        description="Keeps its descriptive metadata",
    )

    assert metadata["allowed_profiles"] is None
    assert kb.read_board_metadata("policy-board")["allowed_profiles"] is None

    for allowed_profiles in (["alpha", "beta"], []):
        written = kb.write_board_metadata(
            "policy-board",
            allowed_profiles=allowed_profiles,
        )
        reloaded = kb.read_board_metadata("policy-board")

        assert written["allowed_profiles"] == allowed_profiles
        assert reloaded["allowed_profiles"] == allowed_profiles
        assert reloaded["name"] == "Policy Board"
        assert reloaded["description"] == "Keeps its descriptive metadata"


def test_write_board_metadata_allowed_profiles_uses_three_way_sentinel_and_normalizes(
    kanban_home: Path,
) -> None:
    kb.create_board(
        "sentinel-policy",
        name="Sentinel Policy",
        description="Original description",
    )

    explicit = kb.write_board_metadata(
        "sentinel-policy",
        allowed_profiles=["alpha", "beta"],
    )
    assert explicit["allowed_profiles"] == ["alpha", "beta"]

    preserved = kb.write_board_metadata(
        "sentinel-policy",
        description="Updated without changing policy",
    )
    assert preserved["allowed_profiles"] == ["alpha", "beta"]
    assert preserved["description"] == "Updated without changing policy"
    assert kb.read_board_metadata("sentinel-policy")["allowed_profiles"] == [
        "alpha",
        "beta",
    ]

    normalized = kb.write_board_metadata(
        "sentinel-policy",
        allowed_profiles=[" Alpha ", "beta", "ALPHA", "Beta"],
    )
    assert normalized["allowed_profiles"] == ["alpha", "beta"]
    assert kb.read_board_metadata("sentinel-policy")["allowed_profiles"] == [
        "alpha",
        "beta",
    ]

    cleared = kb.write_board_metadata(
        "sentinel-policy",
        allowed_profiles=None,
    )
    assert cleared["allowed_profiles"] is None
    assert kb.read_board_metadata("sentinel-policy")["allowed_profiles"] is None


@pytest.mark.parametrize(
    "invalid_allowed_profiles",
    [
        pytest.param(["alpha", 7], id="non-string-entry"),
        pytest.param(["alpha", "../escape"], id="invalid-profile-name"),
    ],
)
def test_write_board_metadata_rejects_invalid_allowed_profiles_before_persisting(
    kanban_home: Path,
    invalid_allowed_profiles: object,
) -> None:
    kb.create_board(
        "validation-policy",
        allowed_profiles=["stable-profile"],
    )
    metadata_path = kb.board_metadata_path("validation-policy")
    before = metadata_path.read_bytes()

    with pytest.raises(ValueError):
        kb.write_board_metadata(
            "validation-policy",
            allowed_profiles=invalid_allowed_profiles,
        )

    assert metadata_path.read_bytes() == before
    assert kb.read_board_metadata("validation-policy")["allowed_profiles"] == [
        "stable-profile"
    ]


@pytest.mark.parametrize(
    "original_bytes",
    [
        pytest.param(
            b'{"allowed_profiles": ["stable-profile"]',
            id="malformed-json",
        ),
        pytest.param(b'["stable-profile"]\n', id="non-mapping-json"),
    ],
)
def test_write_board_metadata_refuses_malformed_existing_metadata(
    kanban_home: Path,
    original_bytes: bytes,
) -> None:
    metadata_path = kb.board_metadata_path("malformed-metadata")
    metadata_path.parent.mkdir(parents=True, exist_ok=True)
    metadata_path.write_bytes(original_bytes)
    before = metadata_path.read_bytes()

    assert kb.kanban_allowed_profiles(board="malformed-metadata") == frozenset()

    with pytest.raises(ValueError, match="refusing to update board metadata"):
        kb.write_board_metadata(
            "malformed-metadata",
            description="must not overwrite recoverable bytes",
        )

    assert metadata_path.read_bytes() == before == original_bytes
    assert kb.kanban_allowed_profiles(board="malformed-metadata") == frozenset()


def test_write_board_metadata_atomic_replace_failure_preserves_existing_file(
    kanban_home: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import utils

    kb.create_board(
        "atomic-policy",
        description="original description",
        allowed_profiles=["stable-profile"],
    )
    metadata_path = kb.board_metadata_path("atomic-policy")
    before = metadata_path.read_bytes()
    before_entries = set(metadata_path.parent.iterdir())

    def refuse_replace(*_args: object, **_kwargs: object) -> None:
        raise OSError("simulated atomic replace failure")

    monkeypatch.setattr(utils, "atomic_replace", refuse_replace)

    with pytest.raises(OSError, match="simulated atomic replace failure"):
        kb.write_board_metadata(
            "atomic-policy",
            description="unrelated metadata update",
        )

    assert metadata_path.read_bytes() == before
    assert kb.kanban_allowed_profiles(board="atomic-policy") == frozenset(
        {"stable-profile"}
    )
    assert set(metadata_path.parent.iterdir()) == before_entries


def test_create_board_omitted_allowed_profiles_preserves_existing_policy(
    kanban_home: Path,
) -> None:
    created = kb.create_board(
        "idempotent-policy",
        name="Idempotent Policy",
        allowed_profiles=["alpha", "beta"],
    )
    assert created["allowed_profiles"] == ["alpha", "beta"]

    recreated = kb.create_board("idempotent-policy")

    assert recreated["allowed_profiles"] == ["alpha", "beta"]
    assert kb.read_board_metadata("idempotent-policy")["allowed_profiles"] == [
        "alpha",
        "beta",
    ]


def test_allowed_profiles_prefers_explicit_board_then_connection_then_global(
    kanban_home: Path,
) -> None:
    kb.create_board("precedence-a", allowed_profiles=["alpha"])
    kb.create_board("precedence-b", allowed_profiles=["beta"])

    with kb.connect_closing(board="precedence-a") as conn:
        kb.set_current_board("precedence-b")
        assert kb.get_current_board() == "precedence-b"
        assert kb.kanban_allowed_profiles(conn=conn) == frozenset({"alpha"})
        assert kb.kanban_allowed_profiles(
            board="precedence-b",
            conn=conn,
        ) == frozenset({"beta"})


def test_create_task_rejects_explicit_board_mismatch_without_mutation(
    kanban_home: Path,
) -> None:
    kb.create_board("frontend-policy", allowed_profiles=["frontend-builder"])
    kb.create_board("systems-policy", allowed_profiles=["systems-builder"])

    with kb.connect_closing(board="frontend-policy") as conn:
        before = conn.serialize()
        with pytest.raises(ValueError, match="does not match connection board"):
            kb.create_task(
                conn,
                title="systems policy must not authorize frontend DB writes",
                assignee="systems-builder",
                board="systems-policy",
            )

        assert conn.serialize() == before
        assert kb.list_tasks(conn) == []


def test_create_task_accepts_matching_explicit_connection_board(
    kanban_home: Path,
) -> None:
    kb.create_board("matching-policy", allowed_profiles=["frontend-builder"])

    with kb.connect_closing(board="matching-policy") as conn:
        task_id = kb.create_task(
            conn,
            title="matching explicit board",
            assignee="Frontend-Builder",
            board="matching-policy",
        )

        task = kb.get_task(conn, task_id)
        assert task is not None
        assert task.assignee == "frontend-builder"


def test_db_override_connection_keeps_frozen_opening_board_context(
    kanban_home: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    kb.create_board("env-policy", allowed_profiles=["from-env"])
    kb.create_board("changed-env-policy", allowed_profiles=["from-changed-env"])
    kb.create_board("explicit-policy", allowed_profiles=["from-explicit"])
    custom_db = kanban_home.parent / "outside-canonical-tree" / "forced-kanban.db"
    monkeypatch.setenv("HERMES_KANBAN_DB", str(custom_db))
    monkeypatch.setenv("HERMES_KANBAN_BOARD", "env-policy")

    conn = kb.connect()
    try:
        db_row = next(
            row for row in conn.execute("PRAGMA database_list") if row[1] == "main"
        )
        assert Path(str(db_row[2])).resolve() == custom_db.resolve()
        assert kb.kanban_allowed_profiles(conn=conn) == frozenset({"from-env"})

        monkeypatch.setenv("HERMES_KANBAN_BOARD", "changed-env-policy")
        assert kb.kanban_allowed_profiles(conn=conn) == frozenset({"from-env"})
        assert kb.kanban_allowed_profiles(
            board="changed-env-policy", conn=conn
        ) == frozenset({"from-changed-env"})

        env_task_id = kb.create_task(
            conn,
            title="frozen env board",
            assignee="from-env",
            board="env-policy",
        )
        before_mismatch = conn.serialize()
        with pytest.raises(ValueError, match="does not match connection board"):
            kb.create_task(
                conn,
                title="changed env must not retag an open connection",
                assignee="from-changed-env",
                board="changed-env-policy",
            )
        assert conn.serialize() == before_mismatch
        assert {task.id for task in kb.list_tasks(conn)} == {env_task_id}

        with pytest.raises(ValueError, match="does not match connection board"):
            kb.dispatch_once(conn, board="changed-env-policy", dry_run=True)
        assert conn.serialize() == before_mismatch

        explicit_conn = kb.connect(board="explicit-policy")
        try:
            assert kb.kanban_allowed_profiles(conn=explicit_conn) == frozenset(
                {"from-explicit"}
            )
            assert kb.kanban_allowed_profiles(conn=conn) == frozenset({"from-env"})
            explicit_task_id = kb.create_task(
                explicit_conn,
                title="frozen explicit board",
                assignee="from-explicit",
                board="explicit-policy",
            )
            before_explicit_mismatch = explicit_conn.serialize()
            with pytest.raises(ValueError, match="does not match connection board"):
                kb.create_task(
                    explicit_conn,
                    title="env hint must not replace explicit opening hint",
                    assignee="from-env",
                    board="env-policy",
                )
            assert explicit_conn.serialize() == before_explicit_mismatch
            assert {task.id for task in kb.list_tasks(explicit_conn)} == {
                env_task_id,
                explicit_task_id,
            }
        finally:
            explicit_conn.close()
    finally:
        conn.close()


def test_canonical_db_path_identity_overrides_conflicting_connection_hint(
    kanban_home: Path,
) -> None:
    kb.create_board("path-identity", allowed_profiles=["path-profile"])
    kb.create_board("conflicting-hint", allowed_profiles=["hint-profile"])

    conn = kb.connect(
        db_path=kb.kanban_db_path("path-identity"),
        board="conflicting-hint",
    )
    try:
        assert kb.kanban_allowed_profiles(conn=conn) == frozenset({"path-profile"})
        before = conn.serialize()
        with pytest.raises(ValueError, match="does not match connection board"):
            kb.create_task(
                conn,
                title="hint must not retag canonical path",
                assignee="hint-profile",
                board="conflicting-hint",
            )
        assert conn.serialize() == before

        task_id = kb.create_task(
            conn,
            title="canonical path board remains authoritative",
            assignee="path-profile",
            board="path-identity",
        )
        assert kb.get_task(conn, task_id) is not None
    finally:
        conn.close()


def test_unidentified_custom_connection_accepts_explicit_board_context(
    kanban_home: Path,
) -> None:
    kb.create_board("compat-policy", allowed_profiles=["compat-profile"])
    custom_db = kanban_home.parent / "test-double" / "kanban.db"

    with kb.connect_closing(db_path=custom_db) as conn:
        task_id = kb.create_task(
            conn,
            title="explicit compatibility context",
            assignee="compat-profile",
            board="compat-policy",
        )

        task = kb.get_task(conn, task_id)
        assert task is not None
        assert task.assignee == "compat-profile"


@pytest.mark.parametrize(
    ("machine_allowed", "board_allowed", "expected"),
    [
        pytest.param(None, None, None, id="machine-null-board-null-unrestricted"),
        pytest.param(
            ["alpha", "beta"],
            None,
            frozenset({"alpha", "beta"}),
            id="machine-list-board-null-machine-ceiling",
        ),
        pytest.param(
            None,
            ["beta", "gamma"],
            frozenset({"beta", "gamma"}),
            id="machine-null-board-list-board-set",
        ),
        pytest.param(
            ["alpha", "beta"],
            ["beta", "gamma"],
            frozenset({"beta"}),
            id="two-lists-intersect",
        ),
        pytest.param(
            None,
            [],
            frozenset(),
            id="explicit-empty-board-allows-none",
        ),
        pytest.param(
            [],
            None,
            frozenset(),
            id="explicit-empty-machine-allows-none",
        ),
    ],
)
def test_effective_allowed_profiles_follows_two_layer_matrix(
    kanban_home: Path,
    machine_allowed: object,
    board_allowed: object,
    expected: frozenset[str] | None,
) -> None:
    kb.create_board("matrix-board")
    _set_machine_allowed(kanban_home, machine_allowed)
    _set_board_allowed("matrix-board", board_allowed)

    with kb.connect_closing(board="matrix-board") as conn:
        assert kb.kanban_allowed_profiles(conn=conn) == expected
        for profile in ("alpha", "beta", "gamma", "outside"):
            assert kb.is_kanban_profile_allowed(profile, conn=conn) is (
                expected is None or profile in expected
            )


def test_two_boards_use_disjoint_connection_scoped_policies(
    kanban_home: Path,
) -> None:
    _set_machine_allowed(kanban_home, ["alpha", "beta"])
    kb.create_board("alpha-board")
    kb.create_board("beta-board")
    _set_board_allowed("alpha-board", ["alpha"])
    _set_board_allowed("beta-board", ["beta"])

    with (
        kb.connect_closing(board="alpha-board") as alpha_conn,
        kb.connect_closing(board="beta-board") as beta_conn,
    ):
        # Deliberately point process-global state at the opposite board. Policy
        # identity must come from each SQLite connection, not this pointer.
        kb.set_current_board("beta-board")
        assert kb.kanban_allowed_profiles(conn=alpha_conn) == frozenset({"alpha"})
        assert kb.is_kanban_profile_allowed("alpha", conn=alpha_conn)
        assert not kb.is_kanban_profile_allowed("beta", conn=alpha_conn)
        alpha_id = kb.create_task(alpha_conn, title="alpha work", assignee="alpha")
        with pytest.raises(ValueError, match="not allowed for Kanban assignment"):
            kb.create_task(alpha_conn, title="wrong alpha work", assignee="beta")

        kb.set_current_board("alpha-board")
        assert kb.kanban_allowed_profiles(conn=beta_conn) == frozenset({"beta"})
        assert kb.is_kanban_profile_allowed("beta", conn=beta_conn)
        assert not kb.is_kanban_profile_allowed("alpha", conn=beta_conn)
        beta_id = kb.create_task(beta_conn, title="beta work", assignee="beta")
        with pytest.raises(ValueError, match="not allowed for Kanban assignment"):
            kb.create_task(beta_conn, title="wrong beta work", assignee="alpha")

        alpha_task = kb.get_task(alpha_conn, alpha_id)
        beta_task = kb.get_task(beta_conn, beta_id)
        assert alpha_task is not None
        assert beta_task is not None
        assert alpha_task.assignee == "alpha"
        assert beta_task.assignee == "beta"


def test_create_and_reassign_enforce_effective_board_policy(
    kanban_home: Path,
) -> None:
    _set_machine_allowed(
        kanban_home,
        ["frontend-builder", "systems-builder"],
    )
    kb.create_board("frontend")
    _set_board_allowed("frontend", ["frontend-builder"])

    with kb.connect_closing(board="frontend") as conn:
        task_id = kb.create_task(
            conn,
            title="allowed work",
            assignee="Frontend-Builder",
        )
        task = kb.get_task(conn, task_id)
        assert task is not None
        assert task.assignee == "frontend-builder"

        with pytest.raises(ValueError, match="not allowed for Kanban assignment"):
            kb.create_task(conn, title="blocked work", assignee="systems-builder")

        with pytest.raises(ValueError, match="not allowed for Kanban assignment"):
            kb.assign_task(conn, task_id, "systems-builder")
        unchanged = kb.get_task(conn, task_id)
        assert unchanged is not None
        assert unchanged.assignee == "frontend-builder"

        unassigned_id = kb.create_task(conn, title="route later")
        with pytest.raises(ValueError, match="not allowed for Kanban assignment"):
            kb.assign_task(conn, unassigned_id, "systems-builder")
        unassigned = kb.get_task(conn, unassigned_id)
        assert unassigned is not None
        assert unassigned.assignee is None

        assert kb.assign_task(conn, unassigned_id, "frontend-builder")
        assigned = kb.get_task(conn, unassigned_id)
        assert assigned is not None
        assert assigned.assignee == "frontend-builder"
        assert kb.assign_task(conn, unassigned_id, None)
        cleared = kb.get_task(conn, unassigned_id)
        assert cleared is not None
        assert cleared.assignee is None


def test_reassign_reclaim_rejects_disallowed_profile_without_any_mutation(
    kanban_home: Path,
) -> None:
    kb.create_board("reassign-policy", allowed_profiles=["builder"])

    with kb.connect_closing(board="reassign-policy") as conn:
        task_id = kb.create_task(conn, title="claimed work", assignee="builder")
        claimed = kb.claim_task(conn, task_id)
        assert claimed is not None
        assert claimed.claim_lock is not None
        assert claimed.current_run_id is not None
        kb.add_comment(conn, task_id, "operator", "Preserve this comment and run.")
        before = _task_mutation_snapshot(conn, task_id)

        with pytest.raises(ValueError, match="not allowed for Kanban assignment"):
            kb.reassign_task(
                conn,
                task_id,
                "systems-builder",
                reclaim_first=True,
                reason="must not reclaim before policy validation",
            )

        assert _task_mutation_snapshot(conn, task_id) == before


@pytest.mark.parametrize(
    "new_profile",
    [pytest.param("replacement", id="allowed-profile"), pytest.param(None, id="unassign")],
)
def test_reassign_reclaim_accepts_allowed_profile_or_unassignment(
    kanban_home: Path,
    new_profile: str | None,
) -> None:
    kb.create_board(
        "allowed-reassign-policy",
        allowed_profiles=["builder", "replacement"],
    )

    with kb.connect_closing(board="allowed-reassign-policy") as conn:
        task_id = kb.create_task(conn, title="claimed work", assignee="builder")
        assert kb.claim_task(conn, task_id) is not None

        assert kb.reassign_task(
            conn,
            task_id,
            new_profile,
            reclaim_first=True,
            reason="authorized reassignment",
        )
        reassigned = kb.get_task(conn, task_id)
        assert reassigned is not None
        assert reassigned.status == "ready"
        assert reassigned.claim_lock is None
        assert reassigned.current_run_id is None
        assert reassigned.assignee == new_profile


def test_list_tasks_assignee_filter_returns_historical_disallowed_assignment(
    kanban_home: Path,
) -> None:
    kb.create_board("historical-query", allowed_profiles=["systems-builder"])

    with kb.connect_closing(board="historical-query") as conn:
        historical_id = kb.create_task(
            conn,
            title="historical systems work",
            assignee="systems-builder",
        )

    _set_board_allowed("historical-query", ["frontend-builder"])
    with kb.connect_closing(board="historical-query") as conn:
        assert not kb.is_kanban_profile_allowed("systems-builder", conn=conn)
        historical = kb.list_tasks(conn, assignee="systems-builder")

    assert [(task.id, task.title, task.assignee) for task in historical] == [
        (historical_id, "historical systems work", "systems-builder")
    ]


def test_named_profile_cannot_escape_machine_ceiling(
    kanban_home: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _set_machine_allowed(kanban_home, ["systems-builder"])
    kb.create_board("named-profile-board")
    _set_board_allowed(
        "named-profile-board",
        ["systems-builder", "frank"],
    )

    named_home = kanban_home / "profiles" / "frank"
    named_home.mkdir(parents=True)
    monkeypatch.setenv("HERMES_HOME", str(named_home))

    # The profile-local home and the board list cannot widen the default-root
    # machine ceiling.
    assert kb.kanban_allowed_profiles() == frozenset({"systems-builder"})
    with kb.connect_closing(board="named-profile-board") as conn:
        assert kb.kanban_allowed_profiles(conn=conn) == frozenset({"systems-builder"})
        assert kb.is_kanban_profile_allowed("systems-builder", conn=conn)
        assert not kb.is_kanban_profile_allowed("frank", conn=conn)
        with pytest.raises(ValueError, match="not allowed for Kanban assignment"):
            kb.create_task(conn, title="ceiling bypass", assignee="frank")


@pytest.mark.parametrize(
    ("machine_allowed", "board_allowed", "board_hand_edited"),
    [
        pytest.param(
            "frontend-builder",
            ["frontend-builder"],
            False,
            id="malformed-machine",
        ),
        pytest.param(
            ["frontend-builder"],
            "frontend-builder",
            True,
            id="malformed-board",
        ),
        pytest.param(
            "frontend-builder",
            "frontend-builder",
            True,
            id="malformed-machine-and-board",
        ),
        pytest.param(
            ["frontend-builder", 7],
            ["frontend-builder"],
            False,
            id="malformed-machine-list-entry",
        ),
        pytest.param(
            ["frontend-builder"],
            ["frontend-builder", 7],
            True,
            id="malformed-board-non-string-list-entry",
        ),
        pytest.param(
            ["frontend-builder"],
            ["frontend-builder", "../escape"],
            True,
            id="malformed-board-invalid-profile-name",
        ),
    ],
)
def test_malformed_policy_layer_fails_closed(
    kanban_home: Path,
    machine_allowed: object,
    board_allowed: object,
    board_hand_edited: bool,
) -> None:
    kb.create_board("malformed-board")
    _set_machine_allowed(kanban_home, machine_allowed)
    if board_hand_edited:
        _set_board_allowed_raw("malformed-board", board_allowed)
    else:
        _set_board_allowed("malformed-board", board_allowed)

    with kb.connect_closing(board="malformed-board") as conn:
        assert kb.kanban_allowed_profiles(conn=conn) == frozenset()
        with pytest.raises(ValueError, match="no profiles are currently allowed"):
            kb.create_task(conn, title="blocked", assignee="frontend-builder")


def test_known_assignees_uses_connection_effective_policy(
    kanban_home: Path,
) -> None:
    _install_profile(kanban_home, "frontend-builder")
    _install_profile(kanban_home, "systems-builder")
    _set_machine_allowed(
        kanban_home,
        ["frontend-builder", "systems-builder"],
    )
    kb.create_board("roster-board")

    with kb.connect_closing(board="roster-board") as conn:
        historical_id = kb.create_task(
            conn,
            title="historical",
            assignee="systems-builder",
        )

    _set_board_allowed("roster-board", ["frontend-builder"])
    # No-context callers retain the machine-ceiling view, while an explicit
    # board connection narrows the assignment roster. Task 4 can request the
    # complete installed-profile inventory without weakening the default.
    assert kb.list_profiles_on_disk() == ["frontend-builder", "systems-builder"]
    with kb.connect_closing(board="roster-board") as conn:
        assert kb.list_profiles_on_disk(conn=conn) == ["frontend-builder"]
        assert set(kb.list_profiles_on_disk(conn=conn, include_disallowed=True)) == {
            "default",
            "frontend-builder",
            "systems-builder",
        }
        assert kb.known_assignees(conn) == [
            {"name": "frontend-builder", "on_disk": True, "counts": {}},
        ]
        visible_tasks = {task.id: task for task in kb.list_tasks(conn)}
        assert visible_tasks[historical_id].assignee == "systems-builder"


def test_tightened_board_policy_blocks_legacy_ready_and_review_dispatch_only_there(
    kanban_home: Path,
) -> None:
    _install_profile(kanban_home, "systems-builder")
    _install_profile(kanban_home, "rose")
    _set_machine_allowed(kanban_home, None)
    kb.create_board("tightened")
    kb.create_board("unchanged")
    _set_board_allowed("tightened", None)
    _set_board_allowed("unchanged", None)

    with kb.connect_closing(board="tightened") as conn:
        allowed_ready = kb.create_task(
            conn,
            title="allowed ready",
            assignee="systems-builder",
        )
        allowed_review = kb.create_task(
            conn,
            title="allowed review",
            assignee="systems-builder",
        )
        blocked_ready = kb.create_task(conn, title="legacy ready", assignee="rose")
        blocked_review = kb.create_task(conn, title="legacy review", assignee="rose")
        _move_to_review(conn, allowed_review)
        _move_to_review(conn, blocked_review)

    with kb.connect_closing(board="unchanged") as conn:
        unchanged_ready = kb.create_task(conn, title="ready elsewhere", assignee="rose")
        unchanged_review = kb.create_task(conn, title="review elsewhere", assignee="rose")
        _move_to_review(conn, unchanged_review)

    # Tighten only one board after the assignments already exist.
    _set_board_allowed("tightened", ["systems-builder"])

    with kb.connect_closing(board="tightened") as conn:
        kb.set_current_board("unchanged")
        tightened_result = kb.dispatch_once(conn, dry_run=True)

    with kb.connect_closing(board="unchanged") as conn:
        kb.set_current_board("tightened")
        unchanged_result = kb.dispatch_once(conn, dry_run=True)

    assert {task_id for task_id, _, _ in tightened_result.spawned} == {
        allowed_ready,
        allowed_review,
    }
    assert set(tightened_result.skipped_nonspawnable) == {
        blocked_ready,
        blocked_review,
    }
    assert {task_id for task_id, _, _ in unchanged_result.spawned} == {
        unchanged_ready,
        unchanged_review,
    }
    assert unchanged_result.skipped_nonspawnable == []


def test_dispatch_rejects_explicit_board_mismatch_without_mutation(
    kanban_home: Path,
) -> None:
    _install_profile(kanban_home, "systems-builder")
    kb.create_board("dispatch-frontend", allowed_profiles=["frontend-builder"])
    kb.create_board("dispatch-systems", allowed_profiles=["systems-builder"])

    spawned: list[str] = []
    with kb.connect_closing(board="dispatch-frontend") as conn:
        task_id = kb.create_task(conn, title="route only on the connected board")
        before = _task_mutation_snapshot(conn, task_id)

        with pytest.raises(ValueError, match="does not match connection board"):
            kb.dispatch_once(
                conn,
                board="dispatch-systems",
                default_assignee="systems-builder",
                spawn_fn=lambda task, _workspace: spawned.append(task.id),
            )

        assert spawned == []
        assert _task_mutation_snapshot(conn, task_id) == before


def _review_state_snapshot(conn, task_id: str) -> dict[str, object]:
    return {
        "task": dict(conn.execute("SELECT * FROM tasks WHERE id = ?", (task_id,)).fetchone()),
        "runs": [
            dict(row)
            for row in conn.execute(
                "SELECT * FROM task_runs WHERE task_id = ? ORDER BY id", (task_id,)
            )
        ],
        "events": [
            dict(row)
            for row in conn.execute(
                "SELECT * FROM task_events WHERE task_id = ? ORDER BY id", (task_id,)
            )
        ],
    }


@pytest.mark.parametrize("use_provenance", [False, True], ids=["explicit", "provenance"])
def test_review_handoff_rejects_disallowed_reviewer_without_mutation(
    kanban_home: Path,
    use_provenance: bool,
) -> None:
    kb.create_board("review-handoff-policy", allowed_profiles=["builder", "reviewer"])

    with kb.connect_closing(board="review-handoff-policy") as conn:
        task_id = kb.create_task(conn, title="review policy", assignee="builder")
        implementation = kb.claim_task(conn, task_id)
        assert implementation is not None

        if use_provenance:
            assert kb.request_review(
                conn,
                task_id,
                reviewer="reviewer",
                expected_run_id=implementation.current_run_id,
            )
            review = kb.claim_review_task(conn, task_id)
            assert review is not None
            assert kb.request_changes(
                conn,
                task_id,
                reason="revise",
                expected_run_id=review.current_run_id,
            ) == (True, "builder")
            implementation = kb.claim_task(conn, task_id)
            assert implementation is not None

        _set_board_allowed("review-handoff-policy", ["builder"])
        before = _review_state_snapshot(conn, task_id)

        with pytest.raises(ValueError, match="not allowed for Kanban assignment"):
            kb.request_review(
                conn,
                task_id,
                reviewer=None if use_provenance else "reviewer",
                expected_run_id=implementation.current_run_id,
            )

        assert _review_state_snapshot(conn, task_id) == before


def test_request_changes_rejects_disallowed_implementer_without_mutation(
    kanban_home: Path,
) -> None:
    kb.create_board(
        "changes-restoration-policy",
        allowed_profiles=["builder", "reviewer"],
    )

    with kb.connect_closing(board="changes-restoration-policy") as conn:
        task_id = kb.create_task(conn, title="restore policy", assignee="builder")
        implementation = kb.claim_task(conn, task_id)
        assert implementation is not None
        assert kb.request_review(
            conn,
            task_id,
            reviewer="reviewer",
            expected_run_id=implementation.current_run_id,
        )
        review = kb.claim_review_task(conn, task_id)
        assert review is not None

        _set_board_allowed("changes-restoration-policy", ["reviewer"])
        before = _review_state_snapshot(conn, task_id)

        with pytest.raises(ValueError, match="not allowed for Kanban assignment"):
            kb.request_changes(
                conn,
                task_id,
                reason="restore the original implementer",
                expected_run_id=review.current_run_id,
            )

        assert _review_state_snapshot(conn, task_id) == before
