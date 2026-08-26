"""Contracts for autonomous cross-gateway hosted-room members."""

from __future__ import annotations

import hashlib

import pytest

from gateway.hosted_room_peer import (
    GatewayRoomCatalog,
    HostedMemberDispatch,
    HostedRoomGrantError,
    HostedRoomPeerError,
    RoomLinkProbe,
    catalog_mapping,
    issue_room_grant,
    select_room_link,
    verify_room_grant,
)


SECRET = b"s" * 32


def _dispatch(**overrides):
    prompt = overrides.pop("prompt", "Review the current room state.")
    value = {
        "protocol_version": 1,
        "room_id": "room-1",
        "home_install_id": "install-home",
        "authority_gateway_id": "gateway-home",
        "authority_epoch": 2,
        "member_id": "member-reviewer",
        "target_install_id": "install-peer",
        "target_profile": "reviewer",
        "task_id": "task-1",
        "execution_generation": 1,
        "source_event_seq": 9,
        "cancellation_scope_id": "cancel-1",
        "prompt": prompt,
        "prompt_digest": hashlib.sha256(prompt.encode()).hexdigest(),
        "capability_digest": "a" * 64,
        "trace_id": "trace-1",
        **overrides,
    }
    return HostedMemberDispatch.from_mapping(value)


def test_catalog_digest_is_canonical_and_tamper_evident():
    value = catalog_mapping(
        installation_id="install-peer",
        protocol_versions=(1,),
        link_modes=("direct", "pull"),
        persistent_process=True,
    )
    catalog = GatewayRoomCatalog.from_mapping(value)
    assert catalog.text is True
    assert catalog.attachments is False

    value["attachments"] = True
    with pytest.raises(HostedRoomPeerError, match="catalog_digest"):
        GatewayRoomCatalog.from_mapping(value)


def test_dispatch_rejects_unknown_fields_and_prompt_digest_mismatch():
    with pytest.raises(HostedRoomPeerError, match="unknown fields"):
        _dispatch(extra=True)
    with pytest.raises(HostedRoomPeerError, match="prompt_digest"):
        _dispatch(prompt_digest="0" * 64)


def test_room_grant_is_scoped_to_exact_room_home_target_and_profile():
    dispatch = _dispatch()
    token = issue_room_grant(
        SECRET,
        grant_id="grant-1",
        room_id=dispatch.room_id,
        home_install_id=dispatch.home_install_id,
        target_install_id=dispatch.target_install_id,
        target_profile=dispatch.target_profile,
        issued_at=100,
        ttl_seconds=60,
    )
    claims = verify_room_grant(SECRET, token, dispatch, now=120)
    assert claims["grant_id"] == "grant-1"

    wrong_target = _dispatch(target_profile="other")
    with pytest.raises(HostedRoomGrantError, match="scope"):
        verify_room_grant(SECRET, token, wrong_target, now=120)


def test_room_grant_fails_closed_for_tamper_expiry_and_permission():
    dispatch = _dispatch()
    token = issue_room_grant(
        SECRET,
        grant_id="grant-1",
        room_id=dispatch.room_id,
        home_install_id=dispatch.home_install_id,
        target_install_id=dispatch.target_install_id,
        target_profile=dispatch.target_profile,
        permissions=("status",),
        issued_at=100,
        ttl_seconds=10,
    )
    with pytest.raises(HostedRoomGrantError, match="allow"):
        verify_room_grant(SECRET, token, dispatch, now=105)
    # Dispatch expires quickly, while observation/stop remains available for
    # bounded headless recovery after the Desktop has closed.
    assert (
        verify_room_grant(SECRET, token, dispatch, permission="status", now=111)[
            "grant_id"
        ]
        == "grant-1"
    )
    with pytest.raises(HostedRoomGrantError, match="expired"):
        verify_room_grant(
            SECRET,
            token,
            dispatch,
            permission="status",
            now=100 + 30 * 24 * 60 * 60,
        )
    with pytest.raises(HostedRoomGrantError, match="signature"):
        verify_room_grant(SECRET, token[:-1] + "A", dispatch, now=105)


def test_link_selection_prefers_safe_direct_then_overlay_then_relay_then_pull():
    selected = select_room_link(
        [
            RoomLinkProbe("relay", True, True, 10),
            RoomLinkProbe("direct", True, True, 50),
            RoomLinkProbe("overlay", True, True, 5),
            RoomLinkProbe("pull", True, True, 1),
        ],
        desktop_available=False,
    )
    assert selected is not None
    assert selected.mode == "direct"


def test_link_selection_never_falls_back_to_unencrypted_route():
    assert (
        select_room_link(
            [RoomLinkProbe("direct", True, False, 1)],
            desktop_available=False,
        )
        is None
    )
    fallback = select_room_link([], desktop_available=True)
    assert fallback is not None
    assert fallback.mode == "desktop"


def test_local_catalog_is_honest_for_app_managed_process(monkeypatch):
    from gateway.hosted_room_peer import local_catalog_mapping

    monkeypatch.setenv("HERMES_DESKTOP", "1")
    catalog = local_catalog_mapping(installation_id="install-desktop")
    assert catalog["persistent_process"] is False
    assert catalog["link_modes"] == ["direct"]


@pytest.mark.parametrize(
    ("configured", "available", "reason", "security"),
    [
        (None, False, "not_configured", None),
        ("http://peer.example.test:8000", False, "invalid_configuration", None),
        ("http://127.0.0.1:8000", True, None, "loopback"),
        ("https://peer.example.test", True, None, "tls"),
    ],
)
def test_self_advertised_endpoint_is_explicit_and_validated(
    monkeypatch, configured, available, reason, security
):
    from gateway.hosted_room_peer import local_catalog_mapping

    if configured is None:
        monkeypatch.delenv("HERMES_ROOM_LINK_URL", raising=False)
    else:
        monkeypatch.setenv("HERMES_ROOM_LINK_URL", configured)
    endpoint = local_catalog_mapping(installation_id="install-peer")["endpoint"]
    assert endpoint["available"] is available
    if reason is not None:
        assert endpoint == {"available": False, "reason": reason}
    else:
        assert endpoint["transport_security"] == security
