"""Behavior tests for the gateway-hosted room event log."""

from __future__ import annotations

import sqlite3
from concurrent.futures import ProcessPoolExecutor, ThreadPoolExecutor

import pytest

from gateway import hosted_rooms as rooms
from hermes_state import SessionDB

USER = {"kind": "user", "id": "desktop-user", "display_name": "User"}
GATEWAY_A = {"kind": "gateway", "id": "gateway-a"}
GATEWAY_B = {"kind": "gateway", "id": "gateway-b"}


def _create_pre_actor_database(path) -> None:
    conn = sqlite3.connect(path)
    try:
        conn.execute(
            """CREATE TABLE hosted_rooms (
                room_id TEXT PRIMARY KEY,
                name TEXT NOT NULL,
                members_json TEXT NOT NULL,
                next_seq INTEGER NOT NULL,
                revision INTEGER NOT NULL,
                created_at REAL NOT NULL,
                updated_at REAL NOT NULL,
                disbanded_at REAL
            )"""
        )
        conn.execute(
            """CREATE TABLE hosted_room_events (
                room_id TEXT NOT NULL,
                seq INTEGER NOT NULL,
                event_id TEXT NOT NULL,
                kind TEXT NOT NULL,
                payload_json TEXT NOT NULL,
                created_at REAL NOT NULL,
                PRIMARY KEY (room_id, seq),
                UNIQUE (room_id, event_id)
            )"""
        )
        conn.execute(
            """INSERT INTO hosted_rooms
               VALUES ('room-1', 'Legacy', '[]', 2, 1, 1, 1, NULL)"""
        )
        conn.execute(
            """INSERT INTO hosted_room_events
               VALUES ('room-1', 1, 'legacy-event', 'message.created', '{}', 1)"""
        )
        conn.commit()
    finally:
        conn.close()


def _read_legacy_state(path: str) -> tuple[str, int]:
    state = rooms.room_state(path, room_id="room-1")
    return state["authority_gateway_id"], state["latest_seq"]


def _create(db, room_id="room-1"):
    return rooms.create_room(
        db,
        room_id=room_id,
        name="Release room",
        members=[{"profile": "ops", "handle": "ops"}],
        authority_gateway_id="gateway-a",
        now=10,
    )


def test_create_room_is_idempotent_but_conflicts_fail_closed(tmp_path):
    db = tmp_path / "state.db"
    first = _create(db)
    second = _create(db)

    assert first["idempotent"] is False
    assert second["idempotent"] is True
    assert first["room_id"] == second["room_id"] == "room-1"

    with pytest.raises(rooms.RoomConflictError):
        rooms.create_room(
            db,
            room_id="room-1",
            name="A different room",
            members=[],
            authority_gateway_id="gateway-a",
        )


def test_room_state_exposes_authority_and_replay_cursor(tmp_path):
    db = tmp_path / "state.db"
    room = _create(db)

    assert room["authority_gateway_id"] == "gateway-a"
    assert room["authority_epoch"] == 1
    assert rooms.room_state(db, room_id="room-1") == {
        **room,
        "latest_seq": 0,
    }


def test_authority_claim_fences_stale_gateway_events(tmp_path):
    db = tmp_path / "state.db"
    _create(db)

    first = rooms.append_event(
        db,
        room_id="room-1",
        event_id="turn-1",
        kind="turn.started",
        actor=GATEWAY_A,
        authority_gateway_id="gateway-a",
        authority_epoch=1,
        payload={"member": "ops"},
    )
    claimed = rooms.claim_authority(
        db,
        room_id="room-1",
        expected_gateway_id="gateway-a",
        expected_epoch=1,
        new_gateway_id="gateway-b",
        event_id="claim-gateway-b",
        now=30,
    )
    retried = rooms.claim_authority(
        db,
        room_id="room-1",
        expected_gateway_id="gateway-a",
        expected_epoch=1,
        new_gateway_id="gateway-b",
        event_id="claim-gateway-b",
        now=40,
    )

    assert claimed["authority_gateway_id"] == "gateway-b"
    assert claimed["authority_epoch"] == 2
    assert claimed["idempotent"] is False
    assert claimed["claim_event"]["kind"] == "authority.claimed"
    assert claimed["claim_event"]["authority_epoch"] == 2
    assert claimed["claim_event"]["payload"] == {
        "previous_gateway_id": "gateway-a",
        "authority_gateway_id": "gateway-b",
        "authority_epoch": 2,
    }
    assert retried["authority_epoch"] == 2
    assert retried["idempotent"] is True
    assert retried["claim_event"]["seq"] == claimed["claim_event"]["seq"]
    assert retried["claim_event"]["idempotent"] is True
    state = rooms.room_state(db, room_id="room-1")
    assert state["authority_claim"]["event_id"] == "claim-gateway-b"
    assert state["authority_claim"]["payload"]["previous_gateway_id"] == "gateway-a"

    # An exact retry admitted before takeover stays idempotent and cannot
    # produce a second side effect, even though its epoch is now stale.
    repeated = rooms.append_event(
        db,
        room_id="room-1",
        event_id="turn-1",
        kind="turn.started",
        actor=GATEWAY_A,
        authority_gateway_id="gateway-a",
        authority_epoch=1,
        payload={"member": "ops"},
    )
    assert repeated["seq"] == first["seq"]
    assert repeated["idempotent"] is True

    with pytest.raises(rooms.AuthorityConflictError, match="stale"):
        rooms.append_event(
            db,
            room_id="room-1",
            event_id="turn-2-stale",
            kind="turn.started",
            actor=GATEWAY_A,
            authority_gateway_id="gateway-a",
            authority_epoch=1,
            payload={"member": "ops"},
        )

    with pytest.raises(rooms.AuthorityConflictError, match="stale"):
        rooms.append_event(
            db,
            room_id="room-1",
            event_id="member-2-stale",
            kind="message.member",
            actor={"kind": "member", "id": "ops"},
            authority_gateway_id="gateway-a",
            authority_epoch=1,
            payload={"text": "stale result"},
        )

    current = rooms.append_event(
        db,
        room_id="room-1",
        event_id="turn-2-current",
        kind="turn.started",
        actor=GATEWAY_B,
        authority_gateway_id="gateway-b",
        authority_epoch=2,
        payload={"member": "ops"},
    )
    assert current["authority_epoch"] == 2
    current_member = rooms.append_event(
        db,
        room_id="room-1",
        event_id="member-2-current",
        kind="message.member",
        actor={"kind": "member", "id": "ops"},
        authority_gateway_id="gateway-b",
        authority_epoch=2,
        payload={"text": "current result"},
    )
    assert current_member["authority_epoch"] == 2


def test_concurrent_authority_claim_has_one_winner(tmp_path):
    db = tmp_path / "state.db"
    _create(db)

    def claim(gateway_id):
        try:
            return rooms.claim_authority(
                db,
                room_id="room-1",
                expected_gateway_id="gateway-a",
                expected_epoch=1,
                new_gateway_id=gateway_id,
                event_id=f"claim-{gateway_id}",
            )
        except rooms.AuthorityConflictError:
            return None

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(claim, ["gateway-b", "gateway-c"]))

    winners = [result for result in results if result is not None]
    assert len(winners) == 1
    assert winners[0]["authority_epoch"] == 2
    assert rooms.room_state(db, room_id="room-1")["authority_gateway_id"] in {
        "gateway-b",
        "gateway-c",
    }


def test_retry_of_successful_but_superseded_claim_is_distinct(tmp_path):
    db = tmp_path / "state.db"
    _create(db)
    rooms.claim_authority(
        db,
        room_id="room-1",
        expected_gateway_id="gateway-a",
        expected_epoch=1,
        new_gateway_id="gateway-b",
        event_id="claim-b",
    )
    rooms.claim_authority(
        db,
        room_id="room-1",
        expected_gateway_id="gateway-b",
        expected_epoch=2,
        new_gateway_id="gateway-c",
        event_id="claim-c",
    )

    with pytest.raises(rooms.AuthoritySupersededError, match="later superseded"):
        rooms.claim_authority(
            db,
            room_id="room-1",
            expected_gateway_id="gateway-a",
            expected_epoch=1,
            new_gateway_id="gateway-b",
            event_id="claim-b",
        )


def test_authority_scoped_events_require_gateway_and_epoch(tmp_path):
    db = tmp_path / "state.db"
    _create(db)

    with pytest.raises(rooms.HostedRoomError, match="authority_gateway_id"):
        rooms.append_event(
            db,
            room_id="room-1",
            event_id="turn-1",
            kind="member.unavailable",
            actor=GATEWAY_A,
            payload={"member": "ops"},
        )

    with pytest.raises(rooms.HostedRoomError, match="actor.id"):
        rooms.append_event(
            db,
            room_id="room-1",
            event_id="turn-2",
            kind="turn.started",
            actor=GATEWAY_B,
            authority_gateway_id="gateway-a",
            authority_epoch=1,
            payload={"member": "ops"},
        )

    with pytest.raises(rooms.HostedRoomError, match="only valid"):
        rooms.append_event(
            db,
            room_id="room-1",
            event_id="message-1",
            kind="message.user",
            actor=USER,
            authority_gateway_id="gateway-a",
            authority_epoch=1,
            payload={"text": "hello"},
        )


def test_append_is_idempotent_and_conflicting_event_id_is_rejected(tmp_path):
    db = tmp_path / "state.db"
    _create(db)

    first = rooms.append_event(
        db,
        room_id="room-1",
        event_id="event-1",
        kind="message.user",
        actor=USER,
        payload={"text": "hello"},
        now=20,
    )
    repeated = rooms.append_event(
        db,
        room_id="room-1",
        event_id="event-1",
        kind="message.user",
        actor=USER,
        payload={"text": "hello"},
        now=30,
    )

    assert first["seq"] == repeated["seq"] == 1
    assert repeated["idempotent"] is True
    assert rooms.read_events(db, room_id="room-1")["latest_seq"] == 1

    with pytest.raises(rooms.EventConflictError):
        rooms.append_event(
            db,
            room_id="room-1",
            event_id="event-1",
            kind="message.user",
            actor=USER,
            payload={"text": "changed"},
        )


def test_since_seq_returns_ordered_deltas_and_stable_cursor(tmp_path):
    db = tmp_path / "state.db"
    _create(db)
    for index in range(1, 5):
        rooms.append_event(
            db,
            room_id="room-1",
            event_id=f"event-{index}",
            kind="message.user",
            actor=USER,
            payload={"index": index},
            now=20 + index,
        )

    first = rooms.read_events(db, room_id="room-1", since_seq=0, limit=2)
    assert [event["seq"] for event in first["events"]] == [1, 2]
    assert first == {
        "events": first["events"],
        "cursor": 2,
        "latest_seq": 4,
        "has_more": True,
    }

    second = rooms.read_events(
        db,
        room_id="room-1",
        since_seq=first["cursor"],
        limit=2,
    )
    assert [event["seq"] for event in second["events"]] == [3, 4]
    assert second["cursor"] == 4
    assert second["has_more"] is False

    settled = rooms.read_events(db, room_id="room-1", since_seq=4)
    assert settled["events"] == []
    assert settled["cursor"] == settled["latest_seq"] == 4


def test_room_log_survives_store_reopen(tmp_path):
    db = tmp_path / "state.db"
    _create(db)
    rooms.append_event(
        db,
        room_id="room-1",
        event_id="event-1",
        kind="message.user",
        actor=USER,
        payload={"text": "persist me"},
    )

    assert rooms.list_rooms(db)[0]["name"] == "Release room"
    replay = rooms.read_events(db, room_id="room-1")
    assert replay["events"][0]["payload"] == {"text": "persist me"}


@pytest.mark.parametrize("session_first", [True, False])
def test_room_tables_coexist_with_session_db_schema(tmp_path, session_first):
    db = tmp_path / "state.db"
    if session_first:
        SessionDB(db_path=db).close()

    _create(db)
    rooms.append_event(
        db,
        room_id="room-1",
        event_id="event-1",
        kind="message.user",
        actor=USER,
        payload={"text": "shared database"},
    )

    SessionDB(db_path=db).close()
    replay = rooms.read_events(db, room_id="room-1")
    assert replay["latest_seq"] == 1
    assert replay["events"][0]["payload"]["text"] == "shared database"


def test_concurrent_appends_allocate_one_monotonic_sequence(tmp_path):
    db = tmp_path / "state.db"
    _create(db)

    def append(index):
        return rooms.append_event(
            db,
            room_id="room-1",
            event_id=f"event-{index}",
            kind="message.user",
            actor=USER,
            payload={"index": index},
        )

    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(append, range(24)))

    assert sorted(result["seq"] for result in results) == list(range(1, 25))
    replay = rooms.read_events(db, room_id="room-1", limit=100)
    assert [event["seq"] for event in replay["events"]] == list(range(1, 25))


def test_rolled_back_append_does_not_consume_sequence(tmp_path):
    db = tmp_path / "state.db"
    _create(db)

    conn = sqlite3.connect(db)
    try:
        conn.execute("BEGIN IMMEDIATE")
        conn.execute(
            """INSERT INTO hosted_room_events
               (room_id, seq, event_id, kind, actor_json, payload_json, created_at)
               VALUES ('room-1', 1, 'crash-event', 'message.user',
                       '{"id":"desktop-user","kind":"user"}', '{}', 20)"""
        )
        conn.execute("UPDATE hosted_rooms SET next_seq=2 WHERE room_id='room-1'")
        conn.rollback()
    finally:
        conn.close()

    event = rooms.append_event(
        db,
        room_id="room-1",
        event_id="after-restart",
        kind="message.user",
        actor=USER,
        payload={"text": "safe"},
    )
    assert event["seq"] == 1


@pytest.mark.parametrize(
    ("kwargs", "message"),
    [
        ({"room_id": "../escape"}, "invalid room_id"),
        ({"event_id": "event id"}, "invalid event_id"),
        ({"kind": "Message Created"}, "invalid event kind"),
        ({"kind": "directive.run"}, "cannot append"),
        ({"actor": {"kind": "member", "id": "ops"}}, "cannot append"),
        ({"payload": []}, "payload must be an object"),
    ],
)
def test_invalid_event_contract_is_rejected(tmp_path, kwargs, message):
    db = tmp_path / "state.db"
    _create(db)
    params = {
        "room_id": "room-1",
        "event_id": "event-1",
        "kind": "message.user",
        "actor": USER,
        "payload": {},
    }
    params.update(kwargs)

    with pytest.raises(rooms.HostedRoomError, match=message):
        rooms.append_event(db, **params)


def test_unknown_room_and_invalid_cursor_fail_closed(tmp_path):
    db = tmp_path / "state.db"
    _create(db)

    with pytest.raises(rooms.RoomNotFoundError):
        rooms.read_events(db, room_id="missing")
    with pytest.raises(rooms.HostedRoomError, match="since_seq"):
        rooms.read_events(db, room_id="room-1", since_seq=-1)
    with pytest.raises(rooms.HostedRoomError, match="ahead"):
        rooms.read_events(db, room_id="room-1", since_seq=1)
    with pytest.raises(rooms.HostedRoomError, match="limit"):
        rooms.read_events(db, room_id="room-1", limit=0)


def test_actor_is_part_of_event_idempotency_and_replay(tmp_path):
    db = tmp_path / "state.db"
    _create(db)
    event = rooms.append_event(
        db,
        room_id="room-1",
        event_id="event-1",
        kind="message.user",
        actor=USER,
        payload={"text": "hello"},
    )

    assert event["actor"] == USER
    assert rooms.read_events(db, room_id="room-1")["events"][0]["actor"] == USER

    with pytest.raises(rooms.EventConflictError):
        rooms.append_event(
            db,
            room_id="room-1",
            event_id="event-1",
            kind="message.user",
            actor={"kind": "user", "id": "another-user"},
            payload={"text": "hello"},
        )


def test_disband_is_idempotent_and_room_id_cannot_be_reused(tmp_path):
    db = tmp_path / "state.db"
    _create(db)

    first = rooms.disband_room(db, room_id="room-1", now=50)
    repeated = rooms.disband_room(db, room_id="room-1", now=60)

    assert first["room_id"] == "room-1"
    assert first["disbanded_at"] == 50.0
    assert first["idempotent"] is False
    assert first["event"]["kind"] == "room.disbanded"
    assert first["event"]["seq"] == 1
    assert first["event"]["payload"] == {"room_id": "room-1"}
    assert repeated["idempotent"] is True
    assert repeated["event"]["seq"] == first["event"]["seq"]
    assert rooms.list_rooms(db) == []
    deleted = rooms.list_rooms(db, include_disbanded=True)
    assert deleted[0]["disbanded_at"] == 50.0
    assert deleted[0]["latest_seq"] == 1
    state = rooms.room_state(db, room_id="room-1", include_disbanded=True)
    assert state["disbanded_at"] == 50.0
    assert state["latest_seq"] == 1
    replay = rooms.read_events(db, room_id="room-1", include_disbanded=True)
    assert [event["kind"] for event in replay["events"]] == ["room.disbanded"]
    with pytest.raises(rooms.RoomConflictError):
        _create(db)
    with pytest.raises(rooms.RoomNotFoundError):
        rooms.read_events(db, room_id="room-1")


def test_pre_actor_draft_database_migrates_with_explicit_legacy_identity(tmp_path):
    db = tmp_path / "state.db"
    _create_pre_actor_database(db)

    replay = rooms.read_events(db, room_id="room-1")
    assert replay["events"][0]["actor"] == {"kind": "system", "id": "legacy"}
    state = rooms.room_state(db, room_id="room-1")
    assert state["authority_gateway_id"] == "legacy"
    assert state["authority_epoch"] == 1
    adopted = rooms.create_room(
        db,
        room_id="room-1",
        name="Legacy",
        members=[],
        authority_gateway_id="gateway-a",
        now=2,
    )
    assert adopted["authority_gateway_id"] == "gateway-a"
    assert adopted["authority_epoch"] == 2
    assert adopted["adopted"] is True
    assert adopted["claim_event"]["seq"] == 2
    assert adopted["claim_event"]["payload"]["previous_gateway_id"] == "legacy"


def test_draft_schema_migration_is_safe_across_processes(tmp_path):
    db = tmp_path / "state.db"
    _create_pre_actor_database(db)

    with ProcessPoolExecutor(max_workers=4) as pool:
        results = list(pool.map(_read_legacy_state, [str(db)] * 4))

    assert results == [("legacy", 1)] * 4
    replay = rooms.read_events(db, room_id="room-1")
    assert replay["events"][0]["actor"] == {"kind": "system", "id": "legacy"}


def test_interrupted_draft_schema_migration_rolls_back_atomically(
    tmp_path,
    monkeypatch,
):
    db = tmp_path / "state.db"
    _create_pre_actor_database(db)
    original = rooms._initialize_schema

    def interrupt_after_first_alter(conn):
        conn.execute(
            "ALTER TABLE hosted_rooms "
            "ADD COLUMN authority_gateway_id TEXT NOT NULL DEFAULT 'legacy'"
        )
        raise RuntimeError("simulated migration interruption")

    monkeypatch.setattr(rooms, "_initialize_schema", interrupt_after_first_alter)
    with pytest.raises(RuntimeError, match="simulated migration interruption"):
        rooms.list_rooms(db)
    with sqlite3.connect(db) as conn:
        columns = {row[1] for row in conn.execute("PRAGMA table_info(hosted_rooms)")}
    assert "authority_gateway_id" not in columns

    monkeypatch.setattr(rooms, "_initialize_schema", original)
    assert rooms.room_state(db, room_id="room-1")["authority_gateway_id"] == "legacy"
