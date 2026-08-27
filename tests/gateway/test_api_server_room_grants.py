"""Compatibility seams for the extracted RoomLink grant HTTP surface."""

import json
from unittest.mock import AsyncMock, MagicMock

import pytest

from gateway.platforms import api_server
from gateway.platforms import api_server_room_grants as room_grants


_HANDLER_DELEGATES = (
    ("_handle_room_member_invitation", "_handle_room_member_invitation"),
    ("_handle_room_member_capabilities", "_handle_room_member_capabilities"),
    ("_handle_room_member_grant_refresh", "_handle_room_member_grant_refresh"),
    ("_handle_room_member_grant_revoke", "_handle_room_member_grant_revoke"),
)


def test_api_server_keeps_room_grant_methods_on_the_adapter_class():
    expected = {
        "_room_grant_token",
        "_room_grant_secret",
        "_room_grant_claims",
        *(adapter_name for adapter_name, _ in _HANDLER_DELEGATES),
    }
    assert expected <= api_server.APIServerAdapter.__dict__.keys()


@pytest.mark.asyncio
@pytest.mark.parametrize(("adapter_name", "implementation_name"), _HANDLER_DELEGATES)
async def test_room_member_handlers_delegate_without_changing_method_surface(
    monkeypatch, adapter_name, implementation_name
):
    adapter = api_server.APIServerAdapter.__new__(api_server.APIServerAdapter)
    request = object()
    expected = object()
    implementation = AsyncMock(return_value=expected)
    monkeypatch.setattr(room_grants, implementation_name, implementation)

    assert await getattr(adapter, adapter_name)(request) is expected
    implementation.assert_awaited_once_with(
        adapter,
        request,
        _openai_error=api_server._openai_error,
        _api_request_profile=api_server._api_request_profile,
    )


def test_room_grant_helpers_delegate_through_legacy_adapter_methods(monkeypatch):
    adapter = api_server.APIServerAdapter.__new__(api_server.APIServerAdapter)
    request = object()
    token = MagicMock(return_value="grant-token")
    secret = MagicMock(return_value=b"secret")
    claims = MagicMock(return_value={"room_id": "room-1"})
    monkeypatch.setattr(room_grants, "_room_grant_token", token)
    monkeypatch.setattr(room_grants, "_room_grant_secret", secret)
    monkeypatch.setattr(room_grants, "_room_grant_claims", claims)

    assert adapter._room_grant_token(request) == "grant-token"
    assert adapter._room_grant_secret() == b"secret"
    assert adapter._room_grant_claims(request, permission="status") == {
        "room_id": "room-1"
    }
    token.assert_called_once_with(request)
    secret.assert_called_once_with(adapter)
    claims.assert_called_once_with(adapter, request, permission="status")


@pytest.mark.asyncio
async def test_capability_handler_uses_legacy_claims_monkeypatch(monkeypatch):
    from gateway import hosted_rooms

    adapter = api_server.APIServerAdapter.__new__(api_server.APIServerAdapter)
    request = object()
    claims = {
        "room_id": "room-1",
        "home_install_id": "install-home",
        "authority_gateway_id": "gateway-home",
        "authority_epoch": 3,
        "member_id": "member-reviewer",
        "target_install_id": "install-target",
        "target_profile": "worker",
    }
    adapter._room_grant_claims = MagicMock(return_value=claims)
    monkeypatch.setattr(
        hosted_rooms,
        "local_authority_gateway_id",
        lambda: "install-target",
    )
    profile_token = api_server._api_request_profile.set("worker")
    try:
        response = await adapter._handle_room_member_capabilities(request)
    finally:
        api_server._api_request_profile.reset(profile_token)

    body = json.loads(response.text)
    assert response.status == 200
    assert body["object"] == "hermes.room_member.capabilities"
    assert body["target_profile"] == "worker"
    adapter._room_grant_claims.assert_called_once_with(
        request,
        permission="status",
    )
