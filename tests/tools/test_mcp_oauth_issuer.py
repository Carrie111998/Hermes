import asyncio
from urllib.parse import quote
from urllib.request import urlopen

import pytest

from mcp.client.auth.oauth2 import OAuthFlowError
from mcp.client.auth.utils import (
    credentials_match_issuer,
    validate_authorization_response_iss,
)
from mcp.shared.auth import (
    OAuthClientInformationFull,
    OAuthMetadata,
    OAuthToken,
)


def _metadata(issuer: str, *, response_iss: bool = True) -> OAuthMetadata:
    return OAuthMetadata.model_validate(
        {
            "issuer": issuer,
            "authorization_endpoint": f"{issuer}/authorize",
            "token_endpoint": f"{issuer}/token",
            "response_types_supported": ["code"],
            "authorization_response_iss_parameter_supported": response_iss,
        }
    )


def _client_info(issuer: str, client_id: str) -> OAuthClientInformationFull:
    return OAuthClientInformationFull.model_validate(
        {
            "client_id": client_id,
            "redirect_uris": ["https://agent.example/oauth/callback"],
            "issuer": issuer,
        }
    )


def _flow(*, flow_id: str = "issuer-flow"):
    from tools.mcp_dashboard_oauth import DashboardOAuthFlow

    return DashboardOAuthFlow(
        flow_id=flow_id,
        server_name="reports",
        profile=None,
        hermes_home="/tmp/hermes-test",
        redirect_uri="https://agent.example/api/mcp/oauth/callback/reports",
    )


def test_dashboard_callback_preserves_expected_issuer_for_sdk_validation():
    from tools.mcp_dashboard_oauth import dashboard_oauth_flow
    from tools.mcp_oauth import _make_callback_waiter

    issuer = "https://auth.example/tenant-a"
    flow = _flow()
    asyncio.run(flow.publish_authorization_url("https://auth.example/authorize?state=expected"))

    with dashboard_oauth_flow(flow):
        flow.deliver_callback(
            code="code",
            state="expected",
            error=None,
            iss=issuer,
        )
        result = asyncio.run(_make_callback_waiter(0)())

    assert result.iss == issuer
    validate_authorization_response_iss(result.iss, _metadata(issuer))


def test_sdk_rejects_missing_issuer_when_metadata_requires_it():
    from tools.mcp_oauth import _authorization_code_result

    result = _authorization_code_result("code", "state")

    with pytest.raises(OAuthFlowError, match="missing iss"):
        validate_authorization_response_iss(
            result.iss,
            _metadata("https://auth.example/tenant-a"),
        )


def test_sdk_rejects_wrong_or_normalised_issuer():
    from tools.mcp_oauth import _authorization_code_result

    expected = "https://auth.example/tenant-a"
    result = _authorization_code_result("code", "state", f"{expected}/")

    with pytest.raises(OAuthFlowError, match="iss mismatch"):
        validate_authorization_response_iss(result.iss, _metadata(expected))


@pytest.mark.asyncio
async def test_two_issuers_on_same_resource_origin_keep_distinct_credentials(
    tmp_path,
    monkeypatch,
):
    from tools.mcp_oauth import HermesTokenStorage
    from tools.mcp_oauth_manager import MCPOAuthManager

    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    resource_url = "https://resource.example/mcp"
    issuer_a = "https://auth.example/tenant-a"
    issuer_b = "https://auth.example/tenant-b"

    for name, issuer, token in (
        ("tenant-a", issuer_a, "token-a"),
        ("tenant-b", issuer_b, "token-b"),
    ):
        storage = HermesTokenStorage(name)
        await storage.set_client_info(_client_info(issuer, f"client-{name}"))
        await storage.set_tokens(
            OAuthToken(
                access_token=token,
                token_type="Bearer",
                refresh_token=f"refresh-{name}",
                expires_in=3600,
            )
        )
        storage.save_oauth_metadata(_metadata(issuer))

    manager = MCPOAuthManager()
    provider_a = manager.get_or_build_provider("tenant-a", resource_url, {})
    provider_b = manager.get_or_build_provider("tenant-b", resource_url, {})
    await provider_a._initialize()
    await provider_b._initialize()

    assert provider_a is not provider_b
    assert provider_a.context.current_tokens.access_token == "token-a"
    assert provider_b.context.current_tokens.access_token == "token-b"
    assert provider_a.context.client_info.issuer == issuer_a
    assert provider_b.context.client_info.issuer == issuer_b
    assert credentials_match_issuer(provider_a.context.client_info, issuer_a, None)
    assert not credentials_match_issuer(provider_a.context.client_info, issuer_b, None)


@pytest.mark.asyncio
async def test_issuer_binding_survives_provider_reconnect(tmp_path, monkeypatch):
    from tools.mcp_oauth import HermesTokenStorage
    from tools.mcp_oauth_manager import MCPOAuthManager

    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    issuer = "https://auth.example/tenant-a"
    storage = HermesTokenStorage("reports")
    await storage.set_client_info(_client_info(issuer, "client-reports"))
    await storage.set_tokens(
        OAuthToken(
            access_token="token",
            token_type="Bearer",
            refresh_token="refresh",
            expires_in=3600,
        )
    )
    storage.save_oauth_metadata(_metadata(issuer))

    manager = MCPOAuthManager()
    first = manager.get_or_build_provider(
        "reports",
        "https://resource.example/mcp",
        {},
    )
    await first._initialize()
    manager.evict("reports")
    reconnected = manager.get_or_build_provider(
        "reports",
        "https://resource.example/mcp",
        {},
    )
    await reconnected._initialize()

    assert reconnected is not first
    assert reconnected.context.client_info.issuer == issuer
    assert str(reconnected.context.oauth_metadata.issuer) == issuer
    assert reconnected.context.current_tokens.access_token == "token"


def test_dashboard_http_relay_preserves_issuer():
    from starlette.testclient import TestClient

    from hermes_cli import web_server

    issuer = "https://auth.example/tenant-a"
    flow = _flow(flow_id="dashboard-route")
    asyncio.run(flow.publish_authorization_url("https://auth.example/authorize?state=expected"))
    web_server._mcp_oauth_flows[flow.flow_id] = flow
    try:
        response = TestClient(web_server.app).get(
            "/api/mcp/oauth/callback/reports"
            f"?code=code&state=expected&iss={quote(issuer, safe='')}"
        )
        callback = asyncio.run(flow.wait_for_callback())
    finally:
        web_server._mcp_oauth_flows.pop(flow.flow_id, None)

    assert response.status_code == 200
    assert callback == ("code", "expected", issuer)


def test_tui_loopback_relay_preserves_issuer():
    from tui_gateway.mcp_oauth_sessions import _start_loopback_listener

    issuer = "https://auth.example/tenant-a"
    flow = _flow(flow_id="tui-route")
    asyncio.run(flow.publish_authorization_url("https://auth.example/authorize?state=expected"))
    server = _start_loopback_listener(flow)
    port = server.server_address[1]
    try:
        with urlopen(
            "http://127.0.0.1:"
            f"{port}/callback?code=code&state=expected&iss={quote(issuer, safe='')}",
            timeout=5,
        ) as response:
            assert response.status == 200
        callback = asyncio.run(flow.wait_for_callback())
    finally:
        server.shutdown()
        server.server_close()

    assert callback == ("code", "expected", issuer)
