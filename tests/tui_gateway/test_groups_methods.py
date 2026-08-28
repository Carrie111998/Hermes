"""Tests for the gateway-hosted ``groups.*`` JSON-RPC contract."""

from __future__ import annotations

import hashlib
import base64

import pytest

import tui_gateway.server as srv


@pytest.fixture
def home(tmp_path, monkeypatch):
    path = tmp_path / ".hermes"
    path.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(path))
    return path


def _result(envelope):
    assert "error" not in envelope, envelope
    return envelope["result"]


def _server_authority():
    from gateway.hosted_rooms import local_authority_gateway_id

    return local_authority_gateway_id()


def _create_room():
    return _result(
        srv._methods["groups.create"](
            1,
            {
                "room_id": "room-1",
                "name": "Release room",
                "members": [{"profile": "ops", "handle": "ops"}],
                "authority_gateway_id": "gateway-a",
            },
        )
    )["room"]


def test_capabilities_are_honest_about_the_driver_boundary(home):
    result = _result(srv._methods["groups.capabilities"](1, {}))

    assert result["protocol_version"] == 2
    assert result["driver"] is False
    assert result["authority_gateway_id"] == _server_authority()
    assert "authority_epoch" in result["features"]
    assert "coordinator_fencing" in result["features"]
    assert "desktop_compatibility_mailbox" in result["features"]
    assert "monotonic_log" in result["features"]
    assert "groups.state" in result["methods"]
    assert "groups.send" in result["methods"]
    assert "groups.attachment.put" in result["methods"]
    assert "groups.attachment.read" in result["methods"]
    assert "attachment_same_gateway_delivery" in result["features"]
    assert "groups.desktop.claim" in result["methods"]
    assert "groups.desktop.renew" in result["methods"]
    assert "groups.desktop.complete" in result["methods"]


def test_desktop_mailbox_rpc_claim_and_complete(home):
    from gateway.desktop_room_mailbox import default_db_path, enqueue_command

    enqueue_command(
        default_db_path(),
        command_id="messaging:one",
        room_id="classic-room",
        authority_hash=hashlib.sha256(b"authority:test").hexdigest(),
        action="send",
        payload={"message": "hello"},
    )

    claimed = _result(
        srv._methods["groups.desktop.claim"](
            1,
            {
                "consumer_id": "desktop:test",
                "room_authorities": [
                    {
                        "room_id": "classic-room",
                        "authority_token": "authority:test",
                    }
                ],
            },
        )
    )["commands"]
    assert [item["command_id"] for item in claimed] == ["messaging:one"]

    renewed = _result(
        srv._methods["groups.desktop.renew"](
            2,
            {
                "consumer_id": "desktop:test",
                "command_id": "messaging:one",
                "lease_token": claimed[0]["lease_token"],
            },
        )
    )["command"]
    assert renewed["lease_token"] == claimed[0]["lease_token"]

    completed = _result(
        srv._methods["groups.desktop.complete"](
            3,
            {
                "consumer_id": "desktop:test",
                "command_id": "messaging:one",
                "lease_token": claimed[0]["lease_token"],
                "success": True,
                "result": {"thread_id": "thread-1"},
            },
        )
    )["command"]
    assert completed["state"] == "completed"


def test_create_list_send_and_log_roundtrip(home):
    room = _create_room()
    assert room["idempotent"] is False

    listed = _result(srv._methods["groups.list"](2, {}))
    assert [item["room_id"] for item in listed["rooms"]] == ["room-1"]
    state = _result(srv._methods["groups.state"](3, {"room_id": "room-1"}))
    assert state["room"]["authority_gateway_id"] == _server_authority()
    assert state["room"]["authority_epoch"] == 1
    assert state["room"]["latest_seq"] == 0

    sent = _result(
        srv._methods["groups.send"](
            4,
            {
                "room_id": "room-1",
                "event_id": "event-1",
                "actor": {"kind": "user", "id": "desktop-user"},
                "payload": {"text": "hello"},
            },
        )
    )
    assert sent["accepted"] is True
    assert sent["driver_started"] is False
    assert sent["event"]["seq"] == 1
    assert sent["event"]["kind"] == "message.user"
    assert sent["event"]["actor"] == {"kind": "user", "id": "desktop"}

    replay = _result(
        srv._methods["groups.log"](
            5,
            {"room_id": "room-1", "since_seq": 0},
        )
    )
    assert replay["latest_seq"] == replay["cursor"] == 1
    assert replay["events"][0]["payload"] == {
        "text": "hello",
        "thread_id": "event-1",
    }


def test_rpc_retry_is_idempotent_and_conflict_is_visible(home):
    _create_room()
    params = {
        "room_id": "room-1",
        "event_id": "event-1",
        "actor": {"kind": "user", "id": "desktop-user"},
        "payload": {"text": "hello"},
    }
    first = _result(srv._methods["groups.send"](2, params))
    repeated = _result(srv._methods["groups.send"](3, params))

    assert first["event"]["seq"] == repeated["event"]["seq"] == 1
    assert repeated["event"]["idempotent"] is True

    conflict = srv._methods["groups.send"](
        4,
        {**params, "payload": {"text": "different"}},
    )
    assert conflict["error"]["code"] == 4111
    assert "different content" in conflict["error"]["message"]


def test_attachment_put_send_read_roundtrip_is_bounded_and_recipient_scoped(home):
    _create_room()
    encoded = base64.b64encode(b"\x89PNG\r\n\x1a\nimage").decode("ascii")
    put_params = {
        "room_id": "room-1",
        "upload_id": "upload-1",
        "kind": "image",
        "name": "diagram.png",
        "mime": "image/png",
        "content_base64": encoded,
    }
    first = _result(srv._methods["groups.attachment.put"](1, put_params))["attachment"]
    repeated = _result(srv._methods["groups.attachment.put"](2, put_params))["attachment"]
    assert repeated["attachment_id"] == first["attachment_id"]
    assert repeated["idempotent"] is True

    before_send = srv._methods["groups.attachment.read"](
        3,
        {
            "room_id": "room-1",
            "attachment_id": first["attachment_id"],
            "purpose": "viewer",
        },
    )
    assert before_send["error"]["code"] == 4141

    manifest = {
        key: first[key]
        for key in ("attachment_id", "kind", "name", "size", "mime")
    }
    sent = _result(
        srv._methods["groups.send"](
            4,
            {
                "room_id": "room-1",
                "event_id": "event-attachment-1",
                "payload": {
                    "text": "",
                    "thread_id": "thread-1",
                    "attachments": [manifest],
                },
            },
        )
    )
    assert sent["event"]["payload"]["attachments"] == [manifest]
    assert "content_base64" not in sent["event"]["payload"]

    hosted_read = srv._methods["groups.attachment.read"](
        5,
        {
            "room_id": "room-1",
            "attachment_id": first["attachment_id"],
            "purpose": "viewer",
            "event_id": "event-attachment-1",
        },
    )
    assert base64.b64decode(_result(hosted_read)["content_base64"]) == b"\x89PNG\r\n\x1a\nimage"
    denied = srv._methods["groups.attachment.read"](
        6,
        {
            "room_id": "room-1",
            "attachment_id": first["attachment_id"],
            "purpose": "desktop-command",
        },
    )
    assert denied["error"]["code"] == 4141


def test_classic_attachment_read_requires_room_authority_and_live_command_lease(home):
    from gateway import desktop_room_mailbox
    from gateway.hosted_room_attachments import HostedRoomAttachmentStore
    from gateway.hosted_rooms import default_db_path as hosted_db_path

    db = desktop_room_mailbox.default_db_path()
    store = HostedRoomAttachmentStore(hosted_db_path())
    stored = store.put(
        room_id="classic-room",
        upload_id="upload-classic-1",
        kind="file",
        name="notes.txt",
        mime="text/plain",
        data=b"release notes",
    )
    manifest = {
        key: stored[key]
        for key in ("attachment_id", "kind", "name", "size", "mime")
    }
    store.commit_message(
        room_id="classic-room",
        event_id="messaging:classic-attachment",
        manifest=[manifest],
        recipient_member_ids=("desktop", "viewer"),
    )
    desktop_room_mailbox.enqueue_command(
        db,
        command_id="messaging:classic-attachment",
        room_id="classic-room",
        authority_hash=hashlib.sha256(b"authority:test").hexdigest(),
        action="send",
        payload={"message": "", "attachments": [manifest]},
    )
    command = desktop_room_mailbox.claim_commands(
        db,
        consumer_id="desktop:test",
        room_authorities=[{
            "room_id": "classic-room",
            "authority_token": "authority:test",
        }],
    )[0]
    params = {
        "room_id": "classic-room",
        "attachment_id": stored["attachment_id"],
        "purpose": "desktop-command",
        "event_id": "messaging:classic-attachment",
        "consumer_id": "desktop:test",
        "lease_token": command["lease_token"],
        "authority_token": "authority:test",
    }

    denied = srv._methods["groups.attachment.read"](
        1,
        {**params, "authority_token": "authority:wrong"},
    )
    assert denied["error"]["code"] == 4141
    read = _result(srv._methods["groups.attachment.read"](2, params))
    assert base64.b64decode(read["content_base64"]) == b"release notes"
    viewer = _result(
        srv._methods["groups.attachment.read"](
            3,
            {
                "room_id": "classic-room",
                "attachment_id": stored["attachment_id"],
                "event_id": "messaging:classic-attachment",
                "purpose": "viewer",
            },
        )
    )
    assert base64.b64decode(viewer["content_base64"]) == b"release notes"


def test_send_does_not_trust_client_supplied_actor_identity(home):
    _create_room()
    sent = _result(
        srv._methods["groups.send"](
            2,
            {
                "room_id": "room-1",
                "event_id": "event-1",
                "actor": {"kind": "user", "id": "spoofed-user"},
                "payload": {"text": "hello"},
            },
        )
    )

    assert sent["event"]["actor"] == {"kind": "user", "id": "desktop"}


def test_create_ignores_client_supplied_authority_identity(home):
    created = _result(
        srv._methods["groups.create"](
            1,
            {"room_id": "legacy-room", "name": "Legacy", "members": []},
        )
    )["room"]
    retried = _result(
        srv._methods["groups.create"](
            2,
            {
                "room_id": "legacy-room",
                "name": "Legacy",
                "members": [],
                "authority_gateway_id": "spoofed-gateway",
            },
        )
    )["room"]

    assert created["authority_gateway_id"] == _server_authority()
    assert retried["authority_gateway_id"] == _server_authority()
    assert retried["idempotent"] is True


def test_legacy_room_adoption_emits_one_lineage_receipt(home):
    from gateway.hosted_rooms import create_room, default_db_path

    members = [{"profile": "ops", "handle": "ops"}]
    create_room(
        default_db_path(),
        room_id="legacy-room",
        name="Legacy",
        members=members,
        authority_gateway_id="legacy",
        now=1,
    )

    adopted = _result(
        srv._methods["groups.create"](
            2,
            {"room_id": "legacy-room", "name": "Legacy", "members": members},
        )
    )["room"]
    state = _result(
        srv._methods["groups.state"](3, {"room_id": "legacy-room"})
    )["room"]

    assert adopted["adopted"] is True
    assert adopted["authority_gateway_id"] == _server_authority()
    assert adopted["authority_epoch"] == 2
    assert adopted["claim_event"]["payload"] == {
        "previous_gateway_id": "legacy",
        "authority_gateway_id": _server_authority(),
        "authority_epoch": 2,
    }
    assert state["authority_claim"]["event_id"] == "system:authority-adopted"
    assert state["latest_seq"] == 1


@pytest.mark.parametrize(
    ("method_name", "params"),
    [
        (
            "groups.create",
            {
                "room_id": "",
                "name": "x",
                "members": [],
                "authority_gateway_id": "gateway-a",
            },
        ),
        (
            "groups.send",
            {
                "room_id": "missing",
                "event_id": "event-1",
                "actor": {"kind": "user", "id": "desktop-user"},
                "payload": {},
            },
        ),
        ("groups.log", {"room_id": "missing", "since_seq": 0}),
    ],
)
def test_invalid_or_unknown_room_returns_contract_error(home, method_name, params):
    result = srv._methods[method_name](1, params)
    assert result["error"]["code"] in {4110, 4111, 4112}


def test_disband_tombstones_room(home):
    _create_room()
    first = _result(srv._methods["groups.disband"](3, {"room_id": "room-1"}))
    repeated = _result(srv._methods["groups.disband"](4, {"room_id": "room-1"}))
    assert first["tombstone"]["idempotent"] is False
    assert repeated["tombstone"]["idempotent"] is True
    assert _result(srv._methods["groups.list"](5, {}))["rooms"] == []
    deleted = _result(
        srv._methods["groups.list"](6, {"include_disbanded": True})
    )["rooms"]
    assert deleted[0]["disbanded_at"] == first["tombstone"]["disbanded_at"]
    replay = _result(
        srv._methods["groups.log"](
            7,
            {"room_id": "room-1", "include_disbanded": True},
        )
    )
    assert [event["kind"] for event in replay["events"]] == ["room.disbanded"]
