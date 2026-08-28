"""Integration tests for the hosted Discussion coordinator."""

from __future__ import annotations

import threading
import time
from pathlib import Path
from types import SimpleNamespace

from gateway import hosted_room_driver as driver
from gateway import hosted_rooms
from tui_gateway.hosted_room_service import HostedRoomService


class _FakeRPC:
    def __init__(self) -> None:
        self.sessions = {}
        self.calls = []
        self.fail_attachment_profiles = set()

    def resolve_exact(self, *, profile, title, source):
        return self.sessions.get((profile, title))

    def create(self, *, profile, title, source):
        session = {"session_id": f"{profile}-session", "title": title}
        self.sessions[(profile, title)] = session
        return session

    def resume(self, *, profile, session_id, source):
        return {"session_id": session_id}

    def submit(
        self,
        *,
        profile,
        session_id,
        prompt,
        source,
        task,
        execution_generation,
        on_terminal,
    ):
        self.calls.append(("submit", {"profile": profile, "prompt": prompt}))
        on_terminal({"status": "settled", "text": f"reply from {profile}"})
        return {"accepted": True}

    def stage_attachment(
        self,
        *,
        profile,
        session_id,
        source,
        attachment,
        data,
        execution_generation,
    ):
        self.calls.append((
            "stage_attachment",
            {
                "profile": profile,
                "attachment": dict(attachment),
                "data": data,
                "execution_generation": execution_generation,
            },
        ))
        if profile in self.fail_attachment_profiles:
            raise RuntimeError("attachment staging unavailable")
        return {
            "attached": True,
            **(
                {"ref_text": f"@file:attachments/{attachment['name']}"}
                if attachment["kind"] == "file"
                else {}
            ),
        }

    def begin_attachment_staging(
        self, *, profile, session_id, source, execution_generation
    ):
        self.calls.append((
            "begin_attachment_staging",
            {"profile": profile, "execution_generation": execution_generation},
        ))

    def commit_attachment_staging(
        self, *, profile, session_id, source, execution_generation
    ):
        self.calls.append((
            "commit_attachment_staging",
            {"profile": profile, "execution_generation": execution_generation},
        ))

    def rollback_attachment_staging(
        self, *, profile, session_id, source, execution_generation
    ):
        self.calls.append((
            "rollback_attachment_staging",
            {"profile": profile, "execution_generation": execution_generation},
        ))

    def history(self, *, profile, session_id, source):
        return []

    def info(self, *, profile, session_id, source):
        return {"active": False, "task_id": None}

    def interrupt(self, *, profile, session_id, source, expected_task_id):
        return {"interrupted": True}


def _server():
    return SimpleNamespace(_methods={}, _sessions={}, _sessions_lock=threading.Lock())


def _wait_for(predicate, timeout=2.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.01)
    raise AssertionError("condition was not reached")


def test_create_send_drive_publish_and_replay_without_client_transport(tmp_path: Path):
    db = tmp_path / "state.db"
    service = HostedRoomService(_server(), db_path=db)
    service.rpc = _FakeRPC()
    service.runtime.rpc = service.rpc
    service.local_profiles = lambda: ("default", "ops")
    room = service.create_room(
        room_id="room-1",
        name="Release room",
        members=[
            {"member_id": "default", "profile": "default", "handle": "hermes"},
            {"member_id": "ops", "profile": "ops", "handle": "ops"},
        ],
    )
    assert room["room_id"] == "room-1"

    service.start()
    service.send(
        room_id="room-1",
        event_id="user-1",
        payload={"text": "@ops inspect the release", "thread_id": "thread-1"},
    )
    _wait_for(
        lambda: any(
            event["kind"] == "message.member"
            for event in service._events("room-1")
        )
    )
    assert service.stop(timeout=1.0)

    events = service._events("room-1")
    assert [event["kind"] for event in events][:3] == [
        "message.user",
        "message.member",
        "turn.settled",
    ]
    assert events[1]["payload"]["text"] == "reply from ops"
    assert service.status("room-1")["working"] is False


def test_attachment_send_freezes_roster_stages_bytes_and_logs_metadata_only(
    tmp_path: Path,
):
    db = tmp_path / "state.db"
    service = HostedRoomService(_server(), db_path=db)
    rpc = _FakeRPC()
    service.rpc = rpc
    service.runtime.rpc = rpc
    service.local_profiles = lambda: ("default", "ops")
    service.create_room(
        room_id="room-1",
        name="Release room",
        members=[
            {"member_id": "default", "profile": "default", "handle": "hermes"},
            {"member_id": "ops", "profile": "ops", "handle": "ops"},
        ],
    )
    stored = service.put_attachment(
        room_id="room-1",
        upload_id="upload-1",
        kind="image",
        name="diagram.png",
        mime="image/png",
        data=b"\x89PNG\r\n\x1a\nimage",
    )
    manifest = {
        key: stored[key]
        for key in ("attachment_id", "kind", "name", "size", "mime")
    }

    service.start()
    service.send(
        room_id="room-1",
        event_id="user-attachment-1",
        payload={
            "text": "@ops inspect",
            "thread_id": "thread-1",
            "attachments": [manifest],
        },
    )
    _wait_for(lambda: any(method == "stage_attachment" for method, _ in rpc.calls))
    _wait_for(
        lambda: any(
            event["kind"] == "message.member" for event in service._events("room-1")
        )
    )
    assert service.stop(timeout=1.0)

    stage_index = next(index for index, call in enumerate(rpc.calls) if call[0] == "stage_attachment")
    submit_index = next(index for index, call in enumerate(rpc.calls) if call[0] == "submit")
    assert stage_index < submit_index
    assert rpc.calls[stage_index][1]["data"] == b"\x89PNG\r\n\x1a\nimage"
    user_event = service._events("room-1")[0]
    assert user_event["payload"]["attachments"] == [manifest]
    assert "PNG" not in repr(user_event)
    assert "base64" not in repr(user_event)
    try:
        service.read_attachment(
            room_id="room-1",
            attachment_id=stored["attachment_id"],
            recipient_member_id="late-member",
        )
    except ValueError:
        pass
    else:  # pragma: no cover - ownership must fail closed
        raise AssertionError("late member unexpectedly received historic attachment")


def test_partial_attachment_failure_never_submits_text_only_and_other_member_continues(
    tmp_path: Path,
):
    db = tmp_path / "state.db"
    service = HostedRoomService(_server(), db_path=db)
    rpc = _FakeRPC()
    rpc.fail_attachment_profiles.add("default")
    service.rpc = rpc
    service.runtime.rpc = rpc
    service.local_profiles = lambda: ("default", "ops")
    service.create_room(
        room_id="room-1",
        name="Release room",
        members=[
            {"member_id": "default", "profile": "default", "handle": "hermes"},
            {"member_id": "ops", "profile": "ops", "handle": "ops"},
        ],
    )
    stored = service.put_attachment(
        room_id="room-1",
        upload_id="upload-1",
        kind="file",
        name="notes.txt",
        mime="text/plain",
        data=b"release notes",
    )
    manifest = {
        key: stored[key]
        for key in ("attachment_id", "kind", "name", "size", "mime")
    }

    service.start()
    service.send(
        room_id="room-1",
        event_id="user-attachment-1",
        payload={
            "text": "Inspect this",
            "thread_id": "thread-1",
            "attachments": [manifest],
        },
    )
    _wait_for(
        lambda: any(
            event["kind"] == "message.member" for event in service._events("room-1")
        )
    )
    assert service.stop(timeout=1.0)

    events = service._events("room-1")
    failed = next(event for event in events if event["kind"] == "turn.failed")
    assert failed["payload"]["member_id"] == "default"
    assert any(
        event["kind"] == "message.member" and event["payload"]["member_id"] == "ops"
        for event in events
    )
    assert not any(
        method == "submit" and params["profile"] == "default"
        for method, params in rpc.calls
    )


def test_restart_republishes_terminal_task_before_admitting_more(tmp_path: Path):
    db = tmp_path / "state.db"
    service = HostedRoomService(_server(), db_path=db)
    service.local_profiles = lambda: ("default", "ops")
    service.create_room(
        room_id="room-1",
        name="Release room",
        members=[
            {"member_id": "default", "profile": "default", "handle": "hermes"},
            {"member_id": "ops", "profile": "ops", "handle": "ops"},
        ],
    )
    event = hosted_rooms.append_event(
        db,
        room_id="room-1",
        event_id="user-1",
        kind="message.user",
        actor={"kind": "user", "id": "desktop"},
        payload={"text": "@ops inspect", "thread_id": "thread-1"},
    )
    binding = service.bindings()[0]
    service.prepare_room(binding)
    task = driver.list_tasks(db, room_id="room-1", status="queued")[0]
    lease = driver.acquire_lease(
        db,
        room_id="room-1",
        gateway_id=binding.gateway_id,
        authority_epoch=binding.authority_epoch,
        process_generation="crashed",
        ttl_seconds=30,
        clock=time.time,
    )
    attempt = driver.start_task(
        db,
        task["identity"],
        lease,
        expected_cancel_generation=0,
        clock=time.time,
    )
    driver.settle_task(
        db,
        attempt,
        settlement_id="reply-1",
        status="settled",
        result={"text": "done"},
        clock=time.time,
    )

    service.prepare_room(binding)
    events = service._events("room-1")
    assert event["seq"] == 1
    assert sum(row["kind"] == "message.member" for row in events) == 1
    assert sum(row["kind"] == "turn.settled" for row in events) == 1
    service.prepare_room(binding)
    replayed = service._events("room-1")
    assert replayed == events


def test_stop_fence_prevents_the_next_room_member_from_starting(
    tmp_path: Path, monkeypatch
):
    db = tmp_path / "state.db"
    service = HostedRoomService(_server(), db_path=db)
    monkeypatch.setattr(service, "local_profiles", lambda: ("default", "ops"))
    service.create_room(
        room_id="room-1",
        name="Release room",
        members=[
            {"member_id": "default", "profile": "default", "handle": "hermes"},
            {"member_id": "ops", "profile": "ops", "handle": "ops"},
        ],
    )
    service.send(
        room_id="room-1",
        event_id="user-1",
        payload={"text": "Inspect the release", "thread_id": "thread-1"},
    )
    assert len(driver.list_tasks(db, room_id="room-1")) == 1

    assert service.stop_room("room-1", cancel_id="stop-1") == 1
    service.prepare_room(service.bindings()[0])

    tasks = driver.list_tasks(db, room_id="room-1")
    assert len(tasks) == 1
    assert tasks[0]["status"] == "cancelled"
    assert any(
        event["kind"] == "room.stop_requested"
        for event in service._events("room-1")
    )
