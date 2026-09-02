"""Contracts at the route-sync, file, and lifecycle composition boundaries."""

import threading
from types import SimpleNamespace

import pytest

from gateway import hosted_room_links, hosted_rooms
from gateway.hosted_room_peer import GatewayRoomCatalog, catalog_mapping
from tui_gateway.hosted_room_service import HostedRoomService


def _server():
    return SimpleNamespace(_methods={}, _sessions={}, _sessions_lock=threading.Lock())


def _link(*, attachments=True):
    catalog = GatewayRoomCatalog.from_mapping(
        catalog_mapping(
            installation_id="install-peer",
            persistent_process=True,
            attachments=attachments,
        )
    )
    return hosted_room_links.make_stored_link(
        room_id="room-before-create",
        member_id="member-peer",
        target_url="https://peer.example.test",
        target_profile="reviewer",
        grant="signed.room.grant",
        catalog=catalog,
        cancellation_scope_id="cancel-room",
        trace_id="trace-room",
    )


def test_worker_restart_preserves_precreation_route_retirement(tmp_path):
    db = tmp_path / "state.db"
    hosted_rooms.begin_room_link_retirement(
        db,
        room_id="room-before-create",
        authority_gateway_id=hosted_rooms.local_authority_gateway_id(),
        authority_epoch=1,
    )

    HostedRoomService(_server(), db_path=db)

    assert hosted_rooms.room_link_retirement_started(db, room_id="room-before-create")
    with pytest.raises(hosted_rooms.HostedRoomError, match="fenced"):
        hosted_room_links.save_room_link(db, _link())


@pytest.mark.parametrize("attachments", [False, True])
def test_cross_process_route_hydration_preserves_file_capability(tmp_path, attachments):
    db = tmp_path / "state.db"
    worker = HostedRoomService(_server(), db_path=db)
    link = _link(attachments=attachments)
    hosted_room_links.save_room_link(db, link)

    route, _client = worker._hydrate_persisted_peer_route(link.room_id, link.member_id)

    assert route.attachments is attachments
    assert route.capability_digest == link.catalog.catalog_digest
    assert route.target_profile == link.target_profile


def test_retirement_uses_public_identifier_limits_after_storage_split(
    tmp_path, monkeypatch
):
    monkeypatch.setattr(hosted_rooms, "MAX_ROOM_ID_CHARS", 4)
    with pytest.raises(hosted_rooms.HostedRoomError, match="room_id"):
        hosted_rooms.begin_room_link_retirement(
            tmp_path / "state.db",
            room_id="too-long",
            authority_gateway_id="install-home",
            authority_epoch=1,
        )
