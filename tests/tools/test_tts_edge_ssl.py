"""Regression tests for custom CA handling in Edge TTS."""

import asyncio
import ssl
from unittest.mock import AsyncMock, MagicMock, patch


def test_edge_connector_overrides_dependency_request_context(monkeypatch):
    """The Hermes CA must win over edge-tts' explicit certifi context."""
    from tools import tts_tool

    hermes_context = ssl.create_default_context()
    dependency_context = ssl.create_default_context()
    monkeypatch.setattr(
        "agent.ssl_verify.resolve_httpx_verify",
        lambda: hermes_context,
    )

    async def check_connector():
        connector = tts_tool._edge_tts_connector()
        request = MagicMock()
        request.is_ssl.return_value = True
        request.ssl = dependency_context

        try:
            assert connector is not None
            assert connector._get_ssl_context(request) is hermes_context
        finally:
            await connector.close()

    asyncio.run(check_connector())


def test_edge_connector_preserves_dependency_default_without_custom_ca(monkeypatch):
    """No custom trust setting should leave edge-tts' defaults untouched."""
    from tools import tts_tool

    monkeypatch.setattr("agent.ssl_verify.resolve_httpx_verify", lambda: True)

    assert tts_tool._edge_tts_connector() is None


def test_generate_edge_tts_passes_custom_ca_connector(tmp_path):
    """The synthesis path must give the enforcing connector to edge-tts."""
    from tools.tts_tool import _generate_edge_tts

    connector = object()
    communicate = MagicMock()
    communicate.save = AsyncMock()
    edge_tts = MagicMock()
    edge_tts.Communicate.return_value = communicate

    with patch("tools.tts_tool._import_edge_tts", return_value=edge_tts), patch(
        "tools.tts_tool._edge_tts_connector", return_value=connector
    ):
        asyncio.run(_generate_edge_tts("Hello", str(tmp_path / "out.mp3"), {}))

    assert edge_tts.Communicate.call_args.kwargs["connector"] is connector
