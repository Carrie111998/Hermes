from __future__ import annotations

import json
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from threading import Barrier

import pytest

from hermes_state import SessionDB
from session_bridge.mirror import (
    DiscoveryMode,
    EligibilityContext,
    MirrorCandidate,
    MirrorPolicy,
    enqueue_mirror_job,
)
from session_bridge.models import (
    ContextPack,
    MirrorJobState,
    OriginKind,
    ProjectedMessage,
    Provider,
    Relation,
    SessionLink,
    SessionProjection,
    canonical_session_id,
)
from session_bridge.store import SessionBridgeStore


@pytest.fixture
def db(tmp_path):
    database = SessionDB(tmp_path / "state.db")
    yield database
    database.close()


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
