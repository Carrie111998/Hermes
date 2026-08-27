"""Messaging controls for gateway-hosted Bot rooms."""

from __future__ import annotations

import time
from concurrent.futures import ThreadPoolExecutor
from types import ModuleType, SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from gateway import hosted_room_driver, hosted_rooms
from gateway.config import GatewayConfig, Platform, PlatformConfig
from gateway.hosted_room_messaging import (
    MessagingRoomBackend,
    RoomControlError,
    command_form,
    format_room_detail,
    format_room_list,
    list_messaging_rooms,
    messaging_actor,
    messaging_event_id,
    parse_room_command,
    relay_provenance_is_unknown,
    resolve_room,
)
from gateway.platforms.base import MessageEvent, MessageType
from gateway.session import SessionSource
from hermes_cli.commands import resolve_command
from tui_gateway.hosted_room_service import HostedRoomService


class _FakeService:
    def __init__(self, db_path):
        self.db_path = db_path
        self.sent = []
        self.stopped = []
        self.room_status = {"running": True, "working": False, "blocked": False}

    def status(self, _room_id):
        return dict(self.room_status)

    def send(self, **kwargs):
        self.sent.append(kwargs)
        return {"seq": 1}

    def stop_room(self, room_id, *, cancel_id):
        self.stopped.append((room_id, cancel_id))
        return 2


class _TestHostedRoomService(HostedRoomService):
    """Hosted service with a fixed two-profile test roster."""

    def __init__(self, db_path):
        super().__init__(ModuleType("test_hosted_room_server"), db_path=db_path)

    def local_profiles(self) -> tuple[str, ...]:
        return ("default", "ops")


class _ImmediateRPC:
    """In-process room worker transport that settles without a model call."""

    def __init__(self):
        self.sessions = {}

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
        on_terminal({"status": "settled", "text": f"reply from {profile}"})
        return {"accepted": True}

    def history(self, *, profile, session_id, source):
        return []

    def info(self, *, profile, session_id, source):
        return {"active": False, "task_id": None}

    def interrupt(self, *, profile, session_id, source, expected_task_id):
        return {"interrupted": True}


class _HoldingRPC(_ImmediateRPC):
    """Keep a turn active until the owner observes a durable stop intent."""

    def __init__(self):
        super().__init__()
        self.active = {}
        self.interrupts = []

    def create(self, *, profile, title, source):
        session = super().create(profile=profile, title=title, source=source)
        self.active[session["session_id"]] = {"active": False, "task_id": None}
        return session

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
        self.active[session_id] = {"active": True, "task_id": task.task_id}
        return {"accepted": True}

    def info(self, *, profile, session_id, source):
        return dict(self.active[session_id])

    def interrupt(self, *, profile, session_id, source, expected_task_id):
        state = self.active[session_id]
        if state["task_id"] != expected_task_id:
            return {"interrupted": False}
        state["active"] = False
        self.interrupts.append(expected_task_id)
        return {"interrupted": True}


def _wait_for(predicate, timeout=2.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.01)
    raise AssertionError("condition was not reached")


def _seed_rooms(tmp_path):
    db = tmp_path / "state.db"
    authority = "install:test-gateway"
    first = hosted_rooms.create_room(
        db,
        room_id="release-room",
        name="Release room",
        members=[
            {"member_id": "default", "handle": "hermes"},
            {"member_id": "ops", "handle": "ops", "display_name": "Operations"},
        ],
        authority_gateway_id=authority,
    )
    second = hosted_rooms.create_room(
        db,
        room_id="research-room",
        name="Research room",
        members=[
            {"member_id": "default", "handle": "hermes"},
            {"member_id": "research", "handle": "research"},
        ],
        authority_gateway_id=authority,
    )
    return db, first, second


def _event(
    text: str,
    *,
    platform: Platform = Platform.SIGNAL,
    user_id: str = "user-1",
    message_id: str = "message-1",
    media: bool = False,
    is_bot: bool = False,
) -> MessageEvent:
    return MessageEvent(
        text=text,
        message_type=MessageType.COMMAND,
        user_id=user_id,
        user_name="Display Name",
        message_id=message_id,
        media_urls=["/tmp/image.png"] if media else [],
        media_types=["image/png"] if media else [],
        source=SessionSource(
            platform=platform,
            chat_id=f"chat-{platform.value}",
            chat_type="dm",
            user_id=user_id,
            user_name="Display Name",
            is_bot=is_bot,
        ),
    )


def _runner(*, platform: Platform = Platform.SIGNAL, extra=None):
    from gateway.run import GatewayRunner

    runner = object.__new__(GatewayRunner)
    effective_extra = (
        {"allow_admin_from": ["user-1"]} if extra is None else extra
    )
    runner.config = GatewayConfig(
        platforms={
            platform: PlatformConfig(
                enabled=True,
                token="***",
                extra=effective_extra,
            )
        }
    )
    return runner


def test_room_commands_are_gateway_dispatchable_without_interrupting_agent():
    rooms = resolve_command("rooms")
    assert rooms is not None and rooms.gateway_only and rooms.busy_policy == "dispatch"
    assert rooms.subcommands == ("send", "stop")
    assert resolve_command("room") is None


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        (
            "send Release room -- check the build -- then report",
            ("send", "Release room", "check the build -- then report"),
        ),
        ("stop Release room", ("stop", "Release room", "")),
    ],
)
def test_parse_room_command_keeps_names_and_message_content(raw, expected):
    parsed = parse_room_command(raw)
    assert (parsed.action, parsed.room_query, parsed.message) == expected


@pytest.mark.parametrize("raw", ["", "send room", "send -- hello", "stop"])
def test_parse_room_command_returns_actionable_usage(raw):
    with pytest.raises(RoomControlError, match="Use `/rooms"):
        parse_room_command(raw)


def test_room_resolution_is_exact_then_unique_prefix_then_substring(tmp_path):
    db, _, _ = _seed_rooms(tmp_path)
    rooms = hosted_rooms.list_rooms(db)
    assert resolve_room(rooms, "release-room")["name"] == "Release room"
    assert resolve_room(rooms, "Research")["room_id"] == "research-room"
    assert resolve_room(rooms, "lease")["room_id"] == "release-room"


def test_room_numbers_stay_stable_and_are_not_reused_after_disband(tmp_path):
    db, first, second = _seed_rooms(tmp_path)
    service = _FakeService(db)
    initial = {
        room["room_id"]: room["messaging_ref"]
        for room in list_messaging_rooms(service)
    }

    hosted_rooms.disband_room(db, room_id=first["room_id"])
    third = hosted_rooms.create_room(
        db,
        room_id="stop-signals",
        name="Stop signals",
        members=[],
        authority_gateway_id="install:test-gateway",
    )
    current = list_messaging_rooms(service)
    refs = {room["room_id"]: room["messaging_ref"] for room in current}

    assert refs[second["room_id"]] == initial[second["room_id"]]
    assert refs[third["room_id"]] > max(initial.values())
    assert resolve_room(current, str(refs[third["room_id"]]))["name"] == "Stop signals"


def test_missing_room_number_fails_closed_without_matching_a_numeric_name(tmp_path):
    db, _, _ = _seed_rooms(tmp_path)
    hosted_rooms.create_room(
        db,
        room_id="roadmap-2026",
        name="2026 roadmap",
        members=[],
        authority_gateway_id="install:test-gateway",
    )
    rooms = list_messaging_rooms(_FakeService(db))

    with pytest.raises(RoomControlError, match="numbered 2026"):
        resolve_room(rooms, "2026")


def test_numeric_internal_id_requires_an_explicit_advanced_escape(tmp_path):
    db, _, _ = _seed_rooms(tmp_path)
    numeric_id = hosted_rooms.create_room(
        db,
        room_id="1",
        name="Legacy numeric ID",
        members=[],
        authority_gateway_id="install:test-gateway",
    )
    rooms = list_messaging_rooms(_FakeService(db))

    assert resolve_room(rooms, "1")["room_id"] != numeric_id["room_id"]
    assert resolve_room(rooms, "id:1")["room_id"] == numeric_id["room_id"]


def test_room_numbers_allocate_once_across_concurrent_gateway_processes(tmp_path):
    db, _, _ = _seed_rooms(tmp_path)

    def load_refs(_index):
        service = _FakeService(db)
        return {
            room["room_id"]: room["messaging_ref"]
            for room in list_messaging_rooms(service)
        }

    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(load_refs, range(24)))

    assert all(result == results[0] for result in results)
    assert len(set(results[0].values())) == 2


def test_room_resolution_explains_ambiguity_and_missing_names(tmp_path):
    db, _, _ = _seed_rooms(tmp_path)
    hosted_rooms.create_room(
        db,
        room_id="release-notes",
        name="Release notes",
        members=[],
        authority_gateway_id="install:test-gateway",
    )
    rooms = hosted_rooms.list_rooms(db)
    with pytest.raises(RoomControlError, match="matches several rooms"):
        resolve_room(rooms, "release")
    with pytest.raises(RoomControlError, match="No hosted Bot room"):
        resolve_room(rooms, "missing")


@pytest.mark.asyncio
async def test_reserved_word_room_name_opens_detail_without_triggering_stop(
    tmp_path, monkeypatch
):
    db, _, _ = _seed_rooms(tmp_path)
    hosted_rooms.create_room(
        db,
        room_id="stop-signals",
        name="Stop signals",
        members=[],
        authority_gateway_id="install:test-gateway",
    )
    service = _FakeService(db)
    monkeypatch.setattr(
        "gateway.hosted_room_messaging.current_room_backend", lambda: service
    )

    result = await _runner()._handle_rooms_command(_event("/rooms Stop signals"))

    assert result.startswith("Stop signals —")
    assert service.stopped == []


@pytest.mark.asyncio
async def test_mutating_room_commands_require_the_stable_number(tmp_path, monkeypatch):
    db, _, _ = _seed_rooms(tmp_path)
    service = _FakeService(db)
    monkeypatch.setattr(
        "gateway.hosted_room_messaging.current_room_backend", lambda: service
    )

    result = await _runner()._handle_room_command(
        _event("/rooms stop Release room")
    )

    assert result == "Use `/rooms stop <room number>`."
    assert service.stopped == []


def test_room_list_and_detail_are_bounded_and_user_facing(tmp_path):
    db, release, _ = _seed_rooms(tmp_path)
    service = _FakeService(db)
    hosted_rooms.append_event(
        db,
        room_id=release["room_id"],
        event_id="user-1",
        kind="message.user",
        actor={"kind": "user", "id": "messaging:signal:abc", "display_name": "Signal"},
        payload={"text": "Please inspect the release", "thread_id": "thread-1"},
    )
    hosted_rooms.append_event(
        db,
        room_id=release["room_id"],
        event_id="member-1",
        kind="message.member",
        actor={"kind": "member", "id": "ops"},
        payload={
            "member_id": "ops",
            "text": "The release is ready",
            "thread_id": "thread-1",
            "task_id": "task-1",
            "turn_id": "turn-1",
            "round_index": 0,
        },
        authority_gateway_id="install:test-gateway",
        authority_epoch=1,
    )

    listing = format_room_list(service)
    assert "Hosted Bot rooms" in listing
    assert "1. Release room — waiting for the room host, 2 bots" in listing
    detail = format_room_detail(service, release)
    assert "Signal: Please inspect the release" in detail
    assert "Operations: The release is ready" in detail
    assert "Send: `/rooms send 1 -- <message>`" in detail
    assert "Stop: `/rooms stop 1`" in detail


def test_messaging_identity_is_stable_private_and_edit_safe():
    first = _event("/rooms send 1 -- hello", platform=Platform.TELEGRAM)
    first.platform_update_id = 123
    second = _event("/rooms send 1 -- edited", platform=Platform.TELEGRAM)
    second.platform_update_id = 124
    actor = messaging_actor(first, gateway_id="install:test-gateway")
    assert actor["display_name"] == "Display Name via Telegram"
    assert "user-1" not in actor["id"]
    assert messaging_event_id(first) != messaging_event_id(second)
    assert messaging_event_id(first) == messaging_event_id(first)


def test_transport_specific_command_forms_are_actionable():
    assert command_form(_event("/rooms", platform=Platform.SIGNAL)) == "/rooms"
    assert command_form(_event("/rooms", platform=Platform.SLACK)) == "/hermes rooms"
    assert command_form(_event("/rooms", platform=Platform.MATRIX)) == "!rooms"


@pytest.mark.parametrize(
    "raw_message",
    [
        {"timestamp_ms": 1770000000000},
        {"trigger_id": "slack-trigger-1"},
        SimpleNamespace(id="discord-interaction-1"),
    ],
)
def test_real_channel_raw_ids_are_stable_without_normalized_message_id(raw_message):
    event = _event("/rooms send 1 -- hello")
    event.message_id = None
    event.source.message_id = None
    event.raw_message = raw_message
    assert messaging_event_id(event) == messaging_event_id(event)


def test_mutation_fails_closed_without_a_transport_redelivery_id():
    event = _event("/rooms send 1 -- hello")
    event.message_id = None
    event.source.message_id = None
    with pytest.raises(RoomControlError, match="stable message ID"):
        messaging_event_id(event)


def test_signal_group_idempotency_includes_sender_identity():
    first = _event("/rooms send 1 -- hello", user_id="sender-a")
    second = _event("/rooms send 1 -- hello", user_id="sender-b")
    for event in (first, second):
        event.message_id = None
        event.source.message_id = None
        event.raw_message = {"timestamp_ms": 1770000000000}
    assert messaging_event_id(first) != messaging_event_id(second)


@pytest.mark.asyncio
@pytest.mark.parametrize("platform", [p for p in Platform if p is not Platform.LOCAL])
async def test_send_handler_is_shared_by_every_gateway_channel(
    tmp_path, monkeypatch, platform
):
    db, _, _ = _seed_rooms(tmp_path)
    service = _FakeService(db)
    monkeypatch.setattr(
        "gateway.hosted_room_messaging.current_room_backend", lambda: service
    )
    event = _event(
        "/rooms send 1 -- hello from messaging",
        platform=platform,
        message_id=f"message-{platform.value}",
    )
    result = await _runner(platform=platform)._handle_room_command(event)
    rooms_command = command_form(event)
    assert result == f"Queued in Release room. Check: `{rooms_command} 1`."
    assert service.sent[-1]["payload"]["text"] == "hello from messaging"
    platform_label = platform.value.replace("_", " ").title()
    assert service.sent[-1]["actor"]["display_name"] == (
        f"Display Name via {platform_label}"
    )


@pytest.mark.asyncio
async def test_send_rejects_attachments_instead_of_silently_dropping_them(
    tmp_path, monkeypatch
):
    db, _, _ = _seed_rooms(tmp_path)
    service = _FakeService(db)
    monkeypatch.setattr(
        "gateway.hosted_room_messaging.current_room_backend", lambda: service
    )
    result = await _runner()._handle_room_command(
        _event("/rooms send 1 -- inspect this", media=True)
    )
    assert "Attachments from messaging chats aren’t supported yet" in result
    assert service.sent == []


@pytest.mark.asyncio
async def test_bot_authored_controls_are_rejected_to_prevent_bridge_loops(
    tmp_path, monkeypatch
):
    db, _, _ = _seed_rooms(tmp_path)
    service = _FakeService(db)
    monkeypatch.setattr(
        "gateway.hosted_room_messaging.current_room_backend", lambda: service
    )
    result = await _runner(platform=Platform.DISCORD)._handle_room_command(
        _event(
            "/rooms send 1 -- repeat this",
            platform=Platform.DISCORD,
            is_bot=True,
        )
    )
    assert result == "Hosted Bot room controls are only available to people."
    assert service.sent == []


@pytest.mark.asyncio
async def test_raw_webhook_bot_marker_is_rejected_even_without_source_flag(
    tmp_path, monkeypatch
):
    db, _, _ = _seed_rooms(tmp_path)
    service = _FakeService(db)
    monkeypatch.setattr(
        "gateway.hosted_room_messaging.current_room_backend", lambda: service
    )
    event = _event("/rooms send 1 -- repeat this")
    event.raw_message = {"subtype": "bot_message", "bot_id": "B123"}
    result = await _runner()._handle_rooms_command(event)
    assert result == "Hosted Bot room controls are only available to people."
    assert service.sent == []


def test_relay_and_session_roundtrip_preserve_bot_provenance():
    from gateway.relay.ws_transport import _event_from_wire

    source = SessionSource(
        platform=Platform.DISCORD,
        chat_id="chat-1",
        user_id="bot-1",
        is_bot=True,
    )
    assert SessionSource.from_dict(source.to_dict()).is_bot is True
    relayed = _event_from_wire(
        {
            "text": "/rooms",
            "message_type": "command",
            "source": source.to_dict(),
        }
    )
    assert relayed.source.is_bot is True
    assert relay_provenance_is_unknown(relayed) is False


def test_legacy_relay_without_author_classification_fails_closed():
    from gateway.relay.ws_transport import _event_from_wire

    relayed = _event_from_wire(
        {
            "text": "/rooms",
            "message_type": "command",
            "source": {
                "platform": "discord",
                "chat_id": "chat-1",
                "chat_type": "dm",
                "user_id": "user-1",
            },
        }
    )
    assert relay_provenance_is_unknown(relayed) is True


@pytest.mark.asyncio
async def test_stop_requires_admin_when_operator_enabled_slash_gating(
    tmp_path, monkeypatch
):
    db, _, _ = _seed_rooms(tmp_path)
    service = _FakeService(db)
    monkeypatch.setattr(
        "gateway.hosted_room_messaging.current_room_backend", lambda: service
    )
    extra = {
        "allow_admin_from": ["admin"],
        "user_allowed_commands": ["rooms"],
    }
    result = await _runner(extra=extra)._handle_room_command(
        _event("/rooms stop 1", user_id="member")
    )
    assert result.startswith("Room controls are off for this chat")
    assert service.stopped == []

    result = await _runner(extra=extra)._handle_room_command(
        _event("/rooms stop 1", user_id="admin", message_id="stop-1")
    )
    assert result == (
        "Stop requested for Release room. Active work will stop safely. "
        "Check: `/rooms 1`."
    )
    assert service.stopped[0][0] == "release-room"


@pytest.mark.asyncio
async def test_room_history_and_mutation_require_an_explicit_admin(
    tmp_path, monkeypatch
):
    db, _, _ = _seed_rooms(tmp_path)
    service = _FakeService(db)
    monkeypatch.setattr(
        "gateway.hosted_room_messaging.current_room_backend", lambda: service
    )
    runner = _runner(extra={})
    denial = "Room controls are off for this chat"
    assert denial in await runner._handle_rooms_command(_event("/rooms"))
    assert denial in await runner._handle_room_command(
        _event("/rooms send 1 -- hello")
    )
    assert service.sent == []


@pytest.mark.asyncio
async def test_busy_dispatch_runs_room_control_without_touching_main_agent():
    runner = _runner()
    runner._handle_rooms_command = AsyncMock(return_value="room-dispatched")
    event = _event("/rooms send 1 -- hello")
    result = await runner._dispatch_busy_slash_command(
        event,
        resolve_command("rooms"),
        "session-key",
        event.source,
    )
    assert result == "room-dispatched"
    runner._handle_rooms_command.assert_awaited_once_with(event)


@pytest.mark.asyncio
async def test_real_service_persists_server_owned_messaging_actor(tmp_path, monkeypatch):
    service = _TestHostedRoomService(tmp_path / "state.db")
    service.create_room(
        room_id="release-room",
        name="Release room",
        members=[
            {"member_id": "default", "profile": "default", "handle": "hermes"},
            {"member_id": "ops", "profile": "ops", "handle": "ops"},
        ],
    )
    monkeypatch.setattr(
        "gateway.hosted_room_messaging.current_room_backend",
        lambda: MessagingRoomBackend(db_path=service.db_path, service=service),
    )
    event = _event(
        "/rooms send 1 -- @ops inspect the release",
        platform=Platform.SIGNAL,
    )
    result = await _runner()._handle_room_command(event)
    assert result == "Queued in Release room. Check: `/rooms 1`."
    delta = hosted_rooms.read_events(
        service.db_path,
        room_id="release-room",
        since_seq=0,
        limit=20,
    )
    user_event = next(row for row in delta["events"] if row["kind"] == "message.user")
    assert user_event["actor"]["display_name"] == "Display Name via Signal"
    assert user_event["actor"]["id"].startswith("messaging:signal:")
    assert "user-1" not in user_event["actor"]["id"]


def test_cross_process_store_wakes_owner_without_desktop_transport(tmp_path):
    service = _TestHostedRoomService(tmp_path / "state.db")
    service.rpc = _ImmediateRPC()
    service.runtime.rpc = service.rpc
    service.create_room(
        room_id="release-room",
        name="Release room",
        members=[
            {"member_id": "default", "profile": "default", "handle": "hermes"},
            {"member_id": "ops", "profile": "ops", "handle": "ops"},
        ],
    )
    messaging_process = MessagingRoomBackend(db_path=service.db_path)
    first_event = _event(
        "/rooms send 1 -- @ops inspect the release",
        platform=Platform.WHATSAPP,
        message_id="message-1",
    )
    second_event = _event(
        "/rooms send 1 -- @ops check the notes",
        platform=Platform.WHATSAPP,
        message_id="message-2",
    )
    for event, text in (
        (first_event, "@ops inspect the release"),
        (second_event, "@ops check the notes"),
    ):
        event_id = messaging_event_id(event)
        messaging_process.send(
            room_id="release-room",
            event_id=event_id,
            payload={"text": text, "thread_id": event_id},
            actor=messaging_actor(event, gateway_id="install:test-gateway"),
        )
    service.start()
    try:
        _wait_for(
            lambda: sum(
                row["kind"] == "message.member"
                for row in hosted_rooms.read_events(
                    service.db_path,
                    room_id="release-room",
                    since_seq=0,
                    limit=40,
                )["events"]
            )
            == 2
        )
    finally:
        assert service.stop(timeout=1.0)

    events = hosted_rooms.read_events(
        service.db_path,
        room_id="release-room",
        since_seq=0,
        limit=40,
    )["events"]
    assert [row["kind"] for row in events[:2]] == ["message.user", "message.user"]
    assert sum(row["kind"] == "message.member" for row in events) == 2
    assert events[0]["actor"]["display_name"] == "Display Name via Whatsapp"


def test_cross_process_stop_cancels_durable_queued_work(tmp_path):
    service = _TestHostedRoomService(tmp_path / "state.db")
    service.create_room(
        room_id="release-room",
        name="Release room",
        members=[
            {"member_id": "default", "profile": "default", "handle": "hermes"},
            {"member_id": "ops", "profile": "ops", "handle": "ops"},
        ],
    )
    service.send(
        room_id="release-room",
        event_id="user-1",
        payload={"text": "@ops inspect", "thread_id": "thread-1"},
    )
    messaging_process = MessagingRoomBackend(db_path=service.db_path)
    assert messaging_process.stop_room("release-room", cancel_id="stop-1") == 1
    assert messaging_process.stop_room("release-room", cancel_id="stop-1") == 1
    service.prepare_room(service.bindings()[0])
    assert messaging_process.status("release-room")["working"] is False
    tasks = hosted_room_driver.list_tasks(service.db_path, room_id="release-room")
    assert [task["status"] for task in tasks] == ["cancelled"]


def test_cross_process_send_is_idempotent_on_transport_redelivery(tmp_path):
    db, room, _ = _seed_rooms(tmp_path)
    messaging_process = MessagingRoomBackend(db_path=db)
    event = _event("/rooms send 1 -- hello", platform=Platform.TELEGRAM)
    event_id = messaging_event_id(event)
    payload = {"text": "hello", "thread_id": event_id}
    actor = messaging_actor(event, gateway_id="install:test-gateway")
    first = messaging_process.send(
        room_id=room["room_id"],
        event_id=event_id,
        payload=payload,
        actor=actor,
    )
    second = messaging_process.send(
        room_id=room["room_id"],
        event_id=event_id,
        payload=payload,
        actor=actor,
    )
    assert first["seq"] == second["seq"] == 1
    assert second["idempotent"] is True
    delta = hosted_rooms.read_events(
        db, room_id=room["room_id"], since_seq=0, limit=20
    )
    assert len(delta["events"]) == 1


def test_cross_process_stop_is_acknowledged_by_owner_for_running_work(tmp_path):
    service = _TestHostedRoomService(tmp_path / "state.db")
    service.rpc = _HoldingRPC()
    service.runtime.rpc = service.rpc
    service.create_room(
        room_id="release-room",
        name="Release room",
        members=[
            {"member_id": "default", "profile": "default", "handle": "hermes"},
            {"member_id": "ops", "profile": "ops", "handle": "ops"},
        ],
    )
    messaging_process = MessagingRoomBackend(db_path=service.db_path)
    event = _event("/rooms send 1 -- inspect")
    messaging_process.send(
        room_id="release-room",
        event_id=messaging_event_id(event),
        payload={"text": "inspect", "thread_id": messaging_event_id(event)},
        actor=messaging_actor(event, gateway_id="install:test-gateway"),
    )
    service.start()
    try:
        _wait_for(
            lambda: any(
                task["status"] == "running"
                for task in hosted_room_driver.list_tasks(
                    service.db_path, room_id="release-room"
                )
            )
        )
        assert messaging_process.stop_room("release-room", cancel_id="stop-1") == 1
        _wait_for(
            lambda: hosted_room_driver.list_tasks(
                service.db_path, room_id="release-room"
            )[0]["status"]
            == "cancelled"
        )
    finally:
        assert service.stop(timeout=1.0)

    assert len(service.rpc.interrupts) == 1
    tasks = hosted_room_driver.list_tasks(service.db_path, room_id="release-room")
    assert len(tasks) == 1
    assert tasks[0]["status"] == "cancelled"
