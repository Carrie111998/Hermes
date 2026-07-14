from __future__ import annotations

import json
from dataclasses import replace

import pytest

from hermes_state import SessionDB
from session_bridge.models import (
    ContextPack,
    OriginKind,
    ProjectedMessage,
    Provider,
    Relation,
    SessionLink,
    SessionProjection,
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


def test_mirror_jobs_are_idempotent_and_transition_durably(db):
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
    assert store.claim_due_jobs(now=99.0, limit=10) == []
    claimed = store.claim_due_jobs(now=100.0, limit=10)
    assert claimed[0]["state"] == "running"
    assert claimed[0]["attempts"] == 1
    assert store.claim_due_jobs(now=100.0, limit=10) == []

    store.retry_job(
        first["id"], code="timeout", detail="temporary", next_attempt_at=120.0
    )
    store.retry_job(
        first["id"], code="timeout", detail="temporary", next_attempt_at=120.0
    )
    assert store.claim_due_jobs(now=119.0, limit=10) == []
    reclaimed = store.claim_due_jobs(now=120.0, limit=10)
    assert reclaimed[0]["attempts"] == 2
    store.fail_job_manually(first["id"], code="ambiguous", detail="operator required")
    failed = _rows(db, "SELECT * FROM session_mirror_jobs")[0]
    assert failed["state"] == "manual_failure"
    assert failed["error_code"] == "ambiguous"
    assert store.claim_due_jobs(now=999.0, limit=10) == []


def test_retry_state_only_allows_exact_replay_or_manual_exhaustion(db):
    store = SessionBridgeStore(db, clock=lambda: 100.0)
    store.upsert_projection(_projection(_message("e1", "source")))
    job = store.enqueue_mirror_job(
        "claude:native-1", Provider.CODEX, policy_generation=1
    )
    store.claim_due_jobs(now=100.0, limit=1)
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
        )
    )
    job = store.enqueue_mirror_job(
        "claude:native-1", Provider.CODEX, policy_generation=1
    )
    store.claim_due_jobs(now=100.0, limit=1)

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
            _message("t1", "target"), provider=Provider.CODEX, native_id="target-1"
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
    job = store.enqueue_mirror_job(
        "claude:native-1", Provider.CODEX, policy_generation=1
    )
    store.claim_due_jobs(now=100.0, limit=1)

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


def test_conflicting_completion_replay_cannot_retarget_succeeded_job(db):
    store = SessionBridgeStore(db, clock=lambda: 100.0)
    store.upsert_projection(_projection(_message("s1", "source")))
    for native_id in ("target-1", "target-2"):
        store.upsert_projection(
            _projection(
                _message(f"event-{native_id}", native_id),
                provider=Provider.CODEX,
                native_id=native_id,
            )
        )
    job = store.enqueue_mirror_job(
        "claude:native-1", Provider.CODEX, policy_generation=1
    )
    store.claim_due_jobs(now=100.0, limit=1)
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
            _message("t1", "target"), provider=Provider.CODEX, native_id="target-1"
        )
    )
    succeeded = store.enqueue_mirror_job(
        "claude:native-1", Provider.CODEX, policy_generation=1
    )
    store.claim_due_jobs(now=100.0, limit=1)
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

    terminal = store.enqueue_mirror_job(
        "claude:native-1", Provider.CODEX, policy_generation=2
    )
    store.claim_due_jobs(now=100.0, limit=1)
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
