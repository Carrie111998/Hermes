from __future__ import annotations

import hashlib
import json
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from threading import Barrier, Event

import pytest

from hermes_state import SCHEMA_VERSION, SessionDB
from session_bridge.mirror import (
    DiscoveryMode,
    EligibilityContext,
    MirrorCandidate,
    MirrorPolicy,
    enqueue_mirror_job,
)
from session_bridge.catalog import UnifiedCatalog
from session_bridge.models import (
    ContextPack,
    MirrorJobState,
    OriginKind,
    ProjectedMessage,
    Provider,
    Relation,
    SessionLink,
    SessionProjection,
    SidebarJobState,
    canonical_session_id,
)
from session_bridge.sidebar import (
    SidebarCandidate,
    sidebar_bridge_id,
    sidebar_idempotency_key,
)
from session_bridge.store import (
    SIDEBAR_EXCLUSION_REASONS,
    SIDEBAR_RETRYABLE_ERRORS,
    SessionBridgeStore,
)
from session_bridge.worktree import capture_worktree_snapshot


@pytest.fixture
def db(tmp_path):
    database = SessionDB(tmp_path / "state.db")
    yield database
    database.close()


def test_fresh_schema_has_current_version_and_sidebar_exclusions_table(db) -> None:
    assert _rows(db, "SELECT version FROM schema_version") == [
        {"version": SCHEMA_VERSION}
    ]
    assert _rows(
        db,
        "SELECT name FROM sqlite_master WHERE type = 'table' AND name = ?",
        ("session_sidebar_exclusions",),
    ) == [{"name": "session_sidebar_exclusions"}]


_CLAUDE_VISIBILITY_JOB_COLUMNS = [
    "id",
    "source_session_id",
    "bridge_id",
    "idempotency_key",
    "reserved_claude_uuid",
    "native_name",
    "source_provider",
    "source_cwd",
    "git_root",
    "git_branch",
    "git_head",
    "worktree_id",
    "signed_marker",
    "state",
    "attempts",
    "next_attempt_at",
    "lease_digest",
    "lease_expires_at",
    "error_code",
    "error_detail",
    "completion_digest",
    "eligible_at",
    "created_at",
    "updated_at",
    "visible_at",
]
_CLAUDE_REGISTRATION_USAGE_COLUMNS = [
    "local_day",
    "job_id",
    "attempt_ordinal",
    "reserved_estimated_cost_usd",
    "reserved_at",
]


def _unique_column_sets(db: SessionDB, table: str) -> set[tuple[str, ...]]:
    unique_sets: set[tuple[str, ...]] = set()
    for index in _rows(db, f'PRAGMA index_list("{table}")'):
        if index["unique"]:
            columns = _rows(db, f'PRAGMA index_info("{index["name"]}")')
            unique_sets.add(tuple(column["name"] for column in columns))
    return unique_sets


def test_fresh_claude_visibility_schema_has_exact_columns_states_and_uniques(
    db,
) -> None:
    assert [
        row["name"]
        for row in _rows(db, 'PRAGMA table_info("session_claude_visibility_jobs")')
    ] == _CLAUDE_VISIBILITY_JOB_COLUMNS
    table_sql = _rows(
        db,
        "SELECT sql FROM sqlite_master WHERE type = 'table' AND name = ?",
        ("session_claude_visibility_jobs",),
    )[0]["sql"]
    normalized = " ".join(table_sql.split())
    assert (
        "state IN ( 'claude_pending', 'claude_leased', 'claude_retry', "
        "'claude_visible', 'claude_failed' )"
    ) in normalized
    unique_sets = _unique_column_sets(db, "session_claude_visibility_jobs")
    assert {
        ("source_session_id",),
        ("bridge_id",),
        ("idempotency_key",),
        ("reserved_claude_uuid",),
    } <= unique_sets


def test_claude_registration_usage_migrates_existing_database_idempotently(
    tmp_path,
) -> None:
    path = tmp_path / "existing-state.db"
    connection = sqlite3.connect(path)
    connection.executescript(
        """
        CREATE TABLE preexisting_sentinel (value TEXT NOT NULL);
        INSERT INTO preexisting_sentinel (value) VALUES ('preserved');
        CREATE TABLE schema_version (version INTEGER NOT NULL);
        INSERT INTO schema_version (version) VALUES (21);
        """
    )
    connection.close()

    first = SessionDB(path)
    first.close()
    migrated = SessionDB(path)
    try:
        assert _rows(migrated, "SELECT * FROM preexisting_sentinel") == [
            {"value": "preserved"}
        ]
        assert [
            row["name"]
            for row in _rows(
                migrated,
                'PRAGMA table_info("session_claude_visibility_jobs")',
            )
        ] == _CLAUDE_VISIBILITY_JOB_COLUMNS
        assert [
            row["name"]
            for row in _rows(
                migrated,
                'PRAGMA table_info("session_claude_registration_usage")',
            )
        ] == _CLAUDE_REGISTRATION_USAGE_COLUMNS
        assert (
            "job_id",
            "attempt_ordinal",
        ) in _unique_column_sets(migrated, "session_claude_registration_usage")
    finally:
        migrated.close()


def test_mirror_worker_lock_serializes_independent_store_instances(db) -> None:
    first = SessionBridgeStore(db)
    second_db = SessionDB(db.db_path)
    second = SessionBridgeStore(second_db)
    first_lock = None
    second_lock = None
    try:
        first_lock = first.try_acquire_mirror_worker_lock()
        assert first_lock is not None
        assert second.try_acquire_mirror_worker_lock() is None

        first_lock.release()
        first_lock = None
        second_lock = second.try_acquire_mirror_worker_lock()
        assert second_lock is not None
    finally:
        if first_lock is not None:
            first_lock.release()
        if second_lock is not None:
            second_lock.release()
        second_db.close()


def _message(
    event_id: str,
    content: str,
    *,
    role: str | None = None,
    tool_calls=None,
    tool_call_id: str | None = None,
    timestamp: float | None = None,
) -> ProjectedMessage:
    return ProjectedMessage(
        native_event_id=event_id,
        ordinal=0,
        role=role or ("assistant" if tool_calls else "user"),
        content=content,
        timestamp=10.0 + len(event_id) if timestamp is None else timestamp,
        tool_calls=tool_calls,
        tool_call_id=tool_call_id,
    )


def _projection(
    *messages: ProjectedMessage,
    provider: Provider = Provider.CLAUDE,
    native_id: str = "native-1",
    cursor: str = "cursor-1",
    native_hash: str = "hash-1",
    last_active: float = 20.0,
    git_branch: str | None = None,
    origin_kind: OriginKind = OriginKind.NATIVE,
    origin_bridge_id: str | None = None,
) -> SessionProjection:
    return SessionProjection(
        provider=provider,
        native_id=native_id,
        title=f"{provider.value} session",
        cwd="C:/workspace/project",
        started_at=10.0,
        last_active=last_active,
        messages=messages,
        native_path=f"C:/{provider.value}/{native_id}.jsonl",
        native_status="active",
        native_cursor=cursor,
        native_hash=native_hash,
        git_branch=git_branch,
        parser_version=3,
        origin_kind=origin_kind,
        origin_bridge_id=origin_bridge_id,
    )


def _rows(db: SessionDB, sql: str, params=()):
    with db._lock:
        return [dict(row) for row in db._conn.execute(sql, params).fetchall()]


def _seed_continuation_snapshot_rows(db: SessionDB, count: int) -> None:
    def _write(conn):
        for index in range(count):
            suffix = f"{index:04d}"
            bridge_id = f"bridge-{suffix}"
            source_id = f"claude:source-{suffix}"
            target_id = f"codex:target-{suffix}"
            pack_id = f"pack-{suffix}"
            conn.executemany(
                "INSERT INTO sessions (id, source, started_at) VALUES (?, ?, ?)",
                [(source_id, "claude", 1.0), (target_id, "codex", 1.0)],
            )
            conn.execute(
                """INSERT INTO session_context_packs (
                   id, bridge_id, source_session_id, target_session_id,
                   source_cursor, source_hash, budget_chars, payload,
                   created_at, immutable_at
                   ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    pack_id,
                    bridge_id,
                    source_id,
                    target_id,
                    f"source-cursor-{suffix}",
                    f"source-hash-{suffix}",
                    4000,
                    "handoff",
                    2.0,
                    3.0,
                ),
            )
            conn.execute(
                """INSERT INTO session_links (
                   id, from_session_id, to_session_id, relation, bridge_id,
                   source_cursor, source_hash, created_at, hydrated_at
                   ) VALUES (?, ?, ?, 'continues', ?, ?, ?, ?, ?)""",
                (
                    f"link-{suffix}",
                    source_id,
                    target_id,
                    bridge_id,
                    f"source-cursor-{suffix}",
                    f"source-hash-{suffix}",
                    2.0,
                    3.0,
                ),
            )
            snapshot = {
                "version": 1,
                "pack_id": pack_id,
                "source_session_id": source_id,
                "source_cursor": f"source-cursor-{suffix}",
                "source_hash": f"source-hash-{suffix}",
                "target_session_id": target_id,
                "target_cursor": f"target-cursor-{suffix}",
                "target_hash": f"target-hash-{suffix}",
            }
            conn.execute(
                "INSERT INTO session_bridge_state (key, value_json, updated_at) VALUES (?, ?, ?)",
                (
                    f"session-bridge:continuation:{bridge_id}",
                    json.dumps(snapshot, sort_keys=True, separators=(",", ":")),
                    4.0,
                ),
            )

    db._execute_write(_write)


def _enqueue_manual_job(
    store: SessionBridgeStore,
    source_session_id: str,
    target_provider: Provider,
    *,
    generation: int = 1,
):
    return enqueue_mirror_job(
        store,
        source_session_id,
        target_provider,
        policy=MirrorPolicy(generation=generation),
        manual_authorized=True,
    )


def _enqueue_automatic_job(
    store: SessionBridgeStore,
    projection: SessionProjection,
    *,
    now: float,
):
    policy = MirrorPolicy(automatic_creation=True)
    source_session_id = canonical_session_id(
        projection.provider, projection.native_id
    )
    target = Provider.CODEX if projection.provider is Provider.CLAUDE else Provider.CLAUDE
    candidate = MirrorCandidate(
        source_session_id=source_session_id,
        target_provider=target,
        last_active=projection.last_active,
        projection=projection,
    )
    context = EligibilityContext(
        now=now,
        discovery_mode=DiscoveryMode.INITIAL_BACKFILL,
        continuous_watermark=None,
        existing_target_mappings=frozenset(),
        policy=policy,
    )
    return enqueue_mirror_job(
        store,
        source_session_id,
        target,
        policy=policy,
        candidate=candidate,
        context=context,
    )


def _capture_mapping_selects(db: SessionDB, operation) -> list[str]:
    statements: list[str] = []
    with db._lock:
        conn = db._conn
        assert conn is not None
        conn.set_trace_callback(statements.append)
    try:
        operation()
    finally:
        with db._lock:
            conn.set_trace_callback(None)
    return [
        statement
        for statement in statements
        if "FROM external_message_map" in statement
        and statement.lstrip().upper().startswith("SELECT")
    ]


def test_list_native_projections_is_newest_first_with_minimal_exact_evidence(db):
    store = SessionBridgeStore(db, clock=lambda: 100.0)
    older = _projection(
        _message(
            "assistant-event",
            "assistant transcript must not be materialized",
            role="assistant",
            timestamp=19.0,
        ),
        replace(
            _message(
                "shared-event",
                "   ",
                role="user",
                timestamp=20.0,
            ),
            ordinal=0,
        ),
        replace(
            _message(
                "shared-event",
                "first meaningful user turn",
                role="user",
                timestamp=21.0,
            ),
            ordinal=1,
        ),
        _message(
            "later-user-event",
            "later meaningful user turn",
            role="user",
            timestamp=22.0,
        ),
        native_id="older",
        last_active=40.0,
        cursor="older-cursor",
        native_hash="older-hash",
        git_branch="feature/older",
    )
    newer = _projection(
        _message("new-event", "newer", timestamp=30.0),
        provider=Provider.CODEX,
        native_id="newer",
        last_active=50.0,
        cursor="newer-cursor",
        native_hash="newer-hash",
    )
    bridged = _projection(
        _message("bridged-event", "must be excluded", timestamp=90.0),
        native_id="bridged",
        last_active=90.0,
        origin_kind=OriginKind.BRIDGE_PLACEHOLDER,
        origin_bridge_id="bridge-excluded",
    )
    for projection in (older, newer, bridged):
        store.upsert_projection(projection)

    projections = store.list_native_projections(after=35.0, limit=2)

    assert [projection.native_id for projection in projections] == ["newer", "older"]
    reconstructed = projections[1]
    assert reconstructed.provider is Provider.CLAUDE
    assert reconstructed.last_active == 40.0
    assert reconstructed.native_cursor == "older-cursor"
    assert reconstructed.native_hash == "older-hash"
    assert reconstructed.git_branch == "feature/older"
    assert tuple(reconstructed.messages) == (
        ProjectedMessage(
            native_event_id="shared-event",
            ordinal=1,
            role="user",
            content="f",
            timestamp=21.0,
        ),
    )


def test_list_native_projections_filters_all_eligibility_predicates_before_limit(db):
    store = SessionBridgeStore(db, clock=lambda: 200.0)
    projections = (
        _projection(
            _message("bridge", "bridge"),
            native_id="bridge",
            last_active=100.0,
            origin_kind=OriginKind.BRIDGE_PLACEHOLDER,
            origin_bridge_id="bridge-origin",
        ),
        _projection(
            _message("mapped", "mapped"),
            native_id="mapped",
            last_active=90.0,
        ),
        _projection(
            _message("active-job", "active job"),
            native_id="active-job",
            last_active=80.0,
        ),
        _projection(
            _message("assistant-only", "not a user", role="assistant"),
            _message("blank-user", "\u2003\n\t", role="user"),
            native_id="meaningless",
            last_active=70.0,
        ),
        _projection(
            _message("eligible", "eligible"),
            native_id="eligible",
            last_active=60.0,
        ),
        _projection(
            _message("target", "target"),
            provider=Provider.CODEX,
            native_id="mapped-target",
            last_active=50.0,
            origin_kind=OriginKind.BRIDGE_PLACEHOLDER,
            origin_bridge_id="mapped-target-origin",
        ),
    )
    for projection in projections:
        store.upsert_projection(projection)
    store.create_link(
        SessionLink(
            id="mapped-link",
            from_session_id="claude:mapped",
            to_session_id="codex:mapped-target",
            relation=Relation.MIRRORS,
            bridge_id="mapped-bridge",
            source_cursor=None,
            source_hash=None,
            created_at=100.0,
        )
    )
    _enqueue_manual_job(store, "claude:active-job", Provider.CODEX)

    page = store.list_native_projections(after=0.0, limit=1)

    assert [projection.native_id for projection in page] == ["eligible"]
    assert page.has_more is False
    assert page.next_cursor is None


def test_list_native_projections_keyset_page_prevents_older_candidate_starvation(db):
    store = SessionBridgeStore(db, clock=lambda: 100.0)
    for native_id, last_active in (
        ("newest", 50.0),
        ("tie-a", 40.0),
        ("tie-b", 40.0),
        ("oldest", 30.0),
    ):
        store.upsert_projection(
            _projection(
                _message(f"event-{native_id}", native_id),
                native_id=native_id,
                last_active=last_active,
            )
        )

    first = store.list_native_projections(after=0.0, limit=2)
    second = store.list_native_projections(
        after=0.0,
        limit=2,
        cursor=first.next_cursor,
    )

    assert [projection.native_id for projection in first] == ["newest", "tie-a"]
    assert first.has_more is True
    assert first.next_cursor == (40.0, "claude:tie-a")
    assert [projection.native_id for projection in second] == ["tie-b", "oldest"]
    assert second.has_more is False
    assert second.next_cursor is None


def test_list_native_projections_applies_inclusive_cutoff_and_bounded_limit(db):
    store = SessionBridgeStore(db, clock=lambda: 100.0)
    store.upsert_projection(
        _projection(_message("boundary", "boundary"), last_active=40.0)
    )

    assert [
        projection.native_id
        for projection in store.list_native_projections(after=40.0, limit=1)
    ] == ["native-1"]
    with pytest.raises(ValueError, match="after"):
        store.list_native_projections(after=float("nan"), limit=1)
    with pytest.raises(ValueError, match="between 1 and 1000"):
        store.list_native_projections(after=0.0, limit=0)
    with pytest.raises(ValueError, match="between 1 and 1000"):
        store.list_native_projections(after=0.0, limit=1001)


def test_list_existing_target_mappings_returns_exact_provider_pairs(db):
    store = SessionBridgeStore(db, clock=lambda: 100.0)
    store.upsert_projection(
        _projection(_message("source", "source"), native_id="source")
    )
    store.upsert_projection(
        _projection(
            _message("target", "target"),
            provider=Provider.CODEX,
            native_id="target",
        )
    )
    store.create_link(
        SessionLink(
            id="mapping-link",
            from_session_id="claude:source",
            to_session_id="codex:target",
            relation=Relation.MIRRORS,
            bridge_id="mapping-bridge",
            source_cursor=None,
            source_hash=None,
            created_at=100.0,
        )
    )

    assert store.list_existing_target_mappings(
        ["claude:source", "claude:unmapped"]
    ) == frozenset({("claude:source", Provider.CODEX)})
    assert store.list_existing_target_mappings([]) == frozenset()
    with pytest.raises(TypeError, match="sequence"):
        store.list_existing_target_mappings("claude:source")
    with pytest.raises(ValueError, match="at most 1000"):
        store.list_existing_target_mappings(
            [f"claude:source-{index}" for index in range(1001)]
        )


def test_first_import_is_idempotent_and_append_only(db):
    store = SessionBridgeStore(db, clock=lambda: 100.0)
    first = _projection(_message("e1", "first"), _message("e2", "second"))

    first_result = store.upsert_projection(first)
    replay_result = store.upsert_projection(first)
    appended_result = store.upsert_projection(
        replace(
            first,
            messages=(*first.messages, _message("e3", "third")),
            native_cursor="cursor-2",
            native_hash="hash-2",
        )
    )

    assert first_result.first_seen is True
    assert first_result.inserted_messages == 2
    assert replay_result.first_seen is False
    assert replay_result.inserted_messages == 0
    assert appended_result.inserted_messages == 1
    assert db.session_count() == 1
    assert [m["content"] for m in db.get_messages("claude:native-1")] == [
        "first",
        "second",
        "third",
    ]
    assert len(_rows(db, "SELECT * FROM external_message_map")) == 3
    session = db.get_session("claude:native-1")
    assert session["source"] == "claude"
    assert session["title"] == "claude session"
    assert session["message_count"] == 3
    assert (
        store.get_external_session("claude:native-1")["last_native_cursor"]
        == "cursor-2"
    )


def test_delta_mapping_lookup_queries_only_incoming_composite_identity(db):
    store = SessionBridgeStore(db, clock=lambda: 100.0)
    store.upsert_projection(
        _projection(*(_message(f"history-{index}", "old") for index in range(25)))
    )

    selects = _capture_mapping_selects(
        db,
        lambda: store.upsert_projection(
            _projection(
                _message("delta-1", "new"),
                cursor="delta-cursor",
                native_hash="delta-hash",
            )
        ),
    )

    assert len(selects) == 1
    assert "(native_event_id, ordinal) IN" in selects[0]
    assert "delta-1" in selects[0]
    assert "history-1" not in selects[0]


def test_empty_delta_skips_message_mapping_lookup(db):
    store = SessionBridgeStore(db, clock=lambda: 100.0)
    store.upsert_projection(_projection(_message("history", "old")))

    selects = _capture_mapping_selects(
        db,
        lambda: store.upsert_projection(
            _projection(
                cursor="empty-cursor",
                native_hash="empty-hash",
            )
        ),
    )

    assert selects == []


def test_rebuild_skips_message_mapping_lookup(db):
    store = SessionBridgeStore(db, clock=lambda: 100.0)
    store.upsert_projection(_projection(_message("history", "old")))

    selects = _capture_mapping_selects(
        db,
        lambda: store.upsert_projection(
            _projection(
                _message("rebuilt", "new"),
                cursor="rebuilt-cursor",
                native_hash="rebuilt-hash",
            ),
            rebuild=True,
        ),
    )

    assert selects == []


def test_mapping_lookup_chunks_large_incoming_projection(db):
    store = SessionBridgeStore(db, clock=lambda: 100.0)
    incoming = tuple(
        _message(f"incoming-{index}", "new", role="assistant") for index in range(401)
    )

    selects = _capture_mapping_selects(
        db,
        lambda: store.upsert_projection(_projection(*incoming)),
    )

    assert len(selects) == 2
    assert all("(native_event_id, ordinal) IN" in statement for statement in selects)


def test_projection_persists_updates_and_preserves_git_branch_on_none(db):
    store = SessionBridgeStore(db, clock=lambda: 100.0)

    store.upsert_projection(
        _projection(_message("e1", "first"), git_branch="feature/first")
    )
    assert db.get_session("claude:native-1")["git_branch"] == "feature/first"

    store.upsert_projection(
        _projection(
            _message("e1", "first"),
            cursor="cursor-2",
            last_active=21.0,
            git_branch="feature/second",
        )
    )
    assert db.get_session("claude:native-1")["git_branch"] == "feature/second"

    store.upsert_projection(
        _projection(
            _message("e1", "first"),
            cursor="cursor-3",
            last_active=22.0,
        )
    )
    assert db.get_session("claude:native-1")["git_branch"] == "feature/second"


@pytest.mark.parametrize("git_branch", ["", " \t "])
def test_projection_preserves_git_branch_on_blank_delta(db, git_branch):
    store = SessionBridgeStore(db, clock=lambda: 100.0)
    store.upsert_projection(
        _projection(_message("e1", "first"), git_branch="feature/current")
    )

    store.upsert_projection(
        _projection(
            _message("e1", "first"),
            cursor="cursor-2",
            last_active=21.0,
            git_branch=git_branch,
        )
    )

    assert db.get_session("claude:native-1")["git_branch"] == "feature/current"


def test_projection_strips_a_non_empty_git_branch(db):
    store = SessionBridgeStore(db, clock=lambda: 100.0)

    store.upsert_projection(
        _projection(
            _message("e1", "first"),
            git_branch="  feature/normalized\t",
        )
    )

    assert db.get_session("claude:native-1")["git_branch"] == "feature/normalized"


def test_rebuild_replaces_only_mapped_messages_and_preserves_first_index_time(db):
    now = [100.0]
    store = SessionBridgeStore(db, clock=lambda: now[0])
    store.upsert_projection(
        _projection(_message("e1", "old one"), _message("e2", "old two"))
    )
    db.append_message(
        "claude:native-1",
        "user",
        "ordinary Hermes continuation",
        timestamp=1_000.0,
    )
    now[0] = 200.0

    result = store.upsert_projection(
        _projection(
            _message("e1", "rebuilt one"),
            cursor="cursor-rebuilt",
            native_hash="hash-rebuilt",
        ),
        rebuild=True,
    )

    assert result.rebuilt is True
    assert result.inserted_messages == 1
    assert [m["content"] for m in db.get_messages("claude:native-1")] == [
        "ordinary Hermes continuation",
        "rebuilt one",
    ]
    external = store.get_external_session("claude:native-1")
    assert external["first_indexed_at"] == 100.0
    assert external["last_indexed_at"] == 200.0
    assert db.get_session("claude:native-1")["message_count"] == 2


def test_projection_rolls_back_every_bridge_write_on_mid_insert_failure(
    db, monkeypatch
):
    store = SessionBridgeStore(db, clock=lambda: 100.0)
    store.upsert_projection(
        _projection(_message("e1", "committed"), git_branch="feature/committed")
    )
    original = db._insert_message_rows_with_ids

    def insert_then_fail(conn, session_id, messages):
        original(conn, session_id, messages[:1])
        raise RuntimeError("injected write failure")

    monkeypatch.setattr(db, "_insert_message_rows_with_ids", insert_then_fail)

    with pytest.raises(RuntimeError, match="injected write failure"):
        store.upsert_projection(
            _projection(
                _message("e1", "committed"),
                _message("e2", "must roll back"),
                cursor="cursor-uncommitted",
                native_hash="hash-uncommitted",
                git_branch="feature/must-roll-back",
            )
        )

    assert [m["content"] for m in db.get_messages("claude:native-1")] == ["committed"]
    assert len(_rows(db, "SELECT * FROM external_message_map")) == 1
    assert (
        store.get_external_session("claude:native-1")["last_native_cursor"]
        == "cursor-1"
    )
    assert db.get_session("claude:native-1")["git_branch"] == "feature/committed"


@pytest.mark.parametrize("existing_source", ["cli", "claude"])
def test_projection_refuses_to_adopt_an_untracked_colliding_session(
    db, existing_source
):
    db.create_session("claude:native-1", existing_source)
    db.append_message("claude:native-1", "user", "must survive")
    store = SessionBridgeStore(db, clock=lambda: 100.0)

    with pytest.raises(ValueError, match="collision"):
        store.upsert_projection(_projection(_message("e1", "external")))

    assert db.get_session("claude:native-1")["source"] == existing_source
    assert [m["content"] for m in db.get_messages("claude:native-1")] == [
        "must survive"
    ]
    assert store.get_external_session("claude:native-1") is None


def test_projection_refuses_a_different_external_identity_collision(db):
    db.create_session("claude:native-1", "codex")
    with db._lock:
        db._conn.execute(
            """INSERT INTO external_sessions (
               session_id, provider, native_id, first_indexed_at, last_indexed_at,
               parser_version, origin_kind
               ) VALUES (?, ?, ?, ?, ?, ?, ?)""",
            ("claude:native-1", "codex", "different", 1.0, 1.0, 1, "native"),
        )
    store = SessionBridgeStore(db, clock=lambda: 100.0)

    with pytest.raises(ValueError, match="collision"):
        store.upsert_projection(_projection(_message("e1", "external")))

    assert store.get_external_session("claude:native-1")["provider"] == "codex"


def test_projection_normalizes_native_identity_before_persisting_and_comparing(db):
    store = SessionBridgeStore(db, clock=lambda: 100.0)

    first = store.upsert_projection(
        _projection(_message("e1", "external"), native_id="  native-1  ")
    )
    replay = store.upsert_projection(
        _projection(_message("e1", "external"), native_id="native-1")
    )

    assert first.session_id == "claude:native-1"
    assert replay.inserted_messages == 0
    assert store.get_external_session("claude:native-1")["native_id"] == "native-1"


def test_projection_preserves_placeholder_on_replay_and_non_human_native_deltas(db):
    store = SessionBridgeStore(db, clock=lambda: 100.0)
    placeholder = _projection(
        _message("e1", "placeholder"),
        cursor="placeholder-cursor",
        origin_kind=OriginKind.BRIDGE_PLACEHOLDER,
        origin_bridge_id="bridge-1",
    )
    store.upsert_projection(placeholder)
    exact_replay = store.upsert_projection(placeholder)

    native_user_replay = store.upsert_projection(
        _projection(
            _message("e1", "placeholder"),
            cursor="native-replay-cursor",
            native_hash="native-replay-hash",
        )
    )
    store.upsert_projection(
        _projection(
            _message("e2", "assistant delta", role="assistant"),
            _message(
                "e3",
                "",
                role="assistant",
                tool_calls=[{"id": "call-1", "name": "Read"}],
            ),
            _message(
                "e4",
                "tool result delta",
                role="tool",
                tool_call_id="call-1",
            ),
            cursor="native-non-human-cursor",
            native_hash="native-non-human-hash",
        )
    )

    external = store.get_external_session("claude:native-1")
    assert external is not None
    assert exact_replay.inserted_messages == 0
    assert native_user_replay.inserted_messages == 0
    assert external["origin_kind"] == "bridge_placeholder"
    assert external["origin_bridge_id"] == "bridge-1"
    assert external["last_native_cursor"] == "native-non-human-cursor"


def test_new_native_user_delta_promotes_placeholder(db):
    store = SessionBridgeStore(db, clock=lambda: 100.0)
    store.upsert_projection(
        _projection(
            _message("e1", "bridge marker"),
            origin_kind=OriginKind.BRIDGE_PLACEHOLDER,
            origin_bridge_id="bridge-1",
        )
    )

    result = store.upsert_projection(
        _projection(
            _message("e2", "  genuine later user  "),
            cursor="human-cursor",
            native_hash="human-hash",
        )
    )

    external = store.get_external_session("claude:native-1")
    assert external is not None
    assert result.inserted_messages == 1
    assert external["origin_kind"] == "bridge_continuation"
    assert external["origin_bridge_id"] == "bridge-1"


@pytest.mark.parametrize("content", ["", "   ", "\t\n"])
def test_empty_pending_user_does_not_promote_placeholder(db, content):
    store = SessionBridgeStore(db, clock=lambda: 100.0)
    store.upsert_projection(
        _projection(
            _message("e1", "bridge marker"),
            origin_kind=OriginKind.BRIDGE_PLACEHOLDER,
            origin_bridge_id="bridge-1",
        )
    )

    result = store.upsert_projection(
        _projection(
            _message("e2", content),
            cursor="empty-user-cursor",
            native_hash="empty-user-hash",
        )
    )

    external = store.get_external_session("claude:native-1")
    assert external is not None
    assert result.inserted_messages == 1
    assert external["origin_kind"] == "bridge_placeholder"
    assert external["origin_bridge_id"] == "bridge-1"


def test_explicit_placeholder_to_continuation_is_monotonic_for_same_bridge(db):
    store = SessionBridgeStore(db, clock=lambda: 100.0)
    placeholder = _projection(
        _message("e1", "bridge marker"),
        origin_kind=OriginKind.BRIDGE_PLACEHOLDER,
        origin_bridge_id="bridge-1",
    )
    store.upsert_projection(placeholder)

    store.upsert_projection(
        replace(
            placeholder,
            native_cursor="continuation-cursor",
            origin_kind=OriginKind.BRIDGE_CONTINUATION,
        )
    )
    store.upsert_projection(
        replace(
            placeholder,
            native_cursor="placeholder-replay-cursor",
        )
    )
    store.upsert_projection(
        replace(
            placeholder,
            native_cursor="native-replay-cursor",
            origin_kind=OriginKind.NATIVE,
            origin_bridge_id=None,
        )
    )

    external = store.get_external_session("claude:native-1")
    assert external is not None
    assert external["origin_kind"] == "bridge_continuation"
    assert external["origin_bridge_id"] == "bridge-1"
    assert external["last_native_cursor"] == "native-replay-cursor"


def test_rebuild_uses_explicit_provenance_without_inferring_from_marker_user(db):
    store = SessionBridgeStore(db, clock=lambda: 100.0)
    store.upsert_projection(
        _projection(
            _message("e1", "original bridge marker"),
            origin_kind=OriginKind.BRIDGE_PLACEHOLDER,
            origin_bridge_id="bridge-1",
        )
    )

    store.upsert_projection(
        _projection(
            _message("rebuilt-marker", "original bridge marker"),
            cursor="rebuilt-cursor",
            native_hash="rebuilt-hash",
            origin_kind=OriginKind.BRIDGE_PLACEHOLDER,
            origin_bridge_id="bridge-1",
        ),
        rebuild=True,
    )

    external = store.get_external_session("claude:native-1")
    assert external is not None
    assert external["origin_kind"] == "bridge_placeholder"
    assert external["origin_bridge_id"] == "bridge-1"


def test_projection_rejects_conflicting_bridge_ids_atomically(db):
    store = SessionBridgeStore(db, clock=lambda: 100.0)
    store.upsert_projection(_projection(_message("e1", "native")))
    store.upsert_projection(
        _projection(
            _message("e1", "native"),
            cursor="upgraded-cursor",
            origin_kind=OriginKind.BRIDGE_CONTINUATION,
            origin_bridge_id="bridge-1",
        )
    )

    conflicts = (
        (OriginKind.BRIDGE_CONTINUATION, "bridge-2"),
        (OriginKind.BRIDGE_PLACEHOLDER, "bridge-2"),
    )
    for origin_kind, origin_bridge_id in conflicts:
        with pytest.raises(ValueError, match="provenance"):
            store.upsert_projection(
                _projection(
                    _message("e1", "native"),
                    _message("e2", "must roll back"),
                    cursor="conflicting-cursor",
                    origin_kind=origin_kind,
                    origin_bridge_id=origin_bridge_id,
                )
            )

    external = store.get_external_session("claude:native-1")
    assert external["origin_kind"] == "bridge_continuation"
    assert external["origin_bridge_id"] == "bridge-1"
    assert external["last_native_cursor"] == "upgraded-cursor"
    assert [row["content"] for row in db.get_messages("claude:native-1")] == ["native"]


def test_observably_stale_projection_cannot_regress_external_metadata(db):
    store = SessionBridgeStore(db, clock=lambda: 100.0)
    store.upsert_projection(
        _projection(
            _message("e1", "newest", timestamp=10.0),
            cursor="new-cursor",
            native_hash="new-hash",
            last_active=100.0,
            git_branch="feature/newest",
        )
    )

    with pytest.raises(ValueError, match="stale"):
        store.upsert_projection(
            replace(
                _projection(
                    _message("e2", "older", timestamp=11.0),
                    cursor="old-cursor",
                    native_hash="old-hash",
                    last_active=90.0,
                    git_branch="feature/stale",
                ),
                native_path="C:/claude/old-path.jsonl",
                native_status="archived",
                parser_version=1,
            )
        )

    external = store.get_external_session("claude:native-1")
    assert external["last_native_cursor"] == "new-cursor"
    assert external["last_native_hash"] == "new-hash"
    assert external["native_status"] == "active"
    assert db.get_session("claude:native-1")["git_branch"] == "feature/newest"
    assert [row["content"] for row in db.get_messages("claude:native-1")] == ["newest"]


def test_rebuild_allows_truncation_and_replaces_external_activity_watermark(db):
    store = SessionBridgeStore(db, clock=lambda: 100.0)
    store.upsert_projection(
        _projection(
            _message("e1", "before truncation", timestamp=80.0),
            cursor="cursor-before",
            native_hash="hash-before",
            last_active=100.0,
        )
    )

    result = store.upsert_projection(
        _projection(
            _message("e2", "after truncation", timestamp=40.0),
            cursor="cursor-after",
            native_hash="hash-after",
            last_active=50.0,
        ),
        rebuild=True,
    )

    external = store.get_external_session("claude:native-1")
    assert result.rebuilt is True
    assert external["last_native_cursor"] == "cursor-after"
    assert external["last_native_hash"] == "hash-after"
    watermark = _rows(
        db,
        "SELECT value_json FROM session_bridge_state WHERE key = ?",
        ("session-bridge:external-activity:claude:native-1",),
    )[0]
    assert json.loads(watermark["value_json"]) == {"last_active": 50.0}


def test_orphan_activity_watermark_does_not_block_fresh_rediscovery(db):
    store = SessionBridgeStore(db, clock=lambda: 100.0)
    store.upsert_projection(
        _projection(
            _message("e1", "before deletion"),
            cursor="cursor-before",
            last_active=100.0,
        )
    )
    assert db.delete_session("claude:native-1") is True

    rediscovered = store.upsert_projection(
        _projection(
            _message("e2", "fresh discovery"),
            cursor="cursor-after",
            last_active=50.0,
        )
    )

    assert rediscovered.first_seen is True
    assert store.get_external_session("claude:native-1")["last_native_cursor"] == (
        "cursor-after"
    )
    watermark = _rows(
        db,
        "SELECT value_json FROM session_bridge_state WHERE key = ?",
        ("session-bridge:external-activity:claude:native-1",),
    )[0]
    assert json.loads(watermark["value_json"]) == {"last_active": 50.0}


def test_imported_messages_are_discoverable_through_existing_fts(db):
    store = SessionBridgeStore(db, clock=lambda: 100.0)
    store.upsert_projection(_projection(_message("e1", "quartzbridge uniqueterm")))

    matches = db.search_messages("quartzbridge")

    assert [match["session_id"] for match in matches] == ["claude:native-1"]


def test_low_level_mirror_job_is_idempotent_but_public_claim_fails_closed(db):
    store = SessionBridgeStore(db, clock=lambda: 100.0)
    store.upsert_projection(_projection(_message("e1", "source")))

    first = store.enqueue_mirror_job(
        "claude:native-1", Provider.CODEX, policy_generation=4
    )
    replay = store.enqueue_mirror_job(
        "claude:native-1", Provider.CODEX, policy_generation=4
    )

    assert replay == first
    assert len(_rows(db, "SELECT * FROM session_mirror_jobs")) == 1
    with pytest.raises(TypeError, match="policy"):
        store.claim_due_jobs(  # type: ignore[missing-argument]
            now=100.0, limit=10
        )
    assert _rows(db, "SELECT * FROM session_mirror_jobs")[0]["state"] == "queued"

    assert (
        store.claim_due_jobs(now=100.0, limit=10, policy=MirrorPolicy()) == []
    )
    failed = _rows(db, "SELECT * FROM session_mirror_jobs")[0]
    assert failed["state"] == "manual_failure"
    assert failed["attempts"] == 0
    assert failed["error_code"] == "authority_missing"


def test_mirror_jobs_can_be_listed_by_state_with_a_deterministic_bound(db):
    store = SessionBridgeStore(db, clock=lambda: 100.0)
    jobs = []
    for native_id in ("source-c", "source-a", "source-b"):
        session_id = f"claude:{native_id}"
        store.upsert_projection(
            _projection(_message(native_id, native_id), native_id=native_id)
        )
        jobs.append(
            store.enqueue_mirror_job(session_id, Provider.CODEX, policy_generation=1)
        )
    retry_id = jobs[1]["id"]
    db._execute_write(
        lambda conn: conn.execute(
            "UPDATE session_mirror_jobs SET state = 'retry' WHERE id = ?",
            (retry_id,),
        )
    )

    first = store.list_mirror_jobs(
        [MirrorJobState.RETRY, MirrorJobState.QUEUED], limit=2
    )
    replay = store.list_mirror_jobs(["queued", "retry"], limit=2)

    expected_ids = sorted(job["id"] for job in jobs)[:2]
    assert [job["id"] for job in first] == expected_ids
    assert replay == first


def test_mirror_job_listing_validates_state_filters_and_limit(db):
    store = SessionBridgeStore(db)

    assert store.list_mirror_jobs([], limit=1) == []
    with pytest.raises(ValueError, match="mirror job state"):
        store.list_mirror_jobs(["unknown"], limit=1)
    with pytest.raises(ValueError, match="between 1 and 1000"):
        store.list_mirror_jobs([MirrorJobState.QUEUED], limit=0)
    with pytest.raises(ValueError, match="between 1 and 1000"):
        store.list_mirror_jobs([MirrorJobState.QUEUED], limit=1001)


def test_mirror_job_counts_have_a_stable_all_states_shape(db):
    store = SessionBridgeStore(db, clock=lambda: 100.0)
    for native_id in ("queued", "running", "failed"):
        store.upsert_projection(
            _projection(_message(native_id, native_id), native_id=native_id)
        )
        store.enqueue_mirror_job(
            f"claude:{native_id}", Provider.CODEX, policy_generation=1
        )
    db._execute_write(
        lambda conn: conn.executescript(
            """UPDATE session_mirror_jobs SET state = 'running'
               WHERE source_session_id = 'claude:running';
               UPDATE session_mirror_jobs SET state = 'manual_failure'
               WHERE source_session_id = 'claude:failed';"""
        )
    )

    assert store.mirror_job_counts() == {
        "queued": 1,
        "running": 1,
        "retry": 0,
        "succeeded": 0,
        "manual_failure": 1,
    }


def test_atomic_rate_limited_claim_prevents_two_store_oversubscription(tmp_path):
    path = tmp_path / "shared-state.db"
    first_db = SessionDB(path)
    second_db = SessionDB(path)
    try:
        first = SessionBridgeStore(first_db, clock=lambda: 100.0)
        second = SessionBridgeStore(second_db, clock=lambda: 100.0)
        for native_id in ("source-a", "source-b"):
            first.upsert_projection(
                _projection(_message(native_id, native_id), native_id=native_id)
            )
            _enqueue_manual_job(first, f"claude:{native_id}", Provider.CODEX)
        barrier = Barrier(2)

        def _claim(store):
            barrier.wait()
            return store.claim_due_jobs_with_limits(
                now=100.0,
                limit=1,
                policy=MirrorPolicy(creates_per_minute=1),
            )

        with ThreadPoolExecutor(max_workers=2) as executor:
            results = list(executor.map(_claim, (first, second)))

        claimed = [job for batch in results for job in batch]
        assert len(claimed) == 1
        assert claimed[0]["claim_authority"] == "manual"
        assert first.get_state("session-bridge:mirror-rate") == {
            "version": 1,
            "attempted_at": [100.0],
        }
        assert len(first.list_mirror_jobs([MirrorJobState.QUEUED])) == 1
    finally:
        second_db.close()
        first_db.close()


def test_atomic_claim_keeps_manual_authority_when_automatic_breaker_halts(db):
    store = SessionBridgeStore(db, clock=lambda: 100.0)
    automatic_projection = _projection(
        _message("auto", "automatic"), native_id="automatic"
    )
    manual_projection = _projection(_message("manual", "manual"), native_id="manual")
    store.upsert_projection(automatic_projection)
    store.upsert_projection(manual_projection)
    automatic = _enqueue_automatic_job(store, automatic_projection, now=100.0)
    manual = _enqueue_manual_job(store, "claude:manual", Provider.CODEX)
    store.accumulate_mirror_breaker_progress(attempts=1, errors=1)

    claimed = store.claim_due_jobs_with_limits(
        now=100.0,
        limit=2,
        policy=MirrorPolicy(automatic_creation=True),
    )

    assert [job["id"] for job in claimed] == [manual["id"]]
    assert claimed[0]["claim_authority"] == "manual"
    rows = {job["id"]: job for job in store.list_mirror_jobs([
        MirrorJobState.QUEUED,
        MirrorJobState.RUNNING,
    ])}
    assert rows[automatic["id"]]["state"] == "queued"


def test_atomic_store_claim_revokes_safe_manual_job_after_mapping_appears(db):
    store = SessionBridgeStore(db, clock=lambda: 100.0)
    store.upsert_projection(_projection(_message("source", "source")))
    enqueue_mirror_job(
        store,
        "claude:native-1",
        Provider.CODEX,
        policy=MirrorPolicy(),
        manual_authorized=True,
        require_unmapped=True,
    )
    store.upsert_projection(
        _projection(
            _message("target", "target"),
            provider=Provider.CODEX,
            native_id="target",
        )
    )
    store.create_link(
        SessionLink(
            id="safe-manual-mapped",
            from_session_id="claude:native-1",
            to_session_id="codex:target",
            relation=Relation.MIRRORS,
            bridge_id="safe-manual-mapped",
            source_cursor=None,
            source_hash=None,
            created_at=100.0,
        )
    )

    claimed = store.claim_due_jobs_with_limits(
        now=100.0,
        limit=1,
        policy=MirrorPolicy(),
    )

    assert claimed == []
    failed = store.list_mirror_jobs([MirrorJobState.MANUAL_FAILURE])
    assert len(failed) == 1
    assert failed[0]["error_code"] == "manual_authority_revoked"


def test_atomic_claim_accepts_an_exact_job_id_scope(db):
    store = SessionBridgeStore(db, clock=lambda: 100.0)
    jobs = []
    for native_id in ("outside", "selected"):
        store.upsert_projection(
            _projection(_message(native_id, native_id), native_id=native_id)
        )
        jobs.append(_enqueue_manual_job(store, f"claude:{native_id}", Provider.CODEX))

    claimed = store.claim_due_jobs_with_limits(
        now=100.0,
        limit=1,
        policy=MirrorPolicy(),
        job_ids=[jobs[1]["id"]],
    )

    assert [job["id"] for job in claimed] == [jobs[1]["id"]]
    assert store.claim_due_jobs_with_limits(
        now=100.0,
        limit=1,
        policy=MirrorPolicy(),
        job_ids=[],
    ) == []
    queued = store.list_mirror_jobs([MirrorJobState.QUEUED])
    assert [job["id"] for job in queued] == [jobs[0]["id"]]
    with pytest.raises(TypeError, match="sequence"):
        store.claim_due_jobs_with_limits(
            now=100.0,
            limit=1,
            policy=MirrorPolicy(),
            job_ids=jobs[0]["id"],
        )


def test_rollout_limited_manual_claims_use_global_breaker_with_auto_off(db):
    store = SessionBridgeStore(db, clock=lambda: 100.0)
    jobs = []
    for native_id in ("rollout-one", "rollout-two"):
        store.upsert_projection(
            _projection(_message(native_id, native_id), native_id=native_id)
        )
        jobs.append(
            enqueue_mirror_job(
                store,
                f"claude:{native_id}",
                Provider.CODEX,
                policy=MirrorPolicy(),
                manual_authorized=True,
                require_unmapped=True,
                rollout_limited=True,
            )
        )
    policy = MirrorPolicy(
        automatic_creation=False,
        creates_per_minute=6,
        stop_after_attempts=1,
        stop_error_rate=0.5,
    )

    claimed = store.claim_due_jobs_with_limits(now=100.0, limit=2, policy=policy)

    assert len(claimed) == 1
    assert claimed[0]["claim_authority"] == "manual"
    assert claimed[0]["rollout_limited"] is True
    assert store.get_mirror_breaker_progress() == {"attempts": 1, "errors": 0}
    assert store.claim_due_jobs_with_limits(now=100.0, limit=2, policy=policy) == []

    store.retry_job(
        claimed[0]["id"],
        code="provider_failed",
        detail="fixed failure",
        next_attempt_at=120.0,
    )

    assert store.get_mirror_breaker_progress() == {"attempts": 1, "errors": 1}
    assert store.claim_due_jobs_with_limits(now=100.0, limit=2, policy=policy) == []
    assert jobs[1]["id"] in {
        job["id"] for job in store.list_mirror_jobs([MirrorJobState.QUEUED])
    }


def test_rollout_limited_claim_resets_a_healthy_completed_batch_with_auto_off(db):
    store = SessionBridgeStore(db, clock=lambda: 100.0)
    store.upsert_projection(_projection(_message("rollout", "rollout")))
    job = enqueue_mirror_job(
        store,
        "claude:native-1",
        Provider.CODEX,
        policy=MirrorPolicy(),
        manual_authorized=True,
        require_unmapped=True,
        rollout_limited=True,
    )
    store.accumulate_mirror_breaker_progress(attempts=1, errors=0)

    claimed = store.claim_due_jobs_with_limits(
        now=100.0,
        limit=1,
        policy=MirrorPolicy(
            automatic_creation=False,
            stop_after_attempts=1,
            stop_error_rate=0.5,
        ),
    )

    assert [claimed_job["id"] for claimed_job in claimed] == [job["id"]]
    assert store.get_mirror_breaker_progress() == {"attempts": 1, "errors": 0}


def test_atomic_claim_resets_completed_healthy_breaker_before_automatic_claim(db):
    store = SessionBridgeStore(db, clock=lambda: 100.0)
    projection = _projection(_message("auto", "automatic"))
    store.upsert_projection(projection)
    automatic = _enqueue_automatic_job(store, projection, now=100.0)
    store.accumulate_mirror_breaker_progress(attempts=2, errors=0)

    claimed = store.claim_due_jobs_with_limits(
        now=100.0,
        limit=1,
        policy=MirrorPolicy(
            automatic_creation=True,
            stop_after_attempts=2,
            stop_error_rate=0.5,
        ),
    )

    assert [job["id"] for job in claimed] == [automatic["id"]]
    assert claimed[0]["claim_authority"] == "automatic"
    assert store.get_mirror_breaker_progress() == {"attempts": 1, "errors": 0}


def test_atomic_claim_reserves_breaker_attempt_and_never_overshoots_cap(db):
    store = SessionBridgeStore(db, clock=lambda: 100.0)
    jobs = []
    for native_id in ("automatic-one", "automatic-two"):
        projection = _projection(
            _message(native_id, native_id),
            native_id=native_id,
            last_active=30.0,
        )
        store.upsert_projection(projection)
        jobs.append(_enqueue_automatic_job(store, projection, now=100.0))
    store.accumulate_mirror_breaker_progress(attempts=19, errors=0)
    policy = MirrorPolicy(
        automatic_creation=True,
        creates_per_minute=6,
        stop_after_attempts=20,
        stop_error_rate=0.01,
    )

    claimed = store.claim_due_jobs_with_limits(now=100.0, limit=6, policy=policy)

    assert len(claimed) == 1
    assert store.get_mirror_breaker_progress() == {"attempts": 20, "errors": 0}
    assert store.claim_due_jobs_with_limits(now=100.0, limit=6, policy=policy) == []

    store.retry_job(
        claimed[0]["id"],
        code="provider_failed",
        detail="fixed failure",
        next_attempt_at=120.0,
    )
    assert store.get_mirror_breaker_progress() == {"attempts": 20, "errors": 1}
    assert store.claim_due_jobs_with_limits(now=100.0, limit=6, policy=policy) == []
    assert jobs[1]["id"] in {
        row["id"]
        for row in store.list_mirror_jobs([MirrorJobState.QUEUED])
    }


def test_zero_error_threshold_resets_a_completed_error_free_batch(db):
    store = SessionBridgeStore(db, clock=lambda: 100.0)
    projection = _projection(_message("zero", "zero"), native_id="zero")
    store.upsert_projection(projection)
    automatic = _enqueue_automatic_job(store, projection, now=100.0)
    store.accumulate_mirror_breaker_progress(attempts=2, errors=0)

    claimed = store.claim_due_jobs_with_limits(
        now=100.0,
        limit=1,
        policy=MirrorPolicy(
            automatic_creation=True,
            stop_after_attempts=2,
            stop_error_rate=0,
        ),
    )

    assert [job["id"] for job in claimed] == [automatic["id"]]
    assert store.get_mirror_breaker_progress() == {"attempts": 1, "errors": 0}


def test_breaker_progress_accumulates_and_resets_across_two_stores(tmp_path):
    path = tmp_path / "shared-breaker.db"
    first_db = SessionDB(path)
    second_db = SessionDB(path)
    try:
        first = SessionBridgeStore(first_db, clock=lambda: 100.0)
        second = SessionBridgeStore(second_db, clock=lambda: 100.0)
        barrier = Barrier(2)

        def _add(store, attempts, errors):
            barrier.wait()
            return store.accumulate_mirror_breaker_progress(
                attempts=attempts, errors=errors
            )

        with ThreadPoolExecutor(max_workers=2) as executor:
            futures = [
                executor.submit(_add, first, 1, 1),
                executor.submit(_add, second, 2, 0),
            ]
            for future in futures:
                future.result()

        assert first.get_mirror_breaker_progress() == {
            "attempts": 3,
            "errors": 1,
        }
        assert second.accumulate_mirror_breaker_progress(
            attempts=0, errors=0, reset=True
        ) == {"attempts": 0, "errors": 0}
        assert first.get_mirror_breaker_progress() == {
            "attempts": 0,
            "errors": 0,
        }
    finally:
        second_db.close()
        first_db.close()


def test_breaker_progress_state_is_strict_and_never_overwritten_on_corruption(db):
    store = SessionBridgeStore(db, clock=lambda: 100.0)
    corrupt = {"version": 1, "attempts": 1, "errors": 0, "extra": True}
    store.set_state("session-bridge:mirror-breaker", corrupt)

    with pytest.raises(ValueError, match="breaker progress"):
        store.get_mirror_breaker_progress()
    with pytest.raises(ValueError, match="breaker progress"):
        store.accumulate_mirror_breaker_progress(attempts=1, errors=0)

    assert store.get_state("session-bridge:mirror-breaker") == corrupt


def test_exact_origin_bridge_lookup_is_provider_scoped(db):
    store = SessionBridgeStore(db, clock=lambda: 100.0)
    for provider, native_id in (
        (Provider.CLAUDE, "claude-target"),
        (Provider.CODEX, "codex-target"),
    ):
        store.upsert_projection(
            _projection(
                _message(native_id, native_id),
                provider=provider,
                native_id=native_id,
                origin_kind=OriginKind.BRIDGE_PLACEHOLDER,
                origin_bridge_id="bridge-shared",
            )
        )

    claude = store.find_external_session_by_origin_bridge(
        "bridge-shared", Provider.CLAUDE
    )
    codex = store.find_external_session_by_origin_bridge(
        "bridge-shared", Provider.CODEX
    )

    assert claude is not None and claude["native_id"] == "claude-target"
    assert codex is not None and codex["native_id"] == "codex-target"
    assert (
        store.find_external_session_by_origin_bridge("missing", Provider.CODEX) is None
    )


def test_exact_origin_bridge_lookup_rejects_duplicate_provenance(db):
    store = SessionBridgeStore(db, clock=lambda: 100.0)
    for native_id in ("target-a", "target-b"):
        store.upsert_projection(
            _projection(
                _message(native_id, native_id),
                provider=Provider.CODEX,
                native_id=native_id,
                origin_kind=OriginKind.BRIDGE_PLACEHOLDER,
                origin_bridge_id="bridge-duplicate",
            )
        )

    with pytest.raises(ValueError, match="duplicate bridge provenance"):
        store.find_external_session_by_origin_bridge("bridge-duplicate", Provider.CODEX)


def test_exact_origin_bridge_lookup_ignores_unauthenticated_native_rows(db):
    store = SessionBridgeStore(db, clock=lambda: 100.0)
    store.upsert_projection(
        _projection(_message("native", "native"), native_id="native-with-marker")
    )
    db._execute_write(
        lambda conn: conn.execute(
            "UPDATE external_sessions SET origin_bridge_id = ? WHERE session_id = ?",
            ("untrusted-bridge", "claude:native-with-marker"),
        )
    )

    assert (
        store.find_external_session_by_origin_bridge(
            "untrusted-bridge", Provider.CLAUDE
        )
        is None
    )


def test_guarded_public_claim_preserves_retry_order_and_cas_transitions(db):
    current_time = [100.0]
    store = SessionBridgeStore(db, clock=lambda: current_time[0])
    store.upsert_projection(_projection(_message("e1", "source")))
    first = _enqueue_manual_job(
        store, "claude:native-1", Provider.CODEX, generation=4
    )

    claimed = store.claim_due_jobs(now=100.0, limit=10, policy=MirrorPolicy())
    assert claimed[0]["state"] == "running"
    assert claimed[0]["attempts"] == 1
    assert store.claim_due_jobs(
        now=100.0, limit=10, policy=MirrorPolicy()
    ) == []

    attempt_key = f"session-bridge:attempt:{first['id']}"
    store.set_state(attempt_key, {"version": 1, "attempts": 1})
    store.retry_job(
        first["id"], code="timeout", detail="temporary", next_attempt_at=120.0
    )
    assert store.get_state(attempt_key) is None
    current_time[0] = 119.0
    assert store.claim_due_jobs(
        now=119.0, limit=10, policy=MirrorPolicy()
    ) == []
    current_time[0] = 120.0
    reclaimed = store.claim_due_jobs(
        now=120.0, limit=10, policy=MirrorPolicy()
    )
    assert reclaimed[0]["attempts"] == 2


def test_public_claim_delegation_pauses_automatic_and_claims_manual(db):
    store = SessionBridgeStore(db, clock=lambda: 100.0)
    automatic_projection = _projection(
        _message("auto", "automatic"), native_id="automatic"
    )
    manual_projection = _projection(_message("manual", "manual"), native_id="manual")
    store.upsert_projection(automatic_projection)
    store.upsert_projection(manual_projection)
    automatic = _enqueue_automatic_job(store, automatic_projection, now=100.0)
    manual = _enqueue_manual_job(store, "claude:manual", Provider.CODEX)

    claimed = store.claim_due_jobs(now=100.0, limit=1, policy=MirrorPolicy())
    rows = {row["id"]: row for row in _rows(db, "SELECT * FROM session_mirror_jobs")}

    assert [job["id"] for job in claimed] == [manual["id"]]
    assert rows[automatic["id"]]["state"] == "queued"


def test_public_claim_delegation_terminalizes_revoked_automatic_when_enabled(db):
    store = SessionBridgeStore(db, clock=lambda: 100.0)
    projection = _projection(_message("auto", "automatic"))
    store.upsert_projection(projection)
    automatic = _enqueue_automatic_job(store, projection, now=100.0)
    store.upsert_projection(
        replace(
            projection,
            origin_kind=OriginKind.BRIDGE_PLACEHOLDER,
            origin_bridge_id="bridge-late",
        )
    )

    claimed = store.claim_due_jobs(
        now=100.0,
        limit=1,
        policy=MirrorPolicy(automatic_creation=True),
    )
    durable = {
        row["id"]: row for row in _rows(db, "SELECT * FROM session_mirror_jobs")
    }[automatic["id"]]

    assert claimed == []
    assert durable["state"] == "manual_failure"
    assert durable["error_code"] == "automatic_authority_revoked"


def test_retry_state_only_allows_exact_replay_or_manual_exhaustion(db):
    store = SessionBridgeStore(db, clock=lambda: 100.0)
    store.upsert_projection(_projection(_message("e1", "source")))
    job = _enqueue_manual_job(store, "claude:native-1", Provider.CODEX)
    store.claim_due_jobs(now=100.0, limit=1, policy=MirrorPolicy())
    store.retry_job(
        job["id"], code="timeout", detail="temporary", next_attempt_at=120.0
    )

    with pytest.raises(ValueError, match="running"):
        store.retry_job(
            job["id"],
            code="different",
            detail="conflicting replay",
            next_attempt_at=130.0,
        )

    persisted = _rows(db, "SELECT * FROM session_mirror_jobs")[0]
    assert persisted["state"] == "retry"
    assert persisted["error_code"] == "timeout"
    assert persisted["next_attempt_at"] == 120.0

    store.fail_job_manually(job["id"], code="exhausted", detail="operator required")
    terminal = _rows(db, "SELECT * FROM session_mirror_jobs")[0]
    assert terminal["state"] == "manual_failure"
    assert terminal["error_code"] == "exhausted"


def test_completing_a_job_records_target_and_idempotent_mirror_link(db):
    store = SessionBridgeStore(db, clock=lambda: 100.0)
    store.upsert_projection(_projection(_message("s1", "source")))
    store.upsert_projection(
        _projection(
            _message("t1", "target"),
            provider=Provider.CODEX,
            native_id="  target-1  ",
            origin_kind=OriginKind.BRIDGE_PLACEHOLDER,
            origin_bridge_id="bridge-1",
        )
    )
    job = _enqueue_manual_job(store, "claude:native-1", Provider.CODEX)
    store.claim_due_jobs(now=100.0, limit=1, policy=MirrorPolicy())

    store.complete_job(
        job["id"],
        target_native_id="  target-1  ",
        target_session_id="codex:target-1",
        bridge_id="bridge-1",
    )
    store.complete_job(
        job["id"],
        target_native_id="  target-1  ",
        target_session_id="codex:target-1",
        bridge_id="bridge-1",
    )

    completed = _rows(db, "SELECT * FROM session_mirror_jobs")[0]
    assert completed["state"] == "succeeded"
    assert completed["target_native_id"] == "target-1"
    links = _rows(db, "SELECT * FROM session_links")
    assert len(links) == 1
    assert links[0]["relation"] == "mirrors"
    assert links[0]["bridge_id"] == "bridge-1"


def test_completion_requires_running_state_and_rolls_back(db):
    store = SessionBridgeStore(db, clock=lambda: 100.0)
    store.upsert_projection(_projection(_message("s1", "source")))
    store.upsert_projection(
        _projection(
            _message("t1", "target"),
            provider=Provider.CODEX,
            native_id="target-1",
            origin_kind=OriginKind.BRIDGE_PLACEHOLDER,
            origin_bridge_id="bridge-1",
        )
    )
    job = store.enqueue_mirror_job(
        "claude:native-1", Provider.CODEX, policy_generation=1
    )

    with pytest.raises(ValueError, match="running"):
        store.complete_job(
            job["id"],
            target_native_id="target-1",
            target_session_id="codex:target-1",
            bridge_id="bridge-1",
        )

    persisted = _rows(db, "SELECT * FROM session_mirror_jobs")[0]
    assert persisted["state"] == "queued"
    assert persisted["target_native_id"] is None
    assert _rows(db, "SELECT * FROM session_links") == []


def test_completion_rejects_ordinary_target_identity_and_rolls_back(db):
    store = SessionBridgeStore(db, clock=lambda: 100.0)
    store.upsert_projection(_projection(_message("s1", "source")))
    db.create_session("codex:ordinary", "cli")
    job = _enqueue_manual_job(store, "claude:native-1", Provider.CODEX)
    store.claim_due_jobs(now=100.0, limit=1, policy=MirrorPolicy())

    with pytest.raises(ValueError, match="cataloged target"):
        store.complete_job(
            job["id"],
            target_native_id="ordinary",
            target_session_id="codex:ordinary",
            bridge_id="bridge-1",
        )

    persisted = _rows(db, "SELECT * FROM session_mirror_jobs")[0]
    assert persisted["state"] == "running"
    assert persisted["target_native_id"] is None
    assert _rows(db, "SELECT * FROM session_links") == []


@pytest.mark.parametrize(
    ("origin_kind", "origin_bridge_id"),
    [
        (OriginKind.NATIVE, None),
        (OriginKind.BRIDGE_PLACEHOLDER, "bridge-other"),
    ],
)
def test_completion_requires_authenticated_exact_bridge_provenance(
    db, origin_kind, origin_bridge_id
):
    store = SessionBridgeStore(db, clock=lambda: 100.0)
    store.upsert_projection(_projection(_message("source", "source")))
    store.upsert_projection(
        _projection(
            _message("target", "target"),
            provider=Provider.CODEX,
            native_id="target-1",
            origin_kind=origin_kind,
            origin_bridge_id=origin_bridge_id,
        )
    )
    job = _enqueue_manual_job(store, "claude:native-1", Provider.CODEX)
    store.claim_due_jobs(now=100.0, limit=1, policy=MirrorPolicy())

    with pytest.raises(ValueError, match="bridge provenance"):
        store.complete_job(
            job["id"],
            target_native_id="target-1",
            target_session_id="codex:target-1",
            bridge_id="bridge-1",
        )

    persisted = _rows(db, "SELECT * FROM session_mirror_jobs")[0]
    assert persisted["state"] == "running"
    assert persisted["target_native_id"] is None
    assert _rows(db, "SELECT * FROM session_links") == []


def test_conflicting_completion_replay_cannot_retarget_succeeded_job(db):
    store = SessionBridgeStore(db, clock=lambda: 100.0)
    store.upsert_projection(_projection(_message("s1", "source")))
    for native_id in ("target-1", "target-2"):
        store.upsert_projection(
            _projection(
                _message(f"event-{native_id}", native_id),
                provider=Provider.CODEX,
                native_id=native_id,
                origin_kind=OriginKind.BRIDGE_PLACEHOLDER,
                origin_bridge_id=f"bridge-{native_id[-1]}",
            )
        )
    job = _enqueue_manual_job(store, "claude:native-1", Provider.CODEX)
    store.claim_due_jobs(now=100.0, limit=1, policy=MirrorPolicy())
    store.complete_job(
        job["id"],
        target_native_id="target-1",
        target_session_id="codex:target-1",
        bridge_id="bridge-1",
    )

    with pytest.raises(ValueError, match="conflicting completion"):
        store.complete_job(
            job["id"],
            target_native_id="target-2",
            target_session_id="codex:target-2",
            bridge_id="bridge-2",
        )

    persisted = _rows(db, "SELECT * FROM session_mirror_jobs")[0]
    assert persisted["target_native_id"] == "target-1"
    assert [row["bridge_id"] for row in _rows(db, "SELECT * FROM session_links")] == [
        "bridge-1"
    ]


def test_terminal_jobs_cannot_be_retried_or_overwritten(db):
    store = SessionBridgeStore(db, clock=lambda: 100.0)
    store.upsert_projection(_projection(_message("s1", "source")))
    store.upsert_projection(
        _projection(
            _message("t1", "target"),
            provider=Provider.CODEX,
            native_id="target-1",
            origin_kind=OriginKind.BRIDGE_PLACEHOLDER,
            origin_bridge_id="bridge-1",
        )
    )
    succeeded = _enqueue_manual_job(store, "claude:native-1", Provider.CODEX)
    store.claim_due_jobs(now=100.0, limit=1, policy=MirrorPolicy())
    store.complete_job(
        succeeded["id"],
        target_native_id="target-1",
        target_session_id="codex:target-1",
        bridge_id="bridge-1",
    )

    with pytest.raises(ValueError, match="terminal"):
        store.retry_job(
            succeeded["id"],
            code="late-timeout",
            detail="must not revive",
            next_attempt_at=200.0,
        )
    with pytest.raises(ValueError, match="terminal"):
        store.fail_job_manually(
            succeeded["id"], code="late-failure", detail="must not overwrite"
        )

    store.upsert_projection(
        _projection(_message("s2", "terminal"), native_id="native-terminal")
    )
    terminal = _enqueue_manual_job(
        store, "claude:native-terminal", Provider.CODEX
    )
    store.claim_due_jobs(now=100.0, limit=1, policy=MirrorPolicy())
    store.fail_job_manually(terminal["id"], code="manual", detail="operator required")
    store.fail_job_manually(terminal["id"], code="manual", detail="operator required")
    with pytest.raises(ValueError, match="terminal"):
        store.retry_job(
            terminal["id"],
            code="retry",
            detail="must not revive",
            next_attempt_at=300.0,
        )

    rows = {row["id"]: row for row in _rows(db, "SELECT * FROM session_mirror_jobs")}
    assert rows[succeeded["id"]]["state"] == "succeeded"
    assert rows[succeeded["id"]]["error_code"] is None
    assert rows[terminal["id"]]["state"] == "manual_failure"
    assert rows[terminal["id"]]["error_code"] == "manual"


def test_links_are_unique_and_divergence_is_marked(db):
    store = SessionBridgeStore(db, clock=lambda: 100.0)
    store.upsert_projection(_projection(_message("s1", "source")))
    store.upsert_projection(
        _projection(
            _message("t1", "target"), provider=Provider.CODEX, native_id="target-1"
        )
    )
    link = SessionLink(
        id="link-1",
        from_session_id="claude:native-1",
        to_session_id="codex:target-1",
        relation=Relation.CONTINUES,
        bridge_id="bridge-1",
        source_cursor="cursor-1",
        source_hash="hash-1",
        created_at=50.0,
    )

    first = store.create_link(link)
    replay = store.create_link(replace(link, id="link-duplicate"))
    store.mark_diverged("bridge-1", at=123.0)

    assert replay == first
    links = _rows(db, "SELECT * FROM session_links")
    assert len(links) == 1
    assert links[0]["id"] == "link-1"
    assert links[0]["diverged_at"] == 123.0


def test_mirror_link_transitions_to_continues_from_an_immutable_pack(db):
    store = SessionBridgeStore(db, clock=lambda: 100.0)
    store.upsert_projection(_projection(_message("source", "source")))
    store.upsert_projection(
        _projection(
            _message("target", "target"),
            provider=Provider.CODEX,
            native_id="target-1",
            cursor="target-cursor",
            native_hash="target-hash",
        )
    )
    store.create_link(
        SessionLink(
            id="link-mirror",
            from_session_id="claude:native-1",
            to_session_id="codex:target-1",
            relation=Relation.MIRRORS,
            bridge_id="bridge-1",
            source_cursor=None,
            source_hash=None,
            created_at=50.0,
        )
    )
    store.put_context_pack(
        ContextPack(
            id="pack-1",
            bridge_id="bridge-1",
            source_session_id="claude:native-1",
            target_session_id="codex:target-1",
            source_cursor="cursor-snapshot",
            source_hash="hash-snapshot",
            budget_chars=4000,
            payload="immutable handoff",
            created_at=60.0,
            immutable_at=90.0,
        )
    )

    transitioned = store.transition_link_to_continues(
        "bridge-1",
        pack_id="pack-1",
        target_cursor="target-cursor",
        target_hash="target-hash",
    )
    replay = store.transition_link_to_continues(
        "bridge-1",
        pack_id="pack-1",
        target_cursor="target-cursor",
        target_hash="target-hash",
    )

    assert replay == transitioned
    assert transitioned["id"] == "link-mirror"
    assert transitioned["relation"] == "continues"
    assert transitioned["source_cursor"] == "cursor-snapshot"
    assert transitioned["source_hash"] == "hash-snapshot"
    assert transitioned["hydrated_at"] == 100.0
    assert store.get_context_pack("bridge-1", budget_chars=4000)[
        "immutable_at"
    ] == 90.0
    assert store.get_continuation_snapshot("bridge-1") == {
        "version": 1,
        "pack_id": "pack-1",
        "source_session_id": "claude:native-1",
        "source_cursor": "cursor-snapshot",
        "source_hash": "hash-snapshot",
        "target_session_id": "codex:target-1",
        "target_cursor": "target-cursor",
        "target_hash": "target-hash",
    }
    assert len(_rows(db, "SELECT * FROM session_links")) == 1


def test_mirror_link_transition_atomically_freezes_and_hydrates_mutable_pack(db):
    current_time = [100.0]
    store = SessionBridgeStore(db, clock=lambda: current_time[0])
    store.upsert_projection(_projection(_message("source", "source")))
    store.upsert_projection(
        _projection(
            _message("target", "target"),
            provider=Provider.CODEX,
            native_id="target-1",
            cursor="target-cursor",
            native_hash="target-hash",
        )
    )
    store.create_link(
        SessionLink(
            id="link-mirror",
            from_session_id="claude:native-1",
            to_session_id="codex:target-1",
            relation=Relation.MIRRORS,
            bridge_id="bridge-1",
            source_cursor=None,
            source_hash=None,
            created_at=50.0,
        )
    )
    store.put_context_pack(
        ContextPack(
            id="pack-mutable",
            bridge_id="bridge-1",
            source_session_id="claude:native-1",
            target_session_id="codex:target-1",
            source_cursor="cursor-snapshot",
            source_hash="hash-snapshot",
            budget_chars=4000,
            payload="not frozen",
            created_at=60.0,
        )
    )

    transitioned = store.transition_link_to_continues(
        "bridge-1",
        pack_id="pack-mutable",
        target_cursor="target-cursor",
        target_hash="target-hash",
    )
    current_time[0] = 200.0
    replay = store.transition_link_to_continues(
        "bridge-1",
        pack_id="pack-mutable",
        target_cursor="target-cursor",
        target_hash="target-hash",
    )

    assert replay == transitioned
    assert transitioned["relation"] == "continues"
    assert transitioned["source_cursor"] == "cursor-snapshot"
    assert transitioned["source_hash"] == "hash-snapshot"
    assert transitioned["hydrated_at"] == 100.0
    pack = store.get_context_pack("bridge-1", budget_chars=4000)
    assert pack is not None and pack["immutable_at"] == 100.0


def test_mirror_link_transition_rejects_pack_link_identity_mismatch(db):
    store = SessionBridgeStore(db, clock=lambda: 100.0)
    store.upsert_projection(_projection(_message("source", "source")))
    for native_id in ("target-a", "target-b"):
        store.upsert_projection(
            _projection(
                _message(native_id, native_id),
                provider=Provider.CODEX,
                native_id=native_id,
                cursor="target-cursor",
                native_hash="target-hash",
            )
        )
    store.create_link(
        SessionLink(
            id="link-mirror",
            from_session_id="claude:native-1",
            to_session_id="codex:target-a",
            relation=Relation.MIRRORS,
            bridge_id="bridge-1",
            source_cursor=None,
            source_hash=None,
            created_at=50.0,
        )
    )
    store.put_context_pack(
        ContextPack(
            id="pack-wrong-target",
            bridge_id="bridge-1",
            source_session_id="claude:native-1",
            target_session_id="codex:target-b",
            source_cursor="cursor-snapshot",
            source_hash="hash-snapshot",
            budget_chars=4000,
            payload="wrong target",
            created_at=60.0,
        )
    )

    with pytest.raises(ValueError, match="identity"):
        store.transition_link_to_continues(
            "bridge-1",
            pack_id="pack-wrong-target",
            target_cursor="target-cursor",
            target_hash="target-hash",
        )

    assert _rows(
        db,
        "SELECT relation, source_cursor, source_hash, hydrated_at FROM session_links",
    ) == [
        {
            "relation": "mirrors",
            "source_cursor": None,
            "source_hash": None,
            "hydrated_at": None,
        }
    ]
    pack = store.get_context_pack("bridge-1", budget_chars=4000)
    assert pack is not None and pack["immutable_at"] is None


def test_mirror_link_transition_rejects_conflicting_continues_row(db):
    store = SessionBridgeStore(db, clock=lambda: 100.0)
    store.upsert_projection(_projection(_message("source", "source")))
    store.upsert_projection(
        _projection(
            _message("target", "target"),
            provider=Provider.CODEX,
            native_id="target-1",
            cursor="target-cursor",
            native_hash="target-hash",
        )
    )
    for link_id, relation, cursor, source_hash in (
        ("link-mirror", Relation.MIRRORS, None, None),
        ("link-continues", Relation.CONTINUES, "other-cursor", "other-hash"),
    ):
        store.create_link(
            SessionLink(
                id=link_id,
                from_session_id="claude:native-1",
                to_session_id="codex:target-1",
                relation=relation,
                bridge_id="bridge-1",
                source_cursor=cursor,
                source_hash=source_hash,
                created_at=50.0,
            )
        )
    store.put_context_pack(
        ContextPack(
            id="pack-1",
            bridge_id="bridge-1",
            source_session_id="claude:native-1",
            target_session_id="codex:target-1",
            source_cursor="cursor-snapshot",
            source_hash="hash-snapshot",
            budget_chars=4000,
            payload="immutable handoff",
            created_at=60.0,
        )
    )

    with pytest.raises(ValueError, match="conflicting continues"):
        store.transition_link_to_continues(
            "bridge-1",
            pack_id="pack-1",
            target_cursor="target-cursor",
            target_hash="target-hash",
        )

    assert {
        (row["id"], row["relation"], row["source_cursor"])
        for row in _rows(db, "SELECT * FROM session_links")
    } == {
        ("link-mirror", "mirrors", None),
        ("link-continues", "continues", "other-cursor"),
    }
    pack = store.get_context_pack("bridge-1", budget_chars=4000)
    assert pack is not None and pack["immutable_at"] is None


def test_continuation_target_baseline_conflict_rolls_back_exact_state(db):
    current_time = [100.0]
    store = SessionBridgeStore(db, clock=lambda: current_time[0])
    store.upsert_projection(_projection(_message("source", "source")))
    store.upsert_projection(
        _projection(
            _message("target", "target"),
            provider=Provider.CODEX,
            native_id="target-1",
            cursor="target-cursor",
            native_hash="target-hash",
        )
    )
    store.create_link(
        SessionLink(
            id="link-mirror",
            from_session_id="claude:native-1",
            to_session_id="codex:target-1",
            relation=Relation.MIRRORS,
            bridge_id="bridge-1",
            source_cursor=None,
            source_hash=None,
            created_at=50.0,
        )
    )
    store.put_context_pack(
        ContextPack(
            id="pack-1",
            bridge_id="bridge-1",
            source_session_id="claude:native-1",
            target_session_id="codex:target-1",
            source_cursor="source-cursor",
            source_hash="source-hash",
            budget_chars=4000,
            payload="handoff",
            created_at=60.0,
        )
    )
    store.transition_link_to_continues(
        "bridge-1",
        pack_id="pack-1",
        target_cursor="target-cursor",
        target_hash="target-hash",
    )
    before_pack = store.get_context_pack("bridge-1", budget_chars=4000)
    before_link = _rows(db, "SELECT * FROM session_links")[0]
    before_snapshot = store.get_continuation_snapshot("bridge-1")
    current_time[0] = 200.0

    with pytest.raises(ValueError, match="target baseline"):
        store.transition_link_to_continues(
            "bridge-1",
            pack_id="pack-1",
            target_cursor="different-cursor",
            target_hash="different-hash",
        )

    assert store.get_context_pack("bridge-1", budget_chars=4000) == before_pack
    assert _rows(db, "SELECT * FROM session_links")[0] == before_link
    assert store.get_continuation_snapshot("bridge-1") == before_snapshot


def test_continuation_transition_rejects_noncurrent_target_baseline(db):
    store = SessionBridgeStore(db, clock=lambda: 100.0)
    store.upsert_projection(_projection(_message("source", "source")))
    store.upsert_projection(
        _projection(
            _message("target", "target"),
            provider=Provider.CODEX,
            native_id="target-1",
            cursor="current-target-cursor",
            native_hash="current-target-hash",
        )
    )
    store.create_link(
        SessionLink(
            id="link-mirror",
            from_session_id="claude:native-1",
            to_session_id="codex:target-1",
            relation=Relation.MIRRORS,
            bridge_id="bridge-1",
            source_cursor=None,
            source_hash=None,
            created_at=50.0,
        )
    )
    store.put_context_pack(
        ContextPack(
            id="pack-1",
            bridge_id="bridge-1",
            source_session_id="claude:native-1",
            target_session_id="codex:target-1",
            source_cursor="source-cursor",
            source_hash="source-hash",
            budget_chars=4000,
            payload="handoff",
            created_at=60.0,
        )
    )

    with pytest.raises(ValueError, match="cataloged target snapshot"):
        store.transition_link_to_continues(
            "bridge-1",
            pack_id="pack-1",
            target_cursor="stale-target-cursor",
            target_hash="stale-target-hash",
        )

    pack = store.get_context_pack("bridge-1", budget_chars=4000)
    assert pack is not None and pack["immutable_at"] is None
    link = _rows(db, "SELECT * FROM session_links")[0]
    assert link["relation"] == "mirrors" and link["hydrated_at"] is None
    assert store.get_continuation_snapshot("bridge-1") is None


@pytest.mark.parametrize(
    "invalid_snapshot",
    [
        {"version": 2},
        {
            "version": 1,
            "pack_id": "pack-1",
            "source_session_id": "claude:source",
            "source_cursor": "source-cursor",
            "source_hash": "source-hash",
            "target_session_id": "codex:target",
            "target_cursor": "target-cursor",
            "target_hash": "",
        },
    ],
)
def test_continuation_snapshot_getter_rejects_invalid_schema(db, invalid_snapshot):
    store = SessionBridgeStore(db, clock=lambda: 100.0)
    store.set_state("session-bridge:continuation:bridge-invalid", invalid_snapshot)

    with pytest.raises(ValueError, match="continuation snapshot"):
        store.get_continuation_snapshot("bridge-invalid")


def test_continuation_snapshot_getter_rejects_false_durable_identity(db):
    store = SessionBridgeStore(db, clock=lambda: 100.0)
    store.set_state(
        "session-bridge:continuation:bridge-false",
        {
            "version": 1,
            "pack_id": "pack-missing",
            "source_session_id": "claude:source",
            "source_cursor": "source-cursor",
            "source_hash": "source-hash",
            "target_session_id": "codex:target",
            "target_cursor": "target-cursor",
            "target_hash": "target-hash",
        },
    )

    with pytest.raises(ValueError, match="durable identity"):
        store.get_continuation_snapshot("bridge-false")


def test_continuation_snapshot_listing_is_empty_and_strictly_bounded(db):
    store = SessionBridgeStore(db, clock=lambda: 100.0)

    assert store.list_continuation_snapshots() == []
    with pytest.raises(ValueError, match="between 1 and 1000"):
        store.list_continuation_snapshots(limit=0)
    with pytest.raises(ValueError, match="between 1 and 1000"):
        store.list_continuation_snapshots(limit=1001)


def test_continuation_snapshot_listing_is_deterministic_and_bounded(db):
    store = SessionBridgeStore(db, clock=lambda: 100.0)
    for suffix in ("b", "a"):
        source_id = f"source-{suffix}"
        target_id = f"target-{suffix}"
        bridge_id = f"bridge-{suffix}"
        store.upsert_projection(
            _projection(
                _message(f"source-event-{suffix}", "source"), native_id=source_id
            )
        )
        store.upsert_projection(
            _projection(
                _message(f"target-event-{suffix}", "target"),
                provider=Provider.CODEX,
                native_id=target_id,
                cursor=f"target-cursor-{suffix}",
                native_hash=f"target-hash-{suffix}",
            )
        )
        store.create_link(
            SessionLink(
                id=f"link-{suffix}",
                from_session_id=f"claude:{source_id}",
                to_session_id=f"codex:{target_id}",
                relation=Relation.MIRRORS,
                bridge_id=bridge_id,
                source_cursor=None,
                source_hash=None,
                created_at=50.0,
            )
        )
        store.put_context_pack(
            ContextPack(
                id=f"pack-{suffix}",
                bridge_id=bridge_id,
                source_session_id=f"claude:{source_id}",
                target_session_id=f"codex:{target_id}",
                source_cursor=f"source-cursor-{suffix}",
                source_hash=f"source-hash-{suffix}",
                budget_chars=4000,
                payload="handoff",
                created_at=60.0,
            )
        )
        store.transition_link_to_continues(
            bridge_id,
            pack_id=f"pack-{suffix}",
            target_cursor=f"target-cursor-{suffix}",
            target_hash=f"target-hash-{suffix}",
        )

    snapshots = store.list_continuation_snapshots(limit=2)

    assert [snapshot["bridge_id"] for snapshot in snapshots] == [
        "bridge-a",
        "bridge-b",
    ]
    assert store.list_continuation_snapshots(limit=1) == [snapshots[0]]
    assert set(snapshots[0]) == {
        "bridge_id",
        "version",
        "pack_id",
        "source_session_id",
        "source_cursor",
        "source_hash",
        "target_session_id",
        "target_cursor",
        "target_hash",
    }


def test_continuation_snapshot_listing_fails_closed_on_corruption(db):
    store = SessionBridgeStore(db, clock=lambda: 100.0)
    store.set_state("session-bridge:continuation:bridge-corrupt", {"version": 2})

    with pytest.raises(ValueError, match="continuation snapshot"):
        store.list_continuation_snapshots()


def test_continuation_snapshot_listing_paginates_past_one_thousand(db):
    store = SessionBridgeStore(db, clock=lambda: 100.0)
    _seed_continuation_snapshot_rows(db, 1002)

    first = store.list_continuation_snapshots(limit=1000)
    second = store.list_continuation_snapshots(
        limit=1000, after_bridge_id=first[-1]["bridge_id"]
    )

    assert len(first) == 1000
    assert first[0]["bridge_id"] == "bridge-0000"
    assert first[-1]["bridge_id"] == "bridge-0999"
    assert [snapshot["bridge_id"] for snapshot in second] == [
        "bridge-1000",
        "bridge-1001",
    ]


def test_continuation_snapshot_listing_rejects_noncanonical_cursor_and_state_key(db):
    store = SessionBridgeStore(db, clock=lambda: 100.0)
    _seed_continuation_snapshot_rows(db, 1)
    with pytest.raises(ValueError, match="after bridge ID"):
        store.list_continuation_snapshots(after_bridge_id=" bridge-0000 ")

    valid = store.get_state("session-bridge:continuation:bridge-0000")
    assert valid is not None
    store.set_state("session-bridge:continuation: bridge-0000 ", valid)

    with pytest.raises(ValueError, match="canonical bridge ID"):
        store.list_continuation_snapshots()


@pytest.mark.parametrize(
    ("target_cursor", "target_hash", "message"),
    [("", "target-hash", "target cursor"), ("target-cursor", " ", "target hash")],
)
def test_continuation_transition_requires_nonempty_target_baseline(
    db, target_cursor, target_hash, message
):
    store = SessionBridgeStore(db, clock=lambda: 100.0)

    with pytest.raises(ValueError, match=message):
        store.transition_link_to_continues(
            "bridge-1",
            pack_id="pack-1",
            target_cursor=target_cursor,
            target_hash=target_hash,
        )


def test_session_launch_metadata_returns_only_title_and_cwd(db):
    store = SessionBridgeStore(db, clock=lambda: 100.0)
    store.upsert_projection(_projection(_message("event", "message")))

    metadata = store.get_session_launch_metadata("claude:native-1")

    assert metadata == {
        "title": "claude session",
        "cwd": "C:/workspace/project",
    }
    assert "native_path" not in metadata
    assert store.get_session_launch_metadata("missing") is None


def test_session_launch_metadata_validates_input_and_persisted_types(db):
    store = SessionBridgeStore(db, clock=lambda: 100.0)
    with pytest.raises(ValueError, match="session ID"):
        store.get_session_launch_metadata(" ")

    store.upsert_projection(_projection(_message("event", "message")))
    db._execute_write(
        lambda conn: conn.execute(
            "UPDATE sessions SET title = ? WHERE id = ?",
            (b"invalid-title", "claude:native-1"),
        )
    )

    with pytest.raises(ValueError, match="launch metadata"):
        store.get_session_launch_metadata("claude:native-1")


def test_hydrated_context_packs_become_immutable(db):
    store = SessionBridgeStore(db, clock=lambda: 100.0)
    store.upsert_projection(_projection(_message("s1", "source")))
    store.upsert_projection(
        _projection(
            _message("t1", "target"), provider=Provider.CODEX, native_id="target-1"
        )
    )
    store.create_link(
        SessionLink(
            id="link-1",
            from_session_id="claude:native-1",
            to_session_id="codex:target-1",
            relation=Relation.CONTINUES,
            bridge_id="bridge-1",
            source_cursor="cursor-1",
            source_hash="hash-1",
            created_at=50.0,
        )
    )
    pack = ContextPack(
        id="pack-1",
        bridge_id="bridge-1",
        source_session_id="claude:native-1",
        target_session_id="codex:target-1",
        source_cursor="cursor-1",
        source_hash="hash-1",
        budget_chars=4000,
        payload="original snapshot",
        created_at=60.0,
    )
    store.put_context_pack(pack)

    store.mark_hydrated(
        "bridge-1", source_cursor="cursor-1", source_hash="hash-1", pack_id="pack-1"
    )
    replay = store.put_context_pack(
        replace(pack, id="pack-2", payload="mutated snapshot", created_at=70.0)
    )

    assert replay["id"] == "pack-1"
    persisted = store.get_context_pack("bridge-1", budget_chars=4000)
    assert persisted["payload"] == "original snapshot"
    assert persisted["immutable_at"] == 100.0
    assert _rows(db, "SELECT hydrated_at FROM session_links")[0]["hydrated_at"] == 100.0


def test_context_pack_semantic_replay_rejects_source_identity_mismatch(db):
    store = SessionBridgeStore(db, clock=lambda: 100.0)
    for native_id in ("source-a", "source-b"):
        store.upsert_projection(
            _projection(_message(f"event-{native_id}", native_id), native_id=native_id)
        )
    pack = ContextPack(
        id="pack-a",
        bridge_id="bridge-1",
        source_session_id="claude:source-a",
        target_session_id=None,
        source_cursor="cursor-1",
        source_hash="hash-1",
        budget_chars=4000,
        payload="source A payload",
        created_at=60.0,
    )
    store.put_context_pack(pack)

    with pytest.raises(ValueError, match="source identity"):
        store.put_context_pack(
            replace(
                pack,
                id="pack-b",
                source_session_id="claude:source-b",
                payload="source B payload",
            )
        )

    persisted = store.get_context_pack("bridge-1", budget_chars=4000)
    assert persisted["source_session_id"] == "claude:source-a"
    assert persisted["payload"] == "source A payload"


def test_context_pack_replay_rejects_conflicting_non_null_target(db):
    store = SessionBridgeStore(db, clock=lambda: 100.0)
    store.upsert_projection(_projection(_message("source", "source")))
    for native_id in ("target-a", "target-b"):
        store.upsert_projection(
            _projection(
                _message(f"event-{native_id}", native_id),
                provider=Provider.CODEX,
                native_id=native_id,
            )
        )
    pack = ContextPack(
        id="pack-a",
        bridge_id="bridge-1",
        source_session_id="claude:native-1",
        target_session_id="codex:target-a",
        source_cursor="cursor-1",
        source_hash="hash-1",
        budget_chars=4000,
        payload="target A payload",
        created_at=60.0,
    )
    store.put_context_pack(pack)

    with pytest.raises(ValueError, match="target identity"):
        store.put_context_pack(
            replace(
                pack,
                id="pack-b",
                target_session_id="codex:target-b",
                payload="target B payload",
            )
        )

    persisted = store.get_context_pack("bridge-1", budget_chars=4000)
    assert persisted["target_session_id"] == "codex:target-a"
    assert persisted["payload"] == "target A payload"


def test_mark_hydrated_rejects_orphan_pack_without_locking_it(db):
    store = SessionBridgeStore(db, clock=lambda: 100.0)
    store.upsert_projection(_projection(_message("source", "source")))
    store.upsert_projection(
        _projection(
            _message("target", "target"),
            provider=Provider.CODEX,
            native_id="target-1",
        )
    )
    pack = ContextPack(
        id="orphan-pack",
        bridge_id="bridge-orphan",
        source_session_id="claude:native-1",
        target_session_id="codex:target-1",
        source_cursor="cursor-1",
        source_hash="hash-1",
        budget_chars=4000,
        payload="orphan",
        created_at=60.0,
    )
    store.put_context_pack(pack)

    with pytest.raises(ValueError, match="matching link"):
        store.mark_hydrated(
            "bridge-orphan",
            source_cursor="cursor-1",
            source_hash="hash-1",
            pack_id="orphan-pack",
        )

    assert (
        store.get_context_pack("bridge-orphan", budget_chars=4000)["immutable_at"]
        is None
    )


def test_mark_hydrated_rejects_pack_whose_identity_does_not_match_link(db):
    store = SessionBridgeStore(db, clock=lambda: 100.0)
    for native_id in ("source-a", "source-b"):
        store.upsert_projection(
            _projection(_message(f"event-{native_id}", native_id), native_id=native_id)
        )
    store.upsert_projection(
        _projection(
            _message("target", "target"),
            provider=Provider.CODEX,
            native_id="target-1",
        )
    )
    store.create_link(
        SessionLink(
            id="link-1",
            from_session_id="claude:source-a",
            to_session_id="codex:target-1",
            relation=Relation.CONTINUES,
            bridge_id="bridge-1",
            source_cursor="cursor-1",
            source_hash="hash-1",
            created_at=50.0,
        )
    )
    mismatched = ContextPack(
        id="pack-mismatch",
        bridge_id="bridge-1",
        source_session_id="claude:source-b",
        target_session_id="codex:target-1",
        source_cursor="cursor-1",
        source_hash="hash-1",
        budget_chars=4000,
        payload="wrong source",
        created_at=60.0,
    )
    store.put_context_pack(mismatched)

    with pytest.raises(ValueError, match="matching link"):
        store.mark_hydrated(
            "bridge-1",
            source_cursor="cursor-1",
            source_hash="hash-1",
            pack_id="pack-mismatch",
        )

    assert store.get_context_pack("bridge-1", budget_chars=4000)["immutable_at"] is None


def test_state_values_are_canonical_snapshots_on_write_and_read(db):
    store = SessionBridgeStore(db, clock=lambda: 100.0)
    value = {"z": [1, {"nested": True}], "a": {"value": "kept"}}

    store.set_state("scan", value)
    value["z"][1]["nested"] = False
    first_read = store.get_state("scan")
    first_read["a"]["value"] = "changed"

    assert store.get_state("scan") == {
        "a": {"value": "kept"},
        "z": [1, {"nested": True}],
    }
    raw_json = _rows(db, "SELECT value_json FROM session_bridge_state")[0]["value_json"]
    assert raw_json == json.dumps(
        {"a": {"value": "kept"}, "z": [1, {"nested": True}]},
        sort_keys=True,
        separators=(",", ":"),
    )


def test_bridge_summaries_are_batched_and_include_catalog_state(db):
    store = SessionBridgeStore(db, clock=lambda: 100.0)
    store.upsert_projection(_projection(_message("e1", "source")))
    db.create_session("ordinary", "cli")

    summaries = store.get_bridge_summaries([
        "claude:native-1",
        "ordinary",
        "missing",
        "claude:native-1",
    ])

    assert set(summaries) == {"claude:native-1", "ordinary"}
    assert summaries["claude:native-1"]["bridge_provider"] == "claude"
    assert summaries["claude:native-1"]["bridge_mirror_state"] == "catalog_only"
    assert summaries["ordinary"] == {
        "bridge_provider": "hermes",
        "bridge_mirror_state": None,
    }


def test_native_hermes_snapshot_identity_is_stable_and_tracks_messages(db):
    store = SessionBridgeStore(db)
    db.create_session("hermes-native", "tui", cwd="C:/workspace/project")
    db.append_message(
        "hermes-native",
        "user",
        "continue this native Hermes session",
        timestamp=100.0,
    )

    first = store.get_native_session_snapshot("hermes-native")
    replay = store.get_native_session_snapshot("hermes-native")

    assert first == replay
    assert first is not None
    assert first["session_id"] == "hermes-native"
    assert first["provider"] == Provider.HERMES.value
    assert first["cursor"].startswith("hermes:")
    assert len(first["source_hash"]) == 64

    db.append_message(
        "hermes-native",
        "assistant",
        "native continuation ready",
        timestamp=101.0,
    )
    advanced = store.get_native_session_snapshot("hermes-native")

    assert advanced is not None
    assert advanced["cursor"] != first["cursor"]
    assert advanced["source_hash"] != first["source_hash"]


def test_named_profile_hermes_session_is_a_sidebar_candidate_and_snapshot(db, tmp_path):
    profile_path = tmp_path / "profiles" / "main" / "state.db"
    profile_path.parent.mkdir(parents=True)
    profile_db = SessionDB(profile_path)
    try:
        profile_db.create_session(
            "hermes-profile-native",
            "tui",
            cwd="C:/workspace/profile-project",
        )
        profile_db.append_message(
            "hermes-profile-native",
            "user",
            "ship the cross-profile sidebar bridge",
            timestamp=100.0,
        )
        profile_db._execute_write(
            lambda conn: (
                conn.execute("DROP TABLE external_sessions"),
                conn.execute("DROP TABLE session_links"),
            )
        )
    finally:
        profile_db.close()

    store = SessionBridgeStore(
        db,
        hermes_profile_db_paths=lambda: (("main", profile_path),),
    )

    page = store.list_sidebar_candidates(after=0.0, limit=10)
    snapshot = store.get_native_session_snapshot("hermes-profile-native")
    metadata = store.get_session_launch_metadata("hermes-profile-native")

    assert [source.source_session_id for source in page] == ["hermes-profile-native"]
    assert page[0].projection.messages[0].content == (
        "ship the cross-profile sidebar bridge"
    )
    assert snapshot is not None
    assert snapshot["session_id"] == "hermes-profile-native"
    assert snapshot["profile"] == "main"
    assert metadata == {
        "title": None,
        "cwd": "C:/workspace/profile-project",
        "profile": "main",
    }

    candidate = SidebarCandidate(
        source_session_id="hermes-profile-native",
        provider=Provider.HERMES,
        bridge_id=sidebar_bridge_id("hermes-profile-native"),
        title="[Hermes] ship the cross-profile sidebar bridge",
        cwd="C:/workspace/profile-project",
        git_root=None,
        git_branch=None,
        git_head=None,
        worktree_id=None,
        eligible_at=100.0,
    )
    queued = store.enqueue_sidebar_job(candidate)
    assert queued["created"] is True
    assert _rows(db, "SELECT source FROM sessions WHERE id = ?", (
        "hermes-profile-native",
    )) == [{"source": "session_bridge_profile"}]

    db._execute_write(
        lambda conn: (
            conn.execute(
                "INSERT INTO sessions (id, source, started_at) VALUES (?, ?, ?)",
                ("codex:profile-target", "codex", 101.0),
            ),
            conn.execute(
                """INSERT INTO session_links (
                       id, from_session_id, to_session_id, relation, bridge_id,
                       created_at, hydrated_at
                   ) VALUES (?, ?, ?, ?, ?, ?, ?)""",
                (
                    "profile-sidebar-link",
                    "hermes-profile-native",
                    "codex:profile-target",
                    Relation.MIRRORS.value,
                    candidate.bridge_id,
                    101.0,
                    None,
                ),
            ),
        )
    )

    read = UnifiedCatalog(db, store).get("hermes-profile-native")
    assert read["session"]["profile"] == "main"
    assert read["session"]["mirror_state"] == "mirrored"
    assert read["session"]["links"] == [
        {
            "id": "profile-sidebar-link",
            "from_session_id": "hermes-profile-native",
            "to_session_id": "codex:profile-target",
            "relation": "mirrors",
            "bridge_id": candidate.bridge_id,
            "created_at": 101.0,
            "hydrated_at": None,
            "diverged_at": None,
        }
    ]
    assert read["messages"][0]["content"] == (
        "ship the cross-profile sidebar bridge"
    )


def test_native_hermes_snapshot_canonicalizes_binary_and_nonfinite_content(db):
    store = SessionBridgeStore(db)
    db.create_session("hermes-structured", "tui")
    db.append_message(
        "hermes-structured",
        "user",
        b"binary content",
        timestamp=100.0,
    )
    db.append_message(
        "hermes-structured",
        "assistant",
        [float("nan"), float("inf"), float("-inf")],
        timestamp=101.0,
    )

    first = store.get_native_session_snapshot("hermes-structured")
    replay = store.get_native_session_snapshot("hermes-structured")

    assert first == replay
    assert first is not None
    assert first["cursor"].startswith("hermes:2:")
    assert len(first["source_hash"]) == 64


def test_native_hermes_snapshot_rejects_external_sessions(db):
    store = SessionBridgeStore(db)
    store.upsert_projection(_projection(_message("external", "source")))

    assert store.get_native_session_snapshot("claude:native-1") is None


def _sidebar_candidate(
    db: SessionDB,
    *,
    provider: Provider = Provider.CLAUDE,
    native_id: str = "sidebar-source",
    eligible_at: float = 100.0,
) -> SidebarCandidate:
    if provider is Provider.CLAUDE:
        source_session_id = canonical_session_id(provider, native_id)
        SessionBridgeStore(db, clock=lambda: eligible_at).upsert_projection(
            _projection(
                _message(f"message-{native_id}", "build the sidebar feature"),
                provider=provider,
                native_id=native_id,
                last_active=eligible_at,
            )
        )
    elif provider is Provider.HERMES:
        source_session_id = canonical_session_id(provider, native_id)
        db.ensure_session(source_session_id, source="cli", started_at=eligible_at)
    else:
        raise ValueError("sidebar candidate helper supports Claude or Hermes")
    return SidebarCandidate(
        source_session_id=source_session_id,
        provider=provider,
        bridge_id=sidebar_bridge_id(source_session_id),
        title=f"[{provider.value}] source",
        cwd="C:/workspace/project",
        git_root="C:/workspace/project",
        git_branch="feature/sidebar",
        git_head="a" * 40,
        worktree_id="worktree-1",
        eligible_at=eligible_at,
    )


def _token_factory(*tokens: str):
    iterator = iter(tokens)
    return lambda: next(iterator)


def _seed_sidebar_codex_target(
    store: SessionBridgeStore,
    candidate: SidebarCandidate,
    thread_id: str,
    *,
    bridge_id: str | None = None,
) -> str:
    store.upsert_projection(_projection(
        _message(f"target-{thread_id}", "Hermes Session Bridge placeholder"),
        provider=Provider.CODEX,
        native_id=thread_id,
        last_active=150.0,
        origin_kind=OriginKind.BRIDGE_PLACEHOLDER,
        origin_bridge_id=bridge_id or candidate.bridge_id,
    ))
    return f"codex:{thread_id}"


def test_sidebar_exclusion_recording_is_idempotent_counted_and_bounded(db) -> None:
    store = SessionBridgeStore(db)
    candidate = _sidebar_candidate(db)

    first = store.record_sidebar_exclusion(
        candidate.source_session_id,
        candidate.provider,
        "source_cwd_missing",
        now=125.0,
    )
    replay = store.record_sidebar_exclusion(
        candidate.source_session_id,
        candidate.provider,
        "source_cwd_missing",
        now=200.0,
    )

    expected_digest = hashlib.sha256(
        (
            candidate.source_session_id
            + "\0"
            + candidate.provider.value
            + "\0source_cwd_missing"
        ).encode("utf-8")
    ).hexdigest()
    assert first == {
        "source_session_id": candidate.source_session_id,
        "reason_code": "source_cwd_missing",
        "created": True,
    }
    assert replay == {**first, "created": False}
    assert _rows(db, "SELECT * FROM session_sidebar_exclusions") == [{
        "source_session_id": candidate.source_session_id,
        "provider": Provider.CLAUDE.value,
        "reason_code": "source_cwd_missing",
        "source_identity_digest": expected_digest,
        "excluded_at": 125.0,
        "updated_at": 125.0,
    }]
    assert SIDEBAR_EXCLUSION_REASONS == frozenset({"source_cwd_missing"})
    assert store.sidebar_exclusion_counts() == {
        "total": 1,
        "by_reason": {"source_cwd_missing": 1},
    }


def test_sidebar_delivery_status_reports_exclusions_without_degradation(db) -> None:
    store = SessionBridgeStore(db)
    candidate = _sidebar_candidate(db, native_id="status-exclusion")
    store.record_sidebar_exclusion(
        candidate.source_session_id,
        candidate.provider,
        "source_cwd_missing",
        now=125.0,
    )

    status = store.sidebar_delivery_status(now=200.0)

    assert status["counts"]["sidebar_excluded"] == 1
    assert status["recent_error_codes"] == []
    assert status["oldest_pending_age_seconds"] is None
    assert status["delivery_latency_seconds"] == {
        "p50": None,
        "p95": None,
        "p99": None,
    }


def test_sidebar_exclusion_replay_fails_closed_on_corrupted_digest(db) -> None:
    store = SessionBridgeStore(db)
    candidate = _sidebar_candidate(db, native_id="corrupt-exclusion")
    store.record_sidebar_exclusion(
        candidate.source_session_id,
        candidate.provider,
        "source_cwd_missing",
        now=125.0,
    )
    db._execute_write(
        lambda conn: conn.execute(
            "UPDATE session_sidebar_exclusions SET source_identity_digest = ?",
            ("0" * 64,),
        )
    )

    with pytest.raises(ValueError, match="conflicting sidebar exclusion"):
        store.record_sidebar_exclusion(
            candidate.source_session_id,
            candidate.provider,
            "source_cwd_missing",
            now=200.0,
        )

    assert _rows(
        db,
        "SELECT source_identity_digest, excluded_at, updated_at "
        "FROM session_sidebar_exclusions",
    ) == [{
        "source_identity_digest": "0" * 64,
        "excluded_at": 125.0,
        "updated_at": 125.0,
    }]


def test_sidebar_candidates_exclude_persisted_rows_before_limit(db) -> None:
    store = SessionBridgeStore(db)
    older = _sidebar_candidate(db, native_id="older-valid", eligible_at=100.0)
    newer = _sidebar_candidate(db, native_id="newer-excluded", eligible_at=200.0)
    store.record_sidebar_exclusion(
        newer.source_session_id,
        newer.provider,
        "source_cwd_missing",
        now=250.0,
    )

    page = store.list_sidebar_candidates(after=0.0, limit=1)

    assert [candidate.source_session_id for candidate in page] == [
        older.source_session_id
    ]
    assert page.has_more is False


def test_sidebar_enqueue_is_source_idempotent_and_preserves_one_bridge(db) -> None:
    store = SessionBridgeStore(db, clock=lambda: 125.0)
    candidate = _sidebar_candidate(db)

    first = store.enqueue_sidebar_job(candidate)
    replay = store.enqueue_sidebar_job(candidate)

    assert first["created"] is True
    assert replay["created"] is False
    assert {key: value for key, value in replay.items() if key != "created"} == {
        key: value for key, value in first.items() if key != "created"
    }
    assert first["idempotency_key"] == sidebar_idempotency_key(
        candidate.source_session_id
    )
    assert first["bridge_id"] == candidate.bridge_id
    assert first["state"] == SidebarJobState.PENDING.value
    assert first["eligible_at"] == candidate.eligible_at
    assert len(_rows(db, "SELECT * FROM session_sidebar_jobs")) == 1


def test_sidebar_worktree_snapshot_roundtrips_without_git_metadata(
    db: SessionDB,
    tmp_path,
) -> None:
    source = tmp_path / "ordinary-source"
    source.mkdir()
    snapshot = capture_worktree_snapshot(str(source))
    candidate = replace(
        _sidebar_candidate(db, native_id="ordinary-source"),
        cwd=snapshot.cwd,
        git_root=snapshot.git_root,
        git_branch=snapshot.branch,
        git_head=snapshot.head,
        worktree_id=snapshot.worktree_id,
    )
    store = SessionBridgeStore(db, clock=lambda: 125.0)

    store.enqueue_sidebar_job(candidate, worktree_snapshot=snapshot)

    assert store.get_worktree_snapshot(candidate.source_session_id) == snapshot


def test_sidebar_enqueue_persists_versioned_delivery_candidate_across_restart(
    db,
) -> None:
    store = SessionBridgeStore(db, clock=lambda: 125.0)
    candidate = _sidebar_candidate(db)
    state_key = "session-bridge:sidebar-delivery:" + hashlib.sha256(
        candidate.source_session_id.encode()
    ).hexdigest()

    store.enqueue_sidebar_job(candidate)
    row = _rows(
        db,
        "SELECT key, value_json FROM session_bridge_state WHERE key = ?",
        (state_key,),
    )[0]
    reopened_db = SessionDB(db.db_path)
    try:
        recovered = SessionBridgeStore(reopened_db).get_sidebar_candidate_for_delivery(
            candidate.source_session_id
        )
    finally:
        reopened_db.close()

    assert row["key"] == state_key
    assert json.loads(row["value_json"]) == {
        "version": 1,
        "source_session_id": candidate.source_session_id,
        "provider": candidate.provider.value,
        "bridge_id": candidate.bridge_id,
        "title": candidate.title,
        "cwd": candidate.cwd,
        "git_root": candidate.git_root,
        "git_branch": candidate.git_branch,
        "git_head": candidate.git_head,
        "worktree_id": candidate.worktree_id,
        "eligible_at": candidate.eligible_at,
    }
    assert recovered == candidate


def test_sidebar_duplicate_enqueue_never_overwrites_delivery_candidate(db) -> None:
    store = SessionBridgeStore(db, clock=lambda: 125.0)
    candidate = _sidebar_candidate(db)
    store.enqueue_sidebar_job(candidate)
    before = _rows(
        db,
        "SELECT key, value_json, updated_at FROM session_bridge_state "
        "WHERE key LIKE 'session-bridge:sidebar-delivery:%'",
    )

    replay = store.enqueue_sidebar_job(candidate)
    with pytest.raises(ValueError, match="conflicting sidebar delivery candidate"):
        store.enqueue_sidebar_job(replace(candidate, title="[Claude] different"))

    after = _rows(
        db,
        "SELECT key, value_json, updated_at FROM session_bridge_state "
        "WHERE key LIKE 'session-bridge:sidebar-delivery:%'",
    )
    assert replay["created"] is False
    assert after == before


def test_sidebar_delivery_candidate_is_redacted_at_enqueue(db) -> None:
    store = SessionBridgeStore(db, clock=lambda: 125.0)
    candidate = replace(
        _sidebar_candidate(db),
        title="[Claude] rotate sk-1234567890abcdefghijkl",
        cwd="C:/workspace?token=must-not-persist",
    )

    store.enqueue_sidebar_job(candidate)
    row = _rows(
        db,
        "SELECT value_json FROM session_bridge_state "
        "WHERE key LIKE 'session-bridge:sidebar-delivery:%'",
    )[0]
    recovered = store.get_sidebar_candidate_for_delivery(candidate.source_session_id)

    assert "sk-1234567890abcdefghijkl" not in row["value_json"]
    assert "must-not-persist" not in row["value_json"]
    assert "[REDACTED]" in row["value_json"]
    assert recovered.title == "[Claude] rotate [REDACTED]"
    assert recovered.cwd == "C:/workspace?token=[REDACTED]"


def test_sidebar_delivery_candidate_missing_or_malformed_state_fails_closed(db) -> None:
    store = SessionBridgeStore(db, clock=lambda: 125.0)
    candidate = _sidebar_candidate(db)
    store.enqueue_sidebar_job(candidate)
    state_key = "session-bridge:sidebar-delivery:" + hashlib.sha256(
        candidate.source_session_id.encode()
    ).hexdigest()

    db._execute_write(
        lambda conn: conn.execute(
            "DELETE FROM session_bridge_state WHERE key = ?", (state_key,)
        )
    )
    with pytest.raises(ValueError, match="missing sidebar delivery candidate"):
        store.get_sidebar_candidate_for_delivery(candidate.source_session_id)

    db._execute_write(
        lambda conn: conn.execute(
            "INSERT INTO session_bridge_state (key, value_json, updated_at) "
            "VALUES (?, ?, ?)",
            (state_key, '{"version":2}', 126.0),
        )
    )
    with pytest.raises(ValueError, match="invalid sidebar delivery candidate"):
        store.get_sidebar_candidate_for_delivery(candidate.source_session_id)


def test_sidebar_enqueue_rejects_a_conflicting_source_bridge_identity(db) -> None:
    store = SessionBridgeStore(db, clock=lambda: 125.0)
    candidate = _sidebar_candidate(db)

    conflicting = replace(candidate, bridge_id="sidebar:" + "0" * 64)

    with pytest.raises(ValueError, match="bridge ID"):
        store.enqueue_sidebar_job(conflicting)
    assert _rows(db, "SELECT * FROM session_sidebar_jobs") == []


def test_sidebar_claims_are_ordered_bounded_and_digest_tokens_at_rest(db) -> None:
    tokens = ("opaque-token-a", "opaque-token-b", "opaque-token-c")
    store = SessionBridgeStore(
        db,
        clock=lambda: 200.0,
        sidebar_token_factory=_token_factory(*tokens),
        sidebar_jitter=lambda _bound: 0.0,
    )
    for native_id, eligible_at in (
        ("later", 30.0),
        ("tie-b", 10.0),
        ("tie-a", 10.0),
    ):
        store.enqueue_sidebar_job(
            _sidebar_candidate(
                db,
                native_id=native_id,
                eligible_at=eligible_at,
            )
        )
    expected = _rows(
        db,
        "SELECT id FROM session_sidebar_jobs ORDER BY eligible_at, id LIMIT 2",
    )

    claimed = store.claim_sidebar_jobs(now=200.0, limit=2)

    assert [job["id"] for job in claimed] == [row["id"] for row in expected]
    assert [job["lease_token"] for job in claimed] == list(tokens[:2])
    assert all(job["lease_expires_at"] == 500.0 for job in claimed)
    persisted = {
        row["id"]: row
        for row in _rows(db, "SELECT * FROM session_sidebar_jobs")
    }
    for job, token in zip(claimed, tokens[:2], strict=True):
        row = persisted[job["id"]]
        assert row["lease_digest"] == hashlib.sha256(token.encode()).hexdigest()
        assert token not in row.values()
        assert "lease_token" not in row
    assert store.claim_sidebar_jobs(now=200.0, limit=1)[0]["lease_token"] == tokens[2]
    assert store.claim_sidebar_jobs(now=200.0, limit=1) == []


def test_sidebar_claim_prioritizes_ready_retry_before_older_pending(db) -> None:
    store = SessionBridgeStore(
        db,
        sidebar_token_factory=_token_factory("initial-token", "recovery-token"),
        sidebar_jitter=lambda _bound: 0.0,
    )
    retry = _sidebar_candidate(db, native_id="retry-first", eligible_at=200.0)
    store.enqueue_sidebar_job(retry)
    lease = store.claim_sidebar_jobs(now=300.0, limit=1)[0]
    store.fail_sidebar_job(
        lease_token=lease["lease_token"],
        error_code="rename_failed",
        now=301.0,
    )
    pending = _sidebar_candidate(db, native_id="older-pending", eligible_at=100.0)
    store.enqueue_sidebar_job(pending)

    claimed = store.claim_sidebar_jobs(now=400.0, limit=1)

    assert claimed[0]["source_session_id"] == retry.source_session_id
    assert claimed[0]["lease_token"] == "recovery-token"
    assert store.get_sidebar_job_for_source(pending.source_session_id)["state"] == (
        SidebarJobState.PENDING.value
    )


@pytest.mark.parametrize("limit", [0, 11, True, 1.5])
def test_sidebar_claim_validates_the_fixed_batch_bound(db, limit) -> None:
    store = SessionBridgeStore(db)

    with pytest.raises(ValueError, match="between 1 and 10"):
        store.claim_sidebar_jobs(now=100.0, limit=limit)


@pytest.mark.parametrize("now", [float("nan"), float("inf"), True, "100"])
def test_sidebar_claim_rejects_nonfinite_times(db, now) -> None:
    store = SessionBridgeStore(db)

    with pytest.raises(ValueError, match="finite"):
        store.claim_sidebar_jobs(now=now, limit=1)


@pytest.mark.parametrize(
    "lease_seconds",
    [299, 301, 300.0, True, False, "300", None],
)
def test_sidebar_claim_rejects_every_nonexact_lease_duration(
    db, lease_seconds
) -> None:
    store = SessionBridgeStore(db)

    with pytest.raises(ValueError, match="exactly 300"):
        store.claim_sidebar_jobs(now=100.0, limit=1, lease_seconds=lease_seconds)


def test_sidebar_claim_accepts_only_exact_integer_five_minute_lease(db) -> None:
    store = SessionBridgeStore(db)

    assert store.claim_sidebar_jobs(now=100.0, limit=1, lease_seconds=300) == []


def test_sidebar_claim_is_atomic_across_independent_store_instances(tmp_path) -> None:
    path = tmp_path / "shared-sidebar-state.db"
    first_db = SessionDB(path)
    second_db = SessionDB(path)
    try:
        first = SessionBridgeStore(
            first_db,
            sidebar_token_factory=_token_factory("first-token"),
            sidebar_jitter=lambda _bound: 0.0,
        )
        second = SessionBridgeStore(
            second_db,
            sidebar_token_factory=_token_factory("second-token"),
            sidebar_jitter=lambda _bound: 0.0,
        )
        for native_id in ("atomic-a", "atomic-b"):
            first.enqueue_sidebar_job(
                _sidebar_candidate(first_db, native_id=native_id, eligible_at=10.0)
            )
        barrier = Barrier(2)

        def _claim(store):
            barrier.wait()
            return store.claim_sidebar_jobs(now=100.0, limit=1)

        with ThreadPoolExecutor(max_workers=2) as executor:
            batches = list(executor.map(_claim, (first, second)))

        claimed = [job for batch in batches for job in batch]
        assert len(claimed) == 2
        assert len({job["id"] for job in claimed}) == 2
        assert {job["lease_token"] for job in claimed} == {
            "first-token",
            "second-token",
        }
    finally:
        second_db.close()
        first_db.close()


def test_expired_sidebar_lease_recovers_to_retry_then_can_be_released_again(db) -> None:
    store = SessionBridgeStore(
        db,
        sidebar_token_factory=_token_factory(
            "expired-token", "other-token", "replacement-token"
        ),
        sidebar_jitter=lambda _bound: 0.0,
    )
    expired = _sidebar_candidate(db, native_id="expired", eligible_at=20.0)
    store.enqueue_sidebar_job(expired)
    first = store.claim_sidebar_jobs(now=100.0, limit=1)[0]
    other = _sidebar_candidate(db, native_id="other", eligible_at=10.0)
    store.enqueue_sidebar_job(other)

    claimed_other = store.claim_sidebar_jobs(now=400.0, limit=1)[0]

    assert claimed_other["source_session_id"] == other.source_session_id
    recovered = store.get_sidebar_job_for_source(expired.source_session_id)
    assert recovered is not None
    assert recovered["state"] == SidebarJobState.RETRY.value
    assert recovered["attempts"] == 0
    assert recovered["lease_digest"] is None
    replacement = store.claim_sidebar_jobs(now=400.0, limit=1)[0]
    assert replacement["source_session_id"] == expired.source_session_id
    assert replacement["lease_token"] == "replacement-token"
    with pytest.raises(ValueError, match="lease token"):
        store.commit_sidebar_job(
            lease_token=first["lease_token"],
            codex_thread_id="codex-old",
            now=400.0,
        )


def test_sidebar_commit_requires_exact_unexpired_token_and_is_idempotent(db) -> None:
    store = SessionBridgeStore(
        db,
        sidebar_token_factory=_token_factory("commit-token"),
        sidebar_jitter=lambda _bound: 0.0,
    )
    candidate = _sidebar_candidate(db, native_id="commit")
    store.enqueue_sidebar_job(candidate)
    lease = store.claim_sidebar_jobs(now=100.0, limit=1)[0]

    committed = store.commit_sidebar_job(
        lease_token=lease["lease_token"],
        codex_thread_id="codex-thread-1",
        now=399.999,
    )
    replay = store.commit_sidebar_job(
        lease_token=lease["lease_token"],
        codex_thread_id="codex-thread-1",
        now=400.0,
    )

    assert replay == committed
    assert committed["state"] == SidebarJobState.VISIBLE.value
    assert committed["codex_thread_id"] == "codex-thread-1"
    assert committed["visible_at"] == 399.999
    assert committed["lease_digest"] is None
    assert committed["lease_expires_at"] is None
    assert committed["completion_digest"] == hashlib.sha256(
        b"commit-token"
    ).hexdigest()


def test_sidebar_bind_persists_exact_thread_across_retry_and_rejects_rebind(db) -> None:
    store = SessionBridgeStore(
        db,
        sidebar_token_factory=_token_factory("bind-token", "retry-token"),
        sidebar_jitter=lambda _bound: 0.0,
    )
    candidate = _sidebar_candidate(db, native_id="bind-retry")
    store.enqueue_sidebar_job(candidate)
    lease = store.claim_sidebar_jobs(now=100.0, limit=1)[0]

    bound = store.bind_sidebar_thread(
        lease_token=lease["lease_token"],
        codex_thread_id="codex-bound-thread",
        now=150.0,
    )
    replay = store.bind_sidebar_thread(
        lease_token=lease["lease_token"],
        codex_thread_id="codex-bound-thread",
        now=151.0,
    )
    with pytest.raises(ValueError, match="conflicting Codex thread identity"):
        store.bind_sidebar_thread(
            lease_token=lease["lease_token"],
            codex_thread_id="codex-replacement-thread",
            now=152.0,
        )

    assert replay == bound
    assert bound["state"] == SidebarJobState.LEASED.value
    assert bound["codex_thread_id"] == "codex-bound-thread"

    retried = store.fail_sidebar_job(
        lease_token=lease["lease_token"],
        error_code="bridge_temporarily_unavailable",
        now=160.0,
    )
    assert retried["state"] == SidebarJobState.RETRY.value
    assert retried["codex_thread_id"] == "codex-bound-thread"
    reclaimed = store.claim_sidebar_jobs(now=1_000.0, limit=1)[0]
    assert reclaimed["lease_token"] == "retry-token"
    assert reclaimed["codex_thread_id"] == "codex-bound-thread"


def test_sidebar_atomic_lineage_commit_and_exact_replay_are_idempotent(db) -> None:
    store = SessionBridgeStore(
        db,
        sidebar_token_factory=_token_factory("atomic-token"),
        sidebar_jitter=lambda _bound: 0.0,
    )
    candidate = _sidebar_candidate(db, native_id="atomic")
    store.enqueue_sidebar_job(candidate)
    lease = store.claim_sidebar_jobs(now=100.0, limit=1)[0]
    thread_id = "44444444-4444-4444-8444-444444444444"
    target_id = _seed_sidebar_codex_target(store, candidate, thread_id)

    committed = store.commit_sidebar_job_with_lineage(
        lease_token=lease["lease_token"],
        codex_thread_id=thread_id,
        source_session_id=candidate.source_session_id,
        bridge_id=candidate.bridge_id,
        now=200.0,
    )
    replay = store.commit_sidebar_job_with_lineage(
        lease_token=lease["lease_token"],
        codex_thread_id=thread_id,
        source_session_id=candidate.source_session_id,
        bridge_id=candidate.bridge_id,
        now=201.0,
    )

    assert replay == committed
    assert committed["state"] == SidebarJobState.VISIBLE.value
    links = _rows(db, "SELECT * FROM session_links WHERE bridge_id = ?", (
        candidate.bridge_id,
    ))
    assert len(links) == 1
    assert links[0]["from_session_id"] == candidate.source_session_id
    assert links[0]["to_session_id"] == target_id
    assert links[0]["relation"] == Relation.MIRRORS.value


@pytest.mark.parametrize("failure", ["wrong_token", "expired", "wrong_target"])
def test_sidebar_atomic_lineage_commit_has_no_partial_state_on_validation_failure(
    db,
    failure: str,
) -> None:
    store = SessionBridgeStore(
        db,
        sidebar_token_factory=_token_factory("atomic-failure-token"),
        sidebar_jitter=lambda _bound: 0.0,
    )
    candidate = _sidebar_candidate(db, native_id=f"atomic-{failure}")
    store.enqueue_sidebar_job(candidate)
    lease = store.claim_sidebar_jobs(now=100.0, limit=1)[0]
    thread_id = f"atomic-{failure}-thread"
    _seed_sidebar_codex_target(
        store,
        candidate,
        thread_id,
        bridge_id=("sidebar:wrong" if failure == "wrong_target" else None),
    )

    with pytest.raises(ValueError):
        store.commit_sidebar_job_with_lineage(
            lease_token=(
                "wrong-token" if failure == "wrong_token" else lease["lease_token"]
            ),
            codex_thread_id=thread_id,
            source_session_id=candidate.source_session_id,
            bridge_id=candidate.bridge_id,
            now=400.0 if failure == "expired" else 200.0,
        )

    assert _rows(db, "SELECT * FROM session_links WHERE bridge_id = ?", (
        candidate.bridge_id,
    )) == []
    job = store.get_sidebar_job_for_source(candidate.source_session_id)
    assert job is not None
    assert job["state"] == (
        SidebarJobState.RETRY.value
        if failure == "expired"
        else SidebarJobState.LEASED.value
    )
    assert job["codex_thread_id"] is None


def test_sidebar_atomic_lineage_write_fault_rolls_back_job_and_link(db) -> None:
    store = SessionBridgeStore(
        db,
        sidebar_token_factory=_token_factory("atomic-link-fault-token"),
        sidebar_jitter=lambda _bound: 0.0,
    )
    candidate = _sidebar_candidate(db, native_id="atomic-link-fault")
    store.enqueue_sidebar_job(candidate)
    lease = store.claim_sidebar_jobs(now=100.0, limit=1)[0]
    thread_id = "atomic-link-fault-thread"
    target_id = _seed_sidebar_codex_target(store, candidate, thread_id)
    collision_id = "sidebar-link:" + hashlib.sha256(
        f"{candidate.bridge_id}\0{candidate.source_session_id}\0{target_id}".encode()
    ).hexdigest()
    db.ensure_session("collision-source", source="cli")
    db.ensure_session("collision-target", source="cli")
    db._execute_write(lambda conn: conn.execute(
        """INSERT INTO session_links (
               id, from_session_id, to_session_id, relation, bridge_id, created_at
           ) VALUES (?, ?, ?, ?, ?, ?)""",
        (
            collision_id,
            "collision-source",
            "collision-target",
            Relation.MIRRORS.value,
            "collision-bridge",
            1.0,
        ),
    ))

    with pytest.raises(ValueError, match="collision"):
        store.commit_sidebar_job_with_lineage(
            lease_token=lease["lease_token"],
            codex_thread_id=thread_id,
            source_session_id=candidate.source_session_id,
            bridge_id=candidate.bridge_id,
            now=200.0,
        )

    assert _rows(db, "SELECT * FROM session_links WHERE bridge_id = ?", (
        candidate.bridge_id,
    )) == []
    job = store.get_sidebar_job_for_source(candidate.source_session_id)
    assert job is not None
    assert job["state"] == SidebarJobState.LEASED.value
    assert job["codex_thread_id"] is None


def test_sidebar_delivery_latency_uses_fixed_recent_indexed_sample(db) -> None:
    sample_limit = 512
    row_count = sample_limit + 75

    def _seed(conn):
        conn.executemany(
            "INSERT INTO sessions (id, source, started_at) VALUES (?, ?, ?)",
            [
                (f"claude:latency-{index}", "claude", float(index))
                for index in range(row_count)
            ],
        )
        conn.executemany(
            """INSERT INTO session_sidebar_jobs (
                   id, idempotency_key, source_session_id, bridge_id, state,
                   attempts, next_attempt_at, completion_digest,
                   codex_thread_id, eligible_at, created_at, updated_at,
                   visible_at
               ) VALUES (?, ?, ?, ?, ?, 0, ?, ?, ?, ?, ?, ?, ?)""",
            [
                (
                    f"latency-job-{index:04d}",
                    f"latency-key-{index:04d}",
                    f"claude:latency-{index}",
                    f"latency-bridge-{index:04d}",
                    SidebarJobState.VISIBLE.value,
                    float(index),
                    f"latency-digest-{index:04d}",
                    f"latency-thread-{index:04d}",
                    float(index - 990 if index < 75 else index),
                    float(index),
                    float(index + 10),
                    float(index + 10),
                )
                for index in range(row_count)
            ],
        )

    db._execute_write(_seed)
    statements: list[str] = []
    with db._lock:
        db._conn.set_trace_callback(statements.append)
    try:
        status = SessionBridgeStore(db).sidebar_delivery_status(now=10_000.0)
    finally:
        with db._lock:
            db._conn.set_trace_callback(None)
    with db._lock:
        plan = db._conn.execute(
            """EXPLAIN QUERY PLAN
               SELECT visible_at - eligible_at AS latency
                 FROM session_sidebar_jobs
                WHERE state = ? AND visible_at IS NOT NULL
                ORDER BY visible_at DESC, id DESC LIMIT ?""",
            (SidebarJobState.VISIBLE.value, sample_limit),
        ).fetchall()

    latency_queries = [
        " ".join(statement.upper().split())
        for statement in statements
        if "VISIBLE_AT - ELIGIBLE_AT AS LATENCY" in statement.upper()
    ]
    assert latency_queries
    assert all("LIMIT 512" in statement for statement in latency_queries)
    assert any(
        "USING INDEX IDX_SESSION_SIDEBAR_JOBS_VISIBLE_AT" in row[3].upper()
        for row in plan
    )
    assert status["delivery_latency_seconds"] == {
        "p50": 10.0,
        "p95": 10.0,
        "p99": 10.0,
    }


def test_sidebar_actionable_age_uses_fresh_lease_time_then_ages_while_leased(
    db: SessionDB,
) -> None:
    store = SessionBridgeStore(
        db,
        clock=lambda: 0.0,
        sidebar_token_factory=_token_factory("leased-age-token"),
    )
    candidate = _sidebar_candidate(db, native_id="leased-age", eligible_at=0.0)
    store.enqueue_sidebar_job(candidate)

    store.claim_sidebar_jobs(now=100.0, limit=1)

    fresh = store.sidebar_delivery_status(now=100.0)
    stale = store.sidebar_delivery_status(now=281.0)
    assert fresh["counts"][SidebarJobState.LEASED.value] == 1
    assert fresh["oldest_pending_age_seconds"] == 0.0
    assert stale["oldest_pending_age_seconds"] == 181.0


def test_sidebar_broker_heartbeat_rejects_invalid_timestamps(db) -> None:
    store = SessionBridgeStore(db)

    for invalid in (True, float("nan"), float("inf"), "100", None):
        with pytest.raises((TypeError, ValueError)):
            store.record_sidebar_broker_heartbeat(now=invalid)  # type: ignore[arg-type]


def test_sidebar_broker_heartbeat_is_monotonic_across_overlapping_stores(db) -> None:
    """An older request finishing late must not overwrite a newer heartbeat."""

    older_store = SessionBridgeStore(db)
    newer_db = SessionDB(db.db_path)
    newer_store = SessionBridgeStore(newer_db)
    older_started = Event()
    newer_finished = Event()

    def finish_older_request_late() -> None:
        older_started.set()
        assert newer_finished.wait(timeout=5)
        older_store.record_sidebar_broker_heartbeat(now=100.0)

    def finish_newer_request_first() -> None:
        assert older_started.wait(timeout=5)
        newer_store.record_sidebar_broker_heartbeat(now=200.0)
        newer_finished.set()

    try:
        with ThreadPoolExecutor(max_workers=2) as executor:
            older = executor.submit(finish_older_request_late)
            newer = executor.submit(finish_newer_request_first)
            older.result(timeout=5)
            newer.result(timeout=5)

        assert older_store.get_state(
            "session-bridge:sidebar:broker-heartbeat"
        ) == {"at": 200.0}
    finally:
        newer_db.close()


def test_sidebar_broker_heartbeat_recovers_malformed_ephemeral_state(db) -> None:
    store = SessionBridgeStore(db)

    def seed_malformed(conn) -> None:
        conn.execute(
            """INSERT INTO session_bridge_state (key, value_json, updated_at)
               VALUES (?, ?, ?)""",
            (
                "session-bridge:sidebar:broker-heartbeat",
                "not-json-sensitive-raw-value",
                1.0,
            ),
        )

    db._execute_write(seed_malformed)

    store.record_sidebar_broker_heartbeat(now=123.0)

    assert store.get_state("session-bridge:sidebar:broker-heartbeat") == {"at": 123.0}


def test_sidebar_lease_lookup_authenticates_active_and_completed_digest_minimally(db) -> None:
    store = SessionBridgeStore(
        db,
        sidebar_token_factory=_token_factory("lookup-token"),
        sidebar_jitter=lambda _bound: 0.0,
    )
    candidate = _sidebar_candidate(db, native_id="lookup-authenticated")
    store.enqueue_sidebar_job(candidate)
    lease = store.claim_sidebar_jobs(now=100.0, limit=1)[0]

    active = store.lookup_sidebar_job_by_lease(lease["lease_token"])
    assert active == {
        "source_session_id": candidate.source_session_id,
        "bridge_id": candidate.bridge_id,
        "state": SidebarJobState.LEASED.value,
        "codex_thread_id": None,
    }
    assert "lease_digest" not in active
    assert "completion_digest" not in active
    assert "lease_token" not in active

    store.commit_sidebar_job(
        lease_token=lease["lease_token"],
        codex_thread_id="codex-lookup-thread",
        now=200.0,
    )
    assert store.lookup_sidebar_job_by_lease(lease["lease_token"]) == {
        "source_session_id": candidate.source_session_id,
        "bridge_id": candidate.bridge_id,
        "state": SidebarJobState.VISIBLE.value,
        "codex_thread_id": "codex-lookup-thread",
    }

    with pytest.raises(ValueError, match="lease token"):
        store.lookup_sidebar_job_by_lease("wrong-token")


def test_sidebar_digest_auth_uses_bounded_equality_candidates_for_commit_replay(
    db,
) -> None:
    store = SessionBridgeStore(
        db,
        sidebar_token_factory=_token_factory("indexed-commit-token"),
        sidebar_jitter=lambda _bound: 0.0,
    )
    candidate = _sidebar_candidate(db, native_id="indexed-commit")
    store.enqueue_sidebar_job(candidate)
    lease = store.claim_sidebar_jobs(now=100.0, limit=1)[0]
    token_digest = hashlib.sha256(lease["lease_token"].encode()).hexdigest()
    with db._lock:
        assert db._conn is not None
        lease_plan = db._conn.execute(
            """EXPLAIN QUERY PLAN SELECT * FROM session_sidebar_jobs
               WHERE lease_digest = ? LIMIT 2""",
            (token_digest,),
        ).fetchall()
    statements: list[str] = []
    with db._lock:
        assert db._conn is not None
        db._conn.set_trace_callback(statements.append)
    try:
        committed = store.commit_sidebar_job(
            lease_token=lease["lease_token"],
            codex_thread_id="codex-indexed-thread",
            now=200.0,
        )
        replay = store.commit_sidebar_job(
            lease_token=lease["lease_token"],
            codex_thread_id="codex-indexed-thread",
            now=201.0,
        )
        with db._lock:
            assert db._conn is not None
            completion_plan = db._conn.execute(
                """EXPLAIN QUERY PLAN SELECT * FROM session_sidebar_jobs
                   WHERE completion_digest = ? LIMIT 2""",
                (token_digest,),
            ).fetchall()
    finally:
        with db._lock:
            assert db._conn is not None
            db._conn.set_trace_callback(None)

    normalized = [" ".join(statement.upper().split()) for statement in statements]
    digest_queries = [
        statement
        for statement in normalized
        if "SELECT * FROM SESSION_SIDEBAR_JOBS" in statement
        and ("LEASE_DIGEST =" in statement or "COMPLETION_DIGEST =" in statement)
    ]
    assert replay == committed
    assert any(
        "USING INDEX idx_session_sidebar_jobs_lease_digest" in row[3]
        for row in lease_plan
    )
    assert any(
        "USING INDEX idx_session_sidebar_jobs_completion_digest" in row[3]
        for row in completion_plan
    )
    assert digest_queries
    assert all("LIMIT 2" in statement for statement in digest_queries)
    assert not any(
        "LEASE_DIGEST IS NOT NULL OR COMPLETION_DIGEST IS NOT NULL" in statement
        for statement in normalized
    )


def test_sidebar_commit_rejects_expiry_and_recovers_the_job(db) -> None:
    store = SessionBridgeStore(
        db,
        sidebar_token_factory=_token_factory("expiring-token"),
        sidebar_jitter=lambda _bound: 0.0,
    )
    candidate = _sidebar_candidate(db, native_id="expiring")
    store.enqueue_sidebar_job(candidate)
    lease = store.claim_sidebar_jobs(now=100.0, limit=1)[0]

    with pytest.raises(ValueError, match="expired"):
        store.commit_sidebar_job(
            lease_token=lease["lease_token"],
            codex_thread_id="codex-too-late",
            now=400.0,
        )

    recovered = store.get_sidebar_job_for_source(candidate.source_session_id)
    assert recovered is not None
    assert recovered["state"] == SidebarJobState.RETRY.value
    assert recovered["lease_digest"] is None


def test_sidebar_commit_fails_closed_for_different_task_or_token(db) -> None:
    store = SessionBridgeStore(
        db,
        sidebar_token_factory=_token_factory("exact-token"),
        sidebar_jitter=lambda _bound: 0.0,
    )
    candidate = _sidebar_candidate(db, native_id="conflict")
    store.enqueue_sidebar_job(candidate)
    lease = store.claim_sidebar_jobs(now=100.0, limit=1)[0]
    store.commit_sidebar_job(
        lease_token=lease["lease_token"],
        codex_thread_id="codex-original",
        now=200.0,
    )

    with pytest.raises(ValueError, match="conflicting"):
        store.commit_sidebar_job(
            lease_token=lease["lease_token"],
            codex_thread_id="codex-different",
            now=201.0,
        )
    with pytest.raises(ValueError, match="lease token"):
        store.commit_sidebar_job(
            lease_token="different-token",
            codex_thread_id="codex-original",
            now=201.0,
        )
    persisted = store.get_sidebar_job_for_source(candidate.source_session_id)
    assert persisted is not None
    assert persisted["codex_thread_id"] == "codex-original"


def test_sidebar_fail_rejects_non_allowlisted_error_without_releasing(db) -> None:
    store = SessionBridgeStore(
        db,
        sidebar_token_factory=_token_factory("error-token"),
        sidebar_jitter=lambda _bound: 0.0,
    )
    candidate = _sidebar_candidate(db, native_id="bad-error")
    store.enqueue_sidebar_job(candidate)
    lease = store.claim_sidebar_jobs(now=100.0, limit=1)[0]

    with pytest.raises(ValueError, match="fixed allowlist"):
        store.fail_sidebar_job(
            lease_token=lease["lease_token"],
            error_code="Traceback: secret detail",
            now=150.0,
        )

    row = store.get_sidebar_job_for_source(candidate.source_session_id)
    assert row is not None
    assert row["state"] == SidebarJobState.LEASED.value
    assert row["error_code"] is None
    assert "error_detail" not in row


@pytest.mark.parametrize(
    "error_code",
    [None, True, False, [], {}, 1, 1.0, b"desktop_offline"],
)
def test_sidebar_fail_rejects_nonstring_error_codes_without_mutating_lease(
    db, error_code
) -> None:
    token = f"nonstring-error-{type(error_code).__name__}-{error_code!r}"
    store = SessionBridgeStore(
        db,
        sidebar_token_factory=_token_factory(token),
        sidebar_jitter=lambda _bound: 0.0,
    )
    candidate = _sidebar_candidate(
        db,
        native_id=f"nonstring-error-{type(error_code).__name__}",
    )
    store.enqueue_sidebar_job(candidate)
    lease = store.claim_sidebar_jobs(now=100.0, limit=1)[0]
    before = store.get_sidebar_job_for_source(candidate.source_session_id)
    assert before is not None

    with pytest.raises(ValueError, match="fixed allowlist"):
        store.fail_sidebar_job(
            lease_token=lease["lease_token"],
            error_code=error_code,
            now=150.0,
        )

    after = store.get_sidebar_job_for_source(candidate.source_session_id)
    assert after == before
    assert after is not None
    assert after["state"] == SidebarJobState.LEASED.value
    assert after["lease_digest"] == hashlib.sha256(token.encode()).hexdigest()
    assert after["attempts"] == 0


def test_sidebar_retryable_error_allowlist_is_the_exact_fixed_contract() -> None:
    assert SIDEBAR_RETRYABLE_ERRORS == frozenset({
        "codex_tool_unavailable",
        "desktop_offline",
        "bridge_temporarily_unavailable",
        "sqlite_busy",
        "rename_failed",
        "project_lookup_failed",
        "native_task_not_indexed",
        "broker_time_budget",
    })


@pytest.mark.parametrize(
    "error_code",
    sorted(SIDEBAR_RETRYABLE_ERRORS - {"broker_time_budget"}),
)
def test_each_regular_retryable_sidebar_error_schedules_a_retry(
    db, error_code: str
) -> None:
    token = f"retryable-{error_code}"
    store = SessionBridgeStore(
        db,
        sidebar_token_factory=_token_factory(token),
        sidebar_jitter=lambda _bound: 0.0,
    )
    candidate = _sidebar_candidate(db, native_id=f"retryable-{error_code}")
    store.enqueue_sidebar_job(candidate)
    lease = store.claim_sidebar_jobs(now=100.0, limit=1)[0]

    failed = store.fail_sidebar_job(
        lease_token=lease["lease_token"],
        error_code=error_code,
        now=150.0,
    )

    assert failed["state"] == SidebarJobState.RETRY.value
    assert failed["attempts"] == 1
    assert failed["next_attempt_at"] == 210.0
    assert failed["error_code"] == error_code


@pytest.mark.parametrize("jitter_kind", ["negative", "above-bound"])
def test_sidebar_retry_rejects_out_of_range_jitter_and_rolls_back_lease(
    db, jitter_kind: str
) -> None:
    def _invalid_jitter(bound: float) -> float:
        return -0.01 if jitter_kind == "negative" else bound + 0.01

    store = SessionBridgeStore(
        db,
        sidebar_token_factory=_token_factory(f"jitter-{jitter_kind}"),
        sidebar_jitter=_invalid_jitter,
    )
    candidate = _sidebar_candidate(db, native_id=f"jitter-{jitter_kind}")
    store.enqueue_sidebar_job(candidate)
    lease = store.claim_sidebar_jobs(now=100.0, limit=1)[0]
    before = store.get_sidebar_job_for_source(candidate.source_session_id)
    assert before is not None

    with pytest.raises(ValueError, match="outside its bound"):
        store.fail_sidebar_job(
            lease_token=lease["lease_token"],
            error_code="desktop_offline",
            now=150.0,
        )

    after = store.get_sidebar_job_for_source(candidate.source_session_id)
    assert after == before
    assert after is not None
    assert after["state"] == SidebarJobState.LEASED.value
    assert after["lease_digest"] == hashlib.sha256(
        lease["lease_token"].encode()
    ).hexdigest()
    assert after["attempts"] == 0


def test_sidebar_retry_backoff_counts_failures_and_fails_on_attempt_five(db) -> None:
    jitter_bounds = []

    def _max_jitter(bound: float) -> float:
        jitter_bounds.append(bound)
        return bound

    store = SessionBridgeStore(
        db,
        sidebar_token_factory=_token_factory(
            "retry-token-1",
            "retry-token-2",
            "retry-token-3",
            "retry-token-4",
            "retry-token-5",
        ),
        sidebar_jitter=_max_jitter,
    )
    candidate = _sidebar_candidate(db, native_id="retry")
    store.enqueue_sidebar_job(candidate)
    now = 100.0
    expected = (
        (60.0, 6.0),
        (120.0, 12.0),
        (240.0, 24.0),
        (480.0, 30.0),
        (900.0, 30.0),
    )

    for attempt, (base, jitter) in enumerate(expected, start=1):
        lease = store.claim_sidebar_jobs(now=now, limit=1)[0]
        failed = store.fail_sidebar_job(
            lease_token=lease["lease_token"],
            error_code="desktop_offline",
            now=now,
        )
        assert failed["attempts"] == attempt
        assert failed["next_attempt_at"] == now + base + jitter
        expected_state = (
            SidebarJobState.FAILED.value
            if attempt == 5
            else SidebarJobState.RETRY.value
        )
        assert failed["state"] == expected_state
        now = failed["next_attempt_at"]

    assert jitter_bounds == [6.0, 12.0, 24.0, 30.0, 30.0]
    assert store.claim_sidebar_jobs(now=now, limit=1) == []


@pytest.mark.parametrize(
    "error_code",
    [
        "marker_conflict",
        "source_identity_mismatch",
        "codex_thread_conflict",
        "provider_mismatch",
        "source_cwd_missing",
        "permission_preflight_failed",
        "retry_budget_exhausted",
    ],
)
def test_sidebar_fatal_errors_fail_immediately_without_counting_attempt(
    db, error_code: str
) -> None:
    store = SessionBridgeStore(
        db,
        sidebar_token_factory=_token_factory(f"fatal-{error_code}"),
        sidebar_jitter=lambda _bound: 0.0,
    )
    candidate = _sidebar_candidate(db, native_id=f"fatal-{error_code}")
    store.enqueue_sidebar_job(candidate)
    lease = store.claim_sidebar_jobs(now=100.0, limit=1)[0]

    failed = store.fail_sidebar_job(
        lease_token=lease["lease_token"],
        error_code=error_code,
        now=150.0,
    )

    assert failed["state"] == SidebarJobState.FAILED.value
    assert failed["attempts"] == 0
    assert failed["error_code"] == error_code
    assert failed["lease_digest"] is None


def test_sidebar_broker_budget_failure_releases_without_attempt_or_delay(db) -> None:
    store = SessionBridgeStore(
        db,
        sidebar_token_factory=_token_factory("budget-token", "budget-retry"),
        sidebar_jitter=lambda _bound: 0.0,
    )
    candidate = _sidebar_candidate(db, native_id="budget")
    store.enqueue_sidebar_job(candidate)
    lease = store.claim_sidebar_jobs(now=100.0, limit=1)[0]

    released = store.fail_sidebar_job(
        lease_token=lease["lease_token"],
        error_code="broker_time_budget",
        now=150.0,
    )

    assert released["state"] == SidebarJobState.PENDING.value
    assert released["attempts"] == 0
    assert released["next_attempt_at"] == 150.0
    assert released["error_code"] == "broker_time_budget"
    assert store.claim_sidebar_jobs(now=150.0, limit=1)[0]["lease_token"] == (
        "budget-retry"
    )


def test_sidebar_release_returns_an_active_lease_to_pending(db) -> None:
    store = SessionBridgeStore(
        db,
        sidebar_token_factory=_token_factory("release-token"),
        sidebar_jitter=lambda _bound: 0.0,
    )
    candidate = _sidebar_candidate(db, native_id="release")
    store.enqueue_sidebar_job(candidate)
    lease = store.claim_sidebar_jobs(now=100.0, limit=1)[0]

    released = store.release_sidebar_job(
        lease_token=lease["lease_token"],
        now=175.0,
    )

    assert released["state"] == SidebarJobState.PENDING.value
    assert released["attempts"] == 0
    assert released["next_attempt_at"] == 175.0
    assert released["lease_digest"] is None
    assert released["error_code"] is None


def test_sidebar_operator_retry_requeues_only_the_expected_failed_job(db) -> None:
    store = SessionBridgeStore(
        db,
        sidebar_token_factory=_token_factory("fatal-token", "recovered-token"),
        sidebar_jitter=lambda _bound: 0.0,
    )
    candidate = _sidebar_candidate(db, native_id="operator-retry")
    store.enqueue_sidebar_job(candidate)
    lease = store.claim_sidebar_jobs(now=100.0, limit=1)[0]
    store.fail_sidebar_job(
        lease_token=lease["lease_token"],
        error_code="marker_conflict",
        now=150.0,
    )

    with pytest.raises(ValueError, match="expected sidebar failure"):
        store.retry_failed_sidebar_job(
            source_session_id=candidate.source_session_id,
            expected_error_code="source_identity_mismatch",
            now=175.0,
        )

    retried = store.retry_failed_sidebar_job(
        source_session_id=candidate.source_session_id,
        expected_error_code="marker_conflict",
        now=200.0,
    )

    assert retried["state"] == SidebarJobState.PENDING.value
    assert retried["attempts"] == 0
    assert retried["next_attempt_at"] == 200.0
    assert retried["lease_digest"] is None
    assert retried["lease_expires_at"] is None
    assert retried["error_code"] is None
    assert retried["codex_thread_id"] is None
    assert store.claim_sidebar_jobs(now=200.0, limit=1)[0]["lease_token"] == (
        "recovered-token"
    )


def test_sidebar_operator_retry_rejects_a_completed_sibling_job(db) -> None:
    store = SessionBridgeStore(
        db,
        sidebar_token_factory=_token_factory("fatal-token"),
        sidebar_jitter=lambda _bound: 0.0,
    )
    candidate = _sidebar_candidate(db, native_id="operator-retry-sibling")
    store.enqueue_sidebar_job(candidate)
    lease = store.claim_sidebar_jobs(now=100.0, limit=1)[0]
    store.fail_sidebar_job(
        lease_token=lease["lease_token"],
        error_code="marker_conflict",
        now=150.0,
    )
    db._execute_write(
        lambda conn: conn.execute(
            """INSERT INTO session_sidebar_jobs (
               id, idempotency_key, source_session_id, bridge_id, state,
               attempts, next_attempt_at, completion_digest, codex_thread_id,
               eligible_at, created_at, updated_at, visible_at
               ) VALUES (?, ?, ?, ?, 'sidebar_visible', 1, 1, ?, ?, 1, 1, 1, 1)""",
            (
                "sidebar-job:completed-sibling",
                "codex-sidebar:completed-sibling:v1",
                candidate.source_session_id,
                "sidebar:" + "f" * 64,
                "completed-digest",
                "codex:completed-sibling",
            ),
        )
    )

    with pytest.raises(ValueError, match="expected sidebar failure"):
        store.retry_failed_sidebar_job(
            source_session_id=candidate.source_session_id,
            expected_error_code="marker_conflict",
            now=200.0,
        )


def test_sidebar_operator_retry_rejects_a_malformed_job_identity(db) -> None:
    store = SessionBridgeStore(
        db,
        sidebar_token_factory=_token_factory("fatal-token"),
        sidebar_jitter=lambda _bound: 0.0,
    )
    candidate = _sidebar_candidate(db, native_id="operator-retry-malformed")
    store.enqueue_sidebar_job(candidate)
    lease = store.claim_sidebar_jobs(now=100.0, limit=1)[0]
    store.fail_sidebar_job(
        lease_token=lease["lease_token"],
        error_code="marker_conflict",
        now=150.0,
    )
    db._execute_write(
        lambda conn: conn.execute(
            """UPDATE session_sidebar_jobs
               SET id = ?, idempotency_key = ?, bridge_id = ?
               WHERE source_session_id = ?""",
            (
                "sidebar-job:malformed",
                "codex-sidebar:malformed:v1",
                "sidebar:" + "e" * 64,
                candidate.source_session_id,
            ),
        )
    )

    with pytest.raises(ValueError, match="expected sidebar failure"):
        store.retry_failed_sidebar_job(
            source_session_id=candidate.source_session_id,
            expected_error_code="marker_conflict",
            now=200.0,
        )


def test_sidebar_operator_retry_rejects_a_stale_visibility_timestamp(db) -> None:
    store = SessionBridgeStore(
        db,
        sidebar_token_factory=_token_factory("fatal-token"),
        sidebar_jitter=lambda _bound: 0.0,
    )
    candidate = _sidebar_candidate(db, native_id="operator-retry-visible-at")
    store.enqueue_sidebar_job(candidate)
    lease = store.claim_sidebar_jobs(now=100.0, limit=1)[0]
    store.fail_sidebar_job(
        lease_token=lease["lease_token"],
        error_code="marker_conflict",
        now=150.0,
    )
    db._execute_write(
        lambda conn: conn.execute(
            """UPDATE session_sidebar_jobs SET visible_at = ?
               WHERE source_session_id = ?""",
            (125.0, candidate.source_session_id),
        )
    )

    with pytest.raises(ValueError, match="expected sidebar failure"):
        store.retry_failed_sidebar_job(
            source_session_id=candidate.source_session_id,
            expected_error_code="marker_conflict",
            now=200.0,
        )


def test_malformed_sidebar_provider_row_does_not_block_valid_provider(db) -> None:
    store = SessionBridgeStore(
        db,
        sidebar_token_factory=_token_factory("valid-provider-token"),
        sidebar_jitter=lambda _bound: 0.0,
    )
    db.create_session("codex:malformed-source", source="codex")
    db._execute_write(
        lambda conn: conn.execute(
            """INSERT INTO session_sidebar_jobs (
               id, idempotency_key, source_session_id, bridge_id, state,
               attempts, next_attempt_at, eligible_at, created_at, updated_at
               ) VALUES (?, ?, ?, ?, 'sidebar_pending', 0, 1, 1, 1, 1)""",
            (
                "sidebar-job-malformed",
                "codex-sidebar:codex:malformed-source:v1",
                "codex:malformed-source",
                "sidebar:" + "f" * 64,
            ),
        )
    )
    candidate = _sidebar_candidate(db, native_id="valid-provider", eligible_at=2.0)
    store.enqueue_sidebar_job(candidate)

    claimed = store.claim_sidebar_jobs(now=100.0, limit=1)

    assert [job["source_session_id"] for job in claimed] == [
        candidate.source_session_id
    ]
    malformed = _rows(
        db,
        "SELECT * FROM session_sidebar_jobs WHERE id = 'sidebar-job-malformed'",
    )[0]
    assert malformed["state"] == SidebarJobState.FAILED.value
    assert malformed["error_code"] == "provider_mismatch"


def test_sidebar_claim_scans_bounded_pages_and_eventually_passes_malformed_rows(
    db,
) -> None:
    store = SessionBridgeStore(
        db,
        sidebar_token_factory=_token_factory("bounded-scan-token"),
        sidebar_jitter=lambda _bound: 0.0,
    )

    def _seed_malformed_page(conn) -> None:
        for index in range(45):
            source_session_id = f"codex:malformed-page-{index:02d}"
            conn.execute(
                "INSERT INTO sessions (id, source, started_at) VALUES (?, 'codex', 1)",
                (source_session_id,),
            )
            conn.execute(
                """INSERT INTO session_sidebar_jobs (
                   id, idempotency_key, source_session_id, bridge_id, state,
                   attempts, next_attempt_at, eligible_at, created_at, updated_at
                   ) VALUES (?, ?, ?, ?, 'sidebar_pending', 0, 1, ?, 1, 1)""",
                (
                    f"sidebar-malformed-page-{index:02d}",
                    f"codex-sidebar:{source_session_id}:v1",
                    source_session_id,
                    "sidebar:" + f"{index:064x}",
                    float(index),
                ),
            )

    db._execute_write(_seed_malformed_page)
    valid = _sidebar_candidate(db, native_id="after-malformed-page", eligible_at=100.0)
    store.enqueue_sidebar_job(valid)
    statements: list[str] = []
    with db._lock:
        assert db._conn is not None
        db._conn.set_trace_callback(statements.append)
    try:
        first = store.claim_sidebar_jobs(now=200.0, limit=1)
        after_first = store.sidebar_job_counts()
        second = store.claim_sidebar_jobs(now=200.0, limit=1)
    finally:
        with db._lock:
            assert db._conn is not None
            db._conn.set_trace_callback(None)

    due_queries = [
        " ".join(statement.upper().split())
        for statement in statements
        if "SELECT * FROM SESSION_SIDEBAR_JOBS" in statement.upper()
        and "ORDER BY CASE WHEN STATE =" in " ".join(statement.upper().split())
        and "ELIGIBLE_AT, ID" in " ".join(statement.upper().split())
    ]
    assert first == []
    assert after_first[SidebarJobState.FAILED.value] == 40
    assert after_first[SidebarJobState.PENDING.value] == 6
    assert [job["source_session_id"] for job in second] == [
        valid.source_session_id
    ]
    assert len({job["id"] for job in second}) == 1
    assert len(due_queries) == 2
    assert all("LIMIT 40" in statement for statement in due_queries)


def test_sidebar_counts_and_source_lookup_have_stable_public_shapes(db) -> None:
    store = SessionBridgeStore(
        db,
        sidebar_token_factory=_token_factory("lookup-token"),
        sidebar_jitter=lambda _bound: 0.0,
    )
    pending = _sidebar_candidate(db, native_id="lookup-pending")
    leased = _sidebar_candidate(db, native_id="lookup-leased")
    store.enqueue_sidebar_job(pending)
    leased_row = store.enqueue_sidebar_job(leased)

    assert store.get_sidebar_job_for_source("missing") is None
    assert store.get_sidebar_job_for_source(leased.source_session_id) == {
        key: value for key, value in leased_row.items() if key != "created"
    }
    store.claim_sidebar_jobs(now=200.0, limit=1)
    assert store.sidebar_job_counts() == {
        "sidebar_pending": 1,
        "sidebar_leased": 1,
        "sidebar_visible": 0,
        "sidebar_retry": 0,
        "sidebar_failed": 0,
    }
