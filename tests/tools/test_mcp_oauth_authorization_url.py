"""Authorization URL compatibility tests for MCP OAuth providers."""

from urllib.parse import parse_qs, urlsplit

import pytest
from mcp.shared.auth import (
    OAuthClientInformationFull,
    OAuthClientMetadata,
    OAuthMetadata,
)
from pydantic import AnyUrl

from tools.mcp_oauth import HermesTokenStorage
from tools.mcp_oauth_manager import _HERMES_PROVIDER_CLS


class _AuthorizationURLCaptured(Exception):
    pass


async def _capture_authorization_url(tmp_path, authorization_endpoint: str) -> str:
    captured: list[str] = []

    async def redirect_handler(url: str) -> None:
        captured.append(url)
        raise _AuthorizationURLCaptured

    async def callback_handler():  # pragma: no cover - redirect stops first
        raise AssertionError("callback must not run")

    provider = _HERMES_PROVIDER_CLS(
        server_name="railway",
        server_url="https://mcp.railway.com",
        client_metadata=OAuthClientMetadata(
            redirect_uris=[AnyUrl("http://127.0.0.1:12345/callback")],
            client_name="Hermes Agent",
        ),
        storage=HermesTokenStorage("railway", hermes_home=tmp_path),
        redirect_handler=redirect_handler,
        callback_handler=callback_handler,
    )
    provider.context.client_info = OAuthClientInformationFull(
        client_id="test-client",
        redirect_uris=[AnyUrl("http://127.0.0.1:12345/callback")],
        grant_types=["authorization_code", "refresh_token"],
        response_types=["code"],
        token_endpoint_auth_method="none",
    )
    provider.context.oauth_metadata = OAuthMetadata.model_validate(
        {
            "issuer": "https://backboard.railway.com",
            "authorization_endpoint": authorization_endpoint,
            "token_endpoint": "https://backboard.railway.com/oauth/token",
            "response_types_supported": ["code"],
        }
    )
    provider.context.protocol_version = "2025-06-18"

    with pytest.raises(_AuthorizationURLCaptured):
        await provider._perform_authorization_code_grant()

    return captured[0]


@pytest.mark.asyncio
async def test_authorization_url_joins_existing_query_and_deduplicates_resource(
    tmp_path,
):
    url = await _capture_authorization_url(
        tmp_path,
        "https://backboard.railway.com/oauth/auth?"
        "resource=https%3A%2F%2Fmcp.railway.com",
    )

    parsed = urlsplit(url)
    params = parse_qs(parsed.query)
    assert params["response_type"] == ["code"]
    assert params["resource"] == ["https://mcp.railway.com"]
    assert parsed.fragment == ""


@pytest.mark.asyncio
async def test_authorization_url_preserves_existing_encoding_and_fragment(tmp_path):
    url = await _capture_authorization_url(
        tmp_path,
        "https://backboard.railway.com/oauth/auth?"
        "audience=hello%20world&opaque=a%2Bb#consent",
    )

    parsed = urlsplit(url)
    assert parsed.query.startswith("audience=hello%20world&opaque=a%2Bb&")
    assert parse_qs(parsed.query)["response_type"] == ["code"]
    assert parsed.fragment == "consent"


@pytest.mark.asyncio
async def test_authorization_url_preserves_distinct_resource_parameters(tmp_path):
    url = await _capture_authorization_url(
        tmp_path,
        "https://backboard.railway.com/oauth/auth?"
        "resource=https%3A%2F%2Fapi.railway.com",
    )

    assert parse_qs(urlsplit(url).query)["resource"] == [
        "https://api.railway.com",
        "https://mcp.railway.com",
    ]


@pytest.mark.asyncio
async def test_authorization_url_without_existing_query_is_unchanged(tmp_path):
    url = await _capture_authorization_url(
        tmp_path,
        "https://backboard.railway.com/oauth/auth",
    )

    parsed = urlsplit(url)
    params = parse_qs(parsed.query)
    assert params["response_type"] == ["code"]
    assert params["resource"] == ["https://mcp.railway.com"]
