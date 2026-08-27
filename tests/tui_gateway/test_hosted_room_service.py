"""Integration tests for the hosted Discussion coordinator."""

from __future__ import annotations

import sqlite3
import threading
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from gateway import hosted_room_driver as driver
from gateway import hosted_rooms
from gateway.hosted_room_peer import (
    GatewayRoomCatalog,
    catalog_mapping,
    issue_room_grant,
)
from tui_gateway.hosted_room_service import HostedRoomService
from tui_gateway.hosted_room_peer_transport import PeerMemberRoute
from tui_gateway.hosted_room_peer_http import PeerRunsHTTPError


class _FakeRPC:
    def __init__(self) -> None:
        self.sessions = {}
        self.approvals = []

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

    def approve(self, **kwargs):
        self.approvals.append(dict(kwargs))
        return {"resolved": 1}


class _FakePeerClient:
    def __init__(self) -> None:
        self.dispatches = []
        self.revoked = []
        self.session = {"session_id": "peer-group-session"}

    def prepare(self, **kwargs):
        return (
            self.session
            if kwargs["create"] or kwargs.get("expected_session_id")
            else None
        )

    def dispatch(self, **kwargs):
        self.dispatches.append(kwargs["dispatch"])
        return {"status": "accepted", "task_id": kwargs["dispatch"]["task_id"]}

    def history(self, **kwargs):
        if not self.dispatches:
            return []
        dispatch = self.dispatches[-1]
        return [
            {
                "role": "assistant",
                "task_id": dispatch["task_id"],
                "execution_generation": dispatch["execution_generation"],
                "status": "settled",
                "message_id": f"peer:{dispatch['task_id']}",
                "content": "Remote review complete.",
            }
        ]

    def status(self, **kwargs):
        task_id = self.dispatches[-1]["task_id"] if self.dispatches else None
        return {"active": False, "task_id": task_id}

    def stop(self, **kwargs):
        return {"status": "cancelled"}

    def revoke_grant(self, **kwargs):
        self.revoked.append(kwargs["grant"])
        return {"revoked": True}


class _UnavailablePeerClient(_FakePeerClient):
    def prepare(self, **kwargs):
        raise RuntimeError("peer is offline before admission")


class _NotAdmittedPeerClient(_FakePeerClient):
    def __init__(self) -> None:
        super().__init__()
        self.offline = True

    def dispatch(self, **kwargs):
        if self.offline:
            raise PeerRunsHTTPError(
                "peer refused the connection",
                retryable=True,
                not_admitted=True,
            )
        return super().dispatch(**kwargs)


class _ExpiredGrantPeerClient(_FakePeerClient):
    def prepare(self, **kwargs):
        raise PeerRunsHTTPError(
            "peer room authorization needs renewal",
            status_code=401,
            error_code="invalid_room_grant",
        )


class _UnavailableRevokePeerClient(_FakePeerClient):
    def revoke_grant(self, **kwargs):
        raise RuntimeError("peer is offline during revocation")


class _RefreshingPeerClient(_FakePeerClient):
    def __init__(self, replacement: str) -> None:
        super().__init__()
        self.replacement = replacement
        self.refreshed = []
        self.dispatched_grants = []

    def refresh_grant(self, **kwargs):
        self.refreshed.append(kwargs["grant"])
        return {"grant": self.replacement}

    def dispatch(self, **kwargs):
        self.dispatched_grants.append(kwargs["grant"])
        return super().dispatch(**kwargs)


class _ApprovalPeerClient(_FakePeerClient):
    def __init__(self) -> None:
        super().__init__()
        self.approvals = []

    def status(self, **kwargs):
        task_id = self.dispatches[-1]["task_id"] if self.dispatches else "task-1"
        return {
            "status": "waiting_for_approval",
            "active": True,
            "task_id": task_id,
                "execution_generation": 2,
                "run_id": "run-peer-1",
                "session_id": "peer-group-session",
                "request_id": "req-peer-1",
                "approval": {
                "description": "Run the focused tests",
                "command": "pytest -q tests/focused",
                "choices": ["once", "deny"],
            },
        }

    def approve_receipt(self, **kwargs):
        self.approvals.append(dict(kwargs))
        return {"resolved": 1}


class _RecoveringPeerClient(_FakePeerClient):
    def __init__(self) -> None:
        super().__init__()
        self.recoveries = []

    def recover_dispatch(self, **kwargs):
        dispatch = dict(kwargs["dispatch"])
        self.recoveries.append({**kwargs, "dispatch": dispatch})
        self.dispatches.append(dispatch)
        return {
            "status": "accepted",
            "task_id": dispatch["task_id"],
            "execution_generation": dispatch["execution_generation"],
            "run_id": "run-recovered",
        }


def _server():
    return SimpleNamespace(_methods={}, _sessions={}, _sessions_lock=threading.Lock())


def _wait_for(predicate, timeout=2.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.01)
    raise AssertionError("condition was not reached")


def test_stop_room_snapshots_tasks_before_status_transitions(monkeypatch, tmp_path):
    """One running task must not be counted again after it becomes stopping."""

    identity = driver.TaskIdentity("room-1", "task-1", "thread-1", "turn-1")
    task = {"identity": identity, "status": "running", "cancel_id": None}
    calls = []

    def listed(_db, *, room_id, status):
        assert room_id == "room-1"
        return [dict(task)] if task["status"] == status else []

    def cancel(_identity, *, cancel_id):
        calls.append(cancel_id)
        task["status"] = "stopping"
        task["cancel_id"] = cancel_id
        return dict(task)

    monkeypatch.setattr(driver, "list_tasks", listed)
    monkeypatch.setattr(
        hosted_rooms,
        "request_room_stop",
        lambda _db, *, room_id, cancel_id: {
            "room_id": room_id,
            "cancel_id": cancel_id,
        },
    )
    service = HostedRoomService(_server(), db_path=tmp_path / "state.db")
    service.runtime = SimpleNamespace(cancel=cancel, wakeup=lambda: None)

    assert service.stop_room("room-1", cancel_id="stop-1") == 1
    assert calls == ["stop-1"]


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
            event["kind"] == "message.member" for event in service._events("room-1")
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


def test_headless_room_publishes_peer_member_reply_without_desktop_transport(
    tmp_path: Path,
):
    db = tmp_path / "state.db"
    peer = _FakePeerClient()
    route = PeerMemberRoute(
        home_install_id="install-home",
        member_id="member-reviewer",
        target_install_id="install-peer",
        target_profile="reviewer",
        capability_digest="a" * 64,
        cancellation_scope_id="cancel-room-1",
        trace_id="trace-room-1",
        grant="signed-room-grant",
    )
    service = HostedRoomService(
        _server(),
        db_path=db,
        peer_routes={("room-1", "member-reviewer"): route},
        peer_clients={"install-peer": peer},
    )
    service.rpc = _FakeRPC()
    service.runtime.rpc = service.rpc
    service.local_profiles = lambda: ("default",)
    room = service.create_room(
        room_id="room-1",
        name="Review room",
        members=[
            {
                "member_id": "default",
                "profile": "default",
                "handle": "local",
            },
            {
                "member_id": "member-reviewer",
                "profile": "reviewer",
                "handle": "reviewer",
                "target": {
                    "kind": "peer",
                    "peer_id": "peer-review",
                    "installation_id": "install-peer",
                    "profile": "reviewer",
                    "capability_digest": "a" * 64,
                },
            },
        ],
    )
    assert room["members"][1]["target"]["kind"] == "peer"

    service.start()
    service.send(
        room_id="room-1",
        event_id="user-peer-1",
        payload={"text": "@reviewer inspect this", "thread_id": "thread-1"},
    )
    _wait_for(
        lambda: any(
            event["kind"] == "message.member" for event in service._events("room-1")
        )
    )
    assert service.stop(timeout=1.0)

    events = service._events("room-1")
    reply = next(event for event in events if event["kind"] == "message.member")
    assert reply["payload"]["member_id"] == "member-reviewer"
    assert reply["payload"]["text"] == "Remote review complete."
    assert reply["actor"]["connection_id"] == "peer-review"
    assert peer.dispatches[0]["target_profile"] == "reviewer"


def test_unadmitted_peer_failure_does_not_block_next_healthy_member(
    tmp_path: Path,
):
    db = tmp_path / "state.db"
    route = PeerMemberRoute(
        home_install_id="install-home",
        member_id="member-peer",
        target_install_id="install-peer",
        target_profile="reviewer",
        capability_digest="a" * 64,
        cancellation_scope_id="cancel-room-1",
        trace_id="trace-room-1",
        grant="signed-room-grant",
    )
    service = HostedRoomService(
        _server(),
        db_path=db,
        peer_routes={("room-1", "member-peer"): route},
        peer_clients={"install-peer": _UnavailablePeerClient()},
    )
    service.rpc = _FakeRPC()
    service.runtime.rpc = service.rpc
    service.local_profiles = lambda: ("local",)
    service.create_room(
        room_id="room-1",
        name="Fallback room",
        members=[
            {
                "member_id": "member-peer",
                "profile": "reviewer",
                "handle": "reviewer",
                "target": {
                    "kind": "peer",
                    "peer_id": "peer-review",
                    "installation_id": "install-peer",
                    "profile": "reviewer",
                    "capability_digest": "a" * 64,
                },
            },
            {"member_id": "local", "profile": "local", "handle": "local"},
        ],
    )

    service.start()
    service.send(
        room_id="room-1",
        event_id="user-fallback-1",
        payload={"text": "Review this together", "thread_id": "thread-1"},
    )
    _wait_for(
        lambda: any(
            event["kind"] == "message.member"
            and event["payload"]["member_id"] == "local"
            for event in service._events("room-1")
        )
    )
    assert service.stop(timeout=1.0)

    events = service._events("room-1")
    assert any(
        event["kind"] == "turn.failed"
        and event["payload"]["member_id"] == "member-peer"
        for event in events
    )
    assert any(
        event["kind"] == "message.member" and event["payload"]["member_id"] == "local"
        for event in events
    )


def test_registered_peer_route_rehydrates_after_service_restart(tmp_path: Path):
    db = tmp_path / "state.db"
    catalog = GatewayRoomCatalog.from_mapping(
        catalog_mapping(
            installation_id="install-peer",
            persistent_process=True,
        )
    )
    route = PeerMemberRoute(
        home_install_id=hosted_rooms.local_authority_gateway_id(),
        member_id="member-peer",
        target_install_id="install-peer",
        target_profile="reviewer",
        capability_digest=catalog.catalog_digest,
        cancellation_scope_id="cancel-room-1",
        trace_id="trace-room-1",
        grant="signed.room.grant",
    )
    first = HostedRoomService(_server(), db_path=db)
    first.register_peer_route(
        room_id="room-1",
        member_id="member-peer",
        route=route,
        client=_FakePeerClient(),
        target_url="https://peer.example.test",
        catalog=catalog,
    )

    restarted = HostedRoomService(_server(), db_path=db)
    restored = restarted.peer_routes[("room-1", "member-peer")]
    assert restored.target_install_id == "install-peer"
    assert restored.target_profile == "reviewer"
    assert restored.grant == "signed.room.grant"
    assert ("room-1", "member-peer") in restarted.peer_clients


def test_one_corrupt_stored_route_does_not_hide_healthy_peers(tmp_path: Path):
    db = tmp_path / "state.db"
    catalog = GatewayRoomCatalog.from_mapping(
        catalog_mapping(installation_id="install-peer", persistent_process=True)
    )
    service = HostedRoomService(_server(), db_path=db)
    for room_id, member_id in (("room-good", "member-good"), ("room-bad", "member-bad")):
        route = PeerMemberRoute(
            home_install_id=hosted_rooms.local_authority_gateway_id(),
            member_id=member_id,
            target_install_id="install-peer",
            target_profile="reviewer",
            capability_digest=catalog.catalog_digest,
            cancellation_scope_id=f"cancel-{room_id}",
            trace_id=f"trace-{room_id}",
            grant=f"grant-{room_id}",
        )
        service.register_peer_route(
            room_id=room_id,
            member_id=member_id,
            route=route,
            client=_FakePeerClient(),
            target_url="https://peer.example.test",
            catalog=catalog,
        )

    with sqlite3.connect(db) as conn:
        conn.execute(
            "UPDATE hosted_room_links SET target_url=? WHERE room_id=?",
            ("http://public-plaintext.example.test", "room-bad"),
        )

    restarted = HostedRoomService(_server(), db_path=db)

    assert ("room-good", "member-good") in restarted.peer_routes
    assert ("room-bad", "member-bad") not in restarted.peer_routes
    assert restarted.status()["link_load_error"] == "room-bad:member-bad:invalid"


def test_unpublished_roomlink_v1_route_is_quarantined_for_reinvitation(
    tmp_path: Path,
):
    db = tmp_path / "state.db"
    legacy_catalog = GatewayRoomCatalog.from_mapping(
        catalog_mapping(
            installation_id="install-peer",
            protocol_versions=(1,),
            persistent_process=True,
        )
    )
    route = PeerMemberRoute(
        home_install_id=hosted_rooms.local_authority_gateway_id(),
        member_id="member-peer",
        target_install_id="install-peer",
        target_profile="reviewer",
        capability_digest=legacy_catalog.catalog_digest,
        cancellation_scope_id="cancel-room-1",
        trace_id="trace-room-1",
        grant="legacy-v1-grant",
    )
    service = HostedRoomService(_server(), db_path=db)
    service.register_peer_route(
        room_id="room-1",
        member_id="member-peer",
        route=route,
        client=_FakePeerClient(),
        target_url="https://peer.example.test",
        catalog=legacy_catalog,
    )

    restarted = HostedRoomService(_server(), db_path=db)

    assert restarted.peer_routes == {}
    assert restarted.status()["link_load_error"] == (
        "room-1:member-peer:protocol-upgrade-required"
    )


def test_peer_member_without_route_fails_closed_instead_of_running_locally(
    tmp_path: Path,
):
    service = HostedRoomService(_server(), db_path=tmp_path / "state.db")
    service.create_room(
        room_id="room-1",
        name="Peer room",
        members=[
            {"member_id": "local", "profile": "default", "handle": "local"},
            {
                "member_id": "member-peer",
                "profile": "reviewer",
                "handle": "reviewer",
                "target": {
                    "kind": "peer",
                    "peer_id": "peer-review",
                    "installation_id": "install-peer",
                    "profile": "reviewer",
                    "capability_digest": "a" * 64,
                },
            },
        ],
    )
    with pytest.raises(RuntimeError, match="route is unavailable"):
        service._resolve_member_transport(
            service.bindings()[0],
            {
                "payload": {
                    "target_member_id": "member-peer",
                    "target_profile": "reviewer",
                    "source_event_seq": 9,
                }
            },
        )


def test_registration_disk_failure_does_not_publish_live_route(
    tmp_path: Path, monkeypatch
):
    db = tmp_path / "state.db"
    service = HostedRoomService(_server(), db_path=db)
    catalog = GatewayRoomCatalog.from_mapping(
        catalog_mapping(installation_id="install-peer", persistent_process=True)
    )
    route = PeerMemberRoute(
        home_install_id=hosted_rooms.local_authority_gateway_id(),
        member_id="member-peer",
        target_install_id="install-peer",
        target_profile="reviewer",
        capability_digest=catalog.catalog_digest,
        cancellation_scope_id="cancel-room-1",
        trace_id="trace-room-1",
        grant="signed.room.grant",
    )
    monkeypatch.setattr(
        "gateway.hosted_room_links.save_room_link",
        lambda *args, **kwargs: (_ for _ in ()).throw(OSError("disk full")),
    )
    with pytest.raises(OSError, match="disk full"):
        service.register_peer_route(
            room_id="room-1",
            member_id="member-peer",
            route=route,
            client=_FakePeerClient(),
            target_url="https://peer.example.test",
            catalog=catalog,
        )
    assert ("room-1", "member-peer") not in service.peer_routes
    assert "install-peer" not in service.peer_clients


def test_room_route_revocation_is_remote_first_and_removes_local_state(
    tmp_path: Path,
):
    from gateway import hosted_room_links

    db = tmp_path / "state.db"
    catalog = GatewayRoomCatalog.from_mapping(
        catalog_mapping(installation_id="install-peer", persistent_process=True)
    )
    route = PeerMemberRoute(
        home_install_id=hosted_rooms.local_authority_gateway_id(),
        member_id="member-peer",
        target_install_id="install-peer",
        target_profile="reviewer",
        capability_digest=catalog.catalog_digest,
        cancellation_scope_id="cancel-room-1",
        trace_id="trace-room-1",
        grant="signed.room.grant",
    )
    peer = _FakePeerClient()
    service = HostedRoomService(_server(), db_path=db)
    service.register_peer_route(
        room_id="room-1",
        member_id="member-peer",
        route=route,
        client=peer,
        target_url="https://peer.example.test",
        catalog=catalog,
    )

    assert service.revoke_room_routes("room-1") == 1
    assert peer.revoked == ["signed.room.grant"]
    assert ("room-1", "member-peer") not in service.peer_routes
    assert hosted_room_links.load_room_links(db) == ()


def test_failed_remote_revocation_preserves_route_for_retry(tmp_path: Path):
    from gateway import hosted_room_links

    db = tmp_path / "state.db"
    catalog = GatewayRoomCatalog.from_mapping(
        catalog_mapping(installation_id="install-peer", persistent_process=True)
    )
    route = PeerMemberRoute(
        home_install_id=hosted_rooms.local_authority_gateway_id(),
        member_id="member-peer",
        target_install_id="install-peer",
        target_profile="reviewer",
        capability_digest=catalog.catalog_digest,
        cancellation_scope_id="cancel-room-1",
        trace_id="trace-room-1",
        grant="signed.room.grant",
    )
    service = HostedRoomService(_server(), db_path=db)
    service.register_peer_route(
        room_id="room-1",
        member_id="member-peer",
        route=route,
        client=_UnavailableRevokePeerClient(),
        target_url="https://peer.example.test",
        catalog=catalog,
    )

    with pytest.raises(RuntimeError, match="offline during revocation"):
        service.revoke_room_routes("room-1")
    assert ("room-1", "member-peer") in service.peer_routes
    assert len(hosted_room_links.load_room_links(db)) == 1


def test_expired_grant_surfaces_needs_reauthorization_without_secret(
    tmp_path: Path,
):
    db = tmp_path / "state.db"
    catalog = GatewayRoomCatalog.from_mapping(
        catalog_mapping(installation_id="install-peer", persistent_process=True)
    )
    route = PeerMemberRoute(
        home_install_id=hosted_rooms.local_authority_gateway_id(),
        member_id="member-peer",
        target_install_id="install-peer",
        target_profile="reviewer",
        capability_digest=catalog.catalog_digest,
        cancellation_scope_id="cancel-room-1",
        trace_id="trace-room-1",
        grant="signed.room.grant",
    )
    service = HostedRoomService(_server(), db_path=db)
    service.register_peer_route(
        room_id="room-1",
        member_id="member-peer",
        route=route,
        client=_ExpiredGrantPeerClient(),
        target_url="https://peer.example.test",
        catalog=catalog,
    )
    service.create_room(
        room_id="room-1",
        name="Peer room",
        members=[
            {"member_id": "local", "profile": "default", "handle": "local"},
            {
                "member_id": "member-peer",
                "profile": "reviewer",
                "handle": "reviewer",
                "target": {
                    "kind": "peer",
                    "peer_id": "peer-review",
                    "installation_id": "install-peer",
                    "profile": "reviewer",
                    "capability_digest": catalog.catalog_digest,
                },
            },
        ],
    )
    transport = service._resolve_member_transport(
        service.bindings()[0],
        {
            "payload": {
                "target_member_id": "member-peer",
                "target_profile": "reviewer",
                "source_event_seq": 3,
            }
        },
    )
    with pytest.raises(PeerRunsHTTPError):
        transport.resolve_exact(
            profile="reviewer",
            title="Group: room-1",
            source="bot_room",
        )
    status = service.status("room-1")
    assert status["peer_routes"] == [
        {
            "room_id": "room-1",
            "member_id": "member-peer",
            "status": "needs_reauthorization",
        }
    ]
    assert "signed.room.grant" not in repr(status)
    restarted = HostedRoomService(_server(), db_path=db)
    assert restarted.status("room-1")["peer_routes"] == status["peer_routes"]

    rotated = PeerMemberRoute(
        home_install_id=route.home_install_id,
        member_id=route.member_id,
        target_install_id=route.target_install_id,
        target_profile=route.target_profile,
        capability_digest=route.capability_digest,
        cancellation_scope_id=route.cancellation_scope_id,
        trace_id="trace-room-rotated",
        grant="rotated.room.grant",
    )
    restarted.register_peer_route(
        room_id="room-1",
        member_id="member-peer",
        route=rotated,
        client=_FakePeerClient(),
        target_url="https://peer.example.test",
        catalog=catalog,
    )
    assert restarted.status("room-1")["peer_routes"][0]["status"] == "ready"
    after_rotation = HostedRoomService(_server(), db_path=db)
    assert after_rotation.peer_routes[("room-1", "member-peer")].grant == (
        "rotated.room.grant"
    )


def test_not_admitted_dispatch_persists_unavailable_route_until_success(
    tmp_path: Path,
):
    db = tmp_path / "state.db"
    catalog = GatewayRoomCatalog.from_mapping(
        catalog_mapping(installation_id="install-peer", persistent_process=True)
    )
    route = PeerMemberRoute(
        home_install_id=hosted_rooms.local_authority_gateway_id(),
        member_id="member-peer",
        target_install_id="install-peer",
        target_profile="reviewer",
        capability_digest=catalog.catalog_digest,
        cancellation_scope_id="cancel-room-1",
        trace_id="trace-room-1",
        grant="signed.room.grant",
    )
    peer = _NotAdmittedPeerClient()
    service = HostedRoomService(_server(), db_path=db)
    service.register_peer_route(
        room_id="room-1",
        member_id="member-peer",
        route=route,
        client=peer,
        target_url="https://peer.example.test",
        catalog=catalog,
    )
    service.create_room(
        room_id="room-1",
        name="Peer room",
        members=[
            {"member_id": "local", "profile": "default", "handle": "local"},
            {
                "member_id": "member-peer",
                "profile": "reviewer",
                "handle": "reviewer",
                "target": {
                    "kind": "peer",
                    "peer_id": "peer-review",
                    "installation_id": "install-peer",
                    "profile": "reviewer",
                    "capability_digest": catalog.catalog_digest,
                },
            },
        ],
    )
    binding = service.bindings()[0]
    task = {
        "identity": driver.TaskIdentity(
            "room-1", "task-peer", "thread-1", "turn-1"
        ),
        "execution_generation": 1,
        "payload": {
            "target_member_id": "member-peer",
            "target_profile": "reviewer",
            "source_event_seq": 3,
        },
    }
    transport = service._resolve_member_transport(binding, task)
    session = transport.create(
        profile="reviewer",
        title="Group: room-1",
        source="bot_room",
    )

    with pytest.raises(PeerRunsHTTPError) as caught:
        transport.submit(
            profile="reviewer",
            session_id=session["session_id"],
            prompt="Review the queued task.",
            source="bot_room",
            task=task["identity"],
            execution_generation=1,
            on_terminal=lambda _receipt: None,
        )

    assert caught.value.not_admitted is True
    assert service.status("room-1")["peer_routes"][0]["status"] == "unavailable"
    restarted = HostedRoomService(_server(), db_path=db)
    assert restarted.status("room-1")["peer_routes"][0]["status"] == "unavailable"

    peer.offline = False
    transport.submit(
        profile="reviewer",
        session_id=session["session_id"],
        prompt="Review the queued task.",
        source="bot_room",
        task=task["identity"],
        execution_generation=2,
        on_terminal=lambda _receipt: None,
    )
    assert service.status("room-1")["peer_routes"][0]["status"] == "ready"


def test_dispatch_refresh_persists_before_remote_admission(tmp_path: Path):
    now = time.time()
    secret = b"s" * 32
    old_grant = issue_room_grant(
        secret,
        grant_id="grant-old",
        room_id="room-1",
        home_install_id=hosted_rooms.local_authority_gateway_id(),
        authority_gateway_id=hosted_rooms.local_authority_gateway_id(),
        authority_epoch=1,
        member_id="member-peer",
        target_install_id="install-peer",
        target_profile="reviewer",
        issued_at=now - 3700,
        ttl_seconds=3600,
        status_expires_at=now + 10_000,
    )
    new_grant = issue_room_grant(
        secret,
        grant_id="grant-new",
        room_id="room-1",
        home_install_id=hosted_rooms.local_authority_gateway_id(),
        authority_gateway_id=hosted_rooms.local_authority_gateway_id(),
        authority_epoch=1,
        member_id="member-peer",
        target_install_id="install-peer",
        target_profile="reviewer",
        issued_at=now,
        ttl_seconds=3600,
        status_expires_at=now + 10_000,
    )
    catalog = GatewayRoomCatalog.from_mapping(
        catalog_mapping(installation_id="install-peer", persistent_process=True)
    )
    route = PeerMemberRoute(
        home_install_id=hosted_rooms.local_authority_gateway_id(),
        member_id="member-peer",
        target_install_id="install-peer",
        target_profile="reviewer",
        capability_digest=catalog.catalog_digest,
        cancellation_scope_id="cancel-room-1",
        trace_id="trace-room-1",
        grant=old_grant,
    )
    peer = _RefreshingPeerClient(new_grant)
    db = tmp_path / "state.db"
    service = HostedRoomService(_server(), db_path=db)
    service.register_peer_route(
        room_id="room-1",
        member_id="member-peer",
        route=route,
        client=peer,
        target_url="https://peer.example.test",
        catalog=catalog,
    )
    service.create_room(
        room_id="room-1",
        name="Peer room",
        members=[
            {"member_id": "local", "profile": "default", "handle": "local"},
            {
                "member_id": "member-peer",
                "profile": "reviewer",
                "handle": "reviewer",
                "target": {
                    "kind": "peer",
                    "peer_id": "peer-review",
                    "installation_id": "install-peer",
                    "profile": "reviewer",
                    "capability_digest": catalog.catalog_digest,
                },
            },
        ],
    )
    identity = driver.TaskIdentity("room-1", "task-1", "thread-1", "turn-1")
    transport = service._resolve_member_transport(
        service.bindings()[0],
        {
            "identity": identity,
            "execution_generation": 1,
            "payload": {
                "target_member_id": "member-peer",
                "target_profile": "reviewer",
                "source_event_seq": 11,
            },
        },
    )
    session = transport.create(
        profile="reviewer", title="Group: room-1", source="bot_room"
    )
    transport.submit(
        profile="reviewer",
        session_id=session["session_id"],
        prompt="Review this",
        source="bot_room",
        task=identity,
        execution_generation=1,
        on_terminal=lambda _receipt: None,
    )
    assert peer.refreshed == [old_grant]
    assert peer.dispatched_grants == [new_grant]
    assert HostedRoomService(_server(), db_path=db).peer_routes[
        ("room-1", "member-peer")
    ].grant == new_grant


def test_peer_approval_is_scoped_visible_and_resolvable(tmp_path: Path):
    db = tmp_path / "state.db"
    catalog = GatewayRoomCatalog.from_mapping(
        catalog_mapping(installation_id="install-peer", persistent_process=True)
    )
    route = PeerMemberRoute(
        home_install_id=hosted_rooms.local_authority_gateway_id(),
        member_id="member-peer",
        target_install_id="install-peer",
        target_profile="reviewer",
        capability_digest=catalog.catalog_digest,
        cancellation_scope_id="cancel-room-1",
        trace_id="trace-room-1",
        grant="signed.room.grant",
    )
    peer = _ApprovalPeerClient()
    service = HostedRoomService(_server(), db_path=db)
    service.register_peer_route(
        room_id="room-1",
        member_id="member-peer",
        route=route,
        client=peer,
        target_url="https://peer.example.test",
        catalog=catalog,
    )
    service.create_room(
        room_id="room-1",
        name="Peer room",
        members=[
            {
                "member_id": "default",
                "profile": "default",
                "handle": "hermes",
            },
            {
                "member_id": "member-peer",
                "profile": "reviewer",
                "handle": "reviewer",
                "target": {
                    "kind": "peer",
                    "peer_id": "peer-review",
                    "installation_id": "install-peer",
                    "profile": "reviewer",
                    "capability_digest": catalog.catalog_digest,
                },
            }
        ],
    )
    identity = driver.TaskIdentity("room-1", "task-1", "thread-1", "turn-1")
    transport = service._resolve_member_transport(
        service.bindings()[0],
        {
            "identity": identity,
            "execution_generation": 2,
            "payload": {
                "target_member_id": "member-peer",
                "target_profile": "reviewer",
                "source_event_seq": 1,
            },
        },
    )

    status = transport.info(
        profile="reviewer",
        session_id="peer-group-session",
        source="bot_room",
    )
    assert status["status"] == "waiting_for_approval"
    service._set_pending_action(
        "room-1",
        "member-peer",
        {
            "kind": "approval",
            "task_id": status["task_id"],
            "execution_generation": status["execution_generation"],
            "run_id": status["run_id"],
            "session_id": "peer-group-session",
            "request_id": "req-peer-1",
            "approval": status["approval"],
        },
    )
    pending = service.status("room-1")["pending_actions"]
    assert pending == [
        {
            "kind": "approval",
            "task_id": "task-1",
            "execution_generation": 2,
            "run_id": "run-peer-1",
            "session_id": "peer-group-session",
            "request_id": "req-peer-1",
            "approval": {
                "description": "Run the focused tests",
                "command": "pytest -q tests/focused",
                "choices": ["once", "deny"],
            },
            "member_id": "member-peer",
        }
    ]

    assert service.approve_room_task(
        "room-1",
        member_id="member-peer",
        task_id="task-1",
        execution_generation=2,
        choice="once",
        request_id="req-peer-1",
    ) == {"resolved": 1}
    assert peer.approvals == [
        {
            "task_id": "task-1",
            "execution_generation": 2,
            "request_id": "req-peer-1",
            "choice": "once",
            "grant": "signed.room.grant",
        }
    ]
    assert service.status("room-1")["pending_actions"] == []


def test_local_room_approval_uses_the_exact_hidden_session(tmp_path: Path):
    service = HostedRoomService(_server(), db_path=tmp_path / "state.db")
    rpc = _FakeRPC()
    service.rpc = rpc
    service.runtime.rpc = rpc
    service._set_pending_action(
        "room-1",
        "local",
        {
            "kind": "approval",
            "task_id": "task-local-1",
            "execution_generation": 1,
            "session_id": "local-session",
            "request_id": "approval-local-1",
            "approval": {
                "description": "Run focused tests",
                "command": "pytest -q tests/focused",
                "choices": ["once", "deny"],
            },
        },
    )

    assert service.approve_room_task(
        "room-1",
        member_id="local",
        task_id="task-local-1",
        execution_generation=1,
        choice="once",
        request_id="approval-local-1",
    ) == {"resolved": 1}
    assert rpc.approvals == [
        {
            "session_id": "local-session",
            "request_id": "approval-local-1",
            "choice": "once",
        }
    ]
    assert service.status("room-1")["pending_actions"] == []


def test_stale_local_approval_cannot_resolve_replacement_request(tmp_path: Path):
    service = HostedRoomService(_server(), db_path=tmp_path / "state.db")
    rpc = _FakeRPC()
    service.rpc = rpc
    service.runtime.rpc = rpc
    action = {
        "kind": "approval",
        "task_id": "task-local-1",
        "execution_generation": 1,
        "session_id": "local-session",
        "approval": {"choices": ["once", "deny"]},
    }
    service._set_pending_action(
        "room-1", "local", {**action, "request_id": "approval-A"}
    )
    service._set_pending_action(
        "room-1", "local", {**action, "request_id": "approval-B"}
    )

    with pytest.raises(RuntimeError, match="no longer pending"):
        service.approve_room_task(
            "room-1",
            member_id="local",
            task_id="task-local-1",
            execution_generation=1,
            choice="once",
            request_id="approval-A",
        )

    assert rpc.approvals == []
    assert service.status("room-1")["pending_actions"][0]["request_id"] == (
        "approval-B"
    )


def test_peer_recovery_replays_the_same_execution_generation(tmp_path: Path):
    db = tmp_path / "state.db"
    catalog = GatewayRoomCatalog.from_mapping(
        catalog_mapping(installation_id="install-peer", persistent_process=True)
    )
    route = PeerMemberRoute(
        home_install_id=hosted_rooms.local_authority_gateway_id(),
        member_id="member-peer",
        target_install_id="install-peer",
        target_profile="reviewer",
        capability_digest=catalog.catalog_digest,
        cancellation_scope_id="cancel-room-1",
        trace_id="trace-room-1",
        grant="signed.room.grant",
    )
    peer = _RecoveringPeerClient()
    service = HostedRoomService(_server(), db_path=db)
    service.register_peer_route(
        room_id="room-1",
        member_id="member-peer",
        route=route,
        client=peer,
        target_url="https://peer.example.test",
        catalog=catalog,
    )
    service.create_room(
        room_id="room-1",
        name="Peer room",
        members=[
            {"member_id": "default", "profile": "default", "handle": "hermes"},
            {
                "member_id": "member-peer",
                "profile": "reviewer",
                "handle": "reviewer",
                "target": {
                    "kind": "peer",
                    "peer_id": "peer-review",
                    "installation_id": "install-peer",
                    "profile": "reviewer",
                    "capability_digest": catalog.catalog_digest,
                },
            },
        ],
    )
    identity = driver.TaskIdentity("room-1", "task-1", "thread-1", "turn-1")

    service._resolve_member_transport(
        service.bindings()[0],
        {
            "identity": identity,
            "status": "indeterminate",
            "execution_generation": 1,
            "payload": {
                "target_member_id": "member-peer",
                "target_profile": "reviewer",
                "source_event_seq": 9,
                "prompt": "Recover the accepted review.",
            },
        },
    )

    assert len(peer.recoveries) == 1
    recovered = peer.recoveries[0]["dispatch"]
    assert recovered["task_id"] == "task-1"
    assert recovered["execution_generation"] == 1
    assert recovered["prompt"] == "Recover the accepted review."
