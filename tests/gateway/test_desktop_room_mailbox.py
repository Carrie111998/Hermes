from __future__ import annotations

import pytest

from gateway import desktop_room_mailbox as mailbox


class Clock:
    def __init__(self, value: float = 1000.0) -> None:
        self.value = value

    def __call__(self) -> float:
        return self.value


def test_enqueue_is_idempotent_and_rejects_key_reuse(tmp_path):
    db = tmp_path / "state.db"
    first = mailbox.enqueue_command(
        db,
        command_id="messaging:abc",
        room_id="room-1",
        action="send",
        payload={"message": "hello"},
    )
    replay = mailbox.enqueue_command(
        db,
        command_id="messaging:abc",
        room_id="room-1",
        action="send",
        payload={"message": "hello"},
    )

    assert first["state"] == "pending"
    assert replay["idempotent"] is True
    with pytest.raises(mailbox.DesktopRoomMailboxError, match="different room work"):
        mailbox.enqueue_command(
            db,
            command_id="messaging:abc",
            room_id="room-1",
            action="send",
            payload={"message": "changed"},
        )


def test_enqueue_moves_the_cross_process_pending_signal(tmp_path):
    db = tmp_path / "desktop_room_mailbox.db"
    signal = mailbox.pending_signal_path(db)

    mailbox.enqueue_command(
        db,
        command_id="messaging:first",
        room_id="room-1",
        action="send",
        payload={"message": "hello"},
    )
    first = signal.read_text(encoding="ascii")
    mailbox.enqueue_command(
        db,
        command_id="messaging:second",
        room_id="room-1",
        action="send",
        payload={"message": "again"},
    )

    assert signal.read_text(encoding="ascii") != first
    assert signal.stat().st_mode & 0o777 == 0o600


def test_signal_failure_does_not_change_a_durable_enqueue(tmp_path, monkeypatch):
    db = tmp_path / "desktop_room_mailbox.db"
    mailbox.enqueue_command(
        db,
        command_id="messaging:seed",
        room_id="room-1",
        action="send",
        payload={"message": "seed"},
    )
    monkeypatch.setattr(
        type(mailbox.pending_signal_path(db)),
        "write_text",
        lambda *args, **kwargs: (_ for _ in ()).throw(OSError("read only")),
    )

    queued = mailbox.enqueue_command(
        db,
        command_id="messaging:still-durable",
        room_id="room-1",
        action="send",
        payload={"message": "hello"},
    )

    assert queued["state"] == "pending"


def test_one_desktop_claims_and_presence_is_room_scoped(tmp_path):
    db = tmp_path / "state.db"
    clock = Clock()
    mailbox.enqueue_command(
        db,
        command_id="messaging:one",
        room_id="name:Classic room",
        action="send",
        payload={"message": "hello"},
        clock=clock,
    )

    claimed = mailbox.claim_commands(
        db,
        consumer_id="desktop:first",
        room_ids=["name:Classic room"],
        clock=clock,
    )
    duplicate = mailbox.claim_commands(
        db,
        consumer_id="desktop:second",
        room_ids=["name:Classic room"],
        clock=clock,
    )

    assert [item["command_id"] for item in claimed] == ["messaging:one"]
    assert duplicate == []
    assert mailbox.room_available(db, "name:Classic room", clock=clock) is True
    assert mailbox.room_available(db, "another-room", clock=clock) is False


def test_expired_claim_moves_to_another_desktop(tmp_path):
    db = tmp_path / "state.db"
    clock = Clock()
    mailbox.enqueue_command(
        db,
        command_id="messaging:retry",
        room_id="room-1",
        action="send",
        payload={"message": "hello"},
        clock=clock,
    )
    first = mailbox.claim_commands(
        db,
        consumer_id="desktop:first",
        room_ids=["room-1"],
        claim_ttl=10,
        presence_ttl=10,
        clock=clock,
    )
    clock.value += 11
    second = mailbox.claim_commands(
        db,
        consumer_id="desktop:second",
        room_ids=["room-1"],
        clock=clock,
    )

    assert first[0]["attempts"] == 1
    assert second[0]["attempts"] == 2
    with pytest.raises(mailbox.DesktopRoomMailboxError, match="no longer owned"):
        mailbox.complete_command(
            db,
            consumer_id="desktop:first",
            command_id="messaging:retry",
            lease_token=first[0]["lease_token"],
            success=True,
            result={"thread_id": "old"},
            clock=clock,
        )


def test_completion_ack_retry_is_idempotent(tmp_path):
    db = tmp_path / "state.db"
    mailbox.enqueue_command(
        db,
        command_id="messaging:complete",
        room_id="room-1",
        action="stop",
        payload={},
    )
    claimed = mailbox.claim_commands(
        db,
        consumer_id="desktop:first",
        room_ids=["room-1"],
    )
    first = mailbox.complete_command(
        db,
        consumer_id="desktop:first",
        command_id="messaging:complete",
        lease_token=claimed[0]["lease_token"],
        success=True,
        result={"stopped": True},
    )
    replay = mailbox.complete_command(
        db,
        consumer_id="desktop:first",
        command_id="messaging:complete",
        lease_token=claimed[0]["lease_token"],
        success=True,
        result={"stopped": True},
    )

    assert first["state"] == "completed"
    assert replay["idempotent"] is True


def test_reclaim_rotates_token_and_fences_a_stale_attempt(tmp_path):
    db = tmp_path / "state.db"
    clock = Clock()
    mailbox.enqueue_command(
        db,
        command_id="messaging:fenced",
        room_id="room-1",
        action="send",
        payload={"message": "hello"},
        clock=clock,
    )
    first = mailbox.claim_commands(
        db,
        consumer_id="desktop:same-install",
        room_ids=["room-1"],
        claim_ttl=5,
        clock=clock,
    )[0]
    clock.value += 6
    second = mailbox.claim_commands(
        db,
        consumer_id="desktop:same-install",
        room_ids=["room-1"],
        claim_ttl=5,
        clock=clock,
    )[0]

    assert first["lease_token"] != second["lease_token"]
    with pytest.raises(mailbox.DesktopRoomMailboxError, match="no longer owned"):
        mailbox.complete_command(
            db,
            consumer_id="desktop:same-install",
            command_id="messaging:fenced",
            lease_token=first["lease_token"],
            success=True,
            result={"thread_id": "old"},
            clock=clock,
        )


def test_live_claim_can_be_renewed(tmp_path):
    db = tmp_path / "state.db"
    clock = Clock()
    mailbox.enqueue_command(
        db,
        command_id="messaging:renew",
        room_id="room-1",
        action="send",
        payload={"message": "hello"},
        clock=clock,
    )
    claimed = mailbox.claim_commands(
        db,
        consumer_id="desktop:first",
        room_ids=["room-1"],
        claim_ttl=10,
        clock=clock,
    )[0]
    clock.value += 8
    renewed = mailbox.renew_command(
        db,
        consumer_id="desktop:first",
        command_id="messaging:renew",
        lease_token=claimed["lease_token"],
        claim_ttl=10,
        clock=clock,
    )
    clock.value += 5

    assert renewed["lease_token"] == claimed["lease_token"]
    assert mailbox.complete_command(
        db,
        consumer_id="desktop:first",
        command_id="messaging:renew",
        lease_token=claimed["lease_token"],
        success=True,
        result={"thread_id": "thread-1"},
        clock=clock,
    )["state"] == "completed"


def test_presence_expires_without_deleting_pending_work(tmp_path):
    db = tmp_path / "state.db"
    clock = Clock()
    mailbox.enqueue_command(
        db,
        command_id="messaging:later",
        room_id="room-1",
        action="send",
        payload={"message": "later"},
        clock=clock,
    )
    mailbox.claim_commands(
        db,
        consumer_id="desktop:first",
        room_ids=["room-1"],
        presence_ttl=5,
        claim_ttl=5,
        clock=clock,
    )
    clock.value += 6

    assert mailbox.room_available(db, "room-1", clock=clock) is False
    reclaimed = mailbox.claim_commands(
        db,
        consumer_id="desktop:second",
        room_ids=["room-1"],
        clock=clock,
    )
    assert reclaimed[0]["command_id"] == "messaging:later"


def test_default_presence_overlaps_the_minute_desktop_backstop(tmp_path):
    db = tmp_path / "state.db"
    clock = Clock()
    mailbox.claim_commands(
        db,
        consumer_id="desktop:first",
        room_ids=["room-1"],
        clock=clock,
    )

    clock.value += 61
    assert mailbox.room_available(db, "room-1", clock=clock) is True
    clock.value += 30
    assert mailbox.room_available(db, "room-1", clock=clock) is False


def test_latest_command_state_is_scoped_per_room(tmp_path):
    db = tmp_path / "state.db"
    clock = Clock()
    mailbox.enqueue_command(
        db,
        command_id="messaging:first",
        room_id="room-1",
        action="send",
        payload={"message": "first"},
        clock=clock,
    )
    clock.value += 1
    mailbox.enqueue_command(
        db,
        command_id="messaging:second",
        room_id="room-1",
        action="send",
        payload={"message": "second"},
        clock=clock,
    )
    mailbox.enqueue_command(
        db,
        command_id="messaging:other",
        room_id="room-2",
        action="stop",
        payload={},
        clock=clock,
    )

    states = mailbox.latest_command_states(db, ["room-1", "room-2"])

    assert states["room-1"]["command_id"] == "messaging:second"
    assert states["room-2"]["command_id"] == "messaging:other"


def test_paged_claims_preserve_presence_for_every_owned_room(tmp_path):
    db = tmp_path / "state.db"
    clock = Clock()
    rooms = [f"room-{index}" for index in range(260)]

    for index in range(0, len(rooms), mailbox.MAX_ROOM_IDS):
        mailbox.claim_commands(
            db,
            consumer_id="desktop:first",
            room_ids=rooms[index : index + mailbox.MAX_ROOM_IDS],
            clock=clock,
        )

    assert mailbox.available_room_ids(db, rooms, clock=clock) == set(rooms)
