"""Tests for the gateway-hosted ``groups.*`` JSON-RPC contract."""

from __future__ import annotations

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
    assert "monotonic_log" in result["features"]
    assert "groups.state" in result["methods"]
    assert "groups.send" in result["methods"]
    assert result["room_link"]["enabled"] is False


def test_capabilities_and_invitation_advertise_scoped_roomlink(home, monkeypatch):
    monkeypatch.setenv("API_SERVER_KEY", "gateway-api-key-1234567890")
    monkeypatch.setenv("HERMES_PROFILE", "reviewer")
    result = _result(srv._methods["groups.capabilities"](1, {}))
    assert result["room_link"]["enabled"] is True
    assert result["room_link"]["profile"] == "reviewer"
    assert result["room_link"]["catalog"]["text"] is True
    assert "groups.peer.invite" in result["methods"]
    assert "groups.peer.register" in result["methods"]

    invitation = _result(
        srv._methods["groups.peer.invite"](
            2,
            {
                "room_id": "room-1",
                "home_install_id": "install-home",
                "grant_id": "grant-room-1",
            },
        )
    )
    assert invitation["target_profile"] == "reviewer"
    assert invitation["catalog"] == result["room_link"]["catalog"]
    assert "." in invitation["grant"]


def test_app_managed_catalog_and_self_advertised_endpoint_are_consistent(
    home, monkeypatch
):
    monkeypatch.setenv("API_SERVER_KEY", "gateway-api-key-1234567890")
    monkeypatch.setenv("HERMES_DESKTOP", "1")
    monkeypatch.setenv("HERMES_ROOM_LINK_URL", "https://peer.example.test/hermes")
    capability = _result(srv._methods["groups.capabilities"](1, {}))
    invitation = _result(
        srv._methods["groups.peer.invite"](
            2,
            {
                "room_id": "room-1",
                "home_install_id": "install-home",
            },
        )
    )
    assert capability["persistent_process"] is False
    assert capability["room_link"]["catalog"] == invitation["catalog"]
    assert capability["room_link"]["endpoint"] == {
        "available": True,
        "url": "https://peer.example.test/hermes",
        "transport_security": "tls",
    }
    assert invitation["endpoint"] == capability["room_link"]["endpoint"]


def test_roomlink_endpoint_absence_has_machine_reason(home, monkeypatch):
    monkeypatch.setenv("API_SERVER_KEY", "gateway-api-key-1234567890")
    monkeypatch.delenv("HERMES_ROOM_LINK_URL", raising=False)
    result = _result(srv._methods["groups.capabilities"](1, {}))
    assert result["room_link"]["endpoint"] == {
        "available": False,
        "reason": "not_configured",
    }


def test_register_peer_route_probes_scope_and_persists_via_service(home, monkeypatch):
    from gateway.hosted_room_peer import catalog_mapping
    from gateway.hosted_rooms import local_authority_gateway_id

    catalog = catalog_mapping(
        installation_id="install-peer",
        persistent_process=True,
    )
    captured = {}

    class FakeClient:
        def __init__(self, *, base_url, api_key, **kwargs):
            captured["base_url"] = base_url
            captured["api_key"] = api_key

        def probe(self, *, grant):
            captured["grant"] = grant
            return {
                "room_id": "room-1",
                "home_install_id": local_authority_gateway_id(),
                "target_profile": "reviewer",
                "catalog": catalog,
            }

    class FakeService:
        db_path = home / "state.db"

        def register_peer_route(self, **kwargs):
            captured["registered"] = kwargs

    monkeypatch.setattr(srv, "get_hosted_room_service", lambda: FakeService())
    monkeypatch.setattr(
        "tui_gateway.hosted_room_peer_http.PeerRunsHTTPClient",
        FakeClient,
    )
    result = _result(
        srv._methods["groups.peer.register"](
            3,
            {
                "room_id": "room-1",
                "member_id": "member-peer",
                "target_url": "https://peer.example.test",
                "target_profile": "reviewer",
                "grant": "signed.room.grant",
                "catalog": catalog,
            },
        )
    )
    assert result["registered"] is True
    assert captured["api_key"] == ""
    assert captured["registered"]["target_url"] == ("https://peer.example.test")


def test_register_rejects_plaintext_non_loopback(home, monkeypatch):
    class FakeService:
        db_path = home / "state.db"

    monkeypatch.setattr(srv, "get_hosted_room_service", lambda: FakeService())
    response = srv._methods["groups.peer.register"](
        4,
        {
            "room_id": "room-1",
            "member_id": "member-peer",
            "target_url": "http://peer.example.test:8377",
            "target_profile": "reviewer",
            "grant": "signed.room.grant",
            "catalog": {},
        },
    )
    assert response["error"]["code"] == 5120
    assert "https outside" in response["error"]["message"]


def test_register_requires_roomlink_protocol_v1(home, monkeypatch):
    from gateway.hosted_room_peer import catalog_mapping

    class FakeService:
        db_path = home / "state.db"

    monkeypatch.setattr(srv, "get_hosted_room_service", lambda: FakeService())
    response = srv._methods["groups.peer.register"](
        5,
        {
            "room_id": "room-1",
            "member_id": "member-peer",
            "target_url": "https://peer.example.test",
            "target_profile": "reviewer",
            "grant": "signed.room.grant",
            "catalog": catalog_mapping(
                installation_id="install-peer",
                protocol_versions=(2,),
                persistent_process=True,
            ),
        },
    )
    assert response["error"]["code"] == 5120
    assert "protocol v1" in response["error"]["message"]


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
    assert replay["events"][0]["payload"] == {"text": "hello"}


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
    state = _result(srv._methods["groups.state"](3, {"room_id": "legacy-room"}))["room"]

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
    deleted = _result(srv._methods["groups.list"](6, {"include_disbanded": True}))[
        "rooms"
    ]
    assert deleted[0]["disbanded_at"] == first["tombstone"]["disbanded_at"]
    replay = _result(
        srv._methods["groups.log"](
            7,
            {"room_id": "room-1", "include_disbanded": True},
        )
    )
    assert [event["kind"] for event in replay["events"]] == ["room.disbanded"]
